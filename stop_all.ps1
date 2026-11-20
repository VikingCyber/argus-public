# Argus: остановка всех компонентов (БД остаётся работать).
$ErrorActionPreference = "SilentlyContinue"

# Graceful, а не -Force сходу: сначала мягкий сигнал (процесс успевает
# завершиться штатно — Spring Boot/Maven без «BUILD FAILURE: exit code -1»
# в лог, python успевает закрыть файлы), потом ожидание, и лишь затем force.
function Stop-Gracefully([object[]]$ids) {
    foreach ($id in $ids) {
        Stop-Process -Id $id -ErrorAction SilentlyContinue
    }
    $deadline = (Get-Date).AddSeconds(8)
    while ((Get-Date) -lt $deadline) {
        $alive = $ids | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue }
        if (-not $alive) { break }
        Start-Sleep 0.5
    }
    foreach ($id in $ids) {
        Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
    }
}

# Vision-процессы
$pids = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match '\\argus\\vision' } |
    ForEach-Object { $_.ProcessId })
if ($pids.Count -gt 0) {
    Stop-Gracefully $pids
}
Write-Host "[ok] vision-процессов остановлено: $($pids.Count)"

# Backend (java на :8080)
$conn = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($conn) {
    $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($p -and $p.ProcessName -eq 'java') {
        Stop-Gracefully @($p.Id)
        Write-Host "[ok] backend остановлен"
    } else {
        Write-Host "[!] Порт 8080 занят не-backend процессом: $($p.ProcessName) — не трогаю." -ForegroundColor Yellow
    }
} else {
    Write-Host "[i] backend не был запущен"
}

Write-Host "[ok] Готово. БД (argus-db в Docker) оставлена работать."