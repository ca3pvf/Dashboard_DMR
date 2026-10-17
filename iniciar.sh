#!/usr/bin/env bash
# Lanzador del dashboard portable (Linux y macOS)
MAIN="portable_app.py"
REQ="requirements-portable.txt"
cd "$(dirname "$0")" || exit 1

if [ ! -f "$MAIN" ]; then echo "No encuentro $MAIN en $(pwd)."; exit 1; fi

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Falta Python 3.8 o superior."
  [ "$(uname)" = "Darwin" ] && echo "Si macOS abrió una ventana para instalar herramientas de desarrollo, acéptala y, al terminar, vuelve a abrir este archivo. Si no apareció, instala Python desde https://www.python.org/downloads/ o con: brew install python" \
                            || echo "Instálalo con: sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Creando entorno local (.venv)..."
  "$PY" -m venv .venv || { echo "No se pudo crear el entorno. En Debian/Ubuntu/Raspberry: sudo apt install python3-venv"; exit 1; }
fi

SELLO=".venv/.requisitos-$(cksum < "$REQ" | cut -d' ' -f1)"
if [ ! -f "$SELLO" ]; then
  echo "Instalando dependencias (solo la primera vez)..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r "$REQ" || { echo "Falló la instalación. Revisa tu conexión a internet."; exit 1; }
  rm -f .venv/.requisitos-*; touch "$SELLO"
fi

echo "Iniciando el dashboard... (Ctrl+C para detenerlo)"
exec .venv/bin/python "$MAIN"
