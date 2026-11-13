"""
auto_solver.py (v0.58, v0.61: hotspots)
Arquitectura por defecto ("Create Architecture") sin tocar la interfaz.

Es la lógica de MegaStripApp._native_auto_solver sacada a funciones puras, para poder calcular la
arquitectura de una campaña que el PM todavía no ha abierto (export de campañas) con exactamente
los mismos intentos que el botón. app.py usa resolver() para que no haya dos copias del algoritmo.
"""
import math
import os

from motor_distribucion import calcular_layout_data
from frame_cache import FRAME_CACHE
from log_utils import debug, log_error

# Claves de la config de calcular_layout_data que se guardan por campaña (más "margin")
CONFIG_KEYS = ("stretch_limit", "squash_logos", "stretch_fit_per", "qty_goal", "qty_corner", "qty_cb", "qty_tl",
               "gl_offset", "corner_logo", "hide_extreme_edges", "logo_under", "qty_gl_under", "qty_tl_under",
               "show_cb", "amplify", "max_anim_gl", "max_anim_tl", "invert_order",
               "margin_tl", "under_hotspots",  # v0.61
               "even_spacing")                  # v0.62


def solver_legacy():
    """v0.61: ARCHIE_SOLVER=legacy en Config/.env vuelve a la lista fija de intentos de v0.58."""
    return os.getenv("ARCHIE_SOLVER", "hotspots").strip().lower() == "legacy"


def logo_es_ancho(logo_path):
    if not logo_path:
        return False
    try:
        w, h = FRAME_CACHE.dimensions(logo_path)
        return h > 0 and (w / h) > 3.0
    except Exception as e:
        log_error(f"Error midiendo logo: {e}")
        return False


def intentos_por_defecto(amplify, is_wide):
    if amplify:
        return [
            {"qty_goal": 1, "qty_corner": 0, "qty_cb": 0, "qty_tl": 1, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Maximum alternation throughout the stadium (Goal Line + Touchline)"},
            {"qty_goal": 1, "qty_corner": 0, "qty_cb": 0, "qty_tl": 0, "stretch_limit": 10, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Narrow Touchline detected. Logo Priority applied to Goal Lines."},
            {"qty_goal": 0, "qty_corner": 0, "qty_cb": 0, "qty_tl": 0, "stretch_limit": 15, "corner_logo": False, "hide_extreme_edges": True, "msg": "AMPLIFY: Extreme compression mode (Anim Zones only)"}
        ]
    if is_wide:
        return [
            {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": False, "hide_extreme_edges": False, "msg": "WIDE Logo: Full logos in corners and maximum Anim Zones density."},
            {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False, "msg": "WIDE Logo: Split Corner activated (1/2 logo) to free up space."},
            {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "WIDE Logo: Absolute edges cleared."},
            {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 0, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "WIDE Logo: Reduction to 1 Anim Zone in Touchline."}
        ]
    return [
        {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": False, "msg": "NARROW Logo: Maximum density fitted perfectly."},
        {"qty_goal": 5, "qty_corner": 3, "qty_cb": 2, "qty_tl": 3, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Edges cleared to save the 5-logo layout in Goal Lines."},
        {"qty_goal": 5, "qty_corner": 1, "qty_cb": 2, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Reduction in Corners and Touchlines."},
        {"qty_goal": 3, "qty_corner": 1, "qty_cb": 1, "qty_tl": 2, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Reduction of Goal Lines to 3 logos."},
        {"qty_goal": 1, "qty_corner": 1, "qty_cb": 1, "qty_tl": 1, "stretch_limit": 15, "corner_logo": True, "hide_extreme_edges": True, "msg": "NARROW Logo: Minimalist mode activated due to lack of space."}
    ]


def resolver(w_total, h_total, goal_line, cb_width, margin, archivos, base):
    """v0.61: por defecto, solver por hotspots (hotspot_solver.py). Amplify, sin logo o ARCHIE_SOLVER=legacy:
    la lista fija de intentos de siempre."""
    if not base.get("amplify") and archivos.get("static_logo") and not solver_legacy():
        try:
            from hotspot_solver import resolver as resolver_hotspots
            return resolver_hotspots(w_total, h_total, goal_line, cb_width, margin, archivos, base)
        except Exception as e:
            log_error(f"Hotspot solver falló, uso el solver clásico: {e}")
    return resolver_legacy(w_total, h_total, goal_line, cb_width, margin, archivos, base)


def completar_goal(w_total, h_total, goal_line, cb_width, margin, archivos, config):
    """v0.66: con una config que no ha salido del auto-solver, prueba sola a meter tras la portería los logos
    que pide el tamaño del logo (ver hotspot_solver.completar_goal). -> (config, msg o None)"""
    if solver_legacy() or not config:
        return config, None
    try:
        from hotspot_solver import completar_goal as _completar
        return _completar(w_total, h_total, goal_line, cb_width, margin, archivos, config)
    except Exception as e:
        log_error(f"completar_goal falló: {e}")
        return config, None


def resolver_legacy(w_total, h_total, goal_line, cb_width, margin, archivos, base):
    """Prueba los intentos por defecto y devuelve (config, msg) del primero sin solapes.

    base: ajustes que el solver no decide (los toma de la interfaz o de los valores por defecto):
      gl_offset, logo_under, corner_logo (solo cuenta en logo under), qty_gl_under, qty_tl_under,
      show_cb, amplify, invert_order, max_anim_gl, max_anim_tl
    """
    is_wide = logo_es_ancho(archivos.get("static_logo"))
    best_config, msg, test_conf = None, "", None
    for config in intentos_por_defecto(base["amplify"], is_wide):
        is_under = base["logo_under"]
        c_logo = base["corner_logo"] if is_under else config["corner_logo"]
        q_corner = (1 if c_logo else 0) if is_under else config["qty_corner"]
        test_conf = {
            "stretch_limit": config.get("stretch_limit", 15), "squash_logos": False,
            "qty_goal": config["qty_goal"], "qty_corner": q_corner,
            "qty_cb": config["qty_cb"], "qty_tl": config["qty_tl"],
            "gl_offset": base["gl_offset"], "corner_logo": c_logo,
            "hide_extreme_edges": config["hide_extreme_edges"],
            "logo_under": is_under,
            "qty_gl_under": int(base["qty_gl_under"]),
            "qty_tl_under": int(base["qty_tl_under"]),
            "show_cb": base["show_cb"],
            "amplify": base["amplify"],
            "invert_order": base["invert_order"],
            "max_anim_gl": int(base["max_anim_gl"]),
            "max_anim_tl": int(base["max_anim_tl"]),
            "margin_tl": base.get("margin_tl"),
            "edge_case": base.get("edge_case"),
        }
        try:
            datos = calcular_layout_data(w_total, h_total, goal_line, cb_width, margin, archivos, test_conf)
            warnings = datos.get("warnings", []) if datos else ["error"]
            if "goal_overlap" not in warnings and "touchline_overlap" not in warnings:
                best_config = test_conf
                msg = "Auto-Solver: " + config["msg"]
                break
        except Exception as e:
            debug(f"Auto-Solver trial failed: {e}")
    if not best_config:
        best_config = test_conf
        msg = "Auto-Solver: WARNING - Stadium too small or crowded. Overlap will occur."
    if best_config:
        best_config = dict(best_config)
        best_config.pop("edge_case", None)
    return best_config, msg


# ---------------------------------------------------------------- campañas (sin interfaz)
def es_logo_under(path):
    """Mismo criterio que app.check_and_activate_logo_under."""
    if not path:
        return False
    import os
    name = os.path.basename(path).lower().replace(" ", "").replace("_", "").replace("-", "")
    return "logounder" in name


def max_logos_under(w_total, h_total, goal_line, cb_width, margin, logo_path):
    """Mismo cálculo que app.get_max_under_logos, sin interfaz."""
    try:
        if not logo_path:
            return 15, 15
        lw, lh = FRAME_CACHE.dimensions(logo_path)
        logo_w = int(round(lw * h_total / lh)) if lh > 0 else int(h_total * 1.5)
        logo_w = max(1, logo_w)
        z2_w = (w_total / 2) - (cb_width / 2) - goal_line
        denom = logo_w + margin
        max_gl = max(1, math.floor((goal_line + margin) / denom))
        max_tl = max(1, math.floor((z2_w + margin) / denom))
        return max_gl, max_tl
    except Exception as e:
        log_error(f"max_logos_under: {e}")
        return 15, 15


def config_campana(perfil, nombre_estadio, archivos, defaults):
    """Arquitectura por defecto de una campaña para un estadio, como si el PM hubiera pulsado Create Architecture
    con la interfaz recién abierta. -> (config completa para calcular_layout_data + "margin", msg)

    perfil: perfil del estadio (stadium_data[nombre]); defaults: valores por defecto de la interfaz
    (MegaStripApp.ui_defaults_campana()).
    """
    w_total, h_total = int(perfil["Width"]), int(perfil["Height"])
    goal_line, cb_width = int(perfil["Goal Line Size"]), int(perfil["Center Board Width"])
    margin = int(defaults["margin"])
    logo = archivos.get("static_logo")
    under = es_logo_under(logo) or bool(defaults.get("logo_under"))
    base = {
        "gl_offset": int(defaults["gl_offset"]),
        "logo_under": under,
        "corner_logo": False if under else bool(defaults["corner_logo"]),
        "qty_gl_under": int(defaults["qty_gl_under"]),
        "qty_tl_under": int(defaults["qty_tl_under"]),
        "show_cb": True,
        "amplify": bool(perfil.get("Amplify")) or "AMPLIFY" in (nombre_estadio or "").upper(),
        "invert_order": bool(defaults.get("invert_order", False)),
        "max_anim_gl": int(defaults["max_anim_gl"]),
        "max_anim_tl": int(defaults["max_anim_tl"]),
        "margin_tl": defaults.get("margin_tl"),            # v0.61
        "edge_case": perfil.get("Edge Case"),               # v0.61: el solver respeta las zonas negras
    }
    if under and es_logo_under(logo):
        max_gl, max_tl = max_logos_under(w_total, h_total, goal_line, cb_width, margin, logo)
        base["qty_gl_under"] = max(1, int(max_gl * 0.8))
        base["qty_tl_under"] = max(1, int(max_tl * 0.8))
    config, msg = resolver(w_total, h_total, goal_line, cb_width, margin, archivos, base)
    config = dict(config)
    config["stretch_fit_per"] = bool(defaults.get("stretch_fit", False))
    config["margin"] = margin
    return config, msg
