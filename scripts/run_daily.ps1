# Ejecucion diaria del scraper (la lanza el Programador de tareas de Windows).
#
# Equivale a `python run.py`: descarga todo, lo fusiona con lo guardado y
# regenera curvas. El log del dia lo escribe la propia aplicacion en
# <ruta base del config>/_logs/run_AAAA-MM-DD.log.
#
# Aqui solo se guarda la salida cruda de la ultima ejecucion en
# %TEMP%\eex_scraper_task.log, por si fallase antes de arrancar (uv, Python...).
#
# Registrar/quitar la tarea:  scripts\install_task.ps1  /  scripts\install_task.ps1 -Remove

$project = Split-Path -Parent $PSScriptRoot
Set-Location $project

$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) { $uv = "uv" }

$raw = Join-Path $env:TEMP "eex_scraper_task.log"
cmd /c "`"$uv`" run --project `"$project`" python `"$project\run.py`" > `"$raw`" 2>&1"
exit $LASTEXITCODE
