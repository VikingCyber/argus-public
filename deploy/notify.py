"""Служебные письма Argus без участия backend.

Нужен, когда backend лежит (упал, перезапускается) — поэтому SMTP-настройки
читаются напрямую из backend/.env, а не из Spring. Использует только stdlib.

    python notify.py --subject "Тема" --body "Текст"
    python notify.py --subject "Тема" --body-file body.html --html
    python notify.py --test
    python notify.py --subject "Тема" --body "Текст" --dry-run
    python notify.py --service --subject "Сбой" --body "Текст"

Получатели: MAIL_TO — обычные уведомления; с флагом --service берётся
MAIL_TO_NOTIFY (письма о сбоях), а если он не задан — снова MAIL_TO.

Коды выхода: 0 — отправлено (или dry-run), 2 — нет настроек/ошибка отправки.
"""

from __future__ import annotations

import argparse
import html
import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "backend" / ".env"

TRUTHY = {"1", "true", "yes", "on", "да"}


def load_env(path: Path) -> None:
    """Читает KEY=VALUE из .env. Уже заданные переменные окружения не трогает."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Служебное письмо Argus")
    parser.add_argument("--subject", default="", help="тема письма (без префикса)")
    parser.add_argument("--body", default="", help="текст письма (обычный текст)")
    parser.add_argument("--body-file", default="", help="файл с текстом письма")
    parser.add_argument("--html", action="store_true", help="body-file уже HTML")
    parser.add_argument("--dry-run", action="store_true", help="только показать, не отправлять")
    parser.add_argument("--test", action="store_true", help="тестовое письмо с настройками")
    parser.add_argument(
        "--service",
        action="store_true",
        help="служебное письмо о сбое: получатели из MAIL_TO_NOTIFY",
    )
    return parser.parse_args()


def parse_recipients(value: str) -> list[str]:
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


def read_body(args: argparse.Namespace) -> str:
    if args.body_file:
        return Path(args.body_file).read_text(encoding="utf-8-sig", errors="replace")
    return args.body


def to_html(text: str) -> str:
    escaped = html.escape(text)
    return (
        '<html><body style="font-family: Arial, sans-serif;">'
        f'<pre style="font-family: Consolas, monospace; white-space: pre-wrap;">{escaped}</pre>'
        "</body></html>"
    )


def main() -> int:
    args = parse_args()
    load_env(ENV_FILE)

    subject = args.subject
    body = read_body(args)
    if args.test:
        subject = subject or "Проверка уведомлений"
        body = body or (
            "Это тестовое письмо от supervise.ps1.\n"
            "Если вы его видите, алерты о падении компонентов дойдут."
        )

    host = env("MAIL_SMTP_HOST")
    port = int(env("MAIL_SMTP_PORT", "465") or "465")
    username = env("MAIL_SMTP_USERNAME")
    password = env("MAIL_SMTP_PASSWORD")
    sender = env("MAIL_FROM")
    recipients = parse_recipients(env("MAIL_TO"))
    if args.service:
        service_recipients = parse_recipients(env("MAIL_TO_NOTIFY"))
        if service_recipients:
            recipients = service_recipients
    use_ssl = env("MAIL_SMTP_SSL", "true").lower() in TRUTHY
    starttls = env("MAIL_SMTP_STARTTLS", "false").lower() in TRUTHY
    prefix = env("MAIL_SUBJECT_PREFIX", "Argus") or "Argus"

    if not subject:
        print("notify: не задана тема (--subject)", file=sys.stderr)
        return 2

    missing = [
        name
        for name, value in (
            ("MAIL_SMTP_HOST", host),
            ("MAIL_FROM", sender),
            ("MAIL_TO", ",".join(recipients)),
        )
        if not value
    ]
    if missing:
        print(
            "notify: не настроено: " + ", ".join(missing) + f" (см. {ENV_FILE})",
            file=sys.stderr,
        )
        return 2

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"{prefix}: {subject}"
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="argus.local")
    if args.html:
        message.set_content(body, subtype="html")
    else:
        message.set_content(to_html(body), subtype="html")

    if args.dry_run:
        print(f"dry-run: {host}:{port} ssl={use_ssl} starttls={starttls}")
        print(f"  to: {', '.join(recipients)}")
        print(f"  subject: {prefix}: {subject}")
        print(f"  body: {body.strip()[:200]}")
        return 0

    context = ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=context) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=20) as smtp:
                smtp.ehlo()
                if starttls:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 - нужен любой сбой SMTP
        print(f"notify: ошибка отправки: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(f"notify: отправлено на {', '.join(recipients)}: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
