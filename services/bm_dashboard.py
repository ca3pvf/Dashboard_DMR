import socketio
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from flask import Flask, jsonify, request

MI_ID = 0
MAX_ENTRIES = 6
MAX_TGS = 10
CONFIG_PATH = "/opt/pcbox/tgs_config.json"
NOMBRES_PATH = "/opt/pcbox/tg_nombres.json"
INACTIVO_MINUTOS = 30
SSH_TARGET = "pi-star@@@HS_IP@@"
LASTHEARD_URL = "http://127.0.0.1:5011/api/lastheard"

app = Flask(__name__)


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


sio = socketio.Client()
lock = threading.Lock()
despertar = threading.Event()

mis_contactos = []
tgs_actividad = {}
tg_activo_eventos = []
redes = []
nombres_cache = {}
MAX_RSSI = 2000
ultimo_rssi = {}   # indicativo -> (rssi, timestamp, TG destino)
act_eventos = []   # actividad reciente de los TGs elegidos en la tarjeta
MAX_ACT = 5


_cfg_cache = {"mtime": None, "data": None}


def leer_config():
    try:
        m = os.path.getmtime(CONFIG_PATH)
        if m != _cfg_cache["mtime"] or _cfg_cache["data"] is None:
            with open(CONFIG_PATH) as f:
                _cfg_cache["data"] = json.load(f)
            _cfg_cache["mtime"] = m
        return dict(_cfg_cache["data"])
    except Exception:
        return {"seguimiento": [], "activo": None}


def escribir_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def leer_nombres():
    try:
        with open(NOMBRES_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


# ---------- Redes según /etc/dmrgateway del hotspot ----------
def leer_redes():
    raw = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_TARGET, "cat /etc/dmrgateway"],
        capture_output=True, text=True, timeout=10,
    ).stdout
    out, cur = [], None
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r"^\[(.+)\]$", line)
        if m:
            sec = m.group(1)
            if sec.startswith("DMR Network") or sec == "XLX Network":
                cur = {"sec": sec, "kv": {}, "tgr": []}
                out.append(cur)
            else:
                cur = None
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.startswith("TGRewrite"):
            try:
                p = [int(x) for x in v.split(",")]
                if len(p) == 5:
                    cur["tgr"].append(tuple(p))
            except ValueError:
                pass
        else:
            cur["kv"][k] = v.strip()
    return out


def habilitada(r):
    return r["kv"].get("Enabled", "0") == "1"


def tipo_red(r):
    t = (r["kv"].get("Address", "") + " " + r["kv"].get("Name", "")).lower()
    if "brandmeister" in t:
        return "DMR BM"
    if "adn.systems" in t:
        return "ADN Systems"
    if "tgif" in t:
        return "TGIF"
    if "freestar" in t or "systemx" in t:
        return "SystemX"
    if r["sec"] == "XLX Network":
        return "XLX"
    if "127.0.0.1" in t:
        return "Cross-Mode"
    return "DMR Custom"


def resolver(tg):
    """Devuelve (red, TG en esa red) según las reglas TGRewrite; si no hay, la red con PassAllTG."""
    for r in redes:
        for (fs, ft, ts, tt, rng) in r["tgr"]:
            if ft <= tg < ft + rng:
                return r, tg - ft + tt
    for r in redes:
        if habilitada(r) and any(k.startswith("PassAllTG") for k in r["kv"]):
            return r, tg
    return None, tg


def bm_nombre(tg):
    try:
        with urllib.request.urlopen(f"https://api.brandmeister.network/v2/talkgroup/{tg}", timeout=5) as r:
            d = json.load(r)
        return d.get("Name") or d.get("name")
    except Exception:
        return None


def refrescar():
    global redes
    while True:
        try:
            nuevas = leer_redes()
            if nuevas:
                redes = nuevas
        except Exception as e:
            print(f"error leyendo dmrgateway: {e}")
        for tg in leer_config().get("seguimiento", []):
            r, tg_red = resolver(tg)
            if r and tipo_red(r) == "DMR BM":
                n = bm_nombre(tg_red)
                if n:
                    nombres_cache[tg] = n
        despertar.wait(300)
        despertar.clear()


def leer_lastheard():
    try:
        with urllib.request.urlopen(LASTHEARD_URL, timeout=3) as r:
            return json.load(r).get("transmissions", [])
    except Exception:
        return []


def ultimo_en(transmisiones, tg):
    for t in transmisiones:
        m = re.search(r"\d+", str(t.get("target", "")))
        if m and int(m.group()) == tg:
            return t.get("callsign")
    return None


# ---------- Socket BrandMeister ----------
@sio.event
def connect():
    print("conectado a BrandMeister")
    sio.emit("join", "everything")


@sio.event
def disconnect():
    print("desconectado")


@sio.on("mqtt")
def on_mqtt(data):
    try:
        raw = data["payload"]
    except (KeyError, TypeError):
        return
    if isinstance(raw, str) and "Session-Stop" not in raw:
        return   # la gran mayoría de los eventos: se descarta sin parsear

    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return

    if payload.get("Event") != "Session-Stop" or not payload.get("SourceCall"):
        return

    src = payload.get("SourceID")
    dst = payload.get("DestinationID")
    ts = payload.get("Start", 0)

    rssi = payload.get("RSSI")
    if rssi is not None:
        with lock:
            ultimo_rssi[str(payload["SourceCall"]).upper()] = (rssi, time.time(), dst)
            if len(ultimo_rssi) > MAX_RSSI:
                for k in sorted(ultimo_rssi, key=lambda c: ultimo_rssi[c][1])[:200]:
                    ultimo_rssi.pop(k, None)

    config = leer_config()
    if (src != MI_ID and dst not in config.get("seguimiento", []) and dst != config.get("activo")
            and dst not in config.get("actividad", [])):
        return   # no es mío ni de un TG seguido o vigilado

    entry = {
        "callsign": payload.get("SourceCall"),
        "name": payload.get("SourceName"),
        "destination": dst,
        "rssi": payload.get("RSSI"),
        "time": datetime.fromtimestamp(ts).strftime("%H:%M:%S"),
        "timestamp": ts,
    }

    with lock:
        if src == MI_ID:
            mis_contactos.insert(0, entry)
            del mis_contactos[MAX_ENTRIES:]
        if dst in config.get("seguimiento", []):
            tgs_actividad[dst] = entry
        if dst == config.get("activo"):
            tg_activo_eventos.insert(0, entry)
            del tg_activo_eventos[MAX_ENTRIES:]
        if dst in config.get("actividad", []):
            act_eventos.insert(0, entry)
            del act_eventos[50:]


# ---------- Endpoints ----------
@app.route("/contactos")
def contactos():
    with lock:
        return jsonify(mis_contactos)


@app.route("/tgs")
def tgs():
    config = leer_config()
    with lock:
        resultado = []
        for tg in config.get("seguimiento", []):
            info = tgs_actividad.get(tg)
            resultado.append({
                "tg": tg,
                "ultimo": info["callsign"] if info else None,
                "hora": info["time"] if info else None,
            })
        return jsonify(resultado)


@app.route("/tgs-detalle")
def tgs_detalle():
    cfg = leer_config()
    locales = leer_nombres()
    with lock:
        actividad = dict(tgs_actividad)
    hs = None
    out = []
    for tg in cfg.get("seguimiento", []):
        r, tg_red = resolver(tg)
        tipo = tipo_red(r) if r else None
        ultimo = None
        if tipo == "DMR BM" and tg in actividad:
            ultimo = actividad[tg]["callsign"]
        if ultimo is None:
            if hs is None:
                hs = leer_lastheard()
            ultimo = ultimo_en(hs, tg)
        out.append({
            "tg": tg,
            "tg_red": tg_red,
            "protocolo": tipo,
            "red": r["kv"].get("Name") if r else None,
            "habilitada": habilitada(r) if r else None,
            "nombre": locales.get(str(tg)) or nombres_cache.get(tg),
            "ultimo": ultimo,
        })
    return jsonify(out)


@app.route("/config-tgs", methods=["GET", "POST"])
def config_tgs():
    cfg = leer_config()
    if request.method == "POST":
        try:
            body = json.loads(request.get_data(as_text=True) or "{}")
            crudo = body.get("seguimiento", "")
            if isinstance(crudo, str):
                crudo = crudo.split(",")
            nuevos = []
            for x in crudo:
                x = str(x).strip()
                if not x:
                    continue
                if not x.isdigit() or int(x) < 1:
                    raise ValueError(f"TG inválido: {x}")
                if int(x) not in nuevos:
                    nuevos.append(int(x))
            if len(nuevos) > MAX_TGS:
                raise ValueError(f"máximo {MAX_TGS} TGs")
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        cfg["seguimiento"] = nuevos
        escribir_config(cfg)
        despertar.set()
    return jsonify({"seguimiento": cfg.get("seguimiento", [])})


@app.route("/inactivos")
def inactivos():
    config = leer_config()
    ahora = time.time()
    with lock:
        resultado = []
        for tg in config.get("seguimiento", []):
            info = tgs_actividad.get(tg)
            minutos = None if info is None else round((ahora - info["timestamp"]) / 60)
            if minutos is None or minutos >= INACTIVO_MINUTOS:
                resultado.append({"tg": tg, "silencio_min": minutos})
        resultado.sort(key=lambda x: (x["silencio_min"] is not None, x["silencio_min"] or 0), reverse=True)
        return jsonify(resultado)


@app.route("/rssi")
def rssi_de():
    call = re.sub(r"[^A-Z0-9/]", "", request.args.get("call", "").upper())
    with lock:
        r = ultimo_rssi.get(call)
    if not r or not r[0]:
        return jsonify({"call": call, "rssi": None})
    return jsonify({"call": call, "rssi": r[0], "hace_s": round(time.time() - r[1])})


@app.route("/actividad-config", methods=["GET", "POST"])
def actividad_config():
    cfg = leer_config()
    if request.method == "POST":
        try:
            body = json.loads(request.get_data(as_text=True) or "{}")
            crudo = body.get("actividad", "")
            if isinstance(crudo, str):
                crudo = crudo.split(",")
            nuevos = []
            for x in crudo:
                x = str(x).strip()
                if not x:
                    continue
                if not x.isdigit() or int(x) < 1:
                    raise ValueError("TG inválido: %s" % x)
                if int(x) not in nuevos:
                    nuevos.append(int(x))
            if len(nuevos) > MAX_ACT:
                raise ValueError("máximo %d TGs" % MAX_ACT)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        cfg["actividad"] = nuevos
        escribir_config(cfg)
    return jsonify({"actividad": cfg.get("actividad", [])})


@app.route("/actividad")
def actividad():
    act = set(leer_config().get("actividad", []))
    with lock:
        return jsonify([e for e in act_eventos if e["destination"] in act][:5])


@app.route("/tg-activo")
def tg_activo():
    config = leer_config()
    with lock:
        return jsonify({"tg": config.get("activo"), "eventos": tg_activo_eventos})


def start_socketio():
    while True:
        try:
            sio.connect(url="https://api.brandmeister.network", socketio_path="/lh/socket.io", transports="websocket")
            sio.wait()
        except Exception as e:
            print(f"error de conexión: {e}, reintentando en 10s")
            time.sleep(10)


if __name__ == "__main__":
    threading.Thread(target=start_socketio, daemon=True).start()
    threading.Thread(target=refrescar, daemon=True).start()
    app.run(host="0.0.0.0", port=5007)
