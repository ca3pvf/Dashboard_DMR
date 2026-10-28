#!/usr/bin/env python3
"""Explorador de grabaciones de QSO (puerto 5021).
El hotspot guarda un .ambe por transmision en /tmp/dmr_rec (servicio dmr-rec). Este servicio las copia a ~/dmr_rec,
las decodifica con mbelib (~/mbelib/libmbe.so) para escucharlas, las pasa a MP3 y las envia con la cuenta Gmail de mail_service."""
import array, ctypes, json, os, re, shutil, subprocess, threading, time, urllib.request, wave
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from flask import Flask, jsonify, request, send_file
import mail_service as ms

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
LOCAL = os.environ.get("REC_DIR", os.path.join(HOME, "dmr_rec"))
AMBE, WAV, MP3 = (os.path.join(LOCAL, d) for d in ("ambe", "wav", "mp3"))
FAVS, BORR, IDS = (os.path.join(LOCAL, n) for n in ("favs.json", "borrados.json", "ids.json"))
REMOTO = "/tmp/dmr_rec"
SYNC_S, PURGA_DIAS, WAV_DIAS, MAX_MAIL = 120, 60, 3, 12 * 1024 * 1024
RE_F = re.compile(r"^(\d{8}T\d{6}Z)_(\d+)_(\d+)_ts([12])_(\d+)\.ambe$")
RE_MAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LOCK = threading.Lock()
app = Flask(__name__)
for _d in (AMBE, WAV, MP3):
    os.makedirs(_d, exist_ok=True)


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r


def leer_json(ruta, defecto):
    try:
        with open(ruta) as f:
            return json.load(f)
    except Exception:
        return defecto


def guardar_json(ruta, datos):
    tmp = ruta + ".tmp"
    with open(tmp, "w") as f:
        json.dump(datos, f)
    os.replace(tmp, ruta)


IDMAP = leer_json(IDS, {})


def valido(f):
    return bool(RE_F.match(str(f))) and os.path.exists(os.path.join(AMBE, f))


def ts_de(m):
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp()


# ---------- hotspot ----------
def hotspot():
    try:
        h = leer_json(os.path.join(BASE, "hotspots.json"), {})["hotspots"][0]
        return h.get("user") or "pi-star", h["ip"]
    except Exception:
        return "pi-star", "192.168.1.35"


def sincronizar():
    u, ip = hotspot()
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "%s@%s" % (u, ip),
                        "systemctl is-active dmr-rec; ls -1 %s 2>/dev/null" % REMOTO],
                       capture_output=True, text=True, timeout=25)
    lineas = r.stdout.split()
    if not lineas:
        raise RuntimeError((r.stderr or "sin respuesta del hotspot").strip()[:150])
    activo, remotos = lineas[0], [x for x in lineas[1:] if RE_F.match(x)]
    borr = set(leer_json(BORR, []))
    nuevos = [x for x in remotos if x not in borr and not os.path.exists(os.path.join(AMBE, x))]
    copiados = 0
    for i in range(0, len(nuevos), 40):
        lote = nuevos[i:i + 40]
        subprocess.run(["scp", "-q", "-p"] + ["%s@%s:%s/%s" % (u, ip, REMOTO, x) for x in lote] + [AMBE + "/"],
                       capture_output=True, timeout=180)
        for x in lote:
            p = os.path.join(AMBE, x)
            if os.path.exists(p):
                if os.path.getsize(p) % 33:
                    os.remove(p)
                else:
                    copiados += 1
    return {"activo": activo, "en_hotspot": len(remotos), "nuevos": copiados}


def borrar_local(f):
    base = f[:-5]
    for p in (os.path.join(AMBE, f), os.path.join(WAV, base + ".wav"), os.path.join(MP3, base + ".mp3")):
        if os.path.exists(p):
            os.remove(p)


def purgar():
    favs, ahora = set(leer_json(FAVS, [])), time.time()
    for f in os.listdir(AMBE):
        m = RE_F.match(f)
        if m and f not in favs and ahora - ts_de(m) > PURGA_DIAS * 86400:
            borrar_local(f)
    for f in os.listdir(WAV):
        p = os.path.join(WAV, f)
        if ahora - os.path.getmtime(p) > WAV_DIAS * 86400:
            os.remove(p)


def bucle():
    time.sleep(10)
    while True:
        try:
            sincronizar()
            purgar()
        except Exception as e:
            print("rec: sincronizacion fallo: %s" % e, flush=True)
        time.sleep(SYNC_S)


# ---------- indicativo a partir del DMR ID ----------
def clave_id(src):
    return src[:7] if len(src) == 9 else src


def resolver(ids):
    falta = list(dict.fromkeys(clave_id(i) for i in ids if clave_id(i) not in IDMAP))[:10]

    def uno(i):
        try:
            with urllib.request.urlopen("http://127.0.0.1:5012/api/radioid?q=%s" % i, timeout=8) as r:
                d = json.loads(r.read().decode())
            return i, (d.get("callsign") or "") if d.get("found") else ""
        except Exception:
            return i, None
    if falta:
        with ThreadPoolExecutor(5) as ex:
            for i, c in ex.map(uno, falta):
                if c is not None:
                    IDMAP[i] = c
        guardar_json(IDS, IDMAP)


# ---------- decodificacion AMBE+2 (mbelib), igual que ambe2mp3.py ----------
A = [0, 4, 8, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48, 52, 56, 60, 64, 68, 1, 5, 9, 13, 17, 21]
B = [25, 29, 33, 37, 41, 45, 49, 53, 57, 61, 65, 69, 2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42]
C = [46, 50, 54, 58, 62, 66, 70, 3, 7, 11, 15, 19, 23, 27, 31, 35, 39, 43, 47, 51, 55, 59, 63, 67, 71]
_LIB = None


def cargar_lib():
    global _LIB
    if _LIB:
        return _LIB
    for r in (os.environ.get("MBELIB"), HOME + "/mbelib/libmbe.so", HOME + "/mbelib/build/libmbe.so", BASE + "/libmbe.so"):
        if r and os.path.exists(r):
            lib = ctypes.CDLL(r)
            lib.mbe_initMbeParms.argtypes = [ctypes.c_void_p] * 3
            lib.mbe_processAmbe3600x2450Frame.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p,
                                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                                          ctypes.c_void_p, ctypes.c_int]
            _LIB = lib
            return lib
    raise RuntimeError("no encuentro libmbe.so (compila mbelib en ~/mbelib)")


class Decodificador:
    def __init__(self, lib):
        self.lib = lib
        self.cur, self.prev, self.enh = (ctypes.create_string_buffer(8192) for _ in range(3))
        lib.mbe_initMbeParms(self.cur, self.prev, self.enh)

    def trama(self, filas):
        fr = (ctypes.c_char * 96).from_buffer_copy(bytes(filas))
        out = (ctypes.c_short * 160)()
        e1, e2 = ctypes.c_int(0), ctypes.c_int(0)
        txt, d = ctypes.create_string_buffer(64), ctypes.create_string_buffer(49)
        self.lib.mbe_processAmbe3600x2450Frame(out, ctypes.byref(e1), ctypes.byref(e2), txt, fr, d,
                                               self.cur, self.prev, self.enh, 3)
        return list(out), e1.value, e2.value


def tramas(rafaga):
    t = [(rafaga[i >> 3] >> (7 - (i & 7))) & 1 for i in range(264)]
    return [t[0:72], t[72:108] + t[156:192], t[192:264]]


def filas(f, oa, ob):
    r = [[0] * 24 for _ in range(4)]
    for i in range(24):
        r[0][23 - i if oa == 0 else i] = f[A[i]]
    for i in range(23):
        r[1][22 - i if ob == 0 else i] = f[B[i]]
    for i in range(25):
        if i < 11:
            r[2][10 - i if ob == 0 else i] = f[C[i]]
        else:
            r[3][13 - (i - 11) if ob == 0 else (i - 11)] = f[C[i]]
    return [x for fila in r for x in fila]


def decodificar(lib, rafagas, oa, ob):
    dec, pcm, e2s, n = Decodificador(lib), array.array("h"), 0, 0
    for rf in rafagas:
        for f in tramas(rf):
            m, e1, e2 = dec.trama(filas(f, oa, ob))
            pcm.extend(m)
            e2s += e2
            n += 1
    return pcm, e2s / max(n, 1)


def asegurar_wav(f):
    wav = os.path.join(WAV, f[:-5] + ".wav")
    if os.path.exists(wav):
        return wav
    with LOCK:
        if os.path.exists(wav):
            return wav
        datos = open(os.path.join(AMBE, f), "rb").read()
        rafagas = [datos[i:i + 33] for i in range(0, len(datos) - 32, 33)]
        if not rafagas:
            raise ValueError("archivo vacío")
        lib = cargar_lib()
        mejor = None
        for oa in (0, 1):
            for ob in (0, 1):
                e = decodificar(lib, rafagas[:40], oa, ob)[1]
                if mejor is None or e < mejor[0]:
                    mejor = (e, oa, ob)
        pcm, _ = decodificar(lib, rafagas, mejor[1], mejor[2])
        pico = max((abs(x) for x in pcm), default=0)
        if pico > 0:
            g = 28000.0 / pico
            pcm = array.array("h", (int(max(-32768, min(32767, x * g))) for x in pcm))
        tmp = wav + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(pcm.tobytes())
        os.replace(tmp, wav)
    return wav


def asegurar_mp3(f):
    out = os.path.join(MP3, f[:-5] + ".mp3")
    if os.path.exists(out):
        return out
    wav = asegurar_wav(f)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("falta ffmpeg (sudo apt install ffmpeg)")
    tmp = out + ".tmp.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-ar", "16000", "-ac", "1",
                    "-codec:a", "libmp3lame", "-b:a", "32k", tmp], check=True, timeout=90)
    os.replace(tmp, out)
    return out


# ---------- API ----------
@app.route("/api/rec/list")
def api_list():
    q = re.sub(r"[^A-Za-z0-9]", "", request.args.get("q", "")).upper()
    favs, items = set(leer_json(FAVS, [])), []
    for f in os.listdir(AMBE):
        m = RE_F.match(f)
        if m:
            items.append({"f": f, "ts": ts_de(m), "src": m.group(2), "tg": m.group(3), "slot": int(m.group(4)),
                          "dur": round(int(m.group(5)) * 0.06, 1), "fav": f in favs,
                          "mp3": os.path.exists(os.path.join(MP3, f[:-5] + ".mp3"))})
    def num(k):
        try:
            return float(request.args.get(k, ""))
        except ValueError:
            return None
    t0, t1, dmin, dmax = num("t0"), num("t1"), num("dmin"), num("dmax")
    if request.args.get("fav") == "1":
        items = [x for x in items if x["fav"]]
    if t0 is not None:
        items = [x for x in items if x["ts"] >= t0]
    if t1 is not None:
        items = [x for x in items if x["ts"] <= t1]
    if dmin is not None:
        items = [x for x in items if x["dur"] >= dmin]
    if dmax is not None:
        items = [x for x in items if x["dur"] <= dmax]
    items.sort(key=lambda x: -x["ts"])
    resolver([x["src"] for x in items])
    for x in items:
        x["call"] = IDMAP.get(clave_id(x["src"]), "")
    pend = len({clave_id(x["src"]) for x in items if clave_id(x["src"]) not in IDMAP})
    if q:
        items = [x for x in items if q in x["call"] or q in x["src"] or q == x["tg"]]
    claves = {"fecha": lambda x: x["ts"], "indicativo": lambda x: x["call"] or "~" + x["src"],
              "tg": lambda x: int(x["tg"]), "duracion": lambda x: x["dur"]}
    items.sort(key=claves.get(request.args.get("orden", "fecha"), claves["fecha"]),
               reverse=request.args.get("dir", "desc") != "asc")
    return jsonify({"items": items[:200], "total": len(items), "pendientes": pend})


@app.route("/api/rec/sync", methods=["POST"])
def api_sync():
    try:
        return jsonify(sincronizar())
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/rec/audio/<f>")
def api_audio(f):
    if not valido(f):
        return jsonify({"error": "no existe"}), 404
    try:
        return send_file(asegurar_wav(f), mimetype="audio/wav", conditional=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rec/mp3/<f>")
def api_mp3(f):
    if not valido(f):
        return jsonify({"error": "no existe"}), 404
    try:
        return send_file(asegurar_mp3(f), mimetype="audio/mpeg", as_attachment=True, download_name=f[:-5] + ".mp3")
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/rec/fav", methods=["POST"])
def api_fav():
    b = request.get_json(silent=True) or {}
    f = str(b.get("f", ""))
    if not valido(f):
        return jsonify({"error": "no existe"}), 404
    favs = set(leer_json(FAVS, []))
    if b.get("fav"):
        favs.add(f)
    else:
        favs.discard(f)
    guardar_json(FAVS, sorted(favs))
    return jsonify({"ok": True, "fav": f in favs})


@app.route("/api/rec/delete", methods=["POST"])
def api_delete():
    f = str((request.get_json(silent=True) or {}).get("f", ""))
    if not valido(f):
        return jsonify({"error": "no existe"}), 404
    borrar_local(f)
    guardar_json(BORR, sorted(set(leer_json(BORR, [])) | {f}))
    guardar_json(FAVS, sorted(set(leer_json(FAVS, [])) - {f}))
    return jsonify({"ok": True})


@app.route("/api/rec/send", methods=["POST"])
def api_send():
    b = request.get_json(silent=True) or {}
    f, to = str(b.get("f", "")), str(b.get("to", "")).strip()
    if not valido(f):
        return jsonify({"error": "grabación inválida"}), 400
    if not RE_MAIL.match(to) or len(to) > 120:
        return jsonify({"error": "correo de destino inválido"}), 400
    cfg = ms.leer_cfg()
    if not (cfg.get("address") and cfg.get("app_password")):
        return jsonify({"error": "Gmail sin configurar (Configuración → Gmail)"}), 400
    if ms.enviados_hoy() >= ms.DAILY_LIMIT:
        return jsonify({"error": "límite diario de correos alcanzado"}), 429
    try:
        mp3 = asegurar_mp3(f)
        if os.path.getsize(mp3) > MAX_MAIL:
            return jsonify({"error": "el MP3 es demasiado pesado para enviarlo por correo"}), 413
        m = RE_F.match(f)
        fecha = datetime.fromtimestamp(ts_de(m), timezone.utc).strftime("%d-%m-%Y %H:%M")
        op = cfg.get("op_call") or "radioaficionado"
        msg = EmailMessage()
        msg["From"] = formataddr((cfg.get("from_name") or op, cfg["address"]))
        msg["To"] = to
        msg["Subject"] = "Grabación de QSO del %s UTC - TG %s" % (fecha, m.group(3))
        msg.set_content("Hola,\n\nAdjunto la grabación del QSO del %s UTC (TG %s, %.1f s).\n\n73, %s"
                        % (fecha, m.group(3), int(m.group(5)) * 0.06, op))
        with open(mp3, "rb") as fh:
            msg.add_attachment(fh.read(), maintype="audio", subtype="mpeg", filename=f[:-5] + ".mp3")
        ms._enviar(msg, cfg)
    except Exception as e:
        return jsonify({"error": ms._err_texto(e)}), 502
    ms.sumar_envio()
    return jsonify({"ok": True})


if __name__ == "__main__":
    threading.Thread(target=bucle, daemon=True).start()
    app.run(host="0.0.0.0", port=5021, threaded=True)
