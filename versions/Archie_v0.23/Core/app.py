import customtkinter as ctk
from tkinterdnd2 import TkinterDnD, DND_FILES
import os
import json
import math
import traceback
import numpy as np
import threading
from PIL import Image, ImageDraw, ImageFont
from moviepy import VideoFileClip

from dotenv import load_dotenv
from google import genai
from google.genai import types

from motor_distribucion import calcular_layout_data, generar_imagen_arquitectura
from queue_manager import QueueManager
from batch_manager import BatchManager

# 1. Definir rutas maestras dinámicas
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(BASE_DIR, "Config")
ASSETS_DIR = os.path.join(BASE_DIR, "Assets")

# 2. Cargar variables de entorno desde Config
load_dotenv(os.path.join(CONFIG_DIR, ".env"))
api_key = os.getenv("GEMINI_API_KEY")

try:
    RESAMPLE_METHOD = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_METHOD = Image.LANCZOS

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

class CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)

class MegaStripApp(CTkDnD):
    def __init__(self):
        super().__init__()
        self.title("AIM Auto Arch - AUTO UPDATE") 
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
        self.preview_current_idx = 0
        self.zoom_factor_strip = 1.0 
        self.zoom_factor_sys = 1.0 
        self.manual_zoom_strip = False
        self.manual_zoom_sys = False
        self.preview_enabled = False

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
        logo_path = os.path.join(ASSETS_DIR, "logo_aim.png")
        if os.path.exists(logo_path):
            img = Image.open(logo_path)
            self.logo_img = ctk.CTkImage(img, size=(int(40 * (img.width / img.height)), 40))
            ctk.CTkLabel(self.header_frame, image=self.logo_img, text="").pack(side="left", padx=(0, 10))

        # 2. Carga del logotipo de la herramienta
        logo_archie_path = os.path.join(ASSETS_DIR, "logo_Archie.png")
        if os.path.exists(logo_archie_path):
            img_archie = Image.open(logo_archie_path)
            self.logo_archie_img = ctk.CTkImage(img_archie, size=(int(40 * (img_archie.width / img_archie.height)), 40))
            ctk.CTkLabel(self.header_frame, image=self.logo_archie_img, text="").pack(side="left", padx=(0, 20))
            
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
        ctk.CTkLabel(self.col_media, text="MEDIA ASSETS", font=self.font_t2, text_color=self.c_text_dark).pack(anchor="w", padx=20, pady=(10, 5))

        self.drop_labels = {}
        self.archivos_cargados = {"background": None, "center_board": None, "static_logo": None, "anim_zone": None, "anim_zones": []}
        
        self.drop_zones_frame = ctk.CTkFrame(self.col_media, fg_color="transparent")
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
            hover_color="#D1007A", command=self.auto_create_architecture
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

        self.sys_preview_frame = ctk.CTkFrame(
            self.preview_area, 
            corner_radius=10, 
            border_width=2, 
            border_color=self.c_magenta,
            fg_color="#0c0c0c")
        self.sys_preview_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 5))
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

        self.output_path_var = ctk.StringVar(value=os.getcwd())
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
            self.col_left, "Minimum Margin (px)", 
            "Spacing in px between adjacent elements.",
            0, 100, 100, 20, self.update_margin_label
        )
        self.margin_val_label.configure(text="20 px")

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
        fields_system = [("Canvas Width (px)", "Canvas Width"), ("Canvas Height (px)", "Canvas Height"), ("Right Mask (px)", "Canvas Mask"), ("Offset X (px)", "Offset X"), ("Offset Y (px)", "Offset Y")]
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
            self.btn_create_arch.configure(state="disabled")
            return

        system_instruction = """
        IDENTITY AND OBJECTIVE
        You are Archie, the Senior AI Architecture Assistant for AIM Auto Arch.
        Your mission is to translate user natural language requests into optimal LED canvas layout configurations, understanding the physical and mathematical constraints of stadium display engines.

        CORE ENGINE PHYSICS & RULES:
        1. INDIRECT ANIM ZONE GENERATION (Crucial): You CANNOT directly create or set the quantity of "Anim Zones". The Python mathematical engine generates them automatically by filling remaining empty space after placing Static Logos.
        2. LOGOS AS SPACERS: To add Anim Zones, you MUST REDUCE static logo quantities (`qty_goal`, `qty_tl`, etc.) to free up physical pixels. To create visual separators between Anim Zones, you MUST INCREASE logo quantities. For example, to get "2 logos between animations in touchline", set `qty_tl`: 2.
        3. SPACING & MARGINS: To add more empty/black space or distance between elements, INCREASE `margin` (e.g., from 20 to 40 or 60 px). To compact elements, DECREASE `margin`.
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
        Valid keys you can include: "qty_goal", "qty_corner", "qty_cb", "qty_tl", "margin", "stretch_limit", "squash_logos", "stretch_fit_per", "gl_offset", "corner_logo", "logo_under", "qty_gl_under", "qty_tl_under", "show_cb", "amplify", "invert_order", "max_anim_gl", "max_anim_tl", "explicacion".
        
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

    def auto_create_architecture(self):
        self.btn_create_arch.configure(state="disabled", text="✨ Calculating Internally...")
        self.reset_zooms_and_pan()
        threading.Thread(target=self._native_auto_solver, daemon=True).start()

    def _native_auto_solver(self):
        stadium = self.stadium_selector_global.get()
        logo_path = self.archivos_cargados.get("static_logo")
        is_wide = False
        
        if logo_path:
            try:
                img = self.obtener_frame_pil(logo_path)
                if img and img.height > 0:
                    ratio = img.width / img.height
                    is_wide = ratio > 3.0
            except Exception as e: 
                print(f"[ARCHIE DEBUG] Error midiendo logo: {e}")

        w_total = int(self.inputs["Width"].get() or 0)
        h_total = int(self.inputs["Height"].get() or 0)
        goal_line = int(self.inputs["Goal Line Size"].get() or 0)
        cb_width = int(self.inputs["Center Board Width"].get() or 0)
        margin = int(self.margin_slider.get())
        gl_offset = int(self.offset_slider.get())

        best_config = None
        msg = ""

        if self.amplify_var.get():
            intentos = [
                {"qty_goal": 1, "qty_corner": 0, "qty_cb": 0, "qty_tl": 1, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Maximum alternation throughout the stadium (Goal Line + Touchline)"},
                {"qty_goal": 1, "qty_corner": 0, "qty_cb": 0, "qty_tl": 0, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Narrow Touchline detected. Logo Priority applied to Goal Lines."},
                {"qty_goal": 0, "qty_corner": 0, "qty_cb": 0, "qty_tl": 0, "stretch_limit": 15, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Extreme compression mode (Anim Zones only)"}
            ]
        elif is_wide:
            intentos = [
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": False, "hide_extreme_edges": False, "msg": "WIDE Logo: Full logos in corners and maximum Anim Zones density."},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False, "msg": "WIDE Logo: Split Corner activated (1/2 logo) to free up space."},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "WIDE Logo: Absolute edges cleared."},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 0, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "WIDE Logo: Reduction to 1 Anim Zone in Touchline."}
            ]
        else:
            intentos = [
                {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False, "msg": "NARROW Logo: Maximum density fitted perfectly."},
                {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Edges cleared to save the 5-logo layout in Goal Lines."},
                {"qty_goal": 5, "qty_corner": 1, "qty_cb": 2, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Reduction in Corners and Touchlines."},
                {"qty_goal": 3, "qty_corner": 1, "qty_cb": 1, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Reduction of Goal Lines to 3 logos."},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Minimalist mode activated due to lack of space."}
            ]

        for config in intentos:
            is_under = self.logo_under_var.get()
            c_logo = self.corner_logo_var.get() if is_under else config["corner_logo"]
            q_corner = (1 if c_logo else 0) if is_under else config["qty_corner"]

            test_conf = {
                "stretch_limit": config.get("stretch_limit", 15), "squash_logos": False, 
                "qty_goal": config["qty_goal"], "qty_corner": q_corner, 
                "qty_cb": config["qty_cb"], "qty_tl": config["qty_tl"], 
                "gl_offset": gl_offset, "corner_logo": c_logo,
                "hide_extreme_edges": config["hide_extreme_edges"],
                "logo_under": is_under,
                "qty_gl_under": int(self.qty_gl_under_slider.get()),
                "qty_tl_under": int(self.qty_tl_under_slider.get()),
                "show_cb": self.show_cb_var.get(),
                "amplify": self.amplify_var.get(),
                "invert_order": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
                "max_anim_gl": int(self.max_anim_gl_slider.get()),
                "max_anim_tl": int(self.max_anim_tl_slider.get())
            }
            
            try:
                datos = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin, self.archivos_cargados, test_conf)
                warnings = datos.get("warnings", []) if datos else ["error"]
                
                if "goal_overlap" not in warnings and "touchline_overlap" not in warnings:
                    best_config = test_conf
                    msg = "Auto-Solver: " + config["msg"]
                    break
            except Exception as e:
                print(f"[ARCHIE DEBUG] Auto-Solver trial failed: {e}")
                
        if not best_config:
            best_config = test_conf
            msg = "Auto-Solver: WARNING - Stadium too small or crowded. Overlap will occur."

        self.current_hide_edges = best_config["hide_extreme_edges"]

        self.after(0, self._actualizar_sliders_desde_ia, 
                   best_config["qty_goal"], best_config["qty_corner"], 
                   best_config["qty_cb"], best_config["qty_tl"], 
                   best_config["stretch_limit"], best_config["squash_logos"], 
                   self.stretch_fit_var.get(),
                   best_config["gl_offset"], best_config["corner_logo"],
                   best_config["logo_under"], best_config["qty_gl_under"], best_config["qty_tl_under"],
                   best_config.get("show_cb", True), best_config["amplify"],
                   getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get(),
                   best_config.get("max_anim_gl", 0), best_config.get("max_anim_tl", 0))
        
        self.preview_enabled = True 
        
        self.after(0, self.trigger_update)
        self.after(0, lambda: self.btn_create_arch.configure(state="normal", text="✨ Create Architecture"))
        self.after(0, self.append_chat, "System AI", msg)

    def aplicar_parametros_ia(self, qty_goal: int, qty_corner: int, qty_cb: int, qty_tl: int, stretch_limit: int, squash_logos: bool, stretch_fit_per: bool, gl_offset: int, corner_logo: bool, logo_under: bool = False, qty_gl_under: int = 1, qty_tl_under: int = 1, show_cb: bool = True, amplify: bool = False, invert_order: bool = False, max_anim_gl: int = 0, max_anim_tl: int = 0, margin: int = 20) -> dict:
        self.reset_zooms_and_pan()
        config_prueba = {
            "stretch_limit": stretch_limit, "squash_logos": squash_logos, "stretch_fit_per": stretch_fit_per,
            "qty_goal": qty_goal, "qty_corner": qty_corner, "qty_cb": qty_cb, "qty_tl": qty_tl, 
            "gl_offset": gl_offset, "corner_logo": corner_logo,
            "hide_extreme_edges": getattr(self, "current_hide_edges", False),
            "logo_under": logo_under, "qty_gl_under": qty_gl_under, "qty_tl_under": qty_tl_under,
            "show_cb": show_cb, "amplify": amplify,
            "max_anim_gl": max_anim_gl, "max_anim_tl": max_anim_tl
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
            
            self.after(0, self._actualizar_sliders_desde_ia, qty_goal, qty_corner, qty_cb, qty_tl, stretch_limit, squash_logos, stretch_fit_per, gl_offset, corner_logo, logo_under, qty_gl_under, qty_tl_under, show_cb, amplify, invert_order, max_anim_gl, max_anim_tl, margin_simulado)
            
            return {
                "status": "SUCCESS", 
                "mensaje": "The parameters fit perfectly and have been applied to the interface. Stop here and answer the user confirming the action."
            }
        except Exception as e:
            traceback.print_exc()
            print(f"[ARCHIE DEBUG] Error in aplicar_parametros_ia: {e}")
            return {"status": "ERROR", "mensaje": f"Math engine failed: {e}"}
    
    def _actualizar_sliders_desde_ia(self, qty_goal, qty_corner, qty_cb, qty_tl, stretch_limit, squash_logos, stretch_fit_per, gl_offset, corner_logo, logo_under=False, qty_gl_under=1, qty_tl_under=1, show_cb=True, amplify=False, invert_order=False, max_anim_gl=0, max_anim_tl=0, margin=20):
        if hasattr(self, "invert_order_var"):
            self.invert_order_var.set(invert_order)
            
        self.margin_slider.set(margin)
        self.update_margin_label(margin)
            
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
                    print(f"[ARCHIE DEBUG] Raw AI response: {response.text}")
                    self.after(0, self._restaurar_botones_ia)
                    return
                    
            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "503" in error_str or "UNAVAILABLE" in error_str:
                    if intento < max_intentos - 1:
                        espera = 2 ** (intento + 1)
                        print(f"[ARCHIE DEBUG] Google rate limit reached. Silently retrying in {espera}s...")
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
            if denom > 0:
                max_gl = max(1, math.floor((goal_line + margin) / denom))
                max_tl = max(1, math.floor((z2_w + margin) / denom))
            else:
                max_gl = max(1, math.floor(goal_line / logo_w))
                max_tl = max(1, math.floor(z2_w / logo_w))
            return max_gl, max_tl

        except Exception as e: 
            traceback.print_exc()
            return 15, 15

    def load_stadium_library(self):
        library_path = os.path.join(CONFIG_DIR, "stadium_library.json")
        try:
            with open(library_path, "r", encoding="utf-8") as f: 
                return json.load(f)
        except: 
            return {"Custom": {"Width": 25000, "Height": 72, "Goal Line Size": 5000, "Center Board Width": 0, "Canvas Width": 3840, "Canvas Height": 2160, "Canvas Mask": 96, "Offset X": 0, "Offset Y": 0}}
    
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
        
        if hasattr(self, "stadium_search_var") and self.stadium_search_var.get() != "":
            self.stadium_search_var.set("")
            
        data = self.stadium_data[choice]
        is_custom = (choice == "Custom")
        
        self.amplify_var.set("AMPLIFY" in choice.upper())
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

    def select_output_path(self):
        if folder := ctk.filedialog.askdirectory(title="Select Destination"): self.output_path_var.set(folder)

    def on_drop(self, event, key):
        file_path = event.data.strip('{}')
        self.archivos_cargados[key] = file_path
        self.drop_labels[key].configure(text=f"Loaded:\n{os.path.basename(file_path)}", text_color="#2CC985")
        if key == "center_board":
            self.show_cb_var.set(True)
        self.check_and_activate_logo_under(file_path)
        self.reset_preview_state()
        self.reset_zooms_and_pan()
        self.trigger_update()

    def calcular_layout(self):
        try:
            w_total = int(self.inputs["Width"].get() or 0)
            h_total = int(self.inputs["Height"].get() or 0)
            goal_line = int(self.inputs["Goal Line Size"].get() or 0)
            cb_width = int(self.inputs["Center Board Width"].get() or 0)
            margin = int(self.margin_slider.get())
            
            config = {
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
                "invert_order": getattr(self, "invert_order_var", ctk.BooleanVar(value=False)).get()
            }
            res = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin, self.archivos_cargados, config)
            if res:
                res["config"] = config
            return res
        except Exception as e:
            traceback.print_exc()
            print(f"[ARCHIE DEBUG] Error CRÍTICO en calcular_layout: {e}")
            return None

    def trigger_update(self, event=None, *args):
        if not getattr(self, 'preview_enabled', False): return
        if getattr(self, 'preview_timer', None): self.after_cancel(self.preview_timer)
        valid_anims = [f for f in self.archivos_cargados["anim_zones"] if f is not None]
        if not valid_anims and not self.archivos_cargados["background"] and not self.archivos_cargados["static_logo"] and not self.archivos_cargados["center_board"]: return
        
        self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Calculating...)", text_color="#E67E22")
        self.preview_timer = self.after(800, self.update_live_preview)

    def reset_preview_state(self):
        self.preview_enabled = False
        if getattr(self, 'preview_timer', None): 
            self.after_cancel(self.preview_timer)
        self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Pending Update... Press Create Architecture)", text_color="gray")

    def trigger_resize(self, event=None):
        if getattr(self, 'resize_timer', None): self.after_cancel(self.resize_timer)
        self.resize_timer = self.after(150, self.auto_fit_zooms)

    def obtener_frame_pil(self, ruta, h_target=None):
        if not ruta: return Image.new("RGBA", (100, h_target or 100), (0,0,0,0))
        ext = os.path.splitext(ruta)[1].lower()
        if ext in ['.mov', '.mp4', '.avi', '.m4v']:
            try:
                with VideoFileClip(ruta, has_mask=True) as cl:
                    if h_target: cl = cl.resized(height=h_target)
                    fr = cl.get_frame(0)
                    if getattr(cl, 'mask', None):
                        ma = (cl.mask.get_frame(0)*255).astype(np.uint8)
                        return Image.fromarray(np.dstack((fr, ma)))
                    else:
                        return Image.fromarray(fr).convert("RGBA")
            except Exception as e:
                print(f"[ARCHIE DEBUG] Fallo al extraer frame de vídeo '{ruta}': {e}")
                return Image.new("RGBA", (100, h_target or 100), (255, 0, 0, 255))
        else:
            return Image.open(ruta).convert("RGBA")

    def safe_crop(self, img, x1, y1, x2, y2):
        x1, x2 = sorted([max(0, min(x1, img.width)), max(0, min(x2, img.width))])
        y1, y2 = sorted([max(0, min(y1, img.height)), max(0, min(y2, img.height))])
        return img.crop((x1, y1, x2, y2))

    def update_live_preview(self):
        print("[ARCHIE DEBUG] Iniciando update_live_preview...")
        try:
            datos = self.calcular_layout()
            if not datos: 
                self.lbl_preview_status.configure(text="Missing / Invalid Parameters", text_color="#E74C3C")
                return

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
            
            w_total = datos["dimensiones_globales"]["width"]
            h_total = datos["dimensiones_globales"]["height"]
            sys_w = int(self.inputs["Canvas Width"].get() or 3840)
            sys_h = int(self.inputs["Canvas Height"].get() or 2160)
            sys_mask = int(self.inputs["Canvas Mask"].get() or 96)
            sys_off_x = int(self.inputs["Offset X"].get() or 0)
            sys_off_y = int(self.inputs["Offset Y"].get() or 0)

            is_amplify = datos.get("config", {}).get("amplify", False)
            if is_amplify and hasattr(self, 'preview_row_idx'):
                h_slice = h_total // 2
                if self.preview_row_idx == 1:
                    src_y_start, src_y_end = h_slice, h_total
                else:
                    src_y_start, src_y_end = 0, h_slice
            else:
                h_slice = h_total
                src_y_start, src_y_end = 0, h_total

            advance = sys_w - sys_mask
            if advance <= 0: raise ValueError("Mask > Width")
            total_rows = math.ceil((w_total + sys_off_x) / advance)
            
            pil_logo_raw = self.obtener_frame_pil(self.archivos_cargados.get("static_logo"), h_total)
            
            valid_anims = [f for f in self.archivos_cargados.get("anim_zones", []) if f is not None]
            if not valid_anims and self.archivos_cargados.get("anim_zone"): valid_anims = [self.archivos_cargados["anim_zone"]]
            
            dur_base = 1.0
            for path in valid_anims:
                try:
                    ext = os.path.splitext(path)[1].lower()
                    if ext in ['.mov', '.mp4']:
                        with VideoFileClip(path, has_mask=True) as cl:
                            if cl.duration > dur_base: dur_base = cl.duration
                except: pass

            tiempos = [dur_base * (i / 8.0) for i in range(8)]
            
            self.pil_preview_images, self.pil_sys_images = [], []

            logo_path = self.archivos_cargados.get("static_logo")
            
            from motor_distribucion import generar_imagen_arquitectura
            gl_offset_pct = self.offset_slider.get() / 100.0

            bg_clip_preview = None
            bg_path = self.archivos_cargados.get("background")
            if bg_path and os.path.splitext(bg_path)[1].lower() in ['.mov', '.mp4', '.m4v', '.avi']:
                try:
                    bg_clip_preview = VideoFileClip(bg_path, has_mask=False)
                except Exception as e:
                    print(f"[ARCHIE DEBUG] Error abriendo vídeo de fondo en UI: {e}")

            pil_bg_static = self.obtener_frame_pil(bg_path, h_total)

            try:
                for t in tiempos:
                    if bg_clip_preview:
                        try:
                            fr_bg = bg_clip_preview.get_frame(t % bg_clip_preview.duration)
                            pil_bg_current = Image.fromarray(fr_bg).convert("RGBA")
                        except Exception:
                            pil_bg_current = pil_bg_static
                    else:
                        pil_bg_current = pil_bg_static

                    new_bg_w = max(1, int(h_total * (pil_bg_current.width / max(1, pil_bg_current.height))))
                    pil_bg_current = pil_bg_current.resize((new_bg_w, h_total), RESAMPLE_METHOD)
                    
                    master = Image.new("RGBA", (w_total, h_total))
                    for x in range(0, w_total, new_bg_w): 
                        master.paste(pil_bg_current, (x, 0))

                    pil_anims_frames = []
                    for path in valid_anims:
                        try:
                            ext = os.path.splitext(path)[1].lower()
                            if ext in ['.mov', '.mp4']:
                                with VideoFileClip(path, has_mask=True) as cl:
                                    fr = cl.get_frame(t % cl.duration)
                                    if getattr(cl, 'mask', None):
                                        ma = (cl.mask.get_frame(t % cl.duration)*255).astype(np.uint8)
                                        img = Image.fromarray(np.dstack((fr, ma)))
                                    else: img = Image.fromarray(fr).convert("RGBA")
                            else: img = Image.open(path).convert("RGBA")
                            pil_anims_frames.append(img)
                        except: pil_anims_frames.append(Image.new("RGBA", (3000, h_total), (255, 0, 0, 255)))
                    
                    if not pil_anims_frames: pil_anims_frames = [Image.new("RGBA", (3000, h_total), (255, 0, 0, 255))]

                    logo_anim_img = pil_logo_raw
                    if logo_path:
                        try:
                            ext = os.path.splitext(logo_path)[1].lower()
                            if ext in ['.mov', '.mp4']:
                                with VideoFileClip(logo_path, has_mask=True) as cl:
                                    fr = cl.get_frame(t % cl.duration)
                                    if getattr(cl, 'mask', None):
                                        ma = (cl.mask.get_frame(t % cl.duration)*255).astype(np.uint8)
                                        logo_anim_img = Image.fromarray(np.dstack((fr, ma)))
                                    else: 
                                        logo_anim_img = Image.fromarray(fr).convert("RGBA")
                        except Exception as e:
                            print(f"[ARCHIE DEBUG] Fallo obteniendo frame animado del logo: {e}")

                    for zona in datos["zonas"].values():
                        for e in zona["elementos"]:
                            if e["tipo"] == "center_board" and e["width"] > 0:
                                cb_path = self.archivos_cargados.get("center_board")
                                mostrar_cb = self.show_cb_var.get() and datos.get("config", {}).get("show_cb", True)
                                if cb_path and mostrar_cb:
                                    pil_cb_raw = self.obtener_frame_pil(cb_path, h_target=h_total)
                                    cb_letterbox = aplicar_letterbox_pil(pil_cb_raw, e["width"], h_total, RESAMPLE_METHOD)
                                    master.paste(cb_letterbox, (e["x_absoluta"], 0), cb_letterbox)
                                else:
                                    master.paste(Image.new("RGBA", (e["width"], h_total), (0,0,0,255)), (e["x_absoluta"], 0))
                            elif e["tipo"] == "anim_zone":
                                a_idx = e.get("anim_idx", 0) % len(pil_anims_frames)
                                anim_img = pil_anims_frames[a_idx].resize((e["width"], h_total), RESAMPLE_METHOD)
                                master.paste(anim_img, (e["x_absoluta"], 0), anim_img)
                                
                    for zona in datos["zonas"].values():
                        for e in zona["elementos"]:
                            if e["tipo"] == "static_logo":
                                if e.get("logo_under_type") == "animated":
                                    logo_img = logo_anim_img.resize((e["width"], h_total), RESAMPLE_METHOD)
                                else:
                                    logo_img = pil_logo_raw.resize((e["width"], h_total), RESAMPLE_METHOD)
                                    
                                master.paste(logo_img, (e["x_absoluta"], 0), logo_img)

                    res = generar_imagen_arquitectura(master, datos, h_total, w_total, gl_offset_pct, bg_color=(0, 0, 0, 0))
                    self.pil_preview_images.append(res)

                    master_slice = self.safe_crop(master, 0, src_y_start, w_total, src_y_end)
                    sys_canvas = Image.new("RGBA", (sys_w, sys_h), (0,0,0,255))

                    for i in range(total_rows):
                        x_m_strip = i * advance - sys_off_x
                        crop_x1 = max(0, x_m_strip)
                        crop_x2 = min(w_total, x_m_strip + advance)
                        if crop_x1 >= crop_x2: continue
                        x_canvas = abs(x_m_strip) if x_m_strip < 0 else 0
                        paste_y = sys_off_y + (i * h_slice)
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

                    self.pil_sys_images.append(sys_canvas)
            finally:
                if bg_clip_preview:
                    try:
                        bg_clip_preview.close()
                    except Exception:
                        pass
                
            self.after(100, self.auto_fit_zooms)
            self.lbl_preview_status.configure(text="ARCHITECTURE PREVIEW (Updated)", text_color=self.c_magenta)
            print("[ARCHIE DEBUG] update_live_preview procesado con éxito.")
        
        except Exception as e: 
            traceback.print_exc()
            err_txt = f"Geometry Error: {type(e).__name__} - {str(e)[:40]}"
            print(f"[ARCHIE DEBUG] {err_txt}")
            self.lbl_preview_status.configure(text=err_txt, text_color="#E74C3C")
    
    def auto_fit_zooms(self):
        self.update_idletasks()
        if not self.pil_preview_images or not self.pil_sys_images: return
        
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
        resized = img.resize((sw, sh), RESAMPLE_METHOD)
        
        max_pan_x = max(0, (sw - container_w) / 2)
        max_pan_y = max(0, (sh - container_h) / 2)
        
        pan_x = max(-max_pan_x, min(max_pan_x, pan_x)) if sw > container_w else 0
        pan_y = max(-max_pan_y, min(max_pan_y, pan_y)) if sh > container_h else 0
        
        canvas = Image.new("RGBA", (container_w, container_h), (0, 0, 0, 0))
        
        paste_x = int((container_w - sw) / 2 + pan_x)
        paste_y = int((container_h - sh) / 2 + pan_y)
        
        canvas.paste(resized, (paste_x, paste_y))
        return canvas, pan_x, pan_y

    def actualizar_imagen_preview(self):
        if not self.pil_preview_images: return
        if hasattr(self, 'lbl_sys_placeholder') and self.lbl_sys_placeholder.winfo_exists(): self.lbl_sys_placeholder.destroy()
        if hasattr(self, 'lbl_arch_placeholder') and self.lbl_arch_placeholder.winfo_exists(): self.lbl_arch_placeholder.destroy()
        
        scale = self.get_ctk_scale()
        if scale <= 0: scale = 1.0
        
        img_strip = self.pil_preview_images[self.preview_current_idx]
        cw_strip = self.preview_image_container.winfo_width()
        ch_strip = self.preview_image_container.winfo_height()
        
        if cw_strip > 10 and ch_strip > 10:
            final_strip, self.pan_x_strip, self.pan_y_strip = self.get_panned_image(
                img_strip, self.zoom_factor_strip, self.pan_x_strip, self.pan_y_strip, cw_strip, ch_strip
            )
            cw_log = max(1, int(cw_strip / scale))
            ch_log = max(1, int(ch_strip / scale))
            self.lbl_live_preview.configure(
                text="", 
                image=ctk.CTkImage(light_image=final_strip, dark_image=final_strip, size=(cw_log, ch_log))
            )

        img_sys = self.pil_sys_images[self.preview_current_idx]
        cw_sys = self.sys_image_container.winfo_width()
        ch_sys = self.sys_image_container.winfo_height()
        
        if cw_sys > 10 and ch_sys > 10:
            final_sys, self.pan_x_sys, self.pan_y_sys = self.get_panned_image(
                img_sys, self.zoom_factor_sys, self.pan_x_sys, self.pan_y_sys, cw_sys, ch_sys
            )
            cw_log_sys = max(1, int(cw_sys / scale))
            ch_log_sys = max(1, int(ch_sys / scale))
            self.lbl_sys_image.configure(
                text="", 
                image=ctk.CTkImage(light_image=final_sys, dark_image=final_sys, size=(cw_log_sys, ch_log_sys))
            )

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
        self.preview_current_idx = (self.preview_current_idx + 1) % len(self.pil_preview_images)
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
        if not self.is_dragging and view_type == "strip":
            self.ciclar_preview()
        self.is_dragging = False

if __name__ == "__main__":
    MegaStripApp().mainloop()