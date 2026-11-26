"""v1.11: vigilante de bloqueos de la ventana.

Si el hilo principal (la interfaz) pasa más de UMBRAL_S segundos sin atender eventos, se escribe en
logs/archie_bloqueos.log en qué línea de código está cada hilo en ese momento (faulthandler). Windows cierra una
ventana que no responde ("dejó de interactuar con Windows") sin dejar ningún Traceback: con este fichero se ve
exactamente dónde se quedó parada. No cuesta nada mientras la ventana responde.
"""
import faulthandler
import os
import threading
import time
from datetime import datetime

UMBRAL_S = 3.0
LATIDO_MS = 250
MAX_BYTES = 1024 * 1024

_estado = {"ultimo": None, "avisado": False}


def iniciar(app, carpeta_logs):
    """Llamar desde el hilo principal, con la ventana ya creada."""
    try:
        os.makedirs(carpeta_logs, exist_ok=True)
    except OSError:
        return
    ruta = os.path.join(carpeta_logs, "archie_bloqueos.log")

    def latido():
        _estado["ultimo"] = time.monotonic()
        try:
            app.after(LATIDO_MS, latido)
        except Exception:
            pass   # ventana cerrada

    def vigilar():
        while True:
            time.sleep(1.0)
            ultimo = _estado["ultimo"]
            if ultimo is None:
                continue
            parado = time.monotonic() - ultimo
            if parado < UMBRAL_S:
                _estado["avisado"] = False
                continue
            if _estado["avisado"]:
                continue
            _estado["avisado"] = True
            _volcar(ruta, parado)

    app.after(LATIDO_MS, latido)
    threading.Thread(target=vigilar, daemon=True, name="vigilante").start()


def _volcar(ruta, parado):
    try:
        if os.path.exists(ruta) and os.path.getsize(ruta) > MAX_BYTES:
            os.replace(ruta, ruta + ".old")
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S} · la ventana lleva {parado:.1f} s sin responder =====\n")
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.flush()
        print(f"  [WARN] La ventana lleva {parado:.1f} s sin responder: detalle en {ruta}")
    except Exception:
        pass
