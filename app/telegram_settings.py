"""Read local Telegram settings without adding a dotenv dependency."""

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if value[:1] in {"'", '"'} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


# Shared timezone is initialized once before domain modules import it.
load_env()
LOCAL_TIME = ZoneInfo(os.getenv("STUDY_TIMEZONE", "UTC"))


@dataclass(frozen=True)
class TelegramSettings:
    api_id: int
    api_hash: str
    session_path: Path
    account_user_id: int | None
    publisher_user_id: int | None
    media_root: Path
    media_max_bytes: int
    reconcile_days: int

    @classmethod
    def read(cls) -> "TelegramSettings":
        load_env()
        api_id = os.getenv("TELEGRAM_API_ID", "")
        api_hash = os.getenv("TELEGRAM_API_HASH", "")
        if not api_id.isdigit() or not api_hash:
            raise ValueError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH locally in .env")
        session_path = Path(os.getenv("TELEGRAM_SESSION_PATH", "private/telegram.session"))
        private_root = Path("private").resolve()
        if not session_path.resolve().is_relative_to(private_root):
            raise ValueError("TELEGRAM_SESSION_PATH must be inside private/")
        media_root = Path(os.getenv("MEDIA_ROOT", "private/media"))
        if not media_root.resolve().is_relative_to(private_root):
            raise ValueError("MEDIA_ROOT must be inside private/")
        for name in ("TELEGRAM_ACCOUNT_USER_ID", "SCHEDULE_PUBLISHER_USER_ID"):
            value = os.getenv(name, "")
            if value and not value.isdigit():
                raise ValueError(f"{name} must be a numeric user ID")
        return cls(
            api_id=int(api_id), api_hash=api_hash, session_path=session_path,
            account_user_id=int(os.environ["TELEGRAM_ACCOUNT_USER_ID"]) if os.getenv("TELEGRAM_ACCOUNT_USER_ID") else None,
            publisher_user_id=int(os.environ["SCHEDULE_PUBLISHER_USER_ID"]) if os.getenv("SCHEDULE_PUBLISHER_USER_ID") else None,
            media_root=media_root,
            media_max_bytes=int(os.getenv("MEDIA_MAX_DOWNLOAD_MB", "25")) * 1024 * 1024,
            reconcile_days=int(os.getenv("RECONCILE_DAYS", "7")),
        )
