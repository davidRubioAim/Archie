# v0.46: sustituido por installer\build_installer.ps1 (Archie.exe sin consola + instalador con accesos directos).
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
& "$PSScriptRoot\installer\build_installer.ps1"
exit $LASTEXITCODE
