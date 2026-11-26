"""
refs_sync.py (v1.10)
Base de referencias ★ compartida por todos los equipos, en el NAS, con caché local en cada PC.

Estructura en el NAS (primera raíz accesible de nas_campaigns.raices(): \\192.168.110.242 y, si no, \\192.168.20.242):
    <raíz>\\Archie\\referencias\\refs\\<nombre>.json      una referencia por fichero
    <raíz>\\Archie\\referencias\\retiradas\\<nombre>.json  dadas de baja (no se borran; no se usan ni se vuelven a subir)
    <raíz>\\Archie\\referencias\\_aprendizaje.json         modelo aprendido (pesos, escalas...) con la firma del conjunto
    <raíz>\\Archie\\referencias\\_modelo.lock              solo un PC publica el modelo a la vez
La carpeta se puede cambiar con ARCHIE_REFS_NAS en Config\\.env (ruta completa; "0" desactiva la sincronización).

Caché local: Config\\referencias_nas\\ (+ _sync.json). El Auto-Solver lee SIEMPRE de disco local: la red solo se toca al
sincronizar (al abrir Archie, en segundo plano) y al guardar una ★.

Al abrir Archie (sincronizar):
  1. Sondea el NAS con timeout. Si no responde, se sigue con la caché.
  2. Lista refs\\ y retiradas\\ (os.scandir: nombre, tamaño y fecha en una pasada) y lo compara con _sync.json.
  3. Baja solo lo nuevo o cambiado (temporal + os.replace) y quita de la caché lo que ya no está en refs\\.
  4. Sube las ★ pendientes (guardadas sin NAS) y, una vez, las referencias de este PC y de la versión que el NAS
     aún no tiene (migración), salvo las retiradas.
  5. Si el modelo del NAS tiene la misma firma que el conjunto local, lo copia: este PC no tiene que aprender.
No se usa un manifest editado por todos (dos PCs a la vez lo corromperían): el listado de la carpeta es la verdad.
"""
import errno
import glob
import hashlib
import json
import os
import threading
import time

from log_utils import debug, log_error

SUB_NAS = ("Archie", "referencias")
REFS = "refs"
RETIRADAS = "retiradas"
CACHE_DIR = "referencias_nas"
SYNC_FILE = "_sync.json"
MODELO = "_aprendizaje.json"
LOCK = "_modelo.lock"
LOCK_STALE_S = 600
PROBE_TIMEOUT = 6

_lock = threading.Lock()
_estado = {"hecho": False, "resultado": None, "hilo": None}


# ---------------------------------------------------------------- rutas
def activo():
    return (os.getenv("ARCHIE_REFS_NAS") or "").strip() != "0"


def _isdir_timeout(path, timeout=PROBE_TIMEOUT):
    res = [False]
    t = threading.Thread(target=lambda: res.__setitem__(0, os.path.isdir(path)), daemon=True)
    t.start()
    t.join(timeout)
    return res[0]


def carpeta_nas():
    """Carpeta compartida de referencias o None si el NAS no responde (o la sincronización está desactivada)."""
    if not activo():
        return None
    env = (os.getenv("ARCHIE_REFS_NAS") or "").strip().strip('"')
    if env:
        # vale si existe la carpeta o alguno de sus 3 padres (las subcarpetas se crean al sincronizar)
        c = env.rstrip("\\/")
        for _ in range(4):
            if c and _isdir_timeout(c):
                return env
            padre = os.path.dirname(c)
            if not padre or padre == c:
                break
            c = padre
        return None
    try:
        from nas_campaigns import raices
        candidatas = raices()
    except Exception:
        candidatas = [r"\\192.168.110.242\Motion Team", r"\\192.168.20.242\Motion Team"]
    for raiz in candidatas:   # primero la 110; si no responde, la 20
        if _isdir_timeout(raiz):
            return os.path.join(raiz, *SUB_NAS)
    return None


def carpeta_cache(config_dir):
    return os.path.join(config_dir, CACHE_DIR)


def _leer_sync(config_dir):
    try:
        with open(os.path.join(carpeta_cache(config_dir), SYNC_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("ficheros", {})     # nombre -> {"size", "mtime", "sha1"} tal como estaban en el NAS
    d.setdefault("pendientes", [])   # nombres guardados con ★ que aún no están en el NAS
    d.setdefault("retiradas", [])    # nombres retirados (en el NAS o desde este PC)
    return d


def _guardar_sync(config_dir, d):
    os.makedirs(carpeta_cache(config_dir), exist_ok=True)
    _escribir_atomico(os.path.join(carpeta_cache(config_dir), SYNC_FILE),
                      json.dumps(d, indent=2, ensure_ascii=False).encode("utf-8"))


def retiradas(config_dir):
    """Nombres (en minúsculas) que el solver no debe usar aunque sigan en una carpeta local o de la versión."""
    return {n.lower() for n in _leer_sync(config_dir)["retiradas"]}


def _escribir_atomico(path, datos):
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "wb") as f:
        f.write(datos)
    os.replace(tmp, path)


def _sha1(path):
    with open(path, "rb") as f:
        return hashlib.sha1(f.read()).hexdigest()


def _copiar(src, dst):
    """Copia verificada: temporal + sha1 + os.replace (nadie lee un JSON a medio copiar)."""
    with open(src, "rb") as f:
        datos = f.read()
    _escribir_atomico(dst, datos)
    if hashlib.sha1(datos).hexdigest() != _sha1(dst):
        raise IOError(f"copia corrupta: {os.path.basename(dst)}")


def _listar(carpeta):
    out = {}
    try:
        with os.scandir(carpeta) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".json") and not e.name.startswith("_"):
                    st = e.stat()
                    out[e.name] = {"size": st.st_size, "mtime": st.st_mtime}
    except FileNotFoundError:
        pass
    return out


# ---------------------------------------------------------------- sincronización
def sincronizar(config_dir, nas=None, migrar=True):
    """-> dict resultado {"nas", "nuevas", "actualizadas", "quitadas", "subidas", "total", "error", "modelo"}."""
    res = {"nas": None, "nuevas": [], "actualizadas": [], "quitadas": [], "subidas": [], "total": 0,
           "error": None, "modelo": False}
    if not activo():
        res["error"] = "disabled"
        return res
    nas = nas or carpeta_nas()
    cache = carpeta_cache(config_dir)
    os.makedirs(cache, exist_ok=True)
    if not nas:
        res["error"] = "nas_unreachable"
        res["total"] = len(_listar(cache))
        return res
    res["nas"] = nas
    with _lock:
        d = _leer_sync(config_dir)
        try:
            os.makedirs(os.path.join(nas, REFS), exist_ok=True)
            os.makedirs(os.path.join(nas, RETIRADAS), exist_ok=True)
        except OSError as e:
            res["error"] = f"cannot create the shared folder ({e})"
            return res
        remotas = _listar(os.path.join(nas, REFS))
        retir = _listar(os.path.join(nas, RETIRADAS))
        d["retiradas"] = sorted(retir)   # la carpeta retiradas\ del NAS es la verdad
        retir_l = {n.lower() for n in d["retiradas"]}

        # 1) subir pendientes (★ guardadas cuando el NAS no respondía)
        for nombre in list(d["pendientes"]):
            src = os.path.join(cache, nombre)
            if nombre.lower() in retir_l or not os.path.isfile(src):
                d["pendientes"].remove(nombre)
                continue
            try:
                _copiar(src, os.path.join(nas, REFS, nombre))
                d["pendientes"].remove(nombre)
                res["subidas"].append(nombre)
            except OSError as e:
                log_error(f"[refs] No se pudo subir {nombre}: {e}")
        # 2) migración: referencias de este PC (Config\referencias) y de la versión que el NAS aún no tiene
        if migrar:
            remotas_l = {n.lower() for n in remotas} | {n.lower() for n in res["subidas"]}
            for carpeta in _carpetas_legacy(config_dir):
                for p in sorted(glob.glob(os.path.join(carpeta, "*.json"))):
                    nombre = os.path.basename(p)
                    if nombre.startswith("_") or nombre == "hotspot_rules.json":
                        continue
                    if nombre.lower() in remotas_l or nombre.lower() in retir_l:
                        continue
                    try:
                        _copiar(p, os.path.join(nas, REFS, nombre))
                        remotas_l.add(nombre.lower())
                        res["subidas"].append(nombre)
                    except OSError as e:
                        log_error(f"[refs] No se pudo migrar {nombre}: {e}")
        if res["subidas"]:
            remotas = _listar(os.path.join(nas, REFS))
        # 3) bajar lo nuevo o cambiado
        for nombre, meta in remotas.items():
            local = os.path.join(cache, nombre)
            prev = d["ficheros"].get(nombre)
            if prev and prev.get("size") == meta["size"] and prev.get("mtime") == meta["mtime"] and os.path.isfile(local):
                continue
            try:
                _copiar(os.path.join(nas, REFS, nombre), local)
                sha = _sha1(local)
                if prev is None and nombre not in res["subidas"]:
                    res["nuevas"].append(nombre)
                elif prev is not None and prev.get("sha1") != sha:
                    res["actualizadas"].append(nombre)
                d["ficheros"][nombre] = dict(meta, sha1=sha)
            except OSError as e:
                log_error(f"[refs] No se pudo bajar {nombre}: {e}")
        # 4) quitar de la caché lo que ya no está en el NAS (retirado o sustituido), salvo pendientes
        pend = set(d["pendientes"])
        for nombre in list(_listar(cache)):
            if nombre not in remotas and nombre not in pend:
                try:
                    os.remove(os.path.join(cache, nombre))
                    res["quitadas"].append(nombre)
                except OSError as e:
                    log_error(f"[refs] No se pudo quitar {nombre} de la caché: {e}")
                d["ficheros"].pop(nombre, None)
        _guardar_sync(config_dir, d)
        res["total"] = len(_listar(cache))
    # 5) modelo compartido
    try:
        res["modelo"] = bajar_modelo(config_dir, nas)
    except Exception as e:
        log_error(f"[refs] Modelo compartido: {e}")
    return res


def _carpetas_legacy(config_dir):
    local = os.path.join(config_dir, "referencias")
    version = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Config", "referencias")
    vistas, out = set(), []
    for c in (local, version):
        k = os.path.normcase(os.path.abspath(c))
        if k not in vistas and os.path.isdir(c):
            vistas.add(k)
            out.append(c)
    return out


def sincronizar_en_segundo_plano(config_dir, al_terminar=None):
    """Al abrir Archie: sincroniza sin bloquear y, si algo cambia, recalcula cuota y aprendizaje."""
    def tarea():
        res = None
        try:
            res = sincronizar(config_dir)
            import hotspot_solver
            hotspot_solver.configurar(config_dir)            # cuota del goal con el conjunto nuevo
            hotspot_solver.aprender_en_segundo_plano(config_dir)
        except Exception as e:
            log_error(f"[refs] Sincronización: {e}")
            res = res or {"error": str(e), "total": 0, "nuevas": [], "actualizadas": [], "quitadas": [], "subidas": []}
        _estado.update(hecho=True, resultado=res)
        debug(f"[refs] {res}")
        if al_terminar:
            try:
                al_terminar(res)
            except Exception as e:
                log_error(f"[refs] al_terminar: {e}")
    _estado.update(hecho=False, resultado=None)
    h = threading.Thread(target=tarea, daemon=True)
    _estado["hilo"] = h
    h.start()
    return h


def estado():
    return dict(_estado)


# ---------------------------------------------------------------- guardar / retirar
def subir(config_dir, path_local):
    """Tras una ★ guardada en la caché: la sube al NAS; si no puede, queda pendiente. -> True si subió."""
    nombre = os.path.basename(path_local)
    nas = carpeta_nas()
    ok = False
    if nas:
        try:
            os.makedirs(os.path.join(nas, REFS), exist_ok=True)
            # una ★ nueva con el nombre de una retirada la "resucita": la retirada se aparta (no se borra)
            vieja = os.path.join(nas, RETIRADAS, nombre)
            if os.path.isfile(vieja):
                os.replace(vieja, f"{vieja}.{time.strftime('%Y%m%d-%H%M%S')}.bak")
            dst = os.path.join(nas, REFS, nombre)
            _copiar(path_local, dst)
            st = os.stat(dst)
            ok = True
        except OSError as e:
            log_error(f"[refs] No se pudo subir {nombre}: {e}")
    with _lock:
        d = _leer_sync(config_dir)
        if ok:
            d["ficheros"][nombre] = {"size": st.st_size, "mtime": st.st_mtime, "sha1": _sha1(path_local)}
            if nombre in d["pendientes"]:
                d["pendientes"].remove(nombre)
        elif nombre not in d["pendientes"]:
            d["pendientes"].append(nombre)
        d["retiradas"] = [n for n in d["retiradas"] if n.lower() != nombre.lower()]
        _guardar_sync(config_dir, d)
    return ok


def subir_en_segundo_plano(config_dir, path_local, al_terminar=None):
    def tarea():
        ok = subir(config_dir, path_local)
        if al_terminar:
            al_terminar(ok)
    threading.Thread(target=tarea, daemon=True).start()


def retirar(config_dir, nombre):
    """Da de baja una referencia para todos: refs\\ -> retiradas\\ en el NAS y fuera de la caché. -> (ok, mensaje)"""
    nas = carpeta_nas()
    if not nas:
        return False, "the NAS is not reachable"
    src = os.path.join(nas, REFS, nombre)
    try:
        os.makedirs(os.path.join(nas, RETIRADAS), exist_ok=True)
        if os.path.isfile(src):
            os.replace(src, os.path.join(nas, RETIRADAS, nombre))
    except OSError as e:
        return False, str(e)
    with _lock:
        d = _leer_sync(config_dir)
        if nombre not in d["retiradas"]:
            d["retiradas"].append(nombre)
        d["ficheros"].pop(nombre, None)
        if nombre in d["pendientes"]:
            d["pendientes"].remove(nombre)
        _guardar_sync(config_dir, d)
    try:
        os.remove(os.path.join(carpeta_cache(config_dir), nombre))
    except OSError:
        pass
    return True, "retired"


# ---------------------------------------------------------------- modelo compartido
def bajar_modelo(config_dir, nas=None):
    """Si el modelo del NAS corresponde al conjunto de referencias local (misma firma), lo copia a la caché local
    del aprendizaje: este PC no tiene que volver a aprender. -> True si lo ha copiado."""
    nas = nas or carpeta_nas()
    if not nas:
        return False
    src = os.path.join(nas, MODELO)
    if not os.path.isfile(src):
        return False
    import hotspot_solver
    _, firma = hotspot_solver.cargar_referencias(config_dir)
    with open(src, "r", encoding="utf-8") as f:
        modelo = json.load(f)
    if modelo.get("firma") != firma:
        return False
    local = hotspot_solver.ruta_aprendizaje(config_dir)
    try:
        with open(local, "r", encoding="utf-8") as f:
            if json.load(f).get("firma") == firma:
                return False   # ya lo teníamos
    except Exception:
        pass
    os.makedirs(os.path.dirname(local), exist_ok=True)
    _escribir_atomico(local, json.dumps(modelo, indent=2, ensure_ascii=False).encode("utf-8"))
    return True


def publicar_modelo(config_dir, modelo):
    """Tras aprender: publica el modelo en el NAS si allí no está ya el de esta firma. Un solo PC a la vez (lock)."""
    nas = carpeta_nas()
    if not nas:
        return False
    dst = os.path.join(nas, MODELO)
    try:
        with open(dst, "r", encoding="utf-8") as f:
            if json.load(f).get("firma") == modelo.get("firma"):
                return False
    except Exception:
        pass
    lock = os.path.join(nas, LOCK)
    try:
        if os.path.isfile(lock) and time.time() - os.path.getmtime(lock) > LOCK_STALE_S:
            os.remove(lock)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as e:
        if e.errno == errno.EEXIST:
            return False   # otro PC está publicando
        raise
    try:
        os.write(fd, (os.getenv("COMPUTERNAME") or "pc").encode("utf-8", "replace"))
        os.close(fd)
        _escribir_atomico(dst, json.dumps(modelo, indent=2, ensure_ascii=False).encode("utf-8"))
        return True
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
