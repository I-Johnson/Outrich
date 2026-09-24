"""Environment-backed application configuration.

Business-specific values intentionally live in the settings table. Environment
variables below are deployment credentials and safe operational defaults only.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
    SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    DATABASE_BACKEND = os.getenv("DATABASE_BACKEND", "supabase")
    DB_PATH = os.getenv("DB_PATH", str(ROOT / "data" / "outreach.db"))
    ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower().strip()
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
    SESSION_SECRET = os.getenv("SESSION_SECRET", os.getenv("SECRET_KEY", "change-me-session-secret"))
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
    SERP_PROVIDER = os.getenv("SERP_PROVIDER", "serper")
    SERP_API_KEY = os.getenv("SERP_API_KEY", "")
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    FREIGHT_AGENT_MODE = os.getenv("FREIGHT_AGENT_MODE", "model").strip().lower()
    EMAIL_PROVIDER = os.getenv("EMAIL_PROVIDER", "gmail")
    PINGRAM_API_KEY = os.getenv("PINGRAM_API_KEY", "")
    PINGRAM_API_URL = os.getenv("PINGRAM_API_URL", "https://api.pingram.io/email")
    PINGRAM_NOTIFICATION_TYPE = os.getenv("PINGRAM_NOTIFICATION_TYPE", "outreach")
    PINGRAM_FROM_EMAIL = os.getenv("PINGRAM_FROM_EMAIL", "")
    PINGRAM_FROM_DOMAIN = os.getenv("FROM_DOMAIN", "contractorops.ai")
    PINGRAM_REPLY_TO = os.getenv("REPLY_TO", "outreach@contractorops.ai")
    GMAIL_USER = os.getenv("GMAIL_USER", "")
    GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
    GMAIL_USER_2 = os.getenv("GMAIL_USER_2", "")
    GMAIL_APP_PASSWORD_2 = os.getenv("GMAIL_APP_PASSWORD_2", "")
    GMAIL_TRANSPORT = os.getenv("GMAIL_TRANSPORT", "auto").strip().lower()
    GMAIL_EDGE_FUNCTION = os.getenv("GMAIL_EDGE_FUNCTION", "send-gmail").strip()
    RAILWAY_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT", "").strip()
    DRY_RUN = _bool("DRY_RUN", True)
    SCHEDULER_ENABLED = _bool("SCHEDULER_ENABLED", True)
    SCHEDULER_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "60"))
    PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    CRAWLER_USER_AGENT = os.getenv("CRAWLER_USER_AGENT", "OutreachResearchBot/1.0 (+admin-only lead research)")
    MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "")

    @property
    def using_supabase(self) -> bool:
        return self.DATABASE_BACKEND != "sqlite"


settings = Settings()
