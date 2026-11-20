# Обёртка над notify.py: находит Python и передаёт аргументы.
# Используется supervise.ps1 и скриптами бэкапа; для ручной проверки:
#
#   .\deploy\notify.ps1 -Test
#   .\deploy\notify.ps1 -Subject "Проверка" -Body "Текст" -DryRun
#
# Секреты берутся из backend\.env (MAIL_SMTP_*, MAIL_FROM, MAIL_TO), поэтому
# письмо уходит даже когда backend лежит. С ключом -Service получатель
# берётся из MAIL_TO_NOTIFY (письма о сбоях), с откатом на MAIL_TO.
param(
    [string]$Subject = "",
    [string]$Body = "",
    [string]$BodyFile = "",
    [switch]$Html,
    [switch]$DryRun,
    [switch]$Test,
    [switch]$Service
)
$ErrorActionPreference = "Stop"

$root  = Split-Path -Parent $PSScriptRoot
$venvPy = Join-Path $root "vision\.venv\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "notify.py"

if (Test-Path $venvPy) {
    $exe = $venvPy
    $argv = @($script)
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $exe = "py"
    $argv = @("-3", $script)
} else {
    Write-Host "[!] Не найден Python: нет $venvPy и нет лаунчера py" -ForegroundColor Red
    exit 3
}

if ($Subject)  { $argv += @("--subject", $Subject) }
if ($Body)     { $argv += @("--body", $Body) }
if ($BodyFile) { $argv += @("--body-file", $BodyFile) }
if ($Html)     { $argv += "--html" }
if ($DryRun)   { $argv += "--dry-run" }
if ($Test)     { $argv += "--test" }
if ($Service)  { $argv += "--service" }

& $exe @argv
exit $LASTEXITCODE
