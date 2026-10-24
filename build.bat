@echo off
REM Genera pcbox-portable.exe para Windows. Resultado: dist\pcbox-portable.exe
cd /d "%~dp0"
set EXTRA=
if exist VERSION set EXTRA=--add-data "VERSION;."
python -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat
pip install --quiet -r requirements-portable.txt pyinstaller || goto :error
pyinstaller --onefile --clean --noconfirm --name pcbox-portable ^
  --add-data "services;services" --add-data "dashboard.html;." --add-data "wizard.html;." %EXTRA% ^
  --collect-submodules socketio --collect-submodules engineio --collect-all sgp4 ^
  --hidden-import PIL.Image --hidden-import PIL.ImageDraw --hidden-import PIL.ImageFont ^
  --hidden-import aprslib --hidden-import websocket --hidden-import werkzeug.serving ^
  --hidden-import smtplib --hidden-import ssl --hidden-import email.message --hidden-import email.utils ^
  --hidden-import sqlite3 --hidden-import csv --hidden-import unicodedata --hidden-import html ^
  --hidden-import xml.etree.ElementTree ^
  --exclude-module tkinter --exclude-module matplotlib --exclude-module numpy ^
  portable_app.py || goto :error
echo.
echo Listo: %cd%\dist\pcbox-portable.exe
dir dist\pcbox-portable.exe
goto :eof
:error
echo Fallo la construccion.
exit /b 1
