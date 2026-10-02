"""Import source snapshots without interpreting their educational meaning."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Message, MessageRevision, SourceChat


@dataclass(frozen=True)
class AttachmentData:
    ordinal: int
    kind: str
    name: str | None
    mime_type: str | None
    export_path: str | None
    declared_size: int | None
    actual_size: int | None
    source_status: str
    local_status: str
    telegram_media_id: int | None = None


@dataclass(frozen=True)
class ImportResult:
    created: int
    changed: int
    unchanged: int


def _date(value: str | int | None, label: str) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"Invalid {label} timestamp") from exc


def _text(value: str | list) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise ValueError("Message text must be a string or ordered entity array")
    parts = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        else:
            raise ValueError("Invalid text entity")
    return "".join(parts)


def _user_id(ref: str | None) -> int | None:
    if isinstance(ref, str) and ref.startswith("user") and ref[4:].isdigit():
        return int(ref[4:])
    return None


def _local_file(root: Path, reference: str) -> tuple[str, int | None]:
    path = PurePosixPath(reference)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return "invalid_path", None
    candidate = (root / Path(*path.parts)).resolve()
    if not candidate.is_relative_to(root.resolve()):
        return "invalid_path", None
    if not candidate.is_file():
        return "missing", None
    return "present", candidate.stat().st_size


def _attachment(raw: dict, field: str, ordinal: int, root: Path) -> AttachmentData:
    reference = raw[field]
    if not isinstance(reference, str):
        raise ValueError("Attachment reference must be a string")
    if reference.startswith("(File not included"):
        source_status = "not_included"
    elif reference.startswith("("):
        source_status = "unavailable"
    else:
        source_status = "referenced"
    if source_status == "referenced":
        local_status, actual_size = _local_file(root, reference)
        export_path = reference
    else:
        local_status, actual_size, export_path = "not_applicable", None, None
    kind = "photo" if field == "photo" else "sticker" if raw.get("media_type") == "sticker" else "document"
    name = Path(raw.get("file_name") or reference).name if source_status == "referenced" else raw.get("file_name")
    size = raw.get("photo_file_size" if field == "photo" else "file_size")
    return AttachmentData(
        ordinal=ordinal, kind=kind, name=name, mime_type=raw.get("mime_type"),
        export_path=export_path, declared_size=int(size) if size is not None else None,
        actual_size=actual_size, source_status=source_status, local_status=local_status,
    )


def normalize(raw: dict, root: Path) -> tuple[dict, list[AttachmentData], str]:
    """Normalize one Telegram Desktop JSON record; preserve raw fields separately."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), int):
        raise ValueError("Message must have a numeric id")
    sent_at = _date(raw.get("date_unixtime"), "date")
    if sent_at is None:
        raise ValueError(f"Message {raw['id']} has no Unix date")
    sender_ref = raw.get("from_id") or raw.get("actor_id")
    fields = {
        "telegram_message_id": raw["id"],
        "message_type": raw.get("type", "message"),
        "sent_at": sent_at,
        "edited_at": _date(raw.get("edited_unixtime"), "edited"),
        "sender_ref": sender_ref,
        "sender_telegram_user_id": _user_id(sender_ref),
        "sender_name": raw.get("from") or raw.get("actor"),
        "reply_to_telegram_id": raw.get("reply_to_message_id"),
        "forwarded_from": raw.get("forwarded_from"),
        "forwarded_from_id": raw.get("forwarded_from_id"),
        "grouped_id": raw.get("grouped_id"),
        "text": _text(raw.get("text", "")),
    }
    attachments = [
        _attachment(raw, field, ordinal, root)
        for ordinal, field in enumerate((field for field in ("photo", "file") if field in raw), 1)
    ]
    payload_hash = hashlib.sha256(
        json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return fields, attachments, payload_hash


async def save_snapshot(
    session: AsyncSession, chat_id: int, fields: dict, attachments: list[AttachmentData],
    payload_hash: str, raw_payload: dict, *, origin: str,
) -> tuple[str, int]:
    """Write one source revision. Caller owns the transaction and checkpoint."""
    message = await session.scalar(select(Message).where(
        Message.chat_id == chat_id,
        Message.telegram_message_id == fields["telegram_message_id"],
    ))
    if message is None:
        message = Message(chat_id=chat_id, current_revision=1, **fields)
        session.add(message)
        await session.flush()
        status = "created"
    else:
        if origin == "live":
            message.deleted_at = None
        current = await session.scalar(select(MessageRevision).where(
            MessageRevision.message_id == message.id,
            MessageRevision.revision_no == message.current_revision,
        ))
        if current.payload_hash == payload_hash or (
            origin == "live" and message.sent_at == fields["sent_at"]
            and message.edited_at == fields["edited_at"]
            and message.text == fields["text"]
            and message.sender_telegram_user_id == fields["sender_telegram_user_id"]
            and message.reply_to_telegram_id == fields["reply_to_telegram_id"]
        ):
            if message.grouped_id is None and fields["grouped_id"] is not None:
                message.grouped_id = fields["grouped_id"]
            if origin == "desktop":
                existing = (await session.scalars(select(Attachment).where(
                    Attachment.message_revision_id == current.id
                ).order_by(Attachment.ordinal))).all()
                for stored, observed in zip(existing, attachments, strict=True):
                    if not stored.storage_key:
                        stored.local_status = observed.local_status
                        stored.actual_size = observed.actual_size
            return "unchanged", message.id
        if message.edited_at is not None and (
            fields["edited_at"] is None or fields["edited_at"] < message.edited_at
        ):
            return "unchanged", message.id
        message.current_revision += 1
        for key, value in fields.items():
            setattr(message, key, value)
        status = "changed"

    revision = MessageRevision(
        message_id=message.id, revision_no=message.current_revision,
        payload_hash=payload_hash, text=fields["text"], raw_payload=raw_payload,
        source_edited_at=fields["edited_at"], received_at=datetime.now(timezone.utc),
    )
    session.add(revision)
    await session.flush()
    for item in attachments:
        session.add(Attachment(message_revision_id=revision.id, **vars(item)))
    return status, message.id


async def import_export(session: AsyncSession, export_path: Path) -> ImportResult:
    """Import one unpacked export atomically. Replaying the same snapshot is safe."""
    export = json.loads(export_path.read_text(encoding="utf-8"))
    if not isinstance(export, dict) or not isinstance(export.get("id"), int) or not isinstance(export.get("messages"), list):
        raise ValueError("Expected a Telegram Desktop chat export with numeric id and messages")
    raw_messages = export["messages"]
    ids = [item.get("id") for item in raw_messages if isinstance(item, dict)]
    if len(ids) != len(raw_messages) or any(not isinstance(item, int) for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Export contains invalid or duplicate message IDs")

    created = changed = unchanged = 0
    root = export_path.parent
    async with session.begin():
        chat = await session.scalar(select(SourceChat).where(SourceChat.desktop_export_chat_id == export["id"]))
        if chat is None:
            chat = SourceChat(desktop_export_chat_id=export["id"], name=export.get("name"), chat_type=export.get("type"))
            session.add(chat)
            await session.flush()
        else:
            chat.name, chat.chat_type = export.get("name"), export.get("type")

        for raw in raw_messages:
            fields, attachments, payload_hash = normalize(raw, root)
            status, _ = await save_snapshot(session, chat.id, fields, attachments, payload_hash, raw, origin="desktop")
            if status == "created":
                created += 1
            elif status == "changed":
                changed += 1
            else:
                unchanged += 1

    return ImportResult(created=created, changed=changed, unchanged=unchanged)
