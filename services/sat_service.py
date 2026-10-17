#!/usr/bin/env python3
"""Satelites para el dashboard (puerto 5020): pasadas sobre tu ubicacion, desde TLE de CelesTrak.
  GET /api/sat/passes   |   GET/POST /api/sat/config {"sats":"ISS, SO-50","min_el":15,"lead_min":10}
La ubicacion sale de aprs_config.json (misma fuente que APRS). Precision de horarios: ~15 s."""
import json, math, os, threading, time, urllib.request
from sgp4.api import Satrec
from flask import Flask, jsonify, request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_PATH, TLE_PATH, APRS_CFG = (os.path.join(BASE, n) for n in ("sat_config.json", "sat_tle.txt", "aprs_config.json"))
TLE_URLS = ["https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
            "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle"]
TLE_MAX_EDAD, HORIZONTE_S, PASO_S = 12 * 3600, 48 * 3600, 15
DEFAULT = {"sats": ["ISS"], "min_el": 15, "lead_min": 10}
ST = {"pasadas": [], "calc": 0.0, "error": None, "resueltos": [], "no_encontrados": []}
LOCK, RLOCK, EV = threading.Lock(), threading.Lock(), threading.Event()
app = Flask(__name__)


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return r


def cfg():
    c = dict(DEFAULT)
    try:
        c.update(json.load(open(CFG_PATH)))
    except Exception:
        pass
    return c


def parsear(texto):
    lin = [l.rstrip() for l in texto.splitlines() if l.strip()]
    cat = {}
    for i in range(0, len(lin) - 2, 3):
        if lin[i + 1].startswith("1 ") and lin[i + 2].startswith("2 "):
            cat[int(lin[i + 1][2:7])] = (lin[i].strip(), lin[i + 1], lin[i + 2])
    return cat


def bajar_tle():
    partes = []
    for u in TLE_URLS:
        with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "pcbox-dashboard"}), timeout=25) as r:
            partes.append(r.read().decode("ascii", "replace"))
    texto = "\n".join(partes)
    if len(parsear(texto)) < 10:
        raise ValueError("TLE incompleto")
    open(TLE_PATH + ".tmp", "w").write(texto)
    os.replace(TLE_PATH + ".tmp", TLE_PATH)


def resolver(tokens, cat):
    ok, falta = [], []
    for t in tokens:
        t = str(t).strip()
        if not t:
            continue
        if t.isdigit() and int(t) in cat:
            ok.append(int(t))
            continue
        b = t.upper()
        m = [n for n, v in cat.items() if v[0].upper().startswith(b)] or [n for n, v in cat.items() if b in v[0].upper()]
        (ok if m else falta).append(min(m) if m else t)
    return list(dict.fromkeys(ok)), falta


def gmst(jd):
    t = (jd - 2451545.0) / 36525.0
    g = 67310.54841 + (876600 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return math.radians((g / 240.0) % 360.0)


def observador(lat, lon):
    la, lo = math.radians(lat), math.radians(lon)
    a, f = 6378.137, 1 / 298.257223563
    e2 = f * (2 - f)
    n = a / math.sqrt(1 - e2 * math.sin(la) ** 2)
    return {"sl": math.sin(la), "cl": math.cos(la), "so": math.sin(lo), "co": math.cos(lo),
            "p": (n * math.cos(la) * math.cos(lo), n * math.cos(la) * math.sin(lo), n * (1 - e2) * math.sin(la))}


def look(sat, t, o):
    jd = t / 86400.0 + 2440587.5
    e, r, _ = sat.sgp4(math.floor(jd), jd - math.floor(jd))
    if e:
        return None
    th = gmst(jd)
    c, s = math.cos(th), math.sin(th)
    rx, ry, rz = r[0] * c + r[1] * s - o["p"][0], -r[0] * s + r[1] * c - o["p"][1], r[2] - o["p"][2]
    east = -o["so"] * rx + o["co"] * ry
    north = -o["sl"] * o["co"] * rx - o["sl"] * o["so"] * ry + o["cl"] * rz
    up = o["cl"] * o["co"] * rx + o["cl"] * o["so"] * ry + o["sl"] * rz
    return math.degrees(math.asin(up / math.sqrt(rx * rx + ry * ry + rz * rz))), math.degrees(math.atan2(east, north)) % 360


def pasadas(norad, nombre, l1, l2, o, t0, min_el):
    sat, out, cur, t = Satrec.twoline2rv(l1, l2), [], None, t0
    while t < t0 + HORIZONTE_S:
        r = look(sat, t, o)
        el, az = r if r else (-90, 0)
        if el > 0:
            if cur is None:
                cur = {"aos": t, "az_aos": az, "max_el": el, "max_t": t, "az_max": az}
            elif el > cur["max_el"]:
                cur.update(max_el=el, max_t=t, az_max=az)
            cur["los"], cur["az_los"] = t, az
        elif cur:
            out.append(cur)
            cur = None
        t += PASO_S
    if cur:
        out.append(cur)
    return [dict(p, sat=nombre, norad=norad) for p in out if p["max_el"] >= min_el]


def recalcular():
    with RLOCK:
        c = cfg()
        ap = json.load(open(APRS_CFG))
        o = observador(float(ap["lat"]), float(ap["lon"]))
        cat = parsear(open(TLE_PATH).read())
        ids, falta = resolver(c["sats"], cat)
        ahora, out = time.time(), []
        for n in ids:
            out += pasadas(n, cat[n][0], cat[n][1], cat[n][2], o, ahora - 900, float(c["min_el"]))
        out.sort(key=lambda p: p["aos"])
        with LOCK:
            ST.update(pasadas=out, calc=ahora, resueltos=[cat[n][0] for n in ids], no_encontrados=falta, error=None)


def bucle():
    while True:
        try:
            if not os.path.exists(TLE_PATH) or time.time() - os.path.getmtime(TLE_PATH) > TLE_MAX_EDAD:
                bajar_tle()
        except Exception as e:
            ST["error"] = "TLE: %s" % e
        try:
            recalcular()
        except Exception as e:
            ST["error"] = "calculo: %s" % e
        EV.wait(600)
        EV.clear()


def vista_cfg():
    c = cfg()
    return {"sats": ", ".join(str(x) for x in c["sats"]), "min_el": c["min_el"], "lead_min": c["lead_min"]}


@app.route("/api/sat/passes")
def api_passes():
    ahora = time.time()
    edad = round((ahora - os.path.getmtime(TLE_PATH)) / 3600, 1) if os.path.exists(TLE_PATH) else None
    with LOCK:
        return jsonify({"now": ahora, "passes": [p for p in ST["pasadas"] if p["los"] > ahora][:30], "config": vista_cfg(),
                        "resueltos": ST["resueltos"], "no_encontrados": ST["no_encontrados"],
                        "tle_edad_h": edad, "error": ST["error"], "calculado": ST["calc"]})


@app.route("/api/sat/config", methods=["GET", "POST"])
def api_config():
    if request.method == "POST":
        b = request.get_json(silent=True) or {}
        try:
            sats = [x.strip() for x in str(b.get("sats", "")).split(",") if x.strip()][:12]
            nuevo = {"sats": sats or ["ISS"], "min_el": float(b.get("min_el", 15)), "lead_min": int(b.get("lead_min", 10))}
            if not 0 <= nuevo["min_el"] <= 80 or not 1 <= nuevo["lead_min"] <= 1440:
                raise ValueError("fuera de rango")
        except (TypeError, ValueError):
            return jsonify({"error": "datos inválidos (elevación 0-80°, aviso 1-1440 min)"}), 400
        open(CFG_PATH + ".tmp", "w").write(json.dumps(nuevo))
        os.replace(CFG_PATH + ".tmp", CFG_PATH)
        try:
            recalcular()
        except Exception as e:
            return jsonify({"error": "no se pudo recalcular: %s" % e}), 500
    with LOCK:
        return jsonify({"ok": True, "config": vista_cfg(), "resueltos": ST["resueltos"], "no_encontrados": ST["no_encontrados"]})


if __name__ == "__main__":
    threading.Thread(target=bucle, daemon=True).start()
    app.run(host="0.0.0.0", port=5020, threaded=True)
