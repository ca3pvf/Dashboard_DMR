#!/usr/bin/env python3
"""Actualiza el programa desde GitHub (solo biblioteca estándar). Nunca toca pcbox-data ni el entorno .venv,
y si no hay internet o falla, no pasa nada: el dashboard arranca igual con la versión que ya tiene.
Antes de usarlo, cambia REPO por 'tu-usuario/tu-repositorio'."""
import hashlib, io, json, os, sys, urllib.request, zipfile

REPO = "USUARIO/pcbox-dashboard"
RAMA = "main"
AQUI = os.path.dirname(os.path.abspath(__file__))
SALTAR = ("pcbox-data/", ".venv", ".git/", "iniciar.bat", "iniciar.command", ".version", "dist/", "build/")


def pedir(url, t=20):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "pcbox-actualizador"}), timeout=t).read()


def main():
    if REPO.startswith("USUARIO/"):
        return 0                                            # sin configurar: no se actualiza
    marca = os.path.join(AQUI, ".version")
    actual = open(marca).read().strip() if os.path.exists(marca) else ""
    try:
        sha = json.loads(pedir("https://api.github.com/repos/%s/commits/%s" % (REPO, RAMA)))["sha"]
    except Exception:
        sha = None                                          # API limitada u offline: se decide con el propio zip
    if sha and sha == actual:
        return 0
    print("Buscando actualizaciones…")
    try:
        datos_zip = pedir("https://codeload.github.com/%s/zip/refs/heads/%s" % (REPO, RAMA), 120)
    except Exception as e:
        print("Sin conexión con GitHub, se usa la versión actual (%s)" % e.__class__.__name__)
        return 0
    if not sha:
        sha = "zip-" + hashlib.sha256(datos_zip).hexdigest()[:40]
        if sha == actual:
            return 0
    try:
        z = zipfile.ZipFile(io.BytesIO(datos_zip))
        nombres = [n for n in z.namelist() if not n.endswith("/")]
        raiz = nombres[0].split("/", 1)[0] + "/"
        nuevos = []
        for n in nombres:
            rel = n[len(raiz):]
            if not rel or any(rel == s or rel.startswith(s) for s in SALTAR):
                continue
            destino = os.path.normpath(os.path.join(AQUI, rel))
            if not destino.startswith(AQUI + os.sep):
                continue                                    # protección contra rutas raras dentro del zip
            nuevos.append((destino, z.read(n)))
        for destino, datos in nuevos:                       # recién aquí se escribe: o todo o nada
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            with open(destino, "wb") as f:
                f.write(datos)
        open(marca, "w").write(sha)
        print("Actualizado (%d archivos)." % len(nuevos))
    except Exception as e:
        print("No se pudo actualizar (%s: %s); se usa la versión actual." % (e.__class__.__name__, e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
