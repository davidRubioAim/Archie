"""
v0.89: previews a medida de cada estadio desde una carpeta de Google Drive (fuera del repositorio de GitHub).

La carpeta (compartida como "cualquiera con el enlace") tiene los plate_<perfil>.png de cada estadio y, si hacen
falta, su plate_<perfil>_delante.png y plate_<perfil>.json. v0.90: el .json puede llevar solo
{"estadios": ["Perfil 1", "Perfil 2"]} para que otros perfiles usen el mismo plate. Archie solo baja lo que necesita el estadio que se va a
renderizar y lo guarda en Assets/plates/estadios/ (ignorado por git). Solo se vuelve a bajar si el archivo de Drive
ha cambiado (id, tamaño o fecha distintos). Sin red o si Drive falla, se usa lo que ya hay en la caché y, si no hay
nada, el plate A.

Carpeta: PLATES_DRIVE_FOLDER, o la variable de entorno ARCHIE_PLATES_DRIVE (id o enlace; "0" la desactiva).
No hace falta ninguna clave: se lee la vista pública de la carpeta y se descarga como cualquier enlace compartido.
"""
import html
import json
import os
import re
import threading
import time
import urllib.request

PLATES_DRIVE_FOLDER = "1KKPtyVQFtx4qhgGPYA__RazDIKRfMbFy"
CARPETA_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Assets", "plates", "estadios")
INDICE = os.path.join(CARPETA_CACHE, "_drive.json")
LISTADO_TTL_S = 300          # el listado de la carpeta se reutiliza durante 5 min en la misma sesión
TIMEOUT_S = 30
UA = {"User-Agent": "Archie"}

_lock = threading.Lock()
_listado = {"t": 0.0, "files": None}


def _carpeta_id():
    v = os.getenv("ARCHIE_PLATES_DRIVE")
    v = (PLATES_DRIVE_FOLDER if v is None else v).strip()
    if v in ("", "0"):
        return None
    m = re.search(r"folders/([A-Za-z0-9_-]+)", v) or re.search(r"id=([A-Za-z0-9_-]+)", v)
    return m.group(1) if m else v


def _abrir(url, headers=None):
    req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
    return urllib.request.urlopen(req, timeout=TIMEOUT_S)


def listar(log=print):
    """{nombre: id} de los archivos de la carpeta (vista pública de Drive). None si no se puede leer."""
    fid = _carpeta_id()
    if not fid:
        return None
    with _lock:
        if _listado["files"] is not None and time.time() - _listado["t"] < LISTADO_TTL_S:
            return _listado["files"]
    try:
        with _abrir("https://drive.google.com/embeddedfolderview?id=%s" % fid) as r:
            txt = r.read().decode("utf-8", "replace")
    except Exception as e:
        log("[plates] Drive no disponible (%s): se usa la caché local." % e)
        return None
    files = {}
    for m in re.finditer(r'id="entry-([A-Za-z0-9_-]+)".*?class="flip-entry-title">([^<]*)<', txt, re.S):
        files[html.unescape(m.group(2)).strip()] = m.group(1)
    with _lock:
        _listado.update(t=time.time(), files=files)
    return files


def _url_descarga(file_id):
    return "https://drive.usercontent.google.com/download?id=%s&export=download&confirm=t" % file_id


def _firma(file_id):
    """Firma del archivo de Drive (id + tamaño + Last-Modified) sin bajarlo: pide 1 byte."""
    with _abrir(_url_descarga(file_id), {"Range": "bytes=0-0"}) as r:
        h = r.headers
        total = (h.get("Content-Range") or "").rpartition("/")[2] or h.get("Content-Length") or ""
        tipo = h.get("Content-Type") or ""
        if "text/html" in tipo:
            raise RuntimeError("Drive devolvió una página (¿la carpeta no es pública?)")
        return "%s|%s|%s" % (file_id, total, h.get("Last-Modified") or "")


def _bajar(file_id, destino):
    tmp = destino + ".part"
    with _abrir(_url_descarga(file_id)) as r, open(tmp, "wb") as f:
        if "text/html" in (r.headers.get("Content-Type") or ""):
            raise RuntimeError("Drive devolvió una página en vez del archivo")
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    with open(tmp, "rb") as f:
        cab = f.read(8)
    if destino.lower().endswith(".png") and cab != b"\x89PNG\r\n\x1a\n":
        os.remove(tmp)
        raise RuntimeError("el archivo bajado no es un PNG")
    os.replace(tmp, destino)


def _leer_indice():
    try:
        with open(INDICE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _actualizar(files, nombre, indice, log):
    """Baja `nombre` de Drive a la caché si no está o si ha cambiado. Si falla, se queda la copia local."""
    destino = os.path.join(CARPETA_CACHE, nombre)
    try:
        firma = _firma(files[nombre])
        if os.path.isfile(destino) and indice.get(nombre) == firma:
            return
        log("[plates] bajando %s de Drive..." % nombre)
        t0 = time.time()
        _bajar(files[nombre], destino)
        indice[nombre] = firma
        log("[plates] %s listo (%.1f s)" % (nombre, time.time() - t0))
    except Exception as e:
        log("[plates] no se pudo actualizar %s (%s): se usa la copia local si existe." % (nombre, e))


def _alias_cache(stem):
    try:
        with open(os.path.join(CARPETA_CACHE, "plate_%s.json" % stem), encoding="utf-8") as f:
            v = json.load(f).get("estadios") or []
        return [v] if isinstance(v, str) else [str(x) for x in v]
    except Exception:
        return []


def sincronizar(casa, log=print):
    """Baja (o actualiza) de Drive los plates para los que casa(stem, alias) es True, con sus _delante/.json.
    alias = "estadios" del plate_<stem>.json (v0.90: los .json, que son pequeños, se bajan siempre para leerlos).
    Devuelve la carpeta de la caché. Nunca lanza excepciones: si algo falla, se queda lo que haya en la caché."""
    try:
        files = listar(log)
        if not files:
            return CARPETA_CACHE
        os.makedirs(CARPETA_CACHE, exist_ok=True)
        indice = _leer_indice()
        stems = {n[6:-4] for n in files if n.lower().startswith("plate_") and n.lower().endswith(".png")}
        stems = {s for s in stems if not s.lower().endswith(("_delante", "_led"))}
        for s in stems:
            if "plate_%s.json" % s in files:
                _actualizar(files, "plate_%s.json" % s, indice, log)
        for s in (s for s in stems if casa(s, _alias_cache(s))):
            for nombre in ("plate_%s.png" % s, "plate_%s_delante.png" % s):
                if nombre in files:
                    _actualizar(files, nombre, indice, log)
        with open(INDICE, "w", encoding="utf-8") as f:
            json.dump(indice, f, indent=1)
    except Exception as e:
        log("[plates] error con Drive (%s): se usa la caché local." % e)
    return CARPETA_CACHE
