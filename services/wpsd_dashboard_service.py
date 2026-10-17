#!/usr/bin/env python3
"""
wpsd_dashboard_service.py - estado de hotspots WPSD, multi-hotspot (hasta 5). Puerto 5008.

  GET  /api/wpsd?hs=N   datos del hotspot N (WPSD por SSH si está vinculado + BrandMeister por DMR ID)
  GET  /api/wpsd/all    resumen de estado de todos los hotspots (franja de LEDs del dashboard)
  GET  /api/hotspots    lista configurada
  POST /api/hotspots    guarda la lista (máx. 5). Las contraseñas NO se guardan.

La lista vive en hotspots.json (misma carpeta). Requisitos para hotspots con SSH vinculado:
  - SSH sin contraseña: pcbox -> usuario@IP
  - sudo NOPASSWD en el hotspot para /usr/local/bin/RemoteCommand
Los hotspots sin SSH se monitorean por conexión TCP a su web (estado) y BrandMeister (si hay DMR ID).
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime

from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTSPOTS_PATH = os.environ.get("HOTSPOTS_PATH", os.path.join(BASE, "hotspots.json"))
PORT = int(os.environ.get("WPSD_PORT", "5008"))
SLOTS = 5
REFRESH_ACTIVE = 15      # hotspot que se está mirando en el dashboard
REFRESH_IDLE = 45        # los demás
ACTIVE_WINDOW = 60
BM_TTL = 60
DEFAULT_LIST = [{"id": 1, "name": "Hotspot 1", "ip": "@@HS_IP@@", "user": "pi-star",
                 "remote_port": "7642", "dmr_id": "", "ssh": True, "enabled": True,
                 "http_port": 80}]

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


_file_lock = threading.Lock()
_lock = threading.Lock()
_cache = {s: None for s in range(1, SLOTS + 1)}
_last_req = {s: 0.0 for s in range(1, SLOTS + 1)}
_wake = {s: threading.Event() for s in range(1, SLOTS + 1)}
_bm = {s: (0.0, None, None) for s in range(1, SLOTS + 1)}   # (timestamp, dmr_id, info)

RE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
RE_USER = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")


# ---------------- lista de hotspots ----------------
def leer_lista():
    with _file_lock:
        try:
            with open(HOTSPOTS_PATH) as f:
                lista = json.load(f).get("hotspots", [])
            if isinstance(lista, list) and lista:
                return lista
        except FileNotFoundError:
            pass
        except Exception as e:
            print("hotspots.json ilegible: %s" % e, flush=True)
    return [dict(h) for h in DEFAULT_LIST]


def escribir_lista(lista):
    with _file_lock:
        tmp = HOTSPOTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"hotspots": lista}, f, indent=2)
        os.replace(tmp, HOTSPOTS_PATH)


def hotspot(slot):
    for h in leer_lista():
        if int(h.get("id", 0)) == slot and h.get("enabled", True) and h.get("ip"):
            return h
    return None


def validar(entrada, existentes):
    """Devuelve (hotspot_limpio, error)."""
    try:
        n = int(entrada.get("id"))
    except (TypeError, ValueError):
        return None, "id inválido"
    if not 1 <= n <= SLOTS:
        return None, "el id debe estar entre 1 y %d" % SLOTS
    ip = str(entrada.get("ip", "")).strip()
    if not RE_HOST.match(ip):
        return None, "HS %d: IP o nombre de host inválido" % n
    user = str(entrada.get("user", "") or "pi-star").strip()
    if not RE_USER.match(user):
        return None, "HS %d: usuario inválido" % n
    port = str(entrada.get("remote_port", "") or "7642").strip()
    if not (port.isdigit() and 1 <= int(port) <= 65535):
        return None, "HS %d: puerto de RemoteCommand inválido" % n
    dmr = str(entrada.get("dmr_id", "") or "").strip()
    if dmr and not (dmr.isdigit() and 7 <= len(dmr) <= 9):
        return None, "HS %d: DMR ID debe tener entre 7 y 9 dígitos" % n
    nombre = re.sub(r"[\x00-\x1f<>&\"']", "", str(entrada.get("name", "") or "")).strip()[:24] or ("Hotspot %d" % n)
    previo = next((h for h in existentes if int(h.get("id", 0)) == n), {})
    http_port = entrada.get("http_port", previo.get("http_port", 80))
    try:
        http_port = int(http_port)
        if not 1 <= http_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return None, "HS %d: puerto web inválido" % n
    return {"id": n, "name": nombre, "ip": ip, "user": user, "remote_port": port, "dmr_id": dmr,
            "ssh": bool(entrada.get("ssh")), "enabled": bool(entrada.get("enabled", True)),
            "http_port": http_port}, None


# ---------------- consultas al hotspot ----------------
def ssh_run(hs, cmd, timeout=15):
    try:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new", "%s@%s" % (hs["user"], hs["ip"]), cmd],
            capture_output=True, text=True, timeout=timeout)
        if result.returncode != 0:
            return None
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return None


def estado_vacio():
    return {"dstar": "n/a", "dmr": "n/a", "ysf": "n/a", "p25": "n/a", "nxdn": "n/a", "fm": "n/a"}


def get_network_status(hs):
    output = ssh_run(hs, "sudo /usr/local/bin/RemoteCommand %s status" % hs["remote_port"])
    status = estado_vacio()
    if output is None:
        return status, False
    for match in re.finditer(r'(\w+):(\S+)', output):
        key, val = match.groups()
        if key in status:
            status[key] = val
    return status, True


def tcp_ok(ip, puerto):
    try:
        with socket.create_connection((ip, int(puerto)), timeout=3):
            return True
    except OSError:
        return False


def get_brandmeister_info(slot, dmr_id):
    ts, dmr_prev, info = _bm[slot]
    if dmr_prev == dmr_id and time.time() - ts < BM_TTL:
        return info
    try:
        req = urllib.request.Request("https://api.brandmeister.network/v2/device/%s" % dmr_id,
                                     headers={"User-Agent": "pcbox-dashboard"})
        with urllib.request.urlopen(req, timeout=10) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        info = None
    _bm[slot] = (time.time(), dmr_id, info)
    return info


def refrescar(slot, hs):
    if hs.get("ssh"):
        net, reachable = get_network_status(hs)
        uptime = (ssh_run(hs, "uptime -p") or "desconocido") if reachable else "desconocido"
    else:
        net, reachable, uptime = estado_vacio(), tcp_ok(hs["ip"], hs.get("http_port", 80)), "desconocido"
    bm = get_brandmeister_info(slot, hs["dmr_id"]) if hs.get("dmr_id") else None
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "hs": {"id": slot, "name": hs["name"], "ip": hs["ip"], "ssh": bool(hs.get("ssh"))},
        "wpsd": {"hostname": hs["name"], "dmr_id": hs.get("dmr_id", ""), "uptime": uptime,
                 "network_status": net, "reachable": reachable},
        "brandmeister": {
            "connected": bm is not None,
            "master": bm.get("lastKnownMaster") if bm else None,
            "status_text": bm.get("statusText") if bm else None,
            "last_seen": bm.get("last_seen") if bm else None,
            "callsign": bm.get("callsign") if bm else None,
        },
    }
    with _lock:
        _cache[slot] = payload


def worker(slot):
    while True:
        hs = hotspot(slot)
        if not hs:
            with _lock:
                _cache[slot] = None
            _wake[slot].wait(10)
            _wake[slot].clear()
            continue
        try:
            refrescar(slot, hs)
        except Exception as e:
            print("error refrescando HS %d: %s" % (slot, e), flush=True)
        activo = time.time() - _last_req[slot] < ACTIVE_WINDOW
        _wake[slot].wait(REFRESH_ACTIVE if activo else REFRESH_IDLE)
        _wake[slot].clear()


# ---------------- API ----------------
def slot_param():
    try:
        n = int(request.args.get("hs", "1"))
    except ValueError:
        return 1
    return n if 1 <= n <= SLOTS else 1


@app.route("/api/wpsd")
def api_wpsd():
    slot = slot_param()
    hs = hotspot(slot)
    if not hs:
        return jsonify({"error": "hotspot no configurado"}), 404
    inactivo = time.time() - _last_req[slot] >= ACTIVE_WINDOW
    _last_req[slot] = time.time()
    if inactivo:
        _wake[slot].set()
    with _lock:
        datos = _cache[slot]
    if datos is None:
        datos = {"updated_at": None,
                 "hs": {"id": slot, "name": hs["name"], "ip": hs["ip"], "ssh": bool(hs.get("ssh"))},
                 "wpsd": {"hostname": hs["name"], "dmr_id": hs.get("dmr_id", ""), "uptime": "desconocido",
                          "network_status": estado_vacio(), "reachable": False},
                 "brandmeister": {"connected": False, "master": None, "status_text": None,
                                  "last_seen": None, "callsign": None}}
    return jsonify(datos)


@app.route("/api/wpsd/all")
def api_wpsd_all():
    ahora = time.time()
    out = []
    for h in sorted(leer_lista(), key=lambda x: int(x.get("id", 0))):
        n = int(h.get("id", 0))
        if not 1 <= n <= SLOTS:
            continue
        with _lock:
            c = _cache[n]
        reachable, edad = None, None
        if c and c.get("updated_at"):
            edad = round(ahora - datetime.fromisoformat(c["updated_at"]).timestamp())
            if edad < 150:
                reachable = c["wpsd"]["reachable"]
        out.append({"id": n, "name": h.get("name"), "ip": h.get("ip"),
                    "enabled": bool(h.get("enabled", True)), "ssh": bool(h.get("ssh")),
                    "reachable": reachable, "age": edad})
    return jsonify({"hotspots": out})


@app.route("/api/hotspots", methods=["GET", "POST"])
def api_hotspots():
    actuales = leer_lista()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        entradas = body.get("hotspots")
        if not isinstance(entradas, list) or not 1 <= len(entradas) <= SLOTS:
            return jsonify({"error": "se necesita entre 1 y %d hotspots" % SLOTS}), 400
        limpios, ids = [], set()
        for e in entradas:
            if not isinstance(e, dict):
                return jsonify({"error": "formato inválido"}), 400
            h, err = validar(e, actuales)
            if err:
                return jsonify({"error": err}), 400
            if h["id"] in ids:
                return jsonify({"error": "id repetido: %d" % h["id"]}), 400
            ids.add(h["id"])
            limpios.append(h)
        limpios.sort(key=lambda x: x["id"])
        escribir_lista(limpios)
        for ev in _wake.values():
            ev.set()
        actuales = limpios
    return jsonify({"hotspots": actuales})


if __name__ == "__main__":
    if not os.path.exists(HOTSPOTS_PATH):
        escribir_lista([dict(h) for h in DEFAULT_LIST])
    for s in range(1, SLOTS + 1):
        threading.Thread(target=worker, args=(s,), daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
