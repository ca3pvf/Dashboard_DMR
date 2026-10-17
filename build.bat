@echo off
REM Genera pcbox-portable.exe para Windows. Resultado: dist\pcbox-portable.exe
cd /d "%~dp0"
python -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat
pip install --quiet -r requirements-portable.txt pyinstaller || goto :error
pyinstaller --onefile --clean --noconfirm --name pcbox-portable ^
  --add-data "services;services" --add-data "dashboard.html;." --add-data "wizard.html;." ^
  --collect-submodules socketio --collect-submodules engineio --collect-all sgp4 ^
  --exclude-module tkinter --exclude-module matplotlib --exclude-module numpy ^
  portable_app.py || goto :error
echo.
echo Listo: %cd%\dist\pcbox-portable.exe
dir dist\pcbox-portable.exe
goto :eof
:error
echo Fallo la construccion.
exit /b 1
