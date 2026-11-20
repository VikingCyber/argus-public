# Запуск бэкенда Argus.
# Читает секреты из .env и передаёт их в приложение как переменные окружения,
# затем запускает Spring Boot через Maven.

$ErrorActionPreference = "Stop"

$backendDir = $PSScriptRoot
$envFile = Join-Path $backendDir ".env"

if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$') {
            Set-Item -Path ("Env:" + $matches[1]) -Value $matches[2]
        }
    }
    Write-Host "[argus] Загружены настройки из .env"
} else {
    Write-Host "[argus] .env не найден. Скопируйте .env.example в .env и заполните SMTP-данные."
}

mvn -f (Join-Path $backendDir "pom.xml") spring-boot:run