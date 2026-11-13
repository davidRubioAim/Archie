"""
edge_cases.py (v0.52)
Estadios cuyo system file no sigue el estándar de la hoja (Canvas Width - Canvas Mask, filas apiladas).

Los datos viven en Config/stadium_overrides.json. Un estadio que aparece ahí NO usa su fila del Google Sheet:
su perfil sale entero del JSON. Formato de cada entrada:

  "TSG Hoffenheim": {
      "alias": ["TSG Hoffenheim FEM"],          # otros nombres que usan el mismo override (opcional)
      "nota": "texto libre",                     # opcional
      "perfil": { "Width": 39360, "Height": 192, ... mismas claves que un perfil de la hoja ...,
                  "Goal Position": 4992 },       # px absolutos desde el inicio del strip (GLL). GLR = espejo
      "zonas_negras": [[0, 3200], [36160, 39360]],          # px del strip, siempre en negro
      "tramos": [[x_ini, x_fin, x_lienzo, y_lienzo], ...]    # system file: cada tramo del strip y dónde va
  }

Todo en píxeles: un tramo puede cortar un cabinet por la mitad.
Dentro de la app, el resultado viaja en datos["config"]["edge_case"] (y así llega a los render y al JSON de AE).
"""
import json
import os

OVERRIDES_FILE = "stadium_overrides.json"


# ---------------------------------------------------------------- carga

def _validar(nombre, entrada):
    """Devuelve (perfil_edge_case | None, avisos)."""
    avisos = []
    perfil = dict(entrada.get("perfil") or {})
    w_total = int(perfil.get("Width") or 0)
    h_total = int(perfil.get("Height") or 0)
    sys_w = int(perfil.get("Canvas Width") or 0)
    sys_h = int(perfil.get("Canvas Height") or 0)
    if not (w_total and h_total and sys_w and sys_h):
        return None, [f"{nombre}: el perfil necesita Width, Height, Canvas Width y Canvas Height"]

    zonas = []
    for z in entrada.get("zonas_negras") or []:
        x1, x2 = sorted(int(v) for v in z[:2])
        x1, x2 = max(0, x1), min(w_total, x2)
        if x2 > x1:
            zonas.append([x1, x2])
        else:
            avisos.append(f"{nombre}: zona negra {z} fuera del strip, se ignora")

    tramos = []
    for t in entrada.get("tramos") or []:
        s1, s2, cx, cy = (int(v) for v in t[:4])
        if not (0 <= s1 < s2 <= w_total):
            avisos.append(f"{nombre}: tramo {t} fuera del strip (0-{w_total}), se ignora")
            continue
        if cx < 0 or cy < 0 or cx + (s2 - s1) > sys_w or cy + h_total > sys_h:
            avisos.append(f"{nombre}: tramo {t} se sale del lienzo {sys_w}x{sys_h}, se recorta")
        tramos.append([s1, s2, cx, cy])
    if tramos:
        cubierto = sorted((t[0], t[1]) for t in tramos)
        x = 0
        for a, b in cubierto:
            if a > x:
                avisos.append(f"{nombre}: los tramos no cubren el strip entre {x} y {a}")
            elif a < x:
                avisos.append(f"{nombre}: tramos solapados entre {a} y {min(x, b)}")
            x = max(x, b)
        if x < w_total:
            avisos.append(f"{nombre}: los tramos no cubren el strip entre {x} y {w_total}")

    gp = perfil.get("Goal Position")
    edge = {
        "nombre": nombre,
        "zonas_negras": zonas,
        "tramos": tramos or None,
        "goal_position": int(gp) if gp not in (None, "", 0) else None,
    }
    perfil["Edge Case"] = edge
    return perfil, avisos


def cargar_overrides(dirs, log=print):
    """Lee stadium_overrides.json de cada carpeta (las últimas ganan) y devuelve {nombre: (perfil, es_alias)}."""
    entradas = {}
    for folder in dirs:
        path = os.path.join(folder, OVERRIDES_FILE)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            entradas.update({k: v for k, v in data.items() if not k.startswith("_")})
        except Exception as e:
            log(f"  [WARN] {path} no se pudo leer ({e}); se ignora.")

    perfiles = {}
    for nombre, entrada in entradas.items():
        perfil, avisos = _validar(nombre, entrada)
        for a in avisos:
            log(f"  [EDGE CASE] {a}")
        if perfil is None:
            continue
        perfiles[nombre] = (perfil, False)
        for n in entrada.get("alias") or []:
            perfiles.setdefault(n, (perfil, True))
    return perfiles


def aplicar_overrides(stadiums, overrides, defaults, log=print):
    """Sustituye los estadios con override (y añade el nombre principal si la hoja no lo trae).
    Un alias solo sustituye: si la hoja no tiene esa fila, no se crea."""
    if not overrides:
        return stadiums
    out, usados = dict(stadiums), []
    for nombre, (perfil, es_alias) in overrides.items():
        if es_alias and nombre not in stadiums:
            continue
        p = dict(defaults)
        p.update(perfil)
        out[nombre] = p
        usados.append(nombre)
    if usados:
        log(f"  [EDGE CASE] Perfil desde {OVERRIDES_FILE} (no desde la hoja): " + ", ".join(usados))
    return out


# ---------------------------------------------------------------- uso

def edge_de(datos):
    """El edge case que viaja dentro de datos (o {} si es un estadio estándar)."""
    return (datos or {}).get("config", {}).get("edge_case") or {}


def reservas_laterales(edge, w_total):
    """(px negros al inicio del strip, px negros al final). Son los que el motor tiene que esquivar."""
    zonas = edge.get("zonas_negras") or []
    izq = 0
    for x1, x2 in sorted(zonas):
        if x1 <= izq:
            izq = max(izq, x2)
    fin = w_total
    for x1, x2 in sorted(zonas, key=lambda z: -z[1]):
        if x2 >= fin:
            fin = min(fin, x1)
    return izq, w_total - fin


def filas_system(w_total, h_slice, sys_w, sys_mask, off_x, off_y, edge=None):
    """Reparto del strip en el System File: [(x_ini, x_fin, x_lienzo, y_lienzo)].
    Con tramos explícitos (edge case) se usan tal cual; si no, el estándar de siempre."""
    tramos = (edge or {}).get("tramos")
    if tramos:
        return [tuple(t) for t in tramos]
    advance = max(1, sys_w - sys_mask)
    filas = []
    for i in range(-(-(w_total + off_x) // advance)):
        x_m = i * advance - off_x
        x1, x2 = max(0, x_m), min(w_total, x_m + advance)
        if x1 >= x2:
            continue
        filas.append((x1, x2, abs(x_m) if x_m < 0 else 0, off_y + i * h_slice))
    return filas


def restar_zonas(segmentos, zonas):
    """Quita las zonas negras de unos segmentos [(a, b, rx, ry)] (para que el reproductor no pinte encima)."""
    if not zonas:
        return list(segmentos)
    out = []
    for a, b, rx, ry in segmentos:
        piezas = [(a, b)]
        for z1, z2 in zonas:
            nuevas = []
            for p1, p2 in piezas:
                if z2 <= p1 or z1 >= p2:
                    nuevas.append((p1, p2))
                    continue
                if z1 > p1:
                    nuevas.append((p1, z1))
                if z2 < p2:
                    nuevas.append((z2, p2))
            piezas = nuevas
        out.extend((p1, p2, rx + (p1 - a), ry) for p1, p2 in piezas)
    return out


def pintar_zonas_negras(img, zonas):
    """Rellena de negro opaco las zonas negras en un strip PIL (cualquier modo)."""
    if not zonas:
        return img
    from PIL import ImageDraw
    negro = (0, 0, 0, 255) if img.mode == "RGBA" else 0
    d = ImageDraw.Draw(img)
    for x1, x2 in zonas:
        d.rectangle([x1, 0, x2 - 1, img.height - 1], fill=negro)
    return img


# ---------------------------------------------------------------- v0.61: orden de capas
def logos_bajo_anims(datos):
    """True si la arquitectura es Logo Under: los logos van DEBAJO de las ANIM ZONES (render, previews y AE).
    En el resto de modos los logos no se solapan con las ANIM ZONES y el orden da igual."""
    if (datos.get("config") or {}).get("logo_under"):
        return True
    for zona in (datos.get("zonas") or {}).values():
        for e in zona.get("elementos", []):
            if e.get("tipo") == "static_logo" and e.get("logo_under_type") == "animated":
                return True
    return False
