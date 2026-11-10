#!/usr/bin/env python3
"""Sismos recientes de Chile desde CSN (sismologia.cl), con cache. Puerto 5022.
  GET /api/sismos?min=0   ultimos sismos, el mas reciente primero"""
import json, math, os, re, threading, time, urllib.request
from datetime import datetime, timezone
from flask import Flask, jsonify, request, Response

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(BASE, "sismos_cache.json")
URL = "https://sismologia.cl/"
UA = "pcbox-dashboard (uso personal @@CALL@@)"
POLL_S, MAX_ITEMS, MAX_DETALLES = 150, 30, 8
FILA = re.compile(r'<a href="(/sismicidad/informes/\d{4}/\d{2}/(\d+)\.html)">([\d\- :]+)</a><br>\s*(.*?)\s*</td>'
                  r'\s*<td>\s*(\d+)\s*km\s*</td>\s*<td class="magnitud">\s*([\d.]+)\s*</td>', re.S)
app = Flask(__name__)
LOCK = threading.Lock()
ST = {"items": {}, "calc": 0.0, "error": None}


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


def bajar(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", "replace")


def detalle(path):
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", bajar("https://sismologia.cl" + path)))
    lat = re.search(r"Latitud (-?[\d.]+)", t)
    lon = re.search(r"Longitud (-?[\d.]+)", t)
    mag = re.search(r"Magnitud ([\d.]+) (\w+)", t)
    utc = re.search(r"Hora UTC (\d\d:\d\d:\d\d) (\d\d)/(\d\d)/(\d{4})", t)
    if not (lat and lon):
        raise ValueError("informe sin coordenadas")
    d = {"lat": float(lat.group(1)), "lon": float(lon.group(1))}
    if mag:
        d["mag"], d["tipo"] = float(mag.group(1)), mag.group(2)
    if utc:
        h, dd, mm, aa = utc.groups()
        d["ts"] = datetime.strptime("%s-%s-%s %s" % (aa, mm, dd, h), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    return d


def ciclo():
    filas = FILA.findall(bajar(URL))
    if not filas:
        raise ValueError("no se reconocio la portada de CSN")
    with LOCK:
        items = ST["items"]
        for path, id_, local, lugar, prof, mag in filas:
            if id_ not in items:
                items[id_] = {"id": id_, "path": path, "local": local, "lugar": re.sub(r"\s+", " ", lugar),
                              "prof": int(prof), "mag": float(mag)}
        pend = [v for v in items.values() if "lat" not in v][:MAX_DETALLES]
    for v in pend:
        try:
            d = detalle(v["path"])
            with LOCK:
                v.update(d)
        except Exception as e:
            print("sismos: informe %s fallo: %s" % (v["id"], e), flush=True)
        time.sleep(1)
    with LOCK:
        top = sorted(ST["items"].values(), key=lambda x: x["local"], reverse=True)[:MAX_ITEMS]
        ST["items"] = {x["id"]: x for x in top}
        ST.update(calc=time.time(), error=None)
        datos = list(ST["items"].values())
    with open(CACHE + ".tmp", "w") as f:
        json.dump(datos, f, ensure_ascii=False)
    os.replace(CACHE + ".tmp", CACHE)


def bucle():
    try:
        with open(CACHE) as f:
            with LOCK:
                ST["items"] = {x["id"]: x for x in json.load(f)}
    except Exception:
        pass
    while True:
        try:
            ciclo()
        except Exception as e:
            ST["error"] = str(e)[:150]
            print("sismos: ciclo fallo: %s" % e, flush=True)
        time.sleep(POLL_S)


APRS_CFG = os.path.join(BASE, "aprs_config.json")


def _ubicacion():
    try:
        with open(APRS_CFG) as f:
            a = json.load(f)
        return float(a["lat"]), float(a["lon"])
    except Exception:
        return None


def _km(lat1, lon1, lat2, lon2):
    f1, f2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((f2 - f1) / 2) ** 2 + math.cos(f1) * math.cos(f2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def con_dist(lista):
    u = _ubicacion()
    out = []
    for x in lista:
        y = dict(x)
        ok = u and x.get("lat") is not None and x.get("lon") is not None
        y["dist_km"] = round(_km(u[0], u[1], x["lat"], x["lon"]), 1) if ok else None
        out.append(y)
    return out


@app.route("/api/sismos")
def api_sismos():
    try:
        minimo = float(request.args.get("min", "0"))
    except ValueError:
        minimo = 0.0
    with LOCK:
        lista = sorted((x for x in ST["items"].values() if x["mag"] >= minimo), key=lambda x: x["local"], reverse=True)
        return jsonify({"sismos": con_dist(lista), "calculado": ST["calc"], "error": ST["error"]})


USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
USGS_MUNDO = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
USGS_POLL_S = POLL_S      # misma periodicidad que CSN
US = {"chile": [], "mundo": [], "calc": 0.0, "error": None}


def _usgs_items(js):
    out = []
    for f in js.get("features", []):
        p = f.get("properties") or {}
        c = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        if p.get("mag") is None or p.get("time") is None or c[0] is None:
            continue
        out.append({"id": f.get("id"), "mag": float(p["mag"]), "tipo": p.get("magType") or "",
                    "lugar": p.get("place") or "", "ts": p["time"] / 1000.0,
                    "lon": c[0], "lat": c[1], "prof": c[2], "url": p.get("url") or ""})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def ciclo_usgs():
    desde = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 2 * 86400))
    q = ("?format=geojson&orderby=time&limit=50&minmagnitude=3&starttime=" + desde +
         "&minlatitude=-56&maxlatitude=-17&minlongitude=-76&maxlongitude=-66")
    nuevo, fallos = {}, []
    for clave, url, tope in (("chile", USGS_URL + q, None), ("mundo", USGS_MUNDO, 30)):
        try:                                   # cada lista por separado: si una falla, la otra igual se actualiza
            items = _usgs_items(json.loads(bajar(url)))
            nuevo[clave] = items[:tope] if tope else items
        except Exception as e:
            fallos.append("%s: %s" % (clave, str(e)[:80]))
    if not nuevo:
        raise ValueError("; ".join(fallos))
    with LOCK:
        US.update(nuevo)
        US.update(calc=time.time(), error=("USGS " + "; ".join(fallos)) if fallos else None)
    if fallos:
        print("sismos: usgs parcial: %s" % "; ".join(fallos), flush=True)
    return not fallos


def bucle_usgs():
    time.sleep(5)
    while True:
        ok = False
        try:
            ok = ciclo_usgs()
        except Exception as e:
            US["error"] = str(e)[:150]
            print("sismos: usgs fallo: %s" % e, flush=True)
        time.sleep(USGS_POLL_S if ok else 60)   # tras una falla reintenta en 1 min, no en 5


PAGINA_SISMO = r"""<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sismo</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
html,body{height:100%;margin:0;background:#0b1020;color:#dfe7ff;font-family:system-ui,sans-serif}
#info{position:absolute;z-index:1000;top:8px;left:8px;right:8px;max-width:340px;background:rgba(11,16,32,.9);
border:1px solid #6b2b2b;border-radius:8px;padding:8px 10px;font-size:13px;line-height:1.5}
#info b{color:#ff9d9d}
#info a{color:#7df9ff}
#map{height:100%}
</style></head><body>
<div id="info"></div><div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
var q=new URLSearchParams(location.search);
function num(k){ var v=q.get(k); return (v===null||v==='')?NaN:parseFloat(v); }
var lat=num('lat'), lon=num('lon'), hlat=num('hlat'), hlon=num('hlon'), mag=num('mag');
var home=!isNaN(hlat)&&!isNaN(hlon);
var info=document.getElementById('info');
function fila(t,v,color){
  var d=document.createElement('div');
  var b=document.createElement('b'); b.textContent=t+' '; d.appendChild(b);
  var s=document.createElement('span'); s.textContent=v; if(color) s.style.color=color; d.appendChild(s);
  info.appendChild(d);
}
var R=Math.PI/180;
function dist(a,b,c,d){
  var f1=a*R,f2=c*R,x=Math.sin((f2-f1)/2),y=Math.sin((d-b)*R/2);
  var h=x*x+Math.cos(f1)*Math.cos(f2)*y*y;
  return 6371*2*Math.asin(Math.sqrt(h));
}
function rumbo(a,b,c,d){
  var f1=a*R,f2=c*R,dl=(d-b)*R;
  var y=Math.sin(dl)*Math.cos(f2), x=Math.cos(f1)*Math.sin(f2)-Math.sin(f1)*Math.cos(f2)*Math.cos(dl);
  return (Math.atan2(y,x)/R+360)%360;
}
var col=mag>=6?'#ff5d5d':(mag>=5?'#ffb020':'#facc15');
var mtxt='M '+(isNaN(mag)?'--':mag.toFixed(1));
fila('Magnitud:',mtxt+(q.get('tipo')?' ('+q.get('tipo')+')':''),col);
if(q.get('lugar')) fila('Lugar:',q.get('lugar'));
if(q.get('hora')) fila('Hora:',q.get('hora'));
if(q.get('prof')) fila('Profundidad:',q.get('prof')+' km');
if(q.get('fuente')) fila('Fuente:',q.get('fuente'));
var map=L.map('map',{worldCopyJump:true});
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:18,attribution:'&copy; OpenStreetMap'}).addTo(map);
if(isNaN(lat)||isNaN(lon)){
  fila('','Este sismo no trae coordenadas.'); map.setView([-30,-70],4);
}else{
  fila('Coordenadas:',lat.toFixed(2)+', '+lon.toFixed(2));
  var b=L.latLngBounds([[lat,lon]]);
  var rad=Math.max(6,(isNaN(mag)?3:mag)*3);
  L.circleMarker([lat,lon],{radius:rad,color:col,fillColor:col,fillOpacity:.35,weight:2}).addTo(map).bindTooltip(mtxt,{permanent:true,direction:'top'});
  if(home){
    var pts=['N','NE','E','SE','S','SO','O','NO'];
    var dk=dist(hlat,hlon,lat,lon), rb=rumbo(hlat,hlon,lat,lon);
    fila('Distancia:',Math.round(dk)+' km · rumbo '+Math.round(rb)+'° '+pts[Math.round(rb/45)%8]);
    L.circleMarker([hlat,hlon],{radius:6,color:'#7df9ff'}).addTo(map).bindTooltip('Mi estación');
    L.polyline([[hlat,hlon],[lat,lon]],{color:'#7df9ff',weight:1.5,dashArray:'6 6'}).addTo(map);
    b.extend([hlat,hlon]);
    map.fitBounds(b.pad(0.4),{maxZoom:8});
  }else{ map.setView([lat,lon],7); }
}
var rep=q.get('rep')||'';
if(/^https:\/\/(sismologia\.cl|earthquake\.usgs\.gov)\//.test(rep)){
  var dv=document.createElement('div'); dv.style.marginTop='4px';
  var a=document.createElement('a'); a.href=rep; a.target='_blank'; a.rel='noopener'; a.textContent='Informe oficial ↗';
  dv.appendChild(a); info.appendChild(dv);
}
})();
</script></body></html>
"""


@app.route("/api/sismos/mapa")
def api_mapa_sismo():
    return Response(PAGINA_SISMO, mimetype="text/html")


@app.route("/api/sismos/usgs")
def api_usgs():
    try:
        minimo = float(request.args.get("min", "0"))
    except ValueError:
        minimo = 0.0
    with LOCK:
        return jsonify({"chile": con_dist([x for x in US["chile"] if x["mag"] >= minimo]),
                        "mundo": con_dist([x for x in US["mundo"] if x["mag"] >= minimo]),
                        "calculado": US["calc"], "error": US["error"]})


_REF = {"t": 0.0}
_REF_LOCK = threading.Lock()


@app.route("/api/sismos/refresh", methods=["POST"])
def api_refresh():
    """Consulta CSN y USGS ahora mismo (minimo 15 s entre consultas, para no abusar de las fuentes)."""
    with _REF_LOCK:
        if time.time() - _REF["t"] < 15:
            return jsonify({"ok": True, "omitido": "consulta reciente"})
        _REF["t"] = time.time()
    res = {}
    for nombre, f in (("csn", ciclo), ("usgs", ciclo_usgs)):
        try:
            f()
            res[nombre] = "ok"
        except Exception as e:
            res[nombre] = str(e)[:80]
            print("sismos: refresh %s fallo: %s" % (nombre, e), flush=True)
    return jsonify(dict(res, ok=True))


if __name__ == "__main__":
    threading.Thread(target=bucle, daemon=True).start()
    threading.Thread(target=bucle_usgs, daemon=True).start()
    app.run(host="0.0.0.0", port=5022, threaded=True)
