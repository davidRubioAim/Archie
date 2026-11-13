"""
campaign_loader.py (v0.58)
Lista de campañas (códigos de proyecto) desde la hoja AimProjectDatabase_LiveRollout, pestaña Projects.

Columnas que se usan (por nombre de cabecera, no por posición):
  "* Project ID"  -> código  (ADID-RMXX-261001)
  "Project Name"  -> nombre
  "Brand ID", "Channel ID"

Solo se muestran los códigos con formato correcto: BRAND(4)-CHANNEL(4)-NNNNNN (6 dígitos).
Los demás se cuentan como inválidos (los PMs deben corregirlos en la hoja). Los códigos repetidos
salen una sola vez, marcados como duplicados para revisarlos a mano.

Esa hoja está restringida al dominio de AIM, así que el export CSV anónimo no funciona. Fuentes, en orden:
  1. CAMPAIGN_SHEET_CSV_URL (Config/.env o la hoja de configuración ARCHIE API): URL de un CSV accesible,
     p. ej. una hoja espejo con =IMPORTRANGE(...) compartida por enlace, igual que la de estadios.
  2. CAMPAIGN_SHEET_ID / CAMPAIGN_SHEET_GID (por defecto la hoja original; solo funciona si es pública).
  3. Config/campaign_cache.json: última lista buena (o la importada a mano con "Import CSV").
"""
import csv
import io
import json
import os
import re
import time
import urllib.request

FETCH_TIMEOUT = 8
DEFAULT_SHEET_ID = "1pRgDZsOZDFyA-bhlPmkRP8Qg8rnAjIpyGwABsCQFOi8"   # AimProjectDatabase_LiveRollout
DEFAULT_SHEET_GID = "229016262"                                     # pestaña Projects
CACHE_FILE = "campaign_cache.json"
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

CODE_RE = re.compile(r"^[A-Z0-9]{4}-[A-Z0-9]{4}-\d{6}$")

# cabecera normalizada -> clave
COLUMNS = {
    "project id": "code",
    "project name": "name",
    "brand id": "brand",
    "channel id": "channel",
}


def _norm_header(text):
    return re.sub(r"[^a-z ]", "", re.sub(r"\s+", " ", str(text or "")).strip().lower()).strip()


def normalizar_codigo(text):
    """' adid_rmxx_261001 ' -> 'ADID-RMXX-261001' (las carpetas del NAS a veces usan _)."""
    return re.sub(r"[\s_]+", "-", str(text or "").strip().upper())


def codigo_valido(code):
    return bool(CODE_RE.match(code or ""))


def parse_rows(rows):
    """rows: lista de listas (CSV o API). -> (campañas, inválidos, duplicados)

    campañas: lista ordenada de {"code", "name", "brand", "channel", "names": [...], "duplicate": bool}
    inválidos: lista de códigos con formato incorrecto (tal cual vienen en la hoja)
    duplicados: {code: [nombres]}
    """
    header_idx, col = None, {}
    for i, row in enumerate(rows[:10]):
        found = {COLUMNS[_norm_header(c)]: j for j, c in enumerate(row) if _norm_header(c) in COLUMNS}
        if "code" in found:
            header_idx, col = i, found
            break
    if header_idx is None:
        raise ValueError("no se encuentra la columna 'Project ID' en la hoja de proyectos")

    def cell(row, key):
        j = col.get(key)
        return str(row[j]).strip() if j is not None and j < len(row) and row[j] is not None else ""

    by_code, invalid = {}, []
    for row in rows[header_idx + 1:]:
        raw = cell(row, "code")
        if not raw:
            continue
        code = raw.strip().upper()
        if not codigo_valido(code):
            invalid.append(raw)
            continue
        name = cell(row, "name")
        if code in by_code:
            c = by_code[code]
            c["duplicate"] = True
            c["names"].append(name)
            continue
        by_code[code] = {"code": code, "name": name, "brand": cell(row, "brand"), "channel": cell(row, "channel"),
                         "names": [name], "duplicate": False}

    campaigns = sorted(by_code.values(), key=lambda c: c["code"])
    duplicates = {c["code"]: c["names"] for c in campaigns if c["duplicate"]}
    return campaigns, invalid, duplicates


def _fetch_csv(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Archie", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8-sig")
    if "<html" in raw[:500].lower():
        raise PermissionError("la hoja no es accesible por enlace (pide inicio de sesión de Google)")
    return list(csv.reader(io.StringIO(raw)))


def _urls():
    urls = []
    direct = (os.getenv("CAMPAIGN_SHEET_CSV_URL") or "").strip()
    if direct:
        urls.append(direct)
    sheet_id = (os.getenv("CAMPAIGN_SHEET_ID") or DEFAULT_SHEET_ID).strip()
    gid = (os.getenv("CAMPAIGN_SHEET_GID") or DEFAULT_SHEET_GID).strip()
    if sheet_id:
        urls.append(SHEET_CSV_URL.format(sheet_id=sheet_id, gid=gid))
    return urls


def _cache_path(config_dir):
    return os.path.join(config_dir, CACHE_FILE)


def guardar_cache(config_dir, rows, source):
    try:
        with open(_cache_path(config_dir), "w", encoding="utf-8") as f:
            json.dump({"source": source, "saved_at": time.strftime("%Y-%m-%d %H:%M"), "rows": rows}, f,
                      ensure_ascii=False)
    except OSError:
        pass


def _leer_cache(config_dir):
    with open(_cache_path(config_dir), "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["rows"], data.get("saved_at", "?"), data.get("source", "cache")


def load_campaigns(config_dir, log=print):
    """-> (campañas, info). info = {"source", "detail", "invalid", "duplicates"}. Nunca lanza excepción."""
    errores = []
    for url in _urls():
        try:
            rows = _fetch_csv(url)
            campaigns, invalid, dups = parse_rows(rows)
            guardar_cache(config_dir, rows, "sheet")
            log(f"  [CAMPAÑAS] {len(campaigns)} códigos desde la hoja ({len(invalid)} con formato incorrecto).")
            return campaigns, {"source": "sheet", "detail": "Live sheet", "invalid": invalid, "duplicates": dups}
        except Exception as e:
            errores.append(str(e))
    try:
        rows, saved_at, origin = _leer_cache(config_dir)
        campaigns, invalid, dups = parse_rows(rows)
        detail = f"Offline copy ({'imported CSV' if origin == 'csv' else 'last sync'}: {saved_at})"
        log(f"  [CAMPAÑAS] Sin conexión con la hoja ({'; '.join(errores)[:160]}). "
            f"{len(campaigns)} códigos desde la copia local ({saved_at}).")
        return campaigns, {"source": "cache", "detail": detail, "invalid": invalid, "duplicates": dups}
    except Exception as e:
        log(f"  [CAMPAÑAS] Sin hoja ni copia local ({e}).")
        return [], {"source": "none", "detail": "No campaign list available", "invalid": [], "duplicates": {}}


def importar_csv(config_dir, path):
    """CSV descargado a mano de la pestaña Projects (Archivo > Descargar > CSV). Lo valida y lo deja como caché."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    campaigns, invalid, dups = parse_rows(rows)   # lanza ValueError si no es la pestaña correcta
    guardar_cache(config_dir, rows, "csv")
    return campaigns, {"source": "cache", "detail": f"Imported CSV ({time.strftime('%Y-%m-%d %H:%M')})",
                       "invalid": invalid, "duplicates": dups}
