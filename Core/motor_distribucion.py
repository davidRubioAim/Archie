import math
import os
import traceback
from moviepy import VideoFileClip
from PIL import Image, ImageDraw, ImageFont
from log_utils import debug, log_error
from frame_cache import FRAME_CACHE


class Distribuidor:

  def __init__(
      self, w_total, h_total, goal_line, cb_width, margin, archivos, config
  ):
    debug(
        f"Inicializando Motor: w_total={w_total}, config={config}"
    )
    self.w_total = w_total
    self.h_total = h_total
    self.goal_line = goal_line
    self.cb_width = cb_width
    self.margin = margin
    self.archivos = archivos
    self.config = config
    self.anim_counter = 0

    self.logo_w, self.logo_h = self.get_dimensions(
        archivos.get("static_logo"), 1000, 200
    )

    self.anim_files = [f for f in archivos.get("anim_zones", []) if f is not None]
    if not self.anim_files and archivos.get("anim_zone"):
      self.anim_files = [archivos["anim_zone"]]

    self.anim_w, self.anim_h = self.get_dimensions(
        self.anim_files[0] if self.anim_files else None, 3000, 200
    )

    if self.logo_h > 0:
      self.logo_w = int(self.logo_w * (self.h_total / self.logo_h))
    self.logo_w = max(1, self.logo_w)

    if self.anim_h > 0:
      self.anim_w = int(self.anim_w * (self.h_total / self.anim_h))
    self.anim_w = max(1, self.anim_w)

    debug(
        f"Dimensiones Motor calculadas: logo_w={self.logo_w},"
        f" anim_w={self.anim_w}"
    )

  def get_dimensions(self, path, def_w, def_h):
    if not path:
      return def_w, def_h
    # Ruta rápida: metadatos cacheados (evita abrir un VideoFileClip en cada cálculo)
    try:
      w, h = FRAME_CACHE.dimensions(path)
      if w and h:
        return w, h
    except Exception as e:
      debug(f"FrameCache sin dimensiones para '{path}', usando lectura directa: {e}")
    ext = os.path.splitext(path)[1].lower()
    if ext in [".mov", ".mp4", ".avi", ".m4v"]:
      try:
        with VideoFileClip(path) as cl:
          return cl.size
      except Exception as e:
        log_error(f"Error leyendo dim Video: {e}")
        return def_w, def_h
    else:
      try:
        with Image.open(path) as im:
          return im.size
      except Exception as e:
        log_error(f"Error leyendo dim Image: {e}")
        return def_w, def_h

  def get_anim_idx(self):
    idx = self.anim_counter
    self.anim_counter += 1
    return idx

  def place_logos(self, x_start, qty):
    elements = []
    cx = x_start
    for _ in range(qty):
      elements.append({
          "tipo": "static_logo",
          "width": self.logo_w,
          "x_absoluta": int(cx),
      })
      cx += self.logo_w + self.margin
    return elements

  def fill_space(self, w_avail, max_anims=None):
    stretch = self.config.get("stretch_limit", 15) / 100.0
    squash_logos = self.config.get("squash_logos", False)

    max_k = int(w_avail // (self.anim_w + self.margin)) + 2

    if max_anims is not None and max_anims >= 0:
      if max_anims == 0:
        return self._fill_only_logos(w_avail)
      max_k = min(max_k, max_anims - 1)

    max_k = max(0, max_k)

    for k in range(max_k, -1, -1):
      req_anim = (k + 1) * self.anim_w
      req_logo = k * self.w_tl
      req_marg = (2 * k) * self.margin

      if req_anim + req_logo + req_marg <= w_avail:
        gap = w_avail - (req_anim + req_logo + req_marg)
        return self._build_fill_list(k, 1.0, 1.0, gap)
      elif stretch > 0:
        target_w = w_avail - req_marg
        if squash_logos:
          S = (
              target_w / (req_anim + req_logo)
              if (req_anim + req_logo) > 0
              else 1.0
          )
          if S >= (1.0 - stretch) and S > 0:
            return self._build_fill_list(k, S, S, 0)
        else:
          if req_anim > 0:
            S_anim = (target_w - req_logo) / req_anim
            if S_anim >= (1.0 - stretch) and S_anim > 0:
              return self._build_fill_list(k, S_anim, 1.0, 0)
    return []

  def _fill_only_logos(self, w_avail):
    if w_avail <= 0 or self.logo_w <= 0:
      return []
    N = int(w_avail // (self.logo_w + self.margin))
    if N <= 0:
      return []
    S = (w_avail - (N * self.logo_w)) / float(N)
    elements = []
    for i in range(N):
      pos = (S / 2.0) + i * (self.logo_w + S)
      elements.append({
          "tipo": "static_logo",
          "width": self.logo_w,
          "offset_x": pos,
          "is_fill": True,
      })
    return elements

  def _build_fill_list(self, k, S_anim, S_logo, gap):
    elements = []
    w_a = int(self.anim_w * S_anim)
    w_l_individual = int(self.logo_w * S_logo)

    total_items = (k + 1) + (k * self.qty_tl)
    extra_gap = gap / float(total_items) if total_items > 0 else 0
    cx = extra_gap / 2.0

    for i in range(k + 1):
      elements.append({
          "tipo": "anim_zone",
          "width": w_a,
          "offset_x": cx,
          "anim_idx": self.get_anim_idx(),
      })
      cx += w_a + self.margin + extra_gap
      if i < k:
        for _ in range(self.qty_tl):
          elements.append({
              "tipo": "static_logo",
              "width": w_l_individual,
              "offset_x": cx,
              "is_fill": True,
          })
          cx += w_l_individual + self.margin + extra_gap
    return elements

  def place_fill(self, w_avail, start_x, max_anims=None):
    if w_avail <= 0:
      return []
    elements = self.fill_space(w_avail, max_anims=max_anims)
    for e in elements:
      e["x_absoluta"] = int(start_x + e.get("offset_x", 0))
    return elements

  def _aplicar_stretch_fit(self, zonas, warnings, cb_start, cb_end):
    if not self.config.get("stretch_fit_per", False):
      return

    elementos_izq = []
    elementos_der = []

    for zona in zonas.values():
      for e in zona["elementos"]:
        if e["tipo"] == "center_board":
          continue

        if self.cb_width > 0:
          centro_e = e["x_absoluta"] + (e["width"] / 2.0)
          if centro_e >= cb_end:
            elementos_der.append(e)
          elif centro_e <= cb_start:
            elementos_izq.append(e)
          else:
            if centro_e < (self.w_total / 2.0):
              elementos_izq.append(e)
            else:
              elementos_der.append(e)
        else:
          elementos_izq.append(e)

    max_distorsion = 1.0

    def procesar_bloque(elementos, bound_left, bound_right):
      nonlocal max_distorsion
      if not elementos:
        return

      elementos.sort(key=lambda e: e["x_absoluta"])
      nuevos_datos = []

      for i, e in enumerate(elementos):
        orig_x = e["x_absoluta"]
        orig_w = e["width"]
        orig_end = orig_x + orig_w

        if i == 0:
          espacio_izq = orig_x - bound_left
          crecimiento_izq = max(0, espacio_izq)
        else:
          prev_end = elementos[i - 1]["x_absoluta"] + elementos[i - 1]["width"]
          espacio_izq = orig_x - prev_end
          crecimiento_izq = max(0, espacio_izq / 2.0)

        if i == len(elementos) - 1:
          espacio_der = bound_right - orig_end
          crecimiento_der = max(0, espacio_der)
        else:
          next_x = elementos[i + 1]["x_absoluta"]
          espacio_der = next_x - orig_end
          crecimiento_der = max(0, espacio_der / 2.0)

        nuevo_x = int(orig_x - crecimiento_izq)
        nuevo_end = int(orig_end + crecimiento_der)
        nuevo_w = nuevo_end - nuevo_x

        nuevos_datos.append({"x_absoluta": nuevo_x, "width": nuevo_w})

      for i, e in enumerate(elementos):
        orig_w = e["width"]
        e["x_absoluta"] = nuevos_datos[i]["x_absoluta"]
        e["width"] = nuevos_datos[i]["width"]

        if orig_w > 0:
          dist = e["width"] / float(orig_w)
          if dist > max_distorsion:
            max_distorsion = dist

    bound_izq = 0
    bound_der_izq = cb_start if self.cb_width > 0 else self.w_total
    procesar_bloque(elementos_izq, bound_izq, bound_der_izq)

    if self.cb_width > 0:
      bound_der = cb_end
      bound_der_der = self.w_total
      procesar_bloque(elementos_der, bound_der, bound_der_der)

    if max_distorsion > 1.15 and "stretch_fit_15" not in warnings:
      warnings.append("stretch_fit_15")

  def procesar(self):
    try:
      debug("Iniciando procesar()...")
      cb_start = (self.w_total / 2) - (self.cb_width / 2)
      cb_end = cb_start + self.cb_width

      z1_w = self.goal_line
      z2_start = z1_w
      z2_w = cb_start - z2_start
      z4_start = cb_end
      z4_w = (self.w_total - self.goal_line) - z4_start
      z5_start = self.w_total - self.goal_line
      z5_w = self.goal_line

      if self.config.get("amplify", False):
        debug("Entrando a modo procesar_amplify (Flexbox)...")
        return self.procesar_amplify(
            cb_start,
            cb_end,
            z1_w,
            z2_start,
            z2_w,
            z4_start,
            z4_w,
            z5_start,
            z5_w,
        )

      if not self.archivos.get("static_logo"):
        debug(
            "Entrando a modo procesar_fallback (sin logo)..."
        )
        return self.procesar_fallback(
            cb_start,
            cb_end,
            z1_w,
            z2_start,
            z2_w,
            z4_start,
            z4_w,
            z5_start,
            z5_w,
        )

      zonas = {
          "ZONA_1": {"x_inicio": 0, "width": z1_w, "elementos": []},
          "ZONA_2": {
              "x_inicio": int(z2_start),
              "width": int(z2_w),
              "elementos": [],
          },
          "ZONA_3": {
              "x_inicio": int(cb_start),
              "width": self.cb_width,
              "elementos": [],
          },
          "ZONA_4": {
              "x_inicio": int(z4_start),
              "width": int(z4_w),
              "elementos": [],
          },
          "ZONA_5": {
              "x_inicio": int(z5_start),
              "width": int(z5_w),
              "elementos": [],
          },
      }

      warnings = []
      if self.cb_width > 0:
        zonas["ZONA_3"]["elementos"].append({
            "tipo": "center_board",
            "width": self.cb_width,
            "x_absoluta": int(cb_start),
        })

      qty_goal = self.config.get("qty_goal", 1) or 0
      qty_corner = self.config.get("qty_corner", 1) or 0
      qty_cb = self.config.get("qty_cb", 1) or 0
      self.qty_tl = self.config.get("qty_tl", 1) or 0
      corner_logo_split = self.config.get("corner_logo", False)

      # --- BLINDAJE ARCHIE: Si Logo Under está ON, anulamos esquinas por defecto ---
      if self.config.get("logo_under", False) and not corner_logo_split:
        qty_corner = 0

      max_anim_gl = self.config.get("max_anim_gl", 0)
      max_anim_tl = self.config.get("max_anim_tl", 0)
      cap_gl = max_anim_gl if max_anim_gl > 0 else None
      cap_tl = max_anim_tl if max_anim_tl > 0 else None

      w_goal = (
          qty_goal * self.logo_w + max(0, qty_goal - 1) * self.margin
          if qty_goal > 0
          else 0
      )
      w_cb = (
          qty_cb * self.logo_w + max(0, qty_cb - 1) * self.margin
          if qty_cb > 0
          else 0
      )
      self.w_tl = (
          self.qty_tl * self.logo_w + max(0, self.qty_tl - 1) * self.margin
          if self.qty_tl > 0
          else 0
      )
      w_corner = (
          qty_corner * self.logo_w + max(0, qty_corner - 1) * self.margin
          if qty_corner > 0
          else 0
      )

      hide_extreme_edges = self.config.get("hide_extreme_edges", False)
      m_half = self.margin / 2.0

      if not hide_extreme_edges and qty_corner > 0:
        zonas["ZONA_1"]["elementos"].extend(
            self.place_logos(m_half, qty_corner)
        )
        zonas["ZONA_5"]["elementos"].extend(
            self.place_logos(
                (self.w_total - m_half) - w_corner, qty_corner
            )
        )
        z1_left_bound = m_half + w_corner
        z5_right_bound = (self.w_total - m_half) - w_corner
      else:
        z1_left_bound = m_half
        z5_right_bound = self.w_total - m_half

      if corner_logo_split and qty_corner > 0:
        c1_start = z1_w - (w_corner / 2.0)
        c1_end = z1_w + (w_corner / 2.0)
        c2_start = z5_start - (w_corner / 2.0)
        c2_end = z5_start + (w_corner / 2.0)

        zonas["ZONA_1"]["elementos"].extend(
            self.place_logos(c1_start, qty_corner)
        )
        zonas["ZONA_4"]["elementos"].extend(
            self.place_logos(c2_start, qty_corner)
        )
      elif qty_corner > 0:
        c1_start = (z1_w - m_half) - w_corner
        c1_end_logo = z2_start + m_half
        c2_start = (z5_start - m_half) - w_corner
        c2_end_logo = z5_start + m_half

        zonas["ZONA_1"]["elementos"].extend(
            self.place_logos(c1_start, qty_corner)
        )
        zonas["ZONA_2"]["elementos"].extend(
            self.place_logos(c1_end_logo, qty_corner)
        )
        zonas["ZONA_4"]["elementos"].extend(
            self.place_logos(c2_start, qty_corner)
        )
        zonas["ZONA_5"]["elementos"].extend(
            self.place_logos(c2_end_logo, qty_corner)
        )

        c1_end = c1_end_logo + w_corner
        c2_end = c2_end_logo + w_corner
      else:
        c1_start = z1_w - m_half
        c1_end = z2_start + m_half
        c2_start = z5_start - m_half
        c2_end = z5_start + m_half

      gl_offset_pct = self.config.get("gl_offset", 0) / 100.0
      anchor_1 = z1_w * (0.5 + gl_offset_pct)

      if qty_goal > 0:
        gl1_start = anchor_1 - (w_goal / 2.0)
        gl1_end = anchor_1 + (w_goal / 2.0)
        zonas["ZONA_1"]["elementos"].extend(
            self.place_logos(gl1_start, qty_goal)
        )
        w_avail_1L = gl1_start - z1_left_bound
        w_avail_1R = c1_start - gl1_end
      else:
        gl1_start = anchor_1
        gl1_end = anchor_1
        w_avail_1L = c1_start - z1_left_bound
        w_avail_1R = 0

      if w_avail_1L < 0 or (qty_goal > 0 and w_avail_1R < 0):
        warnings.append("goal_overlap")

      zonas["ZONA_1"]["elementos"].extend(
          self.place_fill(w_avail_1L, z1_left_bound, max_anims=cap_gl)
      )
      if qty_goal > 0:
        zonas["ZONA_1"]["elementos"].extend(
            self.place_fill(w_avail_1R, gl1_end, max_anims=cap_gl)
        )

      if self.cb_width == 0:
        cb_center = self.w_total / 2.0
        if qty_cb > 0:
          cb_cluster_start = cb_center - (w_cb / 2.0)
          cb_cluster_end = cb_center + (w_cb / 2.0)
          zonas["ZONA_2"]["elementos"].extend(
              self.place_logos(cb_cluster_start, qty_cb)
          )
          w_avail_2 = cb_cluster_start - c1_end
          w_avail_4 = c2_start - cb_cluster_end
          tl_left_end = cb_cluster_start
          tl_right_start = cb_cluster_end
        else:
          w_avail_2 = c2_start - c1_end
          w_avail_4 = 0
          tl_left_end = cb_center
          tl_right_start = cb_center

        if w_avail_2 < 0 or (qty_cb > 0 and w_avail_4 < 0):
          warnings.append("touchline_overlap")
        zonas["ZONA_2"]["elementos"].extend(
            self.place_fill(w_avail_2, c1_end, max_anims=cap_tl)
        )
        if qty_cb > 0:
          zonas["ZONA_4"]["elementos"].extend(
              self.place_fill(w_avail_4, cb_cluster_end, max_anims=cap_tl)
          )
      else:
        if qty_cb > 0:
          cb_logo_z2_start = (cb_start - m_half) - w_cb
          cb_logo_z4_start = cb_end + m_half
          zonas["ZONA_2"]["elementos"].extend(
              self.place_logos(cb_logo_z2_start, qty_cb)
          )
          zonas["ZONA_4"]["elementos"].extend(
              self.place_logos(cb_logo_z4_start, qty_cb)
          )

          w_avail_2 = cb_logo_z2_start - c1_end
          w_avail_4 = c2_start - (cb_logo_z4_start + w_cb)
          tl_left_end = cb_logo_z2_start
          tl_right_start = cb_logo_z4_start + w_cb
        else:
          w_avail_2 = (cb_start - m_half) - c1_end
          w_avail_4 = c2_start - (cb_end + m_half)
          tl_left_end = cb_start - m_half
          tl_right_start = cb_end + m_half

        if w_avail_2 < 0 or w_avail_4 < 0:
          warnings.append("touchline_overlap")
        zonas["ZONA_2"]["elementos"].extend(
            self.place_fill(w_avail_2, c1_end, max_anims=cap_tl)
        )
        zonas["ZONA_4"]["elementos"].extend(
            self.place_fill(w_avail_4, tl_right_start, max_anims=cap_tl)
        )

      anchor_5 = z5_w * (0.5 - gl_offset_pct)
      anchor_5_abs = z5_start + anchor_5

      if qty_goal > 0:
        gl5_start = anchor_5_abs - (w_goal / 2.0)
        gl5_end = anchor_5_abs + (w_goal / 2.0)
        zonas["ZONA_5"]["elementos"].extend(
            self.place_logos(gl5_start, qty_goal)
        )
        w_avail_5L = gl5_start - c2_end
        w_avail_5R = z5_right_bound - gl5_end
      else:
        gl5_start = anchor_5_abs
        gl5_end = anchor_5_abs
        w_avail_5L = z5_right_bound - c2_end
        w_avail_5R = 0

      if w_avail_5L < 0 or (qty_goal > 0 and w_avail_5R < 0):
        warnings.append("goal_overlap")

      zonas["ZONA_5"]["elementos"].extend(
          self.place_fill(w_avail_5L, c2_end, max_anims=cap_gl)
      )
      if qty_goal > 0:
        zonas["ZONA_5"]["elementos"].extend(
            self.place_fill(w_avail_5R, gl5_end, max_anims=cap_gl)
        )

      # --- 6. POST-PROCESADO LOGO UNDER (Con Space-Around proporcional PURO POR ZONA) ---
      if self.config.get("logo_under"):
        debug(
            "Iniciando post-procesado Logo Under PURGADO Y"
            " EQUIDISTANTE..."
        )
        try:
          # 1. PURGA TOTAL: En modo Logo Under, eliminamos TODOS los static_logos previos de las zonas
          # (ya sean de relleno 'is_fill', de porterías 'qty_goal', o de clústeres de touchline).
          # Respetamos únicamente el Center Board (Zona 3) y las Anim Zones ya calculadas.
          for z_name in [
              "ZONA_1",
              "ZONA_2",
              "ZONA_4",
              "ZONA_5",
          ]:  # No tocamos ZONA_3 (Center Board)
            zonas[z_name]["elementos"] = [
                e
                for e in zonas[z_name]["elementos"]
                if e["tipo"] != "static_logo"
            ]

          N_gl = int(self.config.get("qty_gl_under", 1))
          N_tl = int(self.config.get("qty_tl_under", 1))

          def gen_equi(x1, x2, N):
            res = []
            if N <= 0 or x2 <= x1:
              return res
            w_avail = x2 - x1
            max_possible = math.floor(w_avail / self.logo_w)
            N = min(N, max_possible)
            if N <= 0:
              return res
            if N == 1:
              res.append({
                  "tipo": "static_logo",
                  "width": self.logo_w,
                  "x_absoluta": int(x1 + (w_avail - self.logo_w) / 2),
              })
            else:
              # Space-Around puro: Separación S y S/2 en los extremos
              S = (w_avail - N * self.logo_w) / float(N)
              S = max(0.0, S)
              for i in range(N):
                pos = x1 + (S / 2.0) + i * (self.logo_w + S)
                res.append({
                    "tipo": "static_logo",
                    "width": self.logo_w,
                    "x_absoluta": int(pos),
                })
            return res

          # Coordenadas limpias de fronteras físicas por zona
          lu_z1_start = (
              z1_left_bound
              if (not hide_extreme_edges and qty_corner > 0)
              else 0
          )
          lu_c1_start = c1_start if qty_corner > 0 else z1_w
          lu_c1_end = c1_end if qty_corner > 0 else z2_start
          lu_c2_start = c2_start if qty_corner > 0 else z5_start
          lu_c2_end = c2_end if qty_corner > 0 else z5_start
          lu_z5_end = (
              z5_right_bound
              if (not hide_extreme_edges and qty_corner > 0)
              else self.w_total
          )

          # 2. GENERACIÓN UNIFORME PURA SIN SUBDIVISIÓN DE PORTERÍAS NI CLÚSTERES
          # ZONA 1: De extremo izquierdo hasta frontera de corner/zona 2
          zonas["ZONA_1"]["elementos"].extend(
              gen_equi(lu_z1_start, lu_c1_start, N_gl)
          )

          # ZONA 2: Desde frontera de corner/zona 1 hasta el Center Board (o mitad)
          zonas["ZONA_2"]["elementos"].extend(
              gen_equi(lu_c1_end, tl_left_end, N_tl)
          )

          # ZONA 4: Desde el Center Board (o mitad) hasta frontera de corner/zona 5
          zonas["ZONA_4"]["elementos"].extend(
              gen_equi(tl_right_start, lu_c2_start, N_tl)
          )

          # ZONA 5: De frontera de corner/zona 4 hasta extremo derecho
          zonas["ZONA_5"]["elementos"].extend(
              gen_equi(lu_c2_end, lu_z5_end, N_gl)
          )

          # 3. Clasificación de 'animated' o 'frozen' según colisión con Anim Zones
          all_anims = []
          for z_name, z_data in zonas.items():
            for e in z_data["elementos"]:
              if e["tipo"] == "anim_zone":
                all_anims.append(e)

          for z_name, z_data in [
              ("ZONA_1", zonas["ZONA_1"]),
              ("ZONA_2", zonas["ZONA_2"]),
              ("ZONA_4", zonas["ZONA_4"]),
              ("ZONA_5", zonas["ZONA_5"]),
          ]:
            for e in z_data["elementos"]:
              if e["tipo"] == "static_logo":
                logo_start = e["x_absoluta"]
                logo_end = logo_start + e["width"]
                is_anim = False
                for az in all_anims:
                  az_s = az["x_absoluta"]
                  az_e = az_s + az["width"]
                  if logo_start < az_e and logo_end > az_s:
                    is_anim = True
                    break
                e["logo_under_type"] = "animated" if is_anim else "frozen"
                e["freeze_on_frame_1"] = not is_anim

          for z_name in zonas.keys():
            zonas[z_name]["elementos"].sort(key=lambda e: e["x_absoluta"])

        except Exception as e:
          traceback.print_exc()
          log_error(
              f"Error CRÍTICO dentro del bloque Logo Under: {e}"
          )

      if not self.config.get("logo_under"):
        self._aplicar_stretch_fit(zonas, warnings, cb_start, cb_end)

      return {
          "proyecto": "AE_EXPORT",
          "dimensiones_globales": {"width": self.w_total, "height": self.h_total},
          "archivos_fuente": self.archivos,
          "zonas": zonas,
          "warnings": warnings,
      }
    except Exception as e:
      traceback.print_exc()
      log_error(f"Error CRÍTICO en procesar(): {e}")
      raise e

  def procesar_amplify(
      self, cb_start, cb_end, z1_w, z2_start, z2_w, z4_start, z4_w, z5_start, z5_w
  ):
    zonas = {
        "ZONA_1": {"x_inicio": 0, "width": z1_w, "elementos": []},
        "ZONA_2": {"x_inicio": int(z2_start), "width": int(z2_w), "elementos": []},
        "ZONA_3": {
            "x_inicio": int(cb_start),
            "width": self.cb_width,
            "elementos": [],
        },
        "ZONA_4": {"x_inicio": int(z4_start), "width": int(z4_w), "elementos": []},
        "ZONA_5": {"x_inicio": int(z5_start), "width": int(z5_w), "elementos": []},
    }

    warnings = []
    if self.cb_width > 0:
      zonas["ZONA_3"]["elementos"].append({
          "tipo": "center_board",
          "width": self.cb_width,
          "x_absoluta": int(cb_start),
      })

    stretch = self.config.get("stretch_limit", 10) / 100.0
    squash_logos = self.config.get("squash_logos", False)

    qty_goal = self.config.get("qty_goal", 1)
    qty_tl = self.config.get("qty_tl", 1)

    def fill_zone_flexbox(z_name, z_width, z_start, target_logos):
      if z_width <= 0:
        return

      best_A = 0
      best_L = 0
      best_Sa = 1.0
      best_Sl = 1.0
      found_combo = False

      if target_logos == 0:
        max_a = int((z_width + self.margin) // max(1, self.anim_w)) + 1
        for a_test in range(max_a, 0, -1):
          req_w = a_test * self.anim_w
          if req_w <= z_width:
            best_A, best_L = a_test, 0
            found_combo = True
            break
          elif stretch > 0:
            factor = z_width / float(req_w) if req_w > 0 else 1.0
            if factor >= (1.0 - stretch) and factor > 0:
              best_A, best_L = a_test, 0
              best_Sa = factor if squash_logos else factor
              found_combo = True
              break
      else:
        max_total_items = (
            int((z_width + self.margin) // max(1, min(self.anim_w, self.logo_w)))
            + 2
        )

        for total_items in range(max_total_items * 2, 1, -1):
          a_test = total_items // 2
          l_test = total_items - a_test

          if target_logos > 0:
            l_test = min(l_test, target_logos)

          if a_test < 1 or l_test < 1:
            continue

          req_w = a_test * self.anim_w + l_test * self.logo_w
          if req_w <= z_width:
            best_A, best_L = a_test, l_test
            found_combo = True
            break
          elif stretch > 0:
            factor = z_width / float(req_w) if req_w > 0 else 1.0
            if factor >= (1.0 - stretch) and factor > 0:
              best_A, best_L = a_test, l_test
              best_Sa = factor if squash_logos else 1.0
              best_Sl = (
                  factor
                  if squash_logos
                  else (
                      (z_width - a_test * self.anim_w) / float(l_test * self.logo_w)
                      if (l_test * self.logo_w) > 0
                      else 1.0
                  )
              )
              if best_Sl >= (1.0 - stretch) and best_Sl > 0:
                found_combo = True
                break

      if not found_combo and target_logos > 0:
        best_A = 0
        max_logos = int(z_width // max(1, self.logo_w))
        if max_logos >= 1:
          best_L = min(max_logos, target_logos)
          best_Sl = 1.0
        else:
          best_L = 1
          best_Sl = max(0.1, z_width / float(self.logo_w))

        if z_name in ["ZONA_1", "ZONA_5"]:
          warnings.append("goal_overlap")
        else:
          warnings.append("touchline_overlap")
      elif not found_combo and target_logos == 0:
        best_A = 1
        best_L = 0
        best_Sa = max(0.1, z_width / float(self.anim_w))

      items = []
      a_count, l_count = best_A, best_L

      while a_count > 0 or l_count > 0:
        if l_count >= a_count and l_count > 0:
          items.append(
              {"tipo": "static_logo", "w": max(1, int(self.logo_w * best_Sl))}
          )
          l_count -= 1
        if a_count > 0:
          items.append(
              {"tipo": "anim_zone", "w": max(1, int(self.anim_w * best_Sa))}
          )
          a_count -= 1
        if l_count > 0 and l_count > a_count:
          items.append(
              {"tipo": "static_logo", "w": max(1, int(self.logo_w * best_Sl))}
          )
          l_count -= 1

      if not items:
        return

      if z_name in ["ZONA_4", "ZONA_5"]:
        items.reverse()

      if self.config.get("invert_order", False):
        items.reverse()

      total_items_w = sum(item["w"] for item in items)
      S = max(0, z_width - total_items_w)
      N = len(items)
      G = S / float(N) if N > 0 else 0

      cx = z_start + (G / 2.0)
      for item in items:
        elem = {"tipo": item["tipo"], "width": item["w"], "x_absoluta": int(cx)}
        if item["tipo"] == "anim_zone":
          elem["anim_idx"] = self.get_anim_idx()
        else:
          elem["logo_under_type"] = "frozen"
          elem["freeze_on_frame_1"] = True
        zonas[z_name]["elementos"].append(elem)
        cx += item["w"] + G

    fill_zone_flexbox("ZONA_1", z1_w, 0, qty_goal)
    fill_zone_flexbox("ZONA_2", z2_w, z2_start, qty_tl)
    fill_zone_flexbox("ZONA_4", z4_w, z4_start, qty_tl)
    fill_zone_flexbox("ZONA_5", z5_w, z5_start, qty_goal)

    return {
        "proyecto": "AE_EXPORT",
        "dimensiones_globales": {"width": self.w_total, "height": self.h_total},
        "archivos_fuente": self.archivos,
        "zonas": zonas,
        "warnings": warnings,
    }

  def procesar_fallback(
      self, cb_start, cb_end, z1_w, z2_start, z2_w, z4_start, z4_w, z5_start, z5_w
  ):
    zonas = {
        "ZONA_1": {"x_inicio": 0, "width": z1_w, "elementos": []},
        "ZONA_2": {"x_inicio": int(z2_start), "width": int(z2_w), "elementos": []},
        "ZONA_3": {
            "x_inicio": int(cb_start),
            "width": self.cb_width,
            "elementos": [],
        },
        "ZONA_4": {"x_inicio": int(z4_start), "width": int(z4_w), "elementos": []},
        "ZONA_5": {"x_inicio": int(z5_start), "width": int(z5_w), "elementos": []},
        "ZONA_FALLBACK": {
            "x_inicio": 0,
            "width": self.w_total,
            "elementos": [],
        },
    }
    if self.cb_width > 0:
      zonas["ZONA_FALLBACK"]["elementos"].append({
          "tipo": "center_board",
          "width": self.cb_width,
          "x_absoluta": int(cb_start),
      })

    left_w = cb_start
    n_left = int((left_w + self.margin) // (self.anim_w + self.margin))
    if n_left > 0:
      gap = (left_w - n_left * self.anim_w) / max(1, n_left)
      cx = gap / 2.0
      for _ in range(n_left):
        zonas["ZONA_FALLBACK"]["elementos"].append({
            "tipo": "anim_zone",
            "width": self.anim_w,
            "x_absoluta": int(cx),
            "anim_idx": self.get_anim_idx(),
        })
        cx += self.anim_w + gap

    right_w = self.w_total - cb_end
    n_right = int((right_w + self.margin) // (self.anim_w + self.margin))
    if n_right > 0:
      gap = (right_w - n_right * self.anim_w) / max(1, n_right)
      cx = cb_end + gap / 2.0
      for _ in range(n_right):
        zonas["ZONA_FALLBACK"]["elementos"].append({
            "tipo": "anim_zone",
            "width": self.anim_w,
            "x_absoluta": int(cx),
            "anim_idx": self.get_anim_idx(),
        })
        cx += self.anim_w + gap

    warnings = []
    self._aplicar_stretch_fit(zonas, warnings, cb_start, cb_end)

    return {
        "proyecto": "AE_EXPORT",
        "dimensiones_globales": {"width": self.w_total, "height": self.h_total},
        "archivos_fuente": self.archivos,
        "zonas": zonas,
        "warnings": warnings,
    }


def segmentos_arquitectura(datos, h_total, w_total):
  """Geometría de la imagen de arquitectura (3 filas: GL izq, touchline, GL der).

  Devuelve ((res_w, res_h), [(x_ini_master, x_fin_master, x_res, y_res) x 3]).
  La usan generar_imagen_arquitectura y el reproductor de la preview, para que
  ambos coloquen cada tramo del strip exactamente en el mismo sitio.
  """
  z1_w = datos["zonas"]["ZONA_1"]["width"]
  z2_s = datos["zonas"]["ZONA_2"]["x_inicio"]
  z4_e = (
      datos["zonas"]["ZONA_4"]["x_inicio"] + datos["zonas"]["ZONA_4"]["width"]
  )
  z5_s = datos["zonas"]["ZONA_5"]["x_inicio"]
  z234_w = max(1, z4_e - z2_s)

  def tramo(x1, x2):
    x1, x2 = sorted([max(0, min(x1, w_total)), max(0, min(x2, w_total))])
    return x1, x2

  t1, t2, t5 = tramo(0, z1_w), tramo(z2_s, z4_e), tramo(z5_s, w_total)

  v_gap = int(h_total * 2.8)
  top_margin = int(h_total * 2.8)
  res_h = top_margin + (h_total * 3) + (v_gap * 2)

  y1 = top_margin
  y2 = top_margin + h_total + v_gap
  y3 = top_margin + (h_total * 2) + (v_gap * 2)

  x_c1 = 0
  x_c2 = int((z234_w - (t2[1] - t2[0])) / 2)
  x_c5 = max(0, z234_w - (t5[1] - t5[0]))

  return (z234_w, res_h), [
      (t1[0], t1[1], x_c1, y1),
      (t2[0], t2[1], x_c2, y2),
      (t5[0], t5[1], x_c5, y3),
  ]


def generar_imagen_arquitectura(
    master, datos, h_total, w_total, gl_offset_pct, bg_color=(0, 0, 0, 0),
    resample=None, max_png_width=16000
):
  def safe_crop(img, x1, y1, x2, y2):
    x1, x2 = sorted([max(0, min(x1, img.width)), max(0, min(x2, img.width))])
    y1, y2 = sorted([max(0, min(y1, img.height)), max(0, min(y2, img.height))])
    return img.crop((x1, y1, x2, y2))

  (z234_w, res_h), segs = segmentos_arquitectura(datos, h_total, w_total)
  (s1, x_c1, y1), (s2, x_c2, y2), (s5, x_c5, y3) = (
      (seg[0], seg[2], seg[3]) for seg in segs
  )

  c1 = safe_crop(master, s1, 0, segs[0][1], h_total)
  c2 = safe_crop(master, s2, 0, segs[1][1], h_total)
  c5 = safe_crop(master, s5, 0, segs[2][1], h_total)

  res = Image.new("RGBA", (z234_w, res_h), bg_color)

  res.paste(c1, (x_c1, y1), c1 if c1.mode == "RGBA" else None)
  res.paste(c2, (x_c2, y2), c2 if c2.mode == "RGBA" else None)
  res.paste(c5, (x_c5, y3), c5 if c5.mode == "RGBA" else None)

  font_size = max(40, int(h_total * 0.7))
  try:
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    font_path = os.path.join(base_dir, "Assets", "Montserrat-Bold.ttf")
    font = ImageFont.truetype(font_path, size=font_size)
  except IOError:
    font = ImageFont.load_default()

  draw = ImageDraw.Draw(res)
  text_color = (100, 100, 100, 255)

  def draw_label(text, cx, bottom_y, anchor="mb"):
    draw.text((cx, bottom_y), text, fill=text_color, font=font, anchor=anchor)

  def draw_bracket(cx, text_y):
    bw = int(h_total * 10)
    bh = int(h_total * 0.5)
    lw = max(3, int(h_total * 0.05))

    base_y = text_y + int(h_total * 0.15)
    bx1 = cx - (bw // 2)
    bx2 = cx + (bw // 2)

    draw.line([(bx1, base_y), (bx2, base_y)], fill=text_color, width=lw)
    draw.line([(bx1, base_y), (bx1, base_y + bh)], fill=text_color, width=lw)
    draw.line([(bx2, base_y), (bx2, base_y + bh)], fill=text_color, width=lw)

  def draw_dashed_vertical(x, text_y):
    bh = int(h_total * 0.5)
    base_y = text_y + int(h_total * 0.15)
    lw = max(3, int(h_total * 0.05))
    dash_len = int(h_total * 0.15)

    y = base_y
    y_end = base_y + bh
    while y < y_end:
      draw.line(
          [(x, y), (x, min(y + dash_len, y_end))], fill=text_color, width=lw
      )
      y += dash_len * 2

  z1_w = datos["zonas"]["ZONA_1"]["width"]
  z5_w = datos["zonas"]["ZONA_5"]["width"]

  cx_g1 = x_c1 + int(z1_w * (0.5 + gl_offset_pct))
  cx_g5 = x_c5 + int(z5_w * (0.5 - gl_offset_pct))

  text_y1 = y1 - int(h_total * 1.3)
  text_y2 = y2 - int(h_total * 0.5)
  text_y3 = y3 - int(h_total * 1.3)

  draw_bracket(cx_g1, text_y1)
  draw_label("GOAL LEFT", cx_g1, text_y1)
  draw_label("GOAL LINE LEFT", x_c1, text_y1, anchor="lb")
  draw_label("CORNER", x_c1 + c1.width, text_y1, anchor="rb")
  draw_dashed_vertical(x_c1 + c1.width - max(1, int(h_total * 0.02)), text_y1)

  draw_label("TOUCHLINE", x_c2 + (c2.width / 2), text_y2)

  draw_bracket(cx_g5, text_y3)
  draw_label("CORNER", x_c5, text_y3, anchor="lb")
  draw_dashed_vertical(x_c5 + max(1, int(h_total * 0.02)), text_y3)
  draw_label("GOAL RIGHT", cx_g5, text_y3)
  draw_label("GOAL LINE RIGHT", x_c5 + c5.width, text_y3, anchor="rb")

  # max_png_width=None (preview) evita el reescalado final: la UI ya escala al mostrar
  if max_png_width and res.width > max_png_width:
    ratio = max_png_width / float(res.width)
    new_h = int(res.height * ratio)
    if resample is not None:
      resamp = resample
    else:
      try:
        resamp = Image.Resampling.LANCZOS
      except AttributeError:
        resamp = Image.LANCZOS
    res = res.resize((max_png_width, new_h), resamp)

  return res


def calcular_layout_data(
    w_total, h_total, goal_line, cb_width, margin, archivos_cargados, config
):
  dist = Distribuidor(
      w_total, h_total, goal_line, cb_width, margin, archivos_cargados, config
  )
  return dist.procesar()