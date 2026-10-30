import os
import threading
import numpy as np
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont
from moviepy import VideoFileClip

from motor_distribucion import calcular_layout_data, generar_imagen_arquitectura
from log_utils import debug, log_error

try:
    RESAMPLE_METHOD = Image.Resampling.LANCZOS
except AttributeError:
    RESAMPLE_METHOD = Image.LANCZOS

class BatchManager:
    def __init__(self, app):
        self.app = app
        self.modal = None
        self.checkbox_vars = {}

    def aplicar_letterbox_pil(self, img, target_w, target_h, resample_method):
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

    def open_modal(self):
        if self.modal is not None and self.modal.winfo_exists():
            self.modal.lift()
            return

        self.modal = ctk.CTkToplevel(self.app)
        self.modal.title("Batch Setup - Competition Filter")
        self.modal.geometry("520x720")
        self.modal.transient(self.app)
        self.modal.grab_set()
        self.modal.configure(fg_color=self.app.c_grey)

        header = ctk.CTkFrame(self.modal, fg_color="transparent")
        header.pack(fill="x", padx=20, pady=(20, 10))
        
        ctk.CTkLabel(header, text="Select Stadiums for Batch Render", font=ctk.CTkFont(family="Montserrat", size=22, weight="bold"), text_color=self.app.c_magenta, wraplength=480, justify="left").pack(anchor="w")
        ctk.CTkLabel(header, text="Filter by UEFA competition tags or search by name. The system will auto-solve overlaps and generate QC files.", font=self.app.font_t3, text_color=self.app.c_text_muted, justify="left", wraplength=480).pack(anchor="w", pady=(5, 0))

        actions = ctk.CTkFrame(self.modal, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=5)
        ctk.CTkButton(actions, text="Select Visible", width=110, fg_color="#2b2b2b", hover_color="#404040", command=self.select_all).pack(side="left", padx=(0, 10))
        ctk.CTkButton(actions, text="Deselect Visible", width=110, fg_color="#E0E0E0", text_color=self.app.c_text_dark, hover_color="#D0D0D0", command=self.deselect_all).pack(side="left")

        search_frame = ctk.CTkFrame(self.modal, fg_color="transparent")
        search_frame.pack(fill="x", padx=20, pady=(10, 5))
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", self.filter_stadiums)
        self.search_entry = ctk.CTkEntry(search_frame, placeholder_text="🔍 Search stadium name...", textvariable=self.search_var)
        self.search_entry.pack(fill="x")

        tags_frame = ctk.CTkFrame(self.modal, fg_color="transparent")
        tags_frame.pack(fill="x", padx=20, pady=(0, 10))
        
        self.filter_comp_var = ctk.StringVar(value="ALL")
        comp_selector = ctk.CTkSegmentedButton(
            tags_frame,
            values=["ALL", "UCL", "UEL", "UECL", "UWCL"],
            variable=self.filter_comp_var,
            command=self.filter_stadiums,
            selected_color=self.app.c_magenta,
            selected_hover_color="#D1007A",
            unselected_color=self.app.c_light_grey,
            unselected_hover_color="#E0E0E0",
            text_color=self.app.c_text_dark,
            font=ctk.CTkFont(size=12, weight="bold")
        )
        comp_selector.pack(fill="x")

        self.scroll_frame = ctk.CTkScrollableFrame(self.modal, corner_radius=10, fg_color=self.app.c_white)
        self.scroll_frame.pack(fill="both", expand=True, padx=20, pady=5)

        if not self.checkbox_vars:
            for stadium_name in self.app.stadium_data.keys():
                if stadium_name == "Custom": continue
                self.checkbox_vars[stadium_name] = ctk.BooleanVar(value=False)

        self.filter_stadiums()

        ctk.CTkButton(
            self.modal, text="Send Batch to Queue", font=ctk.CTkFont(size=16, weight="bold"), 
            height=45, fg_color=self.app.c_magenta, hover_color="#D1007A", 
            command=self.start_batch_process
        ).pack(fill="x", padx=20, pady=15)

    def _get_matching_stadiums(self):
        query = self.search_var.get().lower()
        selected_comp = getattr(self, "filter_comp_var", ctk.StringVar(value="ALL")).get()
        
        matching = []
        for stadium_name in self.checkbox_vars.keys():
            stadium_info = self.app.stadium_data.get(stadium_name, {})
            comp_tag = stadium_info.get("Competition", "NONE")
            
            match_comp = (selected_comp == "ALL") or (comp_tag == selected_comp)
            match_text = query in stadium_name.lower()
            
            if match_comp and match_text:
                matching.append((stadium_name, comp_tag))
        return matching

    def sync_stadiums(self):
        if not self.checkbox_vars:
            return  # el modal aún no se ha abierto; se construirá con los datos nuevos
        names = [n for n in self.app.stadium_data.keys() if n != "Custom"]
        self.checkbox_vars = {n: self.checkbox_vars.get(n) or ctk.BooleanVar(value=False) for n in names}
        modal = getattr(self, "modal", None)
        
        if modal is not None and modal.winfo_exists():
            self.filter_stadiums()

    def filter_stadiums(self, *args):
        for widget in self.scroll_frame.winfo_children():
            widget.destroy()
            
        matching_stadiums = self._get_matching_stadiums()
        
        if not matching_stadiums:
            ctk.CTkLabel(self.scroll_frame, text="No stadiums found for this filter.", text_color="gray", font=self.app.font_t3).pack(pady=20)
            return

        for stadium_name, comp_tag in matching_stadiums:
            var = self.checkbox_vars[stadium_name]
            label_text = f"[{comp_tag}]  {stadium_name}" if getattr(self, "filter_comp_var", ctk.StringVar(value="ALL")).get() == "ALL" else stadium_name
            
            chk = ctk.CTkCheckBox(
                self.scroll_frame, 
                text=label_text, 
                variable=var, 
                font=self.app.font_t3, 
                fg_color=self.app.c_magenta,
                hover_color="#D1007A"
            )
            chk.pack(anchor="w", padx=10, pady=6)

    def select_all(self):
        for stadium_name, _ in self._get_matching_stadiums():
            self.checkbox_vars[stadium_name].set(True)

    def deselect_all(self):
        for stadium_name, _ in self._get_matching_stadiums():
            self.checkbox_vars[stadium_name].set(False)

    def start_batch_process(self):
        selected_stadiums = [name for name, var in self.checkbox_vars.items() if var.get()]
        if not selected_stadiums: return

        self.modal.destroy()
        
        self.app.overlay_frame.lift()
        self.app.overlay_frame.place(relx=0.5, rely=0.5, relwidth=1, relheight=1, anchor="center")
        self.app.lbl_overlay_title.configure(text="PREPARING BATCH...", text_color=self.app.c_magenta)
        self.app.lbl_overlay_status.configure(text="Calculating geometries and generating QC images...", text_color="white")
        # v0.38: se puede cancelar mientras se calculan los frames de Quality Check
        self.cancel_batch_flag = False
        self.app.btn_overlay_cancel.configure(text="Cancel", state="normal", fg_color="#C0392B", hover_color="#E74C3C", command=self.cancel_batch_process)
        
        self.app.overlay_bar.configure(mode="indeterminate")
        self.app.overlay_bar.start()
        
        threading.Thread(target=self._process_batch_worker, args=(selected_stadiums,), daemon=True).start()

    def cancel_batch_process(self):
        self.cancel_batch_flag = True
        self.app.lbl_overlay_status.configure(text="Cancelling batch... Please wait.", text_color="#E67E22")
        self.app.btn_overlay_cancel.configure(state="disabled")

    def _on_batch_cancelled(self):
        self.app.overlay_bar.stop()
        self.app.overlay_bar.configure(mode="determinate")
        self.app.overlay_frame.place_forget()
        self.app.btn_overlay_cancel.configure(text="Cancel Render", state="normal", command=self.app.queue_manager.cancel_direct_render)
        debug("Batch cancelado por el usuario: no se añade nada a la cola.")

    def _process_batch_worker(self, stadiums):
        out_dir = self.app.output_path_var.get()
        base_filename = self.app.output_filename_var.get().strip()
        cancelado = lambda: getattr(self, "cancel_batch_flag", False)
        preparados = []  # (data, estadio): se añaden a la cola solo si el batch termina sin cancelar

        for n, stadium_name in enumerate(stadiums, 1):
            if cancelado(): break
            self.app.after(0, lambda n=n, s=stadium_name: self.app.lbl_overlay_status.configure(
                text=f"QC images {n}/{len(stadiums)}: {s}", text_color="white"))
            stadium_info = self.app.stadium_data[stadium_name]
            best_config = self.headless_auto_solver(stadium_info)
            
            data = calcular_layout_data(
                w_total=stadium_info["Width"], h_total=stadium_info["Height"], 
                goal_line=stadium_info["Goal Line Size"], cb_width=stadium_info["Center Board Width"], 
                margin=int(self.app.margin_slider.get()), 
                archivos_cargados=self.app.archivos_cargados, 
                config=best_config
            )
            
            if data:
                data["config"] = best_config
            
            sys_w = stadium_info["Canvas Width"]
            sys_h = stadium_info["Canvas Height"]
            sys_mask = stadium_info["Canvas Mask"]
            
            data["system_setup"] = {
                "canvas_width": sys_w, "canvas_height": sys_h, 
                "canvas_mask": sys_mask, "offset_x": stadium_info["Offset X"], "offset_y": stadium_info["Offset Y"]
            }
            
            # NUEVO BLOQUE: system_file
            data["system_file"] = {
                "width": sys_w,
                "height": sys_h,
                "offset": {
                    "x": stadium_info["Offset X"],
                    "y": stadium_info["Offset Y"]
                },
                "mask": {
                    "x": sys_w - sys_mask,
                    "y": 0,
                    "width": sys_mask,
                    "height": sys_h
                }
            }

            self.generate_reference_pngs(data, stadium_name, base_filename, out_dir, cancel_check=cancelado)
            preparados.append((data, stadium_name))

        if cancelado():
            self.app.after(0, self._on_batch_cancelled)
            return
        for d, n in preparados:
            self.app.after(0, lambda d=d, n=n: self.app.queue_manager.add_to_queue("SYSTEM", batch_data=d, batch_preset=n))
        self.app.after(0, lambda: self.show_qc_warning(out_dir))

    def show_qc_warning(self, out_dir):
        self.app.btn_overlay_cancel.configure(text="Cancel Render", state="normal", command=self.app.queue_manager.cancel_direct_render)
        self.app.overlay_bar.stop()
        self.app.overlay_bar.configure(mode="determinate")
        self.app.overlay_frame.place_forget()

        qc_modal = ctk.CTkToplevel(self.app)
        qc_modal.title("Quality Control (QC)")
        qc_modal.geometry("550x280")
        qc_modal.transient(self.app)
        qc_modal.grab_set()
        qc_modal.configure(fg_color=self.app.c_grey)

        ctk.CTkLabel(qc_modal, text="⚠️ BATCH READY", font=self.app.font_t1, text_color="#E67E22").pack(pady=(20, 10))
        
        msg = "Please check the generated reference PNG images to verify that the visual architecture is correct before starting the render queue."
        ctk.CTkLabel(qc_modal, text=msg, font=self.app.font_t3, text_color=self.app.c_text_dark, wraplength=480).pack(padx=20, pady=(0, 10))
        
        dir_frame = ctk.CTkFrame(qc_modal, fg_color=self.app.c_white, corner_radius=8)
        dir_frame.pack(fill="x", padx=30, pady=(0, 20))
        ctk.CTkLabel(dir_frame, text=out_dir, font=ctk.CTkFont(size=12, weight="bold"), text_color=self.app.c_magenta, wraplength=450).pack(pady=10)

        btn_frame = ctk.CTkFrame(qc_modal, fg_color="transparent")
        btn_frame.pack(fill="x", padx=30, pady=(0, 20))
        btn_frame.grid_columnconfigure(0, weight=1)
        btn_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            btn_frame, text="Open Folder", font=ctk.CTkFont(weight="bold"), 
            fg_color="#2b2b2b", hover_color="#404040", text_color=self.app.c_white,
            command=lambda: os.startfile(out_dir) if os.name == 'nt' else os.system(f'open "{out_dir}"')
        ).grid(row=0, column=0, padx=(0, 5), sticky="ew")

        ctk.CTkButton(
            btn_frame, text="Got it", font=ctk.CTkFont(weight="bold"), 
            fg_color=self.app.c_magenta, hover_color="#D1007A", 
            command=qc_modal.destroy
        ).grid(row=0, column=1, padx=(5, 0), sticky="ew")

    def headless_auto_solver(self, stadium_info, stadium_name=""):
        ui_config = {
            "qty_goal": int(self.app.qty_goal_slider.get()),
            "qty_corner": int(self.app.qty_corner_slider.get()),
            "qty_cb": int(self.app.qty_cb_slider.get()),
            "qty_tl": int(self.app.qty_tl_slider.get()),
            "stretch_limit": int(self.app.stretch_slider.get()),
            "squash_logos": self.app.squash_logos_var.get(),
            "stretch_fit_per": self.app.stretch_fit_var.get(),
            "gl_offset": int(self.app.offset_slider.get()),
            "corner_logo": self.app.corner_logo_var.get(),
            "logo_under": self.app.logo_under_var.get(),
            "qty_gl_under": int(self.app.qty_gl_under_slider.get()),
            "qty_tl_under": int(self.app.qty_tl_under_slider.get()),
            "show_cb": getattr(self.app, "show_cb_var", ctk.BooleanVar(value=True)).get(),
            "amplify": self.app.amplify_var.get() or ("AMPLIFY" in stadium_name.upper()),
            "invert_order": getattr(self.app, "invert_order_var", ctk.BooleanVar(value=False)).get(),
            "hide_extreme_edges": getattr(self.app, "current_hide_edges", False),
            "max_anim_gl": int(self.app.max_anim_gl_slider.get()),
            "max_anim_tl": int(self.app.max_anim_tl_slider.get())
        }

        logo_path = self.app.archivos_cargados.get("static_logo")
        is_wide = False
        if logo_path:
            try:
                img = self.app.obtener_frame_pil(logo_path)
                if img and img.height > 0:
                    if (img.width / img.height) > 3.0: is_wide = True
            except: pass

        w_total = stadium_info["Width"]
        h_total = stadium_info["Height"]
        goal_line = stadium_info["Goal Line Size"]
        cb_width = stadium_info["Center Board Width"]
        margin = int(self.app.margin_slider.get())

        intentos = [ui_config]

        if ui_config["amplify"]:
            fallbacks = [
                {"qty_goal": 2, "qty_corner": 0, "qty_cb": 0, "qty_tl": 2, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True},
                {"qty_goal": 1, "qty_corner": 0, "qty_cb": 0, "qty_tl": 1, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True},
                {"qty_goal": 0, "qty_corner": 0, "qty_cb": 0, "qty_tl": 0, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True}
            ]
        elif is_wide:
            fallbacks = [
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": False, "hide_extreme_edges": False},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 0, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True}
            ]
        else:
            fallbacks = [
                {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False},
                {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True},
                {"qty_goal": 5, "qty_corner": 1, "qty_cb": 2, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True},
                {"qty_goal": 3, "qty_corner": 1, "qty_cb": 1, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True},
                {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True}
            ]
        
        intentos.extend(fallbacks)

        best_config = None
        for config in intentos:
            # --- BLINDAJE ARCHIE EN BATCH: Respetar apagado de Corner Logo en modo Logo Under ---
            is_under = ui_config["logo_under"]
            c_logo = ui_config["corner_logo"] if is_under else config["corner_logo"]
            q_corner = (1 if c_logo else 0) if is_under else config["qty_corner"]

            test_conf = ui_config.copy()
            test_conf.update(config)
            test_conf["corner_logo"] = c_logo
            test_conf["qty_corner"] = q_corner
            
            datos = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin, self.app.archivos_cargados, test_conf)
            warnings = datos.get("warnings", []) if datos else ["error"]
            
            if "goal_overlap" not in warnings and "touchline_overlap" not in warnings:
                best_config = test_conf
                break
                
        return best_config if best_config else ui_config

    def generate_reference_pngs(self, datos, stadium_name, base_filename, out_dir, cancel_check=None):
        w_total = datos["dimensiones_globales"]["width"]
        h_total = datos["dimensiones_globales"]["height"]
        
        valid_anims = [f for f in self.app.archivos_cargados.get("anim_zones", []) if f is not None]
        if not valid_anims and self.app.archivos_cargados.get("anim_zone"):
            valid_anims = [self.app.archivos_cargados["anim_zone"]]
            
        dur_base = 1.0
        for path in valid_anims:
            try:
                ext = os.path.splitext(path)[1].lower()
                if ext in ['.mov', '.mp4']:
                    with VideoFileClip(path, has_mask=True) as cl:
                        if cl.duration > dur_base: dur_base = cl.duration
            except: pass

        tiempos = [0.0, dur_base * 0.25, dur_base * 0.50, dur_base * 0.75]
        
        pil_bg = self.app.obtener_frame_pil(self.app.archivos_cargados.get("background"), h_total)
        new_bg_w = max(1, int(h_total * (pil_bg.width / max(1, pil_bg.height))))
        pil_bg = pil_bg.resize((new_bg_w, h_total), RESAMPLE_METHOD)
        canvas_bg = Image.new("RGBA", (w_total, h_total))
        for x in range(0, w_total, new_bg_w): canvas_bg.paste(pil_bg, (x, 0))

        pil_logo_raw = self.app.obtener_frame_pil(self.app.archivos_cargados.get("static_logo"), h_total)
        logo_path = self.app.archivos_cargados.get("static_logo")

        stadium_clean = stadium_name.replace(" ", "_")
        gl_offset_pct = datos.get("config", {}).get("gl_offset", 0) / 100.0
        
        for idx, t in enumerate(tiempos):
            if cancel_check and cancel_check(): return
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
                            else: logo_anim_img = Image.fromarray(fr).convert("RGBA")
                except: pass

            master = canvas_bg.copy()
            for zona in datos["zonas"].values():
                for e in zona["elementos"]:
                    if e["tipo"] == "center_board" and e["width"] > 0:
                        cb_path = self.app.archivos_cargados.get("center_board")
                        mostrar_cb = datos.get("config", {}).get("show_cb", True)
                        if cb_path and mostrar_cb:
                            pil_cb_raw = self.app.obtener_frame_pil(cb_path, h_target=h_total)
                            cb_letterbox = self.aplicar_letterbox_pil(pil_cb_raw, e["width"], h_total, RESAMPLE_METHOD)
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

            res = generar_imagen_arquitectura(master, datos, h_total, w_total, gl_offset_pct, bg_color=(235, 235, 235, 255))
            
            ref_filename = f"{base_filename}_{stadium_clean}_QC_Ref_{idx+1}.png"
            ref_path = os.path.join(out_dir, ref_filename)
            try:
                res.save(ref_path, "PNG")
            except Exception as e:
                log_error(f"Error saving QC image {ref_path}: {e}")