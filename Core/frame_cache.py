"""
Caché de frames para la previsualización de ARCHIE.

- Extrae frames sueltos llamando a ffmpeg directamente (sin abrir un VideoFileClip),
  ya escalados a la altura del strip y con alfa (ProRes 4444 -> RGBA).
- Cada (archivo, instante, altura) se decodifica una sola vez y se guarda en memoria (LRU).
- Las peticiones repetidas o simultáneas del mismo frame comparten el mismo trabajo.
- La caché se invalida sola si el archivo cambia en disco (tamaño o fecha).
"""
import os
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

import compat_ffmpeg  # noqa: F401  v0.78: MoviePy con ffmpeg 8 ('Stream group'), antes de usar MoviePy
from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

from log_utils import debug, log_error

VIDEO_EXTS = (".mov", ".mp4", ".avi", ".m4v")


def is_video(path):
    return bool(path) and os.path.splitext(path)[1].lower() in VIDEO_EXTS


# v1.05: la firma (ruta, tamaño, fecha) se cachea unos segundos por ruta. Antes cada consulta hacía un os.stat:
# con los ELEMENTS en el NAS (\\192.168.110.242) un Create Architecture lanzaba 400-850 stats por SMB (4-9 s solo
# en eso). El cambio de un fichero en disco se sigue detectando, como mucho SIG_TTL_S segundos más tarde.
SIG_TTL_S = 5.0
_SIGS = {}
_SIGS_LOCK = threading.Lock()
STATS = {"stat": 0, "stat_cache": 0}   # v1.05: contadores para perf.py


_TTL = None


def _ttl():
    """ARCHIE_SIG_TTL_S en Config/.env (se lee la 1ª vez, cuando el .env ya está cargado). 0 = sin caché."""
    global _TTL
    if _TTL is None:
        try:
            _TTL = max(0.0, float(os.getenv("ARCHIE_SIG_TTL_S", "") or SIG_TTL_S))
        except ValueError:
            _TTL = SIG_TTL_S
    return _TTL


_REFRESCANDO = set()


def _refrescar_firma(path):
    try:
        _stat_y_guardar(path, time.monotonic())
    except OSError:
        pass
    finally:
        with _SIGS_LOCK:
            _REFRESCANDO.discard(path)


def _file_sig(path):
    ahora = time.monotonic()
    with _SIGS_LOCK:
        c = _SIGS.get(path)
        if c is not None and ahora - c[0] < _ttl():
            STATS["stat_cache"] += 1
            return c[1]
        # v1.11: en el hilo de la ventana nunca se espera al NAS. Si ya hay una firma (aunque tenga más de unos
        # segundos) se usa y se comprueba en segundo plano: un os.stat por SMB con el NAS ocupado (escaneo de
        # CAMPAIGNS, ffmpeg leyendo ProRes) tardaba 3-4 s y la ventana dejaba de responder.
        if c is not None and threading.current_thread() is threading.main_thread():
            STATS["stat_cache"] += 1
            if path not in _REFRESCANDO:
                _REFRESCANDO.add(path)
                threading.Thread(target=_refrescar_firma, args=(path,), daemon=True).start()
            return c[1]
    return _stat_y_guardar(path, ahora)


def _stat_y_guardar(path, ahora):
    st = os.stat(path)
    sig = (os.path.abspath(path), st.st_size, int(st.st_mtime))
    with _SIGS_LOCK:
        STATS["stat"] += 1
        _SIGS[path] = (ahora, sig)
        if len(_SIGS) > 512:
            _SIGS.clear()
    return sig


def file_sig(path):
    """Firma cacheada (ruta, tamaño, fecha) de un fichero. La usan también alpha_utils y el solver."""
    return _file_sig(path)


def olvidar_firmas():
    with _SIGS_LOCK:
        _SIGS.clear()


class FrameCache:
    def __init__(self, max_bytes=1500 * 1024 * 1024, workers=None):
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._frames = OrderedDict()   # key -> PIL.Image (LRU)
        self._bytes = 0
        self._pending = {}             # key -> Future
        self._infos = {}               # sig -> dict(duration, size, fps)
        self._images = OrderedDict()   # sig -> PIL.Image (imágenes estáticas)
        self._info_vuelo = {}          # v1.11: sig -> Event del ffprobe en curso (uno por fichero, no uno por hilo)
        self._precargando = set()      # v1.11: rutas que se están leyendo en segundo plano
        self._fallidos = set()         # v1.11: rutas cuya lectura falló (no se espera por ellas)
        workers = workers or max(2, min(4, (os.cpu_count() or 4) // 2))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="frame_cache")

    # ---------- Metadatos ----------
    def info(self, path):
        """Devuelve {'duration', 'size', 'fps'} de un vídeo (cacheado)."""
        sig = _file_sig(path)
        with self._lock:
            if sig in self._infos:
                return self._infos[sig]
            # v1.11: si otro hilo ya está leyendo este fichero, se espera a su resultado. Antes cada hilo (preview,
            # solver, LOGO UNDER, ventana) lanzaba su propio ffmpeg contra el mismo vídeo del NAS a la vez, y con
            # el NAS ocupado cada uno tardaba más de 3 s.
            ev = self._info_vuelo.get(sig)
            mio = ev is None
            if mio:
                ev = self._info_vuelo[sig] = threading.Event()
        if not mio:
            ev.wait()
            with self._lock:
                if sig in self._infos:
                    return self._infos[sig]
            return self.info(path)   # el otro hilo falló: se intenta de nuevo
        try:
            raw = ffmpeg_parse_infos(path, decode_file=False)
            info = {
                "duration": float(raw.get("duration") or raw.get("video_duration") or 0.0),
                "size": tuple(raw.get("video_size") or (0, 0)),
                "fps": float(raw.get("video_fps") or 25.0),
            }
            with self._lock:
                self._infos[sig] = info
            return info
        finally:
            with self._lock:
                self._info_vuelo.pop(sig, None)
            ev.set()

    def duration(self, path):
        try:
            return self.info(path)["duration"] if is_video(path) else 0.0
        except Exception as e:
            log_error(f"FrameCache: no se pudo leer la duración de '{path}': {e}")
            return 0.0

    def dimensions(self, path):
        if is_video(path):
            return self.info(path)["size"]
        return self.image(path).size

    # ---------- v1.11: lectura anticipada (para no esperar al NAS en el hilo de la ventana) ----------
    def conocido(self, path):
        """True si dimensions(path) responde ya sin tocar el disco (o si no hay fichero / su lectura falló)."""
        if not path or path in self._fallidos:
            return True
        with _SIGS_LOCK:
            c = _SIGS.get(path)
        if c is None:
            return False
        with self._lock:
            return c[1] in (self._infos if is_video(path) else self._images)

    def leyendo(self):
        """v1.11: True mientras hay lecturas de ELEMENTS en curso (el escaneo del NAS les cede el paso)."""
        with self._lock:
            return bool(self._info_vuelo or self._precargando)

    def precargar(self, paths):
        """Lee en segundo plano firma + metadatos (vídeo) o la imagen de cada ruta. Sin duplicados."""
        for p in dict.fromkeys(p for p in paths if p):
            with self._lock:
                if p in self._precargando:
                    continue
                self._precargando.add(p)
            self._fallidos.discard(p)
            threading.Thread(target=self._precargar_una, args=(p,), daemon=True, name="precarga").start()

    def _precargar_una(self, path):
        try:
            self.dimensions(path)
        except Exception as e:
            self._fallidos.add(path)
            log_error(f"FrameCache: no se pudo leer '{path}': {e}")
        finally:
            with self._lock:
                self._precargando.discard(path)

    # ---------- Imágenes estáticas ----------
    def image(self, path):
        """Imagen estática como RGBA, decodificada una sola vez."""
        sig = _file_sig(path)
        with self._lock:
            img = self._images.get(sig)
            if img is not None:
                self._images.move_to_end(sig)
                return img
        img = Image.open(path).convert("RGBA")
        img.load()
        with self._lock:
            self._images[sig] = img
            while len(self._images) > 32:
                self._images.popitem(last=False)
        return img

    # ---------- Frames de vídeo ----------
    def _key(self, path, t, height):
        info = self.info(path)
        dur = info["duration"]
        fps = info["fps"] or 25.0
        if dur > 0:
            t = t % dur
            # ffmpeg no devuelve nada si buscamos más allá del último frame
            t = min(t, max(0.0, dur - 1.0 / fps))
        frame_idx = int(t * fps + 1e-5)
        return (_file_sig(path), frame_idx, int(height or 0)), frame_idx / fps, info

    def _decode(self, path, t, height, info):
        src_w, src_h = info["size"]
        if height and src_h:
            out_h = int(height)
            out_w = max(1, int(round(src_w * out_h / float(src_h))))
        else:
            out_w, out_h = src_w, src_h
        cmd = [
            FFMPEG_BINARY, "-v", "error", "-nostdin",
            "-ss", f"{t:.4f}", "-i", path,
            "-frames:v", "1", "-an",
            "-vf", f"scale={out_w}:{out_h}:flags=bilinear",
            "-pix_fmt", "rgba", "-f", "rawvideo", "-",
        ]
        params = cross_platform_popen_params({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL})
        proc = subprocess.run(cmd, **params)
        expected = out_w * out_h * 4
        data = proc.stdout
        if len(data) < expected:
            if t > 0:
                debug(f"FrameCache: frame vacío en t={t:.3f}s de '{os.path.basename(path)}', reintentando en t=0")
                return self._decode(path, 0.0, height, info)
            raise RuntimeError(proc.stderr.decode(errors="ignore")[:300] or "ffmpeg no devolvió datos")
        data = data[:expected]
        try:  # v0.87: alfa premultiplicado (ProRes 4444 de AE) -> recto, sin rebaba negra en la preview
            from alpha_utils import es_premultiplicado, despremultiplicar
            if es_premultiplicado(path):
                data = despremultiplicar(data, out_w, out_h)
        except Exception as e:
            debug(f"FrameCache: no se pudo comprobar el alfa de '{os.path.basename(path)}': {e}")
        arr = np.frombuffer(data, dtype=np.uint8).reshape((out_h, out_w, 4))
        return Image.fromarray(arr, "RGBA")

    def _store(self, key, img):
        size = img.width * img.height * 4
        with self._lock:
            self._pending.pop(key, None)
            if key not in self._frames:
                self._frames[key] = img
                self._bytes += size
            while self._bytes > self.max_bytes and len(self._frames) > 1:
                _, old = self._frames.popitem(last=False)
                self._bytes -= old.width * old.height * 4

    def _job(self, key, path, t, height, info):
        try:
            t0 = time.perf_counter()
            img = self._decode(path, t, height, info)
            ms = (time.perf_counter() - t0) * 1000.0
            if ms > 400:   # v1.05: con ARCHIE_PERF=1, frames que tardan (NAS lento, ProRes pesado)
                from perf import log as perf_log
                perf_log(f"frame lento {ms:.0f} ms · {os.path.basename(path)} t={t:.2f}s h={height}")
        except Exception:
            with self._lock:
                self._pending.pop(key, None)
            raise
        self._store(key, img)
        return img

    def request(self, path, t, height=None):
        """Encola la decodificación de un frame y devuelve un Future (o el frame ya cacheado envuelto)."""
        key, t_snap, info = self._key(path, t, height)
        with self._lock:
            img = self._frames.get(key)
            if img is not None:
                self._frames.move_to_end(key)
                fut = _Done(img)
                return fut
            fut = self._pending.get(key)
            if fut is None:
                fut = self._pool.submit(self._job, key, path, t_snap, height, info)
                self._pending[key] = fut
            return fut

    def cancelar(self, futures):
        """v1.11: quita de la cola las decodificaciones que aún no han empezado (las que ya corren terminan y se
        guardan en la caché como siempre)."""
        with self._lock:
            for fut in futures:
                if isinstance(fut, _Done) or not fut.cancel():
                    continue
                for k, f in list(self._pending.items()):
                    if f is fut:
                        del self._pending[k]

    def frame(self, path, t, height=None):
        """Frame RGBA en el instante t (en bucle sobre la duración), escalado a 'height'."""
        return self.request(path, t, height).result()

    def frame_sync(self, path, t, height=None):
        """Como frame(), pero decodifica en el hilo que llama (para el hilo de Tk: nunca espera al pool)."""
        key, t_snap, info = self._key(path, t, height)
        with self._lock:
            img = self._frames.get(key)
            if img is not None:
                self._frames.move_to_end(key)
                return img
        img = self._decode(path, t_snap, height, info)
        self._store(key, img)
        return img

    def clear(self):
        olvidar_firmas()
        with self._lock:
            self._frames.clear()
            self._images.clear()
            self._infos.clear()
            self._bytes = 0


class _Done:
    """Future ya resuelto (para frames que estaban en caché)."""
    def __init__(self, value):
        self._value = value

    def result(self, timeout=None):
        return self._value

    def done(self):
        return True


# Instancia compartida por toda la app
FRAME_CACHE = FrameCache()
