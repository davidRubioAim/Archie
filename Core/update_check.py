"""v1.07: aviso "Update available" dentro de la app.

Lee la versión publicada del mismo version.json del Gist que usa el launcher. Primero por la API de GitHub (sin la caché
de ~5 min de gist.githubusercontent.com, que es por lo que a veces seguía saliendo la versión anterior) y, si falla,
por la URL raw. Si hay una versión mayor que la abierta, la app muestra un botón; al pulsarlo, tras confirmar, vuelve a
abrir el launcher con esa versión y se cierra (el launcher la descarga y abre Archie)."""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

GIST_ID = "e575bfbfceef3e187410ee5e6e494a1a"
GIST_API = f"https://api.github.com/gists/{GIST_ID}"
GIST_RAW = f"https://gist.githubusercontent.com/davidRubioAim/{GIST_ID}/raw/version.json"
INTERVALO_S = 15 * 60  # cada cuánto se vuelve a mirar con la app abierta


def leer_version_json(timeout=6):
    """Contenido de version.json del Gist (dict) o {} si no hay conexión."""
    hdr = {"User-Agent": "Archie", "Cache-Control": "no-cache"}
    try:
        req = urllib.request.Request(GIST_API, headers=dict(hdr, Accept="application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=timeout) as r:
            files = json.loads(r.read().decode("utf-8")).get("files", {})
        contenido = (files.get("version.json") or next(iter(files.values()), {})).get("content", "")
        if contenido:
            return json.loads(contenido)
    except Exception:
        pass
    try:
        req = urllib.request.Request(f"{GIST_RAW}?t={int(time.time())}", headers=hdr)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def normalizar(v):
    v = str(v or "").strip()
    return v if not v or v.startswith("v") else f"v{v}"


def tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def version_nueva(actual):
    """La versión publicada si es mayor que `actual`; si no, None."""
    remota = normalizar(leer_version_json().get("version"))
    if remota and tupla(remota) > tupla(actual):
        return remota
    return None


def comando_launcher(master_dir):
    """Cómo volver a abrir el launcher. ARCHIE_LAUNCHER_EXE lo pone el launcher v0.47+; si no está (launchers
    anteriores) se busca Archie.exe o launcher.py en la carpeta maestra (la del programa instalado / del repo)."""
    ruta = os.environ.get("ARCHIE_LAUNCHER_EXE", "")
    if not ruta or not os.path.exists(ruta):
        for nombre in ("Archie.exe", "launcher.py"):
            cand = os.path.join(master_dir, nombre)
            if os.path.exists(cand):
                ruta = cand
                break
    if not ruta:
        return None
    return [sys.executable, ruta] if ruta.lower().endswith(".py") else [ruta]


def relanzar(master_dir, version):
    """Abre el launcher en un proceso independiente (con su propia consola si la usa). True si se pudo."""
    cmd = comando_launcher(master_dir)
    if not cmd:
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    env = {k: v for k, v in os.environ.items() if k not in ("ARCHIE_LAUNCHER", "PYTHONUNBUFFERED")}
    subprocess.Popen(cmd + [version], cwd=os.path.dirname(cmd[-1]), env=env, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True
