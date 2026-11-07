@echo off
setlocal EnableExtensions
rem Lanzador del dashboard portable (Windows)
set MAIN=portable_app.py
set REQ=requirements-portable.txt
set PYINST=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
cd /d "%~dp0"

if not exist "%MAIN%" (
  echo No encuentro %MAIN% en %cd%.
  pause & exit /b 1
)

if exist ".venv\Scripts\python.exe" goto venv_ok

set PY=
call :buscar_python
if defined PY goto crear_venv

echo.
echo No encontre Python en este equipo (el dashboard lo necesita).
echo Puedo instalarlo yo automaticamente: son unos 25 MB, no necesita
echo permisos de administrador y no cambia nada mas de tu equipo.
choice /c SN /n /m "Instalar Python ahora? [S/N]: "
if errorlevel 2 goto sin_python
call :instalar_python
if defined PY goto crear_venv

:sin_python
echo.
echo Para usar el dashboard instala Python 3.8 o superior desde:
echo   https://www.python.org/downloads/
echo Marca la casilla "Add Python to PATH" al instalar y luego abre este archivo de nuevo.
pause & exit /b 1

:crear_venv
echo Creando entorno local .venv ...
%PY% -m venv .venv || ( echo No se pudo crear el entorno. & pause & exit /b 1 )

:venv_ok
fc /b "%REQ%" ".venv\requisitos.instalado" >nul 2>&1
if errorlevel 1 (
  echo Instalando dependencias, solo la primera vez o tras una actualizacion ...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r "%REQ%" || ( echo Fallo la instalacion. Revisa tu internet. & pause & exit /b 1 )
  copy /y "%REQ%" ".venv\requisitos.instalado" >nul
)

echo Iniciando el dashboard... (cierra esta ventana para detenerlo)
".venv\Scripts\python.exe" "%MAIN%"
pause
exit /b 0

:buscar_python
py -3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1 && ( set "PY=py -3" & exit /b 0 )
python -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>&1 && ( set "PY=python" & exit /b 0 )
if exist "%PYINST%" ( set PY="%PYINST%" )
exit /b 0

:instalar_python
set ARQ=-amd64
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set ARQ=-arm64
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 set ARQ=
set "URL=https://www.python.org/ftp/python/3.12.8/python-3.12.8%ARQ%.exe"
set "INST=%TEMP%\python-instalador.exe"
del "%INST%" >nul 2>&1
echo Descargando Python (unos 25 MB), espera un momento ...
curl.exe -L --fail -s -o "%INST%" "%URL%"
if errorlevel 1 powershell -NoProfile -Command "Invoke-WebRequest -UseBasicParsing '%URL%' -OutFile '%INST%'"
if errorlevel 1 (
  echo No se pudo descargar. Revisa tu internet.
  del "%INST%" >nul 2>&1
  exit /b 1
)
echo Instalando Python (puede tardar un minuto) ...
start /wait "" "%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0 Include_doc=0 Shortcuts=0
del "%INST%" >nul 2>&1
if exist "%PYINST%" set PY="%PYINST%"
exit /b 0
