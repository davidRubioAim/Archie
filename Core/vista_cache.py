"""v1.10: reescalado rápido de las previews (Architecture Preview / System File Preview).

El strip completo mide ~17000 x 1600 px (RGBA). Reescalarlo con LANCZOS al tamaño del panel costaba ~200 ms en el
hilo principal cada vez que se repintaba la preview (primer frame, ajuste de zoom, escena del reproductor...), y
eso es lo que se notaba como tirones al cargar una campaña o al tocar Manual Edit.

Es lo mismo que ya hacía resize(..., reducing_gap=3.0): un Image.reduce por un factor entero y el LANCZOS final
desde ahí (el resultado es el mismo, píxel a píxel salvo redondeos). La diferencia es que la copia reducida se
guarda aquí, por imagen, y se reutiliza en todos los repintados de esa imagen con ese zoom; además el hilo del
render la precalcula (PIL suelta el GIL mientras reduce), así que el hilo principal casi nunca tiene que hacerla.
También se guardan los últimos lienzos ya montados (get_panned_image repetido con la misma vista = gratis).
"""
import threading
import weakref
from collections import OrderedDict

# Margen que se deja al LANCZOS final: el mismo reducing_gap=3.0 de siempre (mismo aspecto de la preview)
REDUCING_GAP = 3.0
MAX_ENTRADAS = 20   # 8 key frames x (strip + system) + capas estáticas del reproductor (~5-8 MB cada copia)

_lock = threading.Lock()
_cache = OrderedDict()   # (id(img), factor) -> (weakref(img), reducida)


def factor_para(zoom):
    """Factor entero de reducción previa para mostrar a este zoom (1 = sin reducir)."""
    if zoom <= 0:
        return 1
    return max(1, int(1.0 / (zoom * REDUCING_GAP)))


def reducida(img, factor):
    """Copia de img reducida por `factor` (cacheada). La entrada guarda una referencia débil a la imagen original:
    no la mantiene viva (el strip completo son ~110 MB) y, si muere, su id() reutilizado no da una copia ajena."""
    if factor <= 1:
        return img
    clave = (id(img), factor)
    with _lock:
        e = _cache.get(clave)
        if e is not None and e[0]() is img:
            _cache.move_to_end(clave)
            return e[1]
    red = img.reduce(factor)   # fuera del lock: en el hilo del render no bloquea al principal
    try:
        ref = weakref.ref(img)
    except TypeError:
        return red
    with _lock:
        for k in [k for k, v in _cache.items() if v[0]() is None]:   # imágenes que ya no existen
            del _cache[k]
        _cache[clave] = (ref, red)
        _cache.move_to_end(clave)
        while len(_cache) > MAX_ENTRADAS:
            _cache.popitem(last=False)
    return red


def precalentar(img, zoom):
    """Desde el hilo del render: deja lista la copia reducida para el zoom con el que se va a mostrar."""
    try:
        if img is not None and zoom and zoom > 0:
            reducida(img, factor_para(zoom))
    except Exception:
        pass


def zoom_ajuste(img_size, contenedor, margen=0.92):
    """El zoom que pondría auto_fit_zooms para esta imagen en un contenedor (cw, ch). None si no se sabe."""
    if not contenedor:
        return None
    cw, ch = contenedor
    w, h = img_size
    if cw <= 10 or ch <= 10 or w <= 0 or h <= 0:
        return None
    return min(cw / float(w), ch / float(h)) * margen


_lienzos = OrderedDict()   # clave de la vista -> (weakref(img), lienzo)
MAX_LIENZOS = 8


def lienzo(img, clave):
    """Lienzo ya montado para (img, clave) o None."""
    with _lock:
        e = _lienzos.get((id(img),) + clave)
        if e is not None and e[0]() is img:
            return e[1]
    return None


def guardar_lienzo(img, clave, canvas):
    try:
        ref = weakref.ref(img)
    except TypeError:
        return
    k = (id(img),) + clave
    with _lock:
        _lienzos[k] = (ref, canvas)
        _lienzos.move_to_end(k)
        while len(_lienzos) > MAX_LIENZOS:
            _lienzos.popitem(last=False)


def limpiar():
    with _lock:
        _cache.clear()
        _lienzos.clear()
