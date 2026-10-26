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
DEFAULT_SHEET_GID = "0"  # pestaña MASTER ATLAS

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
            warnings.append(f"Fila {line_no} '{name}': omitida, faltan {bad}")
            continue
        if profile["Canvas Height"] > 4320 or profile["Canvas Width"] > 8192:
            warnings.append(f"Fila {line_no} '{name}': omitida, canvas sospechoso "
                            f"{profile['Canvas Width']}x{profile['Canvas Height']}")
            continue
        if name in stadiums:
            warnings.append(f"Fila {line_no} '{name}': nombre duplicado, se queda la primera")
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


def load_stadium_library(config_dir, log=print):
    """
    Devuelve (stadium_data, source) donde source ∈ {'sheets', 'cache', 'json', 'default'}.
    stadium_data mantiene el formato de stadium_library.json, con 'Custom' siempre primero.
    """
    cache_path = os.path.join(config_dir, "stadium_cache.json")
    legacy_path = os.path.join(config_dir, "stadium_library.json")
    sheet_id = (os.getenv("STADIUM_SHEET_ID") or DEFAULT_SHEET_ID).strip()
    gid = (os.getenv("STADIUM_SHEET_GID") or DEFAULT_SHEET_GID).strip()

    if sheet_id:
        try:
            stadiums, warnings = parse_sheet_rows(fetch_sheet_rows(sheet_id, gid))
            for w in warnings:
                log(f"  [WARN] {w}")
            if stadiums:
                data = _with_custom(stadiums)
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=4, ensure_ascii=False)
                except OSError as e:
                    log(f"  [WARN] No se pudo escribir la caché: {e}")
                log(f"  [OK] {len(stadiums)} estadios cargados desde Google Sheets.")
                return data, "sheets"
            log("  [WARN] La hoja no devolvió estadios válidos.")
        except Exception as e:
            log(f"  [WARN] No se pudo leer Google Sheets: {e}")
    else:
        log("  [WARN] STADIUM_SHEET_ID no configurado en Config/.env")

    for path, source in ((cache_path, "cache"), (legacy_path, "json")):
        try:
            data = _read_json(path)
            log(f"  [OK] Estadios cargados desde {os.path.basename(path)} (fallback).")
            return _with_custom(data), source
        except Exception:
            pass

    log("  [ERROR] Sin fuente de estadios; solo perfil Custom.")
    return {"Custom": dict(CUSTOM_PROFILE)}, "default"
