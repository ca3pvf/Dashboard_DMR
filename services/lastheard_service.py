#!/usr/bin/env python3
"""
Servicio de polling del Last Heard de WPSD (API pública /api/), multi-hotspot (hasta 5).
- /api/lastheard?hs=N       últimas transmisiones del hotspot N (por defecto 1)
- /api/ranking?hs=N|all     ranking por período desde el historial propio (SQLite)
La lista de hotspots está en hotspots.json (la administra wpsd_dashboard_service.py).
"""
from flask import Flask, jsonify, request
import json
import os
import re
import requests
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

# --- Config ---
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOTSPOTS_PATH = os.environ.get("HOTSPOTS_PATH", os.path.join(BASE, "hotspots.json"))
DB_PATH = os.environ.get("LH_DB", "/opt/pcbox/lastheard_hist.db")
PORT = int(os.environ.get("LH_PORT", "5011"))
SLOTS = 5
POLL_ACTIVE_SECONDS = 3      # hotspot que se está mirando en el dashboard
POLL_IDLE_SECONDS = 15       # hotspots que nadie mira (solo para el historial)
ACTIVE_WINDOW_SECONDS = 30
FETCH_LIMIT = 50
REQUEST_TIMEOUT = 30
POLL_LIMIT = 15
DEFAULT_HS = {"id": 1, "name": "Hotspot 1", "ip": "@@HS_IP@@", "http_port": 80, "enabled": True}
# --------------

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


_lock = threading.Lock()
_estado = {s: {"data": [], "last_update": None, "last_error": None, "last_request": 0.0}
           for s in range(1, SLOTS + 1)}
_wake = {s: threading.Event() for s in range(1, SLOTS + 1)}
_hs_cache = {"mtime": None, "data": {1: DEFAULT_HS}}


def hotspots():
    """{id: hotspot} de los habilitados, releyendo hotspots.json solo si cambió."""
    try:
        m = os.path.getmtime(HOTSPOTS_PATH)
    except OSError:
        return {1: DEFAULT_HS}
    if m != _hs_cache["mtime"]:
        try:
            with open(HOTSPOTS_PATH) as f:
                lista = json.load(f).get("hotspots", [])
            data = {int(h["id"]): h for h in lista if h.get("enabled", True) and h.get("ip")}
            _hs_cache["mtime"] = m
            _hs_cache["data"] = data
        except Exception:
            pass
    return _hs_cache["data"]


def api_url(h):
    puerto = int(h.get("http_port") or 80)
    return "http://%s%s/api/" % (h["ip"], "" if puerto == 80 else ":%d" % puerto)


# ---------------- historial (SQLite) ----------------
def init_db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    cols = [r[1] for r in con.execute("PRAGMA table_info(tx)")]
    if cols and "hs" not in cols:      # migración de la versión de un solo hotspot
        con.executescript("""
            DROP INDEX IF EXISTS idx_tx_time;
            ALTER TABLE tx RENAME TO tx_old;
            CREATE TABLE tx (
                hs INTEGER NOT NULL DEFAULT 1, time_utc TEXT, callsign TEXT, target TEXT, mode TEXT,
                name TEXT, country TEXT, src TEXT, duration REAL,
                PRIMARY KEY (hs, time_utc, callsign, target, mode));
            INSERT INTO tx(hs,time_utc,callsign,target,mode,name,country,src,duration)
                SELECT 1,time_utc,callsign,target,mode,name,country,src,duration FROM tx_old;
            DROP TABLE tx_old;
        """)
    con.execute("""CREATE TABLE IF NOT EXISTS tx (
        hs INTEGER NOT NULL DEFAULT 1, time_utc TEXT, callsign TEXT, target TEXT, mode TEXT,
        name TEXT, country TEXT, src TEXT, duration REAL,
        PRIMARY KEY (hs, time_utc, callsign, target, mode))""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tx_time ON tx(time_utc)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_tx_call ON tx(callsign)")
    con.commit()
    con.close()


def guardar(data, hs):
    filas = []
    for t in data:
        try:
            dur = float(t.get("duration"))
        except (TypeError, ValueError):
            continue  # transmisión aún en curso (sin duración)
        if not t.get("time_utc") or not t.get("callsign"):
            continue
        filas.append((hs, t["time_utc"], t["callsign"], t.get("target") or "",
                      t.get("mode") or "", t.get("name") or "",
                      t.get("country") or "", t.get("src") or "", dur))
    if not filas:
        return
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        con.executemany("INSERT OR IGNORE INTO tx VALUES (?,?,?,?,?,?,?,?,?)", filas)
        con.commit()
    finally:
        con.close()


def poll_loop(slot):
    fallos = 0
    while True:
        h = hotspots().get(slot)
        if not h:
            _wake[slot].wait(5)
            _wake[slot].clear()
            continue
        t0 = time.time()
        try:
            resp = requests.get(api_url(h), params={"limit": POLL_LIMIT}, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            with _lock:
                e = _estado[slot]
                e["data"] = data
                e["last_update"] = time.time()
                e["last_error"] = None
            fallos = 0
            try:
                guardar(data, slot)
            except Exception as ex:
                print("error guardando historial: %s" % ex, flush=True)
        except Exception as ex:
            fallos += 1
            with _lock:
                _estado[slot]["last_error"] = str(ex)
        dur = time.time() - t0
        if dur > 8:
            print("hotspot lento: hs=%s %.1fs fallos=%d" % (slot, dur, fallos), flush=True)
        with _lock:
            reciente = time.time() - _estado[slot]["last_request"] < ACTIVE_WINDOW_SECONDS
        base = POLL_ACTIVE_SECONDS if reciente else POLL_IDLE_SECONDS
        # el hotspot descansa al menos lo que tardo en responder (max 15 s);
        # con fallos seguidos la espera sube: 6, 12, 24, 30 s
        espera = max(base, dur)
        if fallos:
            espera = max(espera, min(30, 3 * 2 ** fallos))
        espera = min(espera, 30)
        pausa = min(max(dur, 1.0), 15)
        time.sleep(pausa)
        _wake[slot].wait(max(0, espera - pausa))
        _wake[slot].clear()

def desde_utc(period):
    inicio = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        inicio -= timedelta(days=inicio.weekday())
    elif period == "month":
        inicio = inicio.replace(day=1)
    elif period == "year":
        inicio = inicio.replace(month=1, day=1)
    return inicio.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def slot_param(nombre="hs", defecto="1"):
    v = request.args.get(nombre, defecto)
    if v == "all":
        return "all"
    try:
        n = int(v)
    except ValueError:
        return int(defecto) if defecto != "all" else "all"
    return n if 1 <= n <= SLOTS else (int(defecto) if defecto != "all" else "all")


def historial(hs, limite=FETCH_LIMIT):
    """Ultimas transmisiones completas guardadas en el historial propio."""
    try:
        con = sqlite3.connect(DB_PATH, timeout=5)
        try:
            rows = con.execute("SELECT time_utc,callsign,target,mode,name,country,src,duration FROM tx "
                               "WHERE hs=? ORDER BY time_utc DESC LIMIT ?", (hs, limite)).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [{"time_utc": r[0], "callsign": r[1], "callsign_suffix": "", "target": r[2], "mode": r[3],
             "name": r[4], "country": r[5], "src": r[6], "duration": str(r[7]), "loss": "", "ber": ""}
            for r in rows]


def fusionar(vivo, hist):
    """Une lo que entrega el hotspot en vivo con el historial, sin repetidos, mas reciente primero."""
    vistos, out = set(), []
    for t in list(vivo) + hist:
        k = (t.get("callsign"), t.get("time_utc"), t.get("target") or "", t.get("mode") or "")
        if k in vistos:
            continue
        vistos.add(k)
        out.append(t)
    out.sort(key=lambda t: t.get("time_utc") or "", reverse=True)
    return out[:FETCH_LIMIT]


@app.route("/api/lastheard")
def lastheard():
    hs = slot_param("hs", "1")
    if hs == "all" or hs not in hotspots():
        return jsonify({"transmissions": [], "last_update": None,
                        "error": "hotspot no configurado", "count": 0})
    with _lock:
        e = _estado[hs]
        primera_vez = time.time() - e["last_request"] >= ACTIVE_WINDOW_SECONDS
        e["last_request"] = time.time()
        vivo = list(e["data"]) if isinstance(e["data"], list) else []
        info = (e["last_update"], e["last_error"])
    mezcla = fusionar(vivo, historial(hs))
    resp = jsonify({"transmissions": mezcla, "last_update": info[0], "error": info[1], "count": len(mezcla)})
    if primera_vez:
        _wake[hs].set()      # pasar a ritmo rápido de inmediato
    return resp


@app.route("/api/lastheard/search")
def lastheard_search():
    # Busca por prefijo de indicativo en el historial propio (todas las transmisiones guardadas)
    q = re.sub(r"[^A-Z0-9/\-]", "", request.args.get("q", "").upper())
    if len(q) < 3:
        return jsonify({"transmissions": [], "count": 0, "error": "minimo 3 caracteres"}), 400
    try:
        limite = min(500, max(1, int(request.args.get("limit", "200"))))
    except ValueError:
        limite = 200
    hs = slot_param("hs", "all")
    filtro_hs = "" if hs == "all" else " AND hs = ?"
    args = ["%" + q + "%"] + ([] if hs == "all" else [hs])
    # una misma transmisión escuchada por varios hotspots cuenta una sola vez
    base = ("SELECT callsign, time_utc, target, mode, MAX(name) AS name, MAX(country) AS country, "
            "MAX(src) AS src, MAX(duration) AS duration FROM tx WHERE callsign LIKE ?" + filtro_hs +
            " GROUP BY callsign, time_utc, target, mode")
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = con.execute("SELECT * FROM (" + base + ") ORDER BY time_utc DESC LIMIT ?", args + [limite]).fetchall()
        tot = con.execute("SELECT COUNT(*), COALESCE(SUM(duration), 0), MIN(time_utc), MAX(time_utc) FROM (" + base + ")",
                          args).fetchone()
    finally:
        con.close()
    out = [{"time_utc": r[1], "callsign": r[0], "callsign_suffix": "", "target": r[2], "mode": r[3],
            "name": r[4], "country": r[5], "src": r[6], "duration": str(r[7]), "loss": "", "ber": ""} for r in rows]
    return jsonify({"q": q, "transmissions": out, "count": tot[0], "segundos": tot[1],
                    "primero": tot[2], "ultimo": tot[3], "mostrando": len(out)})


@app.route("/api/ranking")
def ranking():
    period = request.args.get("period", "day")
    if period not in ("day", "week", "month", "year"):
        period = "day"
    by = request.args.get("by", "count")
    orden = "seg DESC, n DESC" if by == "time" else "n DESC, seg DESC"
    try:
        minimo = max(0.0, float(request.args.get("min", "0")))
    except ValueError:
        minimo = 0.0
    try:
        limite = min(50, max(1, int(request.args.get("limit", "10"))))
    except ValueError:
        limite = 10
    hs = slot_param("hs", "all")
    desde = desde_utc(period)
    filtro_hs = "" if hs == "all" else " AND hs = ?"
    args = [desde, minimo] + ([] if hs == "all" else [hs])
    # una misma transmisión escuchada por varios hotspots cuenta una sola vez
    base = ("SELECT callsign, MAX(name) AS name, MAX(country) AS country, time_utc, target, mode, "
            "MAX(duration) AS duration FROM tx WHERE time_utc >= ? AND duration >= ?" + filtro_hs +
            " GROUP BY callsign, time_utc, target, mode")
    con = sqlite3.connect(DB_PATH, timeout=10)
    try:
        rows = con.execute(
            "WITH u AS (" + base + ") SELECT callsign, MAX(name), MAX(country), COUNT(*) AS n, "
            "SUM(duration) AS seg FROM u GROUP BY callsign ORDER BY " + orden + " LIMIT ?",
            args + [limite]).fetchall()
        total = con.execute("SELECT COUNT(*) FROM (" + base + ")", args).fetchone()[0]
        if hs == "all":
            primero = con.execute("SELECT MIN(time_utc) FROM tx").fetchone()[0]
        else:
            primero = con.execute("SELECT MIN(time_utc) FROM tx WHERE hs = ?", (hs,)).fetchone()[0]
    finally:
        con.close()
    return jsonify({
        "period": period, "by": by, "min": minimo, "hs": hs,
        "desde_utc": desde, "primer_registro": primero, "total": total,
        "ranking": [{"callsign": r[0], "name": r[1], "country": r[2],
                     "ptt": r[3], "segundos": r[4]} for r in rows],
    })


if __name__ == "__main__":
    init_db()
    for s in range(1, SLOTS + 1):
        threading.Thread(target=poll_loop, args=(s,), daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
