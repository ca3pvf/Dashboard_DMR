#!/usr/bin/env python3
"""Consulta RadioID (DMR) con caché. GET /api/radioid?q=<indicativo|DMR ID> en puerto 5012."""
import re
import time
import threading
import requests
from flask import Flask, jsonify, request

API = "https://radioid.net/api/dmr/user/"
CACHE_SECONDS = 3600

app = Flask(__name__)
_cache = {}
_lock = threading.Lock()


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    return response


@app.route("/api/radioid")
def api_radioid():
    q = request.args.get("q", "").strip().upper().split("/")[0]
    if q.isdigit() and 1 <= len(q) <= 9:
        params = {"id": q}
    elif re.fullmatch(r"[A-Z0-9]{3,10}", q):
        params = {"callsign": q}
    else:
        return jsonify({"found": False, "error": "consulta inválida"}), 400

    with _lock:
        hit = _cache.get(q)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return jsonify(hit[1])

    try:
        r = requests.get(API, params=params, headers={"User-Agent": "Mozilla/5.0 (dashboard @@CALL@@)"}, timeout=8)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        return jsonify({"found": False, "error": str(e)}), 502

    if not results:
        data = {"found": False}
    else:
        u = results[0]
        data = {
            "found": True,
            "id": u.get("id"),
            "callsign": u.get("callsign"),
            "name": (u.get("fname") or u.get("name") or "") + (" " + u["surname"] if u.get("surname") else ""),
            "city": u.get("city"),
            "state": u.get("state"),
            "country": u.get("country"),
            "lastheard": u.get("lastheard"),
            "lastmaster": u.get("lastmaster"),
            "lasttg": u.get("lasttg"),
            "remarks": (u.get("remarks") or "").strip()[:200],
            "otros_ids": [x.get("id") for x in results[1:] if x.get("id") is not None][:10],
        }
    with _lock:
        _cache[q] = (time.time(), data)
    return jsonify(data)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5012)
