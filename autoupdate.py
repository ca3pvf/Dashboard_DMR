#!/usr/bin/env python3
"""Actualización automática del portable, por releases de GitHub (un release existe solo cuando ya se
compilaron todos los paquetes). Compartido por Dashboard_DMR y DMRShack.
  - Ejecutable (.exe / binario Linux / macOS): descarga el paquete de este sistema, lo verifica (tamaño,
    SHA-256 y formato), lo prueba con --version, conserva el anterior y se reinicia; si el nuevo no
    arranca en ~45 s, un script vuelve a abrir el anterior.
  - Carpeta con código (zip o git): usa actualizar.py, comprueba que el código nuevo compila y arranca,
    y si algo falla deja todo como estaba.
Nunca toca la carpeta de datos. Un archivo .no-actualizar en la carpeta de la app desactiva todo
(para carpetas de desarrollo)."""
import hashlib, json, os, platform, re, shlex, shutil, subprocess, sys, tarfile, threading, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

REPO = "ca3pvf/Dashboard_DMR"
UA = "pcbox-actualizador"
CADA = 6 * 3600
ESPERA_INICIAL = 90
REINTENTO = 24 * 3600          # una versión que falló no se reintenta sola antes de esto

_E = {"res": Path("."), "exe_dir": Path("."), "frozen": False, "datos": Path("."), "version": "0",
      "log": print, "boot": 0, "ver_arranque": "0"}
_cache = {"t": 0.0, "r": None, "rel": None}
_ocupado = threading.Lock()
_cfg_lock = threading.Lock()


def init(res, exe_dir, frozen, datos, version, log=print):
    _E.update(res=Path(res), exe_dir=Path(exe_dir), frozen=bool(frozen), datos=Path(datos),
              version=str(version), log=log, boot=int(time.time() * 1000))
    _E["ver_arranque"] = version_local()


def version_local():
    try:
        v = (_E["res"] / "VERSION").read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+(\.\d+){0,3}", v):
            return v
    except OSError:
        pass
    return _E["version"]


def _num(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4]) or (0,)


def _base():
    return _E["exe_dir"] if _E["frozen"] else _E["res"]


def _bloqueado():
    return (_base() / ".no-actualizar").exists()


# ---------------- ajustes ----------------
def leer_cfg():
    c = {"auto": True}
    try:
        c.update(json.loads((_E["datos"] / "update.json").read_text(encoding="utf-8")))
    except Exception:
        pass
    return c


def guardar_cfg(**kv):
    with _cfg_lock:
        c = leer_cfg()
        c.update(kv)
        try:
            ruta = _E["datos"] / "update.json"
            tmp = ruta.with_suffix(".tmp")
            tmp.write_text(json.dumps(c), encoding="utf-8")
            os.replace(tmp, ruta)
        except OSError:
            pass
    return c


# ---------------- GitHub ----------------
def _get(url, t=15, limite=3000000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=t) as f:
        return f.read(limite)


def _release():
    d = json.loads(_get("https://api.github.com/repos/%s/releases/latest" % REPO).decode("utf-8", "replace"))
    tag = str(d.get("tag_name") or "")
    if not re.fullmatch(r"v?\d+(\.\d+){1,3}", tag):
        raise ValueError("la versión publicada no es válida")
    return {"tag": tag, "version": tag.lstrip("v"), "notas": str(d.get("body") or "")[:4000],
            "assets": d.get("assets") or []}


def plataforma():
    m = platform.machine().lower()
    if os.name == "nt":
        return "windows-x64"
    if sys.platform == "darwin":
        return "macos-arm64" if m in ("arm64", "aarch64") else ("macos-x64" if m in ("x86_64", "amd64") else None)
    if sys.platform.startswith("linux"):
        return "linux-arm64" if m in ("arm64", "aarch64") else ("linux-x64" if m in ("x86_64", "amd64") else None)
    return None


def _asset(rel):
    p = plataforma()
    if not p:
        return None
    ext = r"\.exe" if p.startswith("windows") else r"\.tar\.gz"
    pat = r"pcbox-portable-%s-v?\d+(\.\d+)*%s" % (re.escape(p), ext)
    for a in rel["assets"]:
        if re.fullmatch(pat, str(a.get("name") or "")):
            return a
    return None


def _git(*a, t=60):
    return subprocess.run(["git", "-C", str(_E["res"])] + list(a), capture_output=True, text=True, timeout=t)


def es_git():
    return (not _E["frozen"]) and (_E["res"] / ".git").is_dir()


def _sucio():
    try:
        return bool(_git("status", "--porcelain").stdout.strip())
    except Exception:
        return True


def revisar(fresco=False):
    """Compara la versión instalada con el último RELEASE publicado."""
    if not fresco and _cache["r"] and time.time() - _cache["t"] < CADA:
        r = dict(_cache["r"])
        r["auto"] = leer_cfg()["auto"]
        r["bloqueado"] = _bloqueado()
        return r
    r = {"version": version_local(), "repo": REPO, "ultima": None, "hay_nueva": False, "error": None, "notas": "",
         "es_git": es_git(), "congelado": _E["frozen"], "bloqueado": _bloqueado(), "auto": leer_cfg()["auto"]}
    try:
        rel = _release()
    except Exception as e:
        r["error"] = ("Todavía no hay versiones publicadas." if getattr(e, "code", None) == 404
                      else "Sin conexión con GitHub (%s)" % e.__class__.__name__)
        return r
    r["ultima"], r["notas"] = rel["version"], rel["notas"]
    nueva = _num(rel["version"]) > _num(r["version"])
    if nueva and _E["frozen"] and not _asset(rel):
        nueva = False
        r["error"] = "La versión %s no trae el paquete para este sistema." % rel["version"]
    r["hay_nueva"] = nueva
    _cache.update(t=time.time(), r=dict(r), rel=rel)
    return r


# ---------------- utilidades ----------------
def argumentos():
    a = [x for x in sys.argv[1:] if x != "--setup"]
    if "--no-browser" not in a:
        a.append("--no-browser")          # la pestaña abierta se recarga sola al volver
    return a


def _programar(fn, seg):
    t = threading.Timer(seg, fn)
    t.daemon = True
    t.start()


def _probar(cmd, t, cwd=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=t, stdin=subprocess.DEVNULL,
                           cwd=str(cwd) if cwd else None)
    except Exception as e:
        return False, e.__class__.__name__
    if p.returncode == 0:
        return True, ""
    ult = ((p.stderr or p.stdout or "").strip().splitlines() or ["código %s" % p.returncode])[-1]
    return False, ult[:150]


def _guardar_novedades(rel, desde, n_archivos=0):
    try:
        (_E["datos"] / "_novedades.json").write_text(json.dumps({
            "desde": desde, "hasta": rel["version"], "tag": rel["tag"], "notas": rel["notas"],
            "fecha": time.strftime("%Y-%m-%d %H:%M"), "archivos": n_archivos}), encoding="utf-8")
    except OSError:
        pass


def novedades():
    """Detalle de lo último que se actualizó; solo cuando ya corre esa versión nueva."""
    try:
        n = json.loads((_E["datos"] / "_novedades.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return n if _num(_E["ver_arranque"]) >= _num(n.get("hasta")) else None


def novedades_vistas():
    try:
        (_E["datos"] / "_novedades.json").unlink()
    except OSError:
        pass
    return {"ok": True}


def boot():
    return {"id": _E["boot"], "version": version_local()}


def marcar_listo():
    """El script de relanzamiento espera este archivo para saber que la versión nueva arrancó."""
    def escribir():
        try:
            (_E["datos"] / "_arranque_ok").write_text(version_local(), encoding="utf-8")
        except OSError:
            pass
    t = threading.Timer(3.0, escribir)
    t.daemon = True
    t.start()


def solo_mismo_origen(request):
    o = request.headers.get("Origin", "")
    return (not o) or urlparse(o).netloc == request.host


# ---------------- carpeta con código (zip / git) ----------------
def _revertir(det):
    res = _E["res"]
    try:
        if det.get("modo") == "git":
            if det.get("sha_antes"):
                _git("reset", "--hard", det["sha_antes"])
        else:
            resp = det.get("respaldo")
            for a in det.get("archivos") or []:
                destino = res / a["ruta"]
                if a.get("estado") == "nuevo":
                    destino.unlink()
                elif resp and (Path(resp) / a["ruta"]).exists():
                    shutil.copy2(Path(resp) / a["ruta"], destino)
    except Exception as e:
        _E["log"]("No se pudo revertir del todo (%s)" % e)


def _compilar(det):
    for a in det.get("archivos") or []:
        if a["ruta"].endswith(".py") and a.get("estado") != "eliminado":
            p = _E["res"] / a["ruta"]
            if p.is_file():
                compile(p.read_text(encoding="utf-8"), str(p), "exec")


def _reiniciar_fuente():
    args = [sys.executable, str(Path(sys.argv[0]).resolve())] + argumentos()
    try:
        if os.name == "nt":
            _lanzar_windows(args)
            os._exit(0)
        os.execv(sys.executable, args)          # mismo PID: también sirve bajo systemd
    except Exception as e:
        _E["log"]("No se pudo reiniciar solo (%s). Ciérralo y ábrelo de nuevo." % e)


def _aplicar_fuente(rel):
    res = _E["res"]
    script = res / "actualizar.py"
    if not script.is_file():
        return {"ok": False, "error": "Falta actualizar.py en esta carpeta"}
    if es_git() and _sucio():
        return {"ok": False, "error": "Esta carpeta tiene cambios sin guardar (git); no se actualiza para no perderlos."}
    try:
        p = subprocess.run([sys.executable, str(script), "--detalle"], capture_output=True, text=True,
                           timeout=300, cwd=str(res))
        det = json.loads((p.stdout or "").strip().splitlines()[-1])
    except Exception as e:
        return {"ok": False, "error": "No se pudo ejecutar el actualizador (%s)" % e.__class__.__name__}
    if not det.get("ok"):
        return det
    if not det.get("archivos"):
        det["salida"] = "Ya tienes lo último publicado."
        return det
    try:
        _compilar(det)
        ok, motivo = _probar([sys.executable, str(res / "portable_app.py"), "--version"], 90, cwd=res)
    except Exception as e:
        ok, motivo = False, "%s: %s" % (e.__class__.__name__, e)
    if not ok:
        _revertir(det)
        return {"ok": False, "error": "La versión nueva no arrancó (%s). Se dejó la anterior." % motivo}
    _guardar_novedades(rel, det.get("version_antes") or "", len(det["archivos"]))
    det["reiniciar"] = True
    det["salida"] = "Actualizado a la versión %s (%d archivo(s)). El dashboard se reinicia en unos segundos." % (
        rel["version"], len(det["archivos"]))
    _programar(_reiniciar_fuente, 2.5)
    return det


# ---------------- ejecutable ----------------
def _descargar(asset, destino):
    url = str(asset.get("browser_download_url") or "")
    if not url.startswith("https://github.com/%s/releases/download/" % REPO):
        raise ValueError("dirección de descarga no válida")
    esperado = int(asset.get("size") or 0)
    dig = str(asset.get("digest") or "")
    h, total = hashlib.sha256(), 0
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as f, \
            open(destino, "wb") as out:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            total += len(b)
            if total > 400 * 1024 * 1024:
                raise ValueError("archivo demasiado grande")
            h.update(b)
            out.write(b)
    if esperado and total != esperado:
        raise ValueError("descarga incompleta (%d de %d bytes)" % (total, esperado))
    if dig.startswith("sha256:") and dig[7:].lower() != h.hexdigest():
        raise ValueError("la suma de verificación no coincide")


def _lanzar_windows(cmd, viejo=None, imagen=None):
    ok = _E["datos"] / "_arranque_ok"
    q = lambda s: '"%s"' % s
    L = ["@echo off", "chcp 65001 >nul", "ping -n 4 127.0.0.1 >nul", "cd /d " + q(_E["exe_dir"]),
         "del " + q(ok) + " 2>nul", 'start "" ' + " ".join(q(c) for c in cmd)]
    if viejo and imagen:
        L += ["set n=0", ":espera", "if exist " + q(ok) + " goto fin", "set /a n+=1", "if %n% GEQ 45 goto volver",
              "ping -n 2 127.0.0.1 >nul", "goto espera", ":volver", "taskkill /f /im " + q(imagen) + " >nul 2>&1",
              'start "" ' + " ".join(q(c) for c in viejo), ":fin"]
    L.append('(goto) 2>nul & del "%~f0"')
    bat = _E["exe_dir"] / "_abrir_nueva_version.bat"
    bat.write_text("\r\n".join(L) + "\r\n", encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], cwd=str(_E["exe_dir"]), close_fds=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=0x00000008 | 0x00000200 | 0x08000000)


def _bin_windows(rel, asset):
    exe_dir, actual = _E["exe_dir"], Path(sys.executable).resolve()
    dest, part = exe_dir / asset["name"], exe_dir / (asset["name"] + ".part")
    try:
        _descargar(asset, part)
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("el archivo descargado no es un programa de Windows")
        os.replace(part, dest)
    except Exception as e:
        try:
            part.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "No se pudo descargar la actualización (%s)" % str(e)[:120]}
    ok, motivo = _probar([str(dest), "--version"], 120)
    if not ok:
        try:
            dest.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "La versión nueva no arrancó en este equipo (%s). Se dejó la actual." % motivo}
    for viejo in exe_dir.glob("pcbox-portable-windows-x64*.exe*"):       # quedan solo la actual y la nueva
        try:
            if viejo.resolve() not in (actual, dest.resolve()):
                viejo.unlink()
        except OSError:
            pass
    _guardar_novedades(rel, version_local())
    _lanzar_windows([str(dest)] + argumentos(), viejo=[str(actual)] + argumentos(), imagen=dest.name)
    _programar(lambda: os._exit(0), 2.0)
    return {"ok": True, "reiniciar": True,
            "salida": "Versión %s descargada y verificada. El dashboard se reinicia solo en unos segundos; "
                      "el .exe anterior queda como respaldo en la misma carpeta." % rel["version"]}


def _bin_posix(rel, asset):
    exe = Path(sys.executable).resolve()
    nuevo, ant, tgz = (exe.with_name(exe.name + s) for s in (".nuevo", ".anterior", ".descarga"))
    try:
        _descargar(asset, tgz)
        with tarfile.open(tgz, "r:gz") as t:
            m = next((x for x in t.getmembers() if x.isfile() and re.fullmatch(r"(\./)?pcbox-portable-[\w.\-]+", x.name)), None)
            if m is None:
                raise ValueError("el paquete no trae el programa")
            with t.extractfile(m) as src, open(nuevo, "wb") as dst:
                shutil.copyfileobj(src, dst)
        os.chmod(nuevo, 0o755)
        with open(nuevo, "rb") as f:
            if f.read(4) not in (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf"):
                raise ValueError("el archivo descargado no es un programa válido")
    except PermissionError:
        return {"ok": False, "error": "No hay permiso para escribir en %s" % exe.parent}
    except Exception as e:
        for p in (nuevo, tgz):
            try:
                p.unlink()
            except OSError:
                pass
        return {"ok": False, "error": "No se pudo descargar la actualización (%s)" % str(e)[:120]}
    try:
        tgz.unlink()
    except OSError:
        pass
    ok, motivo = _probar([str(nuevo), "--version"], 90)
    if not ok:
        try:
            nuevo.unlink()
        except OSError:
            pass
        return {"ok": False, "error": "La versión nueva no arrancó en este equipo (%s). Se dejó la actual." % motivo}
    try:
        shutil.copy2(exe, ant)
        os.replace(nuevo, exe)
    except OSError as e:
        return {"ok": False, "error": "No se pudo reemplazar el programa (%s)" % e}
    _guardar_novedades(rel, version_local())
    args = argumentos()
    if os.environ.get("INVOCATION_ID"):                       # servicio systemd: reinicia el propio servicio
        _programar(lambda: os.execv(str(exe), [str(exe)] + args), 2.0)
    else:
        q = shlex.quote
        ok_f, cmd = q(str(_E["datos"] / "_arranque_ok")), " ".join(q(a) for a in [str(exe)] + args)
        sh = ("sleep 3\nrm -f %(ok)s\n%(cmd)s >/dev/null 2>&1 &\nPID=$!\ni=0\n"
              "while [ $i -lt 45 ]; do\n  [ -f %(ok)s ] && exit 0\n  kill -0 $PID 2>/dev/null || break\n  sleep 1; i=$((i+1))\ndone\n"
              "kill $PID 2>/dev/null\nsleep 1\nmv -f %(ant)s %(exe)s\n%(cmd)s >/dev/null 2>&1 &\n"
              % {"ok": ok_f, "cmd": cmd, "ant": q(str(ant)), "exe": q(str(exe))})
        subprocess.Popen(["sh", "-c", sh], start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(exe.parent))
        _programar(lambda: os._exit(0), 1.5)
    return {"ok": True, "reiniciar": True,
            "salida": "Versión %s descargada y verificada. El dashboard se reinicia solo en unos segundos; "
                      "la versión anterior queda como %s." % (rel["version"], ant.name)}


# ---------------- API ----------------
def aplicar():
    if _bloqueado():
        return {"ok": False, "error": "Las actualizaciones están desactivadas en esta carpeta (archivo .no-actualizar)."}
    if not _ocupado.acquire(blocking=False):
        return {"ok": False, "error": "Ya hay una actualización en curso"}
    try:
        r = revisar(fresco=True)
        if not r["ultima"]:
            return {"ok": False, "error": r["error"] or "No se pudo consultar GitHub"}
        if not r["hay_nueva"]:
            return {"ok": True, "salida": r["error"] or "Ya tienes lo último publicado."}
        rel = _cache["rel"]
        guardar_cfg(intento={"tag": rel["tag"], "t": time.time()})
        if _E["frozen"]:
            a = _asset(rel)
            res = _bin_windows(rel, a) if os.name == "nt" else _bin_posix(rel, a)
        else:
            res = _aplicar_fuente(rel)
        _cache["r"] = None
        return res
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (e.__class__.__name__, str(e)[:150])}
    finally:
        _ocupado.release()


def arrancar_vigilante():
    """Revisa cada 6 h y, con 'Actualizar automáticamente' activo, aplica la versión nueva."""
    def bucle():
        time.sleep(ESPERA_INICIAL)
        while True:
            try:
                if leer_cfg()["auto"] and not _bloqueado():
                    r = revisar(fresco=True)
                    if r["hay_nueva"]:
                        c = leer_cfg().get("intento") or {}
                        tag = (_cache["rel"] or {}).get("tag")
                        if not (c.get("tag") == tag and time.time() - c.get("t", 0) < REINTENTO):
                            res = aplicar()
                            _E["log"]("Actualización automática: %s" % (res.get("salida") or res.get("error")))
            except Exception as e:
                _E["log"]("Actualización automática: %s" % e)
            time.sleep(CADA)
    threading.Thread(target=bucle, daemon=True).start()
