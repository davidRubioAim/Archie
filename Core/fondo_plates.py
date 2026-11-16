"""
v0.82 (v0.85): fondo de estadio para las previews a partir de PLATES (Assets/plates).

Cada plate es una panorámica de un estadio tipo vista desde la CÁMARA 1, con su .json (cámara y medidas):
    plate_<nombre>.png            la imagen (esquemática o pasada por IA)
    plate_<nombre>.json           cámara (ojo, ejes r/u/f, focal) y medidas: touchline_m, goalline_m, led_alto_m
    plate_<nombre>_delante.png    (opcional) mate de lo que va DELANTE de las pantallas: porterías y banderines
                                  (vale una silueta rellena: la opacidad de la red sale del brillo del plate)
    plate_<nombre>_led.png        (opcional, solo de control) mate del perímetro LED

Archie elige el plate con la proporción TL/GL más parecida a la del estadio y reparte la diferencia:
  - el perímetro 3D de la preview se estira hasta una proporción intermedia (REPARTO_FONDO);
  - el plate se deforma hasta esa misma proporción.
La deformación del plate usa un "prisma" con la forma del perímetro (suelo + pantallas, con paredes que suben
hasta el cielo): cada píxel se lleva al punto del prisma que ve, se escala a las medidas del plate y se busca en
la imagen original. Así el césped, las líneas, la banda LED y las gradas encajan con el perímetro estirado.
El plate deformado se calcula una vez por estadio y se guarda en caché; en cada frame solo hay una homografía
(la cámara 1 no se mueve, solo gira y hace zoom).
"""
import glob
import hashlib
import json
import math
import os
import tempfile

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from vista_3d import Escena3D, _v_cross, _v_dot, _v_sub

PLATE_UNICO = "plate_A_ancho"   # v0.83: todos los estadios usan este plate (None = el de proporción más parecida)
REPARTO_FONDO = 0.3        # parte de la diferencia de proporción que absorbe el plate (squeeze de la imagen);
                           # el resto lo absorbe el perímetro, que se adapta al plate
REFLEJO_LED = 0.9          # v0.84: intensidad del reflejo de las pantallas en el césped (0 = sin reflejo)
REFLEJO_ALTO = 0.45        # largo del reflejo respecto al alto de la pantalla (corto: pegado al LED)
REFLEJO_BLUR_H = 0.012     # desenfoque horizontal del reflejo (fracción del ancho de la vista)
REFLEJO_BLUR_V = 0.022     # desenfoque vertical (fracción del alto): la hierba lo "arrastra" hacia abajo
BRILLO_LED = 0.18          # halo de las pantallas (0 = sin halo)
# v0.85: dentro del mate "delante" la opacidad sale del brillo del plate: los hilos de la red y los postes (claros)
# tapan las pantallas y los huecos (la banda LED oscura que se ve a través de la red) dejan ver el LED.
# Así el mate puede ser una silueta rellena de la portería, sin tener que dibujar cada hilo.
DELANTE_LUM_MIN = 50       # por debajo (banda LED apagada del plate): transparente
DELANTE_LUM_MAX = 150      # por encima (hilos, postes): opaco
DELANTE_GAMMA = 0.7        # <1: hilos finos algo más presentes
VERSION_CACHE = 2

CARPETA_PLATES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Assets", "plates")


def _carpeta_cache():
    raiz = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    base = os.environ.get("ARCHIE_PLATES_CACHE") or os.path.join(raiz, "Archie", "plates_cache")
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    return base


# ---------------------------------------------------------------- cámaras
class Cam:
    """Cámara (convención de Escena3D): ojo, ejes r/u/f, focal (px) y tamaño. orto = px por unidad si es ortográfica."""

    def __init__(self, eye, r, u, f, focal, size, orto=None):
        self.eye = np.array(eye, float)
        self.r, self.u, self.f = (np.array(v, float) for v in (r, u, f))
        self.focal = float(focal)
        self.size = (int(size[0]), int(size[1]))
        self.orto = orto

    @classmethod
    def de_escena(cls, e3d, escala=1.0):
        eye, r, u, f, focal = e3d._camera()
        w, h = e3d.size
        return cls(eye, r, u, f, focal * escala, (round(w * escala), round(h * escala)))

    def M(self):
        w, h = self.size
        fo = self.focal
        return np.column_stack([self.r / fo, -self.u / fo, self.f - self.r * w / 2 / fo + self.u * h / 2 / fo])

    def P(self):
        w, h = self.size
        return np.vstack([self.focal * self.r + w / 2 * self.f, -self.focal * self.u + h / 2 * self.f, self.f])


def homografia(placa, vista):
    """Coeficientes de Image.PERSPECTIVE: píxel de `vista` -> píxel de `placa` (mismo ojo)."""
    H = placa.P() @ vista.M()
    H = H / H[2, 2]
    return tuple(H.flatten()[:8])


# ---------------------------------------------------------------- plates disponibles
def plates_disponibles():
    out = []
    for js in sorted(glob.glob(os.path.join(CARPETA_PLATES, "plate_*.json"))):
        png = js[:-5] + ".png"
        if not os.path.isfile(png):
            continue
        try:
            meta = json.load(open(js, encoding="utf-8"))
            meta["tl_gl"] = float(meta["touchline_m"]) / float(meta["goalline_m"])
        except Exception:
            continue
        meta["_png"] = png
        d = js[:-5] + "_delante.png"
        meta["_delante"] = d if os.path.isfile(d) else None
        out.append(meta)
    return out


def elegir_plate(tl_gl):
    """v0.83: PLATE_UNICO si está en Assets/plates; si no, el de proporción TL/GL más parecida (escala log)."""
    ps = plates_disponibles()
    if not ps:
        return None
    if PLATE_UNICO:
        for p in ps:
            if os.path.splitext(os.path.basename(p["_png"]))[0] == PLATE_UNICO:
                return p
    return min(ps, key=lambda p: abs(math.log(tl_gl / p["tl_gl"])))


def proporcion_compromiso(tl_gl_estadio, tl_gl_plate, reparto=REPARTO_FONDO):
    """Proporción intermedia: el plate absorbe `reparto` de la diferencia y el perímetro el resto."""
    return math.exp(math.log(tl_gl_estadio) * reparto + math.log(tl_gl_plate) * (1.0 - reparto))


def _bilinear(img, x, y):
    """img (h, w, c) uint8; x, y float -> (n, c) float32. Fuera de la imagen se repite el borde."""
    h, w = img.shape[:2]
    x = np.clip(x, 0, w - 1.001)
    y = np.clip(y, 0, h - 1.001)
    x0, y0 = x.astype(np.int64), y.astype(np.int64)
    fx, fy = (x - x0).astype(np.float32)[:, None], (y - y0).astype(np.float32)[:, None]
    a, b = img[y0, x0].astype(np.float32), img[y0, x0 + 1].astype(np.float32)
    c, d = img[y0 + 1, x0].astype(np.float32), img[y0 + 1, x0 + 1].astype(np.float32)
    return (a * (1 - fx) + b * fx) * (1 - fy) + (c * (1 - fx) + d * fx) * fy


# ---------------------------------------------------------------- fondo
class FondoPlate:
    """El plate elegido, deformado al estadio de compromiso (todo en metros, origen en el centro del campo)."""

    def __init__(self, meta, tl_m, gl_m, led_m, eye_m):
        self.meta = meta
        cm = meta["camara"]
        self.src_cam = Cam(cm["ojo_m"], cm["r"], cm["u"], cm["f"], cm["focal_px"], (cm["ancho"], cm["alto"]))
        self.tl_m, self.gl_m, self.led_m = float(tl_m), float(gl_m), float(led_m)
        # escala compromiso -> plate en cada eje
        self.esc = np.array([float(meta["touchline_m"]) / self.tl_m, float(meta["led_alto_m"]) / self.led_m,
                             float(meta["goalline_m"]) / self.gl_m])
        self.cam = Cam(eye_m, self.src_cam.r, self.src_cam.u, self.src_cam.f, self.src_cam.focal, self.src_cam.size)
        self._src = None
        self._src_del = None
        self.img, self.delante = self._deformado()
        self.niveles = [self.img]
        while len(self.niveles) < 3:
            self.niveles.append(self.niveles[-1].reduce(2))
        self.niveles_del = None
        self._caja_del = self.delante.getbbox() if self.delante is not None else None
        if self.delante is not None:
            self.niveles_del = [self.delante]
            while len(self.niveles_del) < 3:
                self.niveles_del.append(self.niveles_del[-1].reduce(2))

    # ----- fuente
    def _fuente(self):
        if self._src is None:
            im = Image.open(self.meta["_png"]).convert("RGB")
            if im.size != self.src_cam.size:
                im = im.resize(self.src_cam.size, Image.Resampling.LANCZOS)
            self._src = np.asarray(im)
            if self.meta.get("_delante"):
                d = Image.open(self.meta["_delante"]).convert("L")
                if d.size != self.src_cam.size:
                    d = d.resize(self.src_cam.size, Image.Resampling.BILINEAR)
                m = np.asarray(d).astype(np.float32) / 255.0
                lum = self._src.astype(np.float32).mean(axis=2)
                k = np.clip((lum - DELANTE_LUM_MIN) / float(DELANTE_LUM_MAX - DELANTE_LUM_MIN), 0.0, 1.0)
                a = m * k ** DELANTE_GAMMA
                self._src_del = np.round(a * 255.0).astype(np.uint8)[:, :, None]
        return self._src, self._src_del

    def _coords_fuente(self, O, D):
        """Rayos del estadio de compromiso -> píxel (x, y) del plate original, a través del prisma del perímetro."""
        hl, hw = self.tl_m / 2, self.gl_m / 2
        n = len(D)
        t = np.full(n, 1e6)
        with np.errstate(divide="ignore", invalid="ignore"):
            for cand, ok in (((-O[:, 1]) / D[:, 1], D[:, 1] < 0),            # suelo
                             ((-hw - O[:, 2]) / D[:, 2], D[:, 2] < 0),       # pantallas del touchline lejano
                             ((hl - O[:, 0]) / D[:, 0], D[:, 0] > 0),        # goal line derecho
                             ((-hl - O[:, 0]) / D[:, 0], D[:, 0] < 0)):      # goal line izquierdo
                c = np.where(ok & (cand > 0), cand, np.inf)
                t = np.minimum(t, c)
        t = np.where(np.isfinite(t), t, 1e6)
        P = (O + D * t[:, None]) * self.esc[None, :]
        c = self.src_cam
        d = P - c.eye[None, :]
        cz = np.maximum(d @ c.f, 1e-6)
        w, h = c.size
        return w / 2 + c.focal * (d @ c.r) / cz, h / 2 - c.focal * (d @ c.u) / cz

    def _muestrear(self, O, D, con_delante=True):
        src, sdel = self._fuente()
        x, y = self._coords_fuente(O, D)
        col = _bilinear(src, x, y)
        dl = _bilinear(sdel, x, y) if (con_delante and sdel is not None) else None
        return col, dl

    def _deformado(self):
        """Plate deformado al estadio de compromiso, con la misma cámara (orientación, focal y tamaño)."""
        if np.allclose(self.esc, 1.0, atol=1e-4) and np.allclose(self.cam.eye, self.src_cam.eye, atol=1e-3):
            src, sdel = self._fuente()
            return Image.fromarray(src, "RGB"), (Image.fromarray(sdel[:, :, 0], "L") if sdel is not None else None)
        st = os.stat(self.meta["_png"])
        d = self.meta.get("_delante")
        sd = os.stat(d) if d else None
        clave = "%s|%d|%d|%s|%s|%s|%s|%d" % (os.path.basename(self.meta["_png"]), st.st_size, int(st.st_mtime),
                                             np.round(self.esc, 5), np.round(self.cam.eye, 4), d,
                                             (sd.st_size, int(sd.st_mtime)) if sd else None, VERSION_CACHE)
        base = os.path.join(_carpeta_cache(), hashlib.sha1(clave.encode()).hexdigest()[:20])
        p_img, p_del = base + ".png", base + "_delante.png"
        try:
            if os.path.isfile(p_img):
                img = Image.open(p_img).convert("RGB")
                dl = Image.open(p_del).convert("L") if os.path.isfile(p_del) else None
                if img.size == self.cam.size:
                    return img, dl
        except Exception:
            pass
        w, h = self.cam.size
        out = np.zeros((h, w, 3), np.uint8)
        out_d = np.zeros((h, w), np.uint8) if self.meta.get("_delante") else None
        M = self.cam.M()
        xs = np.arange(w) + 0.5
        for y0 in range(0, h, 128):
            y1 = min(h, y0 + 128)
            yy, xx = np.meshgrid(np.arange(y0, y1) + 0.5, xs, indexing="ij")
            pix = np.stack([xx.ravel(), yy.ravel(), np.ones(xx.size)], axis=1)
            D = pix @ M.T
            D /= np.linalg.norm(D, axis=1)[:, None]
            O = np.broadcast_to(self.cam.eye, D.shape)
            col, dl = self._muestrear(O, D)
            out[y0:y1] = np.clip(col + 0.5, 0, 255).astype(np.uint8).reshape(y1 - y0, w, 3)
            if out_d is not None and dl is not None:
                out_d[y0:y1] = np.clip(dl[:, 0] + 0.5, 0, 255).astype(np.uint8).reshape(y1 - y0, w)
        img = Image.fromarray(out, "RGB")
        dl = Image.fromarray(out_d, "L") if out_d is not None else None
        try:
            img.save(p_img + ".tmp.png", compress_level=1)
            os.replace(p_img + ".tmp.png", p_img)
            if dl is not None:
                dl.save(p_del + ".tmp.png", compress_level=1)
                os.replace(p_del + ".tmp.png", p_del)
        except OSError:
            pass
        return img, dl

    # ----- vistas
    def vista(self, cam_vista):
        """(fondo RGB, delante RGBA o None) de una vista de la cámara 1 (mismo ojo, cualquier giro y zoom)."""
        size = cam_vista.size
        ratio = self.cam.focal / max(cam_vista.focal, 1e-6)          # >1: el plate tiene más resolución
        lv = max(0.0, min(len(self.niveles) - 1.0, math.log2(max(ratio, 1e-6)) - 0.5))
        l0 = int(math.floor(lv))
        fr = lv - l0

        def warp(niveles, nivel, modo_fill):
            k = 2 ** nivel
            cp = Cam(self.cam.eye, self.cam.r, self.cam.u, self.cam.f, self.cam.focal / k,
                     (niveles[nivel].width, niveles[nivel].height))
            # las imágenes reducidas tienen el centro en (w/2, h/2) de su propio tamaño: misma cámara a focal/k
            return niveles[nivel].transform(size, Image.Transform.PERSPECTIVE, homografia(cp, cam_vista),
                                            Image.Resampling.BILINEAR, fillcolor=modo_fill)

        fr = min(1.0, max(0.0, (fr - 0.4) / 0.2))      # un solo nivel salvo en el paso de uno a otro
        if fr >= 1.0 and l0 + 1 < len(self.niveles):
            l0, fr = l0 + 1, 0.0
        bg = warp(self.niveles, l0, (0, 0, 0))
        if fr > 0.0 and l0 + 1 < len(self.niveles):
            bg = Image.blend(bg, warp(self.niveles, l0 + 1, (0, 0, 0)), fr)
        delante = None
        if self.niveles_del is not None and self._delante_en_vista(cam_vista):
            a = warp(self.niveles_del, l0, 0)
            if a.getbbox() is not None:
                delante = bg.copy()
                delante.putalpha(a)
        return bg, delante

    def _delante_en_vista(self, cam_vista):
        """¿Cae alguna parte del mate 'delante' (porterías, banderines) dentro de esta vista?"""
        if self._caja_del is None:
            return False
        x0, y0, x1, y1 = self._caja_del
        Minv = self.cam.M()
        Pv = cam_vista.P()
        w, h = cam_vista.size
        pts = []
        for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)):
            q = Pv @ (Minv @ np.array([x, y, 1.0]))
            if q[2] <= 1e-9:
                return True
            pts.append((q[0] / q[2], q[1] / q[2]))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return not (max(xs) < 0 or min(xs) > w or max(ys) < 0 or min(ys) > h)

    def detail(self, cam_orto_m, y_corte=None, estirar=1.0, distancia=60.0):
        """Fondo de DETAIL: cámara ortográfica (en metros) muestreada directamente del plate original.
        Por encima de `y_corte` (m) la imagen se comprime en vertical (`estirar` > 1): se ve más grada."""
        w, h = cam_orto_m.size
        yy, xx = np.mgrid[0:h, 0:w]
        k = 1.0 / cam_orto_m.orto
        O = (cam_orto_m.eye[None, :] + np.outer((xx.ravel() + 0.5 - w / 2) * k, cam_orto_m.r)
             - np.outer((yy.ravel() + 0.5 - h / 2) * k, cam_orto_m.u))
        if y_corte is not None and estirar != 1.0:
            y = O[:, 1] + cam_orto_m.f[1] * distancia             # altura aproximada sobre las pantallas
            O[:, 1] += np.where(y > y_corte, (y - y_corte) * (estirar - 1.0), 0.0)
        D = np.broadcast_to(cam_orto_m.f, O.shape)
        col, _ = self._muestrear(O, D, con_delante=False)
        img = Image.fromarray(np.clip(col + 0.5, 0, 255).astype(np.uint8).reshape(h, w, 3), "RGB")
        return img.filter(ImageFilter.GaussianBlur(0.6))


# ---------------------------------------------------------------- pantallas sobre el fondo
_LUTS = {}
_NEGROS = {}
_REFLEJO_TEX = {}
_GRADIENTES = {}
_LUT_CESPED = [min(255, int(v * 2.4)) for v in range(256)]   # luminancia del césped (~100) -> ~240


def _blur_aniso(img, rx, ry):
    """Desenfoque con radio distinto en horizontal y vertical (dos pasadas de caja ~ gaussiana)."""
    try:
        f = ImageFilter.BoxBlur((rx, ry))
        return img.filter(f).filter(f)
    except TypeError:          # Pillow antiguo: sin radio por eje, se aproxima reduciendo en vertical
        w, h = img.size
        k = max(1.0, ry / max(rx, 1e-3))
        peq = img.resize((w, max(2, int(h / k))), Image.Resampling.BOX).filter(ImageFilter.BoxBlur(rx)).filter(
            ImageFilter.BoxBlur(rx))
        return peq.resize((w, h), Image.Resampling.BILINEAR)


def _gradiente_reflejo(size):
    """Degradado vertical de la textura reflejada: 0 arriba (lejos del suelo en el espejo) -> 1 abajo (junto al LED)."""
    if size not in _GRADIENTES:
        w, h = size
        t = (np.arange(h, dtype=np.float32) + 0.5) / h
        col = (np.clip(t, 0, 1) ** 1.2 * 255).astype(np.uint8)
        g = Image.fromarray(np.repeat(col[:, None], w, axis=1), "L")
        _GRADIENTES[size] = Image.merge("RGB", (g, g, g))
    return _GRADIENTES[size]


def _lut(f):
    if f not in _LUTS:
        _LUTS[f] = [min(255, int(v * f)) for v in range(256)] * 3
    return _LUTS[f]


def _negro(size):
    if size not in _NEGROS:
        _NEGROS[size] = Image.new("RGB", size, (0, 0, 0))
    return _NEGROS[size]


class EscenaLED(Escena3D):
    """Escena3D que solo pinta las pantallas, sobre un fondo dado, con reflejo en el césped y halo.
    Sin fondo (render normal) se comporta igual que Escena3D."""

    def _bandas(self, img, cam, espejo=False, mascara=None, esc_espejo=1.0):
        near = self._near()
        eye = cam[0]
        chunk_len = max(self.L, self.W) / 32
        items = []
        for b in self.bands:
            o, d = self._side_geom(b["side"])
            n_in = _v_cross(d, (0.0, 1.0, 0.0))
            K = max(1, math.ceil(b["length"] / chunk_len))
            if espejo and b["y0"] > 0:
                continue                      # con AMPLIFY solo se refleja la 1st row (la que toca el suelo)
            y0, y1 = (b["y0"], b["y1"]) if not espejo else (-b["y0"] * esc_espejo, -b["y1"] * esc_espejo)
            for k in range(K):
                s0 = b["along"] + b["length"] * k / K
                s1 = b["along"] + b["length"] * (k + 1) / K
                A = (o[0] + d[0] * s0, 0.0, o[2] + d[2] * s0)
                B = (o[0] + d[0] * s1, 0.0, o[2] + d[2] * s1)
                world = [(A[0], y1, A[2]), (B[0], y1, B[2]), (B[0], y0, B[2]), (A[0], y0, A[2])]
                cs = [self._to_cam(cam, q) for q in world]
                mid = ((A[0] + B[0]) / 2, 0.0, (A[2] + B[2]) / 2)
                front = _v_dot(_v_sub(eye, mid), n_in) > 0
                if espejo and not front:
                    continue
                items.append((sum(c[2] for c in cs) / 4, b, k, K, cs, front))
        items.sort(key=lambda it: -it[0])
        draw = ImageDraw.Draw(img, "RGBA")
        dm = ImageDraw.Draw(mascara) if mascara is not None else None
        for _depth, b, k, K, cs, front in items:
            if max(c[2] for c in cs) < near:
                continue
            tex = self.tex.get(b["key"]) if front else None
            if tex is None or min(c[2] for c in cs) < near:
                c = self._clip_poly(cs, near)
                if len(c) < 3:
                    continue
                poly = [self._proj(cam, q) for q in c]
                draw.polygon(poly, fill=(40, 40, 44, 255) if not front else (12, 12, 14, 255), outline=(25, 25, 28, 255))
                if dm is not None:
                    dm.polygon(poly, fill=255)
                continue
            dst = [self._proj(cam, c) for c in cs]
            self._pintar_trozo(img, dst, tex, k, K, Image.Resampling.BILINEAR)
            if dm is not None:
                dm.polygon(dst, fill=255)

    def _reflejo(self, fondo):
        """v0.84: reflejo de las pantallas en el césped.
        - Las pantallas se reflejan en el suelo (espejo bajo la línea del suelo), pero cortas (REFLEJO_ALTO) y
          con un degradado que se apaga al alejarse del LED: queda pegado a la pantalla.
        - Se pinta a tamaño final y se reduce con media por cajas, nunca a resolución baja: así no parpadea.
        - Desenfoque mucho mayor en vertical que en horizontal (la hierba difumina la luz hacia abajo) y se
          modula con la luz del propio césped (franjas de corte, textura): no parece plástico ni hielo."""
        w, h = fondo.size
        # texturas del reflejo: reducidas en horizontal (van muy desenfocadas) y con el degradado ya aplicado
        tex_ref = {}
        for key, t in self.tex.items():
            cache = _REFLEJO_TEX.get(key)
            if cache is not None and cache[0] is t:
                tex_ref[key] = cache[1]
                continue
            pq = t.resize((max(2, t.width // 16), max(2, t.height // 2)), Image.Resampling.BOX)
            grad = _gradiente_reflejo(pq.size)
            r = ImageChops.multiply(pq, grad)
            _REFLEJO_TEX[key] = (t, r)
            tex_ref[key] = r
        tam, tex = self.size, self.tex
        self.size, self.tex = (w, h), tex_ref
        try:
            ref = Image.new("RGB", (w, h), (0, 0, 0))
            soporte = Image.new("L", (w, h), 0)
            self._bandas(ref, self._camera(), espejo=True, mascara=soporte, esc_espejo=REFLEJO_ALTO)
        finally:
            self.size, self.tex = tam, tex
        # todo lo demás a media resolución (el reflejo va muy desenfocado): se reduce con media por cajas
        mw, mh = max(8, w // 2), max(8, h // 2)
        ref = ref.resize((mw, mh), Image.Resampling.BOX)
        soporte = soporte.resize((mw, mh), Image.Resampling.BOX)
        # desenfoque anisótropo (mucho más en vertical), normalizado con el propio soporte: el color no se
        # diluye con el negro de alrededor y el borde queda suave
        rx = max(1.0, REFLEJO_BLUR_H * mw)
        ry = max(1.0, REFLEJO_BLUR_V * mh)
        r = np.asarray(_blur_aniso(ref, rx, ry), np.float32)
        sp = np.asarray(_blur_aniso(soporte, rx, ry), np.float32)[:, :, None] / 255.0
        alfa = np.clip(sp * 1.6, 0.0, 1.0)
        # solo por debajo del borde inferior de las pantallas: el desenfoque no sube a las pantallas ni a la grada
        m = np.asarray(soporte) > 8
        primera = np.where(m.any(axis=0), m.argmax(axis=0), mh)
        alfa = alfa * (np.arange(mh)[:, None] >= primera[None, :])[:, :, None]
        # integración con el césped: el reflejo solo se ve donde la hierba devuelve luz (franjas, textura)
        lum = np.asarray(fondo.resize((mw, mh), Image.Resampling.BOX).convert("L"), np.float32)[:, :, None]
        lum = np.minimum(lum * (2.4 / 255.0), 1.0)
        col = r / np.maximum(sp, 0.08) * alfa * lum * REFLEJO_LED
        return Image.fromarray(np.clip(col, 0, 255).astype(np.uint8), "RGB").resize((w, h), Image.Resampling.BILINEAR)

    def render_sobre(self, fondo, delantera=None):
        """Pantallas (pintadas al tamaño de la escena, con supersampling) sobre `fondo`, que ya está al tamaño
        final. Devuelve PIL RGB del tamaño de `fondo`."""
        k = max(1, int(round(self.size[0] / float(fondo.size[0]))))
        cam = self._camera()
        led = Image.new("RGB", self.size, (0, 0, 0))
        mascara = Image.new("L", self.size, 0)
        self._bandas(led, cam, mascara=mascara)
        if k > 1:
            led, mascara = led.reduce(k), mascara.reduce(k)
        if led.size != fondo.size:
            led, mascara = led.resize(fondo.size), mascara.resize(fondo.size)
        img = fondo.copy()
        if REFLEJO_LED > 0:
            img = ImageChops.add(img, self._reflejo(fondo))       # el reflejo va por debajo de las pantallas
        img.paste(led, (0, 0), mascara)
        if BRILLO_LED > 0:
            peq = (max(8, fondo.size[0] // 4), max(8, fondo.size[1] // 4))
            g = led.resize(peq, Image.Resampling.BOX).filter(ImageFilter.BoxBlur(2)).point(_lut(BRILLO_LED))
            img = ImageChops.add(img, g.resize(fondo.size, Image.Resampling.BILINEAR))
        if delantera is not None:
            img.paste(delantera, (0, 0), delantera)
        return img
