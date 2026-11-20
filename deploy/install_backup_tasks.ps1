# Регистрация задач ночных копий Argus. Запускать от администратора.
#
#   .\deploy\install_backup_tasks.ps1 -DryRun   — показать, что будет создано
#   .\deploy\install_backup_tasks.ps1           — создать/обновить задачи
#   .\deploy\install_backup_tasks.ps1 -Remove   — удалить все три
#
# Задачи работают под SYSTEM, поэтому не зависят от того, кто вошёл на сервер:
#   "Argus backup"         02:30  backup_journal.ps1 -Notify
#   "Argus backup photos"  02:35  backup_photos.ps1  -Notify
#   "Argus backup config"  02:40  backup_config.ps1  -Notify
# Порядок задан намеренно: архив конфигурации собирается последним, когда дамп
# и фото за прошедшие сутки уже готовы.
param(
    [switch]$Remove,
    [switch]$DryRun,
    [string]$Root = "",
    # Каталог копий (например \\storage\argus-backup). Пусто — backup\ рядом с репозиторием.
    [string]$OutDir = ""
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } else { Split-Path -Parent $PSScriptRoot }
$ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$tasks = @(
    @{ Name = "Argus backup";        Time = "02:30"; Script = "backup_journal.ps1" },
    @{ Name = "Argus backup photos"; Time = "02:35"; Script = "backup_photos.ps1" },
    @{ Name = "Argus backup config"; Time = "02:40"; Script = "backup_config.ps1" }
)

if ($Remove) {
    if (-not $DryRun) {
        $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
        if (-not $isAdmin) { Write-Host "[!] Нужны права администратора." -ForegroundColor Red; exit 1 }
    }
    foreach ($t in $tasks) {
        if ($DryRun) { Write-Host ("[dry-run] удалить задачу «{0}»" -f $t.Name); continue }
        Unregister-ScheduledTask -TaskName $t.Name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "[ok] задача удалена: $($t.Name)" -ForegroundColor Green
    }
    exit 0
}

foreach ($t in $tasks) {
    $path = Join-Path $root ("deploy\" + $t.Script)
    if (-not (Test-Path $path)) {
        Write-Host "[!] Не найден $path — скрипт запускается из репозитория Argus." -ForegroundColor Red
        exit 1
    }
    if ($DryRun) {
        $target = if ($OutDir) { " -OutDir `"$OutDir`"" } else { "" }
        Write-Host ("[dry-run] «{0}»  ежедневно {1}  -> {2} -Notify{3}" -f $t.Name, $t.Time, $t.Script, $target)
    }
}

if ($DryRun) { exit 0 }

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[!] Нужны права администратора: Планировщик пишет в системные задачи." -ForegroundColor Red
    exit 1
}

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
# StartWhenAvailable — догнать пропущенный запуск, если сервер был выключен.
# ExecutionTimeLimit 30 мин: архивы фото и конфигурации дольше не собираются.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew

foreach ($t in $tasks) {
    $script = Join-Path $root ("deploy\" + $t.Script)
    # -OutDir в задаче, а не в скрипте: путь к сетевому хранилищу задаётся один раз.
    $target = if ($OutDir) { " -OutDir `"$OutDir`"" } else { "" }
    $action = New-ScheduledTaskAction -Execute $ps `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`" -Notify$target" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $t.Time
    Register-ScheduledTask -TaskName $t.Name -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force `
        -Description ("Argus: ночная копия ({0})" -f $t.Script) | Out-Null
    Write-Host ("[ok] «{0}»: ежедневно {1} -> {2}" -f $t.Name, $t.Time, $t.Script) -ForegroundColor Green
}

Write-Host ""
Write-Host "Проверка:" -ForegroundColor Cyan
Write-Host "  Get-ScheduledTask -TaskName 'Argus backup*' | Get-ScheduledTaskInfo"
Write-Host "  Start-ScheduledTask -TaskName 'Argus backup config'"
Write-Host "  Get-Content deploy\logs\backup.log -Tail 20"
