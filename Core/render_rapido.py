"""
Motor de render rápido de ARCHIE para el System File.

Sustituye la composición de MoviePy por ffmpeg directo, con la misma idea que el reproductor de la preview:
- Cada vídeo único se decodifica UNA sola vez con un proceso ffmpeg propio, ya escalado en C a todos los anchos
  en los que aparece (split + scale + hstack). MoviePy redimensionaba con LANCZOS en Python cada hueco en cada frame.
- Lo estático (fondo imagen, center boards, logos congelados) se prepara una vez; lo que queda por debajo de todo
  lo animado se funde en una capa base.
- Cada frame se compone con PIL (paste con alfa, en C) y se envía en crudo a un ffmpeg codificador.
- Decodificadores, composición y codificador trabajan en paralelo (procesos ffmpeg + hilos con colas).
- Codificador: GPU si el equipo la tiene (NVIDIA / Intel Quick Sync / AMD) y si no libx264 'veryfast'.

Ajustes: RENDER_FPS, ENCODER, X264_PRESET.
"""
import json
import math
import os
import queue
import re
import subprocess
import threading

import numpy as np
from PIL import Image

from moviepy.config import FFMPEG_BINARY
from moviepy.tools import cross_platform_popen_params
from moviepy.video.io.ffmpeg_reader import ffmpeg_parse_infos

from log_utils import debug, log_error

RENDER_FPS = 25
ENCODER = "auto"        # "auto": GPU si hay y funciona, si no CPU · "cpu": siempre libx264 · o un codec concreto ("h264_nvenc"...)
X264_PRESET = "veryfast"

SYSTEM_BITRATE = "15000k"
SYSTEM_BUFSIZE = "30000k"

VIDEO_EXTS = (".mov", ".mp4", ".m4v", ".avi")
_ALPHA_PIX = re.compile(r"Video:.*?\b(yuva\w*|rgba\w*|argb\w*|bgra\w*|abgr\w*|gbrap\w*|ya8|ya16\w*|pal8)\b")


class RenderCancelled(Exception):
    def __init__(self):
        super().__init__("RENDER_CANCELLED")


def es_video(path):
    return bool(path) and os.path.splitext(path)[1].lower() in VIDEO_EXTS


def _popen(cmd, **kw):
    params = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.DEVNULL}
    params.update(kw)
    return subprocess.Popen(cmd, **cross_platform_popen_params(params))


# ---------------------------------------------------------------- Metadatos
_info_cache = {}

def _info(path):
    """(duración, (w, h), tiene_alfa) de un vídeo."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_size, int(st.st_mtime))
    if key not in _info_cache:
        raw = ffmpeg_parse_infos(path, decode_file=False)
        dur = float(raw.get("video_duration") or raw.get("duration") or 0.0)
        size = tuple(raw.get("video_size") or (0, 0))
        proc = _popen([FFMPEG_BINARY, "-hide_banner", "-i", path])
        _, err = proc.communicate()
        alpha = bool(_ALPHA_PIX.search(err.decode("utf8", "ignore")))
        _info_cache[key] = (dur, size, alpha)
    return _info_cache[key]


def _frame(path, t, w, h, alpha):
    """Un frame suelto de un vídeo, escalado a (w, h), como PIL RGBA o RGB."""
    pix, mode, bpp = ("rgba", "RGBA", 4) if alpha else ("rgb24", "RGB", 3)
    cmd = [FFMPEG_BINARY, "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", path, "-frames:v", "1",
           "-vf", f"scale={w}:{h}:flags=lanczos", "-f", "rawvideo", "-pix_fmt", pix, "-"]
    out, err = _popen(cmd).communicate()
    if len(out) < w * h * bpp:
        raise RuntimeError(f"ffmpeg no devolvió frame de {os.path.basename(path)} @ {t:.2f}s: {err.decode('utf8', 'ignore')[-200:]}")
    return Image.frombytes(mode, (w, h), out[:w * h * bpp])


# ---------------------------------------------------------------- Decodificador (1 proceso ffmpeg por vídeo)
class _Decoder:
    """Decodifica un vídeo una vez, en bucle hasta n_frames, escalado a cada ancho de `widths` (misma altura)."""

    def __init__(self, path, widths, h, n_frames, alpha, fps):
        self.path, self.widths, self.h, self.n, self.alpha = path, list(widths), h, n_frames, alpha
        self.mode, pix, self.bpp = ("RGBA", "rgba", 4) if alpha else ("RGB", "rgb24", 3)
        self.total_w = sum(self.widths)
        self.frame_size = self.total_w * h * self.bpp
        self.offsets = np.cumsum([0] + self.widths[:-1]).tolist()

        dur = _info(path)[0]
        loop = ["-stream_loop", "-1"] if dur and dur < n_frames / fps else []
        k = len(self.widths)
        if k == 1:
            graph = f"[0:v]fps={fps},scale={self.widths[0]}:{h}:flags=lanczos,format={pix}[out]"
        else:
            parts = [f"[0:v]fps={fps},split={k}" + "".join(f"[s{i}]" for i in range(k))]
            parts += [f"[s{i}]scale={w}:{h}:flags=lanczos[r{i}]" for i, w in enumerate(self.widths)]
            parts.append("".join(f"[r{i}]" for i in range(k)) + f"hstack=inputs={k},format={pix}[out]")
            graph = ";".join(parts)
        cmd = [FFMPEG_BINARY, "-v", "error", *loop, "-i", path, "-an", "-sn", "-filter_complex", graph,
               "-map", "[out]", "-frames:v", str(n_frames), "-f", "rawvideo", "-pix_fmt", pix, "-"]
        self.proc = _popen(cmd, stderr=subprocess.DEVNULL, bufsize=self.frame_size * 2)
        self.q = queue.Queue(maxsize=6)
        self.last = None
        self.failed = None
        threading.Thread(target=self._run, daemon=True, name=f"dec:{os.path.basename(path)}").start()

    def _run(self):
        got = 0
        try:
            while got < self.n:
                buf = self.proc.stdout.read(self.frame_size)
                if not buf or len(buf) < self.frame_size:
                    break
                self.q.put(buf)
                got += 1
        except Exception as e:
            self.failed = e
        finally:
            self.q.put(None)

    def next_frame(self):
        """Lista de PIL.Image, una por ancho. Si el vídeo se queda corto repite el último frame."""
        if self.last is not None and self.last[0] is None:
            return self.last[1]
        buf = self.q.get()
        if buf is None:
            if self.last is None:
                raise RuntimeError(f"ffmpeg no pudo decodificar {os.path.basename(self.path)}")
            self.last = (None, self.last[1])
            return self.last[1]
        img = Image.frombuffer(self.mode, (self.total_w, self.h), buf, "raw", self.mode, 0, 1)
        if len(self.widths) == 1:
            imgs = [img]
        else:
            imgs = [img.crop((x, 0, x + w, self.h)) for x, w in zip(self.offsets, self.widths)]
        self.last = (buf, imgs)
        return imgs

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:
            pass
        while True:  # libera el hilo lector si estaba esperando hueco en la cola
            try:
                self.q.get_nowait()
            except queue.Empty:
                break


# ---------------------------------------------------------------- Escena
class _Escena:
    """Capas en el mismo orden que construir_mega_strip (fondo, center boards + anims, logos)."""

    def __init__(self, datos, fps):
        from renderizador import aplicar_letterbox_pil  # import tardío, evita el ciclo con renderizador.py
        self.datos = datos
        self.fps = fps
        self.w = datos["dimensiones_globales"]["width"]
        self.h = datos["dimensiones_globales"]["height"]
        archivos = datos["archivos_fuente"]
        h = self.h

        anims = [f for f in archivos.get("anim_zones", []) if f is not None]
        if not anims and archivos.get("anim_zone"):
            anims = [archivos["anim_zone"]]
        dur = 1.0
        for p in anims:
            if es_video(p):
                try:
                    dur = max(dur, _info(p)[0])
                except Exception as e:
                    log_error(f"Error obteniendo duración de {p}: {e}")
        self.duracion = dur
        self.n_frames = int(dur * fps)

        self.layers = []          # (tipo, payload, x) ; tipo 'static' -> (img, mask) ; 'video' -> (key, slot)
        self._uses = {}           # key -> {"path", "alpha", "widths": [..]}

        self._fondo(archivos.get("background"))

        for zona in datos["zonas"].values():
            for e in zona["elementos"]:
                x, w = e["x_absoluta"], max(1, e["width"])
                if e["tipo"] == "center_board":
                    self._center_board(archivos.get("center_board"), datos.get("config", {}).get("show_cb", True), x, w,
                                       aplicar_letterbox_pil)
                elif e["tipo"] == "anim_zone" and anims:
                    self._fuente(anims[e.get("anim_idx", 0) % len(anims)], x, w, frozen=False)
        logo = archivos.get("static_logo")
        if logo:
            for zona in datos["zonas"].values():
                for e in zona["elementos"]:
                    if e["tipo"] == "static_logo":
                        self._fuente(logo, e["x_absoluta"], max(1, e["width"]), frozen=e.get("logo_under_type") == "frozen")

        # Capa base: todo lo estático que queda por debajo de la primera capa animada
        self.base = Image.new("RGB", (self.w, h), (0, 0, 0))
        i = 0
        while i < len(self.layers) and self.layers[i][0] == "static":
            self._paste(self.base, self.layers[i])
            i += 1
        self.layers = self.layers[i:]

        self.decoders = {}
        for key, u in self._uses.items():
            self.decoders[key] = _Decoder(u["path"], u["widths"], h, self.n_frames, u["alpha"], fps)
        n_vid = sum(1 for l in self.layers if l[0] == "video")
        debug(f"[Render rápido] {self.w}x{h}, {self.n_frames} frames, {len(self.decoders)} decodificadores, "
              f"{n_vid} instancias de vídeo, {len(self.layers) - n_vid} capas estáticas por frame")

    # ----- construcción
    def _video_slot(self, key, path, alpha, w):
        u = self._uses.setdefault(key, {"path": path, "alpha": alpha, "widths": []})
        if w not in u["widths"]:
            u["widths"].append(w)
        return u["widths"].index(w)

    def _fondo(self, bg):
        h = self.h
        if not bg:
            return
        try:
            if es_video(bg):
                dur, (sw, sh), _ = _info(bg)
                bw = max(1, int(h * (sw / max(1, sh))))
                f0 = _frame(bg, 0, bw, h, False)
                moving = False
                if dur > 0.1:
                    fm = _frame(bg, dur / 2.0, bw, h, False)
                    fe = _frame(bg, max(0.0, dur - 0.1), bw, h, False)
                    a0 = np.asarray(f0, dtype=np.float32)
                    moving = (np.mean(np.abs(a0 - np.asarray(fm, dtype=np.float32))) > 1.0 or
                              np.mean(np.abs(a0 - np.asarray(fe, dtype=np.float32))) > 1.0)
                if moving:
                    debug(f"[Render rápido] Fondo animado ({os.path.basename(bg)}), tile {bw}px")
                    slot = self._video_slot(("bg", bg), bg, False, bw)
                    self.layers.append(("bg", (("bg", bg), slot, bw), 0))
                    return
                debug(f"[Render rápido] Fondo sin movimiento ({os.path.basename(bg)}), congelado")
                pil_bg = f0
            else:
                pil_bg = Image.open(bg).convert("RGB")
                bw = max(1, int(h * (pil_bg.width / max(1, pil_bg.height))))
                pil_bg = pil_bg.resize((bw, h), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (self.w, h))
            for x in range(0, self.w, pil_bg.width):
                canvas.paste(pil_bg, (x, 0))
            self.layers.append(("static", (canvas, None), 0))
        except Exception as e:
            log_error(f"[Render rápido] Error generando fondo, aplicando negro: {e}")

    def _center_board(self, cb, mostrar, x, w, letterbox):
        h = self.h
        img = None
        if cb and mostrar:
            try:
                if es_video(cb):
                    _, (sw, sh), alpha = _info(cb)
                    src = _frame(cb, 0, sw, sh, alpha).convert("RGBA")
                else:
                    src = Image.open(cb).convert("RGBA")
                img = letterbox(src, w, h, Image.Resampling.LANCZOS).convert("RGB")
            except Exception as e:
                log_error(f"[Render rápido] Error en Center Board, aplicando negro: {e}")
        if img is None:
            img = Image.new("RGB", (w, h), (0, 0, 0))
        self.layers.append(("static", (img, None), x))

    def _fuente(self, path, x, w, frozen):
        h = self.h
        try:
            if es_video(path):
                _, (sw, sh), alpha = _info(path)
                if frozen:
                    img = _frame(path, 0, w, h, alpha)
                    self.layers.append(("static", (img, img if alpha else None), x))
                else:
                    slot = self._video_slot(("v", path, alpha), path, alpha, w)
                    self.layers.append(("video", (("v", path, alpha), slot, alpha), x))
            else:
                img = Image.open(path)
                img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").resize((w, h), Image.Resampling.LANCZOS)
                self.layers.append(("static", (img, img if img.mode == "RGBA" else None), x))
        except Exception as e:
            log_error(f"[Render rápido] Error cargando {path}: {e}")

    # ----- composición
    @staticmethod
    def _paste(dst, layer):
        _, (img, mask), x = layer
        dst.paste(img, (x, 0), mask)

    def frames(self):
        """Generador de frames del Mega Strip como PIL RGB (se reutiliza el mismo objeto)."""
        strip = self.base.copy()
        for _ in range(self.n_frames):
            imgs = {k: d.next_frame() for k, d in self.decoders.items()}
            strip.paste(self.base)
            for tipo, payload, x in self.layers:
                if tipo == "static":
                    strip.paste(payload[0], (x, 0), payload[1])
                elif tipo == "video":
                    key, slot, alpha = payload
                    im = imgs[key][slot]
                    strip.paste(im, (x, 0), im if alpha else None)
                else:  # fondo animado en mosaico
                    key, slot, bw = payload
                    im = imgs[key][slot]
                    for bx in range(0, self.w, bw):
                        strip.paste(im, (bx, 0))
            yield strip

    def close(self):
        for d in self.decoders.values():
            d.close()


# ---------------------------------------------------------------- Codificador
_hw_cache = {}

def _probar_encoder(codec, args):
    cmd = [FFMPEG_BINARY, "-v", "error", "-f", "lavfi", "-i", "color=black:s=3840x2160:r=25:d=0.2",
           "-c:v", codec, *args, "-f", "null", "-"]
    try:
        proc = _popen(cmd)
        proc.communicate(timeout=20)
        return proc.returncode == 0
    except Exception:
        return False


def _hw_args(codec):
    rate = ["-b:v", SYSTEM_BITRATE, "-minrate", SYSTEM_BITRATE, "-maxrate", SYSTEM_BITRATE, "-bufsize", SYSTEM_BUFSIZE]
    if codec == "h264_nvenc":
        return ["-preset", "p4", "-rc", "cbr", *rate, "-pix_fmt", "yuv420p"]
    if codec == "h264_qsv":
        return ["-preset", "veryfast", *rate, "-pix_fmt", "nv12"]
    if codec == "h264_amf":
        return ["-quality", "speed", "-rc", "cbr", *rate, "-pix_fmt", "yuv420p"]
    return [*rate, "-pix_fmt", "yuv420p"]


def elegir_encoder():
    """Devuelve el codec H.264 a usar ('libx264' o uno por GPU). Se prueba una vez por sesión."""
    if ENCODER == "cpu":
        return "libx264"
    if ENCODER != "auto":
        return ENCODER
    if "auto" not in _hw_cache:
        elegido = "libx264"
        for codec in ("h264_nvenc", "h264_qsv", "h264_amf"):
            if _probar_encoder(codec, _hw_args(codec)):
                elegido = codec
                break
        _hw_cache["auto"] = elegido
        debug(f"[Render rápido] Codificador elegido: {elegido}")
    return _hw_cache["auto"]


def _cmd_encoder(codec, w, h, fps, out):
    head = [FFMPEG_BINARY, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "-", "-an"]
    if codec == "libx264":
        enc = ["-c:v", "libx264", "-preset", X264_PRESET, "-b:v", SYSTEM_BITRATE, "-minrate", SYSTEM_BITRATE,
               "-maxrate", SYSTEM_BITRATE, "-bufsize", SYSTEM_BUFSIZE, "-pix_fmt", "yuv420p"]
    else:
        enc = ["-c:v", codec, *_hw_args(codec)]
    return head + enc + [out]


class _Encoder:
    def __init__(self, cmd):
        debug(f"[Render rápido] Encoder: {' '.join(cmd[cmd.index('-c:v'):-1])}")
        self.proc = _popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
        self.q = queue.Queue(maxsize=4)
        self.error = None
        self._err = []
        threading.Thread(target=lambda: self._err.append(self.proc.stderr.read()), daemon=True).start()
        self.t = threading.Thread(target=self._run, daemon=True, name="encoder")
        self.t.start()

    def _run(self):
        while True:
            buf = self.q.get()
            if buf is None:
                break
            if self.error:
                continue  # ffmpeg ya cayó: se vacía la cola para que write() no se quede bloqueado
            try:
                self.proc.stdin.write(buf)
            except Exception as e:
                self.error = e

    def write(self, buf):
        if self.error:
            raise RuntimeError(f"ffmpeg (encoder) se cerró: {self.stderr()}")
        self.q.put(buf)

    def stderr(self):
        return (self._err[0] if self._err else b"").decode("utf8", "ignore")[-400:]

    def finish(self):
        self.q.put(None)
        self.t.join()
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait()
        if self.error or self.proc.returncode != 0:
            raise RuntimeError(f"ffmpeg (encoder) falló: {self.stderr()}")

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------- Render
def _render(escena, out, w, h, make_frame, progress_callback, cancel_check):
    codecs = [elegir_encoder()]
    if codecs[0] != "libx264":
        codecs.append("libx264")
    n = escena.n_frames
    frames = escena.frames()
    for intento, codec in enumerate(codecs):
        enc = _Encoder(_cmd_encoder(codec, w, h, RENDER_FPS, out))
        last_pct = -1.0
        try:
            for i in range(n):
                if cancel_check and cancel_check():
                    raise RenderCancelled()
                enc.write(make_frame(next(frames)))
                pct = (i + 1) / n
                if progress_callback and (pct - last_pct >= 0.01 or i + 1 == n):
                    last_pct = pct
                    progress_callback(pct)
            enc.finish()
            return codec
        except RenderCancelled:
            enc.kill()
            raise
        except Exception as e:
            enc.kill()
            # Si la GPU falla se repite el render entero con libx264 (los decodificadores se reinician)
            if intento + 1 < len(codecs):
                log_error(f"[Render rápido] {codec} falló ({e}); repitiendo con libx264")
                _hw_cache["auto"] = "libx264"
                escena.close()
                escena.__init__(escena.datos, RENDER_FPS)
                frames = escena.frames()
                continue
            raise


def _cargar(json_path):
    with open(json_path, "r", encoding="utf-8") as f:
        datos = json.load(f)
    return datos, _Escena(datos, RENDER_FPS)


def renderizar_system_file(json_path, output_filename, progress_callback=None, cancel_check=None, row_target=None):
    datos, escena = _cargar(json_path)
    try:
        w_total, h_total = escena.w, escena.h
        sys_data = datos.get("system_setup", {})
        sys_w = sys_data.get("canvas_width", 3840)
        sys_h = sys_data.get("canvas_height", 2160)
        sys_mask = sys_data.get("canvas_mask", 96)
        sys_off_x = sys_data.get("offset_x", 0)
        sys_off_y = sys_data.get("offset_y", 0)

        if datos.get("config", {}).get("amplify", False) and row_target in ["1ST_ROW", "2ND_ROW"]:
            h_slice = h_total // 2
            src_y_start, src_y_end = (h_slice, h_total) if row_target == "1ST_ROW" else (0, h_slice)
        else:
            h_slice = h_total
            src_y_start, src_y_end = 0, h_total

        # Recortes precalculados (misma geometría que el motor MoviePy)
        advance = sys_w - sys_mask
        total_rows = math.ceil((w_total + sys_off_x) / max(1, advance))
        cortes = []
        for i in range(total_rows):
            x_m_strip = i * advance - sys_off_x
            crop_x1, crop_x2 = max(0, x_m_strip), min(w_total, x_m_strip + advance)
            if crop_x1 >= crop_x2:
                continue
            x_canvas = abs(x_m_strip) if x_m_strip < 0 else 0
            y1 = sys_off_y + i * h_slice
            y2 = y1 + h_slice
            s1, s2 = src_y_start, src_y_end
            if y1 < 0:
                s1, y1 = src_y_start - y1, 0
            if y2 > sys_h:
                s2, y2 = src_y_end - (y2 - sys_h), sys_h
            if y1 >= y2:
                continue
            cortes.append((y1, y2, x_canvas, x_canvas + crop_x2 - crop_x1, s1, s2, crop_x1, crop_x2))

        # Buffers reutilizables: las zonas fuera de los recortes quedan siempre en negro. Hay más buffers que
        # huecos en la cola del encoder (4) + el que se escribe + el que se rellena, así nunca se pisa uno en uso.
        buffers = [np.zeros((sys_h, sys_w, 3), dtype=np.uint8) for _ in range(7)]
        contador = [0]

        def make_frame(strip):
            src = np.frombuffer(strip.tobytes(), dtype=np.uint8).reshape(h_total, w_total, 3)
            sys_frame = buffers[contador[0] % len(buffers)]
            contador[0] += 1
            for y1, y2, cx1, cx2, s1, s2, x1, x2 in cortes:
                sys_frame[y1:y2, cx1:cx2] = src[s1:s2, x1:x2]
            return sys_frame.data

        debug(f"[Render rápido] SYSTEM {sys_w}x{sys_h} ({row_target or 'FULL'}), {len(cortes)} filas")
        codec = _render(escena, output_filename, sys_w, sys_h, make_frame, progress_callback, cancel_check)
        debug(f"[Render rápido] SYSTEM finalizado con {codec}")
    finally:
        escena.close()

