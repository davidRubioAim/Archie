import os
import sys
import zipfile
import urllib.request
import shutil
import subprocess
import configparser
import json
import time

# Directorios base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(BASE_DIR, "versions")
os.makedirs(VERSIONS_DIR, exist_ok=True)

# URL RAW del Gist de control
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
    except Exception as e:
        print(f"  [WARN] config.ini inválido ({e}). Regenerando archivo...")
        write_default()
        config.read(config_path)

    # Consulta dinámica al Gist maestro con cache-buster por timestamp
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
            print(f"  [*] Orden de despliegue detectada. Sincronizando a {remote_v}...")
            config['ARCHIE']['target_version'] = remote_v
            updated = True
            
        if remote_repo and config['ARCHIE'].get('github_repo') != remote_repo:
            config['ARCHIE']['github_repo'] = remote_repo
            updated = True
            
        if updated:
            with open(config_path, 'w') as f:
                config.write(f)
                
    except Exception as e:
        print(f"  [WARN] No se pudo verificar el Gist ({e}). Usando configuración local.")
        
    return config['ARCHIE']

def download_from_github(version, repo, token):
    print(f"  [*] La versión {version} no está en caché local.")
    print(f"  [*] Descargando paquete desde GitHub ({repo})...")
    
    url = f"https://github.com/{repo}/archive/refs/tags/{version}.zip"
    zip_path = os.path.join(BASE_DIR, f"temp_{version}.zip")
    
    req = urllib.request.Request(url, headers={'User-Agent': 'Archie-Bootstrapper'})
    if token:
        req.add_header("Authorization", f"token {token}")

    try:
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
        print("  [*] Extrayendo módulos y dependencias...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            root_folder = zip_ref.namelist()[0].split('/')[0]
            zip_ref.extractall(VERSIONS_DIR)
            
        extracted_path = os.path.join(VERSIONS_DIR, root_folder)
        final_path = os.path.join(VERSIONS_DIR, version)
        
        if os.path.exists(final_path):
            shutil.rmtree(final_path)
            
        os.rename(extracted_path, final_path)
        os.remove(zip_path)
        print(f"  [OK] Entorno {version} desplegado correctamente.")
        
    except Exception as e:
        print(f"  [ERROR] Falló la descarga desde GitHub ({url}): {e}")
        if os.path.exists(zip_path): 
            os.remove(zip_path)
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

    if not os.path.exists(version_path):
        download_from_github(target_version, repo, token)

    print(f"  [OK] Lanzando Archie (Motor {target_version})...")
    print("=======================================================\n")
    
    # Punto de entrada exacto según la estructura de tu proyecto
    main_script = os.path.join(version_path, "Core", "app.py")
    
    if not os.path.exists(main_script):
        print(f"  [ERROR] No se encontró el archivo de inicio en: {main_script}")
        sys.exit(1)

    # Instalación silenciosa de dependencias si existe requirements.txt
    req_file = os.path.join(version_path, "requirements.txt")
    if os.path.exists(req_file):
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"])

    # Ejecución estableciendo el directorio de trabajo en Core/
    script_dir = os.path.dirname(main_script)
    subprocess.run([sys.executable, main_script], cwd=script_dir)

if __name__ == "__main__":
    main()