import os
import re

# Extensiones válidas para los ELEMENTS de campaña (.mov 444)
EXTENSIONES_ELEMENTS = (".mov",)

# Clave de archivos_cargados -> zona destino. "anim_zone" = Zone A de Anim Zones.
TIPOS = ("static_logo", "anim_zone", "background", "center_board")


def _tokens(nombre):
    """Palabras del nombre en mayúsculas, separando por cualquier cosa que no sea letra."""
    return re.findall(r"[A-Z]+", os.path.splitext(nombre)[0].upper())


def detectar_tipo(file_path):
    """Devuelve el tipo de ELEMENT según el nombre, o None si no lleva etiqueta.

    LOGO tiene prioridad porque existen nombres como "LOGO ANIM" o "LOGO UNDER".
    """
    toks = _tokens(os.path.basename(file_path))
    if any(t.startswith("LOGO") for t in toks):
        return "static_logo"
    if any(t.startswith("ANIM") for t in toks):
        return "anim_zone"
    if any(t == "BG" or t.startswith("BACK") or t.startswith("FONDO") for t in toks):
        return "background"
    juntos = "".join(toks)
    if "CB" in toks or "CENTERBOARD" in juntos or "CENTREBOARD" in juntos:
        return "center_board"
    return None


def buscar_elements_hermanos(file_path):
    """Busca en la carpeta del archivo dropeado el resto de ELEMENTS de la campaña.

    Devuelve {tipo: ruta} con un candidato único, o {tipo: None} si hay varios
    (la zona se deja vacía para dropear a mano). Los tipos sin ningún candidato
    no aparecen. Si el archivo dropeado no lleva etiqueta devuelve {}.
    """
    tipo_origen = detectar_tipo(file_path)
    if tipo_origen is None:
        return {}

    carpeta = os.path.dirname(os.path.abspath(file_path))
    try:
        nombres = os.listdir(carpeta)
    except OSError:
        return {}

    candidatos = {t: [] for t in TIPOS if t != tipo_origen}
    origen = os.path.normcase(os.path.abspath(file_path))
    for ruta, tipo in _elements_de(carpeta, nombres):
        if os.path.normcase(os.path.abspath(ruta)) != origen and tipo in candidatos:
            candidatos[tipo].append(ruta)

    # v1.04: carpeta de versión (Elements\NATIONAL, Elements\REGIONAL...): lo que no esté en ella
    # (p. ej. un BG compartido por todas las versiones) se busca en la carpeta de arriba.
    faltan = [t for t, rutas in candidatos.items() if not rutas]
    padre = os.path.dirname(carpeta)
    if faltan and padre and padre != carpeta:
        try:
            for ruta, tipo in _elements_de(padre, os.listdir(padre)):
                if tipo in faltan:
                    candidatos[tipo].append(ruta)
        except OSError:
            pass

    return {t: (rutas[0] if len(rutas) == 1 else None) for t, rutas in candidatos.items() if rutas}


def _elements_de(carpeta, nombres):
    """[(ruta, tipo)] de los ELEMENTS sueltos (con etiqueta) de una carpeta."""
    out = []
    for nombre in nombres:
        if not nombre.lower().endswith(EXTENSIONES_ELEMENTS) or nombre.startswith("."):
            continue
        ruta = os.path.join(carpeta, nombre)
        tipo = detectar_tipo(nombre)
        if tipo and os.path.isfile(ruta):
            out.append((ruta, tipo))
    return out


def anims_de_carpeta(file_path):
    """v1.04: todas las ANIM de la carpeta de un ELEMENT, por nombre (ANIM A, ANIM B...)."""
    carpeta = os.path.dirname(os.path.abspath(file_path))
    try:
        nombres = os.listdir(carpeta)
    except OSError:
        return []
    return sorted((r for r, t in _elements_de(carpeta, nombres) if t == "anim_zone"),
                  key=lambda r: os.path.basename(r).lower())


def carpeta_de_elements(carpeta):
    """v1.04: carpeta soltada -> (ELEMENTS a cargar, [versiones], versión cargada).
    Si la carpeta tiene sus ELEMENTS sueltos (con ANIM) se usan esos; si no, sus subcarpetas con ELEMENTS son
    versiones (NATIONAL, REGIONAL, C1...): se carga la primera por nombre, y lo que le falte (p. ej. un BG
    compartido suelto en la carpeta) sale de la carpeta soltada."""
    try:
        nombres = sorted(os.listdir(carpeta), key=str.lower)
    except OSError:
        return [], [], None
    sueltos = _elements_de(carpeta, nombres)
    versiones = {}
    for n in nombres:
        sub = os.path.join(carpeta, n)
        if n.startswith((".", "_", "$", "#")) or n.strip().upper() in ("ARCHIE", "OLD", "ARCHIVE", "BACKUP"):
            continue
        if os.path.isdir(sub):
            try:
                els = _elements_de(sub, sorted(os.listdir(sub), key=str.lower))
            except OSError:
                continue
            if els:
                versiones[n] = els
    if not versiones or any(t == "anim_zone" for _, t in sueltos):
        return [r for r, _ in sueltos], [], None
    ver = next(iter(versiones))
    propios = versiones[ver]
    tipos = {t for _, t in propios}
    return [r for r, _ in propios] + [r for r, t in sueltos if t not in tipos], list(versiones), ver
