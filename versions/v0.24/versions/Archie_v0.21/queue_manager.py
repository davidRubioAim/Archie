import os
import json
import uuid
import threading
import traceback
import time
import customtkinter as ctk
from moviepy import VideoFileClip
from renderizador import renderizar_megastrip, renderizar_system_file

class QueueManager:
    def __init__(self, app):
        self.app = app
        self.render_queue = []
        self.is_rendering = False
        self.cancel_render_flag = False

    def generate_safe_filename(self, base, preset, rtype, ext, out_dir):
        preset_clean = preset.replace(" ", "_")
        counter = 1
        while True:
            test_name = f"{base}_{preset_clean}_{rtype}_{counter:02d}{ext}"
            test_path = os.path.join(out_dir, test_name)
            
            in_queue = False
            for j in self.render_queue:
                if "ui" in j and j["ui"]["entry_fn"].get() == test_name and j["out_dir"] == out_dir:
                    in_queue = True
                    break
                    
            if not os.path.exists(test_path) and not in_queue:
                return test_name
            counter += 1

    def add_to_queue(self, render_type, batch_data=None, batch_preset=None):
        data = batch_data if batch_data else self.app.calcular_layout()
        if not data: return
        
        duracion = 1.0
        fps = 25
        try:
            ruta_anim = self.app.archivos_cargados.get("anim_zone")
            if ruta_anim and ruta_anim.lower().endswith(('.mp4', '.mov', '.avi')):
                cl = VideoFileClip(ruta_anim)
                duracion = cl.duration
                fps = cl.fps
                cl.close()
        except Exception:
            pass 

        ext = ".mp4" if render_type == "SYSTEM" else ".mov"
        out_dir = self.app.output_path_var.get()
        base_name = self.app.output_filename_var.get().strip()
        preset = batch_preset if batch_preset else self.app.stadium_selector_global.get()
        
        if render_type == "SYSTEM" and not batch_data:
            data["system_setup"] = {
                "canvas_width": int(self.app.inputs["Canvas Width"].get() or 3840), 
                "canvas_height": int(self.app.inputs["Canvas Height"].get() or 2160), 
                "canvas_mask": int(self.app.inputs["Canvas Mask"].get() or 96), 
                "offset_x": int(self.app.inputs["Offset X"].get() or 0), 
                "offset_y": int(self.app.inputs["Offset Y"].get() or 0)
            }
        
        is_amplify = data.get("config", {}).get("amplify", False)
        
        if render_type == "SYSTEM" and is_amplify:
            rows = [("1ST_ROW", "SYS-1ST"), ("2ND_ROW", "SYS-2ND")]
            for row_target, label in rows:
                safe_filename = self.generate_safe_filename(base_name, f"{preset}_{row_target}", render_type, ext, out_dir)
                job_data = json.loads(json.dumps(data))
                job_data["row_target"] = row_target
                
                job = {
                    "id": str(uuid.uuid4()), "type": label, "data": job_data, "out_dir": out_dir,
                    "ext": ext, "status": "Pending", "info": f"{duracion}s | {row_target}"
                }
                self.build_queue_ui_item(job, safe_filename)
                self.render_queue.append(job)
        else:
            safe_filename = self.generate_safe_filename(base_name, preset, render_type, ext, out_dir)
            job = {
                "id": str(uuid.uuid4()), "type": render_type, "data": data, "out_dir": out_dir,
                "ext": ext, "status": "Pending", "info": f"{duracion}s | {fps} FPS"
            }
            self.build_queue_ui_item(job, safe_filename)
            self.render_queue.append(job)
            
        self.app.queue_panel.pack(fill="x", pady=(5, 10))

    def build_queue_ui_item(self, job, initial_filename):
        color = self.app.c_magenta if "SYS" in job["type"] or job["type"] == "SYSTEM" else "#3498DB"
        
        frame = ctk.CTkFrame(self.app.queue_scroll, fg_color=self.app.c_light_grey, corner_radius=5)
        frame.pack(fill="x", pady=2, padx=5)
        
        info_frame = ctk.CTkFrame(frame, fg_color="transparent")
        info_frame.pack(side="left", fill="x", expand=True, padx=5)
        
        ctk.CTkLabel(info_frame, text=f"[{job['type']}]", font=ctk.CTkFont(weight="bold"), text_color=color, width=80, anchor="w").pack(side="left", padx=(5, 5))
        
        entry_fn = ctk.CTkEntry(info_frame, width=280, fg_color=self.app.c_white, text_color=self.app.c_text_dark)
        entry_fn.insert(0, initial_filename)
        entry_fn.pack(side="left", padx=5)
        
        btn_dir = ctk.CTkButton(info_frame, text="📁", width=30, fg_color="#E0E0E0", hover_color="#D0D0D0", text_color=self.app.c_text_dark, command=lambda j=job: self.change_job_dir(j))
        btn_dir.pack(side="left", padx=5)
        
        lbl_dir = ctk.CTkLabel(info_frame, text=f".../{os.path.basename(job['out_dir'])}", text_color=self.app.c_text_muted, width=120, anchor="w")
        lbl_dir.pack(side="left", padx=5)
        
        lbl_status = ctk.CTkLabel(frame, text="Pending", width=100, text_color=self.app.c_text_dark)
        lbl_status.pack(side="left", padx=10)
        
        prog_bar = ctk.CTkProgressBar(frame, width=150, progress_color=color)
        prog_bar.set(0)
        prog_bar.pack(side="left", padx=10)
        
        btn_del = ctk.CTkButton(frame, text="X", width=30, fg_color="#C0392B", hover_color="#E74C3C", command=lambda j=job["id"]: self.remove_from_queue(j))
        btn_del.pack(side="right", padx=5)
        
        job["ui"] = {"frame": frame, "entry_fn": entry_fn, "btn_dir": btn_dir, "lbl_dir": lbl_dir, "status": lbl_status, "bar": prog_bar, "btn_del": btn_del}

    def change_job_dir(self, job):
        if folder := ctk.filedialog.askdirectory(title="Select Output Directory for this Job"):
            job["out_dir"] = folder
            job["ui"]["lbl_dir"].configure(text=f".../{os.path.basename(folder)}")

    def remove_from_queue(self, job_id):
        for job in self.render_queue:
            if job["id"] == job_id:
                if job["status"] == "Rendering": return 
                job["ui"]["frame"].destroy()
                self.render_queue.remove(job)
                break
        if not self.render_queue and not self.is_rendering:
            self.app.queue_panel.pack_forget()

    def clear_completed_jobs(self):
        for job in list(self.render_queue):
            if job["status"] in ["Done", "Error"]:
                self.remove_from_queue(job["id"])

    def start_queue_processor(self):
        if not self.is_rendering:
            self.is_rendering = True
            self.app.btn_start_queue.configure(state="disabled", text="Rendering...")
            self.toggle_batch_overlay(True)
            threading.Thread(target=self.process_queue_worker, daemon=True).start()

    def process_queue_worker(self):
        while True:
            job = next((j for j in self.render_queue if j["status"] == "Pending"), None)
            if not job: break 

            job["start_time"] = time.time()
            self.app.after(0, lambda j=job: self.update_job_ui(j, "Rendering...", 0))

            def update_progress(percent): 
                elapsed = time.time() - job["start_time"]            

                if percent > 0.01:
                    total_estimated = elapsed / percent
                    remaining = max(0, total_estimated - elapsed)
                    mins, secs = divmod(int(remaining), 60)
                    status_str = f"{int(percent*100)}% - {mins:02d}:{secs:02d}"
                else:
                    status_str = f"{int(percent*100)}% - Calculando..."
                
                # [CORRECCIÓN]: Esta llamada es vital para que la UI no parezca congelada
                self.app.after(0, lambda j=job, p=percent, s=status_str: self.update_job_ui(j, s, p))

            try:
                final_name = job["ui"]["entry_fn"].get().strip()
                
                if not final_name.lower().endswith(job["ext"]): final_name += job["ext"]
                out_path = os.path.join(job["out_dir"], final_name)
                json_path = out_path.replace(job["ext"], ".json")
                
                with open(json_path, 'w', encoding='utf-8') as f: json.dump(job["data"], f, indent=4, ensure_ascii=False)
                
                if job["type"] in ["MEGASTRIP", "AE_EXPORT", "ARCHIEtoAE"]: 
                    job["data"]["proyecto"] = "AE_EXPORT"
                    with open(json_path, 'w', encoding='utf-8') as f:
                        json.dump(job["data"], f, indent=4, ensure_ascii=False)
                else: 
                    renderizar_system_file(
                        json_path, out_path, progress_callback=update_progress, 
                        row_target=job["data"].get("row_target")
                    )
                
                self.app.after(0, lambda j=job: self.update_job_ui(j, "Completed!", 1.0, done=True))
            except Exception as e:
                traceback.print_exc()
                err_msg = type(e).__name__ + ": " + str(e).replace('\n', ' ')[:40]
                self.app.after(0, lambda j=job, e_msg=err_msg: self.update_job_ui(j, f"{e_msg}", 0, done=True))

        self.is_rendering = False
        
        # Desactivamos el bloqueo y restauramos el botón principal
        self.app.after(0, lambda: self.app.btn_start_queue.configure(state="normal", text="▶ Start Queue"))
        self.app.after(0, lambda: self.toggle_batch_overlay(False))
    
    def update_job_ui(self, job, status_text, percent, done=False):
        if "ui" in job:
            job["ui"]["status"].configure(text=status_text)
            job["ui"]["bar"].set(percent)
            job["status"] = "Rendering" if not done else "Done"
            
            if job["status"] == "Rendering":
                job["ui"]["btn_del"].configure(state="disabled")
                job["ui"]["entry_fn"].configure(state="disabled")
                job["ui"]["btn_dir"].configure(state="disabled")
            else:
                job["ui"]["btn_del"].configure(state="normal")
                
            if "Completed" in status_text: job["ui"]["status"].configure(text_color="#27AE60")
            elif "Error" in status_text or "Exception" in status_text: job["ui"]["status"].configure(text_color="#E74C3C")

    def export_ae_json(self):
        """
        Exporta de forma inmediata el archivo .json para After Effects.
        Abre la carpeta del explorador local al finalizar con éxito.
        """
        data = self.app.calcular_layout()
        if not data: 
            return
        
        # Sobrescribimos el nombre del proyecto purgando el término antiguo
        data["proyecto"] = "AE_EXPORT"
        
        out_dir = self.app.output_path_var.get()
        base_name = self.app.output_filename_var.get().strip()
        preset = self.app.stadium_selector_global.get()
        
        # Generamos el nombre con sufijo "ARCHIEtoAE" y extensión directa .json
        safe_filename = self.generate_safe_filename(base_name, preset, "ARCHIEtoAE", ".json", out_dir)
        json_path = os.path.join(out_dir, safe_filename)
        
        try:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            
            # Notificación visual
            self.app.lbl_preview_status.configure(
                text=f"✔ AE Export Ready: {safe_filename}", 
                text_color="#27AE60"
            )
            print(f"[ARCHIE DEBUG] JSON exportado para After Effects en: {json_path}")
            
            # Abrir automáticamente el directorio donde se ha guardado el .json
            if os.path.exists(out_dir):
                if os.name == 'nt':
                    os.startfile(out_dir)
                elif os.name == 'posix':
                    import subprocess
                    cmd = "open" if "darwin" in os.sys.platform else "xdg-open"
                    subprocess.Popen([cmd, out_dir])
                    
        except Exception as e:
            traceback.print_exc()
            self.app.lbl_preview_status.configure(
                text=f"❌ Error exporting AE file: {str(e)[:40]}", 
                text_color="#E74C3C"
            )

    def run_direct_render(self, render_type):
        data = self.app.calcular_layout()
        if not data: return
        
        self.cancel_render_flag = False
        self.app.overlay_frame.lift()
        self.app.overlay_frame.place(relx=0.5, rely=0.5, relwidth=1, relheight=1, anchor="center")
        self.app.overlay_bar.set(0)
        self.app.lbl_overlay_status.configure(text="Starting video engine...", text_color="gray")
        self.app.btn_overlay_cancel.configure(text="Cancel Render", state="normal", fg_color="#C0392B", hover_color="#E74C3C", command=self.cancel_direct_render)
        
        ext = ".mp4" if render_type == "SYSTEM" else ".mov"
        out_dir = self.app.output_path_var.get()
        base_name = self.app.output_filename_var.get().strip()
        preset = self.app.stadium_selector_global.get()
        
        safe_filename = self.generate_safe_filename(base_name, preset, render_type, ext, out_dir)
        out_path = os.path.join(out_dir, safe_filename)
        json_path = out_path.replace(ext, ".json")
        
        if render_type == "SYSTEM":
            data["system_setup"] = {
                "canvas_width": int(self.app.inputs["Canvas Width"].get() or 3840), 
                "canvas_height": int(self.app.inputs["Canvas Height"].get() or 2160), 
                "canvas_mask": int(self.app.inputs["Canvas Mask"].get() or 96), 
                "offset_x": int(self.app.inputs["Offset X"].get() or 0), 
                "offset_y": int(self.app.inputs["Offset Y"].get() or 0)
            }
        with open(json_path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=4, ensure_ascii=False)
        
        threading.Thread(target=self.direct_render_worker, args=(render_type, json_path, out_path), daemon=True).start()

    def cancel_direct_render(self):
        self.cancel_render_flag = True
        self.app.lbl_overlay_status.configure(text="Aborting render... Please wait.", text_color="#E67E22")
        self.app.btn_overlay_cancel.configure(state="disabled")

    def direct_render_worker(self, render_type, json_path, out_path):
        start_time = time.time()
        
        def update_progress(percent): 
            elapsed = time.time() - start_time
            if percent > 0.01:
                total_estimated = elapsed / percent
                remaining = max(0, total_estimated - elapsed)
                mins, secs = divmod(int(remaining), 60)
                status_str = f"Rendering: {int(percent*100)}% - {mins:02d}m {secs:02d}s left"
            else:
                status_str = f"Rendering: {int(percent*100)}% - Estimating time..."
                
            self.app.after(0, lambda p=percent, s=status_str: self.update_overlay_ui(s, p))
            
        try:
            if render_type == "MEGASTRIP": 
                renderizar_megastrip(json_path, out_path, progress_callback=update_progress, cancel_check=lambda: self.cancel_render_flag)
            else: 
                renderizar_system_file(json_path, out_path, progress_callback=update_progress, cancel_check=lambda: self.cancel_render_flag, row_target="1ST_ROW")
            self.app.after(0, self.finish_direct_render_success)
        except Exception as e:
            if "RENDER_CANCELLED" in str(e): self.app.after(0, self.finish_direct_render_cancelled)
            else:
                traceback.print_exc()
                self.app.after(0, lambda err=str(e): self.finish_direct_render_error(err))

    def update_overlay_ui(self, text, percent):
        self.app.lbl_overlay_status.configure(text=text)
        self.app.overlay_bar.set(percent)

    def finish_direct_render_success(self):
        self.app.lbl_overlay_status.configure(text="Render Completed Successfully!", text_color="#27AE60")
        self.app.overlay_bar.set(1.0)
        self.app.btn_overlay_cancel.configure(text="Close", fg_color="#27AE60", hover_color="#2ECC71", state="normal", command=self.close_overlay)
        
    def finish_direct_render_cancelled(self):
        self.app.lbl_overlay_status.configure(text="Render Cancelled by User.", text_color="#E74C3C")
        self.app.btn_overlay_cancel.configure(text="Close", fg_color="gray", hover_color="#555", state="normal", command=self.close_overlay)
        
    def finish_direct_render_error(self, err):
        self.app.lbl_overlay_status.configure(text=f"Error: {err[:40]}", text_color="#E74C3C")
        self.app.btn_overlay_cancel.configure(text="Close", fg_color="gray", hover_color="#555", state="normal", command=self.close_overlay)
        
    def close_overlay(self):
        self.app.overlay_frame.place_forget()

    def toggle_batch_overlay(self, show):
        """Muestra u oculta un panel oscuro sobre la interfaz para evitar ediciones durante el render en lote."""
        if show:
            if not hasattr(self, "batch_block_frame"):
                # Creamos el panel oscuro anclado al main_frame
                self.batch_block_frame = ctk.CTkFrame(self.app.main_frame, fg_color="#141414", corner_radius=10)
                self.batch_block_label = ctk.CTkLabel(
                    self.batch_block_frame, 
                    text="⚙️ BATCH RENDER IN PROGRESS ⚙️\n\nControls are temporarily disabled.", 
                    font=ctk.CTkFont(size=24, weight="bold"), 
                    text_color="#FF099A"
                )
                self.batch_block_label.place(relx=0.5, rely=0.5, anchor="center")
            
            # Lo posicionamos ocupando las filas 0, 1 y 2 (Dejando la fila 3, donde está la cola, visible)
            self.batch_block_frame.grid(row=0, column=0, rowspan=3, columnspan=3, sticky="nsew", padx=5, pady=5)
            self.batch_block_frame.lift()
        else:
            if hasattr(self, "batch_block_frame"):
                self.batch_block_frame.grid_forget()