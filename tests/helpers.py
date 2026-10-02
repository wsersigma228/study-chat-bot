"""Entirely fictional messages for offline checks."""

from datetime import datetime, timezone
from types import SimpleNamespace


def message(number, text, *, sender=101, sent=None, reply=None):
    return SimpleNamespace(
        id=number, telegram_message_id=number, text=text,
        sender_telegram_user_id=sender,
        sent_at=sent or datetime(2030, 4, 8, 12, number, tzinfo=timezone.utc),
        reply_to_telegram_id=reply, current_revision=1,
        message_type="message", deleted_at=None,
    )


def attachment(kind="photo", name=None):
    return SimpleNamespace(ordinal=1, kind=kind, name=name,
        source_status="referenced", local_status="not_downloaded",
        storage_key=None, sha256=None)
