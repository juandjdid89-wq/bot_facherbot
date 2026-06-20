# FacherBot - Discord Music & Voice TTS Bot

Un bot de Discord nativo, altamente optimizado y de bajo consumo (~63 MB RAM) diseñado para la reproducción de música en alta fidelidad y la síntesis de voz (Text-to-Speech) automática del chat de voz. El bot interactúa directamente con las APIs de Windows mediante subprocesos asíncronos para evitar bloqueos y fugas de memoria.

---

## REQUISITOS PREVIOS CRÍTICOS (Antes de empezar)

Para que el bot funcione correctamente en tu máquina, necesitas instalar de forma obligatoria las siguientes dependencias del sistema:

1. **FFmpeg (Obligatorio)**: Es el motor que procesa, decodifica y transmite el audio a los canales de voz de Discord.
   * **Instalación rápida:** Descarga el binario para Windows, extrae el archivo `ffmpeg.exe` y colócalo directamente en la carpeta raíz del bot o dentro de una carpeta `ffmpeg/bin/` dentro del proyecto.
2. **Node.js (Recomendado/LTS)**: Necesario para que `yt-dlp` pueda evadir los bloqueos dinámicos de firmas criptográficas de YouTube (`Signature solving`). Sin Node.js, es probable que los videos de YouTube no se reproduzcan.
3. **Python 3.13**

---

## Características Principales

* **Rendimiento**: Consumo ultra eficiente optimizado a nivel de memoria (apenas ~63 MB en reposo/reproducción).
* **TTS Integrado**: Lee automáticamente los mensajes de texto enviados dentro del canal de voz activo (`!vozchat on`). No genera archivos basura gracias a un recolector de basura asíncrono que elimina los archivos temporales (`.mp3` y `.wav`) 1.5 segundos después de ser reproducidos.
* **Reproductor de Música**: Cola secuencial estructurada mediante `collections.deque` que unifica pistas de streaming (`yt-dlp`) y solicitudes locales de TTS en el mismo flujo de audio (`discord.FFmpegPCMAudio`).
* **Herramientas de Moderación**: Comandos avanzados para control de canales, mutes, bans, kicks y limpiezas de chat integrados en un manejador de errores global.
* **Concurrencia Segura**: Lógica asíncrona robusta con manejo de subprocesos nativos mediante `asyncio.create_subprocess_exec` que previene el congelamiento del Event Loop del bot.

---

## ⚙️ Configuración del Entorno (`.env`)

Crea un archivo `.env` en la raíz del proyecto con la siguiente estructura:

```env
DISCORD_TOKEN=tu_discord_bot_token_aqui
BOT_PREFIX=!
VOICE_IDLE_TIMEOUT_SECONDS=300
FFMPEG_PATH=ffmpeg.exe
