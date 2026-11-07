#!/usr/bin/env bash
# Lanzador del dashboard portable (Linux y macOS)
MAIN="portable_app.py"
REQ="requirements-portable.txt"
cd "$(dirname "$0")" || exit 1
AQUI="$(pwd)"

if [ ! -f "$MAIN" ]; then echo "No encuentro $MAIN en $AQUI."; exit 1; fi

# Python propio del dashboard (solo si el usuario acepta): queda dentro de esta carpeta
export UV_PYTHON_INSTALL_DIR="$AQUI/.python"
export UV_UNMANAGED_INSTALL="$AQUI/.uv"
export UV_PYTHON_PREFERENCE=only-managed

python_ok() { "$1" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; }

buscar_python() {
  for c in python3 python; do
    ruta="$(command -v "$c" 2>/dev/null)" || continue
    # macOS sin herramientas de desarrollo: /usr/bin/python3 es solo un atajo que abre una ventana de instalación
    if [ "$(uname)" = "Darwin" ] && [ "$ruta" = "/usr/bin/$c" ] && ! xcode-select -p >/dev/null 2>&1; then continue; fi
    if python_ok "$ruta"; then PY="$ruta"; return 0; fi
  done
  return 1
}

buscar_uv() {
  UV=""
  for u in "$AQUI/.uv/uv" "$AQUI/.uv/bin/uv"; do [ -x "$u" ] && UV="$u" && return 0; done
  return 1
}

instalar_python() {
  [ -t 0 ] || return 1
  echo
  echo "No encontré Python en este equipo (el dashboard lo necesita)."
  echo "Puedo instalarlo yo automáticamente: son unos 60 MB, no necesita contraseña"
  echo "de administrador y no cambia nada más de tu equipo."
  read -r -p "¿Instalar Python ahora? [S/n]: " R
  case "$R" in n|N|no|NO|No) return 1 ;; esac
  if ! buscar_uv; then
    echo "Descargando el instalador..."
    if command -v curl >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh -o "$AQUI/.uv-install.sh"
    elif command -v wget >/dev/null 2>&1; then wget -qO "$AQUI/.uv-install.sh" https://astral.sh/uv/install.sh
    else echo "No hay curl ni wget para descargarlo."; return 1; fi
    sh "$AQUI/.uv-install.sh" >/dev/null 2>&1; rm -f "$AQUI/.uv-install.sh"
    buscar_uv || { echo "No se pudo descargar el instalador. Revisa tu conexión a internet."; return 1; }
  fi
  echo "Instalando Python (puede tardar un minuto)..."
  "$UV" venv --seed --python 3.12 "$AQUI/.venv" || { rm -rf "$AQUI/.venv"; return 1; }
  [ -x "$AQUI/.venv/bin/python" ]
}

sin_python() {
  echo "Para usar el dashboard instala Python 3.8 o superior."
  if [ "$(uname)" = "Darwin" ]; then
    echo "Descárgalo de https://www.python.org/downloads/ (o con: brew install python) y abre este archivo de nuevo."
    echo "Si macOS abrió una ventana para instalar herramientas de desarrollo, acéptala y vuelve a abrir este archivo."
  else
    echo "Instálalo con: sudo apt install python3 python3-venv python3-pip   y abre este archivo de nuevo."
  fi
  exit 1
}

if [ ! -x .venv/bin/python ]; then
  PY=""
  if buscar_python; then
    echo "Creando entorno local (.venv)..."
    if ! "$PY" -m venv .venv 2>/dev/null; then
      rm -rf .venv
      echo "Este Python no puede crear el entorno local (en Ubuntu/Debian falta el paquete python3-venv)."
      instalar_python || sin_python
    fi
  else
    instalar_python || sin_python
  fi
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
