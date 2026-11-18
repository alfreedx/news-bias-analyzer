"""Runtime settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def _int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real env vars take priority."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", BASE_DIR / "var")))
    catalog_dir: Path = BASE_DIR / "data"
    # Ingestion
    ingest_enabled: bool = field(default_factory=lambda: _bool("INGEST_ENABLED", True))
    ingest_interval_minutes: int = field(default_factory=lambda: _int("INGEST_INTERVAL_MINUTES", 20, 5))
    retention_days: int = field(default_factory=lambda: _int("RETENTION_DAYS", 120, 7))
    user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "USER_AGENT", "Mozilla/5.0 (compatible; NewsBiasAnalyzer/1.0; open-source RSS reader)"
        )
    )
    # Optional live provider. Google's RSS terms only allow personal,
    # non-commercial use, so it is off by default.
    google_news_enabled: bool = field(default_factory=lambda: _bool("GOOGLE_NEWS_ENABLED", False))
    # HTTP / security
    allowed_hosts: list[str] = field(default_factory=lambda: _list("ALLOWED_HOSTS", "localhost,127.0.0.1"))
    https_only: bool = field(default_factory=lambda: _bool("HTTPS_ONLY", False))
    rate_limit_per_minute: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MINUTE", 30, 1))
    trust_proxy_headers: bool = field(default_factory=lambda: _bool("TRUST_PROXY_HEADERS", False))
    site_url: str = field(default_factory=lambda: os.environ.get("SITE_URL", "http://localhost:8000"))
    repo_url: str = field(
        default_factory=lambda: os.environ.get("REPO_URL", "https://github.com/alfreedx/news-bias-analyzer")
    )

    @property
    def db_path(self) -> Path:
        return self.data_dir / "news.sqlite3"


settings = Settings()
