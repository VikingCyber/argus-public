# Публикация публичного зеркала репозитория (https://github.com/VikingCyber/argus-public).
#
#   .\deploy\publish_public.ps1 -Tag v0.2.0 -DryRun   — собрать и проверить, не пушить
#   .\deploy\publish_public.ps1 -Tag v0.2.0          — опубликовать
#
# Зеркало собирается как один коммит без истории: в старой истории остались
# примеры с рабочим ключом, поэтому она не публикуется. Внутренние файлы
# (AGENTS.md, deploy.yml с self-hosted runner) в зеркало не попадают.
# Перед пушем дерево проверяется gitleaks — при находках публикация отменяется.
param(
    [Parameter(Mandatory)][string]$Tag,
    [string]$Remote = "https://github.com/VikingCyber/argus-public.git",
    [string]$Root = "",
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

$root = if ($Root) { $Root } elseif ($PSScriptRoot) { Split-Path -Parent $PSScriptRoot } else { "" }
if (-not $root) { throw "Не могу определить корень проекта: передай -Root" }

# В зеркало не идут: внутренние заметки и workflow выкатки на прод.
$exclude = @("AGENTS.md", ".github\workflows\deploy.yml")

$tmp = Join-Path ([IO.Path]::GetTempPath()) ("argus-public-" + (Get-Date -Format "yyyyMMddHHmmss"))
$url = "file:///" + ($root -replace '\\', '/')
Write-Host "[..] собираю зеркало из $root" -ForegroundColor DarkGray

try {
    git clone --depth 1 $url $tmp 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "git clone во временный каталог провалился" }

    foreach ($rel in $exclude) {
        $p = Join-Path $tmp $rel
        if (Test-Path $p) { Remove-Item $p -Force }
    }
    git -C $tmp add -A
    git -C $tmp -c user.name="Argus Publisher" -c user.email="noreply@localhost" `
        commit --amend -q -m "Argus: релиз $Tag" 2>&1 | Out-Null

    # Проверка на секреты: без неё публикация может вынести ключи наружу.
    if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
        gitleaks dir $tmp --no-banner --exit-code 7 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 7) { throw "gitleaks нашёл секреты в дереве — публикация отменена" }
        Write-Host "[ok] gitleaks: чисто" -ForegroundColor Green
    } else {
        Write-Host "[!] gitleaks не установлен — проверка на секреты пропущена" -ForegroundColor Yellow
    }

    $files = (git -C $tmp ls-files | Measure-Object).Count
    Write-Host ("[ok] зеркало собрано: {0} файлов, ревизия {1}" -f $files, (git -C $tmp rev-parse --short HEAD)) -ForegroundColor Green

    if ($DryRun) {
        Write-Host "[dry-run] запушить: main -> $Remote" -ForegroundColor Yellow
        Write-Host "[dry-run] создать тег: $Tag" -ForegroundColor Yellow
        exit 0
    }

    git -C $tmp tag -a $Tag -m "Argus: релиз $Tag"
    git -C $tmp remote set-url origin $Remote
    git -C $tmp push --force origin HEAD:main 2>&1 | Select-Object -Last 2
    if ($LASTEXITCODE -ne 0) { throw "не удалось запушить main" }
    git -C $tmp push --force origin $Tag 2>&1 | Select-Object -Last 1
    if ($LASTEXITCODE -ne 0) { throw "не удалось запушить тег" }

    # Тот же тег в исходном репозитории: по нему собирается bundle в комплекте.
    # Внимание: пуш тегов v* в приватный репозиторий запускает deploy.yml.
    git -C $root tag -f -a $Tag -m "Argus: релиз $Tag" 2>&1 | Out-Null
    Write-Host "[ok] опубликовано: $Remote (main и $Tag)" -ForegroundColor Green
} finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
