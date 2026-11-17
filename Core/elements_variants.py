"""
elements_variants.py (v0.87)
Búsqueda "inteligente" de ELEMENTS para el worker: subcarpetas y versiones de campaña (C1, C2, C3...).

nas_campaigns.inspeccionar solo mira los .mov sueltos en <CÓDIGO>\\Output\\Elements. Aquí además:
  - Se entra en las subcarpetas de Elements (hasta MAX_DEPTH niveles), p.ej. Elements\\SO_Just_Eat_C1\\.
    Se ignoran la carpeta Archie (arquitecturas guardadas) y las que empiezan por . _ $ # o se llaman OLD/Archive.
  - Cada archivo se asigna a una VERSIÓN:
        1. un token Cn en el nombre        JETX-UEFA-260501_C1_LOGO_FIXED.mov       -> C1
        2. si no, un Cn en la subcarpeta   Elements\\SO_Just_Eat_C2\\LOGO.mov        -> C2
        3. si no, el nombre de la subcarpeta                                        -> SO_Just_Eat
        4. si está suelto en Elements                                              -> "" (común)
  - Los archivos comunes ("") completan lo que falte en cada versión (p.ej. un BG compartido por C1 y C2).
    Si solo hay comunes, hay una única versión "" (el caso de siempre).
  - Cada versión se clasifica igual que en Archie: ANIM (todas), LOGO / BG / CB (uno; si hay varios -> revisar).

Resultado:
  {"status": no_nas | not_found | found, "path": carpeta Elements,
   "variants": [{"key": "C1", "status": ready|incomplete|review, "files", "missing", "ambiguous", "dir"}]}
"""
import os
import re

from auto_import import detectar_tipo, EXTENSIONES_ELEMENTS
from nas_campaigns import REQUIRED, ST_NO_NAS, ST_NOT_FOUND, ST_INCOMPLETE, ST_REVIEW, ST_READY, _isdir_timeout

ST_FOUND = "found"
MAX_DEPTH = 3
IGNORAR = {"ARCHIE", "OLD", "ARCHIVE", "BACKUP", "TEMP", "TMP"}
VERSION_RE = re.compile(r"(?:^|[^A-Z0-9])C(\d{1,2})(?![0-9A-Z])")


def _version_de(texto):
    m = VERSION_RE.search(texto.upper())
    return f"C{int(m.group(1))}" if m else None


def _orden(key):
    m = re.fullmatch(r"C(\d+)", key or "")
    return (0, int(m.group(1)), "") if m else (1, 0, key or "")


def _listar(ed):
    """[(ruta, carpeta relativa)] de los .mov de Elements y sus subcarpetas."""
    out = []

    def rec(path, rel, depth):
        try:
            entradas = list(os.scandir(path))
        except OSError:
            return
        for e in entradas:
            if e.name.startswith((".", "_", "$", "#")):
                continue
            if e.is_dir():
                if depth < MAX_DEPTH and e.name.strip().upper() not in IGNORAR:
                    rec(e.path, os.path.join(rel, e.name) if rel else e.name, depth + 1)
            elif e.name.lower().endswith(EXTENSIONES_ELEMENTS):
                out.append((e.path, rel))
    rec(ed, "", 1)
    return out


def _clasificar(paths):
    por_tipo = {"anim_zone": [], "static_logo": [], "background": [], "center_board": []}
    for p in sorted(paths, key=lambda x: os.path.basename(x).lower()):
        t = detectar_tipo(p)
        if t in por_tipo:
            por_tipo[t].append(p)
    return por_tipo


def _variante(key, por_tipo, carpeta):
    anims = por_tipo["anim_zone"]
    files = {"anim_zones": anims or [None], "anim_zone": anims[0] if anims else None,
             "static_logo": None, "background": None, "center_board": None}
    ambiguous = []
    for t in ("static_logo", "background", "center_board"):
        if len(por_tipo[t]) == 1:
            files[t] = por_tipo[t][0]
        elif len(por_tipo[t]) > 1:
            ambiguous.append(t)
    missing = [t for t in REQUIRED if not por_tipo[t]]
    status = ST_INCOMPLETE if missing else (ST_REVIEW if ambiguous else ST_READY)
    return {"key": key, "status": status, "files": files, "missing": missing, "ambiguous": ambiguous,
            "dir": carpeta}


def inspeccionar_variantes(nas, code):
    base = nas.inspeccionar(code)          # resuelve la carpeta (reescanea el índice si hace falta) y el NAS
    res = {"status": base["status"], "path": base.get("path") or "", "variants": []}
    if base["status"] == ST_NO_NAS:
        return res
    ed = nas.elements_dir(code) if nas.carpeta(code) else None
    if not ed or not _isdir_timeout(ed):
        res["status"] = ST_NOT_FOUND
        return res
    res["path"] = ed

    grupos, carpetas = {}, {}
    for path, rel in _listar(ed):
        if detectar_tipo(path) is None:
            continue
        key = _version_de(os.path.basename(path)) or (_version_de(rel) if rel else None) or \
            (rel.split(os.sep)[0] if rel else "")
        grupos.setdefault(key, []).append(path)
        carpetas.setdefault(key, os.path.join(ed, rel) if rel else ed)
    if not grupos:
        res["status"] = ST_NOT_FOUND
        return res

    comunes = grupos.pop("", [])
    if not grupos:
        res["variants"] = [_variante("", _clasificar(comunes), ed)]
    else:
        tipos_comunes = _clasificar(comunes)
        for key in sorted(grupos, key=_orden):
            propios = _clasificar(grupos[key])
            for t, lst in tipos_comunes.items():   # lo común completa lo que la versión no tiene
                if not propios[t]:
                    propios[t] = list(lst)
            res["variants"].append(_variante(key, propios, carpetas[key]))
    res["status"] = ST_FOUND
    return res
