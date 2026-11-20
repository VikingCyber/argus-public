"""Диагностика SMTP-аутентификации: читает .env из этой папки и проверяет логин на сервере.

Запуск (из корня проекта):
    ./vision/.venv/Scripts/python.exe backend/smtp_test.py

Пароль НЕ печатается.
"""
import smtplib
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"


def load_env(path: Path) -> dict:
    vals = {}
    if not path.exists():
        print(f"ОШИБКА: файл {path} не найден")
        return vals
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            vals[key.strip()] = value.strip()
    return vals


def main() -> None:
    env = load_env(ENV_FILE)
    host = env.get("MAIL_SMTP_HOST", "")
    try:
        port = int(env.get("MAIL_SMTP_PORT", "465"))
    except ValueError:
        port = 465
    user = env.get("MAIL_SMTP_USERNAME", "")
    pwd = env.get("MAIL_SMTP_PASSWORD", "")
    use_ssl = env.get("MAIL_SMTP_SSL", "true").lower() in ("true", "1", "yes")

    print(f"SMTP: {host}:{port}  user={user}  SSL={use_ssl}")
    if not user or not pwd:
        print("ОШИБКА: MAIL_SMTP_USERNAME или MAIL_SMTP_PASSWORD пусты в .env")
        sys.exit(1)

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            server.starttls()
        server.login(user, pwd)
        print("OK: аутентификация SMTP прошла успешно")
        server.quit()
    except smtplib.SMTPAuthenticationError as e:
        print(f"ОШИБКА: сервер отверг логин/пароль (код {e.smtp_code})")
    except Exception as e:
        print(f"ОШИБКА: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()