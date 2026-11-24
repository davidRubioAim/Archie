# Compila Archie como programa de Windows: Archie.exe (PyInstaller) + instalador (Inno Setup 6).
#   powershell -ExecutionPolicy Bypass -File .\installer\build_installer.ps1              (con consola CMD para debug)
#   powershell -ExecutionPolicy Bypass -File .\installer\build_installer.ps1 -SinConsola  (solo la pantalla de carga)
# Resultado: installer\Output\Archie_Setup_vX.XX.exe
# La versión sale de LAUNCHER_VERSION en launcher.py. El Core NO va dentro: sigue bajando del tag de GitHub que marca el Gist.
param([switch]$SinConsola)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$m = Select-String -Path "$Root\launcher.py" -Pattern 'LAUNCHER_VERSION = "v([\d.]+)"'
if (-not $m) { throw "No encuentro LAUNCHER_VERSION en launcher.py" }
$ver = $m.Matches[0].Groups[1].Value                      # p. ej. 0.46
$n = @($ver.Split('.') | ForEach-Object { [int]$_ }) + @(0, 0, 0, 0)
$v4 = "$($n[0]), $($n[1]), $($n[2]), $($n[3])"
Write-Host "== Archie launcher v$ver ==" -ForegroundColor Cyan

# 1. Archie.exe con PyInstaller (carpeta, no --onefile: arranca más rápido y los antivirus lo marcan menos)
python -m pip install --upgrade --disable-pip-version-check pyinstaller truststore certifi
if ($LASTEXITCODE -ne 0) { throw "pip falló" }

New-Item -ItemType Directory -Force "$Root\build" | Out-Null
@"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($v4), prodvers=($v4)),
  kids=[StringFileInfo([StringTable('040A04B0', [
    StringStruct('CompanyName', 'AIM'),
    StringStruct('FileDescription', 'Archie'),
    StringStruct('FileVersion', '$ver'),
    StringStruct('InternalName', 'Archie'),
    StringStruct('OriginalFilename', 'Archie.exe'),
    StringStruct('ProductName', 'Archie - AIM Auto Arch'),
    StringStruct('ProductVersion', '$ver')])]),
    VarFileInfo([VarStruct('Translation', [0x040A, 1200])])]
)
"@ | Set-Content -Encoding ASCII "$Root\build\version_info.txt"

$ico = "$Root\installer\Archie.ico"
$modo = if ($SinConsola) { "--windowed" } else { "--console" }
Write-Host "Modo: $(if ($SinConsola) { 'sin consola' } else { 'con consola (debug)' })" -ForegroundColor Cyan
python -m PyInstaller --noconfirm --clean --onedir $modo --name Archie `
    --icon "$ico" --add-data "$ico;." --version-file "$Root\build\version_info.txt" `
    --add-data "$Root\installer\splash\splash_*.png;splash" --add-data "$Root\Assets\fonts\*.ttf;fonts" `
    --hidden-import truststore --hidden-import certifi `
    --distpath "$Root\build\pyi_dist" --workpath "$Root\build\pyi_work" --specpath "$Root\build" `
    "$Root\launcher.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller falló" }

# 2. Inno Setup (se instala con winget si no está)
function Find-Iscc {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                     "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) { if (Test-Path $p) { return $p } }
    return $null
}
$iscc = Find-Iscc
if (-not $iscc) {
    Write-Host "Inno Setup 6 no está instalado: instalando con winget..." -ForegroundColor Yellow
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    $iscc = Find-Iscc
    if (-not $iscc) { throw "Instala Inno Setup 6 desde https://jrsoftware.org/isdl.php y vuelve a ejecutar este script" }
}
& $iscc "/DAppVersion=$ver" "/DSourceDir=$Root\build\pyi_dist\Archie" "$Root\installer\Archie.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup falló" }

$out = "$Root\installer\Output\Archie_Setup_v$ver.exe"
Write-Host "`n[OK] Instalador listo: $out" -ForegroundColor Green
