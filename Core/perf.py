"""
v1.05: tiempos de las operaciones pesadas de Archie, para ver dónde se va el tiempo.

Se activa con ARCHIE_PERF=1 en Config/.env. Cada operación medida escribe una línea en la consola:

    [PERF] Create Architecture (solver): 64 ms · NAS stat 2 (caché 1310)

"NAS stat" son las consultas reales a disco (por SMB si los ELEMENTS están en el NAS); "caché" las que se ahorran.
Sin ARCHIE_PERF no hace nada (coste cero).
"""
import contextlib
import os
import time


def activo():
    return os.getenv("ARCHIE_PERF", "0").strip() == "1"


def _stats():
    try:
        from frame_cache import STATS
        return dict(STATS)
    except Exception:
        return {"stat": 0, "stat_cache": 0}


def log(msg):
    if activo():
        print(f"  [PERF] {msg}")


@contextlib.contextmanager
def medir(nombre):
    if not activo():
        yield
        return
    s0, t0 = _stats(), time.perf_counter()
    try:
        yield
    finally:
        ms = (time.perf_counter() - t0) * 1000.0
        s1 = _stats()
        print(f"  [PERF] {nombre}: {ms:.0f} ms · NAS stat {s1['stat'] - s0['stat']} "
              f"(caché {s1['stat_cache'] - s0['stat_cache']})")
