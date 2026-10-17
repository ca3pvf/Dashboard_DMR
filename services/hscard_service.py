#!/usr/bin/env python3
"""Datos del equipo del hotspot para la tarjeta Pantalla Hotspot (puerto 5016).
Un hilo por hotspot consulta por SSH de forma continua mientras el dashboard lo pida, y el endpoint
responde al instante con el ultimo dato bueno. Solo marca 'sin conexion' tras varios fallos seguidos."""
import json, subprocess, time, threading
from flask import Flask, jsonify, request
APP = Flask(__name__)
BASE = '/opt/pcbox'
HS_FILE = BASE + '/hotspots.json'
POLL = 5              # segundos entre consultas mientras alguien mira
ACTIVO = 120          # sin consultas del dashboard por este tiempo, el hilo descansa
FALLOS_MAX = 3        # fallos seguidos antes de mostrar "sin conexion"
STATE = {}            # n -> {'data','ok_ts','fails','req','ev','hilo','log'}
LOCK = threading.Lock()

REMOTE = ("echo T=$(cat /sys/class/thermal/thermal_zone0/temp); "
          "echo F=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq); "
          "echo L=$(cut -d' ' -f1 /proc/loadavg); echo D=$(date '+%d/%m/%y %H:%M:%S'); "
          "ip -4 -o addr show scope global | awk '{print \"I=\"$2\" \"$4}'; "
          "grep -E '^(RXFrequency|TXFrequency)=' /etc/mmdvmhost")


@APP.after_request
def cors(r):
    r.headers['Access-Control-Allow-Origin'] = '*'
    return r


def hotspot(n):
    try:
        d = json.load(open(HS_FILE))
        if isinstance(d, dict):
            d = d.get('hotspots') or d.get('lista') or []
        h = d[n - 1]
        ip = h.get('ip') or h.get('host')
        user = h.get('user') or h.get('usuario') or 'pi-star'
        if ip:
            return ip, user
    except Exception:
        pass
    return '@@HS_IP@@', 'pi-star'


def mhz(v):
    try:
        return f"{int(v) / 1e6:.4f}"
    except Exception:
        return None


def query(n):
    """Devuelve (datos, error). error es None si todo salio bien."""
    ip, user = hotspot(n)
    cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5',
           '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3',
           '-o', 'ControlMaster=auto', '-o', 'ControlPath=/tmp/hscard-%r@%h-%p', '-o', 'ControlPersist=600',
           f'{user}@{ip}', REMOTE]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        return None, 'tiempo agotado consultando a %s' % ip
    except Exception as e:
        return None, str(e)
    d = {'ok': True, 'ip': ip}
    for ln in r.stdout.splitlines():
        v = ln[2:].strip()
        if ln.startswith('T=') and v.isdigit(): d['temp'] = round(int(v) / 1000, 1)
        elif ln.startswith('F=') and v.isdigit(): d['mhz'] = int(v) // 1000
        elif ln.startswith('L='): d['load'] = v
        elif ln.startswith('D=') and ' ' in v: d['fecha'], d['hora'] = v.split(' ', 1)
        elif ln.startswith('I=') and 'iface' not in d and ' ' in v:
            i, a = v.split(' ', 1); d['iface'] = i; d['ip'] = a.split('/')[0]
        elif ln.startswith('RXFrequency='): d['rx'] = mhz(v.split('=')[-1])
        elif ln.startswith('TXFrequency='): d['tx'] = mhz(v.split('=')[-1])
    if 'hora' not in d and 'temp' not in d:
        return None, 'ssh codigo %s: %s' % (r.returncode, (r.stderr or 'sin respuesta').strip()[:200])
    return d, None


def estado(n):
    with LOCK:
        st = STATE.get(n)
        if st is None:
            st = STATE[n] = {'data': None, 'ok_ts': 0.0, 'fails': 0, 'req': 0.0,
                             'ev': threading.Event(), 'hilo': None, 'log': 0.0}
        if st['hilo'] is None:
            st['hilo'] = threading.Thread(target=trabajador, args=(n, st), daemon=True)
            st['hilo'].start()
        return st


def trabajador(n, st):
    while True:
        if time.time() - st['req'] > ACTIVO:
            st['ev'].wait(60)
            st['ev'].clear()
            continue
        d, err = query(n)
        if d:
            st['data'], st['ok_ts'], st['fails'] = d, time.time(), 0
        else:
            st['fails'] += 1
            if time.time() - st['log'] > 60:
                st['log'] = time.time()
                print('hscard HS%d: fallo %d: %s' % (n, st['fails'], err), flush=True)
        time.sleep(POLL)


@APP.route('/api/hscard')
def hscard():
    n = request.args.get('hs', 1, type=int)
    st = estado(n)
    st['req'] = time.time()
    st['ev'].set()
    d = st['data']
    if d is None:
        # primera consulta: espera brevemente el primer resultado
        for _ in range(40):
            if st['data'] is not None or st['fails'] >= 1:
                break
            time.sleep(0.25)
        d = st['data']
    if d is None or st['fails'] >= FALLOS_MAX:
        return jsonify({'ok': False, 'fallos': st['fails']})
    out = dict(d)
    out['edad'] = round(time.time() - st['ok_ts'])
    return jsonify(out)


if __name__ == '__main__':
    APP.run(host='0.0.0.0', port=5016, threaded=True)
