"""
v0.77: composición del vídeo PREVIEW (3D_PREVIEW.mp4) de cada campaña.

Distribución de ventanas (1920x1080, proporciones de la preview de referencia "Single Row"):

    +-------------------------------------------+-------------+
    |                                           |   CORNER    |
    |      3D PREVIEW (cámara que panea del     +-------------+
    |      centro al lado derecho y vuelve)     |    GOAL     |
    +-------------------------------------------+-------------+
    |   DETAIL: la ANIM ZONE que hay justo a la derecha del CB  |
    +---------------------------------------------------------+
    |   ARCHITECTURE: GOALLINE LEFT / TOUCHLINE / GOALLINE RIGHT |
    +---------------------------------------------------------+

Sin gradas ni césped: se mantiene la estética de la 3D Preview de Archie (fondo degradado, campo con líneas).
Todo es PIL puro; las tres vistas 3D usan vista_3d.Escena3D sin tocar ese módulo (solo se fija su cámara).
"""
import math
import os
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from PIL import Image, ImageDraw, ImageFont

from vista_3d import Escena3D, geometria_estadio

try:   # v0.82: fondo de estadio a partir de los plates de Assets/plates (sin plates, el fondo degradado de antes)
    import fondo_plates as _fp
except Exception:  # pragma: no cover
    _fp = None

# ---------------------------------------------------------------- Rejilla (px sobre 1920x1080)
REF_W, REF_H = 1920, 1080
GAP = 9                                   # separación negra entre ventanas
MAIN_W, TOP_H = 1275, 543                 # 3D PREVIEW
SIDE_SPLIT = 283                          # alto de CORNER; GOAL ocupa el resto de la columna derecha
DETAIL_H = 179                            # franja DETAIL
ARCH_MARGIN = 20                          # margen lateral de la arquitectura

COL_GAP = (0, 0, 0)
COL_ARCH_BG = (24, 24, 24)
COL_TEXT = (255, 255, 255)
COL_GUIDE = (200, 200, 200)

PAN_ZOOM_INI = 0.75     # v0.80: zoom al empezar, mirando al centro (x1 = el encuadre del círculo central + CB)
PAN_ZOOM = 1.5          # zoom en el extremo del paneo, en el goal line right
PAN_FIN_GLR = 0.5       # v0.81: el paneo termina centrado en la portería derecha (0.5 = la portería, 1 = el final del GLR)
TEX_MAX_PREVIEW = 16384  # ancho máximo de la textura de las vistas 3D
REDUCING_GAP = 2.0      # resize rápido (reduce + bilinear) para la textura y la arquitectura
PAN_BANDA_Y = 0.36      # altura (fracción desde arriba) a la que van las pantallas durante el paneo
SUPERSAMPLING = 2       # las vistas 3D se pintan al doble y se reducen: bordes sin dientes de sierra
PAN_MUESTRAS = 400        # muestras de la trayectoria del paneo
PAN_SUAVIZADO_ESQUINA = 0.06  # anchura (fracción del giro total) del redondeo de la esquina
DETAIL_ESTIRAR_GRADA = 2.5  # v0.82: en DETAIL, lo que hay por encima de las pantallas se comprime (más grada)
DETAIL_MARGEN = 0.02        # v0.91: margen (fracción del ancho visible) entre el borde izquierdo de DETAIL y el CB
DETAIL_HUECO_NEGRO = 1.0    # v0.91: hueco (x alto del LED) antes de la ANIM ZONE que cuenta como zona negra
DETAIL_LED_MAX = 0.85       # v0.91: alto máximo del LED en DETAIL (fracción de la franja)
FONDO_PLATES = True       # v0.82: estadio de Assets/plates detrás de las pantallas (False = fondo degradado de antes)
CAM1_ALTURA_M = 17.0    # cámara 1 (la de la tele): altura sobre el césped
CAM1_DISTANCIA_M = 22.0  # cámara 1: distancia por detrás del touchline cercano, en el centro del campo
FOV_MIN_PREVIEW, FOV_MAX_PREVIEW = 2.0, 70.0
METROS_CAMPO = 105.0     # largo reglamentario de referencia: convierte metros a unidades del strip


def _fuente(px):
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for cand in (os.path.join(base, "Assets", "Montserrat-Bold.ttf"),
                 os.path.join(os.path.dirname(os.path.dirname(base)), "Assets", "Montserrat-Bold.ttf")):
        if os.path.isfile(cand):
            try:
                return ImageFont.truetype(cand, px)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=px)
    except TypeError:   # Pillow < 10.1
        return ImageFont.load_default()


def _suave(t):
    t = max(0.0, min(1.0, t))
    return t * t * t * (t * (t * 6 - 15) + 10)   # smootherstep


def _lerp(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def _apuntar(e3d, eye, objetivo, fov):
    """Coloca la cámara de una Escena3D en `eye` mirando a `objetivo` con un FOV vertical dado (grados)."""
    v = (eye[0] - objetivo[0], eye[1] - objetivo[1], eye[2] - objetivo[2])
    d = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
    e3d.yaw = math.atan2(v[0] / d, v[2] / d)
    e3d.pitch = math.asin(max(-1.0, min(1.0, v[1] / d)))
    e3d.dist = d
    e3d.target = tuple(objetivo)
    e3d.fov = fov


def _texto(draw, xy, txt, font, anchor, sombra=True):
    if sombra:
        draw.text((xy[0] + 2, xy[1] + 2), txt, fill=(0, 0, 0), font=font, anchor=anchor)
    draw.text(xy, txt, fill=COL_TEXT, font=font, anchor=anchor)


class ComposicionPreview:
    """Compone cada frame de la preview a partir del strip completo (PIL RGB del ancho del perfil)."""

    def __init__(self, datos, w_total, h_total, size=(REF_W, REF_H), titulo=None):
        self.datos = datos
        self.w_total, self.h_total = int(w_total), int(h_total)
        self.W_out, self.H_out = size
        sx, sy = self.W_out / float(REF_W), self.H_out / float(REF_H)
        r = lambda v, s: max(1, int(round(v * s)))

        # ----- rejilla
        gap = r(GAP, min(sx, sy))
        self.r_main = (0, 0, r(MAIN_W, sx), r(TOP_H, sy))
        x_side = self.r_main[2] + gap
        self.r_corner = (x_side, 0, self.W_out, r(SIDE_SPLIT, sy))
        self.r_goal = (x_side, self.r_corner[3] + gap, self.W_out, self.r_main[3])
        y_det = self.r_main[3] + gap
        self.r_detail = (0, y_det, self.W_out, y_det + r(DETAIL_H, sy))
        self.r_arch = (0, self.r_detail[3] + gap, self.W_out, self.H_out)

        # ----- geometría del estadio (la misma que la 3D Preview)
        zonas = datos.get("zonas") or {}
        gl = int((zonas.get("ZONA_1") or {}).get("width", 0) or 0)
        amplify = bool(datos.get("config", {}).get("amplify", False))
        self.gl = max(0, min(gl, self.w_total // 2))
        self.L, self.W, self.bandas = geometria_estadio(self.w_total, self.gl, self.h_total, amplify)
        self.top = max([b["y1"] for b in self.bandas] or [1.0])
        m = min(self.L, self.W) * 0.06                     # margen bandas-campo de vista_3d.render
        self.u = (self.L - 2 * m) / METROS_CAMPO            # 1 metro en unidades del strip
        self.sz = (self.W - 2 * m) / 68.0
        self.plate = None
        self.fondo = None
        if FONDO_PLATES and _fp is not None:
            try:
                self._ajustar_a_plate()
            except Exception as ex:   # el fondo nunca debe impedir la preview
                print("[preview] plates desactivados:", ex)
                self.plate = None

        # v0.77: la textura se reduce por un factor entero (Image.reduce, media por cajas: ~8x más rápido que
        # BILINEAR) hasta quedar por debajo del mismo tope de ancho que la 3D Preview
        self.tex_factor = max(1, int(math.ceil(self.w_total / float(TEX_MAX_PREVIEW))))
        self.tex_size = (max(1, self.w_total // self.tex_factor), max(1, self.h_total // self.tex_factor))

        self.e_main = self._escena(self.r_main)
        self.e_corner = self._escena(self.r_corner)
        self.e_goal = self._escena(self.r_goal)
        self._camaras_fijas()
        self._preparar_pan()

        # ----- tipografías
        self.f_panel = _fuente(r(26, sy))
        self.f_arch = _fuente(r(22, sy))
        self.f_titulo = _fuente(r(20, sy))
        self.titulo = titulo or ""   # v0.77: sin título por defecto (ni campaña ni estadio)

        self.lienzo = Image.new("RGB", (self.W_out, self.H_out), COL_GAP)   # los huecos negros no se repintan
        self._pool = None
        self._preparar_detail()
        self._preparar_arquitectura()
        self._preparar_rotulos()
        if self.plate is not None:
            try:
                self._preparar_fondo()
            except Exception as ex:
                print("[preview] plates desactivados:", ex)
                self.fondo = None

    # ------------------------------------------------------------ 3D
    def _escena(self, rect):
        """Escena3D para una ventana. v0.78: se pinta a SUPERSAMPLING x el tamaño y se reduce (media por cajas):
        antialiasing de las diagonales del perímetro y de las líneas del campo, sin parpadeo al mover la cámara."""
        e = _fp.EscenaLED() if _fp is not None else Escena3D()
        e.size = ((rect[2] - rect[0]) * SUPERSAMPLING, (rect[3] - rect[1]) * SUPERSAMPLING)
        e.set_scene(self.L, self.W, self.bandas, reset=True)
        return e

    def _cam1(self):
        """Posición de la CÁMARA 1 (la de la tele): tribuna principal, a la altura del centro del campo.
        Todas las vistas 3D (paneo, CORNER, GOAL) salen de este mismo punto: solo cambian rotación y zoom."""
        hw, u = self.W / 2, self.u
        return (0.0, CAM1_ALTURA_M * u, hw + CAM1_DISTANCIA_M * u)

    def _encuadre(self, e3d, puntos, relleno=0.92):
        """(objetivo, fov) para que, desde la cámara 1, `puntos` llenen la ventana de `e3d`."""
        eye = self._cam1()
        xs, ys, zs = zip(*puntos)
        P = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, (min(zs) + max(zs)) / 2)
        fov0 = 40.0
        _apuntar(e3d, eye, P, fov0)
        w, h = e3d.size
        cam = e3d._camera()
        cs = [e3d._to_cam(cam, q) for q in puntos]
        if min(c[2] for c in cs) <= e3d._near():
            return P, fov0
        pr = [e3d._proj(cam, c) for c in cs]
        ext_x = max(abs(q[0] - w / 2) for q in pr) * 2
        ext_y = max(abs(q[1] - h / 2) for q in pr) * 2
        k = min(relleno * w / max(ext_x, 1.0), relleno * h / max(ext_y, 1.0))
        t = math.tan(math.radians(fov0) / 2) / k
        return P, max(FOV_MIN_PREVIEW, min(FOV_MAX_PREVIEW, math.degrees(2 * math.atan(t))))

    def _camaras_fijas(self):
        hl, hw, u, top = self.L / 2, self.W / 2, self.u, self.top
        ys = (0.0, top)
        # CORNER: la esquina derecha lejana (touchline + goal line right), ~12 m a cada lado
        r = 12 * u
        pts = [(x, y, z) for (x, z) in ((hl - r, -hw), (hl, -hw), (hl, -hw + r)) for y in ys]
        P, fov = self._encuadre(self.e_corner, pts)
        _apuntar(self.e_corner, self._cam1(), P, fov)
        # GOAL: la portería derecha (centro del goal line right), ~el ancho del área grande
        r = 14 * u
        pts = [(hl, y, z) for z in (-r, r) for y in ys]
        P, fov = self._encuadre(self.e_goal, pts)
        _apuntar(self.e_goal, self._cam1(), P, fov)

    def _preparar_pan(self):
        """v0.78: paneo desde la cámara 1 que sigue el recorrido del perímetro: del centro del touchline (CB) a la
        esquina y por el goal line right hasta su mitad, y vuelta. El objetivo de la cámara va siempre sobre las
        pantallas, así los LED no salen nunca de cuadro. El zoom va de PAN_ZOOM_INI (centro) a PAN_ZOOM (GLR) y vuelve."""
        hl, hw, u, top = self.L / 2, self.W / 2, self.u, self.top
        r = 9.15 * u   # círculo central: el encuadre inicial lo incluye junto con el CB
        pts_a = [(x, 0.0, z) for x in (-r, r) for z in (-r, r)] + \
                [(x, y, -hw) for x in (-0.16 * self.L, 0.16 * self.L) for y in (0.0, top)]
        self.fov_a = self._encuadre(self.e_main, pts_a, relleno=0.95)[1]
        self.tramo_tl = hl                                    # del centro del TL a la esquina derecha
        self.tramo_glr = self.gl * PAN_FIN_GLR if self.gl > 0 else 0.0  # de la esquina a PAN_FIN_GLR del GLR
        self._preparar_trayectoria()

    def _punto_perimetro(self, s):
        """Punto (mundo, a media altura de las pantallas) a una fracción s del recorrido centro TL -> mitad GLR."""
        hl, hw = self.L / 2, self.W / 2
        d = s * (self.tramo_tl + self.tramo_glr)
        y = self.top / 2
        if d <= self.tramo_tl:
            return (d, y, -hw)
        return (hl, y, -hw + (d - self.tramo_tl))

    def _preparar_trayectoria(self):
        """v0.79: la trayectoria del paneo se define por el GIRO de la cámara (yaw) y no por la distancia recorrida
        sobre el perímetro: así la velocidad angular es la que marca el ease. La inclinación (pitch) que sigue a
        las pantallas tiene un pico en el corner (cambio de 90º del TL al GLR); se suaviza con una gaussiana sobre el
        giro para que la cámara redondee la esquina sin perder los LED de cuadro."""
        eye = self._cam1()
        ys, ps = [], []
        for i in range(PAN_MUESTRAS + 1):
            P = self._punto_perimetro(i / float(PAN_MUESTRAS))
            v = (eye[0] - P[0], eye[1] - P[1], eye[2] - P[2])
            d = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2) or 1.0
            ys.append(math.atan2(v[0] / d, v[2] / d))
            ps.append(math.asin(max(-1.0, min(1.0, v[1] / d))))
        ys, ps = np.array(ys), np.array(ps)
        self.yaw0, self.yaw1 = float(ys[0]), float(ys[-1])   # mirando al centro del TL / a la mitad del GLR
        orden = np.argsort(ys)
        ys, ps = ys[orden], ps[orden]
        rejilla = np.linspace(float(ys[0]), float(ys[-1]), PAN_MUESTRAS + 1)
        pitch = np.interp(rejilla, ys, ps)
        sigma = max(1.0, PAN_SUAVIZADO_ESQUINA * PAN_MUESTRAS)
        k = np.arange(-int(3 * sigma), int(3 * sigma) + 1)
        g = np.exp(-0.5 * (k / sigma) ** 2)
        g /= g.sum()
        pad = len(k) // 2
        ext = np.concatenate([np.full(pad, pitch[0]), pitch, np.full(pad, pitch[-1])])
        self.pan_rejilla = rejilla
        self.pan_pitch = np.convolve(ext, g, mode="valid")

    def _camara_pan(self, t):
        """t en [0, 1) a lo largo del contenido: centro -> GLR -> centro.
        v0.79: movimiento senoidal (ease in / ease out en los extremos, sin pausas: solo se para un instante para
        dar la vuelta). La cámara no se mueve: solo cambian hacia dónde mira y el zoom."""
        u = 0.5 - 0.5 * math.cos(2.0 * math.pi * t)                 # 0 -> 1 -> 0, velocidad suave
        yaw = self.yaw0 + (self.yaw1 - self.yaw0) * u
        pitch = float(np.interp(yaw, self.pan_rejilla, self.pan_pitch))
        zoom = PAN_ZOOM_INI + (PAN_ZOOM - PAN_ZOOM_INI) * u
        tv = math.tan(math.radians(self.fov_a) / 2) / zoom
        e = self.e_main
        eye = self._cam1()
        o = (math.cos(pitch) * math.sin(yaw), math.sin(pitch), math.cos(pitch) * math.cos(yaw))   # objetivo -> ojo
        dist = self.L / 2
        e.yaw, e.pitch, e.dist = yaw, pitch, dist
        e.target = (eye[0] - o[0] * dist, eye[1] - o[1] * dist, eye[2] - o[2] * dist)
        e.fov = math.degrees(2 * math.atan(tv))
        # las pantallas quedan a PAN_BANDA_Y del alto (desde arriba): se ve el campo debajo
        e.rotar_camara(0.0, math.atan((0.5 - PAN_BANDA_Y) * 2 * tv))

    # ------------------------------------------------------------ DETAIL
    def _centro_detail(self):
        """x (px del strip) del centro de la franja DETAIL: la primera ANIM ZONE a la derecha del CB.
        Sin CB se toma el centro del touchline; sin ANIM ZONES, lo que viene justo después del CB."""
        elems = [e for z in (self.datos.get("zonas") or {}).values() for e in z.get("elementos", [])]
        centro_tl = self.w_total / 2.0
        cbs = [e for e in elems if e.get("tipo") == "center_board"]
        if cbs:
            cb = min(cbs, key=lambda e: abs(e["x_absoluta"] + e["width"] / 2.0 - centro_tl))
            ref = cb["x_absoluta"] + cb["width"]
        else:
            cb, ref = None, centro_tl
        anims = sorted((e for e in elems if e.get("tipo") == "anim_zone" and e["x_absoluta"] >= ref - 1),
                       key=lambda e: e["x_absoluta"])
        if anims:
            a = anims[0]
            # v0.91: límite izquierdo de DETAIL: el final del CB (o el del hueco negro antes de la ANIM ZONE)
            limite = ref if cb is not None else None
            prev = [e["x_absoluta"] + e["width"] for e in elems if e["x_absoluta"] + e["width"] <= a["x_absoluta"] + 1]
            if prev and a["x_absoluta"] - max(prev) > DETAIL_HUECO_NEGRO * self.h_total:
                limite = max(limite or 0.0, float(a["x_absoluta"]))
            elif not prev and cb is None:
                limite = float(a["x_absoluta"])
            return a["x_absoluta"] + a["width"] / 2.0, float(a["width"]), limite
        if cb is not None:
            return ref + cb["width"] / 2.0, float(cb["width"]), None
        return centro_tl, self.w_total / 8.0, None

    def _preparar_detail(self):
        x0, y0, x1, y1 = self.r_detail
        bw, bh = x1 - x0, y1 - y0
        cx, ancho, limite = self._centro_detail()
        # la ANIM ZONE ocupa ~la mitad de la franja, con el LED entre el 30% y el 50% del alto
        s = 0.5 * bw / max(1.0, ancho)
        led_h = max(0.30 * bh, min(0.50 * bh, self.h_total * s))
        s = led_h / float(self.h_total)
        # v0.91: la ANIM ZONE sigue centrada, pero se amplía lo que haga falta para que el CB (en negro) quede
        # fuera de la franja por la izquierda
        if limite is not None and cx - limite > 1.0:
            s_min = bw / (2.0 * (cx - limite) * (1.0 - DETAIL_MARGEN))
            s = min(max(s, s_min), DETAIL_LED_MAX * bh / float(self.h_total))
        self.det_scale = s
        self.det_led_h = max(2, int(round(led_h)))
        cw = bw / s
        self.det_src = (cx - cw / 2.0, cx + cw / 2.0)
        self.det_y = y0 + int(round(bh * 0.55 - self.det_led_h / 2.0))
        # fondo: degradado como la 3D Preview y un suelo algo más oscuro bajo el LED
        fondo = Escena3D()
        fondo.size = (bw, bh)
        bg = fondo._fondo()
        d = ImageDraw.Draw(bg, "RGBA")
        d.rectangle((0, self.det_y - y0 + self.det_led_h, bw, bh), fill=(0, 0, 0, 45))
        self.det_bg = bg

    def _pintar_detail(self, strip):
        x0, y0, x1, y1 = self.r_detail
        bw = x1 - x0
        a, b = self.det_src
        ia, ib = max(0, int(math.floor(a))), min(self.w_total, int(math.ceil(b)))
        self.lienzo.paste(self.det_bg, (x0, y0))
        if ib > ia:
            crop = strip.crop((ia, 0, ib, self.h_total))
            dx0 = int(round((ia - a) * self.det_scale))
            dw = max(1, int(round((ib - ia) * self.det_scale)))
            dw = min(dw, bw - dx0)
            self.lienzo.paste(crop.resize((dw, self.det_led_h), Image.Resampling.BILINEAR), (x0 + dx0, self.det_y))

    # ------------------------------------------------------------ ARCHITECTURE
    def _preparar_arquitectura(self):
        """Filas como en la referencia: GOALLINE LEFT (alineada a la izquierda), TOUCHLINE (todo el ancho) y
        GOALLINE RIGHT (alineada a la derecha), todas a la misma escala, con sus rótulos y las porterías."""
        x0, y0, x1, y1 = self.r_arch
        gl, L, W = self.gl, int(self.L), self.W
        util = (x1 - x0) - 2 * ARCH_MARGIN
        s = util / float(max(L, gl, 1))
        self.arch_scale = s
        row_h = max(2, int(round(self.h_total * s)))
        tl_v = (min(b["src"][2] for b in self.bandas if b["side"] == "TL"),
                max(b["src"][3] for b in self.bandas if b["side"] == "TL"))
        gl_v = next((b["src"][2:] for b in self.bandas if b["side"] == "GLL"), (0, self.h_total))
        goal_w = 7.32 * self.sz
        filas = []   # (src x0, x1, v0, v1, dst x, w, h, etiquetas)
        lx, rx = x0 + ARCH_MARGIN, x1 - ARCH_MARGIN
        if gl > 0:
            w_gl = max(1, int(round(gl * s)))
            g = gl - W / 2.0   # portería izquierda: offset dentro del GLL (el strip empieza en el extremo abierto)
            filas.append(dict(src=(0, gl) + tuple(gl_v), x=lx, w=w_gl,
                              izq="GOALLINE LEFT", der="CORNER", tick_der=True,
                              goal=("GOAL LEFT", g) if 0 <= g <= gl else None))
        tl_h = max(2, int(round((tl_v[1] - tl_v[0]) * s)))
        filas.append(dict(src=(gl, gl + L) + tl_v, x=lx, w=max(1, int(round(L * s))), h=tl_h, centro="TOUCHLINE"))
        if gl > 0:
            w_gl = max(1, int(round(gl * s)))
            g = W / 2.0        # portería derecha: el GLR empieza en la esquina
            filas.append(dict(src=(self.w_total - gl, self.w_total) + tuple(gl_v), x=rx - w_gl, w=w_gl,
                              izq="CORNER", der="GOALLINE RIGHT", tick_izq=True,
                              goal=("GOAL RIGHT", g) if 0 <= g <= gl else None))
        for f in filas:
            f.setdefault("h", row_h)

        # reparto vertical: rótulo + aire + fila, centrado en el área
        asc = self.f_arch.getbbox("GOALLINE")[3]
        aire = max(14, int(round(asc * 1.4)))
        bloque = [asc + aire + f["h"] for f in filas]
        sep = max(10, min(40, ((y1 - y0) - sum(bloque)) // (len(filas) + 1)))
        y = y0 + max(0, ((y1 - y0) - sum(bloque) - sep * (len(filas) - 1)) // 2)

        fondo = Image.new("RGB", (x1 - x0, y1 - y0), COL_ARCH_BG)
        d = ImageDraw.Draw(fondo)
        oy = y0
        for f, alto in zip(filas, bloque):
            yl = y - oy + asc                      # línea base del rótulo
            yr = y - oy + asc + aire              # parte de arriba de la fila
            fx = f["x"] - x0
            if f.get("izq"):
                d.text((fx, yl), f["izq"], fill=COL_TEXT, font=self.f_arch, anchor="ls")
            if f.get("der"):
                d.text((fx + f["w"], yl), f["der"], fill=COL_TEXT, font=self.f_arch, anchor="rs")
            if f.get("centro"):
                d.text((fx + f["w"] / 2, yl), f["centro"], fill=COL_TEXT, font=self.f_arch, anchor="ms")
            if f.get("tick_der"):
                self._punteado(d, fx + f["w"] - 1, yl + 6, yr - 3)
            if f.get("tick_izq"):
                self._punteado(d, fx, yl + 6, yr - 3)
            if f.get("goal"):
                nombre, g = f["goal"]
                gx = fx + g * s
                half = max(8, goal_w * s / 2)
                tw = self.f_arch.getlength(nombre)
                libre_izq = fx + (self.f_arch.getlength(f.get("izq") or "") + 12 if f.get("izq") else 0)
                libre_der = fx + f["w"] - (self.f_arch.getlength(f.get("der") or "") + 12 if f.get("der") else 0)
                if gx - tw / 2 > libre_izq and gx + tw / 2 < libre_der:
                    d.text((gx, yl), nombre, fill=COL_TEXT, font=self.f_arch, anchor="ms")
                yb = yl + max(6, aire // 2)
                d.line((gx - half, yb, gx + half, yb), fill=COL_GUIDE, width=2)
                d.line((gx - half, yb, gx - half, yr - 3), fill=COL_GUIDE, width=2)
                d.line((gx + half, yb, gx + half, yr - 3), fill=COL_GUIDE, width=2)
                # v0.78: la discontinua va en tercios: al final del 1er tercio en GOAL LEFT y del 2º en GOAL RIGHT
                tercio = 1.0 / 3.0 if nombre == "GOAL LEFT" else 2.0 / 3.0
                self._punteado(d, gx - half + 2 * half * tercio, yb, yr - 3)
            f["y"] = y + asc + aire
            y += alto + sep
        self.arch_bg = fondo
        self.arch_filas = filas

    @staticmethod
    def _punteado(d, x, ya, yb, paso=4):
        x = int(round(x))
        for yy in range(int(ya), int(yb), paso):
            d.line((x, yy, x, min(yb, yy + 1)), fill=COL_GUIDE, width=1)

    def _pintar_arquitectura(self, strip):
        """strip: el strip ya reducido a textura (cualquier escala): las filas son mucho más pequeñas."""
        x0, y0 = self.r_arch[:2]
        kx, ky = strip.width / float(self.w_total), strip.height / float(self.h_total)
        self.lienzo.paste(self.arch_bg, (x0, y0))
        for f in self.arch_filas:
            a, b, v0, v1 = f["src"]
            if b <= a or v1 <= v0:
                continue
            box = (int(a * kx), int(v0 * ky), max(int(a * kx) + 1, int(b * kx)), max(int(v0 * ky) + 1, int(v1 * ky)))
            fila = strip.crop(box).resize((f["w"], f["h"]), Image.Resampling.BILINEAR, reducing_gap=REDUCING_GAP)
            self.lienzo.paste(fila, (int(f["x"]), int(f["y"])))

    # ------------------------------------------------------------ frame
    # ------------------------------------------------------------ rótulos (se pintan una vez)
    def _preparar_rotulos(self):
        """Rótulos CORNER / GOAL / DETAIL (y el título, si lo hay) como parches RGBA: no se rasteriza texto
        en cada frame."""
        self.rotulos = []
        items = [((self.r_corner[2] - 20, self.r_corner[3] - 12), "CORNER", self.f_panel, "rs"),
                 ((self.r_goal[2] - 20, self.r_goal[3] - 12), "GOAL", self.f_panel, "rs"),
                 ((self.r_detail[2] - 20, self.r_detail[3] - 12), "DETAIL", self.f_panel, "rs")]
        if self.titulo:
            items.append(((20, 18), self.titulo, self.f_titulo, "la"))
        for xy, txt, font, anchor in items:
            l, t, r, b = ImageDraw.Draw(Image.new("L", (1, 1))).textbbox(xy, txt, font=font, anchor=anchor)
            x0, y0 = int(l) - 2, int(t) - 2
            parche = Image.new("RGBA", (int(r) - x0 + 6, int(b) - y0 + 6), (0, 0, 0, 0))
            _texto(ImageDraw.Draw(parche), (xy[0] - x0, xy[1] - y0), txt, font, anchor)
            self.rotulos.append((parche, (x0, y0)))

    @staticmethod
    def _render_vista(e3d):
        img = e3d.render(smooth=True)
        return img.reduce(SUPERSAMPLING) if SUPERSAMPLING > 1 else img

    def _render_con_fondo(self, e3d, fijo=None):
        """v0.82: pantallas sobre el plate. `fijo` = (fondo, delante) ya calculados (CORNER y GOAL)."""
        if fijo is None:
            fijo = self.fondo.vista(_fp.Cam.de_escena(e3d, 1.0 / SUPERSAMPLING))
        return e3d.render_sobre(*fijo)

    # ------------------------------------------------------------ v0.82: fondo con plates
    def _ajustar_a_plate(self):
        """Elige el plate con la proporción TL/GL más parecida y reparte la diferencia: el perímetro 3D se estira
        hasta la proporción intermedia (el plate se deforma hasta esa misma proporción en _preparar_fondo)."""
        tl_gl = self.L / self.W
        # v0.88: perfil de estadio (preview directa: "estadio_preview"; campañas y worker: archie.stadium)
        estadio = self.datos.get("estadio_preview") or (self.datos.get("archie") or {}).get("stadium")
        meta = _fp.elegir_plate(tl_gl, estadio)
        if meta is None:
            return
        objetivo = _fp.proporcion_compromiso(tl_gl, meta["tl_gl"])
        W_c = self.L / objetivo
        k = W_c / self.W
        if abs(k - 1.0) > 1e-6:
            bandas = []
            for b in self.bandas:
                if b["side"] in ("GLL", "GLR"):
                    b = dict(b, along=b["along"] * k, length=b["length"] * k)
                bandas.append(b)
            self.bandas = bandas
            self.W = W_c
        self.plate = meta
        print("[preview] plate %s (TL/GL %.3f) para un estadio %.3f: perímetro a %.3f"
              % (os.path.basename(meta["_png"]), meta["tl_gl"], tl_gl, objetivo))

    def _preparar_fondo(self):
        u = self.u
        led = min(b["y1"] - b["y0"] for b in self.bandas) / u      # alto de la 1st row (con AMPLIFY hay dos)
        eye = np.array(self._cam1()) / u
        self.fondo = _fp.FondoPlate(self.plate, self.L / u, self.W / u, led, eye)
        ss = 1.0 / SUPERSAMPLING
        self.fijo_corner = self.fondo.vista(_fp.Cam.de_escena(self.e_corner, ss))
        self.fijo_goal = self.fondo.vista(_fp.Cam.de_escena(self.e_goal, ss))
        cam = self._camara_detail()
        if cam is not None:
            self.det_bg = self.fondo.detail(cam, y_corte=self.top / u, estirar=DETAIL_ESTIRAR_GRADA)

    def _camara_detail(self):
        """Cámara ortográfica de DETAIL (en metros): de frente al touchline lejano, un poco desde arriba."""
        x0, y0, x1, y1 = self.r_detail
        bw, bh = x1 - x0, y1 - y0
        cx = (self.det_src[0] + self.det_src[1]) / 2.0
        if not (self.gl <= cx <= self.gl + self.L):
            return None
        u = self.u
        xw = (-self.L / 2 + (cx - self.gl)) / u
        s = self.det_scale * u                                  # px por metro
        fila_led = self.det_y - y0 + self.det_led_h / 2.0
        a = math.radians(4.0)
        f = np.array([0.0, -math.sin(a), -math.cos(a)])
        r = np.array([1.0, 0.0, 0.0])
        up = np.cross(r, f)
        led_c = min(b["y1"] for b in self.bandas) / u / 2.0     # centro de la 1st row
        Q = np.array([xw, led_c + (fila_led - bh / 2.0) / s, -self.W / 2 / u])
        return _fp.Cam(Q - f * 60.0, r, up, f, 1.0, (bw, bh), orto=s)   # 60 m: la "distancia" de detail()

    def close(self):
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    # ------------------------------------------------------------ frame
    def frame(self, strip, t):
        """strip: PIL RGB del strip completo; t: posición en el vídeo, de 0 a 1."""
        tex = strip.reduce(self.tex_factor) if self.tex_factor > 1 else strip
        self.e_main.set_strip(tex, self.w_total, self.h_total)
        self.e_corner.tex = self.e_goal.tex = self.e_main.tex     # mismas bandas: se comparten las texturas
        self._camara_pan(t)

        # v0.77: las tres vistas 3D se pintan en paralelo (PIL suelta el GIL en transform / polygon / paste)
        # mientras este hilo compone DETAIL y la arquitectura (zonas del lienzo que no se solapan)
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="preview3d")
        if self.fondo is not None:
            vistas = [(self._pool.submit(self._render_con_fondo, e, fijo), r)
                      for e, fijo, r in ((self.e_main, None, self.r_main), (self.e_corner, self.fijo_corner, self.r_corner),
                                         (self.e_goal, self.fijo_goal, self.r_goal))]
        else:
            vistas = [(self._pool.submit(self._render_vista, e), r)
                      for e, r in ((self.e_main, self.r_main), (self.e_corner, self.r_corner), (self.e_goal, self.r_goal))]
        lz = self.lienzo
        self._pintar_detail(strip)
        self._pintar_arquitectura(tex)
        for fut, r in vistas:
            lz.paste(fut.result(), r[:2])
        for parche, xy in self.rotulos:
            lz.paste(parche, xy, parche)
        return lz
