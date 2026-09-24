"""Check that a Gmail address and app password actually work.

Outrich sends through Gmail SMTP and reads broker replies over IMAP, so a
sender is only usable when both logins succeed. Nothing is sent.
"""
from __future__ import annotations

import imaplib
import logging
import smtplib
import socket
from dataclasses import dataclass

SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465
IMAP_HOST, IMAP_PORT = "imap.gmail.com", 993
TIMEOUT = 15
log = logging.getLogger(__name__)

BAD_LOGIN = ("Google didn't accept that email and app password. Use the 16-character app password "
             "from myaccount.google.com/apppasswords, not your normal Gmail password, and check that "
             "2-Step Verification is on.")
UNREACHABLE = "Couldn't reach Gmail just now. Wait a minute and try again."


@dataclass
class CheckResult:
    ok: bool
    message: str


def clean_app_password(value: str) -> str:
    return "".join((value or "").split())


def looks_like_app_password(value: str) -> bool:
    return len(value) == 16 and value.isalnum()


def check_gmail_login(email: str, app_password: str) -> CheckResult:
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=TIMEOUT) as smtp:
            smtp.login(email, app_password)
    except smtplib.SMTPAuthenticationError:
        return CheckResult(False, BAD_LOGIN)
    except (OSError, socket.timeout, smtplib.SMTPException) as exc:
        log.warning("Gmail SMTP check failed for %s: %r", email, exc)
        return CheckResult(False, UNREACHABLE)
    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=TIMEOUT) as mailbox:
            mailbox.login(email, app_password)
            mailbox.logout()
    except imaplib.IMAP4.error:
        return CheckResult(False, "Sending works, but Gmail refused the inbox connection Outrich uses to read broker replies. "
                                  "Make sure IMAP isn't turned off in Gmail settings (Forwarding and POP/IMAP), then try again.")
    except (OSError, socket.timeout) as exc:
        log.warning("Gmail IMAP check failed for %s: %r", email, exc)
        return CheckResult(False, UNREACHABLE)
    return CheckResult(True, "Connected. Outrich can send from this Gmail and read broker replies.")
