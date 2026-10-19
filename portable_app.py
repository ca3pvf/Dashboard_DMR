#!/usr/bin/env python3
"""Dashboard pcbox - version portable.

Ejecuta una sola app: abre el navegador, pide la configuracion la primera vez y sirve el dashboard
con TODAS las APIs (antes 14 servicios en 14 puertos) en un unico puerto.

Los servicios de la carpeta services/ son los mismos archivos de pcbox, sin modificar: al arrancar
se les aplican parches en memoria (rutas de datos, puertos, IP del hotspot, SSH opcional).
Asi, cuando mejores un servicio en pcbox, basta copiarlo a services/ y volver a empaquetar.

Uso:  python portable_app.py [--port 8765] [--lan] [--no-browser] [--setup] [--data CARPETA]
"""
import argparse
import html
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
import urllib.request
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

VERSION = "1.1"
FROZEN = getattr(sys, "frozen", False)
RES = Path(getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent)      # codigo y recursos
EXE_DIR = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
RE_CALL = re.compile(r"^[A-Z0-9]{3,9}$")
RE_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
RE_USER = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")

# (clave, archivo). La clave es el nombre del modulo y el prefijo de sus endpoints.
MODULOS = [
    ("store", "store_service.py"),
    ("lastheard", "lastheard_service.py"),
    ("wpsd", "wpsd_dashboard_service.py"),
    ("bm", "bm_dashboard.py"),
    ("protocols", "protocols_status.py"),
    ("system", "system_status.py"),
    ("radioid", "radioid_service.py"),
    ("qrz", "qrz_service.py"),
    ("mail", "mail_service.py"),
    ("contactos", "contactos_service.py"),
    ("hscard", "hscard_service.py"),
    ("sat", "sat_service.py"),
    ("sismos", "sismos_service.py"),
    ("aprs", "aprs_service.py"),
    ("salud", "salud_lite.py"),
]


def _dependencias():
    """Nunca se ejecuta. Declara los modulos que usan los servicios de services/ (se cargan con exec,
    asi que PyInstaller no los ve): sin esta lista el ejecutable saldria sin ellos."""
    import base64, collections, csv, datetime, email.message, email.utils, io, math, smtplib, sqlite3, ssl, unicodedata  # noqa
    import subprocess, xml.etree.ElementTree, requests, psutil, sgp4.api, socketio, engineio, websocket  # noqa


def log(*a):
    print(*a, flush=True)


def hilo(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


# Lo que hay que poner en marcha en cada modulo (en pcbox lo hace su bloque __main__)
ARRANQUE = {
    "store": lambda m: m.init_db(),
    "lastheard": lambda m: (m.init_db(), [hilo(m.poll_loop, s) for s in range(1, m.SLOTS + 1)]),
    "wpsd": lambda m: [hilo(m.worker, s) for s in range(1, m.SLOTS + 1)],
    "bm": lambda m: (hilo(m.start_socketio), hilo(m.refrescar)),
    "protocols": lambda m: (hilo(m._protocols_loop), hilo(m._active_qso_loop)),
    "system": lambda m: hilo(m.muestrear),
    "contactos": lambda m: (m.db().close(), hilo(m._auto_sync)),
    "sat": lambda m: hilo(m.bucle),
    "sismos": lambda m: (hilo(m.bucle), hilo(m.bucle_usgs)),
    "aprs": lambda m: (m.init_db(), m.cargar_config(), m.bot_rearmar(),
                       [hilo(f) for f in (m.conectar_loop, m.reintentos_loop, m.baliza_loop)]),
    "salud": lambda m: hilo(m.bucle),
}


# ---------------------------------------------------------------- datos y configuracion
def elegir_datos(arg):
    cand = Path(arg or os.environ.get("PCBOX_HOME") or EXE_DIR / "pcbox-data")
    try:
        cand.mkdir(parents=True, exist_ok=True)
        t = cand / ".escritura"
        t.write_text("x")
        t.unlink()
        return cand
    except OSError:
        alt = Path.home() / "pcbox-data"
        alt.mkdir(parents=True, exist_ok=True)
        log("Aviso: no se puede escribir en %s; los datos irán a %s" % (cand, alt))
        return alt


def leer_json(ruta, defecto=None):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defecto


def guardar_json(ruta, datos):
    tmp = str(ruta) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=2, ensure_ascii=False)
    os.replace(tmp, ruta)


def validar_setup(b):
    """Devuelve (config, error)."""
    call = str(b.get("callsign", "")).strip().upper()
    if not RE_CALL.match(call):
        return None, "Indicativo inválido (3 a 9 letras o números, sin SSID)"
    try:
        lat, lon = float(str(b.get("lat", "")).replace(",", ".")), float(str(b.get("lon", "")).replace(",", "."))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError
    except ValueError:
        return None, "Latitud o longitud inválida (ej. -33.45 y -70.67)"
    dmr = str(b.get("dmr_id", "")).strip()
    if dmr and not (dmr.isdigit() and len(dmr) == 7):
        return None, "El DMR ID personal tiene 7 dígitos"
    h = b.get("hotspot") or {}
    ip = str(h.get("ip", "")).strip()
    if not RE_HOST.match(ip):
        return None, "IP o nombre del hotspot inválido"
    user = str(h.get("user") or "pi-star").strip()
    if not RE_USER.match(user):
        return None, "Usuario SSH inválido"
    limpio = lambda s, n: re.sub(r"[\x00-\x1f<>&\"']", "", str(s or "")).strip()[:n]
    return {"version": VERSION, "callsign": call, "nombre": limpio(b.get("nombre"), 40),
            "ciudad": limpio(b.get("ciudad"), 40), "pais": limpio(b.get("pais"), 40),
            "lat": lat, "lon": lon, "dmr_id": dmr,
            "hotspot": {"ip": ip, "name": limpio(h.get("name"), 24) or "Hotspot 1", "user": user,
                        "ssh": bool(h.get("ssh"))}}, None


def escribir_archivos(data, cfg):
    guardar_json(data / "config.json", cfg)
    h = cfg["hotspot"]
    guardar_json(data / "hotspots.json", {"hotspots": [{
        "id": 1, "name": h["name"], "ip": h["ip"], "user": h["user"], "remote_port": "7642",
        "dmr_id": (cfg["dmr_id"] + "01") if cfg["dmr_id"] else "", "ssh": h["ssh"], "enabled": True, "http_port": 80}]})
    ap = leer_json(data / "aprs_config.json", {}) or {}
    ap.update({"callsign": cfg["callsign"], "lat": cfg["lat"], "lon": cfg["lon"]})
    ap.setdefault("ssid", "10")
    guardar_json(data / "aprs_config.json", ap)
    if not (data / "tgs_config.json").exists():
        guardar_json(data / "tgs_config.json", {"seguimiento": [], "activo": None})


# ---------------------------------------------------------------- puertos y asistente
def puerto_libre(host, preferido):
    for p in (preferido, 0):
        s = socket.socket()
        try:
            s.bind((host, p))
            return s.getsockname()[1]
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError("no hay puertos libres")


def probar_hotspot(ip):
    if not RE_HOST.match(ip):
        return {"ok": False, "error": "IP inválida"}
    t0 = time.time()
    try:
        socket.create_connection((ip, 80), timeout=4).close()
    except OSError as e:
        return {"ok": False, "error": e.__class__.__name__}
    res = {"ok": True, "ms": int((time.time() - t0) * 1000), "api": False}
    try:
        with urllib.request.urlopen("http://%s/api/?limit=1" % ip, timeout=10) as r:
            res["api"] = r.status == 200
    except Exception:
        pass
    return res


def probar_ssh(ip, user):
    """Una sola conexión de prueba con la configuración SSH de este equipo (llaves de ~/.ssh).
    Nunca pide ni guarda contraseñas. Devuelve {ok, estado, mensaje[, comando]}."""
    ip, user = str(ip or "").strip(), str(user or "pi-star").strip()
    if not RE_HOST.match(ip) or not RE_USER.match(user):
        return {"ok": False, "estado": "datos", "mensaje": "IP o usuario inválido."}
    ssh = shutil.which("ssh")
    if not ssh:
        return {"ok": False, "estado": "sin_ssh",
                "mensaje": "Este equipo no tiene el comando ssh. En Windows 10/11: Configuración → Aplicaciones → "
                           "Características opcionales → Cliente OpenSSH."}
    cmd = [ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new",
           "%s@%s" % (user, ip), "echo PCBOX_OK; hostname"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        return {"ok": False, "estado": "red", "mensaje": "El hotspot no respondió por SSH (tiempo agotado). Revisa que "
                                                         "esté encendido, en la misma red y con SSH habilitado."}
    lineas = (r.stdout or "").split()
    if "PCBOX_OK" in lineas:
        host = lineas[lineas.index("PCBOX_OK") + 1] if len(lineas) > lineas.index("PCBOX_OK") + 1 else ""
        return {"ok": True, "estado": "ok", "mensaje": "SSH funciona" + (" (equipo: %s)." % host if host else ".")}
    err = (r.stderr or "").lower()
    if "host key verification failed" in err or "identification has changed" in err:
        return {"ok": False, "estado": "huella", "comando": "ssh-keygen -R %s" % ip,
                "mensaje": "La huella del hotspot cambió desde la última vez (por ejemplo, tras reinstalar o cambiar "
                           "la tarjeta SD). Si fue por eso, bórrala y vuelve a probar:"}
    if "permission denied" in err:
        return {"ok": False, "estado": "llave",
                "mensaje": "El hotspot responde, pero no acepta la llave de este equipo. Autorízala con los comandos "
                           "de arriba (la contraseña la escribes tú en esa terminal)."}
    if "could not resolve" in err or "name or service not known" in err:
        return {"ok": False, "estado": "nombre", "mensaje": "No se reconoce esa IP o nombre de equipo."}
    if any(x in err for x in ("timed out", "no route", "refused", "unreachable")):
        return {"ok": False, "estado": "red", "mensaje": "No hay conexión SSH con el hotspot: revisa que esté encendido, "
                                                         "en la misma red y con SSH habilitado."}
    return {"ok": False, "estado": "otro", "mensaje": "No se pudo conectar: %s" % ((r.stderr or "sin detalle").strip()[:200])}


def asistente(host, port, data, abrir):
    app = Flask("asistente")
    srv = make_server(host, port, app, threaded=True)

    @app.route("/")
    def pagina():
        return Response((RES / "wizard.html").read_text(encoding="utf-8"), mimetype="text/html")

    @app.route("/api/portable/probe")
    def probe():
        return jsonify(probar_hotspot(request.args.get("ip", "").strip()))

    @app.route("/api/portable/ssh-test")
    def ssh_test():
        return jsonify(probar_ssh(request.args.get("ip"), request.args.get("user")))

    @app.route("/api/portable/setup", methods=["POST"])
    def setup():
        cfg, err = validar_setup(request.get_json(silent=True) or {})
        if err:
            return jsonify({"error": err}), 400
        escribir_archivos(data, cfg)
        threading.Thread(target=lambda: (time.sleep(0.6), srv.shutdown()), daemon=True).start()
        return jsonify({"ok": True})

    log("Primera vez: abre http://%s:%d para configurar." % ("localhost" if host == "127.0.0.1" else host, port))
    if abrir:
        threading.Timer(0.8, webbrowser.open, ["http://localhost:%d/" % port]).start()
    srv.serve_forever()
    srv.server_close()


# ---------------------------------------------------------------- carga de servicios con parches
def parchear(clave, src, ctx):
    """Adapta el codigo de pcbox al modo portable, solo en memoria. Devuelve (src, avisos)."""
    av = []

    def reemplazar(viejo, nuevo, obligatorio=False):
        nonlocal src
        if viejo in src:
            src = src.replace(viejo, nuevo)
        elif obligatorio:
            av.append("no se encontró: %r" % viejo[:60])

    # 1) carpeta de datos
    src = src.replace("os.path.dirname(os.path.dirname(os.path.abspath(__file__)))", "__PCBOX_DATA__")
    RAICES = ["/opt/pcbox"]
    src = re.sub(r"""(["'])(?:%s)(/[^"']*)?\1""" % "|".join(map(re.escape, RAICES)),
                 lambda m: '(__PCBOX_DATA__ + "%s")' % (m.group(2) or ""), src)
    # 2) llamadas entre servicios: ahora todo vive en un solo puerto
    src = re.sub(r"127\.0\.0\.1:50\d\d", "127.0.0.1:%d" % ctx["port"], src)
    # 3) hotspot y operador
    hs = ctx["cfg"]["hotspot"]
    src = src.replace("@@HS_IP@@", hs["ip"])
    src = src.replace("@@CALL@@", ctx["cfg"]["callsign"])
    src = src.replace("pi-star@", hs["user"] + "@")
    src = re.sub(r"""(["'])pi-star\1""", lambda m: m.group(1) + hs["user"] + m.group(1), src)
    if clave == "bm":
        src = re.sub(r"(?m)^MI_ID = \d+", "MI_ID = %s" % (ctx["cfg"]["dmr_id"] or "0"), src)
    # 4) SSH opcional: sin SSH, los servicios que lo usan responden vacio sin lanzar procesos
    if clave == "protocols":
        reemplazar('    full_cmd = ["ssh"] + SSH_OPTS',
                   '    if not __SSH_ON__:\n        return ""\n    full_cmd = ["ssh"] + SSH_OPTS', True)
    if clave == "bm":
        reemplazar("def leer_redes():\n", "def leer_redes():\n    if not __SSH_ON__:\n        return []\n", True)
    if clave == "hscard":
        reemplazar("    ip, user = hotspot(n)\n    cmd = ['ssh',",
                   "    ip, user = hotspot(n)\n    if not __SSH_ON__:\n        return None, 'SSH desactivado'\n    cmd = ['ssh',", True)
        reemplazar("'-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/hscard-%r@%h-%p', '-o', 'ControlPersist=600',",
                   "*__SSH_MUX__,", True)
    # 5) huella del hotspot: se acepta la primera vez y se rechaza si cambia (igual que wpsd_dashboard_service)
    reemplazar('SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]',
               'SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new"]',
               clave == "protocols")
    reemplazar('"ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", SSH_TARGET,',
               '"ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", SSH_TARGET,',
               clave == "bm")
    reemplazar("'-o', 'ServerAliveInterval=10',",
               "'-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=10',", clave == "hscard")
    return src, av


def cargar_modulos(ctx):
    sys.path.insert(0, str(RES / "services"))
    inyectado = {"__PCBOX_DATA__": str(ctx["data"]), "__SSH_ON__": ctx["ssh"],
                 "__SSH_MUX__": [] if os.name == "nt" else
                 ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/pcbox-portable-%r@%h-%p", "-o", "ControlPersist=600"]}
    cargados = {}
    for clave, archivo in MODULOS:
        ruta = RES / "services" / archivo
        if not ruta.exists():
            log("  - %s: archivo no incluido, se omite" % archivo)
            continue
        try:
            src, av = parchear(clave, ruta.read_text(encoding="utf-8"), ctx)
            for a in av:
                log("  ! %s: parche sin aplicar (%s)" % (archivo, a))
            mod = types.ModuleType(clave)
            mod.__file__ = str(ruta)
            mod.__dict__.update(inyectado)
            exec(compile(src, str(ruta), "exec"), mod.__dict__)
            sys.modules[clave] = mod
            cargados[clave] = mod
        except Exception as e:
            log("  ! %s: no se pudo cargar (%s: %s)" % (archivo, e.__class__.__name__, e))
    return cargados


def montar(app, cargados):
    vistos = {}
    for clave, mod in cargados.items():
        sub = getattr(mod, "app", None) or getattr(mod, "APP", None)
        if sub is None:
            continue
        for regla in sub.url_map.iter_rules():
            if regla.endpoint == "static":
                continue
            metodos = sorted((regla.methods or set()) - {"HEAD", "OPTIONS"})
            for m in metodos:
                if (regla.rule, m) in vistos:
                    log("  ! ruta repetida %s %s (%s y %s): gana la primera" % (m, regla.rule, vistos[(regla.rule, m)], clave))
            if any((regla.rule, m) in vistos for m in metodos):
                continue
            app.add_url_rule(regla.rule, endpoint="%s.%s" % (clave, regla.endpoint),
                             view_func=sub.view_functions[regla.endpoint], methods=metodos)
            for m in metodos:
                vistos[(regla.rule, m)] = clave


# ---------------------------------------------------------------- dashboard.html adaptado
def html_dashboard(cfg):
    """El mismo dashboard.html de pcbox: los endpoints pasan al mismo origen y se pone la identidad del usuario."""
    h = (RES / "dashboard.html").read_text(encoding="utf-8")
    h = re.sub(r"'http://'\s*\+\s*location\.hostname\s*\+\s*':\d{4}", "location.origin + '", h)
    h = h.replace("'http://' + location.hostname + ':' + puerto + ruta", "location.origin + ruta")
    h = h.replace("url.indexOf(':5011/api/ranking')", "url.indexOf('/api/ranking')")   # antes distinguia por puerto
    esc = lambda s: html.escape(str(s or ""), quote=True)
    call, nombre = cfg["callsign"], cfg["nombre"]
    qth = ", ".join(x for x in (cfg["ciudad"], cfg["pais"]) if x)
    for marca, valor in (("@@NOMBRE@@", nombre), ("@@QTH@@", qth), ("@@CALL@@", call),
                         ("@@HS_IP@@", cfg["hotspot"]["ip"]), ("@@DMR@@", cfg["dmr_id"])):
        h = h.replace(marca, esc(valor))
    h = re.sub(r"const LAT = [-\d.]+, LON = [-\d.]+;", "const LAT = %s, LON = %s;" % (cfg["lat"], cfg["lon"]), h)
    return h


def _repo_y_rama():
    try:
        t = (RES / "actualizar.py").read_text(encoding="utf-8")
        return re.search(r'(?m)^REPO = "([^"]+)"', t).group(1), re.search(r'(?m)^RAMA = "([^"]+)"', t).group(1)
    except Exception:
        return None, "main"


def _version_local():
    try:
        v = (RES / "VERSION").read_text(encoding="utf-8").strip()
        if v:
            return v
    except OSError:
        pass
    return VERSION


def _num(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


_UPD = {"t": 0.0, "r": None}


def revisar_actualizacion(fresco=False):
    """Compara el archivo VERSION de esta carpeta con el publicado en GitHub (repositorio publico)."""
    if not fresco and _UPD["r"] and time.time() - _UPD["t"] < 6 * 3600:
        return _UPD["r"]
    repo, rama = _repo_y_rama()
    r = {"version": _version_local(), "repo": repo, "ultima": None, "hay_nueva": False, "error": None,
         "es_git": (RES / ".git").is_dir(), "congelado": FROZEN}
    if not repo or repo.startswith("USUARIO/"):
        r["error"] = "El actualizador no está configurado (REPO en actualizar.py)"
        return r
    try:
        req = urllib.request.Request("https://raw.githubusercontent.com/%s/%s/VERSION" % (repo, rama),
                                     headers={"User-Agent": "pcbox-actualizador"})
        with urllib.request.urlopen(req, timeout=10) as f:
            ult = f.read(64).decode("utf-8", "replace").strip()
        if not re.fullmatch(r"\d+(\.\d+){0,3}", ult):
            raise ValueError("el archivo VERSION publicado no es válido")
        r["ultima"] = ult
        r["hay_nueva"] = _num(ult) > _num(r["version"])
        _UPD.update(t=time.time(), r=r)
    except Exception as e:
        if getattr(e, "code", None) == 404:
            r["error"] = "No se encontró la versión publicada (404). El repositorio debe ser público y tener un archivo VERSION."
        else:
            r["error"] = "Sin conexión con GitHub (%s)" % e.__class__.__name__
    return r


def aplicar_actualizacion():
    if FROZEN:
        return {"ok": False, "error": "Esta versión empaquetada no se actualiza sola: descarga la nueva desde GitHub."}
    if (RES / ".git").is_dir():
        return {"ok": False, "error": "Esta carpeta es un repositorio git: actualiza con «git pull»."}
    try:
        p = subprocess.run([sys.executable, str(RES / "actualizar.py")], capture_output=True, text=True,
                           timeout=180, cwd=str(RES))
    except Exception as e:
        return {"ok": False, "error": "No se pudo ejecutar el actualizador (%s)" % e.__class__.__name__}
    _UPD["r"] = None
    salida = ((p.stdout or "") + (p.stderr or "")).strip()[-400:]
    return {"ok": p.returncode == 0, "salida": salida or "Ya tienes lo último publicado."}


def crear_app(cargados, cfg):
    app = Flask("pcbox_portable")
    app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
    app.json.sort_keys = False
    montar(app, cargados)

    @app.errorhandler(404)
    def no_incluido(e):
        if request.path.startswith("/api/"):      # las tarjetas esperan JSON, no la página HTML de error
            return jsonify({"error": "Esta función no está incluida en la versión portable"}), 404
        return e

    def pagina():
        r = Response(html_dashboard(leer_json(app.config["CFG_PATH"], cfg)), mimetype="text/html")
        r.headers["Cache-Control"] = "no-store"
        return r
    app.add_url_rule("/", "portable.pagina", pagina)
    app.add_url_rule("/dashboard.html", "portable.pagina2", pagina)
    app.add_url_rule("/favicon.ico", "portable.favicon", lambda: ("", 204))
    app.add_url_rule("/api/portable/ready", "portable.ready", lambda: jsonify({"ok": True}))
    app.add_url_rule("/api/portable/ssh-test", "portable.ssh_test",
                     lambda: jsonify(probar_ssh(request.args.get("ip"), request.args.get("user"))))
    app.add_url_rule("/api/portable/info", "portable.info",
                     lambda: jsonify({"version": VERSION, "modulos": sorted(cargados), "ssh": app.config["SSH_ON"],
                                      "indicativo": cfg["callsign"], "datos": app.config["DATA_DIR"]}))
    app.add_url_rule("/api/portable/update-check", "portable.upd_check",
                     lambda: jsonify(revisar_actualizacion(request.args.get("fresh") == "1")))
    app.add_url_rule("/api/portable/update-apply", "portable.upd_apply",
                     lambda: jsonify(aplicar_actualizacion()), methods=["POST"])
    return app


# ---------------------------------------------------------------- principal
def main():
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Dashboard pcbox portable")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--lan", action="store_true", help="permitir el acceso desde otros equipos de la red (por defecto solo este equipo)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--setup", action="store_true", help="volver a mostrar el asistente de configuración")
    ap.add_argument("--data", help="carpeta de datos (por defecto pcbox-data junto a la app)")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    data = elegir_datos(a.data)
    host = "0.0.0.0" if a.lan else "127.0.0.1"
    port = puerto_libre(host, a.port)
    cfg_path = data / "config.json"
    log("Dashboard pcbox portable %s · datos en %s" % (VERSION, data))

    cfg = leer_json(cfg_path)
    hubo_asistente = False
    if a.setup or not cfg:
        hubo_asistente = True
        asistente(host, port, data, not a.no_browser)
        cfg = leer_json(cfg_path)
        if not cfg:
            return 1

    ssh = bool(cfg["hotspot"].get("ssh"))
    if ssh and not shutil.which("ssh"):
        log("Aviso: no se encontró el comando 'ssh' en este equipo; se desactiva el acceso SSH.")
        ssh = False
    ctx = {"cfg": cfg, "data": data, "port": port, "ssh": ssh}
    log("Cargando servicios…")
    cargados = cargar_modulos(ctx)
    app = crear_app(cargados, cfg)
    app.config.update(CFG_PATH=str(cfg_path), SSH_ON=ssh, DATA_DIR=str(data))
    for clave, _ in MODULOS:
        if clave in cargados and clave in ARRANQUE:
            try:
                ARRANQUE[clave](cargados[clave])
            except Exception as e:
                log("  ! %s: no arrancó (%s: %s)" % (clave, e.__class__.__name__, e))

    url = "http://localhost:%d/" % port
    log("Listo: %s   (Ctrl+C para cerrar)" % url)
    if a.lan:
        try:
            ip = socket.gethostbyname(socket.gethostname())
            log("Desde otros equipos de la red: http://%s:%d/" % (ip, port))
        except OSError:
            pass
        log("Ojo: en modo --lan cualquiera en tu red puede abrir el dashboard y sus ajustes (Gmail, QRZ).")
    if not a.no_browser and not hubo_asistente:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    srv = make_server(host, port, app, threaded=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("Cerrando…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
