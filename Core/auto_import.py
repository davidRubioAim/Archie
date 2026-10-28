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
    for nombre in nombres:
        ruta = os.path.join(carpeta, nombre)
        if os.path.normcase(os.path.abspath(ruta)) == origen:
            continue
        if not nombre.lower().endswith(EXTENSIONES_ELEMENTS) or nombre.startswith("."):
            continue
        if not os.path.isfile(ruta):
            continue
        tipo = detectar_tipo(nombre)
        if tipo in candidatos:
            candidatos[tipo].append(ruta)

    return {t: (rutas[0] if len(rutas) == 1 else None) for t, rutas in candidatos.items() if rutas}
