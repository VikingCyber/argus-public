<#
.SYNOPSIS
  Бэкап журнала Argus: дамп базы PostgreSQL в файл с ротацией.

.DESCRIPTION
  pg_dump -Fc + проверка pg_restore -l. Старые дампы удаляются через KeepDays.
  Лог: deploy\logs\backup.log. Письмо о сбое — только с -Notify.

  Пароль БД: -Password, затем PGPASSWORD, затем pgpass.conf, затем
  datasource.password из backend\src\main\resources\application.yml.
  Не найден нигде — ошибка, без пароля по умолчанию.

.PARAMETER Password
  Для ручного разового запуска. В задаче Планировщика НЕ использовать:
  аргументы видны в списке процессов и в XML задачи. Автоматика берёт
  пароль из PGPASSWORD, pgpass.conf или application.yml.

.PARAMETER OutDir
  Каталог дампов. По умолчанию <корень репозитория>\backup\journal.

.EXAMPLE
  .\deploy\backup_journal.ps1 -DryRun
  .\deploy\backup_journal.ps1 -OutDir D:\backup\journal -KeepDays 60
  $env:PGPASSWORD = 'секрет'; .\deploy\backup_journal.ps1   # без -Password
#>
param(
    [string]$OutDir = "",
    [int]$KeepDays = 30,
    [string]$Password = "",
    [string]$PgBin = "",
    [string]$Root = "",
    [string]$DbHost = "localhost",
    [int]$DbPort = 5433,
    [string]$DbName = "argus",
    [string]$DbUser = "argus",
    [switch]$DryRun,
    [switch]$Notify
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "" }
if (-not $root) {
    # Бывает при запуске через пайп: $PSScriptRoot пуст, а Split-Path падает невнятно.
    throw "Не могу определить корень проекта: запусти скрипт из файла или передай -Root"
}
if (-not $OutDir) { $OutDir = Join-Path $root "backup\journal" }
$logFile = Join-Path $root "deploy\logs\backup.log"

function Write-Log([string]$text, [string]$color = "") {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    if (-not $DryRun) {
        $dir = Split-Path -Parent $logFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
}

function Find-PgTool([string]$name) {
    if ($PgBin) {
        $p = Join-Path $PgBin "$name.exe"
        if (Test-Path $p) { return $p }
        throw "Не найден $p — проверь -PgBin"
    }
    $candidates = Get-ChildItem "C:\Program Files\PostgreSQL\*\bin\$name.exe" -ErrorAction SilentlyContinue
    if (-not $candidates) { throw "Не найден $name.exe в C:\Program Files\PostgreSQL\*\bin" }
    # Версия — числом и только мажорная: иначе '9.6' (96) оказалась бы новее '18'.
    $best = $candidates | Sort-Object {
        $ver = Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $_.FullName))
        [int](($ver -split "\.")[0])
    } -Descending | Select-Object -First 1
    return $best.FullName
}

function Read-YmlPassword([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    $dsIndent = -1
    foreach ($line in Get-Content $path) {
        if ($dsIndent -lt 0) {
            if ($line -match "^(\s*)datasource:\s*$") { $dsIndent = $Matches[1].Length }
            continue
        }
        if ($line -notmatch "\S" -or $line -match "^\s*#") { continue }     # пустая строка/коммент
        $indent = ([regex]::Match($line, "^\s*")).Value.Length
        if ($indent -le $dsIndent) { $dsIndent = -1; continue }             # блок кончился
        if ($line -match "^\s+password:\s*(.+?)\s*$") {
            $v = $Matches[1]
            if ($v -match '^"([^"]*)"$' -or $v -match "^'([^']*)'$") { return $Matches[1] }
            return ($v -replace "\s+#.*$", "")                              # password: secret  # коммент
        }
    }
    return $null
}

function Resolve-DbPassword() {
    if ($Password) { return @{ Value = $Password; Source = "-Password" } }
    if ($env:PGPASSWORD) { return @{ Value = $env:PGPASSWORD; Source = "PGPASSWORD" } }

    # pgpass.conf: host:port:db:user:password, '*' — любой, ':' и '\' экранируются '\'.
    $pgpass = Join-Path $env:APPDATA "postgresql\pgpass.conf"
    if (Test-Path $pgpass) {
        foreach ($line in Get-Content $pgpass) {
            if (-not $line -or $line.StartsWith("#")) { continue }
            $f = $line -split '(?<!\\):', 5
            if ($f.Count -lt 5) { continue }
            $hit = ($f[0] -eq "*" -or $f[0] -eq $DbHost) -and
                   ($f[1] -eq "*" -or $f[1] -eq "$DbPort") -and
                   ($f[2] -eq "*" -or $f[2] -eq $DbName) -and
                   ($f[3] -eq "*" -or $f[3] -eq $DbUser)
            if ($hit) { return @{ Value = ($f[4] -replace "\\(.)", '$1'); Source = "pgpass.conf" } }
        }
    }

    $yml = Join-Path $root "backend\src\main\resources\application.yml"
    $fromYml = Read-YmlPassword $yml
    if ($fromYml -match '^\$\{[^:}]+:(.*)\}$') { $fromYml = $Matches[1] }   # ${DB_PASSWORD:default}
    if ($fromYml -and $fromYml -notmatch '\$\{') {
        return @{ Value = $fromYml; Source = "application.yml" }
    }

    throw "Пароль БД не найден: задай -Password, PGPASSWORD, pgpass.conf или datasource.password в $yml"
}

function Get-DumpDate($file) {
    if ($file.BaseName -match "^argus_journal_(\d{8})-(\d{6})$") {
        return [datetime]::ParseExact("$($Matches[1])$($Matches[2])", "yyyyMMddHHmmss", $null)
    }
    return $null      # имя не наше — такой файл ротация не трогает
}

function Send-Notify([string]$subject, [string]$body) {
    # notify.py печатает причину в stderr и возвращает 2 — без проверки кода
    # сбой SMTP выглядел бы как успешная отправка.
    $eap = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & (Join-Path $PSScriptRoot "notify.ps1") -Subject $subject -Body $body -Service 2>&1
        $code = $LASTEXITCODE
    } catch {
        $out = $_.Exception.Message
        $code = -1
    } finally {
        $ErrorActionPreference = $eap
    }
    if ($code -ne 0) {
        Write-Log ("письмо не отправлено (код {0}): {1}" -f $code, (($out | Out-String).Trim())) "Yellow"
    }
}

function Fail([string]$text) {
    Write-Log $text "Red"
    if ($Notify -and -not $DryRun) {
        Send-Notify "Argus: бэкап журнала не удался" "$text`n`nКаталог: $OutDir"
    }
    exit 1
}

try {
    $dumpExe = Find-PgTool "pg_dump"
    $restoreExe = Find-PgTool "pg_restore"
    $cred = Resolve-DbPassword
} catch {
    Fail $_.Exception.Message
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$file = Join-Path $OutDir "argus_journal_$stamp.dump"

if ($DryRun) {
    Write-Host "[dry-run] каталог: $OutDir"
    Write-Host "[dry-run] пароль: из $($cred.Source)"
    Write-Host "[dry-run] $dumpExe -h $DbHost -p $DbPort -U $DbUser -Fc -Z 6 -f $file $DbName"
    Write-Host "[dry-run] проверка: $restoreExe -l $file"
    $cutoff = (Get-Date).AddDays(-$KeepDays)
    $old = @(Get-ChildItem $OutDir -Filter "argus_journal_*.dump" -ErrorAction SilentlyContinue |
             Where-Object { $d = Get-DumpDate $_; $d -and $d -lt $cutoff })
    Write-Host "[dry-run] удалить старых дампов: $($old.Count) (старше $KeepDays дней)"
    exit 0
}

if (-not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    Write-Log "создан каталог $OutDir"
}

Write-Log "бэкап журнала: $DbName@$DbHost`:$DbPort -> $file (пароль: $($cred.Source))"

$prevPassword = $env:PGPASSWORD
$env:PGPASSWORD = $cred.Value
try {
    $out = & $dumpExe -h $DbHost -p $DbPort -U $DbUser -Fc -Z 6 -f $file $DbName 2>&1
    $code = $LASTEXITCODE
} finally {
    $env:PGPASSWORD = $prevPassword
}

if ($code -ne 0) {
    $err = ($out | Out-String).Trim()
    if (Test-Path $file) { Remove-Item $file -Force -ErrorAction SilentlyContinue }  # не оставлять пустышку
    Fail "ОШИБКА pg_dump (код $code): $err"
}

if (-not (Test-Path $file)) {
    Fail "ОШИБКА: pg_dump отработал, но файла нет: $file"
}

# Дамп обязан читаться: битый файл хуже отсутствующего.
$list = & $restoreExe -l $file 2>&1
$objects = @($list | Where-Object { $_ -match "^\d+;" }).Count
if ($LASTEXITCODE -ne 0 -or $objects -eq 0) {
    Fail "ОШИБКА: дамп не читается pg_restore (объектов: $objects)"
}

$sizeMb = [math]::Round((Get-Item $file).Length / 1MB, 2)

$readme = @"
Бэкап журнала Argus (PostgreSQL)
================================
Файлы:     argus_journal_ГГГГММДД-ЧЧММСС.dump, по одному в день
           (самый свежий — с наибольшей датой в имени)
База:      $DbName @ ${DbHost}:$DbPort (пользователь $DbUser)
Формат:    pg_dump -Fc (custom, сжатый)

Проверить дамп, не восстанавливая (ничего не меняет):
  & '$restoreExe' -l "<путь к файлу>"

ВОССТАНОВЛЕНИЕ на чистом сервере — три шага:

1) Создать роль и базу (пароль задать любой, он должен совпадать с конфигом backend):
  CREATE ROLE argus LOGIN PASSWORD '<пароль>';
  CREATE DATABASE argus OWNER argus;

2) Залить дамп:
  & '$restoreExe' -h localhost -p 5433 -U argus -d argus "<путь к файлу>"

3) Проверить:
  select count(*) from alert_events;

ВОССТАНОВЛЕНИЕ поверх существующей базы (заменить содержимое):
  тот же pg_restore с флагами --clean --if-exists.

Важно: дамп PostgreSQL 18 восстанавливается в 18 или новее.

Что внутри: события проездов (alert_events), камеры (cameras), место под
номера (plate_events), машины (vehicles) — схема, данные, индексы,
последовательности.

Чего внутри НЕТ:
  - роли и пароли (создаются отдельно, шаг 1);
  - настройки сервера (postgresql.conf, pg_hba.conf);
  - ФОТОГРАФИИ: они лежат отдельно, в C:\argus\data\images\<дата>\,
    и копируются своими средствами. Строка журнала ссылается на файл
    через alert_events.image_path.

Хранить: не менее $KeepDays дней. Хранить копию не только на этом сервере.
"@
Set-Content -Path (Join-Path $OutDir "README.txt") -Value $readme -Encoding UTF8

# Ротация по дате из имени файла: копирование/перенос меняет LastWriteTime.
$deleted = 0
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $OutDir -Filter "argus_journal_*.dump" -ErrorAction SilentlyContinue |
    ForEach-Object {
        $d = Get-DumpDate $_
        if (-not $d -or $d -ge $cutoff) { return }
        try {
            Remove-Item $_.FullName -Force -ErrorAction Stop
            $deleted++
        } catch {
            Write-Log "не удалось удалить $($_.Name): $($_.Exception.Message)" "Yellow"
        }
    }

$kept = @(Get-ChildItem $OutDir -Filter "argus_journal_*.dump" -ErrorAction SilentlyContinue).Count
Write-Log ("ok: {0} ({1} МБ, объектов {2}), дампов в каталоге {3}, удалено старых {4}" -f `
    (Split-Path -Leaf $file), $sizeMb, $objects, $kept, $deleted) "Green"
Write-Host "Бэкап: $file" -ForegroundColor Cyan
exit 0
