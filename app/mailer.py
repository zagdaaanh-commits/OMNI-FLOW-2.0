"""Outbound email over SMTP (sign-up and password-reset verification codes).

Configuration (environment)
---------------------------
``SMTP_HOST``             Server host; empty disables sending.
``SMTP_PORT``             Default 465 when ``SMTP_SECURITY=ssl``, otherwise 587.
``SMTP_SECURITY``         ``ssl`` (implicit TLS), ``starttls`` or ``none``; default ``ssl`` on port 465, else ``starttls``.
``SMTP_USERNAME`` / ``SMTP_PASSWORD``   Login (skipped when the username is empty).
``SMTP_FROM``             Sender address; defaults to ``SMTP_USERNAME``.
``SMTP_TIMEOUT_SECONDS``  Connect / command timeout (default 15).
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from app.config import env_int, env_str, load_environment
from app.redaction import describe_exception

logger = logging.getLogger("omniflow.mailer")


class MailDeliveryError(RuntimeError):
    """The SMTP server could not be reached or refused the message."""


def is_configured() -> bool:
    load_environment()
    return bool(env_str("SMTP_HOST")) and bool(_sender())


def _sender() -> str:
    return env_str("SMTP_FROM", "SMTP_USERNAME")


def _security(port_hint: int | None = None) -> str:
    configured = env_str("SMTP_SECURITY").lower()
    if configured in {"ssl", "starttls", "none"}:
        return configured
    return "ssl" if port_hint == 465 else "starttls"


def send_email(to: str, subject: str, text: str) -> None:
    """Send a plain-text email; raises :class:`MailDeliveryError` on any SMTP failure."""
    load_environment()
    host = env_str("SMTP_HOST")
    if not host:
        raise MailDeliveryError("SMTP_HOST is not configured")
    explicit_port = env_int("SMTP_PORT", 0) or None
    security = _security(explicit_port)
    port = explicit_port or (465 if security == "ssl" else 587)
    timeout = env_int("SMTP_TIMEOUT_SECONDS", 15)
    username = env_str("SMTP_USERNAME")
    password = env_str("SMTP_PASSWORD")

    sender = _sender()
    name, address = parseaddr(sender)
    message = EmailMessage()
    message["From"] = formataddr((name or "OmniFlow", address or sender))
    message["To"] = to
    message["Subject"] = subject
    message.set_content(text)

    try:
        context = ssl.create_default_context()
        if security == "ssl":
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
        with client:
            if security == "starttls":
                client.starttls(context=context)
            if username:
                client.login(username, password)
            client.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Email to %s could not be sent: %s", _mask_address(to), describe_exception(exc))
        raise MailDeliveryError(type(exc).__name__) from exc


def _mask_address(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:2]}***@{domain}" if domain else "***"
