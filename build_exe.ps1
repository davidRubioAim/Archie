# Compila launcher.py en Archie.exe (un solo archivo). Ejecutar desde esta carpeta en PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# El .exe solo contiene el launcher (librería estándar de Python): el Core sigue bajando de GitHub.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
 
python -m pip install --upgrade --disable-pip-version-check pyinstaller
 
$piArgs = @("--onefile", "--console", "--clean", "--noconfirm", "--name", "Archie", "launcher.py")
if (Test-Path "archie.ico") { $piArgs = @("--icon", "archie.ico") + $piArgs }
python -m PyInstaller @args
 
Remove-Item -Recurse -Force build, Archie.spec -ErrorAction SilentlyContinue
Write-Host "`n[OK] Listo: $PSScriptRoot\dist\Archie.exe" -ForegroundColor Green
 