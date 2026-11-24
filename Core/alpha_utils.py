"""
v0.87: alfa premultiplicado en los ELEMENTS (ProRes 4444, PNG/MOV exportados de AE con "Premultiplied").

ffmpeg entrega el RGBA tal cual viene en el archivo. Si el vídeo está premultiplicado (RGB ya multiplicado por el alfa,
"matted with black"), al pegarlo con PIL usando el alfa como máscara el borde se multiplica DOS veces por el alfa
y aparece una rebaba negra. After Effects lo detecta e interpreta como premultiplicado, por eso allí no se ve.

Solución: detectar si el vídeo está premultiplicado (una vez por archivo) y des-premultiplicar cada frame
(RGB / alfa) antes del paste. El paste de PIL vuelve a multiplicar por el alfa: resultado = RGB_premult + fondo*(1-a),
que es la composición correcta.

ALPHA_MODE: "auto" (detecta) · "premult" (fuerza premultiplicado) · "straight" (fuerza recto, comportamiento antiguo).
"""
import os
import subprocess
import threading

import numpy as np

from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params

ALPHA_MODE = "auto"

_cache = {}
_lock = threading.Lock()


def _sig(path):
    try:  # v1.05: firma cacheada (sin un os.stat al NAS por cada frame)
        from frame_cache import file_sig
        return file_sig(path)
    except ImportError:
        st = os.stat(path)
        return (os.path.abspath(path), st.st_size, int(st.st_mtime))


def _probe_dur(path):
    try:
        from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos
        raw = ffmpeg_parse_infos(path, decode_file=False)
        return float(raw.get("video_duration") or raw.get("duration") or 0.0)
    except Exception:
        return 0.0


def _sample(path, t, max_w=1280):
    """Un frame RGBA (uint8, hasta max_w de ancho, escalado con vecino más cercano para no mezclar píxeles)."""
    cmd = [FFMPEG_BINARY, "-v", "error", "-nostdin", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1", "-an",
           "-vf", f"scale='min({max_w},iw)':-1:flags=neighbor,format=rgba", "-f", "image2pipe", "-vcodec", "pam", "-"]
    params = cross_platform_popen_params({"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL})
    data = subprocess.run(cmd, **params).stdout
    if not data:
        return None
    # cabecera PAM: líneas hasta ENDHDR
    end = data.find(b"ENDHDR\n")
    if end < 0:
        return None
    hdr = dict(l.split(b" ", 1) for l in data[:end].split(b"\n")[1:] if b" " in l)
    w, h, d = int(hdr[b"WIDTH"]), int(hdr[b"HEIGHT"]), int(hdr[b"DEPTH"])
    body = data[end + 7:end + 7 + w * h * d]
    if d != 4 or len(body) < w * h * 4:
        return None
    return np.frombuffer(body, np.uint8).reshape(h, w, 4)


def es_premultiplicado(path):
    """True si el vídeo con alfa está premultiplicado. Se mira en los píxeles semitransparentes:
    premultiplicado => ningún canal de color supera al alfa. En recto, cualquier color claro lo supera."""
    if ALPHA_MODE == "premult":
        return True
    if ALPHA_MODE == "straight":
        return False
    try:
        key = _sig(path)
    except OSError:
        return False
    with _lock:
        if key in _cache:
            return _cache[key]
    dur = _probe_dur(path)
    tiempos = [dur * f for f in (0.25, 0.5, 0.75)] if dur > 0.2 else [0.0]
    n_semi = n_sobre = 0
    for t in tiempos:
        try:
            arr = _sample(path, t)
        except Exception:
            arr = None
        if arr is None:
            continue
        a = arr[..., 3].astype(np.int16)
        semi = (a > 12) & (a < 243)
        if not semi.any():
            continue
        mx = arr[..., :3].max(axis=-1).astype(np.int16)
        n_semi += int(semi.sum())
        n_sobre += int((mx[semi] > a[semi] + 3).sum())
    # Sin bordes semitransparentes no hay rebaba posible: da igual, se deja como recto.
    premult = n_semi >= 100 and n_sobre <= n_semi * 0.005
    with _lock:
        _cache[key] = premult
    try:
        from log_utils import debug
        debug(f"[Alpha] {os.path.basename(path)}: {'PREMULTIPLICADO' if premult else 'recto'} "
              f"({n_sobre}/{n_semi} píxeles de borde con color > alfa)")
    except Exception:
        pass
    return premult


def despremultiplicar(buf, w, h):
    """bytes/bytearray RGBA (premultiplicado) -> bytearray RGBA recto. Solo toca los píxeles semitransparentes."""
    out = bytearray(buf)
    arr = np.frombuffer(out, np.uint8).reshape(h, w, 4)
    a = arr[..., 3]
    idx = np.nonzero((a > 0) & (a < 255))
    if idx[0].size:
        al = a[idx].astype(np.uint32)[:, None]
        rgb = arr[idx[0], idx[1], :3].astype(np.uint32)
        arr[idx[0], idx[1], :3] = np.minimum(255, (rgb * 255 + al // 2) // al).astype(np.uint8)
    return out


def despremultiplicar_img(img):
    """PIL RGBA premultiplicado -> PIL RGBA recto."""
    from PIL import Image
    w, h = img.size
    return Image.frombytes("RGBA", (w, h), bytes(despremultiplicar(img.tobytes(), w, h)))
