"""Read one confirmed Telegram group. This module never sends a message."""

import asyncio
import getpass
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError

from app.db import engine
from app.importer import AttachmentData, _text, save_snapshot
from app.models import Attachment, Message, MessageRevision, SourceChat
from app.telegram_settings import TelegramSettings


def publisher_message_ids() -> tuple[int, ...]:
    values = os.getenv("SCHEDULE_PUBLISHER_MESSAGE_IDS", "").split(",")
    if len(values) < 3 or any(not value.strip().isdigit() or int(value) <= 0 for value in values):
        raise ValueError("Set at least three positive SCHEDULE_PUBLISHER_MESSAGE_IDS from your reviewed export")
    ids = tuple(int(value) for value in values)
    if len(set(ids)) != len(ids):
        raise ValueError("Publisher evidence message IDs must be distinct")
    return ids


def client_for(settings: TelegramSettings) -> TelegramClient:
    os.umask(0o077)
    settings.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    settings.session_path.parent.chmod(0o700)
    if settings.session_path.exists():
        settings.session_path.chmod(0o600)
    return TelegramClient(str(settings.session_path), settings.api_id, settings.api_hash, flood_sleep_threshold=0)


async def authorized_account(client: TelegramClient, expected_id: int | None, *, require_expected: bool = True) -> int:
    if not await client.is_user_authorized():
        raise ValueError("Session is not authorized; run login locally first")
    me = await client.get_me()
    if me is None or getattr(me, "bot", False):
        raise ValueError("A user account session is required")
    if require_expected and expected_id is None:
        raise ValueError("Set TELEGRAM_ACCOUNT_USER_ID in .env after login")
    if expected_id is not None and me.id != expected_id:
        raise ValueError("Authorized account ID differs from TELEGRAM_ACCOUNT_USER_ID; stopped")
    return me.id


async def login(settings: TelegramSettings) -> int:
    client = client_for(settings)
    try:
        await client.start(
            phone=lambda: input("Phone for local Telegram login: ").strip(),
            code_callback=lambda: getpass.getpass("Telegram code: "),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
        account_id = await authorized_account(client, settings.account_user_id, require_expected=False)
        return account_id
    finally:
        await client.disconnect()
        if settings.session_path.exists():
            settings.session_path.chmod(0o600)


def export_anchors(path: Path) -> tuple[dict, list[dict]]:
    content = path.read_bytes()
    expected = json.loads(Path(os.getenv("EXPORT_FINGERPRINT_PATH", "private/export/export_fingerprint.json")).read_text(encoding="utf-8"))["sha256"]
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Desktop export fingerprint differs from the reviewed export")
    export = json.loads(content)
    candidates = [m for m in export["messages"] if m.get("type") == "message" and m.get("text")]
    if len(candidates) < 3:
        raise ValueError("Export has fewer than three text anchors")
    return export, [candidates[0], candidates[len(candidates) // 2], candidates[-1]]


async def match_export(client, entity, export_path: Path, session: AsyncSession) -> SourceChat:
    """Require three exact historical anchors before binding the imported row."""
    export, anchors = export_anchors(export_path)
    chat = await session.scalar(select(SourceChat).where(SourceChat.desktop_export_chat_id == export["id"]))
    if chat is None:
        raise ValueError("This Desktop export has not been imported into the database")
    imported_count = await session.scalar(select(func.count()).select_from(Message).where(
        Message.chat_id == chat.id,
        Message.telegram_message_id.in_([item["id"] for item in export["messages"]]),
    ))
    if imported_count != len(export["messages"]):
        raise ValueError("The database does not contain every message from this Desktop export")
    for raw in anchors:
        live = await client.get_messages(entity, ids=raw["id"])
        if live is None or live.id != raw["id"]:
            raise ValueError(f"Group mismatch: message {raw['id']} is missing")
        if int(live.date.timestamp()) != int(raw["date_unixtime"]):
            raise ValueError(f"Group mismatch: message {raw['id']} has a different date")
        expected_sender = raw.get("from_id", "")
        if expected_sender.startswith("user") and live.sender_id != int(expected_sender[4:]):
            raise ValueError(f"Group mismatch: message {raw['id']} has a different sender")
        if (live.raw_text or "") != _text(raw.get("text", "")):
            raise ValueError(f"Group mismatch: message {raw['id']} has different text")
        stored = await session.scalar(select(Message).where(
            Message.chat_id == chat.id, Message.telegram_message_id == raw["id"],
        ))
        if stored is None:
            raise ValueError(f"Imported message {raw['id']} is missing from the database")
    return chat


async def group_dialogs(client) -> list:
    return [dialog async for dialog in client.iter_dialogs() if dialog.is_group]


def peer_id(dialog) -> int:
    return utils.get_peer_id(dialog.entity)


async def select_group(client, account_id: int, export_path: Path, dialog, confirmed_peer_id: int, session: AsyncSession):
    if not dialog.is_group:
        raise ValueError("Selected dialog is not a group")
    selected_peer = peer_id(dialog)
    if selected_peer != confirmed_peer_id:
        raise ValueError("Peer ID confirmation did not match; nothing saved")
    chat = await match_export(client, dialog.entity, export_path, session)
    if chat.telegram_peer_id is not None and chat.telegram_peer_id != selected_peer:
        raise ValueError("The imported chat is already bound to another peer")
    if chat.collector_account_user_id is not None and chat.collector_account_user_id != account_id:
        raise ValueError("The imported chat is bound to another account")
    existing = await session.scalar(select(SourceChat).where(SourceChat.telegram_peer_id == selected_peer))
    if existing is not None and existing.id != chat.id:
        raise ValueError("This Telegram peer is already bound to another export")
    chat.telegram_peer_id = selected_peer
    chat.collector_account_user_id = account_id
    chat.confirmed_at = datetime.now(timezone.utc)
    if chat.last_history_sync_id is None:
        chat.last_history_sync_id = await session.scalar(select(func.max(Message.telegram_message_id)).where(Message.chat_id == chat.id))
    await session.commit()
    return chat


async def confirmed_chat(session: AsyncSession, account_id: int, peer: int | None = None) -> SourceChat:
    query = select(SourceChat).where(
        SourceChat.confirmed_at.is_not(None),
        SourceChat.collector_account_user_id == account_id,
    )
    if peer is not None:
        query = query.where(SourceChat.telegram_peer_id == peer)
    rows = (await session.scalars(query)).all()
    if len(rows) != 1:
        raise ValueError("Expected exactly one confirmed group for this account")
    return rows[0]


async def confirm_publisher(client, entity, chat: SourceChat, export_path: Path, typed_user_id: int, session: AsyncSession):
    export, _ = export_anchors(export_path)
    by_id = {item["id"]: item for item in export["messages"]}
    ids = publisher_message_ids()
    senders = []
    for message_id in ids:
        raw = by_id.get(message_id)
        live = await client.get_messages(entity, ids=message_id)
        if raw is None or live is None or live.id != message_id:
            raise ValueError(f"Cannot verify schedule message {message_id}")
        expected = raw.get("from_id", "")
        if not expected.startswith("user") or live.sender_id != int(expected[4:]):
            raise ValueError(f"Schedule message {message_id} sender differs from export")
        senders.append(live.sender_id)
    if len(set(senders)) != 1 or typed_user_id != senders[0]:
        raise ValueError("Publisher ID confirmation did not match; nothing saved")
    chat.schedule_publisher_user_id = typed_user_id
    await session.commit()
    return ids


async def confirm_additional_publisher(chat: SourceChat, live, typed_user_id: int, session: AsyncSession):
    """Add an owner-confirmed sender evidenced by a saved message from this group."""
    stored = await session.scalar(select(Message).where(
        Message.chat_id == chat.id, Message.telegram_message_id == live.id,
    ))
    if (chat.schedule_publisher_user_id is None or live.sender_id != typed_user_id
            or stored is None or stored.sender_telegram_user_id != typed_user_id
            or stored.deleted_at is not None):
        raise ValueError("Publisher confirmation did not match the live and saved message")
    chat.additional_schedule_publisher_user_ids = sorted(set(
        [*(chat.additional_schedule_publisher_user_ids or []), typed_user_id]
    ) - {chat.schedule_publisher_user_id})
    await session.commit()


def live_snapshot(message) -> tuple[dict, list[AttachmentData], str, dict]:
    """Adapt Telethon message to the same stored fields as Desktop import."""
    sent = message.date.astimezone(timezone.utc)
    edited = message.edit_date.astimezone(timezone.utc) if message.edit_date else None
    sender_id = message.sender_id
    user_id = sender_id if isinstance(sender_id, int) and sender_id > 0 else None
    forwarded = message.fwd_from
    forwarded_peer = getattr(forwarded, "from_id", None) if forwarded else None
    forwarded_id = str(utils.get_peer_id(forwarded_peer)) if forwarded_peer else None
    forwarded_name = (getattr(forwarded, "from_name", None) or getattr(forwarded, "post_author", None)) if forwarded else None
    file = message.file
    attachments = []
    raw = {
        "origin": "telethon", "id": message.id,
        "sent_unixtime": int(sent.timestamp()),
        "edited_unixtime": int(edited.timestamp()) if edited else None,
        "sender_id": sender_id, "text": message.raw_text or "",
        "reply_to_message_id": message.reply_to_msg_id,
        "grouped_id": message.grouped_id,
        "media_type": type(message.media).__name__ if message.media else None,
        "forwarded_from_id": forwarded_id,
        "forwarded_from": forwarded_name,
        "entities": [
            {"type": type(item).__name__, "offset": item.offset, "length": item.length}
            for item in (message.entities or [])
        ],
    }
    if message.media and file:
        media_id = getattr(message.photo or message.document, "id", None)
        kind = "photo" if message.photo else "sticker" if getattr(file, "mime_type", None) == "image/webp" and getattr(message, "sticker", False) else "document"
        attachments.append(AttachmentData(
            ordinal=1, kind=kind, name=getattr(file, "name", None),
            mime_type=getattr(file, "mime_type", None), export_path=None,
            declared_size=getattr(file, "size", None), actual_size=None,
            source_status="referenced", local_status="pending",
            telegram_media_id=media_id,
        ))
        raw["media_id"] = media_id
        raw["media_kind"] = kind
    fields = {
        "telegram_message_id": message.id,
        "message_type": "service" if message.action else "message",
        "sent_at": sent, "edited_at": edited,
        "sender_ref": f"user{user_id}" if user_id is not None else str(sender_id) if sender_id is not None else None,
        "sender_telegram_user_id": user_id,
        "sender_name": None,
        "reply_to_telegram_id": message.reply_to_msg_id,
        "forwarded_from": forwarded_name,
        "forwarded_from_id": forwarded_id,
        "grouped_id": message.grouped_id,
        "text": message.raw_text or "",
    }
    payload_hash = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return fields, attachments, payload_hash, raw


async def persist_live(session: AsyncSession, chat_id: int, message, *, history: bool) -> tuple[str, int]:
    fields, attachments, payload_hash, raw = live_snapshot(message)
    async with session.begin():
        chat = await session.get(SourceChat, chat_id)
        if chat is None or chat.telegram_peer_id is None:
            raise ValueError("Collector group is not confirmed")
        status, internal_id = await save_snapshot(
            session, chat_id, fields, attachments, payload_hash, raw, origin="live",
        )
        if history:
            chat.last_history_sync_id = max(chat.last_history_sync_id or 0, message.id)
        else:
            chat.last_live_event_at = datetime.now(timezone.utc)
    return status, internal_id


async def mark_deleted(session: AsyncSession, chat_id: int, message_ids: list[int]):
    async with session.begin():
        rows = (await session.scalars(select(Message).where(
            Message.chat_id == chat_id, Message.telegram_message_id.in_(message_ids),
        ))).all()
        for item in rows:
            if item.deleted_at is None:
                item.deleted_at = datetime.now(timezone.utc)


async def download_attachment(client, message, internal_id: int, settings: TelegramSettings, session_factory):
    if not message.media or not message.file:
        return
    async with session_factory() as session:
        async with session.begin():
            attachment = await session.scalar(
                select(Attachment).join(MessageRevision).join(Message).where(
                    Message.id == internal_id,
                    MessageRevision.revision_no == Message.current_revision,
                ).order_by(Attachment.ordinal)
            )
            if attachment is None or attachment.kind == "sticker" or attachment.storage_key:
                return
            attachment.telegram_media_id = getattr(message.photo or message.document, "id", None)
            size = getattr(message.file, "size", None)
            if size is None or size > settings.media_max_bytes:
                attachment.local_status = "not_downloaded"
                attachment.error_code = "unknown_size" if size is None else "too_large"
                return
            revision_no = (await session.get(Message, internal_id)).current_revision
            suffix = getattr(message.file, "ext", None) or ""
            if not re.fullmatch(r"\.[A-Za-z0-9]{1,8}", suffix):
                suffix = ".bin"
            target_dir = settings.media_root / str(internal_id)
            target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = target_dir / f"{revision_no}{suffix.lower()}"
    if target.exists():
        result = target
    else:
        temporary = target.with_name(target.stem + ".part" + target.suffix)
        try:
            downloaded = await client.download_media(message, file=str(temporary))
            if downloaded is None:
                raise ValueError("media unavailable")
            result = Path(downloaded).resolve()
            if not result.is_relative_to(settings.media_root.resolve()):
                raise ValueError("download path escaped MEDIA_ROOT")
            if result.stat().st_size > settings.media_max_bytes:
                raise ValueError("media exceeded size limit")
            os.replace(result, target)
            result = target
        except Exception as exc:
            if temporary.exists():
                temporary.unlink()
            async with session_factory() as session:
                async with session.begin():
                    attachment = await session.get(Attachment, attachment.id)
                    attachment.local_status = "not_downloaded"
                    attachment.error_code = "flood_wait" if isinstance(exc, FloodWaitError) else type(exc).__name__[:50]
            return
    result.chmod(0o600)
    with result.open("rb") as media_file:
        digest = hashlib.file_digest(media_file, "sha256").hexdigest()
    async with session_factory() as session:
        async with session.begin():
            attachment = await session.get(Attachment, attachment.id)
            attachment.source_status = "referenced"
            attachment.local_status = "present"
            attachment.actual_size = result.stat().st_size
            attachment.storage_key = str(result.relative_to(settings.media_root))
            attachment.sha256 = digest
            attachment.error_code = None


async def catch_up(client, entity, chat_id: int, session_factory, process, reconcile_days: int):
    while True:
        async with session_factory() as session:
            chat = await session.get(SourceChat, chat_id)
            since_id = chat.last_history_sync_id or 0
        try:
            async for message in client.iter_messages(entity, min_id=since_id, reverse=True):
                await process(message, history=True)
            break
        except FloodWaitError as exc:
            print(f"Telegram requested a {exc.seconds}-second pause during history sync")
            await asyncio.sleep(exc.seconds)
    cutoff = datetime.now(timezone.utc) - timedelta(days=reconcile_days)
    while True:
        try:
            async for message in client.iter_messages(entity):
                if message.date < cutoff:
                    break
                await process(message, history=True)
            break
        except FloodWaitError as exc:
            print(f"Telegram requested a {exc.seconds}-second pause during reconciliation")
            await asyncio.sleep(exc.seconds)
    async with session_factory() as session:
        async with session.begin():
            chat = await session.get(SourceChat, chat_id)
            chat.last_history_sync_at = datetime.now(timezone.utc)


async def run_collector(client, entity, chat: SourceChat, settings: TelegramSettings, session_factory):
    """Register handlers before catch-up; serialize writes for this one group."""
    lock = asyncio.Lock()

    async def process(message, *, history: bool):
        async with lock:
            async with session_factory() as session:
                _, internal_id = await persist_live(session, chat.id, message, history=history)
            await download_attachment(client, message, internal_id, settings, session_factory)

    async def on_message(event):
        if event.chat_id == chat.telegram_peer_id:
            try:
                await process(event.message, history=False)
            except Exception as exc:
                print(f"Collector stopped after {type(exc).__name__}; restart to catch up")
                await client.disconnect()

    async def on_delete(event):
        if event.chat_id == chat.telegram_peer_id:
            try:
                async with lock:
                    async with session_factory() as session:
                        await mark_deleted(session, chat.id, event.deleted_ids)
            except Exception as exc:
                print(f"Collector stopped after {type(exc).__name__}; restart to catch up")
                await client.disconnect()

    client.add_event_handler(on_message, events.NewMessage(chats=entity))
    client.add_event_handler(on_message, events.MessageEdited(chats=entity))
    client.add_event_handler(on_delete, events.MessageDeleted(chats=entity))

    await catch_up(client, entity, chat.id, session_factory, process, settings.reconcile_days)

    async def reconcile_loop():
        while True:
            await asyncio.sleep(300)
            try:
                await catch_up(client, entity, chat.id, session_factory, process, settings.reconcile_days)
            except Exception as exc:
                print(f"Collector reconciliation failed ({type(exc).__name__}); reconnecting")
                await client.disconnect()
                return

    task = asyncio.create_task(reconcile_loop())
    try:
        print("Collector synchronized; waiting for updates. Ctrl+C to stop.")
        await client.run_until_disconnected()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
