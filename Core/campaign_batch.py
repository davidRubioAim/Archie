"""
campaign_batch.py (v0.58)
Estado de cada campaña aprobada para un estadio, su arquitectura y el JSON donde se guarda.

Una campaña = sus ELEMENTS (archivos) + su config de arquitectura (sliders/checkboxes de Manual Edit, que
calcular_layout_data convierte siempre en la misma distribución) para UN estadio. Nada se hereda de otra
campaña: cada una tiene su propia config.

El JSON de arquitectura es el mismo que "Generate AE Import" (lo abre ArchieToAE en After Effects) más un
bloque "archie" con lo necesario para reabrirlo en Archie (campaña, estadio, config, archivos, revisado).
Se guarda en  <CÓDIGO>\\Output\\Elements\\Archie\\<Estadio>.json  y, si no hay permiso de escritura en el NAS,
en la carpeta local que elija el PM:  <carpeta>\\<CÓDIGO>\\<Estadio>.json
"""
import copy
import getpass
import json
import os
import time

from motor_distribucion import calcular_layout_data
from auto_solver import CONFIG_KEYS
from nas_campaigns import safe_name, raices, ST_READY, ST_REVIEW
from log_utils import debug, log_error

SETTINGS_FILE = "archie_settings.json"

DISCLAIMER = ("Keep these architecture JSON files and share them with the Studio whenever you need feedback applied. "
              "The Studio imports them into After Effects, so the approved architecture is preserved and your "
              "feedback can be applied accurately.")

# Estados que se muestran en la columna Approved campaigns
S_CHECKING = "checking"
S_NO_NAS = "no_nas"
S_MISSING = "not_found"
S_INCOMPLETE = "incomplete"
S_REVIEW_FILES = "review"
S_PENDING = "pending"         # Elements OK, sin arquitectura todavía
S_UNREVIEWED = "unreviewed"   # arquitectura calculada / editada, sin revisar
S_REVIEWED = "reviewed"

ETIQUETAS = {
    S_CHECKING: "Checking…", S_NO_NAS: "NAS offline", S_MISSING: "No Elements", S_INCOMPLETE: "Incomplete",
    S_REVIEW_FILES: "Check files", S_PENDING: "Pending", S_UNREVIEWED: "Not reviewed", S_REVIEWED: "Reviewed",
}


class CampaignState:
    def __init__(self, code):
        self.code = code
        self.files = None          # archivos_cargados de la campaña
        self.files_manual = False  # el PM cambió archivos a mano (p.ej. eligió el LOGO cuando había dos)
        self.config = None         # config de calcular_layout_data + "margin"
        self.reviewed = False
        self.saved_path = None
        self.saved_at = None
        self.solver_msg = ""
        self.loaded_from_disk = False


def archivos_completos(files):
    if not files:
        return False
    anims = [a for a in files.get("anim_zones") or [] if a]
    return bool(anims or files.get("anim_zone")) and bool(files.get("static_logo")) and bool(files.get("background"))


def estado(state, elements):
    """Estado visible de una campaña (para la lista y para saber si se puede exportar)."""
    if elements is None:
        return S_CHECKING
    st = elements["status"]
    usable = st in (ST_READY, ST_REVIEW) or (state.files_manual and archivos_completos(state.files))
    if st == ST_REVIEW and not state.files_manual:
        return S_REVIEW_FILES
    if not usable:
        return st          # no_nas / not_found / incomplete (mismos nombres que en nas_campaigns)
    if not archivos_completos(state.files):
        return S_INCOMPLETE
    if state.config is None:
        return S_PENDING
    return S_REVIEWED if state.reviewed else S_UNREVIEWED


def exportable(st):
    return st in (S_PENDING, S_UNREVIEWED, S_REVIEWED)


# ---------------------------------------------------------------- datos de render / AE
def construir_datos(perfil, files, config):
    """Mismo resultado que MegaStripApp.calcular_layout con esta config, sin interfaz."""
    margin = int(config.get("margin", 20))
    conf = {k: config[k] for k in CONFIG_KEYS if k in config}
    conf["edge_case"] = perfil.get("Edge Case")
    datos = calcular_layout_data(int(perfil["Width"]), int(perfil["Height"]), int(perfil["Goal Line Size"]),
                                 int(perfil["Center Board Width"]), margin, files, conf)
    if not datos:
        return None
    datos["config"] = conf
    try:
        fps = float(perfil.get("FPS") or 0)
    except (TypeError, ValueError):
        fps = 0
    datos["fps"] = (int(fps) if fps == int(fps) else fps) if 1 <= fps <= 120 else 25
    sys_w, sys_h = int(perfil["Canvas Width"]), int(perfil["Canvas Height"])
    sys_mask = int(perfil.get("Canvas Mask") or 0)
    off_x, off_y = int(perfil.get("Offset X") or 0), int(perfil.get("Offset Y") or 0)
    datos["system_setup"] = {"canvas_width": sys_w, "canvas_height": sys_h, "canvas_mask": sys_mask,
                             "offset_x": off_x, "offset_y": off_y}
    datos["system_file"] = {"width": sys_w, "height": sys_h, "offset": {"x": off_x, "y": off_y},
                            "mask": {"x": sys_w - sys_mask, "y": 0, "width": sys_mask, "height": sys_h}}
    edge = perfil.get("Edge Case") or {}
    if edge.get("tramos"):
        datos["system_file"]["tramos"] = edge["tramos"]
    if edge.get("zonas_negras"):
        datos["zonas_negras"] = edge["zonas_negras"]
    return datos


def documento(datos, state, campaign, stadium, version):
    """JSON de arquitectura: datos de AE + bloque archie."""
    doc = copy.deepcopy(datos)
    doc["proyecto"] = "AE_EXPORT"
    doc["archie"] = {
        "version": version,
        "campaign": state.code,
        "campaign_name": (campaign or {}).get("name", ""),
        "stadium": stadium,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "saved_by": _usuario(),
        "reviewed": bool(state.reviewed),
        "config": state.config,
        "files": state.files,
        "note": DISCLAIMER,
    }
    return doc


def _usuario():
    try:
        return getpass.getuser()
    except Exception:
        return ""


# ---------------------------------------------------------------- guardado
def ruta_local(local_dir, code, stadium):
    return os.path.join(local_dir, code, safe_name(stadium) + ".json")


def ruta_nas(nas, code, stadium):
    d = nas.archie_dir(code)
    return os.path.join(d, safe_name(stadium) + ".json") if d else None


def _escribir(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def guardar(doc, nas, code, stadium, local_dir=None):
    """Intenta el NAS; si no se puede escribir, la carpeta local. -> (ruta, "nas"|"local") o (None, None)."""
    p = ruta_nas(nas, code, stadium)
    if p:
        try:
            _escribir(p, doc)
            return p, "nas"
        except OSError as e:
            debug(f"[Campañas] Sin escritura en el NAS para {code}: {e}")
    if local_dir:
        p = ruta_local(local_dir, code, stadium)
        try:
            _escribir(p, doc)
            return p, "local"
        except OSError as e:
            log_error(f"[Campañas] No se pudo guardar {p}: {e}")
    return None, None


def _leer(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc.get("archie"), dict) else None
    except Exception:
        return None


def remapear_raiz(path, root_actual):
    """Las dos IPs del NAS son el mismo recurso: una ruta guardada con una se reescribe con la que responde ahora."""
    if not path or not root_actual or os.path.exists(path):
        return path
    low = path.lower()
    for r in raices():
        if low.startswith(r.lower()):
            return root_actual + path[len(r):]
    return path


def cargar(nas, code, stadium, local_dir=None):
    """Arquitectura guardada más reciente (NAS o local) -> (doc, ruta) o (None, None)."""
    candidatos = []
    p = ruta_nas(nas, code, stadium)
    if p and os.path.isfile(p):
        candidatos.append(p)
    if local_dir:
        p = ruta_local(local_dir, code, stadium)
        if os.path.isfile(p):
            candidatos.append(p)
    mejor = None
    for p in candidatos:
        doc = _leer(p)
        if doc and (mejor is None or doc["archie"].get("saved_at", "") > mejor[0]["archie"].get("saved_at", "")):
            mejor = (doc, p)
    return mejor or (None, None)


def aplicar_documento(state, doc, path, root_actual):
    meta = doc["archie"]
    state.config = meta.get("config")
    files = meta.get("files")
    if files:
        files = copy.deepcopy(files)
        for k in ("background", "center_board", "static_logo", "anim_zone"):
            files[k] = remapear_raiz(files.get(k), root_actual)
        files["anim_zones"] = [remapear_raiz(a, root_actual) for a in files.get("anim_zones") or []] or [None]
        state.files = files
        state.files_manual = True   # la selección de archivos guardada manda sobre la detección automática
    state.reviewed = bool(meta.get("reviewed"))
    state.saved_path = path
    state.saved_at = meta.get("saved_at")
    state.loaded_from_disk = True


# ---------------------------------------------------------------- ajustes locales
def leer_ajustes(config_dir):
    try:
        with open(os.path.join(config_dir, SETTINGS_FILE), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_ajuste(config_dir, key, value):
    data = leer_ajustes(config_dir)
    data[key] = value
    try:
        with open(os.path.join(config_dir, SETTINGS_FILE), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except OSError as e:
        log_error(f"No se pudo guardar {SETTINGS_FILE}: {e}")


def nombre_salida(code, stadium, sufijo, ext):
    return f"{code}_{safe_name(stadium).replace(' ', '_')}_{sufijo}{ext}"
