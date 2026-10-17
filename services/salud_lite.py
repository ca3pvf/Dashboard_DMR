#!/usr/bin/env python3
"""Salud del sistema para la version portable (reemplaza a status-api.py, que depende de systemd).
  GET  /status                      LEDs de la tarjeta Sistema
  GET  /api/salud                   chequeos + eventos (mismo formato que en pcbox)
  POST /api/salud/eventos/borrar
Chequeos: hotspot en la red, API web del hotspot, internet, RAM y disco de este equipo."""
import json, os, socket, threading, time, urllib.request
from collections import deque
import psutil
from flask import Flask, jsonify

app = Flask(__name__)
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HS_FILE = os.path.join(BASE, "hotspots.json")
_lock = threading.Lock()
_checks, _nivel_prev = {}, {}
_eventos = deque(maxlen=40)
_lentas = 0


def _hotspot():
    try:
        h = json.load(open(HS_FILE))["hotspots"][0]
        return h["ip"], int(h.get("http_port") or 80)
    except Exception:
        return None, 80


def registrar(clave, nombre, nivel, texto):
    with _lock:
        _checks[clave] = {"clave": clave, "nombre": nombre, "nivel": nivel, "texto": texto}
        previo = _nivel_prev.get(clave)
        if previo != nivel:
            if not (previo is None and nivel == "ok"):
                _eventos.appendleft({"ts": time.time(), "nombre": nombre, "nivel": nivel,
                                     "texto": ("Recuperado: " + texto) if nivel == "ok" else texto})
            _nivel_prev[clave] = nivel


def chequear_hotspot():
    global _lentas
    ip, puerto = _hotspot()
    if not ip:
        registrar("hs_red", "Hotspot en la red", "aviso", "sin hotspot configurado")
        return
    t0 = time.time()
    try:
        socket.create_connection((ip, puerto), timeout=3).close()
        registrar("hs_red", "Hotspot en la red", "ok", "responde (%d ms)" % ((time.time() - t0) * 1000))
    except Exception as e:
        registrar("hs_red", "Hotspot en la red", "error", "sin respuesta (%s)" % e.__class__.__name__)
        return
    t0 = time.time()
    try:
        url = "http://%s%s/api/?limit=1" % (ip, "" if puerto == 80 else ":%d" % puerto)
        with urllib.request.urlopen(url, timeout=20) as r:
            r.read(2048)
        d = time.time() - t0
        _lentas = _lentas + 1 if d >= 8 else 0      # aviso solo con 2 mediciones lentas seguidas
        if _lentas >= 2:
            registrar("hs_api", "API web del hotspot", "aviso", "lenta: %.1f s" % d)
        else:
            registrar("hs_api", "API web del hotspot", "ok", "%.1f s" % d)
    except Exception as e:
        registrar("hs_api", "API web del hotspot", "error", "no responde (%s)" % str(e)[:60])


def chequear_internet():
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=4).close()
        registrar("internet", "Internet", "ok", "conectado")
    except Exception:
        registrar("internet", "Internet", "aviso", "sin salida a internet (QRZ, BrandMeister y sismos no funcionarán)")


def chequear_recursos():
    disp = psutil.virtual_memory().available // (1024 * 1024)
    registrar("ram", "RAM de este equipo", "ok" if disp >= 300 else ("aviso" if disp >= 150 else "error"),
              "%d MB disponibles" % disp)
    uso = psutil.disk_usage(os.path.abspath(os.sep)).percent
    registrar("disco", "Disco de este equipo", "ok" if uso < 90 else ("aviso" if uso < 95 else "error"),
              "%d%% usado" % uso)


def bucle():
    while True:
        for f in (chequear_hotspot, chequear_internet, chequear_recursos):
            try:
                f()
            except Exception as e:
                print("salud: %s fallo: %s" % (f.__name__, e), flush=True)
        time.sleep(60)


@app.route("/status")
def status():
    with _lock:
        c = dict(_checks)
    est = lambda k: "ok" if c.get(k, {}).get("nivel") == "ok" else ("warn" if c.get(k, {}).get("nivel") == "aviso" else "bad")
    return jsonify({"dashboard": "ok", "hotspot": est("hs_red"), "internet": est("internet")})


@app.route("/api/salud")
def salud():
    with _lock:
        checks = list(_checks.values())
        ev = list(_eventos)
    orden = {"error": 0, "aviso": 1, "ok": 2}
    checks.sort(key=lambda c: (orden.get(c["nivel"], 3), c["nombre"]))
    return jsonify({"actualizado": time.time(), "listo": len(checks) > 0,
                    "resumen": {n: sum(1 for c in checks if c["nivel"] == n) for n in ("ok", "aviso", "error")},
                    "checks": checks, "eventos": ev})


@app.route("/api/salud/eventos/borrar", methods=["POST"])
def borrar():
    with _lock:
        n = len(_eventos)
        _eventos.clear()
    return jsonify({"ok": True, "borrados": n})
