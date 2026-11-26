"""v1.08: reglas base de la arquitectura y avisos con confirmación en el chat.

Cuando el usuario pide al chat algo que rompe la lógica base del motor (motor_distribucion.py /
hotspot_solver.py), Archie no lo aplica a ciegas: avisa de por qué es mala idea y ofrece
"Continuar igualmente" / "Cancelar". Si se confirma, la acción se aplica con un override en la config
(config["overrides"]) que el motor respeta y no revierte.

Dos capas:
- El LLM recibe REGLAS_PROMPT en el system prompt y devuelve "requires_confirmation" +
  "architecture_warning" en vez de aplicar.
- detectar_conflictos() es una validación determinista en Python (no depende sólo del LLM) para los casos
  que el motor revertiría por su cuenta, p. ej. dejar las porterías sin logos.
"""
import re

# ---------------------------------------------------------------- overrides que entiende el motor
# allow_no_goal_logos: con qty_goal == 0 no se ponen logos tras las porterías (sin override, 0 = "Auto" y
#   el GL estándar elige 1..9 logos según la cuota aprendida).
OVERRIDES_VALIDOS = ("allow_no_goal_logos",)


def overrides_de(config):
    """Overrides activos de una config de calcular_layout_data (dict, nunca None)."""
    ov = (config or {}).get("overrides") or {}
    return {k: bool(v) for k, v in ov.items() if k in OVERRIDES_VALIDOS and v}


def sin_logos_en_goal(config):
    """True si la config pide porterías sin logos de forma forzada (override + qty_goal 0)."""
    return bool(overrides_de(config).get("allow_no_goal_logos")) and int((config or {}).get("qty_goal") or 0) == 0


# ---------------------------------------------------------------- reglas (para el system prompt)
REGLAS = [
    ("GOAL_HOTSPOT_RESERVED",
     "The zone behind each goal (center of Goal Lines Z1/Z5) is a hotspot RESERVED for static logos: it is the "
     "most valuable TV camera position. The engine always puts k>=1 logos there (k chosen automatically from the "
     "learned ~26% share of the Goal Line). `qty_goal`: 0 means AUTO, not 'none'. Removing goal logos, or asking "
     "for animations/videos behind the goals, BREAKS this rule.",
     "allow_no_goal_logos"),
    ("CORNER_HOTSPOT",
     "Each corner (junction Goal Line / Touchline) carries one static logo on the GL side and one on the TL side. "
     "Removing corner logos in standard mode breaks the symmetry of the approved architecture.",
     None),
    ("NO_OVERLAP",
     "Elements can never overlap. If a request does not fit physically, say what must be sacrificed instead of "
     "applying it.",
     None),
    ("MIN_MARGIN",
     "Margins (`margin`, `margin_tl`) below 10 px make elements look glued together on the LED; 0 px is a "
     "breaking change.",
     None),
    ("AMPLIFY_PROFILE",
     "`amplify` comes from the stadium profile and must never be changed by the chat.",
     None),
    ("SYMMETRY",
     "Goal Line Right (Z5) is always the mirror of Goal Line Left (Z1). Asymmetric goal layouts break the rule.",
     None),
]

REGLAS_PROMPT = "\n".join(f"        - {rid}: {txt}" for rid, txt, _ in REGLAS) + """

        ARCHITECTURE WARNINGS (MANDATORY):
        If the user's request BREAKS any of the rules above, do NOT silently apply it and do NOT silently refuse it.
        Return instead, together with the keys you WOULD change if the user confirms:
          "requires_confirmation": true,
          "architecture_warning": {"rule": "<RULE_ID>", "reason": "<1-2 sentences, in the user's language, explaining why it is a bad decision>"},
          "override": "<override name or null>"
        Known overrides: "allow_no_goal_logos" (rule GOAL_HOTSPOT_RESERVED; then send `qty_goal`: 0).
        Requests that respect the rules never carry these keys. If the [STATE] block shows the override already
        active ("ov"), the user accepted it before: do not warn again for that same rule.
"""

# ---------------------------------------------------------------- validación determinista
_RE_GOAL = re.compile(r"(goal|porter[ií]a|arco|behind the goal|tras (la|las) porter)", re.I)
_RE_NEG_LOGO = re.compile(
    r"(\b(no|sin|without|remove|quita\w*|elimina\w*|borra\w*|delete|zero|cero|ning[uú]n\w*|none)\b[^.\n]{0,40}\blogos?\b"
    r"|\blogos?\b[^.\n]{0,25}\b(fuera|out|off)\b"
    r"|\b(anim\w*|v[ií]deos?|animaci\w*)\b[^.\n]{0,40}\b(en lugar de|instead of)\b[^.\n]{0,20}\blogos?\b)",
    re.I)
_RE_SOLO_LOGOS = re.compile(r"\b(s[oó]lo|only|just|m[aá]s|more)\s+(\d+\s+)?logos?\b", re.I)


def pide_goal_sin_logos(texto):
    """Lectura del texto del usuario: "no pongas logos tras las GOALS", "remove the goal logos",
    "sin logos en las porterías"..."""
    t = texto or ""
    if _RE_SOLO_LOGOS.search(t):   # "sin animaciones en la portería, sólo logos" no es un conflicto
        return False
    return bool(_RE_GOAL.search(t) and _RE_NEG_LOGO.search(t))


def detectar_conflictos(texto_usuario, datos_ia, config_actual):
    """-> lista de avisos {"rule", "reason", "override", "cambios"} que rompen la arquitectura base y que el
    motor revertiría por su cuenta. Vacía si no hay conflicto. config_actual: la de Manual Edit (con
    "overrides" y "logo_under")."""
    avisos = []
    datos_ia = datos_ia or {}
    config_actual = config_actual or {}
    logo_under = bool(datos_ia.get("logo_under", config_actual.get("logo_under")))
    ya_forzado = overrides_de(config_actual).get("allow_no_goal_logos")

    # GOALS sin logos: por texto o porque el JSON del LLM deja qty_goal a 0 (que el motor lee como Auto)
    goal_ia = datos_ia.get("qty_goal")
    pide_cero = goal_ia is not None and int(goal_ia or 0) == 0 and int(config_actual.get("qty_goal") or 0) != 0
    if not logo_under and not ya_forzado and (pide_goal_sin_logos(texto_usuario) or
                                              (pide_cero and datos_ia.get("override") == "allow_no_goal_logos")):
        avisos.append({
            "rule": "GOAL_HOTSPOT_RESERVED",
            "reason": ("Las porterías son hotspots reservados para logos estáticos: es la posición que más ve la "
                       "cámara de TV y la arquitectura aprobada siempre deja logos tras cada GOAL. Sin ellos se "
                       "pierde la exposición más valiosa del patrocinador y el motor, por defecto, los volvería "
                       "a poner."),
            "override": "allow_no_goal_logos",
            "cambios": {"qty_goal": 0},
        })
    return avisos


def aviso_del_llm(datos_ia):
    """Aviso que devuelve el LLM ("requires_confirmation" + "architecture_warning"), normalizado, o None."""
    if not isinstance(datos_ia, dict) or not datos_ia.get("requires_confirmation"):
        return None
    w = datos_ia.get("architecture_warning") or {}
    if isinstance(w, str):
        w = {"reason": w}
    ov = datos_ia.get("override") or w.get("override")
    return {
        "rule": str(w.get("rule") or "ARCHITECTURE"),
        "reason": str(w.get("reason") or "Este cambio rompe la lógica base de la arquitectura."),
        "override": ov if ov in OVERRIDES_VALIDOS else None,
        "cambios": {},
    }


def limpiar_datos_ia(datos_ia):
    """Quita del JSON del LLM las claves del aviso (lo que queda son los parámetros a aplicar)."""
    return {k: v for k, v in (datos_ia or {}).items()
            if k not in ("requires_confirmation", "architecture_warning", "override")}
