"""Front local para elegir lineas del Google Sheet y recargarlas.

Uso: python app.py  ->  abrir http://127.0.0.1:5000
"""
import json
import logging
import sys
import threading
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).parent
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)

# Con pythonw (arranque en segundo plano) no hay consola: stdout/stderr van a un archivo
if sys.stdout is None or sys.stderr is None:
    sys.stdout = sys.stderr = open(LOGS / "consola.log", "a", encoding="utf-8", buffering=1)

import gspread
from dotenv import dotenv_values
from flask import Flask, jsonify, request, send_file

from recarga import MONTOS, recargar

ENV = dotenv_values(BASE / ".env")
CREDENCIALES = next(BASE.glob("accesos-world-sales-*.json"))

# recargas.log: cada paso de cada recarga (legible). recargas.jsonl: una linea por recarga con el resultado final.
logger = logging.getLogger("recargas")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOGS / "recargas.log", maxBytes=5_000_000, backupCount=10, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
logger.addHandler(_handler)
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # no loguear el polling de /api/estado


def registrar_resultado(item):
    r = item["resultado"] or {}
    fila = {
        "fecha": datetime.now().isoformat(timespec="seconds"),
        **{k: item[k] for k in ("fila", "gateway", "puerto", "numero", "monto", "estado")},
        **{k: r.get(k) for k in ("transaccion", "acreditado", "saldo", "error", "sin_fondos", "error_sheet", "carpeta")},
    }
    with open(LOGS / "recargas.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(fila, ensure_ascii=False) + "\n")


app = Flask(__name__)
lock = threading.Lock()
trabajo = {"activo": False, "detener": False, "motivo_detencion": None, "items": []}


def hoja():
    return gspread.service_account(filename=str(CREDENCIALES)).open_by_url(ENV["url_hoja_mantenimientos_chips_gateway"]).sheet1


def sumar_saldo(fila, numero, monto):
    """Suma el monto al saldo de la fila y pone la fecha de hoy en consulta de saldo y ultimo pago,
    verificando antes que la fila siga siendo la del numero."""
    ws = hoja()
    encabezados = ws.row_values(1)
    col_numero, col_saldo = encabezados.index("numero") + 1, encabezados.index("saldo") + 1
    cols_fecha = [encabezados.index(c) + 1 for c in ("saldo-fecha-consulta", "ult_fecha_pago")]
    valores = ws.row_values(fila, value_render_option="UNFORMATTED_VALUE")
    actual_numero = str(valores[col_numero - 1]).strip() if len(valores) >= col_numero else ""
    if actual_numero != str(numero):
        raise RuntimeError(f"La fila {fila} ahora tiene el numero '{actual_numero}', no {numero}; saldo no actualizado")
    actual = valores[col_saldo - 1] if len(valores) >= col_saldo else ""
    try:
        anterior = float(actual)
    except (TypeError, ValueError):
        anterior = 0  # vacio o texto (ej. "falta") se toma como 0
    nuevo = anterior + monto
    nuevo = int(nuevo) if nuevo == int(nuevo) else nuevo
    hoy = datetime.now().strftime("%d/%m/%Y")  # USER_ENTERED: el sheet (es_ES) lo guarda como fecha
    celdas = [(col_saldo, nuevo)] + [(c, hoy) for c in cols_fecha]
    ws.batch_update(
        [{"range": gspread.utils.rowcol_to_a1(fila, c), "values": [[v]]} for c, v in celdas],
        value_input_option="USER_ENTERED",
    )
    return actual, nuevo


def leer_sheet():
    ws = hoja()
    lineas = []
    for i, r in enumerate(ws.get_all_records(), start=2):
        numero = str(r.get("numero", "")).strip()
        motivo = None
        if not numero:
            motivo = "Puerto sin numero"
        elif str(r.get("compania", "")).strip().lower() != "claro":
            motivo = f"Compania {r.get('compania') or '?'}"
        elif str(r.get("tipo", "")).strip().lower() != "prepago":
            motivo = f"Tipo {r.get('tipo') or '?'}"
        lineas.append({
            "fila": i,
            "gateway": str(r.get("gateway", "")),
            "ip": str(r.get("IP", "")).strip(),
            "puerto": str(r.get("puerto", "")).strip(),
            "numero": numero,
            "status": str(r.get("status", "")),
            "compania": str(r.get("compania", "")),
            "tipo": str(r.get("tipo", "")),
            "saldo": r.get("saldo", ""),
            "saldo_fecha": str(r.get("saldo-fecha-consulta", "")),
            "ult_pago": str(r.get("ult_fecha_pago", "")),
            "recargable": motivo is None,
            "motivo": motivo,
        })
    return lineas


def worker():
    logger.info("=== Tanda iniciada: %d lineas: %s", len(trabajo["items"]),
                ", ".join(f"{i['numero']}(GW{i['gateway']} p{i['puerto']} ${i['monto']})" for i in trabajo["items"]))
    for item in trabajo["items"]:
        if trabajo["detener"]:
            item["estado"] = "cancelada"
            item["resultado"] = {"error": trabajo.get("motivo_detencion") or "Detenida por el usuario"}
            logger.info("[%s] cancelada: %s", item["numero"], item["resultado"]["error"])
            registrar_resultado(item)
            continue
        item["estado"] = "en curso"
        item["inicio"] = datetime.now().strftime("%H:%M:%S")

        def log(msg, item=item):
            item["log"].append(f"{datetime.now():%H:%M:%S} {msg}")
            nivel = logging.ERROR if "ERROR" in msg else logging.INFO
            logger.log(nivel, "[%s GW%s p%s] %s", item["numero"], item["gateway"], item["puerto"], msg)

        try:
            res = recargar(item["numero"], item["ip"], item["puerto"], item["monto"], log=log)
        except Exception as e:
            logger.exception("[%s] excepcion no controlada", item["numero"])
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            try:
                anterior, nuevo = sumar_saldo(item["fila"], item["numero"], item["monto"])
                res["saldo"] = nuevo
                log(f"Sheet actualizado: saldo {anterior!r} -> {nuevo}, fechas de consulta y pago = hoy")
            except Exception as e:
                res["error_sheet"] = str(e)
                log(f"ERROR al actualizar el Sheet: {e}")
        item.update(resultado=res, estado="ok" if res.get("ok") else "error")
        registrar_resultado(item)
        if res.get("sin_fondos"):
            trabajo.update(detener=True, motivo_detencion="No se intento: la tarjeta fue rechazada por falta de fondos")
            logger.error("Tanda detenida: fondos insuficientes en la tarjeta")
    logger.info("=== Tanda terminada: %s", ", ".join(f"{i['numero']}={i['estado']}" for i in trabajo["items"]))
    trabajo["activo"] = False


@app.get("/")
def index():
    return send_file(BASE / "static" / "index.html")


@app.get("/api/lineas")
def api_lineas():
    try:
        return jsonify({"lineas": leer_sheet(), "montos": sorted(MONTOS)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/recargar")
def api_recargar():
    datos = request.get_json()
    monto = int(datos.get("monto", 2000))
    if monto not in MONTOS:
        return jsonify({"error": "Monto invalido"}), 400
    with lock:
        if trabajo["activo"]:
            return jsonify({"error": "Ya hay recargas en curso"}), 409
        lineas = {l["fila"]: l for l in leer_sheet()}
        items = []
        for fila in datos.get("filas", []):
            l = lineas.get(int(fila))
            if not l or not l["recargable"]:
                return jsonify({"error": f"La fila {fila} no es recargable"}), 400
            items.append({**{k: l[k] for k in ("fila", "gateway", "ip", "puerto", "numero")},
                          "monto": monto, "estado": "pendiente", "log": [], "resultado": None})
        if not items:
            return jsonify({"error": "No se selecciono ninguna linea"}), 400
        trabajo.update(activo=True, detener=False, motivo_detencion=None, items=items)
        threading.Thread(target=worker, daemon=True).start()
    return jsonify({"ok": True})


@app.post("/api/detener")
def api_detener():
    trabajo["detener"] = True
    return jsonify({"ok": True})


@app.get("/api/estado")
def api_estado():
    return jsonify(trabajo)


if __name__ == "__main__":
    logger.info("Servidor iniciado")
    app.run(host="127.0.0.1", port=5000, debug=False)
