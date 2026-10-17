#!/usr/bin/env python3
"""pcbox - libreta de contactos con correo (puerto 5018).
Guarda indicativo, nombre, correo, pais y notas en contactos.db y exporta a
Google Contacts (CSV), Outlook (CSV) y vCard (.vcf, para Apple/Thunderbird/otros)."""
import csv, io, json, os, re, sqlite3, threading, time, urllib.request
from datetime import datetime
from flask import Flask, Response, jsonify, request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get("CONTACTOS_DB", os.path.join(BASE, "contactos.db"))
STORE_DB = os.environ.get("CONTACTOS_STORE_DB", os.path.join(BASE, "pcbox_store.db"))
PORT = int(os.environ.get("CONTACTOS_PORT", "5018"))
CALL_RE = re.compile(r"^[A-Z0-9]{3,10}$")
EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")
app = Flask(__name__)
_lock = threading.Lock()


@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"] = "*"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    return r


def db():
    con = sqlite3.connect(DB, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("""CREATE TABLE IF NOT EXISTS contactos (
        call TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', email TEXT NOT NULL DEFAULT '',
        country TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', fuente TEXT NOT NULL DEFAULT 'manual',
        creado REAL NOT NULL, actualizado REAL NOT NULL)""")
    con.execute("CREATE TABLE IF NOT EXISTS borrados (call TEXT PRIMARY KEY)")
    return con


def base_call(s):
    return str(s or "").upper().strip().split("-")[0].split("/")[0]


def fila(r):
    return {k: r[k] for k in ("call", "name", "email", "country", "notes", "fuente")}


def enriquecer(call):
    """Nombre y pais desde QRZ (respuesta en cache del servicio QRZ, puerto 5015)."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:5015/api/qrz/lookup?call=" + call, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (d.get("name") or "").strip(), (d.get("country") or "").strip()
    except Exception:
        return "", ""


def sync_cola(max_enriquecer=10):
    """Copia a la libreta los correos que ya estan en la cola QSL (los que encuentra QRZ o escribes a mano)."""
    if not os.path.exists(STORE_DB):
        return 0, 0
    s = sqlite3.connect(STORE_DB, timeout=10)
    s.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in s.execute("PRAGMA table_info(qsl_cola)")]
        pick = lambda *n: next((c for c in cols if c.lower() in n), None)
        cc, ce, cn, cp = pick("callsign", "call"), pick("email", "correo"), pick("name", "nombre"), pick("country", "pais")
        if not cc or not ce:
            return 0, 0
        filas = s.execute("SELECT * FROM qsl_cola").fetchall()
    finally:
        s.close()
    # Las consultas a QRZ (lentas) se hacen ANTES y fuera del _lock; el bloqueo solo cubre las escrituras
    enriq = {}
    con0 = db()
    try:
        for f in filas:
            if len(enriq) >= max_enriquecer:
                break
            call, mail = base_call(f[cc]), str(f[ce] or "").strip()
            nombre0 = str(f[cn] or "").strip() if cn else ""
            if nombre0 or call in enriq or not CALL_RE.match(call) or not EMAIL_RE.match(mail):
                continue
            if con0.execute("SELECT 1 FROM borrados WHERE call=?", (call,)).fetchone():
                continue
            if con0.execute("SELECT 1 FROM contactos WHERE call=?", (call,)).fetchone():
                continue
            enriq[call] = enriquecer(call)
    finally:
        con0.close()
    nuevos, rep, enr, ahora = 0, 0, 0, time.time()
    with _lock:
        con = db()
        try:
            for f in filas:
                call, mail = base_call(f[cc]), str(f[ce] or "").strip()
                if not CALL_RE.match(call) or not EMAIL_RE.match(mail):
                    continue
                if con.execute("SELECT 1 FROM borrados WHERE call=?", (call,)).fetchone():
                    continue
                nombre = str(f[cn] or "").strip() if cn else ""
                pais = str(f[cp] or "").strip() if cp else ""
                r = con.execute("SELECT email FROM contactos WHERE call=?", (call,)).fetchone()
                if (r is None or not r["email"]) and con.execute("SELECT 1 FROM contactos WHERE lower(email)=lower(?) AND call<>?", (mail, call)).fetchone():
                    rep += 1
                    continue
                if r is None:
                    if (not nombre) and call in enriq:
                        nombre, p2 = enriq[call]
                        pais = pais or p2
                    con.execute("INSERT INTO contactos(call,name,email,country,notes,fuente,creado,actualizado) VALUES(?,?,?,?,?,?,?,?)",
                                (call, nombre, mail, pais, "", "cola QSL", ahora, ahora))
                    nuevos += 1
                elif not r["email"]:
                    con.execute("UPDATE contactos SET email=?, actualizado=? WHERE call=?", (mail, ahora, call))
                    nuevos += 1
            con.commit()
        finally:
            con.close()
    return nuevos, rep


def _auto_sync():
    time.sleep(20)
    while True:
        try:
            sync_cola()
        except Exception as e:
            print("contactos: sync fallo: %s" % e, flush=True)
        time.sleep(300)


# ---------- exportacion ----------
def _nombres(c):
    nm = c["name"].strip()
    if nm:
        p = nm.split(None, 1)
        return p[0], ((p[1] + " ") if len(p) > 1 else "") + "(%s)" % c["call"]
    return c["call"], ""


def _nota(c):
    t = ["Indicativo: " + c["call"]]
    if c["country"]:
        t.append("País: " + c["country"])
    if c["notes"]:
        t.append(c["notes"])
    return " | ".join(t)


def _csv(cabecera, filas, bom=False):
    o = io.StringIO()
    w = csv.writer(o, lineterminator="\r\n")
    w.writerow(cabecera)
    w.writerows(filas)
    return ("\ufeff" if bom else "") + o.getvalue()


def _vesc(s):
    return str(s).replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def exportar(fmt, solo_con_correo=True):
    con = db()
    try:
        cs = [fila(r) for r in con.execute("SELECT * FROM contactos ORDER BY call")]
    finally:
        con.close()
    if solo_con_correo:
        cs = [c for c in cs if c["email"]]
        vistos, uni = set(), []
        for c in cs:
            if c["email"].lower() not in vistos:
                vistos.add(c["email"].lower()); uni.append(c)
        cs = uni
    if fmt == "google":
        return _csv(["First Name", "Last Name", "Nickname", "Notes", "Labels", "E-mail 1 - Label", "E-mail 1 - Value"],
                    [[*_nombres(c), c["call"], _nota(c), "Radioaficionados", "Other", c["email"]] for c in cs]), "text/csv", "csv"
    if fmt == "outlook":
        return _csv(["First Name", "Last Name", "E-mail Address", "Notes", "Categories"],
                    [[*_nombres(c), c["email"], _nota(c), "Radioaficionados"] for c in cs], bom=True), "text/csv", "csv"
    if fmt == "vcf":
        out = []
        for c in cs:
            n1, n2 = _nombres(c)
            out += ["BEGIN:VCARD", "VERSION:3.0", "N:%s;%s;;;" % (_vesc(n2), _vesc(n1)), "FN:%s" % _vesc((n1 + " " + n2).strip()),
                    "NICKNAME:%s" % c["call"], "EMAIL;TYPE=INTERNET:%s" % c["email"], "NOTE:%s" % _vesc(_nota(c)),
                    "CATEGORIES:Radioaficionados", "END:VCARD"]
        return "\r\n".join(out) + ("\r\n" if out else ""), "text/vcard", "vcf"
    return None, None, None


# ---------- API ----------
@app.route("/api/contactos")
def api_lista():
    con = db()
    try:
        return jsonify([fila(r) for r in con.execute("SELECT * FROM contactos ORDER BY call")])
    finally:
        con.close()


@app.route("/api/contactos", methods=["POST"])
def api_guardar():
    d = request.get_json(silent=True) or {}
    call = base_call(d.get("call"))
    mail = str(d.get("email") or "").strip()
    if not CALL_RE.match(call):
        return jsonify({"error": "indicativo invalido (3 a 10 letras o numeros, sin SSID)"}), 400
    if mail and not EMAIL_RE.match(mail):
        return jsonify({"error": "correo invalido"}), 400
    ahora = time.time()
    with _lock:
        con = db()
        try:
            r = con.execute("SELECT fuente FROM contactos WHERE call=?", (call,)).fetchone()
            if mail:
                o = con.execute("SELECT call FROM contactos WHERE lower(email)=lower(?) AND call<>?", (mail, call)).fetchone()
                if o:
                    return jsonify({"error": "ese correo ya está en la libreta (%s)" % o["call"]}), 409
            vals = (str(d.get("name") or "").strip()[:80], mail, str(d.get("country") or "").strip()[:60], str(d.get("notes") or "").strip()[:200])
            if r is None:
                con.execute("INSERT INTO contactos(call,name,email,country,notes,fuente,creado,actualizado) VALUES(?,?,?,?,?,?,?,?)",
                            (call,) + vals + ("manual", ahora, ahora))
            else:
                con.execute("UPDATE contactos SET name=?, email=?, country=?, notes=?, actualizado=? WHERE call=?", vals + (ahora, call))
            con.execute("DELETE FROM borrados WHERE call=?", (call,))
            con.commit()
        finally:
            con.close()
    return jsonify({"ok": True, "call": call})


@app.route("/api/contactos/<call>", methods=["DELETE"])
def api_borrar(call):
    with _lock:
        con = db()
        try:
            con.execute("DELETE FROM contactos WHERE call=?", (base_call(call),))
            con.execute("INSERT OR IGNORE INTO borrados(call) VALUES(?)", (base_call(call),))
            con.commit()
        finally:
            con.close()
    return jsonify({"ok": True})


@app.route("/api/contactos/sync", methods=["POST"])
def api_sync():
    n, rep = sync_cola(max_enriquecer=25)
    return jsonify({"nuevos": n, "repetidos": rep})


@app.route("/api/contactos/export")
def api_export():
    fmt = request.args.get("fmt", "google")
    texto, mime, ext = exportar(fmt, request.args.get("todos") != "1")
    if texto is None:
        return jsonify({"error": "formato no soportado"}), 400
    nombre = "contactos-radioaficionados-%s-%s.%s" % (fmt, datetime.now().strftime("%Y%m%d"), ext)
    return Response(texto.encode("utf-8"), mimetype=mime + "; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s"' % nombre})


if __name__ == "__main__":
    db().close()
    threading.Thread(target=_auto_sync, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, threaded=True)
