"""Sesion SSH persistente para el dashboard portable.

El OpenSSH de Windows no soporta ControlMaster: cada consulta de los servicios abre un login nuevo
(1 a 3 s y carga en el hotspot). Este modulo intercepta subprocess.run(["ssh", ..., destino, comando])
y ejecuta el comando dentro de UNA sesion ssh que queda abierta. Si algo no se puede interceptar
(Popen, reenvios de puertos, stdin, -t, etc.) se ejecuta el ssh normal, como siempre.

  Windows: activo por defecto.      Desactivar: variable de entorno PCBOX_SSH_BROKER=0
  Linux/macOS: apagado (ya usan ControlMaster).  Activar: PCBOX_SSH_BROKER=1
Estadisticas: stats() (el portable las expone en /api/portable/perf).
"""
import base64, collections, os, queue, re, shlex, shutil, subprocess, threading, time, uuid

_run_real = subprocess.run
_Popen_real = subprocess.Popen
_NT = os.name == "nt"
_FLAGS = 0x08000000 if _NT else 0          # CREATE_NO_WINDOW

_CON_ARG = {"-o", "-p", "-i", "-l", "-F", "-J", "-b", "-c", "-e", "-m", "-E", "-B", "-I", "-Q"}
_NO_SOPORTADAS = {"-L", "-R", "-D", "-W", "-O", "-S", "-w", "-N", "-t", "-tt", "-f", "-s", "-M", "-X", "-Y", "-A"}
_OPT_DESCARTAR = ("controlmaster", "controlpath", "controlpersist", "batchmode", "serveraliveinterval",
                  "serveralivecountmax", "connecttimeout")
_RE_DESTINO = re.compile(r"^[\w.\-]+@[\w.\-:]+$")
_MAX_SESIONES = 2
_lock = threading.Lock()
_sesiones = {}                              # clave -> [_Sesion]
_bloqueado = {}                             # clave -> instante hasta el que no se reintenta
_st = collections.Counter()
_ms_total = 0.0
_top = {}                                   # comando(60) -> [veces, ms_total]
_activo = False


class _NoAbre(Exception):
    pass


class _Timeout(Exception):
    pass


class _Perdida(Exception):
    pass


class _Sesion:
    def __init__(self, destino, opts):
        self.destino, self.opts = destino, opts
        self.lock = threading.Lock()
        self.proc, self.q = None, None

    def viva(self):
        return self.proc is not None and self.proc.poll() is None

    def cerrar(self):
        p, self.proc = self.proc, None
        if p:
            try:
                p.kill()
            except Exception:
                pass

    def _lector(self, proc, q):
        try:
            for linea in iter(proc.stdout.readline, b""):
                q.put(linea)
        except Exception:
            pass
        q.put(None)

    def abrir(self):
        self.cerrar()
        cmd = _cmd_ssh(self.destino, self.opts)
        self.proc = _Popen_real(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=_FLAGS)
        self.q = queue.Queue()
        threading.Thread(target=self._lector, args=(self.proc, self.q), daemon=True).start()
        # sonda: la Pi debe tener base64; si no, esta sesion no sirve
        self._enviar(b"echo @@P; printf x | base64 -w0; echo; echo @@PE\n")
        lineas = self._leer_hasta(b"@@PE", time.time() + 20)
        if lineas is None or b"eA==" not in b"".join(lineas):
            self.cerrar()
            raise _NoAbre("sesion no disponible")

    def _enviar(self, datos):
        self.proc.stdin.write(datos)
        self.proc.stdin.flush()

    def _leer_hasta(self, marca, limite):
        out = []
        while True:
            rest = limite - time.time()
            if rest <= 0:
                return None
            try:
                l = self.q.get(timeout=rest)
            except queue.Empty:
                return None
            if l is None:
                return None
            if l.strip().startswith(marca):
                return out
            out.append(l)

    def ejecutar(self, remoto, timeout):
        """-> (rc, stdout_bytes, stderr_bytes). Lanza _NoAbre si no pudo ni enviar; _Timeout / _Perdida despues."""
        if not self.viva():
            self.abrir()
        while True:                         # descarta restos de una consulta anterior
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        i = uuid.uuid4().hex[:8]
        f = "/tmp/.pcb_$$"
        script = ('%s -c %s >%s.o 2>%s.e </dev/null; r=$?; echo @@S%s $r; base64 -w0 %s.o; echo; '
                  'base64 -w0 %s.e; echo; rm -f %s.o %s.e; echo @@E%s\n'
                  % ('"${SHELL:-sh}"', shlex.quote(remoto), f, f, i, f, f, f, f, i))
        try:
            self._enviar(script.encode("utf-8"))
        except Exception as e:
            self.cerrar()
            raise _NoAbre(str(e))
        lineas = self._leer_hasta(("@@E" + i).encode(), time.time() + (timeout or 60))
        if lineas is None:
            vivo = self.viva()
            self.cerrar()
            if vivo:
                raise _Timeout()
            raise _Perdida()
        try:
            k = next(n for n, l in enumerate(lineas) if l.startswith(("@@S" + i).encode()))
            rc = int(lineas[k].split()[1])
            out = base64.b64decode(lineas[k + 1].strip() or b"")
            err = base64.b64decode(lineas[k + 2].strip() or b"")
        except Exception:
            self.cerrar()
            raise _Perdida()
        return rc, out, err


def _cmd_ssh(destino, opts):
    ssh = shutil.which("ssh") or "ssh"
    base = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
            "-o", "ConnectTimeout=8"]
    if not any(str(o).lower().startswith("stricthostkeychecking") for o in opts):
        base += ["-o", "StrictHostKeyChecking=accept-new"]
    return [ssh] + base + list(opts) + [destino, "sh"]


def _analizar(args):
    """args de ssh -> (destino, opts, comando_remoto) o None si no se puede interceptar."""
    a = list(args)
    if not a or os.path.basename(str(a[0])).lower() not in ("ssh", "ssh.exe"):
        return None
    opts, destino, i = [], None, 1
    while i < len(a):
        x = str(a[i])
        if destino is None:
            if x in _NO_SOPORTADAS:
                return None
            if x in _CON_ARG:
                if i + 1 >= len(a):
                    return None
                v = str(a[i + 1])
                if x == "-o" and v.split("=", 1)[0].strip().lower() in _OPT_DESCARTAR:
                    pass
                else:
                    opts += [x, v]
                i += 2
                continue
            if x.startswith("-"):
                if len(x) > 2 and x[:2] in _CON_ARG:            # -p2222, -ofoo=bar
                    opts += [x[:2], x[2:]]
                elif x in ("-q", "-T", "-n", "-4", "-6", "-v", "-C", "-a", "-x"):
                    pass
                else:
                    return None
                i += 1
                continue
            if not _RE_DESTINO.match(x):
                return None
            destino = x
            i += 1
            continue
        break
    if destino is None or i >= len(a):
        return None
    return destino, tuple(opts), " ".join(str(z) for z in a[i:])


def _sesion(clave, destino, opts):
    with _lock:
        lista = _sesiones.setdefault(clave, [])
        for s in lista:
            if s.lock.acquire(blocking=False):
                return s
        if len(lista) < _MAX_SESIONES:
            s = _Sesion(destino, opts)
            s.lock.acquire()
            lista.append(s)
            return s
        s = lista[0]
    s.lock.acquire()
    return s


def _run(*popenargs, **kw):
    global _ms_total
    args = popenargs[0] if popenargs else kw.get("args")
    plan = None
    try:
        plan = _analizar(args) if not isinstance(args, str) and not kw.get("shell") else None
    except Exception:
        plan = None
    if plan is None:
        if not isinstance(args, str) and args and os.path.basename(str(args[0])).lower() in ("ssh", "ssh.exe"):
            _st["ssh_directos"] += 1
        return _run_real(*popenargs, **kw)

    destino, opts, remoto = plan
    stdin, stdout, stderr = kw.get("stdin"), kw.get("stdout"), kw.get("stderr")
    cap = kw.get("capture_output")
    if kw.get("input") is not None or stdin not in (None, subprocess.DEVNULL):
        _st["ssh_directos"] += 1
        return _run_real(*popenargs, **kw)
    if cap:
        stdout, stderr = subprocess.PIPE, subprocess.PIPE
    if stdout not in (None, subprocess.PIPE, subprocess.DEVNULL) or stderr not in (None, subprocess.PIPE, subprocess.DEVNULL, subprocess.STDOUT):
        _st["ssh_directos"] += 1
        return _run_real(*popenargs, **kw)

    clave = (destino, opts)
    if _bloqueado.get(clave, 0) > time.time():
        _st["ssh_directos"] += 1
        return _run_real(*popenargs, **kw)
    timeout = kw.get("timeout")
    t0 = time.time()
    s = _sesion(clave, destino, opts)
    try:
        try:
            rc, out, err = s.ejecutar(remoto, timeout)
        except (_NoAbre, OSError):                              # no se pudo abrir/enviar: ssh normal
            _bloqueado[clave] = time.time() + 30
            _st["sesion_fallida"] += 1
            s.cerrar()
            s.lock.release()
            s = None
            return _run_real(*popenargs, **kw)
        except _Timeout:
            _st["timeouts"] += 1
            raise subprocess.TimeoutExpired(args, timeout)
        except _Perdida:
            _st["conexion_perdida"] += 1
            rc, out, err = 255, b"", b"conexion SSH persistente perdida\n"
    finally:
        if s is not None:
            s.lock.release()
    ms = (time.time() - t0) * 1000
    with _lock:
        _st["llamadas"] += 1
        _ms_total += ms
        t = _top.setdefault(remoto[:60], [0, 0.0])
        t[0] += 1
        t[1] += ms
    if stderr == subprocess.STDOUT:
        out, err = out + err, b""
    texto = kw.get("text") or kw.get("universal_newlines") or kw.get("encoding") or kw.get("errors")
    if texto:
        enc = kw.get("encoding") or "utf-8"
        out = out.decode(enc, "replace").replace("\r\n", "\n")
        err = err.decode(enc, "replace").replace("\r\n", "\n")
    o = out if stdout == subprocess.PIPE else None
    e = err if stderr == subprocess.PIPE else None
    cp = subprocess.CompletedProcess(args, rc, o, e)
    if kw.get("check") and rc:
        raise subprocess.CalledProcessError(rc, args, output=o, stderr=e)
    return cp


class _PopenContado(_Popen_real):
    def __init__(self, args, *a, **k):
        try:
            if not isinstance(args, str) and os.path.basename(str(args[0])).lower() in ("ssh", "ssh.exe"):
                _st["ssh_popen"] += 1
        except Exception:
            pass
        super().__init__(args, *a, **k)


def instalar(forzar=None):
    """Activa la interceptacion. Devuelve True si quedo activa."""
    global _activo
    env = os.environ.get("PCBOX_SSH_BROKER")
    if forzar is not None:
        quiere = forzar
    elif env is not None:
        quiere = env != "0"
    else:
        quiere = _NT
    if not quiere or _activo or not shutil.which("ssh"):
        return _activo
    subprocess.run = _run
    subprocess.Popen = _PopenContado
    _activo = True
    return True


def desinstalar():
    global _activo
    subprocess.run = _run_real
    subprocess.Popen = _Popen_real
    _activo = False
    with _lock:
        for lista in _sesiones.values():
            for s in lista:
                s.cerrar()
        _sesiones.clear()


def stats():
    with _lock:
        n = _st["llamadas"]
        top = sorted(_top.items(), key=lambda kv: -kv[1][0])[:8]
        return {"activo": _activo, "sesiones": sum(len(v) for v in _sesiones.values()),
                "consultas_en_sesion_persistente": n, "ms_medio": round(_ms_total / n, 1) if n else None,
                "ssh_normales_restantes": _st["ssh_directos"], "ssh_popen": _st["ssh_popen"],
                "timeouts": _st["timeouts"], "conexion_perdida": _st["conexion_perdida"],
                "sesion_fallida": _st["sesion_fallida"],
                "top": [{"comando": k, "veces": v[0], "ms_medio": round(v[1] / v[0], 1)} for k, v in top]}
