"""v1.06: tipografía Figtree en toda la app sin instalarla en Windows.

Las .ttf de Assets/fonts se registran solo para este proceso (AddFontResourceEx + FR_PRIVATE): Tk las ve como
familias normales ("Figtree", "Figtree Light", "Figtree Medium", "Figtree SemiBold"; "bold" usa Figtree Bold).
Si no se pueden cargar, Tk cae a su fuente por defecto y la app sigue igual."""
import glob
import os

FAMILIA = "Figtree"
_registradas = False


def registrar(*carpetas):
    """Registra las .ttf/.otf de la primera carpeta que tenga alguna. Llamar antes de crear la ventana."""
    global _registradas
    if _registradas or os.name != "nt":
        return _registradas
    try:
        import ctypes
        add = ctypes.windll.gdi32.AddFontResourceExW
    except Exception:
        return False
    for carpeta in carpetas:
        rutas = sorted(glob.glob(os.path.join(carpeta, "*.ttf")) + glob.glob(os.path.join(carpeta, "*.otf")))
        n = sum(1 for r in rutas if add(r, 0x10, 0) > 0)  # 0x10 = FR_PRIVATE
        if n:
            _registradas = True
            break
    return _registradas


def aplicar_a_customtkinter():
    """Fuente por defecto de los widgets de CustomTkinter (los que no pasan family). Antes de crear widgets."""
    try:
        import customtkinter as ctk
        tema = ctk.ThemeManager.theme.get("CTkFont", {})
        if "family" in tema:
            tema["family"] = FAMILIA
        for plataforma in ("Windows", "macOS", "Linux"):  # por si el tema aún no se ha resuelto por plataforma
            if isinstance(tema.get(plataforma), dict):
                tema[plataforma]["family"] = FAMILIA
    except Exception:
        pass


def aplicar_a_tk(root):
    """Fuentes con nombre de Tk (tk.Label, Entry, menús, diálogos...). Con la ventana raíz ya creada."""
    try:
        from tkinter import font as tkfont
        for nombre in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                       "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(nombre, root=root).configure(family=FAMILIA)
            except Exception:
                pass
    except Exception:
        pass
