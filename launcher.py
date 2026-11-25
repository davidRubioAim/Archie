import os
import sys
import zipfile
import urllib.request
import shutil
import subprocess
import configparser
import json
import time
import importlib
import hashlib


LAUNCHER_VERSION = "v0.51"
FROZEN = getattr(sys, 'frozen', False)

# v0.46: instalado como programa (Archie.exe sin consola). Misma ID que pone Core/app.py en su ventana: así Windows
# agrupa la ventana de Archie con el acceso directo del menú Inicio (que el instalador crea con esta ID) y al anclarla
# a la barra de tareas guarda ese acceso directo, con su icono, en vez de python.exe.
APP_ID = "AIM.Archie"
# Pantalla de carga: el .exe instalado, o "python launcher.py --gui" para probarla sin compilar. "--console" la quita.
GUI = (FROZEN or "--gui" in sys.argv) and "--console" not in sys.argv
ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
# v0.47: el .exe puede compilarse con consola (por defecto, para debug) o sin ella (build_installer.ps1 -SinConsola).
# Con consola, todo lo que imprimen launcher y Core sale en ella y además en logs\.
HAS_CONSOLE = sys.stdout is not None and getattr(sys.stdout, "isatty", lambda: False)()
NO_WINDOW = 0 if HAS_CONSOLE else getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Compilado con PyInstaller (--onefile), __file__ apunta a una carpeta temporal que se borra al cerrar:
# la carpeta de trabajo tiene que ser la del .exe, o versions/ y config.ini se perderían en cada arranque.
BASE_DIR = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(BASE_DIR, "versions")
os.makedirs(VERSIONS_DIR, exist_ok=True)

GIST_URL = "https://gist.githubusercontent.com/davidRubioAim/e575bfbfceef3e187410ee5e6e494a1a/raw/version.json"
DEFAULT_REPO = "davidRubioAim/Archie"

# Python privado de Archie: solo se instala si el equipo no tiene un Python válido (3.10+ con tkinter).
# Instalación por usuario dentro de la carpeta de Archie: no pide administrador ni toca el PATH.
PYTHON_VERSION = "3.12.10"
PYTHON_INSTALLER_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe"
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime", "python")

# Respaldo si el tag descargado no trae requirements.txt
DEFAULT_REQUIREMENTS = """customtkinter
pillow
numpy
moviepy>=2.0
proglog
google-genai
tkinterdnd2
python-dotenv
"""

# Hoja "ARCHIE API": de aquí salen GEMINI_API_KEY y GITHUB_TOKEN (columna A = nombre, B = valor)
CONFIG_SHEET_ID = "1NtfSsL-ZdVZEiE7kMaNk0BKyRZ9bNxrYrYLfFmS9Khk"

def setup_ssl():
    """Certificados HTTPS. El Python del .exe trae su propio OpenSSL y no ve bien el almacén de Windows: en equipos
    recién instalados (o con antivirus que inspecciona HTTPS) falla con CERTIFICATE_VERIFY_FAILED.
    truststore valida con el propio Windows, igual que Edge/Chrome; certifi queda como respaldo."""
    try:
        import truststore
        truststore.inject_into_ssl()
        return
    except Exception:
        pass
    try:
        import ssl
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx)))
    except Exception:
        pass

def fetch_sheet_value(key):
    """Lee un valor de la hoja de configuración. Devuelve "" si no hay conexión o no existe."""
    import csv, io
    url = f"https://docs.google.com/spreadsheets/d/{CONFIG_SHEET_ID}/export?format=csv&gid=0"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Archie-Launcher", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode("utf-8")
        if "<html" in raw[:500].lower():
            return ""
        for row in csv.reader(io.StringIO(raw)):
            if len(row) >= 2 and row[0].strip() == key:
                return row[1].strip()
    except Exception as e:
        print(f"  [WARN] No se pudo leer la hoja de configuración ({e}).")
    return ""

REMOTE = {}  # contenido del version.json del Gist (lo usa check_launcher_update)
GIST_API = "https://api.github.com/gists/" + GIST_URL.split("/")[4]


def read_gist():
    """version.json del Gist. v0.48: primero por la API de GitHub, que no tiene la caché de ~5 min de
    gist.githubusercontent.com (por eso a veces se seguía abriendo la versión anterior); si falla, la URL raw."""
    hdr = {'Cache-Control': 'no-cache', 'User-Agent': 'Archie-Launcher'}
    try:
        req = urllib.request.Request(GIST_API, headers=dict(hdr, Accept="application/vnd.github+json"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            files = json.loads(resp.read().decode('utf-8')).get("files", {})
        content = (files.get("version.json") or next(iter(files.values()), {})).get("content", "")
        if content:
            return json.loads(content)
    except Exception as e:
        print(f"  [WARN] API del Gist no disponible ({e}); uso la URL raw.")
    req = urllib.request.Request(f"{GIST_URL}?t={int(time.time())}", headers=hdr)
    with urllib.request.urlopen(req, timeout=4) as response:
        return json.loads(response.read().decode('utf-8'))

def sync_config():
    config = configparser.ConfigParser()
    config_path = os.path.join(BASE_DIR, 'config.ini')
    
    def write_default():
        with open(config_path, 'w') as f:
            f.write(f"[ARCHIE]\ntarget_version = v0.24\ngithub_repo = {DEFAULT_REPO}\ngithub_token = \n")

    if not os.path.exists(config_path):
        write_default()
        
    try:
        config.read(config_path)
        if 'ARCHIE' not in config:
            raise ValueError("Sección [ARCHIE] ausente")
    except Exception:
        write_default()
        config.read(config_path)

    try:
        remote_data = read_gist()
        REMOTE.update(remote_data)

        remote_v = remote_data.get("version")
        remote_repo = remote_data.get("github_repo", DEFAULT_REPO)
        
        if remote_v and not remote_v.startswith("v"):
            remote_v = f"v{remote_v}"
            
        updated = False
        if remote_v and config['ARCHIE'].get('target_version') != remote_v:
            config['ARCHIE']['target_version'] = remote_v
            updated = True
            
        if remote_repo and config['ARCHIE'].get('github_repo') != remote_repo:
            config['ARCHIE']['github_repo'] = remote_repo
            updated = True
            
        if updated:
            with open(config_path, 'w') as f:
                config.write(f)
                
    except Exception as e:
        print(f"  [WARN] No se pudo verificar el Gist ({e}). Usando versión local.")
        
    return config['ARCHIE']

# --- v0.50: actualización incremental ----------------------------------------------------------------------------
INCREMENTAL_MAX_FILES = 150   # más ficheros cambiados que esto: sale más a cuenta el zip completo
KEEP_VERSIONS = 2             # versiones que se conservan en versions\ (además de la que se abre)


def _vkey(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", v))


def _is_version_dir(name):
    import re
    return re.fullmatch(r"v\d+(\.\d+)*", name) is not None


def _gh_headers(token, accept="application/vnd.github+json"):
    h = {"User-Agent": "Archie-Launcher", "Accept": accept}
    if token:
        h["Authorization"] = f"Bearer {token.strip()}"
    return h


def incremental_update(version, repo, token):
    """Crea versions/<version> copiando la última versión local anterior y bajando SOLO los ficheros que cambian entre
    los dos tags (API compare de GitHub). Una versión con cambios de código pesa KB en vez de los ~45 MB del zip.
    Devuelve True si lo consigue; ante cualquier duda devuelve False y se hace la descarga completa de siempre."""
    import urllib.parse
    base = None
    for d in os.listdir(VERSIONS_DIR):
        if _is_version_dir(d) and _vkey(d) < _vkey(version) and \
                os.path.exists(os.path.join(VERSIONS_DIR, d, "Core", "app.py")):
            if base is None or _vkey(d) > _vkey(base):
                base = d
    if not base:
        return False
    try:
        print(f"  [*] Buscando qué ha cambiado de {base} a {version}...")
        url = f"https://api.github.com/repos/{repo}/compare/{base}...{version}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=_gh_headers(token)), timeout=30) as r:
            cmp = json.loads(r.read().decode("utf-8"))
        files = cmp.get("files") or []
        if cmp.get("status") not in ("ahead", "identical") or len(files) >= 300:
            print("  [*] Cambios no comparables: descarga completa.")
            return False
        descargas = [f for f in files if f.get("status") != "removed"]
        if len(descargas) > INCREMENTAL_MAX_FILES:
            print(f"  [*] {len(descargas)} ficheros cambiados: descarga completa.")
            return False

        temp = os.path.join(VERSIONS_DIR, f"_inc_{version}")
        if os.path.exists(temp):
            shutil.rmtree(temp)
        shutil.copytree(os.path.join(VERSIONS_DIR, base), temp,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        root = os.path.realpath(temp)

        def ruta(rel):
            p = os.path.realpath(os.path.join(temp, *rel.split("/")))
            if not p.startswith(root + os.sep):
                raise ValueError(f"ruta fuera de la carpeta: {rel}")
            return p

        for f in files:
            st = f.get("status")
            if st in ("removed", "renamed") and (f.get("previous_filename") or st == "removed"):
                viejo = ruta(f["previous_filename"] if st == "renamed" else f["filename"])
                if os.path.isfile(viejo):
                    os.remove(viejo)
        total = 0
        for i, f in enumerate(descargas, 1):
            rel = f["filename"]
            api = (f"https://api.github.com/repos/{repo}/contents/{urllib.parse.quote(rel)}"
                   f"?ref={urllib.parse.quote(version)}")
            req = urllib.request.Request(api, headers=_gh_headers(token, "application/vnd.github.raw"))
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            dest = ruta(rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as out:
                out.write(data)
            total += len(data)
            print(f"  [*] Actualizando {version}: {i}/{len(descargas)} ficheros ({total / 1024:.0f} KB)")

        if not os.path.exists(os.path.join(temp, "Core", "app.py")):
            raise ValueError("falta Core/app.py tras aplicar los cambios")
        final_path = os.path.join(VERSIONS_DIR, version)
        if os.path.exists(final_path):
            shutil.rmtree(final_path)
        os.rename(temp, final_path)
        print(f"  [OK] {version} lista: {len(descargas)} ficheros nuevos o cambiados ({total / 1048576:.1f} MB) "
              f"sobre {base}.")
        return True
    except Exception as e:
        print(f"  [WARN] Actualización incremental no disponible ({e}). Descarga completa.")
        try:
            shutil.rmtree(os.path.join(VERSIONS_DIR, f"_inc_{version}"))
        except Exception:
            pass
        return False


def cleanup_old_versions(current):
    """Deja en versions/ solo la que se abre y las KEEP_VERSIONS más recientes (las viejas ocupan ~45 MB cada una).
    Solo en el programa instalado: en el repo de desarrollo no se toca nada."""
    if not FROZEN:
        return
    vers = sorted((d for d in os.listdir(VERSIONS_DIR)
                   if _is_version_dir(d) and os.path.isdir(os.path.join(VERSIONS_DIR, d))), key=_vkey, reverse=True)
    keep = set(vers[:KEEP_VERSIONS]) | {current}
    for d in vers:
        if d not in keep:
            try:
                shutil.rmtree(os.path.join(VERSIONS_DIR, d))
                print(f"  [*] Versión antigua eliminada: {d}")
            except Exception:
                pass  # en uso (otro Archie abierto) o bloqueada: se intenta en el próximo arranque
    for d in os.listdir(VERSIONS_DIR):  # restos de descargas interrumpidas
        if d.startswith(("_inc_", "_temp_")):
            shutil.rmtree(os.path.join(VERSIONS_DIR, d), ignore_errors=True)


def download_from_github(version, repo, token):
    print(f"  [*] La versión {version} no está en caché local.")
    if incremental_update(version, repo, (token or "").strip()):
        return True
    print(f"  [*] Descargando paquete completo desde GitHub ({repo})...")
    
    token_clean = token.strip() if token else ""
    if token_clean:
        url = f"https://api.github.com/repos/{repo}/zipball/refs/tags/{version}"
        headers = {
            'User-Agent': 'Archie-Bootstrapper',
            'Authorization': f"Bearer {token_clean}",
            'Accept': 'application/vnd.github+json'
        }
    else:
        url = f"https://github.com/{repo}/archive/refs/tags/{version}.zip"
        headers = {'User-Agent': 'Archie-Bootstrapper'}

    zip_path = os.path.join(BASE_DIR, f"temp_{version}.zip")
    req = urllib.request.Request(url, headers=headers)

    try:
        _download_with_progress(req, zip_path, version)
            
        print("  [*] Extrayendo módulos del motor y CustomTkinter...")
        
        final_path = os.path.join(VERSIONS_DIR, version)
        if os.path.exists(final_path):
            shutil.rmtree(final_path)

        temp_extract_dir = os.path.join(VERSIONS_DIR, f"_temp_{version}")
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)

        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_dir)
            
        extracted_subdirs = [
            os.path.join(temp_extract_dir, d) 
            for d in os.listdir(temp_extract_dir) 
            if os.path.isdir(os.path.join(temp_extract_dir, d))
        ]
        
        if extracted_subdirs:
            shutil.move(extracted_subdirs[0], final_path)
            shutil.rmtree(temp_extract_dir)
        else:
            os.rename(temp_extract_dir, final_path)

        os.remove(zip_path)
        print(f"  [OK] Entorno {version} desplegado correctamente.")
        
    except Exception as e:
        print(f"  [ERROR] Falló la descarga desde GitHub ({url}): {e}")
        if getattr(e, "code", None) == 404:
            print(f"  [ERROR] El tag {version} no existe en {repo} (¿se hizo git push del tag?) o el token no ve el repo.")
        if os.path.exists(zip_path): os.remove(zip_path)
        if 'temp_extract_dir' in locals() and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        return False
    return True


def latest_local_version(exclude=None):
    """La versión descargada más alta que tenga Core/app.py (respaldo si la del Gist no se puede bajar)."""
    import re
    best = None
    for d in os.listdir(VERSIONS_DIR):
        if d == exclude or not re.fullmatch(r"v\d+(\.\d+)*", d):
            continue
        if os.path.exists(os.path.join(VERSIONS_DIR, d, "Core", "app.py")):
            key = tuple(int(x) for x in d[1:].split("."))
            if best is None or key > best[0]:
                best = (key, d)
    return best[1] if best else None

def _download_with_progress(req, zip_path, version, intentos=3):
    """v0.49: descarga por bloques mostrando los MB (el paquete pesa ~45 MB: con conexión lenta parecía colgado) y con
    timeout de 60 s por lectura + reintentos, para que una conexión caída no deje el launcher esperando para siempre."""
    for intento in range(1, intentos + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as response, open(zip_path, 'wb') as out_file:
                total = int(response.headers.get("Content-Length") or 0)
                hecho, aviso, t0 = 0, 0, time.time()
                while True:
                    bloque = response.read(256 * 1024)
                    if not bloque:
                        break
                    out_file.write(bloque)
                    hecho += len(bloque)
                    if hecho - aviso >= 2 * 1024 * 1024:
                        aviso = hecho
                        vel = hecho / max(time.time() - t0, 0.1) / 1048576
                        de = f" de {total / 1048576:.0f}" if total else ""
                        print(f"  [*] Descargando {version}: {hecho / 1048576:.0f}{de} MB ({vel:.1f} MB/s)")
            return
        except Exception as e:
            if getattr(e, "code", None) == 404 or intento == intentos:
                raise
            print(f"  [WARN] Descarga interrumpida ({e}). Reintento {intento + 1} de {intentos}...")
            time.sleep(3)


def self_update_launcher(version_path):
    """Sustituye este launcher.py por el de la versión descargada (efectivo en el próximo arranque)."""
    new_launcher = os.path.join(version_path, "launcher.py")
    this_launcher = os.path.abspath(__file__)
    try:
        if getattr(sys, 'frozen', False) or not os.path.exists(new_launcher):
            return
        with open(new_launcher, 'rb') as f_new, open(this_launcher, 'rb') as f_old:
            if f_new.read() == f_old.read():
                return
        shutil.copyfile(new_launcher, this_launcher)
        print("  [OK] Launcher actualizado (se aplica en el próximo arranque).")
    except Exception as e:
        print(f"  [WARN] No se pudo actualizar el launcher: {e}")

def message_box(text, title="Archie", flags=0x10):
    """MessageBox nativo (seguro desde cualquier hilo). flags: 0x10 error, 0x40 info, 0x24 pregunta Sí/No. Devuelve 6 si Sí."""
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, title, flags | 0x10000)  # 0x10000: en primer plano
    except Exception:
        return 0

def fail(msg):
    print(f"  [ERROR] {msg}")
    if GUI:
        message_box(f"{msg}\n\nDetalle en:\n{LOG_PATH}", "Archie: no se pudo arrancar")
        os._exit(1)  # con consola, el mensaje ya está en ella  # puede llamarse desde el hilo de trabajo: sys.exit solo cerraría ese hilo
    if FROZEN:
        input("\n  Pulsa Enter para cerrar...")
    sys.exit(1)

def _python_ok(cmd):
    """True si cmd es un Python 3.10+ con tkinter (CustomTkinter lo necesita)."""
    try:
        r = subprocess.run(
            cmd + ["-c", "import sys, tkinter; print('ARCHIE_OK' if sys.version_info >= (3, 10) else 'OLD')"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "ARCHIE_OK" in r.stdout
    except Exception:
        return False

def find_python():
    """Devuelve el comando del Python con el que se lanzará el Core, o None si no hay ninguno válido."""
    candidates = []
    if not FROZEN:
        candidates.append([sys.executable])  # lanzado con Archie.bat: el mismo Python que ejecuta este script
    runtime_exe = os.path.join(RUNTIME_DIR, "python.exe")
    if os.path.exists(runtime_exe):
        candidates.append([runtime_exe])
    for name, extra in (("python", []), ("py", ["-3"])):
        path = shutil.which(name)
        # El "python" de WindowsApps es el acceso directo a la Microsoft Store, no un Python real
        if path and "windowsapps" not in path.lower():
            candidates.append([path] + extra)
    # Instalaciones normales de python.org que no están en el PATH (por usuario y para todos)
    import glob
    for pattern in (os.path.expandvars(r"%LOCALAPPDATA%\Programs\Python\Python3*\python.exe"),
                    os.path.expandvars(r"%ProgramFiles%\Python3*\python.exe")):
        for exe in sorted(glob.glob(pattern), reverse=True):
            candidates.append([exe])
    for cmd in candidates:
        if _python_ok(cmd):
            return cmd
    return None

def install_private_python():
    print(f"  [*] No se encontró Python en este equipo. Instalando Python {PYTHON_VERSION} solo para Archie...")
    print("      (descarga de ~25 MB, puede tardar un par de minutos)")
    installer = os.path.join(BASE_DIR, f"python-{PYTHON_VERSION}-installer.exe")
    try:
        req = urllib.request.Request(PYTHON_INSTALLER_URL, headers={'User-Agent': 'Archie-Launcher'})
        with urllib.request.urlopen(req, timeout=60) as resp, open(installer, 'wb') as f:
            shutil.copyfileobj(resp, f)
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        subprocess.run([
            installer, "/quiet",
            "InstallAllUsers=0", f"TargetDir={RUNTIME_DIR}", "PrependPath=0",
            "Include_launcher=0", "Include_test=0", "Include_doc=0", "Shortcuts=0", "AssociateFiles=0",
            "Include_pip=1", "Include_tcltk=1",
        ], check=False, creationflags=NO_WINDOW)
    except Exception as e:
        print(f"  [WARN] Falló la instalación automática de Python: {e}")
    finally:
        if os.path.exists(installer):
            try: os.remove(installer)
            except OSError: pass

    runtime_exe = os.path.join(RUNTIME_DIR, "python.exe")
    if os.path.exists(runtime_exe) and _python_ok([runtime_exe]):
        print(f"  [OK] Python {PYTHON_VERSION} instalado en {RUNTIME_DIR}")
        return [runtime_exe]
    fail(f"No se pudo instalar Python automáticamente. Instálalo desde https://www.python.org/downloads/ "
         f"marcando 'Add python.exe to PATH' y vuelve a abrir Archie.")

def ensure_dependencies(python_cmd, version_path):
    """Instala los paquetes de requirements.txt solo cuando cambian (o cambia el Python), no en cada arranque."""
    req_file = os.path.join(version_path, "requirements.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(BASE_DIR, "requirements.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(VERSIONS_DIR, "_default_requirements.txt")
        with open(req_file, 'w', encoding='utf-8') as f:
            f.write(DEFAULT_REQUIREMENTS)

    with open(req_file, 'rb') as f:
        stamp = hashlib.sha1(f.read() + " ".join(python_cmd).encode()).hexdigest()
    stamp_file = os.path.join(version_path, ".deps_ok")
    if os.path.exists(stamp_file):
        with open(stamp_file, encoding='utf-8') as f:
            if f.read().strip() == stamp:
                print("  [OK] Dependencias al día.")
                return

    print("  [*] Verificando e instalando dependencias (solo la primera vez o si cambian)...\n")
    pip_check = subprocess.run(python_cmd + ["-m", "pip", "--version"], capture_output=True, text=True,
                               creationflags=NO_WINDOW)
    if pip_check.returncode != 0:
        subprocess.run(python_cmd + ["-m", "ensurepip", "--upgrade"], capture_output=True, text=True,
                       creationflags=NO_WINDOW)

    result = subprocess.run(
        python_cmd + ["-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", req_file],
        capture_output=True, text=True, creationflags=NO_WINDOW
    )
    if result.returncode != 0:
        print("  [WARN] Falló la instalación de algunas dependencias mediante pip:")
        print(result.stderr.strip()[-1500:])
        return  # sin sello: se reintenta en el próximo arranque
    with open(stamp_file, 'w', encoding='utf-8') as f:
        f.write(stamp)
    print("  [OK] Dependencias instaladas.")

# v0.51: variables que pone PyInstaller en el .exe (sobre todo TCL_LIBRARY/TK_LIBRARY, apuntando a su Tcl 8.6.13 de
# _internal). Si llegan al Python con el que corre el Core, su tkinter (p. ej. Tcl 8.6.15 del Python privado) carga el
# init.tcl equivocado: "version conflict for package Tcl" y Archie no abre. Se quitan antes de lanzar ningún proceso.
_PYI_ENV_VARS = ("TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH", "TIX_LIBRARY", "PYTHONHOME", "PYTHONPATH",
                 "_MEIPASS2", "_PYI_APPLICATION_HOME_DIR", "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL",
                 "_PYI_SPLASH_IPC", "PYINSTALLER_RESET_ENVIRONMENT")


def clean_child_env():
    if not FROZEN:
        return
    for k in _PYI_ENV_VARS:
        os.environ.pop(k, None)


def main():
    clean_child_env()
    # v0.48: para que el botón "Update available" del Core pueda volver a abrir este launcher
    os.environ["ARCHIE_LAUNCHER_EXE"] = sys.executable if FROZEN else os.path.abspath(__file__)
    print("=======================================================")
    print(f"  ARCHIE: Sincronizando entorno de trabajo... (launcher {LAUNCHER_VERSION})")
    print("=======================================================")
    
    setup_ssl()
    config = sync_config()
    
    check_launcher_update(token_hint=config.get('github_token', fallback=""))

    target_version = ARGS[0] if ARGS else config.get('target_version', 'v0.24')
    repo = config.get('github_repo', DEFAULT_REPO)
    # El token de la hoja manda; el de config.ini solo es respaldo
    token = fetch_sheet_value("GITHUB_TOKEN") or config.get('github_token', fallback="").strip()

    version_path = os.path.join(VERSIONS_DIR, target_version)
    expected_core_app = os.path.join(version_path, "Core", "app.py")

    if os.path.exists(version_path) and not os.path.exists(expected_core_app):
        shutil.rmtree(version_path)

    if not os.path.exists(version_path) and not download_from_github(target_version, repo, token):
        # v0.48: si la versión publicada no se puede bajar, se abre la última que ya está en este equipo
        fallback = latest_local_version(exclude=target_version)
        if not fallback:
            fail(f"No se pudo descargar {target_version} y no hay ninguna versión anterior en este equipo. "
                 f"Revisa la conexión, que el tag exista en GitHub y que GITHUB_TOKEN de la hoja tenga acceso al repo.")
        print(f"  [WARN] Abro la última versión disponible en este equipo: {fallback}")
        target_version = fallback
        version_path = os.path.join(VERSIONS_DIR, target_version)
        expected_core_app = os.path.join(version_path, "Core", "app.py")

    self_update_launcher(version_path)
    cleanup_old_versions(target_version)

    print(f"  [OK] Lanzando Archie (Motor {target_version})...")
    print("=======================================================\n")
    
    if not os.path.exists(expected_core_app):
        fail(f"Punto de entrada no encontrado en: {expected_core_app}")

    script_dir = os.path.dirname(expected_core_app)

    # 1. Python con el que corre el Core: el del equipo si vale, si no uno privado dentro de la carpeta de Archie
    python_cmd = find_python() or install_private_python()

    # 2. Paquetes (customtkinter, moviepy, imageio-ffmpeg con su ffmpeg, etc.)
    ensure_dependencies(python_cmd, version_path)
    importlib.invalidate_caches()

    # 3. Ejecutar la aplicación
    if not GUI:
        subprocess.run(python_cmd + [expected_core_app], cwd=script_dir)
        return
    launch_core_hidden(python_cmd, expected_core_app, script_dir)


# ---------------------------------------------------------------------------------------------------------------
# v0.46: modo programa instalado · v0.47: consola opcional y pantalla de carga AIM
# ---------------------------------------------------------------------------------------------------------------
import threading

LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_PATH = os.path.join(LOG_DIR, "launcher.log")
CORE_LOG_PATH = os.path.join(LOG_DIR, "archie_core.log")
HIDE_SPLASH = threading.Event()  # el Core ya está abriendo su ventana: la de carga sobra
CONSOLE_OUT = sys.stdout         # consola real (None si el .exe se compiló sin consola)


def resource_dir():
    """Recursos empaquetados (splash, fuentes): _internal del .exe; installer/ y Assets/ en el repo."""
    return getattr(sys, "_MEIPASS", None)


def resource(*partes_exe, repo=None):
    base = resource_dir()
    ruta = os.path.join(base, *partes_exe) if base else os.path.join(BASE_DIR, *(repo or partes_exe))
    return ruta if os.path.exists(ruta) else None


def set_app_id():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
        except Exception:
            pass


def launch_core_hidden(python_cmd, app_py, cwd):
    """Arranca el Core. Todo lo que imprime va a logs/archie_core.log y, si hay consola, también a la consola.
    Sin consola: python.exe con CREATE_NO_WINDOW (tampoco salen consolas de ffmpeg) y el launcher se cierra.
    Con consola: el launcher sigue vivo mostrando la salida hasta que se cierra Archie."""
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(CORE_LOG_PATH):
        try: os.replace(CORE_LOG_PATH, CORE_LOG_PATH + ".old")
        except OSError: pass
    log = open(CORE_LOG_PATH, "w", encoding="utf-8", errors="replace", buffering=1)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", ARCHIE_LAUNCHER=LAUNCHER_VERSION)
    print("  [OK] Abriendo la ventana de Archie...")
    if HAS_CONSOLE:
        proc = subprocess.Popen(python_cmd + [app_py], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, encoding="utf-8", errors="replace", bufsize=1)

        def copiar():
            for linea in proc.stdout:
                log.write(linea)
                try:
                    CONSOLE_OUT.write(linea)
                    CONSOLE_OUT.flush()
                except Exception:
                    pass
        lector = threading.Thread(target=copiar, daemon=True)
        lector.start()
    else:
        proc = subprocess.Popen(python_cmd + [app_py], cwd=cwd, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=env, creationflags=NO_WINDOW)
    for i in range(80):  # 8 s: tiempo para importar customtkinter/moviepy y abrir la ventana
        if proc.poll() is not None:
            break
        if i == 30:
            HIDE_SPLASH.set()
        time.sleep(0.1)
    HIDE_SPLASH.set()
    if HAS_CONSOLE:
        proc.wait()
        lector.join(timeout=2)
    log.close()
    if proc.returncode not in (None, 0):
        try:
            with open(CORE_LOG_PATH, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-1500:]
        except OSError:
            tail = ""
        message_box(f"Archie se cerró con un error (código {proc.returncode}).\n\n{tail}\n\nLog completo:\n{CORE_LOG_PATH}",
                    "Archie: error")


def _version_tuple(v):
    import re
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "")))


def check_launcher_update(token_hint=""):
    """El Core se actualiza solo (tag de GitHub). El propio Archie.exe solo cambia cuando cambia el launcher: si el
    version.json del Gist trae "launcher_version" mayor que esta e "installer_url", se descarga el instalador nuevo y
    se ejecuta en silencio (Inno Setup cierra Archie, sustituye el .exe y lo vuelve a abrir)."""
    if not FROZEN:
        return
    remote_lv = REMOTE.get("launcher_version")
    url = REMOTE.get("installer_url", "")
    if not remote_lv or not url or _version_tuple(remote_lv) <= _version_tuple(LAUNCHER_VERSION):
        return
    if message_box(f"Hay una versión nueva del programa Archie ({remote_lv}, tienes {LAUNCHER_VERSION}).\n\n"
                   f"¿Instalarla ahora? Tarda unos segundos y Archie se vuelve a abrir solo.",
                   "Archie: actualización", 0x24) != 6:
        return
    import tempfile
    dest = os.path.join(tempfile.gettempdir(), f"Archie_Setup_{remote_lv}.exe")
    try:
        print(f"  [*] Descargando el instalador {remote_lv}...")
        headers = {"User-Agent": "Archie-Launcher"}
        token = (fetch_sheet_value("GITHUB_TOKEN") or token_hint or "").strip()
        if url.startswith("https://api.github.com/"):  # release de GitHub (sirve aunque el repo sea privado)
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if "/releases/tags/" in url or url.rstrip("/").endswith("/releases/latest"):
                # URL de la release: se busca el .exe entre sus assets
                req = urllib.request.Request(url, headers=dict(headers, Accept="application/vnd.github+json"))
                with urllib.request.urlopen(req, timeout=20) as resp:
                    assets = json.loads(resp.read().decode("utf-8")).get("assets", [])
                url = next(a["url"] for a in assets if a.get("name", "").lower().endswith(".exe"))
            headers["Accept"] = "application/octet-stream"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)
        with open(dest, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("lo descargado no es un .exe (¿enlace sin acceso directo?)")
        subprocess.Popen([dest, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH"], close_fds=True)
        os._exit(0)
    except Exception as e:
        print(f"  [WARN] No se pudo actualizar el programa ({e}). Sigo con la versión instalada.")


# --- Pantalla de carga --------------------------------------------------------------------------------------------
# Geometría a escala 1 (la misma que installer/splash/generar_splash.py, que hornea fondo, logos y pista de la barra)
SPLASH_W, SPLASH_H = 640, 360
SPLASH_TEXTO = (44, 290)              # x, línea base inferior del texto de estado
SPLASH_BARRA = (44, 302, 596)         # x0, y centro, x1
SPLASH_FUENTE = "Figtree"


def _registrar_fuentes():
    carpeta = resource("fonts", repo=("Assets", "fonts"))
    if not carpeta or os.name != "nt":
        return False
    import ctypes, glob
    return sum(ctypes.windll.gdi32.AddFontResourceExW(r, 0x10, 0) > 0
               for r in glob.glob(os.path.join(carpeta, "*.ttf"))) > 0   # 0x10 = FR_PRIVATE: solo este proceso


def _dpi_aware():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # nítido en pantallas al 125-200 %
    except Exception:
        pass


def _esquinas_redondeadas(root):
    """Windows 11: esquinas redondeadas también en una ventana sin marco (en Windows 10 no hace nada)."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        valor = ctypes.c_int(2)  # DWMWCP_ROUND
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(valor), ctypes.sizeof(valor))
    except Exception:
        pass


def run_gui():
    """Pantalla de carga con la estética AIM mientras se sincroniza (descargas, pip la primera vez).
    Se oculta al abrirse el Core. Los mensajes van también a la consola (si la hay) y a logs/launcher.log."""
    import queue
    import tkinter as tk

    os.makedirs(LOG_DIR, exist_ok=True)
    msgs = queue.Queue()

    class _Out:
        def __init__(self):
            self.f = open(LOG_PATH, "w", encoding="utf-8", errors="replace", buffering=1)
        def write(self, text):
            self.f.write(text)
            if CONSOLE_OUT is not None:
                try:
                    CONSOLE_OUT.write(text)
                    CONSOLE_OUT.flush()
                except Exception:
                    pass
            for line in text.splitlines():
                line = line.strip(" =")
                if line:
                    msgs.put(line.replace("[*] ", "").replace("[OK] ", "").replace("[WARN] ", "Aviso: ")
                             .replace("[ERROR] ", "Error: "))
        def flush(self):
            self.f.flush()

    sys.stdout = sys.stderr = _Out()

    _dpi_aware()
    fuente = SPLASH_FUENTE if _registrar_fuentes() else "Segoe UI"

    root = tk.Tk()
    root.title("Archie")
    root.overrideredirect(True)
    root.attributes("-alpha", 0.0)
    try:
        root.iconbitmap(default=resource("Archie.ico", repo=("installer", "Archie.ico")))
    except Exception:
        pass

    # Escala de la pantalla -> imagen 100/150/200 %
    escala_real = root.winfo_fpixels("1i") / 96.0
    nombre, s = "100", 1.0
    for n, v in (("150", 1.5), ("200", 2.0)):
        if escala_real >= v - 0.13:
            nombre, s = n, v
    img_path = resource("splash", f"splash_{nombre}.png", repo=("installer", "splash", f"splash_{nombre}.png"))
    w, h = round(SPLASH_W * s), round(SPLASH_H * s)
    root.geometry(f"{w}x{h}+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 2}")

    cv = tk.Canvas(root, width=w, height=h, highlightthickness=0, bd=0, bg="#ff2bd6")
    cv.pack()
    if img_path:
        root._fondo = tk.PhotoImage(file=img_path)
        cv.create_image(0, 0, image=root._fondo, anchor="nw")
    else:  # sin imagen: degradado plano con los rosas AIM
        cv.create_text(44 * s, 150 * s, text="Archie", anchor="w", fill="white", font=(fuente, -round(64 * s), "bold"))
        x0, y, x1 = [v * s for v in SPLASH_BARRA]
        cv.create_line(x0, y, x1, y, fill="#ff8fd8", width=round(4 * s), capstyle="round")

    tx, ty = SPLASH_TEXTO
    estado = cv.create_text(tx * s, ty * s, anchor="sw", fill="white", text="Comprobando actualizaciones...",
                            font=(f"{fuente} Medium" if fuente == SPLASH_FUENTE else fuente, -round(13 * s)),
                            width=round((SPLASH_W - 2 * tx) * s))
    cv.create_text((SPLASH_W - 44) * s, (SPLASH_H - 24) * s, anchor="se", fill="#ffd9f1",
                   text=f"launcher {LAUNCHER_VERSION}", font=(fuente, -round(10 * s)))
    x0, yb, x1 = [v * s for v in SPLASH_BARRA]
    tramo = cv.create_line(x0, yb, x0, yb, fill="white", width=round(4 * s), capstyle="round")

    _esquinas_redondeadas(root)
    done = threading.Event()
    t0 = time.time()

    def work():
        try:
            main()
        except SystemExit:
            pass
        except Exception as e:
            import traceback
            traceback.print_exc()
            fail(f"Error inesperado: {e}")
        finally:
            done.set()

    def anim():
        if done.is_set():
            root.destroy()
            return
        if HIDE_SPLASH.is_set():
            if root.state() != "withdrawn":
                root.withdraw()
            root.after(200, anim)
            return
        t = time.time() - t0
        root.attributes("-alpha", min(1.0, t / 0.25))          # aparece en 0,25 s
        # barra indeterminada: un tramo del 28 % que recorre la pista con aceleración suave
        largo = (x1 - x0) * 0.28
        fase = (t % 1.6) / 1.6
        fase = fase * fase * (3 - 2 * fase)
        cabeza = x0 + (x1 - x0 + largo) * fase
        cv.coords(tramo, max(x0, cabeza - largo), yb, min(x1, cabeza), yb)
        try:
            ultimo = None
            while True:
                ultimo = msgs.get_nowait()
        except queue.Empty:
            if ultimo:
                cv.itemconfigure(estado, text=ultimo)
        root.after(16, anim)

    threading.Thread(target=work, daemon=True).start()
    root.after(16, anim)
    root.mainloop()


if __name__ == "__main__":
    set_app_id()
    if GUI:
        run_gui()
    else:
        main()
