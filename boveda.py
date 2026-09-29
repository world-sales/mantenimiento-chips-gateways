"""Guarda los datos de la tarjeta cifrados con DPAPI de Windows (atado al usuario de Windows y a esta PC).

El archivo tarjeta.dat no se puede leer con un editor ni descifrar desde otra PC u otro usuario.
Solo el programa, corriendo con este mismo usuario de Windows, lo descifra en memoria al momento de pagar.

Cargar o cambiar la tarjeta (pide los datos sin mostrarlos en pantalla):
    python boveda.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import sys
from getpass import getpass
from pathlib import Path

ARCHIVO = Path(__file__).with_name("tarjeta.dat")
CAMPOS = ("NUMERO_TARJETA", "FECHA_VENCIMIENTO", "CODIGO_SEGURIDAD", "TITULAR_TARJETA", "DNI_TITULAR")
# Entropia adicional: ademas del usuario de Windows, hace falta este valor para descifrar
_ENTROPIA = b"mantenimiento-chips-gateways/tarjeta/v1"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(datos):
    buf = ctypes.create_string_buffer(datos, len(datos))
    return _Blob(len(datos), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(funcion, datos):
    entrada, _b1 = _blob(datos)
    entropia, _b2 = _blob(_ENTROPIA)
    salida = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not funcion(ctypes.byref(entrada), None, ctypes.byref(entropia), None, None,
                   CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(salida)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(salida.pbData, salida.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(salida.pbData)


def _cifrar(datos):
    return _dpapi(lambda i, d, e, r, p, f, o: ctypes.windll.crypt32.CryptProtectData(i, "tarjeta", e, r, p, f, o), datos)


def _descifrar(datos):
    return _dpapi(lambda i, d, e, r, p, f, o: ctypes.windll.crypt32.CryptUnprotectData(i, None, e, r, p, f, o), datos)


def guardar(valores):
    faltan = [c for c in CAMPOS if not valores.get(c)]
    if faltan:
        raise ValueError(f"Faltan campos: {faltan}")
    ARCHIVO.write_bytes(_cifrar(json.dumps({c: valores[c] for c in CAMPOS}).encode("utf-8")))


def cargar():
    """Devuelve el dict con los datos de la tarjeta, descifrado solo en memoria."""
    if not ARCHIVO.exists():
        raise FileNotFoundError("No hay tarjeta guardada: correr 'python boveda.py' para cargarla")
    return json.loads(_descifrar(ARCHIVO.read_bytes()).decode("utf-8"))


if __name__ == "__main__":
    print("Carga de la tarjeta (lo que escribas no se muestra en pantalla)")
    valores = {
        "NUMERO_TARJETA": getpass("Numero de tarjeta (solo digitos): ").replace(" ", ""),
        "FECHA_VENCIMIENTO": getpass("Vencimiento (MM/AA): ").strip(),
        "CODIGO_SEGURIDAD": getpass("Codigo de seguridad: ").strip(),
        "TITULAR_TARJETA": getpass("Titular (como figura en la tarjeta): ").strip(),
        "DNI_TITULAR": getpass("DNI del titular: ").strip(),
    }
    try:
        guardar(valores)
    except ValueError as e:
        sys.exit(f"No se guardo: {e}")
    print(f"Tarjeta guardada cifrada en {ARCHIVO.name}")
