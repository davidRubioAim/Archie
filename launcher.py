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


LAUNCHER_VERSION = "v0.45"
FROZEN = getattr(sys, 'frozen', False)

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
        url_busted = f"{GIST_URL}?t={int(time.time())}"
        req = urllib.request.Request(url_busted, headers={'Cache-Control': 'no-cache', 'User-Agent': 'Archie-Launcher'})
        with urllib.request.urlopen(req, timeout=4) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
            
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

def download_from_github(version, repo, token):
    print(f"  [*] La versión {version} no está en caché local.")
    print(f"  [*] Descargando paquete desde GitHub ({repo})...")
    
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
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
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
        if os.path.exists(zip_path): os.remove(zip_path)
        if 'temp_extract_dir' in locals() and os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)
        fail("Revisa la conexión y que GITHUB_TOKEN de la hoja tenga acceso al repo.")

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

def fail(msg):
    print(f"  [ERROR] {msg}")
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
        ], check=False)
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
    pip_check = subprocess.run(python_cmd + ["-m", "pip", "--version"], capture_output=True, text=True)
    if pip_check.returncode != 0:
        subprocess.run(python_cmd + ["-m", "ensurepip", "--upgrade"], capture_output=True, text=True)

    result = subprocess.run(
        python_cmd + ["-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", req_file],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("  [WARN] Falló la instalación de algunas dependencias mediante pip:")
        print(result.stderr.strip()[-1500:])
        return  # sin sello: se reintenta en el próximo arranque
    with open(stamp_file, 'w', encoding='utf-8') as f:
        f.write(stamp)
    print("  [OK] Dependencias instaladas.")

def main():
    print("=======================================================")
    print(f"  ARCHIE: Sincronizando entorno de trabajo... (launcher {LAUNCHER_VERSION})")
    print("=======================================================")
    
    setup_ssl()
    config = sync_config()
    
    target_version = sys.argv[1] if len(sys.argv) > 1 else config.get('target_version', 'v0.24')
    repo = config.get('github_repo', DEFAULT_REPO)
    # El token de la hoja manda; el de config.ini solo es respaldo
    token = fetch_sheet_value("GITHUB_TOKEN") or config.get('github_token', fallback="").strip()

    version_path = os.path.join(VERSIONS_DIR, target_version)
    expected_core_app = os.path.join(version_path, "Core", "app.py")

    if os.path.exists(version_path) and not os.path.exists(expected_core_app):
        shutil.rmtree(version_path)

    if not os.path.exists(version_path):
        download_from_github(target_version, repo, token)

    self_update_launcher(version_path)

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
    subprocess.run(python_cmd + [expected_core_app], cwd=script_dir)

if __name__ == "__main__":
    main()