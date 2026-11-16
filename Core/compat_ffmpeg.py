"""
v0.78: compatibilidad de MoviePy 2.x con ffmpeg 8.

ffmpeg 8 escribe en `ffmpeg -i` una línea nueva para los .mov con pista de timecode (tmcd):

    Stream group #0:0[0x2]: Track Reference:

El parser de MoviePy (FFmpegInfosParser) la toma por la línea "Stream #..." y falla con
"Error passing ffmpeg -i command output" (AttributeError: 'NoneType' object has no attribute 'groups').
Eso rompe VideoFileClip, ffmpeg_parse_infos, la caché de frames y el render en cualquier equipo con ffmpeg 8.

Este módulo quita esas líneas antes de que MoviePy las lea. Basta con importarlo una vez (se hace en app.py,
render_rapido.py y frame_cache.py); importarlo más veces no hace nada.
"""
import re

_STREAM_GROUP = re.compile(r"^\s*Stream group #.*$\n?", re.MULTILINE)


def limpiar_infos(txt):
    """Texto de `ffmpeg -i` sin las líneas 'Stream group' de ffmpeg 8."""
    return _STREAM_GROUP.sub("", txt) if txt and "Stream group" in txt else txt


def _parchear():
    try:
        from moviepy.video.io import ffmpeg_reader
    except Exception:
        return
    cls = getattr(ffmpeg_reader, "FFmpegInfosParser", None)
    if cls is None or getattr(cls, "_archie_stream_group_fix", False):
        return
    original = cls.__init__

    def __init__(self, infos, *args, **kwargs):
        original(self, limpiar_infos(infos), *args, **kwargs)

    cls.__init__ = __init__
    cls._archie_stream_group_fix = True


_parchear()
