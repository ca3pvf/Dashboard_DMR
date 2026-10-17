#!/usr/bin/env python3
"""
protocols_status.py
Consulta el estado de cada protocolo del hotspot WPSD (Raspi 3B) combinando:
- systemctl is-active del servicio real en la Raspi (FUENTE DE VERDAD:
  si el servicio está inactivo/deshabilitado, el protocolo NO puede estar
  "green" sin importar lo que diga el log histórico)
- RemoteCommand 'status' (puerto 7642, vía SSH + nc local en la Raspi)
- Parseo de los logs de cada gateway (/var/log/pi-star/), solo quien
  matiza green/yellow/red cuando el servicio SÍ está activo
Expone GET /api/protocols -> JSON con dmr_networks, ysf, p25, nxdn, dstar,
más los 5 modos cruzados: ysf2dmr, ysf2nxdn, ysf2p25, dmr2ysf, dmr2nxdn
Expone GET /api/active-qso -> JSON con las transmisiones en curso AHORA
MISMO (no historial), parseando /var/log/pi-star/MMDVM-*.log
"""
import re
import subprocess
import threading
import time
from datetime import datetime
from flask import Flask, jsonify

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


# ---- Config: ajusta a tu entorno ----
SSH_HOST = "@@HS_IP@@"
SSH_USER = "pi-star"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
LOG_DIR = "/var/log/pi-star"
LOOKBACK_LINES = 150
FAIL_STREAK_FOR_YELLOW = 2
FAIL_STREAK_FOR_RED = 5
ACTIVE_QSO_LOOKBACK_LINES = 500  # ventana de MMDVM-*.log a revisar para QSO's activos

# Nombre del protocolo -> nombre real del servicio systemd en la Raspi.
# Estos 4 SÍ son daemons permanentes: systemctl is-active es confiable.
SERVICES = {
    "ysf": "ysfgateway",
    "p25": "p25gateway",
    "nxdn": "nxdngateway",
    "dstar": "ircddbgateway",
}

# Los 5 modos cruzados NO son daemons permanentes: corren disparados por
# un .timer cada ~10 min, y su .service siempre queda "dead" entre ciclos
# -- is-active no sirve para diferenciar habilitado/deshabilitado acá.
# La señal real: cuando el modo está habilitado, el script lanza el
# proceso de puente y lo loguea como "<NOMBRE> started as PID <pid>".
# Cuando está deshabilitado, el ciclo del timer arranca y se desactiva
# de inmediato sin llegar a lanzar ningún proceso -> no aparece esa línea.
CROSS_SERVICES = {
    "ysf2dmr": "ysf2dmr",
    "ysf2nxdn": "ysf2nxdn",
    "ysf2p25": "ysf2p25",
    "dmr2ysf": "dmr2ysf",
    "dmr2nxdn": "dmr2nxdn",
}
CROSS_JOURNAL_LINES = 15  # cuántas líneas recientes de journal revisar por servicio


def ssh_run(cmd: str) -> str:
    """Ejecuta un comando en la Raspi vía SSH y devuelve stdout como texto."""
    full_cmd = ["ssh"] + SSH_OPTS + [f"{SSH_USER}@{SSH_HOST}", cmd]
    try:
        result = subprocess.run(full_cmd, capture_output=True, text=True, timeout=10)
        return result.stdout
    except Exception:
        return ""


def get_service_states() -> dict:
    """Una sola llamada SSH para los 4 gateways permanentes (is-active confiable)."""
    names = list(SERVICES.values())
    cmd = "systemctl is-active " + " ".join(names) + " 2>/dev/null"
    raw = ssh_run(cmd)
    lines = raw.strip().split("\n") if raw.strip() else []
    states = {}
    for i, name in enumerate(names):
        states[name] = lines[i].strip() if i < len(lines) else "unknown"
    return states


def get_cross_journal_snapshots() -> dict:
    """
    Una sola llamada SSH que trae el journal reciente de los 5 servicios
    cruzados, delimitado por marcadores para poder separarlos en Python
    después (más rápido que 5 llamadas SSH individuales).
    """
    parts = []
    for name in CROSS_SERVICES.values():
        parts.append(
            f'echo "===START:{name}==="; '
            f"journalctl -u {name}.service -n {CROSS_JOURNAL_LINES} --no-pager -q 2>/dev/null; "
            f'echo "===END:{name}==="'
        )
    raw = ssh_run(" ; ".join(parts))
    snapshots = {}
    for name in CROSS_SERVICES.values():
        m = re.search(
            rf"===START:{re.escape(name)}===(.*?)===END:{re.escape(name)}===",
            raw, re.S,
        )
        snapshots[name] = m.group(1) if m else ""
    return snapshots


def is_service_up(service_name: str, states: dict) -> bool:
    return states.get(service_name) == "active"


def get_remote_command_status() -> dict:
    """Consulta RemoteCommand 'status' vía nc (referencia/debug, no fuente única)."""
    raw = ssh_run("echo -n status | nc -u -w1 127.0.0.1 7642")
    result = {}
    for pair in raw.strip().split():
        if ":" in pair:
            proto, state = pair.split(":", 1)
            result[proto] = state
    return result


def tail_latest_log(prefix: str, lines: int = LOOKBACK_LINES) -> str:
    cmd = (
        f"f=$(ls -t {LOG_DIR}/{prefix}-*.log 2>/dev/null | head -1); "
        f'[ -n "$f" ] && tail -n {lines} "$f"'
    )
    return ssh_run(cmd)


def classify(success_hits: int, fail_hits: int, last_is_success: bool) -> str:
    if success_hits == 0 and fail_hits == 0:
        return "no_configurado"
    if success_hits == 0 and fail_hits > 0:
        return "red"
    if last_is_success:
        return "green"
    if fail_hits >= FAIL_STREAK_FOR_RED:
        return "red"
    if fail_hits >= FAIL_STREAK_FOR_YELLOW:
        return "yellow"
    return "green"


DMR_FLAP_FAILS_FOR_YELLOW = 3   # fallas de login en la ventana para marcar amarilla una red conectada
DMR_FLAP_WINDOW_SECONDS = 600

MODE_SECTION = {"ysf": "System Fusion", "p25": "P25", "nxdn": "NXDN", "dstar": "D-Star"}
CROSS_BASE = {
    "ysf2dmr": "System Fusion", "ysf2nxdn": "System Fusion", "ysf2p25": "System Fusion",
    "dmr2ysf": "DMR", "dmr2nxdn": "DMR",
}

# Una sola llamada SSH con todo el estado "real" del hotspot (misma fuente que usa el panel de WPSD)
WPSD_STATE_CMD = r'''
echo "###MODES"
awk '/^\[/{s=$0} /^Enable=/{print s "=" $0}' /etc/mmdvmhost | grep -E '^\[(D-Star|DMR|System Fusion|P25|NXDN)\]='
echo "###CROSS"
for f in ysf2dmr ysf2nxdn ysf2p25 dmr2ysf dmr2nxdn; do
  if grep -q 'Enabled=1' /etc/$f 2>/dev/null; then echo "$f=1"; else echo "$f=0"; fi
done
echo "###PROCS"
for p in YSF2DMR YSF2NXDN YSF2P25 DMR2YSF DMR2NXDN; do
  if pgrep -x "$p" >/dev/null; then echo "$p=1"; else echo "$p=0"; fi
done
echo "###DMRGW"
P=$(grep -A8 '^\[Remote Control\]' /etc/dmrgateway | grep -m1 '^Port=' | cut -d= -f2)
cd /var/log/pi-star && sudo /usr/local/bin/RemoteCommand "$P" status | tail -n +2
echo "###CONF"
cat /etc/dmrgateway
'''


def get_wpsd_state() -> dict:
    raw = ssh_run(WPSD_STATE_CMD)
    sections, cur = {}, None
    for line in raw.splitlines():
        if line.startswith("###"):
            cur = line[3:].strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(line)

    state = {"ok": "CONF" in sections and "MODES" in sections,
             "modes": {}, "cross_cfg": {}, "cross_proc": {}, "net_status": {}, "networks": []}

    for l in sections.get("MODES", []):
        m = re.match(r"^\[(.+)\]=Enable=(\d)", l.strip())
        if m:
            state["modes"][m.group(1)] = (m.group(2) == "1")
    for l in sections.get("CROSS", []):
        k, _, v = l.strip().partition("=")
        if k:
            state["cross_cfg"][k] = (v == "1")
    for l in sections.get("PROCS", []):
        k, _, v = l.strip().partition("=")
        if k:
            state["cross_proc"][k.lower()] = (v == "1")
    for l in sections.get("DMRGW", []):
        for k, v in re.findall(r"(net\d+|xlx):(\S+)", l):
            state["net_status"][k] = v

    idx, cur = 0, None
    for l in sections.get("CONF", []):
        l = l.strip()
        m = re.match(r"^\[(.+)\]$", l)
        if m:
            sec = m.group(1)
            if sec.startswith("DMR Network"):
                idx += 1
                cur = {"net": f"net{idx}", "name": sec, "enabled": False}
                state["networks"].append(cur)
            elif sec == "XLX Network":
                cur = {"net": "xlx", "name": "XLX", "enabled": False}
                state["networks"].append(cur)
            else:
                cur = None
            continue
        if cur is not None and "=" in l:
            k, v = l.split("=", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "enabled":
                cur["enabled"] = (v == "1")
            elif k == "name" and v:
                cur["name"] = v
    return state


def get_dmr_events() -> str:
    """Eventos de login de DMRGateway en los ultimos 3 archivos de log (solo para detectar inestabilidad)."""
    cmd = (
        f"cat $(ls -t {LOG_DIR}/DMRGateway-*.log 2>/dev/null | head -3 | tac) 2>/dev/null | "
        "grep -E 'Logged into the master successfully|Login to the master has failed|Closing DMR Network'"
    )
    return ssh_run(cmd)


def _dmr_fail_counts(events_text: str) -> dict:
    ahora = datetime.utcnow()  # los logs vienen en UTC
    counts = {}
    for line in events_text.splitlines():
        m = re.match(r"^[A-Z]: ([\d\-]+ [\d:]+)\.\d+ ([A-Za-z0-9_]+), (.+)$", line)
        if not m:
            continue
        ts, name, msg = m.groups()
        if "Login to the master has failed" not in msg and "Closing DMR Network" not in msg:
            continue
        try:
            t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if -5 <= (ahora - t).total_seconds() <= DMR_FLAP_WINDOW_SECONDS:
            counts[name] = counts.get(name, 0) + 1
    return counts


def build_dmr_networks(state: dict, events_text: str) -> list:
    fails = _dmr_fail_counts(events_text)
    out = []
    for n in state["networks"]:
        if not n["enabled"]:
            continue
        if state["net_status"].get(n["net"]) == "conn":
            status = "yellow" if fails.get(n["name"], 0) >= DMR_FLAP_FAILS_FOR_YELLOW else "green"
        else:
            status = "red"
        out.append({"name": n["name"], "status": status})
    return out


def mode_enabled(state: dict, proto_key: str) -> bool:
    return state["modes"].get(MODE_SECTION[proto_key], False)


def cross_status(name: str, state: dict, journal_text: str) -> dict:
    if not (state["modes"].get(CROSS_BASE[name], False) and state["cross_cfg"].get(name, False)):
        return {"status": "no_configurado"}
    if state["cross_proc"].get(name, False) or re.search(r"started as PID", journal_text, re.I):
        return {"status": "green"}
    return {"status": "yellow"}


def parse_single_gateway(log_text: str, success_patterns: list, fail_patterns: list,
                          unconfigured_patterns: list = None) -> dict:
    success, fail, unconfigured, last_success = 0, 0, 0, False
    for line in log_text.splitlines():
        if any(p in line for p in success_patterns):
            success += 1
            last_success = True
        elif unconfigured_patterns and any(p in line for p in unconfigured_patterns):
            unconfigured += 1
            last_success = False
        elif any(p in line for p in fail_patterns):
            fail += 1
            last_success = False
    if success == 0 and fail == 0 and unconfigured > 0:
        return {"status": "no_configurado"}
    return {"status": classify(success, fail, last_success)}


def gated_status(proto_key: str, log_based_status: dict, states: dict) -> dict:
    """
    Aplica la regla de oro: si el servicio real no está activo en la
    Raspi, el protocolo NO puede mostrarse en green pase lo que pase
    en el log histórico. Si está activo, se respeta el resultado del
    parseo de log (que sí distingue green/yellow/red con matiz).
    """
    service_name = SERVICES[proto_key]
    if not is_service_up(service_name, states):
        return {"status": "no_configurado"}
    return log_based_status


def cross_mode_status(service_name: str, journal_text: str) -> dict:
    """
    Habilitado (verde): el journal reciente muestra que el puente
    efectivamente arrancó con un PID ('started as PID').
    Deshabilitado (no_configurado): el timer se dispara pero el
    service se desactiva sin llegar a lanzar el proceso.
    Sin datos en absoluto (SSH falló, o nunca ha corrido el timer):
    también no_configurado, por seguridad (mejor subestimar que
    marcar en falso positivo verde).
    """
    if re.search(r"started as PID", journal_text, re.I):
        return {"status": "green"}
    return {"status": "no_configurado"}


# ============================================================
# QSO's ACTIVOS
# Parsea MMDVM-*.log (no los logs por gateway, que solo hablan de
# apertura/cierre de red) buscando pares "inicio de transmisión" /
# "fin de transmisión" por (protocolo, slot, vía RF|network). Un
# "inicio" sin su "fin" correspondiente, al llegar al final del log,
# es una transmisión que sigue en curso ahora mismo.
#
# Ejemplos reales vistos en el log:
#   DMR (network):
#     M: ... DMR Slot 2, received network voice header from CA3DOF to TG 730
#     M: ... DMR Slot 2, received network end of voice transmission from
#         CA3DOF to TG 730, 0.8 seconds, 0% packet loss, BER: 0.0%
#   DMR (RF, tu propia transmisión):
#     M: ... DMR Slot 2, received RF voice header from @@CALL@@ to 730997
#     M: ... DMR Slot 2, received RF end of voice transmission from
#         @@CALL@@ to 730997, 19.4 seconds, BER: 1.4%, RSSI: -72/-68/-69 dBm
#   NXDN (network):
#     M: ... NXDN, received network transmission from K2CRM to TG 65000
#     M: ... NXDN, received network end of transmission from K2CRM to
#         TG 65000, 0.6 seconds
#
# YSF/P25/D-Star deberían seguir un patrón similar (MMDVMHost es quien
# escribe estas líneas para todos los modos), pero falta confirmar el
# wording exacto con ejemplos reales de esos protocolos.
# ============================================================
QSO_HEADER_RE = re.compile(
    r"^M: (?P<ts>[\d\- :.]+) (?P<proto>[A-Za-z0-9\-]+)(?: Slot (?P<slot>\d))?, "
    r"received (?P<via>RF|network) (?:voice )?(?:header|transmission) from "
    r"(?P<call>[\w.]+) to (?P<target>.+)$"
)
QSO_END_RE = re.compile(
    r"^M: (?P<ts>[\d\- :.]+) (?P<proto>[A-Za-z0-9\-]+)(?: Slot (?P<slot>\d))?, "
    r"received (?P<via>RF|network) end of (?:voice )?transmission from "
    r"(?P<call>[\w.]+) to (?P<target>.+?), (?P<dur>[\d.]+) seconds"
)


def tail_latest_mmdvm_log(lines: int = ACTIVE_QSO_LOOKBACK_LINES) -> str:
    cmd = (
        f"f=$(ls -t {LOG_DIR}/MMDVM-*.log 2>/dev/null | head -1); "
        f'[ -n "$f" ] && tail -n {lines} "$f"'
    )
    return ssh_run(cmd)


def parse_active_qsos(log_text: str) -> list:
    """
    Recorre el log en orden cronológico. Por cada línea de fin de
    transmisión, cierra (descarta) el "inicio" pendiente que le
    corresponde. Lo que quede pendiente al terminar de recorrer todo
    el log son las transmisiones que siguen activas.
    """
    pending = {}
    for line in log_text.splitlines():
        m_end = QSO_END_RE.match(line)
        if m_end:
            key = (m_end.group("proto"), m_end.group("slot"), m_end.group("via"))
            pending.pop(key, None)
            continue
        m_start = QSO_HEADER_RE.match(line)
        if m_start:
            key = (m_start.group("proto"), m_start.group("slot"), m_start.group("via"))
            pending[key] = {
                "protocolo": m_start.group("proto"),
                "slot": m_start.group("slot"),
                "via": m_start.group("via"),  # RF (local) o network (remoto)
                "callsign": m_start.group("call"),
                "target": m_start.group("target").strip(),
                "inicio": m_start.group("ts"),
            }
    return list(pending.values())


ACTIVE_QSO_MAX_AGE = 300  # segundos; un "inicio" sin "fin" mas viejo que esto se descarta


def filtrar_vigentes(activos):
    ahora = datetime.utcnow()  # los logs de MMDVM vienen en UTC
    out = []
    for q in activos:
        try:
            ini = datetime.strptime(q["inicio"].split(".")[0].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if -5 <= (ahora - ini).total_seconds() <= ACTIVE_QSO_MAX_AGE:
            out.append(q)
    return out


ACTIVE_QSO_REFRESH_SECONDS = 1   # cada cuanto el hilo lee el log de la Raspi
ACTIVE_QSO_IDLE_SECONDS = 30     # si nadie consulta en este tiempo, el hilo se duerme
ACTIVE_QSO_STALE_SECONDS = 5     # dato mas viejo que esto se recalcula al momento
_active_cache = {"data": None, "ts": 0.0, "last_request": 0.0}
_active_lock = threading.Lock()
_active_wake = threading.Event()


def _active_qso_compute():
    log_text = tail_latest_mmdvm_log()
    activos = filtrar_vigentes(parse_active_qsos(log_text))
    data = {
        "activos": activos,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    with _active_lock:
        _active_cache["data"] = data
        _active_cache["ts"] = time.time()
    return data


def _active_qso_loop():
    while True:
        with _active_lock:
            inactivo = time.time() - _active_cache["last_request"] > ACTIVE_QSO_IDLE_SECONDS
        if inactivo:
            _active_wake.wait()
            _active_wake.clear()
            continue
        try:
            _active_qso_compute()
        except Exception as e:
            print(f"error refrescando active-qso: {e}", flush=True)
        time.sleep(ACTIVE_QSO_REFRESH_SECONDS)


@app.route("/api/active-qso")
def api_active_qso():
    with _active_lock:
        _active_cache["last_request"] = time.time()
        data = _active_cache["data"]
        edad = time.time() - _active_cache["ts"]
    _active_wake.set()
    if data is None or edad > ACTIVE_QSO_STALE_SECONDS:
        data = _active_qso_compute()
    return jsonify(data)


def build_protocols():
    states = get_service_states()
    cross_snapshots = get_cross_journal_snapshots()
    rc_status = get_remote_command_status()

    dmr_events = get_dmr_events()
    wstate = get_wpsd_state()
    if not wstate["ok"]:
        raise RuntimeError("SSH a la Raspi no respondio")
    ysf_log = tail_latest_log("YSFGateway")
    p25_log = tail_latest_log("P25Gateway")
    nxdn_log = tail_latest_log("NXDNGateway")
    dstar_log = tail_latest_log("ircDDBGateway")

    dmr_networks = build_dmr_networks(wstate, dmr_events)

    ysf_log_status = parse_single_gateway(
        ysf_log,
        success_patterns=["Linked to", "Link successful to MMDVM"],
        fail_patterns=["Unlinked from", "connection has failed"],
    )
    p25_log_status = parse_single_gateway(
        p25_log,
        success_patterns=["linked to reflector", "Switched to reflector"],
        fail_patterns=["error", "failed"],
    )
    nxdn_log_status = parse_single_gateway(
        nxdn_log,
        success_patterns=["linked to reflector", "Switched to reflector"],
        fail_patterns=["error", "failed"],
    )
    dstar_log_status = parse_single_gateway(
        dstar_log,
        success_patterns=["Logged in", "Connected to"],
        fail_patterns=["connection has failed"],
        unconfigured_patterns=["getaddrinfo", "Name or service not known"],
    )

    ysf = gated_status("ysf", ysf_log_status, states) if mode_enabled(wstate, "ysf") else {"status": "no_configurado"}
    p25 = gated_status("p25", p25_log_status, states) if mode_enabled(wstate, "p25") else {"status": "no_configurado"}
    nxdn = gated_status("nxdn", nxdn_log_status, states) if mode_enabled(wstate, "nxdn") else {"status": "no_configurado"}
    dstar = gated_status("dstar", dstar_log_status, states) if mode_enabled(wstate, "dstar") else {"status": "no_configurado"}

    ysf2dmr = cross_status("ysf2dmr", wstate, cross_snapshots.get("ysf2dmr", ""))
    ysf2nxdn = cross_status("ysf2nxdn", wstate, cross_snapshots.get("ysf2nxdn", ""))
    ysf2p25 = cross_status("ysf2p25", wstate, cross_snapshots.get("ysf2p25", ""))
    dmr2ysf = cross_status("dmr2ysf", wstate, cross_snapshots.get("dmr2ysf", ""))
    dmr2nxdn = cross_status("dmr2nxdn", wstate, cross_snapshots.get("dmr2nxdn", ""))

    return ({
        "remote_command_status": rc_status,
        "service_states": states,  # debug: gateways permanentes
        "dmr_networks": dmr_networks,
        "ysf": ysf,
        "p25": p25,
        "nxdn": nxdn,
        "dstar": dstar,
        "ysf2dmr": ysf2dmr,
        "ysf2nxdn": ysf2nxdn,
        "ysf2p25": ysf2p25,
        "dmr2ysf": dmr2ysf,
        "dmr2nxdn": dmr2nxdn,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })


PROTOCOLS_REFRESH_SECONDS = 10   # cada cuanto el hilo refresca mientras alguien mira
PROTOCOLS_IDLE_SECONDS = 60      # sin consultas en este tiempo, el hilo se duerme
PROTOCOLS_STALE_SECONDS = 25     # dato mas viejo que esto se recalcula al momento
_protocols_cache = {"data": None, "ts": 0.0, "last_request": 0.0}
_protocols_lock = threading.Lock()
_protocols_wake = threading.Event()


def _protocols_compute():
    data = build_protocols()
    with _protocols_lock:
        _protocols_cache["data"] = data
        _protocols_cache["ts"] = time.time()
    return data


def _protocols_loop():
    while True:
        with _protocols_lock:
            inactivo = time.time() - _protocols_cache["last_request"] > PROTOCOLS_IDLE_SECONDS
        if inactivo:
            _protocols_wake.wait()
            _protocols_wake.clear()
            continue
        try:
            _protocols_compute()
        except Exception as e:
            print(f"error refrescando protocolos: {e}", flush=True)
        time.sleep(PROTOCOLS_REFRESH_SECONDS)


@app.route("/api/protocols")
def api_protocols():
    with _protocols_lock:
        _protocols_cache["last_request"] = time.time()
        data = _protocols_cache["data"]
        edad = time.time() - _protocols_cache["ts"]
    _protocols_wake.set()
    if data is None or edad > PROTOCOLS_STALE_SECONDS:
        try:
            data = _protocols_compute()
        except Exception as e:
            if data is None:
                return jsonify({"error": str(e)}), 503
            with _protocols_lock:
                _protocols_cache["ts"] = time.time()   # no reintentar en cada consulta
    return jsonify(data)


if __name__ == "__main__":
    threading.Thread(target=_protocols_loop, daemon=True).start()
    threading.Thread(target=_active_qso_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=5009)
