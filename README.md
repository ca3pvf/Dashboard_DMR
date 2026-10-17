# Dashboard pcbox (versión portable)

Dashboard para hotspots DMR (Pi-Star/WPSD): Last Heard, ranking, BrandMeister, RadioID, QRZ, QSL y correo,
contactos, sismos (CSN/USGS) y pasadas de satélites. Todo corre en tu computador (Windows, Mac o Linux) en un
solo puerto, sin instalar servicios.

## Uso

**Windows:** descarga el proyecto (botón verde *Code → Download ZIP*), descomprímelo y haz doble clic en `iniciar.bat`.

**Mac:** descarga y descomprime el ZIP, haz clic derecho sobre `iniciar.command` → *Abrir*. Si no abre,
ejecuta una vez en Terminal: `chmod +x iniciar.command`.

La primera vez prepara el entorno (1-2 minutos, requiere internet; si falta Python, intenta instalarlo)
y abre un **asistente de configuración** en el navegador: indicativo, ubicación, DMR ID e IP de tu hotspot.
Después abre el dashboard en `http://localhost:8765`.

## Datos y privacidad

- Tu configuración y datos quedan en la carpeta `pcbox-data/` (no se sube a GitHub ni se borra al actualizar).
- Por defecto solo escucha en tu equipo. Con `--lan` otros equipos de tu red pueden abrirlo y modificar sus ajustes (Gmail, QRZ).
- Para repetir el asistente: `iniciar.bat --setup` (Windows) o `./iniciar.command --setup` (Mac).

## Actualizaciones

En cada arranque revisa GitHub y baja los cambios sin tocar `pcbox-data/`. Para activarlo, edita `REPO` en
`actualizar.py` (`tu-usuario/tu-repositorio`). Los archivos `iniciar.*` no se actualizan solos.

## Opcional: SSH al hotspot

Activa en el asistente "Acceso por SSH" para habilitar Protocolos MMDVM, Pantalla Hotspot y redes DMR.
Requiere acceso SSH con llave desde este equipo al hotspot.

## No incluido

Grabaciones de QSO (necesitan `mbelib` y `ffmpeg`) y APRS.
