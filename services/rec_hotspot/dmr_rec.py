#!/usr/bin/env python3
"""Graba las tramas de voz DMR (AMBE) de cada transmision que pasa por el hotspot.
Un archivo .ambe por transmision: <fecha UTC>_<origen>_<destino>_ts<slot>_<paquetes de voz>.ambe
Cada paquete de voz son 33 bytes (60 ms). Escucha la interfaz interna (lo): no genera trafico."""
import os, socket, struct, sys, time

DIR = os.environ.get("DMR_REC_DIR", "/tmp/dmr_rec")
MIN_VOZ = 17        # unos 1 s; descarta los "clics" de PTT
IDLE = 3.0          # segundos sin paquetes = fin de la transmision
MAX_MB = 30         # tope de espacio
MAX_ARCH = 600      # tope de archivos


def limpiar():
    try:
        fs = sorted(os.path.join(DIR, f) for f in os.listdir(DIR) if f.endswith(".ambe"))
        tam = sum(os.path.getsize(f) for f in fs)
        while fs and (len(fs) > MAX_ARCH or tam > MAX_MB * 1048576):
            tam -= os.path.getsize(fs[0])
            os.remove(fs.pop(0))
    except OSError:
        pass


def cerrar(s):
    if s["voz"] < MIN_VOZ:
        return
    nombre = "%s_%d_%d_ts%d_%d.ambe" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(s["t0"])),
                                       s["src"], s["dst"], s["slot"], s["voz"])
    tmp = os.path.join(DIR, nombre + ".tmp")
    with open(tmp, "wb") as f:
        f.write(s["bin"])
    os.replace(tmp, os.path.join(DIR, nombre))
    limpiar()


def main():
    os.makedirs(DIR, exist_ok=True)
    sk = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
    sk.bind(("lo", 0))
    sk.settimeout(1)
    st = {}
    while True:
        ahora = time.time()
        for k in [k for k, v in st.items() if ahora - v["ult"] > IDLE]:
            cerrar(st.pop(k))
        try:
            p = sk.recv(65535)
        except socket.timeout:
            continue
        if len(p) < 42 or p[12:14] != b"\x08\x00" or p[23] != 17:
            continue
        c = p[14 + (p[14] & 15) * 4 + 8:]
        if len(c) < 53 or c[:4] != b"DMRD":
            continue
        k, bits = c[16:20], c[15]
        s = st.get(k)
        if s is None:
            s = st[k] = {"src": int.from_bytes(c[5:8], "big"), "dst": int.from_bytes(c[8:11], "big"),
                         "slot": 2 if bits & 0x80 else 1, "t0": time.time(), "ult": 0, "voz": 0,
                         "bin": b"", "prev": None}
        if s["prev"] == c[:53]:          # en lo cada paquete aparece dos veces
            continue
        s["prev"], s["ult"] = c[:53], time.time()
        tipo = (bits >> 4) & 3
        if tipo in (0, 1):
            s["voz"] += 1
            s["bin"] += c[20:53]
        elif tipo == 2 and (bits & 15) == 2:      # terminador: fin de la transmision
            cerrar(st.pop(k))


if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("dmr_rec: %s" % e, file=sys.stderr, flush=True)
            time.sleep(5)
