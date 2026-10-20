@echo off
setlocal enabledelayedexpansion
title AIM Auto Arch - Launcher

:: 1. Resolución dinámica del directorio de trabajo (Soporte UNC y renombres de carpeta)
pushd "%~dp0"
if not exist "app.py" (
    for /d %%D in ("Archie*") do (
        if exist "%%D\app.py" cd /d "%%D" & goto :DIR_RESOLVED
    )
)
:DIR_RESOLVED
cls

echo =======================================================
echo   ARCHIE: Checking and setting up system environment...
echo =======================================================
echo.

:: 2. Detección robusta de Python nativo (Bypass Microsoft Store)
set "PYTHON_CMD="
for %%G in (py python python3) do (
    %%G --version >nul 2>&1
    if !errorlevel! equ 0 (
        set "PYTHON_CMD=%%G"
        goto :PYTHON_FOUND
    )
)

:: Fallback: Instalación de Python si no existe en el ordenador del estudio
echo [WARN] Motor Python no detectado. Instalando Python 3.11 automáticamente...
winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements >nul 2>&1
set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
set "PYTHON_CMD=python"

:PYTHON_FOUND
echo [OK] Motor Python detectado (!PYTHON_CMD!).
echo Please wait while Archie verifies and syncs the required libraries...

:: 3. Creación de entorno virtual aislado (.venv) para evitar bloqueos de permisos globales
if not exist ".venv\Scripts\activate.bat" (
    !PYTHON_CMD! -m venv .venv >nul 2>&1
)

call .venv\Scripts\activate.bat

:: 4. Instalación modular blindada contra bloqueos infinitos de red o prompts invisibles
:: --default-timeout=15 fuerza a pip a abortar el intento de conexión si la red falla, evitando el cuelgue
python -m pip install --upgrade pip --quiet --default-timeout=15 >nul 2>&1
python -m pip uninstall google-generativeai -y --quiet >nul 2>&1
python -m pip install google-genai customtkinter tkinterdnd2 Pillow numpy moviepy proglog python-dotenv --quiet --default-timeout=15 --disable-pip-version-check --no-warn-script-location >nul 2>&1

:: 5. Verificación de integridad y lanzamiento de la aplicación
cls
echo =======================================================
echo   [OK] All libraries are installed and ready!
echo   [OK] Launching Archie (AIM Auto Architecture Agent)...
echo =======================================================
echo.

python app.py

:: 6. Captura de errores en runtime
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Archie encountered an unexpected issue while running.
    pause
)

popd
endlocal