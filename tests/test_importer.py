import asyncio
import json
import os
import secrets
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.importer import import_export, normalize
from app.models import Attachment, Message, MessageRevision, SourceChat


def test_synthetic_export_format():
    path = Path("examples/desktop-export.json")
    messages = json.loads(path.read_text(encoding="utf-8"))["messages"]
    normalized = [normalize(message, path.parent) for message in messages]
    assert len(normalized) == 8
    assert sum(len(attachments) for _, attachments, _ in normalized) == 1
    assert normalized[4][1][0].source_status == "unavailable"


def test_text_entities_keep_order_and_paths_stay_inside_export(tmp_path):
    raw = {
        "id": 10, "type": "message", "date_unixtime": "1788300000", "from_id": "user123",
        "text": ["A", {"type": "bold", "text": "B"}, "C"],
        "photo": "../outside.jpg", "photo_file_size": 10,
    }
    fields, attachments, _ = normalize(raw, tmp_path)
    assert fields["text"] == "ABC"
    assert fields["sender_telegram_user_id"] == 123
    assert attachments[0].local_status == "invalid_path"


def test_replay_edit_missing_reply_and_unavailable_file(tmp_path):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration test")
    assert url != os.getenv("DATABASE_URL"), "Integration tests need a separate database"
    assert url.rsplit("/", 1)[-1].endswith("_test"), "Use a dedicated *_test database"
    chat_id = secrets.randbelow(2**31) + 2**32
    path = tmp_path / "result.json"
    export = {
        "id": chat_id, "name": "test", "type": "private_group", "messages": [
            {
                "id": 10, "type": "message", "date_unixtime": "1788300000", "from_id": "user123",
                "text": "before", "reply_to_message_id": 999,
                "file": "(File unavailable, please try again later)",
                "file_name": "missing.pdf", "file_size": 42,
            },
            {
                "id": 11, "type": "message", "date_unixtime": "1788300060", "from_id": "user456",
                "text": ["A", {"type": "bold", "text": "B"}],
                "photo": "photos/demo.jpg", "photo_file_size": 3,
            },
        ],
    }

    async def exercise():
        engine = create_async_engine(url)
        try:
            path.write_text(json.dumps(export), encoding="utf-8")
            async with AsyncSession(engine) as session:
                assert (await import_export(session, path)).created == 2
                assert (await import_export(session, path)).unchanged == 2
                first = await session.scalar(
                    select(Message).join(SourceChat).where(
                        SourceChat.desktop_export_chat_id == chat_id,
                        Message.telegram_message_id == 10,
                    )
                )
                assert first.reply_to_telegram_id == 999  # No FK to a missing reply.
                assert first.current_revision == 1
                file = await session.scalar(
                    select(Attachment).join(MessageRevision).where(MessageRevision.message_id == first.id)
                )
                assert (file.source_status, file.local_status) == ("unavailable", "not_applicable")

            export["messages"][0]["text"] = "after"
            export["messages"][0]["edited_unixtime"] = "1788300100"
            path.write_text(json.dumps(export), encoding="utf-8")
            async with AsyncSession(engine) as session:
                result = await import_export(session, path)
                assert (result.created, result.changed, result.unchanged) == (0, 1, 1)
                first = await session.get(Message, first.id)
                revisions = (await session.scalars(
                    select(MessageRevision).where(MessageRevision.message_id == first.id).order_by(MessageRevision.revision_no)
                )).all()
                assert [revision.text for revision in revisions] == ["before", "after"]
                assert first.text == "after" and first.current_revision == 2

            stale = dict(export)
            stale["messages"] = [dict(item) for item in export["messages"]]
            stale["messages"][0]["text"] = "before"
            del stale["messages"][0]["edited_unixtime"]
            path.write_text(json.dumps(stale), encoding="utf-8")
            async with AsyncSession(engine) as session:
                assert (await import_export(session, path)).unchanged == 2
                assert (await session.get(Message, first.id)).text == "after"

            (tmp_path / "photos").mkdir()
            (tmp_path / "photos/demo.jpg").write_bytes(b"abc")
            path.write_text(json.dumps(export), encoding="utf-8")
            async with AsyncSession(engine) as session:
                assert (await import_export(session, path)).unchanged == 2
                local_photo = await session.scalar(
                    select(Attachment).where(Attachment.kind == "photo").order_by(Attachment.id.desc())
                )
                assert (local_photo.local_status, local_photo.actual_size) == ("present", 3)
                assert (await session.scalar(select(func.count()).select_from(MessageRevision).where(
                    MessageRevision.message_id == first.id
                ))) == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())
