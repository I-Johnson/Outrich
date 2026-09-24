from app.adapters.gmail_adapter import GmailProvider
from app.adapters.pingram_adapter import PingramProvider
from app.config import settings as env
from app.core.crypto import decrypt_secret
from app.core.gmail_senders import get_gmail_sender, sender_password


def get_provider(name: str, app_settings: dict, gmail_account: str = "1", gmail_sender: dict | None = None):
    if name == "gmail":
        sender = gmail_sender or get_gmail_sender(gmail_account, cfg=app_settings)
        if sender:
            password = sender_password(sender)
            if str(gmail_account) == "1":
                password = password or decrypt_secret(app_settings.get("gmail_app_password_encrypted", "")) or env.GMAIL_APP_PASSWORD
            elif str(gmail_account) == "2":
                password = password or env.GMAIL_APP_PASSWORD_2
            return GmailProvider(sender.get("email", ""), password, account_id=str(gmail_account))
        return GmailProvider("", "", account_id=str(gmail_account))
    return PingramProvider()
