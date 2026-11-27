"""
campaign_panel.py (v0.58, v0.59)
Pestaña CAMPAIGNS de Media Assets: batch de campañas para el estadio seleccionado.

  Buscador: se escribe el código exacto de la campaña (ADID-RMXX-261001) y Enter / Add. Se pueden pegar varios.
  CAMPAIGN CODES: la lista que se procesa, con su estado. Al hacer clic en una campaña se cargan sus ELEMENTS
  (NAS) y SU arquitectura en Manual Edit y en las tres previews. Cada campaña guarda su propia arquitectura.

v0.94: la columna Status es un botón por campaña: WIP (naranja) <-> READY (verde) al hacer clic; MISSING (rojo)
si no tiene ELEMENTS listos. El aviso de ELEMENTS solo sale al hacer clic en MISSING o en el nombre de esa campaña,
y nunca hay más de uno abierto. Sin Mark as reviewed ni Export ready campaigns: los botones de abajo (Generate AE
Import, Generate SYSTEM FILE, Render Preview) trabajan con la campaña que se está previsualizando y Batch Add to
Queue manda a la cola de render todas las campañas READY (System File + JSON + vídeo 3D de cada una).

v0.97: los ELEMENTS se buscan también en las subcarpetas de Output\\Elements (elements_variants). Si una campaña
tiene versiones (C1, C2... por subcarpeta o nombre) o idiomas (_ANIM_SPANISH / _ANIM_INGLES), al añadirla se
sustituye por una fila por variante: JETX-UEFA-260501_C1, JETX-UEFA-260501_C1_SPANISH... Cada variante tiene su
estado, su arquitectura (<Estadio>_<variante>.json) y sus archivos de salida.
ON THE NAS enseña por defecto solo las campañas con ELEMENTS ("Only with Elements"); al buscar salen todas.
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
from PIL import Image, ImageDraw, ImageFont, ImageTk

import campaign_batch as cb
from campaign_loader import load_campaigns, normalizar_codigo, codigo_valido
from nas_campaigns import NasIndex, mensaje, ST_READY, REQUIRED
from elements_variants import inspeccionar_variantes, ST_FOUND
from auto_solver import config_campana, CONFIG_KEYS
from log_utils import debug, log_error

# v0.94: botón de estado (columna Status) -> (texto, color)
FALTAN = (cb.S_NO_NAS, cb.S_MISSING, cb.S_INCOMPLETE)
PILL_WIP, PILL_READY, PILL_MISSING, PILL_CHECKING = "WIP", "READY", "MISSING", "…"
PILLS = {PILL_WIP: "#E67E22", PILL_READY: "#27AE60", PILL_MISSING: "#C0392B", PILL_CHECKING: "#9A9A9A"}


def pill_de(estado):
    if estado == cb.S_CHECKING:
        return PILL_CHECKING
    if estado in FALTAN:
        return PILL_MISSING
    return PILL_READY if estado == cb.S_REVIEWED else PILL_WIP


def _fuente_pill(px):
    core = os.path.dirname(os.path.abspath(__file__))
    for base in (os.path.dirname(core), os.path.dirname(os.path.dirname(os.path.dirname(core)))):
        for nombre in (os.path.join("fonts", "Figtree-Bold.ttf"), "Montserrat-Bold.ttf"):  # v1.06: Figtree
            cand = os.path.join(base, "Assets", nombre)
            if os.path.isfile(cand):
                try:
                    return ImageFont.truetype(cand, px)
                except Exception:
                    pass
    for cand in ("arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(cand, px)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size=px)
    except TypeError:   # Pillow < 10.1
        return ImageFont.load_default()


def _imagen_pill(texto, color, w, h, ss=4):
    """Píldora redondeada con el texto centrado (se dibuja a 4x y se reduce para suavizar los bordes)."""
    W, H = w * ss, h * ss
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, W - 1, H - 1), radius=H // 2, fill=color)
    font = _fuente_pill(int(H * 0.56))
    x0, y0, x1, y1 = d.textbbox((0, 0), texto, font=font)
    d.text(((W - (x1 - x0)) / 2 - x0, (H - (y1 - y0)) / 2 - y0), texto, font=font, fill="white")
    return img.resize((w, h), Image.LANCZOS)


TREE_ROWS = 7

# v1.02: scrollbars de las listas con los colores del Design System AIM 2026 (escala Magenta / Grey)
SCROLL_TRACK = "#F5F6FB"        # fondo de la barra (gris claro de la app)
SCROLL_THUMB = "#FFB3DF"        # Magenta-300 aprox.
SCROLL_THUMB_HOVER = "#FF099A"  # magenta corporativo


def _scrollbar_aim(parent, app, command):
    """Scrollbar fina y redondeada en magenta, en lugar de la ttk gris de sistema."""
    return ctk.CTkScrollbar(parent, orientation="vertical", command=command, width=12, corner_radius=6,
                            fg_color=SCROLL_TRACK, button_color=SCROLL_THUMB,
                            button_hover_color=SCROLL_THUMB_HOVER, bg_color=app.c_white)
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
        self._drag = None
        self._drag_win = None
        self._dialogo = None          # v0.94: único aviso de ELEMENTS abierto
        self._popup_code = None       # v0.94: campaña cuyo aviso se pidió con un clic (se enseña tras comprobarla)
        self._ready_tras_carga = None  # v0.94: campaña que pasa a READY en cuanto termine de cargarse
        self.local_dir = cb.leer_ajustes(config_dir).get("architecture_dir")
        self._refresco_l = None        # v0.97: refresco pendiente de ON THE NAS mientras se revisan los ELEMENTS

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
        fuente = tkfont.Font(f, family="Figtree", size=9)
        self._fuente = fuente
        alto_fila = fuente.metrics("linespace") + 6
        w_code = fuente.measure("ADID-RMXX-261001") + 18
        # v0.94: píldoras de estado del alto de la fila
        pill_h = max(14, alto_fila - 6)
        fp = _fuente_pill(int(pill_h * 0.56))
        pill_w = max(int(fp.getlength(t)) for t in PILLS) + pill_h
        self._pills = {t: ImageTk.PhotoImage(_imagen_pill(t, c, pill_w, pill_h), master=f) for t, c in PILLS.items()}
        w_status = pill_w + 16
        style.configure("Archie.Treeview", font=fuente, rowheight=alto_fila, background=app.c_white,
                        fieldbackground=app.c_white, foreground=app.c_text_dark, borderwidth=0)
        style.configure("Archie.Treeview.Heading", font=(fuente.actual("family"), 9, "bold"), background=app.c_light_grey,
                        foreground=app.c_text_dark, relief="flat")
        style.map("Archie.Treeview", background=[("selected", app.c_light_magenta)],
                  foreground=[("selected", app.c_text_dark)])
        # v0.94: la columna de árbol (#0) solo enseña la píldora, sin hueco para el desplegable
        style.layout("Archie.Treeview.Item", [("Treeitem.padding", {"sticky": "nswe", "children": [
            ("Treeitem.image", {"side": "left", "sticky": ""}),
            ("Treeitem.text", {"side": "left", "sticky": ""})]})])

        top = ctk.CTkFrame(f, fg_color="transparent")
        top.pack(fill="x", padx=5, pady=(0, 6))
        top.grid_columnconfigure(0, weight=1)
        # v0.60: al escribir se filtran (por el principio del código) las campañas que hay en el NAS.
        # Enter añade el código exacto escrito (o la única coincidencia); se pueden pegar varios códigos.
        self.search_var = ctk.StringVar()
        self.search_var.trace_add("write", lambda *a: self._filtrar())
        self.entry_code = ctk.CTkEntry(top, textvariable=self.search_var, height=28, fg_color=app.c_white,
                                       text_color=app.c_text_dark)
        self.entry_code.grid(row=0, column=0, sticky="ew")
        self.entry_code.bind("<Return>", lambda e: self.agregar_desde_buscador())
        self.entry_code.bind("<Down>", lambda e: self._foco_lista())
        ctk.CTkButton(top, text="Add", width=60, height=28, fg_color="#2b2b2b", hover_color=app.c_magenta,
                      font=app.font_t2, command=self.agregar_desde_buscador).grid(row=0, column=1, padx=(6, 0))
        self.btn_refresh = ctk.CTkButton(top, text="↺", width=28, height=28, fg_color="#2b2b2b",
                                         hover_color=app.c_magenta, font=ctk.CTkFont(size=14, weight="bold"),
                                         command=self.reescanear)
        self.btn_refresh.grid(row=0, column=2, padx=(6, 0))
        self.lbl_hint = tk.Label(self.entry_code, text="🔍 Type a campaign code (e.g. ABNB-LIGA) to find it on the NAS",
                                 fg="#9A9A9A", bg=app.c_white, font=(fuente.actual("family"), 9), anchor="w")
        self.lbl_hint.place(x=10, rely=0.5, anchor="w")
        self.lbl_hint.bind("<Button-1>", lambda e: self.entry_code.focus_set())

        mid = ctk.CTkFrame(f, fg_color="transparent")
        mid.pack(fill="both", expand=True, padx=5)   # v1.02: las listas crecen con el divisor de MEDIA ASSETS
        mid.grid_rowconfigure(0, weight=1)
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(2, weight=1)

        # --- izquierda: campañas en el NAS
        left = ctk.CTkFrame(mid, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew")
        head_l = ctk.CTkFrame(left, fg_color="transparent")
        head_l.pack(fill="x")
        self.lbl_left = ctk.CTkLabel(head_l, text="ON THE NAS", font=app.font_t2, text_color=app.c_text_dark)
        self.lbl_left.pack(side="left")
        # v0.97: por defecto solo las campañas que ya tienen ELEMENTS
        self.solo_elements = ctk.BooleanVar(value=bool(cb.leer_ajustes(self.config_dir).get("only_with_elements", True)))
        ctk.CTkCheckBox(head_l, text="Only with Elements", variable=self.solo_elements, command=self._toggle_solo,
                        font=ctk.CTkFont(size=11), text_color=app.c_text_dark, fg_color=app.c_magenta,
                        hover_color="#D1007A", checkbox_width=16, checkbox_height=16, border_width=2,
                        width=20).pack(side="right")
        lw = tk.Frame(left, bg=app.c_white, highlightthickness=1, highlightbackground="#E0E0E0")
        lw.pack(fill="both", expand=True)
        self.tree_l = ttk.Treeview(lw, columns=("code", "name"), show="headings", height=TREE_ROWS,
                                   style="Archie.Treeview", selectmode="extended")
        self.tree_l.heading("code", text="Code")
        self.tree_l.heading("name", text="Folder")
        self.tree_l.column("code", width=w_code, minwidth=w_code, stretch=False)
        self.tree_l.column("name", width=90, minwidth=40, stretch=True)
        sl = _scrollbar_aim(lw, app, self.tree_l.yview)   # v1.02: scrollbar corporativa (Design System AIM)
        self.tree_l.configure(yscrollcommand=sl.set)
        self.tree_l.pack(side="left", fill="both", expand=True)
        sl.pack(side="right", fill="y")
        self.tree_l.tag_configure("added", foreground="#B0B0B0")
        self.tree_l.tag_configure("noelem", foreground="#C98A8A")
        self.lbl_left_hint = tk.Label(lw, text="", fg="#A0A0A0", bg=app.c_white, font=(fuente.actual("family"), 9),
                                      wraplength=220, justify="center")

        # --- centro: botones
        cen = ctk.CTkFrame(mid, fg_color="transparent", width=34)
        cen.grid(row=0, column=1, sticky="ns", padx=4)
        ctk.CTkButton(cen, text="→", width=30, height=28, fg_color="#2b2b2b", hover_color=app.c_magenta,
                      command=lambda: self.agregar(self._sel(self.tree_l))).pack(pady=(50, 4))
        ctk.CTkButton(cen, text="←", width=30, height=28, fg_color="#E0E0E0", hover_color="#D0D0D0",
                      text_color=app.c_text_dark, command=lambda: self.quitar(self._sel(self.tree_r))).pack()

        # --- derecha: CAMPAIGN CODES (la lista que se procesa)
        right = ctk.CTkFrame(mid, fg_color="transparent")
        right.grid(row=0, column=2, sticky="nsew")
        self.lbl_right = ctk.CTkLabel(right, text="CAMPAIGN CODES", font=app.font_t2, text_color=app.c_magenta)
        self.lbl_right.pack(anchor="w")
        rw = tk.Frame(right, bg=app.c_white, highlightthickness=2, highlightbackground="#E0E0E0",
                      highlightcolor="#E0E0E0")
        rw.pack(fill="both", expand=True)
        self.right_wrap = rw
        self.tree_r = ttk.Treeview(rw, columns=("code",), show=("tree", "headings"), height=TREE_ROWS,
                                   style="Archie.Treeview", selectmode="extended")
        self.tree_r.heading("#0", text="Status")
        self.tree_r.heading("code", text="Code")
        self.tree_r.column("#0", width=w_status, minwidth=w_status, stretch=False, anchor="center")
        self.tree_r.column("code", width=w_code, minwidth=w_code, stretch=True)
        sr = _scrollbar_aim(rw, app, self.tree_r.yview)   # v1.02: scrollbar corporativa (Design System AIM)
        self.tree_r.configure(yscrollcommand=sr.set)
        self.tree_r.pack(side="left", fill="both", expand=True)
        sr.pack(side="right", fill="y")
        self.tree_r.tag_configure("active", background="#FDEAF4")
        self.lbl_drop_hint = tk.Label(rw, text="Drag campaigns here", fg="#A0A0A0",
                                      bg=app.c_white, font=(fuente.actual("family"), 9))
        self.lbl_drop_hint.place(relx=0.5, rely=0.55, anchor="center")

        # --- abajo: estado (v0.94: sin Mark as reviewed / Export ready campaigns ni el aviso de los JSON)
        self.lbl_info = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=11), text_color=app.c_text_muted,
                                     anchor="w", justify="left", wraplength=640)
        self.lbl_info.pack(fill="x", padx=5, pady=(4, 0))

        # --- eventos
        tl, tr = self.tree_l, self.tree_r
        tl.bind("<ButtonPress-1>", self._drag_press, add="+")
        tl.bind("<B1-Motion>", self._drag_motion, add="+")
        tl.bind("<ButtonRelease-1>", self._drag_release, add="+")
        tl.bind("<Double-1>", lambda e: self.agregar(self._sel(tl)))
        tl.bind("<Return>", lambda e: self.agregar(self._sel(tl)))
        tl.bind("<Button-3>", self._menu_left)
        # v0.94: clic en la píldora = WIP <-> READY (o aviso si es MISSING); clic en el código = previsualizar
        tr.bind("<ButtonRelease-1>", self._click_right, add="+")
        tr.bind("<KeyRelease-Up>", lambda e: self._on_select_right())
        tr.bind("<KeyRelease-Down>", lambda e: self._on_select_right())
        tr.bind("<Return>", lambda e: self._on_select_right(popup=True))
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
        # v0.60: se usa el índice en caché al momento y en cada arranque se reescanea Brands en segundo plano
        self.app.after(0, self._filtrar)
        ok = self.nas.escanear()
        self.app.after(0, self._fin_escaneo)
        if ok:
            self._revisar_elements()

    def reescanear(self):
        self.btn_refresh.configure(state="disabled", text="…")
        self.lbl_left.configure(text="ON THE NAS (scanning…)")

        def worker():
            ok = self.nas.escanear()
            self.app.after(0, self._fin_escaneo)
            self.app.after(0, self._recomprobar_todas)
            if ok:
                self._revisar_elements()
        threading.Thread(target=worker, daemon=True).start()

    def _revisar_elements(self):
        """v0.97: qué campañas del NAS tienen ELEMENTS (en el hilo del escaneo; la lista se refresca a ratos)."""
        try:
            self.nas.revisar_elements(progreso=lambda h, t: self.app.after(0, self._filtrar_pronto))
        except Exception as e:
            log_error(f"[Campañas] revisando ELEMENTS del NAS: {e}")
        self.app.after(0, self._filtrar)

    def _filtrar_pronto(self):
        if self._refresco_l is None:
            self._refresco_l = self.app.after(300, self._filtrar)

    def _toggle_solo(self):
        cb.guardar_ajuste(self.config_dir, "only_with_elements", bool(self.solo_elements.get()))
        self._filtrar()

    def _fin_escaneo(self):
        self.btn_refresh.configure(state="normal", text="↺")
        self._filtrar()

    def _consulta(self):
        return normalizar_codigo(self.search_var.get()).strip("-")

    def _filtrar(self):
        q = self._consulta()
        self.lbl_hint.place_forget() if self.search_var.get() else self.lbl_hint.place(x=10, rely=0.5, anchor="w")
        self._refresco_l = None
        tl = self.tree_l
        sel = set(tl.selection())
        tl.delete(*tl.get_children())
        disp = self.nas.disponibles()
        aprobadas = {cb.base_codigo(c) for c in self.approved}
        solo = bool(self.solo_elements.get()) and not q   # v0.97: al buscar se enseñan todas
        n = 0
        for code, titulo in disp:
            if q and not code.startswith(q):
                continue
            tiene = self.nas.tiene_elements(code)
            if solo and not tiene:
                continue
            nombre = titulo or self.by_code.get(code, {}).get("name", "")
            if tiene is False:
                nombre = (nombre + "  · no Elements") if nombre else "no Elements"
            tags = ("added",) if code in aprobadas else (("noelem",) if tiene is False else ())
            tl.insert("", "end", iid=code, values=(code, nombre), tags=tags)
            n += 1
        keep = [c for c in sel if tl.exists(c)]
        if keep:
            tl.selection_set(keep)
        rev = self.nas.revisando
        extra = f" · checking Elements {rev[0]}/{rev[1]}…" if rev else ""
        if q:
            self.lbl_left.configure(text=f"ON THE NAS ({n})")
        elif solo:
            self.lbl_left.configure(text=f"ON THE NAS ({n} with Elements){extra}")
        else:
            self.lbl_left.configure(text=f"ON THE NAS ({len(disp)}){extra}")
        if not disp:
            txt = ("Scanning the NAS…" if self.nas.scanned_at is None else
                   "No campaign folders found on the NAS.\nPress ↺ to scan again.")
        elif not n and q:
            txt = f"No campaign on the NAS starts with {q}."
        elif not n:
            txt = ("Checking which campaigns have Elements…" if rev or self.nas.elements == {} else
                   "No campaign on the NAS has Elements yet.\nUntick 'Only with Elements' to see all of them.")
        else:
            txt = ""
        if txt:
            self.lbl_left_hint.configure(text=txt)
            self.lbl_left_hint.place(relx=0.5, rely=0.55, anchor="center")
        else:
            self.lbl_left_hint.place_forget()

    def _foco_lista(self):
        hijos = self.tree_l.get_children()
        if hijos:
            self.tree_l.focus_set()
            self.tree_l.selection_set(hijos[0])
            self.tree_l.focus(hijos[0])

    def _infos(self, code, res):
        """v0.97: resultado de inspeccionar_variantes -> {fila: info como nas.inspeccionar}."""
        base = cb.base_codigo(code)
        if res["status"] != ST_FOUND or not res["variants"]:
            return {code: {"status": res["status"], "files": None, "missing": list(REQUIRED), "ambiguous": [],
                           "path": res.get("path") or self.nas.ruta_esperada(base)}}
        out = {}
        for v in res["variants"]:
            key = base + ("_" + re.sub(r"[^A-Za-z0-9]+", "_", v["key"]).strip("_").upper() if v["key"] else "")
            out[key] = {"status": v["status"], "files": v["files"], "missing": v["missing"],
                        "ambiguous": v["ambiguous"], "path": v["dir"]}
        return out

    def _comprobar(self, code):
        stadium = self.stadium()
        base = cb.base_codigo(code)

        def worker():
            try:
                res = inspeccionar_variantes(self.nas, base, idiomas=True)
            except Exception as e:
                log_error(f"[Campañas] {code}: {e}")
                res = {"status": "not_found", "path": self.nas.ruta_esperada(base), "variants": []}
            infos = self._infos(code, res)
            self.nas.marcar_elements(base, sum(1 for i in infos.values() if i.get("files")))
            docs = {}
            for key in infos:
                try:
                    docs[key] = cb.cargar(self.nas, key, stadium, self.local_dir)
                except Exception as e:
                    log_error(f"[Campañas] leyendo arquitectura de {key}: {e}")
            self.app.after(0, lambda: self._on_comprobada(code, stadium, infos, docs))
        threading.Thread(target=worker, daemon=True).start()

    def _on_comprobada(self, code, stadium, infos, docs):
        if code not in self.approved:
            return
        keys = list(infos)
        if code == cb.base_codigo(code) and keys != [code]:
            # v0.97: la campaña tiene variantes: su fila se sustituye por una por variante
            pos = self.approved.index(code)
            if code == self.active:
                self.active = None
            self.approved.remove(code)
            self.tree_r.delete(code)
            for key in keys:
                if key in self.approved:
                    continue
                self.approved.insert(pos, key)
                self.tree_r.insert("", pos, iid=key, text="", values=(key,))
                pos += 1
            for attr in ("pending_activate", "_popup_code", "_ready_tras_carga"):
                if getattr(self, attr) == code:
                    setattr(self, attr, keys[0])
            self._actualizar_cabeceras()
        elif code not in infos:
            # variante que ya no está en el NAS
            infos = {code: {"status": "not_found", "files": None, "missing": list(REQUIRED), "ambiguous": [],
                            "path": self.nas.elements_dir(cb.base_codigo(code)) or ""}}
            keys = [code]
        for key in keys:
            if key not in self.approved:
                continue
            info = infos[key]
            self.elements[key] = info
            st = self._state(key, stadium)
            doc, path = docs.get(key) or (None, None)
            if doc and st.config is None and not st.loaded_from_disk:
                cb.aplicar_documento(st, doc, path, self.nas.root)
                debug(f"[Campañas] {key} @ {stadium}: arquitectura guardada cargada de {path}")
            if not st.files_manual and info.get("files"):
                st.files = copy.deepcopy(info["files"])
            self._pintar_fila(key)
        self._actualizar_cabeceras()
        pend = self.pending_activate
        if pend in keys and stadium == self.stadium():
            self.pending_activate = None
            self.tree_r.selection_set(pend)
            self.activar(pend)

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
        self.tree_r.item(code, text="", image=self._pills[pill_de(st)], values=(code,), tags=tags)
        if code == self.active:
            self._actualizar_info()

    def _actualizar_cabeceras(self):
        n = len(self.approved)
        ready = sum(1 for c in self.approved if self.estado(c) == cb.S_REVIEWED)
        self.lbl_right.configure(text=f"CAMPAIGN CODES ({n})" + (f" · {ready} READY" if n else ""))
        if n:
            self.lbl_drop_hint.place_forget()
        else:
            self.lbl_drop_hint.place(relx=0.5, rely=0.55, anchor="center")

    def _actualizar_info(self, extra=None):
        if extra is not None:
            self.lbl_info.configure(text=extra[0], text_color=extra[1])
            return
        code = self.active
        if not code:
            self.lbl_info.configure(text="Click a campaign in CAMPAIGN CODES to preview it. Click WIP to mark it READY.",
                                    text_color=self.app.c_text_muted)
            return
        name = self.by_code.get(cb.base_codigo(code), {}).get("name", "")
        estado = self.estado(code)
        pill = pill_de(estado)
        txt = f"Previewing {code}" + (f" · {name}" if name else "") + f" · {pill}"
        if estado == cb.S_REVIEW_FILES:
            info = self.elements.get(code) or {}
            txt += "\n" + mensaje(code, info).split("\n")[0] + " Check the Elements, then click WIP to mark it READY."
        self.lbl_info.configure(text=txt, text_color=PILLS[pill] if pill != PILL_CHECKING else self.app.c_text_muted)

    # ================================================================ añadir / quitar
    def agregar_desde_buscador(self):
        if not self.app.estadio_listo():   # v1.11: primero el estadio
            return
        texto = self.entry_code.get()
        tokens = [normalizar_codigo(t) for t in re.split(r"[\s,;]+", texto) if t.strip()]
        if not tokens:
            return
        if len(tokens) == 1 and not codigo_valido(tokens[0]):
            # código incompleto: si solo hay una campaña del NAS que empieza así, se añade esa
            hijos = self.tree_l.get_children()
            if len(hijos) == 1:
                tokens = [hijos[0]]
            elif hijos:
                self._actualizar_info((f"{len(hijos)} campaigns on the NAS start with {self._consulta()}. "
                                       "Pick one in the list (double-click or drag it).", "#E67E22"))
                self._foco_lista()
                return
        malos = [t for t in tokens if not codigo_valido(t)]
        buenos = [t for t in tokens if codigo_valido(t)]
        en_lista = {cb.base_codigo(c): c for c in reversed(self.approved)}
        ya = [en_lista[t] for t in buenos if t in en_lista]
        nuevos = self.agregar(buenos)
        if malos:
            self._actualizar_info((f"Invalid code format: {', '.join(malos)}. "
                                   "Use the exact code BRND-CHNL-NNNNNN (e.g. ADID-RMXX-261001).", "#C0392B"))
            self._set_buscador(" ".join(malos))
        else:
            self._set_buscador("")
            if ya and not nuevos:
                self.tree_r.selection_set(ya[0])
                self.tree_r.see(ya[0])
                self.activar(ya[0])
                self._actualizar_info((f"{', '.join(ya)} is already in the list.", "#E67E22"))

    def _set_buscador(self, texto):
        self.entry_code.delete(0, "end")
        if texto:
            self.entry_code.insert(0, texto)
        else:
            self.app.focus_set()      # vuelve a mostrar el texto de ayuda
            self.entry_code.focus_set()

    def _marcar_izquierda(self, code, added):
        code = cb.base_codigo(code)
        if self.tree_l.exists(code):
            tiene = self.nas.tiene_elements(code)
            self.tree_l.item(code, tags=("added",) if added else (("noelem",) if tiene is False else ()))

    def agregar(self, codes):
        if not self.app.estadio_listo():   # v1.11: primero el estadio
            return
        nuevos = []
        for code in codes:
            # v0.97: volver a añadir una campaña con variantes recupera las que se hubieran quitado
            if code in self.approved or code in nuevos:
                continue
            nuevos.append(code)
            self.approved.append(code)
            self.tree_r.insert("", "end", iid=code, text="", values=(code,))
            self._marcar_izquierda(code, True)
            self._pintar_fila(code)
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
                self.activar(nuevos[0])   # v0.94: sin aviso; si le faltan ELEMENTS queda en MISSING
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
            base = cb.base_codigo(code)
            if not any(cb.base_codigo(c) == base for c in self.approved):
                self._marcar_izquierda(base, False)
        self._actualizar_cabeceras()

    def _menu_left(self, e):
        """v0.97: ruta de los ELEMENTS de cualquier campaña del NAS, para comprobarla a mano."""
        item = self.tree_l.identify_row(e.y)
        if not item:
            return
        if item not in self.tree_l.selection():
            self.tree_l.selection_set(item)
        path = self.nas.elements_dir(item) or self.nas.carpeta(item) or ""
        m = tk.Menu(self.tree_l, tearoff=0)
        m.add_command(label="Add to CAMPAIGN CODES", command=lambda: self.agregar(self._sel(self.tree_l)))
        if path:
            m.add_command(label="Copy Elements path", command=lambda: self._copiar(path))
            m.add_command(label="Open Elements folder", command=lambda: self._abrir_carpeta(
                path if os.path.isdir(path) else (self.nas.carpeta(item) or path)))
        m.tk_popup(e.x_root, e.y_root)

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

    # ================================================================ arrastrar
    def _drag_press(self, e):
        item = self.tree_l.identify_row(e.y)
        self._drag = {"x": e.x_root, "y": e.y_root, "item": item, "moved": False}
        if item and item in self.tree_l.selection() and len(self.tree_l.selection()) > 1 and not (e.state & 0x0005):
            return "break"   # mantener la selección múltiple para arrastrarla entera

    def _drag_motion(self, e):
        d = self._drag
        if not d or not d["item"]:
            return
        if not d["moved"] and abs(e.x_root - d["x"]) + abs(e.y_root - d["y"]) < 6:
            return
        d["moved"] = True
        codes = self._sel(self.tree_l) or [d["item"]]
        if self._drag_win is None:
            w = tk.Toplevel(self.tree_l)
            w.overrideredirect(True)
            try:
                w.attributes("-topmost", True)
            except tk.TclError:
                pass
            txt = codes[0] + (f"  (+{len(codes) - 1})" if len(codes) > 1 else "")
            tk.Label(w, text=txt, bg=self.app.c_magenta, fg="white", font=("Figtree", 10, "bold"),
                     padx=8, pady=3).pack()
            self._drag_win = w
        self._drag_win.geometry(f"+{e.x_root + 12}+{e.y_root + 8}")
        sobre = self._sobre_derecha(e.x_root, e.y_root)
        self.right_wrap.configure(highlightbackground=self.app.c_magenta if sobre else self.app.c_light_grey)

    def _drag_release(self, e):
        d, self._drag = self._drag, None
        if self._drag_win is not None:
            self._drag_win.destroy()
            self._drag_win = None
        self.right_wrap.configure(highlightbackground=self.app.c_light_grey)
        if not d or not d["item"]:
            return
        if not d["moved"]:
            if len(self.tree_l.selection()) > 1 and not (e.state & 0x0005):
                self.tree_l.selection_set(d["item"])
            return
        if self._sobre_derecha(e.x_root, e.y_root):
            self.agregar(self._sel(self.tree_l) or [d["item"]])

    def _sobre_derecha(self, x, y):
        w = self.right_wrap.winfo_containing(x, y)
        while w is not None:
            if w is self.right_wrap:
                return True
            w = getattr(w, "master", None)
        return False

    # ================================================================ activar campaña
    def _on_select_right(self, popup=False):
        sel = self._sel(self.tree_r)
        if len(sel) != 1:
            return
        code = sel[0]
        if code != self.active:
            self.activar(code, popup=popup)

    def _click_right(self, e):
        """v0.94: clic en la píldora de Status o en el código de una campaña de CAMPAIGN CODES."""
        if e.state & 0x0005:          # Shift / Ctrl: solo selección múltiple (para quitar varias)
            return
        tr = self.tree_r
        if tr.identify_region(e.x, e.y) not in ("tree", "cell"):
            return
        code = tr.identify_row(e.y)
        if not code:
            return
        en_pill = tr.identify_column(e.x) == "#0"
        self.app.after_idle(lambda: self._click_estado(code) if en_pill else self._click_codigo(code))

    def _click_codigo(self, code):
        if not self.tree_r.exists(code):
            return
        if code != self.active or self.estado(code) in FALTAN:
            self.activar(code, popup=True)

    def _click_estado(self, code):
        """WIP -> READY -> WIP. MISSING abre el aviso de ELEMENTS."""
        if not self.tree_r.exists(code) or self.exporting:
            return
        estado = self.estado(code)
        if estado == cb.S_CHECKING:
            return
        if estado in FALTAN:
            self.activar(code, popup=True)
            return
        st = self._state(code)
        if estado == cb.S_REVIEWED:
            st.reviewed = False
            self._ready_tras_carga = None
            self._guardar_silencioso(code)
            self._pintar_fila(code)
            self._actualizar_cabeceras()
            return
        if code == self.active and not self.loading and not self.app.solver_busy:
            self._capturar()
            if st.config:
                self._marcar_ready(code)
            else:
                self._actualizar_info((f"{code}: create the architecture before marking it READY.", "#E67E22"))
            return
        # Todavía no está cargada: se carga (previsualiza) y pasa a READY al terminar
        self._ready_tras_carga = code
        if code != self.active:
            self.activar(code)

    def activar(self, code, popup=False):
        app = self.app
        if not self.app.estadio_listo():   # v1.11: primero el estadio
            return
        if self.exporting:
            return
        if popup:
            self._popup_code = code
        elif self._popup_code != code:
            self._popup_code = None
        if self._ready_tras_carga and self._ready_tras_carga != code:
            self._ready_tras_carga = None
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
        if estado in FALTAN and code not in self._rechecked:
            # Puede que el estudio ya haya subido los Elements: se vuelve a mirar el NAS antes de avisar
            self._rechecked.add(code)
            self.elements.pop(code, None)
            self.pending_activate = code
            self._pintar_fila(code)
            self._actualizar_info((f"Checking Elements of {code} on the NAS…", self.app.c_text_muted))
            self._comprobar(code)
            return
        self._rechecked.discard(code)
        if estado in FALTAN:
            anterior = self.active
            self.active = None
            if anterior:
                self._pintar_fila(anterior)
            self._ready_tras_carga = None
            self._actualizar_info((f"{code}: MISSING · " + mensaje(code, info).split("\n")[0], "#C0392B"))
            if self._popup_code == code:    # v0.94: el aviso solo sale si se hizo clic en la campaña
                self._dialogo_elements(code, info)
            self._popup_code = None
            return
        self._popup_code = None

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
        self._actualizar_info()   # v0.94: "Check files" se explica aquí, sin ventana

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
        if self._ready_tras_carga == code and st.config:
            self._marcar_ready(code)
        self._ready_tras_carga = None
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
        return cb.documento(datos, st, self.by_code.get(cb.base_codigo(code)), stadium, self.app.current_version_str)

    def _pedir_carpeta_local(self, motivo):
        messagebox.showinfo(
            "Save architecture",
            f"{motivo}\n\nArchie cannot write to the campaign folder on the NAS (read-only access). "
            "Please choose a local folder to save the architecture JSON files.", parent=self.app)
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

    def _guardar_silencioso(self, code):
        """Guarda el JSON (NAS o carpeta local ya elegida) sin preguntar nada."""
        try:
            doc = self._documento(code)
            path, _ = cb.guardar(doc, self.nas, code, self.stadium(), self.local_dir)
            if path:
                st = self._state(code)
                st.saved_path, st.saved_at = path, doc["archie"]["saved_at"]
        except Exception as e:
            log_error(f"[Campañas] guardando {code}: {e}")

    def _marcar_ready(self, code):
        """v0.94: la campaña pasa a READY (sustituye a Mark as reviewed) y se guarda su JSON de arquitectura."""
        st = self._state(code)
        if not st.config:
            return
        if self.estado(code) == cb.S_REVIEW_FILES:
            st.files_manual = True   # el PM da por buenos los ELEMENTS elegidos
        st.reviewed = True
        try:
            path, _ = self._guardar(code)
        except Exception as e:
            log_error(f"[Campañas] guardando {code}: {e}")
            path = None
        self._pintar_fila(code)
        self._actualizar_cabeceras()
        if self.estado(code) != cb.S_REVIEWED:
            self._actualizar_info()
        elif path:
            self._actualizar_info((f"✔ {code} READY · architecture saved: {path}", "#27AE60"))
        else:
            self._actualizar_info((f"✔ {code} READY (the architecture JSON could not be saved).", "#E67E22"))

    # ================================================================ exportar
    def batch_add_queue(self):
        """v0.94 (Batch Add to Queue en la pestaña CAMPAIGNS): todas las campañas READY a la cola de render."""
        app = self.app
        titulo = "Batch Add to Queue"
        if self.exporting:
            self._actualizar_info(("Campaigns are already being added to the render queue…", app.c_text_muted))
            return
        self._capturar()
        stadium = self.stadium()
        perfil = self._perfil()
        if not perfil or stadium == "Custom":
            messagebox.showwarning(titulo, "Select a stadium profile first.", parent=app)
            return
        estados = {c: self.estado(c) for c in self.approved}
        targets = [c for c in self.approved if estados[c] == cb.S_REVIEWED]
        no_ready = [c for c in self.approved if c not in targets]
        if not targets:
            messagebox.showwarning(titulo, "No campaign is READY yet. Click WIP next to a campaign to mark it READY."
                                   "\n\n" + self._resumen_saltadas(no_ready, estados), parent=app)
            return
        if no_ready:
            texto = (f"{len(no_ready)} campaign(s) are not READY and will NOT be added to the render queue:\n\n" +
                     self._resumen_saltadas(no_ready, estados) +
                     f"\n\n{len(targets)} READY campaign(s) will be added for {stadium}.")
            if not messagebox.askokcancel(titulo, texto, icon="warning", parent=app):
                return
        out_root = app.output_path_var.get()
        defaults = app.ui_defaults_campana()
        self.exporting = True
        self._actualizar_info((f"Preparing {len(targets)} READY campaign(s)…", app.c_text_muted))
        threading.Thread(target=self._export_worker, args=(targets, stadium, perfil, out_root, defaults),
                         daemon=True).start()

    def _resumen_saltadas(self, codes, estados):
        return "\n".join(f"   {c}: {pill_de(estados[c])}" for c in codes[:20]) + ("\n   …" if len(codes) > 20 else "")

    def _export_worker(self, targets, stadium, perfil, out_root, defaults):
        trabajos, errores, sin_guardar = [], [], []
        for i, code in enumerate(targets, 1):
            self.app.after(0, lambda i=i, c=code: self._actualizar_info(
                (f"Preparing {c} ({i}/{len(targets)})…", self.app.c_text_muted)))
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
        self._actualizar_cabeceras()
        self._actualizar_info()
        if errores:
            messagebox.showerror("Batch Add to Queue", "Some campaigns could not be prepared:\n\n" + "\n".join(errores),
                                 parent=self.app)
        if trabajos:
            self._actualizar_info((f"{len(trabajos)} campaign(s) added to the render queue · "
                                   f"{os.path.join(self.app.output_path_var.get(), cb.safe_name(stadium))}", "#27AE60"))
            if not qm.is_rendering:
                qm.start_queue_processor()

    # ================================================================ avisos
    def _dialogo_elements(self, code, info):
        app = self.app
        # v0.94: un solo aviso a la vez (el anterior se cierra)
        if self._dialogo is not None:
            try:
                if self._dialogo.winfo_exists():
                    self._dialogo.destroy()
            except tk.TclError:
                pass
            self._dialogo = None
        win = ctk.CTkToplevel(app)
        self._dialogo = win
        win.title(f"Campaign {code}")
        win.transient(app)
        win.configure(fg_color=app.c_white)
        win.resizable(False, False)
        ok = info["status"] == ST_READY
        titulo = "Check the campaign files" if info["status"] == "review" else "Campaign Elements not available"
        ctk.CTkLabel(win, text=titulo, font=ctk.CTkFont(family="Figtree", size=18, weight="bold"),
                     text_color=app.c_magenta).pack(anchor="w", padx=24, pady=(20, 8))
        ctk.CTkLabel(win, text=mensaje(code, info), font=ctk.CTkFont(size=13), text_color=app.c_text_dark,
                     justify="left", wraplength=560, anchor="w").pack(anchor="w", padx=24)
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=24, pady=(16, 20))
        path = info.get("path") or ""
        if path:
            # v0.97: la ruta se puede seleccionar y copiar (Ctrl+C) además del botón
            e_path = ctk.CTkEntry(win, height=30, font=ctk.CTkFont(size=12), fg_color=app.c_light_grey,
                                  text_color=app.c_text_dark, border_width=0)
            e_path.insert(0, path)
            e_path.configure(state="readonly")
            e_path.pack(fill="x", padx=24, pady=(12, 0), before=bar)
        ctk.CTkButton(bar, text="OK", width=90, fg_color=app.c_magenta, hover_color="#D1007A",
                      command=win.destroy).pack(side="right")
        if path:
            btn_copy = ctk.CTkButton(bar, text="Copy path", width=100, fg_color="#E0E0E0", hover_color="#D0D0D0",
                                     text_color=app.c_text_dark)
            btn_copy.configure(command=lambda: (self._copiar(path), btn_copy.configure(text="Copied ✓"),
                                                win.after(1500, lambda: btn_copy.winfo_exists() and
                                                          btn_copy.configure(text="Copy path"))))
            btn_copy.pack(side="right", padx=8)
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
        self.app.update()   # v0.97: en Windows el portapapeles no se fija hasta que Tk procesa el evento

    @staticmethod
    def _abrir_carpeta(path):
        try:
            if os.name == "nt":
                os.startfile(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:
            log_error(f"No se pudo abrir {path}: {e}")
