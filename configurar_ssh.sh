#!/usr/bin/env bash
# Crea una llave SSH propia (si no existe) y la copia a tu hotspot. Linux y macOS.
# La clave del hotspot se escribe UNA vez, aquí en tu terminal; el dashboard nunca la ve.
read -r -p "IP del hotspot (ej. 192.168.1.100): " IP
read -r -p "Usuario [pi-star]: " USR; USR="${USR:-pi-star}"
[ -f "$HOME/.ssh/id_ed25519" ] || ssh-keygen -t ed25519 -N "" -f "$HOME/.ssh/id_ed25519"
if command -v ssh-copy-id >/dev/null 2>&1; then
  ssh-copy-id -i "$HOME/.ssh/id_ed25519.pub" "$USR@$IP"
else
  ssh "$USR@$IP" "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys" < "$HOME/.ssh/id_ed25519.pub"
fi
echo; echo "Probando acceso sin contraseña..."
ssh -o BatchMode=yes -o ConnectTimeout=5 "$USR@$IP" "echo CONEXION_OK" && echo "Listo: ya puedes usar 'Probar SSH' en el asistente."
