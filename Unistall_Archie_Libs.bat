@echo off
echo ===================================================
echo   ARCHIE: Auto Arch Dependency Cleanup...
echo ===================================================
echo.

:: 1. Silently uninstall all environment libraries
echo Uninstalling packages, please wait...
pip uninstall google-genai google-generativeai customtkinter tkinterdnd2 Pillow numpy moviepy proglog python-dotenv -y --quiet 2>nul

echo.
echo ===================================================
echo   Done! All libraries have been removed.
echo ===================================================
echo.

:: 2. Pause so you can see the confirmation message before exiting
pause