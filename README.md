# Argus

Учёт проездов транспортных средств на весовом пункте / КПП: детекция машин,
журнал проездов, тревоги и живой обзор камеры в браузере.

Рассчитан на один сервер под Windows без GPU: CPU-инференс YOLOv8n, PostgreSQL,
веб-интерфейсы на портах 8080 (журнал) и 8090 (обзор камеры).

## Состав

| Каталог | Что внутри |
|---|---|
| `vision` | Python: обработка RTSP, детекция, трекинг, аннотации |
| `backend` | Java 21 / Spring Boot: API, журнал проездов, уведомления, веб-страницы |
| `deploy` | PowerShell: развёртывание, запуск, надзор, резервные копии |
| `anpr` | модуль распознавания номеров (git submodule) |

## Требования

- Windows Server x64, 16 ГБ ОЗУ
- Python 3.12 x64
- JDK 21, Maven 3.9+
- PostgreSQL 16+ на порту 5433, база `argus`

## Развёртывание

```powershell
git clone --recurse-submodules <адрес-репозитория> C:\argus
cd C:\argus
copy vision\config.prod.example.yaml vision\config.prod.yaml
copy backend\.env.example backend\.env
.\deploy\deploy.ps1
```

Подробно — `deploy\DEPLOY.md`: установка компонентов, первый запуск, обновление,
резервное копирование и восстановление.

## Проверка

```powershell
curl.exe -s -o NUL -w "%{http_code}" http://localhost:8080/api/v1/stats
curl.exe -s -o NUL -w "%{http_code}" http://localhost:8090/frame
```

## Безопасность

Пароль базы и ключ API живут только в `backend\.env` и `vision\config.prod.yaml`;
в репозитории их нет. Файлы `backend\.env`, `vision\config.prod.yaml` и веса
`vision\*.pt` не коммитятся.

## Лицензии

Проект использует сторонние компоненты с разными лицензиями, включая
Ultralytics YOLOv8 (AGPL-3.0). Подробности и обязательства — в `NOTICE.md`.
