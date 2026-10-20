import json
import math
import numpy as np
import os
import traceback
from PIL import Image
from moviepy import VideoFileClip, ImageClip, ColorClip, CompositeVideoClip, VideoClip, concatenate_videoclips
from proglog import ProgressBarLogger

class UILogger(ProgressBarLogger):
    def __init__(self, ui_callback, cancel_check=None):
        super().__init__()
        self.ui_callback = ui_callback
        self.cancel_check = cancel_check
        self.last_percent = -1
        self.active_bar = None

    def callback(self, **kwargs):
        if self.cancel_check and self.cancel_check(): raise Exception("RENDER_CANCELLED")
        if "message" in kwargs: print(f"[Motor Background] {kwargs['message']}")

    def bars_callback(self, bar, attr, value, old_value):
        if self.cancel_check and self.cancel_check(): raise Exception("RENDER_CANCELLED")
        total = self.bars[bar].get('total', 0)
        if total > 0:
            percent = value / total
            if self.active_bar != bar or percent < self.last_percent:
                self.active_bar = bar
                self.last_percent = -1
            if percent - self.last_percent >= 0.01 or percent >= 1.0:
                self.last_percent = percent
                if self.ui_callback: self.ui_callback(percent)

def es_video(archivo):
    if not archivo: return False
    ext = os.path.splitext(archivo)[1].lower()
    return ext in ['.mov', '.mp4', '.m4v', '.avi']

def aplicar_letterbox_pil(img, target_w, target_h, resample_method):
    if target_w <= 0 or target_h <= 0:
        return Image.new("RGBA", (max(1, target_w), max(1, target_h)), (0, 0, 0, 255))
    ratio = min(target_w / float(img.width), target_h / float(img.height))
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))
    resized = img.resize((new_w, new_h), resample_method)
    canvas = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 255))
    paste_x = (target_w - new_w) // 2
    paste_y = (target_h - new_h) // 2
    canvas.paste(resized, (paste_x, paste_y), resized if resized.mode == "RGBA" else None)
    return canvas

def detectar_movimiento_video(clip, threshold=1.0):
    """
    Muestrea 3 frames del vídeo (inicio, mitad y fin). Si la diferencia 
    de píxeles es prácticamente cero, determina que es un vídeo estático.
    """
    try:
        if clip.duration <= 0.1: 
            return False
        
        f0 = clip.get_frame(0)
        f_mid = clip.get_frame(clip.duration / 2.0)
        f_end = clip.get_frame(max(0, clip.duration - 0.1))
        
        # Calculamos la diferencia media absoluta (MAD) entre frames
        diff1 = np.mean(np.abs(f0.astype(float) - f_mid.astype(float)))
        diff2 = np.mean(np.abs(f0.astype(float) - f_end.astype(float)))
        
        return (diff1 > threshold) or (diff2 > threshold)
    except Exception as e:
        print(f"[ARCHIE DEBUG] Fallo en detección de movimiento, asumiendo estático: {e}")
        return False

def obtener_clip_fondo_inteligente(bg_path, w_total, h_total, duracion_objetivo):
    """
    Evalúa el fondo y devuelve un ImageClip (si es estático) o un VideoClip 
    animado optimizado con repetición horizontal (np.tile) si tiene movimiento.
    """
    if not bg_path:
        return ColorClip(size=(w_total, h_total), color=(0,0,0), duration=duracion_objetivo)

    try:
        if es_video(bg_path):
            clip_orig = VideoFileClip(bg_path).resized(height=h_total)
            tiene_movimiento = detectar_movimiento_video(clip_orig)
            
            if not tiene_movimiento:
                print(f"[ARCHIE DEBUG] Fondo optimizado: Vídeo SIN movimiento detectado ({os.path.basename(bg_path)}). Congelando frame 0.")
                pil_bg = Image.fromarray(clip_orig.get_frame(0)).convert("RGB")
                clip_orig.close()
            else:
                print(f"[ARCHIE DEBUG] Fondo animado DETECTADO ({os.path.basename(bg_path)}). Renderizando stream dinámico.")
                new_bg_w = max(1, int(h_total * (clip_orig.w / max(1, clip_orig.h))))
                clip_orig = clip_orig.resized(width=new_bg_w, height=h_total)
                repeats = int(math.ceil(w_total / float(new_bg_w)))
                dur_orig = clip_orig.duration

                # Generador dinámico de frames con repetición ultra-rápida (np.tile)
                def make_tiled_bg(t):
                    fr = clip_orig.get_frame(t % dur_orig)
                    tiled = np.tile(fr, (1, repeats, 1))
                    return tiled[:, :w_total, :] 

                return VideoClip(make_tiled_bg, duration=duracion_objetivo)
        else:
            pil_bg = Image.open(bg_path).convert("RGB")
            
        # --- RUTA ESTÁTICA (Imágenes o Vídeos sin movimiento) ---
        aspect_ratio = pil_bg.width / max(1, pil_bg.height)
        new_bg_w = max(1, int(h_total * aspect_ratio))
        pil_bg = pil_bg.resize((new_bg_w, h_total), Image.Resampling.LANCZOS)
        
        canvas = Image.new("RGB", (w_total, h_total))
        for x in range(0, w_total, new_bg_w): 
            canvas.paste(pil_bg, (x, 0))
            
        return ImageClip(np.array(canvas)).with_duration(duracion_objetivo)

    except Exception as e:
        traceback.print_exc()
        print(f"[ARCHIE DEBUG] Error generando fondo, aplicando negro: {e}")
        return ColorClip(size=(w_total, h_total), color=(0,0,0), duration=duracion_objetivo)

def obtener_clip_fuente(ruta, h_total, duracion=None):
    if not ruta: return None
    print(f"[ARCHIE DEBUG] Cargando clip para render: {ruta}")
    try:
        if es_video(ruta):
            clip = VideoFileClip(ruta, has_mask=True).resized(height=h_total)
            if duracion:
                if clip.duration < duracion:
                    repeticiones = int(math.ceil(duracion / clip.duration))
                    clip = concatenate_videoclips([clip] * repeticiones).with_duration(duracion)
                else:
                    clip = clip.with_duration(duracion)
            return clip
        else:
            clip = ImageClip(ruta).resized(height=h_total)
            if duracion: clip = clip.with_duration(duracion)
            return clip
    except Exception as e:
        traceback.print_exc()
        print(f"[ARCHIE DEBUG] Error al cargar clip {ruta}: {e}")
        return None

def construir_mega_strip(datos):
    print("[ARCHIE DEBUG] Iniciando construcción visual de Mega Strip...")
    w_total = datos["dimensiones_globales"]["width"]
    h_total = datos["dimensiones_globales"]["height"]
    archivos = datos["archivos_fuente"]

    bg_path = archivos.get("background")
    logo_path = archivos.get("static_logo")
    
    valid_anims = [f for f in archivos.get("anim_zones", []) if f is not None]
    if not valid_anims and archivos.get("anim_zone"): valid_anims = [archivos["anim_zone"]]

    duracion_video = 1.0
    anim_clips_base = []
    
    for path in valid_anims:
        if es_video(path):
            try:
                temp_clip = VideoFileClip(path)
                if temp_clip.duration > duracion_video: duracion_video = temp_clip.duration
                temp_clip.close()
            except Exception as e:
                print(f"[ARCHIE DEBUG] Error obteniendo duración de {path}: {e}")
            
    for path in valid_anims:
        anim_clips_base.append(obtener_clip_fuente(path, h_total, duracion_video))

    # 1. Obtención del fondo mediante motor inteligente (animado o estático según detección)
    fondo_base = obtener_clip_fondo_inteligente(bg_path, w_total, h_total, duracion_video)
    lista_composicion = [fondo_base]

    clip_logo_base = obtener_clip_fuente(logo_path, h_total, duracion_video)

    for nombre_zona, datos_zona in datos["zonas"].items():
        for elemento in datos_zona["elementos"]:
            tipo = elemento["tipo"]
            pos_x = elemento["x_absoluta"]
            w_elem = max(1, elemento["width"])

            if tipo == "center_board" and w_elem > 0:
                cb_path = archivos.get("center_board")
                mostrar_cb = datos.get("config", {}).get("show_cb", True)
                if cb_path and mostrar_cb:
                    try:
                        if es_video(cb_path):
                            with VideoFileClip(cb_path) as cl:
                                frame_0 = cl.get_frame(0)
                                pil_cb = Image.fromarray(frame_0).convert("RGBA")
                        else:
                            pil_cb = Image.open(cb_path).convert("RGBA")
                        
                        cb_encuadrado = aplicar_letterbox_pil(pil_cb, w_elem, h_total, Image.Resampling.LANCZOS)
                        clip_cb = ImageClip(np.array(cb_encuadrado)).with_duration(duracion_video).with_position((pos_x, 0))
                        lista_composicion.append(clip_cb)
                    except Exception as e:
                        print(f"[ARCHIE DEBUG] Error procesando Center Board en render, aplicando negro: {e}")
                        clip = ColorClip(size=(w_elem, h_total), color=(0, 0, 0), duration=duracion_video).with_position((pos_x, 0))
                        lista_composicion.append(clip)
                else:
                    clip = ColorClip(size=(w_elem, h_total), color=(0, 0, 0), duration=duracion_video).with_position((pos_x, 0))
                    lista_composicion.append(clip)
            elif tipo == "anim_zone" and anim_clips_base:
                a_idx = elemento.get("anim_idx", 0) % len(anim_clips_base)
                if anim_clips_base[a_idx]:
                    clip = anim_clips_base[a_idx].resized((w_elem, h_total)).with_position((pos_x, 0))
                    lista_composicion.append(clip)
                
    for nombre_zona, datos_zona in datos["zonas"].items():
        for elemento in datos_zona["elementos"]:
            tipo = elemento["tipo"]
            pos_x = elemento["x_absoluta"]
            w_elem = max(1, elemento["width"])

            if tipo == "static_logo" and clip_logo_base:
                if elemento.get("logo_under_type") == "frozen":
                    frame_0 = clip_logo_base.get_frame(0)
                    clip = ImageClip(frame_0).with_duration(duracion_video).resized((w_elem, h_total)).with_position((pos_x, 0))
                    if getattr(clip_logo_base, 'mask', None):
                        mask_frame_0 = clip_logo_base.mask.get_frame(0)
                        mask_clip = ImageClip(mask_frame_0, is_mask=True).with_duration(duracion_video)
                        clip = clip.with_mask(mask_clip)
                else:
                    clip = clip_logo_base.resized((w_elem, h_total)).with_position((pos_x, 0))

                lista_composicion.append(clip)

    mega_strip_clip = CompositeVideoClip(lista_composicion, size=(w_total, h_total))
    print("[ARCHIE DEBUG] Composición base de Mega Strip finalizada.")
    return mega_strip_clip, w_total, h_total, duracion_video

def renderizar_megastrip(json_path, output_filename="resultado_megastrip.mov", progress_callback=None, cancel_check=None):
    with open(json_path, 'r', encoding='utf-8') as f: datos = json.load(f)
    mega_strip_clip, w_total, h_total, duracion = construir_mega_strip(datos)
    logger = UILogger(progress_callback, cancel_check) if progress_callback else "bar"

    print("[ARCHIE DEBUG] Iniciando MoviePy write_videofile (MEGASTRIP)...")
    mega_strip_clip.write_videofile(
        output_filename, fps=25, codec="libx264", audio=False, ffmpeg_params=["-pix_fmt", "yuv420p"], logger=logger
    )
    mega_strip_clip.close()
    print("[ARCHIE DEBUG] Render MEGASTRIP finalizado.")

def renderizar_system_file(json_path, output_filename="resultado_system.mp4", progress_callback=None, cancel_check=None, row_target=None):
    with open(json_path, 'r', encoding='utf-8') as f: datos = json.load(f)
    mega_strip_clip, w_total, h_total, duracion = construir_mega_strip(datos)
    
    sys_data = datos.get("system_setup", {})
    sys_w = sys_data.get("canvas_width", 3840)
    sys_h = sys_data.get("canvas_height", 2160)
    sys_mask = sys_data.get("canvas_mask", 96)
    sys_off_x = sys_data.get("offset_x", 0)
    sys_off_y = sys_data.get("offset_y", 0)
    
    is_amplify = datos.get("config", {}).get("amplify", False)
    if is_amplify and row_target in ["1ST_ROW", "2ND_ROW"]:
        h_slice = h_total // 2
        if row_target == "1ST_ROW":
            src_y_start, src_y_end = h_slice, h_total
        else:
            src_y_start, src_y_end = 0, h_slice
    else:
        h_slice = h_total
        src_y_start, src_y_end = 0, h_total

    advance = sys_w - sys_mask
    total_rows = math.ceil((w_total + sys_off_x) / max(1, advance))
    
    def make_system_frame(t):
        mega_frame = mega_strip_clip.get_frame(t)
        sys_frame = np.zeros((sys_h, sys_w, 3), dtype=np.uint8)
        
        for i in range(total_rows):
            x_m_strip = i * advance - sys_off_x
            crop_x1 = max(0, x_m_strip)
            crop_x2 = min(w_total, x_m_strip + advance)
            if crop_x1 >= crop_x2: continue 
            
            x_canvas = abs(x_m_strip) if x_m_strip < 0 else 0
            paste_y = sys_off_y + (i * h_slice)
            
            y1, y2 = paste_y, paste_y + h_slice
            cur_src_y1, cur_src_y2 = src_y_start, src_y_end
            
            if y1 < 0: 
                cur_src_y1 = src_y_start - y1
                y1 = 0
            if y2 > sys_h: 
                cur_src_y2 = src_y_end - (y2 - sys_h)
                y2 = sys_h
            if y1 >= y2: continue
            
            width_to_paste = crop_x2 - crop_x1
            sys_frame[y1:y2, x_canvas:x_canvas+width_to_paste] = mega_frame[cur_src_y1:cur_src_y2, crop_x1:crop_x2]
            
        return sys_frame
        
    system_clip_final = VideoClip(make_system_frame).with_duration(duracion)
    logger = UILogger(progress_callback, cancel_check) if progress_callback else "bar"

    print(f"[ARCHIE DEBUG] Iniciando MoviePy write_videofile (SYSTEM - {row_target or 'FULL'})...")
    system_clip_final.write_videofile(
        output_filename, fps=25, codec="libx264", bitrate="15000k", audio=False, 
        ffmpeg_params=["-minrate", "15000k", "-maxrate", "15000k", "-bufsize", "30000k", "-pix_fmt", "yuv420p"], logger=logger
    )
    
    system_clip_final.close()
    mega_strip_clip.close()
    print("[ARCHIE DEBUG] Render SYSTEM finalizado.")