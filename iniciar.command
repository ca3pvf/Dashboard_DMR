#!/usr/bin/env bash
# Lanzador para macOS (doble clic en Finder)
cd "$(dirname "$0")" || exit 1
bash ./iniciar.sh
echo; read -n 1 -s -r -p "Presiona una tecla para cerrar..."
