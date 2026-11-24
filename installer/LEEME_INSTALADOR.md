# Archie como programa de Windows (launcher v0.50)

## Qué cambia
- **Pantalla de carga AIM** (v0.47): fondo de gradiente AIM, logotipo Archie y AIM en blanco, texto en Figtree y
  barra animada. Se hornea con `installer\splash\generar_splash.py` (fondos en `installer\splash\fondo_*.png`).
- **Consola para debug** (por defecto): el .exe se compila con consola; ahí sale todo lo que imprimen launcher y Core
  mientras Archie está abierto (cerrar la consola cierra Archie). Además siempre se guarda en `logs\launcher.log` y
  `logs\archie_core.log`. Para la versión final sin consola: `build_installer.ps1 -SinConsola`.
- **Figtree en la app**: `Assets\fonts\Figtree-*.ttf` (licencia OFL) se registra solo para Archie al arrancar
  (`Core\fuentes.py`); no hay que instalarla en Windows.
- **Instalador** (`Archie_Setup_vX.XX.exe`): instala en `%LOCALAPPDATA%\Programs\Archie` sin pedir administrador,
  crea acceso en el menú Inicio (y en el escritorio si se marca) y aparece en *Aplicaciones instaladas* con desinstalador.
- **Barra de tareas**: launcher, ventana de Archie y accesos directos comparten la ID `AIM.Archie`. Con Archie abierto,
  clic derecho en su icono > *Anclar a la barra de tareas*: queda el icono de Archie y abre Archie.exe (no python.exe).
- **Icono**: `installer\Archie.ico` es un .ico real con 9 tamaños (16-256). `Assets\logo_Archie.ico` era un PNG
  renombrado, y por eso el .exe y Windows no lo usaban bien.

## Actualizaciones
- **No hace falta un .exe nuevo por versión.** Cada vez que se abre, Archie.exe lee `version` del Gist y baja ese tag.
- **Descarga incremental** (launcher v0.50): si ya hay una versión anterior en `versions\`, se copia y solo se bajan
  los ficheros que cambian entre los dos tags (API compare de GitHub). Si hay más de 150 cambios o algo falla, se baja
  el zip completo (~45 MB) mostrando el progreso. En `versions\` se quedan solo las 2 últimas versiones.
- **Botón "Update available"** (Core v1.07): la app mira el Gist al abrir y cada 15 min. Si hay una versión mayor sale
  arriba a la derecha; al pulsarlo avisa de que se cerrará el proyecto y las campañas no exportadas, y si se acepta
  cierra Archie y lo vuelve a abrir ya con la versión nueva.
- **El Gist se lee por la API de GitHub** (launcher v0.48 y botón): la URL raw tiene ~5 min de caché.
- **Si el tag del Gist no existe** (p. ej. no se hizo push del tag), el launcher v0.48 abre la última versión que ya
  tenga descargada en vez de fallar.
- **Core (lo de siempre)**: el Gist manda `version`; el launcher baja ese tag de GitHub a `versions\` y lo abre.
- **El propio programa** (solo cuando cambie launcher.py): añade al `version.json` del Gist
  ```json
  "launcher_version": "v0.47",
  "installer_url": "https://api.github.com/repos/davidRubioAim/Archie/releases/tags/launcher-v0.47"
  ```
  Al abrir Archie pregunta si instalar la versión nueva, baja el .exe de esa release (usa GITHUB_TOKEN de la hoja,
  vale con repo privado), lo instala en silencio y vuelve a abrir Archie. `installer_url` también puede ser un enlace
  directo a un .exe.

## Compilar
Requisitos: Python 3.10+ con tkinter (el de python.org vale). Inno Setup 6 se instala solo con winget si falta.
```
cd /d "D:\D Rubio\_Git\Archie"
powershell -ExecutionPolicy Bypass -File .\installer\build_installer.ps1
```
Sale `installer\Output\Archie_Setup_v0.50.exe`. Añade `-SinConsola` para la versión sin consola. La versión del instalador es `LAUNCHER_VERSION` de launcher.py.

## Pasar de un Archie.exe suelto al instalado
Instala, y si quieres conservar ajustes copia la carpeta `Config\` de la carpeta del .exe antiguo a
`%LOCALAPPDATA%\Programs\Archie\Config\`. La primera vez vuelve a bajar el Core y a revisar dependencias.
