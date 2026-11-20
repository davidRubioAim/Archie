"""
v0.93: IMPORT JSON. Abre un JSON exportado por Archie (Generate AE Import, cola/render o export de CAMPAIGNS)
y restaura la arquitectura aprobada: estadio, ELEMENTS, sliders y la posición exacta de cada ANIM ZONE y logo.

Mixin de MegaStripApp. Pasos:
  1. Estadio: archie.stadium del JSON (v0.93+ y campañas) > nombre del archivo > tamaño del strip > el actual.
  2. ELEMENTS: archie.files o archivos_fuente (las dos IPs del NAS se reescriben con la que responde ahora).
  3. Sliders: config del JSON (+ archie.config, que trae el margin). Sin margin (JSON antiguos) se deduce.
  4. Posiciones: si el motor reproduce el JSON tal cual, queda en automático (sliders libres).
     Si no (el JSON se hizo en MODO MANUAL o el motor ha cambiado), se carga como MODO MANUAL con las
     posiciones del JSON, listo para mover un elemento concreto.
"""
import json
import os
import time
import traceback

import customtkinter as ctk
from tkinter import messagebox

from log_utils import debug, log_error
from manual_layout import ManualLayout
from motor_distribucion import calcular_layout_data
from nas_campaigns import safe_name
import campaign_batch as cb

MARGIN_MAX = 600          # búsqueda del margin en JSON antiguos (rango del slider)
MARGIN_TIMEOUT_S = 4.0


# ---------------------------------------------------------------- utilidades sin interfaz
def firma_elementos(datos):
    """Geometría comparable de una arquitectura: [(tipo, x, ancho)] ordenada."""
    out = []
    for z in (datos or {}).get("zonas", {}).values():
        for e in z.get("elementos", []):
            out.append((e.get("tipo"), int(round(float(e.get("x_absoluta", 0)))), int(round(float(e.get("width", 0))))))
    return sorted(out, key=lambda t: (t[1], t[0], t[2]))


def dims_json(doc):
    z = doc.get("zonas") or {}
    d = doc.get("dimensiones_globales") or {}
    return (int(d.get("width", 0)), int(d.get("height", 0)),
            int((z.get("ZONA_1") or {}).get("width", 0)), int((z.get("ZONA_3") or {}).get("width", 0)))


def _int(v):
    try:
        return int(float(str(v).replace(",", ".")))
    except (TypeError, ValueError):
        return 0


def dims_perfil(perfil):
    return (_int(perfil.get("Width")), _int(perfil.get("Height")),
            _int(perfil.get("Goal Line Size")), _int(perfil.get("Center Board Width")))


def leer_json(path):
    """-> doc o lanza ValueError con un mensaje en inglés para la interfaz."""
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or "zonas" not in doc or "dimensiones_globales" not in doc:
        raise ValueError("This JSON is not an Archie architecture (no 'zonas' / 'dimensiones_globales').")
    return doc


def config_json(doc):
    """config del JSON con archie.config por encima (trae margin y es la que se guardó por campaña)."""
    cfg = dict(doc.get("config") or {})
    archie = doc.get("archie") if isinstance(doc.get("archie"), dict) else {}
    cfg.update(archie.get("config") or {})
    return cfg


def files_json(doc, root_actual=None):
    archie = doc.get("archie") if isinstance(doc.get("archie"), dict) else {}
    src = archie.get("files") or doc.get("archivos_fuente") or {}
    files = {}
    for k in ("background", "center_board", "static_logo"):
        files[k] = cb.remapear_raiz(src.get(k), root_actual) if src.get(k) else None
    anims = [a for a in (src.get("anim_zones") or []) if a] or ([src["anim_zone"]] if src.get("anim_zone") else [])
    files["anim_zones"] = [cb.remapear_raiz(a, root_actual) for a in anims]
    return files


def estadio_json(doc, path, stadium_data, actual=None):
    """-> (nombre, cómo se ha encontrado) o (None, motivo)."""
    archie = doc.get("archie") if isinstance(doc.get("archie"), dict) else {}
    nombre = archie.get("stadium")
    if nombre and nombre in stadium_data:
        return nombre, "from the JSON"
    # Nombre de archivo: <base>_<Estadio_con_guiones_bajos>_ARCHIEtoAE_NN.json (o de campaña)
    fn = os.path.basename(path)
    candidatos = []
    for s in stadium_data:
        if s in ("Custom", "No matches found"):
            continue
        for variante in {s.replace(" ", "_"), safe_name(s).replace(" ", "_")}:
            if variante and variante in fn:
                candidatos.append((len(variante), s))
    if candidatos:
        return max(candidatos)[1], "from the file name"
    # Tamaño del strip (Width / Height / Goal Line / Center Board)
    dj = dims_json(doc)
    iguales = [s for s, p in stadium_data.items() if s != "Custom" and isinstance(p, dict) and dims_perfil(p) == dj]
    if actual in iguales:
        return actual, "same size as the current stadium"
    if len(iguales) == 1:
        return iguales[0], "matched by strip size"
    if nombre:
        return None, f"stadium '{nombre}' is not in the library"
    return None, "stadium not found in the JSON"


def _margin_tl(cfg):
    try:
        m = int(float(cfg.get("margin_tl")))
    except (TypeError, ValueError):
        return 20
    return m if 0 <= m <= MARGIN_MAX else 20


def deducir_margin(doc, w, h, gl, cb_w, archivos, conf, preferidos=()):
    """JSON sin margin (antes de v0.93): el que hace que el motor reproduzca el JSON; si ninguno, preferidos[0]."""
    objetivo = firma_elementos(doc)
    vistos, t0 = set(), time.time()
    for m in list(preferidos) + list(range(0, MARGIN_MAX + 1)):
        m = int(m)
        if m in vistos:
            continue
        vistos.add(m)
        if time.time() - t0 > MARGIN_TIMEOUT_S:
            break
        try:
            d = calcular_layout_data(w, h, gl, cb_w, m, archivos, dict(conf))
        except Exception:
            continue
        if d and firma_elementos(d) == objetivo:
            return m, True
    return (int(preferidos[0]) if preferidos else 20), False   # p. ej. el margin de la Touchline del JSON


# ---------------------------------------------------------------- interfaz
class JsonImportMixin:
    def importar_json(self, path=None):
        if getattr(self, "solver_busy", False):
            self.append_chat("Archie", "📂 Wait until the Auto-Solver finishes, then import the JSON.")
            return
        if not path:
            inicial = self.output_path_var.get() if os.path.isdir(self.output_path_var.get() or "") else None
            path = ctk.filedialog.askopenfilename(
                title="Import an Archie JSON (approved architecture)", initialdir=inicial,
                filetypes=[("Archie JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            doc = leer_json(path)
        except Exception as e:
            log_error(f"Import JSON {path}: {e}")
            messagebox.showerror("Import JSON", f"Could not read {os.path.basename(path)}:\n\n{e}", parent=self)
            return
        try:
            self._importar_doc(doc, path)
        except Exception as e:
            traceback.print_exc()
            log_error(f"Import JSON {path}: {e}")
            messagebox.showerror("Import JSON", f"Could not import {os.path.basename(path)}:\n\n{e}", parent=self)

    def _importar_doc(self, doc, path):
        nombre = os.path.basename(path)
        avisos = []

        # 0. Fuera de CAMPAIGNS: lo importado no debe pisar la campaña activa
        panel = getattr(self, "campaign_panel", None)
        if panel is not None:
            panel.before_stadium_change()   # guarda la campaña activa y la suelta
        if self.media_mode.get() != "FILES":
            self.media_mode.set("FILES")
            self.on_media_mode("FILES")
        self._mv_descartar()

        # 1. Estadio
        actual = self.stadium_selector_global.get()
        estadio, como = estadio_json(doc, path, self.stadium_data, actual)
        if estadio is None:
            estadio = actual
            avisos.append(f"{como}: kept the current stadium ({actual}).")
        if estadio != actual:
            self.stadium_selector_global.set(estadio)
            self.on_stadium_change(estadio)
            if panel is not None:
                panel.before_stadium_change()
        dj = dims_json(doc)
        da = (_int(self.inputs["Width"].get()), _int(self.inputs["Height"].get()),
              _int(self.inputs["Goal Line Size"].get()), _int(self.inputs["Center Board Width"].get()))
        mismas_dims = dj == da
        if not mismas_dims:
            avisos.append(f"The stadium profile has changed since the export (JSON {dj[0]}x{dj[1]}, GL {dj[2]}, "
                          f"CB {dj[3]} · now {da[0]}x{da[1]}, GL {da[2]}, CB {da[3]}): "
                          "only the settings were restored, positions are recalculated.")

        # 2. ELEMENTS
        root = getattr(getattr(panel, "nas", None), "root", None)
        files = files_json(doc, root)
        faltan = []
        for k in ("background", "center_board", "static_logo"):
            if files[k] and not os.path.exists(files[k]):
                faltan.append(os.path.basename(files[k]))
                files[k] = None
        anims = []
        for a in files["anim_zones"]:
            if os.path.exists(a):
                anims.append(a)
            else:
                faltan.append(os.path.basename(a))
        files["anim_zones"] = anims or [None]
        files["anim_zone"] = anims[0] if anims else None
        if faltan:
            avisos.append("Media not found (drop them again): " + ", ".join(faltan[:6]) + (" …" if len(faltan) > 6 else ""))
        self.cargar_archivos_campana(files)

        # 3. Sliders
        cfg = dict(self.config_actual())   # claves que falten en JSON antiguos: lo que hay ahora
        cfg.update({k: v for k, v in config_json(doc).items() if k != "edge_case"})
        margin = cfg.get("margin")
        if margin is None:
            conf = dict(self._config_ui())
            conf.update({k: v for k, v in cfg.items() if k in conf and k != "edge_case"})
            margin, exacto = deducir_margin(doc, da[0], da[1], da[2], da[3], self.archivos_cargados, conf,
                                            preferidos=(_margin_tl(cfg), 20, int(self.margin_slider.get())))
            debug(f"Import JSON: margin deducido {margin} ({'exacto' if exacto else 'hueco mínimo'})")
        cfg["margin"] = int(margin)
        self._aplicar_config_importada(cfg)

        # 4. Posiciones exactas
        auto = self.calcular_layout()
        objetivo = firma_elementos(doc)
        modo = "automatic"
        if auto and firma_elementos(auto) == objetivo:
            pass
        elif not mismas_dims:
            modo = "recalculated"
        else:
            ok, motivo = ManualLayout.soportado(doc)
            if ok and auto:
                try:
                    m = ManualLayout(doc, self._mv_anim_nat(doc), cfg["margin"], cfg.get("margin_tl", cfg["margin"]),
                                     cfg.get("stretch_limit", self.stretch_slider.get()))
                    if firma_elementos(m.a_datos(auto)) != objetivo:
                        raise ValueError("manual layout does not reproduce the JSON")
                    self.manual = m
                    self.manual_sig = self._mv_firma()
                    self._manual_tmp = None
                    self.manual_sel = None
                    self._mv_bloquear(True)
                    self._mv_refresh_ui()
                    modo = "manual"
                except Exception as e:
                    traceback.print_exc()
                    log_error(f"Import JSON: modo manual no aplicable: {e}")
                    self._mv_descartar()
                    modo = "recalculated"
            else:
                modo = "recalculated"
                avisos.append(f"Exact positions can't be restored ({motivo or 'layout not available'}).")

        archie = doc.get("archie") if isinstance(doc.get("archie"), dict) else {}
        base = archie.get("campaign") or self._base_desde_nombre(nombre, estadio)
        if base:
            self.output_filename_var.set(base)

        self._init_history_state()
        self.start_preview_render()

        if modo == "automatic":
            txt = "exact layout restored (automatic: sliders free)."
        elif modo == "manual":
            txt = "exact layout restored in ✋ MANUAL MODE: click an element to adjust it."
        else:
            n = len(set(objetivo) ^ set(firma_elementos(self.calcular_layout() or {})))
            txt = f"settings restored, but {n} element positions differ from the JSON."
        msg = f"📂 Imported {nombre} · {estadio} ({como}): {txt}"
        if avisos:
            msg += "\n• " + "\n• ".join(avisos)
        self.append_chat("Archie", msg)
        if avisos or modo == "recalculated":
            messagebox.showwarning("Import JSON", msg.replace("📂 ", ""), parent=self)
        debug(f"Import JSON {path}: estadio={estadio} modo={modo} avisos={avisos}")

    def _aplicar_config_importada(self, cfg):
        """Como aplicar_config_campana, sin pintar (el import decide después si va en modo manual)."""
        self.current_hide_edges = bool(cfg.get("hide_extreme_edges", False))
        self.current_under_hotspots = bool(cfg.get("under_hotspots", False))
        self.current_even_spacing = bool(cfg.get("even_spacing", False))
        self.architecture_created = True
        self.arch_stadium = self.stadium_selector_global.get()
        self.arch_key = self.solver_key = self._arch_key()   # JSON aprobado = arquitectura aprobada
        self.preview_enabled = True
        self._actualizar_sliders_desde_ia(
            cfg["qty_goal"], cfg["qty_corner"], cfg["qty_cb"], cfg["qty_tl"], cfg["stretch_limit"],
            cfg["squash_logos"], cfg.get("stretch_fit_per", False), cfg["gl_offset"], cfg["corner_logo"],
            cfg["logo_under"], cfg["qty_gl_under"], cfg["qty_tl_under"], cfg.get("show_cb", True),
            self.amplify_var.get(), cfg.get("invert_order", False), cfg.get("max_anim_gl", 0),
            cfg.get("max_anim_tl", 0), margin=cfg["margin"], margin_tl=cfg.get("margin_tl", cfg["margin"]))
        self._f2_desde_config(cfg)   # v0.95: sliders de la 2nd row (SINGLE ROW)

    @staticmethod
    def _base_desde_nombre(nombre, estadio):
        """<base>_<Estadio>_ARCHIEtoAE_01.json -> <base>"""
        stem = os.path.splitext(nombre)[0]
        for variante in (estadio.replace(" ", "_"), safe_name(estadio).replace(" ", "_")):
            i = stem.find("_" + variante)
            if i > 0:
                return stem[:i]
        i = stem.find("_ARCHIEtoAE")
        return stem[:i] if i > 0 else None
