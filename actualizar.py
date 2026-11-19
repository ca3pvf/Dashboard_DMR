#!/usr/bin/env python3
"""Actualiza el programa desde GitHub, siguiendo los RELEASES (solo biblioteca estándar). Nunca toca pcbox-data
ni el entorno .venv; si no hay internet o falla, el dashboard arranca igual con la versión que ya tiene.
Modos: sin argumentos (arranque), --estado (¿hay versión nueva?) y --detalle (actualiza y devuelve JSON)."""
import io, json, os, re, shutil, subprocess, sys, time, urllib.request, zipfile

REPO = "ca3pvf/Dashboard_DMR"
RAMA = "main"
AQUI = os.path.dirname(os.path.abspath(__file__))
SALTAR = ("pcbox-data/", ".venv", ".git/", "iniciar.bat", "iniciar.command", ".version", ".no-actualizar",
          "dist/", "build/", "BAK/")


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


def release():
    d = json.loads(pedir("https://api.github.com/repos/%s/releases/latest" % REPO, 15))
    tag = str(d.get("tag_name") or "")
    if not re.fullmatch(r"v?\d+(\.\d+){1,3}", tag):
        raise ValueError("la versión publicada no es válida")
    return {"tag": tag, "version": tag.lstrip("v"), "texto": (d.get("body") or "")[:3000]}


def estado():
    r = {"local": version_local(), "remota": "", "hay": False, "es_git": es_git(), "error": ""}
    try:
        r["remota"] = release()["version"]
        r["hay"] = tupla(r["remota"]) > tupla(r["local"])
    except Exception as e:
        r["error"] = "Sin conexión con GitHub (%s)" % e.__class__.__name__
    return r


def instalar_requisitos(det):
    req = os.path.join(AQUI, "requirements.txt")
    if os.path.exists(req):
        p = subprocess.run([sys.executable, "-m", "pip", "install", "-r", req], capture_output=True, text=True, timeout=600)
        det["requisitos"] = "Dependencias instaladas" if p.returncode == 0 else "Error en dependencias: " + (p.stderr or p.stdout)[-300:]


def git(*a, t=120):
    return subprocess.run(["git", "-C", AQUI] + list(a), capture_output=True, text=True, timeout=t)


def aplicar_git(det, rel):
    antes = git("rev-parse", "HEAD").stdout.strip()
    f = git("fetch", "--tags", "--quiet")
    if f.returncode:
        raise RuntimeError("git fetch: " + (f.stderr or f.stdout).strip()[:300])
    p = git("merge", "--ff-only", rel["tag"])
    if p.returncode:
        raise RuntimeError("git merge %s: %s" % (rel["tag"], (p.stderr or p.stdout).strip()[:300]))
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


def aplicar_zip(det, rel):
    datos_zip = pedir("https://codeload.github.com/%s/zip/refs/tags/%s" % (REPO, rel["tag"]), 120)
    det["sha_antes"], det["sha_despues"] = version_local(), rel["version"]
    z = zipfile.ZipFile(io.BytesIO(datos_zip))
    nombres = [n for n in z.namelist() if not n.endswith("/")]
    raiz = nombres[0].split("/", 1)[0] + "/"
    cambios = []
    for n in nombres:
        rel_ruta = n[len(raiz):]
        if not rel_ruta or any(rel_ruta == s or rel_ruta.startswith(s) for s in SALTAR):
            continue
        destino = os.path.normpath(os.path.join(AQUI, rel_ruta))
        if not destino.startswith(AQUI + os.sep):
            continue
        datos = z.read(n)
        if os.path.exists(destino):
            if open(destino, "rb").read() == datos:
                continue
            est = "modificado"
        else:
            est = "nuevo"
        cambios.append((destino, rel_ruta, datos, est))
    respaldo = os.path.join(AQUI, "BAK", "actualizacion-" + time.strftime("%Y%m%d-%H%M%S"))
    hechos = []
    try:
        for destino, rel_ruta, datos, est in cambios:
            if est == "modificado":
                b = os.path.join(respaldo, rel_ruta)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(destino, b)
            hechos.append((destino, rel_ruta, est))
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino, "wb") as f:
                f.write(datos)
    except Exception:
        for destino, rel_ruta, est in hechos:                # revertir todo lo escrito
            b = os.path.join(respaldo, rel_ruta)
            if est == "nuevo" and os.path.exists(destino):
                os.remove(destino)
            elif os.path.exists(b):
                shutil.copy2(b, destino)
        raise
    det["archivos"] = [{"ruta": r, "estado": e} for _, r, e in hechos]
    if any(e == "modificado" for _, _, e in hechos):
        det["respaldo"] = respaldo
    if any(r == "requirements.txt" for _, r, _ in hechos):
        instalar_requisitos(det)


def aplicar():
    det = {"ok": False, "fecha": time.strftime("%Y-%m-%d %H:%M:%S"), "modo": "git" if es_git() else "zip",
           "version_antes": version_local(), "version_despues": "", "sha_antes": "", "sha_despues": "",
           "archivos": [], "commits": [], "notas": {}, "respaldo": "", "requisitos": "", "error": "", "reinicio": False}
    try:
        rel = release()
        det["notas"] = {"tag": rel["tag"], "texto": rel["texto"]}
        if tupla(rel["version"]) > tupla(version_local()):
            (aplicar_git if es_git() else aplicar_zip)(det, rel)
        det["version_despues"] = version_local()
        det["reinicio"] = bool(det["archivos"])
        det["ok"] = True
    except Exception as e:
        det["error"] = "%s: %s" % (e.__class__.__name__, e)
    return det


def main():
    if es_git() or REPO.startswith("USUARIO/"):
        return 0                                    # copia de desarrollo (git): se actualiza desde el dashboard
    if os.path.exists(os.path.join(AQUI, ".no-actualizar")):
        return 0
    try:
        if json.load(open(os.path.join(AQUI, "pcbox-data", "update.json"))).get("auto") is False:
            return 0                                # el usuario desactivó la actualización automática
    except Exception:
        pass
    e = estado()
    if not e["hay"]:
        return 0
    print("Buscando actualizaciones…")
    det = aplicar()
    if det["ok"]:
        print("Actualizado a %s (%d archivos)." % (det["version_despues"], len(det["archivos"])))
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
