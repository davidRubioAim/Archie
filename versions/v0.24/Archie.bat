@echo off
title AIM Auto Arch - Launcher

:: Fuerza el cambio de directorio a la ruta exacta donde está este archivo .bat
cd /d "%~dp0"

:: Verifica si le pasaste una version por consola (ej: ARCHIE.BAT v0.25)
if "%~1"=="" (
    python launcher.py
) else (
    python launcher.py %1
)

pause