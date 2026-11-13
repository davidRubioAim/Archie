"""
hotspot_solver.py (v0.61)
Auto-solver por HOTSPOTS: elige la arquitectura por puntuación, no por "primer intento sin solapes".

HOTSPOTS (posiciones del strip donde siempre queremos marca estática, para fotos y repeticiones):
  - goal_L / goal_R   tras las porterías (Goal Position del edge case o centro del Goal Line)
  - corner_L/corner_R corners del fondo = unión Goal Line / Touchline (un cluster en el GL y otro en el TL)
  - centro            centro del campo o, si hay Center Board, a ambos lados del CB (misma cantidad)
  - cerca_L/cerca_R   corners cercanos a cámara = extremos del strip. OPCIONALES: se sacrifican si así cabe
                      alguna ANIM ZONE más.
Entre hotspots: el máximo de ANIM ZONES, siempre separadas por logos (cantidad libre, 1-3).

Cada candidato se calcula con el motor de siempre (calcular_layout_data) y se puntúa con rasgos medidos
sobre el RESULTADO (no sobre la config). Así los mismos rasgos se pueden medir en un JSON de arquitectura
aprobado y Archie aprende de ellos:
  - Config/referencias/*.json  (JSON _ARCHIEtoAE o <Estadio>.json de campaña, tal cual)
  - Plantilla: la referencia más parecida (estadio + logo) fija los tamaños objetivo de cada hotspot y da
    un bonus a los candidatos con su misma estructura.
  - Pesos: se ajustan (perceptrón) para que, con la geometría de cada referencia, gane su arquitectura.
    Se guardan en Config/referencias/_aprendizaje.json y se recalculan cuando cambian las referencias.

Reglas editables (opcional): Config/hotspot_rules.json, mismas claves que REGLAS_DEFECTO.

CLI (informe de aprendizaje):  python Core\\hotspot_solver.py [carpeta_Config]
"""
import copy
import glob
import hashlib
import json
import math
import os
import sys
from datetime import datetime

from motor_distribucion import calcular_layout_data
from log_utils import debug, log_error

REFS_DIR = "referencias"
RULES_FILE = "hotspot_rules.json"
LEARN_FILE = "_aprendizaje.json"

REGLAS_DEFECTO = {
    # Tamaño del logo por aspect ratio (ancho / alto del fichero)
    "aspect_ancho": 6.0,     # >= -> logo ANCHO (v0.62: PlayStation 5.2:1 lleva 3 en el goal)
    "aspect_medio": 2.2,     # >= -> MEDIO ; por debajo -> PEQUEÑO
    # Logos objetivo por hotspot según tamaño
    "objetivo_goal": {"ancho": 1, "medio": 3, "pequeno": 5},
    "objetivo_corner_lado": {"ancho": 1, "medio": 1, "pequeno": 2},   # por lado (GL y TL)
    "objetivo_centro_lado": {"ancho": 1, "medio": 1, "pequeno": 2},   # por lado del CB (sin CB: objetivo_goal)
    # Espacio de búsqueda
    "goal_opciones": [1, 3, 5],
    "corner_opciones": [1, 2, 3],
    "centro_opciones": [1, 2, 3],
    "separador_opciones": [1, 2, 3],
    "gl_offsets": [0, 5, 10, 15],         # % del GL: desplaza los logos del goal hacia el corner del fondo
    "stretch_limit": 15,
    "corner_logo_partido": False,         # Corner Logo (logo partido GL/TL): solo si lo pedimos
    # Pesos de la puntuación (los aprende de las referencias)
    "pesos": {
        "hot_ok": 1000.0,    # hotspot obligatorio con logo y sin ANIM ZONE encima (fijo)
        "pegadas": -800.0,   # dos ANIM ZONES sin logo entre medias (fijo)
        "anims": 100.0,      # cada ANIM ZONE
        "fit": 45.0,         # cada logo de hotspot hasta su objetivo
        "exceso": -8.0,      # cada logo de hotspot por encima del objetivo
        "cerca": 30.0,       # corner cercano a cámara con logo (cada lado)
        "sep": 0.0,          # cada logo separador entre ANIM ZONES (lo decide el aprendizaje; puede ser < 0)
        "stretch": -4.0,     # % medio de compresión de las ANIM ZONES
        "offset": -2.0,      # % de desplazamiento del goal
        "plantilla": 120.0,  # bonus por parecerse a la referencia más cercana (x similitud)
    },
}
PESOS_FIJOS = ("hot_ok", "pegadas", "plantilla")
PESOS_LIBRES = ("sep",)   # pueden cambiar de signo al aprender


# ---------------------------------------------------------------- reglas y rutas
def config_dir_defecto():
    core = os.path.dirname(os.path.abspath(__file__))
    base = os.path.dirname(core)
    if os.path.basename(os.path.dirname(base)).lower() == "versions":
        base = os.path.dirname(os.path.dirname(base))
    return os.path.join(base, "Config")


_CONFIG_DIR = None


def configurar(config_dir):
    global _CONFIG_DIR
    _CONFIG_DIR = config_dir


def _cfg_dir(config_dir=None):
    return config_dir or _CONFIG_DIR or config_dir_defecto()


def cargar_reglas(config_dir=None):
    reglas = copy.deepcopy(REGLAS_DEFECTO)
    path = os.path.join(_cfg_dir(config_dir), RULES_FILE)
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                extra = json.load(f)
            for k, v in extra.items():
                if isinstance(v, dict) and isinstance(reglas.get(k), dict):
                    reglas[k].update(v)
                else:
                    reglas[k] = v
        except Exception as e:
            log_error(f"hotspot_rules.json ilegible, uso los valores por defecto: {e}")
    return reglas


def clase_logo(aspect, reglas):
    if aspect >= reglas["aspect_ancho"]:
        return "ancho"
    if aspect >= reglas["aspect_medio"]:
        return "medio"
    return "pequeno"


# ---------------------------------------------------------------- geometría y rasgos
class Geometria:
    def __init__(self, w_total, h_total, goal_line, cb_width, edge_case=None):
        from edge_cases import reservas_laterales
        self.w, self.h, self.gl, self.cb = w_total, h_total, goal_line, cb_width
        edge = edge_case or {}
        self.res_izq, self.res_der = reservas_laterales(edge, w_total)
        gp = edge.get("goal_position")
        self.goal_L = gp if gp is not None else goal_line / 2.0
        self.goal_R = w_total - gp if gp is not None else w_total - goal_line / 2.0
        self.puntos = {
            "goal_L": self.goal_L, "goal_R": self.goal_R,
            "corner_L": float(goal_line), "corner_R": float(w_total - goal_line),
            "centro": w_total / 2.0,
            "cerca_L": self.res_izq + 1.0, "cerca_R": w_total - self.res_der - 1.0,
        }


def _elementos(datos):
    anims, logos = [], []
    for z in datos["zonas"].values():
        for e in z["elementos"]:
            x0, x1 = e["x_absoluta"], e["x_absoluta"] + e["width"]
            if e["tipo"] == "anim_zone":
                anims.append((x0, x1))
            elif e["tipo"] == "static_logo":
                logos.append((x0, x1))
    anims.sort()
    logos.sort()
    return anims, logos


def _intervalo_libre(p, anims, w):
    """Tramo sin ANIM ZONES que contiene p (None si una ANIM ZONE tapa p)."""
    izq, der = 0.0, float(w)
    for a0, a1 in anims:
        if a0 <= p < a1:
            return None
        if a1 <= p:
            izq = max(izq, a1)
        elif a0 > p:
            der = min(der, a0)
    return izq, der


def rasgos(datos, geo, logo_w, anim_w, objetivos, gl_offset=0.0, under=False):
    """Rasgos medidos sobre el layout (sirven igual para candidatos y para JSON de referencia)."""
    anims, logos = _elementos(datos)
    phi = {"hot_ok": 0, "pegadas": 0, "anims": len(anims), "fit": 0, "exceso": 0, "cerca": 0, "sep": 0,
           "stretch": 0.0, "offset": float(gl_offset)}
    estructura = {}
    tol = max(1.0, logo_w * 0.5)
    libres = {}
    for nombre, p in geo.puntos.items():
        libre = _intervalo_libre(p, anims, geo.w)
        if libre and nombre.startswith("goal"):
            # el logo del goal puede ir algo desplazado: basta con que el entorno inmediato esté libre
            if any(a0 < p + tol and a1 > p - tol for a0, a1 in anims):
                libre = None
        libres[nombre] = libre
    # cada logo cuenta para el hotspot más cercano cuyo tramo libre lo contiene (sin contar dos veces)
    cuenta = {n: 0 for n in geo.puntos}
    usados = set()
    for i, (l0, l1) in enumerate(logos):
        cx = (l0 + l1) / 2.0
        mejor, dist = None, None
        for nombre, libre in libres.items():
            if libre and l0 >= libre[0] - 1 and l1 <= libre[1] + 1:
                d = abs(cx - geo.puntos[nombre])
                if dist is None or d < dist:
                    mejor, dist = nombre, d
        if mejor:
            cuenta[mejor] += 1
            usados.add(i)
    for nombre, n in cuenta.items():
        estructura[nombre] = n
        tipo = nombre.split("_")[0]
        if tipo == "cerca":
            phi["cerca"] += 1 if n > 0 else 0
            continue
        obj = objetivos[tipo]
        if n > 0:
            phi["hot_ok"] += 1
        phi["fit"] += min(n, obj)
        phi["exceso"] += max(0, n - obj)
    phi["sep"] = len(logos) - len(usados) if not under else 0
    if not under:
        for (a0, a1), (b0, b1) in zip(anims, anims[1:]):
            if not any(l0 >= a1 - 1 and l1 <= b0 + 1 for l0, l1 in logos):
                phi["pegadas"] += 1
    if anims and anim_w > 0:
        phi["stretch"] = 100.0 * sum(abs((a1 - a0) / float(anim_w) - 1.0) for a0, a1 in anims) / len(anims)
    seps = []
    for (a0, a1), (b0, b1) in zip(anims, anims[1:]):
        seps.append(sum(1 for l0, l1 in logos if l0 >= a1 - 1 and l1 <= b0 + 1))
    estructura["sep"] = sorted(seps)[len(seps) // 2] if seps else 0
    estructura["anims"] = len(anims)
    return phi, estructura


def objetivos_por_clase(clase, reglas, cb_width):
    goal = reglas["objetivo_goal"][clase]
    corner = 2 * reglas["objetivo_corner_lado"][clase]
    centro = 2 * reglas["objetivo_centro_lado"][clase] if cb_width > 0 else goal
    return {"goal": goal, "corner": corner, "centro": centro}


def puntuar(phi, pesos):
    return sum(pesos.get(k, 0.0) * float(v) for k, v in phi.items())


# ---------------------------------------------------------------- referencias
def _rasgos_caso(w, h, gl, cb, logo_w, anim_w, under):
    return [math.log(max(1e-6, logo_w / float(h))), math.log(max(1e-6, anim_w / float(max(1, logo_w)))),
            gl / float(w), cb / float(w), 1.0 if under else 0.0]


_ESCALA = [0.15, 0.35, 0.05, 0.1, 0.5]   # v0.62: el logo pesa más que el CB al buscar la plantilla


def similitud(a, b):
    d2 = sum(((x - y) / s) ** 2 for x, y, s in zip(a, b, _ESCALA))
    return math.exp(-d2 / 2.0)


def _moda(valores):
    if not valores:
        return 0
    cuenta = {}
    for v in valores:
        cuenta[v] = cuenta.get(v, 0) + 1
    return max(cuenta.items(), key=lambda kv: (kv[1], kv[0]))[0]


def leer_referencia(path):
    """JSON de arquitectura aprobado -> dict con geometría, dims, config, rasgos. None si no sirve."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        log_error(f"Referencia ilegible {os.path.basename(path)}: {e}")
        return None
    if "zonas" not in doc or "dimensiones_globales" not in doc:
        return None
    zon = doc["zonas"]
    w = int(doc["dimensiones_globales"]["width"])
    h = int(doc["dimensiones_globales"]["height"])
    gl = int(zon.get("ZONA_1", {}).get("width", 0))
    cb = int(zon.get("ZONA_3", {}).get("width", 0))
    cfg = dict(doc.get("config") or {})
    archie_cfg = (doc.get("archie") or {}).get("config") or {}
    cfg.update({k: v for k, v in archie_cfg.items() if k not in cfg})
    if cfg.get("amplify"):
        return None
    logos = [e for z in zon.values() for e in z["elementos"] if e["tipo"] == "static_logo"]
    anims = [e for z in zon.values() for e in z["elementos"] if e["tipo"] == "anim_zone"]
    if not logos:
        return None
    logo_w = _moda([e["width"] for e in logos])
    anim_w = max([e["width"] for e in anims] or [logo_w * 3])
    margin = archie_cfg.get("margin")
    if margin is None:   # se deduce: hueco mínimo entre elementos consecutivos
        todos = sorted((e["x_absoluta"], e["x_absoluta"] + e["width"]) for e in logos + anims)
        huecos = [b0 - a1 for (a0, a1), (b0, b1) in zip(todos, todos[1:]) if b0 - a1 > 0]
        margin = min(huecos) if huecos else 20
    under = bool(cfg.get("logo_under"))
    geo = Geometria(w, h, gl, cb, cfg.get("edge_case"))
    return {
        "path": path, "nombre": os.path.basename(path), "doc": doc, "geo": geo,
        "w": w, "h": h, "gl": gl, "cb": cb, "logo_w": logo_w, "anim_w": anim_w, "margin": int(margin),
        "margin_tl": cfg.get("margin_tl"), "config": cfg, "under": under,
        "caso": _rasgos_caso(w, h, gl, cb, logo_w, anim_w, under),
    }


def _firma(paths):
    hsh = hashlib.sha1()
    for p in sorted(paths):
        try:
            st = os.stat(p)
            hsh.update(f"{os.path.basename(p)}|{st.st_size}|{int(st.st_mtime)}".encode())
        except OSError:
            pass
    return hsh.hexdigest()


_CACHE = {"firma": None, "refs": []}


def carpetas_referencias(config_dir=None):
    """v0.64: 0) carpetas de ARCHIE_REFERENCIAS (Config\\.env); v0.62: 1) Config\\referencias de la instalación (las que guarda ★ en este PC);
    2) Config\\referencias de la propia versión (las que van en el repo de GitHub y llegan con cada versión).
    Si un fichero está en las dos, gana el local."""
    local = os.path.join(_cfg_dir(config_dir), REFS_DIR)
    version = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Config", REFS_DIR)
    carpetas = []
    # v0.64: carpeta(s) maestra(s) compartidas, en Config\\.env: ARCHIE_REFERENCIAS=ruta1;ruta2
    # (p. ej. la carpeta del repo o una del NAS). Tienen prioridad: es la "memoria master".
    for extra in (os.getenv("ARCHIE_REFERENCIAS") or "").split(";"):
        extra = extra.strip().strip('"')
        if extra and os.path.isdir(extra):
            carpetas.append(extra)
    carpetas += [local, version]
    unicas, vistas = [], set()
    for c in carpetas:
        k = os.path.normcase(os.path.abspath(c))
        if k not in vistas:
            vistas.add(k)
            unicas.append(c)
    return unicas


def cargar_referencias(config_dir=None):
    paths, vistos = [], set()
    for carpeta in carpetas_referencias(config_dir):
        for p in sorted(glob.glob(os.path.join(carpeta, "*.json"))):
            nombre = os.path.basename(p)
            if nombre.startswith("_") or nombre.lower() in vistos:
                continue
            vistos.add(nombre.lower())
            paths.append(p)
    firma = _firma(paths)
    if firma != _CACHE["firma"]:
        refs = [r for r in (leer_referencia(p) for p in paths) if r]
        _CACHE.update(firma=firma, refs=refs)
    return _CACHE["refs"], firma


def guardar_referencia(datos, nombre, config_dir=None):
    """Guarda la arquitectura actual como referencia aprobada (botón ★ de Manual Edit).
    v0.64: si hay carpeta maestra (ARCHIE_REFERENCIAS), se guarda allí; si no, en Config\\referencias local."""
    maestras = [c.strip().strip('"') for c in (os.getenv("ARCHIE_REFERENCIAS") or "").split(";") if c.strip()]
    maestras = [c for c in maestras if os.path.isdir(c)]
    carpeta = maestras[0] if maestras else os.path.join(_cfg_dir(config_dir), REFS_DIR)
    os.makedirs(carpeta, exist_ok=True)
    limpio = "".join(c if c.isalnum() or c in "-_ ." else "_" for c in nombre).strip() or "referencia"
    path = os.path.join(carpeta, f"{limpio}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(limpiar_referencia(datos), f, indent=2, ensure_ascii=False)
    return path


def limpiar_referencia(datos):
    """v0.62: quita las rutas de los media (NAS, nombres de cliente). El solver solo usa la geometría,
    así que la referencia se puede subir a GitHub sin exponer rutas."""
    doc = copy.deepcopy(datos)
    doc["archivos_fuente"] = {}
    if isinstance(doc.get("archie"), dict):
        doc["archie"].pop("files", None)
    return doc


# ---------------------------------------------------------------- búsqueda
def _plantilla(caso, refs):
    mejor, sim = None, 0.0
    for r in refs:
        s = similitud(caso, r["caso"])
        if s > sim:
            mejor, sim = r, s
    return mejor, sim


def _base_conf(base, reglas):
    return {
        "stretch_limit": reglas["stretch_limit"], "squash_logos": False,
        "gl_offset": base.get("gl_offset", 0),
        "corner_logo": bool(base.get("corner_logo")) if base.get("logo_under") else reglas["corner_logo_partido"],
        "logo_under": bool(base.get("logo_under")),
        "under_hotspots": bool(base.get("logo_under")),
        "qty_gl_under": int(base.get("qty_gl_under", 1)),
        "qty_tl_under": int(base.get("qty_tl_under", 1)),
        "show_cb": base.get("show_cb", True),
        "amplify": False,
        "invert_order": bool(base.get("invert_order", False)),
        "max_anim_gl": int(base.get("max_anim_gl", 0)),
        "max_anim_tl": int(base.get("max_anim_tl", 0)),
        "margin_tl": base.get("margin_tl"),
        "edge_case": base.get("edge_case"),
        "even_spacing": True,   # v0.62: clusters de hotspot repartidos entre las ANIM ZONES
    }


def _evaluar(geo, margin, archivos, conf, logo_w, anim_w, objetivos, pesos, estructura_ref, sim):
    try:
        datos = calcular_layout_data(geo.w, geo.h, geo.gl, geo.cb, margin, archivos, conf)
    except Exception as e:
        debug(f"Hotspot solver: candidato falló {e}")
        return None
    warnings = datos.get("warnings", [])
    solapa = "goal_overlap" in warnings or "touchline_overlap" in warnings
    phi, estructura = rasgos(datos, geo, logo_w, anim_w, objetivos, conf.get("gl_offset", 0), conf.get("logo_under"))
    if estructura_ref and sim > 0:
        claves = [k for k in estructura_ref if k != "anims"]
        iguales = sum(1 for k in claves if estructura.get(k) == estructura_ref.get(k))
        phi["plantilla"] = sim * iguales / float(max(1, len(claves)))
    else:
        phi["plantilla"] = 0.0
    return {"conf": conf, "phi": phi, "estructura": estructura, "solapa": solapa,
            "score": puntuar(phi, pesos), "datos": datos}


def buscar(geo, margin, archivos, base, logo_w, anim_w, reglas, pesos, refs, excluir=None, dims=None):
    """Devuelve (mejor, todos los evaluados). logo_w / anim_w ya escalados al alto del strip."""
    clase = clase_logo(logo_w / float(max(1, geo.h)), reglas)
    objetivos = objetivos_por_clase(clase, reglas, geo.cb)
    caso = _rasgos_caso(geo.w, geo.h, geo.gl, geo.cb, logo_w, anim_w, bool(base.get("logo_under")))
    ref, sim = _plantilla(caso, [r for r in refs if r is not excluir])
    estructura_ref = None
    if ref is not None and sim >= 0.5:
        estructura_ref = ref["estructura"]
        # la referencia parecida manda en los tamaños objetivo de los hotspots
        obj_ref = ref["objetivos_medidos"]
        objetivos = {k: (obj_ref[k] if obj_ref.get(k) else objetivos[k]) for k in objetivos}
    else:
        ref, sim = None, 0.0

    plantilla_conf = None
    if ref is not None:
        rc = ref["config"]
        plantilla_conf = (int(rc.get("qty_goal", 1)), int(rc.get("qty_corner", 1)), int(rc.get("qty_cb", 1)),
                          int(rc.get("qty_tl", 1)), bool(rc.get("hide_extreme_edges", False)))

    combos = set()
    for qg in reglas["goal_opciones"]:
        for qc in reglas["corner_opciones"] + [0]:
            for qcb in reglas["centro_opciones"] + [0]:
                for qtl in reglas["separador_opciones"]:
                    for hide in (False, True):
                        combos.add((qg, qc, qcb, qtl, hide))
    if plantilla_conf:
        combos.add(plantilla_conf)

    evaluados = []
    for qg, qc, qcb, qtl, hide in sorted(combos):
        conf = _base_conf(base, reglas)
        if dims:
            conf["dims"] = dims
        conf.update(qty_goal=qg, qty_corner=qc, qty_cb=qcb, qty_tl=qtl, hide_extreme_edges=hide, gl_offset=0)
        r = _evaluar(geo, margin, archivos, conf, logo_w, anim_w, objetivos, pesos, estructura_ref, sim)
        if r:
            evaluados.append(r)
    validos = sorted([r for r in evaluados if not r["solapa"]], key=lambda r: -r["score"])
    # desplazar los logos del goal hacia el corner del fondo solo en los mejores (gana si cabe algo más)
    for r in validos[:6]:
        for off in reglas["gl_offsets"]:
            if off == 0:
                continue
            conf = dict(r["conf"], gl_offset=off)
            r2 = _evaluar(geo, margin, archivos, conf, logo_w, anim_w, objetivos, pesos, estructura_ref, sim)
            if r2:
                evaluados.append(r2)
    validos = sorted([r for r in evaluados if not r["solapa"]], key=lambda r: -r["score"])
    mejor = validos[0] if validos else None
    return mejor, evaluados, {"clase": clase, "objetivos": objetivos, "ref": ref, "sim": sim}


# ---------------------------------------------------------------- aprendizaje
def _preparar_refs(refs, reglas):
    for r in refs:
        clase = clase_logo(r["logo_w"] / float(r["h"]), reglas)
        obj = objetivos_por_clase(clase, reglas, r["cb"])
        doc = r["doc"]
        phi, est = rasgos(doc, r["geo"], r["logo_w"], r["anim_w"], obj, r["config"].get("gl_offset", 0), r["under"])
        r["phi"], r["estructura"] = phi, est
        r["objetivos_medidos"] = {
            "goal": max(est["goal_L"], est["goal_R"]),
            "corner": max(est["corner_L"], est["corner_R"]),
            "centro": est["centro"],
        }


def _archivos_ref():
    return {"static_logo": "ref_logo", "anim_zones": ["ref_anim"], "anim_zone": "ref_anim"}


def _base_ref(r):
    c = r["config"]
    return {"gl_offset": 0, "logo_under": r["under"], "corner_logo": bool(c.get("corner_logo")),
            "qty_gl_under": int(c.get("qty_gl_under", 1)), "qty_tl_under": int(c.get("qty_tl_under", 1)),
            "show_cb": c.get("show_cb", True), "invert_order": bool(c.get("invert_order", False)),
            "max_anim_gl": int(c.get("max_anim_gl", 0)), "max_anim_tl": int(c.get("max_anim_tl", 0)),
            "margin_tl": r.get("margin_tl"), "edge_case": c.get("edge_case"),
            "dims": {"static_logo": [r["logo_w"], r["h"]], "anim": [r["anim_w"], r["h"]]}}


def aprender(config_dir=None, epocas=12, informe=None):
    """Ajusta los pesos para que cada referencia gane con su propia geometría. -> (pesos, líneas de informe)"""
    reglas = cargar_reglas(config_dir)
    refs, firma = cargar_referencias(config_dir)
    pesos = dict(reglas["pesos"])
    lineas = []
    if not refs:
        return pesos, ["Sin referencias en Config/referencias: se usan los pesos por defecto."]
    _preparar_refs(refs, reglas)
    aprendibles = [k for k in pesos if k not in PESOS_FIJOS]
    defecto = dict(pesos)
    lr = 0.15
    for _ in range(epocas):
        cambios = 0
        for r in refs:
            mejor, _, info = _buscar_ref(r, reglas, pesos, refs)
            if not mejor:
                continue
            phi_ref = dict(r["phi"], plantilla=0.0)
            phi_best = dict(mejor["phi"], plantilla=0.0)
            if _misma(mejor, r):
                continue
            if puntuar(phi_ref, pesos) > puntuar(phi_best, pesos):
                continue
            for k in aprendibles:
                delta = phi_ref.get(k, 0) - phi_best.get(k, 0)
                if delta:
                    escala = abs(defecto[k]) or 2.0
                    paso = lr * escala * (1 if delta > 0 else -1)
                    nuevo = pesos[k] + paso
                    # cada peso se mueve como mucho x0.25 .. x4 de su valor por defecto y no cambia de signo
                    # (los de PESOS_LIBRES: entre -4 y +4 veces su escala)
                    if k in PESOS_LIBRES:
                        lo, hi = -4 * escala, 4 * escala
                    else:
                        lo, hi = sorted((defecto[k] * 0.25, defecto[k] * 4.0))
                    pesos[k] = min(hi, max(lo, nuevo))
                    cambios += 1
        if not cambios:
            break
    # v0.62: los pesos aprendidos solo se quedan si reproducen más referencias que los de por defecto
    # (referencias contradictorias no deben estropear las reglas generales)
    def contar(p):
        return sum(1 for r in refs if (lambda m: bool(m) and _misma(m, r))(_buscar_ref(r, reglas, p, refs)[0]))
    if contar(pesos) <= contar(defecto):
        pesos = dict(defecto)
    aciertos = 0
    for r in refs:
        mejor, _, info = _buscar_ref(r, reglas, pesos, refs)
        ok = bool(mejor) and _misma(mejor, r)
        aciertos += ok
        est_ref = {k: r["estructura"][k] for k in ("goal_L", "corner_L", "centro", "cerca_L", "sep", "anims")}
        est_sol = {k: mejor["estructura"][k] for k in est_ref} if mejor else {}
        lineas.append(f"{'OK ' if ok else 'DIF'} {r['nombre']}: referencia {est_ref} | Archie {est_sol}")
    lineas.insert(0, f"Referencias: {len(refs)} · Archie reproduce {aciertos}/{len(refs)} · pesos: "
                     + ", ".join(f"{k}={v:.1f}" for k, v in pesos.items()))
    _guardar_aprendizaje(config_dir, firma, pesos, lineas)
    return pesos, lineas


def _buscar_ref(r, reglas, pesos, refs):
    """Resuelve la geometría de una referencia sin usarla a ella misma como plantilla."""
    base = _base_ref(r)
    dims = base.pop("dims")
    return buscar(r["geo"], r["margin"], _archivos_ref(), base, r["logo_w"], r["anim_w"], reglas, pesos, refs,
                  excluir=r, dims=dims)


def _misma(cand, r):
    claves = ("goal_L", "goal_R", "corner_L", "corner_R", "centro", "cerca_L", "cerca_R", "anims")
    return all(cand["estructura"].get(k) == r["estructura"].get(k) for k in claves)


def _guardar_aprendizaje(config_dir, firma, pesos, lineas):
    try:
        path = os.path.join(_cfg_dir(config_dir), REFS_DIR, LEARN_FILE)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"firma": firma, "pesos": pesos, "informe": lineas,
                       "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log_error(f"No se pudo guardar el aprendizaje: {e}")


def pesos_actuales(config_dir=None):
    """Pesos aprendidos si las referencias no han cambiado; si han cambiado, vuelve a aprender."""
    refs, firma = cargar_referencias(config_dir)
    if not refs:
        return cargar_reglas(config_dir)["pesos"]
    path = os.path.join(_cfg_dir(config_dir), REFS_DIR, LEARN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("firma") == firma:
            return data["pesos"]
    except Exception:
        pass
    pesos, lineas = aprender(config_dir)
    for l in lineas:
        print(f"  [Hotspots] {l}")
    return pesos


# ---------------------------------------------------------------- API (misma firma que auto_solver.resolver)
def _dims_reales(archivos, h_total):
    from frame_cache import FRAME_CACHE
    def dims(path, defecto):
        if not path:
            return defecto
        try:
            w, h = FRAME_CACHE.dimensions(path)
            return (w, h) if w and h else defecto
        except Exception:
            return defecto
    lw, lh = dims(archivos.get("static_logo"), (1000, 200))
    anims = [a for a in archivos.get("anim_zones") or [] if a] or ([archivos["anim_zone"]] if archivos.get("anim_zone") else [])
    aw, ah = dims(anims[0] if anims else None, (3000, 200))
    logo_w = max(1, int(lw * (h_total / float(lh)))) if lh else lw
    anim_w = max(1, int(aw * (h_total / float(ah)))) if ah else aw
    return logo_w, anim_w


def resolver(w_total, h_total, goal_line, cb_width, margin, archivos, base, config_dir=None):
    """-> (config, msg). Misma salida que auto_solver.resolver (más margin_tl / under_hotspots)."""
    reglas = cargar_reglas(config_dir)
    refs, _ = cargar_referencias(config_dir)
    if refs and "estructura" not in refs[0]:
        _preparar_refs(refs, reglas)
    pesos = pesos_actuales(config_dir)
    geo = Geometria(w_total, h_total, goal_line, cb_width, base.get("edge_case"))
    logo_w, anim_w = _dims_reales(archivos, h_total)
    mejor, evaluados, info = buscar(geo, margin, archivos, base, logo_w, anim_w, reglas, pesos, refs)
    if not mejor:
        peor = min(evaluados, key=lambda r: -r["score"]) if evaluados else None
        conf = peor["conf"] if peor else _base_conf(base, reglas)
        return _limpiar(conf), "Auto-Solver (hotspots): WARNING - Stadium too small or crowded. Overlap will occur."
    return _limpiar(mejor["conf"]), _mensaje(mejor, info)


def _limpiar(conf):
    c = dict(conf)
    c.pop("dims", None)
    c.pop("edge_case", None)   # la pone quien calcula el layout (perfil del estadio)
    return c


_NOMBRE_CLASE = {"ancho": "WIDE", "medio": "MEDIUM", "pequeno": "SMALL"}


def _mensaje(mejor, info):
    e, c = mejor["estructura"], mejor["conf"]
    partes = [f"{_NOMBRE_CLASE[info['clase']]} logo",
              f"{e['anims']} Anim Zones",
              f"logos: goal {e['goal_L']}/{e['goal_R']}, far corners {e['corner_L']}/{e['corner_R']}, "
              f"center {e['centro']}, near-camera corners {e['cerca_L']}/{e['cerca_R']}",
              f"separators {c['qty_tl']}"]
    if c.get("hide_extreme_edges"):
        partes.append("near-camera corner logos removed to fit Anim Zones")
    if c.get("gl_offset"):
        partes.append(f"goal logos shifted {c['gl_offset']}% towards the far corners")
    if info.get("ref") is not None:
        partes.append(f"template: {info['ref']['nombre']} ({info['sim']:.0%})")
    return "Auto-Solver (hotspots): " + ", ".join(partes) + "."


if __name__ == "__main__":
    cdir = sys.argv[1] if len(sys.argv) > 1 else None
    _, lineas = aprender(cdir)
    print("\n".join(lineas))
