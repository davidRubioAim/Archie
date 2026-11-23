"""
v0.93: DROP EN CUALQUIER PARTE DE LA VENTANA.

Mixin de MegaStripApp. Las drop zones de ELEMENTS siguen igual; además se puede soltar en cualquier sitio:
  - Un .json de Archie -> se importa el proyecto (IMPORT JSON, como el botón 📂).
  - Uno o varios ELEMENTS (.mov, .png...) -> cada uno va a su zona según su etiqueta en el nombre
    (LOGO / ANIM / BG / CB, auto_import.detectar_tipo). Varios ANIM a la vez -> Zone A, B, C... por nombre.
    Del primer ELEMENT soltado se buscan en su carpeta los que falten (misma lógica que en las drop zones;
    si hay varias ANIM en la carpeta, se cargan todas como Zone A, B, C... por nombre);
    lo que se ha soltado manda sobre lo encontrado en la carpeta.
Si se sueltan varios archivos o un .json sobre una drop zone concreta, también se usa esta lógica.
v1.04: también se puede soltar una carpeta: sus ELEMENTS, o si solo tiene subcarpetas de versión
(Elements\\NATIONAL, Elements\\REGIONAL...), los de la primera versión (lo que falte, p. ej. un BG compartido,
se busca en la carpeta de arriba).
"""
import os

from tkinter import messagebox

from auto_import import detectar_tipo, anims_de_carpeta, carpeta_de_elements
from log_utils import debug

NOMBRES = {"static_logo": "LOGO", "anim_zone": "ANIM", "background": "BG", "center_board": "CB"}


class GlobalDropMixin:
    def _dnd_paths(self, event):
        """Rutas de un evento de tkinterdnd2 (las que llevan espacios vienen entre llaves)."""
        try:
            return [p for p in self.tk.splitlist(event.data) if p]
        except Exception:
            return [event.data.strip("{}")] if event.data else []

    @staticmethod
    def _drop_especial(paths):
        """True si no es un solo ELEMENT (varios archivos o un .json): lo resuelve _soltar_archivos."""
        return len(paths) != 1 or paths[0].lower().endswith(".json") or os.path.isdir(paths[0])

    def on_drop_global(self, event):
        self._soltar_archivos(self._dnd_paths(event))
        return getattr(event, "action", None)

    def _modo_files(self):
        """Lo soltado va a FILES: la campaña activa se guarda y se suelta para no pisarla."""
        panel = getattr(self, "campaign_panel", None)
        if panel is not None:
            panel.before_stadium_change()
        if self.media_mode.get() != "FILES":
            self.media_mode.set("FILES")
            self.on_media_mode("FILES")

    @staticmethod
    def _anims_de_carpeta(file_path):
        """Todas las ANIM de la carpeta del ELEMENT soltado (ANIM A, ANIM B... por nombre)."""
        return anims_de_carpeta(file_path)

    def _soltar_archivos(self, paths):
        paths = [p for p in paths if p]
        if not paths:
            return
        jsons = [p for p in paths if p.lower().endswith(".json")]
        if jsons:
            if len(jsons) > 1 or len(jsons) != len(paths):
                self.append_chat("Archie", f"📂 Several files dropped: importing only {os.path.basename(jsons[0])}.")
            self.importar_json(jsons[0])
            return

        # v1.04: carpetas soltadas -> sus ELEMENTS (o los de su primera subcarpeta de versión)
        files, versiones = [], None
        for p in paths:
            if os.path.isdir(p):
                encontrados, vers, ver = carpeta_de_elements(p)
                files += encontrados
                if vers and versiones is None:
                    versiones = (os.path.basename(os.path.normpath(p)), vers, ver)
            elif os.path.isfile(p):
                files.append(p)
        fijos, anims, sin_tipo = {}, [], []
        for p in files:
            t = detectar_tipo(p)
            if t == "anim_zone":
                anims.append(p)
            elif t:
                fijos[t] = p
            else:
                sin_tipo.append(os.path.basename(p))
        if not fijos and not anims:
            messagebox.showinfo(
                "Drop files",
                "Archie can't tell which ELEMENT this is.\n\nThe file name needs LOGO, ANIM, BG or CB, "
                "or drop it on its zone in MEDIA ASSETS.\n\n" + "\n".join(sin_tipo[:8]), parent=self)
            return

        self._modo_files()

        # 1. Los que falten, de la carpeta del primer ELEMENT soltado (misma lógica que las drop zones)
        primero = next(p for p in files if detectar_tipo(p))
        self._autoimportar_elements(primero, detectar_tipo(primero))

        # 2. Lo soltado manda
        for t, p in fijos.items():
            self.archivos_cargados[t] = p
            self.drop_labels[t].configure(text=f"Loaded:\n{os.path.basename(p)}", text_color="#2CC985")
            if t == "center_board":
                self.show_cb_var.set(True)
        if not anims and not any(self.archivos_cargados.get("anim_zones") or []):
            anims = self._anims_de_carpeta(primero)   # grupo de ANIM en la carpeta (A, B, C...)
        elif len(anims) == 1:
            # v1.04: una sola ANIM soltada con ANIM B (C...) en su carpeta -> se cargan todas
            anims = self._anims_de_carpeta(anims[0]) or anims
        if anims:
            anims = sorted(anims, key=lambda p: os.path.basename(p).lower())
            self.archivos_cargados["anim_zones"] = list(anims)
            self.archivos_cargados["anim_zone"] = anims[0]
            self.output_filename_var.set(os.path.splitext(os.path.basename(anims[0]))[0])
        for p in list(fijos.values()) + anims:
            self.check_and_activate_logo_under(p)

        self.refresh_anim_zones_ui()
        self.reset_preview_state()
        self.reset_zooms_and_pan()
        self.trigger_update()

        a = self.archivos_cargados
        cargados = [f"{NOMBRES[k]}: {os.path.basename(a[k])}" for k in ("static_logo", "background", "center_board") if a.get(k)]
        cargados += [f"ANIM {chr(65 + i)}: {os.path.basename(p)}" for i, p in enumerate(a.get("anim_zones") or []) if p]
        msg = "📥 ELEMENTS loaded · " + " · ".join(cargados)
        if versiones and len(versiones[1]) > 1:
            msg += (f"\n• {versiones[0]} has {len(versiones[1])} versions ({', '.join(versiones[1])}): "
                    f"loaded {versiones[2]}. Drop another version folder to switch.")
        if sin_tipo:
            msg += "\n• Ignored (no LOGO / ANIM / BG / CB in the name): " + ", ".join(sin_tipo[:6])
        self.append_chat("Archie", msg)
        debug(f"Drop global: {paths} -> {a}")
