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
    os.makedirs(os.path.dirname(TLE_PATH), exist_ok=True)
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


# SATRUTA-INICIO
from flask import Response as _Resp


def subpunto(sat, t):
    # latitud, longitud y altura (km) bajo el satelite en el instante t
    jd = t / 86400.0 + 2440587.5
    e, r, _ = sat.sgp4(math.floor(jd), jd - math.floor(jd))
    if e:
        return None
    th = gmst(jd)
    c, s = math.cos(th), math.sin(th)
    x, y, z = r[0] * c + r[1] * s, -r[0] * s + r[1] * c, r[2]
    a, f = 6378.137, 1 / 298.257223563
    e2 = f * (2 - f)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1 - e2))
    for _ in range(6):
        n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
        h = p / math.cos(lat) - n if abs(lat) < 1.4 else z / math.sin(lat) - n * (1 - e2)
        lat = math.atan2(z, p * (1 - e2 * n / (n + h)))
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    h = p / math.cos(lat) - n if abs(lat) < 1.4 else z / math.sin(lat) - n * (1 - e2)
    return math.degrees(lat), math.degrees(math.atan2(y, x)), h


def radio_visibilidad(h, el_deg):
    # radio (km) del circulo desde donde se ve el satelite sobre la elevacion dada
    re_, el = 6371.0, math.radians(el_deg)
    return re_ * (math.acos(re_ * math.cos(el) / (re_ + h)) - el)


@app.route("/api/sat/track")
def api_track():
    try:
        n, a, l = int(request.args["norad"]), float(request.args["aos"]), float(request.args["los"])
        if not 0 < l - a <= 3 * 3600:
            raise ValueError
    except (KeyError, ValueError):
        return jsonify({"error": "parámetros inválidos"}), 400
    try:
        cat = parsear(open(TLE_PATH).read())
        ap = json.load(open(APRS_CFG))
        o = observador(float(ap["lat"]), float(ap["lon"]))
    except Exception as e:
        return jsonify({"error": "faltan datos (TLE o ubicación): %s" % e}), 503
    if n not in cat:
        return jsonify({"error": "satélite no encontrado en los TLE"}), 404
    nombre, l1, l2 = cat[n]
    sat = Satrec.twoline2rv(l1, l2)
    pts, t = [], a - 900
    while t <= l + 900:
        sp, lk = subpunto(sat, t), look(sat, t, o)
        if sp:
            pts.append([int(t), round(sp[0], 3), round(sp[1], 3), round(lk[0], 1) if lk else None, round(lk[1]) if lk else None])
        t += 20
    if not pts:
        return jsonify({"error": "no se pudo calcular la órbita"}), 500
    h = subpunto(sat, (a + l) / 2)[2]
    return jsonify({"sat": nombre, "norad": n, "aos": a, "los": l, "alt_km": round(h),
                    "obs": {"lat": float(ap["lat"]), "lon": float(ap["lon"])}, "puntos": pts,
                    "radio_horizonte_km": round(radio_visibilidad(h, 0)),
                    "radio_minimo_km": round(radio_visibilidad(h, float(cfg()["min_el"]))), "min_el": cfg()["min_el"]})


PAGINA_RUTA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Ruta del satélite</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.css">
<style>html,body{height:100%;margin:0;font:14px system-ui,sans-serif;background:#101418;color:#e8eaed}
#cab{padding:10px 14px;background:#161b22;border-bottom:1px solid #2a3340}
#cab h1{margin:0 0 4px;font-size:1.15em;color:#ffb020}#info{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:.9em;color:#c9d1d9}
#mapa{height:calc(100% - 92px)}.k{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:5px;vertical-align:middle}
#pie{padding:4px 14px;font-size:.75em;color:#8a96a3}</style></head><body>
<div id="cab"><h1 id="tit">Calculando ruta…</h1><div id="info"></div></div><div id="mapa"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"></script>
<script>
const $ = id => document.getElementById(id);
const hora = t => new Date(t * 1000).toLocaleString('es-CL', {dateStyle: 'short', timeStyle: 'medium'});
function trozos(pts, ok){
  const out = []; let cur = [], prev = null;
  pts.forEach(p => {
    if(!ok(p)){ if(cur.length > 1) out.push(cur); cur = []; prev = null; return; }
    if(prev && Math.abs(p[2] - prev[2]) > 180){ if(cur.length > 1) out.push(cur); cur = []; }
    cur.push([p[1], p[2]]); prev = p;
  });
  if(cur.length > 1) out.push(cur);
  return out;
}
function dibujar(d){
  const mapa = L.map('mapa', {worldCopyJump: true}).setView([d.obs.lat, d.obs.lon], 3);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom: 12, attribution: '&copy; OpenStreetMap'}).addTo(mapa);
  const P = d.puntos, dentro = p => p[0] >= d.aos && p[0] <= d.los;
  trozos(P, () => true).forEach(s => L.polyline(s, {color: '#7aa2ff', weight: 2, dashArray: '6 6', opacity: .8}).addTo(mapa));
  const seg = trozos(P, dentro);
  seg.forEach(s => L.polyline(s, {color: '#ff9f1a', weight: 5}).addTo(mapa));
  L.circle([d.obs.lat, d.obs.lon], {radius: d.radio_horizonte_km * 1000, color: '#39ff14', weight: 1, fillOpacity: .04, dashArray: '4 6'}).addTo(mapa);
  L.circle([d.obs.lat, d.obs.lon], {radius: d.radio_minimo_km * 1000, color: '#39ff14', weight: 1, fillOpacity: .08}).addTo(mapa);
  L.circleMarker([d.obs.lat, d.obs.lon], {radius: 7, color: '#fff', fillColor: '#2f81f7', fillOpacity: 1}).addTo(mapa).bindTooltip('Tu ubicación');
  const pas = P.filter(dentro);
  const cerca = t => P.reduce((m, p) => Math.abs(p[0] - t) < Math.abs(m[0] - t) ? p : m, P[0]);
  const ini = pas.length ? pas[0] : cerca(d.aos), fin = pas.length ? pas[pas.length - 1] : cerca(d.los);
  const max = (pas.length ? pas : P).reduce((m, p) => (p[3] || -99) > (m[3] || -99) ? p : m);
  [[ini, '#2ecc71', 'Inicio (AOS)'], [max, '#ffb020', 'Máxima elevación'], [fin, '#ff5d5d', 'Fin (LOS)']].forEach(x =>
    L.circleMarker([x[0][1], x[0][2]], {radius: 8, color: '#fff', weight: 2, fillColor: x[1], fillOpacity: 1}).addTo(mapa)
      .bindTooltip(x[2] + ' · ' + hora(x[0][0]).split(', ').pop(), {permanent: true, direction: 'top', offset: [0, -6]}));
  const todos = pas.length ? pas.map(p => [p[1], p[2]]) : P.map(p => [p[1], p[2]]);
  todos.push([d.obs.lat, d.obs.lon]);
  mapa.fitBounds(L.latLngBounds(todos).pad(.25));
  $('tit').textContent = d.sat + ' — pasada del ' + hora(d.aos).split(', ')[0];
  const dur = Math.round(d.los - d.aos), kk = c => '<span class="k" style="background:' + c + '"></span>';
  $('info').innerHTML = '<span>' + kk('#2ecc71') + 'Inicio ' + hora(ini[0]).split(', ').pop() + ' · acimut ' + ini[4] + '°</span>' +
    '<span>' + kk('#ffb020') + 'Máx ' + (max[3] != null ? Math.round(max[3]) : '--') + '° a las ' + hora(max[0]).split(', ').pop() + ' · acimut ' + max[4] + '°</span>' +
    '<span>' + kk('#ff5d5d') + 'Fin ' + hora(fin[0]).split(', ').pop() + ' · acimut ' + fin[4] + '°</span>' +
    '<span>Duración ' + Math.floor(dur / 60) + ' min ' + (dur % 60) + ' s</span><span>Altura ' + d.alt_km + ' km</span>' +
    '<span>Naranja: tramo de la pasada · azul punteado: ±15 min de órbita · verde: zona de visibilidad (horizonte y elevación mínima ' + d.min_el + '°)</span>';
}
fetch('track' + location.search).then(r => r.json().then(d => { if(!r.ok) throw new Error(d.error || ('error ' + r.status)); return d; }))
  .then(dibujar).catch(e => { $('tit').textContent = 'No se pudo calcular la ruta'; $('info').textContent = e.message; });
</script><div id="pie">La ruta es aproximada (TLE de CelesTrak). El mapa usa OpenStreetMap y requiere internet en este navegador.</div></body></html>"""


@app.route("/api/sat/ruta")
def api_ruta():
    return _Resp(PAGINA_RUTA, mimetype="text/html")
# SATRUTA-FIN


if __name__ == "__main__":
    threading.Thread(target=bucle, daemon=True).start()
    app.run(host="0.0.0.0", port=5020, threaded=True)
