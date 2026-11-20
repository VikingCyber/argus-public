# Остановка серверного Argus.
#   .\deploy\stop_prod.ps1            — мягкая остановка (по PID-файлу + поиск по cmdline)
#   .\deploy\stop_prod.ps1 -Force     — добить всё, что осталось
#
# Остановка ставит паузу авто-восстановлению (deploy\logs\supervise.paused):
# иначе supervise.ps1 через 5 минут поднял бы всё обратно. Пауза снимается
# полным запуском run_prod.ps1 (без -Only).
param([switch]$Force)
$ErrorActionPreference = "SilentlyContinue"

$root    = Split-Path -Parent $PSScriptRoot
$vision  = Join-Path $root "vision"
$logs    = Join-Path $PSScriptRoot "logs"
$pidFile = Join-Path $logs "pids.json"

New-Item -ItemType Directory -Force -Path $logs | Out-Null
Set-Content -Path (Join-Path $logs "supervise.paused") -Value (Get-Date -Format "s") -Encoding UTF8

# ВАЖНО: venv-скрипты видны в диспетчере задач ПАРОЙ процессов (лаунчер + интерпретатор),
# поэтому одного PID мало — kill убирает всё дерево процесса (/T).
function Stop-Tree([int]$procId, [string]$what) {
    if ($procId -le 0) { return }
    taskkill /PID $procId /T /F *> $null
    Write-Host "[ok] остановлен $what (PID $procId)" -ForegroundColor Green
}

if (Test-Path $pidFile) {
    try {
        $pids = Get-Content $pidFile -Raw | ConvertFrom-Json
        Stop-Tree ([int]$pids.main)    "main.py"
        Stop-Tree ([int]$pids.stream) "stream_server.py"
        Stop-Tree ([int]$pids.backend) "backend"
        Remove-Item $pidFile -Force
    } catch {
        Write-Host "[i] PID-файл не читается, ищу процессы по командной строке" -ForegroundColor DarkGray
    }
}

# Подчистка: все процессы этого проекта, оставшиеся после неудачного старта.
$mine = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match [regex]::Escape($root) -and
    ($_.CommandLine -match '(main|stream_server|data_collector)\.py' -or $_.CommandLine -match 'argus.*\.jar')
})
if ($mine.Count -gt 0) {
    Write-Host "[i] найдено процессов проекта: $($mine.Count)"
    foreach ($p in $mine) {
        if ($Force) { Stop-Tree ([int]$p.ProcessId) "$($p.Name) $($p.ProcessId)" }
    }
    if (-not $Force) {
        Write-Host "[i] остались процессы (мягкая остановка не сработала). Добавь -Force:" -ForegroundColor Yellow
        foreach ($p in $mine) {
            Write-Host "    $($p.ProcessId)  $($p.Name)" -ForegroundColor DarkGray
        }
    }
}

# Проверка, что RTSP-порт освободился: иначе следующий старт упадёт на "порт занят".
Start-Sleep 1
foreach ($port in 8080, 8090) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "[!] Порт $port всё ещё слушается — что-то осталось жить." -ForegroundColor Yellow
    } else {
        Write-Host "[ok] порт $port свободен"
    }
}
Write-Host "[ok] Argus остановлен."
