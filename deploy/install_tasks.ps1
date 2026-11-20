# Регистрация задач Планировщика для Argus. Запускать от администратора.
#
#   .\deploy\install_tasks.ps1          — создать/обновить задачи
#   .\deploy\install_tasks.ps1 -Remove  — удалить обе задачи
#
# Задачи:
#   "Argus"           — запуск всех компонентов при старте сервера;
#   "Argus supervise" — надзор каждые 5 минут (авто-рестарт упавших).
#
# Задача "Argus" уже описана в DEPLOY.md; этот скрипт создаёт её заново
# (Register-ScheduledTask -Force), чтобы действие и рабочий каталог совпадали
# с текущим расположением репозитория.
param(
    [switch]$Remove,
    [string]$Root = ""
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } else { Split-Path -Parent $PSScriptRoot }
$ps   = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$startTask = "Argus"
$superviseTask = "Argus supervise"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] Нужны права администратора: Планировщик пишет в системные задачи." -ForegroundColor Red
    Write-Host "    Запусти PowerShell от имени администратора и повтори." -ForegroundColor Red
    exit 1
}

if ($Remove) {
    foreach ($name in @($superviseTask, $startTask)) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[ok] задача удалена: $name" -ForegroundColor Green
    }
    exit 0
}

$runProd   = Join-Path $root "deploy\run_prod.ps1"
$supervise = Join-Path $root "deploy\supervise.ps1"
foreach ($f in @($runProd, $supervise)) {
    if (-not (Test-Path $f)) {
        Write-Host "[!] Не найден $f — скрипт запускается из репозитория Argus." -ForegroundColor Red
        exit 1
    }
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
# ExecutionTimeLimit = 0 — без ограничения: run_prod ждёт готовности до 90 с,
# supervise может ждать камеру, оба не должны обрываться по таймауту задачи.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

# --- 1. Запуск при загрузке сервера ---
$startAction = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runProd`"" `
    -WorkingDirectory $root
$startTrigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName $startTask -Action $startAction -Trigger $startTrigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Argus: запуск backend, stream_server и main.py при старте сервера" | Out-Null
Write-Host "[ok] задача «$startTask»: при старте сервера -> run_prod.ps1" -ForegroundColor Green

# --- 2. Надзор каждые 5 минут ---
# RepetitionDuration не задаём: Windows трактует это как «повторять бесконечно».
$supAction = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$supervise`"" `
    -WorkingDirectory $root
$supTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskName $superviseTask -Action $supAction -Trigger $supTrigger `
    -Principal $principal -Settings $settings -Force `
    -Description "Argus: проверка и авто-рестарт компонентов каждые 5 минут" | Out-Null
Write-Host "[ok] задача «$superviseTask»: каждые 5 минут -> supervise.ps1" -ForegroundColor Green

Write-Host ""
Write-Host "Проверка:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName 'Argus*' | Get-ScheduledTaskInfo"
Write-Host "  Start-ScheduledTask -TaskName 'Argus supervise'"
Write-Host "  Get-Content deploy\logs\supervise.log -Tail 30"
Write-Host ""
Write-Host "Пауза авто-восстановления (обслуживание):" -ForegroundColor Cyan
Write-Host "  New-Item deploy\logs\supervise.paused -Force    # пауза"
Write-Host "  Remove-Item deploy\logs\supervise.paused       # снять"
Write-Host "  (stop_prod.ps1 ставит паузу сам, полный run_prod.ps1 её снимает)"
