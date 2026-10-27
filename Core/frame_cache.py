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
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

from log_utils import debug, log_error

VIDEO_EXTS = (".mov", ".mp4", ".avi", ".m4v")


def is_video(path):
    return bool(path) and os.path.splitext(path)[1].lower() in VIDEO_EXTS


def _file_sig(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_size, int(st.st_mtime))


class FrameCache:
    def __init__(self, max_bytes=1500 * 1024 * 1024, workers=None):
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self._frames = OrderedDict()   # key -> PIL.Image (LRU)
        self._bytes = 0
        self._pending = {}             # key -> Future
        self._infos = {}               # sig -> dict(duration, size, fps)
        self._images = OrderedDict()   # sig -> PIL.Image (imágenes estáticas)
        workers = workers or max(2, min(4, (os.cpu_count() or 4) // 2))
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="frame_cache")

    # ---------- Metadatos ----------
    def info(self, path):
        """Devuelve {'duration', 'size', 'fps'} de un vídeo (cacheado)."""
        sig = _file_sig(path)
        with self._lock:
            if sig in self._infos:
                return self._infos[sig]
        raw = ffmpeg_parse_infos(path, decode_file=False)
        info = {
            "duration": float(raw.get("duration") or raw.get("video_duration") or 0.0),
            "size": tuple(raw.get("video_size") or (0, 0)),
            "fps": float(raw.get("video_fps") or 25.0),
        }
        with self._lock:
            self._infos[sig] = info
        return info

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
        arr = np.frombuffer(data[:expected], dtype=np.uint8).reshape((out_h, out_w, 4))
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
            img = self._decode(path, t, height, info)
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
