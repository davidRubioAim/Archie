# Archie Worker: arranque automático en segundo plano (Programador de tareas de Windows)
# Uso:  clic derecho > "Ejecutar con PowerShell"   (no hace falta ser administrador)
#   - Crea la tarea "Archie Worker": arranca al iniciar sesión, sin ventana (pythonw),
#     se reinicia sola si se cae y no se para por estar con batería.
#   - La arranca ahora mismo.
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $repo "Core\archie_worker.py"
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) { throw "No se encuentra pythonw.exe en el PATH (es el mismo Python que usa Archie.bat)." }
if (-not (Test-Path $script)) { throw "No se encuentra $script" }

$action   = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$script`"" -WorkingDirectory $repo
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName "Archie Worker" -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "Archie silencioso: renders desde la hoja AIM Sport - Archie Campaigns" -Force | Out-Null
Start-ScheduledTask -TaskName "Archie Worker"
Write-Host "OK: tarea 'Archie Worker' creada y arrancada. Log: $repo\logs\archie_worker.log"
Write-Host "En la hoja, la celda A1 debe pasar a '● Archie online' en menos de un minuto."
Read-Host "Pulsa Enter para cerrar"
