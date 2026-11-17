@echo off
title Archie Worker (Google Sheets)
cd /d "%~dp0"
python Core\archie_worker.py
pause
