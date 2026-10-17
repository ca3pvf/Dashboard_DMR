#!/usr/bin/env bash
# Genera el ejecutable para ESTE sistema (macOS o Linux). Resultado: dist/pcbox-portable
set -e
cd "$(dirname "$0")"
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --quiet -r requirements-portable.txt pyinstaller
pyinstaller --onefile --clean --noconfirm --name pcbox-portable \
  --add-data "services:services" --add-data "dashboard.html:." --add-data "wizard.html:." \
  --collect-submodules socketio --collect-submodules engineio --collect-all sgp4 \
  --exclude-module tkinter --exclude-module matplotlib --exclude-module numpy \
  portable_app.py
echo
echo "Listo: $(pwd)/dist/pcbox-portable"
ls -lh dist/pcbox-portable
