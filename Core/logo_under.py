"""
logo_under.py (v1.04)
Post-procesado de la arquitectura que devuelve motor_distribucion.calcular_layout_data.

1. ANIM A / ANIM B (anims_izq_der): con varias ANIM (A, B, C...) cada ANIM ZONE coge la siguiente de izquierda
   a derecha por todo el strip, empezando por la A: A, B, A, B... (antes dependía del orden en el que el motor
   creaba las zonas).

2. LOGO UNDER sin logos a medias (logos_visibles): los logos se reparten a la misma distancia y solo se ocultan
   (quedan debajo, "animated") los que caen bajo una ANIM ZONE. Cuando la rejilla alineada del motor no se puede
   aplicar, la rejilla equidistante deja logos medio tapados por una ANIM ZONE: se animan con la ANIM y
   desaparecen aunque se vean, y a veces entre dos ANIM ZONES no queda ningún logo entero a la vista.
   Aquí, en cada tramo del strip (de un extremo al Center Board, o el strip entero sin CB) que tenga un logo
   medio tapado o un hueco entre ANIM ZONES sin logo:
     - cada hueco libre (entre ANIM ZONES, o entre el borde y una ANIM ZONE) se queda con los logos que ya
       tenía y los reparte a la misma distancia, bordes incluidos; entre dos ANIM ZONES siempre queda al menos
       un logo entero si cabe;
     - los logos que caían debajo de una ANIM ZONE se quedan enteros debajo (ocultos).
   Los tramos que ya están bien (rejilla alineada) no se tocan.
"""
import math

from edge_cases import reservas_laterales
from log_utils import debug, log_error

ZONAS_LOGOS = ("ZONA_1", "ZONA_2", "ZONA_4", "ZONA_5")


def _elementos(datos, tipo):
    return [e for z in (datos.get("zonas") or {}).values() for e in z.get("elementos", []) if e.get("tipo") == tipo]


def anims_izq_der(datos):
    """anim_idx = 0, 1, 2... de izquierda a derecha (el render usa anim_idx % nº de ANIM -> A, B, A, B...)."""
    anims = sorted(_elementos(datos, "anim_zone"), key=lambda e: (e.get("x_absoluta", 0), e.get("y") or 0))
    for i, e in enumerate(anims):
        e["anim_idx"] = i


def _solape(a0, a1, b0, b1):
    return max(0, min(a1, b1) - max(a0, b0))


def clasificar(datos):
    """logo_under_type: 'animated' si una ANIM ZONE lo pisa, 'frozen' si se ve entero."""
    anims = [(a["x_absoluta"], a["x_absoluta"] + a["width"]) for a in _elementos(datos, "anim_zone")]
    for e in _elementos(datos, "static_logo"):
        x0, x1 = e["x_absoluta"], e["x_absoluta"] + e["width"]
        tapado = any(_solape(x0, x1, a0, a1) > 0 for a0, a1 in anims)
        e["logo_under_type"] = "animated" if tapado else "frozen"
        e["freeze_on_frame_1"] = not tapado


def _repartir(a, b, k, w):
    """k logos de ancho w en [a, b] con huecos iguales, bordes incluidos."""
    if k <= 0:
        return []
    g = max(0.0, (b - a - k * w) / float(k + 1))
    return [int(round(a + g * (i + 1) + w * i)) for i in range(k)]


def _capacidad(L, w, g):
    """Logos enteros que caben en L con hueco g alrededor; al menos 1 si cabe uno sin hueco."""
    if L < w:
        return 0
    return max(1, int(math.floor((L - g) / float(w + g))))


def logos_visibles(datos, config, margin):
    if not datos or not config.get("logo_under") or config.get("amplify"):
        return
    zonas = datos.get("zonas") or {}
    logos = _elementos(datos, "static_logo")
    if not logos:
        return
    W = int((datos.get("dimensiones_globales") or {}).get("width") or 0)
    if W <= 0:
        return
    anchos = sorted(e["width"] for e in logos)
    w = anchos[len(anchos) // 2]
    g = max(1, int(margin or 0))
    res_izq, res_der = reservas_laterales(config.get("edge_case") or {}, W)
    negras = [(int(a), int(b)) for a, b in ((config.get("edge_case") or {}).get("zonas_negras") or [])]
    cbs = [e for e in _elementos(datos, "center_board") if e.get("width", 0) > 0]
    anims = sorted(_elementos(datos, "anim_zone"), key=lambda e: e["x_absoluta"])

    # tramos del strip separados por el Center Board
    if cbs:
        cb0 = min(e["x_absoluta"] for e in cbs)
        cb1 = max(e["x_absoluta"] + e["width"] for e in cbs)
        tramos = [(res_izq, cb0), (cb1, W - res_der)]
    else:
        tramos = [(res_izq, W - res_der)]

    hotspots = bool(config.get("under_hotspots"))
    cambiado = False
    for T0, T1 in tramos:
        mios = [e for e in logos if T0 <= e["x_absoluta"] + e["width"] / 2.0 <= T1]
        bloques = sorted([(a["x_absoluta"], a["x_absoluta"] + a["width"], "anim") for a in anims
                          if _solape(a["x_absoluta"], a["x_absoluta"] + a["width"], T0, T1) > 0] +
                         [(a, b, "negra") for a, b in negras if _solape(a, b, T0, T1) > 0 and a > res_izq - 1
                          and b < W - res_der + 1])
        if not mios or not any(t == "anim" for _, _, t in bloques):
            continue
        if not hotspots:
            # sin hotspots la rejilla no llega a los extremos (corners): se respeta lo que ocupaba
            T0 = max(T0, min([e["x_absoluta"] for e in mios] + [b[0] for b in bloques]))
            T1 = min(T1, max([e["x_absoluta"] + e["width"] for e in mios] + [b[1] for b in bloques]))

        # huecos libres del tramo: (inicio, fin, entre dos ANIM ZONES)
        huecos, cur, prev = [], T0, None
        for b0, b1, t in bloques:
            if b0 > cur:
                huecos.append((cur, b0, prev == "anim" and t == "anim"))
            if b1 > cur:
                cur, prev = b1, t
        if cur < T1:
            huecos.append((cur, T1, False))

        a_medias = any(0 < _solape(e["x_absoluta"], e["x_absoluta"] + e["width"], b0, b1) < e["width"]
                       for e in mios for b0, b1, t in bloques if t == "anim")
        entero = lambda e, h: e["x_absoluta"] >= h[0] and e["x_absoluta"] + e["width"] <= h[1]
        vacio = any(entre and h1 - h0 >= w and not any(entero(e, (h0, h1)) for e in mios)
                    for h0, h1, entre in huecos)
        if not a_medias and not vacio:
            continue

        # nuevo reparto: los logos de cada hueco / ANIM ZONE se quedan en ella (por su centro)
        centro = lambda e: e["x_absoluta"] + e["width"] / 2.0
        nuevos = []
        for h0, h1, entre in huecos:
            k = sum(1 for e in mios if h0 <= centro(e) < h1)
            cap = _capacidad(h1 - h0, w, g)
            if entre and cap:
                k = max(1, k)
            nuevos += _repartir(h0, h1, min(k, cap), w)
        for b0, b1, t in bloques:
            if t != "anim":
                continue
            a0, a1 = max(b0, T0), min(b1, T1)
            k = sum(1 for e in mios if b0 <= centro(e) < b1)
            k = min(k, int((a1 - a0) // w)) if a1 - a0 >= w else 0
            nuevos += _repartir(a0, a1, k, w)

        # se reasignan a sus zonas (por el centro del logo)
        ids = set(id(e) for e in mios)
        for zn in ZONAS_LOGOS:
            if zn in zonas:
                zonas[zn]["elementos"] = [e for e in zonas[zn]["elementos"] if id(e) not in ids]
        for x in nuevos:
            c = x + w / 2.0
            zn = next((z for z in ZONAS_LOGOS if z in zonas and
                       zonas[z]["x_inicio"] <= c < zonas[z]["x_inicio"] + max(1, zonas[z]["width"])), None)
            if zn is None:
                zn = min((z for z in ZONAS_LOGOS if z in zonas),
                         key=lambda z: abs(zonas[z]["x_inicio"] + zonas[z]["width"] / 2.0 - c))
            zonas[zn]["elementos"].append({"tipo": "static_logo", "width": int(w), "x_absoluta": int(x)})
        cambiado = True
        debug(f"Logo Under: tramo {int(T0)}-{int(T1)} repartido sin logos a medias ({len(mios)} -> {len(nuevos)} logos)")

    if cambiado:
        clasificar(datos)
        for z in zonas.values():
            z["elementos"].sort(key=lambda e: e["x_absoluta"])


def postprocesar(datos, config, margin):
    try:
        logos_visibles(datos, config or {}, margin)
    except Exception as e:
        log_error(f"Logo Under visibles: {e}")
    try:
        anims_izq_der(datos)
    except Exception as e:
        log_error(f"ANIM A/B: {e}")
