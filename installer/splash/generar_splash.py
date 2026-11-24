"""Genera la imagen de la pantalla de carga del launcher (installer/splash/splash_<escala>.png).
Uso (desde la raíz del repo): python installer/splash/generar_splash.py Assets installer/splash/fondo_02.png installer/splash
import sys, os
from PIL import Image, ImageDraw, ImageFilter

W, H = 640, 360
ESCALAS = {"100": 1.0, "150": 1.5, "200": 2.0}
# Geometría a escala 1 (la comparte launcher.py para dibujar texto y barra encima)
LOGO_ALTO = 84               # alto del logotipo Archie (centrado en la pantalla)
BARRA = (44, 300, 596, 304)  # pista de la barra de progreso


def blanco(im):
    """Logo de un color -> blanco conservando el alfa."""
    a = im.getchannel("A")
    out = Image.new("RGBA", im.size, (255, 255, 255, 0))
    out.putalpha(a)
    return out


def generar(assets, fondo, salida):
    os.makedirs(salida, exist_ok=True)
    bg0 = Image.open(fondo).convert("RGB")
    logo = blanco(Image.open(os.path.join(assets, "logo_Archie.png")).convert("RGBA"))
    logo = logo.crop(logo.getbbox())
    aim = blanco(Image.open(os.path.join(assets, "logo_aim.png")).convert("RGBA"))
    aim = aim.crop(aim.getbbox())
    for nombre, s in ESCALAS.items():
        w, h = round(W * s), round(H * s)
        # fondo: recorte 16:9 centrado
        r = max(w / bg0.width, h / bg0.height)
        bg = bg0.resize((round(bg0.width * r), round(bg0.height * r)), Image.LANCZOS)
        bg = bg.crop(((bg.width - w) // 2, (bg.height - h) // 2, (bg.width - w) // 2 + w, (bg.height - h) // 2 + h))
        im = bg.convert("RGBA")
        # velo suave abajo para que el texto blanco se lea sobre los rosas claros
        velo = Image.new("L", (1, h))
        for y in range(h):
            velo.putpixel((0, y), int(70 * max(0, (y / h - 0.45) / 0.55) ** 1.4))
        sombra = Image.new("RGBA", (w, h), (120, 0, 70, 0))
        sombra.putalpha(velo.resize((w, h)))
        im.alpha_composite(sombra)

        def pegar(capa, x, y, con_sombra=True):
            if con_sombra:
                m = round(24 * s)  # margen: el desenfoque no puede cortarse en el borde de la capa
                alfa = Image.new("L", (capa.width + 2 * m, capa.height + 2 * m), 0)
                alfa.paste(capa.getchannel("A").point(lambda v: v * 40 // 100), (m, m))
                sh = Image.new("RGBA", alfa.size, (90, 0, 50, 0))
                sh.putalpha(alfa.filter(ImageFilter.GaussianBlur(7 * s)))
                im.alpha_composite(sh, (x - m, y - m + round(4 * s)))
            im.alpha_composite(capa, (x, y))

        # v1.07: solo el logotipo Archie sobre el fondo (sin el icono, que hacía ruido)
        lh = round(LOGO_ALTO * s)
        lg = logo.resize((round(logo.width * lh / logo.height), lh), Image.LANCZOS)
        pegar(lg, (w - lg.width) // 2, (h - lg.height) // 2)  # v1.07: centrado en horizontal y vertical
        ah = round(20 * s)
        ag = aim.resize((round(aim.width * ah / aim.height), ah), Image.LANCZOS)
        pegar(ag, w - ag.width - round(44 * s), round(30 * s), con_sombra=False)
        # pista de la barra (blanco translúcido; el tramo animado lo pinta el launcher)
        pista = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        b = [round(v * s) for v in BARRA]
        ImageDraw.Draw(pista).rounded_rectangle(b, radius=round(2 * s), fill=(255, 255, 255, 80))
        im.alpha_composite(pista)
        im.convert("RGB").save(os.path.join(salida, f"splash_{nombre}.png"), optimize=True)
        print("ok", nombre, w, h)


if __name__ == "__main__":
    generar(*sys.argv[1:4])
