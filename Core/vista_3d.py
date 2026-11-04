"""
vista_3d.py (v0.46): 3D PREVIEW, la maqueta "fake 3D" del perímetro portada del modo 3D de Checker.

Cada tramo del strip (Goal Line Left, Touchline, Goal Line Right) es un plano vertical con su textura
en perspectiva, colocado en U alrededor de un campo de fútbol dibujado con sus líneas. La cara abierta
de la U mira a la cámara. No usa OpenGL: cada trozo se pinta con PIL (Image.transform PERSPECTIVE),
igual que Checker lo hace con QTransform.quadToQuad.

Unidades: píxeles del perfil del estadio (Width, Goal Line Size, Height). La altura de las bandas es la
real (Height en la misma escala que los anchos): para verla más grande, zoom.

Ratón (v0.47):
  botón izquierdo (o rueda pulsada) y arrastrar = mover la cámara en paralelo al suelo (v0.50)
  botón derecho y arrastrar                     = rotar la cámara sobre su propio centro (v0.48)
  rueda                                         = zoom de la cámara (cambia el FOV, la cámara no se mueve)
  doble clic                                    = vista inicial
La cámara nunca baja del suelo.
"""
import math
import tkinter as tk

import numpy as np
from PIL import Image, ImageDraw, ImageTk

try:
    _NEAREST = Image.Resampling.NEAREST
    _BILINEAR = Image.Resampling.BILINEAR
    _PERSPECTIVE = Image.Transform.PERSPECTIVE
except AttributeError:  # Pillow < 9.1
    _NEAREST, _BILINEAR, _PERSPECTIVE = Image.NEAREST, Image.BILINEAR, Image.PERSPECTIVE

FOV = 45.0                       # FOV inicial (vertical, grados)
FOV_MIN, FOV_MAX = 3.0, 75.0     # límites del zoom con la rueda
PITCH_RESET = math.radians(18)   # inclinación de la vista inicial (v0.47: más baja)
PITCH_MIN = math.radians(1)      # la cámara no puede bajar del suelo
TEX_LADDER = (1 / 64, 1 / 32, 1 / 16, 1 / 8, 1 / 4, 1 / 2, 1.0)  # escalones de resolución de la textura
TEX_STRIP_MAX_W = 16384          # ancho máximo del strip que se usa como textura (el de 40000 px se reduce)
PITCH_W_MIN = 64.0 / 110.0       # ancho/largo mínimo de un campo reglamentario (110 x 64 m)
PITCH_W_STD = 68.0 / 105.0       # campo estándar (105 x 68 m), solo si no hay goal line
SETTLE_MS = 180                  # tras soltar el ratón se repinta con filtrado suave
ROT_SENS = 0.5                   # v0.49: suavidad de la rotación con clic derecho (1 = la imagen sigue al ratón)


# --------------------------------------------------------------------------
# Geometría del estadio a partir del perfil
# --------------------------------------------------------------------------
def geometria_estadio(w_total, goal_line, h_total, amplify=False):
    """
    -> (L, W, bandas)
    L = largo del touchline (Width - 2 x Goal Line Size), W = largo de cada fondo.
    Si el goal line es más corto que el fondo de un campo reglamentario con ese touchline, el fondo
    se alarga hasta ese mínimo y el goal line queda pegado a la esquina del touchline, con el hueco
    hacia la cámara (a la izquierda de la portería izquierda y a la derecha de la derecha).
    bandas: dicts con side ('GLL'|'TL'|'GLR'), along (px desde el inicio del lado), length,
            y0/y1 (altura en el mundo), key y src = (x0, x1, v0, v1) del strip (px del perfil).
    AMPLIFY: el strip lleva las dos filas (arriba la 2nd row, abajo la 1st row, como el System File).
             La 2nd row se pinta sobre el touchline, centrada y justo encima de la 1st row.
    """
    w_total, h_total = int(max(1, w_total)), int(max(1, h_total))
    gl = int(max(0, min(goal_line, w_total // 2)))
    L = float(max(1, w_total - 2 * gl))
    W = float(max(gl, L * PITCH_W_MIN)) if gl > 0 else L * PITCH_W_STD

    if amplify:
        h_row = h_total // 2
        v_row1, v_row2 = (h_row, h_total), (0, h_row)
    else:
        h_row = h_total
        v_row1, v_row2 = (0, h_total), None

    bandas = []
    if gl > 0:
        # GLL se recorre de la esquina cercana a la lejana: el strip empieza en el extremo abierto
        bandas.append(dict(side="GLL", along=W - gl, length=gl, y0=0.0, y1=h_row,
                           key=(1, "GLL"), src=(0, gl) + v_row1))
    bandas.append(dict(side="TL", along=0.0, length=L, y0=0.0, y1=h_row,
                       key=(1, "TL"), src=(gl, gl + int(L)) + v_row1))
    if gl > 0:
        # GLR se recorre de la esquina lejana a la cercana: el strip llega a su extremo abierto
        bandas.append(dict(side="GLR", along=0.0, length=gl, y0=0.0, y1=h_row,
                           key=(1, "GLR"), src=(w_total - gl, w_total) + v_row1))
    if v_row2 is not None:
        # 2nd row: centrada en el touchline (along=None) y justo encima de la 1st row
        bandas.append(dict(side="TL", along=None, length=L, y0=float(h_row),
                           y1=float(h_row + (v_row2[1] - v_row2[0])),
                           key=(2, "TL"), src=(gl, gl + int(L)) + v_row2))
    return L, W, bandas


def escala_textura(w_total):
    """Factor al que se reduce el strip para usarlo como textura."""
    return min(1.0, TEX_STRIP_MAX_W / float(max(1, w_total)))


# --------------------------------------------------------------------------
# Vectores y campo
# --------------------------------------------------------------------------
def _v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _v_norm(a):
    n = math.sqrt(_v_dot(a, a)) or 1.0
    return (a[0] / n, a[1] / n, a[2] / n)


def field_lines():
    """Líneas de un campo reglamentario (105x68 m) como polilíneas en metros (x, z)."""
    hx, hz = 52.5, 34.0
    lines = [[(-hx, -hz), (hx, -hz), (hx, hz), (-hx, hz), (-hx, -hz)], [(0, -hz), (0, hz)]]

    def arc(cx, cz, r, a0, a1, n=48):
        return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * k / n)),
                 cz + r * math.sin(math.radians(a0 + (a1 - a0) * k / n))) for k in range(n + 1)]

    lines.append(arc(0, 0, 9.15, 0, 360, 72))
    lines.append(arc(0, 0, 0.3, 0, 360, 12))
    for s in (-1, 1):
        x = s * hx
        lines.append([(x, -20.16), (x - s * 16.5, -20.16), (x - s * 16.5, 20.16), (x, 20.16)])
        lines.append([(x, -9.16), (x - s * 5.5, -9.16), (x - s * 5.5, 9.16), (x, 9.16)])
        lines.append([(x, -3.66), (x + s * 2, -3.66), (x + s * 2, 3.66), (x, 3.66)])  # portería
        spot = x - s * 11
        lines.append(arc(spot, 0, 0.3, 0, 360, 12))
        a = math.degrees(math.acos(5.5 / 9.15))
        lines.append(arc(spot, 0, 9.15, -a, a, 24) if s < 0 else arc(spot, 0, 9.15, 180 - a, 180 + a, 24))
        for zs in (-1, 1):  # cuarto de círculo en cada córner
            lines.append([(x - s * math.cos(math.radians(9 * k)), zs * (hz - math.sin(math.radians(9 * k))))
                          for k in range(11)])
    return lines


FIELD_LINES = field_lines()


def _coeficientes(dst, src):
    """Coeficientes de Image.PERSPECTIVE: punto de salida (dst) -> punto de la textura (src)."""
    a, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return np.linalg.solve(np.array(a, dtype=float), np.array(b, dtype=float)).tolist()


# --------------------------------------------------------------------------
# Escena + cámara + render (sin Tk: se puede probar sin ventana)
# --------------------------------------------------------------------------
class Escena3D:
    def __init__(self):
        self.L, self.W = 1000.0, 650.0
        self.bands = []
        self.tex = {}          # key -> PIL RGBA opaca
        self.top = 1.0
        self.size = (800, 400)
        self.yaw, self.pitch, self.dist = 0.0, PITCH_RESET, 1000.0
        self.fov = FOV
        self.target = (0.0, 0.0, 0.0)
        self._bg = None

    # ----- contenido -----
    def set_scene(self, L, W, bands, reset=True):
        self.L, self.W = float(max(L, 1)), float(max(W, 1))
        out = []
        for b in bands:
            side_len = self.L if b["side"] == "TL" else self.W
            along = b.get("along")
            if along is None:
                along = (side_len - b["length"]) / 2
            out.append(dict(b, along=float(along)))
        self.bands = out
        self.top = max([b["y1"] for b in out] or [1.0])
        self.tex = {k: v for k, v in self.tex.items() if any(b["key"] == k for b in out)}
        if reset:
            self.reset_view()

    def set_strip(self, img, w_total, h_total):
        """img: el strip completo (cualquier escala; RGBA con transparencia = LED apagado)."""
        if img is None:
            self.tex = {}
            return
        sx, sy = img.width / float(max(1, w_total)), img.height / float(max(1, h_total))
        tex = {}
        for b in self.bands:
            x0, x1, v0, v1 = b["src"]
            box = (int(round(x0 * sx)), int(round(v0 * sy)), max(int(round(x0 * sx)) + 1, int(round(x1 * sx))),
                   max(int(round(v0 * sy)) + 1, int(round(v1 * sy))))
            crop = img.crop(box)
            if crop.mode == "RGBA":
                base = Image.new("RGB", crop.size, (0, 0, 0))   # transparente = LED apagado
                base.paste(crop, (0, 0), crop)
            else:
                base = crop.convert("RGB")
            tex[b["key"]] = base
        self.tex = tex

    def reset_view(self):
        w, h = self.size
        self.yaw, self.pitch = 0.0, PITCH_RESET
        self.fov = FOV
        tv = math.tan(math.radians(FOV) / 2)
        th = tv * max(w, 1) / max(h, 1)
        self.dist = max(self.L / 2 / th, self.W / 2 / tv) * 1.1
        self.target = (0.0, self.top / 2, -self.W * 0.05)
        self._clamp_suelo()
        self.encuadrar()

    def encuadrar(self, relleno=0.94):
        """v0.50: con la cámara quieta, centra la U en vertical (inclinando la cámara) y ajusta el FOV
        para que el estadio ocupe la ventana (relleno = fracción del ancho/alto disponible)."""
        w, h = self.size
        hl, hw = self.L / 2, self.W / 2
        pts = [(x, y, z) for x in (-hl, hl) for z in (-hw, hw) for y in (0.0, self.top)]
        for _ in range(3):
            cam = self._camera()
            cs = [self._to_cam(cam, q) for q in pts]
            if min(c[2] for c in cs) <= self._near():
                return
            pr = [self._proj(cam, c) for c in cs]
            ys = [p[1] for p in pr]
            off = (min(ys) + max(ys)) / 2 - h / 2      # >0: el estadio está por debajo del centro
            self.rotar_camara(0.0, math.atan2(off, cam[4]))
        cam = self._camera()
        pr = [self._proj(cam, self._to_cam(cam, q)) for q in pts]
        ext_x = max(abs(p[0] - w / 2) for p in pr) * 2
        ext_y = max(abs(p[1] - h / 2) for p in pr) * 2
        s = min(relleno * w / max(ext_x, 1.0), relleno * h / max(ext_y, 1.0))
        t = math.tan(math.radians(self.fov) / 2) / s
        t = max(math.tan(math.radians(FOV_MIN) / 2), min(math.tan(math.radians(FOV_MAX) / 2), t))
        self.fov = math.degrees(2 * math.atan(t))

    def escala_necesaria(self, w_total, k_max):
        """Escalón de TEX_LADDER que da al menos ~1.5 texels por píxel de pantalla en la zona del objetivo."""
        focal = self._camera()[4]
        s = 1.5 * focal / max(1.0, self.dist)
        for k in TEX_LADDER:
            if k >= s or k >= k_max:
                return min(k, k_max)
        return k_max

    def _clamp_suelo(self):
        """El ojo de la cámara siempre por encima del suelo (no se ve el estadio por debajo)."""
        min_y = max(1.0, self.top * 0.25)
        eye_y = self.target[1] + self.dist * math.sin(self.pitch)
        if eye_y < min_y:
            tx, ty, tz = self.target
            self.target = (tx, ty + (min_y - eye_y), tz)

    # ----- cámara -----
    def _camera(self):
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        tx, ty, tz = self.target
        eye = (tx + self.dist * cp * math.sin(self.yaw), ty + self.dist * sp,
               tz + self.dist * cp * math.cos(self.yaw))
        f = _v_norm(_v_sub(self.target, eye))
        r = _v_norm(_v_cross(f, (0.0, 1.0, 0.0)))
        u = _v_cross(r, f)
        focal = (self.size[1] / 2) / math.tan(math.radians(self.fov) / 2)
        return eye, r, u, f, focal

    @staticmethod
    def _to_cam(cam, p):
        eye, r, u, f, _ = cam
        d = _v_sub(p, eye)
        return (_v_dot(d, r), _v_dot(d, u), _v_dot(d, f))

    def _proj(self, cam, c):
        focal = cam[4]
        return (self.size[0] / 2 + focal * c[0] / c[2], self.size[1] / 2 - focal * c[1] / c[2])

    def _near(self):
        return max(self.dist * 0.002, 0.5)

    @staticmethod
    def _clip_poly(pts, near):
        """Sutherland-Hodgman contra el plano z = near (coordenadas de cámara)."""
        out = []
        n = len(pts)
        for k in range(n):
            a, b = pts[k], pts[(k + 1) % n]
            ina, inb = a[2] >= near, b[2] >= near
            if ina:
                out.append(a)
            if ina != inb:
                t = (near - a[2]) / (b[2] - a[2])
                out.append(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))
        return out

    def _side_geom(self, side):
        """(origen, dirección) de cada lado, de izquierda a derecha mirando desde el campo."""
        hl, hw = self.L / 2, self.W / 2
        return {"GLL": ((-hl, 0.0, hw), (0.0, 0.0, -1.0)),
                "TL": ((-hl, 0.0, -hw), (1.0, 0.0, 0.0)),
                "GLR": ((hl, 0.0, -hw), (0.0, 0.0, 1.0))}[side]

    def _fondo(self):
        w, h = self.size
        if self._bg is None or self._bg.size != (w, h):
            # degradado vertical como Checker: (118,122,128) -> (70,73,78) al 55% -> (38,40,44)
            t = np.linspace(0.0, 1.0, h)[:, None]
            top, mid, bot = np.array([118, 122, 128.]), np.array([70, 73, 78.]), np.array([38, 40, 44.])
            k1 = np.clip(t / 0.55, 0, 1)
            k2 = np.clip((t - 0.55) / 0.45, 0, 1)
            col = np.where(t <= 0.55, top + (mid - top) * k1, mid + (bot - mid) * k2)
            rows = np.repeat(col[:, None, :], w, axis=1).astype(np.uint8)
            self._bg = Image.fromarray(rows, "RGB")
        return self._bg.copy()

    # ----- dibujo -----
    def render(self, smooth=True):
        w, h = self.size
        img = self._fondo()
        draw = ImageDraw.Draw(img, "RGBA")
        cam = self._camera()
        near = self._near()

        def poly2d(world_pts):
            c = self._clip_poly([self._to_cam(cam, q) for q in world_pts], near)
            return [self._proj(cam, q) for q in c] if len(c) >= 3 else None

        # suelo: el rectángulo del perímetro un poco más oscuro y, dentro, las líneas del campo
        hl, hw = self.L / 2, self.W / 2
        ground = poly2d([(-hl, 0, -hw), (hl, 0, -hw), (hl, 0, hw), (-hl, 0, hw)])
        if ground is not None:
            draw.polygon(ground, fill=(0, 0, 0, 45))
        m = min(self.L, self.W) * 0.06  # margen entre las bandas y el campo
        sx, sz = (self.L - 2 * m) / 105.0, (self.W - 2 * m) / 68.0
        lw = max(1, int(round(h / 600.0)))
        for line in FIELD_LINES:
            pts = [self._to_cam(cam, (x * sx, 0.0, z * sz)) for x, z in line]
            for a, b in zip(pts, pts[1:]):
                if a[2] < near and b[2] < near:
                    continue
                if a[2] < near or b[2] < near:
                    t = (near - a[2]) / (b[2] - a[2])
                    c = tuple(a[i] + (b[i] - a[i]) * t for i in range(3))
                    a, b = (c, b) if a[2] < near else (a, c)
                draw.line([self._proj(cam, a), self._proj(cam, b)], fill=(255, 255, 255, 150), width=lw)

        # bandas troceadas: se recortan las que quedan detrás de la cámara y se pintan de lejos a cerca
        eye = cam[0]
        chunk_len = max(self.L, self.W) / 32
        items = []
        for b in self.bands:
            o, d = self._side_geom(b["side"])
            n_in = _v_cross(d, (0.0, 1.0, 0.0))  # normal hacia dentro del campo
            K = max(1, math.ceil(b["length"] / chunk_len))
            for k in range(K):
                s0 = b["along"] + b["length"] * k / K
                s1 = b["along"] + b["length"] * (k + 1) / K
                A = (o[0] + d[0] * s0, 0.0, o[2] + d[2] * s0)
                B = (o[0] + d[0] * s1, 0.0, o[2] + d[2] * s1)
                world = [(A[0], b["y1"], A[2]), (B[0], b["y1"], B[2]),
                         (B[0], b["y0"], B[2]), (A[0], b["y0"], A[2])]
                cs = [self._to_cam(cam, q) for q in world]
                mid = ((A[0] + B[0]) / 2, (b["y0"] + b["y1"]) / 2, (A[2] + B[2]) / 2)
                front = _v_dot(_v_sub(eye, mid), n_in) > 0
                items.append((sum(c[2] for c in cs) / 4, b, k, K, cs, front))
        items.sort(key=lambda it: -it[0])
        resample = _BILINEAR if smooth else _NEAREST
        for _depth, b, k, K, cs, front in items:
            if max(c[2] for c in cs) < near:
                continue
            tex = self.tex.get(b["key"]) if front else None
            if tex is None or min(c[2] for c in cs) < near:
                c = self._clip_poly(cs, near)
                if len(c) < 3:
                    continue
                draw.polygon([self._proj(cam, q) for q in c],
                             fill=(52, 54, 58, 255) if not front else (28, 28, 30, 255), outline=(25, 25, 28, 255))
                continue
            self._pintar_trozo(img, [self._proj(cam, c) for c in cs], tex, k, K, resample)
        return img

    def _pintar_trozo(self, img, dst, tex, k, K, resample):
        w, h = self.size
        xs, ys = [p[0] for p in dst], [p[1] for p in dst]
        bx0, by0 = max(0, int(math.floor(min(xs)))), max(0, int(math.floor(min(ys))))
        bx1, by1 = min(w, int(math.ceil(max(xs))) + 1), min(h, int(math.ceil(max(ys))) + 1)
        if bx1 - bx0 < 1 or by1 - by0 < 1:
            return
        tw, th = tex.size
        x0, x1 = tw * k / K, tw * (k + 1) / K
        cx0, cx1 = max(0, int(math.floor(x0)) - 1), min(tw, int(math.ceil(x1)) + 1)  # 1 texel de más: sin rendijas
        crop = tex.crop((cx0, 0, cx1, th))  # RGB opaca
        local = [(px - bx0, py - by0) for px, py in dst]
        src = [(x0 - cx0, 0), (x1 - cx0, 0), (x1 - cx0, th), (x0 - cx0, th)]
        try:
            coeffs = _coeficientes(local, src)
        except np.linalg.LinAlgError:
            return
        piece = crop.transform((bx1 - bx0, by1 - by0), _PERSPECTIVE, coeffs, resample, fillcolor=(0, 0, 0))
        # máscara con la forma exacta del trozo (fuera de él la proyección no vale)
        mask = Image.new("L", piece.size, 0)
        ImageDraw.Draw(mask).polygon(local, fill=255)
        img.paste(piece, (bx0, by0), mask)

    # ----- interacción -----
    def _offset_dir(self, yaw, pitch):
        cp = math.cos(pitch)
        return (cp * math.sin(yaw), math.sin(pitch), cp * math.cos(yaw))

    def orbit(self, dyaw, dpitch):
        """Gira la cámara alrededor del centro del campo; el objetivo gira con ella."""
        c = (0.0, 0.0, 0.0)
        v = _v_sub(self.target, c)
        ca, sa = math.cos(dyaw), math.sin(dyaw)
        v = (v[0] * ca + v[2] * sa, v[1], -v[0] * sa + v[2] * ca)
        yaw = self.yaw + dyaw
        pitch = max(PITCH_MIN, min(math.radians(89), self.pitch + dpitch))
        b = pitch - self.pitch
        if abs(b) > 1e-9:
            o1, o2 = self._offset_dir(yaw, self.pitch), self._offset_dir(yaw, pitch)
            kx = _v_norm(_v_cross(o1, o2))
            cb, sb = math.cos(b), math.sin(abs(b))
            kv = _v_cross(kx, v)
            kd = _v_dot(kx, v)
            v = tuple(v[i] * cb + kv[i] * sb + kx[i] * kd * (1 - cb) for i in range(3))
        self.target = (c[0] + v[0], c[1] + v[1], c[2] + v[2])
        self.yaw, self.pitch = yaw, pitch
        self._clamp_suelo()

    def rotar_camara(self, dyaw, dpitch):
        """v0.48: la cámara gira sobre sí misma (el ojo no se mueve; cambia hacia dónde mira)."""
        eye = self._camera()[0]
        self.yaw += dyaw
        self.pitch = max(math.radians(-60), min(math.radians(89), self.pitch + dpitch))
        o = self._offset_dir(self.yaw, self.pitch)
        self.target = (eye[0] - o[0] * self.dist, eye[1] - o[1] * self.dist, eye[2] - o[2] * self.dist)

    def pan(self, dx, dy):
        """v0.50: mueve la cámara en paralelo al suelo (la altura no cambia). El suelo sigue al ratón:
        arrastrar en horizontal = desplazarse a los lados, en vertical = avanzar / retroceder."""
        _eye, r, _u, f, focal = self._camera()
        k = self.dist / focal
        fh = _v_norm((f[0], 0.0, f[2]))                          # adelante, sin componente vertical
        kz = k / max(math.sin(abs(self.pitch)), 0.2)             # px de pantalla -> distancia sobre el suelo
        tx, ty, tz = self.target
        self.target = (tx - r[0] * dx * k + fh[0] * dy * kz, ty, tz - r[2] * dx * k + fh[2] * dy * kz)

    def _pick(self, px, py):
        """Punto de la maqueta bajo el puntero (suelo o paredes); si no toca nada, el del plano del objetivo."""
        eye, r, u, f, focal = self._camera()
        x, y = px - self.size[0] / 2, self.size[1] / 2 - py
        d = tuple(f[i] * focal + r[i] * x + u[i] * y for i in range(3))
        hl, hw, top = self.L / 2, self.W / 2, max(self.top, 1.0)
        best = [None]

        def hit(axis, value, check):
            if abs(d[axis]) < 1e-9:
                return
            t = (value - eye[axis]) / d[axis]
            if t <= 0 or (best[0] is not None and t >= best[0]):
                return
            q = tuple(eye[i] + d[i] * t for i in range(3))
            if check(q):
                best[0] = t

        mx, mz = hl * 1.25, hw * 1.25
        hit(1, 0.0, lambda q: abs(q[0]) <= mx and abs(q[2]) <= mz)
        hit(2, -hw, lambda q: abs(q[0]) <= hl and 0 <= q[1] <= top)   # TL
        hit(0, -hl, lambda q: abs(q[2]) <= hw and 0 <= q[1] <= top)   # GLL
        hit(0, hl, lambda q: abs(q[2]) <= hw and 0 <= q[1] <= top)    # GLR
        t = best[0] if best[0] is not None else self.dist / focal
        return tuple(eye[i] + d[i] * t for i in range(3))

    def zoom(self, notches, px, py):
        """Zoom de cámara: cambia el FOV. notches en unidades de la rueda de Windows (120 = un paso).
        La cámara mantiene su altura; en horizontal el punto bajo el puntero se queda bajo el puntero."""
        t_old = math.tan(math.radians(self.fov) / 2)
        t_new = t_old * 0.9985 ** notches
        t_new = max(math.tan(math.radians(FOV_MIN) / 2), min(math.tan(math.radians(FOV_MAX) / 2), t_new))
        if abs(t_new - t_old) < 1e-12:
            return
        # el rayo del puntero no cambia si el desplazamiento respecto al centro se escala igual que tan(FOV/2)
        cx, cy = self.size[0] / 2, self.size[1] / 2
        ox, oy = px - cx, py - cy
        self.fov = math.degrees(2 * math.atan(t_new))
        k = t_old / t_new
        # tras el zoom el punto que estaba en ox aparece en ox*k: se corrige solo en horizontal,
        # moviendo la cámara de lado (v0.50: la altura y la inclinación no cambian)
        self.pan(ox - ox * k, 0.0)


# --------------------------------------------------------------------------
# Widget Tk
# --------------------------------------------------------------------------
class Visor3D:
    """Pinta la Escena3D en un tk.Label que ocupa todo 'parent' y gestiona el ratón."""

    def __init__(self, parent, bg="#0c0c0c"):
        self.parent = parent
        self.escena = Escena3D()
        self.label = tk.Label(parent, bg=bg, bd=0, highlightthickness=0, cursor="fleur")
        self.label.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.placeholder = tk.Label(parent, text="3D Preview will appear here.", fg="gray", bg=bg)
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self._photo = None
        self._drag = None
        self.interacting = False
        self._render_id = None
        self._settle_id = None
        self._has_scene = False
        self._sized = False
        self._pending_reset = False
        self.activo = lambda: True   # app.py lo sustituye: solo se pinta si la pestaña está visible
        self.on_escala = None        # app.py: avisar cuando la cámara pide otra resolución de textura
        self._escala = None
        self._w_total = 1

        lb = self.label
        lb.bind("<Configure>", self._on_resize)
        for n in (1, 2, 3):
            lb.bind(f"<ButtonPress-{n}>", lambda e, n=n: self._press(e, n))
            lb.bind(f"<B{n}-Motion>", self._motion)
            lb.bind(f"<ButtonRelease-{n}>", self._release)
        lb.bind("<Double-Button-1>", lambda e: self.reset_view())
        lb.bind("<MouseWheel>", lambda e: self._wheel(e, e.delta))
        lb.bind("<Button-4>", lambda e: self._wheel(e, 120))   # Linux
        lb.bind("<Button-5>", lambda e: self._wheel(e, -120))

    # ----- API para app.py -----
    def set_scene(self, L, W, bands):
        """Rehace la maqueta; la cámara solo vuelve a la vista inicial si cambian las medidas."""
        e = self.escena
        reset = (not self._has_scene) or abs(e.L - L) > 0.5 or abs(e.W - W) > 0.5
        e.set_scene(L, W, bands, reset=reset and self._sized)
        if reset and not self._sized:
            self._pending_reset = True
        self._has_scene = True
        self.request_render()

    def escala_necesaria(self, w_total):
        """Resolución de textura (fracción del strip) que pide la cámara ahora mismo."""
        self._w_total = w_total
        k = self.escena.escala_necesaria(w_total, escala_textura(w_total))
        self._escala = k
        return k

    def set_strip(self, img, w_total, h_total):
        self._w_total = w_total
        self.escena.set_strip(img, w_total, h_total)
        if self.placeholder is not None:
            self.placeholder.destroy()
            self.placeholder = None
        self.render_now()

    def reset_view(self):
        self.escena.reset_view()
        self.request_render()

    # ----- pintado -----
    def request_render(self):
        if self._render_id is None:
            self._render_id = self.label.after_idle(self.render_now)

    def render_now(self):
        if self._render_id is not None:
            try:
                self.label.after_cancel(self._render_id)
            except Exception:
                pass
            self._render_id = None
        if not self._has_scene or self.placeholder is not None or not self.activo():
            return  # sin strip todavía no se pinta nada (evita los bloques negros)
        w, h = self.label.winfo_width(), self.label.winfo_height()
        if w <= 10 or h <= 10:
            return
        self.escena.size = (w, h)
        img = self.escena.render(smooth=not self.interacting)
        # si la cámara se ha acercado/alejado de escalón, pedir la textura a otra resolución
        if self._escala is not None and self.on_escala is not None and not self.interacting:
            k = self.escena.escala_necesaria(self._w_total, escala_textura(self._w_total))
            if k != self._escala:
                self._escala = k
                self.label.after_idle(self.on_escala)
        if self._photo is None or (self._photo.width(), self._photo.height()) != img.size:
            self._photo = ImageTk.PhotoImage(img)
            self.label.configure(image=self._photo)
        else:
            self._photo.paste(img)

    def _on_resize(self, _e):
        w, h = self.label.winfo_width(), self.label.winfo_height()
        if w > 10 and h > 10:
            self.escena.size = (w, h)
            if not self._sized or self._pending_reset:
                self._sized = True
                self._pending_reset = False
                self.escena.reset_view()
        self.request_render()

    # ----- ratón -----
    def _press(self, e, n):
        self._drag = (n, e.x, e.y)
        self.interacting = True
        self.label.configure(cursor="exchange" if n == 3 else "fleur")

    def _motion(self, e):
        if not self._drag:
            return
        n, lx, ly = self._drag
        dx, dy = e.x - lx, e.y - ly
        self._drag = (n, e.x, e.y)
        if n == 3:   # botón derecho: la cámara gira sobre su propio centro
            # v0.49: sensibilidad proporcional al FOV. Con ROT_SENS = 1 la imagen seguiría al ratón 1:1;
            # por debajo va más suave. Al hacer zoom (FOV menor) gira menos por píxel.
            k = math.radians(self.escena.fov) / max(1, self.label.winfo_height()) * ROT_SENS
            self.escena.rotar_camara(dx * k, -dy * k)
        else:        # izquierdo o rueda pulsada: mover
            self.escena.pan(dx, dy)
        self.request_render()

    def _release(self, _e):
        self._drag = None
        self.label.configure(cursor="fleur")
        self._settle()

    def _wheel(self, e, delta):
        if delta:
            self.escena.zoom(delta, e.x, e.y)
            self.interacting = True
            self._settle()
            self.request_render()

    def _settle(self):
        if self._settle_id is not None:
            self.label.after_cancel(self._settle_id)

        def done():
            self._settle_id = None
            if self._drag is None:
                self.interacting = False
                self.request_render()
        self._settle_id = self.label.after(SETTLE_MS, done)
