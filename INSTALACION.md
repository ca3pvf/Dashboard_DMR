# Instalación de pcbox portable

No necesitas permisos de administrador en ningún sistema.

## 1. Descarga
En **Releases** (columna derecha del repositorio), última versión, descarga el archivo de tu sistema:

| Sistema | Archivo |
|---|---|
| Windows | `pcbox-portable-windows-x64.exe` |
| Mac Intel | `pcbox-portable-macos-x64.tar.gz` |
| Mac Apple Silicon (M1, M2, M3, M4) | `pcbox-portable-macos-arm64.tar.gz` |
| Linux PC | `pcbox-portable-linux-x64.tar.gz` |
| Linux ARM (Raspberry Pi, TV box) | `pcbox-portable-linux-arm64.tar.gz` |

¿Mac o Linux, cuál es el tuyo? En la Terminal escribe `uname -m`: `x86_64` es Intel/PC, `arm64` o `aarch64` es ARM.

Descarga también el script de SSH: `configurar_ssh.bat` (Windows) o `configurar_ssh.sh` (Mac y Linux).

## 2. Abrir el programa
- **Windows:** doble clic en el `.exe`. Si aparece el aviso azul de SmartScreen, pulsa "Más información" y luego "Ejecutar de todas formas".
- **Mac:** en la Terminal, `cd ~/Downloads && tar xzf pcbox-portable-macos-*.tar.gz && xattr -d com.apple.quarantine pcbox-portable-macos-*; ./pcbox-portable-macos-*` (usa el nombre completo del archivo que descargaste).
- **Linux:** `tar xzf pcbox-portable-linux-*.tar.gz && ./pcbox-portable-linux-*`.

Se abre el asistente en tu navegador. Escribe tu indicativo (se guarda en mayúsculas), ciudad y país; las coordenadas se buscan solas, verifica que sean las tuyas. Tus datos quedan en la carpeta `pcbox-data`, junto al programa.

## 3. Conectar el hotspot por SSH
1. **Windows:** doble clic en `configurar_ssh.bat`. **Mac y Linux:** en la Terminal, `bash ~/Downloads/configurar_ssh.sh`.
2. Escribe la IP de tu hotspot y el usuario (Enter = `pi-star`).
3. Escribe la clave del hotspot **una sola vez**. Al escribirla no se ve nada; es normal.
4. Debe terminar con `CONEXION_OK`. Vuelve al asistente y pulsa "Probar SSH".

No ejecutes `ssh-keygen` a mano: el script hace todo y no te pregunta nombres de archivo.

## Problemas frecuentes
- **`bad CPU type in executable` (Mac):** descargaste la versión equivocada. Prueba la otra (Intel o Apple Silicon).
- **`Read-only file system` al configurar SSH:** usa el `configurar_ssh` de esta versión (v1.1.2 o posterior); habilita la escritura del hotspot un momento y la vuelve a bloquear.
- **La tarjeta Satélites no muestra pasadas:** necesita internet la primera vez para bajar los datos orbitales.
