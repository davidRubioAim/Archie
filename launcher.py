import os
import sys
import zipfile
import urllib.request
import urllib.error
import shutil
import subprocess
import configparser
import json
import time
import glob
import traceback

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERSIONS_DIR = os.path.join(BASE_DIR, "versions")
os.makedirs(VERSIONS_DIR, exist_ok=True)

GIST_URL = "https://gist.githubusercontent.com/davidRubioAim/e575bfbfceef3e187410ee5e6e494a1a/raw/version.json"
DEFAULT_REPO = "davidRubioAim/Archie"

def log(tag, msg, error=False):
    prefix = "[ERROR]" if error else "[INFO]"
    print(f"  {prefix} [{tag}] {msg}")

def sync_config():
    log("SYNC", "Leyendo configuración local...")
    config = configparser.ConfigParser()
    config_path = os.path.join(BASE_DIR, 'config.ini')
    
    def write_default():
        with open(config_path, 'w') as f:
            f.write(f"[ARCHIE]\ntarget_version = v0.24\ngithub_repo = {DEFAULT_REPO}\ngithub_token = \n")

    if not os.path.exists(config_path):
        log("SYNC", "config.ini no existe. Generando valor por defecto...")
        write_default()
        
    try:
        config.read(config_path)
        if 'ARCHIE' not in config:
            raise ValueError("Sección [ARCHIE] no encontrada")
    except Exception as e:
        log("SYNC", f"config.ini inválido ({e}). Regenerando...", error=True)
        write_default()
        config.read(config_path)

    # Consulta al Gist
    try:
        url_busted = f"{GIST_URL}?t={int(time.time())}"
        log("GIST", f"Consultando estado en nube: {GIST_URL}")
        req = urllib.request.Request(url_busted, headers={'Cache-Control': 'no-cache', 'User-Agent': 'Archie-Launcher'})
        with urllib.request.urlopen(req, timeout=5) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
            
        remote_v = remote_data.get("version")
        remote_repo = remote_data.get("github_repo", DEFAULT_REPO)
        
        if remote_v and not remote_v.startswith("v"):
            remote_v = f"v{remote_v}"
            
        updated = False
        if remote_v and config['ARCHIE'].get('target_version') != remote_v:
            log("GIST", f"Nueva versión remota detectada: {remote_v}")
            config['ARCHIE']['target_version'] = remote_v
            updated = True
            
        if remote_repo and config['ARCHIE'].get('github_repo') != remote_repo:
            log("GIST", f"Nuevo repositorio detectado: {remote_repo}")
            config['ARCHIE']['github_repo'] = remote_repo
            updated = True
            
        if updated:
            with open(config_path, 'w') as f:
                config.write(f)
                
    except Exception as e:
        log("GIST", f"Falló conexión con Gist ({e}). Usando datos de config.ini local.", error=True)
        
    return config['ARCHIE']

def download_from_github(version, repo, token):
    log("DOWNLOAD", f"Iniciando descarga de {version} desde repositorio {repo}")
    
    token_clean = token.strip() if token else ""
    if token_clean:
        url = f"https://api.github.com/repos/{repo}/zipball/refs/tags/{version}"
        headers = {
            'User-Agent': 'Archie-Bootstrapper',
            'Authorization': f"Bearer {token_clean}",
            'Accept': 'application/vnd.github+json'
        }
        log("DOWNLOAD", "Usando conexión autenticada por API Token (Repo Privado).")
    else:
        url = f"https://github.com/{repo}/archive/refs/tags/{version}.zip"
        headers = {'User-Agent': 'Archie-Bootstrapper'}
        log("DOWNLOAD", "Usando descarga directa pública (Repo Público).")

    log("DOWNLOAD", f"URL Endpoint: {url}")
    zip_path = os.path.join(BASE_DIR, f"temp_{version}.zip")
    req = urllib.request.Request(url, headers=headers)

    try:
        log("DOWNLOAD", "Conectando con servidores de GitHub...")
        with urllib.request.urlopen(req) as response, open(zip_path, 'wb') as out_file:
            log("DOWNLOAD", f"Código de Respuesta HTTP: {response.status}")
            shutil.copyfileobj(response, out_file)
            
        log("ZIP", f"Descarga completa. Tamaño ZIP: {os.path.getsize(zip_path)} bytes")
        
        final_path = os.path.join(VERSIONS_DIR, version)
        if os.path.exists(final_path):
            shutil.rmtree(final_path)

        temp_extract_dir = os.path.join(VERSIONS_DIR, f"_temp_{version}")
        if os.path.exists(temp_extract_dir):
            shutil.rmtree(temp_extract_dir)

        log("ZIP", "Descomprimiendo paquete dinámico...")
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_dir)
            
        extracted_subdirs = [
            os.path.join(temp_extract_dir, d) 
            for d in os.listdir(temp_extract_dir) 
            if os.path.isdir(os.path.join(temp_extract_dir, d))
        ]
        
        if extracted_subdirs:
            log("ZIP", f"Aplanando subcarpeta contenedora de GitHub: {os.path.basename(extracted_subdirs[0])}")
            shutil.move(extracted_subdirs[0], final_path)
            shutil.rmtree(temp_extract_dir)
        else:
            os.rename(temp_extract_dir, final_path)

        os.remove(zip_path)
        log("ZIP", f"Estructura desplegada correctamente en: {final_path}")
        
    except urllib.error.HTTPError as e:
        log("DOWNLOAD", f"Error HTTP {e.code}: {e.reason}", error=True)
        log("DOWNLOAD", f"Cabeceras de Respuesta: {e.headers}", error=True)
        if os.path.exists(zip_path): os.remove(zip_path)
        sys.exit(1)
    except Exception as e:
        log("DOWNLOAD", f"Error inesperado: {e}", error=True)
        log("TRACEBACK", traceback.format_exc(), error=True)
        if os.path.exists(zip_path): os.remove(zip_path)
        sys.exit(1)

def main():
    print("=======================================================")
    print("  ARCHIE: Sincronizando entorno de trabajo (DEBUG MODE)")
    print("=======================================================")
    
    config = sync_config()
    
    target_version = sys.argv[1] if len(sys.argv) > 1 else config.get('target_version', 'v0.24')
    repo = config.get('github_repo', DEFAULT_REPO)
    token = config.get('github_token', fallback="").strip()

    version_path = os.path.join(VERSIONS_DIR, target_version)

    if not os.path.exists(version_path):
        log("MAIN", f"La versión {target_version} no existe en la carpeta /versions local.")
        download_from_github(target_version, repo, token)

    log("MAIN", f"Estructura local detectada para {target_version}")
    
    # Búsqueda estricta de Core/app.py evitando la carpeta /versions anidada
    primary_entry = os.path.join(version_path, "Core", "app.py")
    if os.path.exists(primary_entry):
        main_script = primary_entry
    else:
        log("MAIN", f"Buscando 'app.py' mediante rastreo estricto...", error=True)
        search_pattern = os.path.join(version_path, "**", "app.py")
        candidates = glob.glob(search_pattern, recursive=True)
        
        # Filtrar subcarpetas accidentales de versiones antiguas
        valid_candidates = [
            c for c in candidates 
            if f"{os.sep}versions{os.sep}" not in c 
            and "Archie_v" not in os.path.basename(os.path.dirname(c))
        ]
        
        if valid_candidates:
            main_script = valid_candidates[0]
        else:
            log("MAIN", f"No se encontró 'app.py' válido en {version_path}", error=True)
            log("MAIN", f"Contenido del directorio {version_path}: {os.listdir(version_path)}")
            sys.exit(1)

    log("MAIN", f"Punto de entrada seleccionado: {main_script}")
    script_dir = os.path.dirname(main_script)
    
    # Verificación de requerimientos
    req_file = os.path.join(version_path, "requirements.txt")
    if os.path.exists(req_file):
        log("PIP", "Instalando dependencias de requirements.txt...")
        subprocess.run([sys.executable, "-m", "pip", "install", "-r", req_file, "--quiet"])

    log("MAIN", f"Ejecutando CustomTkinter en directorio: {script_dir}")
    print("=======================================================\n")
    
    subprocess.run([sys.executable, main_script], cwd=script_dir)

if __name__ == "__main__":
    main()