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
import threading
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
    try:
        aplicar_cuota_goal(config_dir)   # v1.03
    except Exception as e:
        log_error(f"cuota_goal: {e}")


def cuota_goal_de(doc):
    """v1.03: fracción del Goal Line (ZONA_1) que ocupa el bloque de logos tras la portería en un JSON de
    arquitectura: la racha de logos consecutivos que contiene el centro del GL. None si no hay ANIM ZONE a
    los dos lados (no es la arquitectura estándar)."""
    z = (doc.get("zonas") or {}).get("ZONA_1")
    if not z or not z.get("width"):
        return None
    x0, w = z["x_inicio"], float(z["width"])
    els = sorted((e for e in z["elementos"] if e["tipo"] in ("static_logo", "anim_zone")), key=lambda e: e["x_absoluta"])
    centro = x0 + w / 2.0
    i = next((j for j, e in enumerate(els) if e["x_absoluta"] <= centro <= e["x_absoluta"] + e["width"]), None)
    if i is None:   # el centro cae en un hueco: el logo más cercano
        if not els:
            return None
        i = min(range(len(els)), key=lambda j: abs(els[j]["x_absoluta"] + els[j]["width"] / 2.0 - centro))
    if els[i]["tipo"] != "static_logo":
        return None
    a = b = i
    while a > 0 and els[a - 1]["tipo"] == "static_logo":
        a -= 1
    while b < len(els) - 1 and els[b + 1]["tipo"] == "static_logo":
        b += 1
    if a == 0 or b == len(els) - 1:
        return None
    return (els[b]["x_absoluta"] + els[b]["width"] - els[a]["x_absoluta"]) / w


def cuota_goal_aprendida(config_dir=None):
    """Mediana de cuota_goal_de() en Config/referencias. Las correcciones (manual / CORRECT en el nombre)
    cuentan doble: son el criterio más reciente. -> (cuota o None, nº de referencias usadas)"""
    # v1.10: lee las mismas referencias que el solver (carpeta maestra + local + versión), no solo la local.
    # En las Amplify, doc es su equivalente de una fila: el Goal Line es el aprobado, tal cual.
    vals = []
    refs, _ = cargar_referencias(config_dir)
    for r in refs:
        doc = r["doc"]
        try:
            c = cuota_goal_de(doc)
        except Exception:
            continue
        if c is None:
            continue
        peso = 2 if (doc.get("manual") or "CORRECT" in r["nombre"].upper()) else 1
        vals += [c] * peso
    if len(vals) < 3:
        return None, len(vals)
    vals.sort()
    n = len(vals)
    med = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0
    return max(0.12, min(0.45, med)), n


def aplicar_cuota_goal(config_dir=None):
    import motor_distribucion
    cuota, n = cuota_goal_aprendida(config_dir)
    if cuota is not None:
        motor_distribucion.CUOTA_GOAL = round(cuota, 4)
        debug(f"GL estándar: cuota del goal aprendida {cuota:.3f} ({n} referencias)")
    return motor_distribucion.CUOTA_GOAL


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
def _rasgos_caso(w, h, gl, cb, logo_w, anim_w, under, amplify=False):
    """Rasgos de un caso para buscar referencias parecidas. Ninguno depende de la brand ni del estadio: solo de la
    geometría y del tamaño de los ELEMENTS. v1.10: + cuántos logos caben en el Goal Line y cuántas ANIM ZONES en
    cada touchline (las "unidades de ELEMENT" que deciden la distribución)."""
    tl = max(1.0, (w - 2.0 * gl - cb) / 2.0)
    return [math.log(max(1e-6, logo_w / float(h))), math.log(max(1e-6, anim_w / float(max(1, logo_w)))),
            gl / float(w), cb / float(w), 1.0 if under else 0.0, 1.0 if amplify else 0.0,
            math.log(max(1e-6, gl / float(max(1, logo_w)))), math.log(max(1e-6, tl / float(max(1, anim_w))))]


# v0.62: el logo pesa más que el CB al buscar la plantilla.
# v1.10: Amplify con escala 1.0: una referencia Amplify se parece más a un caso Amplify (x1 frente a x0,61), pero
# sigue sirviendo de plantilla a un estadio de una fila con geometría parecida.
# v1.10: logos en el GL y ANIM ZONES por TL, en escala logarítmica (0.3 ~ un 35% de diferencia).
_ESCALA = [0.15, 0.35, 0.05, 0.1, 0.5, 1.0, 0.3, 0.3]
_ESCALA_ACTIVA = list(_ESCALA)   # v1.10: las aprendidas (_aprendizaje.json "escalas") si mejoran a las de defecto
K_VECINOS = 3
SIM_MIN = 0.5


def similitud(a, b, escala=None):
    d2 = sum(((x - y) / s) ** 2 for x, y, s in zip(a, b, escala or _ESCALA_ACTIVA))
    return math.exp(-d2 / 2.0)


def _usar_escalas(escalas):
    global _ESCALA_ACTIVA
    _ESCALA_ACTIVA = list(escalas) if escalas and len(escalas) == len(_ESCALA) else list(_ESCALA)


def _moda(valores):
    if not valores:
        return 0
    cuenta = {}
    for v in valores:
        cuenta[v] = cuenta.get(v, 0) + 1
    return max(cuenta.items(), key=lambda kv: (kv[1], kv[0]))[0]


_MOTIVOS = {}   # v1.10: path -> por qué una referencia no se puede usar (para avisar al guardarla)


def motivo_no_usable(path):
    """v1.10: None si el solver puede aprender de la referencia; si no, el motivo (en inglés, para el chat)."""
    r = leer_referencia(path)
    return None if r else (_MOTIVOS.get(path) or "unknown format")


def _deducir_margen(elementos, defecto=20):
    todos = sorted((e["x_absoluta"], e["x_absoluta"] + e["width"]) for e in elementos)
    huecos = [b0 - a1 for (a0, a1), (b0, b1) in zip(todos, todos[1:]) if b0 - a1 > 0]
    return min(huecos) if huecos else defecto


def equivalente_una_fila(doc, cfg, margin=None):
    """v1.10: JSON de un estadio de 2 rows (Amplify) -> (doc equivalente de UNA fila al alto h1, h1, modo).
    El Auto-Solver resuelve Amplify como un estadio de una fila de alto h1 (auto_solver.resolver) y el motor aplica
    esa config a las dos filas, así que la referencia se aprende en ese mismo idioma:
      - SINGLE ROW: la 1st row (ZONA_1..5) ya es un estadio completo de una fila a alto h1: se usa tal cual.
        La 2nd row (FILA2_*) es un strip sin Goal Lines y no tiene hotspots de porterías: no se usa (de momento).
      - AMPLIFY: las Goal Lines (ZONA_1 / ZONA_5) están a alto h1: se usan tal cual (lo aprobado). El touchline
        amplificado (alto H) se rehace a alto h1 con la config aprobada y los tamaños del GL.
    -> (None, motivo, None) si no se puede."""
    zon = doc["zonas"]
    H = int(doc["dimensiones_globales"]["height"])
    w = int(doc["dimensiones_globales"]["width"])
    geo_a = doc.get("amplify_geo") or cfg.get("amplify_geo") or {}
    h1 = int(geo_a.get("h1") or H // 2)
    modo = geo_a.get("modo") or "amplify"
    fila1 = {k: copy.deepcopy(z) for k, z in zon.items() if k.startswith("ZONA_")}
    out = copy.deepcopy({k: v for k, v in doc.items() if k != "zonas"})
    out["dimensiones_globales"] = {"width": w, "height": h1}
    out["zonas"] = fila1
    if modo == "single":
        return out, h1, modo
    gl_els = [e for k in ("ZONA_1", "ZONA_5") for e in zon.get(k, {}).get("elementos", [])]
    gl_logos = [e["width"] for e in gl_els if e["tipo"] == "static_logo"]
    gl_anims = [e["width"] for e in gl_els if e["tipo"] == "anim_zone"]
    if not gl_logos:
        return None, "Amplify: no static logos on the Goal Lines", None
    logo_w = _moda(gl_logos)
    anim_w = max(gl_anims or [logo_w * 3])
    if margin is None:
        margin = _deducir_margen([e for e in gl_els if e["tipo"] in ("static_logo", "anim_zone")])
    conf = {k: v for k, v in cfg.items() if not k.startswith("f2_") and k not in ("amplify", "amplify_geo", "margin")}
    conf["amplify"] = False
    conf["dims"] = {"static_logo": [logo_w, h1], "anim": [anim_w, h1]}
    gl = int(zon.get("ZONA_1", {}).get("width", 0))
    cb = int(zon.get("ZONA_3", {}).get("width", 0))
    try:
        datos = calcular_layout_data(w, h1, gl, cb, int(margin), _archivos_ref(), conf)
    except Exception as e:
        return None, f"Amplify: could not rebuild the touchline at 1st-row height ({e})", None
    for k in ("ZONA_2", "ZONA_3", "ZONA_4"):
        if k in (datos.get("zonas") or {}):
            out["zonas"][k] = datos["zonas"][k]
    return out, h1, modo


def leer_referencia(path):
    """JSON de arquitectura aprobado -> dict con geometría, dims, config, rasgos. None si no sirve
    (el motivo queda en _MOTIVOS[path]). v1.10: las Amplify se leen como su equivalente de una fila."""
    _MOTIVOS.pop(path, None)
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        log_error(f"Referencia ilegible {os.path.basename(path)}: {e}")
        _MOTIVOS[path] = f"unreadable JSON ({e})"
        return None
    if "zonas" not in doc or "dimensiones_globales" not in doc:
        _MOTIVOS[path] = "not an architecture JSON (no zones / global dimensions)"
        return None
    cfg = dict(doc.get("config") or {})
    archie_cfg = (doc.get("archie") or {}).get("config") or {}
    cfg.update({k: v for k, v in archie_cfg.items() if k not in cfg})
    amplify, modo_amplify = bool(cfg.get("amplify")), None
    if amplify:
        doc, h_o_motivo, modo_amplify = equivalente_una_fila(doc, cfg, archie_cfg.get("margin"))
        if doc is None:
            _MOTIVOS[path] = h_o_motivo
            return None
        cfg["amplify"] = False
    zon = doc["zonas"]
    w = int(doc["dimensiones_globales"]["width"])
    h = int(doc["dimensiones_globales"]["height"])
    gl = int(zon.get("ZONA_1", {}).get("width", 0))
    cb = int(zon.get("ZONA_3", {}).get("width", 0))
    logos = [e for z in zon.values() for e in z["elementos"] if e["tipo"] == "static_logo"]
    anims = [e for z in zon.values() for e in z["elementos"] if e["tipo"] == "anim_zone"]
    if not logos:
        _MOTIVOS[path] = "it has no static logos (the Auto-Solver learns from logo positions)"
        return None
    logo_w = _moda([e["width"] for e in logos])
    anim_w = max([e["width"] for e in anims] or [logo_w * 3])
    margin = archie_cfg.get("margin")
    if margin is None:   # se deduce: hueco mínimo entre elementos consecutivos
        margin = _deducir_margen(logos + anims)
    under = bool(cfg.get("logo_under"))
    geo = Geometria(w, h, gl, cb, cfg.get("edge_case"))
    return {
        "path": path, "nombre": os.path.basename(path), "doc": doc, "geo": geo,
        "w": w, "h": h, "gl": gl, "cb": cb, "logo_w": logo_w, "anim_w": anim_w, "margin": int(margin),
        "margin_tl": cfg.get("margin_tl"), "config": cfg, "under": under,
        "amplify": amplify, "modo_amplify": modo_amplify,   # v1.10
        "caso": _rasgos_caso(w, h, gl, cb, logo_w, anim_w, under, amplify),
    }


_FIRMAS = {}   # path -> ((tamaño, mtime), sha1 del contenido)


def _firma(paths):
    """v0.75: firma por contenido (nombre + sha1), no por fecha. Al instalar una versión nueva los JSON de
    referencia se extraen con otra fecha pero son los mismos: así no se vuelve a aprender sin motivo."""
    hsh = hashlib.sha1()
    for p in sorted(paths, key=lambda x: os.path.basename(x).lower()):
        try:
            st = os.stat(p)
            k = (st.st_size, st.st_mtime)
            c = _FIRMAS.get(p)
            if c is None or c[0] != k:
                with open(p, "rb") as f:
                    c = (k, hashlib.sha1(f.read()).hexdigest())
                _FIRMAS[p] = c
            hsh.update(f"{os.path.basename(p).lower()}|{c[1]}".encode())
        except OSError:
            pass
    return hsh.hexdigest()


_CACHE = {"firma": None, "refs": []}


def carpetas_referencias(config_dir=None):
    """Carpetas de referencias, por prioridad:
      0) v0.64: carpetas de ARCHIE_REFERENCIAS (Config\\.env): la memoria compartida ("master");
      0b) v1.10: Config\\referencias_nas, copia local de la base del NAS (ver refs_sync.py);
      1) v0.62: Config\\referencias de la instalación (las que guarda ★ en este PC sin carpeta maestra);
      2) Config\\referencias de la propia versión (las del repo de GitHub, llegan con cada versión).
    v1.10: si el mismo nombre de fichero está en varias, gana la de MAYOR prioridad (maestra > local > versión)."""
    local = os.path.join(_cfg_dir(config_dir), REFS_DIR)
    version = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Config", REFS_DIR)
    carpetas = []
    # v0.64: carpeta(s) maestra(s) compartidas, en Config\\.env: ARCHIE_REFERENCIAS=ruta1;ruta2
    # (p. ej. la carpeta del repo o una del NAS). Tienen prioridad: es la "memoria master".
    for extra in (os.getenv("ARCHIE_REFERENCIAS") or "").split(";"):
        extra = extra.strip().strip('"')
        if extra and os.path.isdir(extra):
            carpetas.append(extra)
    # v1.10: copia local de la base compartida del NAS (refs_sync): se lee de disco, nunca de la red
    import refs_sync
    if refs_sync.activo():
        carpetas.append(refs_sync.carpeta_cache(_cfg_dir(config_dir)))
        # una vez migradas al NAS, las carpetas local y de la versión ya no cuentan: el NAS es la única fuente
        if refs_sync.migrado(_cfg_dir(config_dir)):
            local = version = None
    carpetas += [c for c in (local, version) if c]
    unicas, vistas = [], set()
    for c in carpetas:
        k = os.path.normcase(os.path.abspath(c))
        if k not in vistas:
            vistas.add(k)
            unicas.append(c)
    return unicas


def cargar_referencias(config_dir=None):
    import refs_sync
    vistos = set(refs_sync.retiradas(_cfg_dir(config_dir)))   # v1.10: retiradas = no se usan en ningún PC
    paths = []
    for carpeta in carpetas_referencias(config_dir):
        for p in sorted(glob.glob(os.path.join(carpeta, "*.json"))):
            nombre = os.path.basename(p)
            if nombre.startswith("_") or nombre == RULES_FILE or nombre.lower() in vistos:
                continue
            vistos.add(nombre.lower())
            paths.append(p)
    firma = _firma(paths)
    if firma != _CACHE["firma"]:
        refs = [r for r in (leer_referencia(p) for p in paths) if r]
        _CACHE.update(firma=firma, refs=refs)
    return _CACHE["refs"], firma


def _carpeta_guardado(config_dir=None):
    """ARCHIE_REFERENCIAS si está; v1.10: si no, la caché del NAS (refs_sync la sube); sin sincronización, la local."""
    maestras = [c.strip().strip('"') for c in (os.getenv("ARCHIE_REFERENCIAS") or "").split(";") if c.strip()]
    maestras = [c for c in maestras if os.path.isdir(c)]
    if maestras:
        return maestras[0]
    import refs_sync
    if refs_sync.activo():
        return refs_sync.carpeta_cache(_cfg_dir(config_dir))
    return os.path.join(_cfg_dir(config_dir), REFS_DIR)


def se_sube_al_nas(path, config_dir=None):
    """v1.10: True si la referencia guardada en path la tiene que subir refs_sync al NAS."""
    import refs_sync
    return refs_sync.activo() and os.path.normcase(os.path.dirname(os.path.abspath(path))) == \
        os.path.normcase(os.path.abspath(refs_sync.carpeta_cache(_cfg_dir(config_dir))))


def _nombre_limpio(nombre):
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in nombre).strip() or "referencia"


def _contenido_referencia(datos):
    return json.dumps(limpiar_referencia(datos), indent=2, ensure_ascii=False)


def _huella(contenido):
    """sha1 de la referencia sin el bloque archie (versión, fecha...): dos ★ de la misma arquitectura coinciden."""
    try:
        doc = json.loads(contenido)
        doc.pop("archie", None)
        contenido = json.dumps(doc, sort_keys=True, ensure_ascii=False)
    except Exception:
        pass
    return hashlib.sha1(contenido.encode("utf-8")).hexdigest()


def referencia_existente(datos, nombre, config_dir=None):
    """v1.10: antes de guardar con ★. -> (estado, path)
      ("nueva", None)       no hay ninguna referencia con ese nombre
      ("igual", path)       ya está guardada exactamente esa arquitectura
      ("distinta", path)    hay otra arquitectura con ese nombre (de este PC, de otro equipo o de la versión)"""
    objetivo = f"{_nombre_limpio(nombre)}.json".lower()
    nuevo = _huella(_contenido_referencia(datos))
    for carpeta in carpetas_referencias(config_dir):
        for p in glob.glob(os.path.join(carpeta, "*.json")):
            if os.path.basename(p).lower() != objetivo:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    viejo = _huella(f.read())
            except OSError:
                return "distinta", p
            return ("igual" if viejo == nuevo else "distinta"), p
    return "nueva", None


def guardar_referencia(datos, nombre, config_dir=None, variante=False):
    """Guarda la arquitectura actual como referencia aprobada (botón ★ de Manual Edit).
    v0.64: si hay carpeta maestra (ARCHIE_REFERENCIAS), se guarda allí; si no, en Config\\referencias local.
    v1.10: variante=True guarda junto a la que ya hay con ese nombre (<nombre>__<hash8>.json) en vez de
    sustituirla. Escritura atómica (temporal + os.replace): nadie lee un JSON a medio escribir."""
    carpeta = _carpeta_guardado(config_dir)
    os.makedirs(carpeta, exist_ok=True)
    limpio = _nombre_limpio(nombre)
    contenido = _contenido_referencia(datos)
    path = os.path.join(carpeta, f"{limpio}.json")
    if variante:
        path = os.path.join(carpeta, f"{limpio}__{_huella(contenido)[:8]}.json")
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(contenido)
    os.replace(tmp, path)
    return path


def referencia_exacta(estadio, logo, config_dir=None):
    """v1.10: la referencia ★ guardada para ESTE estadio y ESTE logo (mismo nombre que pone ★: "<estadio> - <logo>",
    o sus variantes "__hash"). Si hay varias, la más reciente. -> (config, nombre, path) o (None, None, None).
    Con ella, Create Architecture reproduce lo aprobado tal cual en vez de volver a calcular."""
    clave = _nombre_limpio(f"{estadio} - {logo}").lower()
    mejor = None
    for r in cargar_referencias(config_dir)[0]:
        base = os.path.splitext(r["nombre"])[0].lower()
        if base != clave and not base.startswith(clave + "__"):
            continue
        try:
            t = os.path.getmtime(r["path"])
        except OSError:
            t = 0
        if mejor is None or t > mejor[0]:
            mejor = (t, r)
    if mejor is None:
        return None, None, None
    r = mejor[1]
    try:
        with open(r["path"], "r", encoding="utf-8") as f:
            doc = json.load(f)   # la config tal cual (en memoria las Amplify van como su equivalente de una fila)
    except Exception:
        return None, None, None
    cfg = dict(doc.get("config") or {})
    if not cfg or "qty_goal" not in cfg:
        return None, None, None
    margin = ((doc.get("archie") or {}).get("config") or {}).get("margin")
    if margin is not None:
        cfg.setdefault("margin", margin)
    return cfg, r["nombre"], r["path"]


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


CLAVES_EST = ("goal_L", "goal_R", "corner_L", "corner_R", "centro", "cerca_L", "cerca_R", "sep", "anims")


def vecinos(caso, refs, k=None, escala=None):
    """v1.10: las k referencias más parecidas con similitud >= SIM_MIN -> [(ref, sim)], de más a menos parecida."""
    pares = sorted(((r, similitud(caso, r["caso"], escala)) for r in refs), key=lambda rs: -rs[1])
    return [(r, s) for r, s in pares[:k or K_VECINOS] if s >= SIM_MIN]


def _voto(valores_pesos):
    """Moda ponderada (a igualdad, el valor de la referencia más parecida, que va primero)."""
    tot = {}
    for v, w in valores_pesos:
        tot[v] = tot.get(v, 0.0) + w
    mejor = max(tot.values())
    return next(v for v, _ in valores_pesos if tot[v] == mejor)


def _mediana_ponderada(valores_pesos):
    vp = sorted((v, w) for v, w in valores_pesos if v)
    if not vp:
        return None
    mitad, acc = sum(w for _, w in vp) / 2.0, 0.0
    for v, w in vp:
        acc += w
        if acc >= mitad:
            return v
    return vp[-1][0]


def consenso(vec):
    """v1.10: estructura y objetivos de los k vecinos (voto ponderado por similitud), no solo de la más parecida."""
    if not vec:
        return None, None
    est = {k: _voto([(r["estructura"].get(k, 0), s) for r, s in vec]) for k in CLAVES_EST}
    obj = {}
    for k in ("goal", "corner", "centro"):
        v = _mediana_ponderada([(r["objetivos_medidos"].get(k), s) for r, s in vec])
        if v:
            obj[k] = v
    return est, obj


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
        **({"overrides": dict(base["overrides"])} if base.get("overrides") else {}),   # v1.08
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


def _cabe_mas(r2, r):
    e2, e = r2["estructura"], r["estructura"]
    if r2["phi"]["anims"] != r["phi"]["anims"] or r2["phi"]["hot_ok"] != r["phi"]["hot_ok"]:
        return r2["phi"]["anims"] > r["phi"]["anims"] or r2["phi"]["hot_ok"] > r["phi"]["hot_ok"]
    claves = ("goal_L", "goal_R", "corner_L", "corner_R", "centro", "cerca_L", "cerca_R")
    return sum(e2.get(k, 0) for k in claves) > sum(e.get(k, 0) for k in claves)


def buscar(geo, margin, archivos, base, logo_w, anim_w, reglas, pesos, refs, excluir=None, dims=None, escala=None):
    """Devuelve (mejor, todos los evaluados). logo_w / anim_w ya escalados al alto del strip."""
    clase = clase_logo(logo_w / float(max(1, geo.h)), reglas)
    objetivos = objetivos_por_clase(clase, reglas, geo.cb)
    caso = _rasgos_caso(geo.w, geo.h, geo.gl, geo.cb, logo_w, anim_w, bool(base.get("logo_under")),
                        bool(base.get("_amplify_caso")))   # v1.10
    excl = excluir if isinstance(excluir, (set, frozenset)) else ({id(excluir)} if excluir is not None else set())
    vec = vecinos(caso, [r for r in refs if id(r) not in excl], escala=escala)
    estructura_ref = None
    if vec:
        ref, sim = vec[0]
        # v1.10: los k vecinos (voto ponderado) mandan en la estructura y en los tamaños objetivo de los hotspots
        estructura_ref, obj_ref = consenso(vec)
        objetivos = {k: (obj_ref[k] if obj_ref.get(k) else objetivos[k]) for k in objetivos}
    else:
        ref, sim = None, 0.0

    # v1.10: con Logo Under, cuántos logos van bajo las ANIM ZONES (GL y TL) lo dicen las referencias Logo Under
    # parecidas (voto ponderado), no los sliders: el motor no puede deducirlo de la geometría
    under_vec = [(r, s) for r, s in (vec or []) if r["under"]] if base.get("logo_under") else []
    if under_vec:
        base = dict(base,
                    qty_gl_under=_voto([(int(r["config"].get("qty_gl_under", 1)), s) for r, s in under_vec]),
                    qty_tl_under=_voto([(int(r["config"].get("qty_tl_under", 1)), s) for r, s in under_vec]))

    plantilla_conf = None
    if ref is not None:
        rc = ref["config"]
        plantilla_conf = (int(rc.get("qty_goal", 1)), int(rc.get("qty_corner", 1)), int(rc.get("qty_cb", 1)),
                          int(rc.get("qty_tl", 1)), bool(rc.get("hide_extreme_edges", False)))

    combos = set()
    # v1.08: override confirmado en el chat (porterías sin logos): el solver no vuelve a meterlos
    sin_goal = bool((base.get("overrides") or {}).get("allow_no_goal_logos"))
    for qg in ([0] if sin_goal else reglas["goal_opciones"]):
        # v1.03: con GL estándar el corner del fondo lleva siempre su logo en el GL y otro en el TL
        for qc in reglas["corner_opciones"] + ([] if base.get("gl_estandar", True) and not base.get("logo_under") else [0]):
            for qcb in reglas["centro_opciones"] + [0]:
                for qtl in reglas["separador_opciones"]:
                    for hide in (False, True):
                        combos.add((qg, qc, qcb, qtl, hide))
    if plantilla_conf and not sin_goal:
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
    # v1.03: con GL estándar el motor ya decide el goal y el reparto del GL: sin desplazamientos
    for r in ([] if base.get("gl_estandar", True) else validos[:6]):
        for off in reglas["gl_offsets"]:
            if off == 0:
                continue
            conf = dict(r["conf"], gl_offset=off)
            r2 = _evaluar(geo, margin, archivos, conf, logo_w, anim_w, objetivos, pesos, estructura_ref, sim)
            # v0.67: el desplazamiento solo cuenta si así cabe algo más (con offset 0 las GL se reparten
            # con hueco uniforme, que es lo preferido)
            if r2 and _cabe_mas(r2, r):
                evaluados.append(r2)
    validos = sorted([r for r in evaluados if not r["solapa"]], key=lambda r: -r["score"])
    mejor = validos[0] if validos else None
    return mejor, evaluados, {"clase": clase, "objetivos": objetivos, "ref": ref, "sim": sim, "vecinos": vec}


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
            "_amplify_caso": bool(r.get("amplify")),   # v1.10
            "dims": {"static_logo": [r["logo_w"], r["h"]], "anim": [r["anim_w"], r["h"]]}}


def aprender(config_dir=None, epocas=12, informe=None):
    """Ajusta los pesos para que cada referencia gane con su propia geometría. -> (pesos, líneas de informe)"""
    reglas = cargar_reglas(config_dir)
    refs_todas, firma = cargar_referencias(config_dir)
    pesos = dict(reglas["pesos"])
    lineas = []
    if not refs_todas:
        _usar_escalas(None)
        return pesos, ["Sin referencias en Config/referencias: se usan los pesos por defecto."]
    _preparar_refs(refs_todas, reglas)
    # v1.10: duplicados (misma geometría y misma estructura) cuentan una vez; al resolver una referencia no se
    # usan como plantilla ni ella ni sus copias
    refs, grupos = _deduplicar(refs_todas)
    escalas, acierto_def, acierto_esc = aprender_escalas(refs)
    # (el aprendizaje va en un hilo: no toca las escalas activas hasta el final)
    aprendibles = [k for k in pesos if k not in PESOS_FIJOS]
    defecto = dict(pesos)
    lr = 0.15
    for _ in range(epocas):
        cambios = 0
        for r in refs:
            mejor, _, info = _buscar_ref(r, reglas, pesos, refs, escalas)
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
    def contar(p, esc=None):
        esc = esc or escalas
        return sum(1 for r in refs if (lambda m: bool(m) and _misma(m, r))(_buscar_ref(r, reglas, p, refs, esc)[0]))
    if contar(pesos) <= contar(defecto):
        pesos = dict(defecto)
    # v1.10: las escalas aprendidas solo se quedan si, con la búsqueda completa, reproducen más que las de defecto
    if escalas != _ESCALA and contar(pesos) <= contar(pesos, list(_ESCALA)):
        escalas = list(_ESCALA)
    aciertos = 0
    for r in refs:
        mejor, _, info = _buscar_ref(r, reglas, pesos, refs, escalas)
        ok = bool(mejor) and _misma(mejor, r)
        aciertos += ok
        est_ref = {k: r["estructura"][k] for k in ("goal_L", "corner_L", "centro", "cerca_L", "sep", "anims")}
        est_sol = {k: mejor["estructura"][k] for k in est_ref} if mejor else {}
        lineas.append(f"{'OK ' if ok else 'DIF'} {r['nombre']}: referencia {est_ref} | Archie {est_sol}")
    cab = [f"Referencias: {len(refs_todas)} ({len(refs)} distintas) · Archie reproduce {aciertos}/{len(refs)} · pesos: "
           + ", ".join(f"{k}={v:.1f}" for k, v in pesos.items()),
           "Escalas de similitud: " + ("aprendidas " if escalas != _ESCALA else "por defecto ")
           + ", ".join(f"{v:g}" for v in escalas)
           + f" (vecinos que aciertan la estructura: {acierto_def} -> {acierto_esc} de {len(refs)})"]
    cab += [f"DUP {', '.join(g)}" for g in grupos if len(g) > 1]
    cab += contradicciones(refs)
    cab += cobertura(refs_todas, reglas)
    lineas[:0] = cab
    _usar_escalas(escalas)
    _guardar_aprendizaje(config_dir, firma, pesos, lineas, escalas)
    return pesos, lineas


def _clave_dup(r):
    return (tuple(round(x, 3) for x in r["caso"]), tuple(r["estructura"].get(k) for k in CLAVES_EST))


def _deduplicar(refs):
    """-> (una referencia por grupo, [nombres de cada grupo]). Cada referencia guarda los id() de su grupo."""
    por_clave = {}
    for r in refs:
        por_clave.setdefault(_clave_dup(r), []).append(r)
    unicas, grupos = [], []
    for g in por_clave.values():
        ids = frozenset(id(x) for x in g)
        for x in g:
            x["_grupo"] = ids
        # se queda la corrección (CORRECT / manual) si la hay
        g.sort(key=lambda x: not (x["doc"].get("manual") or "CORRECT" in x["nombre"].upper()))
        unicas.append(g[0])
        grupos.append([x["nombre"] for x in g])
    return unicas, grupos


def _acierto_vecinos(refs, escala):
    """Cuántas referencias acierta el consenso de sus vecinos (sin ella ni sus copias): rápido, sin el motor."""
    n = 0
    for r in refs:
        otras = [x for x in refs if id(x) not in r.get("_grupo", {id(r)})]
        est, _ = consenso(vecinos(r["caso"], otras, escala=escala))
        if est and all(est.get(k) == r["estructura"].get(k) for k in CLAVES_EST if k != "sep"):
            n += 1
    return n


def aprender_escalas(refs, multiplicadores=(0.5, 2.0), pasadas=2):
    """v1.10: ajusta las escalas de similitud (descenso por coordenadas) para que los vecinos de cada referencia
    acierten su estructura. -> (escalas, aciertos con las de defecto, aciertos con las elegidas)"""
    base = list(_ESCALA)
    mejor = _acierto_vecinos(refs, base)
    inicial = mejor
    if len(refs) < 6:   # con tan pocas referencias no hay de dónde aprender
        return base, inicial, inicial
    esc = list(base)
    for _ in range(pasadas):
        cambio = False
        for i in range(len(esc)):
            for m in multiplicadores:
                prueba = list(esc)
                prueba[i] = max(base[i] / 8.0, min(base[i] * 8.0, esc[i] * m))
                a = _acierto_vecinos(refs, prueba)
                if a > mejor:
                    mejor, esc, cambio = a, prueba, True
        if not cambio:
            break
    return esc, inicial, mejor


def contradicciones(refs, umbral=0.9):
    """v1.10: referencias casi iguales en geometría que piden estructuras distintas (alguien debería retirar una)."""
    out = []
    for i, a in enumerate(refs):
        for b in refs[i + 1:]:
            s = similitud(a["caso"], b["caso"])
            if s < umbral:
                continue
            dif = [k for k in CLAVES_EST if k != "sep" and a["estructura"].get(k) != b["estructura"].get(k)]
            if dif:
                out.append(f"CONTRADICCION ({s:.0%}) {a['nombre']} vs {b['nombre']}: "
                           + ", ".join(f"{k} {a['estructura'].get(k)}/{b['estructura'].get(k)}" for k in dif))
    return out


def cobertura(refs, reglas):
    """v1.10: cuántas referencias hay por tipo de caso (logo ancho/medio/pequeño, con o sin CB, Amplify, Logo Under).
    Los tipos con 0 o 1 son los que más aporta marcar con ★."""
    cuenta = {}
    for r in refs:
        k = (clase_logo(r["logo_w"] / float(r["h"]), reglas), "CB" if r["cb"] else "sin CB",
             "Amplify" if r.get("amplify") else "1 fila", "Logo Under" if r["under"] else "normal")
        cuenta[k] = cuenta.get(k, 0) + 1
    tipos = [(c, l, a, u) for c in ("ancho", "medio", "pequeno") for l in ("CB", "sin CB")
             for a in ("1 fila", "Amplify") for u in ("normal", "Logo Under")]
    vacios = [" / ".join(t) for t in tipos if cuenta.get(t, 0) == 0]
    return ["Cobertura: " + "; ".join(f"{' / '.join(k)}: {n}" for k, n in sorted(cuenta.items()))
            + (f" · sin referencias: {len(vacios)} tipos" if vacios else "")]


def _buscar_ref(r, reglas, pesos, refs, escala=None):
    """Resuelve la geometría de una referencia sin usarla a ella misma como plantilla."""
    base = _base_ref(r)
    dims = base.pop("dims")
    return buscar(r["geo"], r["margin"], _archivos_ref(), base, r["logo_w"], r["anim_w"], reglas, pesos, refs,
                  excluir=r.get("_grupo") or {id(r)}, dims=dims, escala=escala)


def _misma(cand, r):
    claves = ("goal_L", "goal_R", "corner_L", "corner_R", "centro", "cerca_L", "cerca_R", "anims")
    return all(cand["estructura"].get(k) == r["estructura"].get(k) for k in claves)


def ruta_aprendizaje(config_dir=None):
    return os.path.join(_cfg_dir(config_dir), REFS_DIR, LEARN_FILE)


def _guardar_aprendizaje(config_dir, firma, pesos, lineas, escalas=None):
    modelo = {"firma": firma, "pesos": pesos, "escalas": list(escalas or _ESCALA), "informe": lineas,
              "fecha": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "equipo": os.getenv("COMPUTERNAME") or ""}
    try:
        path = ruta_aprendizaje(config_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(modelo, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as e:
        log_error(f"No se pudo guardar el aprendizaje: {e}")
    # v1.10: se publica en el NAS para que el resto de PCs no tengan que aprender lo mismo (en segundo plano)
    def publicar():
        try:
            import refs_sync
            if refs_sync.activo() and refs_sync.publicar_modelo(_cfg_dir(config_dir), modelo):
                debug("[refs] Modelo publicado en el NAS")
        except Exception as e:
            log_error(f"[refs] No se pudo publicar el modelo: {e}")
    if os.getenv("ARCHIE_NO_PUBLICAR") != "1":
        threading.Thread(target=publicar, daemon=True).start()


_APRENDIZAJE = {"hilo": None, "lock": threading.Lock()}


def _aprender_y_guardar(config_dir):
    pesos, lineas = aprender(config_dir)   # aprender() ya guarda el resultado
    for l in lineas:
        debug(f"[Hotspots] {l}")   # v0.75: informe técnico solo con ARCHIE_DEBUG=1
    return pesos


def aprender_en_segundo_plano(config_dir=None):
    """v0.75: si las referencias han cambiado, aprende los pesos en un hilo (al abrir la app), para que el primer
    Create Architecture no tenga que esperar."""
    with _APRENDIZAJE["lock"]:
        h = _APRENDIZAJE["hilo"]
        if h is not None and h.is_alive():
            return h
        h = threading.Thread(target=lambda: _aprender_si_hace_falta(config_dir), daemon=True)
        _APRENDIZAJE["hilo"] = h
        h.start()
        return h


def _leer_aprendizaje(config_dir):
    path = os.path.join(_cfg_dir(config_dir), REFS_DIR, LEARN_FILE)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _aprender_si_hace_falta(config_dir=None):
    try:
        refs, firma = cargar_referencias(config_dir)
        if not refs:
            return
        data = _leer_aprendizaje(config_dir)
        if data and data.get("firma") == firma:
            return
        _aprender_y_guardar(config_dir)
    except Exception as e:
        log_error(f"Hotspots: no se pudo aprender de las referencias: {e}")


def pesos_actuales(config_dir=None):
    """Pesos aprendidos si las referencias no han cambiado. v0.75: si han cambiado y ya hay unos pesos
    aprendidos, se usan esos mientras se aprende en segundo plano (no se bloquea el cálculo). Solo la primera
    vez, sin pesos guardados, se espera al aprendizaje."""
    refs, firma = cargar_referencias(config_dir)
    if not refs:
        _usar_escalas(None)
        return cargar_reglas(config_dir)["pesos"]
    data = _leer_aprendizaje(config_dir)
    if data and data.get("firma") == firma:
        _usar_escalas(data.get("escalas"))   # v1.10
        return data["pesos"]
    h = _APRENDIZAJE["hilo"]
    if data and data.get("pesos"):
        if h is None or not h.is_alive():
            aprender_en_segundo_plano(config_dir)
        if h is None or not h.is_alive() or h is threading.current_thread():
            _usar_escalas(data.get("escalas"))
        return data["pesos"]
    if h is not None and h.is_alive() and h is not threading.current_thread():
        h.join()
        data = _leer_aprendizaje(config_dir)
        if data and data.get("pesos"):
            _usar_escalas(data.get("escalas"))
            return data["pesos"]
    return _aprender_y_guardar(config_dir)


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


def _dims_conf(archivos):
    """v1.05: dimensiones reales (sin escalar) del logo y de la 1ª ANIM ZONE, leídas UNA vez por cálculo, en el
    formato de config["dims"] del motor. Así los cientos de candidatos del solver no vuelven a tocar los ficheros
    (en el NAS cada consulta era un viaje por SMB). None si falta alguno: el motor lee como siempre."""
    from frame_cache import FRAME_CACHE
    anims = [a for a in archivos.get("anim_zones") or [] if a] or ([archivos["anim_zone"]] if archivos.get("anim_zone") else [])
    out = {}
    for clave, path in (("static_logo", archivos.get("static_logo")), ("anim", anims[0] if anims else None)):
        if not path:
            return None
        try:
            w, h = FRAME_CACHE.dimensions(path)
        except Exception:
            return None
        if not (w and h):
            return None
        out[clave] = [int(w), int(h)]
    return out


def resolver(w_total, h_total, goal_line, cb_width, margin, archivos, base, config_dir=None):
    """-> (config, msg). Misma salida que auto_solver.resolver (más margin_tl / under_hotspots)."""
    reglas = cargar_reglas(config_dir)
    refs, _ = cargar_referencias(config_dir)
    if refs and "estructura" not in refs[0]:
        _preparar_refs(refs, reglas)
    pesos = pesos_actuales(config_dir)
    geo = Geometria(w_total, h_total, goal_line, cb_width, base.get("edge_case"))
    logo_w, anim_w = _dims_reales(archivos, h_total)
    mejor, evaluados, info = buscar(geo, margin, archivos, base, logo_w, anim_w, reglas, pesos, refs,
                                    dims=_dims_conf(archivos))   # v1.05
    if not mejor:
        peor = min(evaluados, key=lambda r: -r["score"]) if evaluados else None
        conf = peor["conf"] if peor else _base_conf(base, reglas)
        return _limpiar(conf), "Auto-Solver (hotspots): WARNING - Stadium too small or crowded. Overlap will occur."
    return _limpiar(mejor["conf"]), _mensaje(mejor, info)


def completar_goal(w_total, h_total, goal_line, cb_width, margin, archivos, config, config_dir=None):
    """v0.66: si una config que no viene del auto-solver (chat, batch con la config de Manual Edit, logo
    cambiado...) deja tras la portería menos logos que el objetivo del logo (1 ancho / 3 medio / 5 pequeño,
    o lo que diga la plantilla), prueba sola a subir qty_goal: mismo resto de la config, logos del goal
    desplazados hacia el corner del fondo y ANIM ZONES comprimidas hasta el stretch_limit de las reglas,
    con el espaciado repartido. Solo acepta si no hay solapes y no se pierde ninguna ANIM ZONE ni ningún
    hotspot. -> (config, mensaje o None si no cambia nada)"""
    conf0 = dict(config or {})
    if conf0.get("amplify") or conf0.get("logo_under") or not (archivos or {}).get("static_logo"):
        return config, None
    if (conf0.get("gl_estandar", True) and conf0.get("even_spacing") and not conf0.get("edge_case")
            and not (conf0.get("gl_offset") or 0) and not conf0.get("corner_logo")):
        return config, None   # v1.03: el GL estándar ya elige cuántos logos van tras la portería
    qg0 = int(conf0.get("qty_goal", 0) or 0)
    if qg0 <= 0:
        return config, None
    try:
        reglas = cargar_reglas(config_dir)
        refs, _ = cargar_referencias(config_dir)
        if refs and "estructura" not in refs[0]:
            _preparar_refs(refs, reglas)
        pesos = pesos_actuales(config_dir)
        geo = Geometria(w_total, h_total, goal_line, cb_width, conf0.get("edge_case"))
        if conf0.get("dims"):
            logo_w = conf0["dims"]["static_logo"][0] * h_total / float(conf0["dims"]["static_logo"][1])
            anim_w = conf0["dims"]["anim"][0] * h_total / float(conf0["dims"]["anim"][1])
        else:
            logo_w, anim_w = _dims_reales(archivos, h_total)
        if not logo_w:
            return config, None
        clase = clase_logo(logo_w / float(max(1, h_total)), reglas)
        objetivos = objetivos_por_clase(clase, reglas, cb_width)
        caso = _rasgos_caso(w_total, h_total, goal_line, cb_width, logo_w, anim_w or logo_w, False)
        _, obj_vec = consenso(vecinos(caso, refs))   # v1.10: k vecinos
        if obj_vec and obj_vec.get("goal"):
            objetivos = dict(objetivos, goal=obj_vec["goal"])
        meta = int(objetivos["goal"])
        dims = conf0.get("dims") or _dims_conf(archivos)   # v1.05: los ficheros se miden una sola vez
        conf_eval = dict(conf0, dims=dims) if dims else conf0

        actual = _evaluar(geo, margin, archivos, conf_eval, logo_w, anim_w, objetivos, pesos, None, 0.0)
        if not actual or actual["solapa"]:
            return config, None
        e0, p0 = actual["estructura"], actual["phi"]
        goal0 = min(e0["goal_L"], e0["goal_R"])
        if goal0 >= meta:
            return config, None

        off0 = conf0.get("gl_offset", 0) or 0
        offsets = [off0] + [o for o in reglas["gl_offsets"] if o > off0]
        st0 = conf0.get("stretch_limit", 0) or 0
        stretches = sorted({st0, max(st0, reglas["stretch_limit"])})
        mejor = None
        for qg in sorted(set(reglas["goal_opciones"]) | {meta}):
            if qg <= qg0 or qg > meta:
                continue
            for st in stretches:
                for off in offsets:
                    c = dict(conf0, qty_goal=qg, gl_offset=off, stretch_limit=st, even_spacing=True)
                    r = _evaluar(geo, margin, archivos, dict(c, dims=dims) if dims else c,
                                 logo_w, anim_w, objetivos, pesos, None, 0.0)
                    if not r or r["solapa"]:
                        continue
                    e, p = r["estructura"], r["phi"]
                    if e["anims"] < e0["anims"] or p["hot_ok"] < p0["hot_ok"] or p["pegadas"] > p0["pegadas"]:
                        continue
                    goal = min(e["goal_L"], e["goal_R"])
                    if goal <= goal0:
                        continue
                    clave = (goal, -st, -off, r["score"])   # más logos; a igualdad, menos compresión y desplazamiento
                    if mejor is None or clave > mejor[0]:
                        mejor = (clave, c, goal)
        if not mejor:
            return config, None
        _, c, goal = mejor
        partes = [f"goal logos {goal0} -> {goal} (they fit without losing Anim Zones)"]
        if c["gl_offset"] != off0:
            partes.append(f"shifted {c['gl_offset']}% towards the far corners")
        if c["stretch_limit"] != st0:
            partes.append(f"Anim Zones squashed up to {c['stretch_limit']}%")
        return c, "Auto-Solver (hotspots): " + ", ".join(partes) + "."
    except Exception as e:
        debug(f"completar_goal: {e}")
        return config, None


def _limpiar(conf):
    c = dict(conf)
    c.pop("dims", None)
    c.pop("edge_case", None)   # la pone quien calcula el layout (perfil del estadio)
    return c


_NOMBRE_CLASE = {"ancho": "WIDE", "medio": "MEDIUM", "pequeno": "SMALL"}


def _nombre_corto(nombre):
    n = os.path.splitext(nombre)[0]
    return n if len(n) <= 60 else n[:57] + "..."


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
    vec = info.get("vecinos") or ([(info["ref"], info["sim"])] if info.get("ref") is not None else [])
    if vec:   # v1.10: de qué referencias sale la decisión
        partes.append("based on " + ("similar references: " if len(vec) > 1 else "the similar reference ")
                      + "; ".join(f"{_nombre_corto(r['nombre'])} ({s:.0%})" for r, s in vec))
    return "Auto-Solver (hotspots): " + ", ".join(partes) + "."


if __name__ == "__main__":
    cdir = sys.argv[1] if len(sys.argv) > 1 else None
    _, lineas = aprender(cdir)
    print("\n".join(lineas))
