# Registra (o quita con -Remove) la tarea diaria "EEX Scraper" en el
# Programador de tareas de Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # 20:00 cada dia
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -At 21:30
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Remove
#
# 20:00 porque EEX publica los precios de liquidacion por la tarde. Si el
# equipo esta apagado a esa hora, la tarea se lanza en cuanto se encienda.
# Solo corre con la sesion del usuario iniciada (no pide contrasena).

param(
    [string]$At = "20:00",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$name = "EEX Scraper"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    "Tarea '$name' eliminada."
    return
}

$script = Join-Path $PSScriptRoot "run_daily.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`"" `
    -WorkingDirectory (Split-Path -Parent $PSScriptRoot)
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 5) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings `
    -Description "Descarga diaria del EEX Market Data Hub (python run.py)" -Force | Out-Null

"Tarea '$name' registrada: cada dia a las $At."
"Logs en: <ruta base de config.toml>\_logs (y la salida cruda en $env:TEMP\eex_scraper_task.log)"
