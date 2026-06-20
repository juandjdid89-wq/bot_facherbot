@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\Erika\AppData\Local\Programs\Python\Python313\python.exe"
set "VENV_PY=.venv\Scripts\python.exe"

echo ========================================
echo        BOT DE DISCORD - INICIO FACIL
echo ========================================
echo.

if not exist "%PYTHON_EXE%" (
    echo No encontre Python en:
    echo %PYTHON_EXE%
    echo.
    echo Instala Python y marca "Add Python to PATH".
    pause
    exit /b 1
)

if not exist ".env" (
    echo Falta el archivo .env con DISCORD_TOKEN.
    echo Crea un archivo .env en esta carpeta.
    echo.
    echo Ejemplo:
    echo DISCORD_TOKEN=tu_token_aqui
    pause
    exit /b 1
)

if not exist "%VENV_PY%" (
    echo Creando entorno virtual...
    "%PYTHON_EXE%" -m venv .venv
    if errorlevel 1 (
        echo No pude crear el entorno virtual.
        pause
        exit /b 1
    )
)

echo Instalando o actualizando dependencias...
"%VENV_PY%" -m pip install -U pip
"%VENV_PY%" -m pip install "discord.py[voice]>=2.4.0" "python-dotenv>=1.0.1" "yt-dlp>=2025.1.15"
if errorlevel 1 (
    echo No pude instalar las dependencias.
    pause
    exit /b 1
)

echo.
echo Iniciando bot...
echo Para apagarlo, cierra esta ventana o presiona CTRL+C.
echo.
"%VENV_PY%" bot.py

echo.
echo El bot se detuvo.
pause
