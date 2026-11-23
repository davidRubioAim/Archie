"""
archie_worker.py (v0.87)
Archie sin interfaz: procesa las campañas de la hoja de control "AIM Sport - Archie Campaigns".

Flujo
  1. Cada POLL_S segundos lee la hoja (a través del Apps Script ArchieControl.gs, web app con clave).
  2. Comprueba los ELEMENTS de cada código en el NAS (nas_campaigns, igual que la pestaña CAMPAIGNS)
     y pinta la celda:  rojo = no hay Elements / incompletos · naranja = revisar a mano (2 LOGO / 2 BG)
                         azul = Elements OK, listo para renderizar · amarillo = en cola / renderizando
                         verde = entregado (nota con el link de Drive y la ruta del NAS)
  3. Solo renderiza cuando alguien pulsa  Archie ▸ Render new campaigns  (o Re-render selected cells):
     arquitectura (la guardada por el PM si existe; si no, la automática de Create Architecture) ->
     JSON ARCHIEtoAE + System File + 3D Preview -> <CÓDIGO>\\BCSA\\<Estadio>\\ en el NAS -> carpeta de Drive.
  4. Progreso: nota de la celda, filas 1-2 de cada estadio (texto + barra) y A2 (estado del worker).

Configuración (Config/.env):
  ARCHIE_SHEET_URL=<URL /exec de la web app>
  ARCHIE_SHEET_SECRET=<clave que muestra el menú Archie ▸ Show worker key>
  (opcional) ARCHIE_WORKER_POLL_S=30 · ARCHIE_WORKER_CHECK_S=180 · ARCHIE_WORKER_TMP=<carpeta temporal local>

Uso:  python Core/archie_worker.py         (o Archie_Worker.bat)
      python Core/archie_worker.py --once  (una sola pasada, para probar)
"""
import copy
import json
import logging
import os
import queue
import re
import shutil
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)
MASTER_DIR = (os.path.dirname(os.path.dirname(BASE_DIR))
              if os.path.basename(os.path.dirname(BASE_DIR)).lower() == "versions" else BASE_DIR)
CONFIG_DIR = os.path.join(MASTER_DIR, "Config")
if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)

try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(CONFIG_DIR, ".env"))
except Exception:
    pass

import hotspot_solver                                       # noqa: E402
import campaign_batch as cb                                 # noqa: E402
from auto_solver import config_campana                      # noqa: E402
from campaign_loader import normalizar_codigo, codigo_valido  # noqa: E402
from nas_campaigns import (NasIndex, safe_name, NOMBRES, raices,  # noqa: E402
                           ST_NO_NAS, ST_NOT_FOUND, ST_INCOMPLETE, ST_REVIEW, ST_READY)
from stadium_loader import load_stadium_library, ROW_SUFFIX, _clean_name  # noqa: E402
from elements_variants import inspeccionar_variantes, ST_FOUND  # noqa: E402

VERSION = "v0.87-worker"
POLL_S = int(os.getenv("ARCHIE_WORKER_POLL_S") or 30)
CHECK_S = int(os.getenv("ARCHIE_WORKER_CHECK_S") or 180)      # caducidad de la comprobación de Elements
PAINT_BUSY_S = 8                                              # refresco del progreso mientras renderiza
STADIUMS_S = 1800                                             # recarga del Master Atlas
HTTP_TIMEOUT = 120
PAINT_BATCH = 150                                             # celdas por llamada a la hoja
OUTPUT_DIR = "BCSA"
STATE_FILE = os.path.join(CONFIG_DIR, "worker_state.json")
LOG_DIR = os.path.join(MASTER_DIR, "logs")
TMP_ROOT = os.getenv("ARCHIE_WORKER_TMP") or os.path.join(os.getenv("LOCALAPPDATA") or MASTER_DIR, "ArchieWorker")
CHUNK = 8 * 1024 * 1024                                      # subida a Drive (múltiplo de 256 KB)

# Valores por defecto de la interfaz al abrir Archie (MegaStripApp.ui_defaults_campana)
UI_DEFAULTS = {"margin": 20, "margin_tl": 20, "gl_offset": 0, "corner_logo": False, "logo_under": False,
               "qty_gl_under": 1, "qty_tl_under": 1, "max_anim_gl": 0, "max_anim_tl": 0,
               "invert_order": False, "stretch_fit": False}

# Colores (minúsculas: así los devuelve getBackgrounds)
C_NONE = None
C_RED = "#ea9999"
C_ORANGE = "#f9cb9c"
C_BLUE = "#cfe2f3"
C_YELLOW = "#ffe599"
C_GREEN = "#b6d7a8"
C_GREY = "#d9d9d9"

log = logging.getLogger("archie_worker")


# ====================================================================== puente con la hoja
class SheetBridge:
    def __init__(self, url, secret):
        self.url, self.secret = url, secret

    def call(self, action, **kw):
        body = json.dumps({"secret": self.secret, "action": action, **kw}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:   # sigue el 302 de Apps Script
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"sheet {action}: {data.get('error')}")
        return data


# ====================================================================== Drive
def drive_upload(token, folder_id, path, progress=None):
    """Subida reanudable a Drive (API v3) con el token de la web app. -> id del archivo."""
    size = os.path.getsize(path)
    meta = json.dumps({"name": os.path.basename(path), "parents": [folder_id]}).encode("utf-8")
    req = urllib.request.Request(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true",
        data=meta, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
                 "X-Upload-Content-Type": "application/octet-stream", "X-Upload-Content-Length": str(size)})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        session = resp.headers["Location"]
    sent = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            end = sent + len(chunk) - 1
            put = urllib.request.Request(session, data=chunk, method="PUT", headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {sent}-{end}/{size}" if chunk else f"bytes */{size}"})
            try:
                with urllib.request.urlopen(put, timeout=300) as resp:
                    return json.loads(resp.read().decode("utf-8")).get("id")
            except urllib.error.HTTPError as e:
                if e.code != 308:      # 308 = "Resume Incomplete": sigue el siguiente trozo
                    raise
            sent += len(chunk)
            if progress:
                progress(sent / max(size, 1))


# ====================================================================== estado persistente
class Store:
    """Entregas hechas: (estadio, código[, versión]) -> {link, nas, at}."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.done = json.load(f).get("done", {})
        except Exception:
            self.done = {}

    @staticmethod
    def key(stadium, code, variant=""):
        return f"{stadium}|{code}" + (f"|{variant}" if variant else "")

    def get(self, stadium, code, variant=""):
        with self.lock:
            return self.done.get(self.key(stadium, code, variant))

    def de_codigo(self, stadium, code):
        """Todas las entregas de un código en un estadio: {versión: rec}."""
        pre = f"{stadium}|{code}"
        with self.lock:
            return {k[len(pre) + 1:] if k != pre else "": v for k, v in self.done.items()
                    if k == pre or k.startswith(pre + "|")}

    def set(self, stadium, code, info, variant=""):
        with self.lock:
            self.done[self.key(stadium, code, variant)] = info
            self._guardar()

    def quitar(self, stadium, code):
        """Olvida todas las entregas (todas las versiones) de un código en un estadio. Devuelve los registros."""
        pre = f"{stadium}|{code}"
        with self.lock:
            fuera = {k: self.done.pop(k) for k in list(self.done) if k == pre or k.startswith(pre + "|")}
            if fuera:
                self._guardar()
        return fuera

    def _guardar(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"done": self.done}, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)


# ====================================================================== textos (inglés, para los PMs)
def note_elements(code, info):
    st, path = info["status"], info.get("path") or ""
    if st == ST_NO_NAS:
        return (f"⚠ Archie could not reach the NAS ({' / '.join(raices())}).\n"
                f"Campaign {code} will be checked again automatically.")
    if st == ST_NOT_FOUND:
        return (f"✖ ELEMENTS NOT CREATED · {code}\n\n"
                f"No Elements were found for this campaign. Please ask the Studio to create the ELEMENTS "
                f"(BG, ANIM and LOGO) and save them in:\n{path}\n\n"
                f"Archie checks again automatically; then use Archie ▸ Render new campaigns.")
    if st == ST_INCOMPLETE:
        faltan = ", ".join(NOMBRES[t] for t in info["missing"])
        return (f"✖ INCOMPLETE ELEMENTS · {code}\nMissing: {faltan}\n\n"
                f"Please ask the Studio to create the missing ELEMENTS and save them in:\n{path}")
    if st == ST_REVIEW:
        tipos = " and ".join(NOMBRES[t] for t in info["ambiguous"])
        return (f"⚠ MANUAL REVIEW NEEDED · {code}\n\n"
                f"More than one {tipos} file was found in:\n{path}\n\n"
                f"Each campaign code must have a single LOGO and a single BACKGROUND. Please ask the Studio "
                f"to keep only the correct files (or use a different campaign code for each version).")
    return ""


def note_ready(code):
    return f"✔ Elements OK · {code}\nReady to render: use Archie ▸ Render new campaigns."


def note_done(code, stadium, rec):
    return (f"✔ DELIVERED · {code} · {stadium}\n\n"
            f"Download (System File, JSON and 3D Preview):\n{rec.get('link', '')}\n\n"
            f"NAS: {rec.get('nas', '')}\n"
            f"Rendered {rec.get('at', '')}")


def etiqueta(v):
    return v or "main"


def _norm_version(v):
    """'C1 SPANISH' / 'national' -> 'C1_SPANISH' / 'NATIONAL' (para comparar con lo escrito en la hoja)."""
    return re.sub(r"[^A-Z0-9]+", "_", str(v or "").upper()).strip("_")


def codigo_y_version(raw):
    """v1.03: la celda puede llevar la versión detrás del código: 'AUDI-MLSX-261001 NATIONAL',
    'AUDI-MLSX-261001_REGIONAL'... -> ('AUDI-MLSX-261001', 'NATIONAL'). Sin versión -> (código, None)."""
    code = normalizar_codigo(raw)
    if not codigo_valido(code) and len(code) > 17 and code[16] == "-" and codigo_valido(code[:16]):
        return code[:16], _norm_version(code[17:])
    return code, None


def filtrar_versiones(variants, ver, elegidas=None):
    """Versiones a mostrar / renderizar: la escrita en la celda (ver) y/o las elegidas en el menú."""
    out = list(variants)
    if ver:
        out = [v for v in out if _norm_version(v["key"]) == ver]
    if elegidas:
        sel = {_norm_version(x) for x in elegidas}
        out = [v for v in out if _norm_version(v["key"]) in sel]
    return out


def nota_versiones(code, info, filas, bloqueada, recs):
    """Nota de una campaña con varias versiones (C1, C2...): una línea por versión."""
    keys = [v["key"] for v in info["variants"]]
    lineas = [f"{code} · {len(keys)} versions ({', '.join(etiqueta(k) for k in keys)})", ""] + filas
    if bloqueada:
        lineas += ["", "Each version must have a single LOGO, a single BACKGROUND and its ANIM. "
                       "Please ask the Studio to create / fix the ELEMENTS in:", info.get("path") or ""]
    rec = next(iter(recs.values()), None)
    if rec:
        lineas += ["", f"Download (System File, JSON and 3D Preview per version):", rec.get("link", ""),
                   f"NAS: {rec.get('nas', '')}"]
    return "\n".join(lineas)


# ====================================================================== NAS
def carpeta_bcsa(base):
    """<carpeta de la campaña>\\BCSA. Reutiliza la que ya exista aunque cambien mayúsculas o espacios
    ("Bcsa", "BCSA ") para no crear duplicados; solo si no hay ninguna se crea "BCSA"."""
    try:
        for e in os.scandir(base):
            if e.is_dir() and e.name.strip().upper() == OUTPUT_DIR:
                return e.path
    except OSError:
        pass
    return os.path.join(base, OUTPUT_DIR)


# Archivos que genera Archie en BCSA\<Estadio>[\<versión>]: JSON, System File(s) y 3D Preview
SALIDA_RE = re.compile(r"(ARCHIETOAE\.JSON|_SYS(_1ST_ROW|_2ND_ROW)?\.MP4|3D_PREVIEW\.MP4)$")


def borrar_salidas_nas(carpeta, code):
    """Borra de `carpeta` (y sus subcarpetas de versión) solo los archivos de entrega de Archie de ese código;
    luego quita las carpetas que queden vacías. Lo que haya puesto el equipo a mano no se toca."""
    if not carpeta or not os.path.isdir(carpeta):
        return 0
    n = 0
    for raiz, _dirs, files in os.walk(carpeta, topdown=False):
        for f in files:
            up = f.upper()
            if code.upper() in up and SALIDA_RE.search(up):
                try:
                    os.remove(os.path.join(raiz, f))
                    n += 1
                except OSError as e:
                    log.warning(f"[BORRAR] No se pudo borrar {os.path.join(raiz, f)}: {e}")
        try:
            if not os.listdir(raiz):
                os.rmdir(raiz)
        except OSError:
            pass
    return n


# ====================================================================== worker
class Job:
    def __init__(self, stadium, code, variant=""):
        self.stadium, self.code, self.variant = stadium, code, variant
        self.status = "queued"     # queued / rendering / done / error
        self.step = "Queued"
        self.pct = 0.0
        self.error = ""

    @property
    def key(self):
        return Store.key(self.stadium, self.code, self.variant)

    @property
    def label(self):
        return self.code + (f" {self.variant}" if self.variant else "")


class Worker:
    def __init__(self, bridge):
        self.bridge = bridge
        self.store = Store(STATE_FILE)
        self.nas = NasIndex(CONFIG_DIR, log=log.info)
        self.stadiums = {}
        self._stadiums_at = 0
        self.elements = {}          # código -> (t, info)
        self.jobs = {}              # key -> Job (de esta sesión)
        self.jobs_lock = threading.Lock()
        self.q = queue.Queue()
        self.last_launch = None
        self.last_reset = None
        self.last_delete = None
        self._lista_estadios = None   # v1.03: última lista enviada al dropdown de la fila 3
        self.busy = threading.Event()
        self.wake = threading.Event()
        self.rendering = None

    # ------------------------------------------------------------ datos
    def cargar_estadios(self, force=False):
        if force or not self.stadiums or time.time() - self._stadiums_at > STADIUMS_S:
            data, source = load_stadium_library(CONFIG_DIR, log=log.info,
                                                fallback_dirs=[os.path.join(BASE_DIR, "Config")])
            self.stadiums = {k: v for k, v in data.items() if k != "Custom"}
            self._stadiums_at = time.time()
            log.info(f"[ESTADIOS] {len(self.stadiums)} perfiles ({source}).")

    def sincronizar_dropdown(self, force=False):
        """v1.03: el dropdown de estadios de la fila 3 con los perfiles de Archie, como en la app: los estadios
        de 2 rows ('X - 1st' / 'X - 2nd') salen una sola vez ('X'). Solo se envía cuando cambia la lista."""
        nombres = sorted(self.stadiums, key=str.lower)
        if not nombres or (nombres == self._lista_estadios and not force):
            return
        try:
            r = self.bridge.call("stadium_list", names=nombres)
            self._lista_estadios = nombres
            log.info(f"[ESTADIOS] Dropdown de la hoja: {len(nombres)} estadios"
                     f"{' · celdas 1st/2nd agrupadas: ' + str(r['fixed']) if r.get('fixed') else ''}.")
        except Exception as e:      # script de la hoja sin 'stadium_list' (versión anterior): no se reintenta
            self._lista_estadios = nombres
            log.warning(f"[ESTADIOS] No se pudo actualizar el dropdown de la hoja: {e}")

    def comprobar(self, code, force=False):
        t, info = self.elements.get(code, (0, None))
        if force or info is None or time.time() - t > CHECK_S:
            info = inspeccionar_variantes(self.nas, code)
            self.elements[code] = (time.time(), info)
        return info

    def perfil(self, nombre):
        """Nombre del dropdown -> nombre del perfil ('Deportivo A Coruña - 1st'/'- 2nd' -> 'Deportivo A Coruña')."""
        if not nombre:
            return None
        if nombre in self.stadiums:
            return nombre
        limpio = _clean_name(nombre)
        if limpio in self.stadiums:
            return limpio
        m = ROW_SUFFIX.search(limpio)
        if m and limpio[:m.start()].strip() in self.stadiums:
            return limpio[:m.start()].strip()
        return None

    # ------------------------------------------------------------ estado deseado de una celda
    def deseado(self, cell, stadium):
        raw = cell["code"]
        if not raw:
            return C_NONE, ""
        code, ver = codigo_y_version(raw)
        if not codigo_valido(code):
            return C_RED, (f"✖ INVALID CAMPAIGN CODE: '{raw}'\n"
                           f"Expected format BRAND-CHANNEL-NNNNNN, e.g. ADID-RMXX-261001.")
        if not stadium:
            return C_GREY, "Select a stadium (venue) in row 3 of this column."
        sheet_name, stadium = stadium, self.perfil(stadium)
        if not stadium:
            return C_RED, (f"✖ Stadium '{sheet_name}' was not found in the Master Atlas (or its profile is "
                           f"incomplete). Archie cannot render {code} for it.")
        info = self.comprobar(code)
        recs = self.store.de_codigo(stadium, code)
        if info["status"] != ST_FOUND:
            if recs and info["status"] == ST_NO_NAS:      # sin NAS, lo ya entregado sigue en verde
                return C_GREEN, note_done(code, stadium, next(iter(recs.values())))
            return (C_GREY if info["status"] == ST_NO_NAS else C_RED), note_elements(code, info)

        variants = filtrar_versiones(info["variants"], ver)
        if ver and not variants:
            disponibles = ", ".join(etiqueta(v["key"]) for v in info["variants"])
            return C_RED, (f"✖ VERSION NOT FOUND · {code} {ver}\n\nAvailable versions: {disponibles}\n"
                           f"Write the code alone to render all of them, or the code and one version "
                           f"(e.g. {code} {etiqueta(info['variants'][0]['key'])}).")
        info = dict(info, variants=variants)
        estados, filas = [], []
        for v in variants:
            key = v["key"]
            with self.jobs_lock:
                job = self.jobs.get(Store.key(stadium, code, key))
            rec = recs.get(key)
            if job and job.status in ("queued", "rendering"):
                st, fila = "working", f"⏳ {etiqueta(key)}: {job.step} · {int(job.pct * 100)}%"
            elif job and job.status == "error":
                st, fila = "error", f"✖ {etiqueta(key)}: render failed · {job.error}"
            elif rec:
                st, fila = "done", f"✔ {etiqueta(key)}: delivered {rec.get('at', '')}"
            elif v["status"] == ST_READY:
                st, fila = "ready", f"● {etiqueta(key)}: Elements OK, ready to render"
            elif v["status"] == ST_REVIEW:
                st = "review"
                fila = (f"⚠ {etiqueta(key)}: more than one "
                        f"{' and '.join(NOMBRES[t] for t in v['ambiguous'])} file · manual review ({v['dir']})")
            else:
                st = "incomplete"
                fila = f"✖ {etiqueta(key)}: missing {', '.join(NOMBRES[t] for t in v['missing'])} ({v['dir']})"
            estados.append(st)
            filas.append(fila)

        bloqueadas = [e for e in estados if e in ("review", "incomplete")]
        if "working" in estados:
            color = C_YELLOW
        elif "error" in estados:
            color = C_RED
        elif all(e == "done" for e in estados):
            color = C_GREEN
        elif bloqueadas:
            color = C_ORANGE if (len(bloqueadas) < len(estados) or "incomplete" not in bloqueadas) else C_RED
        else:
            color = C_BLUE

        if len(variants) == 1 and not variants[0]["key"]:          # campaña normal: textos de siempre
            v, st = variants[0], estados[0]
            if st == "working":
                return color, (f"⏳ ARCHIE IS WORKING · {code}\n{filas[0][2:].split(': ', 1)[1]}\n\n"
                               f"The cell turns green with the download link when it is done.")
            if st == "error":
                return color, (f"✖ RENDER FAILED · {code}\n{filas[0].split(' · ', 1)[1]}\n\n"
                               f"Fix the problem and use Archie ▸ Re-render selected cells.")
            if st == "done":
                return color, note_done(code, stadium, recs[""])
            if st == "ready":
                return color, note_ready(code)
            return color, note_elements(code, dict(v, path=v["dir"]))
        nota = nota_versiones(code, info, filas, bool(bloqueadas), recs)
        if "working" in estados:
            nota = "⏳ ARCHIE IS WORKING · " + nota
        elif color == C_BLUE:
            nota += "\n\nReady to render: use Archie ▸ Render new campaigns."
        return color, nota

    # ------------------------------------------------------------ bucle principal
    def ciclo(self):
        data = self.bridge.call("poll")
        stadium_by_col = {s["col"]: s["name"] for s in data["stadiums"]}
        if data.get("recheck"):
            self.elements.clear()
            self.cargar_estadios(force=True)
        else:
            self.cargar_estadios()
        self.sincronizar_dropdown(force=bool(data.get("recheck")))

        reset = data.get("reset") or ""
        if reset and reset != self.last_reset:
            if self.last_reset is not None:    # (al arrancar solo se toma nota)
                with self.jobs_lock:               # el progreso del lote se reinicia; lo que esté en marcha sigue
                    self.jobs = {k: j for k, j in self.jobs.items() if j.status in ("queued", "rendering")}
                log.info("[RESET] Códigos borrados en la hoja.")
            self.last_reset = reset
        elif self.last_reset is None:
            self.last_reset = ""

        borrado = data.get("delete")
        if borrado and borrado.get("id") != self.last_delete:
            self.borrar(borrado, stadium_by_col)
            self.last_delete = borrado.get("id")
            self.bridge.call("ack_delete", id=borrado.get("id"))

        launch = data.get("launch")
        if launch and launch.get("id") != self.last_launch:
            self.elements.clear()          # se vuelve a mirar el NAS antes de decidir
            self.lanzar(launch, stadium_by_col)
            self.last_launch = launch.get("id")
            self.bridge.call("ack_launch", id=launch.get("id"))
        elif data.get("recheck"):
            self.bridge.call("ack_launch", id="")

        cambios = []
        for cell in data["cells"]:
            stadium = stadium_by_col.get(cell["col"], "")
            try:
                bg, note = self.deseado(cell, stadium)
            except Exception as e:
                log.error(f"[CELDA] {cell}: {e}")
                continue
            actual_bg = None if cell["bg"] in ("#ffffff", "") else cell["bg"]
            if (bg or None) != actual_bg or (note or "") != (cell["note"] or ""):
                cambios.append({"row": cell["row"], "col": cell["col"], "code": cell["code"], "bg": bg,
                                "note": note})
        progreso = self.progreso(data.get("progress") or [], stadium_by_col)
        for i in range(0, max(len(cambios) - PAINT_BATCH, 0), PAINT_BATCH):   # tandas, para no pasar del timeout
            self.bridge.call("paint", cells=cambios[i:i + PAINT_BATCH], progress=[], status="")
        resto = cambios[(max(len(cambios) - 1, 0) // PAINT_BATCH) * PAINT_BATCH:] if cambios else []
        self.bridge.call("paint", cells=resto, progress=progreso, status=self.status_text())
        if cambios:
            log.info(f"[HOJA] {len(cambios)} celda(s) actualizadas.")

    def lanzar(self, launch, stadium_by_col):
        force = bool(launch.get("force"))
        n = 0
        for cell in launch.get("cells") or []:
            stadium = self.perfil(stadium_by_col.get(cell["col"]) or cell.get("stadium") or "")
            code, ver = codigo_y_version(cell.get("code"))
            if not codigo_valido(code) or not stadium:
                continue
            info = self.comprobar(code, force=True)
            if info["status"] != ST_FOUND:
                continue
            # v1.03: solo la versión escrita en la celda y/o las marcadas en "Choose versions to render"
            for v in filtrar_versiones(info["variants"], ver, cell.get("variants")):
                if v["status"] != ST_READY:
                    continue
                key = Store.key(stadium, code, v["key"])
                with self.jobs_lock:
                    job = self.jobs.get(key)
                    if job and job.status in ("queued", "rendering"):
                        continue
                if self.store.get(stadium, code, v["key"]) and not force:
                    continue
                job = Job(stadium, code, v["key"])
                with self.jobs_lock:
                    self.jobs[key] = job
                self.q.put(job)
                n += 1
        log.info(f"[LANZAMIENTO] {launch.get('by') or '?'}: {n} campaña(s) en cola"
                 f"{' (re-render)' if force else ''}.")

    def borrar(self, req, stadium_by_col):
        """Archie ▸ Delete files of selected cells: borra las entregas (NAS + Drive) para poder renderizar de nuevo."""
        for cell in req.get("cells") or []:
            nombre = stadium_by_col.get(cell["col"]) or cell.get("stadium") or ""
            stadium = self.perfil(nombre)
            code = normalizar_codigo(cell.get("code"))
            if not codigo_valido(code) or not stadium:
                log.warning(f"[BORRAR] Se omite {cell.get('code')} · {nombre}: código o estadio no válido.")
                continue
            with self.jobs_lock:
                ocupado = any(j.code == code and j.stadium == stadium and j.status in ("queued", "rendering")
                              for j in self.jobs.values())
            if ocupado:
                log.warning(f"[BORRAR] Se omite {code} · {stadium}: se está renderizando.")
                continue
            recs = self.store.de_codigo(stadium, code)
            carpetas = {r.get("nas") for r in recs.values() if r.get("nas")}
            base = self.nas.carpeta(code)
            if base:
                carpetas.add(os.path.join(carpeta_bcsa(base), safe_name(stadium)))
            n_nas = sum(borrar_salidas_nas(c, code) for c in carpetas)
            n_drive = "?"
            try:
                n_drive = self.bridge.call("drive_delete", code=code, stadium=stadium).get("trashed", 0)
            except Exception as e:
                log.error(f"[BORRAR] Drive {code} · {stadium}: {e}")
            self.store.quitar(stadium, code)
            with self.jobs_lock:
                self.jobs = {k: j for k, j in self.jobs.items() if not (j.code == code and j.stadium == stadium)}
            self.elements.pop(code, None)
            log.info(f"[BORRAR] {req.get('by') or '?'}: {code} · {stadium} · {n_nas} archivo(s) del NAS, "
                     f"{n_drive} carpeta(s) de Drive a la papelera.")

    def progreso(self, actual, stadium_by_col):
        """Texto + barra por estadio, solo con los trabajos de esta sesión."""
        por_estadio = {}
        with self.jobs_lock:
            for job in self.jobs.values():
                por_estadio.setdefault(job.stadium, []).append(job)
        actual_by_col = {p["col"]: p for p in actual}
        out = []
        for col, stadium in stadium_by_col.items():
            jobs = por_estadio.get(self.perfil(stadium)) or []
            if not jobs:
                if actual_by_col.get(col, {}).get("text"):
                    out.append({"col": col, "text": "", "value": None})
                continue
            done = sum(1 for j in jobs if j.status == "done")
            err = sum(1 for j in jobs if j.status == "error")
            pct = sum(1.0 if j.status in ("done", "error") else j.pct for j in jobs) / len(jobs)
            if done + err == len(jobs):
                text = f"✔ {done}/{len(jobs)} delivered" + (f" · {err} failed" if err else "")
            else:
                text = f"⏳ {done}/{len(jobs)} · {int(pct * 100)}%"
            if actual_by_col.get(col, {}).get("text") != text:
                out.append({"col": col, "text": text, "value": round(pct, 3)})
        return out

    def status_text(self):
        hora = time.strftime("%H:%M")
        job = self.rendering
        if job:
            pend = self.q.qsize()
            return f"● Archie online · rendering {job.label} ({int(job.pct * 100)}%)" + \
                   (f" · {pend} queued" if pend else "") + f" · {hora}"
        return f"● Archie online · idle · {hora}"

    def run(self, once=False):
        log.info(f"Archie worker {VERSION} · Config: {CONFIG_DIR}")
        hotspot_solver.configurar(CONFIG_DIR)
        self.cargar_estadios(force=True)
        self.nas.escanear()
        threading.Thread(target=self.render_loop, daemon=True).start()
        while True:
            t0 = time.time()
            try:
                self.ciclo()
            except Exception as e:
                log.error(f"[CICLO] {e}")
                log.debug(traceback.format_exc())
            if once:
                self.q.join()
                self.ciclo()
                return
            espera = PAINT_BUSY_S if self.rendering or not self.q.empty() else POLL_S
            self.wake.wait(max(1.0, espera - (time.time() - t0)))
            self.wake.clear()

    # ------------------------------------------------------------ render
    def render_loop(self):
        while True:
            job = self.q.get()
            self.rendering = job
            job.status = "rendering"
            self.wake.set()
            try:
                self.render(job)
                job.status, job.pct, job.step = "done", 1.0, "Done"
            except Exception as e:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"[:300]
                log.error(f"[RENDER] {job.label} · {job.stadium}: {job.error}")
                log.debug(traceback.format_exc())
            finally:
                self.rendering = None
                self.q.task_done()
                self.wake.set()

    def render(self, job):
        from renderizador import renderizar_system_file
        from render_rapido import renderizar_preview_3d

        code, stadium, variant = job.code, job.stadium, job.variant
        perfil = self.stadiums[stadium]
        log.info(f"[RENDER] {job.label} · {stadium}")

        job.step = "Preparing architecture"
        info = self.comprobar(code, force=True)
        v = next((x for x in info.get("variants") or [] if x["key"] == variant), None)
        if info["status"] != ST_FOUND or not v or v["status"] != ST_READY:
            raise RuntimeError("Elements are no longer complete on the NAS")
        # Arquitectura guardada por estadio y versión: Output\Elements\Archie\<Estadio>[ - C1].json
        arch_name = f"{stadium} - {variant}" if variant else stadium
        sufijo_v = f"{variant}_" if variant else ""
        state = cb.CampaignState(code)
        state.files = copy.deepcopy(v["files"])
        doc_prev, path_prev = cb.cargar(self.nas, code, arch_name)
        if doc_prev:
            cb.aplicar_documento(state, doc_prev, path_prev, self.nas.root)   # arquitectura guardada por el PM
            log.info(f"  arquitectura guardada: {path_prev}")
        else:
            state.config, state.solver_msg = config_campana(perfil, stadium, state.files, UI_DEFAULTS)
        datos = cb.construir_datos(perfil, state.files, state.config)
        if not datos:
            raise RuntimeError("the layout could not be calculated for this stadium")
        doc = cb.documento(datos, state, None, stadium, VERSION)
        if variant:
            doc["archie"]["variant"] = variant
        if not doc_prev:
            cb.guardar(doc, self.nas, code, arch_name)  # como Export: queda para abrirla en Archie

        base = self.nas.carpeta(code)
        if not base:
            raise RuntimeError("campaign folder not found on the NAS")
        nas_stadium = os.path.join(carpeta_bcsa(base), safe_name(stadium))
        nas_dir = os.path.join(nas_stadium, safe_name(variant)) if variant else nas_stadium
        tmp = os.path.join(TMP_ROOT, f"{code}_{safe_name(stadium)}_{variant}")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)

        json_path = os.path.join(tmp, cb.nombre_salida(code, stadium, sufijo_v + "ARCHIEtoAE", ".json"))
        cb._escribir(json_path, doc)

        # Pasos y su peso en la barra: System File(s) 55 % · Preview 30 % · NAS 5 % · Drive 10 %
        sistemas = ([("SYS_1ST_ROW", "1ST_ROW"), ("SYS_2ND_ROW", "2ND_ROW")]
                    if doc.get("config", {}).get("amplify") else [("SYS", None)])
        salidas = [json_path]
        base_pct = 0.0
        for i, (sufijo, row) in enumerate(sistemas, 1):
            out = os.path.join(tmp, cb.nombre_salida(code, stadium, sufijo_v + sufijo, ".mp4"))
            peso = 0.55 / len(sistemas)
            job.step = f"Rendering System File{f' {i}/{len(sistemas)}' if len(sistemas) > 1 else ''}"
            renderizar_system_file(json_path, out, progress_callback=self._prog(job, base_pct, peso),
                                   row_target=row)
            salidas.append(out)
            base_pct += peso

        job.step = "Rendering 3D Preview"
        out = os.path.join(tmp, cb.nombre_salida(code, stadium, sufijo_v + "3D_PREVIEW", ".mp4"))
        renderizar_preview_3d(json_path, out, progress_callback=self._prog(job, 0.55, 0.30))
        salidas.append(out)

        job.step, job.pct = "Copying to the NAS", 0.85
        os.makedirs(nas_dir, exist_ok=True)
        for p in salidas:
            shutil.copy2(p, os.path.join(nas_dir, os.path.basename(p)))

        job.step, job.pct = "Uploading to Drive", 0.90
        folder = self.bridge.call("drive_folder", code=code, stadium=stadium, sub=variant,
                                  replace=[os.path.basename(p) for p in salidas])
        token = self.bridge.call("token")["token"]
        total = sum(os.path.getsize(p) for p in salidas) or 1
        hecho = 0
        for p in salidas:
            tam = os.path.getsize(p)
            drive_upload(token, folder["id"], p,
                         progress=lambda f, h=hecho, t=tam: setattr(job, "pct", 0.90 + 0.10 * (h + f * t) / total))
            hecho += tam

        self.store.set(stadium, code, {"link": folder["url"], "nas": nas_stadium,
                                       "at": time.strftime("%Y-%m-%d %H:%M")}, variant)
        shutil.rmtree(tmp, ignore_errors=True)
        log.info(f"[RENDER] {job.label} · {stadium} entregado: {folder['url']}")

    @staticmethod
    def _prog(job, base, peso):
        def cb_(p):
            job.pct = base + peso * max(0.0, min(1.0, float(p or 0)))
        return cb_


# ====================================================================== main
def configurar_log():
    os.makedirs(LOG_DIR, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(os.path.join(LOG_DIR, "archie_worker.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    log.setLevel(logging.DEBUG)
    log.addHandler(fh)
    if sys.stdout is not None:          # con pythonw (segundo plano) no hay consola
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.setLevel(logging.INFO)
        log.addHandler(sh)


_INSTANCIA = None


def una_sola_instancia(port=47811):
    """Evita dos workers a la vez en el mismo PC (pintarían la hoja y renderizarían dos veces)."""
    global _INSTANCIA
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        return False
    _INSTANCIA = s
    return True


def main():
    configurar_log()
    if not una_sola_instancia():
        log.error("Ya hay un Archie worker corriendo en este PC.")
        sys.exit(0)
    url = (os.getenv("ARCHIE_SHEET_URL") or "").strip()
    secret = (os.getenv("ARCHIE_SHEET_SECRET") or "").strip()
    if not url or not secret:
        log.error("Faltan ARCHIE_SHEET_URL y/o ARCHIE_SHEET_SECRET en Config/.env (ver sheets/LEEME_WORKER.md).")
        sys.exit(1)
    Worker(SheetBridge(url, secret)).run(once="--once" in sys.argv)


if __name__ == "__main__":
    main()
