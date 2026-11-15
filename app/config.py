"""Application configuration (env-driven, with sane defaults)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "WA Bulk Portal"
    version: str = "1.1.0"
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", BASE_DIR / "data")))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    # WasenderAPI
    wasender_base_url: str = os.getenv("WASENDER_BASE_URL", "https://www.wasenderapi.com")
    wasender_mock: bool = _bool("WASENDER_MOCK", False)  # simulate sending (for testing)

    # Portal login (leave empty to disable – only safe on your own PC)
    portal_password: str = os.getenv("PORTAL_PASSWORD", "")
    secret_key: str = os.getenv("SECRET_KEY", "change-me-please")

    default_country: str = os.getenv("DEFAULT_COUNTRY", "PK")
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "20"))

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'portal.db'}"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
