@echo off
rem Crea una llave SSH propia (si no existe) y la copia a tu hotspot. Windows 10/11.
rem La clave del hotspot se escribe UNA vez, aqui; el dashboard nunca la ve.
set /p IP=IP del hotspot (ej. 192.168.1.100): 
set /p USR=Usuario [pi-star]: 
if "%USR%"=="" set USR=pi-star
where ssh >nul 2>&1 || ( echo Falta el cliente SSH: Configuracion ^> Aplicaciones ^> Caracteristicas opcionales ^> Cliente OpenSSH. & pause & exit /b 1 )
if not exist "%USERPROFILE%\.ssh" mkdir "%USERPROFILE%\.ssh"
if not exist "%USERPROFILE%\.ssh\id_ed25519" ssh-keygen -t ed25519 -N "" -f "%USERPROFILE%\.ssh\id_ed25519"
type "%USERPROFILE%\.ssh\id_ed25519.pub" | ssh %USR%@%IP% "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
echo.
echo Probando acceso sin contrasena...
ssh -o BatchMode=yes -o ConnectTimeout=5 %USR%@%IP% "echo CONEXION_OK" && echo Listo: ya puedes usar "Probar SSH" en el asistente.
pause
