"""
v1.11: tiempos de cada "Loading new architecture", siempre activos, en logs/archie_tiempos.log.

Una línea por arquitectura: cuánto tardó hasta el primer frame y en qué se fue (cálculo, lectura de vídeos,
composición) y cuál fue el ELEMENT más lento de leer. Sirve para saber, con datos del equipo real, si lo que
frena es el NAS, un vídeo pesado o el cálculo.
"""
import os
import threading
import time

MAX_BYTES = 512 * 1024

_ruta = None
_lock = threading.Lock()
_ciclo = None
_lecturas = {}   # nombre de fichero -> ms más alto leyendo un frame en este ciclo


def iniciar(carpeta_logs):
    global _ruta
    try:
        os.makedirs(carpeta_logs, exist_ok=True)
        _ruta = os.path.join(carpeta_logs, "archie_tiempos.log")
    except OSError:
        _ruta = None


def empezar():
    """Al aparecer el aviso de carga (si ya estaba, sigue el mismo ciclo)."""
    global _ciclo
    with _lock:
        if _ciclo is None:
            _ciclo = {"t0": time.perf_counter(), "marcas": []}
            _lecturas.clear()


def marca(nombre):
    with _lock:
        if _ciclo is not None:
            _ciclo["marcas"].append((nombre, time.perf_counter()))


def lectura(path, ms):
    """FrameCache: tiempo de leer un frame de un vídeo."""
    nombre = os.path.basename(path or "")
    with _lock:
        if _ciclo is not None and ms > _lecturas.get(nombre, 0):
            _lecturas[nombre] = ms


def terminar(detalle=""):
    """Primer frame en pantalla (o cancelado): escribe la línea y cierra el ciclo."""
    global _ciclo
    with _lock:
        c, _ciclo = _ciclo, None
        lect = dict(_lecturas)
        _lecturas.clear()
    if c is None or _ruta is None:
        return
    fin = time.perf_counter()
    partes, prev = [], c["t0"]
    for nombre, t in c["marcas"]:
        partes.append(f"{nombre} {(t - prev) * 1000:.0f}")
        prev = t
    partes.append(f"mostrar {(fin - prev) * 1000:.0f}")
    lentos = sorted(lect.items(), key=lambda kv: -kv[1])[:3]
    txt = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} · {detalle + ' · ' if detalle else ''}"
           f"total {(fin - c['t0']) * 1000:.0f} ms · " + " · ".join(partes) + " ms")
    if lentos:
        txt += " · frame más lento: " + ", ".join(f"{n} {ms:.0f} ms" for n, ms in lentos)
    try:
        if os.path.exists(_ruta) and os.path.getsize(_ruta) > MAX_BYTES:
            os.replace(_ruta, _ruta + ".1")
        with open(_ruta, "a", encoding="utf-8") as f:
            f.write(txt + "\n")
    except OSError:
        pass
