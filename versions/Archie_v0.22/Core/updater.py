import os
import json
import urllib.request
import zipfile

# 1. Resolución absoluta de directorios basada en la ubicación del script (Core/)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "Config")
LOCAL_VERSION_FILE = os.path.join(CONFIG_DIR, "version.json")

# ⚠️ SUSTITUYE ESTA URL POR LA URL 'RAW' DE TU GITHUB GIST
UPDATE_URL = "https://gist.githubusercontent.com/davidRubioAim/e575bfbfceef3e187410ee5e6e494a1a/raw/version.json"

def check_and_update():
    local_version = "0.0.0"
    
    # 2. Leer versión local desde la carpeta Config/
    if os.path.exists(LOCAL_VERSION_FILE):
        try:
            with open(LOCAL_VERSION_FILE, 'r') as f:
                local_version = json.load(f).get("version", "0.0.0")
        except:
            pass

    # 3. Consultar versión en la nube (Gist)
    try:
        req = urllib.request.Request(UPDATE_URL, headers={'Cache-Control': 'no-cache'})
        with urllib.request.urlopen(req, timeout=5) as response:
            remote_data = json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"  [WARN] No se pudo conectar al servidor de actualizaciones: {e}")
        return

    remote_version = remote_data.get("version", "0.0.0")
    drive_id = remote_data.get("drive_id", "")

    # 4. Lógica de actualización diferencial
    if remote_version != local_version and drive_id:
        print(f"  [>] NUEVA VERSION DETECTADA: v{local_version} -^> v{remote_version}")
        print("  [>] Descargando parche desde Google Drive. Por favor, espera...")
        
        # Endpoint de descarga directa de Google Drive
        download_url = f"https://drive.google.com/uc?export=download&id={drive_id}"
        zip_path = os.path.join(BASE_DIR, "update.zip")

        try:
            # Descargar archivo en la raíz del proyecto
            urllib.request.urlretrieve(download_url, zip_path)
            print("  [>] Extrayendo e instalando nuevos modulos...")
            
            # Extraer respetando el árbol de directorios de BASE_DIR
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(BASE_DIR)
            
            # Limpieza del ZIP temporal
            os.remove(zip_path)
            
            # Guardar la nueva versión localmente en Config/
            with open(LOCAL_VERSION_FILE, 'w') as f:
                json.dump({"version": remote_version}, f)
                
            print(f"  [OK] Archie actualizado exitosamente a la v{remote_version}.")
        except Exception as e:
            print(f"  [ERROR] Fallo critico durante la instalacion del parche: {e}")
            if os.path.exists(zip_path):
                os.remove(zip_path)
    else:
        print(f"  [OK] Archie esta actualizado a la ultima version (v{local_version}).")

if __name__ == "__main__":
    check_and_update()