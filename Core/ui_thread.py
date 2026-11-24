"""v1.04: `after()` seguro desde hilos secundarios.

Tkinter solo admite llamadas a Tcl desde el hilo principal. Un hilo que llama a `app.after(...)` antes de que arranque
mainloop (o en Python 3.14 / Tcl 9, en cualquier momento) lanza `RuntimeError: main thread is not in main loop`.

Con este mixin, `after()` llamado desde otro hilo NO toca Tcl: deja la petición en una cola y el hilo principal la
recoge con un sondeo periódico (`after` propio, siempre en el hilo principal). Todo el código que ya hacía
`self.app.after(0, fn)` desde un hilo queda arreglado sin tocarlo.
"""
import queue
import threading

POLL_MS = 30


class UiThreadMixin:
    _ui_cola = None
    _ui_sondeo_activo = False

    def ui_iniciar_cola(self):
        """Llamar en el hilo principal justo después de crear la ventana."""
        if self._ui_cola is None:
            self._ui_cola = queue.SimpleQueue()
        if not self._ui_sondeo_activo:
            self._ui_sondeo_activo = True
            super().after(POLL_MS, self._ui_sondear)

    def after(self, ms, func=None, *args):
        if threading.current_thread() is threading.main_thread():
            return super().after(ms, func, *args)
        if func is None:  # after(ms) sin función = sleep: no tiene sentido fuera del hilo principal
            return None
        if self._ui_cola is None:  # no debería pasar (se crea en __init__), pero sin carreras de creación
            with _LOCK:
                if self._ui_cola is None:
                    self._ui_cola = queue.SimpleQueue()
        self._ui_cola.put((ms, func, args))
        return None  # desde un hilo no hay id de after que cancelar

    def ui_llamar(self, func, *args):
        """Ejecuta func(*args) en el hilo principal, se llame desde donde se llame."""
        return self.after(0, func, *args)

    def _ui_sondear(self):
        cola = self._ui_cola
        try:
            while True:
                ms, func, args = cola.get_nowait()
                if ms and ms > 0:
                    super().after(ms, func, *args)
                    continue
                try:
                    func(*args)
                except Exception:
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            super().after(POLL_MS, self._ui_sondear)
        except Exception:  # ventana destruida: se acaba el sondeo
            self._ui_sondeo_activo = False


_LOCK = threading.Lock()
