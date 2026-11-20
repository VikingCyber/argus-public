# Деплой Argus на Windows Server

Схема: **репозиторий (GitHub) → сервер → `deploy\deploy.ps1` → процессы + логи.**
На сервере ничего не редактируется руками: код приезжает из git, настройки лежат в
`config.prod.yaml` и `backend\.env` (в git не попадают, обновление их не затирает).

```
БД PostgreSQL :5433 ──┐
                       ├── backend (Spring Boot, jar)  :8080   журнал событий
main.py (ANPR/tracking)┘        │
   │ читает кадры из релея      │ пишет события
   ▼                            │
stream_server.py ── единственный владелец RTSP камеры   :8090   /frame, /stream
```

## 0. Что должно стоять на сервере

| Компонент | Версия | Зачем |
|---|---|---|
| Python | 3.12 (x64) | vision |
| Git for Windows | любая | `git pull` при обновлении |
| JDK (Temurin) | 21 | сборка и запуск backend |
| Maven | 3.9+ | `mvn package` |
| PostgreSQL | 16+ (проверено на 18) | база, порт **5433** |

Ссылки: <https://www.postgresql.org/download/windows/>, <https://adoptium.net/>,
<https://maven.apache.org/download.cgi>

## 1. PostgreSQL (нативно, порт 5433)
Docker Desktop на Windows Server не нужен — это лишний слой и лишняя память.
Ставим нативный PostgreSQL инсталлятором и **меняем порт на 5433**, чтобы не конфликтовать
с другими службами сервера.

При установке задай:
- порт: `5433`
- пароль суперпользователя: свой (в `backend\.env`)

Создай базу и пользователя приложения:

```sql
CREATE USER argus WITH PASSWORD 'пароль_из_env';
CREATE DATABASE argus OWNER argus;
```

Проверка:
```powershell
Get-Service postgresql* | Start-Service
Test-NetConnection -ComputerName localhost -Port 5433
```

## 2. Код и окружение Python

```powershell
cd C:\argus
git clone --recurse-submodules <url-репозитория> .
```

Дальше — `.\deploy\deploy.ps1`: он сам создаст `vision\.venv` (Python 3.12), поставит
CPU-сборку torch и `requirements.txt`, соберёт backend и запустит всё. Отдельно
venv готовить не нужно.

Если окружение нужно поднять руками (например, для отладки), шаги такие:
```powershell
cd C:\argus\vision
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
# torch ставим CPU-сборкой: обычный pip тянет CUDA ~2.5 ГБ (на 16 ГБ это много)
.\.venv\Scripts\python -m pip install torch==2.13.0 torchvision==0.28.0 `
    --index-url https://download.pytorch.org/whl/cpu
.\.venv\Scripts\python -m pip install -r requirements.txt
```

Важно: `torch` нельзя просто оставить `pip install -r requirements.txt`. В
requirements.txt версия зафиксирована, но вариант сборки выбирает индекс, поэтому
CPU-шаг обязателен и должен идти первым. Если torch уже стоял, но оказался
CUDA-сборкой, `deploy.ps1` это заметит по `torch.version.cuda` и переустановит.

`anpr/` — git submodule (сторонний репозиторий), поэтому клон всегда с `--recurse-submodules`.
Веса детектора номера и OCR-модель лежат **внутри** submodule
(`anpr\models\yolo\model\best.pt`, `anpr\models\ocr_crnn\quant\crnn_ocr_model_int8_fx.pth`,
всего ~26 МБ) и приезжают вместе с клоном. `run_prod.ps1` проверяет их преflight-ом
и не стартует, если их нет, — иначе падение было бы через минуту после запуска.
Проверка, что окружение живое:
```powershell
.\.venv\Scripts\python -c "import torch, cv2, ultralytics; print(torch.__version__, torch.cuda.is_available())"
```

## 3. Настройки (в git не попадают)

```powershell
copy vision\config.prod.example.yaml vision\config.prod.yaml
copy backend\.env.example backend\.env
notepad vision\config.prod.yaml   # rtsp_url камеры
notepad backend\.env               # пароль БД, SMTP
```

Минимально в `config.prod.yaml`:
```yaml
cameras:
  - name: cam-01
    channel: 101
    fps: 25
    rtsp_url: "rtsp://пользователь:пароль@адрес_камеры:554/Streaming/Channels/101"
```

Профиль сервера уже настроен на слабый CPU без GPU: `yolov8n@640`, `anpr imgsz 1024`,
`relay_fps 6`, `live_nn: false` (обзор в браузере без распознавания, CPU занят только `main.py`).

### Фото событий (журнал и письма)

Фото складываются в `<корень репозитория>\data\images\<ГГГГ-ММ-ДД>\` и отдаются по
`/images/...`. Значение по умолчанию в `application.yml` — относительное (`data/images`),
поэтому раньше каталог зависел от того, откуда запущен backend: `java -jar` из
`backend\` писал в `backend\data\images`, запуск из `C:\argus` — в `C:\argus\data\images`.
После смены способа запуска старые события показывали 404 («видно только фото
текущего запуска»).

`run_prod.ps1` теперь всегда передаёт абсолютный `-Dargus.images-dir`
(`<корень>\data\images`). Переопределить можно в `backend\.env`:

```
ARGUS_IMAGES_DIR=C:\argus\data\images
```

Проверить, какой каталог реально используется, и где лежат старые фото:

```powershell
Select-String C:\argus\deploy\logs\backend.log -Pattern 'Каталог изображений'
Get-ChildItem C:\argus -Recurse -Directory -Filter images -Depth 3 |
    Select-Object FullName, @{n='Files';e={(Get-ChildItem $_.FullName -Recurse -File).Count}}
```

Если старые фото нашлись в другом каталоге — перенести их, не затирая новые:

```powershell
robocopy C:\argus\backend\data\images C:\argus\data\images /E /XC /XN /XO
```

(`/XC /XN /XO` — не перезаписывать уже существующие файлы.)

### Ссылка на снимок в письме (шара и веб)

По умолчанию в письме только локальный путь (`C:\argus\data\images\...`) — он
бесполезен получателю на другом компьютере. Два независимых варианта:

**1. Файловая шара (кликабельный UNC-путь, как в проводнике).**
Расшарьте каталог с фото и укажите его в `backend\.env`:

```powershell
# на сервере, один раз (доступ — только нужным учётным записям)
New-SmbShare -Name 'images$' -Path 'C:\argus\data\images' -ReadAccess 'EXAMPLE\Domain Users'
```

```
ARGUS_FILE_BASE_URL=\\192.168.1.10\images$
```

В письме появится строка «Снимок:» с кликабельной ссылкой:

```
\\192.168.1.10\images$\2026-10-05\cam-01-alert-20261005-162013-521.jpg
```

Внутри письма это `<a href="file://192.168.1.10/images$/...">`: Outlook и
Thunderbird открывают её в проводнике. Веб-почта (mail.ru, Яндекс) `file:`-ссылки
обычно блокирует — путь остаётся видимым текстом, его можно скопировать в
проводник. Шара должна быть доступна получателю (локальная сеть объекта или VPN).

**2. Веб-ссылка (открывается в браузере у любого получателя).**

```
ARGUS_PUBLIC_BASE_URL=http://192.168.1.10:8080
```

Можно задать оба — в письме будут обе ссылки. Пустое значение = ссылки нет.

После правки `backend\.env` — перезапуск (`stop_prod.ps1` / `run_prod.ps1`),
пересборка не нужна.

## 4. Первый запуск

Скрипты — `.ps1`, а на сервере по умолчанию стоит политика `Restricted` (запуск любых
сценариев запрещён). Один раз разреши для своей учётной записи:

```powershell
Get-ExecutionPolicy -List                    # что задано
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned -Force
```

`RemoteSigned` разрешает локальные скрипты, но запрещает неподписанные скачанные из
интернета — этого достаточно, файлы из git копируются локально. Если политику
задаёт групповая политика сервера и `Set-ExecutionPolicy` не помогает, запускай
скрипт в обход: `powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\deploy.ps1`.

**Не запускай скрипты через ПКМ → «Выполнить с помощью PowerShell»:** окно
закрывается мгновенно, и любая ошибка исчезает без следа. Запускай из уже
открытого окна PowerShell — тогда вывод и ошибки остаются на экране.

```powershell
cd C:\argus
.\deploy\deploy.ps1
```

Скрипт по порядку: гасит работающий Argus → `git pull --recurse-submodules` →
`pip install -r requirements.txt` → проверяет `import torch, cv2, ultralytics` →
`mvn package` → `run_prod.ps1` → проверяет `/frame` и `/api/v1/stats`.

Если что-то не так — логи в `deploy\logs\`:
```powershell
Get-Content deploy\logs\main.log -Tail 40
Get-Content deploy\logs\stream_server.log -Tail 40
Get-Content deploy\logs\backend.log -Tail 40
```

## 5. Ручной запуск/остановка

```powershell
.\deploy\run_prod.ps1      # старт (без окон, логи в deploy\logs\)
.\deploy\run_prod.ps1 -Only stream   # только релей :8090 (так делает supervise.ps1)
.\deploy\stop_prod.ps1     # мягкая остановка
.\deploy\stop_prod.ps1 -Force   # если что-то зависло
```

`-Only` (`all` | `backend` | `stream` | `main`) поднимает один компонент и
обновляет в `pids.json` только его PID — остальные процессы не трогаются.
Полный запуск (без `-Only`) снимает паузу авто-восстановления; `-Only` — нет.

## 6. Автозапуск и авто-восстановление

Две задачи Планировщика создаются одним скриптом (от администратора):

| Задача | Когда | Что делает |
|---|---|---|
| `Argus` | при старте сервера | `run_prod.ps1` — backend, stream_server, main.py |
| `Argus supervise` | каждые 5 минут | `supervise.ps1` — проверяет и перезапускает упавшее |

```powershell
cd C:\argus
.\deploy\install_tasks.ps1
.\deploy\install_tasks.ps1 -Remove   # удалить обе задачи
```

**Не запускай из RDP-сессии:** после выхода из RDP дочерние процессы могут быть убиты.
Обе задачи работают от SYSTEM, поэтому переживают выход из RDP и перезагрузку.

Проверка:
```powershell
Get-ScheduledTask -TaskName 'Argus*' | Get-ScheduledTaskInfo
Start-ScheduledTask -TaskName 'Argus supervise'
Get-Content deploy\logs\supervise.log -Tail 30
```

### Что делает надзор

- компонента нет в процессах → перезапуск через `run_prod.ps1 -Only <имя>`;
- backend жив, но `/api/v1/stats` не отвечает 2 прогона (10 мин) → перезапуск;
- stream жив, но `/frame` не отдаёт кадр 3 прогона (15 мин) → перезапуск релея + письмо;
- main жив и кадры идут, но `main.log` не обновлялся 120 с → перезапуск;
- больше 4 рестартов компонента за час → пауза и письмо (без зацикливания);
- свободного места меньше 5 ГБ → письмо не чаще раза в 12 ч.

Письма шлёт `deploy\notify.ps1` напрямую через SMTP из `backend\.env`, поэтому они
приходят, даже когда backend лежит. Нужны `MAIL_SMTP_*`, `MAIL_FROM`, `MAIL_TO`.
Письма о сбоях (супервизор, бэкапы) уходят с ключом `-Service` на адреса из
`MAIL_TO_NOTIFY`, чтобы не беспокоить заказчика; если переменная не задана —
используется `MAIL_TO`.

```powershell
.\deploy\notify.ps1 -Test -DryRun   # показать настройки, ничего не отправляя
.\deploy\notify.ps1 -Test           # отправить тестовое письмо
.\deploy\notify.ps1 -Test -Service -DryRun   # проверить адресата писем о сбоях
```

### Пауза на время обслуживания

`stop_prod.ps1` сам ставит `deploy\logs\supervise.paused` — иначе надзор поднял бы
всё обратно через 5 минут. Полный `run_prod.ps1` (без `-Only`) паузу снимает.

```powershell
New-Item deploy\logs\supervise.paused -Force   # пауза вручную
Remove-Item deploy\logs\supervise.paused       # снять
```

## 6b. Хранение снимков и журнала (ретеншн)

Backend сам удаляет старые данные раз в сутки (03:30) — отдельная задача не нужна.
Настройки в `application.yml`, переопределяются переменными в `backend\.env`:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `ARGUS_RETENTION_ENABLED` | `true` | включить очистку |
| `ARGUS_RETENTION_DAYS` | `90` | сколько дней хранить события и снимки |
| `ARGUS_RETENTION_MIN_FREE_GB` | `5` | порог свободного места для письма (0 = не проверять) |
| `ARGUS_RETENTION_CRON` | `0 30 3 * * *` | время запуска (03:30 по серверу) |

Удаляются строки `plate_events`/`alert_events` старше отсечки и каталоги
`images\YYYY-MM-DD` с датой раньше неё (день отсечки остаётся). Посторонние
каталоги в `images\` не трогаются.

Проверка без удаления (ничего не меняет):
```powershell
curl.exe -X POST "http://127.0.0.1:8080/api/v1/admin/retention?dryRun=true" `
    -H "X-API-Key: <ключ из config.prod.yaml>"
```

В ответе: сколько строк и файлов попадёт под очистку, какие каталоги и сколько
места освободится. Боевой запуск — тот же запрос с `dryRun=false`.

## 7. Обновление (обычная операция)

Руками — одна команда (она делает pull + зависимости + сборку + перезапуск + проверки):

```powershell
cd C:\argus
.\deploy\deploy.ps1
.\deploy\deploy.ps1 -SkipBuild   # если менялась только vision-часть
.\deploy\deploy.ps1 -Ref v2026.11.09-1   # выкатить/откатить конкретную ревизию
```

Проверка камеры (`/frame`) теперь **предупреждение, а не провал деплоя**: код и
backend могут обновиться, даже если камера недоступна (VPN/сеть объекта/обслуживание).
Если отсутствие кадров должно валить выкатку — добавь `-RequireCamera`.

Перед выкаткой `deploy.ps1` сам:
- останавливает работающий Argus;
- копирует `vision\config.prod.yaml` и `backend\.env` в `deploy\backup\<дата-время>\`;
- запоминает ревизию до деплоя в `deploy\logs\last-deploy.json`.

Откат на прошлую версию — одной строкой (ревизия печатается при провале деплоя
и лежит в `deploy\logs\last-deploy.json`):

```powershell
.\deploy\deploy.ps1 -Ref <предыдущая-ревизия>
```

Конфиги при откате не трогаются; бэкап из `deploy\backup\` можно вернуть копированием.

## 7a. Автоматический деплой из GitHub (без RDP)

Схема: **GitHub Actions → self-hosted runner на сервере → `deploy.ps1`**.
Работает и по кнопке, и по тегу. RDP нужен только для первичной установки runner'а.

### Установка runner'а (один раз, на сервере)

1. GitHub → репозиторий → **Settings → Actions → Runners → New self-hosted runner**
   → Windows x64. GitHub покажет ссылку на архив и токен.
2. На сервере:

```powershell
mkdir C:\actions-runner; cd C:\actions-runner
# скачать и распаковать архив, который показал GitHub, затем:
.\config.cmd --url https://github.com/VikingCyber/argus --token <ТОКЕН> `
             --labels argus-prod --runasservice
```

`--runasservice` ставит runner службой: он переживает выход из RDP и перезагрузку.
Метка `argus-prod` — та, по которой workflow находит этот сервер. Службу лучше
запускать от учётной записи с правами на `C:\argus` (Administrator), иначе
остановка/перезапуск процессов и чтение логов могут не сработать.

3. Проверка: **Settings → Actions → Runners** — runner должен быть `Idle` с метками
   `self-hosted, Windows, X64, argus-prod`.

### Как выкатывать

- **Кнопкой:** GitHub → **Actions → Deploy → Run workflow**. Можно оставить ревизию
  пустой (возьмётся `origin/main`) или указать тег/коммит. `skip_build` — если
  менялась только vision-часть; `require_camera` — если отсутствие кадров должно
  валить деплой.
- **Тегом:** `git tag v2026.11.09-1; git push origin v2026.11.09-1` — деплой
  запустится сам. Тег — это одновременно метка версии для откката.

Одновременно идёт только одна выкатка (`concurrency: production-deploy`,
`cancel-in-progress: false`): второе задание ждёт в очереди, а не рвёт начатую сборку.

### Если runner поставить нельзя

Запасной вариант — задача планировщика на сервере, раз в 10–15 минут:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\argus\deploy\autodeploy.ps1" `
    -WorkingDirectory "C:\argus"
Register-ScheduledTask -TaskName "Argus autodeploy" -Action $action `
    -Trigger (New-ScheduledTaskTrigger -Once -At (Get-Date) `
              -RepetitionInterval (New-TimeSpan -Minutes 10)) `
    -Principal (New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest)
```

Скрипт `autodeploy.ps1` делает `git fetch`, сравнивает `origin/main` с `HEAD` и,
если появились изменения, запускает `deploy.ps1 -SkipPull`. Минус: нет кнопки и
наглядного лога в GitHub; плюс: не нужны исходящие соединения GitHub → сервер,
только `git fetch`.


## 8. Бэкап БД

```powershell
$env:PGPASSWORD = 'пароль_из_env'
& 'C:\Program Files\PostgreSQL\18\bin\pg_dump.exe' -h localhost -p 5433 -U argus -Fc argus `
    > C:\argus\backup\argus-$(Get-Date -Format yyyyMMdd-HHmm).dump
```

Автобэкап раз в сутки — та же команда в задаче планировщика (At 03:00, повтор каждые 24 ч).

## 9. Брандмауэр

Открыть наружу (или только во внутреннюю сеть): `8080` (журнал) и `8090` (обзор камеры).
`5433` наружу не открывать — БД нужна только локально:

```powershell
New-NetFirewallRule -DisplayName "Argus backend" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
New-NetFirewallRule -DisplayName "Argus view"   -Direction Inbound -LocalPort 8090 -Protocol TCP -Action Allow
```

## 10. Если сломалось

| Симптом | Причина | Что делать |
|---|---|---|
| `[!] Кадры не пошли за 60с` | нет RTSP: неверный URL/канал/пароль | `Get-Content deploy\logs\stream_server.log -Tail 30` |
| `[!] БД не отвечает на :5433` | служба PostgreSQL не запущена | `Get-Service postgresql* \| Start-Service` |
| `[!] import torch не прошёл` | встал CUDA-пакет вместо CPU | см. п.2, поставить CPU-torch заново |
| `[!] Порт 8080 занят` | остался старый процесс | `.\deploy\stop_prod.ps1 -Force` |
| `! SacEvent ID 3033` в логе | Smart App Control блокирует `torch\_C*.pyd` | выключить Smart App Control (Панель управления → Безопасность) |
| Номера не читаются, кадры идут | нет весов из submodule или слабый кадр | `git submodule update --init --recursive`; проверить `anpr\models\yolo\model\best.pt` и `anpr\models\ocr_crnn\quant\crnn_ocr_model_int8_fx.pth` |
| Всё тормозит | два процесса делят CPU | в `config.prod.yaml` должно быть `live_nn: false` |
| Фото видно только за текущий запуск, старое — битое | backend стартовал из другого каталога, `data/images` резолвился по-разному | `Select-String deploy\logs\backend.log -Pattern 'Каталог изображений'`; задать `ARGUS_IMAGES_DIR` в `backend\.env`, старые файлы слить `robocopy` (см. п.3) |
| `supervise.log`: компонент «не работает» каждые 5 минут | компонент падает на старте | `Get-Content deploy\logs\backend.err.log -Tail 40` (или `main.err.log`, `stream_server.err.log`) |
| Письма-алерты не приходят | SMTP не настроен/неверный пароль | `.\deploy\notify.ps1 -Test -DryRun`, затем `.\deploy\notify.ps1 -Test` |
| После `stop_prod.ps1` всё поднялось само | нет паузы авто-восстановления | паузу ставит сам `stop_prod.ps1`; проверить `deploy\logs\supervise.paused` |
| Диск не освобождается | ретеншн выключен или порог дней слишком большой | `curl.exe -X POST "http://127.0.0.1:8080/api/v1/admin/retention?dryRun=true" -H "X-API-Key: <ключ из config.prod.yaml>"`; см. п.6b |
