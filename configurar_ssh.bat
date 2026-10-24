@echo off
rem Crea una llave SSH propia (si no existe) y la copia a tu hotspot. Windows 10/11. No requiere administrador.
rem La clave del hotspot se escribe UNA vez, aqui; el dashboard nunca la ve.
set /p IP=IP del hotspot (ej. 192.168.1.100): 
set /p USR=Usuario [pi-star]: 
if "%USR%"=="" set USR=pi-star
where ssh >nul 2>&1 || ( echo Falta el cliente SSH: Configuracion ^> Aplicaciones ^> Caracteristicas opcionales ^> Cliente OpenSSH. & pause & exit /b 1 )
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if not exist "%USERPROFILE%\.ssh\id_ed25519" ssh-keygen -t ed25519 -N "" -f "%USERPROFILE%\.ssh\id_ed25519"
set /p KEY=<"%USERPROFILE%\.ssh\id_ed25519.pub"
echo.
echo Ahora te pedira la clave del hotspot (al escribirla no se vera nada).
rem El hotspot (Pi-Star/WPSD) tiene el disco en solo lectura: se habilita la escritura un momento y se vuelve a bloquear.
ssh %USR%@%IP% "sudo mount -o remount,rw / 2>/dev/null; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && (grep -qxF '%KEY%' ~/.ssh/authorized_keys || echo '%KEY%' >> ~/.ssh/authorized_keys) && chmod 600 ~/.ssh/authorized_keys; R=$?; sudo mount -o remount,ro / 2>/dev/null; exit $R"
echo.
echo Probando acceso sin contrasena...
ssh -o BatchMode=yes -o ConnectTimeout=5 %USR%@%IP% "echo CONEXION_OK" && echo Listo: ya puedes usar "Probar SSH" en el asistente.
pause
