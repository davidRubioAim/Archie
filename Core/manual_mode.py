"""
v0.76: MODO MANUAL en la Architecture Preview (interfaz). Espacio = play / pausa.

Mixin de MegaStripApp: clic sobre un elemento = selección / arrastre con espejo (el modo manual se activa solo),
menú de clic derecho (Duplicate / Remove), Supr / Ctrl+D / Esc, bloqueo de sliders y vuelta al modo automático
(con el RESET del panel MEDIA ASSETS). Botón 🧲 SNAP (huecos iguales) en modo LOGO; en LOGO UNDER el snap a la
rejilla va siempre activado. La barra de ayuda flota bajo el título y no mueve los paneles.
La lógica de la arquitectura (espejo, empuje, fronteras, LOGO UNDER) vive en manual_layout.py.
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
DRAG_FRAME_MS = 16   # v1.05: repintado del arrastre en modo manual (≈60 fps)
C_ESPEJO = (255, 150, 210, 255)
C_AVISO = "#E67E22"

AYUDA_MANUAL = (
    "MANUAL MODE · quick guide\n\n"
    "There is no button to turn it on: click an ANIM ZONE or a logo in the Architecture Preview and drag it. "
    "The first change switches the layout to MANUAL MODE.\n\n"
    "• Everything is symmetric: the mirror element moves too "
    "(Goal Line Left ↔ Goal Line Right, left ↔ right half of the Touchline).\n"
    "• Elements never overlap. Pushing an element moves its neighbours, keeping at least the Margin; when there "
    "is no room left, the ANIM ZONES squash automatically up to the Squash Limit.\n"
    "• Elements stay inside their section (Goal Line up to the corner, Touchline between corners) and never "
    "cover the Center Board.\n"
    "• Right click an element: Duplicate / Remove. Duplicate moves the element half a step to one side and "
    "drops the copy on the other side.\n"
    "• 🧲 SNAP (LOGO mode): elements move freely; SNAP re-spaces every section with equal gaps, keeping the "
    "order, and copies the last edited side to the other one.\n"
    "• LOGO UNDER: snap is always on, ANIM ZONES snap to the logo grid. Logos can't be moved: Duplicate / Remove "
    "adds or removes one logo in that section and the equidistant grid is rebuilt.\n"
    "• RESET (bottom of the MEDIA ASSETS panel) discards the manual layout and goes back to automatic: "
    "sliders and chat drive the layout again.\n\n"
    "While the layout is manual the distribution sliders are locked. Changing the stadium, its size, the logo "
    "or the ANIM ZONES goes back to automatic. Writing to the assistant asks first.\n\n"
    "Keys: Space: play / pause · Del: Remove · Ctrl+D: Duplicate · Esc: deselect. Undo / Redo work too.\n"
    "Click on an empty area: play / pause. Drag on an empty area or with the middle mouse button: pan.\n\n"
    "The queue, direct render and AE JSON use the manual layout as it is when you add the job. "
    "The 3D Preview shows the changes but can't be edited.\n"
    "• 2 ROWS: AMPLIFY edits the Goal Lines and the amplified Touchline. SINGLE ROW: the 2nd row is one more "
    "section (click on the upper row to pick its elements)."
)

def icono_iman(color=(255, 9, 154), puntas=(235, 235, 235), lado=64):
    """v0.74: imán de herradura con los polos hacia arriba (U), dibujado a 4x y reducido para que quede suave."""
    k = 4
    n = lado * k
    img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    g = int(n * 0.22)                       # grosor de los brazos
    x0, x1 = int(n * 0.14), int(n * 0.86)   # borde exterior
    yt, yb = int(n * 0.10), int(n * 0.90)   # arriba de los polos / abajo de la curva
    r = (x1 - x0) // 2
    yc = yb - r                             # centro de la curva
    d.pieslice((x0, yc - r, x1, yb), 0, 180, fill=color)
    d.rectangle((x0, yt, x0 + g, yc), fill=color)
    d.rectangle((x1 - g, yt, x1, yc), fill=color)
    ri = r - g
    cx = (x0 + x1) // 2
    d.pieslice((cx - ri, yc - ri, cx + ri, yc + ri), 0, 180, fill=(0, 0, 0, 0))
    d.rectangle((x0 + g, yt, x1 - g, yc), fill=(0, 0, 0, 0))
    hp = int(n * 0.17)                      # polos
    d.rectangle((x0, yt, x0 + g, yt + hp), fill=puntas)
    d.rectangle((x1 - g, yt, x1, yt + hp), fill=puntas)
    return img.resize((lado, lado), Image.LANCZOS)


PISTA_MANUAL = "✋ MANUAL MODE · Drag: move · Right click: Duplicate / Remove · Del · Ctrl+D · Esc · ? guide"


class ManualModeMixin:

    # ================================================================== estado e interfaz
    def _mv_init_state(self):
        self.manual = None            # ManualLayout cuando la arquitectura es manual (sliders bloqueados)
        self.manual_sig = None        # firma (estadio, tamaño, ELEMENTS) con la que se creó
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
        self._mv_pan = None
        self.lbl_manual_lock = None

    def _mv_build_ui(self):
        """Botones en la cabecera de la Architecture Preview y barra de ayuda (fila 3 del contenedor)."""
        bc = dict(height=20, fg_color="#2b2b2b", hover_color="#3a3a3a", border_width=1, border_color="gray")
        ctk.CTkButton(self.preview_header, text="?", width=24, command=self._mv_ayuda, **bc).pack(side="right", padx=(5, 10))
        self._img_snap = ctk.CTkImage(icono_iman(), size=(16, 16))
        self._img_snap_off = ctk.CTkImage(icono_iman((110, 110, 110), (150, 150, 150)), size=(16, 16))
        self.btn_snap = ctk.CTkButton(self.preview_header, text="", image=self._img_snap, width=30,
                                      command=self._mv_snap, **bc)
        self.btn_snap.pack(side="right", padx=5)
        # info del elemento bajo el cursor (tamaño y squeeze): flota junto al puntero, no mueve nada
        self.lbl_mv_tip = ctk.CTkLabel(self.preview_image_container, text="", fg_color="#151515", corner_radius=4,
                                       text_color="#EEEEEE", font=ctk.CTkFont(size=11), height=20)
        self._mv_tip_on = False

        # Barra de ayuda: flota (place) arriba a la izquierda de la preview, bajo el título. place() no reparte
        # sitio, así que mostrarla u ocultarla no mueve los paneles ni la imagen.
        self.manual_bar = ctk.CTkFrame(self.preview_image_container, fg_color="#151515", corner_radius=4)
        self.lbl_manual_hint = ctk.CTkLabel(self.manual_bar, text=PISTA_MANUAL, font=ctk.CTkFont(size=11),
                                            text_color="#9a9a9a", anchor="w", height=18)
        self.lbl_manual_hint.pack(side="left", padx=(8, 0))
        self.lbl_manual_msg = ctk.CTkLabel(self.manual_bar, text="", font=ctk.CTkFont(size=11, weight="bold"),
                                           text_color=C_AVISO, anchor="w", height=18)
        self.lbl_manual_msg.pack(side="left", padx=(12, 8))
        self._manual_bar_on = False

        lbl = self.lbl_live_preview
        lbl.bind("<Button-3>", self._mv_menu)
        lbl.bind("<Motion>", self._mv_hover)
        lbl.bind("<Leave>", lambda e: self._mv_tip(None))
        lbl.bind("<ButtonPress-2>", self._mv_pan_start)
        lbl.bind("<B2-Motion>", self._mv_pan_motion)
        lbl.bind("<ButtonRelease-2>", self._mv_pan_end)
        for seq, fn in (("<Delete>", self._mv_key_remove),
                        ("<Control-d>", self._mv_key_duplicate), ("<Control-D>", self._mv_key_duplicate),
                        ("<Escape>", self._mv_key_escape),
                        ("<KeyPress-space>", self._mv_key_space), ("<KeyRelease-space>", self._mv_key_space_up)):
            self.bind_all(seq, fn, add="+")
        self._mv_refresh_ui()

    def _mv_activo(self):
        """La barra de ayuda se ve con la arquitectura manual o con un elemento seleccionado."""
        return self.manual is not None or self.manual_sel is not None

    def _mv_refresh_ui(self):
        if not hasattr(self, "manual_bar"):
            return
        on = self._mv_activo()
        if on != self._manual_bar_on:
            self._manual_bar_on = on
            if on:
                self.manual_bar.place(x=0, y=0, anchor="nw")
                self.manual_bar.lift()
            else:
                self.manual_bar.place_forget()
        d = self._disp_datos
        lu = bool(((d or {}).get("config") or {}).get("logo_under"))
        ok = ManualLayout.soportado(d)[0] and not lu
        try:
            self.btn_snap.configure(state="normal" if ok else "disabled",
                                    image=self._img_snap if ok else self._img_snap_off)
        except Exception:
            pass
        self._mv_cursor("")

    def _mv_msg(self, text, color=C_AVISO, segundos=6):
        if not text:
            return
        debug(f"Manual: {text}")
        if self._manual_msg_timer:
            try: self.after_cancel(self._manual_msg_timer)
            except Exception: pass
            self._manual_msg_timer = None
        if self._mv_activo() and hasattr(self, "lbl_manual_msg"):
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

    # ================================================================== firma / vuelta a automático
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

    # ================================================================== bloqueo de sliders
    def _mv_bloqueables(self):
        ws = [self.margin_slider, self.margin_tl_slider, self.offset_slider, self.stretch_slider,
              self.qty_goal_slider, self.qty_corner_slider, self.qty_cb_slider, self.qty_tl_slider,
              self.max_anim_gl_slider, self.max_anim_tl_slider, self.qty_gl_under_slider, self.qty_tl_under_slider]
        ws += [sl for sl, _, _ in getattr(self, "f2_sliders", {}).values()]   # v0.95: 2nd row (SINGLE ROW)
        if hasattr(self, "chk_f2_custom"):
            ws.append(self.chk_f2_custom)
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
                                        "Press RESET (bottom of MEDIA ASSETS) to go back to automatic.",
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
    def _mv_nat(self, d):
        """Anchos naturales (px del strip) de ANIM ZONE y logo para esta arquitectura -> {tipo: px}."""
        c = getattr(self, "_mv_nat_cache", None)
        if c is not None and c[0] is d:
            return c[1]
        res = {}
        try:
            dims = d["dimensiones_globales"]
            z = d["zonas"]
            dist = Distribuidor(dims["width"], dims["height"], z["ZONA_1"]["width"], z.get("ZONA_3", {}).get("width", 0),
                                int(self.margin_slider.get()), self.archivos_cargados, d.get("config") or {})
            res = {"anim_zone": float(dist.anim_w), "static_logo": float(dist.logo_w)}
        except Exception as e:
            log_error(f"Manual: anchos naturales no disponibles: {e}")
        self._mv_nat_cache = (d, res)
        return res

    def _mv_info(self, m, uid):
        """'ANIM ZONE · 2040 px · Squeeze 15%' del elemento."""
        e = m.el(uid) if m is not None else None
        if e is None:
            return ""
        nombre = {"anim_zone": "ANIM ZONE", "static_logo": "LOGO", "center_board": "CENTER BOARD"}.get(e["tipo"], "")
        txt = f"{nombre} · {int(round(e['w']))} px"
        nat = self._mv_nat(self._disp_datos).get(e["tipo"]) if self._disp_datos else None
        if e["tipo"] == "anim_zone" and m.nat:
            nat = m._nat(m._par(e)) if hasattr(m, "_nat") else m.nat
        if nat:
            pct = (1.0 - e["w"] / float(nat)) * 100.0
            if pct > 0.05:
                txt += f" · Squeeze {pct:.0f}%" if pct >= 1 else f" · Squeeze {pct:.1f}%"
            elif pct < -0.05:
                txt += f" · Stretch {-pct:.0f}%"
            else:
                txt += " · Squeeze 0%"
        return txt

    def _mv_tip(self, event, texto=""):
        if event is None or not texto:
            if self._mv_tip_on:
                self._mv_tip_on = False
                self.lbl_mv_tip.place_forget()
            return
        cont = self.preview_image_container
        x = event.x_root - cont.winfo_rootx() + 14
        y = event.y_root - cont.winfo_rooty() + 16
        if self.lbl_mv_tip.cget("text") != texto:
            self.lbl_mv_tip.configure(text=f" {texto} ")
        tw = max(self.lbl_mv_tip.winfo_reqwidth(), 8 * len(texto))
        if x + tw > cont.winfo_width():
            x = max(0, event.x_root - cont.winfo_rootx() - tw - 8)
        if y + 22 > cont.winfo_height():
            y = max(0, y - 40)
        self.lbl_mv_tip.place(x=x, y=y, anchor="nw")
        if not self._mv_tip_on:
            self._mv_tip_on = True
            self.lbl_mv_tip.lift()

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
            if not getattr(self, "_manual_ayuda_vista", False):
                self._manual_ayuda_vista = True
                self.after(6500, lambda: None if self.lbl_manual_msg.cget("text") else
                           self._mv_msg("MANUAL MODE on · press ? for the guide.", "gray", 8))
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
        self.start_preview_render(quiet=True)   # v1.05: sin oscurecer ni bloquear el siguiente arrastre

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

    def _mv_rects(self, x, w, v, segs, h, y0=0.0, y1=None):
        """Rectángulos en pantalla (puede haber 2 si cruza filas) de un tramo [x, x+w) del strip.
        v0.95: y0 / y1 = franja vertical del elemento en el strip (2 rows); por defecto el alto completo."""
        y1 = h if y1 is None else y1
        out = []
        for x1m, x2m, xr, yr in segs:
            a, b = max(x, x1m), min(x + w, x2m)
            if b <= a:
                continue
            out.append((v["px"] + (xr + a - x1m) * v["sx"], v["py"] + (yr + y0) * v["sy"],
                        v["px"] + (xr + b - x1m) * v["sx"], v["py"] + (yr + y1) * v["sy"]))
        return out

    @staticmethod
    def _mv_caja(m, e):
        """(y0, y1) de un elemento del modelo (2 rows)."""
        try:
            return m._caja(e)
        except Exception:
            return 0.0, None

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
        self._mv_ultimo_y = None
        for x1m, x2m, xr, yr in segs:
            if yr - tol <= ry < yr + h + tol and xr <= rx < xr + (x2m - x1m):
                self._mv_ultimo_y = min(max(ry - yr, 0.0), h - 1.0)   # v0.95: fila del clic (2 rows)
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
        """Contorno del elemento seleccionado (y de su espejo) encima de la preview."""
        if self.manual_sel is None or img is None or self.loading_overlay_on:
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
            for uid, color, ancho in ((espejo, C_ESPEJO, 1), (sel, C_SEL, 2)):
                e = m.el(uid) if uid is not None else None
                if e is None:
                    continue
                for r in self._mv_rects(e["x"], e["w"], v, segs, h, *self._mv_caja(m, e)):
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
        base_rgba = base if base.mode == "RGBA" else base.convert("RGBA")   # v1.05: una conversión, no una por elemento
        img = base_rgba.copy()
        viejo = {e["uid"]: (e["x"], e["w"]) for e in antes.els}
        nuevo = {e["uid"]: (e["x"], e["w"], e["src"]) for e in despues.els}
        cajas = {e["uid"]: self._mv_caja(antes, e) for e in antes.els}          # v0.95: 2 rows
        cajas.update({e["uid"]: self._mv_caja(despues, e) for e in despues.els})
        cambia_v = [u for u in viejo if u not in nuevo or nuevo[u][:2] != viejo[u]]
        cambia_n = [u for u in nuevo if u not in viejo or nuevo[u][:2] != viejo[u]]
        sombra = (0, 0, 0, 165)
        for u in cambia_v:
            for r in self._mv_rects(viejo[u][0], viejo[u][1], v, segs, h, *cajas[u]):
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
            r_src = self._mv_rects(viejo[fuente][0], viejo[fuente][1], v, segs, h, *cajas[fuente])
            r_dst = self._mv_rects(x, w, v, segs, h, *cajas[u])
            if not r_src or not r_dst:
                continue
            s = tuple(int(round(c)) for c in r_src[0])
            for r in r_dst:
                d = tuple(int(round(c)) for c in r)
                dw, dh = d[2] - d[0], d[3] - d[1]
                if dw <= 0 or dh <= 0 or s[2] <= s[0] or s[3] <= s[1]:
                    continue
                trozo = base_rgba.crop(s)
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
                for r in self._mv_rects(eo["x"], eo["w"], v, segs, h, *self._mv_caja(despues, eo)):
                    dr.rectangle((r[0] - 1, r[1] - 1, r[2] + 1, r[3] + 1), outline=C_ESPEJO, width=1)
            rs = self._mv_rects(e["x"], e["w"], v, segs, h, *self._mv_caja(despues, e))
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
        """on_drag_start de la Architecture Preview. True si el clic cae sobre un elemento (lo gestiona el modo
        manual); False en zona vacía, que sigue con el comportamiento normal (play / pausa y pan)."""
        self._mv_tip(None)
        m = self._mv_modelo(avisar=False)
        x = self._mv_evento_x(event) if m is not None else None
        uid = m.hit(x, getattr(self, "_mv_ultimo_y", None)) if x is not None else None
        if uid is None:
            if self.manual_sel is not None:
                self.manual_sel = None
                self._mv_refresh_ui()
                self._mv_repintar()
            return False
        try: self.preview_image_container.focus_set()
        except Exception: pass
        self.manual_sel = uid
        self._mv_refresh_ui()
        ok, msg = m.puede_mover(uid)
        if not ok or self.render_busy or self._shown_strip is None:
            self.manual_drag = None
            self._mv_msg(msg if not ok else "Wait a moment: the preview is still updating.", C_AVISO if not ok else "gray",
                         6 if not ok else 3)
            self._mv_repintar()
            self.manual_drag = {"noop": True}   # el clic es del modo manual: ni pan ni play / pausa
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
        d = self.manual_drag
        if d is None:
            return False
        if d.get("noop"):
            return True
        dxp = event.x_root - d["x"]
        if not d["moved"] and abs(dxp) < 3:
            return True
        d["moved"] = True
        # v1.05: el ratón manda muchos más eventos de los que se pueden pintar; se guarda el último y se pinta
        # una vez por fotograma (~60 fps). Antes cada evento clonaba el modelo y recomponía la vista: lag al arrastrar.
        d["dxp"] = dxp
        if d.get("timer") is None:
            d["timer"] = self.after(DRAG_FRAME_MS, self._mv_motion_tick)
        return True

    def _mv_motion_tick(self):
        d = self.manual_drag
        if d is None or d.get("noop"):
            return
        d["timer"] = None
        self._mv_arrastre(d)

    def _mv_arrastre(self, d):
        """Aplica el último desplazamiento pendiente del arrastre (vista rápida, sin render)."""
        dxp = d.get("dxp")
        if dxp is None or dxp == d.get("aplicado"):
            return
        d["aplicado"] = dxp
        v = d["view"]
        if v is None:
            return
        m = d["snap"].clone()
        ok, _ = m.mover(d["uid"], dxp / v["sx"])
        if not ok:
            return
        d["model"] = m
        img, d["limpia"] = self._mv_overlay(d["snap"], m, d["base"], v, sel=d["uid"])
        self._mv_mostrar(img)

    def _mv_release(self, event):
        d = self.manual_drag
        if d is None:
            return False
        self.manual_drag = None
        if d.get("noop"):
            return True
        if d.get("timer") is not None:   # v1.05: último movimiento pendiente, antes de confirmar
            try: self.after_cancel(d["timer"])
            except Exception: pass
            d["timer"] = None
        if d["moved"]:
            self._mv_arrastre(d)
        if d["moved"] and d.get("model") is not None:
            if d.get("limpia") is not None:
                self._shown_strip, self._shown_strip_player = d["limpia"], False
            self._mv_commit(d["model"])
        else:
            self._mv_repintar()
        return True

    def _mv_hover(self, event):
        if self.manual_drag is not None:
            self._mv_tip(None)
            return
        if self.manual is None and self._medios_pendientes():   # v1.11: sin esperar al NAS al pasar el ratón
            self._mv_tip(None)
            return
        m = self._mv_modelo(avisar=False)
        x = self._mv_evento_x(event) if m is not None else None
        uid = m.hit(x, getattr(self, "_mv_ultimo_y", None)) if x is not None else None
        self._mv_tip(event, self._mv_info(m, uid) if uid is not None else "")
        if uid is not None and m.puede_mover(uid)[0]:
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
        m = self._mv_modelo()
        x = self._mv_evento_x(event) if m is not None else None
        uid = m.hit(x, getattr(self, "_mv_ultimo_y", None)) if x is not None else None
        if uid is None:
            return
        self.manual_sel = uid
        self._mv_refresh_ui()
        self._mv_repintar()
        self._mv_tip(None)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=self._mv_info(m, uid), state="disabled")
        menu.add_separator()
        menu.add_command(label="Duplicate    Ctrl+D", command=lambda: self._mv_duplicar(uid),
                         state="normal" if m.puede_duplicar(uid) else "disabled")
        menu.add_command(label="Remove        Del", command=lambda: self._mv_eliminar(uid),
                         state="normal" if m.puede_eliminar(uid) else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _mv_snap(self):
        """🧲 SNAP (modo LOGO): huecos iguales en cada tramo, mismo orden, y espejo del último lado editado."""
        m = self._mv_modelo()
        if m is None:
            return
        if m.lu:
            self._mv_msg("LOGO UNDER already snaps to the logo grid.", "gray", 4)
            return
        if self._mv_operar(lambda mm: mm.magnet()):
            self._mv_msg("SNAP: equal spacing in every section.", "gray", 4)

    def _mv_duplicar(self, uid):
        self._mv_operar(lambda m: m.duplicar(uid))

    def _mv_eliminar(self, uid):
        if self._mv_operar(lambda m: m.eliminar(uid)):
            if self.manual is not None and self.manual.el(uid) is None:
                self.manual_sel = None

    # ================================================================== teclado
    def _mv_tecla_ok(self):
        try:
            w = self.focus_get()
        except Exception:
            w = None
        if isinstance(w, (tk.Entry, tk.Text)):
            return False
        return True

    def _mv_key_space(self, event):
        """v0.76: Espacio = play / pausa de la preview (como el clic en una zona vacía).
        No actúa mientras se escribe (chat, campos) ni repite al mantener la tecla pulsada."""
        if not self._mv_tecla_ok() or getattr(self, "_space_held", False):
            return None
        self._space_held = True
        if self.player.ready:          # igual que el botón ▶ del reproductor
            self.player.toggle()
        elif self.pil_preview_images:  # sin vídeos: pasa al siguiente key frame, como el clic
            self.ciclar_preview()
        return "break"

    def _mv_key_space_up(self, event):
        self._space_held = False

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
        if not self._mv_tecla_ok() or self.manual_sel is None:
            return None
        self.manual_sel = None
        self._mv_refresh_ui()
        self._mv_repintar()
        return None

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
