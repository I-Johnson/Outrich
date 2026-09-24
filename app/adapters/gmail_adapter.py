import errno
import smtplib
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from bs4 import BeautifulSoup
import httpx

from app.adapters.base import EmailProvider, SendResult
from app.config import settings


HARD_BOUNCE_TEXT = (
    "5.1.1", "5.1.0", "user unknown", "no such user", "unknown recipient",
    "invalid recipient", "recipient address rejected", "address does not exist",
    "mailbox not found", "mailbox unavailable", "mailbox disabled",
)


def _recipient_failure(exc: smtplib.SMTPRecipientsRefused) -> SendResult:
    """Classify a recipient refusal without treating temporary 4xx replies as bounces."""
    replies = list((exc.recipients or {}).values())
    code = int(replies[0][0]) if replies and replies[0] else 0
    detail = replies[0][1] if replies and len(replies[0]) > 1 else str(exc)
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", errors="replace")
    message = str(detail)
    normalized = message.lower()
    hard = 500 <= code < 600 and any(marker in normalized for marker in HARD_BOUNCE_TEXT)
    return SendResult(
        False,
        error=f"SMTP {code}: {message}" if code else message,
        hard_bounce=hard,
        retryable=400 <= code < 500,
    )


class GmailProvider(EmailProvider):
    name = "gmail"
    def __init__(self, user: str, app_password: str, account_id: str = "1"):
        self.user = (user or "").strip()
        self.app_password = (app_password or "").replace(" ", "").strip()
        self.account_id = str(account_id or "1")

    def _use_edge_function(self) -> bool:
        return settings.GMAIL_TRANSPORT == "supabase" or (
            settings.GMAIL_TRANSPORT == "auto" and bool(settings.RAILWAY_ENVIRONMENT)
        )

    def _send_via_edge(self, *, to, subject, body, content_type, from_address, from_name, reply_to, headers=None):
        if not settings.SUPABASE_URL or not settings.SUPABASE_SERVICE_ROLE_KEY:
            return SendResult(False, error="Supabase URL or service-role key is missing", retryable=False)
        url = f"{settings.SUPABASE_URL}/functions/v1/{settings.GMAIL_EDGE_FUNCTION}"
        key = settings.SUPABASE_SERVICE_ROLE_KEY
        try:
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}", "apikey": key},
                json={
                    "to": to,
                    "subject": subject,
                    "body": body,
                    "content_type": content_type,
                    "from_address": from_address,
                    "from_name": from_name,
                    "reply_to": reply_to,
                    "account_id": self.account_id,
                    "gmail_user": self.user,
                    "gmail_app_password": self.app_password,
                    "message_id": (headers or {}).get("Message-ID"),
                    "in_reply_to": (headers or {}).get("In-Reply-To"),
                    "references": (headers or {}).get("References"),
                },
                timeout=35,
            )
            data = response.json() if response.content else {}
            if response.is_success and data.get("ok"):
                return SendResult(True, str(data.get("message_id") or ""))
            error = data.get("error") or f"Supabase email function returned HTTP {response.status_code}"
            retryable = data.get("retryable")
            return SendResult(
                False,
                error=str(error),
                hard_bounce=bool(data.get("hard_bounce", False)),
                retryable=bool(response.status_code >= 500 if retryable is None else retryable),
            )
        except Exception as exc:
            return SendResult(False, error=f"Supabase email function failed: {type(exc).__name__}: {exc}")

    def send(self, *, to, subject, body, content_type, from_address, from_name, reply_to, headers=None):
        if settings.DRY_RUN: return SendResult(True, (headers or {}).get("Message-ID") or make_msgid())
        if self._use_edge_function():
            return self._send_via_edge(
                to=to,
                subject=subject,
                body=body,
                content_type=content_type,
                from_address=from_address,
                from_name=from_name,
                reply_to=reply_to,
                headers=headers,
            )
        if not self.user or not self.app_password:
            return SendResult(False, error="Gmail user or app password is missing", retryable=False)
        msg = EmailMessage()
        msg["Message-ID"] = make_msgid()
        msg["To"] = to
        msg["From"] = formataddr((from_name or "ContractorOps", from_address or self.user))
        msg["Subject"] = subject
        if reply_to: msg["Reply-To"] = reply_to
        for key in ("Message-ID", "In-Reply-To", "References"):
            value = (headers or {}).get(key)
            if value:
                if key == "Message-ID":
                    msg.replace_header(key, value)
                else:
                    msg[key] = value
        if content_type == "html":
            msg.set_content(BeautifulSoup(body, "html.parser").get_text("\n", strip=True))
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        last_error = None
        for port, use_ssl in [(465, True), (587, False)]:
            try:
                if use_ssl:
                    with smtplib.SMTP_SSL("smtp.gmail.com", port, timeout=25) as smtp:
                        smtp.login(self.user, self.app_password)
                        smtp.send_message(msg)
                else:
                    with smtplib.SMTP("smtp.gmail.com", port, timeout=25) as smtp:
                        smtp.ehlo()
                        smtp.starttls()
                        smtp.ehlo()
                        smtp.login(self.user, self.app_password)
                        smtp.send_message(msg)
                return SendResult(True, msg.get("Message-ID", ""))
            except smtplib.SMTPRecipientsRefused as exc:
                return _recipient_failure(exc)
            except smtplib.SMTPAuthenticationError:
                return SendResult(
                    False,
                    error="Gmail rejected the login. Check the Gmail address and 16-character app password.",
                    retryable=False,
                )
            except OSError as exc:
                if exc.errno == errno.ENETUNREACH:
                    return SendResult(
                        False,
                        error=(
                            "SMTP network is unreachable. Railway blocks outbound SMTP on Free, Trial, "
                            "and Hobby plans; deploy the Supabase send-gmail function and set "
                            "GMAIL_TRANSPORT=supabase, use Railway Pro, or switch to Pingram."
                        ),
                        retryable=False,
                    )
                last_error = exc
                continue
            except Exception as exc:
                last_error = exc
                continue

        return SendResult(False, error=f"{type(last_error).__name__}: {last_error}")
