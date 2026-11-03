# Compila launcher.py en Archie.exe (un solo archivo). Ejecutar desde la raíz del repo en PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
# El .exe solo contiene el launcher (librería estándar de Python): el Core sigue bajando de GitHub.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --upgrade --disable-pip-version-check pyinstaller truststore certifi

$piArgs = @("--onefile", "--console", "--clean", "--noconfirm", "--name", "Archie")
if (Test-Path "Assets\logo_Archie.ico") { $piArgs += @("--icon", "Assets\logo_Archie.ico") }
$piArgs += "launcher.py"
python -m PyInstaller @piArgs
if ($LASTEXITCODE -ne 0) { Write-Host "`n[ERROR] PyInstaller falló" -ForegroundColor Red; exit 1 }

Remove-Item -Recurse -Force build, Archie.spec -ErrorAction SilentlyContinue
Write-Host "`n[OK] Listo: $PSScriptRoot\dist\Archie.exe" -ForegroundColor Green
