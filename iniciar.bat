@echo off
setlocal
rem Lanzador del dashboard portable (Windows)
set MAIN=portable_app.py
set REQ=requirements-portable.txt
cd /d "%~dp0"

if not exist "%MAIN%" (
  echo No encuentro %MAIN% en %cd%.
  pause & exit /b 1
)

set PY=
py -3 -c "import sys" >nul 2>&1 && set PY=py -3
if "%PY%"=="" python -c "import sys" >nul 2>&1 && set PY=python
if "%PY%"=="" (
  echo Falta Python 3. Descargalo de https://www.python.org/downloads/ y marca "Add Python to PATH".
  pause & exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creando entorno local .venv ...
  %PY% -m venv .venv || ( echo No se pudo crear el entorno. & pause & exit /b 1 )
)

if not exist ".venv\.listo" (
  echo Instalando dependencias, solo la primera vez ...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r %REQ% || ( echo Fallo la instalacion. Revisa tu internet. & pause & exit /b 1 )
  echo ok> ".venv\.listo"
)

echo Iniciando el dashboard... (cierra esta ventana para detenerlo)
".venv\Scripts\python.exe" "%MAIN%"
pause
