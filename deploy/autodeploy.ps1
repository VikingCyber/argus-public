# Автодеплой без GitHub Actions: раз в N минут проверяет origin/main и, если
# появилась новая ревизия, запускает обычный deploy.ps1.
#
# Нужен, когда self-hosted runner поставить нельзя, но сервер умеет `git fetch`.
# Ставится задачей планировщика от SYSTEM — см. deploy\DEPLOY.md, раздел 7a.
#
#   .\deploy\autodeploy.ps1            — проверить и, если есть новое, выкатить
#   .\deploy\autodeploy.ps1 -DryRun    — только показать, что изменилось
param(
    [string]$Branch = "main",
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$logFile = Join-Path $logs "autodeploy.log"

function Say([string]$msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Set-Location $root

# Свой лок: два автодеплоя не должны накладываться. Сам deploy.ps1 берёт
# отдельный лок (Global\ArgusDeploy) — так ручной запуск и автодеплой не дерутся.
$mutex = [System.Threading.Mutex]::new($false, "Global\ArgusAutodeploy")
$got = $false
try { $got = $mutex.WaitOne(0) } catch {}
if (-not $got) { Say "уже выполняется — пропуск"; exit 0 }

try {
    & git fetch origin $Branch --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Say "git fetch провалился (код $LASTEXITCODE)"; exit 1 }

    $local = (& git rev-parse HEAD 2>$null)
    $remote = (& git rev-parse "origin/$Branch" 2>$null)
    if (-not $local -or -not $remote) { Say "не удалось определить ревизии"; exit 1 }

    if ($local -eq $remote) { exit 0 }   # без изменений — молчим, чтобы не засорять лог

    $subject = (& git log -1 --format=%s $remote 2>$null)
    Say "новая ревизия: $($local.Substring(0,7)) -> $($remote.Substring(0,7)) ($subject)"

    if ($DryRun) { Say "DryRun: выкатка не запускалась"; exit 0 }

    & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "deploy.ps1")
    $code = $LASTEXITCODE
    Say "deploy.ps1 завершился с кодом $code"
    exit $code
}
finally {
    if ($got) { $mutex.ReleaseMutex() | Out-Null }
}
