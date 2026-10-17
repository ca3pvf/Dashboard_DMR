#!/usr/bin/env python3
"""Envio de QSL por correo via Gmail (SMTP + contrasena de aplicacion). Puerto 5017.
  GET  /api/mail/status
  POST /api/mail/config  {"address","app_password","from_name","subject","body"}  (prueba el acceso antes de guardar)
  POST /api/mail/clear   borra la cuenta guardada
  POST /api/mail/test    envia un correo de prueba a la propia cuenta
  POST /api/mail/send    {"to","call","fecha","filename","png_b64"}  (solo a correos que esten en la cola QSL)
La contrasena vive solo en mail_config.json (permisos 600) y nunca se devuelve."""
import base64
import json
import os
import re
import smtplib
import socket
import ssl
import sqlite3
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr

from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("MAIL_CONFIG", os.path.join(BASE, "mail_config.json"))
STATS_PATH = os.environ.get("MAIL_STATS", os.path.join(BASE, "mail_stats.json"))
STORE_DB = os.environ.get("PCBOX_STORE_DB", os.path.join(BASE, "pcbox_store.db"))
PORT = int(os.environ.get("MAIL_PORT", "5017"))
HOST, SMTP_PORT = "smtp.gmail.com", 587
DAILY_LIMIT = 400          # Gmail gratuito permite ~500 por dia; se deja margen
MIN_GAP = 1.5              # segundos entre envios
MAX_PNG = 12 * 1024 * 1024
RE_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DEF_SUBJECT = "QSL {op} - confirmacion de contacto con {dx}"
DEF_BODY = ("Hola {dx},\n\nAdjunto mi tarjeta QSL por nuestro contacto del {fecha}.\n"
            "Gracias por el contacto, 73!\n\n{op}")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
_lock = threading.Lock()
ST = {"ok": None, "error": None, "last": 0.0}
_FAM_OK = []     # familia de red (IPv6/IPv4) que funcionó la última vez con Gmail


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def leer_cfg():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_cfg(d):
    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


def enviados_hoy():
    try:
        with open(STATS_PATH) as f:
            d = json.load(f)
        return d["n"] if d.get("dia") == time.strftime("%Y-%m-%d") else 0
    except Exception:
        return 0


def sumar_envio():
    with open(STATS_PATH, "w") as f:
        json.dump({"dia": time.strftime("%Y-%m-%d"), "n": enviados_hoy() + 1}, f)


def _conn(host, port, timeout, fam):
    ultimo = None
    for f, tipo, proto, _, sa in socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM):
        try:
            sk = socket.socket(f, tipo, proto)
            sk.settimeout(timeout)
            sk.connect(sa)
            return sk
        except OSError as e:
            ultimo = e
    raise ultimo or OSError("sin direccion para " + host)


def _clases(fam):
    class S(smtplib.SMTP):
        def _get_socket(self, host, port, timeout):
            return _conn(host, port, timeout, fam)

    class L(smtplib.SMTP_SSL):
        def _get_socket(self, host, port, timeout):
            return self.context.wrap_socket(_conn(host, port, timeout, fam), server_hostname=self._host)
    return S, L


def _smtp(addr, pwd):
    """Gmail: prueba IPv6 e IPv4, primero STARTTLS (587) y luego SSL directo (465)."""
    errores = []
    fams = [(socket.AF_INET6, "IPv6"), (socket.AF_INET, "IPv4")]
    if _FAM_OK and _FAM_OK[0] == socket.AF_INET:
        fams.reverse()          # prueba primero la familia que funcionó la última vez
    for fam, nf in fams:
        S, L = _clases(fam)
        for modo, puerto in (("starttls", 587), ("ssl", 465)):
            s = None
            etapa = "conexion"
            try:
                ctx = ssl.create_default_context()
                if modo == "starttls":
                    s = S(HOST, puerto, timeout=20)
                    etapa = "ehlo"
                    s.ehlo()
                    etapa = "starttls"
                    s.starttls(context=ctx)
                    etapa = "ehlo-tls"
                    s.ehlo()
                else:
                    s = L(HOST, puerto, timeout=20, context=ctx)
                    etapa = "ehlo"
                    s.ehlo()
                etapa = "login"
                s.user, s.password = addr, pwd
                s.auth("PLAIN", s.auth_plain, initial_response_ok=True)   # solo PLAIN: Gmail corta si se reintenta con LOGIN
                _FAM_OK[:] = [fam]
                return s
            except smtplib.SMTPAuthenticationError:
                if s:
                    s.close()
                raise
            except Exception as e:
                errores.append("%s:%d [%s] %s" % (nf, puerto, etapa, e))
                print("gmail:", errores[-1], flush=True)
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
    raise RuntimeError("no se pudo conectar a Gmail (" + "; ".join(errores) + ")")


def _err_texto(e):
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return ("Gmail rechazo el acceso. Usa una contrasena de aplicacion de 16 caracteres "
                "(requiere verificacion en dos pasos), no la contrasena normal.")
    return "%s: %s" % (type(e).__name__, e)


def _fmt(txt, d):
    for k, v in d.items():
        txt = txt.replace("{%s}" % k, str(v))
    return txt


def _mensaje(cfg, to, dx, fecha, adjunto=None, nombre=None):
    op = cfg.get("op_call") or "@@CALL@@"
    vars_ = {"op": op, "dx": dx, "fecha": fecha}
    m = EmailMessage()
    m["From"] = formataddr((cfg.get("from_name") or op, cfg["address"]))
    m["To"] = to
    m["Subject"] = _fmt(cfg.get("subject") or DEF_SUBJECT, vars_).replace("\n", " ")[:200]
    m.set_content(_fmt(cfg.get("body") or DEF_BODY, vars_))
    if adjunto:
        m.add_attachment(adjunto, maintype="image", subtype="png", filename=nombre or "QSL.png")
    return m


def _destino_en_cola(to, call):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE_DB, uri=True, timeout=5)
        try:
            r = con.execute("SELECT 1 FROM qsl_cola WHERE lower(email)=lower(?) AND upper(callsign)=upper(?) LIMIT 1",
                            (to, call)).fetchone()
        finally:
            con.close()
        return bool(r)
    except Exception:
        return False


@app.route("/api/mail/status")
def api_status():
    c = leer_cfg()
    return jsonify({"configured": bool(c.get("address") and c.get("app_password")), "address": c.get("address", ""),
                    "from_name": c.get("from_name", ""), "op_call": c.get("op_call", ""),
                    "subject": c.get("subject") or DEF_SUBJECT, "body": c.get("body") or DEF_BODY,
                    "ok": ST["ok"], "error": ST["error"], "enviados_hoy": enviados_hoy(), "limite_diario": DAILY_LIMIT})


@app.route("/api/mail/config", methods=["POST"])
def api_config():
    b = request.get_json(silent=True) or {}
    prev = leer_cfg()
    addr = str(b.get("address", "")).strip()
    if not RE_MAIL.match(addr) or len(addr) > 120:
        return jsonify({"error": "correo invalido"}), 400
    pwd = re.sub(r"\s+", "", str(b.get("app_password", "")))
    mismo = addr.lower() == str(prev.get("address", "")).lower()
    if not pwd and mismo:
        pwd = prev.get("app_password", "")
    if not pwd or len(pwd) > 64:
        return jsonify({"error": "falta la contrasena de aplicacion"}), 400
    nuevo = {"address": addr, "app_password": pwd,
             "from_name": str(b.get("from_name", ""))[:60].strip(),
             "op_call": re.sub(r"[^A-Za-z0-9/]", "", str(b.get("op_call", "")))[:12].upper(),
             "subject": str(b.get("subject", "") or DEF_SUBJECT)[:200],
             "body": str(b.get("body", "") or DEF_BODY)[:4000]}
    if pwd != prev.get("app_password") or not mismo:
        try:
            with _lock:
                _smtp(addr, pwd).quit()
        except Exception as e:
            ST.update(ok=False, error=_err_texto(e))
            return jsonify({"error": _err_texto(e)}), 400
    guardar_cfg(nuevo)
    ST.update(ok=True, error=None)
    return jsonify({"ok": True})


@app.route("/api/mail/clear", methods=["POST"])
def api_clear():
    try:
        os.remove(CONFIG_PATH)
    except FileNotFoundError:
        pass
    ST.update(ok=None, error=None)
    return jsonify({"ok": True})


def _enviar(msg, cfg):
    with _lock:
        espera = MIN_GAP - (time.time() - ST["last"])
        if espera > 0:
            time.sleep(espera)
        try:
            s = _smtp(cfg["address"], cfg["app_password"])
            try:
                s.send_message(msg)
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
        finally:
            ST["last"] = time.time()


@app.route("/api/mail/test", methods=["POST"])
def api_test():
    cfg = leer_cfg()
    if not (cfg.get("address") and cfg.get("app_password")):
        return jsonify({"error": "Gmail sin configurar"}), 400
    try:
        _enviar(_mensaje(cfg, cfg["address"], "PRUEBA", time.strftime("%d-%m-%Y")), cfg)
    except Exception as e:
        ST.update(ok=False, error=_err_texto(e))
        return jsonify({"error": _err_texto(e)}), 502
    ST.update(ok=True, error=None)
    return jsonify({"ok": True, "to": cfg["address"]})


@app.route("/api/mail/send", methods=["POST"])
def api_send():
    cfg = leer_cfg()
    if not (cfg.get("address") and cfg.get("app_password")):
        return jsonify({"error": "Gmail sin configurar"}), 400
    b = request.get_json(silent=True) or {}
    to = str(b.get("to", "")).strip()
    call = str(b.get("call", "")).strip().upper()[:20]
    if not RE_MAIL.match(to) or not call:
        return jsonify({"error": "destino invalido"}), 400
    if not _destino_en_cola(to, call):
        return jsonify({"error": "ese correo no esta asociado a %s en la cola QSL" % call}), 403
    if enviados_hoy() >= DAILY_LIMIT:
        return jsonify({"error": "limite diario alcanzado (%d)" % DAILY_LIMIT}), 429
    try:
        png = base64.b64decode(str(b.get("png_b64", "")), validate=True)
    except Exception:
        return jsonify({"error": "imagen invalida"}), 400
    if not png.startswith(b"\x89PNG") or len(png) > MAX_PNG:
        return jsonify({"error": "imagen invalida o muy grande"}), 400
    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", str(b.get("filename", "QSL.png")))[:80] or "QSL.png"
    fecha = str(b.get("fecha", ""))[:30]
    try:
        _enviar(_mensaje(cfg, to, call, fecha, png, nombre), cfg)
    except Exception as e:
        ST.update(ok=False, error=_err_texto(e))
        return jsonify({"error": _err_texto(e)}), 502
    sumar_envio()
    ST.update(ok=True, error=None)
    return jsonify({"ok": True})


STORE = "http://127.0.0.1:5013/api/store"


def _http(url, cuerpo=None, timeout=15):
    import urllib.request
    data = None if cuerpo is None else json.dumps(cuerpo).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _item_cola(call):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE_DB, uri=True, timeout=5)
        try:
            return con.execute("SELECT uid, email FROM qsl_cola WHERE upper(callsign)=upper(?) AND email<>'' "
                               "ORDER BY added DESC LIMIT 1", (call,)).fetchone()
        finally:
            con.close()
    except Exception:
        return None


@app.route("/api/mail/auto", methods=["POST"])
def api_auto():
    """Dibuja la QSL en el servidor y la envia al correo que la cola QSL tiene para ese indicativo."""
    cfg = leer_cfg()
    if not (cfg.get("address") and cfg.get("app_password")):
        return jsonify({"error": "Gmail sin configurar"}), 400
    b = request.get_json(silent=True) or {}
    call = str(b.get("call", "")).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,10}", call):
        return jsonify({"error": "indicativo invalido"}), 400
    item = _item_cola(call)
    if not item:
        return jsonify({"error": "no hay correo para %s en la cola QSL" % call}), 404
    uid, to = item
    if enviados_hoy() >= DAILY_LIMIT:
        return jsonify({"error": "limite diario alcanzado (%d)" % DAILY_LIMIT}), 429
    try:
        import qsl_render
    except ImportError:
        return jsonify({"error": "falta Pillow: sudo apt install -y python3-pil"}), 500
    try:
        diseno = json.loads(_http(STORE + "/config/pcbox-qsl-design").get("value") or "{}")
        bg = _http(STORE + "/config/pcbox-qsl-bg", timeout=30).get("value") or ""
        bg_bytes = base64.b64decode(bg.split(",", 1)[1]) if bg.startswith("data:") and "," in bg else None
    except Exception as e:
        return jsonify({"error": "no se pudo leer el diseno de la QSL: %s" % e}), 502

    def t(k, n):
        return re.sub(r"[\x00-\x1f<>]", "", str(b.get(k, "") or "")).strip()[:n]
    op = re.sub(r"[^A-Za-z0-9/]", "", str(b.get("op") or cfg.get("op_call") or "@@CALL@@")).upper()[:12]
    datos = {"opCall": op, "dxCall": call, "fechaHora": t("time_utc", 19)[:16], "freq": t("freq", 12),
             "band": t("band", 8), "mode": t("mode", 10), "rstS": t("rst_s", 6), "rstR": t("rst_r", 6),
             "comments": t("comments", 120)}
    try:
        png = qsl_render.render(datos, diseno, bg_bytes)
    except Exception as e:
        return jsonify({"error": "error dibujando la QSL: %s" % e}), 500
    fecha = qsl_render._fecha(datos["fechaHora"])[0]
    try:
        _enviar(_mensaje(cfg, to, call, fecha, png, "QSL_%s_%s.png" % (op.replace("/", ""), call)), cfg)
    except Exception as e:
        ST.update(ok=False, error=_err_texto(e))
        return jsonify({"error": _err_texto(e)}), 502
    sumar_envio()
    ST.update(ok=True, error=None)
    try:
        _http(STORE + "/qslcola/estado", {"uids": [uid], "estado": "enviada"})
    except Exception:
        pass
    return jsonify({"ok": True, "to": to})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
