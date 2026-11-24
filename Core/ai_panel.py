"""
ai_panel.py (v1.02): pestaña AI ASSISTANT con el estilo del Design System AIM 2026.

Fondo en gradiente (blanco -> rosa Magenta/Coral) pintado con PIL en un Canvas; encima, la tarjeta de cabecera
(avatar de Archie + "Online"), el saludo en magenta fino, la tarjeta del chat y la caja de pregunta con "Send".
Tk no tiene transparencias: las tarjetas blancas se pintan en la imagen de fondo y los widgets van sobre blanco.

construir(app, tab) crea app.chat_history, app.chat_input y app.btn_send_ai (los mismos que usaba app.py)
y devuelve la altura mínima (px) que necesita la pestaña: cabecera, ~6 líneas de chat y la caja de pregunta.
"""
import tkinter as tk
import tkinter.font as tkfont

import numpy as np
import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter, ImageTk

MAGENTA = "#FF099A"
MAGENTA_HOVER = "#D1007A"
TEXTO = "#2B2B2B"
TEXTO_SUAVE = "#8A8A8A"
BORDE_TARJETA = (246, 214, 228, 255)     # rosa muy claro para el filo de las tarjetas
GRAD_ARRIBA = np.array([255, 250, 252], np.float32)
GRAD_ABAJO = np.array([252, 214, 226], np.float32)   # Magenta-200 / Coral-200 del Figma, suavizado
AVATAR_A, AVATAR_B = (255, 79, 199), (255, 156, 120)  # Gradient 1: Fuchsia -> Coral

PAD, CAB_H, SALUDO_H, INPUT_H, GAP, LINEAS_MIN = 12, 52, 66, 54, 10, 6


def _familia(preferidas, defecto):
    try:
        disponibles = set(tkfont.families())
    except Exception:
        return defecto
    for f in preferidas:
        if f in disponibles:
            return f
    return defecto


def _redondeado(d, box, r, fill, outline=None):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=1 if outline else 0)


def _avatar(px):
    """Círculo con el gradiente Fuchsia -> Coral y un brillo, suavizado (supermuestreo x4)."""
    n = px * 4
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) / max(1, n - 1)
    t = np.clip((xx + yy) / 2, 0, 1)[..., None]
    rgb = np.array(AVATAR_A, np.float32) * (1 - t) + np.array(AVATAR_B, np.float32) * t
    img = Image.fromarray(rgb.astype(np.uint8), "RGB").convert("RGBA")
    brillo = Image.new("L", (n, n), 0)
    ImageDraw.Draw(brillo).ellipse((n * 0.15, n * 0.1, n * 0.6, n * 0.55), fill=90)
    brillo = brillo.filter(ImageFilter.GaussianBlur(n * 0.08))
    img = Image.composite(Image.new("RGBA", (n, n), (255, 255, 255, 255)), img, brillo)
    mask = Image.new("L", (n, n), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, n - 1, n - 1), fill=255)
    img.putalpha(mask)
    return img.resize((px, px), Image.LANCZOS)


def fondo(w, h, s, con_saludo):
    """Imagen de fondo de la pestaña (w x h px reales, s = escala de CTk)."""
    yy = np.linspace(0, 1, h, dtype=np.float32)[:, None, None]
    xx = np.linspace(-1, 1, w, dtype=np.float32)[None, :, None]
    t = np.clip(yy ** 1.3 * (0.85 + 0.15 * xx ** 2), 0, 1)
    rgb = GRAD_ARRIBA * (1 - t) + GRAD_ABAJO * t
    img = Image.fromarray(np.broadcast_to(rgb, (h, w, 3)).astype(np.uint8), "RGB").convert("RGBA")
    capa = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(capa)
    p = int(PAD * s)
    r = int(14 * s)
    box = lambda y0, y1: (p, y0, w - p - 1, y1)
    cab = box(p, p + int(CAB_H * s))
    _redondeado(d, cab, r, (255, 255, 255, 170), BORDE_TARJETA)
    inp_y0 = h - p - int(INPUT_H * s)
    _redondeado(d, box(inp_y0, h - p - 1), int(INPUT_H * s / 2), (255, 255, 255, 255), BORDE_TARJETA)
    chat_y0 = cab[3] + int(GAP * s) + (int(SALUDO_H * s) if con_saludo else 0)
    _redondeado(d, box(chat_y0, inp_y0 - int(GAP * s)), r, (255, 255, 255, 255), BORDE_TARJETA)
    # sombra suave bajo la caja de pregunta
    sombra = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(sombra).rounded_rectangle(box(inp_y0 + int(3 * s), h - p + int(2 * s)), radius=int(INPUT_H * s / 2),
                                             fill=(214, 120, 160, 50))
    img = Image.alpha_composite(img, sombra.filter(ImageFilter.GaussianBlur(6 * s)))
    img = Image.alpha_composite(img, capa)
    av = _avatar(int(32 * s))
    img.alpha_composite(av, (p + int(14 * s), cab[1] + (cab[3] - cab[1] - av.height) // 2))
    return img.convert("RGB"), dict(cab=cab, chat_y0=chat_y0, chat_y1=inp_y0 - int(GAP * s), inp_y0=inp_y0, p=p)


class PanelAI:
    def __init__(self, app, tab):
        self.app = app
        self.s = self._escala(tab)
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(0, weight=1)
        self.cv = tk.Canvas(tab, highlightthickness=0, bd=0, bg="#FFF8FB")
        self.cv.grid(row=0, column=0, sticky="nsew")
        self._photo = None
        self._tam = None
        self._fam = _familia(("Figtree", "Open Sans"), "Segoe UI")  # v1.06
        self._fam_fina = _familia(("Figtree Light", "Open Sans Light", "Segoe UI Light", "Segoe UI Semilight"), self._fam)

        app.chat_history = ctk.CTkTextbox(self.cv, wrap="word", fg_color="#FFFFFF", bg_color="#FFFFFF",
                                          text_color=TEXTO, corner_radius=0, border_width=0,
                                          font=ctk.CTkFont(family=self._fam, size=12),
                                          scrollbar_button_color="#FFD0E8", scrollbar_button_hover_color=MAGENTA)
        app.chat_history.configure(state="disabled")
        try:   # nombre del que habla en magenta y negrita (CTkTextbox no deja poner font en tag_config)
            app.chat_history._textbox.tag_configure("bold", foreground=MAGENTA,
                                                     font=(self._fam, -int(12 * self.s), "bold"))
        except Exception:
            pass
        app.chat_input = ctk.CTkEntry(self.cv, placeholder_text="Ask Archie a question...", fg_color="#FFFFFF",
                                      bg_color="#FFFFFF", border_width=0, text_color=TEXTO,
                                      placeholder_text_color="#9A9A9A", font=ctk.CTkFont(family=self._fam, size=13))
        app.chat_input.bind("<Return>", lambda e: app.send_ai_message())
        app.btn_send_ai = ctk.CTkButton(self.cv, text="Send", width=76, height=34, corner_radius=17,
                                        fg_color=MAGENTA, hover_color=MAGENTA_HOVER, bg_color="#FFFFFF",
                                        text_color="#FFFFFF", font=ctk.CTkFont(family=self._fam, size=12, weight="bold"),
                                        command=app.send_ai_message)
        self._w_chat = self.cv.create_window(0, 0, window=app.chat_history, anchor="nw")
        self._w_inp = self.cv.create_window(0, 0, window=app.chat_input, anchor="w")
        self._w_send = self.cv.create_window(0, 0, window=app.btn_send_ai, anchor="e")
        self.cv.bind("<Configure>", self._on_resize)

    @staticmethod
    def _escala(w):
        try:
            return ctk.ScalingTracker.get_widget_scaling(w)
        except Exception:
            return 1.0

    def altura_minima(self):
        """Px reales: cabecera + ~6 líneas de chat + caja de pregunta (sin el saludo)."""
        s = self.s
        try:
            linea = tkfont.Font(family=self._fam, size=-int(12 * s)).metrics("linespace")
        except Exception:
            linea = int(17 * s)
        chat = LINEAS_MIN * linea + int(20 * s)
        return int(2 * PAD * s + CAB_H * s + 2 * GAP * s + INPUT_H * s) + chat

    def _on_resize(self, e):
        # v1.02: varios <Configure> seguidos (al redimensionar) -> un solo repintado del fondo
        self._pendiente = (e.width, e.height)
        if getattr(self, "_id_resize", None) is None:
            self._id_resize = self.cv.after(16, self._aplicar_resize)

    def _aplicar_resize(self):
        self._id_resize = None
        w, h = self._pendiente
        if w < 50 or h < 50 or (w, h) == self._tam:
            return
        self._tam = (w, h)
        s = self.s
        con_saludo = h - self.altura_minima() >= SALUDO_H * s
        img, g = fondo(w, h, s, con_saludo)
        self._photo = ImageTk.PhotoImage(img)
        cv = self.cv
        cv.delete("deco")
        cv.create_image(0, 0, image=self._photo, anchor="nw", tags="deco")
        cab, p = g["cab"], g["p"]
        cy = (cab[1] + cab[3]) / 2
        x_txt = p + int(56 * s)
        cv.create_text(x_txt, cy - int(8 * s), text="Archie", anchor="w", fill=TEXTO,
                       font=(self._fam, -int(14 * s), "bold"), tags="deco")
        r = 3.5 * s
        cv.create_oval(x_txt + 1, cy + int(9 * s) - r, x_txt + 1 + 2 * r, cy + int(9 * s) + r,
                       fill="#13B66B", outline="", tags="deco")
        cv.create_text(x_txt + int(12 * s), cy + int(9 * s), text="Online", anchor="w", fill=TEXTO_SUAVE,
                       font=(self._fam, -int(11 * s)), tags="deco")
        if con_saludo:
            yc = cab[3] + int(GAP * s) + int(SALUDO_H * s) / 2
            cv.create_text(w / 2, yc, text="Hi I'm Archie\nHow Can I Help You?", justify="center", fill=MAGENTA,
                           font=(self._fam_fina, -int(21 * s)), tags="deco")
        cv.tag_lower("deco")
        pi = int(10 * s)
        cv.coords(self._w_chat, p + pi, g["chat_y0"] + pi)
        cv.itemconfigure(self._w_chat, width=max(10, w - 2 * (p + pi)),
                         height=max(10, g["chat_y1"] - g["chat_y0"] - 2 * pi))
        yi = (g["inp_y0"] + h - p) / 2
        x_send = w - p - int(10 * s)
        cv.coords(self._w_send, x_send, yi)
        cv.coords(self._w_inp, p + int(20 * s), yi)
        cv.itemconfigure(self._w_inp, width=max(10, x_send - int(76 * s) - int(12 * s) - (p + int(20 * s))),
                         height=int(34 * s))


def construir(app, tab):
    """Monta la pestaña y devuelve el PanelAI (altura_minima() para el divisor de MEDIA ASSETS)."""
    return PanelAI(app, tab)
