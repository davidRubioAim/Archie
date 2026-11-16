"""
v0.71: MODO MANUAL de la Architecture Preview.

Modelo de la arquitectura editada a mano (mover, duplicar, eliminar y Magnet) sobre los datos que da el motor.

Reglas:
  - Todo es simétrico: GLL <-> GLR y la mitad izquierda del TL <-> la derecha (respecto al centro / CB).
    Se edita una mitad ("lado maestro") y la otra se rehace en espejo. Magnet copia el último lado editado.
  - Cada elemento se queda en su tramo (GL hasta el corner, TL entre corners) y no pisa el Center Board,
    las zonas negras del edge case ni los logos partidos del corner (Corner Logo).
  - Sin solapes: al empujar un elemento contra otro, el de al lado se desplaza (margen mínimo = Margin del tramo).
    Si ya no cabe, las ANIM ZONES se comprimen solas hasta el Squash Limit.
  - LOGO UNDER: los logos forman una rejilla equidistante por tramo y no se mueven. Duplicate / Remove suma o
    resta un logo al tramo y la rejilla se rehace. Las ANIM ZONES se mueven por celdas enteras de la rejilla.

Todo se hace en "espacio izquierdo": las coordenadas de la mitad maestra pasadas a la mitad izquierda
(x' = W - x - w si el maestro es la derecha). Así toda la lógica es la de una sola mitad.
"""
import copy
import math

from edge_cases import reservas_laterales

INF = float("inf")


# ---------------------------------------------------------------------------------------------- cadenas
def _comprimir(items, d, entero):
    """Reduce d (px o celdas) el ancho total de los items, solo de las ANIM ZONES (hasta su minw)."""
    holgura = sum(max(0, it["w"] - it["minw"]) for it in items)
    if d <= 1e-9:
        return True
    if holgura + 1e-6 < d:
        for it in items:
            it["w"] = min(it["w"], it["minw"])
        return False
    if entero:
        d = int(math.ceil(d - 1e-9))
        while d > 0:
            it = max(items, key=lambda i: i["w"] - i["minw"])
            if it["w"] - it["minw"] <= 0:
                return False
            it["w"] -= 1
            d -= 1
        return True
    f = d / float(holgura)
    for it in items:
        it["w"] -= max(0, it["w"] - it["minw"]) * f
    return True


def resolver(items, k1, k2, x_bloque, g, L, R, entero=False):
    """Coloca el bloque items[k1..k2] (separaciones internas fijas) empezando en x_bloque, empujando a los de
    los lados (y comprimiendo ANIM ZONES si hace falta) sin salir de [L, R]. -> lista nueva o None si no cabe."""
    out = [dict(it) for it in items]
    x0 = out[k1]["x"]
    offs = [it["x"] - x0 for it in out[k1:k2 + 1]]
    B = out[k2]["x"] + out[k2]["w"] - x0
    A, C = out[:k1], out[k2 + 1:]
    lo = L + sum(it["minw"] for it in A) + g * len(A)
    hi = R - B - sum(it["minw"] for it in C) - g * len(C)
    if lo > hi + 1e-6:
        return None
    x = min(max(x_bloque, lo), hi)
    if entero:
        x = int(round(x))
        x = min(max(x, int(math.ceil(lo - 1e-9))), int(math.floor(hi + 1e-9)))
    for j, off in enumerate(offs):
        out[k1 + j]["x"] = x + off
    # izquierda: empujar y, si no cabe, comprimir y empaquetar desde L
    lim = x - g
    for it in reversed(A):
        if it["x"] + it["w"] > lim:
            it["x"] = lim - it["w"]
        lim = it["x"] - g
    if A and A[0]["x"] < L - 1e-6:
        if not _comprimir(A, L - A[0]["x"], entero):
            return None
        cx = x - g
        for it in reversed(A):
            it["x"] = cx - it["w"]
            cx = it["x"] - g
    # derecha
    lim = x + B + g
    for it in C:
        if it["x"] < lim:
            it["x"] = lim
        lim = it["x"] + it["w"] + g
    if C and C[-1]["x"] + C[-1]["w"] > R + 1e-6:
        if not _comprimir(C, C[-1]["x"] + C[-1]["w"] - R, entero):
            return None
        cx = x + B + g
        for it in C:
            it["x"] = cx
            cx += it["w"] + g
    return out


def legalizar(items, g, L, R, entero=False):
    """Deja los items (ordenados) sin solapes dentro de [L, R]; comprime ANIM ZONES si no caben. -> bool"""
    if not items:
        return True
    if entero:
        L, R = int(math.ceil(L - 1e-9)), int(math.floor(R + 1e-9))
    lim = L
    for it in items:
        it["x"] = max(it["x"], lim)
        lim = it["x"] + it["w"] + g
    lim = R
    for it in reversed(items):
        if it["x"] + it["w"] > lim:
            it["x"] = lim - it["w"]
        lim = it["x"] - g
    if items[0]["x"] < L - 1e-6:
        total = sum(it["w"] for it in items) + g * (len(items) - 1)
        ok = _comprimir(items, total - (R - L), entero)
        cx = L
        for it in items:
            it["x"] = cx
            cx += it["w"] + g
        return ok
    return True


# ---------------------------------------------------------------------------------------------- modelo
class ManualLayout:
    """Arquitectura en MODO MANUAL. Se crea a partir de los datos del motor (calcular_layout_data)."""

    TIPOS = ("anim_zone", "static_logo", "center_board")

    @staticmethod
    def soportado(datos):
        """-> (True, "") o (False, motivo en inglés)."""
        if not datos or not datos.get("zonas"):
            return False, "Create the architecture first."
        cfg = datos.get("config") or {}
        if cfg.get("amplify"):
            return False, "Manual mode is not available in AMPLIFY mode yet."
        if "ZONA_FALLBACK" in datos["zonas"]:
            return False, "Manual mode needs a static logo (fallback layout)."
        return True, ""

    def __init__(self, datos, anim_nat_w, m_gl, m_tl, stretch_pct):
        dims = datos["dimensiones_globales"]
        z = datos["zonas"]
        self.W = int(dims["width"])
        self.H = int(dims["height"])
        self.gl = int(z["ZONA_1"]["width"])
        self.cb = int(z.get("ZONA_3", {}).get("width") or 0)
        self.cb_start = float(z["ZONA_3"]["x_inicio"]) if self.cb > 0 else self.W / 2.0
        cfg = datos.get("config") or {}
        self.lu = bool(cfg.get("logo_under"))
        edge = cfg.get("edge_case") or {}
        self.negras = [(float(a), float(b)) for a, b in (edge.get("zonas_negras") or [])]
        ri, rd = reservas_laterales(edge, self.W)
        self.lat = float(max(ri, rd))
        self.m = {"GL": max(0.0, float(m_gl)), "TL": max(0.0, float(m_tl if m_tl is not None and m_tl >= 0 else m_gl))}
        self.s = max(0.0, min(0.9, float(stretch_pct or 0) / 100.0))
        self.nat = max(1.0, float(anim_nat_w or 1))
        self.master = {"GL": "L", "TL": "L"}
        self._next = 1
        self.els = []
        for zona in z.values():
            for e in zona.get("elementos", []):
                if e.get("tipo") not in self.TIPOS:
                    continue
                extra = {k: v for k, v in e.items() if k not in ("tipo", "width", "x_absoluta", "offset_x",
                                                                  "logo_under_type", "freeze_on_frame_1")}
                self.els.append({"uid": self._uid(), "tipo": e["tipo"], "x": int(e["x_absoluta"]),
                                 "w": int(e["width"]), "base_w": int(e["width"]), "src": None, "extra": extra})

    # ------------------------------------------------------------------ estado (undo / redo)
    def estado(self):
        return copy.deepcopy(self.__dict__)

    @classmethod
    def desde_estado(cls, st):
        obj = cls.__new__(cls)
        obj.__dict__.update(copy.deepcopy(st))
        return obj

    def clone(self):
        return ManualLayout.desde_estado(self.__dict__)

    def _uid(self):
        u = self._next
        self._next += 1
        return u

    # ------------------------------------------------------------------ clasificación
    def el(self, uid):
        return next((e for e in self.els if e["uid"] == uid), None)

    def _fijo(self, e):
        """Center Board y logos partidos en el corner (a caballo entre GL y TL): no se mueven."""
        if e["tipo"] == "center_board":
            return True
        for b in (self.gl, self.W - self.gl):
            if e["x"] < b - 3 and e["x"] + e["w"] > b + 3:   # 3 px de tolerancia (redondeos de la rejilla)
                return True
        return False

    def _par(self, e):
        c = e["x"] + e["w"] / 2.0
        return "GL" if (c < self.gl or c >= self.W - self.gl) else "TL"

    def _lado(self, e):
        c = e["x"] + e["w"] / 2.0
        if self._par(e) == "TL" and abs(c - self.W / 2.0) <= 1.0:
            return "C"
        return "L" if c < self.W / 2.0 else "R"

    def _compartida(self, par):
        """TL sin Center Board en LOGO UNDER: una sola rejilla para todo el TL."""
        return par == "TL" and self.cb <= 0

    def _lx(self, e, s):
        return float(e["x"]) if s == "L" else float(self.W - e["x"] - e["w"])

    def _region(self, par):
        if par == "GL":
            return self.lat, float(self.gl)
        return float(self.gl), (self.cb_start if self.cb > 0 else self.W / 2.0)

    # ------------------------------------------------------------------ LOGO UNDER: rejilla
    def _logos_lu(self, par, s):
        """Logos de la rejilla del tramo (espacio izquierdo)."""
        res = []
        for e in self.els:
            if e["tipo"] != "static_logo" or self._fijo(e) or self._par(e) != par:
                continue
            if self._compartida(par) or self._lado(e) == s:
                res.append(e)
        return res

    def _rejilla(self, par, s):
        if not self.lu:
            return None
        logos = self._logos_lu(par, s)
        if not logos:
            return None
        xs = sorted(self._lx(e, s) for e in logos)
        lw = float(logos[0]["w"])
        N = len(xs)
        if N >= 2:
            p = (xs[-1] - xs[0]) / float(N - 1)
        else:
            lo, hi = self._region(par) if not self._compartida(par) else (float(self.gl), float(self.W - self.gl))
            c = xs[0] + lw / 2.0
            p = 2.0 * max(lw / 2.0, min(c - lo, hi - c))
        if p < lw * 0.5:
            return None
        L0 = xs[0] - (p - lw) / 2.0
        # "util": la rejilla es más fina que una ANIM ZONE (la de v0.70). Con pocos logos (celdas más anchas que
        # una ANIM ZONE) las ANIM ZONES no se encajan en celdas: se mueven libres como en el modo normal.
        return {"L0": L0, "p": p, "N": N, "lw": lw, "compartida": self._compartida(par), "util": p <= self.nat + 0.5}

    @staticmethod
    def _R_celdas(grid):
        N = grid["N"]
        return (N - 1) // 2 if grid["compartida"] else N

    # ------------------------------------------------------------------ mitades y cadenas
    def _movibles(self, par, s):
        res = []
        for e in self.els:
            if self._par(e) != par or self._fijo(e) or self._lado(e) != s:
                continue
            if self.lu and e["tipo"] == "static_logo":
                continue
            res.append(e)
        return res

    def _minw_px(self, e, w):
        if e["tipo"] != "anim_zone":
            return w
        return min(w, max(1.0, self.nat * (1.0 - self.s)))

    def _cadenas(self, par, s):
        """Cadenas de elementos movibles de la mitad s (espacio izquierdo) entre paredes.
        -> [{"items", "L", "R", "Lraw", "Rraw", "wl", "wr", "g", "celdas", "grid"}]"""
        grid = self._rejilla(par, s)
        movs = self._movibles(par, s)
        axis = [e for e in self.els if self._par(e) == par and not self._fijo(e) and self._lado(e) == "C"
                and not (self.lu and e["tipo"] == "static_logo")]
        g = self.m[par]
        cadenas = []

        # --- ANIM ZONES de LOGO UNDER sobre la rejilla (celdas enteras)
        if grid and grid["util"]:
            p, L0 = grid["p"], grid["L0"]
            Rc = self._R_celdas(grid)
            muros = [(-INF, 0.0, 0.0, 1.0), (Rc, INF, 0.0, 0.5 if grid["compartida"] else 1.0)]
            for e in axis:
                if e["tipo"] == "anim_zone":
                    c0 = int(round((self._lx(e, s) - L0) / p))
                    muros.append((c0, c0 + max(1, int(round(e["w"] / p))), 1.0, 1.0))
            items = []
            for e in movs:
                if e["tipo"] != "anim_zone":
                    continue
                c0 = int(round((self._lx(e, s) - L0) / p))
                m = max(1, int(round(e["w"] / p)))
                mmin = min(m, max(1, int(math.ceil(self.nat * (1.0 - self.s) / p - 1e-6))))
                items.append({"uid": e["uid"], "e": e, "x": c0, "w": m, "minw": mmin,
                              "bw": max(m, int(round(e["base_w"] / p)))})
            cadenas += self._agrupar(items, muros, 1, True, grid)
            movs = [e for e in movs if e["tipo"] != "anim_zone"]

        # --- resto: píxeles
        lo, hi = self._region(par)
        eje = par == "TL" and self.cb <= 0
        muros = [(-INF, lo, g / 2.0, 1.0), (hi, INF, g / 2.0, 0.5 if eje else 1.0)]
        for e in self.els:
            if self._fijo(e) and not (self.lu and e["tipo"] == "static_logo"):   # en LOGO UNDER los logos van debajo
                for a in (float(e["x"]), float(self.W - e["x"] - e["w"])):
                    if a < hi and a + e["w"] > lo:
                        muros.append((a, a + e["w"], g, 1.0))
        for e in axis:
            if not (grid and e["tipo"] == "anim_zone"):
                muros.append((float(e["x"]), float(e["x"] + e["w"]), g, 1.0))
        for a, b in self.negras:
            for x1, x2 in ((a, b), (self.W - b, self.W - a)):
                if x1 < hi and x2 > lo:
                    muros.append((x1, x2, g / 2.0, 1.0))
        items = []
        for e in movs:
            w = float(e["w"])
            items.append({"uid": e["uid"], "e": e, "x": self._lx(e, s), "w": w, "minw": self._minw_px(e, w),
                          "bw": max(float(e["base_w"]), self._minw_px(e, w))})
        cadenas += self._agrupar(items, muros, g, False, None)
        for c in cadenas:
            c["par"] = par
        return cadenas

    @staticmethod
    def _agrupar(items, muros, g, entero, grid):
        grupos = {}
        for it in items:
            c = it["x"] + it["w"] / 2.0
            izq = max((m for m in muros if (m[0] + m[1]) / 2.0 < c), key=lambda m: m[1])
            der = min((m for m in muros if (m[0] + m[1]) / 2.0 >= c), key=lambda m: m[0])
            clave = (izq, der)
            grupos.setdefault(clave, []).append(it)
        res = []
        for (izq, der), its in grupos.items():
            its.sort(key=lambda i: i["x"])
            # si el motor ya los dejó más juntos que el margen, ese es el mínimo de esta cadena (mover no comprime
            # ni recoloca nada por sí solo); los solapes reales sí se corrigen
            huecos = [b["x"] - (a["x"] + a["w"]) for a, b in zip(its, its[1:])]
            gc = max(0, min([g] + huecos))
            gl_ = min(izq[2], max(0, its[0]["x"] - izq[1]))
            gr_ = min(der[2], max(0, der[0] - (its[-1]["x"] + its[-1]["w"])))
            res.append({"items": its, "L": izq[1] + gl_, "R": der[0] - gr_, "Lraw": izq[1], "Rraw": der[0],
                        "wl": izq[3], "wr": der[3], "g": gc, "gmin": g, "celdas": entero, "grid": grid})
        return res

    # ------------------------------------------------------------------ escribir una mitad + espejo
    def _a_px(self, cad, it):
        if cad["celdas"]:
            gr = cad["grid"]
            x = int(round(gr["L0"] + gr["p"] * it["x"]))
            x2 = int(round(gr["L0"] + gr["p"] * (it["x"] + it["w"])))
            if cad.get("par"):   # la rejilla sale de los logos y puede pasarse 1 px del tramo por redondeo
                lo, hi = self._region(cad["par"])
                x, x2 = max(x, int(math.ceil(lo))), min(x2, int(math.floor(hi)))
            return x, max(1, x2 - x)
        return int(round(it["x"])), max(1, int(round(it["w"])))

    def _escribir(self, par, s, cadenas, quitar=()):
        """cadenas: las de _cadenas con items ya resueltos (pueden traer items nuevos con "e" plantilla)."""
        viejos = set(e["uid"] for e in self._movibles(par, s)) | set(quitar)
        self.els = [e for e in self.els if e["uid"] not in viejos]
        mitad = []
        for cad in cadenas:
            for it in cad["items"]:
                x, w = self._a_px(cad, it)
                e = dict(it["e"])
                e["extra"] = dict(e["extra"])
                e["w"] = w
                e["x"] = x if s == "L" else self.W - x - w
                mitad.append(e)
        self.els += mitad
        self._espejar(par, s)
        self.master[par] = s

    def _espejar(self, par, s):
        o = "R" if s == "L" else "L"
        fuente = sorted(self._movibles(par, s), key=lambda e: self._lx(e, s))
        viejos = sorted(self._movibles(par, o), key=lambda e: self._lx(e, o))
        idx_viejos = [e["extra"].get("anim_idx") for e in viejos if e["tipo"] == "anim_zone"]
        ids = set(e["uid"] for e in viejos)
        if self.lu:
            # logos de la rejilla: también en espejo (con rejilla compartida, los del eje se quedan)
            todos = [e for e in self.els if e["tipo"] == "static_logo" and not self._fijo(e) and self._par(e) == par]
            fuente_logos = [e for e in todos if self._lado(e) == s]
            ids |= set(e["uid"] for e in todos if self._lado(e) == o)
        else:
            fuente_logos = []
        self.els = [e for e in self.els if e["uid"] not in ids]
        ia = 0
        for e in list(fuente) + list(fuente_logos):
            m = dict(e)
            m["extra"] = dict(e["extra"])
            m["uid"] = self._uid()
            m["src"] = e["uid"]
            m["x"] = self.W - e["x"] - e["w"]
            if e["tipo"] == "anim_zone":
                if ia < len(idx_viejos) and idx_viejos[ia] is not None:
                    m["extra"]["anim_idx"] = idx_viejos[ia]
                ia += 1
            self.els.append(m)

    # ------------------------------------------------------------------ consultas para la interfaz
    def elementos(self):
        """[(uid, tipo, x, w, src, editable)] para pintar."""
        return [(e["uid"], e["tipo"], e["x"], e["w"], e["src"], self.puede_mover(e["uid"])[0]) for e in self.els]

    def hit(self, x):
        cands = [e for e in self.els if e["x"] <= x < e["x"] + e["w"]]
        if not cands:
            return None
        cands.sort(key=lambda e: {"anim_zone": 0, "static_logo": 1}.get(e["tipo"], 2) if self.lu
                   else {"static_logo": 0, "anim_zone": 1}.get(e["tipo"], 2))
        return cands[0]["uid"]

    def pareja(self, uid):
        e = self.el(uid)
        if e is None:
            return None
        xm = self.W - e["x"] - e["w"]
        best = None
        for o in self.els:
            if o is e or o["tipo"] != e["tipo"]:
                continue
            d = abs(o["x"] - xm) + abs(o["w"] - e["w"])
            if d <= 3 and (best is None or d < best[0]):
                best = (d, o["uid"])
        return best[1] if best else None

    def paso(self, uid):
        """Desplazamiento de una flecha: 1 px, o una celda de la rejilla en LOGO UNDER."""
        e = self.el(uid)
        if e and self.lu and e["tipo"] == "anim_zone":
            gr = self._rejilla(self._par(e), self._lado(e) if self._lado(e) != "C" else "L")
            if gr and gr["util"]:
                return gr["p"]
        return 1.0

    def puede_mover(self, uid):
        e = self.el(uid)
        if e is None:
            return False, ""
        if e["tipo"] == "center_board":
            return False, "The Center Board is fixed."
        if self._fijo(e):
            return False, "Corner logos split between Goal Line and Touchline can't be moved (Remove is allowed)."
        if self.lu and e["tipo"] == "static_logo":
            return False, "LOGO UNDER: logos can't be moved. Right click > Duplicate / Remove."
        if self._lado(e) == "C":
            return False, "Centered element: it stays on the axis (Duplicate splits it in two)."
        return True, ""

    def puede_duplicar(self, uid):
        e = self.el(uid)
        if e is None or e["tipo"] == "center_board":
            return False
        return not self._fijo(e)

    def puede_eliminar(self, uid):
        e = self.el(uid)
        return e is not None and e["tipo"] != "center_board"

    def huecos(self, uid):
        """(hueco izq, hueco der) en px con los vecinos del mismo tramo (para las cotas al arrastrar)."""
        e = self.el(uid)
        if e is None:
            return None, None
        par = self._par(e)
        vec = [o for o in self.els if o is not e and self._par(o) == par
               and not (self.lu and o["tipo"] == "static_logo")]
        izq = [e["x"] - (o["x"] + o["w"]) for o in vec if o["x"] + o["w"] <= e["x"] + 0.5]
        der = [o["x"] - (e["x"] + e["w"]) for o in vec if o["x"] >= e["x"] + e["w"] - 0.5]
        return (min(izq) if izq else None), (min(der) if der else None)

    # ------------------------------------------------------------------ operaciones
    def _buscar(self, cadenas, uid):
        for ci, cad in enumerate(cadenas):
            for k, it in enumerate(cad["items"]):
                if it["uid"] == uid:
                    return ci, k
        return None, None

    def mover(self, uid, dx):
        """Mueve el elemento dx px (en pantalla, en su fila). -> (ok, mensaje)"""
        ok, msg = self.puede_mover(uid)
        if not ok:
            return False, msg
        e = self.el(uid)
        par, s = self._par(e), self._lado(e)
        cads = self._cadenas(par, s)
        ci, k = self._buscar(cads, uid)
        if ci is None:
            return False, ""
        cad = cads[ci]
        d = dx if s == "L" else -dx
        it = cad["items"][k]
        if cad["celdas"]:
            destino = it["x"] + d / cad["grid"]["p"]
        else:
            destino = it["x"] + d
        nuevos = resolver(cad["items"], k, k, destino, cad["g"], cad["L"], cad["R"], cad["celdas"])
        if nuevos is None:
            return False, "No room to move it."
        cad["items"] = nuevos
        self._escribir(par, s, cads)
        return True, ""

    def duplicar(self, uid):
        """-> (ok, mensaje, uid del nuevo)"""
        e = self.el(uid)
        if e is None:
            return False, "", None
        if not self.puede_duplicar(uid):
            return False, ("The Center Board can't be duplicated." if e["tipo"] == "center_board"
                           else "Split corner logos can't be duplicated."), None
        if self.lu and e["tipo"] == "static_logo":
            return self._lu_logos(e, +1)
        par, lado = self._par(e), self._lado(e)
        if lado == "C":
            return self._duplicar_eje(e)
        cads = self._cadenas(par, lado)
        ci, k = self._buscar(cads, uid)
        if ci is None:
            return False, "", None
        cad = cads[ci]
        it = cad["items"][k]
        nuevo_e = dict(e)
        nuevo_e["extra"] = dict(e["extra"])
        nuevo_e["uid"] = self._uid()
        nuevo_e["src"] = None
        g = cad["g"]
        copia = dict(it)
        copia["uid"] = nuevo_e["uid"]
        copia["e"] = nuevo_e
        copia["x"] = it["x"] + it["w"] + g
        items = cad["items"][:k + 1] + [copia] + cad["items"][k + 1:]
        destino = it["x"] - (it["w"] + g) / 2.0
        nuevos = resolver(items, k, k + 1, destino, g, cad["L"], cad["R"], cad["celdas"])
        if nuevos is None:
            return False, "Not enough space to duplicate it here (Squash Limit reached).", None
        cad["items"] = nuevos
        self._escribir(par, lado, cads)
        # el nuevo queda en el lado maestro con su uid si se ha editado la mitad izquierda; si no, el de la derecha
        return True, "", nuevo_e["uid"]

    def _duplicar_eje(self, e):
        """Elemento centrado en el eje del TL (sin CB): se parte en dos, simétricos respecto al centro."""
        par, s = "TL", self.master["TL"]
        estado = self.estado()
        self.els = [o for o in self.els if o["uid"] != e["uid"]]
        cads = self._cadenas(par, s)
        nuevo_e = dict(e)
        nuevo_e["extra"] = dict(e["extra"])
        nuevo_e["uid"] = self._uid()
        nuevo_e["src"] = None
        grid = self._rejilla(par, s) if (self.lu and e["tipo"] == "anim_zone") else None
        if grid and not grid["util"]:
            grid = None
        if grid:
            m = max(1, int(round(e["w"] / grid["p"])))
            Rc = self._R_celdas(grid)
            it = {"uid": nuevo_e["uid"], "e": nuevo_e, "x": Rc - m, "w": m, "bw": m,
                  "minw": min(m, max(1, int(math.ceil(self.nat * (1.0 - self.s) / grid["p"] - 1e-6))))}
            celdas = True
        else:
            w = float(e["w"])
            it = {"uid": nuevo_e["uid"], "e": nuevo_e, "x": self.W / 2.0 - self.m["TL"] / 2.0 - w, "w": w,
                  "minw": self._minw_px(e, w), "bw": max(float(e["base_w"]), self._minw_px(e, w))}
            celdas = False
        cand = [c for c in cads if c["celdas"] == celdas]
        if not cand:
            # mitad vacía: cadena entre el inicio de la región y el eje
            lo, hi = self._region(par)
            if celdas:
                cand = [{"items": [], "L": 0, "R": self._R_celdas(grid), "Lraw": 0, "Rraw": self._R_celdas(grid),
                         "wl": 1.0, "wr": 0.5, "g": 1, "celdas": True, "grid": grid}]
            else:
                g = self.m["TL"]
                cand = [{"items": [], "L": lo + g / 2.0, "R": hi - g / 2.0, "Lraw": lo, "Rraw": hi, "wl": 1.0,
                         "wr": 0.5, "g": g, "celdas": False, "grid": None}]
            cads += cand
        cad = max(cand, key=lambda c: c["R"])
        items = cad["items"] + [it]
        k = len(items) - 1
        nuevos = resolver(items, k, k, it["x"], cad["g"], cad["L"], cad["R"], celdas)
        if nuevos is None:
            self.__dict__.update(copy.deepcopy(estado))
            return False, "Not enough space to split it (Squash Limit reached).", None
        cad["items"] = nuevos
        self._escribir(par, s, cads)
        return True, "", nuevo_e["uid"]

    def eliminar(self, uid):
        e = self.el(uid)
        if e is None:
            return False, ""
        if e["tipo"] == "center_board":
            return False, "The Center Board can't be removed."
        if self.lu and e["tipo"] == "static_logo" and not self._fijo(e):
            ok, msg, _ = self._lu_logos(e, -1)
            return ok, msg
        if self._fijo(e) or self._lado(e) == "C":
            otro = self.pareja(uid) if self._fijo(e) else None
            self.els = [o for o in self.els if o["uid"] not in (uid, otro)]
            return True, ""
        par, s = self._par(e), self._lado(e)
        cads = self._cadenas(par, s)
        for cad in cads:
            cad["items"] = [it for it in cad["items"] if it["uid"] != uid]
        self._escribir(par, s, cads, quitar=(uid,))
        return True, ""

    def _lu_logos(self, e, signo):
        """LOGO UNDER: suma / resta logos a la rejilla del tramo y vuelve a encajar las ANIM ZONES."""
        par = self._par(e)
        lado = self._lado(e)
        s = self.master[par] if lado == "C" else lado
        grid = self._rejilla(par, s)
        if grid is None:
            return False, "", None
        paso = 1 if (not grid["compartida"] or lado == "C") else 2
        N1 = grid["N"] + signo * paso
        if N1 < 1:
            return False, "LOGO UNDER: each section needs at least one logo.", None
        L0, p0 = grid["L0"], grid["p"]
        L1 = L0 + grid["N"] * p0
        # la rejilla nueva no sale del tramo (con pocos logos la deducida puede pasar del corner)
        lo, hi = self._region(par) if not grid["compartida"] else (float(self.gl), float(self.W - self.gl))
        L0, L1 = max(L0, lo), min(L1, hi)
        p = (L1 - L0) / float(N1)
        if p < grid["lw"]:
            return False, "No room for another logo in this section.", None
        estado = self.estado()
        lw = grid["lw"]
        # 1. ANIM ZONES de la mitad (y del eje si la rejilla es compartida), en px del espacio izquierdo
        anims = [o for o in self._movibles(par, s) if o["tipo"] == "anim_zone"]
        eje = [o for o in self.els if o["tipo"] == "anim_zone" and self._par(o) == par and self._lado(o) == "C"
               and not self._fijo(o)] if grid["compartida"] else []
        # 2. logos nuevos (espacio izquierdo)
        viejos_logos = self._logos_lu(par, s)
        plantilla = viejos_logos[0] if viejos_logos else e
        self.els = [o for o in self.els if o["uid"] not in set(x["uid"] for x in viejos_logos)]
        for i in range(N1):
            lx = L0 + p * i + (p - lw) / 2.0
            n = dict(plantilla)
            n["extra"] = dict(plantilla["extra"])
            n["uid"] = self._uid()
            n["src"] = None
            n["w"] = int(lw)
            x = int(round(lx))
            n["x"] = x if s == "L" else self.W - x - n["w"]
            self.els.append(n)
        # 3. re-encajar ANIM ZONES en la rejilla nueva (si es más gruesa que una ANIM ZONE, se quedan donde están)
        g2 = {"L0": L0, "p": p, "N": N1, "lw": lw, "compartida": grid["compartida"], "util": p <= self.nat + 0.5}
        if not g2["util"]:
            self._escribir(par, s, self._cadenas(par, s))
            return True, "", None
        Rc = self._R_celdas(g2)
        mmin_c = max(1, int(math.ceil(self.nat * (1.0 - self.s) / p - 1e-6)))
        mmax_c = max(1, int(math.floor((self.nat + 0.5) / p)))

        def celdas_de(o, centrado=False):
            tw = float(o["base_w"])
            m = int(round(tw / p))
            if mmin_c <= mmax_c:
                m = max(mmin_c, min(m, mmax_c))
            else:   # ningún número de celdas entra en el Squash Limit: el que menos se aleja
                lim_lo, lim_hi = self.nat * (1.0 - self.s), self.nat
                m = min((mmax_c, mmin_c), key=lambda k: max(lim_lo - k * p, k * p - lim_hi, 0.0))
            m = max(1, m)
            if centrado and (N1 - m) % 2:
                m = m - 1 if m - 1 >= max(1, mmin_c) else m + 1
            return m

        for o in eje:
            m = celdas_de(o, centrado=True)
            c0 = (N1 - m) // 2
            x = int(round(L0 + p * c0))
            o["w"] = max(1, int(round(L0 + p * (c0 + m))) - x)
            o["x"] = x if s == "L" else self.W - x - o["w"]
        items = []
        for o in sorted(anims, key=lambda o: self._lx(o, s)):
            m = celdas_de(o)
            cc = (self._lx(o, s) + o["w"] / 2.0 - L0) / p
            items.append({"uid": o["uid"], "e": o, "x": int(round(cc - m / 2.0)), "w": m,
                          "minw": min(m, mmin_c), "bw": m})
        muros_R = Rc
        for o in eje:
            c0 = int(round((self._lx(o, s) - L0) / p))
            muros_R = min(muros_R, c0 - 1)
        if not legalizar(items, 1, 0, muros_R, True):
            self.__dict__.update(copy.deepcopy(estado))
            return False, "The ANIM ZONES don't fit on the new logo grid.", None
        cad = {"items": items, "celdas": True, "grid": g2, "par": par}
        self._escribir(par, s, [cad])
        return True, "", None

    def magnet(self):
        """Iguala los huecos de cada tramo (mismo orden y cantidad) y copia el último lado editado al otro."""
        for par in ("GL", "TL"):
            s = self.master[par]
            cads = self._cadenas(par, s)
            for cad in cads:
                its = cad["items"]
                if not its:
                    continue
                n = len(its)
                ent = cad["celdas"]
                if ent:
                    gr = cad["grid"]
                    Lraw, Rraw = float(cad["Lraw"]), float(cad["Rraw"])
                    if gr["compartida"] and cad["Rraw"] == self._R_celdas(gr):
                        Rraw = gr["N"] / 2.0   # el eje de verdad: el hueco con la ANIM ZONE espejo queda igual
                    total = sum(it["w"] for it in its)
                    den = (n - 1) + cad["wl"] + cad["wr"]
                    h = (Rraw - Lraw - total) / den if den > 0 else 0.0
                    x = Lraw + cad["wl"] * h
                    for it in its:
                        it["x"] = int(round(x))
                        x += it["w"] + h
                    legalizar(its, 1, cad["L"], cad["R"], True)
                    continue
                g = cad.get("gmin", cad["g"])
                for it in its:
                    it["w"] = it["bw"]
                span = cad["Rraw"] - cad["Lraw"]
                den = (n - 1) + cad["wl"] + cad["wr"]
                h = (span - sum(it["w"] for it in its)) / den if den > 0 else 0.0
                if h < g:
                    _comprimir(its, g * den - (span - sum(it["w"] for it in its)), False)
                    h = max(0.0, (span - sum(it["w"] for it in its)) / den) if den > 0 else 0.0
                x = cad["Lraw"] + cad["wl"] * h
                for it in its:
                    it["x"] = x
                    x += it["w"] + h
                legalizar(its, min(g, cad["g"]) if h < g else g, cad["L"], cad["R"], False)
            self._escribir(par, s, cads)
        return True, ""

    # ------------------------------------------------------------------ salida
    def a_datos(self, base):
        """Datos con el formato del motor (zonas/elementos) a partir de base (los datos automáticos actuales)."""
        d = dict(base)
        zonas = {}
        for zn, z in base["zonas"].items():
            zonas[zn] = {k: v for k, v in z.items() if k != "elementos"}
            zonas[zn]["elementos"] = []
        anims = [(e["x"], e["x"] + e["w"]) for e in self.els if e["tipo"] == "anim_zone"]
        for e in sorted(self.els, key=lambda e: e["x"]):
            c = e["x"] + e["w"] / 2.0
            if c < self.gl:
                zn = "ZONA_1"
            elif c >= self.W - self.gl:
                zn = "ZONA_5"
            elif e["tipo"] == "center_board":
                zn = "ZONA_3"
            elif self.cb > 0:
                zn = "ZONA_2" if c < self.cb_start else "ZONA_4"
            else:
                zn = "ZONA_2" if c <= self.W / 2.0 else "ZONA_4"
            if zn not in zonas:
                zn = "ZONA_2"
            out = dict(e["extra"])
            out.update({"tipo": e["tipo"], "width": int(e["w"]), "x_absoluta": int(e["x"])})
            if e["tipo"] == "static_logo" and self.lu:
                tapado = any(e["x"] < b and e["x"] + e["w"] > a for a, b in anims)
                out["logo_under_type"] = "animated" if tapado else "frozen"
                out["freeze_on_frame_1"] = not tapado
            zonas[zn]["elementos"].append(out)
        d["zonas"] = zonas
        d["warnings"] = []
        d["manual"] = True
        return d
