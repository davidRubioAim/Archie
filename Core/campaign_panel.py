"""
campaign_panel.py (v0.58, v0.59)
Pestaña CAMPAIGNS de Media Assets: batch de campañas para el estadio seleccionado.

  Buscador: se escribe el código exacto de la campaña (ADID-RMXX-261001) y Enter / Add. Se pueden pegar varios.
  CAMPAIGN CODES: la lista que se procesa, con su estado. Al hacer clic en una campaña se cargan sus ELEMENTS
  (NAS) y SU arquitectura en Manual Edit y en las tres previews. Cada campaña guarda su propia arquitectura.

Botones: Mark as reviewed (guarda el JSON de arquitectura) y Export ready campaigns (System File + JSON
+ vídeo de previsualización 3D de cada campaña lista para renderizar, a la cola de render).
"""
import copy
import re
import os
import subprocess
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

import customtkinter as ctk

import campaign_batch as cb
from campaign_loader import load_campaigns, normalizar_codigo, codigo_valido
from nas_campaigns import NasIndex, mensaje, ST_READY
from auto_solver import config_campana, CONFIG_KEYS
from log_utils import debug, log_error

COLORES = {
    cb.S_CHECKING: "#7A7A7A", cb.S_NO_NAS: "#C0392B", cb.S_MISSING: "#C0392B", cb.S_INCOMPLETE: "#C0392B",
    cb.S_REVIEW_FILES: "#E67E22", cb.S_PENDING: "#7A7A7A", cb.S_UNREVIEWED: "#2E86C1", cb.S_REVIEWED: "#27AE60",
}
ICONOS = {
    cb.S_CHECKING: "…", cb.S_NO_NAS: "⚠", cb.S_MISSING: "⚠", cb.S_INCOMPLETE: "⚠", cb.S_REVIEW_FILES: "?",
    cb.S_PENDING: "○", cb.S_UNREVIEWED: "◐", cb.S_REVIEWED: "✔",
}
TREE_ROWS = 7
CAPTURE_DELAY_MS = 500
LOAD_SETTLE_MS = 900


class CampaignPanel:
    def __init__(self, app, parent, config_dir):
        self.app = app
        self.config_dir = config_dir
        self.nas = NasIndex(config_dir, log=print)
        self.campaigns = []          # lista de la hoja
        self.by_code = {}
        self.list_info = {"source": "none", "detail": "Loading…", "invalid": [], "duplicates": {}}
        self.approved = []           # códigos en CAMPAIGN CODES (orden)
        self._rechecked = set()      # campañas no listas que ya se volvieron a comprobar al hacer clic
        self.elements = {}           # código -> info de nas_campaigns.inspeccionar (no depende del estadio)
        self.states = {}             # estadio -> {código: CampaignState}
        self.active = None           # código cargado en la interfaz
        self.active_stadium = None
        self.loading = False         # aplicando una campaña a la interfaz (no capturar)
        self.pending_activate = None
        self._capture_timer = None
        self.exporting = False
        self.local_dir = cb.leer_ajustes(config_dir).get("architecture_dir")

        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        self._build()
        threading.Thread(target=self._cargar_lista, daemon=True).start()
        threading.Thread(target=self._escanear_nas, daemon=True).start()

    # ================================================================ UI
    def _build(self):
        app = self.app
        f = self.frame
        style = ttk.Style(f)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        fuente = tkfont.Font(f, family="Open Sans", size=9)
        self._fuente = fuente
        alto_fila = fuente.metrics("linespace") + 6
        w_code = fuente.measure("ADID-RMXX-261001") + 18
        w_status = fuente.measure("◐ Not reviewed") + 18
        style.configure("Archie.Treeview", font=fuente, rowheight=alto_fila, background=app.c_white,
                        fieldbackground=app.c_white, foreground=app.c_text_dark, borderwidth=0)
        style.configure("Archie.Treeview.Heading", font=(fuente.actual("family"), 9, "bold"), background=app.c_light_grey,
                        foreground=app.c_text_dark, relief="flat")
        style.map("Archie.Treeview", background=[("selected", app.c_light_magenta)],
                  foreground=[("selected", app.c_text_dark)])

        w_name = 150
        top = ctk.CTkFrame(f, fg_color="transparent")
        top.pack(fill="x", padx=5, pady=(0, 6))
        top.grid_columnconfigure(0, weight=1)
        # v0.59: solo se añaden campañas escribiendo su código exacto (se pueden pegar varios separados por espacios/comas)
        self.entry_code = ctk.CTkEntry(top, height=28, fg_color=app.c_white,
                                       text_color=app.c_text_dark,
                                       placeholder_text="🔍 Enter the exact campaign code (e.g. ADID-RMXX-261001) and press Enter")
        self.entry_code.grid(row=0, column=0, sticky="ew")
        self.entry_code.bind("<Return>", lambda e: self.agregar_desde_buscador())
        ctk.CTkButton(top, text="Add", width=60, height=28, fg_color="#2b2b2b", hover_color=app.c_magenta,
                      font=app.font_t2, command=self.agregar_desde_buscador).grid(row=0, column=1, padx=(6, 0))

        self.lbl_right = ctk.CTkLabel(f, text="CAMPAIGN CODES", font=app.font_t2, text_color=app.c_text_dark)
        self.lbl_right.pack(anchor="w", padx=5)
        rw = tk.Frame(f, bg=app.c_white, highlightthickness=1, highlightbackground="#E0E0E0",
                      highlightcolor="#E0E0E0")
        rw.pack(fill="x", padx=5)
        self.right_wrap = rw
        self.tree_r = ttk.Treeview(rw, columns=("status", "code", "name"), show="headings", height=TREE_ROWS,
                                   style="Archie.Treeview", selectmode="extended")
        self.tree_r.heading("status", text="Status")
        self.tree_r.heading("code", text="Code")
        self.tree_r.heading("name", text="Project name")
        self.tree_r.column("status", width=w_status, minwidth=w_status, stretch=False)
        self.tree_r.column("code", width=w_code, minwidth=w_code, stretch=False)
        self.tree_r.column("name", width=w_name, minwidth=60, stretch=True)
        sr = ttk.Scrollbar(rw, orient="vertical", command=self.tree_r.yview)
        self.tree_r.configure(yscrollcommand=sr.set)
        self.tree_r.pack(side="left", fill="both", expand=True)
        sr.pack(side="right", fill="y")
        for st, color in COLORES.items():
            self.tree_r.tag_configure(st, foreground=color)
        self.tree_r.tag_configure("active", background="#FDEAF4")
        self.lbl_drop_hint = tk.Label(rw, text="Add campaigns by typing their exact code above", fg="#A0A0A0",
                                      bg=app.c_white, font=(fuente.actual("family"), 9))
        self.lbl_drop_hint.place(relx=0.5, rely=0.55, anchor="center")

        # --- abajo: estado, acciones y aviso
        self.lbl_info = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=11), text_color=app.c_text_muted,
                                     anchor="w", justify="left", wraplength=640)
        self.lbl_info.pack(fill="x", padx=5, pady=(4, 0))
        acts = ctk.CTkFrame(f, fg_color="transparent")
        acts.pack(fill="x", padx=5, pady=(4, 0))
        acts.grid_columnconfigure(0, weight=1)
        acts.grid_columnconfigure(1, weight=1)
        self.btn_review = ctk.CTkButton(acts, text="✔ Mark as reviewed", height=30, fg_color="#27AE60",
                                        hover_color="#2ECC71", font=app.font_t2, command=self.marcar_revisada,
                                        state="disabled")
        self.btn_review.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.btn_export = ctk.CTkButton(acts, text="Export ready campaigns", height=30, fg_color=app.c_magenta,
                                        hover_color="#D1007A", font=app.font_t2, command=self.exportar)
        self.btn_export.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkLabel(f, text="ⓘ " + cb.DISCLAIMER, font=ctk.CTkFont(size=10), text_color=app.c_text_muted,
                     anchor="w", justify="left", wraplength=660).pack(fill="x", padx=5, pady=(4, 0))

        # --- eventos
        tr = self.tree_r
        tr.bind("<<TreeviewSelect>>", lambda e: self._on_select_right())
        tr.bind("<Delete>", lambda e: self.quitar(self._sel(tr)))
        tr.bind("<BackSpace>", lambda e: self.quitar(self._sel(tr)))
        tr.bind("<Button-3>", self._menu_right)
        self._actualizar_cabeceras()

    @staticmethod
    def _sel(tree):
        return list(tree.selection())

    # ================================================================ lista de la hoja
    def _cargar_lista(self):
        """La lista de la hoja (o su copia local) solo se usa para el nombre del proyecto y avisar de duplicados."""
        try:
            campaigns, info = load_campaigns(self.config_dir, log=print)
        except Exception as e:
            log_error(f"[Campañas] {e}")
            return
        self.app.after(0, lambda: self._aplicar_lista(campaigns, info))

    def _aplicar_lista(self, campaigns, info):
        self.campaigns, self.list_info = campaigns, info
        self.by_code = {c["code"]: c for c in campaigns}
        for code in self.approved:
            self._pintar_fila(code)

    def _nombre(self, code):
        c = self.by_code.get(code)
        if not c:
            return "(not in the project list)" if self.by_code else ""
        if c["duplicate"]:
            return "⚠ DUPLICATED · " + " | ".join(n or "(no name)" for n in c["names"])
        return c["name"]

    # ================================================================ NAS
    def _escanear_nas(self):
        # Al arrancar se usa el índice en caché; si está vacío o es de otro día, se reescanea en segundo plano
        hoy = __import__("time").strftime("%Y-%m-%d")
        if not self.nas.codes or not (self.nas.scanned_at or "").startswith(hoy):
            self.nas.escanear()

    def _comprobar(self, code):
        stadium = self.stadium()

        def worker():
            try:
                info = self.nas.inspeccionar(code)
            except Exception as e:
                log_error(f"[Campañas] {code}: {e}")
                info = {"status": "not_found", "files": None, "missing": [], "ambiguous": [],
                        "path": self.nas.ruta_esperada(code)}
            doc, path = (None, None)
            try:
                doc, path = cb.cargar(self.nas, code, stadium, self.local_dir)
            except Exception as e:
                log_error(f"[Campañas] leyendo arquitectura de {code}: {e}")
            self.app.after(0, lambda: self._on_comprobada(code, stadium, info, doc, path))
        threading.Thread(target=worker, daemon=True).start()

    def _on_comprobada(self, code, stadium, info, doc, path):
        self.elements[code] = info
        st = self._state(code, stadium)
        if doc and st.config is None and not st.loaded_from_disk:
            cb.aplicar_documento(st, doc, path, self.nas.root)
            debug(f"[Campañas] {code} @ {stadium}: arquitectura guardada cargada de {path}")
        if not st.files_manual and info.get("files"):
            st.files = copy.deepcopy(info["files"])
        self._pintar_fila(code)
        if self.pending_activate == code and stadium == self.stadium():
            self.pending_activate = None
            self.activar(code)

    def _recomprobar(self, codes):
        for code in codes:
            self.elements.pop(code, None)
            self._pintar_fila(code)
            self._comprobar(code)

    def _recomprobar_todas(self):
        for code in self.approved:
            self.elements.pop(code, None)
            self._pintar_fila(code)
            self._comprobar(code)

    # ================================================================ estado
    def stadium(self):
        return self.app.stadium_selector_global.get()

    def _state(self, code, stadium=None):
        stadium = stadium or self.stadium()
        return self.states.setdefault(stadium, {}).setdefault(code, cb.CampaignState(code))

    def estado(self, code):
        return cb.estado(self._state(code), self.elements.get(code))

    def _pintar_fila(self, code):
        if not self.tree_r.exists(code):
            return
        st = self.estado(code)
        tags = [st] + (["active"] if code == self.active else [])
        self.tree_r.item(code, values=(f"{ICONOS[st]} {cb.ETIQUETAS[st]}", code, self._nombre(code)), tags=tags)
        if code == self.active:
            self._actualizar_info()

    def _actualizar_cabeceras(self):
        n = len(self.approved)
        ok = sum(1 for c in self.approved if cb.exportable(self.estado(c)))
        rev = sum(1 for c in self.approved if self.estado(c) == cb.S_REVIEWED)
        self.lbl_right.configure(text=f"CAMPAIGN CODES ({n})" + (f" · {ok} ready to render · {rev} reviewed" if n else ""))
        if n:
            self.lbl_drop_hint.place_forget()
        else:
            self.lbl_drop_hint.place(relx=0.5, rely=0.55, anchor="center")

    def _actualizar_info(self, extra=None):
        if extra is not None:
            self.lbl_info.configure(text=extra[0], text_color=extra[1])
            if not self.active:
                self.btn_review.configure(state="disabled", text="✔ Mark as reviewed")
            return
        code = self.active
        if not code:
            self.lbl_info.configure(text="Click a campaign in CAMPAIGN CODES to load its Elements and architecture for this stadium.",
                                    text_color=self.app.c_text_muted)
            self.btn_review.configure(state="disabled", text="✔ Mark as reviewed")
            return
        st = self._state(code)
        name = self.by_code.get(code, {}).get("name", "")
        estado = self.estado(code)
        txt = f"Editing {code}" + (f" · {name}" if name else "") + f" · {cb.ETIQUETAS[estado]}"
        if st.saved_path and st.reviewed:
            txt += f"\nSaved: {st.saved_path}"
        self.lbl_info.configure(text=txt, text_color=COLORES.get(estado, self.app.c_text_muted))
        if st.reviewed:
            self.btn_review.configure(state="normal", text="↺ Mark as not reviewed")
        else:
            self.btn_review.configure(state="normal" if st.config else "disabled", text="✔ Mark as reviewed")

    # ================================================================ añadir / quitar
    def agregar_desde_buscador(self):
        texto = self.entry_code.get()
        tokens = [normalizar_codigo(t) for t in re.split(r"[\s,;]+", texto) if t.strip()]
        if not tokens:
            return
        malos = [t for t in tokens if not codigo_valido(t)]
        buenos = [t for t in tokens if codigo_valido(t)]
        ya = [t for t in buenos if t in self.approved]
        nuevos = self.agregar(buenos)
        if malos:
            self._actualizar_info((f"Invalid code format: {', '.join(malos)}. "
                                   "Use the exact code BRND-CHNL-NNNNNN (e.g. ADID-RMXX-261001).", "#C0392B"))
            self._set_buscador(" ".join(malos))
        else:
            self._set_buscador("")
            if ya and not nuevos:
                self._actualizar_info((f"{', '.join(ya)} is already in the list.", "#E67E22"))
                self.tree_r.selection_set(ya[0])
                self.tree_r.see(ya[0])

    def _set_buscador(self, texto):
        self.entry_code.delete(0, "end")
        if texto:
            self.entry_code.insert(0, texto)
        else:
            self.app.focus_set()      # vuelve a mostrar el texto de ayuda
            self.entry_code.focus_set()

    def agregar(self, codes):
        nuevos = []
        for code in codes:
            if code in self.approved or code in nuevos:
                continue
            nuevos.append(code)
            self.approved.append(code)
            self.tree_r.insert("", "end", iid=code, values=("", code, self._nombre(code)))
            self._pintar_fila(code)
            if code in self.elements:
                self._on_comprobada(code, self.stadium(), self.elements[code], None, None)
            self._comprobar(code)
        dups = [c for c in nuevos if self.by_code.get(c, {}).get("duplicate")]
        if dups:
            messagebox.showwarning(
                "Duplicated campaign code",
                "These codes appear more than once in the project sheet. Please review them manually:\n\n" +
                "\n".join(f"{c}: " + " | ".join(self.by_code[c]["names"]) for c in dups), parent=self.app)
        self._actualizar_cabeceras()
        if nuevos:
            self.tree_r.see(nuevos[-1])
            if not self.active:
                self.tree_r.selection_set(nuevos[0])
        return nuevos

    def quitar(self, codes):
        for code in codes:
            if code not in self.approved:
                continue
            if code == self.active:
                self._capturar()
                self.active = None
                self._actualizar_info()
            self.approved.remove(code)
            self.tree_r.delete(code)
        self._actualizar_cabeceras()

    def _menu_right(self, e):
        item = self.tree_r.identify_row(e.y)
        if not item:
            return
        if item not in self.tree_r.selection():
            self.tree_r.selection_set(item)
        m = tk.Menu(self.tree_r, tearoff=0)
        m.add_command(label="Check Elements again", command=lambda: self._recomprobar(self._sel(self.tree_r)))
        m.add_command(label="Remove from the list", command=lambda: self.quitar(self._sel(self.tree_r)))
        info = self.elements.get(item)
        if info and info.get("path"):
            m.add_command(label="Open Elements folder", command=lambda: self._abrir_carpeta(info["path"]))
            m.add_command(label="Copy Elements path", command=lambda: self._copiar(info["path"]))
        m.tk_popup(e.x_root, e.y_root)

    # ================================================================ activar campaña
    def _on_select_right(self):
        sel = self._sel(self.tree_r)
        if len(sel) != 1:
            return
        code = sel[0]
        if code != self.active:
            self.activar(code)

    def activar(self, code):
        app = self.app
        if self.exporting:
            return
        if app.solver_busy:
            self.pending_activate = code
            return
        self._capturar()
        info = self.elements.get(code)
        if info is None:
            self.pending_activate = code
            self._actualizar_info((f"Checking Elements of {code} on the NAS…", self.app.c_text_muted))
            return
        st = self._state(code)
        estado = self.estado(code)
        if estado in (cb.S_NO_NAS, cb.S_MISSING, cb.S_INCOMPLETE) and code not in self._rechecked:
            # Puede que el estudio ya haya subido los Elements: se vuelve a mirar el NAS antes de avisar
            self._rechecked.add(code)
            self.elements.pop(code, None)
            self.pending_activate = code
            self._pintar_fila(code)
            self._actualizar_info((f"Checking Elements of {code} on the NAS…", self.app.c_text_muted))
            self._comprobar(code)
            return
        self._rechecked.discard(code)
        if estado in (cb.S_NO_NAS, cb.S_MISSING, cb.S_INCOMPLETE):
            anterior = self.active
            self.active = None
            if anterior:
                self._pintar_fila(anterior)
            self._actualizar_info((f"{code}: " + mensaje(code, info).split("\n")[0], "#C0392B"))
            self._dialogo_elements(code, info)
            return

        anterior, self.active = self.active, code
        self.active_stadium = self.stadium()
        if anterior:
            self._pintar_fila(anterior)
        self._pintar_fila(code)
        self.loading = True
        app.cargar_archivos_campana(st.files)
        app.output_filename_var.set(code)
        if st.config:
            app.aplicar_config_campana(st.config)
            app.after(LOAD_SETTLE_MS, lambda c=code: self._fin_carga(c))
        else:
            app.preparar_campana_nueva()
            app.auto_create_architecture()   # al terminar llama a on_solver_done
        if estado == cb.S_REVIEW_FILES:
            self._dialogo_elements(code, info)
        self._actualizar_info()

    def on_solver_done(self, msg):
        """Lo llama app._on_solver_done: la arquitectura por defecto de la campaña activa ya está en la interfaz."""
        if self.active and self.loading:
            self._state(self.active).solver_msg = msg
            self.app.after(LOAD_SETTLE_MS, lambda c=self.active: self._fin_carga(c))
        if self.pending_activate:
            code, self.pending_activate = self.pending_activate, None
            self.app.after(10, lambda: self.activar(code))

    def _fin_carga(self, code):
        if code != self.active:
            return
        self.loading = False
        st = self._state(code)
        # Línea base: lo que quedó en la interfaz (los sliders pueden redondear o acotar valores)
        st.config = self.app.config_actual()
        st.files = copy.deepcopy(self.app.archivos_cargados)
        self._pintar_fila(code)
        self._actualizar_cabeceras()

    def on_ui_change(self):
        """Lo llama app.trigger_update: algo cambió en Manual Edit / archivos."""
        if not self.active or self.loading:
            return
        if self._capture_timer:
            self.app.after_cancel(self._capture_timer)
        self._capture_timer = self.app.after(CAPTURE_DELAY_MS, self._capturar)

    def _capturar(self):
        self._capture_timer = None
        code = self.active
        if not code or self.loading or self.active_stadium != self.stadium():
            return
        app = self.app
        st = self._state(code)
        files = copy.deepcopy(app.archivos_cargados)
        if files != st.files:
            st.files = files
            st.files_manual = True
        if app.architecture_created and app.arch_stadium == self.active_stadium and not app.solver_busy:
            cfg = app.config_actual()
            if cfg != st.config:
                if st.reviewed:
                    st.reviewed = False
                st.config = cfg
        self._pintar_fila(code)
        self._actualizar_cabeceras()

    # ================================================================ estadio
    def before_stadium_change(self):
        if self.active:
            self._capturar()
            anterior, self.active = self.active, None
            self._pintar_fila(anterior)

    def after_stadium_change(self):
        for code in self.approved:
            self._pintar_fila(code)
            self._comprobar(code)   # carga la arquitectura guardada para el nuevo estadio, si existe
        self._actualizar_cabeceras()
        self._actualizar_info()

    # ================================================================ revisar / guardar
    def _perfil(self):
        return self.app.stadium_data.get(self.stadium())

    def _documento(self, code, stadium=None):
        stadium = stadium or self.stadium()
        st = self._state(code, stadium)
        datos = cb.construir_datos(self.app.stadium_data[stadium], st.files, st.config)
        if not datos:
            raise RuntimeError(f"{code}: layout could not be calculated")
        return cb.documento(datos, st, self.by_code.get(code), stadium, self.app.current_version_str)

    def _pedir_carpeta_local(self, motivo):
        messagebox.showinfo(
            "Save architecture",
            f"{motivo}\n\nArchie cannot write to the campaign folder on the NAS (read-only access). "
            "Please choose a local folder to save the architecture JSON files.\n\n" + cb.DISCLAIMER, parent=self.app)
        folder = ctk.filedialog.askdirectory(title="Folder for the architecture JSON files")
        if folder:
            self.local_dir = folder
            cb.guardar_ajuste(self.config_dir, "architecture_dir", folder)
        return folder

    def _guardar(self, code, stadium=None, doc=None):
        stadium = stadium or self.stadium()
        doc = doc or self._documento(code, stadium)
        path, donde = cb.guardar(doc, self.nas, code, stadium, self.local_dir)
        if path is None and not self.local_dir:
            if self._pedir_carpeta_local(f"Saving the architecture of {code}."):
                path, donde = cb.guardar(doc, self.nas, code, stadium, self.local_dir)
        if path:
            st = self._state(code, stadium)
            st.saved_path, st.saved_at = path, doc["archie"]["saved_at"]
        return path, donde

    def marcar_revisada(self):
        code = self.active
        if not code:
            return
        self._capturar()
        st = self._state(code)
        if st.reviewed:
            st.reviewed = False
            self._pintar_fila(code)
            self._actualizar_cabeceras()
            return
        if not st.config:
            return
        st.reviewed = True
        try:
            path, donde = self._guardar(code)
        except Exception as e:
            log_error(f"[Campañas] guardando {code}: {e}")
            path, donde = None, None
        self._pintar_fila(code)
        self._actualizar_cabeceras()
        if path:
            where = "on the NAS" if donde == "nas" else "locally"
            self._actualizar_info((f"✔ {code} reviewed · architecture saved {where}:\n{path}", "#27AE60"))
        else:
            self._actualizar_info((f"✔ {code} reviewed, but the architecture JSON could not be saved.", "#E67E22"))

    # ================================================================ exportar
    def exportar(self):
        if self.exporting:
            return
        app = self.app
        self._capturar()
        stadium = self.stadium()
        perfil = self._perfil()
        if not perfil or stadium == "Custom":
            messagebox.showwarning("Export campaigns", "Select a stadium profile first.", parent=app)
            return
        if not self.approved:
            messagebox.showinfo("Export campaigns", "Add the campaigns to export by typing their codes first.",
                                parent=app)
            return
        estados = {c: self.estado(c) for c in self.approved}
        targets = [c for c in self.approved if cb.exportable(estados[c])]
        skipped = [c for c in self.approved if c not in targets]
        if not targets:
            messagebox.showwarning("Export campaigns", "None of the campaigns can be rendered yet.\n\n" +
                                   self._resumen_saltadas(skipped, estados), parent=app)
            return
        sin_revisar = [c for c in targets if estados[c] != cb.S_REVIEWED]
        texto = f"Export {len(targets)} campaign(s) for {stadium}?\n\nEach one: System File + architecture JSON + 3D preview video."
        if sin_revisar:
            texto += (f"\n\n{len(sin_revisar)} campaign(s) have not been reviewed and will use their current "
                      f"(automatic or edited) architecture:\n" + "\n".join("   " + c for c in sin_revisar[:15]) +
                      ("\n   …" if len(sin_revisar) > 15 else ""))
        if skipped:
            texto += "\n\nSkipped:\n" + self._resumen_saltadas(skipped, estados)
        if not messagebox.askyesno("Export campaigns", texto, parent=app):
            return
        out_root = app.output_path_var.get()
        defaults = app.ui_defaults_campana()
        self.exporting = True
        self.btn_export.configure(state="disabled", text="Preparing…")
        threading.Thread(target=self._export_worker, args=(targets, stadium, perfil, out_root, defaults),
                         daemon=True).start()

    def _resumen_saltadas(self, codes, estados):
        return "\n".join(f"   {c}: {cb.ETIQUETAS[estados[c]]}" for c in codes[:20]) + ("\n   …" if len(codes) > 20 else "")

    def _export_worker(self, targets, stadium, perfil, out_root, defaults):
        trabajos, errores, sin_guardar = [], [], []
        for i, code in enumerate(targets, 1):
            self.app.after(0, lambda i=i, c=code: self.btn_export.configure(text=f"Preparing {i}/{len(targets)}…"))
            st = self._state(code, stadium)
            try:
                if st.config is None:
                    st.config, st.solver_msg = config_campana(perfil, stadium, st.files, defaults)
                doc = self._documento(code, stadium)
                carpeta = os.path.join(out_root, cb.safe_name(stadium), code)
                os.makedirs(carpeta, exist_ok=True)
                json_path = os.path.join(carpeta, cb.nombre_salida(code, stadium, "ARCHIEtoAE", ".json"))
                cb._escribir(json_path, doc)
                path, donde = cb.guardar(doc, self.nas, code, stadium, self.local_dir)
                if path:
                    st.saved_path, st.saved_at = path, doc["archie"]["saved_at"]
                else:
                    sin_guardar.append((code, doc))
                trabajos.append((code, doc, carpeta, json_path))
            except Exception as e:
                log_error(f"[Campañas] export {code}: {e}")
                errores.append(f"{code}: {e}")
        self.app.after(0, lambda: self._export_fin(trabajos, errores, sin_guardar, stadium))

    def _export_fin(self, trabajos, errores, sin_guardar, stadium):
        qm = self.app.queue_manager
        for code, doc, carpeta, json_path in trabajos:
            dur = ""
            info = f"{doc.get('fps', 25)} FPS"
            if doc.get("config", {}).get("amplify"):
                for row in ("1ST_ROW", "2ND_ROW"):
                    qm.add_job("SYSTEM", doc, carpeta, cb.nombre_salida(code, stadium, f"SYS_{row}", ".mp4"),
                               info=info, json_path=json_path, row_target=row)
            else:
                qm.add_job("SYSTEM", doc, carpeta, cb.nombre_salida(code, stadium, "SYS", ".mp4"),
                           info=info, json_path=json_path)
            qm.add_job("PREVIEW3D", doc, carpeta, cb.nombre_salida(code, stadium, "3D_PREVIEW", ".mp4"),
                       info="25 FPS", json_path=json_path)
            self._pintar_fila(code)
        if sin_guardar and not self.local_dir:
            if self._pedir_carpeta_local(f"{len(sin_guardar)} architecture JSON file(s) could not be saved on the NAS."):
                for code, doc in sin_guardar:
                    path, _ = cb.guardar(doc, self.nas, code, stadium, self.local_dir)
                    if path:
                        st = self._state(code, stadium)
                        st.saved_path, st.saved_at = path, doc["archie"]["saved_at"]
        self.exporting = False
        self.btn_export.configure(state="normal", text="Export ready campaigns")
        self._actualizar_cabeceras()
        if errores:
            messagebox.showerror("Export campaigns", "Some campaigns could not be prepared:\n\n" + "\n".join(errores),
                                 parent=self.app)
        if trabajos:
            self._actualizar_info((f"{len(trabajos)} campaign(s) added to the render queue · "
                                   f"{os.path.join(self.app.output_path_var.get(), cb.safe_name(stadium))}", "#27AE60"))
            if not qm.is_rendering:
                qm.start_queue_processor()

    # ================================================================ avisos
    def _dialogo_elements(self, code, info):
        app = self.app
        win = ctk.CTkToplevel(app)
        win.title(f"Campaign {code}")
        win.transient(app)
        win.configure(fg_color=app.c_white)
        win.resizable(False, False)
        ok = info["status"] == ST_READY
        titulo = "Check the campaign files" if info["status"] == "review" else "Campaign Elements not available"
        ctk.CTkLabel(win, text=titulo, font=ctk.CTkFont(family="Montserrat", size=18, weight="bold"),
                     text_color=app.c_magenta).pack(anchor="w", padx=24, pady=(20, 8))
        ctk.CTkLabel(win, text=mensaje(code, info), font=ctk.CTkFont(size=13), text_color=app.c_text_dark,
                     justify="left", wraplength=560, anchor="w").pack(anchor="w", padx=24)
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=24, pady=(16, 20))
        path = info.get("path") or ""
        ctk.CTkButton(bar, text="OK", width=90, fg_color=app.c_magenta, hover_color="#D1007A",
                      command=win.destroy).pack(side="right")
        if path:
            ctk.CTkButton(bar, text="Copy path", width=100, fg_color="#E0E0E0", hover_color="#D0D0D0",
                          text_color=app.c_text_dark, command=lambda: self._copiar(path)).pack(side="right", padx=8)
        if path and os.path.isdir(path):
            ctk.CTkButton(bar, text="Open folder", width=100, fg_color="#2b2b2b", hover_color="#404040",
                          command=lambda: self._abrir_carpeta(path)).pack(side="right")
        win.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - win.winfo_reqwidth()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.after(100, win.lift)
        win.after(150, win.focus_force)

    def _copiar(self, text):
        self.app.clipboard_clear()
        self.app.clipboard_append(text)

    @staticmethod
    def _abrir_carpeta(path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            log_error(f"No se pudo abrir {path}: {e}")
