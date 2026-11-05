#!/usr/bin/env python3
"""Actualiza el programa desde GitHub (solo biblioteca estándar). Nunca toca pcbox-data ni el entorno .venv,
y si no hay internet o falla, no pasa nada: el dashboard arranca igual con la versión que ya tiene.
Modos: sin argumentos (arranque), --estado (¿hay versión nueva?) y --detalle (actualiza y devuelve JSON)."""
import hashlib, io, json, os, re, shutil, subprocess, sys, time, urllib.request, zipfile

REPO = "ca3pvf/Dashboard_DMR"
RAMA = "main"
AQUI = os.path.dirname(os.path.abspath(__file__))
SALTAR = ("pcbox-data/", ".venv", ".git/", "iniciar.bat", "iniciar.command", ".version", "dist/", "build/", "BAK/")


def pedir(url, t=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pcbox-actualizador"}), timeout=t).read()


def leer(ruta):
    try:
        return open(ruta, encoding="utf-8").read().strip()
    except Exception:
        return ""


def version_local():
    return leer(os.path.join(AQUI, "VERSION"))


def tupla(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def es_git():
    return os.path.isdir(os.path.join(AQUI, ".git"))


def estado():
    r = {"local": version_local(), "remota": "", "hay": False, "es_git": es_git(), "error": ""}
    try:
        r["remota"] = pedir("https://raw.githubusercontent.com/%s/%s/VERSION" % (REPO, RAMA), 10).decode().strip()
        r["hay"] = tupla(r["remota"]) > tupla(r["local"])
    except Exception as e:
        r["error"] = "Sin conexión con GitHub (%s)" % e.__class__.__name__
    return r


def notas():
    try:
        d = json.loads(pedir("https://api.github.com/repos/%s/releases/latest" % REPO, 10))
        return {"tag": d.get("tag_name", ""), "texto": (d.get("body") or "")[:3000]}
    except Exception:
        return {"tag": "", "texto": ""}


def instalar_requisitos(det):
    req = os.path.join(AQUI, "requirements.txt")
    if os.path.exists(req):
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req], capture_output=True, text=True, timeout=600)
        det["requisitos"] = "Dependencias instaladas" if p.returncode == 0 else "Error en dependencias: " + (p.stderr or p.stdout)[-300:]


def git(*a, t=120):
    return subprocess.run(["git", "-C", AQUI] + list(a), capture_output=True, text=True, timeout=t)


def aplicar_git(det):
    antes = git("rev-parse", "HEAD").stdout.strip()
    f = git("fetch", "--quiet")
    if f.returncode:
        raise RuntimeError("git fetch: " + (f.stderr or f.stdout).strip()[:300])
    p = git("pull", "--ff-only")
    if p.returncode:
        raise RuntimeError("git pull: " + (p.stderr or p.stdout).strip()[:300])
    despues = git("rev-parse", "HEAD").stdout.strip()
    det["sha_antes"], det["sha_despues"] = antes[:7], despues[:7]
    if antes != despues:
        mapa = {"A": "nuevo", "M": "modificado", "D": "eliminado"}
        for linea in git("diff", "--name-status", antes, despues).stdout.splitlines():
            partes = linea.split("\t")
            det["archivos"].append({"ruta": partes[-1], "estado": mapa.get(partes[0][:1], "modificado")})
        det["commits"] = git("log", "--oneline", antes + ".." + despues).stdout.strip().splitlines()[:30]
        if any(a["ruta"] == "requirements.txt" for a in det["archivos"]):
            instalar_requisitos(det)


def aplicar_zip(det):
    marca = os.path.join(AQUI, ".version")
    actual = leer(marca)
    try:
        sha = json.loads(pedir("https://api.github.com/repos/%s/commits/%s" % (REPO, RAMA)))["sha"]
    except Exception:
        sha = None
    datos_zip = pedir("https://codeload.github.com/%s/zip/refs/heads/%s" % (REPO, RAMA), 120)
    if not sha:
        sha = "zip-" + hashlib.sha256(datos_zip).hexdigest()[:40]
    det["sha_antes"], det["sha_despues"] = actual[:7], sha[:7]
    if sha == actual:
        return
    z = zipfile.ZipFile(io.BytesIO(datos_zip))
    nombres = [n for n in z.namelist() if not n.endswith("/")]
    raiz = nombres[0].split("/", 1)[0] + "/"
    cambios = []
    for n in nombres:
        rel = n[len(raiz):]
        if not rel or any(rel == s or rel.startswith(s) for s in SALTAR):
            continue
        destino = os.path.normpath(os.path.join(AQUI, rel))
        if not destino.startswith(AQUI + os.sep):
            continue
        datos = z.read(n)
        if os.path.exists(destino):
            if open(destino, "rb").read() == datos:
                continue
            est = "modificado"
        else:
            est = "nuevo"
        cambios.append((destino, rel, datos, est))
    respaldo = os.path.join(AQUI, "BAK", "actualizacion-" + time.strftime("%Y%m%d-%H%M%S"))
    hechos = []
    try:
        for destino, rel, datos, est in cambios:
            if est == "modificado":
                b = os.path.join(respaldo, rel)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(destino, b)
            hechos.append((destino, rel, est))
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino, "wb") as f:
                f.write(datos)
    except Exception:
        for destino, rel, est in hechos:                    # revertir todo lo escrito
            b = os.path.join(respaldo, rel)
            if est == "nuevo" and os.path.exists(destino):
                os.remove(destino)
            elif os.path.exists(b):
                shutil.copy2(b, destino)
        raise
    open(marca, "w").write(sha)
    det["archivos"] = [{"ruta": rel, "estado": est} for _, rel, est in hechos]
    if any(e == "modificado" for _, _, e in hechos):
        det["respaldo"] = respaldo
    if any(rel == "requirements.txt" for _, rel, _ in hechos):
        instalar_requisitos(det)


def aplicar():
    det = {"ok": False, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "modo": "git" if es_git() else "zip",
           "version_antes": version_local(), "version_despues": "", "sha_antes": "", "sha_despues": "",
           "archivos": [], "commits": [], "notas": {}, "respaldo": "", "requisitos": "", "error": "", "reinicio": False}
    try:
        (aplicar_git if es_git() else aplicar_zip)(det)
        det["version_despues"] = version_local()
        det["notas"] = notas()
        det["reinicio"] = bool(det["archivos"])
        det["ok"] = True
    except Exception as e:
        det["error"] = "%s: %s" % (e.__class__.__name__, e)
    return det


def main():
    if es_git():
        return 0                                            # copia de desarrollo (git): se actualiza con git pull
    if REPO.startswith("USUARIO/"):
        return 0                                            # sin configurar: no se actualiza
    marca = os.path.join(AQUI, ".version")
    actual = leer(marca)
    try:
        sha = json.loads(pedir("https://api.github.com/repos/%s/commits/%s" % (REPO, RAMA)))["sha"]
    except Exception:
        sha = None
    if sha and sha == actual:
        return 0
    print("Buscando actualizaciones…")
    det = aplicar()
    if det["ok"]:
        print("Actualizado (%d archivos)." % len(det["archivos"]))
    else:
        print("No se pudo actualizar (%s); se usa la versión actual." % det["error"])
    return 0


if __name__ == "__main__":
    if "--estado" in sys.argv:
        print(json.dumps(estado()))
        sys.exit(0)
    if "--detalle" in sys.argv:
        print(json.dumps(aplicar()))
        sys.exit(0)
    sys.exit(main())
