import asyncio
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from telethon import utils
from telethon.tl.types import PeerChannel

from app.collector import (
    authorized_account, export_anchors, publisher_message_ids, catch_up, confirm_additional_publisher, download_attachment,
    live_snapshot, persist_live, select_group,
)
from app.importer import import_export
from app.models import Attachment, Message, MessageRevision, SourceChat
from app.telegram_settings import TelegramSettings


def test_account_mismatch_stops_before_collection():
    class Client:
        async def is_user_authorized(self):
            return True

        async def get_me(self):
            return SimpleNamespace(id=123, bot=False)

    with pytest.raises(ValueError, match="differs"):
        asyncio.run(authorized_account(Client(), 456))


def test_live_snapshot_keeps_media_metadata():
    message = fake_message(10, "photo")
    message.photo = SimpleNamespace(id=987)
    message.media = object()
    message.file = SimpleNamespace(name=None, mime_type="image/jpeg", size=12, ext=".jpg")
    fields, attachments, _, raw = live_snapshot(message)
    assert fields["sender_telegram_user_id"] == 123
    assert (attachments[0].kind, attachments[0].telegram_media_id, attachments[0].local_status) == ("photo", 987, "pending")
    assert raw["media_id"] == 987


def fake_message(message_id, text, *, edited=None, sent=None):
    return SimpleNamespace(
        id=message_id, date=sent or datetime.now(timezone.utc), edit_date=edited,
        sender_id=123, fwd_from=None, file=None, media=None, photo=None,
        document=None, action=None, raw_text=text, reply_to_msg_id=None,
        grouped_id=None, entities=[],
    )


def test_binding_replay_edit_and_restart(tmp_path, monkeypatch):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration test")
    assert url != os.getenv("DATABASE_URL") and url.rsplit("/", 1)[-1].endswith("_test")
    chat_export_id = secrets.randbelow(2**31) + 2**32
    sent = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=4)
    raw_messages = [
        {"id": number, "type": "message", "date_unixtime": str(int((sent + timedelta(minutes=number - 1)).timestamp())),
         "from_id": "user123", "text": f"anchor {number}"}
        for number in (1, 2, 3)
    ]
    export = {"id": chat_export_id, "name": "test", "type": "private_group", "messages": raw_messages}
    path = tmp_path / "result.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    monkeypatch.setattr("app.collector.export_anchors", lambda _: (export, raw_messages))
    peer = PeerChannel(secrets.randbelow(2**31 - 1) + 1)
    marked_peer = utils.get_peer_id(peer)
    dialog = SimpleNamespace(entity=peer, is_group=True, title="test")

    class Client:
        def __init__(self, wrong=False):
            self.wrong = wrong
            self.late_edit = False

        async def get_messages(self, entity, ids):
            raw = next(item for item in raw_messages if item["id"] == ids)
            text = "wrong chat" if self.wrong else raw["text"]
            return fake_message(ids, text, sent=datetime.fromtimestamp(int(raw["date_unixtime"]), timezone.utc))

        def iter_messages(self, entity, *, min_id=0, reverse=False):
            async def items():
                candidates = [fake_message(4, "new one edited" if self.late_edit else "new one",
                                           sent=sent + timedelta(minutes=4),
                                           edited=sent + timedelta(minutes=7) if self.late_edit else None),
                              fake_message(5, "new two", sent=sent + timedelta(minutes=5))]
                for item in (candidates if reverse else reversed(candidates)):
                    if item.id > min_id:
                        yield item
            return items()

        async def download_media(self, message, file):
            Path(file).write_bytes(b"abc")
            return file

    async def exercise():
        db = create_async_engine(url)
        factory = async_sessionmaker(db, expire_on_commit=False)
        try:
            async with factory() as session:
                assert (await import_export(session, path)).created == 3
            async with factory() as session:
                with pytest.raises(ValueError, match="Group mismatch"):
                    await select_group(Client(wrong=True), 999, path, dialog, marked_peer, session)
            async with factory() as session:
                chat = await session.scalar(select(SourceChat).where(SourceChat.desktop_export_chat_id == chat_export_id))
                original_row = chat.id
                assert chat.telegram_peer_id is None
            async with factory() as session:
                chat = await select_group(Client(), 999, path, dialog, marked_peer, session)
                assert chat.id == original_row and chat.last_history_sync_id == 3
                chat.schedule_publisher_user_id = 999
                await session.commit()
            async with factory() as session:
                chat = await session.get(SourceChat, original_row)
                with pytest.raises(ValueError, match="did not match"):
                    await confirm_additional_publisher(chat, fake_message(1, "anchor 1"), 456, session)
                await confirm_additional_publisher(chat, fake_message(1, "anchor 1"), 123, session)
                await confirm_additional_publisher(chat, fake_message(1, "anchor 1"), 123, session)
                assert chat.schedule_publisher_user_id == 999
                assert chat.additional_schedule_publisher_user_ids == [123]
            async with factory() as session:
                chat = await session.get(SourceChat, original_row)
                assert chat.telegram_peer_id == marked_peer
                assert (await session.scalar(select(func.count()).select_from(SourceChat).where(
                    SourceChat.desktop_export_chat_id == chat_export_id
                ))) == 1

            original = fake_message(1, "anchor 1", sent=sent)
            async with factory() as session:
                assert (await persist_live(session, original_row, original, history=False))[0] == "unchanged"
            edited = fake_message(1, "corrected", sent=sent, edited=sent + timedelta(minutes=6))
            async with factory() as session:
                assert (await persist_live(session, original_row, edited, history=False))[0] == "changed"
            async with factory() as session:
                assert (await persist_live(session, original_row, edited, history=False))[0] == "unchanged"

            async with factory() as session:
                live_event_before_catch_up = (await session.get(SourceChat, original_row)).last_live_event_at

            async def process(message, *, history):
                async with factory() as session:
                    await persist_live(session, original_row, message, history=history)

            replay_client = Client()
            await catch_up(replay_client, peer, original_row, factory, process, 7)
            replay_client.late_edit = True
            await catch_up(replay_client, peer, original_row, factory, process, 7)
            await catch_up(replay_client, peer, original_row, factory, process, 7)
            async with factory() as session:
                chat = await session.get(SourceChat, original_row)
                assert chat.last_history_sync_id == 5
                assert chat.last_live_event_at == live_event_before_catch_up
                assert (await session.scalar(select(func.count()).select_from(Message).where(Message.chat_id == original_row))) == 5
                original_row_message = await session.scalar(select(Message).where(
                    Message.chat_id == original_row, Message.telegram_message_id == 1,
                ))
                assert original_row_message.current_revision == 2
                assert (await session.scalar(select(func.count()).select_from(MessageRevision).where(
                    MessageRevision.message_id == original_row_message.id,
                ))) == 2
                replayed = await session.scalar(select(Message).where(
                    Message.chat_id == original_row, Message.telegram_message_id == 4,
                ))
                assert replayed.text == "new one edited" and replayed.current_revision == 2

            photo = fake_message(6, "photo", sent=sent + timedelta(minutes=6))
            photo.photo = SimpleNamespace(id=987)
            photo.media = object()
            photo.file = SimpleNamespace(name=None, mime_type="image/jpeg", size=3, ext=".jpg")
            async with factory() as session:
                _, internal_id = await persist_live(session, original_row, photo, history=False)
            settings = TelegramSettings(
                api_id=1, api_hash="fake", session_path=tmp_path / "private/session",
                account_user_id=999, publisher_user_id=None,
                media_root=tmp_path / "private/media", media_max_bytes=100,
                reconcile_days=7,
            )
            await download_attachment(Client(), photo, internal_id, settings, factory)
            async with factory() as session:
                item = await session.scalar(
                    select(Attachment).join(MessageRevision).where(MessageRevision.message_id == internal_id)
                )
                assert (item.source_status, item.local_status, item.actual_size) == ("referenced", "present", 3)
                assert (settings.media_root / item.storage_key).read_bytes() == b"abc"

            large = fake_message(7, "large file", sent=sent + timedelta(minutes=7))
            large.document = SimpleNamespace(id=988)
            large.media = object()
            large.file = SimpleNamespace(name="large.pdf", mime_type="application/pdf", size=101, ext=".pdf")
            async with factory() as session:
                _, large_id = await persist_live(session, original_row, large, history=False)
            await download_attachment(Client(), large, large_id, settings, factory)
            async with factory() as session:
                item = await session.scalar(
                    select(Attachment).join(MessageRevision).where(MessageRevision.message_id == large_id)
                )
                assert (item.local_status, item.error_code) == ("not_downloaded", "too_large")
        finally:
            await db.dispose()

    asyncio.run(exercise())


def test_publisher_evidence_requires_distinct_reviewed_ids(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PUBLISHER_MESSAGE_IDS", raising=False)
    with pytest.raises(ValueError, match="at least three"):
        publisher_message_ids()
    monkeypatch.setenv("SCHEDULE_PUBLISHER_MESSAGE_IDS", "1,1,3")
    with pytest.raises(ValueError, match="distinct"):
        publisher_message_ids()
    monkeypatch.setenv("SCHEDULE_PUBLISHER_MESSAGE_IDS", "1,4,7")
    assert publisher_message_ids() == (1,4,7)


def test_export_fingerprint_is_private_and_checks_exact_bytes(tmp_path, monkeypatch):
    import hashlib
    path = tmp_path / "export.json"
    path.write_text(json.dumps({"messages": [{"id": i, "type": "message", "text": f"fictional {i}"} for i in range(1,4)]}))
    fingerprint = tmp_path / "fingerprint.json"
    fingerprint.write_text(json.dumps({"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    monkeypatch.setenv("EXPORT_FINGERPRINT_PATH", str(fingerprint))
    assert len(export_anchors(path)[1]) == 3
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="fingerprint differs"):
        export_anchors(path)
