#!/usr/bin/env python3
"""Almacen del dashboard de pcbox (SQLite), puerto 5013.
Guarda la configuracion del dashboard y el registro de QSOs para que se vean
igual desde cualquier equipo. Las contrasenas NUNCA se guardan aqui."""
import json
import os
import re
import sqlite3
import time

from flask import Flask, jsonify, request

DB_PATH = os.environ.get("PCBOX_STORE_DB", "/opt/pcbox/pcbox_store.db")
PORT = 5013

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB (imagen de fondo QSL)
app.json.sort_keys = False


@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return resp


def db():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY, value TEXT NOT NULL, updated REAL NOT NULL)""")
    con.execute("""CREATE TABLE IF NOT EXISTS qsos (
        uid TEXT PRIMARY KEY, seq INTEGER NOT NULL,
        fecha_hora TEXT, op_call TEXT, dx_call TEXT, freq TEXT, band TEXT,
        mode TEXT, rst_s TEXT, rst_r TEXT, comments TEXT,
        data TEXT NOT NULL, updated REAL NOT NULL)""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_qsos_seq ON qsos(seq)")
    con.execute("""CREATE TABLE IF NOT EXISTS qsl_cola (
        uid TEXT PRIMARY KEY, callsign TEXT NOT NULL, time_utc TEXT, target TEXT, mode TEXT,
        name TEXT, country TEXT, duration REAL, estado TEXT NOT NULL DEFAULT 'pendiente',
        added REAL NOT NULL)""")
    if "email" not in [r[1] for r in con.execute("PRAGMA table_info(qsl_cola)")]:
        con.execute("ALTER TABLE qsl_cola ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    con.commit()
    con.close()


# ---------- configuracion ----------
@app.route("/api/store/config", methods=["GET"])
def config_all():
    con = db()
    rows = con.execute("SELECT key, value FROM config").fetchall()
    con.close()
    return jsonify({r["key"]: r["value"] for r in rows})


@app.route("/api/store/config/<path:key>", methods=["GET"])
def config_get(key):
    con = db()
    r = con.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    con.close()
    return jsonify({"key": key, "value": r["value"] if r else None})


@app.route("/api/store/config/<path:key>", methods=["PUT"])
def config_put(key):
    body = request.get_json(silent=True) or {}
    value = body.get("value")
    if not isinstance(value, str):
        return jsonify({"error": "falta 'value' (texto)"}), 400
    con = db()
    con.execute(
        "INSERT INTO config(key,value,updated) VALUES(?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (key, value, time.time()))
    con.commit()
    con.close()
    return jsonify({"ok": True})


@app.route("/api/store/config/<path:key>", methods=["DELETE"])
def config_delete(key):
    con = db()
    con.execute("DELETE FROM config WHERE key=?", (key,))
    con.commit()
    con.close()
    return jsonify({"ok": True})


# ---------- QSOs ----------
@app.route("/api/store/qsos", methods=["GET"])
def qsos_all():
    con = db()
    rows = con.execute("SELECT data FROM qsos ORDER BY seq").fetchall()
    con.close()
    return jsonify([json.loads(r["data"]) for r in rows])


@app.route("/api/store/qsos/sync", methods=["POST"])
def qsos_sync():
    body = request.get_json(silent=True) or {}
    upsert = body.get("upsert") or []
    borrar = body.get("delete") or []
    con = db()
    try:
        con.execute("BEGIN IMMEDIATE")
        for uid in borrar:
            con.execute("DELETE FROM qsos WHERE uid=?", (str(uid),))
        for q in upsert:
            uid = str(q.get("uid") or "")
            if not uid:
                continue
            existe = con.execute("SELECT seq FROM qsos WHERE uid=?", (uid,)).fetchone()
            if existe:
                seq = existe["seq"]
            else:
                seq = con.execute("SELECT COALESCE(MAX(seq),0)+1 FROM qsos").fetchone()[0]
            con.execute(
                "INSERT INTO qsos(uid,seq,fecha_hora,op_call,dx_call,freq,band,mode,rst_s,rst_r,comments,data,updated) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid) DO UPDATE SET fecha_hora=excluded.fecha_hora, op_call=excluded.op_call, "
                "dx_call=excluded.dx_call, freq=excluded.freq, band=excluded.band, mode=excluded.mode, "
                "rst_s=excluded.rst_s, rst_r=excluded.rst_r, comments=excluded.comments, "
                "data=excluded.data, updated=excluded.updated",
                (uid, seq, q.get("fechaHora"), q.get("opCall"), q.get("dxCall"), q.get("freq"),
                 q.get("band"), q.get("mode"), q.get("rstS"), q.get("rstR"), q.get("comments"),
                 json.dumps(q, ensure_ascii=False), time.time()))
        con.commit()
    except Exception as e:
        con.rollback()
        con.close()
        return jsonify({"error": str(e)}), 500
    n = con.execute("SELECT COUNT(*) FROM qsos").fetchone()[0]
    con.close()
    return jsonify({"ok": True, "total": n})


# ---------- cola de QSL (desde Last Heard) ----------
@app.route("/api/store/qslcola", methods=["GET"])
def qslcola_list():
    con = db()
    rows = con.execute("SELECT uid,callsign,time_utc,target,mode,name,country,duration,estado,added,email "
                       "FROM qsl_cola ORDER BY added DESC").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/store/qslcola", methods=["POST"])
def qslcola_add():
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    nuevos = 0
    con = db()
    try:
        for q in items:
            uid = str(q.get("uid") or "")[:200]
            call = str(q.get("callsign") or "").strip().upper()[:20]
            if not uid or not call:
                continue
            try:
                dur = float(q.get("duration") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            cur = con.execute(
                "INSERT OR IGNORE INTO qsl_cola(uid,callsign,time_utc,target,mode,name,country,duration,added) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, call, str(q.get("time_utc") or "")[:30], str(q.get("target") or "")[:40],
                 str(q.get("mode") or "")[:20], str(q.get("name") or "")[:80],
                 str(q.get("country") or "")[:60], dur, time.time()))
            nuevos += cur.rowcount
        con.commit()
        total = con.execute("SELECT COUNT(*) FROM qsl_cola").fetchone()[0]
    finally:
        con.close()
    return jsonify({"ok": True, "agregados": nuevos, "total": total})


@app.route("/api/store/qslcola/delete", methods=["POST"])
def qslcola_delete():
    body = request.get_json(silent=True) or {}
    con = db()
    try:
        if body.get("all"):
            con.execute("DELETE FROM qsl_cola")
        else:
            for uid in body.get("uids") or []:
                con.execute("DELETE FROM qsl_cola WHERE uid=?", (str(uid),))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.route("/api/store/qslcola/estado", methods=["POST"])
def qslcola_estado():
    body = request.get_json(silent=True) or {}
    estado = str(body.get("estado") or "")
    if estado not in ("pendiente", "generada", "enviada"):
        return jsonify({"error": "estado inválido"}), 400
    con = db()
    try:
        for uid in body.get("uids") or []:
            con.execute("UPDATE qsl_cola SET estado=? WHERE uid=?", (estado, str(uid)))
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True})


@app.route("/api/store/qslcola/email", methods=["POST"])
def qslcola_email():
    body = request.get_json(silent=True) or {}
    emails = body.get("emails") or {}
    if not isinstance(emails, dict):
        return jsonify({"error": "formato invalido"}), 400
    con = db()
    n = 0
    try:
        for call, mail in emails.items():
            mail = str(mail or "").strip()[:120]
            if mail and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", mail):
                continue
            n += con.execute("UPDATE qsl_cola SET email=? WHERE callsign=?", (mail, str(call).strip().upper())).rowcount
        con.commit()
    finally:
        con.close()
    return jsonify({"ok": True, "actualizados": n})


@app.route("/api/store/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT)
