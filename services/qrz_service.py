#!/usr/bin/env python3
"""Consulta de QRZ.com (API XML, requiere suscripcion) con cache. Puerto 5015.
  GET  /api/qrz/status
  POST /api/qrz/config   {"username": "...", "password": "..."}  (prueba el acceso antes de guardar)
  POST /api/qrz/clear    borra las credenciales
  GET  /api/qrz/lookup?call=XX
  POST /api/qrz/lookup-batch  {"calls": ["XX", ...]}   (max 40)
Las credenciales viven solo en qrz_config.json (permisos 600) y nunca se devuelven."""
import json
import os
import re
import sqlite3
import threading
import time
import xml.etree.ElementTree as ET

import requests
from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("QRZ_CONFIG", os.path.join(BASE, "qrz_config.json"))
DB_PATH = os.environ.get("QRZ_DB", os.path.join(BASE, "qrz_cache.db"))
PORT = 5015
URL = "https://xmldata.qrz.com/xml/current/"
AGENT = "pcbox-dashboard-1.0"
CACHE_FOUND_S = 7 * 24 * 3600
CACHE_MISS_S = 24 * 3600
MIN_GAP = 1.0
MAX_BATCH = 40

app = Flask(__name__)
_qlock = threading.Lock()
_key = None
ST = {"ok": None, "error": None, "sub": None, "last": 0.0}


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


# ---------- credenciales ----------
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
        json.dump(d, f)
    os.replace(tmp, CONFIG_PATH)
    os.chmod(CONFIG_PATH, 0o600)


# ---------- cache ----------
def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("CREATE TABLE IF NOT EXISTS cache (call TEXT PRIMARY KEY, data TEXT NOT NULL, ts REAL NOT NULL)")
    return con


def cache_get(call):
    con = db()
    try:
        r = con.execute("SELECT data, ts FROM cache WHERE call=?", (call,)).fetchone()
    finally:
        con.close()
    if not r:
        return None
    data = json.loads(r[0])
    ttl = CACHE_FOUND_S if (data.get("found") and data.get("email")) else (600 if data.get("found") else CACHE_MISS_S)
    return data if time.time() - r[1] < ttl else None


def cache_put(call, data):
    con = db()
    try:
        con.execute("INSERT OR REPLACE INTO cache(call,data,ts) VALUES(?,?,?)",
                    (call, json.dumps(data, ensure_ascii=False), time.time()))
        con.commit()
    finally:
        con.close()


# ---------- QRZ ----------
def _parse(content):
    root = ET.fromstring(content)
    res = {}
    for sec in root:
        res[sec.tag.split("}")[-1]] = {el.tag.split("}")[-1]: (el.text or "").strip() for el in sec}
    return res


def _login(user, pwd):
    r = requests.get(URL, params={"username": user, "password": pwd, "agent": AGENT}, timeout=15)
    r.raise_for_status()
    ses = _parse(r.content).get("Session", {})
    if ses.get("Key"):
        return ses["Key"], ses, None
    return None, ses, ses.get("Error") or "no se pudo iniciar sesion"


def _base_call(call):
    partes = [p for p in re.split(r"/", str(call).upper().strip()) if p]
    if not partes:
        return None
    c = max(partes, key=len)
    return c if re.fullmatch(r"[A-Z0-9]{3,10}", c) else None


def _qrz_lookup(call):
    """Llamar con _qlock tomado."""
    global _key
    c = leer_cfg()
    if not c.get("username") or not c.get("password"):
        return {"found": False, "error": "QRZ sin configurar"}
    for _ in (1, 2):
        if not _key:
            _key, ses, err = _login(c["username"], c["password"])
            if not _key:
                ST.update(ok=False, error=err)
                return {"found": False, "error": err}
            ST["sub"] = ses.get("SubExp")
        espera = MIN_GAP - (time.time() - ST["last"])
        if espera > 0:
            time.sleep(espera)
        r = requests.get(URL, params={"s": _key, "callsign": call, "agent": AGENT}, timeout=15)
        ST["last"] = time.time()
        d = _parse(r.content)
        if "Callsign" in d:
            x = d["Callsign"]
            ST.update(ok=True, error=None)
            return {"found": True, "call": x.get("call", call),
                    "name": " ".join(v for v in (x.get("fname", ""), x.get("name", "")) if v),
                    "email": x.get("email", ""), "country": x.get("country", ""), "state": x.get("state", "")}
        err = d.get("Session", {}).get("Error", "")
        if err.startswith("Session Timeout") or "session key" in err.lower():
            _key = None
            continue
        if "not found" in err.lower():
            return {"found": False}
        ST.update(ok=False, error=err or "respuesta inesperada")
        return {"found": False, "error": err or "respuesta inesperada"}
    return {"found": False, "error": "no se pudo renovar la sesion"}


def lookup(call):
    base = _base_call(call)
    if not base:
        return {"found": False, "error": "indicativo invalido"}
    hit = cache_get(base)
    if hit is not None:
        return hit
    with _qlock:
        hit = cache_get(base)
        if hit is not None:
            return hit
        try:
            res = _qrz_lookup(base)
        except Exception as e:
            ST.update(ok=False, error=str(e))
            return {"found": False, "error": str(e)}
    if "error" not in res:
        cache_put(base, res)
    return res


# ---------- API ----------
@app.route("/manuales/")
def manuales_indice():
    return manuales("index.html")


@app.route("/manuales/<path:p>")
def manuales(p):
    import os
    from flask import send_from_directory
    return send_from_directory(os.path.join(BASE, "manuales"), p)


@app.route("/api/qrz/status")
def api_status():
    c = leer_cfg()
    con = db()
    try:
        n = con.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
    finally:
        con.close()
    return jsonify({"configured": bool(c.get("username") and c.get("password")), "username": c.get("username", ""),
                    "ok": ST["ok"], "error": ST["error"], "subscription": ST["sub"], "cache": n})


@app.route("/api/qrz/config", methods=["POST"])
def api_config():
    global _key
    b = request.get_json(silent=True) or {}
    user = str(b.get("username", "")).strip()
    pwd = str(b.get("password", ""))
    if not re.fullmatch(r"[A-Za-z0-9._\-]{3,40}", user):
        return jsonify({"error": "usuario invalido"}), 400
    if not pwd and user.lower() == str(leer_cfg().get("username", "")).lower():
        pwd = leer_cfg().get("password", "")
    if not pwd or len(pwd) > 100:
        return jsonify({"error": "falta la contrasena"}), 400
    try:
        with _qlock:
            key, ses, err = _login(user, pwd)
    except Exception as e:
        return jsonify({"error": "no se pudo contactar a QRZ: %s" % e}), 502
    if not key:
        return jsonify({"error": err}), 400
    guardar_cfg({"username": user, "password": pwd})
    _key = key
    ST.update(ok=True, error=None, sub=ses.get("SubExp"))
    return jsonify({"ok": True, "subscription": ses.get("SubExp")})


@app.route("/api/qrz/clear", methods=["POST"])
def api_clear():
    global _key
    _key = None
    try:
        os.remove(CONFIG_PATH)
    except FileNotFoundError:
        pass
    ST.update(ok=None, error=None, sub=None)
    return jsonify({"ok": True})


@app.route("/api/qrz/lookup")
def api_lookup():
    if request.args.get("fresh"):
        b = _base_call(request.args.get("call", ""))
        if b:
            con = db()
            try:
                con.execute("DELETE FROM cache WHERE call=?", (b,)); con.commit()
            finally:
                con.close()
    return jsonify(lookup(request.args.get("call", "")))


@app.route("/api/qrz/lookup-batch", methods=["POST"])
def api_batch():
    b = request.get_json(silent=True) or {}
    calls = []
    for c in b.get("calls") or []:
        c = str(c).upper().strip()
        if c and c not in calls:
            calls.append(c)
    return jsonify({"results": {c: lookup(c) for c in calls[:MAX_BATCH]}, "truncado": len(calls) > MAX_BATCH})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
