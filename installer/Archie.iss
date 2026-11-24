; Instalador de Archie (Inno Setup 6). No se compila a mano: lo llama installer\build_installer.ps1,
; que antes genera el Archie.exe con PyInstaller y le pasa AppVersion y SourceDir.
;
; - Instalación por usuario en %LOCALAPPDATA%\Programs\Archie (no pide administrador y el launcher puede escribir ahí
;   versions\, runtime\, Config\ y logs\, igual que hacía junto al .exe suelto).
; - Accesos directos con AppUserModelID "AIM.Archie" = la misma ID que ponen launcher.py y Core/app.py: la ventana de
;   Archie se agrupa con este acceso directo y al anclarla a la barra de tareas queda el icono de Archie, no python.exe.
; - Actualizar = volver a instalar encima (mismo AppId): se conservan versions\, Config\ y runtime\.

#ifndef AppVersion
  #define AppVersion "0.46"
#endif
#ifndef SourceDir
  #define SourceDir "..\build\pyi_dist\Archie"
#endif

[Setup]
AppId={{6F1C2B9E-6A41-4C7E-9D7B-3A2F5E8C1A47}
AppName=Archie
AppVersion={#AppVersion}
AppVerName=Archie {#AppVersion}
AppPublisher=AIM
AppComments=AIM Auto Arch
DefaultDirName={localappdata}\Programs\Archie
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=Archie_Setup_v{#AppVersion}
SetupIconFile=Archie.ico
UninstallDisplayIcon={app}\Archie.ico
UninstallDisplayName=Archie
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "Archie.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\Archie"; Filename: "{app}\Archie.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Archie.ico"; AppUserModelID: "AIM.Archie"; Comment: "AIM Auto Arch"
Name: "{autodesktop}\Archie"; Filename: "{app}\Archie.exe"; WorkingDir: "{app}"; IconFilename: "{app}\Archie.ico"; AppUserModelID: "AIM.Archie"; Comment: "AIM Auto Arch"; Tasks: desktopicon

[Run]
; Instalación normal: casilla "Ejecutar Archie" al terminar
Filename: "{app}\Archie.exe"; Description: "{cm:LaunchProgram,Archie}"; Flags: nowait postinstall skipifsilent
; Actualización automática lanzada por el propio Archie (/SILENT /RELAUNCH): se vuelve a abrir solo
Filename: "{app}\Archie.exe"; Flags: nowait; Check: Relanzar

[UninstallDelete]
; Lo que el launcher crea al usarse (motores descargados, Python privado, ajustes, logs)
Type: filesandordirs; Name: "{app}\versions"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\Config"
Type: files; Name: "{app}\config.ini"
Type: files; Name: "{app}\temp_*.zip"

[Code]
function Relanzar: Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to ParamCount do
    if CompareText(ParamStr(i), '/RELAUNCH') = 0 then
      Result := True;
end;
