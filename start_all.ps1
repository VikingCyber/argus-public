# Argus: запуск всех компонентов без окон терминалов.
# Весь вывод каждого процесса пишется в отдельный файл лога.
# Использование: .\start_all.ps1   (остановка: .\stop_all.ps1)
#
# Логи (в .\logs\, обрезаются при каждом запуске):
#   stream.log    - обзор камеры / релей кадров (:8090)
#   backend.log   - backend Java Spring Boot (:8080)
#   main.log      - основной CV-поток (ANPR/люди)
#   collector.log - сбор датасета
# Смотреть живой лог:  Get-Content logs\main.log -Follow   (Ctrl+C - выход)
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = "$root\vision\.venv\Scripts\python.exe"
$logsDir = Join-Path $root "logs"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

# Анти-гонка: пока один start_all.ps1 работает, второй параллельный запуск выходит.
# (Проверки ниже видят только УЖЕ запущенные процессы, но не ловят одновременный старт.)
$mutex = [System.Threading.Mutex]::new($false, "ArgusStartAll")
$got = $false
try { $got = $mutex.WaitOne(0) } catch {}
if (-not $got) {
    Write-Host "[!] start_all.ps1 уже выполняется — второй запуск пропущен." -ForegroundColor Yellow
    exit 1
}

function Test-PortBusy([int]$port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Запускает процесс в скрытом окне, stdout+stderr -> в один файл.
# ВАЖНО: редирект делает cmd.exe (не PowerShell!), иначе Windows PowerShell 5.1
# перехватывает stderr как свою ошибку (NativeCommandError, «строка:1» в логе)
# и ломает кодировку кириллицы. cmd передаёт байты как есть.
# PYTHONIOENCODING=utf-8 — чтобы Python писал именно UTF-8, а не cp1251 локали.
function Start-Logged([string]$name, [string]$script, [string]$logFile, [string]$workDir) {
    $wrapper = Join-Path $logsDir ("_launch_" + ($name -replace '[^\w]', '') + ".cmd")
    $lines = @(
        "@echo off",
        "set PYTHONIOENCODING=utf-8",
        "set PYTHONUNBUFFERED=1",
        "cd /d `"$workDir`"",
        "`"$py`" -u `"$script`" > `"$logFile`" 2>&1"
    )
    Set-Content -Path $wrapper -Value $lines -Encoding Ascii
    Start-Process powershell -WindowStyle Hidden -ArgumentList @("-Command", ("cmd /c `"" + $wrapper + "`""))
    Write-Host ("[..] {0}: {1}" -f $name, $logFile)
}

# --- 0. Защита от дублей ---
# ВАЖНО: каждый скрипт виден как ПАРА процессов python (venv-лаунчер + интерпретатор),
# поэтому считаем уникальные имена скриптов, а не число процессов.
$runningScripts = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    ForEach-Object {
        if ($_.CommandLine -match '(main|data_collector|stream_server)\.py') { $Matches[0] }
    } | Select-Object -Unique)
if ($runningScripts.Count -gt 0 -or (Test-PortBusy 8080) -or (Test-PortBusy 8090)) {
    Write-Host "[!] Уже запущено: $($runningScripts -join ', '); порт 8080 занят: $(Test-PortBusy 8080), порт 8090 занят: $(Test-PortBusy 8090)." -ForegroundColor Yellow
    Write-Host "    Сначала выполни: .\stop_all.ps1" -ForegroundColor Yellow
    exit 1
}

# --- 1. Docker + БД ---
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[i] Docker не запущен — стартую Docker Desktop (до 2 мин)..."
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    $ok = $false
    foreach ($i in 1..24) {
        Start-Sleep 5
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { $ok = $true; break }
    }
    if (-not $ok) {
        Write-Host "[!] Docker так и не поднялся." -ForegroundColor Red
        exit 1
    }
}
if (-not (docker ps --format '{{.Names}}' | Select-String -Quiet argus)) {
    Push-Location $root
    docker compose up -d | Out-Null
    Pop-Location
    Start-Sleep 4
}
Write-Host "[ok] БД (argus-db)" -ForegroundColor Green

# --- 2. Обзор камеры (ВЛАДЕЕТ RTSP-сессией, запускается ПЕРВЫМ) ---
Start-Logged "stream_server" "$root\vision\stream_server.py" (Join-Path $logsDir "stream.log") "$root\vision"

# Ждём первый кадр от релея /frame
$framesOk = $false
foreach ($i in 1..20) {
    Start-Sleep 2
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8090/frame" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200 -and $r.Content.Length -gt 10000) { $framesOk = $true; break }
    } catch {}
}
if ($framesOk) {
    Write-Host "[ok] обзор + релей кадров: http://localhost:8090" -ForegroundColor Green
} else {
    Write-Host "[!] Кадры от камеры не пошли за 40с — смотри logs\stream.log." -ForegroundColor Red
}

# --- 3. Backend ---
# Backend запускается через run.ps1 (PowerShell-скрипт, ставит .env в окружение).
$backendLog = Join-Path $logsDir "backend.log"
$backendWrapper = Join-Path $logsDir "_launch_backend.cmd"
$backendLines = @(
    "@echo off",
    "set PYTHONIOENCODING=utf-8",
    "cd /d `"$root\backend`"",
    "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$root\backend\run.ps1`" > `"$backendLog`" 2>&1"
)
Set-Content -Path $backendWrapper -Value $backendLines -Encoding Ascii
Start-Process powershell -WindowStyle Hidden -ArgumentList @("-Command", ("cmd /c `"" + $backendWrapper + "`""))
Write-Host ("[..] backend (Java :8080): {0}" -f $backendLog)

# --- 4-5. Vision-компоненты (кадры берут из релея) ---
Start-Logged "main" "$root\vision\main.py" (Join-Path $logsDir "main.log") "$root\vision"
Start-Logged "collector" "$root\vision\data_collector.py" (Join-Path $logsDir "collector.log") "$root\vision"

# --- Ожидание backend ---
Write-Host "[i] Жду готовности backend на :8080..."
$ready = $false
foreach ($i in 1..30) {
    Start-Sleep 2
    try {
        $r = Invoke-WebRequest -Uri "http://localhost:8080/api/v1/stats" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
}
if ($ready) {
    Write-Host ""
    Write-Host "[ok] Argus полностью запущен (без окон, логи в файлах):" -ForegroundColor Green
    Write-Host "     журнал:      http://localhost:8080"
    Write-Host "     обзор камеры: http://localhost:8090"
    Write-Host "     логи:        $logsDir"
    Write-Host "     смотреть:    Get-Content $logsDir\main.log -Follow"
} else {
    Write-Host "[!] Backend не ответил за 60с — смотри logs\backend.log." -ForegroundColor Red
}