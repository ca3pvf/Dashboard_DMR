from flask import Flask, jsonify, request
import os
import psutil
import shutil
import subprocess
import threading
import time
from collections import deque
from urllib.parse import urlparse

app = Flask(__name__)

MUESTREO_S = 30        # una muestra cada 30 s
MAX_MUESTRAS = 240     # 240 x 30 s = 2 horas de historial
_hist = deque(maxlen=MAX_MUESTRAS)
_lock = threading.Lock()
_ultimo = {"cpu": None}


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response


def leer_temp_c():
    # Rockchip (RK3318) expone la temperatura en thermal_zone0 en miligrados
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None


def muestrear():
    psutil.cpu_percent(interval=None)   # inicializa la referencia de CPU
    espera = 2
    while True:
        time.sleep(espera)
        espera = MUESTREO_S
        try:
            cpu = psutil.cpu_percent(interval=None)
            ram = round(psutil.virtual_memory().percent, 1)
            temp = leer_temp_c()
            with _lock:
                _ultimo["cpu"] = cpu
                _hist.append((int(time.time()), cpu, ram, temp))
        except Exception as e:
            print("system_status: muestreo fallo: %s" % e, flush=True)


@app.route("/api/system")
def system_status():
    mem = psutil.virtual_memory()
    disco = shutil.disk_usage("/")

    ram_total_gb = round(mem.total / (1024**3), 1)
    ram_libre_gb = round(mem.available / (1024**3), 1)

    disco_total_gb = round(disco.total / (1024**3), 1)
    disco_libre_gb = round(disco.free / (1024**3), 1)

    temp_c = leer_temp_c()
    temp_f = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
    with _lock:
        cpu = _ultimo["cpu"]

    return jsonify({
        "ram": {"libre_gb": ram_libre_gb, "total_gb": ram_total_gb},
        "disco": {"libre_gb": disco_libre_gb, "total_gb": disco_total_gb},
        "temp_c": temp_c,
        "temp_f": temp_f,
        "cpu_pct": cpu
    })


@app.route("/api/system/history")
def system_history():
    with _lock:
        filas = list(_hist)
    return jsonify({"paso_s": MUESTREO_S,
                    "t": [f[0] for f in filas], "cpu": [f[1] for f in filas],
                    "ram": [f[2] for f in filas], "temp": [f[3] for f in filas]})


# ---------- VNC: activar / desactivar vncserver@1 desde el dashboard ----------
VNC_UNIT = "vncserver@1.service"
SYSTEMCTL = "/usr/bin/systemctl" if os.path.exists("/usr/bin/systemctl") else shutil.which("systemctl")


def vnc_soportado():
    # solo en equipos con systemd y la unidad vncserver@1 (en el portable de Windows/Mac no aparece)
    if not SYSTEMCTL:
        return False
    try:
        r = subprocess.run([SYSTEMCTL, "cat", VNC_UNIT], capture_output=True, text=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def vnc_estado():
    try:
        r = subprocess.run([SYSTEMCTL, "is-active", VNC_UNIT], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() or "desconocido"
    except Exception:
        return "desconocido"


@app.route("/api/vnc", methods=["GET", "POST", "OPTIONS"])
def api_vnc():
    if request.method == "OPTIONS":
        return ("", 204)
    if not vnc_soportado():
        return jsonify({"soportado": False, "activo": False, "estado": "no disponible"})
    if request.method == "POST":
        # solo se acepta desde el propio dashboard (mismo equipo), no desde otras páginas web
        origen = request.headers.get("Origin", "")
        if origen and urlparse(origen).hostname != urlparse("//" + request.host).hostname:
            return jsonify({"error": "origen no permitido"}), 403
        accion = (request.get_json(silent=True) or {}).get("accion")
        if accion not in ("start", "stop"):
            return jsonify({"error": "acción inválida"}), 400
        try:
            r = subprocess.run(["sudo", "-n", SYSTEMCTL, accion, VNC_UNIT],
                               capture_output=True, text=True, timeout=30)
            error = None if r.returncode == 0 else ((r.stderr or r.stdout or "falló").strip()[:200])
        except Exception as e:
            error = str(e)
        estado = vnc_estado()
        out = {"soportado": True, "activo": estado == "active", "estado": estado}
        if error:
            out["error"] = error
            return jsonify(out), 500
        return jsonify(out)
    estado = vnc_estado()
    return jsonify({"soportado": True, "activo": estado == "active", "estado": estado})


if __name__ == "__main__":
    threading.Thread(target=muestrear, daemon=True).start()
    app.run(host="0.0.0.0", port=5010)
