from flask import Flask, jsonify
import psutil
import shutil
import threading
import time
from collections import deque

app = Flask(__name__)

MUESTREO_S = 30        # una muestra cada 30 s
MAX_MUESTRAS = 240     # 240 x 30 s = 2 horas de historial
_hist = deque(maxlen=MAX_MUESTRAS)
_lock = threading.Lock()
_ultimo = {"cpu": None}


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
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


if __name__ == "__main__":
    threading.Thread(target=muestrear, daemon=True).start()
    app.run(host="0.0.0.0", port=5010)
