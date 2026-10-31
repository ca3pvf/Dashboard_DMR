#!/usr/bin/env python3
"""
aprs_service.py - Monitor, baliza y mensajes APRS-IS para el dashboard de pcbox.
Puerto 5014. Configuracion en aprs_config.json (se crea sola la primera vez).

  GET  /api/aprs/status      estado de la conexion, baliza y mensajes sin leer
  GET  /api/aprs/stations    estaciones escuchadas (dentro del radio configurado)
  GET  /api/aprs/messages    ultimos mensajes enviados y recibidos
  POST /api/aprs/send        {"to": "CALL-SSID", "text": "..."}
  POST /api/aprs/beacon      {"enabled": true, "interval_min": 30, "send_now": true}
  POST /api/aprs/messages/read
"""
import json
import math
import os
import re
import socket
import sqlite3
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime

from flask import Flask, jsonify, request

try:
    import aprslib  # opcional: mejora la lectura de posiciones (Mic-E, etc.)
except ImportError:
    aprslib = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("APRS_CONFIG", os.path.join(BASE_DIR, "aprs_config.json"))
DB_PATH = os.environ.get("APRS_DB", os.path.join(BASE_DIR, "aprs.db"))
BOT_PATH = os.environ.get("APRS_BOT", os.path.join(BASE_DIR, "aprs_bot.json"))
PORT = int(os.environ.get("APRS_PORT", "5014"))
VERSION = "1.0"
TOCALL = "APZPCB"                      # APZ = experimental
MAX_ESTACIONES = 500
ESTACION_TTL = 6 * 3600                # segundos
REINTENTOS = [30, 60, 120, 240, 480]   # espera entre reintentos de un mensaje sin ACK
MIN_BALIZA_MIN = 10

DEFAULTS = {
    "callsign": "@@CALL@@",
    "ssid": "10",
    "passcode": "",                    # vacio = se calcula desde el indicativo
    "server": "rotate.aprs2.net",
    "port": 14580,
    "lat": -33.7524010,
    "lon": -70.7359641,
    "radius_km": 100,
    "beacon_enabled": False,
    "beacon_interval_min": 30,
    "beacon_comment": "pcbox dashboard @@CALL@@",
    "symbol_table": "/",
    "symbol": "-",
    "ambiguity": 0,
    "station_ttl_h": 6,
    "ui_refresh_s": 15,
}

app = Flask(__name__)


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


_lock = threading.Lock()
_sock_lock = threading.Lock()
_sock = None
CFG = dict(DEFAULTS)
ESTACIONES = {}
ST = {"conectado": False, "verificado": False, "error": None, "servidor": None,
      "paquetes": 0, "ultimo_paquete": None, "ultima_baliza": 0.0, "desde": None}


# ---------------- configuracion ----------------
def cargar_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"aprs_config.json ilegible, uso valores por defecto: {e}", flush=True)
    with _lock:
        CFG.clear()
        CFG.update(cfg)
    if not os.path.exists(CONFIG_PATH):
        guardar_config()


def guardar_config():
    with _lock:
        datos = dict(CFG)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(datos, f, indent=2)
    os.replace(tmp, CONFIG_PATH)


def mi_call():
    ssid = str(CFG.get("ssid", "")).strip()
    base = CFG["callsign"].upper()
    return base if ssid in ("", "0") else f"{base}-{ssid}"


def calcular_passcode(call):
    call = call.split("-")[0].upper()
    h = 0x73E2
    for i in range(0, len(call), 2):
        h ^= ord(call[i]) << 8
        if i + 1 < len(call):
            h ^= ord(call[i + 1])
    return h & 0x7FFF


# ---------------- utilidades de posicion ----------------
def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _mascara(s, posiciones, amb):
    s = list(s)
    if amb > 0:
        for i in posiciones[-amb:]:
            s[i] = " "
    return "".join(s)


def fmt_lat(lat, amb=0):
    cent = round(abs(lat) * 6000)
    d, mm = divmod(cent, 6000)
    s = f"{d:02d}{mm // 100:02d}.{mm % 100:02d}"
    return _mascara(s, [0, 1, 2, 3, 5, 6], amb) + ("N" if lat >= 0 else "S")


def fmt_lon(lon, amb=0):
    cent = round(abs(lon) * 6000)
    d, mm = divmod(cent, 6000)
    s = f"{d:03d}{mm // 100:02d}.{mm % 100:02d}"
    return _mascara(s, [0, 1, 2, 3, 4, 6, 7], amb) + ("E" if lon >= 0 else "W")


POS_RE = re.compile(r"^(\d{2})([\d ]{2}\.[\d ]{2})([NS])(.)(\d{3})([\d ]{2}\.[\d ]{2})([EW])(.)(.*)$", re.S)


def _num(s):
    return float(s.replace(" ", "0"))


def _b91(s):
    v = 0
    for c in s:
        v = v * 91 + (ord(c) - 33)
    return v


def parse_posicion(payload):
    """(lat, lon, simbolo, comentario) o None. Soporta posicion normal y comprimida."""
    if not payload:
        return None
    t = payload[0]
    if t in "!=":
        body = payload[1:]
    elif t in "/@":
        body = payload[8:]
    else:
        return None
    m = POS_RE.match(body)
    if m:
        lat = int(m.group(1)) + _num(m.group(2)) / 60
        lon = int(m.group(5)) + _num(m.group(6)) / 60
        if m.group(3) == "S":
            lat = -lat
        if m.group(7) == "W":
            lon = -lon
        return lat, lon, m.group(4) + m.group(8), m.group(9)
    if len(body) >= 13 and body[0] in "/\\ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij":
        try:
            lat = 90 - _b91(body[1:5]) / 380926.0
            lon = -180 + _b91(body[5:9]) / 190463.0
            return lat, lon, body[0] + body[9], body[13:]
        except Exception:
            return None
    return None


def parse_con_aprslib(raw):
    if aprslib is None:
        return None
    try:
        d = aprslib.parse(raw)
        if d.get("latitude") is not None and d.get("longitude") is not None:
            return (d["latitude"], d["longitude"],
                    (d.get("symbol_table") or "/") + (d.get("symbol") or "?"),
                    d.get("comment") or "")
    except Exception:
        pass
    return None


# ---------------- base de datos de mensajes ----------------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, dir TEXT NOT NULL,
        peer TEXT NOT NULL, text TEXT NOT NULL, msgid TEXT, estado TEXT NOT NULL,
        intentos INTEGER NOT NULL DEFAULT 0, prox REAL NOT NULL DEFAULT 0,
        leido INTEGER NOT NULL DEFAULT 0)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages(ts)")
    if "origen" not in [r[1] for r in con.execute("PRAGMA table_info(messages)")]:
        con.execute("ALTER TABLE messages ADD COLUMN origen TEXT NOT NULL DEFAULT ''")
    con.execute("""CREATE TABLE IF NOT EXISTS bot_part (
        concurso TEXT NOT NULL, call TEXT NOT NULL, peer TEXT, ts REAL NOT NULL, texto TEXT,
        aviso REAL NOT NULL DEFAULT 0, PRIMARY KEY (concurso, call))""")
    if "qsl" not in [r[1] for r in con.execute("PRAGMA table_info(bot_part)")]:
        con.execute("ALTER TABLE bot_part ADD COLUMN qsl TEXT NOT NULL DEFAULT ''")
    con.commit()
    con.close()


# ---------------- envio de paquetes ----------------
def enviar_paquete(txt):
    global _sock
    with _sock_lock:
        if _sock is None or not ST["verificado"]:
            return False
        try:
            _sock.sendall((txt + "\r\n").encode("ascii", "replace"))
            return True
        except Exception as e:
            ST["error"] = f"error enviando: {e}"
            try:
                _sock.close()
            except Exception:
                pass
            return False


def a_ascii(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    t = re.sub(r"[\r\n\t]+", " ", t)
    return re.sub(r"[{|~]", " ", t).strip()


def paquete_baliza():
    c = CFG
    amb = max(0, min(4, int(c.get("ambiguity", 0))))
    comentario = a_ascii(str(c.get("beacon_comment", "")))[:43]
    return (f"{mi_call()}>{TOCALL},TCPIP*:={fmt_lat(c['lat'], amb)}{c['symbol_table']}"
            f"{fmt_lon(c['lon'], amb)}{c['symbol']}{comentario}")


def enviar_baliza():
    if enviar_paquete(paquete_baliza()):
        ST["ultima_baliza"] = time.time()
        return True
    return False


def paquete_mensaje(destino, texto, msgid=None):
    pkt = f"{mi_call()}>{TOCALL},TCPIP*::{destino.ljust(9)[:9]}:{texto}"
    return pkt + (f"{{{msgid}" if msgid else "")


# ---------------- procesamiento de paquetes recibidos ----------------
def registrar_estacion(src, pos):
    lat, lon, simbolo, comentario = pos
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return
    dist = haversine_km(CFG["lat"], CFG["lon"], lat, lon)
    ahora = time.time()
    with _lock:
        e = ESTACIONES.get(src)
        n = (e["paquetes"] + 1) if e else 1
        ESTACIONES[src] = {"call": src, "lat": round(lat, 5), "lon": round(lon, 5),
                           "dist_km": round(dist, 1), "simbolo": simbolo,
                           "comentario": comentario.strip()[:80], "ts": ahora, "paquetes": n}
        if len(ESTACIONES) > MAX_ESTACIONES:
            for k, _ in sorted(ESTACIONES.items(), key=lambda kv: kv[1]["ts"])[:50]:
                ESTACIONES.pop(k, None)


def guardar_mensaje(direccion, peer, texto, msgid, estado, leido, origen=""):
    con = db()
    try:
        cur = con.execute(
            "INSERT INTO messages(ts,dir,peer,text,msgid,estado,leido,origen) VALUES(?,?,?,?,?,?,?,?)",
            (time.time(), direccion, peer, texto, msgid, estado, leido, origen))
        con.execute("DELETE FROM messages WHERE id < (SELECT MAX(id) FROM messages) - 2000")
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def procesar_mensaje(src, payload):
    if len(payload) < 11 or payload[10] != ":":
        return
    destino = payload[1:10].strip().upper()
    if destino != mi_call().upper():
        return
    texto = payload[11:]
    bajo = texto.lower()
    if bajo.startswith("ack") or bajo.startswith("rej"):
        msgid = texto[3:].split("}")[0].strip()
        con = db()
        try:
            nuevo = "entregado" if bajo.startswith("ack") else "fallido"
            con.execute("UPDATE messages SET estado=? WHERE dir='out' AND peer=? AND msgid=? AND estado='pendiente'",
                        (nuevo, src, msgid))
            con.commit()
        finally:
            con.close()
        return
    msgid = None
    if "{" in texto:
        texto, msgid = texto.rsplit("{", 1)
        msgid = msgid.split("}")[0].strip()[:5] or None
    if msgid:
        enviar_paquete(paquete_mensaje(src, f"ack{msgid}"))
        con = db()
        try:
            dup = con.execute("SELECT 1 FROM messages WHERE dir='in' AND peer=? AND msgid=? AND ts>?",
                              (src, msgid, time.time() - 3600)).fetchone()
        finally:
            con.close()
        if dup:
            print("bot: ignorado %s: mensaje repetido (mismo numero {%s} en la ultima hora)" % (src, msgid), flush=True)
            return
    mid = guardar_mensaje("in", src, texto.strip(), msgid, "recibido", 0)
    threading.Thread(target=bot_procesar, args=(src, texto.strip(), mid), daemon=True).start()


# ---------------- bot de concurso (palabra clave -> QSL a la cola) ----------------
BOT_DEFAULTS = {
    "enabled": False,
    "nombre": "Concurso APRS",
    "palabras": [],
    "inicio": "",                      # "AAAA-MM-DDTHH:MM", hora de pcbox; vacio = sin limite
    "fin": "",
    "resp_ok": "Recibido {call}! QSL en camino a tu correo. Gracias, 73 de {op}",
    "resp_ok_manual": "Recibido {call}! Anotado, te enviare la QSL. 73 de {op}",
    "resp_dup": "{call} ya estabas registrado en el concurso. 73 de {op}",
    "resp_mal": "PALABRA INCORRECTA, LA CLAVE ES: {clave}",
    "resp_fuera": "CONCURSO NO DISPONIBLE",
    "resp_pregunta": "Recibido {call}! QSL por mail? Responde MAIL tu@correo.com",
    "resp_mail_ok": "Gracias {call}! QSL enviada a {mail}. 73 de {op}",
    "resp_no": "Ok {call}. Confirmo QSL por APRS el {fecha}. 73 de {op}",
    "max_por_dia": 200,
    "auto": False,                     # generar y enviar la QSL por correo sin intervencion
    "q_freq": "", "q_band": "", "q_mode": "APRS", "q_rst_s": "59", "q_rst_r": "59",
    "q_comments": "Gracias por participar, 73!",
}
_bot_lock = threading.Lock()
_bot_resp = []                         # marcas de tiempo de respuestas, para limitar a 60 por hora
RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _norm(t):
    t = unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", t).split())


def bot_cfg():
    c = dict(BOT_DEFAULTS)
    try:
        with open(BOT_PATH) as f:
            c.update(json.load(f))
    except Exception:
        pass
    return c


def bot_guardar(c):
    tmp = BOT_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(c, f, indent=2, ensure_ascii=False)
    os.replace(tmp, BOT_PATH)


def bot_id(c):
    return _norm(c.get("nombre", "")) + "|" + str(c.get("inicio", ""))


def bot_en_ventana(c, ahora=None):
    ahora = ahora or datetime.now()
    try:
        if c.get("inicio") and ahora < datetime.strptime(c["inicio"], "%Y-%m-%dT%H:%M"):
            return False
        if c.get("fin") and ahora > datetime.strptime(c["fin"], "%Y-%m-%dT%H:%M"):
            return False
    except ValueError:
        return False
    return True


def _http_json(url, cuerpo=None, timeout=8):
    data = None if cuerpo is None else json.dumps(cuerpo).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def enviar_mensaje(destino, texto):
    """Mensaje APRS saliente con ACK y reintentos (igual que /api/aprs/send)."""
    texto = a_ascii(texto)[:67]
    if not texto or not ST["verificado"]:
        return False
    con = db()
    try:
        n = con.execute("SELECT COALESCE(MAX(CAST(msgid AS INTEGER)),0)+1 FROM messages WHERE dir='out'").fetchone()[0]
    finally:
        con.close()
    msgid = str(n if n < 100000 else 1)
    guardar_mensaje("out", destino, texto, msgid, "pendiente", 1, "bot")
    if enviar_paquete(paquete_mensaje(destino, texto, msgid)):
        con = db()
        try:
            con.execute("UPDATE messages SET intentos=1, prox=? WHERE dir='out' AND msgid=? AND peer=? AND intentos=0",
                        (time.time() + REINTENTOS[0], msgid, destino))
            con.commit()
        finally:
            con.close()
    return True


def _qtxt(b, k, n):
    return re.sub(r"[\x00-\x1f<>]", "", str(b.get(k, "") or "")).strip()[:n]


def bot_marcar(base, cid, estado):
    con = db()
    try:
        con.execute("UPDATE bot_part SET qsl=? WHERE concurso=? AND call=?", (estado[:100], cid, base))
        con.commit()
    finally:
        con.close()


def _err_http(e):
    try:
        return json.loads(e.read().decode("utf-8", "replace")).get("error") or str(e)
    except Exception:
        return str(e)


RE_CORREO = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+")
RE_CORREO_OK = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")


def _ocultar(mail):
    u, _, d = mail.partition("@")
    return (u[:1] + "***@" + d)[:30]


def _bot_decir(src, plantilla, base, **extra):
    """Responde por APRS respetando el limite de 60 respuestas por hora."""
    ts = time.time()
    with _bot_lock:
        _bot_resp[:] = [t for t in _bot_resp if ts - t < 3600]
        if len(_bot_resp) >= 60:
            return False
        _bot_resp.append(ts)
    txt = plantilla.replace("{call}", base).replace("{op}", CFG["callsign"].upper())
    for k, v in extra.items():
        txt = txt.replace("{%s}" % k, v)
    return enviar_mensaje(src, txt)


def _correo_conocido(base):
    """Busca el correo en la libreta de contactos, luego en la cola QSL y por ultimo en QRZ."""
    try:
        for c in _http_json("http://127.0.0.1:5018/api/contactos"):
            if c.get("call") == base and c.get("email"):
                return c["email"], "libreta"
    except Exception as e:
        print("bot: libreta no disponible: %s" % e, flush=True)
    try:
        for q in _http_json("http://127.0.0.1:5013/api/store/qslcola"):
            if str(q.get("callsign", "")).upper() == base and q.get("email"):
                return q["email"], "cola QSL"
    except Exception as e:
        print("bot: cola QSL no disponible: %s" % e, flush=True)
    try:
        r = _http_json("http://127.0.0.1:5015/api/qrz/lookup?call=%s&fresh=1" % base, timeout=20)
        if r.get("email"):
            return r["email"], "QRZ"
    except Exception as e:
        print("bot: consulta a QRZ fallo: %s" % e, flush=True)
    return None, ""


def _datos_qsl(base, c, ahora):
    return {"call": base, "op": CFG["callsign"].upper(), "time_utc": ahora, "freq": c.get("q_freq", ""),
            "band": c.get("q_band", ""), "mode": c.get("q_mode", "APRS"), "rst_s": c.get("q_rst_s", "59"),
            "rst_r": c.get("q_rst_r", "59"), "comments": c.get("q_comments", "")}


def _url_valida(u):
    return u if isinstance(u, str) and re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?/q/[A-Za-z0-9_-]{10}", u) else None


def bot_enviar_qsl(base, cid, c):
    """Dibuja y envia la QSL al correo de la cola QSL. False si fallo; si se envio, la URL temporal (str) o True si no hay URL."""
    bot_marcar(base, cid, "enviando...")
    ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = _http_json("http://127.0.0.1:5017/api/mail/auto", _datos_qsl(base, c, ahora), timeout=90)
        bot_marcar(base, cid, "enviada")
        return _url_valida(r.get("url")) or True
    except urllib.error.HTTPError as e:
        bot_marcar(base, cid, "error: " + _err_http(e))
    except Exception as e:
        bot_marcar(base, cid, "error: " + str(e))
    return False


def bot_enlace_solo(base, c):
    """Crea solo el enlace temporal (sin correo). Devuelve la URL o None."""
    ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        r = _http_json("http://127.0.0.1:5017/api/mail/enlace", _datos_qsl(base, c, ahora), timeout=60)
        return _url_valida(r.get("url"))
    except Exception as e:
        print("bot: no se pudo crear el enlace: %s" % e, flush=True)
        return None


def bot_decir_enlace(src, base, url):
    """Segundo mensaje APRS con el enlace temporal. Nunca se recorta (limite 67 caracteres)."""
    for t in ("QSL %s (10 min): %s" % (base, url), "QSL 10 min: " + url, url):
        if len(a_ascii(t)) <= 67:
            return _bot_decir(src, t, base)
    print("bot: el enlace es muy largo para APRS (%d caracteres)" % len(url), flush=True)
    return False


def bot_nuevo_participante(base, cid, src):
    """Participante nuevo: lo agrega a la cola QSL y, segun tenga o no correo, responde y envia o pregunta."""
    c = bot_cfg()
    ahora = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        _http_json("http://127.0.0.1:5013/api/store/qslcola",
                   {"items": [{"uid": "aprs|%s|%s" % (base, cid), "callsign": base, "time_utc": ahora,
                               "target": "", "mode": "APRS", "name": "", "country": "", "duration": 0}]})
    except Exception as e:
        print("bot: no se pudo agregar a la cola QSL: %s" % e, flush=True)
        bot_marcar(base, cid, "error al agregar a la cola")
        return
    email, origen = _correo_conocido(base)
    if not email:
        bot_marcar(base, cid, "esperando correo")
        _bot_decir(src, c["resp_pregunta"], base)
        return
    try:
        _http_json("http://127.0.0.1:5013/api/store/qslcola/email", {"emails": {base: email}})
    except Exception as e:
        print("bot: no se pudo guardar el correo en la cola: %s" % e, flush=True)
    _bot_decir(src, c["resp_ok"] if c.get("auto") else (c.get("resp_ok_manual") or BOT_DEFAULTS["resp_ok_manual"]), base)
    if c.get("auto"):
        url = bot_enviar_qsl(base, cid, c)
        if isinstance(url, str):
            bot_decir_enlace(src, base, url)
    else:
        bot_marcar(base, cid, "en cola")


def bot_respuesta_correo(src, base, texto, c):
    """Atiende la respuesta de un participante al que se le pregunto por su correo. True si el mensaje era para eso."""
    cid = bot_id(c)
    con = db()
    try:
        r = con.execute("SELECT qsl FROM bot_part WHERE concurso=? AND call=?", (cid, base)).fetchone()
    finally:
        con.close()
    if not r or not (r["qsl"].startswith("esperando correo") or r["qsl"].startswith("sin correo")):
        return False
    m = RE_CORREO.search(texto)
    if m and RE_CORREO_OK.match(m.group(0)) and len(m.group(0)) <= 120:
        email = m.group(0)
        try:                                   # libreta de contactos: nunca pisa un correo que ya exista
            lib = next((x for x in _http_json("http://127.0.0.1:5018/api/contactos") if x.get("call") == base), None)
            if lib is None or not lib.get("email"):
                _http_json("http://127.0.0.1:5018/api/contactos",
                           {"call": base, "email": email, "name": (lib or {}).get("name", ""),
                            "country": (lib or {}).get("country", ""),
                            "notes": (lib or {}).get("notes") or "Registrado por APRS"})
        except Exception as e:
            print("bot: no se pudo guardar en la libreta: %s" % e, flush=True)
        try:
            _http_json("http://127.0.0.1:5013/api/store/qslcola/email", {"emails": {base: email}})
        except Exception as e:
            print("bot: no se pudo guardar el correo en la cola: %s" % e, flush=True)
            bot_marcar(base, cid, "error al guardar el correo")
            return True
        if c.get("auto"):
            url = bot_enviar_qsl(base, cid, c)
            if url:
                _bot_decir(src, c["resp_mail_ok"], base, mail=_ocultar(email))
                if isinstance(url, str):
                    bot_decir_enlace(src, base, url)
        else:
            bot_marcar(base, cid, "en cola (correo recibido)")
            _bot_decir(src, "Gracias {call}! Anote tu correo y te enviare la QSL. 73 de {op}", base)
        return True
    if _norm(texto) in ("no", "no gracias", "nop", "nope"):
        bot_marcar(base, cid, "sin correo (prefirio no)")
        _bot_decir(src, c["resp_no"], base, fecha=datetime.now().strftime("%d/%m/%y"))
        if c.get("auto"):
            url = bot_enlace_solo(base, c)
            if url:
                bot_marcar(base, cid, "sin correo (prefirio no; enlace enviado)")
                bot_decir_enlace(src, base, url)
        return True
    return False


_bot_aviso_t = {}


def _fmt_f(v):
    try:
        return datetime.strptime(v, "%Y-%m-%dT%H:%M").strftime("%d/%m %H:%M")
    except (TypeError, ValueError):
        return ""


def _bot_avisar(src, base, plantilla, c):
    """Respuesta de palabra incorrecta / concurso no disponible (max 1 cada 30 s por indicativo)."""
    ahora = time.time()
    clave = a_ascii(" o ".join(str(p) for p in c["palabras"]))
    ex = {"inicio": _fmt_f(c.get("inicio", "")), "fin": _fmt_f(c.get("fin", ""))}
    resto = plantilla.replace("{call}", base).replace("{op}", CFG["callsign"].upper()).replace("{clave}", "")
    for k_, v_ in ex.items():
        resto = resto.replace("{%s}" % k_, v_)
    clave = clave[:max(0, 67 - len(a_ascii(resto)))]
    with _bot_lock:
        if ahora - _bot_aviso_t.get(base, 0) < 30:
            return
        _bot_aviso_t[base] = ahora
        if len(_bot_aviso_t) > 500:
            for k in [k for k, t in _bot_aviso_t.items() if ahora - t > 3600]:
                _bot_aviso_t.pop(k, None)
    if not _bot_aviso_permitido(base):
        return
    _bot_decir(src, plantilla, base, clave=clave, **ex)


def bot_procesar(src, texto, mid=None):
    try:
        c = bot_cfg()
        if not c["enabled"] or not c["palabras"] or src == mi_call().upper():
            return
        base = src.split("-")[0]
        if not re.fullmatch(r"[A-Z0-9]{3,8}", base) or not re.search(r"\d", base) or not re.search(r"[A-Z]", base):
            return
        if _bot_ritmo_excedido(base):
            return
        if bot_respuesta_correo(src, base, texto, c):
            _marcar_origen(mid, "concurso")
            return
        if not bot_en_ventana(c):
            _bot_avisar(src, base, c["resp_fuera"], c)
            return
        n = " " + _norm(texto) + " "
        if not any(" " + _norm(k) + " " in n for k in c["palabras"] if _norm(k)):
            print("bot: ignorado %s: sin palabra clave (%r)" % (src, texto[:40]), flush=True)
            _bot_avisar(src, base, c["resp_mal"], c)
            return
        _marcar_origen(mid, "concurso")
        cid = bot_id(c)
        ts = time.time()
        inicio_dia = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        con = db()
        try:
            hoy = con.execute("SELECT COUNT(*) FROM bot_part WHERE concurso=? AND ts>=?", (cid, inicio_dia)).fetchone()[0]
            ya = con.execute("SELECT aviso FROM bot_part WHERE concurso=? AND call=?", (cid, base)).fetchone()
            if ya is None and hoy >= int(c["max_por_dia"]):
                print("bot: tope diario alcanzado, se ignora %s" % base, flush=True)
                return
            if ya is None:
                con.execute("INSERT INTO bot_part(concurso,call,peer,ts,texto,aviso) VALUES(?,?,?,?,?,?)",
                            (cid, base, src, ts, texto[:67], 0))
                plantilla = None      # la respuesta la decide bot_nuevo_participante (con o sin correo)
            elif ts - ya["aviso"] > 3600:
                con.execute("UPDATE bot_part SET aviso=? WHERE concurso=? AND call=?", (ts, cid, base))
                plantilla = c["resp_dup"]
            else:
                plantilla = None
                print("bot: %s ya estaba registrado y ya se le aviso hace menos de 1 hora; sin respuesta" % base, flush=True)
            con.commit()
        finally:
            con.close()
        if ya is None:
            threading.Thread(target=bot_nuevo_participante, args=(base, cid, src), daemon=True).start()
        if plantilla:
            _bot_decir(src, plantilla, base)
    except Exception as e:
        print("bot: error: %s" % e, flush=True)


def procesar(linea):
    linea = linea.rstrip("\r\n")
    if not linea:
        return
    if linea.startswith("#"):
        if "logresp" in linea:
            ST["verificado"] = (" verified" in linea) and ("unverified" not in linea)
        return
    ST["paquetes"] += 1
    ST["ultimo_paquete"] = time.time()
    if ":" not in linea:
        return
    cab, payload = linea.split(":", 1)
    if not payload:
        return
    src = cab.split(">", 1)[0].strip().upper()
    if payload[0] == ":":
        procesar_mensaje(src, payload)
        return
    if src == mi_call().upper():
        return
    pos = parse_posicion(payload) or parse_con_aprslib(linea)
    if pos:
        registrar_estacion(src, pos)


# ---------------- hilos ----------------
def conectar_loop():
    global _sock
    espera = 5
    while True:
        s = None
        try:
            s = socket.create_connection((CFG["server"], int(CFG["port"])), timeout=15)
            s.settimeout(90)
            f = s.makefile("rb")
            f.readline()  # banner del servidor
            call = mi_call()
            pc = str(CFG.get("passcode") or "").strip() or str(calcular_passcode(call))
            filtro = f"r/{CFG['lat']:.4f}/{CFG['lon']:.4f}/{int(CFG['radius_km'])} g/{CFG['callsign'].upper()}*"
            s.sendall(f"user {call} pass {pc} vers pcbox-dashboard {VERSION} filter {filtro}\r\n".encode())
            with _sock_lock:
                _sock = s
            ST.update(conectado=True, verificado=False, error=None, desde=time.time(),
                      servidor=f"{CFG['server']}:{CFG['port']}")
            espera = 5
            for raw in f:
                try:
                    procesar(raw.decode("utf-8", "replace"))
                except Exception as e:
                    print(f"error procesando paquete: {e}", flush=True)
        except Exception as e:
            ST["error"] = str(e)
        finally:
            with _sock_lock:
                _sock = None
            ST.update(conectado=False, verificado=False)
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
        time.sleep(espera)
        espera = min(espera * 2, 120)


def reintentos_loop():
    while True:
        time.sleep(5)
        try:
            if not ST["verificado"]:
                continue
            con = db()
            try:
                filas = con.execute(
                    "SELECT * FROM messages WHERE dir='out' AND estado='pendiente' AND prox<=?",
                    (time.time(),)).fetchall()
                for m in filas:
                    if m["intentos"] > len(REINTENTOS):
                        con.execute("UPDATE messages SET estado='fallido' WHERE id=?", (m["id"],))
                        continue
                    if enviar_paquete(paquete_mensaje(m["peer"], m["text"], m["msgid"])):
                        k = m["intentos"] + 1
                        prox = time.time() + REINTENTOS[min(k - 1, len(REINTENTOS) - 1)]
                        con.execute("UPDATE messages SET intentos=?, prox=? WHERE id=?", (k, prox, m["id"]))
                con.commit()
            finally:
                con.close()
        except Exception as e:
            print(f"error en reintentos: {e}", flush=True)


def baliza_loop():
    while True:
        time.sleep(10)
        try:
            if CFG.get("beacon_enabled") and ST["verificado"]:
                intervalo = max(MIN_BALIZA_MIN, int(CFG.get("beacon_interval_min", 30))) * 60
                if time.time() - ST["ultima_baliza"] >= intervalo:
                    enviar_baliza()
        except Exception as e:
            print(f"error en baliza: {e}", flush=True)


# ---------------- API ----------------
@app.route("/api/aprs/status")
def api_status():
    con = db()
    try:
        sin_leer = con.execute("SELECT COUNT(*) FROM messages WHERE dir='in' AND leido=0").fetchone()[0]
    finally:
        con.close()
    with _lock:
        n_est = len(ESTACIONES)
    return jsonify({
        "indicativo": mi_call(), "conectado": ST["conectado"], "verificado": ST["verificado"],
        "servidor": ST["servidor"], "error": ST["error"], "paquetes": ST["paquetes"],
        "ultimo_paquete": ST["ultimo_paquete"], "estaciones": n_est, "radio_km": CFG["radius_km"],
        "sin_leer": sin_leer,
        "baliza": {"activa": bool(CFG.get("beacon_enabled")),
                   "intervalo_min": max(MIN_BALIZA_MIN, int(CFG.get("beacon_interval_min", 30))),
                   "ultima": ST["ultima_baliza"] or None},
    })


@app.route("/api/aprs/stations")
def api_stations():
    try:
        limite = min(200, max(1, int(request.args.get("limit", "50"))))
    except ValueError:
        limite = 50
    ahora = time.time()
    with _lock:
        for k in [k for k, e in ESTACIONES.items() if ahora - e["ts"] > int(CFG.get("station_ttl_h", 6)) * 3600]:
            ESTACIONES.pop(k, None)
        radio = CFG["radius_km"]
        lista = [e for e in ESTACIONES.values() if e["dist_km"] <= radio]
        q = re.sub(r"[^A-Z0-9/\-]", "", request.args.get("q", "").upper())
        if q:
            lista = [e for e in lista if q in e["call"]]
        lista = sorted(lista, key=lambda e: -e["ts"])[:limite]
        out = [dict(e, hace_s=int(ahora - e["ts"])) for e in lista]
    return jsonify(out)


@app.route("/api/aprs/messages")
def api_messages():
    con = db()
    try:
        filas = con.execute(
            "SELECT id, ts, dir, peer, text, estado, intentos, origen FROM messages ORDER BY id DESC LIMIT 100").fetchall()
    finally:
        con.close()
    return jsonify([dict(r) for r in reversed(filas)])


@app.route("/api/aprs/messages/read", methods=["POST"])
def api_read():
    con = db()
    try:
        con.execute("UPDATE messages SET leido=1 WHERE dir='in' AND leido=0")
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.route("/api/aprs/messages/delete", methods=["POST"])
def api_messages_delete():
    body = request.get_json(silent=True) or {}
    con = db()
    try:
        if body.get("all"):
            n = con.execute("DELETE FROM messages").rowcount
        else:
            n = 0
            for x in (body.get("ids") or [])[:200]:
                try:
                    n += con.execute("DELETE FROM messages WHERE id=?", (int(x),)).rowcount
                except (TypeError, ValueError):
                    pass
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True, "borrados": n})


@app.route("/api/aprs/send", methods=["POST"])
def api_send():
    body = request.get_json(silent=True) or {}
    destino = str(body.get("to", "")).strip().upper()
    texto = a_ascii(str(body.get("text", "")))[:67]
    if not re.fullmatch(r"[A-Z0-9]{1,6}(-\d{1,2})?", destino):
        return jsonify({"error": "indicativo destino invalido"}), 400
    if destino == mi_call().upper():
        return jsonify({"error": "no puedes enviarte mensajes a ti mismo"}), 400
    if not texto:
        return jsonify({"error": "mensaje vacio"}), 400
    if not ST["verificado"]:
        return jsonify({"error": "sin conexion verificada a APRS-IS"}), 503
    con = db()
    try:
        n = con.execute("SELECT COALESCE(MAX(CAST(msgid AS INTEGER)),0)+1 FROM messages WHERE dir='out'").fetchone()[0]
    finally:
        con.close()
    msgid = str(n if n < 100000 else 1)
    guardar_mensaje("out", destino, texto, msgid, "pendiente", 1)
    # el primer envio lo hace el hilo de reintentos en <5 s; lo adelantamos aqui
    if enviar_paquete(paquete_mensaje(destino, texto, msgid)):
        con = db()
        try:
            con.execute("UPDATE messages SET intentos=1, prox=? WHERE dir='out' AND msgid=? AND peer=? AND intentos=0",
                        (time.time() + REINTENTOS[0], msgid, destino))
            con.commit()
        finally:
            con.close()
    return jsonify({"ok": True, "msgid": msgid})


@app.route("/api/aprs/radius", methods=["POST"])
def api_radius():
    body = request.get_json(silent=True) or {}
    try:
        km = int(body.get("radius_km"))
    except (TypeError, ValueError):
        return jsonify({"error": "radio invalido"}), 400
    if not 5 <= km <= 500:
        return jsonify({"error": "el radio debe estar entre 5 y 500 km"}), 400
    with _lock:
        CFG["radius_km"] = km
    guardar_config()
    filtro = f"r/{CFG['lat']:.4f}/{CFG['lon']:.4f}/{km} g/{CFG['callsign'].upper()}*"
    enviar_paquete("#filter " + filtro)   # cambia el filtro en la conexion activa (si no hay, aplica al reconectar)
    return jsonify({"ok": True, "radio_km": km})


@app.route("/api/aprs/config", methods=["GET"])
def api_config_get():
    with _lock:
        c = dict(CFG)
    c["passcode_set"] = bool(str(c.pop("passcode", "") or "").strip())
    c["indicativo_completo"] = mi_call()
    return jsonify(c)


def _validar_config(b):
    """Devuelve (cambios_validos, error)."""
    out = {}
    if "callsign" in b:
        v = str(b["callsign"]).strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{3,9}", v):
            return None, "indicativo invalido (3 a 9 letras/numeros, sin SSID)"
        out["callsign"] = v
    if "ssid" in b:
        v = str(b["ssid"]).strip()
        if v not in [""] + [str(i) for i in range(16)]:
            return None, "SSID debe estar entre 0 y 15"
        out["ssid"] = v
    if "passcode" in b and b["passcode"] is not None:
        v = str(b["passcode"]).strip()
        if v and not (v.isdigit() and len(v) <= 5):
            return None, "passcode invalido (solo numeros)"
        out["passcode"] = v
    if "server" in b:
        v = str(b["server"]).strip()
        if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?", v):
            return None, "servidor invalido"
        out["server"] = v
    try:
        if "port" in b:
            v = int(b["port"])
            if not 1 <= v <= 65535:
                return None, "puerto invalido"
            out["port"] = v
        if "lat" in b:
            v = float(b["lat"])
            if not -90 <= v <= 90:
                return None, "latitud fuera de rango"
            out["lat"] = v
        if "lon" in b:
            v = float(b["lon"])
            if not -180 <= v <= 180:
                return None, "longitud fuera de rango"
            out["lon"] = v
        if "radius_km" in b:
            v = int(b["radius_km"])
            if not 5 <= v <= 500:
                return None, "el radio debe estar entre 5 y 500 km"
            out["radius_km"] = v
        if "beacon_interval_min" in b:
            out["beacon_interval_min"] = max(MIN_BALIZA_MIN, min(1440, int(b["beacon_interval_min"])))
        if "ambiguity" in b:
            v = int(b["ambiguity"])
            if not 0 <= v <= 4:
                return None, "ambiguedad entre 0 y 4"
            out["ambiguity"] = v
        if "station_ttl_h" in b:
            v = int(b["station_ttl_h"])
            if not 1 <= v <= 48:
                return None, "horas de memoria de estaciones: 1 a 48"
            out["station_ttl_h"] = v
        if "ui_refresh_s" in b:
            v = int(b["ui_refresh_s"])
            if not 5 <= v <= 300:
                return None, "actualizacion del panel: 5 a 300 s"
            out["ui_refresh_s"] = v
    except (TypeError, ValueError):
        return None, "hay un valor numerico invalido"
    if "beacon_enabled" in b:
        out["beacon_enabled"] = bool(b["beacon_enabled"])
    if "beacon_comment" in b:
        out["beacon_comment"] = a_ascii(str(b["beacon_comment"]))[:43]
    if "symbol_table" in b:
        v = str(b["symbol_table"])
        if v not in ("/", "\\"):
            return None, "tabla de simbolos: / o \\"
        out["symbol_table"] = v
    if "symbol" in b:
        v = str(b["symbol"])
        if not (len(v) == 1 and 33 <= ord(v) <= 126):
            return None, "simbolo invalido (1 caracter ASCII)"
        out["symbol"] = v
    return out, None


@app.route("/api/aprs/config", methods=["POST"])
def api_config_post():
    body = request.get_json(silent=True) or {}
    cambios, err = _validar_config(body)
    if err:
        return jsonify({"error": err}), 400
    claves_conexion = ("callsign", "ssid", "passcode", "server", "port", "lat", "lon")
    with _lock:
        reconectar = any(k in cambios and cambios[k] != CFG.get(k) for k in claves_conexion)
        radio_nuevo = "radius_km" in cambios and cambios["radius_km"] != CFG.get("radius_km")
        CFG.update(cambios)
    guardar_config()
    if reconectar:
        with _sock_lock:
            if _sock is not None:
                try:
                    _sock.close()      # conectar_loop reconecta solo con la configuracion nueva
                except Exception:
                    pass
    elif radio_nuevo:
        enviar_paquete(f"#filter r/{CFG['lat']:.4f}/{CFG['lon']:.4f}/{int(CFG['radius_km'])} g/{CFG['callsign'].upper()}*")
    return jsonify({"ok": True, "reconectando": reconectar, "indicativo_completo": mi_call()})


@app.route("/api/aprs/beacon", methods=["POST"])
def api_beacon():
    body = request.get_json(silent=True) or {}
    with _lock:
        if "enabled" in body:
            CFG["beacon_enabled"] = bool(body["enabled"])
        if "interval_min" in body:
            try:
                CFG["beacon_interval_min"] = max(MIN_BALIZA_MIN, min(1440, int(body["interval_min"])))
            except (TypeError, ValueError):
                return jsonify({"error": "intervalo invalido"}), 400
    guardar_config()
    enviado = None
    if body.get("send_now"):
        enviado = enviar_baliza()
        if not enviado:
            return jsonify({"error": "sin conexion verificada a APRS-IS"}), 503
    return jsonify({"ok": True, "enviado": enviado, "paquete": paquete_baliza() if enviado else None})


# BOTB1-INICIO
_REQ = {"t": 0.0, "d": None}


def bot_codigo(q):
    q = str(q or "").lower()
    for pref, cod in (("esperando correo", "esperando_correo"), ("sin correo", "sin_correo"), ("enviando", "enviando"),
                      ("enviada", "enviada"), ("error", "error"), ("en cola", "en_cola")):
        if q.startswith(pref):
            return cod
    return "nuevo" if not q else "otro"


def bot_sync_cola(cid, filas):
    # si una QSL se envio a mano desde la cola, el estado del participante lo refleja
    pend = [r for r in filas if bot_codigo(r["qsl"]) in ("en_cola", "error", "enviando")]
    if not pend:
        return {}
    try:
        estados = {q.get("uid"): q.get("estado") for q in _http_json("http://127.0.0.1:5013/api/store/qslcola", timeout=3)}
    except Exception:
        return {}
    cambios = {}
    for r in pend:
        if estados.get("aprs|%s|%s" % (r["call"], cid)) == "enviada":
            bot_marcar(r["call"], cid, "enviada (desde la cola)")
            cambios[r["call"]] = "enviada (desde la cola)"
    return cambios


def _req_get(url, timeout=3):
    try:
        return _http_json(url, timeout=timeout)
    except Exception:
        return None


def _es_servicio_enlaces(url):
    # el servicio de enlaces responde 404 con una pagina "Enlace vencido" para un token inexistente
    try:
        urllib.request.urlopen(url + "/q/aaaaaaaaaa", timeout=5)
        return True, ""
    except urllib.error.HTTPError as e:
        try:
            return b"Enlace vencido" in e.read(2000), ""
        except Exception:
            return False, ""
    except Exception as e:
        return False, str(e)


def _req_enlace():
    try:
        with open(os.path.join(BASE_DIR, "qsl_link.json")) as f:
            base = str(json.load(f).get("base", "")).strip().rstrip("/")
    except Exception:
        base = ""
    if not base:
        return "aviso", "sin enlace temporal (opcional): la QSL se envía solo por correo"
    if not re.fullmatch(r"https?://[A-Za-z0-9.\-]+(:\d+)?", base):
        return "error", "qsl_link.json tiene una dirección inválida: %s" % base[:50]
    ok, err = _es_servicio_enlaces(base)
    if ok:
        return "ok", "%s responde" % base[:60]
    if any(k in err for k in ("Name or service", "Temporary failure", "nodename nor servname", "getaddrinfo")):
        return "error", "el dominio %s no existe (¿quedó el nombre de ejemplo en qsl_link.json?)" % base[:60]
    if _es_servicio_enlaces("http://127.0.0.1:5023")[0]:
        return "error", "%s no responde desde internet (revisa Tailscale Funnel)" % base[:60]
    return "error", "el servicio de enlaces (puerto 5023) no está corriendo"


def bot_requisitos():
    ahora = time.time()
    if _REQ["d"] and ahora - _REQ["t"] < 15:
        return _REQ["d"]
    c = bot_cfg()
    out = []

    def add(id_, nombre, nivel, texto, tab=None):
        out.append({"id": id_, "nombre": nombre, "nivel": nivel, "texto": texto, "tab": tab})
    if c["palabras"]:
        add("palabras", "Palabras clave", "ok", "%d definida(s)" % len(c["palabras"]), "bot")
    else:
        add("palabras", "Palabras clave", "error", "no hay ninguna: el bot no atiende a nadie", "bot")
    if not c.get("inicio") and not c.get("fin"):
        add("ventana", "Período del concurso", "aviso", "sin fechas: el bot atiende siempre", "bot")
    elif bot_en_ventana(c):
        add("ventana", "Período del concurso", "ok", "dentro de las fechas", "bot")
    else:
        add("ventana", "Período del concurso", "aviso", "fuera de las fechas: responde \"concurso no disponible\"", "bot")
    if ST["conectado"] and ST["verificado"]:
        add("aprs", "APRS-IS", "ok", "conectado y verificado (%s)" % mi_call(), "aprs")
    elif ST["conectado"]:
        add("aprs", "APRS-IS", "error", "conectado pero sin verificar: no puede responder (revisa indicativo y passcode)", "aprs")
    else:
        add("aprs", "APRS-IS", "error", "sin conexión: no recibe ni responde mensajes", "aprs")
    if _req_get("http://127.0.0.1:5013/api/store/health") is None:
        add("cola", "Cola QSL (almacén)", "error", "el almacén no responde (puerto 5013)")
    else:
        add("cola", "Cola QSL (almacén)", "ok", "disponible")
    m = _req_get("http://127.0.0.1:5017/api/mail/status")
    if m is None:
        add("gmail", "Correo (Gmail)", "error" if c.get("auto") else "aviso", "el servicio de correo no responde (puerto 5017)", "gmail")
    elif not m.get("configured"):
        add("gmail", "Correo (Gmail)", "error" if c.get("auto") else "aviso",
            "sin configurar" + ("; el envío automático no puede enviar" if c.get("auto") else ""), "gmail")
    elif m.get("ok") is False:
        add("gmail", "Correo (Gmail)", "error", "el último envío falló: %s" % str(m.get("error"))[:80], "gmail")
    else:
        add("gmail", "Correo (Gmail)", "ok", "%s · %s de %s envíos hoy" % (m.get("address", ""), m.get("enviados_hoy", 0), m.get("limite_diario", "?")), "gmail")
    d = _req_get("http://127.0.0.1:5013/api/store/config/pcbox-qsl-design")
    if d is not None and not d.get("value"):
        add("diseno", "Diseño de la QSL", "aviso", "sin diseño guardado: se usará el diseño por defecto", "qsl")
    elif d is not None:
        add("diseno", "Diseño de la QSL", "ok", "guardado")
    import importlib.util as _ilu
    if _ilu.find_spec("PIL") is None:
        add("pillow", "Dibujo de la QSL (Pillow)", "error", "falta Pillow: sudo apt install -y python3-pil")
    nivel, texto = _req_enlace()
    add("enlace", "Enlace temporal de la QSL", nivel, texto)
    lib = _req_get("http://127.0.0.1:5018/api/contactos")
    if lib is None:
        add("libreta", "Libreta de contactos", "aviso", "no responde (puerto 5018); se usará la cola QSL y QRZ", "contactos")
    else:
        add("libreta", "Libreta de contactos", "ok", "%d contacto(s) con correo" % sum(1 for x in lib if x.get("email")), "contactos")
    q = _req_get("http://127.0.0.1:5015/api/qrz/status")
    if q is None:
        add("qrz", "QRZ.com", "aviso", "el servicio no responde (puerto 5015); el bot igual pregunta el correo por APRS", "qrz")
    elif not q.get("configured"):
        add("qrz", "QRZ.com", "aviso", "sin configurar; el bot igual pregunta el correo por APRS", "qrz")
    elif q.get("ok") is False:
        add("qrz", "QRZ.com", "aviso", "error: %s" % str(q.get("error"))[:60], "qrz")
    else:
        add("qrz", "QRZ.com", "ok", "configurado")
    res = {"checks": out, "errores": sum(1 for x in out if x["nivel"] == "error"),
           "avisos": sum(1 for x in out if x["nivel"] == "aviso")}
    _REQ.update(t=ahora, d=res)
    return res


@app.route("/api/aprs/bot/requisitos")
def api_bot_requisitos():
    return jsonify(bot_requisitos())
# BOTB1-FIN


# BOTB2-INICIO
_bot_ritmo = {}          # indicativo -> marcas de tiempo de los mensajes que ha enviado
_bot_silencio = {}       # indicativo -> hasta cuando se ignora
BOT_RITMO_MAX, BOT_RITMO_VENT, BOT_SILENCIO = 6, 600, 3600
_bot_avisos_h = []       # marcas de tiempo de los avisos (palabra incorrecta / fuera de fecha) de la ultima hora
_bot_avisos_call = {}    # indicativo -> marcas de tiempo de avisos en 24 h
BOT_AVISOS_HORA, BOT_AVISOS_DIA_CALL = 20, 3


def _bot_ritmo_excedido(base):
    # un indicativo que manda mas de 6 mensajes en 10 minutos se ignora 1 hora (posible bucle entre bots o inundacion)
    ahora = time.time()
    with _bot_lock:
        if _bot_silencio.get(base, 0) > ahora:
            return True
        marcas = [t for t in _bot_ritmo.get(base, []) if ahora - t < BOT_RITMO_VENT] + [ahora]
        _bot_ritmo[base] = marcas
        if len(_bot_ritmo) > 500:
            for k in [k for k, v in _bot_ritmo.items() if not v or ahora - v[-1] > BOT_RITMO_VENT]:
                _bot_ritmo.pop(k, None)
        if len(marcas) > BOT_RITMO_MAX:
            _bot_silencio[base] = ahora + BOT_SILENCIO
            print("bot: %s envio %d mensajes en %d min: se ignora %d min (posible bucle entre bots)"
                  % (base, len(marcas), BOT_RITMO_VENT // 60, BOT_SILENCIO // 60), flush=True)
            return True
    return False


def _bot_aviso_permitido(base):
    # los avisos tienen su propio tope: no pueden agotar las 60 respuestas por hora que necesitan los participantes
    ahora = time.time()
    with _bot_lock:
        _bot_avisos_h[:] = [t for t in _bot_avisos_h if ahora - t < 3600]
        propios = [t for t in _bot_avisos_call.get(base, []) if ahora - t < 86400]
        if len(_bot_avisos_h) >= BOT_AVISOS_HORA or len(propios) >= BOT_AVISOS_DIA_CALL:
            _bot_avisos_call[base] = propios
            return False
        _bot_avisos_h.append(ahora)
        _bot_avisos_call[base] = propios + [ahora]
        if len(_bot_avisos_call) > 500:
            for k in [k for k, v in _bot_avisos_call.items() if not v or ahora - v[-1] > 86400]:
                _bot_avisos_call.pop(k, None)
    return True


def _marcar_origen(mid, origen):
    if not mid:
        return
    con = db()
    try:
        con.execute("UPDATE messages SET origen=? WHERE id=?", (origen, mid))
        con.commit()
    finally:
        con.close()


@app.route("/api/aprs/bot/resumen")
def api_bot_resumen():
    c = bot_cfg()
    cid = bot_id(c)
    inicio_dia = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    con = db()
    try:
        filas = con.execute("SELECT call,ts,qsl FROM bot_part WHERE concurso=? ORDER BY ts DESC LIMIT 300", (cid,)).fetchall()
        total = con.execute("SELECT COUNT(*) FROM bot_part WHERE concurso=?", (cid,)).fetchone()[0]
    finally:
        con.close()
    cambios = bot_sync_cola(cid, filas)
    conteo, ultimos = {}, []
    for r in filas:
        q = cambios.get(r["call"], r["qsl"])
        cod = bot_codigo(q)
        conteo[cod] = conteo.get(cod, 0) + 1
        if len(ultimos) < 8:
            ultimos.append({"call": r["call"], "ts": r["ts"], "qsl": q, "cod": cod})
    if not c["enabled"]:
        estado = "apagado"
    elif not c["palabras"]:
        estado = "sin palabras clave"
    elif bot_en_ventana(c):
        estado = "activo"
    else:
        estado = "fuera de fecha"
    rq = bot_requisitos() if c["enabled"] else None
    return jsonify({"nombre": c["nombre"], "estado": estado, "total": total, "hoy": sum(1 for r in filas if r["ts"] >= inicio_dia),
                    "conteo": conteo, "ultimos": ultimos, "errores": rq["errores"] if rq else 0})
# BOTB2-FIN


@app.route("/api/aprs/bot", methods=["GET"])
def api_bot_get():
    c = bot_cfg()
    cid = bot_id(c)
    con = db()
    try:
        total = con.execute("SELECT COUNT(*) FROM bot_part WHERE concurso=?", (cid,)).fetchone()[0]
        filas = con.execute("SELECT call,peer,ts,texto,qsl FROM bot_part WHERE concurso=? ORDER BY ts DESC LIMIT 30",
                            (cid,)).fetchall()
    finally:
        con.close()
    if not c["enabled"]:
        estado = "Desactivado"
    elif not c["palabras"]:
        estado = "Activado, pero faltan palabras clave"
    elif bot_en_ventana(c):
        estado = "Activo: escuchando mensajes APRS" + ("" if ST["verificado"] else " (SIN conexión verificada a APRS-IS)")
    else:
        estado = "Activado, fuera del período del concurso"
    cambios = bot_sync_cola(cid, filas)
    part = []
    for r in filas:
        q = cambios.get(r["call"], r["qsl"])
        part.append({"call": r["call"], "peer": r["peer"], "ts": r["ts"], "texto": r["texto"], "qsl": q, "cod": bot_codigo(q)})
    return jsonify({"config": c, "estado": estado, "total": total, "participantes": part})


@app.route("/api/aprs/bot", methods=["POST"])
def api_bot_post():
    b = request.get_json(silent=True) or {}
    palabras = b.get("palabras", [])
    if isinstance(palabras, str):
        palabras = palabras.splitlines()
    limpias = []
    for p_ in palabras:
        p_ = str(p_).strip()
        if not p_:
            continue
        if len(p_) > 60 or not _norm(p_):
            return jsonify({"error": "palabra o frase inválida: %s" % p_[:30]}), 400
        if p_ not in limpias:
            limpias.append(p_)
    if len(limpias) > 20:
        return jsonify({"error": "máximo 20 palabras o frases"}), 400
    fechas = {}
    for k in ("inicio", "fin"):
        v = str(b.get(k, "") or "").strip()
        if v and not RE_FECHA.match(v):
            return jsonify({"error": "fecha de %s inválida" % k}), 400
        fechas[k] = v
    if fechas["inicio"] and fechas["fin"] and fechas["fin"] <= fechas["inicio"]:
        return jsonify({"error": "el fin debe ser posterior al inicio"}), 400
    try:
        mx = int(b.get("max_por_dia", 200))
        if not 1 <= mx <= 2000:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "máximo por día debe estar entre 1 y 2000"}), 400
    nombre = re.sub(r"[\x00-\x1f<>&\"'|]", "", str(b.get("nombre", "") or "")).strip()[:40]
    if not nombre:
        return jsonify({"error": "falta el nombre del concurso"}), 400
    resp_ok = str(b.get("resp_ok", "") or BOT_DEFAULTS["resp_ok"])[:200]
    resp_ok_manual = str(b.get("resp_ok_manual", "") or BOT_DEFAULTS["resp_ok_manual"])[:200]
    resp_dup = str(b.get("resp_dup", "") or BOT_DEFAULTS["resp_dup"])[:200]
    resp_pregunta = str(b.get("resp_pregunta", "") or BOT_DEFAULTS["resp_pregunta"])[:200]
    resp_mail_ok = str(b.get("resp_mail_ok", "") or BOT_DEFAULTS["resp_mail_ok"])[:200]
    resp_no = str(b.get("resp_no", "") or BOT_DEFAULTS["resp_no"])[:200]
    resp_mal = str(b.get("resp_mal", "") or BOT_DEFAULTS["resp_mal"])[:200]
    resp_fuera = str(b.get("resp_fuera", "") or BOT_DEFAULTS["resp_fuera"])[:200]
    for t_ in (resp_ok, resp_ok_manual, resp_dup, resp_pregunta, resp_mail_ok, resp_no, resp_mal, resp_fuera):
        peor = a_ascii(t_.replace("{call}", "AAAAAA9A").replace("{op}", CFG["callsign"].upper())
                       .replace("{clave}", "").replace("{inicio}", "01/01 00:00").replace("{fin}", "01/01 00:00").replace("{mail}", "j***@gmail.com").replace("{fecha}", "19/10/26"))
        if len(peor) > 67:
            return jsonify({"error": "una respuesta supera los 67 caracteres que permite APRS (%d)" % len(peor)}), 400
    c = {"enabled": bool(b.get("enabled")), "nombre": nombre, "palabras": limpias,
         "inicio": fechas["inicio"], "fin": fechas["fin"],
         "resp_ok": resp_ok, "resp_ok_manual": resp_ok_manual, "resp_dup": resp_dup,
         "resp_pregunta": resp_pregunta, "resp_mail_ok": resp_mail_ok, "resp_no": resp_no,
         "resp_mal": resp_mal, "resp_fuera": resp_fuera,
         "max_por_dia": mx, "auto": bool(b.get("auto")),
         "q_freq": _qtxt(b, "q_freq", 12), "q_band": _qtxt(b, "q_band", 8), "q_mode": _qtxt(b, "q_mode", 10),
         "q_rst_s": _qtxt(b, "q_rst_s", 6), "q_rst_r": _qtxt(b, "q_rst_r", 6),
         "q_comments": _qtxt(b, "q_comments", 120)}
    bot_guardar(c)
    return jsonify({"ok": True})


@app.route("/api/aprs/bot/reset", methods=["POST"])
def api_bot_reset():
    b = request.get_json(silent=True) or {}
    cid = bot_id(bot_cfg())
    con = db()
    try:
        if b.get("all"):
            con.execute("DELETE FROM bot_part WHERE concurso=?", (cid,))
        else:
            con.execute("DELETE FROM bot_part WHERE concurso=? AND call=?", (cid, str(b.get("call", "")).strip().upper()))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    cargar_config()
    for fn in (conectar_loop, reintentos_loop, baliza_loop):
        threading.Thread(target=fn, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
