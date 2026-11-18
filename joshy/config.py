"""Runtime settings for Joshy, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Resy's public web-client key. It ships inside resy.com's JavaScript bundle and is the
# same for every visitor. Override with JOSHY_RESY_API_KEY if Resy rotates it.
DEFAULT_RESY_API_KEY = "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader so we don't need python-dotenv. Existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    password: str = ""
    data_dir: Path = Path("./data")
    timezone: str = "America/New_York"
    poll_seconds: int = 60
    resy_api_key: str = DEFAULT_RESY_API_KEY

    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    pushover_token: str = ""
    pushover_user: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_to: str = ""

    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            password=_env("JOSHY_PASSWORD"),
            data_dir=Path(_env("JOSHY_DATA_DIR", "./data")),
            timezone=_env("JOSHY_TIMEZONE", "America/New_York"),
            poll_seconds=max(15, int(_env("JOSHY_POLL_SECONDS", "60") or 60)),
            resy_api_key=_env("JOSHY_RESY_API_KEY", DEFAULT_RESY_API_KEY),
            ntfy_topic=_env("JOSHY_NTFY_TOPIC"),
            ntfy_server=_env("JOSHY_NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
            pushover_token=_env("JOSHY_PUSHOVER_TOKEN"),
            pushover_user=_env("JOSHY_PUSHOVER_USER"),
            smtp_host=_env("JOSHY_SMTP_HOST"),
            smtp_port=int(_env("JOSHY_SMTP_PORT", "587") or 587),
            smtp_user=_env("JOSHY_SMTP_USER"),
            smtp_password=_env("JOSHY_SMTP_PASSWORD"),
            email_to=_env("JOSHY_EMAIL_TO"),
        )
