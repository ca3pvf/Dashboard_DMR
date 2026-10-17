#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
bash ./configurar_ssh.sh
echo; read -n 1 -s -r -p "Presiona una tecla para cerrar..."
