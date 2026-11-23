# Archie silencioso (worker v0.87): guía paso a paso

El worker es Archie sin interfaz. Lee la hoja **AIM Sport - Archie Campaigns**, comprueba los ELEMENTS en el NAS, renderiza cuando alguien pulsa ▶ en la hoja y deja el link de Drive en cada celda.

```
 Hoja de Google  ⇄  Apps Script (ArchieControl.gs)  ⇄  archie_worker.py en tu PC  ⇄  NAS + Drive
 (PMs)              menú ▶, colores, notas,             lee la hoja cada 30 s,       BCSA\<Estadio>\
                    carpetas de Drive                   renderiza y sube             ARCHIE-BCSAs/
```

Archivos del repo:

| Archivo | Qué es |
|---|---|
| `Core/archie_worker.py` | El worker. Reutiliza `nas_campaigns`, `campaign_batch`, `auto_solver`, `renderizador` y `render_rapido` de Archie |
| `sheets/ArchieControl.gs` | El script que se pega en la hoja |
| `Archie_Worker.bat` | Arranque con consola, para probar y ver qué hace |
| `worker/instalar_worker.ps1` | Lo deja corriendo en segundo plano y arrancando con Windows |
| `worker/parar_worker.ps1` | Lo para (`-Desinstalar` también quita el arranque automático) |
| `logs/archie_worker.log` | Log completo |
| `Config/worker_state.json` | Campañas ya entregadas (estadio + código → link) |

---

## Paso 1 · Instalar el script en la hoja (una vez)

> Hazlo con una cuenta que pueda **editar** la carpeta de Drive **ARCHIE-BCSAs** (ahora solo puede david.rubio). Los archivos se suben en nombre de esa cuenta.

1. Abre la hoja y ve a **Extensiones ▸ Apps Script**.
2. Borra el contenido de `Code.gs`, pega todo `sheets/ArchieControl.gs` y guarda (Ctrl+S). Ponle de nombre al proyecto *Archie Control*.
3. Vuelve a la hoja y recárgala (F5). Aparece el menú **Archie** junto a *Ayuda*.
4. Pulsa **Archie ▸ ⚙ Show worker key**. Google pide permisos: *Revisar permisos ▸ tu cuenta ▸ Configuración avanzada ▸ Ir a Archie Control ▸ Permitir*. Después aparece una línea `ARCHIE_SHEET_SECRET=…`. **Cópiala.**

## Paso 2 · Publicar el puente (una vez)

1. En Apps Script ve a **Implementar ▸ Nueva implementación**, pulsa ⚙ junto a *Seleccionar tipo* y elige **Aplicación web**.
2. Configura:
   - *Descripción*: `Archie worker`
   - *Ejecutar como*: **Yo**
   - *Quién tiene acceso*: **Cualquier usuario**. Hace falta para que el worker entre sin iniciar sesión, y la clave del paso 1 protege el acceso.
3. Pulsa **Implementar** y copia la **URL de la aplicación web** (termina en `/exec`).

## Paso 3 · Configurar el PC (una vez)

Añade estas dos líneas a `Config/.env` (en `D:\D Rubio\_Git\Archie\Config\.env`):

```
ARCHIE_SHEET_URL=https://script.google.com/macros/s/XXXXXXXX/exec
ARCHIE_SHEET_SECRET=la_clave_del_paso_1
```

Opcionales:
- `ARCHIE_WORKER_POLL_S=30`: cada cuánto lee la hoja.
- `ARCHIE_WORKER_CHECK_S=180`: cada cuánto vuelve a mirar los ELEMENTS en el NAS.
- `ARCHIE_WORKER_TMP=D:\ArchieTmp`: carpeta local donde renderiza antes de copiar al NAS. Por defecto es `%LOCALAPPDATA%\ArchieWorker`.

## Paso 4 · Primera prueba con consola

1. Doble clic en **`Archie_Worker.bat`**. Se abre una consola con el log.
2. En la hoja, la celda **A2** pasa a `● Archie online · idle · hh:mm` y las celdas con códigos se colorean en menos de un minuto.
3. Elige un estadio en la fila 3, escribe debajo un código que tenga ELEMENTS y espera a que la celda se ponga 🔵.
4. Pulsa **Archie ▸ ▶ Render new campaigns**. La celda pasa a 🟡 (pon el ratón encima para ver el paso y el %) y, al terminar, a 🟢 con el link.
5. Comprueba `\\192.168.110.242\Motion Team\Brands\<BRAND>\<CODE>\BCSA\<Estadio>\` y la carpeta de Drive.
6. Cierra la consola para parar el worker.

## Paso 5 · Dejarlo en segundo plano

1. Abre PowerShell en la carpeta del repo y ejecuta:
   ```
   powershell -ExecutionPolicy Bypass -File worker\instalar_worker.ps1
   ```
2. Esto crea la tarea **Archie Worker** en el Programador de tareas de Windows con esta configuración:
   - Arranca al iniciar sesión, sin ventana (`pythonw`).
   - Se reinicia sola si se cae (cada minuto).
   - No se para con batería.
   - Solo corre una copia a la vez. Si abres también el `.bat`, avisa de que ya hay uno corriendo y no arranca.
3. Para saber si está vivo, mira la celda **A2** de la hoja o `logs\archie_worker.log`.
4. Para pararlo: `powershell -ExecutionPolicy Bypass -File worker\parar_worker.ps1`. Para que deje de arrancar con Windows, añade `-Desinstalar`.

> El worker usa tu sesión de Windows para entrar al NAS: tiene que estar la sesión iniciada y el NAS accesible (red de la oficina o VPN). Si el PC se suspende, el worker se pausa y sigue al despertar. Para que no se pare, desactiva la suspensión en *Configuración ▸ Energía*.

## Paso 6 · El botón ▶ de los PMs

El menú **Archie** ya hace de botón:
- **▶ Render new campaigns**: guarda una "foto" de todos los códigos que hay en ese momento. El worker la recoge en ≤30 s y pone en cola las campañas con ELEMENTS completos 🔵 que aún no se han entregado en ese estadio. Lo que se escriba después no entra hasta el siguiente ▶.
- **↻ Re-render selected cells**: vuelve a renderizar las celdas seleccionadas, aunque ya estén en verde. Sustituye los archivos en el NAS y en Drive (en Drive, los anteriores van a la papelera).
- **🔍 Check Elements now**: vuelve a mirar el NAS sin esperar los ~3 min.
- **🗑 Delete files of selected cells**: pide confirmación y borra los System Files, 3D Previews y JSON de las celdas seleccionadas: en el NAS (solo los archivos de Archie dentro de `BCSA\<Estadio>`, sin papelera) y en Drive (la carpeta del estadio va a la papelera). Los códigos se quedan y pasan a azul, listos para renderizar otra vez (p.ej. tras cambiar un ANIM). Las celdas en amarillo se saltan. La arquitectura guardada en `Output\Elements\Archie` no se toca.
- **🧹 Reset campaign codes**: borra todos los códigos de debajo de los estadios, con sus colores, notas y el progreso. Pide confirmación y mantiene los estadios. Los archivos entregados siguen en el NAS y en Drive: si se vuelve a escribir un código ya entregado para el mismo estadio, sale en verde con su link, sin volver a renderizar.

> La primera vez que cada PM use el menú, Google le pedirá autorizar *Archie Control*. Es un paso único y aparece porque el script también gestiona las carpetas de Drive, aunque el PM no sube nada.

Si quieres un **botón visible en la hoja**:
1. Ve a **Insertar ▸ Dibujo**, crea un rectángulo con el texto `▶ RENDER` y pulsa *Guardar y cerrar*.
2. En el dibujo, pulsa ⋮ ▸ **Asignar secuencia de comandos**, escribe `menuRenderNew` y acepta.
3. Colócalo en la zona de A1:A3.

Los colores:

| Color | Significado |
|---|---|
| 🔴 Rojo | Sin ELEMENTS, ELEMENTS incompletos, código mal escrito, estadio desconocido o fallo de render. La nota explica qué pasa y muestra la ruta del NAS |
| 🟠 Naranja | Revisión manual: hay más de un LOGO o BACKGROUND. Cada código debe tener uno solo |
| 🔵 Azul | ELEMENTS OK, listo para ▶ |
| 🟡 Amarillo | En cola o renderizando, con paso y % en la nota |
| 🟢 Verde | Entregado, con el link de Drive y la ruta del NAS |
| ⚪ Gris | Falta elegir el estadio, o el NAS no responde |

Además, en las **filas 1 y 2** de cada estadio aparece el progreso del lote (`⏳ 2/5 · 43%`) con una barra. Si alguien cambia un código o un estadio, su color y su nota se borran al momento y el worker vuelve a evaluar esas celdas.

## Cómo busca los ELEMENTS (versiones C1, C2…)

- Busca los `.mov` en `<CODE>\Output\Elements` **y en sus subcarpetas** (hasta 3 niveles). Ignora `Archie`, `OLD`, `Archive`, `Backup` y las carpetas que empiezan por `.` o `_`.
- El tipo se saca del nombre: `LOGO…` (por ejemplo `LOGO_FIXED`), `ANIM…` (por ejemplo `ANIM_ZONE`), `BG`/`BACKGROUND` y `CB`.
- **Versiones**: cada archivo se asigna a una versión según este orden:
  1. el `Cn` del nombre (`JETX-UEFA-260501_C1_LOGO_FIXED.mov` → C1);
  2. si el nombre no lo lleva, el `Cn` de la subcarpeta (`SO_Just_Eat_C2\…` → C2);
  3. si tampoco, el nombre de la subcarpeta.

  Los archivos sueltos en `Elements` son comunes y completan lo que le falte a cada versión (por ejemplo, un BG compartido).
- Cada versión se valida por separado (un LOGO, un BG y al menos una ANIM) y **se renderiza por separado**: en el NAS va a `BCSA\<Estadio>\C1\`, `…\C2\`, y los nombres de archivo llevan `_C1_`. En Drive va a `<CODE>/<Estadio>/C1/`. El link de la celda abre la carpeta del estadio, con todas las versiones.
- Con versiones, la nota de la celda tiene una línea por versión (✔ entregada, ● lista, ⚠ revisar, ✖ falta algo). La celda se pone 🟢 cuando están todas entregadas, 🔵 cuando todas están listas o entregadas, 🟠 si alguna está bloqueada pero otras se pueden hacer (▶ renderiza las que estén listas) y 🔴 si no se puede hacer ninguna.

## Qué genera cada campaña

En `\\192.168.110.242\Motion Team\Brands\<BRAND>\<CODE>\BCSA\<Estadio>\` y en Drive `ARCHIE-BCSAs/<CODE>/<Estadio>/`:
- `<CODE>_<Estadio>_ARCHIEtoAE.json`
- `<CODE>_<Estadio>_SYS.mp4` (o `SYS_1ST_ROW` + `SYS_2ND_ROW` si el estadio es Amplify)
- `<CODE>_<Estadio>_3D_PREVIEW.mp4`

Para no crear duplicados en el NAS:
- La carpeta del código es la que ya existe, con su nombre completo. El worker nunca la crea.
- `BCSA` se reutiliza si ya existe, aunque esté escrita como `Bcsa` o con espacios.

Sobre la arquitectura: si el PM ya guardó una en Archie (`Output\Elements\Archie\<Estadio>.json`), se usa esa. Si no, se calcula la automática (como *Create Architecture*) y se guarda ahí para poder abrirla y ajustarla después en Archie.

---

## Desarrollo: cómo seguir evolucionándolo

- **Código Python** (`Core/archie_worker.py`): edítalo en el repo y prueba con `python Core\archie_worker.py --once`. Hace una pasada, renderiza lo que se lance y termina. Para que el worker en segundo plano use los cambios, ejecuta `parar_worker.ps1` y luego `Start-ScheduledTask "Archie Worker"`, o reinicia la sesión.
  - Los colores y textos de las notas están arriba del archivo (`C_RED`… y las funciones `note_*`).
  - `Worker.deseado()` decide el estado de cada celda, `Worker.lanzar()` decide qué entra en cola al pulsar ▶, y `Worker.render()` es el pipeline (arquitectura → System File → Preview → NAS → Drive).
- **Script de la hoja** (`sheets/ArchieControl.gs`): pégalo de nuevo en Apps Script y guarda. **Importante:** para que el worker vea los cambios, ve a *Implementar ▸ Gestionar implementaciones ▸ ✏ ▸ Versión: Nueva versión ▸ Implementar*. Así la URL no cambia. Una *Nueva implementación* crearía otra URL.
- **La estructura de la hoja** está en las constantes de arriba del `.gs`: `STADIUM_ROW`, `FIRST_CODE_ROW`, `FIRST_COL`/`LAST_COL` (B..AH), las filas de progreso, `STATUS_CELL` y `DRIVE_ROOT_ID`.
- **Versionado**: haz commit como en el resto de versiones. `Config/worker_state.json` y `logs/` están en `.gitignore`.
- **Pasar a un servidor dedicado**: el mismo repo con Python + requirements, acceso al NAS con un usuario de servicio, el `.env` con las dos líneas e `instalar_worker.ps1`. En Windows Server conviene cambiar el disparador de la tarea a *al iniciar el sistema* con ese usuario. La hoja y el script no cambian.

## Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| A2 no cambia | Revisa `logs/archie_worker.log`. Lo habitual es que falte `ARCHIE_SHEET_URL`/`SECRET` en `.env`, que la clave sea incorrecta (`unauthorized`) o que no se haya creado una nueva versión tras editar el script |
| Todo gris "NAS could not be reached" | El PC no ve `\\192.168.110.242`. Comprueba la red o la VPN |
| Rojo "RENDER FAILED · …Drive…" | La cuenta que publicó el script no puede editar *ARCHIE-BCSAs* |
| Estadio en rojo "not found in the Master Atlas" | Falta en el Master Atlas o le faltan medidas obligatorias (Canvas, Perimeter…). El log lo indica en `[ESTADIOS]` |
| Una campaña no entra al pulsar ▶ | Solo entran las 🔵. Las 🟢 ya están entregadas: usa *Re-render selected cells* |
