#!/usr/bin/env python3
"""Ubicacion posible de un colega (servicio del dashboard, puerto 5024).
Cadena: HamQTH (opcional, con cuenta) -> RadioID + Nominatim (ciudad/region/pais).
Cache en SQLite. Sin dependencias fuera de Flask."""
import json, math, os, re, sqlite3, threading, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from flask import Flask, jsonify, request, Response

PORT = 5024
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = globals().get("__PCBOX_DATA__") or BASE
DB = os.path.join(BASE, "ubicacion_cache.db")
CFG = os.path.join(BASE, "ubicacion_config.json")
UA = "DashboardDMR/1.0 (github.com/ca3pvf/Dashboard_DMR)"
CALL_RE = re.compile(r"^[A-Z0-9/\-]{3,16}$")
DIA = 86400

app = Flask(__name__)


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r


def cargar_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def guardar_cfg(cfg):
    fd = os.open(CFG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(CFG, 0o600)


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.execute("CREATE TABLE IF NOT EXISTS loc (call TEXT PRIMARY KEY, data TEXT, ts REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS geo (q TEXT PRIMARY KEY, lat REAL, lon REAL, ts REAL)")
    return c


def http_get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def base_call(c):
    c = re.sub(r"-\d{1,2}$", "", c.upper())
    partes = [p for p in c.split("/") if p]
    return max(partes, key=len) if partes else c


def grid_a_latlon(g):
    m = re.match(r"^([A-R]{2})([0-9]{2})([A-X]{2})?", (g or "").strip().upper())
    if not m:
        return None
    lon = (ord(m.group(1)[0]) - 65) * 20 - 180 + int(m.group(2)[0]) * 2
    lat = (ord(m.group(1)[1]) - 65) * 10 - 90 + int(m.group(2)[1])
    if m.group(3):
        lon += (ord(m.group(3)[0]) - 65) * (5 / 60) + (5 / 60) / 2
        lat += (ord(m.group(3)[1]) - 65) * (2.5 / 60) + (2.5 / 60) / 2
    else:
        lon += 1.0
        lat += 0.5
    return lat, lon


def dist_rumbo(la1, lo1, la2, lo2):
    p1, p2 = math.radians(la1), math.radians(la2)
    dl = math.radians(lo2 - lo1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    d = 2 * 6371.0 * math.asin(math.sqrt(a))
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    b = (math.degrees(math.atan2(y, x)) + 360) % 360
    return round(d), round(b)


# ---------- HamQTH (opcional) ----------
_sid = {"id": None}


def _xml_dict(txt):
    d = {}
    for el in ET.fromstring(txt).iter():
        k = el.tag.split("}")[-1]
        if el.text and el.text.strip():
            d[k] = el.text.strip()
    return d


def hamqth_login(cfg):
    q = urllib.parse.urlencode({"u": cfg["hamqth_user"], "p": cfg["hamqth_pass"]})
    d = _xml_dict(http_get("https://www.hamqth.com/xml.php?" + q))
    _sid["id"] = d.get("session_id")
    return _sid["id"]


def hamqth_buscar(call, cfg):
    if not cfg.get("hamqth_user") or not cfg.get("hamqth_pass"):
        return None
    for _ in range(2):
        if not _sid["id"] and not hamqth_login(cfg):
            return None
        q = urllib.parse.urlencode({"id": _sid["id"], "callsign": call, "prg": "pcbox-dashboard"})
        d = _xml_dict(http_get("https://www.hamqth.com/xml.php?" + q))
        err = d.get("error", "")
        if "ession" in err:
            _sid["id"] = None
            continue
        return None if err else d
    return None


# ---------- RadioID + Nominatim ----------
def radioid_buscar(call):
    d = json.loads(http_get("https://radioid.net/api/dmr/user/?callsign=" + urllib.parse.quote(call)))
    res = d.get("results") or []
    return res[0] if res else None


_geo_lock = threading.Lock()
_geo_last = [0.0]


def geocodificar(q):
    """(lat, lon) o None si no existe; lanza RuntimeError si falla la red."""
    c = db()
    row = c.execute("SELECT lat, lon, ts FROM geo WHERE q=?", (q,)).fetchone()
    c.close()
    if row and time.time() - row[2] < 180 * DIA:
        return (row[0], row[1]) if row[0] is not None else None
    with _geo_lock:
        espera = 1.1 - (time.time() - _geo_last[0])
        if espera > 0:
            time.sleep(espera)
        _geo_last[0] = time.time()
        try:
            url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
                {"q": q, "format": "json", "limit": 1})
            r = json.loads(http_get(url, 10))
        except Exception as e:
            raise RuntimeError(str(e))
    lat = lon = None
    if r:
        lat, lon = float(r[0]["lat"]), float(r[0]["lon"])
    c = db()
    c.execute("INSERT OR REPLACE INTO geo VALUES (?,?,?,?)", (q, lat, lon, time.time()))
    c.commit()
    c.close()
    return (lat, lon) if lat is not None else None


def aprs_pos(call, port_local=None):
    """Posicion real desde el servicio APRS local (5014), si el colega fue visto."""
    lst = None
    for puerto in (5014, port_local):
        if not puerto:
            continue
        try:
            lst = json.loads(http_get("http://127.0.0.1:%s/api/aprs/stations?limit=50&q=%s" % (puerto, urllib.parse.quote(call)), 3))
            break
        except Exception:
            lst = None
    if lst is None:
        return None
    if isinstance(lst, dict):
        lst = lst.get("stations") or lst.get("items") or []
    mejor = None
    for e in (lst if isinstance(lst, list) else []):
        if not isinstance(e, dict) or str(e.get("call", "")).upper().split("-")[0] != call:
            continue
        la = e.get("lat", e.get("latitude"))
        lo = e.get("lon", e.get("lng", e.get("longitude")))
        try:
            la, lo = float(la), float(lo)
        except (TypeError, ValueError):
            continue
        if mejor is None or (e.get("hace_s") or 1e12) < (mejor[2] or 1e12):
            mejor = (la, lo, e.get("hace_s"), str(e.get("call")))
    return mejor


def resolver(call_in):
    call = base_call(call_in)
    c = db()
    row = c.execute("SELECT data, ts FROM loc WHERE call=?", (call,)).fetchone()
    c.close()
    if row:
        d = json.loads(row[0])
        ttl = 30 * DIA if d.get("lat") is not None else DIA
        if time.time() - row[1] < ttl:
            d["cache"] = True
            return d

    cfg = cargar_cfg()
    out = {"call": call, "lat": None, "lon": None, "precision": None,
           "fuente": None, "nombre": "", "lugar": "", "grid": ""}
    red_ok = True

    try:
        h = hamqth_buscar(call, cfg)
    except Exception:
        h = None
    if h:
        out["nombre"] = h.get("nick") or h.get("adr_name") or ""
        out["lugar"] = ", ".join(x for x in (h.get("qth"), h.get("country")) if x)
        out["grid"] = h.get("grid", "")
        try:
            lat, lon = float(h["latitude"]), float(h["longitude"])
        except Exception:
            ll = grid_a_latlon(out["grid"])
            lat, lon = ll if ll else (None, None)
        if lat is not None:
            out.update(lat=lat, lon=lon, fuente="HamQTH", precision="locator")

    if out["lat"] is None:
        try:
            r = radioid_buscar(call)
        except Exception:
            r = None
            red_ok = False
        if r:
            city = (r.get("city") or "").strip()
            state = (r.get("state") or "").strip()
            pais = (r.get("country") or "").strip()
            nombre = " ".join(x for x in (r.get("fname"), r.get("surname")) if x)
            out["nombre"] = out["nombre"] or nombre
            out["lugar"] = out["lugar"] or ", ".join(x for x in (city, state, pais) if x)
            cands = []
            if city:
                cands.append((", ".join(x for x in (city, state, pais) if x), "ciudad"))
                cands.append((", ".join(x for x in (city, pais) if x), "ciudad"))
            if state:
                cands.append((", ".join(x for x in (state, pais) if x), "region"))
            if pais:
                cands.append((pais, "pais"))
            vistos = set()
            for q, prec in cands:
                if q in vistos:
                    continue
                vistos.add(q)
                try:
                    ll = geocodificar(q)
                except Exception:
                    red_ok = False
                    break
                if ll:
                    out.update(lat=ll[0], lon=ll[1], precision=prec, fuente="RadioID + OpenStreetMap")
                    break

    if out["lat"] is None:
        out["error"] = "sin datos"
    if out["lat"] is not None or red_ok:
        c = db()
        c.execute("INSERT OR REPLACE INTO loc VALUES (?,?,?)", (call, json.dumps(out), time.time()))
        c.commit()
        c.close()
    return out


# ---------- API ----------
@app.route("/api/ubicacion/estado")
def api_estado():
    cfg = cargar_cfg()
    return jsonify({"ok": True, "hamqth": bool(cfg.get("hamqth_user") and cfg.get("hamqth_pass"))})


@app.route("/api/ubicacion/buscar")
def api_buscar():
    call = (request.args.get("call") or "").upper().strip()
    if not CALL_RE.match(call):
        return jsonify({"error": "indicativo invalido"}), 400
    d = resolver(call)
    ap = aprs_pos(base_call(call), request.environ.get("SERVER_PORT"))
    if ap:
        d.update(lat=ap[0], lon=ap[1], precision="aprs", fuente="APRS (" + ap[3] + ")")
        d.pop("error", None)
        if ap[2]:
            d["hace_min"] = max(1, round(ap[2] / 60))
    try:
        la, lo = float(request.args["lat"]), float(request.args["lon"])
        if d.get("lat") is not None:
            d["dist_km"], d["rumbo"] = dist_rumbo(la, lo, d["lat"], d["lon"])
    except Exception:
        pass
    return jsonify(d)


@app.route("/api/ubicacion/config", methods=["GET", "POST", "OPTIONS"])
def api_config():
    cfg = cargar_cfg()
    if request.method == "POST":
        j = request.get_json(silent=True) or {}
        if "hamqth_user" in j:
            cfg["hamqth_user"] = str(j["hamqth_user"]).strip()
        if j.get("hamqth_pass"):
            cfg["hamqth_pass"] = str(j["hamqth_pass"])
        guardar_cfg(cfg)
        _sid["id"] = None
    return jsonify({"hamqth_user": cfg.get("hamqth_user", ""),
                    "hamqth_pass_set": bool(cfg.get("hamqth_pass"))})


PAGINA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ubicación posible</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body{height:100%;margin:0;background:#0b1020;color:#dfe7ff;font-family:system-ui,sans-serif}
#info{position:absolute;z-index:1000;top:8px;left:8px;right:8px;max-width:340px;background:rgba(11,16,32,.9);
border:1px solid #2b3a67;border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.5}
#info b{color:#7df9ff}
#map{height:100%}
</style></head><body>
<div id="info">Buscando…</div><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
var q=new URLSearchParams(location.search);
var call=(q.get('call')||'').toUpperCase();
var lat=parseFloat(q.get('lat')), lon=parseFloat(q.get('lon'));
var home=!isNaN(lat)&&!isNaN(lon);
var info=document.getElementById('info');
var map=L.map('map',{worldCopyJump:true}).setView(home?[lat,lon]:[20,0],home?4:2);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'&copy; OpenStreetMap'}).addTo(map);
function fila(t,v,color){
  var d=document.createElement('div');
  var b=document.createElement('b'); b.textContent=t+' '; d.appendChild(b);
  var s=document.createElement('span'); s.textContent=v; if(color) s.style.color=color; d.appendChild(s);
  info.appendChild(d);
}
function miEstacion(){
  if(home) L.circleMarker([lat,lon],{radius:6,color:'#7df9ff'}).addTo(map).bindTooltip('Mi estación');
}
var url='/api/ubicacion/buscar?call='+encodeURIComponent(call)+(home?'&lat='+lat+'&lon='+lon:'');
fetch(url).then(function(r){return r.json();}).then(function(d){
  info.textContent='';
  fila('Indicativo:',d.call||call);
  if(d.nombre) fila('Nombre:',d.nombre);
  if(d.lugar) fila('Lugar registrado:',d.lugar);
  if(d.lat==null){ fila('','Sin datos de ubicación para este indicativo.'); miEstacion(); return; }
  var etiquetas={aprs:'APRS (posición real)',locator:'Locator (aprox.)',ciudad:'Ciudad',region:'Región',pais:'País'};
  var colores={aprs:'#4ade80',locator:'#4ade80',ciudad:'#facc15',region:'#fb923c',pais:'#f87171'};
  var km=d.precision=='locator'?((d.grid&&d.grid.length<6)?100:5):({aprs:0.5,ciudad:15,region:100,pais:400}[d.precision]||50);
  fila('Precisión:',(etiquetas[d.precision]||d.precision)+' · ±'+km+' km',colores[d.precision]);
  fila('Fuente:',d.fuente);
  if(d.grid) fila('Locator:',d.grid);
  if(d.hace_min) fila('Visto:','hace '+d.hace_min+' min');
  if(d.dist_km!=null){
    var pts=['N','NE','E','SE','S','SO','O','NO'];
    fila('Distancia:',d.dist_km+' km · rumbo '+d.rumbo+'° '+pts[Math.round(d.rumbo/45)%8]);
  }
  var c=colores[d.precision]||'#7df9ff';
  L.circle([d.lat,d.lon],{radius:km*1000,color:c,fillColor:c,fillOpacity:.12,weight:1}).addTo(map);
  L.marker([d.lat,d.lon]).addTo(map).bindTooltip(d.call,{permanent:true,direction:'top'});
  var b=L.latLngBounds([[d.lat,d.lon]]);
  if(home){
    miEstacion();
    L.polyline([[lat,lon],[d.lat,d.lon]],{color:'#7df9ff',weight:1.5,dashArray:'6 6'}).addTo(map);
    b.extend([lat,lon]);
  }
  map.fitBounds(b.pad(0.6),{maxZoom:9});
  var nota=document.createElement('div');
  nota.style.cssText='margin-top:4px;font-size:11px;opacity:.7';
  nota.textContent='Ubicación posible, según datos que el propio colega registró.';
  info.appendChild(nota);
}).catch(function(){ info.textContent='No se pudo consultar el servicio de ubicación.'; });
})();
</script></body></html>"""


@app.route("/api/ubicacion/mapa")
def api_mapa():
    return Response(PAGINA, mimetype="text/html")


if __name__ == "__main__":
    if not os.path.exists(CFG):
        guardar_cfg({"hamqth_user": "", "hamqth_pass": ""})
    app.run(host="0.0.0.0", port=PORT, threaded=True)
