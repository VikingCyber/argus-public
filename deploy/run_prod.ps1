# Серверный запуск Argus: без окон, логи в deploy\logs\, профиль config.prod.yaml.
# Отличия от dev-скрипта start_all.ps1: нет data_collector, нет NN-обзора (live_nn=false),
# backend запускается из собранного jar, всё пишется в файлы (без RDP-окон).
#
#   .\deploy\run_prod.ps1                    — запуск всех компонентов
#   .\deploy\run_prod.ps1 -Only stream       — только релей :8090 (так делает supervise.ps1)
#   .\deploy\stop_prod.ps1                   — остановка
#   .\deploy\deploy.ps1                      — обновление кода + перезапуск (обычная операция)
#
# -Only не трогает остальные компоненты и обновляет в pids.json только свой PID.
param(
    [string]$Config = "",
    [ValidateSet("all", "backend", "stream", "main")]
    [string]$Only = "all",
    [switch]$SkipDbCheck,
    [switch]$NoWait
)
$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $PSScriptRoot
$vision  = Join-Path $root "vision"
$py      = Join-Path $vision ".venv\Scripts\python.exe"
$logs    = Join-Path $PSScriptRoot "logs"
$pidFile = Join-Path $logs "pids.json"
$pauseFile = Join-Path $logs "supervise.paused"
if ([string]::IsNullOrEmpty($Config)) { $Config = Join-Path $vision "config.prod.yaml" }

$needBackend = $Only -in @("all", "backend")
$needStream  = $Only -in @("all", "stream")
$needMain    = $Only -in @("all", "main")

# Анти-гонка: параллельный запуск выходит, а не плодит вторую копию процессов.
$mutex = [System.Threading.Mutex]::new($false, "ArgusProd")
$got = $false
try { $got = $mutex.WaitOne(0) } catch {}
if (-not $got) {
    Write-Host "[!] run_prod.ps1 уже выполняется — второй запуск пропущен." -ForegroundColor Yellow
    exit 1
}

function Test-PortBusy([int]$port) {
    [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# --- 0. Предусловия (только для тех компонентов, которые запускаем) ---
if ($needStream -or $needMain) {
    if (-not (Test-Path $py)) {
        Write-Host "[!] Нет venv: $py" -ForegroundColor Red
        Write-Host "    Создай по deploy\DEPLOY.md (раздел «Подготовка сервера»)." -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $Config)) {
        Write-Host "[!] Нет конфига: $Config" -ForegroundColor Red
        Write-Host "    Скопируй образец и правь RTSP:" -ForegroundColor Red
        Write-Host "    copy vision\config.prod.example.yaml vision\config.prod.yaml" -ForegroundColor Red
        exit 1
    }
}

# --- 0b. PREFLIGHT: блокеры, из-за которых сервер падает на старте ---
# Веса ANPR лежат внутри submodule anpr/ и приезжают вместе с --recurse-submodules.
# Если их нет, main.py упадёт с ImportError уже после старта.
if ($needMain) {
    $mustExist = @(
        @{ Path = (Join-Path $root "anpr\inference.py");                        What = "модуль ANPR" },
        @{ Path = (Join-Path $root "anpr\models\yolo\model\best.pt");          What = "веса детектора номера" },
        @{ Path = (Join-Path $root "anpr\models\ocr_crnn\quant\crnn_ocr_model_int8_fx.pth"); What = "OCR-модель номера" }
    )
    $missing = @($mustExist | Where-Object { -not (Test-Path $_.Path) })
    if ($missing.Count -gt 0) {
        Write-Host "[!] PREFLIGHT: не хватает файлов из submodule anpr:" -ForegroundColor Red
        $missing | ForEach-Object { Write-Host "    $($_.What): $($_.Path)" -ForegroundColor Red }
        Write-Host "    Вылечивается так (файлы лежат в git submodule, 26 МБ):" -ForegroundColor Red
        Write-Host "    git submodule update --init --recursive" -ForegroundColor DarkGray
        exit 1
    }
    Write-Host "[ok] preflight: submodule anpr на месте (веса + OCR)"
}

# Порты 8080/8090 должны быть свободны: единственный владелец RTSP — stream_server.
# При -Only проверяем только тот порт, который нужен этому компоненту.
$busy = @()
if ($needBackend -and (Test-PortBusy 8080)) { $busy += 8080 }
if ($needStream  -and (Test-PortBusy 8090)) { $busy += 8090 }
if ($busy.Count -gt 0) {
    Write-Host "[!] Порты уже заняты: $($busy -join ', '). Сначала .\deploy\stop_prod.ps1" -ForegroundColor Yellow
    exit 1
}
if ($needBackend -and -not $SkipDbCheck -and -not (Test-PortBusy 5433)) {
    Write-Host "[!] БД не отвечает на :5433 — backend не поднимется." -ForegroundColor Red
    Write-Host "    Локально:  docker compose up -d" -ForegroundColor Red
    Write-Host "    На сервере: запусти службу PostgreSQL (Get-Service postgresql* | Start-Service)" -ForegroundColor Red
    Write-Host "    Обойти проверку: .\deploy\run_prod.ps1 -SkipDbCheck" -ForegroundColor DarkGray
    exit 1
}

New-Item -ItemType Directory -Force -Path $logs | Out-Null

# Полный запуск снимает паузу надзора (её ставит stop_prod.ps1).
# -Only паузу не трогает: ручной рестарт одного компонента не должен
# неожиданно включать авто-восстановление, если оно осознанно выключено.
if ($Only -eq "all" -and (Test-Path $pauseFile)) {
    Remove-Item $pauseFile -Force -ErrorAction SilentlyContinue
    Write-Host "[ok] авто-восстановление включено (снят supervise.paused)"
}

# --- 1. Секреты backend (.env -> переменные окружения) ---
$envFile = Join-Path $root "backend\.env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2]
        }
    }
    Write-Host "[ok] секреты загружены из backend\.env"
} elseif ($needBackend) {
    Write-Host "[!] backend\.env нет — SMTP-уведомления молчат (скопируй .env.example)." -ForegroundColor Yellow
}

# --- 1b. Потоки PyTorch/OpenMP ---
# На 4-ядерном CPU (Ryzen 3 4300GE) torch по умолчанию поднимает по потоку на
# каждое логическое ядро и после каждой параллельной секции держит их в
# spin-wait (KMP_BLOCKTIME ~200 мс). При коротких частых инференсах они не
# успевают уснуть: наблюдали 495% CPU у main.py при ~4 инференсах в секунду.
# Ограничиваем параллелизм и переводим ожидание в пассивный режим — скорость
# yolov8n@640 почти не меняется, а 3-4 ядра освобождаются под backend.
# Переменные ставятся только если не заданы снаружи (backend\.env тоже подходит).
# Ставим ДО запуска любого компонента: при `-Only main` от этого зависит CPU.
if (-not $env:OMP_NUM_THREADS) { $env:OMP_NUM_THREADS = "4" }
if (-not $env:MKL_NUM_THREADS) { $env:MKL_NUM_THREADS = "4" }
if (-not $env:KMP_BLOCKTIME)   { $env:KMP_BLOCKTIME   = "0" }
if (-not $env:OMP_WAIT_POLICY) { $env:OMP_WAIT_POLICY = "PASSIVE" }
if ($needStream -or $needMain) {
    Write-Host "[..] потоки: OMP=$env:OMP_NUM_THREADS MKL=$env:MKL_NUM_THREADS KMP_BLOCKTIME=$env:KMP_BLOCKTIME"
}

# --- 2. Backend: jar из deploy.ps1, иначе fallback на mvn spring-boot:run ---
$backend = $null
if ($needBackend) {
    $jar = Get-ChildItem (Join-Path $root "backend\target\*.jar") -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -notlike "*.original" -and $_.Name -notlike "*-sources*" } |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1

    # Каталог фото задаётся абсолютным путём через переменную окружения
    # ARGUS_IMAGES_DIR (application.yml читает её: ${ARGUS_IMAGES_DIR:data/images}).
    # Раньше путь передавался токеном -Dargus.images-dir=..., но при запуске через
    # `powershell -Command` он разваливался на части и java падала с
    # "Could not find or load main class .images-dir=..." — от этой схемы отказались.
    # ARGUS_IMAGES_DIR из backend\.env имеет приоритет.
    $imagesDir = if ($env:ARGUS_IMAGES_DIR) { $env:ARGUS_IMAGES_DIR } else { Join-Path $root "data\images" }
    $env:ARGUS_IMAGES_DIR = $imagesDir
    New-Item -ItemType Directory -Force -Path $imagesDir | Out-Null

    if ($jar) {
        $backendCmd = "java -jar `"$($jar.FullName)`""
        $backendDesc = "jar $($jar.Name)"
    } else {
        if (Get-Command mvn -ErrorAction SilentlyContinue) {
            $backendCmd = "mvn -f `"$(Join-Path $root 'backend\pom.xml')`" spring-boot:run"
            $backendDesc = "mvn spring-boot:run (jar не собран — первый запуск медленнее)"
            Write-Host "[i] jar не найден, backend через Maven. Для продакшена выполни deploy.ps1." -ForegroundColor Yellow
        } else {
            Write-Host "[!] Нет собранного jar и нет mvn в PATH. Собери: mvn -f backend\pom.xml -DskipTests package" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host "[..] backend: $backendDesc"
    Write-Host "[..] фото: $imagesDir"

    $backend = Start-Process powershell -PassThru -WindowStyle Hidden `
        -WorkingDirectory $root `
        -RedirectStandardOutput (Join-Path $logs "backend.log") `
        -RedirectStandardError  (Join-Path $logs "backend.err.log") `
        -ArgumentList @("-NoProfile", "-Command", $backendCmd, ";", "exit", "`$LASTEXITCODE")
    Write-Host "[ok] backend PID $($backend.Id)"
}

# --- 3. stream_server: единственный владелец RTSP, запускается ПЕРВЫМ из vision ---
$stream = $null
if ($needStream) {
    $streamLog = Join-Path $logs "stream_server.log"
    $stream = Start-Process $py -PassThru -WindowStyle Hidden `
        -WorkingDirectory $vision `
        -RedirectStandardOutput $streamLog `
        -RedirectStandardError  (Join-Path $logs "stream_server.err.log") `
        -ArgumentList @("-u", "stream_server.py", "--config", $Config)
    Write-Host "[ok] stream_server PID $($stream.Id) (лог: $streamLog)"

    # Ждём первый кадр от релея /frame — без него main.py работать не будет.
    if (-not $NoWait) {
        $framesOk = $false
        foreach ($i in 1..30) {
            Start-Sleep 2
            try {
                $r = Invoke-WebRequest -Uri "http://127.0.0.1:8090/frame" -UseBasicParsing -TimeoutSec 2
                if ($r.StatusCode -eq 200 -and $r.RawContentLength -gt 10000) { $framesOk = $true; break }
            } catch {}
        }
        if ($framesOk) {
            Write-Host "[ok] кадры от камеры идут (релей /frame)"
        } else {
            Write-Host "[!] Кадры не пошли за 60с. Проверь RTSP в конфиге и лог stream_server:" -ForegroundColor Red
            Write-Host "    Get-Content deploy\logs\stream_server.log -Tail 30" -ForegroundColor DarkGray
        }
    }
}

# --- 4. main.py: распознавание номеров (NN-обзор выключен, CPU занят только им) ---
$main = $null
if ($needMain) {
    $main = Start-Process $py -PassThru -WindowStyle Hidden `
        -WorkingDirectory $vision `
        -RedirectStandardOutput (Join-Path $logs "main.log") `
        -RedirectStandardError  (Join-Path $logs "main.err.log") `
        -ArgumentList @("-u", "main.py", "--config", $Config)
    Write-Host "[ok] main PID $($main.Id)"
}

# --- 5. Готовность backend ---
if ($needBackend -and -not $NoWait) {
    $ready = $false
    foreach ($i in 1..45) {
        Start-Sleep 2
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8080/api/v1/stats" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch {}
    }
    if ($ready) {
        Write-Host "[ok] backend отвечает на :8080"
    } else {
        Write-Host "[!] Backend не ответил за 90с — смотри deploy\logs\backend.log" -ForegroundColor Red
    }
}

# --- 6. Сохраняем PID для stop_prod.ps1 ---
# Читаем старый файл и обновляем только свои ключи: при -Only нельзя затирать
# PID остальных компонентов, иначе stop_prod.ps1 их не найдёт.
$pids = @{}
if (Test-Path $pidFile) {
    try {
        $old = Get-Content $pidFile -Raw | ConvertFrom-Json
        foreach ($p in $old.PSObject.Properties) {
            $value = 0
            if ([int]::TryParse([string]$p.Value, [ref]$value) -and $value -gt 0) {
                $pids[$p.Name] = $value
            }
        }
    } catch {
        Write-Host "[!] pids.json не читается — будет перезаписан" -ForegroundColor Yellow
    }
}
if ($needBackend -and $backend) { $pids["backend"] = $backend.Id }
if ($needStream  -and $stream)  { $pids["stream"]  = $stream.Id }
if ($needMain    -and $main)    { $pids["main"]    = $main.Id }
$pids["started"] = (Get-Date).ToString("s")
$pids | ConvertTo-Json | Set-Content $pidFile -Encoding UTF8

Write-Host ""
Write-Host "=== Argus запущен (серверный режим: $Only) ===" -ForegroundColor Green
if ($needBackend) { Write-Host "  журнал:        http://127.0.0.1:8080" }
if ($needStream)  { Write-Host "  обзор камеры:  http://127.0.0.1:8090   (без распознавания: live_nn=false)" }
Write-Host "  логи:          deploy\logs\*.log"
Write-Host "  остановка:     .\deploy\stop_prod.ps1"
Write-Host "  PID-файл:      $pidFile"
