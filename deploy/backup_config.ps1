<#
.SYNOPSIS
  Упаковка конфигурации Argus в zip-архив: настройки, пароли, веса модели.

.DESCRIPTION
  Три файла не хранятся в git, но без них система не поднимется: config.prod.yaml
  (камера, зоны, ключ API), backend\.env (база, почта) и веса yolov8n.pt.
  Рядом кладётся снимок исходников (git bundle) из локального клона — тогда
  комплект восстановления не зависит от доступа к репозиторию.
  Архив собирается целиком, проверяется по числу записей и ротируется по
  количеству: хранятся последние KeepCount версий. Лог: deploy\logs\backup.log.
  Письмо об ошибках — только с -Notify.

.PARAMETER OutDir
  Каталог архивов. По умолчанию <корень репозитория>\backup\config.

.PARAMETER KeepCount
  Сколько последних архивов оставлять. По умолчанию 10.

.EXAMPLE
  .\deploy\backup_config.ps1 -DryRun
  .\deploy\backup_config.ps1 -OutDir D:\backup\config -KeepCount 20
#>
param(
    [string]$OutDir = "",
    [int]$KeepCount = 10,
    [string]$Root = "",
    [switch]$DryRun,
    [switch]$Notify
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "" }
if (-not $root) {
    throw "Не могу определить корень проекта: запусти скрипт из файла или передай -Root"
}
if (-not $OutDir) { $OutDir = Join-Path $root "backup\config" }
$logFile = Join-Path $root "deploy\logs\backup.log"

function Write-Log([string]$text, [string]$color = "") {
    $line = "{0} конфиг: {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $text
    if (-not $DryRun) {
        $dir = Split-Path -Parent $logFile
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
    if ($color) { Write-Host $line -ForegroundColor $color } else { Write-Host $line }
}

function Send-Notify([string]$subject, [string]$body) {
    # notify.py возвращает 2 при сбое SMTP — код проверяем, иначе ошибка отправки
    # выглядела бы как успешная доставка.
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
        Send-Notify "Argus: архив конфигурации не собрался" "$text`n`nЛог: $logFile"
    }
    exit 1
}

# Обязательные и необязательные файлы: путь в репозитории -> путь внутри архива.
$required = [ordered]@{
    "vision\config.prod.yaml" = "vision\config.prod.yaml"
    "backend\.env"            = "backend\.env"
    "vision\yolov8n.pt"       = "vision\yolov8n.pt"
}
$optional = [ordered]@{
    "vision\bytetrack_argus.yaml" = "vision\bytetrack_argus.yaml"
}

$missing = @()
foreach ($rel in $required.Keys) {
    if (-not (Test-Path (Join-Path $root $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
    Fail ("нет обязательных файлов: {0}. Без них архив неполный — восстановление не пройдёт." -f ($missing -join ", "))
}

$stamp = Get-Date -Format "yyyy-MM-dd_HHmm"
$zip = Join-Path $OutDir ("argus_config_{0}.zip" -f $stamp)

if ($DryRun) {
    Write-Host "[dry-run] каталог архивов: $OutDir"
    foreach ($rel in $required.Keys) { Write-Host ("[dry-run] упакую {0} ({1:N0} КБ)" -f $rel, ((Get-Item (Join-Path $root $rel)).Length / 1KB)) }
    foreach ($rel in $optional.Keys) { if (Test-Path (Join-Path $root $rel)) { Write-Host "[dry-run] упакую $rel" } }
    Write-Host "[dry-run] в архив: $zip"
    exit 0
}

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

# Сборка через промежуточный каталог: Compress-Archive иначе схлопывает
# одинаковые имена из разных папок и теряет структуру vision\ / backend\.
$stage = Join-Path ([IO.Path]::GetTempPath()) ("argus_config_" + $stamp)
try {
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    foreach ($pair in @($required.GetEnumerator()) + @($optional.GetEnumerator())) {
        $src = Join-Path $root $pair.Key
        if (-not (Test-Path $src)) { continue }
        $dst = Join-Path $stage $pair.Value
        New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
        Copy-Item $src $dst -Force
    }

    # Задачи Планировщика — на случай, если install_tasks.ps1 изменится.
    try {
        $taskDir = Join-Path $stage "tasks"
        New-Item -ItemType Directory -Path $taskDir -Force | Out-Null
        Get-ScheduledTask -TaskName "Argus*" -ErrorAction Stop | ForEach-Object {
            $file = Join-Path $taskDir (($_.TaskName -replace '[\\/:*?"<>|]', "_") + ".xml")
            Export-ScheduledTask -TaskName $_.TaskName | Set-Content -Path $file -Encoding UTF8
        }
    } catch {
        Write-Log ("задачи не выгружены: {0}" -f $_.Exception.Message) "Yellow"
    }

    $expected = @(Get-ChildItem $stage -File -Recurse).Count
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zip -CompressionLevel Optimal -Force

    # Сверка по записям: каталоги в счёт не идут (Name пустое).
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zip)
    try {
        $entries = @($archive.Entries | Where-Object { $_.Name -ne "" })
        $count = $entries.Count
        $empty = @($entries | Where-Object { $_.Length -eq 0 })
    } finally {
        $archive.Dispose()
    }
    if ($count -ne $expected) { throw "в архиве $count файлов, в каталоге $expected" }
    if ($empty.Count -gt 0) { throw ("пустые файлы в архиве: {0}" -f (($empty | ForEach-Object { $_.FullName }) -join ", ")) }

    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 2)
    Write-Log ("собран {0} ({1} файлов, {2} МБ)" -f (Split-Path -Leaf $zip), $count, $mb) "Green"
} catch {
    if (Test-Path $zip) { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
    Fail ("ошибка сборки: {0}" -f $_.Exception.Message)
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}

# Ротация по количеству: имя содержит отметку времени, сортировка по нему надёжнее
# LastWriteTime, который сдвигается при копировании архива.
$deleted = 0
$old = @(Get-ChildItem $OutDir -Filter "argus_config_*.zip" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -Skip $KeepCount)
foreach ($f in $old) {
    Remove-Item $f.FullName -Force
    $deleted++
}
if ($deleted) { Write-Log ("удалено старых архивов: {0}" -f $deleted) }

# --- Исходники: снимок ровно того кода, который работает на этом сервере ---
# Bundle собирается из локального клона, без сети и без доступа к GitHub:
# после сбоя сервер поднимается из него, даже если репозиторий недоступен.
$bundleDir = Join-Path (Split-Path -Parent $OutDir) "source"
$isRepo = (git -C $root rev-parse --is-inside-work-tree 2>$null) -eq "true"
if ($isRepo) {
    if (-not (Test-Path $bundleDir)) { New-Item -ItemType Directory -Path $bundleDir -Force | Out-Null }
    $dirty = @(git -C $root status --porcelain 2>$null)
    if ($dirty.Count -gt 0) {
        # Незакоммиченные правки в bundle не попадают — предупреждаем, но не падаем.
        Write-Log ("в рабочем дереве {0} изменённых файлов, в архив исходников они не войдут" -f $dirty.Count) "Yellow"
    }
    $bundle = Join-Path $bundleDir ("argus-src_{0}.bundle" -f $stamp)
    & git -C $root bundle create $bundle --branches --tags 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "не удалось собрать архив исходников (git bundle, код $LASTEXITCODE)" }
    & git -C $root bundle verify $bundle 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Remove-Item $bundle -Force -ErrorAction SilentlyContinue
        Fail "архив исходников не прошёл проверку git bundle verify"
    }
    $rev = (git -C $root rev-parse --short HEAD 2>$null)
    # version.txt — чтобы при восстановлении не гадать, какую версию клонировать.
    $tag = (git -C $root describe --tags --abbrev=0 2>$null)
    $version = @(
        "ревизия:  $rev",
        "тег:      $(if ($tag) { $tag } else { 'нет' })",
        "снято:    $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
    ) -join "`r`n"
    Set-Content -Path (Join-Path $bundleDir "version.txt") -Value $version -Encoding UTF8
    Write-Log ("собран {0} (ревизия {1}, {2:N0} КБ)" -f (Split-Path -Leaf $bundle), $rev, ((Get-Item $bundle).Length / 1KB)) "Green"

    $oldBundles = @(Get-ChildItem $bundleDir -Filter "argus-src_*.bundle" -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -Skip $KeepCount)
    foreach ($f in $oldBundles) { Remove-Item $f.FullName -Force }
} else {
    Write-Log "каталог не является git-репозиторием: архив исходников не собран" "Yellow"
}

$readme = @"
Архив конфигурации Argus
========================
Каталог:   $OutDir
Формат:    argus_config_ГГГГ-ММДД_ЧЧММ.zip
Внутри:    vision\config.prod.yaml  — камера, зоны, ключ API
           backend\.env            — пароль базы, почта
           vision\yolov8n.pt       — веса детектора (6,5 МБ)
           tasks\*.xml             — задачи Планировщика (справочно)
Исходники: $bundleDir
           argus-src_ГГГГ-ММДД_ЧЧММ.bundle — код, который работает сейчас
Хранить:   последние $KeepCount архивов

Восстановление на новом сервере:
  1) распаковать архив в каталог с исходниками (C:\argus), сохранив структуру;
  2) проверить, что backend\.env заканчивается переводом строки;
  3) запустить deploy\deploy.ps1 -SkipPull.

Проверить архив: открыть zip штатными средствами Windows.
Важно: внутри рабочие пароли — хранить с ограниченным доступом.
"@
Set-Content -Path (Join-Path $OutDir "README.txt") -Value $readme -Encoding UTF8

exit 0
