"""
stadium_loader.py
Carga la librería de estadios desde la hoja máster de Google Sheets (Atlas Venue Specs)
y la devuelve con la MISMA estructura que stadium_library.json, para que app.py,
batch_manager.py y queue_manager.py no necesiten cambios.

Orden de fuentes:
  1. Google Sheets (export CSV)          -> si OK, se guarda en caché
  2. Config/stadium_cache.json           -> última descarga buena (modo offline)
  3. Config/stadium_library.json         -> JSON legacy, por si acaso
  4. Perfil "Custom" mínimo hardcodeado

Configuración: usa DEFAULT_SHEET_ID / DEFAULT_SHEET_GID; se pueden sobrescribir en Config/.env
  STADIUM_SHEET_ID=<id de la URL: docs.google.com/spreadsheets/d/<ID>/edit>
  STADIUM_SHEET_GID=<gid de la pestaña: ...#gid=<GID>>
"""
import csv
import io
import json
import os
import re
import urllib.request

SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
FETCH_TIMEOUT = 6
DEFAULT_SHEET_ID = "15hfgbbpTAd06OEmSR6Zpad3-xjgN_CiSuLtnS5-ExL0"  # AIM Sport - Atlas Venue Specs
DEFAULT_SHEET_GID = "1272902361"   # pestaña MASTER ATLAS

# Columna de la hoja (normalizada) -> clave que espera la app
COLUMN_MAP = {
    "perimeter width": "Width",
    "perimeter height": "Height",
    "goal line width": "Goal Line Size",
    "centerboard width": "Center Board Width",
    "canvas width": "Canvas Width",
    "canvas height": "Canvas Height",
    "canvas mask": "Canvas Mask",
    "offset x": "Offset X",
    "offset y": "Offset Y",
    "fps": "FPS",
    "competition": "Competition",  # aún no existe en la hoja; se usa si se añade
}
NAME_COLUMN = "club name"

DEFAULTS = {
    "Competition": "NONE",
    "Width": 0, "Height": 0, "Goal Line Size": 0, "Center Board Width": 0,
    "Canvas Width": 0, "Canvas Height": 0, "Canvas Mask": 0,  # canvas obligatorio desde la hoja
    "Offset X": 0, "Offset Y": 0, "FPS": 60,
}
REQUIRED = ("Width", "Height", "Canvas Width", "Canvas Height")

CUSTOM_PROFILE = {
    "Competition": "NONE", "Width": 39456, "Height": 144, "Goal Line Size": 11232,
    "Center Board Width": 1920, "Canvas Width": 3840, "Canvas Height": 2160,
    "Canvas Mask": 96, "Offset X": 0, "Offset Y": 0, "FPS": 60,
}


def _norm(text):
    """'Goal Line\\nWidth ' -> 'goal line width'"""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _clean_name(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _to_int(value):
    """'39,456' / '39456' / '-' / '' -> int | None"""
    s = str(value or "").strip().replace(",", "").replace(".", "")
    if not s or s == "-":
        return None
    return int(s) if s.isdigit() else None


def _infer_competition(name):
    """
    La hoja no tiene columna Competition: se deduce del nombre.
      - Sigla explícita en el nombre: UWCL / UECL / UEL / UCL  (p.ej. 'Ajax - Arena (UEL)')
      - '... UEFA Women'  -> UWCL
      - '... (UEFA)'      -> UCL
      - resto             -> NONE
    """
    upper = name.upper()
    for tag in ("UWCL", "UECL", "UEL", "UCL"):
        if re.search(rf"\b{tag}\b", upper):
            return tag
    if "UEFA WOMEN" in upper:
        return "UWCL"
    if re.search(r"\bUEFA\b", upper):
        return "UCL"
    return "NONE"


def parse_sheet_rows(rows):
    """
    rows: lista de listas (CSV ya leído). Detecta la fila de cabecera buscando 'Club Name'
    (la hoja tiene filas de título encima). Devuelve (stadiums: dict, warnings: list[str]).
    """
    header_idx = next((i for i, r in enumerate(rows) if NAME_COLUMN in (_norm(c) for c in r)), None)
    if header_idx is None:
        raise ValueError("No se encontró la columna 'Club Name' en la hoja.")

    headers = [_norm(c) for c in rows[header_idx]]
    name_col = headers.index(NAME_COLUMN)
    col_for_key = {COLUMN_MAP[h]: i for i, h in enumerate(headers) if h in COLUMN_MAP}

    missing = [k for k in REQUIRED if k not in col_for_key]
    if missing:
        raise ValueError(f"Faltan columnas obligatorias en la hoja: {missing}")

    stadiums, warnings = {}, []
    for line_no, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        name = _clean_name(row[name_col] if name_col < len(row) else "")
        if not name:
            continue

        profile = dict(DEFAULTS)
        for key, col in col_for_key.items():
            raw = row[col] if col < len(row) else ""
            if key == "Competition":
                if _clean_name(raw):
                    profile[key] = _clean_name(raw).upper()
                continue
            val = _to_int(raw)
            if val is not None:
                profile[key] = val
        if "Competition" not in col_for_key:
            profile["Competition"] = _infer_competition(name)

        if not any(_to_int(c) for c in row):
            continue  # fila plantilla/vacía (p.ej. 'NUEVO ESTADIO')
        bad = [k for k in REQUIRED if not profile[k]]
        if bad:
            warnings.append(f"{name}: falta {', '.join(bad)} (fila {line_no})")
            continue
        if profile["Canvas Height"] > 4320 or profile["Canvas Width"] > 8192:
            warnings.append(f"{name}: canvas no valido "
                            f"{profile['Canvas Width']}x{profile['Canvas Height']} (fila {line_no})")
            continue
        if name in stadiums:
            warnings.append(f"{name}: nombre duplicado, se usa la primera fila (fila {line_no})")
            continue
        stadiums[name] = profile

    return stadiums, warnings


def fetch_sheet_rows(sheet_id, gid="0"):
    url = SHEET_CSV_URL.format(sheet_id=sheet_id, gid=gid)
    req = urllib.request.Request(url, headers={"User-Agent": "Archie", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    if raw.lstrip().lower().startswith("<!doctype html") or "<html" in raw[:500].lower():
        # Google devuelve la página de login si la hoja no es accesible por enlace
        raise PermissionError("La hoja no es accesible (¿compartida como 'Cualquiera con el enlace'?).")
    return list(csv.reader(io.StringIO(raw)))


def _with_custom(stadiums):
    return {"Custom": dict(CUSTOM_PROFILE), **{k: v for k, v in stadiums.items() if k != "Custom"}}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _log_summary(log, stadiums, warnings, previous):
    names = list(stadiums.keys())
    log(f"  [ESTADIOS] {len(names)} cargados desde Google Sheets.")
    if previous:
        nuevos = [n for n in names if n not in previous]
        quitados = [n for n in previous if n not in stadiums and n != "Custom"]
        if nuevos:
            log("             Nuevos: " + ", ".join(nuevos))
        if quitados:
            log("             Ya no estan en la hoja: " + ", ".join(quitados))
    if warnings:
        log(f"  [ESTADIOS] {len(warnings)} omitidos por datos incompletos:")
        for w in warnings:
            log(f"             - {w}")


def load_stadium_library(config_dir, log=print, fallback_dirs=()):
    """
    Devuelve (stadium_data, source) donde source ∈ {'sheets', 'cache', 'json', 'default'}.
    stadium_data mantiene el formato de stadium_library.json, con 'Custom' siempre primero.
    fallback_dirs: carpetas extra donde buscar stadium_library.json (p.ej. Config/ de la versión).
    """
    cache_path = os.path.join(config_dir, "stadium_cache.json")
    sheet_id = (os.getenv("STADIUM_SHEET_ID") or DEFAULT_SHEET_ID).strip()
    gid = (os.getenv("STADIUM_SHEET_GID") or DEFAULT_SHEET_GID).strip()

    previous = {}
    try:
        previous = _read_json(cache_path)
    except Exception:
        pass

    try:
        stadiums, warnings = parse_sheet_rows(fetch_sheet_rows(sheet_id, gid))
        if stadiums:
            data = _with_custom(stadiums)
            try:
                os.makedirs(config_dir, exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4, ensure_ascii=False)
            except OSError as e:
                log(f"  [WARN] No se pudo guardar la copia local de estadios: {e}")
            _log_summary(log, stadiums, warnings, previous)
            return data, "sheets"
        log("  [WARN] La hoja de estadios no tiene filas validas.")
    except Exception as e:
        log(f"  [WARN] Sin conexion con la hoja de estadios ({e}).")

    if previous:
        log(f"  [ESTADIOS] {len(previous) - 1} cargados desde la copia local (ultima sincronizacion).")
        return _with_custom(previous), "cache"

    for folder in (config_dir, *fallback_dirs):
        path = os.path.join(folder, "stadium_library.json")
        try:
            data = _read_json(path)
            log(f"  [ESTADIOS] {len(data)} cargados desde stadium_library.json (respaldo).")
            return _with_custom(data), "json"
        except Exception:
            pass

    log("  [ERROR] No hay ninguna fuente de estadios; solo perfil Custom.")
    return {"Custom": dict(CUSTOM_PROFILE)}, "default"
