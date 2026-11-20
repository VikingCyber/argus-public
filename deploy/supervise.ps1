# Надзор за компонентами Argus: проверяет, что backend, stream_server и main.py
# живы и отвечают, и перезапускает упавшие. Запускается Планировщиком каждые
# 5 минут (deploy\install_tasks.ps1), вручную — для проверки.
#
#   .\deploy\supervise.ps1 -DryRun     — только показать, что было бы сделано
#   .\deploy\supervise.ps1             — боевой прогон
#
# Правила:
#  * компонент не работает (нет процесса) — перезапуск;
#  * процесс жив, но не отвечает 2 прогона подряд — перезапуск;
#  * stream жив, но кадров нет 3 прогона (15 мин) — перезапуск релея + письмо;
#  * main жив, кадры идут, но его лог не обновлялся 120 с — перезапуск
#    (main пишет статистику каждые 10 с, пока обрабатывает кадры);
#  * больше 4 рестартов компонента за час — пауза и письмо (без зацикливания);
#  * мало места на диске — письмо не чаще раза в 12 ч.
#
# Письма шлёт notify.ps1 (напрямую через SMTP из backend\.env), поэтому
# работают и когда backend лежит. Пауза авто-восстановления:
# deploy\logs\supervise.paused (её ставит stop_prod.ps1).
param(
    [string]$Config = "",
    [int]$MinFreeGb = 5,
    [int]$LogStaleSec = 120,
    [switch]$DryRun
)
$ErrorActionPreference = "Continue"

$root      = Split-Path -Parent $PSScriptRoot
$vision    = Join-Path $root "vision"
$logs      = Join-Path $PSScriptRoot "logs"
$pidFile   = Join-Path $logs "pids.json"
$stateFile = Join-Path $logs "supervise-state.json"
$logFile   = Join-Path $logs "supervise.log"
$pauseFile = Join-Path $logs "supervise.paused"
if ([string]::IsNullOrEmpty($Config)) { $Config = Join-Path $vision "config.prod.yaml" }
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# Ротация: задача работает каждые 5 минут, файл иначе растёт без предела.
if ((Test-Path $logFile) -and (Get-Item $logFile).Length -gt 5MB) {
    Move-Item $logFile "$logFile.1" -Force -ErrorAction SilentlyContinue
}

function Log([string]$message, [string]$level = "info") {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $message
    Add-Content -Path $logFile -Value $line -Encoding UTF8
    Write-Host $line
}

if (Test-Path $pauseFile) {
    Log "авто-восстановление на паузе ($pauseFile) — выходим"
    exit 0
}

# Один надзор за раз.
$mutex = [System.Threading.Mutex]::new($false, "Global\ArgusSupervise")
$locked = $false
try { $locked = $mutex.WaitOne(0) } catch {}
if (-not $locked) {
    Log "предыдущий прогон надзора ещё идёт — выходим"
    exit 0
}

# Не мешаем деплою: он сам останавливает и поднимает компоненты.
$deployMutex = [System.Threading.Mutex]::new($false, "Global\ArgusDeploy")
$deployFree = $false
try { $deployFree = $deployMutex.WaitOne(0) } catch { $deployFree = $true }
if (-not $deployFree) {
    Log "идёт деплой — надзор пропущен"
    if ($locked) { $mutex.ReleaseMutex() | Out-Null }
    exit 0
}

function Read-State {
    $state = @{ version = 1; components = @{}; alerts = @{}; lastRun = $null }
    if (-not (Test-Path $stateFile)) { return $state }
    try {
        $raw = Get-Content $stateFile -Raw | ConvertFrom-Json
        if ($raw.components) {
            foreach ($p in $raw.components.PSObject.Properties) {
                $c = $p.Value
                $state.components[$p.Name] = @{
                    restarts      = @($c.restarts | Where-Object { $_ })
                    failStreak    = [int]$c.failStreak
                    noFrameStreak = [int]$c.noFrameStreak
                    lastRestart   = $c.lastRestart
                }
            }
        }
        if ($raw.alerts) {
            foreach ($p in $raw.alerts.PSObject.Properties) {
                $state.alerts[$p.Name] = $p.Value
            }
        }
    } catch {
        Log "state-файл не читается ($($_.Exception.Message)) — начинаем с чистого" "warn"
    }
    return $state
}

function Save-State($state) {
    $state.lastRun = (Get-Date).ToString("s")
    $state | ConvertTo-Json -Depth 6 | Set-Content $stateFile -Encoding UTF8
}

function Get-CompState($state, [string]$name) {
    if (-not $state.components.ContainsKey($name)) {
        $state.components[$name] = @{
            restarts = @(); failStreak = 0; noFrameStreak = 0; lastRestart = $null
        }
    }
    return $state.components[$name]
}

function Get-Pids {
    $map = @{}
    if (-not (Test-Path $pidFile)) { return $map }
    try {
        $raw = Get-Content $pidFile -Raw | ConvertFrom-Json
        foreach ($p in $raw.PSObject.Properties) {
            $value = 0
            if ([int]::TryParse([string]$p.Value, [ref]$value) -and $value -gt 0) {
                $map[$p.Name] = $value
            }
        }
    } catch {
        Log "pids.json не читается: $($_.Exception.Message)" "warn"
    }
    return $map
}

function Get-Pid($pids, [string]$name) {
    if ($pids.ContainsKey($name)) { return $pids[$name] }
    return 0
}

# Запасной поиск, если pids.json потерялся: ищем процесс этого репозитория по
# командной строке. Без него надзор считал бы живой Argus упавшим и пытался
# запустить второй экземпляр (тот упал бы на занятом порту).
function Find-ProcessId([string]$pattern) {
    $found = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and
        $_.CommandLine -match [regex]::Escape($root) -and
        $_.CommandLine -match $pattern
    } | Sort-Object CreationDate -Descending | Select-Object -First 1
    if ($found) { return [int]$found.ProcessId }
    return 0
}

# PID переиспользуется Windows: проверяем не только номер, но и командную строку.
function Test-ProcessAlive([int]$procId, [string]$pattern) {
    if ($procId -le 0) { return $false }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { return $false }
    if ($p.CommandLine -and $p.CommandLine -match $pattern) { return $true }
    return $false
}

function Test-Http([string]$url, [int]$minBytes = 0) {
    try {
        $r = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -ne 200) { return $false }
        if ($minBytes -gt 0 -and $r.RawContentLength -lt $minBytes) { return $false }
        return $true
    } catch {
        return $false
    }
}

function Get-LogAgeSec([string]$path) {
    if (-not (Test-Path $path)) { return [double]::MaxValue }
    return ((Get-Date) - (Get-Item $path).LastWriteTime).TotalSeconds
}

function Send-Alert($state, [string]$key, [string]$subject, [string]$body, [int]$hours = 6) {
    $now = Get-Date
    if ($state.alerts.ContainsKey($key)) {
        try {
            $last = [datetime]::Parse([string]$state.alerts[$key])
            if (($now - $last).TotalHours -lt $hours) {
                Log "письмо «$subject» не нужно — уже отправляли $($state.alerts[$key])"
                return
            }
        } catch { }
    }
    if ($DryRun) {
        Log "DryRun: письмо «$subject» не отправляю"
        return
    }
    & (Join-Path $PSScriptRoot "notify.ps1") -Subject $subject -Body $body -Service | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $state.alerts[$key] = $now.ToString("s")
        Log "письмо отправлено: $subject" "ok"
    } else {
        Log "письмо НЕ отправлено (код $LASTEXITCODE): $subject" "error"
    }
}

function Get-ComponentPattern([string]$name) {
    switch ($name) {
        "backend" { return '(java|mvn).*(argus|backend)' }
        "stream"  { return 'stream_server\.py' }
        "main"    { return 'main\.py' }
    }
    return ''
}

# PID переиспользуется Windows: убиваем только если по этому PID всё ещё наш
# процесс. Иначе taskkill мог бы прибить чужую программу с тем же номером.
function Stop-Component([int]$procId, [string]$pattern) {
    if ($procId -le 0) { return }
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$procId" -ErrorAction SilentlyContinue
    if (-not $p) { return }
    if (-not $pattern -or ($p.CommandLine -and $p.CommandLine -match $pattern)) {
        taskkill /PID $procId /T /F *> $null
    } else {
        Log "PID $procId уже занят другим процессом — не убиваю" "warn"
    }
}

# Перезапуск через run_prod.ps1 -Only: он поднимает один компонент и не трогает
# остальные PID в pids.json.
function Restart-Component($state, [string]$name, [int]$procId, [string]$reason) {
    $cs = Get-CompState $state $name
    $now = Get-Date

    if ($cs.lastRestart) {
        try {
            $last = [datetime]::Parse([string]$cs.lastRestart)
            if (($now - $last).TotalSeconds -lt 60) {
                Log "${name}: рестарт был меньше минуты назад — пропускаем" "warn"
                return $false
            }
        } catch { }
    }
    $recent = @($cs.restarts | Where-Object {
        try { ([datetime]::Parse([string]$_)) -gt $now.AddHours(-1) } catch { $false }
    })
    if ($recent.Count -ge 4) {
        Log "${name}: $($recent.Count) рестартов за час — не зацикливаемся" "error"
        Send-Alert $state "restart-loop-$name" `
            "Argus: $name не поднимается" `
            "<p>Компонент <b>$name</b> перезапускали $($recent.Count) раз за час, он не работает.</p><p>Причина последней попытки: $reason</p><p>Логи: <code>deploy\logs\</code></p>"
        return $false
    }

    if ($DryRun) {
        Log "${name}: DryRun — перезапуск не выполняю (причина: $reason)" "warn"
        return $false
    }

    Log "${name}: перезапуск (причина: $reason)" "warn"
    Stop-Component $procId (Get-ComponentPattern $name)
    Start-Sleep -Seconds 2
    & (Join-Path $PSScriptRoot "run_prod.ps1") -Config $Config -Only $name -NoWait | Out-Null
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        Log "${name}: run_prod.ps1 -Only $name вернул код $code" "error"
        return $false
    }

    $cs.restarts = @($recent) + @($now.ToString("s"))
    $cs.lastRestart = $now.ToString("s")
    $cs.failStreak = 0
    $cs.noFrameStreak = 0
    Log "${name}: перезапущен" "ok"
    return $true
}

# ============================ проверки ============================

$pids = Get-Pids
$state = Read-State
$problems = 0

if ($pids.Count -eq 0) {
    Log "pids.json пуст — ищу процессы Argus по командной строке"
}

$backendPattern = '(java|mvn).*(argus|backend)'
$streamPattern  = 'stream_server\.py'
$mainPattern    = 'main\.py'

$backendPid = Get-Pid $pids "backend"
$streamPid  = Get-Pid $pids "stream"
$mainPid    = Get-Pid $pids "main"
if ($backendPid -le 0) { $backendPid = Find-ProcessId $backendPattern }
if ($streamPid  -le 0) { $streamPid  = Find-ProcessId $streamPattern }
if ($mainPid    -le 0) { $mainPid    = Find-ProcessId $mainPattern }

$backendAlive = Test-ProcessAlive $backendPid $backendPattern
$streamAlive  = Test-ProcessAlive $streamPid $streamPattern
$mainAlive    = Test-ProcessAlive $mainPid $mainPattern
$backendHttp  = Test-Http "http://127.0.0.1:8080/api/v1/stats"
$framesOk     = Test-Http "http://127.0.0.1:8090/frame" 10000

# --- backend ---
$bc = Get-CompState $state "backend"
if (-not $backendAlive) {
    $bc.failStreak++
    Log "backend: процесс не работает (PID $backendPid)" "error"
    if (-not (Restart-Component $state "backend" $backendPid "процесс не найден")) {
        $problems++
        if ($bc.failStreak -ge 2) {
            Send-Alert $state "backend-down" "Argus: backend не работает" `
                "<p>Процесс backend (журнал :8080) не запущен, перезапустить не удалось.</p><p>Проверь <code>deploy\logs\backend.err.log</code> и свободное место.</p>"
        }
    }
} elseif (-not $backendHttp) {
    $bc.failStreak++
    Log "backend: процесс жив, но /api/v1/stats не отвечает (подряд $($bc.failStreak))" "warn"
    if ($bc.failStreak -ge 2) {
        if (-not (Restart-Component $state "backend" $backendPid "не отвечает на :8080")) {
            $problems++
            Send-Alert $state "backend-hung" "Argus: backend не отвечает" `
                "<p>Процесс backend жив, но <code>/api/v1/stats</code> не отвечает дольше 10 минут.</p>"
        }
    }
} else {
    if ($bc.failStreak -gt 0) { Log "backend: восстановился" "ok" }
    $bc.failStreak = 0
}

# --- stream_server (релей камеры) ---
$sc = Get-CompState $state "stream"
if (-not $streamAlive) {
    $sc.failStreak++
    Log "stream: процесс не работает (PID $streamPid)" "error"
    if (-not (Restart-Component $state "stream" $streamPid "процесс не найден")) {
        $problems++
        if ($sc.failStreak -ge 2) {
            Send-Alert $state "stream-down" "Argus: релей камеры не работает" `
                "<p>stream_server.py (:8090) не запущен, перезапустить не удалось.</p><p>Проверь <code>deploy\logs\stream_server.log</code> и RTSP в config.prod.yaml.</p>"
        }
    }
} elseif ($framesOk) {
    if ($sc.failStreak -gt 0 -or $sc.noFrameStreak -gt 0) {
        Log "stream: кадры идут, счётчики сброшены" "ok"
    }
    $sc.failStreak = 0
    $sc.noFrameStreak = 0
} else {
    # Процесс жив, но кадров нет: чаще всего это камера/сеть, а не наш код.
    # Не дёргаем релей каждые 5 минут — ждём 3 прогона (15 минут).
    $sc.noFrameStreak++
    Log "stream: процесс жив, но /frame не отдаёт кадр (подряд $($sc.noFrameStreak))" "warn"
    if ($sc.noFrameStreak -ge 3) {
        if (Restart-Component $state "stream" $streamPid "нет кадров 15 минут") {
            $sc.noFrameStreak = 0
        } else {
            $problems++
            Send-Alert $state "no-frames" "Argus: нет кадров с камеры" `
                "<p>15+ минут нет кадров с камеры, перезапуск релея не помог.</p><p>Проверь питание/сеть камеры и доступность RTSP (камера доступна только через VPN).</p>" 2
        }
    }
}

# --- main.py (аналитика) ---
# Пока кадров нет, main.py молчит в лог — по свежести судить нельзя, проверяем
# только процесс. Когда кадры идут, статистика пишется каждые 10 с.
$mc = Get-CompState $state "main"
if (-not $mainAlive) {
    $mc.failStreak++
    Log "main: процесс не работает (PID $mainPid)" "error"
    if (-not (Restart-Component $state "main" $mainPid "процесс не найден")) {
        $problems++
        if ($mc.failStreak -ge 2) {
            Send-Alert $state "main-down" "Argus: аналитика не работает" `
                "<p>main.py не запущен, перезапустить не удалось.</p><p>Проверь <code>deploy\logs\main.err.log</code>.</p>"
        }
    }
} elseif (-not $framesOk) {
    Log "main: камера недоступна — проверку по логу пропускаю" "warn"
} else {
    $age = Get-LogAgeSec (Join-Path $logs "main.log")
    if ($age -le $LogStaleSec) {
        if ($mc.failStreak -gt 0) { Log "main: работает (лог свежий)" "ok" }
        $mc.failStreak = 0
    } else {
        $mc.failStreak++
        Log ("main: лог не обновлялся {0:N0} с (порог {1} с) — похоже, завис" -f $age, $LogStaleSec) "warn"
        if ($mc.failStreak -ge 2) {
            if (-not (Restart-Component $state "main" $mainPid "лог не обновляется")) {
                $problems++
                Send-Alert $state "main-hung" "Argus: аналитика зависла" `
                    "<p>main.py жив, но не пишет в лог дольше 10 минут при идущих кадрах.</p>"
            }
        }
    }
}

# --- свободное место ---
if ($MinFreeGb -gt 0) {
    try {
        $drive = (Get-Item $root).PSDrive.Name
        $freeGb = [math]::Round((Get-PSDrive -Name $drive).Free / 1GB, 1)
        if ($freeGb -lt $MinFreeGb) {
            $problems++
            Log "диск $drive`: свободно $freeGb ГБ (порог $MinFreeGb ГБ)" "error"
            Send-Alert $state "disk-low" "Argus: мало места на диске" `
                "<p>На диске <b>$drive`:</b> свободно <b>$freeGb ГБ</b> (порог $MinFreeGb ГБ).</p><p>Ночная очистка удаляет снимки старше <code>argus.retention.days</code>; проверь, что она отрабатывает.</p>" 12
        } else {
            Log "диск $drive`: свободно $freeGb ГБ" 
        }
    } catch {
        Log "не удалось проверить свободное место: $($_.Exception.Message)" "warn"
    }
}

Save-State $state
Log "итог: проблем $problems; backend=$backendAlive/$backendHttp stream=$streamAlive/$framesOk main=$mainAlive"

if ($deployFree) { $deployMutex.ReleaseMutex() | Out-Null }
if ($locked) { $mutex.ReleaseMutex() | Out-Null }

if ($problems -gt 0) { exit 1 }
exit 0
