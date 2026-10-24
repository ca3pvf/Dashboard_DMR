#!/usr/bin/env bash
# Genera el ejecutable para ESTE sistema (macOS o Linux). Resultado: dist/pcbox-portable
set -e
cd "$(dirname "$0")"
EXTRA=()
[ -f VERSION ] && EXTRA+=(--add-data "VERSION:.")
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install --quiet -r requirements-portable.txt pyinstaller
pyinstaller --onefile --clean --noconfirm --name pcbox-portable \
  --add-data "services:services" --add-data "dashboard.html:." --add-data "wizard.html:." "${EXTRA[@]}" \
  --collect-submodules socketio --collect-submodules engineio --collect-all sgp4 \
  --hidden-import PIL.Image --hidden-import PIL.ImageDraw --hidden-import PIL.ImageFont \
  --hidden-import aprslib --hidden-import websocket --hidden-import werkzeug.serving \
  --hidden-import smtplib --hidden-import ssl --hidden-import email.message --hidden-import email.utils \
  --hidden-import sqlite3 --hidden-import csv --hidden-import unicodedata --hidden-import html \
  --hidden-import xml.etree.ElementTree \
  --exclude-module tkinter --exclude-module matplotlib --exclude-module numpy \
  portable_app.py
echo
echo "Listo: $(pwd)/dist/pcbox-portable"
ls -lh dist/pcbox-portable
