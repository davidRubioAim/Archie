"""
v0.71: MODO MANUAL en la Architecture Preview (interfaz).

Mixin de MegaStripApp: botones (Manual / Magnet / Reset / ?), arrastre con espejo, menú de clic derecho
(Duplicate / Remove), atajos de teclado, bloqueo de sliders y vuelta al modo automático.
La lógica de la arquitectura (espejo, empuje, fronteras, LOGO UNDER, Magnet) vive en manual_layout.py.
"""
import tkinter as tk
from tkinter import messagebox
import traceback

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from log_utils import debug, log_error
from manual_layout import ManualLayout
from motor_distribucion import Distribuidor, segmentos_arquitectura
from video_player import PhotoTarget

C_SEL = (255, 9, 154, 255)        # magenta de la app
C_ESPEJO = (255, 150, 210, 255)
C_EDIT = (255, 255, 255, 110)
C_AVISO = "#E67E22"

AYUDA_MANUAL = (
    "MANUAL MODE · quick guide\n\n"
    "✋ Manual turns editing on and off in the Architecture Preview.\n\n"
    "• Drag an ANIM ZONE or a logo to move it. Everything is symmetric: its mirror moves too "
    "(Goal Line Left ↔ Goal Line Right, left ↔ right half of the Touchline).\n"
    "• Elements never overlap. Pushing an element moves its neighbours; when there is no room left, "
    "the ANIM ZONES squash automatically up to the Squash Limit.\n"
    "• Elements stay inside their section (Goal Line up to the corner, Touchline between corners) and never "
    "cover the Center Board.\n"
    "• Right click an element: Duplicate / Remove. Duplicate moves the element half a step to one side and "
    "drops the copy on the other side.\n"
    "• LOGO UNDER: logos can't be moved. Duplicate / Remove adds or removes one logo in that section and the "
    "equidistant grid is rebuilt. ANIM ZONES snap to the logo grid.\n"
    "• ⇆ Magnet evens out the spacing in every section and copies the last edited side to the other one.\n"
    "• ↺ Reset discards the manual layout and goes back to automatic: sliders and chat drive the layout again.\n\n"
    "While the layout is manual the distribution sliders are locked. Changing the stadium, its size, the logo "
    "or the ANIM ZONES goes back to automatic. Writing to the assistant asks first.\n\n"
    "Keys: ← / → nudge 1 px (Shift: 10 px; one grid cell in LOGO UNDER) · Del: Remove · Ctrl+D: Duplicate · "
    "Esc: deselect. Undo / Redo buttons work too.\n"
    "Pan: middle mouse drag, Space + drag, or drag on an empty area.\n\n"
    "The queue, direct render and AE JSON use the manual layout as it is when you add the job. "
    "The 3D Preview shows the changes but can't be edited. Not available in AMPLIFY mode yet."
)

PISTA_MANUAL = ("✋ MANUAL MODE · Drag: move · Right click: Duplicate / Remove · ←/→ · Del · Ctrl+D · "
                "Pan: middle drag")


class ManualModeMixin:

    # ================================================================== estado e interfaz
    def _mv_init_state(self):
        self.manual = None            # ManualLayout cuando la arquitectura es manual (sliders bloqueados)
        self.manual_sig = None        # firma (estadio, tamaño, ELEMENTS) con la que se creó
        self.manual_tool = False      # edición activada (botón ✋ Manual)
        self.manual_sel = None        # uid seleccionado
        self.manual_drag = None
        self._manual_tmp = None       # (datos, modelo) mientras no hay cambios manuales
        self._manual_lock = {}
        self._manual_lock_lu = None
        self._manual_target = PhotoTarget()
        self._manual_msg_timer = None
        self._manual_render_timer = None
        self._disp_datos = None       # datos de la arquitectura que se ve ahora en la preview
        self._render_datos = None     # (gen, datos) del render en curso
        self._space_down = False
        self._mv_pan = None
        self.lbl_manual_lock = None

    def _mv_build_ui(self):
        """Botones en la cabecera de la Architecture Preview y barra de ayuda (fila 3 del contenedor)."""
        bc = dict(height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray")
        ctk.CTkButton(self.preview_header, text="?", width=24, command=self._mv_ayuda, **bc).pack(side="right", padx=(5, 10))
        self.btn_manual_reset = ctk.CTkButton(self.preview_header, text="↺ Reset", width=62, command=self._mv_reset, **bc)
        self.btn_manual_reset.pack(side="right", padx=5)
        self.btn_manual_magnet = ctk.CTkButton(self.preview_header, text="⇆ Magnet", width=72, command=self._mv_magnet, **bc)
        self.btn_manual_magnet.pack(side="right", padx=5)
        self.btn_manual = ctk.CTkButton(self.preview_header, text="✋ Manual", width=72, command=self._mv_toggle_tool, **bc)
        self.btn_manual.pack(side="right", padx=5)

        self.manual_bar = ctk.CTkFrame(self.preview_container, fg_color="transparent")
        self.manual_bar.grid_columnconfigure(0, weight=1)
        self.lbl_manual_hint = ctk.CTkLabel(self.manual_bar, text=PISTA_MANUAL, font=ctk.CTkFont(size=11),
                                            text_color="gray", anchor="w")
        self.lbl_manual_hint.grid(row=0, column=0, sticky="ew")
        self.lbl_manual_msg = ctk.CTkLabel(self.manual_bar, text="", font=ctk.CTkFont(size=11, weight="bold"),
                                           text_color=C_AVISO, anchor="e")
        self.lbl_manual_msg.grid(row=0, column=1, sticky="e", padx=(10, 0))

        lbl = self.lbl_live_preview
        lbl.bind("<Button-3>", self._mv_menu)
        lbl.bind("<Motion>", self._mv_hover)
        lbl.bind("<ButtonPress-2>", self._mv_pan_start)
        lbl.bind("<B2-Motion>", self._mv_pan_motion)
        lbl.bind("<ButtonRelease-2>", self._mv_pan_end)
        for seq, fn in (("<KeyPress-Left>", lambda e: self._mv_nudge(e, -1, 1)),
                        ("<KeyPress-Right>", lambda e: self._mv_nudge(e, 1, 1)),
                        ("<Shift-Left>", lambda e: self._mv_nudge(e, -1, 10)),
                        ("<Shift-Right>", lambda e: self._mv_nudge(e, 1, 10)),
                        ("<Delete>", self._mv_key_remove),
                        ("<Control-d>", self._mv_key_duplicate), ("<Control-D>", self._mv_key_duplicate),
                        ("<Escape>", self._mv_key_escape),
                        ("<KeyPress-space>", self._mv_space_down), ("<KeyRelease-space>", self._mv_space_up)):
            self.bind_all(seq, fn, add="+")
        self._mv_refresh_ui()

    def _mv_refresh_ui(self):
        if not hasattr(self, "btn_manual"):
            return
        if self.manual_tool:
            self.btn_manual.configure(fg_color=self.c_magenta, hover_color="#d1077e", border_color=self.c_magenta)
            self.manual_bar.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
        else:
            self.btn_manual.configure(fg_color="#2b2b2b", hover_color="#3a3a3a", border_color="gray")
            self.manual_bar.grid_remove()
        if self.manual is not None:
            self.btn_manual_reset.configure(fg_color=C_AVISO, hover_color="#D35400", border_color=C_AVISO)
        else:
            self.btn_manual_reset.configure(fg_color="#2b2b2b", hover_color="#3a3a3a", border_color="gray")
        self._mv_cursor("")

    def _mv_msg(self, text, color=C_AVISO, segundos=6):
        if not text:
            return
        debug(f"Manual: {text}")
        if self._manual_msg_timer:
            try: self.after_cancel(self._manual_msg_timer)
            except Exception: pass
            self._manual_msg_timer = None
        if self.manual_tool and hasattr(self, "lbl_manual_msg"):
            self.lbl_manual_msg.configure(text=text, text_color=color)
            self._manual_msg_timer = self.after(segundos * 1000, lambda: self.lbl_manual_msg.configure(text=""))
        elif hasattr(self, "lbl_preview_status"):
            self.lbl_preview_status.configure(text=f"ARCHITECTURE PREVIEW · {text}", text_color=color)

    def _mv_ayuda(self):
        win = ctk.CTkToplevel(self)
        win.title("Manual mode")
        win.geometry("560x560")
        win.transient(self)
        box = ctk.CTkTextbox(win, wrap="word", font=ctk.CTkFont(size=12))
        box.pack(fill="both", expand=True, padx=12, pady=(12, 6))
        box.insert("1.0", AYUDA_MANUAL)
        box.configure(state="disabled")
        ctk.CTkButton(win, text="OK", width=80, fg_color=self.c_magenta, hover_color="#d1077e",
                      command=win.destroy).pack(pady=(0, 12))
        win.after(50, win.lift)

    # ================================================================== activar / desactivar
    def _mv_toggle_tool(self):
        if self.manual_tool:
            self.manual_tool = False
            self.manual_sel = None
            self.manual_drag = None
        else:
            ok, msg = ManualLayout.soportado(self._disp_datos)
            if not ok:
                self._mv_msg(msg)
                return
            self.manual_tool = True
            if self.player.playing:
                self.player.pause()
            if not getattr(self, "_manual_ayuda_vista", False):
                self._manual_ayuda_vista = True
                self.after(100, lambda: self._mv_msg("Press ? for the MANUAL MODE guide.", "gray", 8))
        self._mv_refresh_ui()
        self._mv_repintar()

    def _mv_firma(self):
        a = self.archivos_cargados or {}
        try:
            dims = tuple(self.inputs[k].get() for k in ("Width", "Height", "Goal Line Size", "Center Board Width"))
        except Exception:
            dims = ()
        return (self.stadium_selector_global.get(), dims, a.get("static_logo"),
                tuple(a.get("anim_zones") or [a.get("anim_zone")]),
                bool(self.logo_under_var.get()), bool(self.amplify_var.get()))

    def _mv_check_firma(self):
        """Si ha cambiado el estadio, su tamaño o los ELEMENTS: vuelta al modo automático."""
        if self.manual is not None and self._mv_firma() != self.manual_sig:
            self._mv_descartar("Manual layout discarded: stadium, size or ELEMENTS changed. Back to automatic.")

    def _mv_aplicar(self, res):
        """calcular_layout: con arquitectura manual, sus elementos sustituyen a los del motor."""
        self._mv_check_firma()
        if self.manual is None or not res:
            return res
        try:
            return self.manual.a_datos(res)
        except Exception as e:
            traceback.print_exc()
            log_error(f"Manual layout no aplicable: {e}")
            return res

    def _mv_descartar(self, msg=None):
        if self.manual is None:
            self._manual_tmp = None
            return
        self.manual = None
        self.manual_sig = None
        self.manual_sel = None
        self._manual_tmp = None
        self._mv_bloquear(False)
        self._mv_refresh_ui()
        if msg:
            self._mv_msg(msg)
            try: self.append_chat("Archie", f"✋ {msg}")
            except Exception: pass

    def _mv_reset(self):
        if self.manual is None:
            self._mv_msg("Already automatic: sliders and chat drive the layout.", "gray")
            return
        if not messagebox.askyesno("Reset to automatic",
                                   "Discard all MANUAL MODE changes and go back to the automatic layout "
                                   "(sliders + chat)?", parent=self):
            return
        self._mv_descartar()
        self._mv_msg("Back to automatic.", "gray")
        self.save_history_state()
        self.start_preview_render()

    # ================================================================== bloqueo de sliders
    def _mv_bloqueables(self):
        ws = [self.margin_slider, self.margin_tl_slider, self.offset_slider, self.stretch_slider,
              self.qty_goal_slider, self.qty_corner_slider, self.qty_cb_slider, self.qty_tl_slider,
              self.max_anim_gl_slider, self.max_anim_tl_slider, self.qty_gl_under_slider, self.qty_tl_under_slider]
        for child in self.col_left.winfo_children():
            if isinstance(child, ctk.CTkCheckBox) and child not in (self.chk_logo_under, self.chk_show_cb):
                ws.append(child)
        return ws

    def _mv_bloquear(self, on):
        if on:
            if self._manual_lock:
                for w in self._manual_lock:   # p. ej. tras un Undo que ha tocado los estados de los sliders
                    try: w.configure(state="disabled")
                    except Exception: pass
                return
            self._manual_lock_lu = bool(self.logo_under_var.get())
            for w in self._mv_bloqueables():
                try:
                    self._manual_lock[w] = w.cget("state")
                    w.configure(state="disabled")
                except Exception:
                    pass
            if self.lbl_manual_lock is None:
                self.lbl_manual_lock = ctk.CTkLabel(
                    self.col_left, text="✋ MANUAL MODE · distribution sliders locked.\n"
                                        "Press ↺ Reset in the Architecture Preview to go back to automatic.",
                    font=ctk.CTkFont(size=11, weight="bold"), text_color=C_AVISO, justify="left", anchor="w")
            hijos = [c for c in self.col_left.winfo_children() if c is not self.lbl_manual_lock]
            try:
                self.lbl_manual_lock.pack(anchor="w", padx=20, pady=(8, 0), before=hijos[0])
            except Exception:
                self.lbl_manual_lock.pack(anchor="w", padx=20, pady=(8, 0))
        else:
            for w, st in self._manual_lock.items():
                try: w.configure(state=st)
                except Exception: pass
            self._manual_lock = {}
            if self.lbl_manual_lock is not None:
                self.lbl_manual_lock.pack_forget()
            if self._manual_lock_lu is not None and self._manual_lock_lu != bool(self.logo_under_var.get()):
                self._manual_lock_lu = None
                self.toggle_logo_under_mode()   # los estados de los sliders dependen de LOGO UNDER
            self._manual_lock_lu = None

    # ================================================================== historial (undo / redo)
    def _mv_estado(self):
        if getattr(self, "manual", None) is None:
            return None
        return {"model": self.manual.estado(), "sig": self.manual_sig}

    def _mv_restaurar(self, st):
        if not st:
            if self.manual is not None:
                self._mv_descartar()
            return
        self.manual = ManualLayout.desde_estado(st["model"])
        self.manual_sig = st["sig"]
        self._manual_tmp = None
        if self.manual_sel is not None and self.manual.el(self.manual_sel) is None:
            self.manual_sel = None
        self._mv_bloquear(True)
        self._mv_refresh_ui()

    # ================================================================== modelo
    def _mv_anim_nat(self, d):
        try:
            dims = d["dimensiones_globales"]
            z = d["zonas"]
            dist = Distribuidor(dims["width"], dims["height"], z["ZONA_1"]["width"], z.get("ZONA_3", {}).get("width", 0),
                                int(self.margin_slider.get()), self.archivos_cargados, d.get("config") or {})
            return dist.anim_w
        except Exception as e:
            log_error(f"Manual: ancho natural de ANIM ZONE no disponible: {e}")
            ws = [e["width"] for z in d["zonas"].values() for e in z["elementos"] if e["tipo"] == "anim_zone"]
            return max(ws) if ws else 1

    def _mv_modelo(self, avisar=True):
        if self.manual is not None:
            return self.manual
        d = self._disp_datos
        ok, msg = ManualLayout.soportado(d)
        if not ok:
            if avisar: self._mv_msg(msg)
            return None
        if self._manual_tmp is not None and self._manual_tmp[0] is d:
            return self._manual_tmp[1]
        try:
            m = ManualLayout(d, self._mv_anim_nat(d), self.margin_slider.get(), self.margin_tl_slider.get(),
                             self.stretch_slider.get())
        except Exception as e:
            traceback.print_exc()
            log_error(f"Manual: no se pudo crear el modelo: {e}")
            if avisar: self._mv_msg("Manual mode is not available for this layout.")
            return None
        self._manual_tmp = (d, m)
        return m

    def _mv_commit(self, model, delay=0):
        if self.manual is None:
            self.manual_sig = self._mv_firma()
            self._mv_bloquear(True)
        self.manual = model
        self._manual_tmp = None
        self._mv_refresh_ui()
        self.save_history_state()
        self.preview_dirty = True
        if self._manual_render_timer:
            try: self.after_cancel(self._manual_render_timer)
            except Exception: pass
            self._manual_render_timer = None
        self._cancel_auto_update()
        if delay:
            self._manual_render_timer = self.after(delay, self._mv_render)
        else:
            self._mv_render()

    def _mv_render(self):
        self._manual_render_timer = None
        if self.manual_drag is not None:   # no re-renderizar en mitad de un arrastre
            self._manual_render_timer = self.after(200, self._mv_render)
            return
        self.start_preview_render()

    def _mv_operar(self, fn, delay=0):
        """Aplica fn(modelo) sobre una copia; si sale bien, la pinta al momento y la confirma."""
        base = self._mv_modelo()
        if base is None:
            return False
        nuevo = base.clone()
        try:
            res = fn(nuevo)
        except Exception as e:
            traceback.print_exc()
            log_error(f"Manual: operación fallida: {e}")
            self._mv_msg("That change could not be applied.")
            return False
        ok, msg = res[0], res[1]
        if not ok:
            self._mv_msg(msg or "That change is not possible here.")
            return False
        if msg:
            self._mv_msg(msg, "gray")
        if len(res) > 2 and res[2] is not None:
            self.manual_sel = res[2]
        if self.manual_sel is not None and nuevo.el(self.manual_sel) is None:
            self.manual_sel = None
        if not self.render_busy and self._shown_strip is not None:
            try:
                img, limpia = self._mv_overlay(base, nuevo, self._shown_strip, self._mv_vista())
                self._shown_strip, self._shown_strip_player = limpia, False
                self._mv_mostrar(img)
            except Exception:
                traceback.print_exc()
        self._mv_commit(nuevo, delay)
        return True

    # ================================================================== geometría de la vista
    def _mv_vista(self):
        cw = self.preview_image_container.winfo_width()
        ch = self.preview_image_container.winfo_height()
        if cw <= 10 or ch <= 10:
            return None
        if self.player.engaged and self.player.ready and self.player.escena is not None:
            rw, rh = self.player.escena.res_size
        elif self.pil_preview_images and 0 <= self.preview_current_idx < len(self.pil_preview_images) \
                and self.pil_preview_images[self.preview_current_idx] is not None:
            rw, rh = self.pil_preview_images[self.preview_current_idx].size
        else:
            return None
        zoom = self.zoom_factor_strip
        sw, sh = max(1, int(rw * zoom)), max(1, int(rh * zoom))
        max_px, max_py = max(0, (sw - cw) / 2), max(0, (sh - ch) / 2)
        pan_x = max(-max_px, min(max_px, self.pan_x_strip)) if sw > cw else 0
        pan_y = max(-max_py, min(max_py, self.pan_y_strip)) if sh > ch else 0
        return {"cw": cw, "ch": ch, "px": int((cw - sw) / 2 + pan_x), "py": int((ch - sh) / 2 + pan_y),
                "sx": sw / float(rw), "sy": sh / float(rh)}

    def _mv_segs(self):
        d = self._disp_datos
        if not d:
            return None, 0
        dims = d["dimensiones_globales"]
        try:
            return segmentos_arquitectura(d, dims["height"], dims["width"])[1], dims["height"]
        except Exception:
            return None, 0

    def _mv_rects(self, x, w, v, segs, h):
        """Rectángulos en pantalla (puede haber 2 si cruza filas) de un tramo [x, x+w) del strip."""
        out = []
        for x1m, x2m, xr, yr in segs:
            a, b = max(x, x1m), min(x + w, x2m)
            if b <= a:
                continue
            out.append((v["px"] + (xr + a - x1m) * v["sx"], v["py"] + yr * v["sy"],
                        v["px"] + (xr + b - x1m) * v["sx"], v["py"] + (yr + h) * v["sy"]))
        return out

    def _mv_evento_x(self, event):
        v = self._mv_vista()
        segs, h = self._mv_segs()
        if v is None or not segs:
            return None
        ex = event.x_root - self.preview_image_container.winfo_rootx()
        ey = event.y_root - self.preview_image_container.winfo_rooty()
        rx = (ex - v["px"]) / v["sx"]
        ry = (ey - v["py"]) / v["sy"]
        tol = 6.0 / max(1e-6, v["sy"])
        for x1m, x2m, xr, yr in segs:
            if yr - tol <= ry < yr + h + tol and xr <= rx < xr + (x2m - x1m):
                return x1m + (rx - xr)
        return None

    # ================================================================== pintar
    def _mv_mostrar(self, img):
        try:
            self._manual_target.show(self.lbl_live_preview._label, img)
        except Exception:
            self._pintar_label(self.lbl_live_preview, img)

    def _mv_repintar(self):
        """Vuelve a pintar la preview (con o sin los contornos del modo manual)."""
        if self.manual_drag is not None:
            return
        if self._shown_strip is None:
            self.actualizar_imagen_preview()
            return
        if self._shown_strip_player and self.player.engaged and self.player.ready:
            self.player_show(self._shown_strip)
        else:
            self._pintar_label(self.lbl_live_preview, self._mv_decorar(self._shown_strip))

    def _mv_decorar(self, img):
        """Contornos de los elementos editables y del seleccionado (y su espejo) encima de la preview."""
        if not self.manual_tool or img is None or self.loading_overlay_on:
            return img
        m = self._mv_modelo(avisar=False)
        v = self._mv_vista()
        segs, h = self._mv_segs()
        if m is None or v is None or not segs:
            return img
        try:
            out = img.convert("RGBA").copy()
            capa = Image.new("RGBA", out.size, (0, 0, 0, 0))
            dr = ImageDraw.Draw(capa)
            sel = self.manual_sel if (self.manual_sel is not None and m.el(self.manual_sel)) else None
            espejo = m.pareja(sel) if sel is not None else None
            for uid, tipo, x, w, src, editable in m.elementos():
                if uid in (sel, espejo):
                    continue
                if editable:
                    for r in self._mv_rects(x, w, v, segs, h):
                        dr.rectangle(r, outline=C_EDIT, width=1)
            for uid, color, ancho in ((espejo, C_ESPEJO, 1), (sel, C_SEL, 2)):
                e = m.el(uid) if uid is not None else None
                if e is None:
                    continue
                for r in self._mv_rects(e["x"], e["w"], v, segs, h):
                    dr.rectangle((r[0] - 1, r[1] - 1, r[2] + 1, r[3] + 1), outline=color, width=ancho)
            return Image.alpha_composite(out, capa)
        except Exception:
            traceback.print_exc()
            return img

    def _mv_fuente(self):
        f = getattr(self, "_manual_font", None)
        if f is None:
            try:
                f = ImageFont.load_default(size=13)
            except Exception:
                f = ImageFont.load_default()
            self._manual_font = f
        return f

    def _mv_overlay(self, antes, despues, base, v, sel=None):
        """Vista rápida de un cambio sin re-renderizar: los elementos que cambian se recortan de la preview actual
        y se pegan en su sitio nuevo (lo que había debajo queda oscurecido). -> (con contornos, limpia)"""
        segs, h = self._mv_segs()
        if base is None or v is None or not segs:
            return base, base
        img = base.convert("RGBA").copy()
        viejo = {e["uid"]: (e["x"], e["w"]) for e in antes.els}
        nuevo = {e["uid"]: (e["x"], e["w"], e["src"]) for e in despues.els}
        cambia_v = [u for u in viejo if u not in nuevo or nuevo[u][:2] != viejo[u]]
        cambia_n = [u for u in nuevo if u not in viejo or nuevo[u][:2] != viejo[u]]
        sombra = (0, 0, 0, 165)
        for u in cambia_v:
            for r in self._mv_rects(viejo[u][0], viejo[u][1], v, segs, h):
                box = tuple(int(round(c)) for c in r)
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                trozo = img.crop(box)
                img.paste(Image.alpha_composite(trozo, Image.new("RGBA", trozo.size, sombra)), box[:2])
        for u in cambia_n:
            x, w, src = nuevo[u]
            fuente = u if u in viejo else (src if src in viejo else None)
            if fuente is None:
                # copia nueva (Duplicate): se recorta del original
                plantilla = next((e for e in despues.els if e["uid"] == u), None)
                fuente = next((e["uid"] for e in antes.els if plantilla and e["tipo"] == plantilla["tipo"]
                               and e["uid"] in viejo and e["w"] == plantilla["w"]), None)
            if fuente is None:
                continue
            r_src = self._mv_rects(viejo[fuente][0], viejo[fuente][1], v, segs, h)
            r_dst = self._mv_rects(x, w, v, segs, h)
            if not r_src or not r_dst:
                continue
            s = tuple(int(round(c)) for c in r_src[0])
            for r in r_dst:
                d = tuple(int(round(c)) for c in r)
                dw, dh = d[2] - d[0], d[3] - d[1]
                if dw <= 0 or dh <= 0 or s[2] <= s[0] or s[3] <= s[1]:
                    continue
                trozo = base.convert("RGBA").crop(s)
                if trozo.size != (dw, dh):
                    trozo = trozo.resize((dw, dh), Image.BILINEAR)
                img.paste(trozo, d[:2])
        limpia = img.copy()
        # contornos y cotas
        dr = ImageDraw.Draw(img)
        sel = sel if sel is not None else self.manual_sel
        if sel is not None and despues.el(sel) is not None:
            e = despues.el(sel)
            esp = despues.pareja(sel)
            if esp is not None:
                eo = despues.el(esp)
                for r in self._mv_rects(eo["x"], eo["w"], v, segs, h):
                    dr.rectangle((r[0] - 1, r[1] - 1, r[2] + 1, r[3] + 1), outline=C_ESPEJO, width=1)
            rs = self._mv_rects(e["x"], e["w"], v, segs, h)
            for r in rs:
                dr.rectangle((r[0] - 1, r[1] - 1, r[2] + 1, r[3] + 1), outline=C_SEL, width=2)
            if rs:
                hl, hr = despues.huecos(sel)
                txt = f"< {int(hl)} px" if hl is not None else ""
                txt += ("  |  " if txt else "") + (f"{int(hr)} px >" if hr is not None else "")
                if txt:
                    f = self._mv_fuente()
                    r = rs[0]
                    tx, ty = (r[0] + r[2]) / 2.0, r[1] - 6
                    try:
                        bb = dr.textbbox((tx, ty), txt, font=f, anchor="mb")
                        dr.rectangle((bb[0] - 4, bb[1] - 2, bb[2] + 4, bb[3] + 2), fill=(0, 0, 0, 200))
                        dr.text((tx, ty), txt, fill=(255, 255, 255, 255), font=f, anchor="mb")
                    except Exception:
                        dr.text((r[0], max(0, r[1] - 16)), txt, fill=(255, 255, 255, 255), font=f)
        return img, limpia

    def _mv_cursor(self, cur):
        try:
            self.lbl_live_preview._label.configure(cursor=cur)
        except Exception:
            pass

    # ================================================================== ratón
    def _mv_press(self, event):
        """on_drag_start de la Architecture Preview. True si lo gestiona el modo manual."""
        if not self.manual_tool:
            return False
        try: self.preview_image_container.focus_set()
        except Exception: pass
        if self._space_down:
            self.manual_drag = {"pan": True, "x": event.x_root, "y": event.y_root}
            return True
        m = self._mv_modelo()
        x = self._mv_evento_x(event)
        uid = m.hit(x) if (m is not None and x is not None) else None
        if uid is None:
            self.manual_sel = None
            self.manual_drag = {"pan": True, "x": event.x_root, "y": event.y_root}
            self._mv_repintar()
            return True
        self.manual_sel = uid
        ok, msg = m.puede_mover(uid)
        if not ok:
            self.manual_drag = None
            self._mv_msg(msg)
            self._mv_repintar()
            return True
        if self.render_busy or self._shown_strip is None:
            self.manual_drag = None
            self._mv_msg("Wait a moment: the preview is still updating.", "gray", 3)
            self._mv_repintar()
            return True
        if self.player.playing:
            self.player.pause()
        self.manual_drag = {"uid": uid, "x": event.x_root, "snap": m.clone(), "base": self._shown_strip,
                            "view": self._mv_vista(), "moved": False, "model": None}
        self._mv_repintar_sel(m)
        return True

    def _mv_repintar_sel(self, m):
        d = self.manual_drag
        self.manual_drag = None
        self._mv_repintar()
        self.manual_drag = d

    def _mv_motion(self, event):
        if not self.manual_tool:
            return False
        d = self.manual_drag
        if d is None:
            return True
        if d.get("pan"):
            self.pan_x_strip += event.x_root - d["x"]
            self.pan_y_strip += event.y_root - d["y"]
            d["x"], d["y"] = event.x_root, event.y_root
            self.manual_drag = None
            self.actualizar_imagen_preview()
            self.manual_drag = d
            return True
        dxp = event.x_root - d["x"]
        if not d["moved"] and abs(dxp) < 3:
            return True
        d["moved"] = True
        v = d["view"]
        if v is None:
            return True
        m = d["snap"].clone()
        ok, _ = m.mover(d["uid"], dxp / v["sx"])
        if not ok:
            return True
        d["model"] = m
        img, d["limpia"] = self._mv_overlay(d["snap"], m, d["base"], v, sel=d["uid"])
        self._mv_mostrar(img)
        return True

    def _mv_release(self, event):
        if not self.manual_tool:
            return False
        d = self.manual_drag
        self.manual_drag = None
        if d is None or d.get("pan"):
            self._mv_repintar()
            return True
        if d["moved"] and d.get("model") is not None:
            if d.get("limpia") is not None:
                self._shown_strip, self._shown_strip_player = d["limpia"], False
            self._mv_commit(d["model"])
        else:
            self._mv_repintar()
        return True

    def _mv_hover(self, event):
        if not self.manual_tool or self.manual_drag is not None:
            return
        m = self._mv_modelo(avisar=False)
        x = self._mv_evento_x(event) if m is not None else None
        uid = m.hit(x) if x is not None else None
        if self._space_down:
            self._mv_cursor("fleur")
        elif uid is not None and m.puede_mover(uid)[0]:
            self._mv_cursor("sb_h_double_arrow")
        elif uid is not None:
            self._mv_cursor("hand2")
        else:
            self._mv_cursor("")

    def _mv_pan_start(self, event):
        self._mv_pan = (event.x_root, event.y_root)

    def _mv_pan_motion(self, event):
        if self._mv_pan is None:
            return
        self.pan_x_strip += event.x_root - self._mv_pan[0]
        self.pan_y_strip += event.y_root - self._mv_pan[1]
        self._mv_pan = (event.x_root, event.y_root)
        self.actualizar_imagen_preview()

    def _mv_pan_end(self, event):
        self._mv_pan = None

    # ================================================================== menú contextual
    def _mv_menu(self, event):
        if not self.manual_tool:
            self._mv_msg("Turn on ✋ Manual to edit elements.", "gray", 3)
            return
        m = self._mv_modelo()
        x = self._mv_evento_x(event) if m is not None else None
        uid = m.hit(x) if x is not None else None
        if uid is None:
            return
        self.manual_sel = uid
        self._mv_repintar()
        e = m.el(uid)
        nombre = {"anim_zone": "ANIM ZONE", "static_logo": "Logo", "center_board": "Center Board"}.get(e["tipo"], "")
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=nombre, state="disabled")
        menu.add_separator()
        menu.add_command(label="Duplicate    Ctrl+D", command=lambda: self._mv_duplicar(uid),
                         state="normal" if m.puede_duplicar(uid) else "disabled")
        menu.add_command(label="Remove        Del", command=lambda: self._mv_eliminar(uid),
                         state="normal" if m.puede_eliminar(uid) else "disabled")
        menu.add_separator()
        menu.add_command(label="⇆ Magnet", command=self._mv_magnet)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _mv_duplicar(self, uid):
        self._mv_operar(lambda m: m.duplicar(uid))

    def _mv_eliminar(self, uid):
        if self._mv_operar(lambda m: m.eliminar(uid)):
            if self.manual is not None and self.manual.el(uid) is None:
                self.manual_sel = None

    def _mv_magnet(self):
        if self._mv_operar(lambda m: m.magnet()):
            self._mv_msg("Magnet: spacing evened out and mirrored.", "gray", 4)

    # ================================================================== teclado
    def _mv_tecla_ok(self):
        if not self.manual_tool:
            return False
        w = self.focus_get()
        if isinstance(w, (tk.Entry, tk.Text)):
            return False
        return True

    def _mv_nudge(self, event, signo, mult):
        if not self._mv_tecla_ok() or self.manual_sel is None:
            return None
        m = self._mv_modelo()
        if m is None or m.el(self.manual_sel) is None:
            return None
        paso = m.paso(self.manual_sel)
        dx = signo * (paso if paso > 1 else float(mult))
        uid = self.manual_sel
        self._mv_operar(lambda mm: mm.mover(uid, dx), delay=450)
        return "break"

    def _mv_key_remove(self, event):
        if not self._mv_tecla_ok() or self.manual_sel is None:
            return None
        self._mv_eliminar(self.manual_sel)
        return "break"

    def _mv_key_duplicate(self, event):
        if not self._mv_tecla_ok() or self.manual_sel is None:
            return None
        self._mv_duplicar(self.manual_sel)
        return "break"

    def _mv_key_escape(self, event):
        if not self._mv_tecla_ok():
            return None
        self.manual_sel = None
        self._mv_repintar()
        return None

    def _mv_space_down(self, event):
        if self._mv_tecla_ok():
            self._space_down = True

    def _mv_space_up(self, event):
        self._space_down = False

    # ================================================================== chat
    def _mv_confirmar_chat(self):
        """Antes de que el asistente toque la arquitectura. -> True si se puede seguir."""
        if self.manual is None:
            return True
        if not messagebox.askyesno("Manual layout",
                                   "The layout was edited in MANUAL MODE.\n\nDiscard the manual layout so the "
                                   "assistant can apply its changes?", parent=self):
            return False
        self._mv_descartar("Manual layout discarded: the assistant drives the layout again.")
        self.save_history_state()
        return True
