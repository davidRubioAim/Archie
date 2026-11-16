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

from PIL import Image, ImageDraw, ImageFont

from vista_3d import Escena3D, geometria_estadio

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

PAN_ZOOM = 2.0          # zoom del paneo en el extremo del GLR (x1 en el centro)
TEX_MAX_PREVIEW = 16384  # ancho máximo de la textura de las vistas 3D
REDUCING_GAP = 2.0      # resize rápido (reduce + bilinear) para la textura y la arquitectura
PAN_HOLD = 0.08          # fracción del ciclo quieta en cada extremo del paneo (centro y lado derecho)
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

    # ------------------------------------------------------------ 3D
    def _escena(self, rect):
        e = Escena3D()
        e.size = (rect[2] - rect[0], rect[3] - rect[1])
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
        """Paneo desde la cámara 1: del centro del campo (con el CB detrás) al goal line right, y vuelta.
        El zoom va de x1 (centro) a PAN_ZOOM (GLR) y vuelve a x1."""
        hl, hw, u, top = self.L / 2, self.W / 2, self.u, self.top
        r = 9.15 * u   # círculo central
        pts_a = [(x, 0.0, z) for x in (-r, r) for z in (-r, r)] + \
                [(x, y, -hw) for x in (-0.16 * self.L, 0.16 * self.L) for y in (0.0, top)]
        z0 = -hw
        z1 = -hw + (self.gl if self.gl > 0 else self.W)
        pts_b = [(hl, y, z) for z in (z0, z1) for y in (0.0, top)] + [(hl - 18 * u, 0.0, 0.0)]
        self.pan_a = self._encuadre(self.e_main, pts_a, relleno=0.95)
        P_b, _ = self._encuadre(self.e_main, pts_b, relleno=0.95)
        # zoom x1 en el centro -> x2 mirando al GLR (mitad de tan(FOV/2))
        fov_b = math.degrees(2 * math.atan(math.tan(math.radians(self.pan_a[1]) / 2) / PAN_ZOOM))
        self.pan_b = (P_b, fov_b)

    def _camara_pan(self, t):
        """t en [0, 1) a lo largo del vídeo: centro -> GLR -> centro, con pausas en los extremos.
        La cámara no se mueve: se interpola hacia dónde mira y el zoom."""
        tri = 1.0 - abs(2.0 * t - 1.0)                              # 0 -> 1 -> 0
        s = _suave((tri - PAN_HOLD) / max(1e-6, 1.0 - 2 * PAN_HOLD))
        (pa, fa), (pb, fb) = self.pan_a, self.pan_b
        ta, tb = math.tan(math.radians(fa) / 2), math.tan(math.radians(fb) / 2)
        fov = math.degrees(2 * math.atan(ta + (tb - ta) * s))
        _apuntar(self.e_main, self._cam1(), _lerp(pa, pb, s), fov)

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
            return a["x_absoluta"] + a["width"] / 2.0, float(a["width"])
        if cb is not None:
            return ref + cb["width"] / 2.0, float(cb["width"])
        return centro_tl, self.w_total / 8.0

    def _preparar_detail(self):
        x0, y0, x1, y1 = self.r_detail
        bw, bh = x1 - x0, y1 - y0
        cx, ancho = self._centro_detail()
        # la ANIM ZONE ocupa ~la mitad de la franja, con el LED entre el 30% y el 50% del alto
        s = 0.5 * bw / max(1.0, ancho)
        led_h = max(0.30 * bh, min(0.50 * bh, self.h_total * s))
        s = led_h / float(self.h_total)
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
                self._punteado(d, gx, yb, yr - 3)
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
        vistas = [(self._pool.submit(e.render, True), r)
                  for e, r in ((self.e_main, self.r_main), (self.e_corner, self.r_corner), (self.e_goal, self.r_goal))]
        lz = self.lienzo
        self._pintar_detail(strip)
        self._pintar_arquitectura(tex)
        for fut, r in vistas:
            lz.paste(fut.result(), r[:2])
        for parche, xy in self.rotulos:
            lz.paste(parche, xy, parche)
        return lz
