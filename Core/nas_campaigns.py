"""
nas_campaigns.py (v0.58)
Localiza en el NAS la carpeta de cada campaña y sus ELEMENTS.

Estructura:  <raíz>\\Brands\\<BRAND>\\<CÓDIGO>\\Output\\Elements\\*.mov
  - La raíz es la primera accesible de NAS_ROOTS (o de la variable NAS_ROOTS en Config/.env, separadas por ;).
  - No se usa el nombre de la carpeta de la brand (ADIDAS, VOLKSWAGEN...): se recorren todas las carpetas
    de Brands y se indexa cada subcarpeta con nombre de código (ADID-RMXX-261001, también con _).
  - El índice código -> ruta se guarda en Config/nas_index.json para no recorrer el NAS en cada arranque.

Los ELEMENTS se clasifican con auto_import.detectar_tipo (LOGO / ANIM / BG / CB en el nombre).
Arquitecturas guardadas: <CÓDIGO>\\Output\\Elements\\Archie\\<Estadio>.json
"""
import json
import os
import re
import threading
import time

from auto_import import detectar_tipo, EXTENSIONES_ELEMENTS
from campaign_loader import normalizar_codigo, codigo_valido

NAS_ROOTS = (r"\\192.168.110.242\Motion Team", r"\\192.168.20.242\Motion Team")
BRANDS_DIR = "Brands"
ELEMENTS_REL = ("Output", "Elements")
ARCHIE_DIR = "Archie"
INDEX_FILE = "nas_index.json"
PROBE_TIMEOUT = 6      # s: un recurso SMB que no responde puede tardar ~20 s en fallar
RESCAN_MIN_S = 60      # s: una campaña que no está en el índice provoca un reescaneo, como mucho uno por minuto

REQUIRED = ("anim_zone", "static_logo", "background")
NOMBRES = {"anim_zone": "ANIM", "static_logo": "LOGO", "background": "BG", "center_board": "CB"}

# Estados de los ELEMENTS de una campaña
ST_NO_NAS = "no_nas"            # NAS inaccesible
ST_NOT_FOUND = "not_found"      # no hay carpeta de la campaña / no hay Elements
ST_INCOMPLETE = "incomplete"    # faltan BG, ANIM o LOGO
ST_REVIEW = "review"            # hay varios archivos de un mismo tipo (LOGO o BG): elegir a mano
ST_READY = "ready"


def raices():
    env = [r.strip() for r in (os.getenv("NAS_ROOTS") or "").split(";") if r.strip()]
    return env or list(NAS_ROOTS)


def _isdir_timeout(path, timeout=PROBE_TIMEOUT):
    res = [False]
    t = threading.Thread(target=lambda: res.__setitem__(0, os.path.isdir(path)), daemon=True)
    t.start()
    t.join(timeout)
    return res[0]


def safe_name(text):
    return re.sub(r'[<>:"/\\|?*]+', "_", str(text or "")).strip().strip(".") or "Stadium"


class NasIndex:
    def __init__(self, config_dir, log=print):
        self.config_dir = config_dir
        self.log = log
        self.root = None
        self.codes = {}          # CÓDIGO -> carpeta de la campaña
        self.brand_dirs = {}     # prefijo (ADID) -> carpeta de la brand (para el aviso cuando falta la campaña)
        self.scanned_at = None
        self.lock = threading.Lock()
        self.scan_lock = threading.Lock()
        self._last_scan_ts = 0.0
        self._last_scan_ok = False
        self._leer_cache()

    # ------------------------------------------------------------ caché
    def _cache_path(self):
        return os.path.join(self.config_dir, INDEX_FILE)

    def _leer_cache(self):
        try:
            with open(self._cache_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            self.codes = data.get("codes", {})
            self.brand_dirs = data.get("brand_dirs", {})
            self.scanned_at = data.get("scanned_at")
            self.root = data.get("root")
        except Exception:
            pass

    def _guardar_cache(self):
        try:
            with open(self._cache_path(), "w", encoding="utf-8") as f:
                json.dump({"root": self.root, "scanned_at": self.scanned_at, "codes": self.codes,
                           "brand_dirs": self.brand_dirs}, f, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # ------------------------------------------------------------ NAS
    def buscar_raiz(self):
        """Primera raíz con carpeta Brands accesible, o None."""
        for r in raices():
            if _isdir_timeout(os.path.join(r, BRANDS_DIR)):
                return r
        return None

    def escanear(self):
        """Recorre Brands\\*\\ y rehace el índice. Devuelve True si el NAS respondió. Bloqueante (usar en hilo)."""
        with self.scan_lock:
            ok = self._escanear()
            self._last_scan_ts, self._last_scan_ok = time.time(), ok
            return ok

    def escanear_si_toca(self):
        """Reescanea si hace más de RESCAN_MIN_S del último escaneo. Devuelve si el NAS respondió."""
        with self.scan_lock:
            if time.time() - self._last_scan_ts < RESCAN_MIN_S:
                return self._last_scan_ok
        return self.escanear()

    def _escanear(self):
        root = self.buscar_raiz()
        if not root:
            self.log("  [NAS] Ninguna raíz accesible: " + " | ".join(raices()))
            return False
        brands = os.path.join(root, BRANDS_DIR)
        codes, brand_dirs = {}, {}
        t0 = time.time()
        try:
            marcas = [e for e in os.scandir(brands) if e.is_dir()]
        except OSError as e:
            self.log(f"  [NAS] No se puede listar {brands}: {e}")
            return False
        for marca in marcas:
            try:
                subs = [e for e in os.scandir(marca.path) if e.is_dir()]
            except OSError:
                continue
            for sub in subs:
                code = normalizar_codigo(sub.name)
                if codigo_valido(code):
                    codes.setdefault(code, sub.path)
                    brand_dirs.setdefault(code[:4], marca.path)
        with self.lock:
            self.root, self.codes, self.brand_dirs = root, codes, brand_dirs
            self.scanned_at = time.strftime("%Y-%m-%d %H:%M")
        self._guardar_cache()
        self.log(f"  [NAS] {len(codes)} campañas indexadas en {len(marcas)} brands ({time.time() - t0:.1f} s).")
        return True

    def carpeta(self, code):
        with self.lock:
            return self.codes.get(code)

    def elements_dir(self, code):
        """Carpeta Output\\Elements de la campaña (exista o no). None si ni siquiera se conoce la brand."""
        base = self.carpeta(code)
        if base:
            return os.path.join(base, *ELEMENTS_REL)
        brand = self.brand_dirs.get(code[:4])
        if brand:
            return os.path.join(brand, code, *ELEMENTS_REL)
        return None

    def ruta_esperada(self, code):
        """Ruta para el aviso: la real si se conoce, si no el patrón con <BRAND>."""
        ruta = self.elements_dir(code)
        if ruta:
            return ruta
        root = self.root or raices()[0]
        return os.path.join(root, BRANDS_DIR, "<BRAND>", code, *ELEMENTS_REL)

    def archie_dir(self, code):
        ed = self.elements_dir(code)
        return os.path.join(ed, ARCHIE_DIR) if ed and self.carpeta(code) else None

    # ------------------------------------------------------------ ELEMENTS
    def inspeccionar(self, code, rescan_if_missing=True):
        """-> dict(status, files, missing, ambiguous, path). Bloqueante (usar en hilo)."""
        info = {"status": ST_NOT_FOUND, "files": None, "missing": list(REQUIRED), "ambiguous": [],
                "path": self.ruta_esperada(code)}
        if not self.carpeta(code) and rescan_if_missing:
            # Carpeta nueva desde el último escaneo (o índice vacío): se reescanea una vez
            if not self.escanear_si_toca():
                info["status"] = ST_NO_NAS
                return info
            info["path"] = self.ruta_esperada(code)
        if not self.carpeta(code):
            if not self.root and not self.buscar_raiz():
                info["status"] = ST_NO_NAS
            return info

        ed = self.elements_dir(code)
        info["path"] = ed
        if not _isdir_timeout(ed):
            if not _isdir_timeout(self.carpeta(code)):
                info["status"] = ST_NO_NAS if not self.buscar_raiz() else ST_NOT_FOUND
            return info
        try:
            nombres = sorted(os.listdir(ed), key=str.lower)
        except OSError:
            return info

        por_tipo = {"anim_zone": [], "static_logo": [], "background": [], "center_board": []}
        for n in nombres:
            ruta = os.path.join(ed, n)
            if n.startswith(".") or not n.lower().endswith(EXTENSIONES_ELEMENTS) or not os.path.isfile(ruta):
                continue
            t = detectar_tipo(n)
            if t in por_tipo:
                por_tipo[t].append(ruta)

        if not any(por_tipo.values()):
            return info   # carpeta Elements vacía = Elements no encontrados

        anims = por_tipo["anim_zone"]
        files = {"anim_zones": anims or [None], "anim_zone": anims[0] if anims else None,
                 "static_logo": None, "background": None, "center_board": None}
        ambiguous = []
        for t in ("static_logo", "background", "center_board"):
            if len(por_tipo[t]) == 1:
                files[t] = por_tipo[t][0]
            elif len(por_tipo[t]) > 1:
                ambiguous.append(t)
        missing = [t for t in REQUIRED if not por_tipo[t]]
        info.update(files=files, missing=missing, ambiguous=ambiguous,
                    status=ST_INCOMPLETE if missing else (ST_REVIEW if ambiguous else ST_READY))
        return info


# ---------------------------------------------------------------- textos (inglés, para los PMs)
def mensaje(code, info):
    st, path = info["status"], info.get("path") or ""
    if st == ST_NO_NAS:
        return ("The NAS could not be reached (" + " / ".join(raices()) + ").\n\n"
                "Please check your network or VPN connection and press Refresh.")
    if st == ST_NOT_FOUND:
        return (f"Elements not found for campaign {code}.\n\n"
                f"Please contact the Studio to request the Elements (BG, ANIM and LOGO) for this campaign "
                f"and make sure they are saved in:\n{path}\n\n"
                f"This campaign cannot be rendered until its Elements are available.")
    if st == ST_INCOMPLETE:
        faltan = ", ".join(NOMBRES[t] for t in info["missing"])
        return (f"Incomplete Elements for campaign {code}: {faltan} missing.\n\n"
                f"Please contact the Studio to request the missing Elements for this campaign "
                f"and make sure they are saved in:\n{path}\n\n"
                f"This campaign cannot be rendered until its Elements are complete.")
    if st == ST_REVIEW:
        tipos = " and ".join(NOMBRES[t] for t in info["ambiguous"])
        return (f"More than one {tipos} file was found for campaign {code}.\n\n"
                f"Open the FILES tab and drop the correct file before reviewing this campaign.")
    return ""
