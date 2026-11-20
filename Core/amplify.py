"""
amplify.py  (v0.95)
Estadios de 2 rows: la 2nd row está encima de la 1st, solo a lo largo del touchline y centrada con él.
Dos modos (columna Amplify de la hoja; la hoja duplica el estadio con las dos variantes):

  AMPLIFY      Las Goal Lines son de una fila y el touchline se trata como un strip de alto "1st + 2nd":
               lleva el mismo contenido que una arquitectura normal, pero amplificado.
  SINGLE ROW   Cada fila lleva su contenido completo a su alto: la 1st row es un estadio normal de una fila y la
               2nd row se reparte aparte, como un strip propio (con su CB), con sus sliders (config "f2_*").

Strip maestro (W x H, H = h1 + h2):
    y = 0 .. h2      2nd row (solo existe físicamente entre tl2_s y tl2_e)
    y = h2 .. H      1st row (todo el perímetro)
Cada elemento lleva su caja vertical: e["y"], e["h"] (sin ellas, el alto completo, como siempre).
  AMPLIFY:    GL (ZONA_1 / ZONA_5) en y = h2, alto h1 · TL (ZONA_2 / 3 / 4) en y = 0, alto H.
  SINGLE ROW: 1st row (ZONA_1..5) en y = h2, alto h1 · 2nd row (FILA2_2 / 3 / 4, e["fila"] = 2) en y = 0, alto h2.

Reparto: pasadas del motor de siempre (misma lógica):
  - 1st row / Goal Lines: el estadio completo con alto h1.
  - TL amplificado o 2nd row: un estadio virtual con touchline = largo de la 2nd row, al alto que toque.
    Si los logos fijos de ese TL no caben, se quitan de uno en uno (ver _reducciones).

config["amplify"] = True en los dos modos (estadio de 2 rows: dos System Files) y
config["amplify_geo"] = {"modo": "amplify"|"single", "h1", "h2", "tl2", "cb2", "row2": {canvas del System File 2nd}}
Sin geometría (perfil Custom o JSON antiguos): AMPLIFY, h1 = H / 2 y la 2nd row tan larga como el touchline.
"""

ROW1, ROW2 = "1ST_ROW", "2ND_ROW"
AMPLIFY, SINGLE = "amplify", "single"
ZONAS_F2 = ("FILA2_2", "FILA2_3", "FILA2_4")
# Sliders propios de la 2nd row en SINGLE ROW -> clave de la config de la 1st row que sustituyen
F2_CLAVES = {"f2_qty_corner": "qty_corner", "f2_qty_cb": "qty_cb", "f2_qty_tl": "qty_tl",
             "f2_margin": "margin_tl", "f2_max_anim": "max_anim_tl"}

# Fila "- 2nd" de la hoja -> canvas del System File de la 2nd row
_ROW2_SYS = {"Canvas Width": "canvas_width", "Canvas Height": "canvas_height", "Canvas Mask": "canvas_mask",
             "Offset X": "offset_x", "Offset Y": "offset_y"}


def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- geometría
def geo_de_perfil(perfil, nombre=""):
    """Perfil del estadio (stadium_loader) -> config["amplify_geo"], o None si el estadio no es de 2 rows."""
    if not isinstance(perfil, dict):
        return None
    if not (perfil.get("Amplify") or "AMPLIFY" in str(nombre or "").upper()):
        return None
    row2 = perfil.get("Row 2") or {}
    h1 = _int(perfil.get("Cabinet Height")) or _int(perfil.get("Height")) // 2
    h2 = _int(row2.get("Cabinet Height")) or h1
    tl2 = _int(row2.get("Touchline Width")) or _int(row2.get("Width"))
    sys2 = {dst: _int(row2[src]) for src, dst in _ROW2_SYS.items()
            if src in row2 and (_int(row2[src]) > 0 or dst in ("canvas_mask", "offset_x", "offset_y"))}
    modo = SINGLE if (str(perfil.get("Row Mode", "")).upper() == "SINGLE ROW"
                      or "SINGLE ROW" in str(nombre or "").upper()) else AMPLIFY
    return {"modo": modo, "h1": h1, "h2": h2, "tl2": tl2 or None,
            "cb2": _int(row2.get("Center Board Width")) or None, "row2": sys2 or None}


def modo_de(geo):
    return SINGLE if (geo or {}).get("modo") == SINGLE else AMPLIFY


def resolver_geo(w_total, h_total, goal_line, geo_cfg=None, cb_width=0):
    """Geometría completa en px del strip maestro."""
    g = dict(geo_cfg or {})
    w_total, H = int(w_total), max(2, int(h_total))
    h1 = _int(g.get("h1")) or H // 2
    h1 = max(1, min(h1, H - 1))
    gl = max(0, min(int(goal_line or 0), w_total // 2))
    tl_s, tl_e = gl, w_total - gl
    tl = max(1, tl_e - tl_s)
    tl2 = max(1, min(_int(g.get("tl2")) or tl, tl))
    tl2_s = int(round(w_total / 2.0 - tl2 / 2.0))
    cb2 = _int(g.get("cb2")) if g.get("cb2") is not None else int(cb_width or 0)
    return {"modo": modo_de(g), "h1": h1, "h2": H - h1, "H": H, "tl_s": tl_s, "tl_e": tl_e,
            "tl2_s": tl2_s, "tl2_e": tl2_s + tl2, "cb2": max(0, min(cb2, tl2)), "row2": g.get("row2")}


def es_amplify(datos):
    return bool((datos or {}).get("config", {}).get("amplify")) or bool((datos or {}).get("amplify_geo"))


def geo_de_datos(datos):
    """Geometría de un layout ya calculado (o de un JSON de antes de v0.95 con amplify)."""
    if not datos:
        return None
    if datos.get("amplify_geo"):
        return datos["amplify_geo"]
    cfg = datos.get("config") or {}
    if not cfg.get("amplify"):
        return None
    dims = datos.get("dimensiones_globales") or {}
    z = datos.get("zonas") or {}
    gl = z.get("ZONA_1", {}).get("width", 0)
    return resolver_geo(dims.get("width", 0), dims.get("height", 0), gl, cfg.get("amplify_geo"),
                        z.get("ZONA_3", {}).get("width", 0))


def etiqueta(geo):
    """'AMPLIFY' / 'SINGLE ROW' para la interfaz."""
    return "SINGLE ROW" if modo_de(geo) == SINGLE else "AMPLIFY"


def rects_sin_led(datos):
    """Rectángulos (x0, y0, x1, y1) del strip sin pantalla: encima de las Goal Lines y de los tramos del TL
    que no tienen 2nd row."""
    geo = geo_de_datos(datos)
    if not geo or geo["h2"] <= 0:
        return []
    w = (datos.get("dimensiones_globales") or {}).get("width", 0)
    out = []
    if geo["tl2_s"] > 0:
        out.append((0, 0, geo["tl2_s"], geo["h2"]))
    if geo["tl2_e"] < w:
        out.append((geo["tl2_e"], 0, w, geo["h2"]))
    return out


def pintar_sin_led(img, datos):
    """Negro opaco donde no hay pantalla (PIL, cualquier modo)."""
    rects = rects_sin_led(datos)
    if not rects:
        return img
    from PIL import ImageDraw
    negro = (0, 0, 0, 255) if img.mode == "RGBA" else 0
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in rects:
        if x1 > x0 and y1 > y0:
            d.rectangle([x0, y0, x1 - 1, y1 - 1], fill=negro)
    return img


def caja(e, h_total):
    """(y, h) de un elemento en el strip maestro (sin claves: alto completo, como siempre)."""
    return int(e.get("y", 0) or 0), int(e.get("h", h_total) or h_total)


# ---------------------------------------------------------------- reparto
def config_f2(config):
    """Config de la 2nd row en SINGLE ROW: la de la 1st row con los sliders propios (f2_*) si están activos."""
    c = dict(config)
    if config.get("f2_custom"):
        for k2, k1 in F2_CLAVES.items():
            if config.get(k2) is not None:
                c[k1] = config[k2]
    c["qty_goal"] = 0
    c["hide_extreme_edges"] = True
    c["corner_logo"] = False   # los extremos de la 2nd row no son un corner: el logo va entero
    return c


def calcular_layout_amplify(calc, w_total, h_total, goal_line, cb_width, margin, archivos, config):
    """calc = motor_distribucion.calcular_layout_data (se llama con amplify=False)."""
    geo = resolver_geo(w_total, h_total, goal_line, config.get("amplify_geo"), cb_width)
    H, h1, h2 = geo["H"], geo["h1"], geo["h2"]
    single = geo["modo"] == SINGLE
    base = {c: v for c, v in config.items() if c not in ("amplify_geo", "tl_override")}
    base["amplify"] = False
    if not single:
        # AMPLIFY: el logo partido en la esquina no existe (en el corner se juntan una fila y dos), así que el
        # corner lleva su logo en el GL y otro (amplificado) en el TL
        base["corner_logo"] = False

    # 1) 1st row (SINGLE ROW: el estadio entero) / Goal Lines (AMPLIFY), con el alto de la 1st row
    d1 = calc(w_total, h1, goal_line, cb_width, margin, archivos, dict(base))
    m_tl = config.get("margin_tl")
    m_tl = margin if m_tl is None or m_tl < 0 else m_tl

    zonas = {}
    if single:
        for z, zz in d1["zonas"].items():
            zonas[z] = dict(zz, elementos=[dict(e, y=h2, h=h1) for e in zz["elementos"]])
        conf2 = config_f2(base)
        m2 = conf2.get("margin_tl")
        m2 = margin if m2 is None or m2 < 0 else m2
        z2, avisos2, reducido = _tl_virtual(calc, geo, h2, 1.0 * h2 / h1, goal_line, geo["cb2"], m2, m2, archivos,
                                            conf2, ZONAS_F2, extra={"fila": 2})
        zonas.update(z2)
        avisos = list(d1.get("warnings", []))
        avisos += [("2nd_row_" + w) for w in avisos2 if "touchline" in w]
    else:
        k = H / float(h1)
        zt, avisos2, reducido = _tl_virtual(calc, geo, H, k, goal_line, cb_width, margin * k, m_tl * k, archivos,
                                            base, ("ZONA_2", "ZONA_3", "ZONA_4"))
        if "ZONA_FALLBACK" in d1.get("zonas", {}):
            zonas = _gl_sin_logo(w_total, cb_width, margin, archivos, config, geo)
        else:
            for z in ("ZONA_1", "ZONA_5"):
                zonas[z] = dict(d1["zonas"][z])
                zonas[z]["elementos"] = [dict(e, y=h2, h=h1) for e in d1["zonas"][z]["elementos"]]
        zonas.update(zt)
        # las zonas del TL cubren todo el touchline de la 1st row (también lo que queda fuera de la 2nd)
        zonas["ZONA_2"]["width"] = int(zonas["ZONA_2"]["x_inicio"] + zonas["ZONA_2"]["width"] - geo["tl_s"])
        zonas["ZONA_2"]["x_inicio"] = geo["tl_s"]
        zonas["ZONA_4"]["width"] = int(geo["tl_e"] - zonas["ZONA_4"]["x_inicio"])
        avisos = [w for w in d1.get("warnings", []) if "touchline" not in w]
        avisos += [w for w in avisos2 if "goal" not in w and w not in avisos]

    if reducido:
        try:
            from log_utils import debug
            debug(f"{etiqueta(geo)}: TL de la 2nd row reducido a {reducido} para que quepa")
        except Exception:
            pass
    datos = dict(d1)
    datos["zonas"] = zonas
    datos["warnings"] = avisos
    datos["dimensiones_globales"] = {"width": int(w_total), "height": H}
    datos["amplify_geo"] = dict(geo, tl_reducido=reducido)
    return datos


def _tl_virtual(calc, geo, alto, k, goal_line, cb, margin_v, m_tl_v, archivos, conf, nombres, extra=None):
    """Touchline sobre el tramo de la 2nd row, como un estadio virtual de alto `alto` (k = alto / h1), colocado
    arriba del strip (y = 0). -> ({nombre: zona}, warnings, cantidades reducidas o None)"""
    tl2 = geo["tl2_e"] - geo["tl2_s"]
    gl_v = int(round(goal_line * k))
    w_v = 2 * gl_v + tl2
    c = dict(conf)
    c["edge_case"] = None                  # las zonas negras del edge case se pintan igual al final
    c["margin_tl"] = int(round(m_tl_v))
    mv = int(round(margin_v))
    d = calc(w_v, alto, gl_v, cb, mv, archivos, c)
    reducido = None
    if "touchline_overlap" in d.get("warnings", []):
        # si los logos fijos (qty_tl, qty_cb, corner) no caben, se quitan de uno en uno (siempre del que más
        # tiene) hasta que quepan: la 1st row / las Goal Lines no cambian
        for q in _reducciones(c):
            prueba = calc(w_v, alto, gl_v, cb, mv, archivos, dict(c, **q))
            if "touchline_overlap" not in prueba.get("warnings", []):
                d, reducido = prueba, q
                break
    dx = geo["tl2_s"] - gl_v
    extra = dict(extra or {}, y=0, h=int(alto))
    if "ZONA_FALLBACK" in d.get("zonas", {}):
        return _tl_sin_logo(geo, alto, cb, m_tl_v, archivos, conf, nombres, extra), d.get("warnings", []), None
    zonas = {}
    for z, nombre in zip(("ZONA_2", "ZONA_3", "ZONA_4"), nombres):
        src = d["zonas"][z]
        zonas[nombre] = dict(src, x_inicio=int(src["x_inicio"] + dx))
        zonas[nombre]["elementos"] = [dict(e, x_absoluta=int(e["x_absoluta"] + dx), **extra)
                                      for e in src["elementos"]]
    return zonas, d.get("warnings", []), reducido


def _reducciones(conf):
    """Cantidades del TL cada vez más ligeras: se resta 1 al que más tiene (empate: qty_tl, qty_cb, qty_corner)."""
    claves = ("qty_tl", "qty_cb", "qty_corner")
    q = {c: int(conf.get(c) or 0) for c in claves}
    while any(q.values()):
        c = max(claves, key=lambda c: q[c])
        q[c] -= 1
        yield dict(q)


def _anim_w(alto, archivos, config):
    from motor_distribucion import Distribuidor
    return Distribuidor(1000, alto, 0, 0, 0, archivos, dict(config, amplify=False)).anim_w


def _rellenar(a, b, aw, m, extra, contador):
    """ANIM ZONES repartidas en [a, b] (como procesar_fallback)."""
    w = b - a
    n = int((w + m) // (aw + m)) if aw > 0 else 0
    if n <= 0:
        return []
    gap = (w - n * aw) / float(n)
    out, cx = [], a + gap / 2.0
    for _ in range(n):
        out.append(dict(extra, tipo="anim_zone", width=int(aw), x_absoluta=int(cx), anim_idx=contador[0]))
        contador[0] += 1
        cx += aw + gap
    return out


def _gl_sin_logo(w_total, cb_width, margin, archivos, config, geo):
    """AMPLIFY sin logo: ANIM ZONES en las Goal Lines (1st row)."""
    from edge_cases import reservas_laterales
    res_izq, res_der = reservas_laterales(config.get("edge_case") or {}, w_total)
    aw = _anim_w(geo["h1"], archivos, config)
    ex = {"y": geo["h2"], "h": geo["h1"]}
    cont = [0]
    return {"ZONA_1": {"x_inicio": 0, "width": geo["tl_s"],
                       "elementos": _rellenar(res_izq, geo["tl_s"], aw, margin, ex, cont)},
            "ZONA_5": {"x_inicio": geo["tl_e"], "width": w_total - geo["tl_e"],
                       "elementos": _rellenar(geo["tl_e"], w_total - res_der, aw, margin, ex, cont)}}


def _tl_sin_logo(geo, alto, cb, m, archivos, config, nombres, extra):
    """Sin logo: ANIM ZONES a los dos lados del CB en el tramo de la 2nd row."""
    aw = _anim_w(alto, archivos, config)
    w_total = geo["tl2_s"] + geo["tl2_e"]          # centro = W / 2
    cb_s = int(w_total / 2.0 - cb / 2.0)
    cb_e = cb_s + int(cb)
    hueco = m / 2.0 if cb else 0
    cont = [0]
    zonas = {
        nombres[0]: {"x_inicio": geo["tl2_s"], "width": cb_s - geo["tl2_s"],
                     "elementos": _rellenar(geo["tl2_s"], cb_s - hueco, aw, m, extra, cont)},
        nombres[1]: {"x_inicio": cb_s, "width": int(cb), "elementos": []},
        nombres[2]: {"x_inicio": cb_e, "width": geo["tl2_e"] - cb_e,
                     "elementos": _rellenar(cb_e + hueco, geo["tl2_e"], aw, m, extra, cont)},
    }
    if cb > 0:
        zonas[nombres[1]]["elementos"].append(dict(extra, tipo="center_board", width=int(cb), x_absoluta=cb_s))
    return zonas


# ---------------------------------------------------------------- System Files
def filas_sistema(datos, row_target, w_total, h_total, sys):
    """Reparto del strip en el System File de una fila.

    sys: {"canvas_width", "canvas_height", "canvas_mask", "offset_x", "offset_y"} (los de la 1st row).
    -> (filas [(x_ini, x_fin, x_lienzo, y_lienzo)], (y0, y1) del strip, sys de esa fila)

    PROVISIONAL (se decidirá caso por caso):
      1ST_ROW: la franja de abajo (alto h1), todo el perímetro, con el canvas de la 1st row.
      2ND_ROW: la franja de arriba (alto h2), solo el tramo con 2nd row, con el canvas de la fila "- 2nd"
               de la hoja (Canvas Width/Height/Mask, Offset X/Y); lo que no traiga, el de la 1st row.
    Sin amplify o sin fila: el strip entero, como siempre."""
    from edge_cases import filas_system, edge_de
    geo = geo_de_datos(datos)
    if not geo or row_target not in (ROW1, ROW2):
        filas = filas_system(w_total, h_total, sys["canvas_width"], sys["canvas_mask"],
                             sys["offset_x"], sys["offset_y"], edge_de(datos))
        return filas, (0, h_total), sys
    if row_target == ROW1:
        filas = filas_system(w_total, geo["h1"], sys["canvas_width"], sys["canvas_mask"],
                             sys["offset_x"], sys["offset_y"], edge_de(datos))
        return filas, (geo["h2"], geo["H"]), sys
    sys2 = dict(sys)
    sys2.update({k: v for k, v in (geo.get("row2") or {}).items() if k in sys2})
    s = geo["tl2_s"]
    filas = [(a + s, b + s, xc, y) for a, b, xc, y in
             filas_system(geo["tl2_e"] - s, geo["h2"], sys2["canvas_width"], sys2["canvas_mask"],
                          sys2["offset_x"], sys2["offset_y"], None)]
    return filas, (0, geo["h2"]), sys2
