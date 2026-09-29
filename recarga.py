"""Recarga de un chip Claro via simple.claro.com.ar, leyendo los PIN desde el gateway DINSTAR.

Uso manual: python recarga.py <numero> <ip_gateway> <puerto> [monto]
"""
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

import boveda

BASE = Path(__file__).parent
CAPTURAS = BASE / "capturas"
CLARO = "https://simple.claro.com.ar/inicio/auth/pin"
MONTOS = {2000: "VTC2M0", 3000: "VTC3M0", 5000: "VTC5M0", 8000: "VTC8M0"}
GW_USER, GW_PASS = "admin", "admin"

PAT_LOGIN = r"para PACKS YA es pin="
PAT_PAGO = r"para realizar la recarga en Packs YA! es pin="
PAT_OK = r"Te acreditamos"
CAMPOS_TARJETA = ("NUMERO_TARJETA", "FECHA_VENCIMIENTO", "CODIGO_SEGURIDAD", "TITULAR_TARJETA", "DNI_TITULAR")


class Gateway:
    """Lee el SMS Inbox del DINSTAR filtrando por puerto (la letra del puerto es el slot de SIM activo)."""

    def __init__(self, page, ip, puerto):
        self.page, self.ip = page, ip
        # GW36 muestra el puerto con la letra del slot de SIM ("1B"), GW37 sin letra ("19")
        self.puerto = re.compile(rf"^{puerto}[A-Z]?$")
        self.puerto_n = puerto

    def mensajes(self, patron):
        pg = self.page
        pg.goto(f"https://{self.ip}/enSmsRecvNew.htm")
        if pg.locator("#loginname").count():
            pg.fill("#loginname", GW_USER)
            pg.fill("#loginpass", GW_PASS)
            pg.click("#login_button")
            pg.wait_for_load_state("networkidle")
            pg.goto(f"https://{self.ip}/enSmsRecvNew.htm")
        pg.wait_for_load_state("networkidle")
        res = []
        for fila in pg.locator("tr").all():
            celdas = fila.locator("td")
            if celdas.count() != 5 or not self.puerto.match(celdas.nth(0).inner_text().strip()):
                continue
            cont = celdas.nth(4)
            texto = cont.locator("textarea").input_value() if cont.locator("textarea").count() else cont.inner_text()
            if re.search(patron, texto):
                res.append((celdas.nth(3).inner_text().strip(), texto))
        return res

    def esperar_nuevo(self, patron, previos, timeout=120):
        fin = time.time() + timeout
        while time.time() < fin:
            nuevos = sorted((m for m in self.mensajes(patron) if m not in previos), reverse=True)
            if nuevos:
                return nuevos[0][1]
            time.sleep(5)
        raise TimeoutError(f"No llego el SMS esperado al puerto {self.puerto_n} en {timeout}s")


class FondosInsuficientes(RuntimeError):
    """La tarjeta no tiene fondos: no tiene sentido seguir con el resto de la tanda."""


# Mensajes de rechazo que puede mostrar Claro durante el pago (se buscan en el texto visible, sin mayusculas)
RECHAZOS = [
    (r"fondos? insuficientes?|saldo insuficiente|sin fondos", "fondos"),
    (r"tu tarjeta fue rechazada|pago (fue )?rechazado|rechazamos", "rechazo"),
    (r"no pudimos (procesar|realizar|completar)|no se pudo (procesar|realizar|completar)", "rechazo"),
]


def _esperar_o_rechazo(page, selector, timeout, carpeta=None, volver_inicio_es_error=False, rechazo_es_fondos=False):
    """Espera el selector. Si Claro muestra un rechazo (o vuelve al inicio sin confirmar) corta con un error claro."""
    fin = time.time() + timeout / 1000
    while True:
        if page.locator(selector).count() and page.locator(selector).first.is_visible():
            return
        try:
            texto = page.locator("body").inner_text(timeout=2000)
        except Exception:
            texto = ""
        if carpeta and texto and texto != getattr(page, "_ultimo_texto", None):
            # Registro de cada pantalla distinta que muestra Claro, por si un mensaje aparece y desaparece rapido
            page._ultimo_texto = texto
            with open(carpeta / "pantallas.txt", "a", encoding="utf-8") as f:
                f.write(f"===== {datetime.now():%H:%M:%S} {page.url}\n{texto.strip()[:3000]}\n\n")
        for patron, tipo in RECHAZOS:
            m = re.search(patron, texto, re.I)
            if m:
                if carpeta:
                    _captura(page, carpeta, "rechazo")
                linea = next((l.strip() for l in texto.splitlines() if m.group(0).lower() in l.lower()), m.group(0))
                if tipo == "fondos":
                    raise FondosInsuficientes(f"Fondos insuficientes en la tarjeta (no se cobro). Claro: '{linea}'")
                if rechazo_es_fondos:
                    # Claro no detalla el motivo; si rechaza despues del PIN de pago es el banco (normalmente sin fondos)
                    raise FondosInsuficientes(f"Tarjeta rechazada al confirmar el pago, probablemente sin fondos (no se cobro). Claro: '{linea}'")
                raise RuntimeError(f"Claro rechazo el pago (no se cobro). Claro: '{linea}'")
        if volver_inicio_es_error and page.locator("[data-testid=home-page]").count():
            if carpeta:
                _captura(page, carpeta, "volvio_al_inicio")
            raise RuntimeError("El pago no se confirmo: Claro volvio al inicio sin mostrar 'Recibimos tu pago'")
        if time.time() > fin:
            raise TimeoutError(f"Claro no respondio en {timeout // 1000}s esperando {selector}")
        time.sleep(1)


def _pin(texto):
    return re.search(r"pin=(\d{4})", texto).group(1)


def _captura(page, carpeta, nombre):
    """Screenshot + HTML con los datos de la tarjeta enmascarados."""
    try:
        page.screenshot(path=str(carpeta / f"{nombre}.png"), full_page=True)
        html = page.content()
        try:
            tarjeta = boveda.cargar()
        except Exception:
            tarjeta = {}
        for k in CAMPOS_TARJETA:
            v = tarjeta.get(k) or ""
            for variante in {v, v.replace("/", ""), v[:20]}:
                if len(variante) >= 3:
                    html = html.replace(variante, "***")
        (carpeta / f"{nombre}.html").write_text(html, encoding="utf-8")
    except Exception:
        pass


def recargar(numero, ip, puerto, monto=2000, log=print, headless=False):
    """Hace una recarga completa. Devuelve dict con ok, transaccion, acreditado, error, carpeta."""
    numero, puerto = str(numero), str(puerto)
    if monto not in MONTOS:
        raise ValueError(f"Monto {monto} no soportado; opciones: {sorted(MONTOS)}")
    carpeta = CAPTURAS / f"{datetime.now():%Y%m%d_%H%M%S}_{numero}"
    carpeta.mkdir(parents=True, exist_ok=True)
    res = {"ok": False, "transaccion": None, "acreditado": False, "error": None, "carpeta": str(carpeta)}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=100)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
        claro, gw = ctx.new_page(), Gateway(ctx.new_page(), ip, puerto)
        try:
            log("Leyendo SMS previos del gateway")
            previos = {pat: gw.mensajes(pat) for pat in (PAT_LOGIN, PAT_PAGO, PAT_OK)}

            log("Pidiendo codigo de ingreso a Claro")
            claro.bring_to_front()
            claro.goto(CLARO)
            claro.fill("#number", numero)
            claro.click("#pin__submit")
            claro.wait_for_selector("#pin", timeout=30000)

            log("Esperando PIN de ingreso en el gateway")
            claro.fill("#pin", _pin(gw.esperar_nuevo(PAT_LOGIN, previos[PAT_LOGIN])))
            claro.click("#send")
            claro.wait_for_url(re.compile(r"simple\.claro\.com\.ar/inicio/?$"), timeout=30000)

            log("Ingreso OK, eligiendo linea")
            claro.click("#link-to-recharge")
            claro.wait_for_selector("[data-testid=link-to-recharge-current-line]")
            linea = claro.locator(".box__milinea__data__number").inner_text().strip()
            if linea != numero:
                raise RuntimeError(f"La linea logueada ({linea}) no es {numero}")
            claro.click("[data-testid=link-to-recharge-current-line]")

            log(f"Eligiendo monto ${monto}")
            claro.click(f"[data-testid={MONTOS[monto]}-recargar]")
            claro.get_by_text("Con tarjeta", exact=True).click()

            log("Cargando datos de la tarjeta")
            tarjeta = boveda.cargar()
            _esperar_o_rechazo(claro, "[data-testid=card-number-input]", 30000, carpeta)
            campos = [
                ("card-number-input", tarjeta["NUMERO_TARJETA"]),
                ("due-date-input", tarjeta["FECHA_VENCIMIENTO"].replace("/", "")),
                ("security-code-input", tarjeta["CODIGO_SEGURIDAD"]),
                ("cardholder-name-input", tarjeta["TITULAR_TARJETA"]),
                ("document-number-input", tarjeta["DNI_TITULAR"]),
            ]
            for testid, valor in campos:
                campo = claro.locator(f"[data-testid={testid}]")
                campo.click()
                campo.press_sequentially(valor, delay=40)
            claro.add_style_tag(content="input{color:transparent!important}")

            log("Pagando")
            claro.click("#confirm-payment-button")
            _esperar_o_rechazo(claro, "[data-testid=pin-modal-input]", 60000, carpeta)

            log("Esperando PIN de pago en el gateway")
            claro.fill("[data-testid=pin-modal-input]", _pin(gw.esperar_nuevo(PAT_PAGO, previos[PAT_PAGO])))
            claro.click("[data-testid=pin-modal-continuar]")
            log("PIN de pago enviado, esperando confirmacion de Claro")
            _esperar_o_rechazo(claro, "text=Recibimos tu pago", 90000, carpeta, volver_inicio_es_error=True, rechazo_es_fondos=True)
            m = re.search(r"PRT-[\w-]+", claro.locator("body").inner_text())
            res.update(ok=True, transaccion=m.group(0) if m else None)
            _captura(claro, carpeta, "resultado")
            log(f"Pago recibido. Transaccion {res['transaccion']}")

            log("Esperando SMS de acreditacion")
            try:
                gw.esperar_nuevo(PAT_OK, previos[PAT_OK], timeout=90)
                res["acreditado"] = True
                log("Acreditacion confirmada por SMS")
            except TimeoutError:
                log("No llego el SMS de acreditacion en 90s (el pago si se hizo)")
        except Exception as e:
            res["error"] = str(e).splitlines()[0]
            res["sin_fondos"] = isinstance(e, FondosInsuficientes)
            log(f"ERROR: {res['error']}")
            _captura(claro, carpeta, "error")
        finally:
            browser.close()
    return res


if __name__ == "__main__":
    print(recargar(sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 2000))
