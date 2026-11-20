# Обновление Argus на сервере: код -> зависимости -> сборка backend -> перезапуск.
# Обычная операция после `git push` в main.
#
#   .\deploy\deploy.ps1                 — обновиться и перезапустить
#   .\deploy\deploy.ps1 -SkipBuild      — без пересборки jar (быстро, если код не менялся)
#   .\deploy\deploy.ps1 -Ref origin/main — обновиться до конкретной ревизии
#   .\deploy\deploy.ps1 -RequireCamera  — считать отсутствие кадров ошибкой деплоя
#
# Весь вывод (включая stderr git/mvn/pip) остаётся в deploy\logs\deploy.log:
# окно RDP закрывается, скролл уезжает — лог на месте.
param(
    [string]$Ref = "",
    [switch]$SkipBuild,
    [switch]$SkipPull,
    [switch]$RequireCamera
)

$root    = Split-Path -Parent $PSScriptRoot
$vision  = Join-Path $root "vision"
$py      = Join-Path $vision ".venv\Scripts\python.exe"
$logs    = Join-Path $PSScriptRoot "logs"
$logFile = Join-Path $logs "deploy.log"
$gitLog  = Join-Path $logs "git.log"
$mvnLog  = Join-Path $logs "mvn.log"
$pipLog  = Join-Path $logs "pip.log"

# Версии torch дублируют vision\requirements.txt и .github\workflows\ci.yml.
# CPU-индекс обязателен: обычный pip на Windows тянет CUDA-сборку (~2.5 ГБ),
# которая на сервере без видеокарты бесполезна.
$pyMajorMinor = "3.12"
$torchIdx     = "https://download.pytorch.org/whl/cpu"
$torchPins    = @("torch==2.13.0", "torchvision==0.28.0")

New-Item -ItemType Directory -Force -Path $logs | Out-Null

# Один деплой за раз. Global\ — чтобы лок виден между сессиями: ручной запуск из
# RDP, задание runner'а (служба, сессия 0) и планировщик не должны накладываться
# (иначе два git pull и два перезапуска одновременно).
$deployMutex = [System.Threading.Mutex]::new($false, "Global\ArgusDeploy")
$deployLocked = $false
try { $deployLocked = $deployMutex.WaitOne(0) } catch {}
if (-not $deployLocked) {
    Write-Host "[!] Другой деплой уже выполняется — этот запуск пропущен." -ForegroundColor Yellow
    exit 1
}

Start-Transcript -Path $logFile -Append | Out-Null

# Точка возврата: заполняется до git pull, используется в catch для подсказки откката.
$prevRev = ""
$backupDir = ""

# ВАЖНО: git и mvn пишут прогресс в stderr (Cloning into..., Receiving objects...),
# даже когда всё прошло успешно. В Windows PowerShell 5.1 stderr нативной команды
# становится NativeCommandError, и при ErrorActionPreference="Stop" скрипт умирает
# на первой же строке, с сообщением не по теме. Поэтому Stop выключаем, а код
# возврата проверяем руками в Run-Native.
$ErrorActionPreference = "Continue"
Set-Location $root

function Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# Внешняя команда с проверкой кода возврата. Без -Log вывод идёт в консоль
# построчно; с -Log уходит в файл, и при провале показывается его хвост.
function Run-Native {
    param(
        [Parameter(Mandatory)][string]$Exe,
        [Parameter(Mandatory)][string[]]$Argv,
        [Parameter(Mandatory)][string]$What,
        [string]$Log,
        [int]$Tail = 25
    )
    Write-Host "[..] $What" -ForegroundColor DarkGray
    if ($Log) {
        & $Exe @Argv *> $Log
    } else {
        & $Exe @Argv 2>&1 | ForEach-Object { Write-Host "    $_" }
    }
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        if ($Log -and (Test-Path $Log)) {
            Write-Host "[x] $What — код выхода $code. Конец $Log :" -ForegroundColor Red
            Get-Content $Log -Tail $Tail | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        }
        throw "$What провалился (код выхода $code)"
    }
    Write-Host "[ok] $What" -ForegroundColor Green
}

try {
    # --- 0. Не накладываемся на работающий Argus ---
    if (Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine -match [regex]::Escape($root) -and
            $_.CommandLine -match '(main|stream_server)\.py' }) {
        Write-Host "[*] Останавливаю работающий Argus..." -ForegroundColor Yellow
        & (Join-Path $PSScriptRoot "stop_prod.ps1") -Force
    }

    # --- 0.5. Бэкап конфигов и точка возврата ---
    # config.prod.yaml и backend\.env живут только на сервере (gitignored).
    # Копируем их перед выкаткой: откат кода не должен ронять рабочий конфиг,
    # а если новая версия его испортит — есть откуда взять.
    Step "Бэкап конфигов и ревизия для откката"
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $backupDir = Join-Path $PSScriptRoot "backup\$stamp"
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    foreach ($f in @((Join-Path $vision "config.prod.yaml"), (Join-Path $root "backend\.env"))) {
        if (Test-Path $f) {
            Copy-Item $f (Join-Path $backupDir (Split-Path $f -Leaf)) -Force
            Write-Host "[ok] бэкап $(Split-Path $f -Leaf)" -ForegroundColor DarkGray
        }
    }
    $prevRev = (git rev-parse HEAD 2>$null)
    @{ stamp = $stamp; previous_rev = $prevRev; backup = $backupDir } |
        ConvertTo-Json | Set-Content (Join-Path $logs "last-deploy.json") -Encoding UTF8
    Write-Host "[ok] ревизия до деплоя: $prevRev (для откката)" -ForegroundColor DarkGray

    # --- 1. Код (anpr едет как submodule — нужен --recurse-submodules) ---
    if (-not $SkipPull) {
        Step "Код: git pull"
        if ($Ref) {
            Run-Native -Exe git -Argv @("fetch", "--all", "--tags") -What "git fetch"
            Run-Native -Exe git -Argv @("checkout", $Ref)           -What "git checkout $Ref"
        } else {
            Run-Native -Exe git -Argv @("pull", "--ff-only", "origin", "main") -What "git pull origin main"
        }
        $rev = (git rev-parse --short HEAD 2>$null)
        $sub = (git log -1 --format=%s 2>$null)
        Write-Host "[ok] ревизия: $rev ($sub)" -ForegroundColor Green

        Step "Код: submodule anpr"
        git submodule sync --recursive 2>&1 | Out-Null
        Run-Native -Exe git -Argv @("submodule", "update", "--init", "--recursive") `
                   -What "git submodule update --init --recursive" -Log $gitLog -Tail 40
    }

    # --- 2. Окружение Python ---
    # Одноразовый бутстрап: скрипт сам создаёт venv и ставит CPU-torch.
    # На повторных деплоях обе проверки проходят за секунду, ничего не качается.
    if (-not (Test-Path $py)) {
        Step "Окружение: создаю venv (Python $pyMajorMinor)"
        if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
            throw "Не найден лаунчер 'py'. Установи Python $pyMajorMinor и повтори."
        }
        & py "-$pyMajorMinor" -c "import sys; sys.exit(0 if sys.version_info[:2]==tuple(map(int,'$pyMajorMinor'.split('.'))) else 1)" *> $null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "    Нужен ровно Python $pyMajorMinor. Установленные версии:" -ForegroundColor Red
            & py -0p 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor Red }
            throw "Python $pyMajorMinor не найден."
        }
        Run-Native -Exe py -Argv @("-$pyMajorMinor", "-m", "venv", (Join-Path $vision ".venv")) `
                   -What "py -$pyMajorMinor -m venv .venv"
    } else {
        Write-Host "[ok] venv на месте: $py" -ForegroundColor DarkGray
    }

    # CPU-вариант torch ставим из CPU-индекса и только если его нет. Отдельно от
    # requirements.txt: там torch просто зафиксирован версией, а вариант сборки
    # (CPU или CUDA) выбирает индекс, поэтому шаг нельзя выкинуть в pip -r.
    & $py -c "import torch" *> $null
    if ($LASTEXITCODE -ne 0) {
        Step "Зависимости: CPU-сборка torch"
        Run-Native -Exe $py -Argv (@("-m", "pip", "install", "--disable-pip-version-check") + $torchPins +
                                   @("--index-url", $torchIdx)) `
                   -What "pip install torch/torchvision (CPU)" -Log $pipLog -Tail 20
    } else {
        # torch стоит, но версия одна и та же у CPU и CUDA-сборки — pip ответит
        # «уже установлено» и CUDA останется. Проверяем флаг и переустанавливаем.
        & $py -c "import torch, sys; sys.exit(1 if torch.version.cuda else 0)" *> $null
        if ($LASTEXITCODE -ne 0) {
            Step "Зависимости: заменяю CUDA-сборку torch на CPU"
            Run-Native -Exe $py -Argv (@("-m", "pip", "install", "--disable-pip-version-check",
                                         "--force-reinstall", "--no-deps") + $torchPins +
                                       @("--index-url", $torchIdx)) `
                       -What "pip install --force-reinstall torch/torchvision (CPU)" -Log $pipLog -Tail 20
        } else {
            Write-Host "[ok] torch уже установлен — CPU-установку пропускаю" -ForegroundColor DarkGray
        }
    }

    Step "Зависимости: pip install -r vision/requirements.txt"
    Run-Native -Exe $py -Argv @("-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                                "-r", (Join-Path $vision "requirements.txt")) `
               -What "pip install -r requirements.txt" -Log $pipLog -Tail 20
    Run-Native -Exe $py -Argv @("-c", "import torch, cv2, ultralytics; print('[ok] torch', torch.__version__, '| cuda', torch.cuda.is_available())") `
               -What "проверка импортов torch/cv2/ultralytics"

    # --- 3. Сборка backend (jar) ---
    if (-not $SkipBuild) {
        Step "Сборка backend: mvn package"
        Push-Location (Join-Path $root "backend")
        try {
            Run-Native -Exe mvn -Argv @("-B", "-DskipTests", "package") `
                       -What "mvn package" -Log $mvnLog -Tail 30
        } finally {
            Pop-Location
        }
        $jar = Get-ChildItem (Join-Path $root "backend\target\*.jar") -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -notlike "*.original" } |
               Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $jar) {
            Write-Host "[!] Сборка прошла, но jar не найден в backend\target\" -ForegroundColor Red
            exit 1
        }
        Write-Host "[ok] собран $($jar.Name)" -ForegroundColor Green
    }

    # --- 4. Конфиг прод-профиля должен существовать (git его не трогает: он в .gitignore) ---
    $prodCfg = Join-Path $vision "config.prod.yaml"
    if (-not (Test-Path $prodCfg)) {
        Write-Host "[!] Нет $prodCfg — скопируй vision\config.prod.example.yaml и заполни RTSP." -ForegroundColor Red
        exit 1
    }

    # --- 5. Запуск + проверки ---
    Step "Запуск: run_prod.ps1"
    & (Join-Path $PSScriptRoot "run_prod.ps1") -Config $prodCfg
    if ($LASTEXITCODE -ne 0) { throw "run_prod.ps1 провалился (код $LASTEXITCODE)" }

    # --- 6. Итоговая проверка: кадры идут и backend отвечает ---
    # Камера может быть недоступна независимо от деплоя (VPN, сеть объекта,
    # обслуживание). Это не повод считать выкатку кода проваленной: по
    # умолчанию предупреждаем, а ошибкой считаем только с -RequireCamera.
    Step "Проверка после деплоя"
    $ok = $true
    $cameraOk = $true
    try {
        $f = Invoke-WebRequest "http://127.0.0.1:8090/frame" -UseBasicParsing -TimeoutSec 5
        if ($f.StatusCode -eq 200 -and $f.RawContentLength -gt 10000) {
            Write-Host "[ok] кадры идут (:8090)" -ForegroundColor Green
        } else {
            Write-Host "[!] /frame отвечает, но кадр пустой" -ForegroundColor Yellow
            $cameraOk = $false
        }
    } catch {
        Write-Host "[!] /frame не отвечает — камера/источник недоступен" -ForegroundColor Yellow
        $cameraOk = $false
    }
    if (-not $cameraOk) {
        if ($RequireCamera) {
            Write-Host "[-RequireCamera] отсутствие кадров считается ошибкой деплоя" -ForegroundColor Red
            $ok = $false
        } else {
            Write-Host "    Деплой кода это не отменяет: проверь stream_server.log и доступность камеры." -ForegroundColor Yellow
        }
    }

    try {
        $s = Invoke-WebRequest "http://127.0.0.1:8080/api/v1/stats" -UseBasicParsing -TimeoutSec 5
        if ($s.StatusCode -eq 200) {
            Write-Host "[ok] backend отвечает (:8080)" -ForegroundColor Green
        } else {
            Write-Host "[!] /api/v1/stats отвечает кодом $($s.StatusCode)" -ForegroundColor Red
            $ok = $false
        }
    } catch { Write-Host "[!] backend не отвечает" -ForegroundColor Red; $ok = $false }

    if (-not $ok) {
        # Без хвоста лога причина падения backend видна только после ручного
        # открытия файлов — а окно RDP к этому моменту часто уже закрыто.
        foreach ($name in @("backend.err.log", "backend.log")) {
            $p = Join-Path $logs $name
            if ((Test-Path $p) -and (Get-Item $p).Length -gt 0) {
                Write-Host "`n--- последние 20 строк $name ---" -ForegroundColor DarkYellow
                Get-Content $p -Tail 20 -ErrorAction SilentlyContinue |
                    ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
            }
        }
        Write-Host "`nЛоги процессов: deploy\logs\main.log, stream_server.log, backend.log" -ForegroundColor Yellow
        Write-Host "Полный лог деплоя: $logFile" -ForegroundColor Yellow
        exit 1
    }

    # --- 7. Комплект восстановления: конфиг и снимок исходников сразу после выкатки ---
    # Без этого свежий код попадёт в архив только ночью, и сбой в тот же день
    # откатит сервер на вчерашнюю ревизию. Ошибка копии деплой не отменяет.
    Step "Комплект восстановления"
    $backupCfg = Join-Path $PSScriptRoot "backup_config.ps1"
    if (Test-Path $backupCfg) {
        & $backupCfg
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[!] архив конфигурации не собран — смотри deploy\logs\backup.log" -ForegroundColor Yellow
        }
    } else {
        Write-Host "[!] нет deploy\backup_config.ps1 — комплект не обновлён" -ForegroundColor Yellow
    }

    if ($cameraOk) {
        Write-Host "`n=== Деплой завершён успешно ===" -ForegroundColor Green
    } else {
        Write-Host "`n=== Деплой завершён, НО кадры не идут ===" -ForegroundColor Yellow
        Write-Host "Код и backend обновлены; камера/поток недоступен — это не ошибка выкатки." -ForegroundColor Yellow
    }
    Write-Host "Проверь глазами: журнал http://127.0.0.1:8080, обзор http://127.0.0.1:8090"
    Write-Host "Лог деплоя: $logFile"
    exit 0
}
catch {
    Write-Host ""
    Write-Host "=== ДЕПЛОЙ ПРОВАЛЕН ===" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($_.ScriptStackTrace) { Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray }
    Write-Host ""
    Write-Host "Полный лог: $logFile" -ForegroundColor Yellow
    if ($prevRev) {
        Write-Host ""
        Write-Host "Откат к прежней ревизии:" -ForegroundColor Yellow
        Write-Host "   .\deploy\deploy.ps1 -Ref $prevRev" -ForegroundColor Yellow
        Write-Host "   (конфиги из $backupDir)" -ForegroundColor DarkGray
    }
    foreach ($f in @($gitLog, $pipLog, $mvnLog)) {
        if ($f -and (Test-Path $f) -and (Get-Item $f).Length -gt 0) {
            Write-Host "`n--- последние 25 строк $f ---" -ForegroundColor DarkYellow
            Get-Content $f -Tail 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        }
    }
    Write-Host ""
    Write-Host "Логи процессов: deploy\logs\main.log, stream_server.log, backend.log" -ForegroundColor Yellow
    Write-Host "Авто-восстановление на паузе (deploy\logs\supervise.paused):" -ForegroundColor Yellow
    Write-Host "  снимет полный .\deploy\run_prod.ps1 после устранения причины." -ForegroundColor Yellow
    exit 1
}
finally {
    Stop-Transcript | Out-Null
    if ($deployLocked) { $deployMutex.ReleaseMutex() | Out-Null }
}