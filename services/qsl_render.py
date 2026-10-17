#!/usr/bin/env python3
"""Dibuja la QSL en el servidor (Pillow) con el mismo diseno que el generador del dashboard.
Usado por mail_service.py para el envio automatico del bot de concurso."""
import base64
import io
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont

DEFAULTS = {"colorText": "#566573", "colorBox": "#154360", "colorTitle": "#ffffff", "colorBoxBg": "#ffffff",
            "boxTransp": 0, "showComments": True, "boxX": 15, "boxY": 420, "boxW": 380}
_RUTAS = {
    False: ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
    True: ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
}
_ruta_cache = {}


def _ruta_fuente(bold):
    if bold in _ruta_cache:
        return _ruta_cache[bold]
    ruta = None
    try:
        ruta = subprocess.run(["fc-match", "-f", "%{file}", "sans-serif:bold" if bold else "sans-serif"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or None
    except Exception:
        pass
    if not ruta or not ruta.lower().endswith((".ttf", ".otf")):
        ruta = None
        import os
        for r in _RUTAS[bold]:
            if os.path.exists(r):
                ruta = r
                break
    if not ruta:
        raise RuntimeError("no se encontro una fuente TrueType (instala fonts-dejavu-core)")
    _ruta_cache[bold] = ruta
    return ruta


def _fuente(px, bold):
    return ImageFont.truetype(_ruta_fuente(bold), max(1, int(round(px))))


def _color(h, defecto):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(h or ""))
    h = m.group(1) if m else defecto.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rr_mask(w, h, r, ss=4):
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    m = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius=r * ss, fill=255)
    return m.resize((w, h), Image.LANCZOS)


def _pegar(img, mask, x, y, color, alpha):
    if alpha < 1:
        mask = mask.point(lambda v: int(v * alpha))
    img.paste(Image.new("RGB", mask.size, color), (int(round(x)), int(round(y))), mask)


def _fecha(fh):
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}:\d{2}))?", str(fh or ""))
    return (m.group(3) + "-" + m.group(2) + "-" + m.group(1), m.group(4) or "") if m else (str(fh or ""), "")


def render(data, design=None, bg_bytes=None, W=1920, H=1080):
    d = dict(DEFAULTS)
    d.update(design or {})
    if bg_bytes:
        img = Image.open(io.BytesIO(bg_bytes)).convert("RGB").resize((W, H), Image.LANCZOS)
    else:
        img = Image.new("RGB", (W, H), (255, 255, 255))
    s = W / 960.0
    X, Y, BW = d["boxX"] * s, d["boxY"] * s, d["boxW"] * s
    gap, rad, h1, hH, hV, hC = 4 * s, 5 * s, 28 * s, 22 * s, 24 * s, 26 * s
    try:
        alpha = 1 - min(100.0, max(0.0, float(d.get("boxTransp") or 0))) / 100
    except (TypeError, ValueError):
        alpha = 1.0
    c_texto, c_caja = _color(d["colorText"], DEFAULTS["colorText"]), _color(d["colorBox"], DEFAULTS["colorBox"])
    c_titulo, c_fondo = _color(d["colorTitle"], DEFAULTS["colorTitle"]), _color(d["colorBoxBg"], DEFAULTS["colorBoxBg"])
    dib = ImageDraw.Draw(img)

    def ajustar(txt, fuente, mw):
        txt = str(txt if txt is not None else "")
        if fuente.getlength(txt) <= mw:
            return txt
        while len(txt) > 1 and fuente.getlength(txt + "…") > mw:
            txt = txt[:-1]
        return txt + "…"

    fecha, hora = _fecha(data.get("fechaHora"))
    rs, rr = str(data.get("rstS") or ""), str(data.get("rstR") or "")
    rst = rs + "/" + rr if (rs and rr and rs != rr) else (rs or rr)

    # fila 1
    y = Y
    pw1, vw1, pw2 = BW * 0.25, BW * 0.27, BW * 0.24
    vw2 = BW - pw1 - vw1 - pw2
    _pegar(img, _rr_mask(BW, h1, rad), X, y, c_fondo, alpha)
    _pegar(img, _rr_mask(pw1, h1, rad), X, y, c_caja, alpha)
    _pegar(img, _rr_mask(pw2, h1, rad), X + pw1 + vw1, y, c_caja, alpha)
    cy = y + h1 / 2 + 0.5 * s
    fb12, fb13 = _fuente(12 * s, True), _fuente(13 * s, True)
    dib.text((X + pw1 / 2, cy), "OPERADOR", font=fb12, fill=c_titulo, anchor="mm")
    dib.text((X + pw1 + vw1 + pw2 / 2, cy), "QSO CON", font=fb12, fill=c_titulo, anchor="mm")
    dib.text((X + pw1 + vw1 / 2, cy), ajustar(data.get("opCall"), fb13, vw1 - 8 * s), font=fb13, fill=c_texto, anchor="mm")
    dib.text((X + pw1 + vw1 + pw2 + vw2 / 2, cy), ajustar(data.get("dxCall"), fb13, vw2 - 8 * s), font=fb13, fill=c_texto, anchor="mm")

    # filas 2 y 3
    y += h1 + gap
    bloque = _rr_mask(BW, hH + hV, rad)
    _pegar(img, bloque, X, y, c_fondo, alpha)
    _pegar(img, bloque.crop((0, 0, bloque.width, int(round(hH)))), X, y, c_caja, alpha)
    pesos = [1.75, 0.95, 1.0, 0.95, 1.65, 0.8]
    tot = sum(pesos)
    cab = ["FECHA", "UTC", "BANDA", "MODO", "FRECUENCIA", "RST"]
    val = [fecha, hora, data.get("band"), data.get("mode"), data.get("freq"), rst]
    fb11, fb12b = _fuente(11 * s, True), _fuente(12 * s, True)
    cx = X
    for i in range(6):
        cw = BW * pesos[i] / tot
        mid = cx + cw / 2
        dib.text((mid, y + hH / 2 + 0.5 * s), ajustar(cab[i], fb11, cw - 4 * s), font=fb11, fill=c_titulo, anchor="mm")
        dib.text((mid, y + hH + hV / 2 + 0.5 * s), ajustar(val[i], fb12b, cw - 4 * s), font=fb12b, fill=c_texto, anchor="mm")
        cx += cw
    y += hH + hV

    # fila 4
    if d.get("showComments") and data.get("comments"):
        y += gap
        _pegar(img, _rr_mask(BW, hC, rad), X, y, c_fondo, alpha)
        f12 = _fuente(12 * s, False)
        dib.text((X + 10 * s, y + hC / 2 + 0.5 * s), ajustar(data["comments"], f12, BW - 20 * s), font=f12, fill=c_texto, anchor="lm")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=False)
    return out.getvalue()
