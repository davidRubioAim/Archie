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

# 1. INYECCIÓN DE LA API KEY (Reemplaza la función del .bat)
os.environ["GEMINI_API_KEY"] = "AQ.Ab8RN6JrNe_NB4tTtmA3bJeILrl0PSh9S-bD8I0KvFSMD_PqjQ"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(BASE_DIR, "versions")
os.makedirs(VERSIONS_DIR, exist_ok=True)

GIST_URL = "https://gist.githubusercontent.com/davidRubioAim/e575bfbfceef3e187410ee5e6e494a1a/raw/version.json"
DEFAULT_REPO = "davidRubioAim/Archie"

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
        sys.exit(1)

def main():
    print("=======================================================")
    print("  ARCHIE: Sincronizando entorno de trabajo...")
    print("=======================================================")
    
    config = sync_config()
    
    target_version = sys.argv[1] if len(sys.argv) > 1 else config.get('target_version', 'v0.24')
    repo = config.get('github_repo', DEFAULT_REPO)
    token = config.get('github_token', fallback="").strip()

    version_path = os.path.join(VERSIONS_DIR, target_version)
    expected_core_app = os.path.join(version_path, "Core", "app.py")

    if os.path.exists(version_path) and not os.path.exists(expected_core_app):
        shutil.rmtree(version_path)

    if not os.path.exists(version_path):
        download_from_github(target_version, repo, token)

    print(f"  [OK] Lanzando Archie (Motor {target_version})...")
    print("=======================================================\n")
    
    if not os.path.exists(expected_core_app):
        print(f"  [ERROR] Punto de entrada no encontrado en: {expected_core_app}")
        sys.exit(1)

    script_dir = os.path.dirname(expected_core_app)
    
    # 1. Detectar el intérprete correcto para evitar el bucle del .exe
    python_cmd = "python" if getattr(sys, 'frozen', False) else sys.executable
    
    req_file = os.path.join(version_path, "requirements.txt")
    if not os.path.exists(req_file):
        req_file = os.path.join(BASE_DIR, "requirements.txt")
        
    if os.path.exists(req_file):
        print("  [*] Comprobando dependencias...")
        result = subprocess.run(
            [python_cmd, "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r", req_file],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print("  [WARN] Falló la instalación de algunas dependencias mediante pip:")
            print(result.stderr.strip()[-1500:])
            
        importlib.invalidate_caches()

    # 2. Ejecutar la aplicación con el intérprete del sistema
    subprocess.run([python_cmd, expected_core_app], cwd=script_dir)

if __name__ == "__main__":
    main()