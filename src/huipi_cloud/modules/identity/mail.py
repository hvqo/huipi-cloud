"""Optional TLS SMTP delivery; reset links use a URL fragment and never enter access logs."""

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlsplit

from huipi_cloud.core.config import settings


def configured() -> bool:
    try:
        base = urlsplit(settings.auth_public_base_url)
        _ = base.port
    except ValueError:
        return False
    allowed_scheme = base.scheme == "https" or (
        settings.app_env == "development"
        and base.scheme == "http"
        and base.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    credentials_match = bool(settings.auth_smtp_user) == bool(
        settings.auth_smtp_password and settings.auth_smtp_password.get_secret_value()
    )
    return bool(
        allowed_scheme
        and base.hostname
        and not base.username
        and not base.password
        and base.path in {"", "/"}
        and not base.query
        and not base.fragment
        and settings.auth_smtp_host
        and settings.auth_smtp_sender
        and credentials_match
    )


def _deliver(email: str, token: str, purpose: str) -> None:
    path = "verify-email" if purpose == "verify_email" else "reset-password"
    url = settings.auth_public_base_url.rstrip("/") + f"/{path}#token={token}"
    message = EmailMessage()
    message["From"] = settings.auth_smtp_sender
    message["To"] = email
    message["Subject"] = "慧批云端邮箱验证" if purpose == "verify_email" else "慧批云端密码重置"
    message.set_content(
        "请在 30 分钟内打开以下链接完成操作。链接只能使用一次，请勿转发。\n\n"
        + url
        + "\n\n如果不是你本人操作，请忽略此邮件。"
    )
    with smtplib.SMTP(settings.auth_smtp_host, settings.auth_smtp_port, timeout=8) as smtp:
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
        smtp.ehlo()
        if settings.auth_smtp_user:
            smtp.login(settings.auth_smtp_user, settings.auth_smtp_password.get_secret_value())
        smtp.send_message(message)


async def send(email: str, token: str, purpose: str) -> None:
    if not configured():
        raise RuntimeError("mail_not_configured")
    await asyncio.to_thread(_deliver, email, token, purpose)
