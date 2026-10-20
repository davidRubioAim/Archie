import os
import sys
import zipfile
import urllib.request
import shutil
import subprocess
import configparser
import json
import time

# Rutas base
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(BASE_DIR, "versions")
os.makedirs(VERSIONS_DIR, exist_ok=True)

# ⚠️ URL RAW DE TU GIST (Sin el hash del commit para que apunte siempre al último)
GIST_URL = "https://gist.githubusercontent.com/davidRubioAim/e575bfbfceef3e187410ee5e6e494a1a/raw/version.json"

def sync_config():
    config = configparser.ConfigParser()
    config_path = os.path.join(BASE_DIR, 'config.ini')
    
    # 1. Crear config base si es la primera ejecución
    if not os.path.exists(config_path):
        with open(config_path, 'w') as f:
            f.write("[ARCHIE]\ntarget_version = v0.24\ngithub_repo = davidRubioAim/AimAutoArch\ngithub_token = \n")
    
    config.read(config_path)
    
    # 2. Consultar Gist maestro (con cache-buster)
    try:
        url_busted = f"{GIST_URL}?t={int(time.time())}"
        req = urllib.request.Request(url_busted, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=3) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
            
        remote_v = remote_data.get("version")
        remote_repo = remote_data.get("github_repo")
        
        # Normalizar versión para asegurar el prefijo 'v' (ej. "0.25" -> "v0.25")
        if remote_v and not remote_v.startswith("v"):
            remote_v = f"v{remote_v}"
            
        # 3. Sincronizar si hay cambios en el servidor maestro
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
        # Falla silenciosamente si no hay internet (arranca la versión local cacheada)
        print(f"  [WARN] Modo Offline: No se pudo verificar el Gist ({e})")
        
    return config['ARCHIE']

def download_from_github(version, repo, token):
    print(f"  [*] La version {version} no existe localmente.")
    print(f"  [*] Descargando núcleo geométrico desde GitHub...")
    
    url = f"https://github.com/{repo}/archive/refs/tags/{version}.zip"
    zip_path = os.path.join(BASE_DIR, f"temp_{version}.zip")
    
    req = urllib.request.Request(url, headers={'User-Agent': 'Archie-Bootstrapper'})
    if token:
        req.add_header("Authorization", f"token {token}")

    try:
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
        print("  [*] Extrayendo entorno...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            root_folder = zip_ref.namelist()[0].split('/')[0]
            zip_ref.extractall(VERSIONS_DIR)
            
        extracted_path = os.path.join(VERSIONS_DIR, root_folder)
        final_path = os.path.join(VERSIONS_DIR, version)
        os.rename(extracted_path, final_path)
        os.remove(zip_path)
        print(f"  [OK] Archie {version} instalado correctamente.")
        
    except Exception as e:
        print(f"  [ERROR] Falló la descarga de GitHub: {e}")
        if os.path.exists(zip_path): os.remove(zip_path)
        sys.exit(1)

def main():
    print("=======================================================")
    print("  ARCHIE: Sincronizando entorno de trabajo...")
    print("=======================================================")
    
    config = sync_config()
    
    # Sobreescritura manual por consola sigue permitida (ej: ARCHIE.BAT v0.23)
    target_version = sys.argv[1] if len(sys.argv) > 1 else config.get('target_version')
    repo = config.get('github_repo')
    token = config.get('github_token', fallback="")

    version_path = os.path.join(VERSIONS_DIR, target_version)

    if not os.path.exists(version_path):
        download_from_github(target_version, repo, token)

    print(f"  [OK] Lanzando Motor (v{target_version})...")
    print("=======================================================\n")
    
    main_script = os.path.join(version_path, "main.py")
    
    # Inyección de dependencias silenciosa
    req_file = os.path.join(version_path, "requirements.txt")
    if os.path.exists(req_file):
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"])

    # Ejecución aislada
    subprocess.run([sys.executable, main_script], cwd=version_path)

if __name__ == "__main__":
    main()