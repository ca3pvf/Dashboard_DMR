#!/usr/bin/env bash
# Crea una llave SSH propia (si no existe) y la copia a tu hotspot. Linux y macOS.
# La clave del hotspot se escribe UNA vez, aquí en tu terminal; el dashboard nunca la ve.
read -r -p "IP del hotspot (ej. 192.168.1.100): " IP
read -r -p "Usuario [pi-star]: " USR; USR="${USR:-pi-star}"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
[ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
KEY="$(cat "$HOME/.ssh/id_ed25519.pub")"
echo; echo "Ahora te pedirá la clave del hotspot (al escribirla no se verá nada)."
# El hotspot (Pi-Star/WPSD) tiene el disco en solo lectura: se habilita la escritura un momento y se vuelve a bloquear.
ssh "$USR@$IP" "sudo mount -o remount,rw / 2>/dev/null; mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && (grep -qxF '$KEY' ~/.ssh/authorized_keys || echo '$KEY' >> ~/.ssh/authorized_keys) && chmod 600 ~/.ssh/authorized_keys; R=\$?; sudo mount -o remount,ro / 2>/dev/null; exit \$R"
echo; echo "Probando acceso sin contraseña..."
ssh -o BatchMode=yes -o ConnectTimeout=5 "$USR@$IP" "echo CONEXION_OK" && echo "Listo: ya puedes usar 'Probar SSH' en el asistente."
