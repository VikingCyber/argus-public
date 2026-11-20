<#
.SYNOPSIS
  Упаковка фото проездов Argus в zip-архивы по датам.

.DESCRIPTION
  Один архив на дату: photos_ГГГГ-ММДД.zip. Фото неизменяемы, поэтому архив
  собирается один раз; папка текущего дня пропускается. После сборки число
  записей в zip сверяется с числом файлов. Исходные фото не удаляются.
  Ротация по дате из имени файла (KeepDays). Лог: deploy\logs\backup.log.
  Письмо об ошибках — только с -Notify.

.PARAMETER ImagesDir
  Каталог с папками-датами. По умолчанию <корень репозитория>\data\images.

.PARAMETER OutDir
  Каталог архивов. По умолчанию <корень репозитория>\backup\photos.

.EXAMPLE
  .\deploy\backup_photos.ps1 -DryRun
  .\deploy\backup_photos.ps1 -OutDir D:\backup\photos -KeepDays 180
  .\deploy\backup_photos.ps1 -Force
#>
param(
    [string]$ImagesDir = "",
    [string]$OutDir = "",
    [int]$KeepDays = 120,
    [string]$Root = "",
    [switch]$DryRun,
    [switch]$Notify,
    [switch]$Force
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "" }
if (-not $root) {
    # Бывает при запуске через пайп: $PSScriptRoot пуст, а Split-Path падает невнятно.
    throw "Не могу определить корень проекта: запусти скрипт из файла или передай -Root"
}
if (-not $ImagesDir) { $ImagesDir = Join-Path $root "data\images" }
if (-not $OutDir) { $OutDir = Join-Path $root "backup\photos" }
$logFile = Join-Path $root "deploy\logs\backup.log"

function Write-Log([string]$text, [string]$color = "") {
    $line = "{0} фото: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    if (-not $DryRun) {
        $dir = Split-Path -Parent $logFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
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
        Send-Notify "Argus: архив фото не собрался" "$text`n`nЛог: $logFile"
    }
    exit 1
}

if (-not (Test-Path $ImagesDir)) {
    Fail "нет каталога с фото: $ImagesDir"
}
if (-not $DryRun -and -not (Test-Path $OutDir)) {
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
}

$today = Get-Date -Format "yyyy-MM-dd"
$built = 0
$skipped = 0
$failed = 0

$days = Get-ChildItem $ImagesDir -Directory |
    Where-Object { $_.Name -match "^\d{4}-\d{2}-\d{2}$" } |
    Sort-Object Name

foreach ($day in $days) {
    if ($day.Name -eq $today) { continue }          # день ещё наполняется
    $files = @(Get-ChildItem $day.FullName -File -Recurse)
    if ($files.Count -eq 0) { continue }

    $zip = Join-Path $OutDir ("photos_{0}.zip" -f $day.Name)
    if ((Test-Path $zip) -and -not $Force) {
        $skipped++
        continue
    }

    if ($DryRun) {
        Write-Host ("[dry-run] {0}: {1} файлов -> {2}" -f $day.Name, $files.Count, $zip)
        continue
    }

    try {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path $day.FullName -DestinationPath $zip -CompressionLevel Optimal

        # Сверка по файлам: на PS 5.1 Compress-Archive может добавить записи
        # каталогов (Name пустое) — их не считаем.
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $entries = [IO.Compression.ZipFile]::OpenRead($zip)
        try { $count = @($entries.Entries | Where-Object { $_.Name -ne "" }).Count } finally { $entries.Dispose() }
        if ($count -ne $files.Count) {
            throw "в архиве $count файлов, в каталоге $($files.Count)"
        }
        $mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
        Write-Log ("{0}: {1} файлов -> {2} ({3} МБ)" -f $day.Name, $count, (Split-Path -Leaf $zip), $mb)
        $built++
    } catch {
        $failed++
        Write-Log ("ОШИБКА {0}: {1}" -f $day.Name, $_.Exception.Message) "Red"
        if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
    }
}

# Ротация: дата из имени, не из LastWriteTime (пересборка её сдвигает).
$deleted = 0
$cutoff = (Get-Date).Date.AddDays(-$KeepDays)
Get-ChildItem $OutDir -Filter "photos_*.zip" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.BaseName -match "photos_(\d{4}-\d{2}-\d{2})") {
        $d = [datetime]::ParseExact($Matches[1], "yyyy-MM-dd", $null)
        if ($d -lt $cutoff) {
            if ($DryRun) { Write-Host ("[dry-run] удалить {0}" -f $_.Name) }
            else { Remove-Item $_.FullName -Force; $deleted++ }
        }
    }
}

if ($DryRun) { exit 0 }

$readme = @"
Архив фотографий проездов Argus
===============================
Каталог:   $OutDir
Формат:    один zip на дату: photos_ГГГГ-ММДД.zip
Внутри:    папка с датой и файлами снимков (cam-01-alert-*.jpg)
Собрано:   $(Get-Date -Format "yyyy-MM-dd HH:mm")
Хранить:   $KeepDays дней (старые удаляются автоматически)

Зачем архив: рабочие фото ($ImagesDir) чистятся регламентом (90 дней).
Архив собирается один раз за дату и живёт дольше, поэтому снимок можно
достать, даже если рабочий файл уже удалён.

Связь с базой: alert_events.image_path = 2026-11-17/cam-01-alert-....jpg,
это содержимое архива photos_2026-11-17.zip.

Проверить: открыть zip штатными средствами Windows.
Восстановить: распаковать нужный zip в каталог с фото.
"@
Set-Content -Path (Join-Path $OutDir "README.txt") -Value $readme -Encoding UTF8

Write-Log ("готово: собрано {0}, пропущено {1}, ошибок {2}, удалено старых {3}" -f `
    $built, $skipped, $failed, $deleted) $(if ($failed) { "Yellow" } else { "Green" })

if ($failed -and $Notify) {
    Send-Notify "Argus: архив фото собрался с ошибками" "Ошибок: $failed. Подробности: $logFile"
}

if ($failed) { exit 1 }
exit 0
