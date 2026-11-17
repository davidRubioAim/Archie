# Para el Archie Worker. Con -Desinstalar borra también la tarea (deja de arrancar con Windows).
param([switch]$Desinstalar)
Stop-ScheduledTask -TaskName "Archie Worker" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*archie_worker.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
if ($Desinstalar) { Unregister-ScheduledTask -TaskName "Archie Worker" -Confirm:$false -ErrorAction SilentlyContinue }
Write-Host ("Archie Worker parado" + $(if ($Desinstalar) { " y desinstalado." } else { ". Arranca otra vez con: Start-ScheduledTask 'Archie Worker' o al reiniciar sesión." }))
Read-Host "Pulsa Enter para cerrar"
