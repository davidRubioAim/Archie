"""
Reproductor de vídeo de la Architecture Preview (v0.35).

Funciona como las instancias de Cinema 4D:
- Cada ELEMENT de vídeo se decodifica UNA sola vez (un proceso ffmpeg por archivo) a un proxy
  de baja resolución que se guarda en memoria. El proxy no depende de la arquitectura: cambiar
  sliders o volver a pulsar Create Architecture no vuelve a decodificar nada.
- La escena es una lista de instancias (archivo + posición en el strip). En cada tick se toma
  el frame del proxy de cada archivo y se pinta en todas sus instancias, directamente a la
  resolución de pantalla y solo en la parte visible (zoom/arrastre incluidos).
- Lo estático (textos, logos, center board, fondo si es imagen) se compone una vez como capa base.
- En pausa se sustituye el proxy por el frame a resolución completa (FRAME_CACHE).
"""
import atexit
import math
import os
import subprocess
import threading
import time
import weakref

from PIL import Image, ImageTk

from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params

from frame_cache import FRAME_CACHE
from log_utils import debug, log_error

try:
    _BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    _BILINEAR = Image.BILINEAR

PLAYER_FPS = 12                         # frames por segundo de la reproducción (y de los proxies)
PROXY_LADDER = (24, 48, 96, 144, 216, 288)
PROXY_BUDGET = 700 * 1024 * 1024        # memoria máxima para proxies de todos los ELEMENTS
RESUME_DELAY_MS = 900                   # reanudar tras dejar de tocar sliders

_READERS = weakref.WeakSet()


@atexit.register
def _stop_all_readers():
    for r in list(_READERS):
        r.stop()


class ProxyReader:
    """Decodifica un vídeo entero, una vez, a PLAYER_FPS y altura 'height', guardando los frames en RAM."""

    def __init__(self, path, info, height, fps=PLAYER_FPS):
        self.path = path
        self.fps = fps
        src_w, src_h = info["size"]
        self.height = int(height)
        self.width = max(1, int(round(src_w * self.height / float(max(1, src_h)))))
        self.duration = max(1.0 / fps, float(info["duration"] or 0.0))
        self.total = max(1, int(math.ceil(self.duration * fps)))
        self.frames = []        # bytes RGBA (los bytes no los rastrea el GC: nada de objetos Tk/PIL en este hilo)
        self.done = False
        self.error = None
        self._proc = None
        self._stopped = False
        _READERS.add(self)
        threading.Thread(target=self._run, daemon=True, name=f"proxy:{os.path.basename(path)}").start()

    @property
    def nbytes(self):
        return self.width * self.height * 4 * self.total

    def _run(self):
        cmd = [
            FFMPEG_BINARY, "-v", "error", "-nostdin", "-threads", "2",
            "-i", self.path, "-an",
            "-vf", f"fps={self.fps},scale={self.width}:{self.height}:flags=area",
            "-pix_fmt", "rgba", "-f", "rawvideo", "-",
        ]
        frame_size = self.width * self.height * 4
        try:
            params = cross_platform_popen_params({"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL})
            self._proc = subprocess.Popen(cmd, bufsize=frame_size * 4, **params)
            read = self._proc.stdout.read
            while not self._stopped:
                data = read(frame_size)
                if len(data) < frame_size:
                    break
                self.frames.append(data)
            if not self.frames and not self._stopped:
                raise RuntimeError("ffmpeg no devolvió frames")
            self.total = max(1, len(self.frames)) if not self._stopped else self.total
        except Exception as e:
            self.error = e
            log_error(f"Reproductor: no se pudo decodificar '{self.path}': {e}")
        finally:
            self.done = True
            self._kill()
            debug(f"Reproductor: proxy {os.path.basename(self.path)} {self.width}x{self.height} -> {len(self.frames)} frames")

    def _kill(self):
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        if proc:
            try:
                proc.stdout.close()
            except Exception:
                pass

    def stop(self):
        self._stopped = True
        self._kill()

    def index(self, t):
        return min(self.total - 1, int((t % self.duration) * self.fps + 1e-6))

    def has(self, t):
        return self.index(t) < len(self.frames) or (self.done and bool(self.frames))

    def image(self, t):
        """Frame en t, o el último decodificado si aún no ha llegado (None si no hay ninguno)."""
        n = len(self.frames)
        if not n:
            return None
        idx = min(self.index(t), n - 1)
        return Image.frombuffer("RGBA", (self.width, self.height), self.frames[idx], "raw", "RGBA", 0, 1)

    def progress(self):
        return 1.0 if self.done else min(1.0, len(self.frames) / float(self.total))


class Escena:
    """Lo que hay que pintar: capa estática + instancias de cada ELEMENT."""

    def __init__(self, static_res, segmentos, h_total, instancias, videos):
        self.static_res = static_res            # PIL RGBA con lo que no se mueve
        self.res_size = static_res.size
        self.segmentos = segmentos              # [(x_ini, x_fin, x_res, y_res)] (motor_distribucion)
        self.h_total = h_total
        # instancias en orden de pintado: dict(src=ruta_video | PIL.Image, x=, w=, mask=bool)
        self.instancias = instancias
        self.videos = videos                    # {ruta: info} de los vídeos únicos animados
        self.duration = max([v["duration"] for v in videos.values()] + [1.0 / PLAYER_FPS])


class PreviewPlayer:
    """
    app debe ofrecer:
      app.after / app.after_cancel
      app.player_view()            -> dict(cw, ch, zoom, pan_x, pan_y) o None
      app.player_static_view(img)  -> PIL del tamaño del contenedor (zoom/pan aplicados), para la capa base
      app.player_show(img)         -> pinta el frame en el panel
      app.player_state_changed()   -> refrescar botones / barra / tiempo
    """

    def __init__(self, app):
        self.app = app
        self.escena = None
        self.t = 0.0
        self.playing = False
        self.loop = True
        self.engaged = False            # True cuando el panel muestra el reproductor en vez de los 8 keyframes
        self.buffering = False
        self.readers = {}               # (ruta, altura) -> ProxyReader
        self.proxy_h = None
        self._tick_id = None
        self._resume_id = None
        self._hq_id = None
        self._resume_after_interrupt = False
        self._held = False
        self._last_wall = None
        self._view_key = None
        self._base_view = None
        self._placements = None
        self._still_memo = {}
        self._hq = None                 # (t, {ruta: PIL}) frames a resolución completa para la pausa
        self.last_render_ms = 0.0

    # ---------- Escena ----------
    def set_escena(self, escena):
        self.escena = escena
        self._view_key = None
        self._hq = None
        if not escena.videos:
            self.pause()
            self.engaged = False
        else:
            self.t = self.t % escena.duration if escena.duration else 0.0
            # liberar proxies de archivos que ya no están en la escena
            for key in list(self.readers):
                if key[0] not in escena.videos:
                    self.readers.pop(key).stop()
            if self.engaged:
                self._ensure_readers()
        self.app.player_state_changed()
        if self.engaged:
            self.render()
            if not self.playing:
                self._schedule_hq()

    def clear(self):
        self.pause()
        self.escena = None
        self.engaged = False
        self.app.player_state_changed()

    @property
    def ready(self):
        return self.escena is not None and bool(self.escena.videos)

    @property
    def duration(self):
        return self.escena.duration if self.escena else 0.0

    # ---------- Proxies ----------
    def _altura_proxy(self, view):
        h = self.escena.h_total
        rw, rh = self.escena.res_size
        on_screen = h * max(1, int(rh * view["zoom"])) / float(rh)
        ladder = sorted({min(h, v) for v in PROXY_LADDER} | {h})
        target = next((v for v in ladder if v >= on_screen), ladder[-1])
        # bajar de escalón si no cabe en el presupuesto de memoria
        while True:
            total = 0
            for info in self.escena.videos.values():
                sw, sh = info["size"]
                total += int(round(sw * target / float(max(1, sh)))) * target * 4 * math.ceil(max(info["duration"], 0.1) * PLAYER_FPS)
            if total <= PROXY_BUDGET or target == ladder[0]:
                return target
            target = ladder[max(0, ladder.index(target) - 1)]

    def _ensure_readers(self):
        if not self.ready: return
        view = self.app.player_view()
        if view is None: return
        h = self._altura_proxy(view)
        if self.proxy_h is not None and h <= self.proxy_h and all((p, self.proxy_h) in self.readers for p in self.escena.videos):
            return  # el proxy actual ya tiene resolución de sobra
        self.proxy_h = h
        for path, info in self.escena.videos.items():
            if (path, h) not in self.readers:
                self.readers[(path, h)] = ProxyReader(path, info, h)
        # los proxies de otras alturas se quedan hasta que el nuevo termine (ver _gc_readers)

    def _gc_readers(self):
        for key in list(self.readers):
            path, h = key
            if h != self.proxy_h and (path, self.proxy_h) in self.readers and self.readers[(path, self.proxy_h)].done:
                self.readers.pop(key).stop()

    def _reader(self, path, t):
        """Mejor proxy disponible para path en t: el de la altura actual o, mientras carga, otro que ya tenga el frame."""
        cur = self.readers.get((path, self.proxy_h))
        if cur is not None and cur.has(t):
            return cur
        for (p, h), r in sorted(self.readers.items(), key=lambda kv: -kv[0][1]):
            if p == path and r.has(t):
                return r
        return cur

    def buffer_progress(self):
        if not self.ready or self.proxy_h is None: return 0.0
        rs = [self.readers.get((p, self.proxy_h)) for p in self.escena.videos]
        rs = [r for r in rs if r is not None]
        return min([r.progress() for r in rs] or [0.0])

    # ---------- Transporte ----------
    def play(self):
        if not self.ready: return
        if self.duration and not self.loop and self.t >= self.duration - 1e-3:
            self.t = 0.0
        self.engaged = True
        self.playing = True
        self._hq = None
        self._cancel(self._hq_id); self._hq_id = None
        self._ensure_readers()
        self._last_wall = time.perf_counter()
        self.app.player_state_changed()
        self._tick()

    @property
    def hq_shown(self):
        return self._hq is not None and abs(self._hq[0] - self.t) < 1e-6

    def pause(self):
        self._resume_after_interrupt = False
        self._cancel(self._resume_id); self._resume_id = None
        was = self.playing
        self.playing = False
        self.buffering = False
        self._cancel(self._tick_id); self._tick_id = None
        if was:
            self.app.player_state_changed()
            self._schedule_hq()

    def toggle(self):
        if self.playing: self.pause()
        else: self.play()

    def seek(self, t, hq=False):
        if not self.ready: return
        self.engaged = True
        self.t = max(0.0, min(self.duration, t))
        self._hq = None
        self._ensure_readers()
        self.render()
        self.app.player_state_changed()
        if hq and not self.playing:
            self._schedule_hq()

    def interrupt(self):
        """El usuario está tocando parámetros: pausar y reanudar solo cuando deje de hacerlo."""
        if self._held: return
        if self.playing:
            self.pause()
            self._resume_after_interrupt = True
        if self._resume_after_interrupt:
            self._cancel(self._resume_id)
            self._resume_id = self.app.after(RESUME_DELAY_MS, self._resume)

    def hold(self):
        """Como interrupt(), pero sin temporizador (p. ej. mientras se calcula la arquitectura)."""
        if self.playing:
            self.pause()
            self._resume_after_interrupt = True
        self._cancel(self._resume_id); self._resume_id = None
        self._held = True

    def release(self, immediate=False):
        self._held = False
        if self._resume_after_interrupt:
            self._cancel(self._resume_id)
            self._resume_id = self.app.after(1 if immediate else RESUME_DELAY_MS, self._resume)

    def _resume(self):
        self._resume_id = None
        if self._resume_after_interrupt:
            self._resume_after_interrupt = False
            self.play()

    def _cancel(self, after_id):
        if after_id:
            try: self.app.after_cancel(after_id)
            except Exception: pass

    def _tick(self):
        self._tick_id = None
        if not self.playing or not self.ready: return
        start = time.perf_counter()
        dt = start - (self._last_wall or start)
        self._last_wall = start

        # Buffering: si algún vídeo aún no tiene decodificado el frame de t, el reloj espera
        self.buffering = not all(self._reader(p, self.t) is not None and self._reader(p, self.t).has(self.t) for p in self.escena.videos)
        if not self.buffering:
            self.t += dt
            if self.t >= self.duration:
                if self.loop:
                    self.t %= self.duration
                else:
                    self.t = self.duration
                    self.render()
                    self.pause()
                    return
        self._gc_readers()
        self.render()
        self.app.player_state_changed()
        elapsed = time.perf_counter() - start
        delay = max(5, int((1.0 / PLAYER_FPS - elapsed) * 1000))
        self._tick_id = self.app.after(delay, self._tick)

    # ---------- Calidad completa en pausa ----------
    def _schedule_hq(self, delay=150):
        self._cancel(self._hq_id)
        self._hq_id = self.app.after(delay, self._start_hq)

    def _start_hq(self):
        self._hq_id = None
        if self.playing or not self.ready or not self.engaged: return
        if self.proxy_h is not None and self.proxy_h >= self.escena.h_total: return
        t = self.t
        h = self.escena.h_total
        futures = {}
        try:
            for path in self.escena.videos:
                futures[path] = FRAME_CACHE.request(path, t, h)
        except Exception as e:
            debug(f"Reproductor: HQ no disponible ({e})")
            return
        self._poll_hq(t, futures)

    def _poll_hq(self, t, futures):
        if self.playing or abs(t - self.t) > 1e-6 or not self.ready: return
        if not all(f.done() for f in futures.values()):
            self._hq_id = self.app.after(40, self._poll_hq, t, futures)
            return
        self._hq_id = None
        frames = {}
        for path, f in futures.items():
            try: frames[path] = f.result()
            except Exception: pass
        self._hq = (t, frames)
        self.render()
        self.app.player_state_changed()

    # ---------- Pintado ----------
    def _prepare_view(self, view):
        key = (view["cw"], view["ch"], round(view["zoom"], 6), round(view["pan_x"], 2), round(view["pan_y"], 2), id(self.escena))
        if key == self._view_key:
            return
        self._view_key = key
        self._still_memo = {}
        self._base_view = self.app.player_static_view(self.escena.static_res).convert("RGBA")
        if self.engaged and self.ready:
            self._ensure_readers()
            self._gc_readers()

        rw, rh = self.escena.res_size
        cw, ch, zoom = view["cw"], view["ch"], view["zoom"]
        sw, sh = max(1, int(rw * zoom)), max(1, int(rh * zoom))
        px = int((cw - sw) / 2 + view["pan_x"])
        py = int((ch - sh) / 2 + view["pan_y"])
        kx, ky = sw / float(rw), sh / float(rh)
        h_total = self.escena.h_total

        placements = []
        for inst in self.escena.instancias:
            x, w = inst["x"], inst["w"]
            subs = []
            if w > 0:
                for a, b, rx, ry in self.escena.segmentos:
                    lo, hi = max(x, a), min(x + w, b, a + (rw - rx))
                    if hi <= lo: continue
                    vx0, vx1 = px + (rx + lo - a) * kx, px + (rx + hi - a) * kx
                    vy0, vy1 = py + ry * ky, py + (ry + h_total) * ky
                    dx0, dx1 = max(0, int(round(vx0))), min(cw, int(round(vx1)))
                    dy0, dy1 = max(0, int(round(vy0))), min(ch, int(round(vy1)))
                    if dx1 - dx0 < 1 or dy1 - dy0 < 1: continue
                    # coordenadas fuente normalizadas (0..1 sobre el frame completo de la instancia)
                    u0 = ((dx0 - px) / kx - rx + a - x) / w
                    u1 = ((dx1 - px) / kx - rx + a - x) / w
                    v0 = ((dy0 - py) / ky - ry) / h_total
                    v1 = ((dy1 - py) / ky - ry) / h_total
                    subs.append((dx0, dy0, dx1 - dx0, dy1 - dy0,
                                 max(0.0, u0), max(0.0, v0), min(1.0, u1), min(1.0, v1)))
            placements.append(subs)
        self._placements = placements

    def _source(self, src, t):
        if not isinstance(src, str):
            return src, True                                  # imagen estática
        if self._hq is not None and abs(self._hq[0] - t) < 1e-6 and src in self._hq[1]:
            return self._hq[1][src], True
        r = self._reader(src, t)
        if r is not None:
            img = r.image(t)
            if img is not None:
                return img, False
        return None, False

    def compose(self, view, t):
        self._prepare_view(view)
        canvas = self._base_view.copy()
        frames = {}
        memo = {}
        for inst, subs in zip(self.escena.instancias, self._placements):
            if not subs: continue
            src = inst["src"]
            key_src = src if isinstance(src, str) else id(src)
            if key_src not in frames:
                frames[key_src] = self._source(src, t)
            img, persistent = frames[key_src]
            if img is None: continue
            fw, fh = img.size
            store = self._still_memo if (persistent and not isinstance(src, str)) else memo
            for dx, dy, tw, th, u0, v0, u1, v1 in subs:
                box = (u0 * fw, v0 * fh, u1 * fw, v1 * fh)
                k = (key_src, round(box[0], 3), round(box[1], 3), round(box[2], 3), round(box[3], 3), tw, th)
                piece = store.get(k)
                if piece is None:
                    piece = img.resize((tw, th), _BILINEAR, box=box)
                    store[k] = piece
                canvas.paste(piece, (dx, dy), piece if inst.get("mask", True) else None)
        return canvas

    def render(self):
        if not self.ready or not self.engaged: return
        view = self.app.player_view()
        if view is None: return
        t0 = time.perf_counter()
        img = self.compose(view, self.t)
        self.app.player_show(img)
        self.last_render_ms = (time.perf_counter() - t0) * 1000.0


class PhotoTarget:
    """PhotoImage persistente: cada frame se copia encima (photo.paste) sin crear objetos Tk nuevos."""

    def __init__(self):
        self.photo = None
        self.size = None

    def show(self, tk_label, img):
        if self.photo is None or self.size != img.size:
            self.photo = ImageTk.PhotoImage(img)
            self.size = img.size
            tk_label.configure(image=self.photo)
        else:
            self.photo.paste(img)
            if str(tk_label.cget("image")) != str(self.photo):
                tk_label.configure(image=self.photo)
