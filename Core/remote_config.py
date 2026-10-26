"""
remote_config.py
Lee claves de configuración (p.ej. GEMINI_API_KEY) desde una hoja de Google privada
compartida por enlace, para no guardarlas en GitHub ni configurarlas PC a PC.

Formato de la hoja (pestaña única): columna A = nombre, columna B = valor
    GEMINI_API_KEY | AIza....

Prioridad: variable de entorno / Config/.env  >  hoja remota.
"""
import csv
import io
import os
import urllib.request

CONFIG_SHEET_ID = "1NtfSsL-ZdVZEiE7kMaNk0BKyRZ9bNxrYrYLfFmS9Khk"  # hoja "ARCHIE API" (no la de estadios)
CONFIG_SHEET_GID = "0"
FETCH_TIMEOUT = 6


def load_remote_config(keys=("GEMINI_API_KEY",), log=print):
    pending = [k for k in keys if not os.getenv(k)]
    sheet_id = (os.getenv("CONFIG_SHEET_ID") or CONFIG_SHEET_ID).strip()
    if not pending or not sheet_id:
        return

    url = (f"https://docs.google.com/spreadsheets/d/{sheet_id}/export"
           f"?format=csv&gid={os.getenv('CONFIG_SHEET_GID') or CONFIG_SHEET_GID}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Archie", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
        if "<html" in raw[:500].lower():
            raise PermissionError("la hoja de configuración no es accesible por enlace")
        values = {r[0].strip(): r[1].strip() for r in csv.reader(io.StringIO(raw)) if len(r) >= 2}
    except Exception as e:
        log(f"  [WARN] No se pudo leer la configuración remota ({e}).")
        return

    for k in pending:
        if values.get(k):
            os.environ[k] = values[k]
        else:
            log(f"  [WARN] {k} no está en la hoja de configuración.")
