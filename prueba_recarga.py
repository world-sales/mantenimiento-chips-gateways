"""Prueba de una recarga completa, paso a paso, con capturas en cada etapa.

Uso: python prueba_recarga.py <numero> <ip_gateway> <puerto> <carpeta_capturas>
"""
import re
import sys
import time
from pathlib import Path

from dotenv import dotenv_values
from playwright.sync_api import sync_playwright

NUMERO, IP, PUERTO, OUT = sys.argv[1], sys.argv[2], sys.argv[3], Path(sys.argv[4])
PUERTO_GW = re.compile(rf"^{PUERTO}[A-Z]$")  # la letra es el slot de SIM activo
ENV = dotenv_values(Path(__file__).with_name(".env"))
CLARO = "https://simple.claro.com.ar/inicio/auth/pin"
MONTO_TESTID = "VTC2M0-recargar"  # $2000

OUT.mkdir(parents=True, exist_ok=True)
paso_n = 0


def captura(page, nombre):
    global paso_n
    paso_n += 1
    base = OUT / f"{paso_n:02d}_{nombre}"
    page.screenshot(path=f"{base}.png", full_page=True)
    html = page.content()
    for k in ("NUMERO_TARJETA", "FECHA_VENCIMIENTO", "CODIGO_SEGURIDAD", "TITULAR_TARJETA", "DNI_TITULAR"):
        v = ENV.get(k) or ""
        for variante in {v, v.replace("/", ""), v[:20]}:
            if len(variante) >= 3:
                html = html.replace(variante, "***")
    html = re.sub(r'(<input[^>]*data-testid="(card-number|due-date|security-code|cardholder-name|document-number)-input"[^>]*value=")[^"]*', r"\1***", html)
    base.with_suffix(".html").write_text(html, encoding="utf-8")
    print(f"[{paso_n:02d}] {nombre} -> {page.url}", flush=True)


def leer_pins(gw, patron):
    """Devuelve [(fecha, pin)] de los SMS del puerto que matchean el patron."""
    gw.goto(f"https://{IP}/enSmsRecvNew.htm")
    if gw.locator("#loginname").count():
        gw.fill("#loginname", "admin")
        gw.fill("#loginpass", "admin")
        gw.click("#login_button")
        gw.wait_for_load_state("networkidle")
        gw.goto(f"https://{IP}/enSmsRecvNew.htm")
    gw.wait_for_load_state("networkidle")
    res = []
    for fila in gw.locator("tr").all():
        celdas = fila.locator("td")
        if celdas.count() != 5 or not PUERTO_GW.match(celdas.nth(0).inner_text().strip()):
            continue
        texto = celdas.nth(4).locator("textarea").input_value() if celdas.nth(4).locator("textarea").count() else celdas.nth(4).inner_text()
        if re.search(patron, texto):
            m = re.search(r"pin=(\d{4})", texto)
            res.append((celdas.nth(3).inner_text().strip(), m.group(1) if m else texto))
    return res


def esperar_pin_nuevo(gw, patron, previos, timeout=120):
    fin = time.time() + timeout
    while time.time() < fin:
        nuevos = [p for p in leer_pins(gw, patron) if p not in previos]
        if nuevos:
            nuevos.sort(reverse=True)
            print(f"    PIN recibido {nuevos[0]}", flush=True)
            return nuevos[0][1]
        time.sleep(5)
    raise TimeoutError(f"No llego SMS '{patron}' al puerto {PUERTO}")


PAT_LOGIN = r"para PACKS YA es pin="
PAT_PAGO = r"para realizar la recarga en Packs YA! es pin="
PAT_OK = r"Te acreditamos"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, slow_mo=150)
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 900})
    claro = ctx.new_page()
    gw = ctx.new_page()
    try:
        previos_login = leer_pins(gw, PAT_LOGIN)
        previos_pago = leer_pins(gw, PAT_PAGO)
        previos_ok = leer_pins(gw, PAT_OK)

        claro.bring_to_front()
        claro.goto(CLARO)
        claro.wait_for_selector("#number")
        captura(claro, "login")
        claro.fill("#number", NUMERO)
        claro.click("#pin__submit")
        claro.wait_for_selector("#pin", timeout=30000)
        captura(claro, "pide_pin")

        pin = esperar_pin_nuevo(gw, PAT_LOGIN, previos_login)
        claro.bring_to_front()
        claro.fill("#pin", pin)
        claro.click("#send")
        claro.wait_for_url(re.compile(r"simple\.claro\.com\.ar/inicio/?$"), timeout=30000)
        captura(claro, "inicio")

        claro.click("#link-to-recharge")
        claro.wait_for_selector("[data-testid=link-to-recharge-current-line]")
        captura(claro, "elegir_linea")
        linea = claro.locator(".box__milinea__data__number").inner_text().strip()
        print(f"    Mi linea muestra: {linea}", flush=True)
        if linea != NUMERO:
            raise RuntimeError(f"La linea logueada ({linea}) no es {NUMERO}")
        claro.click("[data-testid=link-to-recharge-current-line]")

        claro.wait_for_selector(f"[data-testid={MONTO_TESTID}]")
        captura(claro, "montos")
        claro.click(f"[data-testid={MONTO_TESTID}]")

        claro.get_by_text("Con tarjeta", exact=True).wait_for()
        captura(claro, "metodo_pago")
        claro.get_by_text("Con tarjeta", exact=True).click()

        claro.wait_for_selector("[data-testid=card-number-input]")
        captura(claro, "form_tarjeta")
        campos = [
            ("card-number-input", ENV["NUMERO_TARJETA"]),
            ("due-date-input", ENV["FECHA_VENCIMIENTO"].replace("/", "")),
            ("security-code-input", ENV["CODIGO_SEGURIDAD"]),
            ("cardholder-name-input", ENV["TITULAR_TARJETA"]),
            ("document-number-input", ENV["DNI_TITULAR"]),
        ]
        for testid, valor in campos:
            campo = claro.locator(f"[data-testid={testid}]")
            campo.click()
            campo.press_sequentially(valor, delay=40)
            print(f"    {testid}: {len(campo.input_value())} caracteres cargados", flush=True)
        # Oculta los datos de la tarjeta en la captura
        claro.add_style_tag(content="input{color:transparent!important}")
        captura(claro, "form_completo")

        claro.click("#confirm-payment-button")
        claro.wait_for_selector("[data-testid=pin-modal-input]", timeout=60000)
        captura(claro, "modal_pin_pago")

        pin2 = esperar_pin_nuevo(gw, PAT_PAGO, previos_pago)
        claro.bring_to_front()
        claro.fill("[data-testid=pin-modal-input]", pin2)
        claro.click("[data-testid=pin-modal-continuar]")
        claro.wait_for_selector("[data-testid=go-to-home-link]", timeout=90000)
        captura(claro, "resultado")
        print("    TEXTO RESULTADO:", claro.locator("body").inner_text()[:800].replace("\n", " | "), flush=True)

        try:
            esperar_pin_nuevo(gw, PAT_OK, previos_ok, timeout=90)
            print("OK: llego SMS de acreditacion", flush=True)
        except TimeoutError:
            print("AVISO: no llego SMS de acreditacion en 90s", flush=True)
    except Exception as e:
        print("ERROR:", repr(e), flush=True)
        captura(claro, "error")
    finally:
        browser.close()
