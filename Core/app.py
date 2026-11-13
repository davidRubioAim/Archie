import customtkinter as ctk
import tkinter as tk
from tkinterdnd2 import TkinterDnD, DND_FILES
import os
import json
import copy
import math
import traceback
import numpy as np
import threading
import gc
from PIL import Image, ImageDraw, ImageFont, ImageTk
from moviepy import VideoFileClip

from dotenv import load_dotenv
from google import genai
from google.genai import types

from motor_distribucion import calcular_layout_data, generar_imagen_arquitectura, segmentos_arquitectura
from edge_cases import edge_de, filas_system, restar_zonas, pintar_zonas_negras, logos_bajo_anims
from queue_manager import QueueManager
from batch_manager import BatchManager
from stadium_loader import load_stadium_library as cargar_estadios
from log_utils import debug, log_error
from frame_cache import FRAME_CACHE, is_video
from remote_config import load_remote_config
from auto_import import buscar_elements_hermanos
from auto_solver import resolver as resolver_arquitectura, completar_goal, CONFIG_KEYS
import hotspot_solver  # v0.61: auto-solver por hotspots + referencias JSON aprobadas
from campaign_panel import CampaignPanel  # v0.58: pestaña CAMPAIGNS (batch de campañas por estadio)
from video_player import PreviewPlayer, PhotoTarget, Escena, Vista, PLAYER_FPS
from vista_3d import Visor3D, geometria_estadio, escala_textura

# 1. Definir rutas maestras dinámicas (Soporte para arquitectura Launcher)
CORE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(CORE_DIR)

# Si estamos ejecutando desde 'versions/vX.X/', subimos al directorio maestro
if os.path.basename(os.path.dirname(BASE_DIR)).lower() == "versions":
    MASTER_DIR = os.path.dirname(os.path.dirname(BASE_DIR))
else:
    MASTER_DIR = BASE_DIR

CONFIG_DIR = os.path.join(MASTER_DIR, "Config")
hotspot_solver.configurar(CONFIG_DIR)
ASSETS_DIR = os.path.join(MASTER_DIR, "Assets")


# v0.38: auto-actualización de la Architecture Preview al tocar Manual Edit
AUTO_UPDATE_DELAY_MS = 350    # sliders / checkboxes
AUTO_UPDATE_TYPING_MS = 800   # campos de texto (Width, Height...)

def find_asset(filename):
    """Busca un asset en Assets/ junto al .bat y, si no está, en el Assets/ de la versión descargada."""
    for folder in (ASSETS_DIR, os.path.join(BASE_DIR, "Assets"), os.path.join(CORE_DIR, "Assets")):
        path = os.path.join(folder, filename)
        if os.path.exists(path):
            return path
    return None

# Windows agrupa la ventana bajo python.exe (y le pone su icono en la barra de tareas) si no tiene un AppUserModelID propio
if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AIM.Archie")
    except Exception:
        pass

# Validar HTTPS con los certificados de Windows (hoja de configuración, estadios, Gemini). Sin esto, algunos equipos
# fallan con CERTIFICATE_VERIFY_FAILED. truststore viene en requirements.txt; si no está, se sigue igual.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

# 2. Cargar variables de entorno desde Config
load_dotenv(os.path.join(CONFIG_DIR, ".env"))
load_remote_config(optional=("CAMPAIGN_SHEET_CSV_URL",))  # GEMINI_API_KEY (+ v0.58: URL CSV de la lista de campañas)
api_key = os.getenv("GEMINI_API_KEY")

try:
    RESAMPLE_METHOD = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_METHOD = Image.LANCZOS

# La previsualización usa un filtro más rápido; el render final sigue usando RESAMPLE_METHOD
try:
    PREVIEW_RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:
    PREVIEW_RESAMPLE = Image.BILINEAR

PREVIEW_FRAMES = 8

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

def obtener_version_local():
    """Lee la versión real dictada por el launcher desde config.ini maestro"""
    import configparser
    config_path = os.path.join(MASTER_DIR, "config.ini")
    if os.path.exists(config_path):
        try:
            config = configparser.ConfigParser()
            config.read(config_path)
            v = config['ARCHIE'].get('target_version', '').strip()
            if v: return v
        except Exception:
            pass

    # Respaldo: Inferencia desde el árbol de carpetas
    try:
        partes_ruta = BASE_DIR.replace("\\", "/").split("/")
        for parte in partes_ruta:
            if parte.startswith("v0.") or parte.startswith("v1."):
                return parte
    except Exception:
        pass

    return "v0.25"

class CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)

class MegaStripApp(CTkDnD):
    def _set_window_icon(self):
        """Icono de ventana y barra de tareas a partir de Assets/logo_Archie.ico.
        No se usa iconbitmap(): el lector de .ico de Tk no entiende las capas guardadas como PNG (lo habitual en .ico
        modernos) y deja un icono en blanco. PIL sí las lee, y iconphoto() recibe las imágenes ya decodificadas."""
        self._iconbitmap_method_called = True  # evita que CustomTkinter ponga su icono a los 200 ms
        ico = find_asset("logo_Archie.ico") or find_asset("logo_Archie.png")
        if not ico:
            print("  [WARN] Icono no encontrado: falta Assets/logo_Archie.ico")
            return
        try:
            src = Image.open(ico)
            if src.format == "ICO" and src.info.get("sizes"):
                src.size = max(src.info["sizes"])  # elige la capa más grande del .ico
            base = src.convert("RGBA")
            self._icon_photos = [ImageTk.PhotoImage(base.resize((n, n), RESAMPLE_METHOD)) for n in (256, 64, 48, 32, 16)]
            self.iconphoto(True, *self._icon_photos)
        except Exception as e:
            print(f"  [WARN] No se pudo poner el icono ({ico}): {e}")

    def __init__(self):
        super().__init__()
        
        # 1. Obtener versión dinámica al inicio
        self.current_version_str = obtener_version_local()
        
        # 2. Título de la ventana dinámico[cite: 3]
        self.title(f"AIM Auto Arch - AUTO UPDATE ({self.current_version_str})")
        self._set_window_icon()
        self.after(400, self._set_window_icon)  # por si CustomTkinter lo sobrescribe al terminar de crear la ventana
        self.geometry("1400x850")
        self.minsize(1280, 720)
        
        ctk.set_appearance_mode("light")

        self.c_magenta = "#FF099A"
        self.c_light_magenta = "#FDEAF4"
        self.c_white = "#FFFFFF"
        self.c_grey = "#F3F3F7"
        self.c_light_grey = "#F5F6FB"
        self.c_text_dark = "#1A1A1A" 
        self.c_text_muted = "#7A7A7A"

        self.font_t1 = ctk.CTkFont(family="Montserrat", size=28, weight="bold")
        self.font_t2 = ctk.CTkFont(family="Open Sans", size=12, weight="bold")
        self.font_t3 = ctk.CTkFont(family="Open Sans", size=14, weight="normal")

        self.configure(fg_color=self.c_grey)

        self.stadium_data = self.load_stadium_library()
        self.queue_manager = QueueManager(self)
        self.batch_manager = BatchManager(self)        
        self.resize_timer = None 
        self.preview_timer = None 
        self.pil_preview_images = []
        self.pil_sys_images = []
        self.pil_strip3d_images = []         # v0.46: strip (sin anotaciones, reducido) de cada key frame, para 3D PREVIEW
        self._geo3d = None                   # v0.46: (w_total, h_total, L, W) de la maqueta 3D actual
        self.preview_current_idx = 0
        self.zoom_factor_strip = 1.0 
        self.zoom_factor_sys = 1.0 
        self.manual_zoom_strip = False
        self.manual_zoom_sys = False
        self.preview_enabled = False
        self.architecture_created = False   # True tras el primer "Create Architecture" (auto-solver)
        self.arch_stadium = None            # estadio para el que se calculó la arquitectura por defecto
        self.arch_key = None                # v0.66: estadio + logo + anims de la arquitectura actual
        self.solver_key = None              # v0.66: estadio + logo + anims que resolvió el auto-solver
        self.preview_dirty = False          # hay cambios sin aplicar a la preview
        self.render_gen = 0                 # id del render en curso (descarta resultados obsoletos)
        self.render_busy = False
        self.prefetch_timer = None
        self.auto_update_timer = None       # v0.38: recálculo automático tras tocar Manual Edit
        self.solver_busy = False            # el auto-solver (arquitectura por defecto) está calculando
        self.solver_gen = 0                 # id del auto-solver en curso (Cancel lo invalida)
        self.loading_overlay_on = False     # v0.38: previews oscurecidas con "Loading new architecture"
        self._shown_strip = None            # última imagen pintada en Architecture Preview (sin oscurecer)
        self._shown_strip_player = False    # ...y si la pintó el reproductor
        self._shown_sys = None              # última imagen pintada en System File Preview
        self._shown_sys_player = False      # ...y si la pintó el reproductor
        self.player = PreviewPlayer(self)    # reproductor de vídeo de la Architecture Preview
        self._player_target = PhotoTarget()
        # v0.39: el reproductor también pinta en System File Preview (mismo reloj, mismos proxies)
        self._sys_player_target = PhotoTarget()
        self.sys_vista = self.player.add_vista(Vista(self.sys_player_view, self.sys_player_static_view, self.sys_player_show))
        # v0.46: y en 3D PREVIEW (el strip entero como textura, mismo reloj y mismos proxies)
        self.vista_3d = self.player.add_vista(Vista(self.v3d_player_view, self.v3d_player_static_view, self.v3d_player_show))

        self.pan_x_strip = 0
        self.pan_y_strip = 0
        self.pan_x_sys = 0
        self.pan_y_sys = 0
        self.drag_start_x = 0
        self.drag_start_y = 0
        self.is_dragging = False

        self.show_cb_var = ctk.BooleanVar(value=True)

        self.main_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.main_frame.pack(fill="both", expand=True, padx=15, pady=15)
        self.main_frame.grid_columnconfigure(0, weight=0, minsize=380) 
        self.main_frame.grid_columnconfigure(1, weight=0, minsize=340)
        self.main_frame.grid_columnconfigure(2, weight=1)
        self.main_frame.grid_rowconfigure(0, weight=0) 
        self.main_frame.grid_rowconfigure(1, weight=2) 
        self.main_frame.grid_rowconfigure(2, weight=1) 
        self.main_frame.grid_rowconfigure(3, weight=0)

        self.top_left_container = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.top_left_container.grid(row=0, column=0, columnspan=2, padx=5, pady=5, sticky="nsew")
        self.header_frame = ctk.CTkFrame(self.top_left_container, fg_color="transparent")
        self.header_frame.pack(fill="x", anchor="w", padx=10, pady=(0, 10))

        # 1. Carga del logotipo principal AIM
        logo_path = find_asset("logo_aim.png")
        if logo_path:
            img = Image.open(logo_path)
            self.logo_img = ctk.CTkImage(img, size=(int(40 * (img.width / img.height)), 40))
            ctk.CTkLabel(self.header_frame, image=self.logo_img, text="").pack(side="left", padx=(0, 10))

        # 2. Carga del logotipo de la herramienta y versión dinámica
        # Sub-contenedor vertical exclusivo para Logo + Etiqueta de versión
        archie_box = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        archie_box.pack(side="left", padx=(0, 20))

        logo_archie_path = find_asset("logo_Archie.png")
        if logo_archie_path:
            img_archie = Image.open(logo_archie_path)
            self.logo_archie_img = ctk.CTkImage(img_archie, size=(int(40 * (img_archie.width / img_archie.height)), 40))
            ctk.CTkLabel(archie_box, image=self.logo_archie_img, text="").pack(anchor="w")
        else:
            ctk.CTkLabel(archie_box, text="Archie", font=self.font_t1, text_color=self.c_magenta).pack(anchor="w")

        # Etiqueta magenta en letra fina debajo del logo (siempre visible, haya logo o no)
        self.lbl_version_tag = ctk.CTkLabel(
            archie_box,
            text=f"Beta {self.current_version_str}",
            font=ctk.CTkFont(family="Open Sans", size=10, weight="normal"),
            text_color=self.c_magenta
        )
        self.lbl_version_tag.pack(anchor="w", pady=(0, 0))
            
        self.stadium_frame = ctk.CTkFrame(self.header_frame, fg_color="transparent")
        self.stadium_frame.pack(side="right", padx=10)

        top_stadium_row = ctk.CTkFrame(self.stadium_frame, fg_color="transparent")
        top_stadium_row.pack(fill="x", anchor="e", pady=(0, 4))
        
        ctk.CTkLabel(top_stadium_row, text="Stadium Profile:", font=self.font_t2, text_color=self.c_text_dark).pack(side="left", padx=(0, 10))
        
        self.stadium_search_var = ctk.StringVar()
        self.stadium_search_var.trace_add("write", self.filter_stadium_dropdown)
        self.stadium_search_entry = ctk.CTkEntry(
            top_stadium_row, 
            placeholder_text="🔍 Filter stadium...", 
            textvariable=self.stadium_search_var, 
            width=170, 
            height=24, 
            font=ctk.CTkFont(size=11),
            fg_color=self.c_white,
            text_color=self.c_text_dark
        )
        self.stadium_search_entry.pack(side="right")
        self.stadium_search_entry.bind("<Return>", self.on_search_return)
        
        self.btn_refresh_stadiums = ctk.CTkButton(
            top_stadium_row,
            text="↺",
            width=28,
            height=24,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#2b2b2b",
            hover_color=self.c_magenta,
            text_color=self.c_white,
            command=self.refresh_stadium_library
        )
        self.btn_refresh_stadiums.pack(side="right", padx=(0, 6))
        
        self.stadium_selector_global = ctk.CTkOptionMenu(
            self.stadium_frame, 
            values=list(self.stadium_data.keys()), 
            command=self.on_stadium_change, 
            fg_color="#2b2b2b",                   
            text_color=self.c_white,              
            button_color="#1a1a1a",               
            button_hover_color=self.c_magenta,    
            font=self.font_t3,
            width=400,
            dynamic_resizing=False
        )
        self.stadium_selector_global.pack(anchor="e", fill="x")

        self.col_media = ctk.CTkFrame(self.top_left_container, fg_color=self.c_white, corner_radius=15, border_width=0)
        self.col_media.pack(fill="x", expand=False)
        media_header = ctk.CTkFrame(self.col_media, fg_color="transparent")
        media_header.pack(fill="x", padx=20, pady=(10, 5))
        ctk.CTkLabel(media_header, text="MEDIA ASSETS", font=self.font_t2, text_color=self.c_text_dark).pack(side="left")
        # v0.58: FILES = zonas de drop de siempre · CAMPAIGNS = batch de campañas para el estadio seleccionado
        self.media_mode = ctk.CTkSegmentedButton(
            media_header, values=["FILES", "CAMPAIGNS"], command=self.on_media_mode,
            selected_color=self.c_magenta, selected_hover_color="#D1007A",
            unselected_color=self.c_light_grey, unselected_hover_color="#E0E0E0",
            fg_color=self.c_light_grey, text_color=self.c_text_dark, font=self.font_t2, height=24)
        self.media_mode.set("FILES")
        self.media_mode.pack(side="right")

        self.drop_labels = {}
        self.archivos_cargados = {"background": None, "center_board": None, "static_logo": None, "anim_zone": None, "anim_zones": []}
        
        self.media_body = ctk.CTkFrame(self.col_media, fg_color="transparent")
        self.media_body.pack(fill="x", expand=False)
        self.drop_zones_frame = ctk.CTkFrame(self.media_body, fg_color="transparent")
        self.drop_zones_frame.pack(fill="x", expand=False, padx=10, pady=(0, 5))
        self.drop_zones_frame.grid_columnconfigure(0, weight=1)
        self.drop_zones_frame.grid_columnconfigure(1, weight=1)
        self.drop_zones_frame.grid_rowconfigure(0, weight=0)
        self.drop_zones_frame.grid_rowconfigure(1, weight=0)

        self.create_main_drop_zone(self.drop_zones_frame, "Background", "background", 0, 0)
        self.create_main_drop_zone(self.drop_zones_frame, "Center Board", "center_board", 0, 1)
        self.create_main_drop_zone(self.drop_zones_frame, "Logo Layer", "static_logo", 1, 0)

        col_anim = ctk.CTkFrame(self.drop_zones_frame, fg_color="transparent")
        col_anim.grid(row=1, column=1, sticky="new", padx=5, pady=2)
        
        anim_header = ctk.CTkFrame(col_anim, fg_color="transparent")
        anim_header.pack(fill="x", pady=(0, 5))
        ctk.CTkLabel(anim_header, text="Anim Zones", font=self.font_t2, text_color=self.c_text_dark).pack(side="left")
        ctk.CTkButton(
            anim_header, text="+", width=25, height=25, 
            fg_color="#2b2b2b", hover_color="#404040", text_color=self.c_white, 
            font=ctk.CTkFont(weight="bold"), command=self.add_anim_zone
        ).pack(side="right")
        
        self.anim_scroll_wrapper = ctk.CTkFrame(col_anim, fg_color="transparent", height=55)
        self.anim_scroll_wrapper.pack_propagate(False) 
        self.anim_scroll_wrapper.pack(fill="x", expand=False)
        
        self.anim_scroll = ctk.CTkScrollableFrame(
            self.anim_scroll_wrapper, fg_color="transparent", corner_radius=0, border_width=0,
            scrollbar_button_color=self.c_white, scrollbar_button_hover_color=self.c_white
        )
        self.anim_scroll.pack(fill="both", expand=True)
        self.add_anim_zone()

        # v0.58: pestaña CAMPAIGNS (se muestra en lugar de las zonas de drop)
        self.campaign_panel = CampaignPanel(self, self.media_body, CONFIG_DIR)

        # --- NUEVO BLOQUE DE ACCIONES (CREATE + RESET + UNDO/REDO) ---
        self.actions_arch_frame = ctk.CTkFrame(self.col_media, fg_color="transparent")
        self.actions_arch_frame.pack(fill="x", padx=20, pady=(10, 15))
        
        self.actions_arch_frame.grid_columnconfigure(0, weight=3)
        self.actions_arch_frame.grid_columnconfigure(1, weight=1)
        self.actions_arch_frame.grid_columnconfigure(2, weight=0)
        self.actions_arch_frame.grid_columnconfigure(3, weight=0)

        # corner_radius unificado a 10
        self.btn_create_arch = ctk.CTkButton(
            self.actions_arch_frame, text="✨ Create Architecture", font=self.font_t2, height=40, 
            corner_radius=10, fg_color=self.c_magenta, text_color=self.c_white,
            hover_color="#D1007A", command=self.on_create_architecture
        )
        self.btn_create_arch.grid(row=0, column=0, sticky="ew", padx=(0, 5))

        self.btn_reset_arch = ctk.CTkButton(
            self.actions_arch_frame, text="🔄 RESET", font=self.font_t2, height=40, 
            corner_radius=10, fg_color="#2b2b2b", text_color=self.c_white,
            hover_color="#404040", command=self.reset_layout_to_defaults
        )
        self.btn_reset_arch.grid(row=0, column=1, sticky="ew", padx=(5, 5))

        self.btn_undo = ctk.CTkButton(
            self.actions_arch_frame, text="↶", font=ctk.CTkFont(size=18, weight="bold"), 
            width=40, height=40, corner_radius=10, fg_color="#2b2b2b", text_color=self.c_white,
            hover_color="#404040", command=self.undo_action, state="disabled"
        )
        self.btn_undo.grid(row=0, column=2, sticky="ew", padx=(0, 5))

        self.btn_redo = ctk.CTkButton(
            self.actions_arch_frame, text="↷", font=ctk.CTkFont(size=18, weight="bold"), 
            width=40, height=40, corner_radius=10, fg_color="#2b2b2b", text_color=self.c_white,
            hover_color="#404040", command=self.redo_action, state="disabled"
        )
        self.btn_redo.grid(row=0, column=3, sticky="ew", padx=(0, 0))

        # v0.61: guarda la arquitectura actual como referencia aprobada (Config/referencias)
        self.actions_arch_frame.grid_columnconfigure(4, weight=0)
        self.btn_ref = ctk.CTkButton(
            self.actions_arch_frame, text="★", font=ctk.CTkFont(size=18, weight="bold"),
            width=40, height=40, corner_radius=10, fg_color="#2b2b2b", text_color="#FFC83D",
            hover_color="#404040", command=self.guardar_como_referencia
        )
        self.btn_ref.grid(row=0, column=4, sticky="ew", padx=(5, 0))

        # --- Inicialización de Historial ---
        self.undo_stack = []
        self.redo_stack = []
        self.last_saved_state = None
        self.after(500, self._init_history_state)

        self.tabs_container = ctk.CTkTabview(
            self.main_frame, 
            corner_radius=10, 
            fg_color=self.c_white, 
            segmented_button_fg_color=self.c_light_grey, 
            segmented_button_selected_color=self.c_magenta, 
            segmented_button_selected_hover_color="#D1007A",
            segmented_button_unselected_color=self.c_light_grey,
            segmented_button_unselected_hover_color="#E0E0E0",
            text_color=self.c_text_dark
        )
        self.tabs_container.grid(row=1, column=0, rowspan=2, columnspan=2, padx=5, pady=(5, 0), sticky="nsew")
        self.tabs_container.add("MANUAL EDIT")
        self.tabs_container.add("AI ASSISTANT")
        
        self.tabs_container.set("AI ASSISTANT")
        
        self.inputs = {}
        self.setup_manual_edit_tab()
        self._ui_defaults = self._capture_ui_state()   # v0.58: valores por defecto para campañas nuevas
        self.setup_ai_assistant_tab()
        self.init_ai_session()

        self.preview_area = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.preview_area.grid(row=0, column=2, rowspan=3, padx=5, pady=(5, 0), sticky="nsew")
        self.preview_area.grid_columnconfigure(0, weight=1)
        self.preview_area.grid_rowconfigure(0, weight=0) 
        self.preview_area.grid_rowconfigure(1, weight=2) 
        self.preview_area.grid_rowconfigure(2, weight=1) 
        
        self.lbl_warning = ctk.CTkLabel(self.preview_area, text="", text_color="#E74C3C", font=ctk.CTkFont(weight="bold", size=13))
        self.lbl_warning.grid(row=0, column=0, sticky="ew", pady=(0, 5))

        # v0.46: SYSTEM FILE PREVIEW y 3D PREVIEW en pestañas (la Architecture Preview de abajo no cambia)
        self.preview_tabs = ctk.CTkTabview(
            self.preview_area,
            corner_radius=0,      # v0.47: sin margen interno -> mismo ancho que la Architecture Preview
            border_width=0,
            fg_color="transparent",
            segmented_button_fg_color=self.c_light_grey,
            segmented_button_selected_color=self.c_magenta,
            segmented_button_selected_hover_color="#D1007A",
            segmented_button_unselected_color=self.c_light_grey,
            segmented_button_unselected_hover_color="#E0E0E0",
            text_color=self.c_text_dark,
            command=self.on_preview_tab_change
        )
        self.preview_tabs.grid(row=1, column=0, sticky="nsew", pady=(0, 5))
        tab_sys = self.preview_tabs.add("SYSTEM FILE PREVIEW")
        tab_3d = self.preview_tabs.add("3D PREVIEW")
        self.preview_tabs.set("SYSTEM FILE PREVIEW")
        try:
            self.preview_tabs._segmented_button.configure(corner_radius=8)  # botones redondeados como las otras pestañas
        except Exception:
            pass

        self.sys_preview_frame = ctk.CTkFrame(
            tab_sys, 
            corner_radius=10, 
            border_width=2, 
            border_color=self.c_magenta,
            fg_color="#0c0c0c")
        self.sys_preview_frame.pack(fill="both", expand=True)
        self.sys_preview_frame.grid_rowconfigure(1, weight=1)
        self.sys_preview_frame.grid_columnconfigure(0, weight=1)

        sys_header = ctk.CTkFrame(self.sys_preview_frame, fg_color="transparent")
        sys_header.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(sys_header, text="SYSTEM FILE PREVIEW", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta).pack(side="left")
        ctk.CTkButton(sys_header, text="+ Zoom", width=50, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.zoom_in_sys).pack(side="right", padx=5)
        ctk.CTkButton(sys_header, text="- Zoom", width=50, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.zoom_out_sys).pack(side="right", padx=5)
        ctk.CTkButton(sys_header, text="Fit", width=40, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.fit_sys).pack(side="right", padx=10)

        self.preview_row_idx = 1
        self.btn_toggle_row = ctk.CTkButton(
            sys_header, 
            text="🔄 View: 1ST ROW", 
            width=110, height=20, 
            fg_color="#8E44AD", hover_color="#9B59B6", 
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.toggle_preview_row
        )
        
        self.sys_image_container = ctk.CTkFrame(self.sys_preview_frame, fg_color="transparent")
        self.sys_image_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.lbl_sys_image = ctk.CTkLabel(self.sys_image_container, text="")
        self.lbl_sys_image.place(relx=0.5, rely=0.5, anchor="center")
        self.lbl_sys_placeholder = ctk.CTkLabel(self.sys_image_container, text="System File Preview will appear here.", text_color="gray")
        self.lbl_sys_placeholder.place(relx=0.5, rely=0.5, anchor="center")

        # --- 3D PREVIEW (v0.46): maqueta fake 3D del perímetro, lógica portada de Checker ---
        self.v3d_frame = ctk.CTkFrame(tab_3d, corner_radius=10, border_width=2, border_color=self.c_magenta, fg_color="#0c0c0c")
        self.v3d_frame.pack(fill="both", expand=True)
        self.v3d_frame.grid_rowconfigure(1, weight=1)
        self.v3d_frame.grid_columnconfigure(0, weight=1)
        v3d_header = ctk.CTkFrame(self.v3d_frame, fg_color="transparent")
        v3d_header.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        ctk.CTkLabel(v3d_header, text="3D PREVIEW", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta).pack(side="left")
        ctk.CTkLabel(v3d_header, text="Left drag: move · Right drag: rotate · Wheel: zoom · Double click: reset",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(side="left", padx=12)
        ctk.CTkButton(v3d_header, text="Reset view", width=70, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=lambda: self.visor_3d.reset_view()).pack(side="right", padx=5)
        self.lbl_v3d_info = ctk.CTkLabel(v3d_header, text="", font=ctk.CTkFont(size=11), text_color="gray")
        self.lbl_v3d_info.pack(side="right", padx=10)
        self.v3d_container = tk.Frame(self.v3d_frame, bg="#0c0c0c", bd=0, highlightthickness=0)
        self.v3d_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.visor_3d = Visor3D(self.v3d_container)
        self.visor_3d.activo = self._v3d_visible
        self.visor_3d.on_escala = self._on_escala_3d



        self.preview_container = ctk.CTkFrame(
            self.preview_area, 
            corner_radius=10, 
            border_width=2, 
            border_color=self.c_magenta, 
            fg_color="#0c0c0c")
        self.preview_container.grid(row=2, column=0, sticky="nsew")
        self.preview_container.grid_rowconfigure(1, weight=1)
        self.preview_container.grid_columnconfigure(0, weight=1)
        
        self.preview_header = ctk.CTkFrame(self.preview_container, fg_color="transparent")
        self.preview_header.grid(row=0, column=0, sticky="ew", padx=10, pady=5)
        self.lbl_preview_status = ctk.CTkLabel(self.preview_header, text="ARCHITECTURE PREVIEW", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta)
        self.lbl_preview_status.pack(side="left", padx=10)
        ctk.CTkButton(self.preview_header, text="+ Zoom", width=60, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.zoom_in_strip).pack(side="right", padx=5)
        ctk.CTkButton(self.preview_header, text="- Zoom", width=60, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.zoom_out_strip).pack(side="right", padx=5)
        ctk.CTkButton(self.preview_header, text="Fit", width=40, height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray", command=self.fit_strip).pack(side="right", padx=10)
        
        self.preview_image_container = ctk.CTkFrame(self.preview_container, fg_color="transparent")
        self.preview_image_container.grid(row=1, column=0, sticky="nsew", padx=10, pady=5)
        self.lbl_live_preview = ctk.CTkLabel(self.preview_image_container, text="")
        self.lbl_live_preview.place(relx=0.5, rely=0.5, anchor="center")

        self.lbl_arch_placeholder = ctk.CTkLabel(
            self.preview_image_container, 
            text="Architecture Preview will appear here.", 
            text_color="gray"
        )
        self.lbl_arch_placeholder.place(relx=0.5, rely=0.5, anchor="center")

        # --- REPRODUCTOR DE VÍDEO (v0.35) ---
        self.player_bar = ctk.CTkFrame(self.preview_container, fg_color="transparent")
        self.player_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))
        self.player_bar.grid_columnconfigure(3, weight=1)
        self._player_icons = self._crear_iconos_reproductor()
        self.btn_player_play = ctk.CTkButton(
            self.player_bar, text="", image=self._player_icons["play"], width=34, height=24, fg_color=self.c_magenta, hover_color="#d1077e",
            font=ctk.CTkFont(size=13, weight="bold"), command=self.on_player_toggle, state="disabled")
        self.btn_player_play.grid(row=0, column=0, padx=(0, 6))
        self.player_loop_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.player_bar, text="Loop", width=60, checkbox_width=16, checkbox_height=16,
                        font=ctk.CTkFont(size=11), text_color="gray", fg_color=self.c_magenta,
                        variable=self.player_loop_var, command=self.on_player_loop).grid(row=0, column=1, padx=(0, 4))
        self.lbl_player_time = ctk.CTkLabel(self.player_bar, text="--:--.- / --:--.-", text_color="gray",
                                            font=ctk.CTkFont(family="Consolas", size=11), width=110)
        self.lbl_player_time.grid(row=0, column=2, padx=(0, 8))
        # v0.47: la línea de tiempo llega hasta el borde derecho (sin el texto Proxy / fps / Paused)
        self.player_slider = ctk.CTkSlider(
            self.player_bar, from_=0, to=1, number_of_steps=1000, height=14,
            button_color=self.c_magenta, button_hover_color="#d1077e", progress_color=self.c_magenta,
            command=self.on_player_scrub, state="disabled")
        self.player_slider.set(0)
        self.player_slider.grid(row=0, column=3, sticky="ew")
        self.player_slider.bind("<ButtonRelease-1>", self.on_player_scrub_end, add="+")
        self.lbl_player_info = ctk.CTkLabel(self.player_bar, text="")  # ya no se muestra (se mantiene para no tocar player_state_changed)
        self._player_scrubbing = False
        self._player_scrub_resume = False
        
        self.lbl_live_preview.bind("<ButtonPress-1>", lambda e: self.on_drag_start(e, "strip"))
        self.lbl_live_preview.bind("<B1-Motion>", lambda e: self.on_drag_motion(e, "strip"))
        self.lbl_live_preview.bind("<ButtonRelease-1>", lambda e: self.on_drag_release(e, "strip"))
        self.lbl_sys_image.bind("<ButtonPress-1>", lambda e: self.on_drag_start(e, "sys"))
        self.lbl_sys_image.bind("<B1-Motion>", lambda e: self.on_drag_motion(e, "sys"))
        self.lbl_sys_image.bind("<ButtonRelease-1>", lambda e: self.on_drag_release(e, "sys"))
        
        self.sys_image_container.bind("<Configure>", self.trigger_resize)
        self.preview_image_container.bind("<Configure>", self.trigger_resize)

        # --- NUEVO CONTENEDOR DE CONTROLES DE EXPORTACIÓN (Debajo de Previews) ---
        self.preview_area.grid_rowconfigure(3, weight=0) 
        
        self.export_controls_frame = ctk.CTkFrame(self.preview_area, fg_color="transparent")
        self.export_controls_frame.grid(row=3, column=0, sticky="nsew", pady=(10, 0))

        # Rutas de Exportación
        self.output_frame = ctk.CTkFrame(self.export_controls_frame, fg_color="transparent")
        self.output_frame.pack(fill="x", pady=(0, 5))
        self.output_frame.grid_columnconfigure(0, weight=1)
        self.output_frame.grid_columnconfigure(1, weight=0)

        self.output_path_var = ctk.StringVar(value=MASTER_DIR)  # antes os.getcwd(): con el .exe era la carpeta interna de la versión
        ctk.CTkLabel(self.output_frame, text="Output Directory:", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=0, sticky="w", padx=5)
        
        path_sub_frame = ctk.CTkFrame(self.output_frame, fg_color="transparent")
        path_sub_frame.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 2))
        ctk.CTkEntry(path_sub_frame, textvariable=self.output_path_var, state="readonly", height=30).pack(side="left", fill="x", expand=True, padx=(0, 10))
        ctk.CTkButton(path_sub_frame, text="Browse", width=80, height=30, fg_color=self.c_magenta, hover_color="#D1007A", command=self.select_output_path).pack(side="right")

        ctk.CTkLabel(self.output_frame, text="Base Filename:", font=ctk.CTkFont(size=12, weight="bold")).grid(row=0, column=1, sticky="w", padx=(10, 5))
        self.output_filename_var = ctk.StringVar(value="NEW_AE_EXPORT")
        ctk.CTkEntry(self.output_frame, textvariable=self.output_filename_var, width=220, height=30).grid(row=1, column=1, sticky="ew", padx=(10, 5), pady=(0, 2))

        # Botones de Acción Redimensionados
        self.btn_frame = ctk.CTkFrame(self.export_controls_frame, fg_color="transparent")
        self.btn_frame.pack(fill="x", pady=(2, 0))
        self.btn_frame.grid_columnconfigure(0, weight=1)
        self.btn_frame.grid_columnconfigure(1, weight=1)
        self.btn_frame.grid_columnconfigure(2, weight=1)

        self.left_btn_frame = ctk.CTkFrame(self.btn_frame, fg_color="transparent")
        self.left_btn_frame.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.left_btn_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkButton(
            self.left_btn_frame, 
            text="Generate AE Import", 
            font=ctk.CTkFont(size=13, weight="bold"), 
            height=35,
            fg_color="#E0E0E0",
            hover_color="#D0D0D0",
            text_color=self.c_text_dark,
            command=lambda: self.queue_manager.export_ae_json()
        ).grid(row=0, column=0, sticky="ew")

        self.center_btn_frame = ctk.CTkFrame(self.btn_frame, fg_color="transparent")
        self.center_btn_frame.grid(row=0, column=1, sticky="ew", padx=5)
        ctk.CTkButton(
            self.center_btn_frame, 
            text="Batch Add to Queue", 
            font=ctk.CTkFont(size=13, weight="bold"), 
            height=35,
            fg_color="#2b2b2b", 
            hover_color="#404040", 
            text_color=self.c_white, 
            command=self.batch_manager.open_modal
        ).pack(fill="both", expand=True)

        self.right_btn_frame = ctk.CTkFrame(self.btn_frame, fg_color="transparent")
        self.right_btn_frame.grid(row=0, column=2, sticky="ew", padx=(5, 0))
        self.right_btn_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(
            self.right_btn_frame, 
            text="+", 
            font=ctk.CTkFont(size=18, weight="bold"), 
            width=35, height=35,
            fg_color="#2b2b2b", 
            hover_color="#404040", 
            text_color=self.c_white, 
            command=lambda: self.queue_manager.add_to_queue("SYSTEM")
        ).grid(row=0, column=0, padx=(0,5))
        ctk.CTkButton(
            self.right_btn_frame, 
            text="Generate SYSTEM FILE", 
            font=ctk.CTkFont(size=13, weight="bold"), 
            height=35,
            fg_color=self.c_magenta, 
            hover_color="#D1007A", 
            command=lambda: self.queue_manager.run_direct_render("SYSTEM")
        ).grid(row=0, column=1, sticky="ew")

        # --- CONTENEDOR EXCLUSIVO PARA QUEUE MANAGER (Ocupa todo el ancho cuando está visible) ---
        self.bottom_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent", width=0, height=0)
        self.bottom_frame.grid(row=3, column=0, columnspan=3, sticky="nsew")
        
        self.queue_panel = ctk.CTkFrame(self.bottom_frame, corner_radius=10, border_width=2, border_color=self.c_magenta)
        queue_header = ctk.CTkFrame(self.queue_panel, fg_color="transparent")
        queue_header.pack(fill="x", padx=10, pady=(5, 0))
        ctk.CTkLabel(queue_header, text="BACKGROUND RENDER QUEUE", font=ctk.CTkFont(weight="bold"), text_color=self.c_magenta).pack(side="left", padx=5)
        
        self.btn_toggle_queue = ctk.CTkButton(queue_header, text="▲ Hide Queue", fg_color="#2b2b2b", hover_color="#404040", text_color=self.c_white, width=100, height=24, font=ctk.CTkFont(size=11, weight="bold"), command=self.toggle_queue_visibility)
        self.btn_toggle_queue.pack(side="right", padx=10)
        self.queue_is_visible = True

        self.btn_start_queue = ctk.CTkButton(queue_header, text="▶ Start Queue", fg_color="#27AE60", hover_color="#2ECC71", width=110, height=24, font=ctk.CTkFont(weight="bold"), command=self.queue_manager.start_queue_processor)
        self.btn_start_queue.pack(side="right", padx=5)
        
        self.btn_clear_queue = ctk.CTkButton(queue_header, text="Clear Done", fg_color="#7f8c8d", hover_color="#95a5a6", width=80, height=24, font=ctk.CTkFont(weight="bold"), command=self.queue_manager.clear_completed_jobs)
        self.btn_clear_queue.pack(side="right", padx=5)
        
        self.queue_scroll = ctk.CTkScrollableFrame(self.queue_panel, height=100, fg_color="transparent")
        self.queue_scroll.pack(fill="x", padx=10, pady=(5, 5))

        self.queue_panel.pack_forget()

        self.overlay_frame = ctk.CTkFrame(self, fg_color="#1a1a1a", corner_radius=0)
        self.overlay_inner = ctk.CTkFrame(self.overlay_frame, corner_radius=15, border_width=2, border_color=self.c_magenta, fg_color="#2b2b2b")
        self.overlay_inner.place(relx=0.5, rely=0.5, anchor="center", relwidth=0.4, relheight=0.3)
        self.lbl_overlay_title = ctk.CTkLabel(self.overlay_inner, text="RENDERING DIRECT FILE...", font=ctk.CTkFont(size=20, weight="bold"), text_color=self.c_magenta)
        self.lbl_overlay_title.pack(pady=(30, 10))
        self.lbl_overlay_status = ctk.CTkLabel(self.overlay_inner, text="Preparing engine...", text_color="gray")
        self.lbl_overlay_status.pack(pady=(0, 10))
        self.overlay_bar = ctk.CTkProgressBar(self.overlay_inner, progress_color=self.c_magenta)
        self.overlay_bar.pack(fill="x", padx=40, pady=10)
        self.overlay_bar.set(0)
        self.btn_overlay_cancel = ctk.CTkButton(self.overlay_inner, text="Cancel Render", fg_color="#C0392B", hover_color="#E74C3C", font=ctk.CTkFont(weight="bold"), command=self.queue_manager.cancel_direct_render)
        self.btn_overlay_cancel.pack(pady=(20, 20))

        # v0.38: aviso de carga solo sobre las dos previews (la imagen anterior se oscurece debajo);
        # el resto de la ventana sigue editable.
        self.sys_loading_box = ctk.CTkFrame(self.sys_image_container, corner_radius=10, border_width=1, border_color=self.c_magenta, fg_color="#111111")
        ctk.CTkLabel(self.sys_loading_box, text="Loading new architecture", font=ctk.CTkFont(size=16, weight="bold"), text_color=self.c_white).pack(padx=24, pady=12)

        self.preview_loading_box = ctk.CTkFrame(self.preview_image_container, corner_radius=10, border_width=1, border_color=self.c_magenta, fg_color="#111111")
        ctk.CTkLabel(self.preview_loading_box, text="Loading new architecture", font=ctk.CTkFont(size=16, weight="bold"), text_color=self.c_white).pack(padx=24, pady=(12, 2))
        self.lbl_preview_overlay_status = ctk.CTkLabel(self.preview_loading_box, text="Preparing...", text_color="gray", font=ctk.CTkFont(size=11))
        self.lbl_preview_overlay_status.pack(padx=24)
        self.preview_overlay_bar = ctk.CTkProgressBar(self.preview_loading_box, progress_color=self.c_magenta, width=220, height=6)
        self.preview_overlay_bar.pack(padx=24, pady=(4, 6))
        self.preview_overlay_bar.set(0)
        ctk.CTkButton(self.preview_loading_box, text="Cancel", width=80, height=22, fg_color="#C0392B", hover_color="#E74C3C",
                      font=ctk.CTkFont(size=11, weight="bold"), command=self.cancel_preview_render).pack(pady=(0, 10))

    def get_ctk_scale(self):
        try:
            if hasattr(self, '_get_window_scaling'):
                return self._get_window_scaling()
            return ctk.ScalingTracker.get_widget_scaling(self)
        except Exception:
            return 1.0

    def reset_zooms_and_pan(self):
        self.manual_zoom_strip = False
        self.manual_zoom_sys = False
        self.pan_x_strip = 0
        self.pan_y_strip = 0
        self.pan_x_sys = 0
        self.pan_y_sys = 0

    def reset_layout_to_defaults(self):
        """
        Restaura la distribución y configuración original calculada por Archie por defecto 
        para el estadio y los elementos importados actualmente, sin eliminar los archivos cargados [source: 6].
        """
        stadium_actual = self.stadium_selector_global.get()
        if stadium_actual and stadium_actual in self.stadium_data:
            # 1. Recargamos el perfil nativo del estadio desde stadium_data [source: 6]
            self.on_stadium_change(stadium_actual)
        
        # 2. Reseteamos el estado visual de zoom y paneo [source: 6]
        self.reset_zooms_and_pan()
        
        # 3. Regeneramos el empaquetado óptimo sin solapamientos [source: 6]
        self.auto_create_architecture()
        self.append_chat("Archie", "🔄 Layout reset to default optimal values.")

    # ==========================================
    # LÓGICA DE HISTORIAL (UNDO/REDO)
    # ==========================================
    def _init_history_state(self):
        self.last_saved_state = self._capture_ui_state()
        self._update_undo_redo_buttons()

    def _capture_ui_state(self):
        return {
            "inputs": {k: v.get() for k, v in self.inputs.items()},
            "sliders": {
                "margin": self.margin_slider.get(),
                "margin_tl": self.margin_tl_slider.get(),
                "offset": self.offset_slider.get(),
                "stretch": self.stretch_slider.get(),
                "qty_goal": self.qty_goal_slider.get(),
                "qty_corner": self.qty_corner_slider.get(),
                "qty_cb": self.qty_cb_slider.get(),
                "qty_tl": self.qty_tl_slider.get(),
                "max_anim_gl": self.max_anim_gl_slider.get(),
                "max_anim_tl": self.max_anim_tl_slider.get(),
                "qty_gl_under": self.qty_gl_under_slider.get(),
                "qty_tl_under": self.qty_tl_under_slider.get()
            },
            "vars": {
                "squash_logos": self.squash_logos_var.get(),
                "stretch_fit": self.stretch_fit_var.get(),
                "corner_logo": self.corner_logo_var.get(),
                "show_cb": getattr(self, "show_cb_var", ctk.BooleanVar(value=True)).get(),
                "logo_under": self.logo_under_var.get(),
                "amplify": self.amplify_var.get(),
                "invert_order": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get()
            }
        }

    def _apply_ui_state(self, state):
        # 1. Aplicar Inputs
        for k, v in state["inputs"].items():
            if k in self.inputs:
                self.inputs[k].delete(0, "end")
                self.inputs[k].insert(0, v)
        
        # 2. Aplicar Sliders
        sl = state["sliders"]
        self.margin_slider.set(sl["margin"])
        self.update_margin_label(sl["margin"])
        self.margin_tl_slider.set(sl.get("margin_tl", sl["margin"]))
        self.update_margin_tl_label(sl.get("margin_tl", sl["margin"]))
        self.offset_slider.set(sl["offset"])
        self.update_offset_label(sl["offset"])
        self.stretch_slider.set(sl["stretch"])
        self.update_stretch_label(sl["stretch"])
        self.qty_goal_slider.set(sl["qty_goal"])
        self.update_qty_labels(sl["qty_goal"], "goal")
        self.qty_corner_slider.set(sl["qty_corner"])
        self.update_qty_labels(sl["qty_corner"], "corner")
        self.qty_cb_slider.set(sl["qty_cb"])
        self.update_qty_labels(sl["qty_cb"], "cb")
        self.qty_tl_slider.set(sl["qty_tl"])
        self.update_qty_labels(sl["qty_tl"], "tl")
        self.max_anim_gl_slider.set(sl["max_anim_gl"])
        self.update_qty_anim_labels(sl["max_anim_gl"], "anim_gl")
        self.max_anim_tl_slider.set(sl["max_anim_tl"])
        self.update_qty_anim_labels(sl["max_anim_tl"], "anim_tl")
        self.qty_gl_under_slider.set(sl["qty_gl_under"])
        self.update_qty_under_labels(sl["qty_gl_under"], "gl_under")
        self.qty_tl_under_slider.set(sl["qty_tl_under"])
        self.update_qty_under_labels(sl["qty_tl_under"], "tl_under")

        # 3. Aplicar Variables (Checkboxes)
        vr = state["vars"]
        self.squash_logos_var.set(vr["squash_logos"])
        self.stretch_fit_var.set(vr["stretch_fit"])
        self.corner_logo_var.set(vr["corner_logo"])
        if hasattr(self, "show_cb_var"): self.show_cb_var.set(vr["show_cb"])
        self.logo_under_var.set(vr["logo_under"])
        self.amplify_var.set(vr["amplify"])
        if hasattr(self, "invert_order_var"): self.invert_order_var.set(vr["invert_order"])

        # 4. Disparar lógica de UI anidada
        self.toggle_logo_under_mode()
        self.on_amplify_toggle()

    def save_history_state(self, event=None):
        if not hasattr(self, "last_saved_state") or self.last_saved_state is None:
            return
            
        current = self._capture_ui_state()
        if current != self.last_saved_state:
            self.undo_stack.append(self.last_saved_state)
            if len(self.undo_stack) > 30: # Max 30 estados en RAM
                self.undo_stack.pop(0)
            
            self.redo_stack.clear()
            self.last_saved_state = current
            self._update_undo_redo_buttons()

    def undo_action(self):
        if self.undo_stack:
            current = self._capture_ui_state()
            self.redo_stack.append(current)
            
            prev_state = self.undo_stack.pop()
            self._apply_ui_state(prev_state)
            self.last_saved_state = prev_state
            
            self._update_undo_redo_buttons()
            self.trigger_update()

    def redo_action(self):
        if self.redo_stack:
            current = self._capture_ui_state()
            self.undo_stack.append(current)
            
            next_state = self.redo_stack.pop()
            self._apply_ui_state(next_state)
            self.last_saved_state = next_state
            
            self._update_undo_redo_buttons()
            self.trigger_update()

    def _update_undo_redo_buttons(self):
        if hasattr(self, 'btn_undo'):
            self.btn_undo.configure(state="normal" if len(self.undo_stack) > 0 else "disabled")
            self.btn_redo.configure(state="normal" if len(self.redo_stack) > 0 else "disabled")

    def toggle_queue_visibility(self):
        if self.queue_is_visible:
            self.queue_scroll.pack_forget()
            self.btn_toggle_queue.configure(text="▼ Show Queue")
            self.queue_is_visible = False
        else:
            self.queue_scroll.pack(fill="x", padx=10, pady=(5, 5))
            self.btn_toggle_queue.configure(text="▲ Hide Queue")
            self.queue_is_visible = True

    def _create_labeled_slider(self, parent, title, description, from_, to, steps, default_val, command_cb, is_disabled=False):
        """Clean generator for architectural sliders with concise English technical guidelines[cite: 3, 7]."""
        frm = ctk.CTkFrame(parent, fg_color="transparent")
        frm.pack(fill="x", padx=20, pady=(6, 2))
        
        frm_top = ctk.CTkFrame(frm, fg_color="transparent")
        frm_top.pack(fill="x")
        lbl_title = ctk.CTkLabel(frm_top, text=title, font=ctk.CTkFont(size=12, weight="bold"), text_color=self.c_text_dark if not is_disabled else "gray")
        lbl_title.pack(side="left")
        lbl_val = ctk.CTkLabel(frm_top, text=f"{default_val}", font=ctk.CTkFont(size=11, weight="bold"), text_color=self.c_text_dark if not is_disabled else "gray")
        lbl_val.pack(side="right")
        
        # Concise sub-label text formatting to prevent truncation in CustomTkinter
        lbl_desc = ctk.CTkLabel(frm, text=description, font=ctk.CTkFont(size=10), text_color="#7A7A7A", anchor="w", justify="left")
        lbl_desc.pack(fill="x", pady=(0, 2))
        
        slider = ctk.CTkSlider(
            parent, from_=from_, to=to, height=15, number_of_steps=steps, 
            command=command_cb, progress_color=self.c_magenta,
            state="disabled" if is_disabled else "normal"
        )
        slider.set(default_val)
        slider.pack(padx=20, pady=(0, 6), fill="x")
        
        return slider, lbl_title, lbl_val

    def setup_manual_edit_tab(self):
        tab_manual = self.tabs_container.tab("MANUAL EDIT")
        tab_manual.grid_rowconfigure(0, weight=1) 
        tab_manual.grid_columnconfigure(0, weight=1)
        tab_manual.grid_columnconfigure(1, weight=1)
        
        self.col_left = ctk.CTkScrollableFrame(tab_manual, corner_radius=10, fg_color="transparent")
        self.col_left.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

        # --- SECTION 1: CONTENT & PHYSICS SETUP ---
        ctk.CTkLabel(self.col_left, text="CONTENT SETUP", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta).pack(anchor="w", padx=20, pady=(10, 5))

        self.margin_slider, _, self.margin_val_label = self._create_labeled_slider(
            self.col_left, "Goal Line Margin (px)", 
            "Spacing in px between adjacent elements on the Goal Lines.",
            0, 600, 600, 20, self.update_margin_label
        )
        self.margin_val_label.configure(text="20 px")

        # v0.61: margen independiente para la Touchline
        self.margin_tl_slider, _, self.margin_tl_val_label = self._create_labeled_slider(
            self.col_left, "Touchline Margin (px)",
            "Spacing in px between adjacent elements on the Touchline.",
            0, 600, 600, 20, self.update_margin_tl_label
        )
        self.margin_tl_val_label.configure(text="20 px")

        self.offset_slider, _, self.offset_val_label = self._create_labeled_slider(
            self.col_left, "Goal Line Offset (%)", 
            "Shifts goal center anchor left or right.",
            0, 50, 50, 0, self.update_offset_label
        )
        self.offset_val_label.configure(text="0 %")

        self.stretch_slider, _, self.stretch_val_label = self._create_labeled_slider(
            self.col_left, "Squash Limit (%)", 
            "Max compression to absorb empty gaps.",
            0, 25, 25, 0, self.update_stretch_label
        )
        self.stretch_val_label.configure(text="0 %")
        
        self.squash_logos_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.col_left, text="Allow squashing Static Logos", font=ctk.CTkFont(size=11), variable=self.squash_logos_var, command=self.trigger_update, fg_color=self.c_magenta).pack(padx=20, pady=(3, 3), fill="x")

        self.stretch_fit_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.col_left, text="Stretch Fit Per (Eliminate black gaps)", font=ctk.CTkFont(size=11), variable=self.stretch_fit_var, command=self.on_stretch_fit_toggle, fg_color=self.c_magenta).pack(padx=20, pady=(0, 3), fill="x")

        self.corner_logo_var = ctk.BooleanVar(value=False)
        self.chk_corner_logo = ctk.CTkCheckBox(self.col_left, text="Corner Logo (Split logos at extreme corners)", font=ctk.CTkFont(size=11), variable=self.corner_logo_var, command=self.trigger_update, fg_color=self.c_magenta)
        self.chk_corner_logo.pack(padx=20, pady=(0, 3), fill="x")

        self.chk_show_cb = ctk.CTkCheckBox(self.col_left, text="Show Center Board Layer", font=ctk.CTkFont(size=11), variable=self.show_cb_var, command=self.trigger_update, fg_color=self.c_magenta)
        self.chk_show_cb.pack(padx=20, pady=(0, 3), fill="x")

        self.logo_under_var = ctk.BooleanVar(value=False)
        self.chk_logo_under = ctk.CTkCheckBox(self.col_left, text="Logo Under Mode (Static logos beneath video)", font=ctk.CTkFont(size=11), variable=self.logo_under_var, command=self.toggle_logo_under_mode, fg_color=self.c_magenta)
        self.chk_logo_under.pack(padx=20, pady=(0, 5), fill="x")

        # --- SECTION 2: STANDARD LOGOS DISTRIBUTION ---
        ctk.CTkLabel(self.col_left, text="STANDARD LOGOS DISTRIBUTION", font=ctk.CTkFont(size=12, weight="bold"), text_color="#A9A9A9").pack(anchor="w", padx=20, pady=(10, 2))

        self.qty_goal_slider, self.lbl_qty_goal_title, self.qty_goal_label = self._create_labeled_slider(
            self.col_left, "Goals Logo Qty", 
            "Logos centered in Goal Lines (Z1/Z5).",
            0, 20, 20, 0, lambda v: self.update_qty_labels(v, "goal")
        )
        self.qty_goal_label.configure(text="Auto")

        self.qty_corner_slider, self.lbl_qty_corner_title, self.qty_corner_label = self._create_labeled_slider(
            self.col_left, "Corner Logos Qty", 
            "Logos placed at corners and zone borders.",
            0, 20, 20, 0, lambda v: self.update_qty_labels(v, "corner")
        )
        self.qty_corner_label.configure(text="Auto")

        self.qty_cb_slider, self.lbl_qty_cb_title, self.qty_cb_label = self._create_labeled_slider(
            self.col_left, "Centerboard Logos Qty", 
            "Logos flanking Center Board left/right (Z3).",
            0, 20, 20, 0, lambda v: self.update_qty_labels(v, "cb")
        )
        self.qty_cb_label.configure(text="Auto")

        self.qty_tl_slider, self.lbl_qty_tl_title, self.qty_tl_label = self._create_labeled_slider(
            self.col_left, "Touchline Logos Qty", 
            "Logo spacers between Touchline videos.",
            0, 20, 20, 0, lambda v: self.update_qty_labels(v, "tl")
        )
        self.qty_tl_label.configure(text="Auto")

        # --- SECTION 3: ANIM ZONES DISTRIBUTION ---
        ctk.CTkLabel(self.col_left, text="ANIM ZONES DISTRIBUTION", font=ctk.CTkFont(size=12, weight="bold"), text_color="#A9A9A9").pack(anchor="w", padx=20, pady=(10, 2))

        self.max_anim_gl_slider, self.lbl_max_anim_gl_title, self.max_anim_gl_label = self._create_labeled_slider(
            self.col_left, "Max Anim Zones GL", 
            "Max Goal Line videos; rest filled with logos.",
            0, 10, 10, 0, lambda v: self.update_qty_anim_labels(v, "anim_gl")
        )
        self.max_anim_gl_label.configure(text="Auto")

        self.max_anim_tl_slider, self.lbl_max_anim_tl_title, self.max_anim_tl_label = self._create_labeled_slider(
            self.col_left, "Max Anim Zones TL", 
            "Max Touchline videos; rest filled with logos.",
            0, 10, 10, 0, lambda v: self.update_qty_anim_labels(v, "anim_tl")
        )
        self.max_anim_tl_label.configure(text="Auto")

        # --- SECTION 4: LOGO UNDER DISTRIBUTION ---
        ctk.CTkLabel(self.col_left, text="LOGO UNDER DISTRIBUTION", font=ctk.CTkFont(size=12, weight="bold"), text_color="#A9A9A9").pack(anchor="w", padx=20, pady=(10, 2))

        self.qty_gl_under_slider, self.lbl_qty_gl_under_title, self.qty_gl_under_label = self._create_labeled_slider(
            self.col_left, "GL Logos Under (Per Zone Z1/Z5)", 
            "Equidistant logo grid in Goal Lines.",
            1, 60, 59, 1, lambda v: self.update_qty_under_labels(v, "gl_under"), is_disabled=True
        )
        self.qty_gl_under_label.configure(text="1 Logos")

        self.qty_tl_under_slider, self.lbl_qty_tl_under_title, self.qty_tl_under_label = self._create_labeled_slider(
            self.col_left, "TL Logos Under (Per Zone Z2/Z4)", 
            "Equidistant logo grid in Touchlines.",
            1, 60, 59, 1, lambda v: self.update_qty_under_labels(v, "tl_under"), is_disabled=True
        )
        self.qty_tl_under_label.configure(text="1 Logos")

        # --- SECTION 5: RIGHT COLUMN (SYSTEM FILE SETUP) ---
        self.col_right = ctk.CTkScrollableFrame(tab_manual, corner_radius=10, fg_color="transparent")
        self.col_right.grid(row=0, column=1, sticky="nsew", padx=5, pady=5)

        # --- STADIUM PARAMETERS (MOVIDO DESDE COL_LEFT) ---
        ctk.CTkLabel(self.col_right, text="STADIUM PARAMETERS", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta).pack(anchor="w", padx=20, pady=(5, 5))
        fields_strip = [("Total Width (px)", "Width"), ("Height (px)", "Height"), ("Goal Line Size (px)", "Goal Line Size"), ("Center Board Width (px)", "Center Board Width")]
        for label_text, key in fields_strip:
            ctk.CTkLabel(self.col_right, text=label_text, anchor="w", font=ctk.CTkFont(size=11, weight="bold")).pack(padx=20, pady=(1, 0), fill="x")
            entry = ctk.CTkEntry(self.col_right, height=22)
            entry.insert(0, str(self.stadium_data["Custom"][key]))
            entry.pack(padx=20, pady=(0, 3), fill="x")
            entry.bind("<KeyRelease>", self.trigger_update)
            entry.bind("<FocusOut>", self.save_history_state)
            entry.bind("<Return>", self.save_history_state)
            self.inputs[key] = entry

        # --- SYSTEM FILE SETUP ---
        ctk.CTkLabel(self.col_right, text="SYSTEM FILE SETUP", font=ctk.CTkFont(size=14, weight="bold"), text_color=self.c_magenta).pack(anchor="w", padx=20, pady=(5, 5))
        fields_system = [("Canvas Width (px)", "Canvas Width"), ("Canvas Height (px)", "Canvas Height"), ("Right Mask (px)", "Canvas Mask"), ("Offset X (px)", "Offset X"), ("Offset Y (px)", "Offset Y"), ("Frame Rate (fps)", "FPS")]
        for label_text, key in fields_system:
            ctk.CTkLabel(self.col_right, text=label_text, anchor="w", font=ctk.CTkFont(size=11, weight="bold")).pack(padx=20, pady=(1, 0), fill="x")
            entry = ctk.CTkEntry(self.col_right, height=22)
            entry.insert(0, str(self.stadium_data["Custom"][key]))
            entry.pack(padx=20, pady=(0, 3), fill="x")
            entry.bind("<KeyRelease>", self.trigger_update)
            self.inputs[key] = entry

        self.amplify_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.col_right, 
            text="⚡ AMPLIFY Mode (Flexbox layout for 2-row strips)", 
            font=ctk.CTkFont(size=11, weight="bold"), 
            variable=self.amplify_var, 
            command=self.on_amplify_toggle, 
            fg_color=self.c_magenta,
            hover_color="#D1007A"
        ).pack(padx=20, pady=(15, 5), anchor="w", fill="x")

        self.invert_order_var = ctk.BooleanVar(value=False)

        # --- BINDINGS DE HISTORIAL PARA SLIDERS ---
        self.margin_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.margin_tl_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.offset_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.stretch_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_goal_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_corner_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_cb_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_tl_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.max_anim_gl_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.max_anim_tl_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_gl_under_slider.bind("<ButtonRelease-1>", self.save_history_state)
        self.qty_tl_under_slider.bind("<ButtonRelease-1>", self.save_history_state)

        # --- RE-RUTEO DE CHECKBOXES PARA HISTORIAL ---
        self.chk_corner_logo.configure(command=lambda: [self.trigger_update(), self.save_history_state()])
        self.chk_show_cb.configure(command=lambda: [self.trigger_update(), self.save_history_state()])
        self.chk_logo_under.configure(command=lambda: [self.toggle_logo_under_mode(), self.save_history_state()])
        
        # Debemos actualizar los comandos de los checkboxes que se crearon al vuelo
        for child in self.col_left.winfo_children() + self.col_right.winfo_children():
            if isinstance(child, ctk.CTkCheckBox):
                original_cmd = child.cget("command")
                if original_cmd not in [None, ""]:
                    # Interceptamos el comando para encadenar el guardado de historial
                    child.configure(command=lambda cmd=original_cmd: [cmd(), self.save_history_state()])
        
        ctk.CTkCheckBox(
            self.col_right, 
            text="🔄 Invert Order / Mirror Flip", 
            font=ctk.CTkFont(size=11), 
            variable=self.invert_order_var, 
            command=self.trigger_update, 
            fg_color=self.c_magenta,
            hover_color="#D1007A"
        ).pack(padx=20, pady=(0, 5), anchor="w", fill="x")

    def setup_ai_assistant_tab(self):
        tab_ai = self.tabs_container.tab("AI ASSISTANT")
        tab_ai.grid_columnconfigure(0, weight=1)
        tab_ai.grid_rowconfigure(0, weight=1)

        self.chat_history = ctk.CTkTextbox(
            tab_ai, 
            wrap="word", 
            fg_color="#dbdbdb", 
            text_color=self.c_text_dark, 
            corner_radius=10
        )
        self.chat_history.grid(row=0, column=0, padx=10, pady=(10, 5), sticky="nsew")
        self.chat_history.configure(state="disabled")

        input_frame = ctk.CTkFrame(tab_ai, fg_color="transparent")
        input_frame.grid(row=1, column=0, padx=10, pady=(5, 10), sticky="ew")
        input_frame.grid_columnconfigure(0, weight=1)

        self.chat_input = ctk.CTkEntry(input_frame, placeholder_text="Ask the AI for a layout...", height=40)
        self.chat_input.grid(row=0, column=0, padx=(0, 10), sticky="ew")
        self.chat_input.bind("<Return>", lambda e: self.send_ai_message())

        self.btn_send_ai = ctk.CTkButton(input_frame, text="Send", width=70, height=40, fg_color=self.c_magenta, hover_color="#D1007A", font=ctk.CTkFont(weight="bold"), command=self.send_ai_message)
        self.btn_send_ai.grid(row=0, column=1)

    def init_ai_session(self):
        if not api_key:
            self.append_chat("System", "⚠️ GEMINI_API_KEY not found. AI is disabled.")
            self.chat_input.configure(state="disabled")
            self.btn_send_ai.configure(state="disabled")
            return

        system_instruction = """
        IDENTITY AND OBJECTIVE
        You are Archie, the Senior AI Architecture Assistant for AIM Auto Arch.
        Your mission is to translate user natural language requests into optimal LED canvas layout configurations, understanding the physical and mathematical constraints of stadium display engines.

        CORE ENGINE PHYSICS & RULES:
        1. INDIRECT ANIM ZONE GENERATION (Crucial): You CANNOT directly create or set the quantity of "Anim Zones". The Python mathematical engine generates them automatically by filling remaining empty space after placing Static Logos.
        2. LOGOS AS SPACERS: To add Anim Zones, you MUST REDUCE static logo quantities (`qty_goal`, `qty_tl`, etc.) to free up physical pixels. To create visual separators between Anim Zones, you MUST INCREASE logo quantities. For example, to get "2 logos between animations in touchline", set `qty_tl`: 2.
        3. SPACING & MARGINS: To add more empty/black space or distance between elements, INCREASE `margin` (e.g., from 20 to 40 or 60 px). To compact elements, DECREASE `margin`. `margin` is the Goal Line spacing and `margin_tl` the Touchline spacing: change only the one the user refers to (both if unspecified).
        4. STRETCH FIT vs. GAPS: If the user explicitly asks to eliminate gaps, remove black spaces, or "stretch fit", set `stretch_fit_per`: true and MANDATORILY set `stretch_limit`: 0.
        5. AMPLIFY MODE (Symmetry & Alternation): When `amplify`: true, traditional zone rules are disabled. The engine uses a Flexbox-like alternating distribution.
           - To change the visual sequence or flip sides symmetrically, toggle or set `invert_order`: true.
           - NEVER activate `logo_under` or modify `qty_tl_under`/`qty_gl_under` while in AMPLIFY mode.
        6. LOGO UNDER MODE (Equidistant Grid Override): Only use when the user wants static logos frozen or animated *underneath* the main video feed. 
           - CRITICAL: When `logo_under`: true is active, standard static logo distributions are completely wiped and replaced by an equidistant grid across the Full Strip.
           - PER-ZONE QUANTITIES: `qty_gl_under` controls logos EXACTLY for Zone 1 (and identically for Zone 5). `qty_tl_under` controls logos EXACTLY for Zone 2 (and identically for Zone 4). Do NOT treat them as total sums.
           - CORNER LOGOS: When in Logo Under mode, `corner_logo` MUST remain false (disabled) and `qty_corner` at 0 unless the user explicitly requests corner logos.
           - DENSITY RULE: By default, aim for ~80% density of physical capacity so the space-around 50% edge margins are clearly visible.
        7. CONSERVATION OF STATE: Review the [STATE] block provided in the user prompt. ONLY modify parameters that are necessary to fulfill the user's request or solve spatial overlap warnings.
        8. ANIM ZONES LIMITS: To explicitly cap the number of video animations displayed in Goal Lines or Touchlines, modify `max_anim_gl` or `max_anim_tl` (0 = Auto/No limit, 1-10 = explicit limit). When capped, the engine automatically fills remaining space with static logos acting as spacers.

        CRITICAL OUTPUT FORMAT:
        You are a strict configuration API. Return a MINIMAL valid JSON object containing ONLY the keys that need to be modified from the current state. Do NOT use markdown code blocks if possible, just pure JSON.
        Valid keys you can include: "qty_goal", "qty_corner", "qty_cb", "qty_tl", "margin", "margin_tl", "stretch_limit", "squash_logos", "stretch_fit_per", "gl_offset", "corner_logo", "logo_under", "qty_gl_under", "qty_tl_under", "show_cb", "amplify", "invert_order", "max_anim_gl", "max_anim_tl", "explicacion".
        
        The "explicacion" key MUST be included: a single, clear, professional sentence explaining the architectural change made.
        """

        try:
            self.ai_client = genai.Client(api_key=api_key)
            self.ai_config = types.GenerateContentConfig(
                system_instruction=system_instruction,
                response_mime_type="application/json" 
            )
            self.append_chat("Archie", "Hi! I'm Archie. How can I help you?")
        except Exception as e:
            self.append_chat("System Error", f"⚠️ Failed to initialize AI: {str(e)}")

    def _arch_key(self):
        """v0.66: lo que define la arquitectura por defecto: estadio, logo y ANIM ZONES cargados."""
        a = self.archivos_cargados or {}
        anims = tuple(a.get("anim_zones") or [a.get("anim_zone")])
        return (self.stadium_selector_global.get(), a.get("static_logo"), anims)

    def on_create_architecture(self):
        """1er click (o estadio nuevo): arquitectura por defecto. Después: aplica los ajustes actuales a la preview."""
        if self.solver_busy: return  # un render de preview en curso sí se puede sustituir
        stadium = self.stadium_selector_global.get()
        # v0.66: también si han cambiado el logo o las ANIM ZONES (p. ej. Mastercard -> PlayStation)
        if not self.architecture_created or stadium != self.arch_stadium or self._arch_key() != self.arch_key:
            self.auto_create_architecture()
        else:
            self.start_preview_render()

    def auto_create_architecture(self):
        self._cancel_auto_update()
        self.render_gen += 1  # descarta cualquier render de preview anterior
        self.solver_gen += 1
        self.solver_busy = True
        self.btn_create_arch.configure(state="disabled", text="✨ Calculating Internally...")
        self.reset_zooms_and_pan()
        self.show_preview_overlay("Calculating default architecture...")
        self._start_thread(self._solver_thread, self.solver_gen)

    def _solver_thread(self, gen):
        try:
            self._native_auto_solver(gen)
        except Exception as e:
            traceback.print_exc()
            log_error(f"Auto-Solver falló: {e}")
            self.after(0, self._on_solver_error, gen, e)

    def _on_solver_error(self, gen, e):
        if gen != self.solver_gen: return  # cancelado
        self.solver_busy = False
        self._on_preview_error(self.render_gen, e)

    def _on_solver_done(self, gen, slider_args, msg):
        if gen != self.solver_gen: return  # el usuario pulsó Cancel: no tocar nada
        self.solver_busy = False
        self.architecture_created = True
        self.arch_stadium = self.stadium_selector_global.get()
        self.arch_key = self.solver_key = self._arch_key()   # v0.66
        self._actualizar_sliders_desde_ia(*slider_args)
        self.append_chat("System AI", msg)
        self.start_preview_render()
        if getattr(self, "campaign_panel", None): self.campaign_panel.on_solver_done(msg)  # v0.58

    def _native_auto_solver(self, gen=None):
        # v0.58: el algoritmo vive en auto_solver.resolver (lo usa también el export de campañas sin interfaz)
        w_total = int(self.inputs["Width"].get() or 0)
        h_total = int(self.inputs["Height"].get() or 0)
        goal_line = int(self.inputs["Goal Line Size"].get() or 0)
        cb_width = int(self.inputs["Center Board Width"].get() or 0)
        margin = int(self.margin_slider.get())
        base = {
            "gl_offset": int(self.offset_slider.get()),
            "logo_under": self.logo_under_var.get(),
            "corner_logo": self.corner_logo_var.get(),
            "qty_gl_under": int(self.qty_gl_under_slider.get()),
            "qty_tl_under": int(self.qty_tl_under_slider.get()),
            "show_cb": self.show_cb_var.get(),
            "amplify": self.amplify_var.get(),
            "invert_order": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
            "max_anim_gl": int(self.max_anim_gl_slider.get()),
            "max_anim_tl": int(self.max_anim_tl_slider.get()),
            "margin_tl": int(self.margin_tl_slider.get()),               # v0.61
            "edge_case": getattr(self, "edge_case_actual", None),        # v0.61: respeta las zonas negras
        }
        best_config, msg = resolver_arquitectura(w_total, h_total, goal_line, cb_width, margin, self.archivos_cargados, base)

        self.current_hide_edges = best_config["hide_extreme_edges"]
        self.current_under_hotspots = best_config.get("under_hotspots", True)  # v0.61
        self.current_even_spacing = best_config.get("even_spacing", True)      # v0.62

        slider_args = (
                   best_config["qty_goal"], best_config["qty_corner"], 
                   best_config["qty_cb"], best_config["qty_tl"], 
                   best_config["stretch_limit"], best_config["squash_logos"], 
                   self.stretch_fit_var.get(),
                   best_config["gl_offset"], best_config["corner_logo"],
                   best_config["logo_under"], best_config["qty_gl_under"], best_config["qty_tl_under"],
                   best_config.get("show_cb", True), best_config["amplify"],
                   getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
                   best_config.get("max_anim_gl", 0), best_config.get("max_anim_tl", 0),
                   margin, False, best_config.get("margin_tl", margin))  # v0.61: conserva los márgenes usados
        
        self.preview_enabled = True 
        
        # Sliders, chat y preview se aplican juntos en el hilo principal (y nada si se canceló)
        self.after(0, self._on_solver_done, gen, slider_args, msg)

    def aplicar_parametros_ia(self, qty_goal: int, qty_corner: int, qty_cb: int, qty_tl: int, stretch_limit: int, squash_logos: bool, stretch_fit_per: bool, gl_offset: int, corner_logo: bool, logo_under: bool = False, qty_gl_under: int = 1, qty_tl_under: int = 1, show_cb: bool = True, amplify: bool = False, invert_order: bool = False, max_anim_gl: int = 0, max_anim_tl: int = 0, margin: int = 20, margin_tl: int = -1) -> dict:
        self.reset_zooms_and_pan()
        config_prueba = {
            "stretch_limit": stretch_limit, "squash_logos": squash_logos, "stretch_fit_per": stretch_fit_per,
            "qty_goal": qty_goal, "qty_corner": qty_corner, "qty_cb": qty_cb, "qty_tl": qty_tl, 
            "gl_offset": gl_offset, "corner_logo": corner_logo,
            "hide_extreme_edges": getattr(self, "current_hide_edges", False),
            "logo_under": logo_under, "qty_gl_under": qty_gl_under, "qty_tl_under": qty_tl_under,
            "show_cb": show_cb, "amplify": amplify,
            "max_anim_gl": max_anim_gl, "max_anim_tl": max_anim_tl,
            "margin_tl": int(margin_tl) if margin_tl is not None and int(margin_tl) >= 0 else int(margin),
            "under_hotspots": getattr(self, "current_under_hotspots", True),
            "even_spacing": getattr(self, "current_even_spacing", True),   # v0.62: el chat también reparte el espacio
            "edge_case": getattr(self, "edge_case_actual", None),
        }
        
        w_total = int(self.inputs["Width"].get() or 0)
        h_total = int(self.inputs["Height"].get() or 0)
        goal_line = int(self.inputs["Goal Line Size"].get() or 0)
        cb_width = int(self.inputs["Center Board Width"].get() or 0)
        margin_simulado = int(margin)

        try:
            datos = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin_simulado, self.archivos_cargados, config_prueba)
            warnings = datos.get("warnings", []) if datos else []
            
            if "goal_overlap" in warnings or "touchline_overlap" in warnings:
                return {
                    "status": "OVERLAP ERROR",
                    "mensaje": "You have requested to add more elements than can fit. To fulfill your instructions, we need to modify the rest of the elements to avoid overlapping. Are you sure you want to proceed by indicating what to sacrifice?"
                }
            
            self.current_even_spacing = True  # v0.62: lo que pide el chat se reparte siempre con espaciado uniforme
            # v0.66: si el auto-solver aún no ha calculado esta arquitectura (sliders por defecto o heredados de
            # otro logo/estadio) y el chat no ha tocado los logos del goal, prueba a meter los que pide el logo
            extra = ""
            if self.solver_key != self._arch_key() and int(qty_goal) == int(self.qty_goal_slider.get()):
                conf2, msg_goal = completar_goal(w_total, h_total, goal_line, cb_width, margin_simulado,
                                                 self.archivos_cargados, config_prueba)
                if msg_goal:
                    qty_goal, gl_offset, stretch_limit = conf2["qty_goal"], conf2["gl_offset"], conf2["stretch_limit"]
                    self.after(0, self.append_chat, "System AI", msg_goal)
                    extra = (f" Also, the goal now has {qty_goal} logos because they fit automatically "
                             "(tell the user).")
            self.after(0, self._actualizar_sliders_desde_ia, qty_goal, qty_corner, qty_cb, qty_tl, stretch_limit, squash_logos, stretch_fit_per, gl_offset, corner_logo, logo_under, qty_gl_under, qty_tl_under, show_cb, amplify, invert_order, max_anim_gl, max_anim_tl, margin_simulado, True, config_prueba["margin_tl"])
            
            return {
                "status": "SUCCESS", 
                "mensaje": "The parameters fit perfectly and have been applied to the interface." + extra + " Stop here and answer the user confirming the action."
            }
        except Exception as e:
            traceback.print_exc()
            log_error(f"Error in aplicar_parametros_ia: {e}")
            return {"status": "ERROR", "mensaje": f"Math engine failed: {e}"}
    
    def _actualizar_sliders_desde_ia(self, qty_goal, qty_corner, qty_cb, qty_tl, stretch_limit, squash_logos, stretch_fit_per, gl_offset, corner_logo, logo_under=False, qty_gl_under=1, qty_tl_under=1, show_cb=True, amplify=False, invert_order=False, max_anim_gl=0, max_anim_tl=0, margin=20, render_now=False, margin_tl=None):
        if hasattr(self, "invert_order_var"):
            self.invert_order_var.set(invert_order)
            
        self.margin_slider.set(margin)
        self.update_margin_label(margin)
        if margin_tl is not None:  # v0.61 (None: el TL no se toca)
            self.margin_tl_slider.set(margin_tl)
            self.update_margin_tl_label(margin_tl)
            
        self.qty_goal_slider.set(qty_goal)
        self.qty_corner_slider.set(qty_corner)
        self.qty_cb_slider.set(qty_cb)
        self.qty_tl_slider.set(qty_tl)
        self.stretch_slider.set(stretch_limit)
        self.squash_logos_var.set(squash_logos)
        self.stretch_fit_var.set(stretch_fit_per)
        self.offset_slider.set(gl_offset)
        self.corner_logo_var.set(corner_logo)
        
        self.logo_under_var.set(logo_under)
        self.qty_gl_under_slider.set(qty_gl_under)
        self.qty_tl_under_slider.set(qty_tl_under)
        self.show_cb_var.set(show_cb)
        self.amplify_var.set(amplify)
        
        self.toggle_logo_under_mode()
        self.on_amplify_toggle()

        self.update_qty_labels(qty_goal, "goal")
        self.update_qty_labels(qty_corner, "corner")
        self.update_qty_labels(qty_cb, "cb")
        self.update_qty_labels(qty_tl, "tl")
        self.update_stretch_label(stretch_limit)
        self.update_offset_label(gl_offset)
        self.update_qty_under_labels(qty_gl_under, "gl_under")
        self.update_qty_under_labels(qty_tl_under, "tl_under")

        self.max_anim_gl_slider.set(max_anim_gl)
        self.max_anim_tl_slider.set(max_anim_tl)
        self.update_qty_anim_labels(max_anim_gl, "anim_gl")
        self.update_qty_anim_labels(max_anim_tl, "anim_tl")
        
        self.trigger_update()

        if render_now:
            # v0.38: los cambios pedidos al chatbot se ven al momento, sin pulsar Create Architecture
            if self.solver_busy: return  # el auto-solver los recogerá al terminar (auto-update)
            self.architecture_created = True
            self.arch_stadium = self.stadium_selector_global.get()
            self.arch_key = self._arch_key()   # v0.66
            self.save_history_state()
            self.start_preview_render()

    def send_ai_message(self):
        user_text = self.chat_input.get().strip()
        if not user_text: return
        self.chat_input.delete(0, 'end')
        self.append_chat("You", user_text)
        
        datos = self.calcular_layout()
        anims_count = 0
        warnings = []
        if datos:
            warnings = datos.get("warnings", [])
            for zona in datos.get("zonas", {}).values():
                for e in zona.get("elementos", []):
                    if e["tipo"] == "anim_zone":
                        anims_count += 1
                        
        estado_mini = {
            "stadium": {
                "w": int(self.inputs["Width"].get() or 0),
                "gl": int(self.inputs["Goal Line Size"].get() or 0),
                "cb": int(self.inputs["Center Board Width"].get() or 0)
            },
            "ui": {
                "margin": int(self.margin_slider.get()),
                "margin_tl": int(self.margin_tl_slider.get()),
                "q_gl": int(self.qty_goal_slider.get()),
                "q_cor": int(self.qty_corner_slider.get()),
                "q_cb": int(self.qty_cb_slider.get()),
                "q_tl": int(self.qty_tl_slider.get()),
                "str_lim": int(self.stretch_slider.get()),
                "str_fit": self.stretch_fit_var.get(),
                "sq": self.squash_logos_var.get(),
                "off": int(self.offset_slider.get()),
                "cor_logo": self.corner_logo_var.get(),
                "amp": self.amplify_var.get(),
                "inv": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
                "logo_under": self.logo_under_var.get(),
                "qty_gl_under": int(self.qty_gl_under_slider.get()),
                "max_a_gl": int(self.max_anim_gl_slider.get()),
                "max_a_tl": int(self.max_anim_tl_slider.get()),
                "qty_tl_under": int(self.qty_tl_under_slider.get())
            },
            "result": {
                "anims_rendered": anims_count,
                "overlaps": warnings
            }
        }
        
        prompt_enriquecido = f"{user_text}\n\n[STATE:{json.dumps(estado_mini, separators=(',', ':'))}]"
        
        self.btn_send_ai.configure(state="disabled", text="...")
        threading.Thread(target=self._ai_worker, args=(prompt_enriquecido,), daemon=True).start()

    def _ai_worker(self, text):
        import time
        max_intentos = 3
        
        for intento in range(max_intentos):
            try:
                response = self.ai_client.models.generate_content(
                    model="gemini-2.5-flash", 
                    contents=text,
                    config=self.ai_config
                )
                
                try:
                    raw_json = response.text.replace('```json', '').replace('```', '').strip()
                    datos_ia = json.loads(raw_json)
                    
                    resultado = self.aplicar_parametros_ia(
                        margin=datos_ia.get("margin", int(self.margin_slider.get())),
                        margin_tl=datos_ia.get("margin_tl", int(self.margin_tl_slider.get())),
                        qty_goal=datos_ia.get("qty_goal", int(self.qty_goal_slider.get())),
                        qty_corner=datos_ia.get("qty_corner", int(self.qty_corner_slider.get())),
                        qty_cb=datos_ia.get("qty_cb", int(self.qty_cb_slider.get())),
                        qty_tl=datos_ia.get("qty_tl", int(self.qty_tl_slider.get())),
                        stretch_limit=datos_ia.get("stretch_limit", int(self.stretch_slider.get())),
                        squash_logos=datos_ia.get("squash_logos", self.squash_logos_var.get()),
                        stretch_fit_per=datos_ia.get("stretch_fit_per", self.stretch_fit_var.get()), 
                        gl_offset=datos_ia.get("gl_offset", int(self.offset_slider.get())),
                        corner_logo=datos_ia.get("corner_logo", self.corner_logo_var.get()),
                        logo_under=datos_ia.get("logo_under", self.logo_under_var.get()),
                        qty_gl_under=datos_ia.get("qty_gl_under", int(self.qty_gl_under_slider.get())),
                        qty_tl_under=datos_ia.get("qty_tl_under", int(self.qty_tl_under_slider.get())),
                        show_cb=datos_ia.get("show_cb", self.show_cb_var.get()),
                        amplify=datos_ia.get("amplify", self.amplify_var.get()),
                        invert_order=datos_ia.get("invert_order", getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get()),
                        max_anim_gl=datos_ia.get("max_anim_gl", int(self.max_anim_gl_slider.get())),
                        max_anim_tl=datos_ia.get("max_anim_tl", int(self.max_anim_tl_slider.get()))
                    )
                    
                    if resultado["status"] == "OVERLAP ERROR":
                        self.after(0, self.append_chat, "System Warning", resultado["mensaje"])
                    else:
                        explicacion = datos_ia.get("explicacion", "Layout adjusted successfully.")
                        self.after(0, self.append_chat, "AI Assistant", explicacion)
                        
                    self.after(0, self._restaurar_botones_ia)
                    return 
                    
                except json.JSONDecodeError:
                    self.after(0, self.append_chat, "System Error", "Formatting error: The AI did not return a valid JSON.")
                    debug(f"Raw AI response: {response.text}")
                    self.after(0, self._restaurar_botones_ia)
                    return
                    
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "503" in error_str or "UNAVAILABLE" in error_str:
                    if intento < max_intentos - 1:
                        espera = 2 ** (intento + 1)
                        debug(f"Google rate limit reached. Silently retrying in {espera}s...")
                        time.sleep(espera)
                        continue
                    else:
                        import re
                        match = re.search(r'retry in (\d+(?:\.\d+)?)s', error_str)
                        segundos = int(float(match.group(1))) if match else 15 
                        self.after(0, self.append_chat, "System Warning", "⚠️ Google servers are experiencing high demand. Cooling down the system...")
                        self.after(0, lambda: self._iniciar_cooldown_ia(segundos))
                        return
                else:
                    self.after(0, self.append_chat, "System Error", f"Critical error: {error_str[:150]}...")
                    self.after(0, self._restaurar_botones_ia)
                    return

    def _restaurar_botones_ia(self):
        self.btn_send_ai.configure(state="normal", text="Send")
        self.btn_create_arch.configure(state="normal", text="✨ Create Architecture")
        self.chat_input.configure(state="normal")

    def _iniciar_cooldown_ia(self, segundos_restantes):
        if segundos_restantes > 0:
            self.btn_send_ai.configure(state="disabled", text=f"Wait {segundos_restantes}s")
            self.btn_create_arch.configure(state="disabled")
            self.chat_input.configure(state="disabled")
            self.after(1000, self._iniciar_cooldown_ia, segundos_restantes - 1)
        else:
            self._restaurar_botones_ia()
            self.append_chat("System", "🟢 System cooled down. AI ready to receive commands.")
            
    def append_chat(self, sender, text):
        self.chat_history.configure(state="normal")
        self.chat_history.insert("end", f"{sender}:\n", "bold")
        self.chat_history.insert("end", f"{text}\n\n")
        self.chat_history.see("end")
        self.chat_history.configure(state="disabled")

    def create_main_drop_zone(self, parent, title, key, row_idx, col_idx):
        col = ctk.CTkFrame(parent, fg_color="transparent")
        col.grid(row=row_idx, column=col_idx, sticky="new", padx=5, pady=2)
        
        header = ctk.CTkFrame(col, fg_color="transparent")
        header.pack(fill="x", pady=(0, 3))
        ctk.CTkLabel(header, text=title, font=ctk.CTkFont(weight="bold", size=12)).pack(side="left")
        ctk.CTkButton(header, text="X", width=22, height=22, fg_color="#C0392B", hover_color="#E74C3C", 
                      font=ctk.CTkFont(weight="bold", size=11), command=lambda k=key: self.clear_main_zone(k)).pack(side="right")
        
        box = ctk.CTkFrame(col, corner_radius=8, border_width=0, fg_color=self.c_light_grey, height=55)
        box.pack_propagate(False)
        box.pack(fill="x", expand=False)
        self.drop_labels[key] = ctk.CTkLabel(box, text="Drop file here", text_color="gray", font=ctk.CTkFont(size=11))
        self.drop_labels[key].place(relx=0.5, rely=0.5, anchor="center")
        box.drop_target_register(DND_FILES)
        box.dnd_bind('<<Drop>>', lambda e, k=key: self.on_drop(e, k))
        return col

    def clear_main_zone(self, key):
        self.archivos_cargados[key] = None
        self.drop_labels[key].configure(text="Drop file here", text_color="gray")
        self.trigger_update()

    def add_anim_zone(self):
        self.archivos_cargados["anim_zones"].append(None)
        self.refresh_anim_zones_ui()

    def remove_anim_zone(self, idx):
        del self.archivos_cargados["anim_zones"][idx]
        if len(self.archivos_cargados["anim_zones"]) == 0:
            self.add_anim_zone()
            self.archivos_cargados["anim_zone"] = None
        else:
            self.archivos_cargados["anim_zone"] = self.archivos_cargados["anim_zones"][0]
            self.refresh_anim_zones_ui()
        self.trigger_update()

    def update_anim_zone(self, event, idx):
        file_path = event.data.strip('{}')
        self.archivos_cargados["anim_zones"][idx] = file_path
        if idx == 0:
            self.archivos_cargados["anim_zone"] = file_path 
            name = os.path.splitext(os.path.basename(file_path))[0]
            self.output_filename_var.set(f"{name}")
        self.check_and_activate_logo_under(file_path)
        if idx == 0:
            self._autoimportar_elements(file_path, "anim_zone")
        self.refresh_anim_zones_ui()
        self.reset_preview_state()
        self.reset_zooms_and_pan()
        self.trigger_update()

    def refresh_anim_zones_ui(self):
        for widget in self.anim_scroll.winfo_children(): widget.destroy()
            
        num_zones = len(self.archivos_cargados["anim_zones"])
        
        for idx, path in enumerate(self.archivos_cargados["anim_zones"]):
            box = ctk.CTkFrame(self.anim_scroll, corner_radius=10, border_width=0, fg_color=self.c_light_grey, height=65)
            box.pack_propagate(False)
            
            margen_inferior = 5 if num_zones > 1 else 0
            box.pack(fill="x", pady=(0, margen_inferior))
            
            btn_x = ctk.CTkButton(box, text="X", width=22, height=22, corner_radius=5, fg_color="#C0392B", hover_color="#E74C3C", font=ctk.CTkFont(size=10, weight="bold"), command=lambda i=idx: self.remove_anim_zone(i))
            btn_x.place(relx=1.0, rely=0.0, anchor="ne", x=-16, y=6)
            
            prefix = f"Zone {chr(65+idx)}\n" if num_zones > 1 else ""
            txt = f"{prefix}Loaded:\n{os.path.basename(path)}" if path else f"{prefix}Drop here .mov / .mp4"
            color = "#2CC985" if path else "gray"
            
            lbl_file = ctk.CTkLabel(box, text=txt, text_color=color, font=ctk.CTkFont(size=11), wraplength=100)
            lbl_file.place(relx=0.5, rely=0.5, anchor="center") 
            
            box.drop_target_register(DND_FILES)
            box.dnd_bind('<<Drop>>', lambda e, i=idx: self.update_anim_zone(e, i))

        if num_zones == 1:
            nueva_altura = 55
        else:
            nueva_altura = min(210, (num_zones * 70) - 5)
            
        self.anim_scroll_wrapper.configure(height=nueva_altura)
        
        if num_zones > 1:
            self.anim_scroll.configure(scrollbar_button_color="#2b2b2b", scrollbar_button_hover_color="#404040")
        else:
            self.anim_scroll.configure(scrollbar_button_color=self.c_white, scrollbar_button_hover_color=self.c_white)

    def on_stretch_fit_toggle(self):
        if self.stretch_fit_var.get():
            self.stretch_slider.set(0)
            self.update_stretch_label(0)
        self.trigger_update()

    def on_amplify_toggle(self):
        if self.amplify_var.get():
            self.btn_toggle_row.pack(side="right", padx=5)
        else:
            self.btn_toggle_row.pack_forget()
            self.preview_row_idx = 1
        self.trigger_update()

    def toggle_preview_row(self):
        if self.preview_row_idx == 1:
            self.preview_row_idx = 2
            self.btn_toggle_row.configure(text="🔄 View: 2ND ROW", fg_color="#E67E22", hover_color="#D35400")
        else:
            self.preview_row_idx = 1
            self.btn_toggle_row.configure(text="🔄 View: 1ST ROW", fg_color="#8E44AD", hover_color="#9B59B6")
        if self.pil_preview_images and not self.preview_dirty:
            self.start_preview_render()
        else:
            self.trigger_update()
    
    def update_margin_tl_label(self, value, *args):
        self.margin_tl_val_label.configure(text=f"{int(value)} px")
        if getattr(self, "logo_under_var", None) and self.logo_under_var.get():
            self.update_qty_under_labels(self.qty_tl_under_slider.get(), "tl_under")
        self.trigger_update()

    def update_margin_label(self, value, *args): 
        self.margin_val_label.configure(text=f"{int(value)} px")
        if getattr(self, "logo_under_var", None) and self.logo_under_var.get():
            self.update_qty_under_labels(self.qty_gl_under_slider.get(), "gl_under")
            self.update_qty_under_labels(self.qty_tl_under_slider.get(), "tl_under")
        self.trigger_update()

    def update_stretch_label(self, value, *args):
        val = int(value)
        color = "#E74C3C" if val > 15 else "white"
        txt = f"{val} % (HIGH SQUASH!)" if val > 15 else f"{val} %"
        self.stretch_val_label.configure(text=txt, text_color=color)
        self.trigger_update()

    def update_offset_label(self, value, *args):
        self.offset_val_label.configure(text=f"{int(value)} %")
        self.trigger_update()

    def update_qty_labels(self, value, qty_type):
        val = int(value)
        txt = f"{val} Logos" if val > 0 else "Auto"
        if qty_type == "goal": self.qty_goal_label.configure(text=txt)
        elif qty_type == "corner": self.qty_corner_label.configure(text=txt)
        elif qty_type == "cb": self.qty_cb_label.configure(text=txt)
        elif qty_type == "tl": self.qty_tl_label.configure(text=txt)
        self.trigger_update()

    def update_qty_anim_labels(self, value, anim_type):
        val = int(value)
        txt = f"{val} Zones" if val > 0 else "Auto"
        if anim_type == "anim_gl": self.max_anim_gl_label.configure(text=txt)
        elif anim_type == "anim_tl": self.max_anim_tl_label.configure(text=txt)
        self.trigger_update()

    def update_qty_under_labels(self, value, qty_type):
        val = int(value)
        max_gl, max_tl = self.get_max_under_logos()
        
        try:
            if qty_type == "gl_under":
                safe_max_gl = max(2, max_gl) 
                if self.qty_gl_under_slider.cget("to") != safe_max_gl:
                    self.qty_gl_under_slider.configure(to=safe_max_gl, number_of_steps=max(1, safe_max_gl-1))
                
                if val > max_gl: val = max_gl; self.qty_gl_under_slider.set(val)
                self.qty_gl_under_label.configure(text=f"{val} Logos (Max: {max_gl})")
                
            elif qty_type == "tl_under":
                safe_max_tl = max(2, max_tl)
                if self.qty_tl_under_slider.cget("to") != safe_max_tl:
                    self.qty_tl_under_slider.configure(to=safe_max_tl, number_of_steps=max(1, safe_max_tl-1))
                    
                if val > max_tl: val = max_tl; self.qty_tl_under_slider.set(val)
                self.qty_tl_under_label.configure(text=f"{val} Logos (Max: {max_tl})")
                
        except Exception as e:
            traceback.print_exc()
            
        self.trigger_update()

    def toggle_logo_under_mode(self):
        is_under = self.logo_under_var.get()
        state_normal = "disabled" if is_under else "normal"
        color_normal = "gray" if is_under else self.c_text_dark
        
        self.qty_goal_slider.configure(state=state_normal)
        self.qty_corner_slider.configure(state=state_normal)
        self.qty_cb_slider.configure(state=state_normal)
        self.qty_tl_slider.configure(state=state_normal)
        self.lbl_qty_goal_title.configure(text_color=color_normal)
        self.lbl_qty_corner_title.configure(text_color=color_normal)
        self.lbl_qty_cb_title.configure(text_color=color_normal)
        self.lbl_qty_tl_title.configure(text_color=color_normal)
        self.qty_goal_label.configure(text_color=color_normal)
        self.qty_corner_label.configure(text_color=color_normal)
        self.qty_cb_label.configure(text_color=color_normal)
        self.qty_tl_label.configure(text_color=color_normal)

        state_under = "normal" if is_under else "disabled"
        color_under = self.c_text_dark if is_under else "gray"
        self.qty_gl_under_slider.configure(state=state_under)
        self.qty_tl_under_slider.configure(state=state_under)
        self.lbl_qty_gl_under_title.configure(text_color=color_under)
        self.lbl_qty_tl_under_title.configure(text_color=color_under)
        self.qty_gl_under_label.configure(text_color=color_under)
        self.qty_tl_under_label.configure(text_color=color_under)
        
        if is_under:
            self.update_qty_under_labels(self.qty_gl_under_slider.get(), "gl_under")
            self.update_qty_under_labels(self.qty_tl_under_slider.get(), "tl_under")
            self.max_anim_gl_slider.configure(state=state_normal)
            self.max_anim_tl_slider.configure(state=state_normal)
            self.lbl_max_anim_gl_title.configure(text_color=color_normal)
            self.lbl_max_anim_tl_title.configure(text_color=color_normal)
            self.max_anim_gl_label.configure(text_color=color_normal)
            self.max_anim_tl_label.configure(text_color=color_normal)
        self.trigger_update()

    def check_and_activate_logo_under(self, file_path):
        if not file_path: return
        name = os.path.basename(file_path).lower().replace(" ", "").replace("_", "").replace("-", "")
        
        if "logounder" in name:
            if not self.logo_under_var.get():
                self.logo_under_var.set(True)
                self.chk_logo_under.select() 
                
                # --- BLINDAJE ARCHIE: Apagar Corner Logo por defecto en Logo Under ---
                self.corner_logo_var.set(False)
                self.chk_corner_logo.deselect()
                self.qty_corner_slider.set(0)
                
                self.update_idletasks() 
                
                max_gl, max_tl = self.get_max_under_logos()
                default_gl = max(1, int(max_gl * 0.8))
                default_tl = max(1, int(max_tl * 0.8))
                
                self.update_qty_under_labels(default_gl, "gl_under")
                self.update_qty_under_labels(default_tl, "tl_under")
                
                self.qty_gl_under_slider.set(default_gl)
                self.qty_tl_under_slider.set(default_tl)
                
                self.toggle_logo_under_mode()

    def get_max_under_logos(self):
        try:
            w_total = int(self.inputs["Width"].get() or 0)
            h_total = int(self.inputs["Height"].get() or 0)
            goal_line = int(self.inputs["Goal Line Size"].get() or 0)
            cb_width = int(self.inputs["Center Board Width"].get() or 0)
            margin = int(self.margin_slider.get())
            logo_path = self.archivos_cargados.get("static_logo")
            
            # --- BLINDAJE ARCHIE: Fallback realista por zona en vez de 60 ---
            if not logo_path: return 15, 15
            
            img = self.obtener_frame_pil(logo_path, h_target=h_total)
            logo_w = img.width if img.width > 0 else int(h_total * 1.5)
            logo_w = max(1, logo_w) 
            
            cb_start = (w_total / 2) - (cb_width / 2)
            z2_w = cb_start - goal_line
            
            denom = logo_w + margin
            margin_tl = int(self.margin_tl_slider.get())  # v0.61
            if denom > 0:
                max_gl = max(1, math.floor((goal_line + margin) / denom))
                max_tl = max(1, math.floor((z2_w + margin_tl) / (logo_w + margin_tl)))
            else:
                max_gl = max(1, math.floor(goal_line / logo_w))
                max_tl = max(1, math.floor(z2_w / logo_w))
            return max_gl, max_tl

        except Exception as e: 
            traceback.print_exc()
            return 15, 15

    def load_stadium_library(self):
        data, self.stadium_source = cargar_estadios(CONFIG_DIR, fallback_dirs=[os.path.join(BASE_DIR, "Config")])
        return data

    def refresh_stadium_library(self):
        """Relee la hoja en un hilo aparte para no congelar la UI."""
        self.btn_refresh_stadiums.configure(state="disabled", text="…")

        def worker():
            data, source = cargar_estadios(CONFIG_DIR, fallback_dirs=[os.path.join(BASE_DIR, "Config")])
            self.after(0, lambda: self._apply_stadium_library(data, source))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_stadium_library(self, data, source):
        self.btn_refresh_stadiums.configure(state="normal", text="↺")
        if source != "sheets":
            print(f"  [WARN] Refresh sin conexión a Sheets; se mantiene la librería actual ({source}).")
            return

        previous = self.stadium_selector_global.get()
        self.stadium_data = data
        self.stadium_source = source
        self.stadium_search_var.set("")
        self.stadium_selector_global.configure(values=list(data.keys()))
        self.batch_manager.sync_stadiums()

        if previous in data:
            self.on_stadium_change(previous)  # recarga valores por si cambiaron en la hoja
        else:
            self.stadium_selector_global.set("Custom")
            self.on_stadium_change("Custom")
    
    def filter_stadium_dropdown(self, *args):
        query = self.stadium_search_var.get().lower().strip()
        all_stadiums = list(self.stadium_data.keys())
        
        if not query:
            filtered = all_stadiums
        else:
            filtered = [s for s in all_stadiums if query in s.lower()]
            if "Custom" not in filtered and "custom" in query:
                filtered.insert(0, "Custom")
                
        if not filtered:
            filtered = ["No matches found"]
            
        self.stadium_selector_global.configure(values=filtered)

    def on_search_return(self, event=None):
        query = self.stadium_search_var.get().lower().strip()
        if not query: return
        all_stadiums = list(self.stadium_data.keys())
        matches = [s for s in all_stadiums if query in s.lower()]
        if matches:
            first_match = matches[0]
            self.stadium_selector_global.set(first_match)
            self.on_stadium_change(first_match)

    def on_stadium_change(self, choice):
        if choice == "No matches found" or choice not in self.stadium_data: return
        panel = getattr(self, "campaign_panel", None)
        if panel: panel.before_stadium_change()  # v0.58: la campaña activa pertenece al estadio anterior
        
        if hasattr(self, "stadium_search_var") and self.stadium_search_var.get() != "":
            self.stadium_search_var.set("")
            
        data = self.stadium_data[choice]
        is_custom = (choice == "Custom")
        # v0.52: edge case (Config/stadium_overrides.json): tramos del system file, zonas negras y Goal Position
        self.edge_case_actual = None if is_custom else data.get("Edge Case")
        if self.edge_case_actual:
            debug(f"Edge case activo: {choice} ({len(self.edge_case_actual.get('tramos') or [])} tramos, "
                  f"zonas negras {self.edge_case_actual.get('zonas_negras')})")
        
        # v0.51: la hoja trae columna Amplify (y/n); el nombre con "AMPLIFY" se mantiene por compatibilidad
        self.amplify_var.set(bool(data.get("Amplify")) or "AMPLIFY" in choice.upper())
        self.on_amplify_toggle()
        
        for key, entry in self.inputs.items():
            entry.configure(state="normal")
            entry.delete(0, "end")
            entry.insert(0, str(data.get(key, "0")))
            if not is_custom: 
                entry.configure(state="disabled", text_color=self.c_text_muted, fg_color=self.c_light_grey)
            else: 
                entry.configure(state="normal", text_color=self.c_text_dark, fg_color=self.c_white)
        self.trigger_update()
        if panel: panel.after_stadium_change()

    def select_output_path(self):
        if folder := ctk.filedialog.askdirectory(title="Select Destination"): self.output_path_var.set(folder)

    def on_drop(self, event, key):
        file_path = event.data.strip('{}')
        self.archivos_cargados[key] = file_path
        self.drop_labels[key].configure(text=f"Loaded:\n{os.path.basename(file_path)}", text_color="#2CC985")
        if key == "center_board":
            self.show_cb_var.set(True)
        self.check_and_activate_logo_under(file_path)
        if self._autoimportar_elements(file_path, key):
            self.refresh_anim_zones_ui()
        self.reset_preview_state()
        self.reset_zooms_and_pan()
        self.trigger_update()

    def _autoimportar_elements(self, file_path, zona_dropeada):
        """v0.34: al dropear un ELEMENT etiquetado (ANIM/BG/LOGO/CB), carga el resto
        de ELEMENTS de su carpeta en sus zonas. Si hay varios de un tipo, esa zona
        queda vacía para dropear a mano. Devuelve True si tocó la Zone A de Anim."""
        try:
            encontrados = buscar_elements_hermanos(file_path)
        except Exception as e:
            log_error(f"Autoimport: {e}")
            return False

        toco_anim = False
        for tipo, ruta in encontrados.items():
            if tipo == zona_dropeada:
                continue
            if tipo == "anim_zone":
                if not self.archivos_cargados["anim_zones"]:
                    self.archivos_cargados["anim_zones"].append(None)
                self.archivos_cargados["anim_zones"][0] = ruta
                self.archivos_cargados["anim_zone"] = ruta
                if ruta:
                    self.output_filename_var.set(os.path.splitext(os.path.basename(ruta))[0])
                toco_anim = True
            else:
                self.archivos_cargados[tipo] = ruta
                if ruta:
                    self.drop_labels[tipo].configure(text=f"Loaded:\n{os.path.basename(ruta)}", text_color="#2CC985")
                    if tipo == "center_board":
                        self.show_cb_var.set(True)
                else:
                    self.drop_labels[tipo].configure(text="Drop file here", text_color="gray")
            if ruta:
                self.check_and_activate_logo_under(ruta)
            debug(f"Autoimport {tipo}: {ruta}")
        return toco_anim

    def fps_actual(self):
        """v0.57: fps del estadio seleccionado (campo Frame Rate, que se rellena con la columna FPS de la hoja)."""
        try:
            fps = float(str(self.inputs["FPS"].get()).replace(",", "."))
        except (KeyError, ValueError):
            fps = 0
        if not 1 <= fps <= 120:
            return 25
        return int(fps) if fps == int(fps) else fps

    def _config_ui(self):
        """Config de calcular_layout_data con lo que hay ahora en Manual Edit (v0.58: sacado de calcular_layout)."""
        return {
            "stretch_limit": self.stretch_slider.get(),
            "squash_logos": self.squash_logos_var.get(),
            "stretch_fit_per": self.stretch_fit_var.get(),
            "qty_goal": int(self.qty_goal_slider.get()),
            "qty_corner": int(self.qty_corner_slider.get()),
            "qty_cb": int(self.qty_cb_slider.get()),
            "qty_tl": int(self.qty_tl_slider.get()),
            "gl_offset": self.offset_slider.get(),
            "corner_logo": self.corner_logo_var.get(),
            "hide_extreme_edges": getattr(self, "current_hide_edges", False),
            "logo_under": self.logo_under_var.get(),
            "qty_gl_under": int(self.qty_gl_under_slider.get()),
            "qty_tl_under": int(self.qty_tl_under_slider.get()),
            "show_cb": self.show_cb_var.get(),
            "amplify": self.amplify_var.get(),
            "max_anim_gl": int(self.max_anim_gl_slider.get()),
            "max_anim_tl": int(self.max_anim_tl_slider.get()),
            "invert_order": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
            "edge_case": getattr(self, "edge_case_actual", None),
            "margin_tl": int(self.margin_tl_slider.get()),                        # v0.61
            "under_hotspots": getattr(self, "current_under_hotspots", True),      # v0.61
            "even_spacing": getattr(self, "current_even_spacing", True),          # v0.62
        }

    # ---------------------------------------------------------------- v0.58: campañas
    def guardar_como_referencia(self):
        """v0.61 (★): guarda la arquitectura actual como referencia aprobada. El auto-solver aprende de
        Config/referencias la próxima vez que calcula (plantillas por similitud + pesos de la puntuación)."""
        if not self.architecture_created:
            self.append_chat("Archie", "★ Create the architecture first, then save it as a reference.")
            return
        datos = self.calcular_layout()
        if not datos:
            return
        estadio = self.stadium_selector_global.get() or "stadium"
        logo = os.path.splitext(os.path.basename(self.archivos_cargados.get("static_logo") or "logo"))[0]
        try:
            path = hotspot_solver.guardar_referencia(datos, f"{estadio} - {logo}", CONFIG_DIR)
            n = len(hotspot_solver.cargar_referencias(CONFIG_DIR)[0])
            self.append_chat("Archie", f"★ Saved as approved reference ({n} in total): {os.path.basename(path)}. "
                                       "The Auto-Solver will learn from it on the next Create Architecture.")
        except Exception as e:
            log_error(f"No se pudo guardar la referencia: {e}")
            self.append_chat("Archie", f"★ Could not save the reference: {e}")

    def config_actual(self):
        """Config de la arquitectura actual para guardarla por campaña (sin edge_case, con margin)."""
        cfg = {k: v for k, v in self._config_ui().items() if k in CONFIG_KEYS}
        cfg["margin"] = int(self.margin_slider.get())
        return cfg

    def ui_defaults_campana(self):
        """Valores por defecto de la interfaz (al abrir Archie) para calcular campañas sin abrirlas."""
        d = self._ui_defaults
        return {"margin": d["sliders"]["margin"], "margin_tl": d["sliders"].get("margin_tl", d["sliders"]["margin"]),
                "gl_offset": d["sliders"]["offset"],
                "corner_logo": d["vars"]["corner_logo"], "logo_under": d["vars"]["logo_under"],
                "qty_gl_under": d["sliders"]["qty_gl_under"], "qty_tl_under": d["sliders"]["qty_tl_under"],
                "max_anim_gl": d["sliders"]["max_anim_gl"], "max_anim_tl": d["sliders"]["max_anim_tl"],
                "invert_order": d["vars"]["invert_order"], "stretch_fit": d["vars"]["stretch_fit"]}

    def on_media_mode(self, mode):
        if mode == "CAMPAIGNS":
            self.drop_zones_frame.pack_forget()
            self.campaign_panel.frame.pack(fill="x", expand=False, padx=10, pady=(0, 5))
        else:
            self.campaign_panel.frame.pack_forget()
            self.drop_zones_frame.pack(fill="x", expand=False, padx=10, pady=(0, 5))

    def cargar_archivos_campana(self, files):
        """Pone los ELEMENTS de una campaña en las zonas de FILES (sin autoimport: ya vienen clasificados)."""
        files = files or {}
        anims = [a for a in files.get("anim_zones") or [] if a] or ([files["anim_zone"]] if files.get("anim_zone") else [])
        self.archivos_cargados = {
            "background": files.get("background"), "center_board": files.get("center_board"),
            "static_logo": files.get("static_logo"),
            "anim_zone": anims[0] if anims else None, "anim_zones": list(anims) or [None],
        }
        for key in ("background", "center_board", "static_logo"):
            ruta = self.archivos_cargados[key]
            if ruta:
                self.drop_labels[key].configure(text=f"Loaded:\n{os.path.basename(ruta)}", text_color="#2CC985")
            else:
                self.drop_labels[key].configure(text="Drop file here", text_color="gray")
        self.refresh_anim_zones_ui()
        self.reset_zooms_and_pan()
        self.pil_preview_images, self.pil_sys_images, self.pil_strip3d_images = [], [], []
        self.preview_current_idx = 0
        self.undo_stack, self.redo_stack = [], []

    def aplicar_config_campana(self, cfg):
        """Carga en Manual Edit la arquitectura guardada de una campaña y la pinta."""
        self.current_hide_edges = bool(cfg.get("hide_extreme_edges", False))
        self.current_under_hotspots = bool(cfg.get("under_hotspots", False))  # JSON antiguos: como antes
        self.current_even_spacing = bool(cfg.get("even_spacing", False))      # v0.62 (JSON antiguos: como antes)
        self.architecture_created = True
        self.arch_stadium = self.stadium_selector_global.get()
        self.arch_key = self.solver_key = self._arch_key()   # v0.66: arquitectura guardada = aprobada
        self.preview_enabled = True
        self._actualizar_sliders_desde_ia(
            cfg["qty_goal"], cfg["qty_corner"], cfg["qty_cb"], cfg["qty_tl"], cfg["stretch_limit"],
            cfg["squash_logos"], cfg.get("stretch_fit_per", False), cfg["gl_offset"], cfg["corner_logo"],
            cfg["logo_under"], cfg["qty_gl_under"], cfg["qty_tl_under"], cfg.get("show_cb", True),
            self.amplify_var.get(), cfg.get("invert_order", False), cfg.get("max_anim_gl", 0),
            cfg.get("max_anim_tl", 0), margin=cfg.get("margin", 20),
            margin_tl=cfg.get("margin_tl", cfg.get("margin", 20)))
        self._init_history_state()
        self.start_preview_render()

    def preparar_campana_nueva(self):
        """Manual Edit a los valores por defecto (nada se hereda de la campaña anterior) antes del auto-solver."""
        estado = copy.deepcopy(self._ui_defaults)
        estado["inputs"] = {}                                   # el perfil del estadio no se toca
        estado["vars"]["amplify"] = self.amplify_var.get()      # amplify es del estadio
        self.architecture_created = False
        self._apply_ui_state(estado)
        self.check_and_activate_logo_under(self.archivos_cargados.get("static_logo"))
        self._init_history_state()

    def calcular_layout(self):
        try:
            w_total = int(self.inputs["Width"].get() or 0)
            h_total = int(self.inputs["Height"].get() or 0)
            goal_line = int(self.inputs["Goal Line Size"].get() or 0)
            cb_width = int(self.inputs["Center Board Width"].get() or 0)
            margin = int(self.margin_slider.get())
            
            config = self._config_ui()
            res = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin, self.archivos_cargados, config)
            if res:
                res["config"] = config
                res["fps"] = self.fps_actual()  # v0.57: fps del render, de la columna FPS de la hoja
                # NUEVO BLOQUE: system_file para After Effects
                sys_w = int(self.inputs["Canvas Width"].get() or 3840)
                sys_h = int(self.inputs["Canvas Height"].get() or 2160)
                sys_mask = int(self.inputs["Canvas Mask"].get() or 96)
                res["system_file"] = {
                    "width": sys_w,
                    "height": sys_h,
                    "offset": {
                        "x": int(self.inputs["Offset X"].get() or 0),
                        "y": int(self.inputs["Offset Y"].get() or 0)
                    },
                    "mask": {
                        "x": sys_w - sys_mask,
                        "y": 0,
                        "width": sys_mask,
                        "height": sys_h
                    }
                }
                edge = config["edge_case"] or {}
                if edge.get("tramos"):
                    res["system_file"]["tramos"] = edge["tramos"]
                if edge.get("zonas_negras"):
                    res["zonas_negras"] = edge["zonas_negras"]
            return res
        except Exception as e:
            traceback.print_exc()
            log_error(f"Error CRÍTICO en calcular_layout: {e}")
            return None

    def _auto_update_enabled(self):
        """v0.38: con la arquitectura ya creada para este estadio, cualquier cambio se aplica solo."""
        return self.architecture_created and self.stadium_selector_global.get() == self.arch_stadium

    def trigger_update(self, event=None, *args):
        """Marca la preview como pendiente y, si ya hay arquitectura, la recalcula sola tras una pausa corta."""
        if not hasattr(self, "lbl_preview_status"): return
        if getattr(self, "campaign_panel", None): self.campaign_panel.on_ui_change()  # v0.58
        self.preview_dirty = True
        self.player.interrupt()  # pausa mientras se tocan parámetros; reanuda al soltar
        if self._auto_update_enabled():
            # Al teclear en un campo se espera más, para no recalcular con números a medias
            self._schedule_auto_update(AUTO_UPDATE_TYPING_MS if isinstance(event, tk.Event) else AUTO_UPDATE_DELAY_MS)
            if not self.render_busy:
                self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Updating...)", text_color="#E67E22")
        elif not self.render_busy:
            if self.pil_preview_images:
                self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (New stadium · Press Create Architecture)", text_color="#E67E22")
            else:
                self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Press Create Architecture)", text_color="gray")
        self.schedule_prefetch()

    def _schedule_auto_update(self, delay_ms=None):
        if self.auto_update_timer: self.after_cancel(self.auto_update_timer)
        self.auto_update_timer = self.after(delay_ms or AUTO_UPDATE_DELAY_MS, self._run_auto_update)

    def _cancel_auto_update(self):
        if self.auto_update_timer:
            self.after_cancel(self.auto_update_timer)
            self.auto_update_timer = None

    def _run_auto_update(self):
        self.auto_update_timer = None
        if not self._auto_update_enabled(): return
        if self.solver_busy:
            # El auto-solver aún está calculando: reintentar cuando acabe
            self._schedule_auto_update()
            return
        # Si hay un render de preview en marcha, start_preview_render lo sustituye (render_gen)
        self.start_preview_render()

    def reset_preview_state(self):
        self.trigger_update()
        # Garantizar que el botón de crear arquitectura siempre esté disponible
        if hasattr(self, 'btn_create_arch') and not self.render_busy:
            self.btn_create_arch.configure(state="normal", text="✨ Create Architecture")

    def trigger_resize(self, event=None):
        if getattr(self, 'resize_timer', None): self.after_cancel(self.resize_timer)
        self.resize_timer = self.after(150, self.auto_fit_zooms)

    def obtener_frame_pil(self, ruta, h_target=None):
        if not ruta: return Image.new("RGBA", (100, h_target or 100), (0,0,0,0))
        if is_video(ruta):
            try:
                return FRAME_CACHE.frame_sync(ruta, 0, h_target).copy()
            except Exception as e:
                log_error(f"Fallo al extraer frame de vídeo '{ruta}': {e}")
                return Image.new("RGBA", (100, h_target or 100), (255, 0, 0, 255))
        else:
            return FRAME_CACHE.image(ruta).copy()

    def safe_crop(self, img, x1, y1, x2, y2):
        x1, x2 = sorted([max(0, min(x1, img.width)), max(0, min(x2, img.width))])
        y1, y2 = sorted([max(0, min(y1, img.height)), max(0, min(y2, img.height))])
        return img.crop((x1, y1, x2, y2))

    # ==========================================
    # PREVIEW: manual, en segundo plano y con caché de frames
    # ==========================================
    def _start_thread(self, target, *args):
        # Recolectar basura de Tk (fuentes/imágenes de widgets destruidos) en el hilo principal:
        # si el GC la libera dentro de un hilo secundario mientras el principal espera, Tk se bloquea.
        gc.collect()
        threading.Thread(target=target, args=args, daemon=True).start()

    def show_preview_overlay(self, status_text):
        self.render_busy = True
        self.player.hold()
        self.lbl_preview_overlay_status.configure(text=status_text)
        self.preview_overlay_bar.set(0)
        if not self.loading_overlay_on:
            self.loading_overlay_on = True
            self._repintar_previews()  # oscurece lo que se ve ahora
        self.sys_loading_box.place(relx=0.5, rely=0.5, anchor="center")
        self.sys_loading_box.lift()
        self.preview_loading_box.place(relx=0.5, rely=0.5, anchor="center")
        self.preview_loading_box.lift()

    def hide_preview_overlay(self):
        self.render_busy = False
        self.sys_loading_box.place_forget()
        self.preview_loading_box.place_forget()
        if self.loading_overlay_on:
            self.loading_overlay_on = False
            self._repintar_previews()
        if hasattr(self, 'btn_create_arch'):
            self.btn_create_arch.configure(state="normal", text="✨ Create Architecture")

    def cancel_preview_render(self):
        """v0.38: Cancel del aviso de carga. Descarta el cálculo en curso y deja la preview anterior."""
        self._cancel_auto_update()
        self.render_gen += 1
        if self.solver_busy:
            self.solver_gen += 1
            self.solver_busy = False
        self.hide_preview_overlay()
        self.player.release()
        self.preview_dirty = True
        self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Update cancelled)", text_color="#E67E22")

    @staticmethod
    def _oscurecer(img):
        """Negro semitransparente encima de la imagen (aviso de carga)."""
        img = img.convert("RGBA")
        return Image.alpha_composite(img, Image.new("RGBA", img.size, (0, 0, 0, 170)))

    def _repintar_previews(self):
        """Vuelve a pintar la última imagen de cada preview (oscurecida o no según loading_overlay_on)."""
        if self._shown_strip is not None:
            if self._shown_strip_player:
                self.player_show(self._shown_strip)
            else:
                self._pintar_label(self.lbl_live_preview, self._shown_strip)
        if self._shown_sys is not None:
            if self._shown_sys_player:
                self.sys_player_show(self._shown_sys)
            else:
                self._pintar_label(self.lbl_sys_image, self._shown_sys)

    def _pintar_label(self, label, img):
        scale = self.get_ctk_scale()
        if scale <= 0: scale = 1.0
        shown = self._oscurecer(img) if self.loading_overlay_on else img
        size = (max(1, int(img.width / scale)), max(1, int(img.height / scale)))
        label.configure(text="", image=ctk.CTkImage(light_image=shown, dark_image=shown, size=size))

    def _set_preview_progress(self, gen, value, text):
        if gen != self.render_gen or not self.render_busy: return
        self.preview_overlay_bar.set(value)
        self.lbl_preview_overlay_status.configure(text=text)

    def _aplicar_warnings(self, datos):
        warnings = datos.get("warnings", [])
        color_default = "white"
        color_error = "#E74C3C"
        self.lbl_qty_goal_title.configure(text_color=color_default)
        self.lbl_qty_corner_title.configure(text_color=color_default)
        self.lbl_qty_cb_title.configure(text_color=color_default)
        self.lbl_qty_tl_title.configure(text_color=color_default)
        self.lbl_warning.configure(text="")

        if "goal_overlap" in warnings:
            self.lbl_qty_goal_title.configure(text_color=color_error)
            self.lbl_qty_corner_title.configure(text_color=color_error)
            self.lbl_warning.configure(text="⚠️ OVERLAP: Too many logos in Goals/Corners!")
        if "touchline_overlap" in warnings:
            self.lbl_qty_cb_title.configure(text_color=color_error)
            self.lbl_qty_corner_title.configure(text_color=color_error)
            self.lbl_qty_tl_title.configure(text_color=color_error)
            self.lbl_warning.configure(text="⚠️ OVERLAP: Too many logos in Touchline/Centerboard!")
        if "goal_overlap" in warnings and "touchline_overlap" in warnings:
            self.lbl_warning.configure(text="⚠️ OVERLAP: Too many logos in multiple zones!")

        if "stretch_fit_15" in warnings:
            self.lbl_warning.configure(text="⚠️ WARNING: Stretch Fit ha deformado los elementos más del 15%!")

    def _archivos_preview(self):
        valid_anims = [f for f in self.archivos_cargados.get("anim_zones", []) if f is not None]
        if not valid_anims and self.archivos_cargados.get("anim_zone"): valid_anims = [self.archivos_cargados["anim_zone"]]
        return {
            "anims": valid_anims,
            "static_logo": self.archivos_cargados.get("static_logo"),
            "background": self.archivos_cargados.get("background"),
            "center_board": self.archivos_cargados.get("center_board"),
        }

    @staticmethod
    def _tiempos_preview(anims):
        dur_base = 1.0
        for path in anims:
            if is_video(path):
                dur_base = max(dur_base, FRAME_CACHE.duration(path))
        return [dur_base * (i / float(PREVIEW_FRAMES)) for i in range(PREVIEW_FRAMES)]

    @staticmethod
    def _solicitar_frames(files, h_total, tiempos, idx):
        """Encola en la caché los frames de vídeo que necesita el frame 'idx' de la preview."""
        t = tiempos[idx]
        futures = []
        for path in files["anims"]:
            if is_video(path): futures.append(FRAME_CACHE.request(path, t, h_total))
        bg = files["background"]
        if is_video(bg): futures.append(FRAME_CACHE.request(bg, t, h_total))
        logo = files["static_logo"]
        if is_video(logo):
            futures.append(FRAME_CACHE.request(logo, 0, h_total))
            futures.append(FRAME_CACHE.request(logo, t, h_total))
        cb = files["center_board"]
        if is_video(cb): futures.append(FRAME_CACHE.request(cb, 0, h_total))
        return futures

    def schedule_prefetch(self):
        """Precarga en segundo plano los frames de los archivos actuales (no bloquea nada)."""
        if self.prefetch_timer: self.after_cancel(self.prefetch_timer)
        self.prefetch_timer = self.after(600, self._prefetch_current)

    def _prefetch_current(self):
        self.prefetch_timer = None
        try:
            h_total = int(self.inputs["Height"].get() or 0)
        except Exception:
            return
        files = self._archivos_preview()
        if h_total <= 0 or not any(is_video(f) for f in files["anims"] + [files["background"], files["static_logo"], files["center_board"]]):
            return
        first = self.preview_current_idx

        def worker():
            try:
                tiempos = self._tiempos_preview(files["anims"])
                for idx in [first] + [i for i in range(PREVIEW_FRAMES) if i != first]:
                    self._solicitar_frames(files, h_total, tiempos, idx)
            except Exception as e:
                debug(f"Prefetch de frames falló: {e}")
        self._start_thread(worker)

    def update_live_preview(self):
        # Compatibilidad: cualquier llamada antigua pasa por el nuevo pipeline
        self.start_preview_render()

    def start_preview_render(self):
        debug("Iniciando render de preview...")
        self._cancel_auto_update()
        datos = self.calcular_layout()
        if not datos:
            self.hide_preview_overlay()
            self.player.release()
            self.lbl_preview_status.configure(text="Missing / Invalid Parameters", text_color="#E74C3C")
            return
        try:
            self._aplicar_warnings(datos)
            w_total = datos["dimensiones_globales"]["width"]
            h_total = datos["dimensiones_globales"]["height"]
            params = {
                "datos": datos,
                "w_total": w_total,
                "h_total": h_total,
                "sys_w": int(self.inputs["Canvas Width"].get() or 3840),
                "sys_h": int(self.inputs["Canvas Height"].get() or 2160),
                "sys_mask": int(self.inputs["Canvas Mask"].get() or 96),
                "sys_off_x": int(self.inputs["Offset X"].get() or 0),
                "sys_off_y": int(self.inputs["Offset Y"].get() or 0),
                "row_idx": getattr(self, "preview_row_idx", None),
                "show_cb": self.show_cb_var.get() and datos.get("config", {}).get("show_cb", True),
                "gl_offset_pct": self.offset_slider.get() / 100.0,
                "files": self._archivos_preview(),
            }
            if params["sys_w"] - params["sys_mask"] <= 0: raise ValueError("Mask > Width")
        except Exception as e:
            traceback.print_exc()
            self.hide_preview_overlay()
            self.player.release()
            self.lbl_preview_status.configure(text=f"Geometry Error: {type(e).__name__} - {str(e)[:40]}", text_color="#E74C3C")
            return

        self.render_gen += 1
        gen = self.render_gen
        self.preview_dirty = False
        first_idx = self.preview_current_idx if 0 <= self.preview_current_idx < PREVIEW_FRAMES else 0
        self.btn_create_arch.configure(state="disabled", text="✨ Building Preview...")
        self.show_preview_overlay("Loading key frames...")
        self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Calculating...)", text_color="#E67E22")
        self._start_thread(self._render_worker, gen, params, first_idx)

    def _render_worker(self, gen, p, first_idx):
        try:
            files, h_total = p["files"], p["h_total"]
            tiempos = self._tiempos_preview(files["anims"])
            order = [first_idx] + [i for i in range(PREVIEW_FRAMES) if i != first_idx]

            # Encolar todas las decodificaciones por prioridad: el frame visible va primero
            first_futures = self._solicitar_frames(files, h_total, tiempos, first_idx)
            for idx in order[1:]:
                self._solicitar_frames(files, h_total, tiempos, idx)

            total = max(1, len(first_futures))
            for k, fut in enumerate(first_futures, 1):
                try: fut.result()
                except Exception: pass  # el compositor pinta el placeholder rojo
                self.after(0, self._set_preview_progress, gen, 0.85 * k / total, f"Decoding key frames {k}/{total}...")

            self.after(0, self._set_preview_progress, gen, 0.9, "Composing architecture...")
            memo = {}
            out = {}
            res, sys_canvas = self._componer_frame_preview(p, tiempos[first_idx], memo, strip_out=out)
            if gen != self.render_gen: return
            self.after(0, self._on_first_preview_frame, gen, first_idx, res, sys_canvas, self._strip_3d(out, p), p)

            # Escena del reproductor: capa estática + instancias (los vídeos se decodifican aparte, una vez)
            try:
                escena = self._construir_escena_reproductor(p, memo)
            except Exception as e:
                traceback.print_exc()
                log_error(f"Reproductor: no se pudo preparar la escena: {e}")
                escena = None  # (escena strip, escena system) o None
            if gen != self.render_gen: return
            self.after(0, self._on_player_scene, gen, escena)

            # Resto de frames en segundo plano, sin bloquear la ventana
            for idx in order[1:]:
                if gen != self.render_gen: return
                out = {}
                res, sys_canvas = self._componer_frame_preview(p, tiempos[idx], memo, strip_out=out)
                if gen != self.render_gen: return
                self.after(0, self._on_background_preview_frame, gen, idx, res, sys_canvas, self._strip_3d(out, p))
        except Exception as e:
            traceback.print_exc()
            log_error(f"Error en render de preview: {e}")
            self.after(0, self._on_preview_error, gen, e)

    def _on_first_preview_frame(self, gen, idx, res, sys_canvas, strip3d=None, p=None):
        if gen != self.render_gen: return
        self.pil_preview_images = [None] * PREVIEW_FRAMES
        self.pil_sys_images = [None] * PREVIEW_FRAMES
        self.pil_strip3d_images = [None] * PREVIEW_FRAMES
        self.pil_preview_images[idx] = res
        self.pil_sys_images[idx] = sys_canvas
        self.pil_strip3d_images[idx] = strip3d
        if p is not None:
            self._actualizar_maqueta_3d(p)
        self.preview_current_idx = idx
        self.preview_frames_ready = 1
        self._shown_strip = self._shown_sys = None  # no repintar la imagen vieja: la nueva llega enseguida
        self.hide_preview_overlay()
        self._update_preview_status_label()
        self.after(100, self.auto_fit_zooms)
        debug("Preview: primer frame listo.")

    def _on_background_preview_frame(self, gen, idx, res, sys_canvas, strip3d=None):
        if gen != self.render_gen: return
        self.pil_preview_images[idx] = res
        self.pil_sys_images[idx] = sys_canvas
        if len(self.pil_strip3d_images) == PREVIEW_FRAMES:
            self.pil_strip3d_images[idx] = strip3d
        self.preview_frames_ready += 1
        self._update_preview_status_label()
        if self.preview_frames_ready == PREVIEW_FRAMES:
            debug("Preview: 8 frames listos.")

    def _update_preview_status_label(self):
        if self.preview_dirty: return  # no pisar el aviso de cambios pendientes
        if self.preview_frames_ready < PREVIEW_FRAMES:
            self.lbl_preview_status.configure(text=f"ARCHITECTURE PREVIEW (Updated · loading frames {self.preview_frames_ready}/{PREVIEW_FRAMES})", text_color=self.c_magenta)
        else:
            self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Updated)", text_color=self.c_magenta)

    def _on_preview_error(self, gen, e):
        if gen != self.render_gen: return
        self.hide_preview_overlay()
        self.player.release()
        err_txt = f"Geometry Error: {type(e).__name__} - {str(e)[:40]}"
        debug(f"{err_txt}")
        self.lbl_preview_status.configure(text=err_txt, text_color="#E74C3C")

    def _componer_frame_preview(self, p, t, memo, capa_estatica=False, strip_out=None):
        """Compone un frame de la preview (strip + system). Corre en un hilo: no tocar widgets aquí.

        capa_estatica=True: solo lo que no se mueve (base del reproductor); devuelve (res, None).
        Si el fondo es vídeo, todo el strip pasa al reproductor y aquí solo quedan los textos.
        """
        bg_video = is_video(p["files"]["background"])
        solo_textos = capa_estatica and bg_video
        datos, files = p["datos"], p["files"]
        w_total, h_total = p["w_total"], p["h_total"]
        sys_w, sys_h, sys_mask = p["sys_w"], p["sys_h"], p["sys_mask"]
        sys_off_x, sys_off_y = p["sys_off_x"], p["sys_off_y"]

        def fetch(path, tt):
            try:
                return FRAME_CACHE.frame(path, tt, h_total) if is_video(path) else FRAME_CACHE.image(path)
            except Exception as e:
                log_error(f"Preview: no se pudo leer '{path}': {e}")
                return None

        def resized(key, img, size):
            # memo compartido entre frames para lo estático; por frame para lo animado
            k = (key, size)
            if k not in memo:
                memo[k] = img.resize(size, PREVIEW_RESAMPLE)
            return memo[k]

        frame_memo = {}

        master = Image.new("RGBA", (w_total, h_total))

        # Fondo
        bg_path = files["background"]
        if bg_path and not solo_textos:
            bg_img = fetch(bg_path, t)
            if bg_img is not None:
                new_bg_w = max(1, int(h_total * (bg_img.width / max(1, bg_img.height))))
                if is_video(bg_path):
                    tile = bg_img if bg_img.size == (new_bg_w, h_total) else bg_img.resize((new_bg_w, h_total), PREVIEW_RESAMPLE)
                else:
                    tile = resized(("bg", bg_path), bg_img, (new_bg_w, h_total))
                for x in range(0, w_total, new_bg_w):
                    master.paste(tile, (x, 0))

        # Zonas de animación
        pil_anims_frames = []
        for path in files["anims"]:
            img = fetch(path, t)
            pil_anims_frames.append(img if img is not None else Image.new("RGBA", (3000, h_total), (255, 0, 0, 255)))
        if not pil_anims_frames: pil_anims_frames = [Image.new("RGBA", (3000, h_total), (255, 0, 0, 255))]

        # Logo (frame 0 para los estáticos, frame t para los "animated" del modo Logo Under)
        logo_path = files["static_logo"]
        pil_logo_raw = fetch(logo_path, 0) if logo_path else None
        if pil_logo_raw is None: pil_logo_raw = Image.new("RGBA", (100, h_total), (0, 0, 0, 0))
        logo_anim_img = pil_logo_raw
        if is_video(logo_path):
            logo_anim_frame = fetch(logo_path, t)
            if logo_anim_frame is not None: logo_anim_img = logo_anim_frame

        cb_path = files["center_board"]
        anim_paths = files["anims"] or [None]
        logos_debajo = logos_bajo_anims(datos)  # v0.61: Logo Under -> ANIM ZONES encima de los logos
        anims_diferidas = []
        for zona in ([] if solo_textos else datos["zonas"].values()):
            for e in zona["elementos"]:
                if e["tipo"] == "center_board" and e["width"] > 0:
                    if cb_path and p["show_cb"]:
                        k = ("cb", cb_path, e["width"], h_total)
                        if k not in memo:
                            pil_cb_raw = fetch(cb_path, 0)
                            if pil_cb_raw is None: pil_cb_raw = Image.new("RGBA", (100, h_total), (255, 0, 0, 255))
                            memo[k] = aplicar_letterbox_pil(pil_cb_raw, e["width"], h_total, PREVIEW_RESAMPLE)
                        cb_letterbox = memo[k]
                        master.paste(cb_letterbox, (e["x_absoluta"], 0), cb_letterbox)
                    else:
                        master.paste(Image.new("RGBA", (e["width"], h_total), (0,0,0,255)), (e["x_absoluta"], 0))
                elif e["tipo"] == "anim_zone":
                    a_idx = e.get("anim_idx", 0) % len(pil_anims_frames)
                    if capa_estatica and is_video(anim_paths[a_idx % len(anim_paths)]): continue
                    k = (a_idx, e["width"])
                    if k not in frame_memo:
                        frame_memo[k] = pil_anims_frames[a_idx].resize((e["width"], h_total), PREVIEW_RESAMPLE)
                    anim_img = frame_memo[k]
                    if logos_debajo:
                        anims_diferidas.append((anim_img, e["x_absoluta"]))
                    else:
                        master.paste(anim_img, (e["x_absoluta"], 0), anim_img)

        for zona in ([] if solo_textos else datos["zonas"].values()):
            for e in zona["elementos"]:
                if e["tipo"] == "static_logo":
                    if capa_estatica and e.get("logo_under_type") == "animated" and is_video(logo_path): continue
                    if e.get("logo_under_type") == "animated" and logo_anim_img is not pil_logo_raw:
                        k = ("logo_anim", e["width"])
                        if k not in frame_memo:
                            frame_memo[k] = logo_anim_img.resize((e["width"], h_total), PREVIEW_RESAMPLE)
                        logo_img = frame_memo[k]
                    else:
                        logo_img = resized(("logo", logo_path), pil_logo_raw, (e["width"], h_total))
                    master.paste(logo_img, (e["x_absoluta"], 0), logo_img)

        for anim_img, ax in anims_diferidas:
            master.paste(anim_img, (ax, 0), anim_img)

        pintar_zonas_negras(master, edge_de(datos).get("zonas_negras"))  # v0.52: brands reservados

        if strip_out is not None:
            strip_out["master"] = master  # v0.46: el strip tal cual, para 3D PREVIEW
        res = generar_imagen_arquitectura(master, datos, h_total, w_total, p["gl_offset_pct"], bg_color=(0, 0, 0, 0), resample=PREVIEW_RESAMPLE, max_png_width=None)

        filas, (src_y_start, src_y_end) = self._filas_system(p)
        h_slice = src_y_end - src_y_start
        master_slice = self.safe_crop(master, 0, src_y_start, w_total, src_y_end)
        sys_canvas = Image.new("RGBA", (sys_w, sys_h), (0,0,0,255))

        for crop_x1, crop_x2, x_canvas, paste_y in filas:
            row_img = self.safe_crop(master_slice, crop_x1, 0, crop_x2, h_slice)
            sys_canvas.paste(row_img, (int(x_canvas), int(paste_y)))

        if sys_mask > 0: ImageDraw.Draw(sys_canvas).rectangle([sys_w - sys_mask, 0, sys_w, sys_h], fill=(0,0,0,255))

        draw_sys = ImageDraw.Draw(sys_canvas)
        stroke_w = max(3, int(min(sys_w, sys_h) * 0.0018))
        draw_sys.rectangle(
            [0, 0, sys_w - 1, sys_h - 1],
            outline=(180, 180, 180, 255),
            width=stroke_w
        )
        return res, sys_canvas

    @staticmethod
    def _filas_system(p):
        """Cómo se reparte el strip en el System File: ([(x_ini, x_fin, x_lienzo, y_lienzo)], (y0, y1) del strip)."""
        datos, w_total, h_total = p["datos"], p["w_total"], p["h_total"]
        sys_mask, sys_off_x, sys_off_y = p["sys_mask"], p["sys_off_x"], p["sys_off_y"]
        is_amplify = datos.get("config", {}).get("amplify", False)
        if is_amplify and p["row_idx"] is not None:
            h_slice = h_total // 2
            v_slice = (h_slice, h_total) if p["row_idx"] == 1 else (0, h_slice)
        else:
            h_slice = h_total
            v_slice = (0, h_total)

        # v0.52: el reparto vive en edge_cases.filas_system (estándar o tramos explícitos del edge case)
        filas = filas_system(w_total, h_slice, p["sys_w"], sys_mask, sys_off_x, sys_off_y, edge_de(datos))
        return filas, v_slice

    # ==========================================
    # REPRODUCTOR (v0.35): cada ELEMENT se decodifica una vez y se pinta en todas sus instancias
    # ==========================================
    def _construir_escena_reproductor(self, p, memo):
        """Corre en el hilo de render. Replica el orden de pintado de _componer_frame_preview."""
        datos, files = p["datos"], p["files"]
        w_total, h_total = p["w_total"], p["h_total"]
        out = {}
        static_res, static_sys = self._componer_frame_preview(p, 0.0, memo, capa_estatica=True, strip_out=out)
        _, segmentos = segmentos_arquitectura(datos, h_total, w_total)

        videos, instancias = {}, []

        def video(path):
            try:
                info = FRAME_CACHE.info(path)
            except Exception as e:
                log_error(f"Reproductor: no se pudo leer '{path}': {e}")
                return None
            videos[path] = info
            return path

        def still(path, t=0.0):
            try:
                return FRAME_CACHE.frame(path, t, h_total) if is_video(path) else FRAME_CACHE.image(path)
            except Exception as e:
                log_error(f"Reproductor: no se pudo leer '{path}': {e}")
                return None

        bg_path = files["background"]
        bg_video = is_video(bg_path)
        if bg_video and video(bg_path):
            bw, bh = videos[bg_path]["size"]
            tile_w = max(1, int(round(bw * h_total / float(max(1, bh)))))
            for x in range(0, w_total, tile_w):
                instancias.append({"src": bg_path, "x": x, "w": tile_w, "mask": True})

        anims = files["anims"]
        cb_path = files["center_board"]
        logos_debajo = logos_bajo_anims(datos)  # v0.61: Logo Under -> las ANIM ZONES se apilan después de los logos
        inst_anims = [] if logos_debajo else instancias
        for zona in datos["zonas"].values():
            for e in zona["elementos"]:
                if e["width"] <= 0: continue
                if e["tipo"] == "center_board" and bg_video:
                    if cb_path and p["show_cb"]:
                        k = ("cb", cb_path, e["width"], h_total)
                        if k not in memo:
                            raw = still(cb_path) or Image.new("RGBA", (100, h_total), (255, 0, 0, 255))
                            memo[k] = aplicar_letterbox_pil(raw, e["width"], h_total, PREVIEW_RESAMPLE)
                        instancias.append({"src": memo[k], "x": e["x_absoluta"], "w": e["width"], "mask": True})
                    else:
                        instancias.append({"src": Image.new("RGBA", (1, 1), (0, 0, 0, 255)), "x": e["x_absoluta"], "w": e["width"], "mask": False})
                elif e["tipo"] == "anim_zone" and anims:
                    path = anims[e.get("anim_idx", 0) % len(anims)]
                    if is_video(path):
                        if path in videos or video(path):
                            inst_anims.append({"src": path, "x": e["x_absoluta"], "w": e["width"], "mask": True})
                    elif bg_video:
                        img = still(path)
                        if img is not None:
                            inst_anims.append({"src": img, "x": e["x_absoluta"], "w": e["width"], "mask": True})

        logo_path = files["static_logo"]
        logo_still = None
        for zona in datos["zonas"].values():
            for e in zona["elementos"]:
                if e["tipo"] != "static_logo" or e["width"] <= 0 or not logo_path: continue
                if e.get("logo_under_type") == "animated" and is_video(logo_path):
                    if logo_path in videos or video(logo_path):
                        instancias.append({"src": logo_path, "x": e["x_absoluta"], "w": e["width"], "mask": True})
                elif bg_video:
                    if logo_still is None: logo_still = still(logo_path)
                    if logo_still is not None:
                        instancias.append({"src": logo_still, "x": e["x_absoluta"], "w": e["width"], "mask": True})
        if logos_debajo:
            instancias.extend(inst_anims)

        # v0.52: el reproductor no pinta vídeo sobre las zonas negras (edge cases)
        negras = edge_de(datos).get("zonas_negras")
        escena = Escena(static_res, restar_zonas(segmentos, negras), h_total, instancias, videos)
        # v0.39: mismas instancias, colocadas en las filas del System File
        filas, v_slice = self._filas_system(p)
        escena_sys = Escena(static_sys, restar_zonas(filas, negras), h_total, instancias, videos, v_slice=v_slice)
        # v0.46: mismas instancias sobre el strip entero (un solo tramo), textura de 3D PREVIEW
        escena_3d = Escena(out["master"], restar_zonas([(0, w_total, 0, 0)], negras), h_total, instancias, videos)
        return escena, escena_sys, escena_3d

    def _on_player_scene(self, gen, escena):
        if gen != self.render_gen: return
        if escena is None:
            self.player.clear()
        else:
            escena_strip, escena_sys, escena_3d = escena
            self.player.set_escena(escena_strip, {self.sys_vista: escena_sys, self.vista_3d: escena_3d})
        self.player.release(immediate=True)
        self.actualizar_imagen_preview()

    # --- Callbacks que usa PreviewPlayer ---
    def player_view(self):
        if not self.pil_preview_images or self.player.escena is None: return None
        cw = self.preview_image_container.winfo_width()
        ch = self.preview_image_container.winfo_height()
        if cw <= 10 or ch <= 10: return None
        rw, rh = self.player.escena.res_size
        zoom = self.zoom_factor_strip
        sw, sh = max(1, int(rw * zoom)), max(1, int(rh * zoom))
        # mismo recorte del pan que get_panned_image
        max_px, max_py = max(0, (sw - cw) / 2), max(0, (sh - ch) / 2)
        self.pan_x_strip = max(-max_px, min(max_px, self.pan_x_strip)) if sw > cw else 0
        self.pan_y_strip = max(-max_py, min(max_py, self.pan_y_strip)) if sh > ch else 0
        return {"cw": cw, "ch": ch, "zoom": zoom, "pan_x": self.pan_x_strip, "pan_y": self.pan_y_strip}

    def _sys_visible(self):
        try:
            return self.preview_tabs.get() == "SYSTEM FILE PREVIEW" and self.sys_image_container.winfo_ismapped()
        except Exception:
            return True

    def sys_player_view(self):
        if not self.pil_sys_images or self.sys_vista.escena is None: return None
        if not self._sys_visible(): return None  # v0.47: pestaña oculta, no se pinta
        cw = self.sys_image_container.winfo_width()
        ch = self.sys_image_container.winfo_height()
        if cw <= 10 or ch <= 10: return None
        rw, rh = self.sys_vista.escena.res_size
        zoom = self.zoom_factor_sys
        sw, sh = max(1, int(rw * zoom)), max(1, int(rh * zoom))
        max_px, max_py = max(0, (sw - cw) / 2), max(0, (sh - ch) / 2)
        self.pan_x_sys = max(-max_px, min(max_px, self.pan_x_sys)) if sw > cw else 0
        self.pan_y_sys = max(-max_py, min(max_py, self.pan_y_sys)) if sh > ch else 0
        return {"cw": cw, "ch": ch, "zoom": zoom, "pan_x": self.pan_x_sys, "pan_y": self.pan_y_sys}

    def sys_player_static_view(self, static_sys):
        v = self.sys_player_view()
        canvas, _, _ = self.get_panned_image(static_sys, v["zoom"], v["pan_x"], v["pan_y"], v["cw"], v["ch"])
        return canvas

    def sys_player_show(self, img):
        self._shown_sys, self._shown_sys_player = img, True
        self._sys_player_target.show(self.lbl_sys_image._label, self._oscurecer(img) if self.loading_overlay_on else img)

    # --- 3D PREVIEW (v0.46) ---
    def _v3d_visible(self):
        try:
            return self.preview_tabs.get() == "3D PREVIEW" and self.v3d_container.winfo_ismapped()
        except Exception:
            return False

    @staticmethod
    def _strip_3d(out, p):
        """Strip limpio de un key frame, reducido a la resolución de textura del 3D. Corre en el hilo de render."""
        master = out.get("master")
        if master is None: return None
        k = escala_textura(p["w_total"])
        if k >= 1.0: return master.copy()
        size = (max(1, int(round(master.width * k))), max(1, int(round(master.height * k))))
        return master.resize(size, PREVIEW_RESAMPLE)

    def _actualizar_maqueta_3d(self, p):
        """Rehace la U del perímetro con el perfil actual (Width, Goal Line Size, Height) y el modo amplify."""
        datos = p["datos"]
        w_total, h_total = p["w_total"], p["h_total"]
        goal_line = datos["zonas"]["ZONA_1"]["width"]
        amplify = bool(datos.get("config", {}).get("amplify", False))
        L, W, bandas = geometria_estadio(w_total, goal_line, h_total, amplify)
        self._geo3d = (w_total, h_total, L, W)
        self.visor_3d.set_scene(L, W, bandas)
        gap = W - goal_line
        txt = f"Touchline {int(L)} px · Goal Line {goal_line} px"
        if gap > 0.5:
            txt += f" · {int(round(gap))} px gap (pitch 110x64 min.)"
        if amplify:
            txt += " · 2 rows"
        self.lbl_v3d_info.configure(text=txt)

    def _refrescar_3d(self):
        """Pinta en 3D PREVIEW lo mismo que la Architecture Preview: el reproductor o el key frame actual."""
        if self._geo3d is None or not self._v3d_visible(): return
        if self.player.engaged and self.player.ready and self.vista_3d.escena is not None:
            self.player.render()
            return
        if not self.pil_strip3d_images: return
        idx = self.preview_current_idx
        img = self.pil_strip3d_images[idx] if 0 <= idx < len(self.pil_strip3d_images) else None
        if img is not None:
            self.visor_3d.set_strip(img, self._geo3d[0], self._geo3d[1])

    def _on_escala_3d(self):
        """La cámara se ha acercado/alejado lo bastante como para pedir otra resolución de textura."""
        if self.player.engaged and self.player.ready and not self.player.playing and self.vista_3d.escena is not None:
            self.player.render()

    def on_preview_tab_change(self):
        if self.preview_tabs.get() == "3D PREVIEW":
            self.after(30, self._refrescar_3d)
        else:
            self.after(30, self.auto_fit_zooms)  # el System File estaba oculto: reajustar su zoom

    def v3d_player_view(self):
        escena = self.vista_3d.escena
        if escena is None or self._geo3d is None or not self._v3d_visible(): return None
        rw, rh = escena.res_size
        # v0.47: resolución de la textura según lo cerca que esté la cámara (escalones, para no recalcular en cada paso de rueda)
        k = self.visor_3d.escala_necesaria(rw)
        return {"cw": max(1, int(rw * k)), "ch": max(1, int(rh * k)), "zoom": k, "pan_x": 0, "pan_y": 0}

    def v3d_player_static_view(self, static_strip):
        v = self.v3d_player_view()
        if v is None or (v["cw"], v["ch"]) == static_strip.size: return static_strip.copy()
        cache = getattr(self, "_v3d_static_cache", None)
        if cache is None or cache[0] is not static_strip:
            cache = self._v3d_static_cache = (static_strip, {})
        size = (v["cw"], v["ch"])
        if size not in cache[1]:
            cache[1][size] = static_strip.resize(size, PREVIEW_RESAMPLE, reducing_gap=3.0)
        return cache[1][size]

    def v3d_player_show(self, img):
        if self._geo3d is not None:
            self.visor_3d.set_strip(img, self._geo3d[0], self._geo3d[1])

    def player_static_view(self, static_res):
        v = self.player_view()
        canvas, _, _ = self.get_panned_image(static_res, v["zoom"], v["pan_x"], v["pan_y"], v["cw"], v["ch"])
        return canvas

    def player_show(self, img):
        for attr in ("lbl_arch_placeholder", "lbl_sys_placeholder"):
            w = getattr(self, attr, None)
            if w is not None and w.winfo_exists(): w.destroy()
        self._shown_strip, self._shown_strip_player = img, True
        self._player_target.show(self.lbl_live_preview._label, self._oscurecer(img) if self.loading_overlay_on else img)

    @staticmethod
    def _crear_iconos_reproductor():
        """Iconos dibujados (los glifos ▶/⏸ no existen en todas las fuentes de Windows)."""
        icons = {}
        for name in ("play", "pause"):
            img = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            if name == "play":
                d.polygon([(14, 8), (14, 40), (40, 24)], fill=(255, 255, 255, 255))
            else:
                d.rectangle([12, 9, 20, 39], fill=(255, 255, 255, 255))
                d.rectangle([28, 9, 36, 39], fill=(255, 255, 255, 255))
            icons[name] = ctk.CTkImage(light_image=img, dark_image=img, size=(12, 12))
        return icons

    @staticmethod
    def _fmt_tiempo(t):
        m, s = divmod(max(0.0, t), 60)
        return f"{int(m):02d}:{s:04.1f}"

    def player_state_changed(self):
        pl = self.player
        ready = pl.ready
        self.btn_player_play.configure(state="normal" if ready else "disabled", image=self._player_icons["pause" if pl.playing else "play"])
        self.player_slider.configure(state="normal" if ready else "disabled")
        if not ready:
            self.player_slider.set(0)
            self.lbl_player_time.configure(text="--:--.- / --:--.-")
            self.lbl_player_info.configure(text="No video ELEMENTS" if pl.escena is not None else "")
            return
        dur = pl.duration or 1.0
        if not self._player_scrubbing:
            self.player_slider.set(min(1.0, pl.t / dur))
        self.lbl_player_time.configure(text=f"{self._fmt_tiempo(pl.t)} / {self._fmt_tiempo(dur)}")
        if not pl.engaged:
            info = f"{len(pl.escena.videos)} videos · {len(pl.escena.instancias)} instances"
        elif pl.buffering:
            info = f"Buffering {int(pl.buffer_progress() * 100)}%"
        elif pl.playing:
            info = f"Proxy {pl.proxy_h}p · {PLAYER_FPS} fps"
        else:
            info = "Paused · full quality" if pl.hq_shown else "Paused"
        self.lbl_player_info.configure(text=info)

    # --- Controles ---
    def on_player_toggle(self):
        self.player.toggle()

    def on_player_loop(self):
        self.player.loop = self.player_loop_var.get()

    def on_player_scrub(self, value):
        if not self.player.ready: return
        if not self._player_scrubbing:
            self._player_scrubbing = True
            self._player_scrub_resume = self.player.playing
            self.player.pause()
        self.player.seek(float(value) * self.player.duration)

    def on_player_scrub_end(self, event=None):
        if not self._player_scrubbing: return
        resume = self._player_scrub_resume
        self._player_scrubbing = self._player_scrub_resume = False
        if not self.player.ready: return
        if resume:
            self.player.play()
        else:
            self.player.seek(self.player.t, hq=True)

    def auto_fit_zooms(self):
        self.update_idletasks()
        if not self.pil_preview_images or not self.pil_sys_images: return
        if self.pil_preview_images[self.preview_current_idx] is None: return
        
        if not getattr(self, 'manual_zoom_sys', False):
            img_sys = self.pil_sys_images[self.preview_current_idx]
            container_w = self.sys_image_container.winfo_width()
            container_h = self.sys_image_container.winfo_height()
            
            if container_w > 10 and container_h > 10 and img_sys.width > 0 and img_sys.height > 0: 
                ratio_w = container_w / float(img_sys.width)
                ratio_h = container_h / float(img_sys.height)
                self.zoom_factor_sys = min(ratio_w, ratio_h) * 0.92
            else: 
                self.zoom_factor_sys = 1.0

        if not getattr(self, 'manual_zoom_strip', False):
            img_strip = self.pil_preview_images[self.preview_current_idx]
            container_w = self.preview_image_container.winfo_width()
            container_h = self.preview_image_container.winfo_height()
            
            if container_w > 10 and container_h > 10 and img_strip.width > 0 and img_strip.height > 0: 
                ratio_w = container_w / float(img_strip.width)
                ratio_h = container_h / float(img_strip.height)
                self.zoom_factor_strip = min(ratio_w, ratio_h) * 0.92
            else: 
                self.zoom_factor_strip = 1.0

        self.actualizar_imagen_preview()

    def get_panned_image(self, img, zoom, pan_x, pan_y, container_w, container_h):
        sw, sh = max(1, int(img.width * zoom)), max(1, int(img.height * zoom))
        
        max_pan_x = max(0, (sw - container_w) / 2)
        max_pan_y = max(0, (sh - container_h) / 2)
        
        pan_x = max(-max_pan_x, min(max_pan_x, pan_x)) if sw > container_w else 0
        pan_y = max(-max_pan_y, min(max_pan_y, pan_y)) if sh > container_h else 0
        
        canvas = Image.new("RGBA", (container_w, container_h), (0, 0, 0, 0))
        
        paste_x = int((container_w - sw) / 2 + pan_x)
        paste_y = int((container_h - sh) / 2 + pan_y)
        
        # Solo se reescala la parte visible (antes se reescalaba la imagen entera en cada zoom/arrastre)
        rx1, ry1 = max(0, -paste_x), max(0, -paste_y)
        rx2, ry2 = min(sw, container_w - paste_x), min(sh, container_h - paste_y)
        if rx2 > rx1 and ry2 > ry1:
            sx, sy = img.width / float(sw), img.height / float(sh)
            visible = img.resize(
                (rx2 - rx1, ry2 - ry1), RESAMPLE_METHOD,
                box=(rx1 * sx, ry1 * sy, rx2 * sx, ry2 * sy), reducing_gap=3.0
            )
            canvas.paste(visible, (paste_x + rx1, paste_y + ry1))
        return canvas, pan_x, pan_y

    def actualizar_imagen_preview(self):
        if not self.pil_preview_images: return
        if self.pil_preview_images[self.preview_current_idx] is None: return
        if hasattr(self, 'lbl_sys_placeholder') and self.lbl_sys_placeholder.winfo_exists(): self.lbl_sys_placeholder.destroy()
        if hasattr(self, 'lbl_arch_placeholder') and self.lbl_arch_placeholder.winfo_exists(): self.lbl_arch_placeholder.destroy()
        
        img_strip = self.pil_preview_images[self.preview_current_idx]
        cw_strip = self.preview_image_container.winfo_width()
        ch_strip = self.preview_image_container.winfo_height()
        
        if self.player.engaged and self.player.ready:
            self.player.render()
        elif cw_strip > 10 and ch_strip > 10:
            final_strip, self.pan_x_strip, self.pan_y_strip = self.get_panned_image(
                img_strip, self.zoom_factor_strip, self.pan_x_strip, self.pan_y_strip, cw_strip, ch_strip
            )
            self._shown_strip, self._shown_strip_player = final_strip, False
            self._pintar_label(self.lbl_live_preview, final_strip)

        if not (self.player.engaged and self.player.ready):
            self._refrescar_3d()  # v0.46: key frame actual en 3D PREVIEW (con el reproductor lo pinta player.render)

        if self.player.engaged and self.player.ready and self.sys_vista.escena is not None:
            return  # el System File también lo pinta el reproductor (player.render de arriba)
        if not self._sys_visible():
            return  # v0.47: pestaña oculta

        img_sys = self.pil_sys_images[self.preview_current_idx]
        cw_sys = self.sys_image_container.winfo_width()
        ch_sys = self.sys_image_container.winfo_height()
        
        if cw_sys > 10 and ch_sys > 10:
            final_sys, self.pan_x_sys, self.pan_y_sys = self.get_panned_image(
                img_sys, self.zoom_factor_sys, self.pan_x_sys, self.pan_y_sys, cw_sys, ch_sys
            )
            self._shown_sys, self._shown_sys_player = final_sys, False
            self._pintar_label(self.lbl_sys_image, final_sys)

    def zoom_in_strip(self):
        self.manual_zoom_strip = True
        self.zoom_factor_strip *= 1.15 
        self.actualizar_imagen_preview()
        
    def zoom_out_strip(self):
        self.manual_zoom_strip = True
        self.zoom_factor_strip /= 1.15 
        self.actualizar_imagen_preview()
        
    def fit_strip(self):
        self.manual_zoom_strip = False
        self.pan_x_strip = 0
        self.pan_y_strip = 0
        self.auto_fit_zooms()

    def zoom_in_sys(self):
        self.manual_zoom_sys = True
        self.zoom_factor_sys *= 1.15
        self.actualizar_imagen_preview()

    def zoom_out_sys(self):
        self.manual_zoom_sys = True
        self.zoom_factor_sys /= 1.15
        self.actualizar_imagen_preview()
        
    def fit_sys(self):
        self.manual_zoom_sys = False
        self.pan_x_sys = 0
        self.pan_y_sys = 0
        self.auto_fit_zooms()

    def ciclar_preview(self, e=None):
        if not self.pil_preview_images: return
        n = len(self.pil_preview_images)
        for step in range(1, n + 1):
            idx = (self.preview_current_idx + step) % n
            if self.pil_preview_images[idx] is not None and self.pil_sys_images[idx] is not None:
                self.preview_current_idx = idx
                break
        self.actualizar_imagen_preview()

    def on_drag_start(self, event, view_type):
        self.drag_start_x = event.x_root
        self.drag_start_y = event.y_root
        self.is_dragging = False
        
    def on_drag_motion(self, event, view_type):
        dx = event.x_root - self.drag_start_x
        dy = event.y_root - self.drag_start_y
        
        if not self.is_dragging and (abs(dx) > 5 or abs(dy) > 5):
            self.is_dragging = True
            
        if self.is_dragging:
            if view_type == "strip":
                self.pan_x_strip += dx
                self.pan_y_strip += dy
            else:
                self.pan_x_sys += dx
                self.pan_y_sys += dy
                
            self.drag_start_x = event.x_root
            self.drag_start_y = event.y_root
            
            self.actualizar_imagen_preview()

    def on_drag_release(self, event, view_type):
        if not self.is_dragging:
            if self.player.engaged and self.player.ready:
                self.player.toggle()  # clic en cualquiera de las dos previews: play / pausa
            elif view_type == "strip":
                self.ciclar_preview()
        self.is_dragging = False

if __name__ == "__main__":
    MegaStripApp().mainloop()