"""Fictional publication scenarios; no live Telegram requests."""

import asyncio
import os
import secrets
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.bot import BotSettings
from app.channel import prepare_schedule_posts, publish_one
from app.homework import effective, sync_homework
from app.importer import import_export
from app.models import Homework, Message, SourceChat
from app.schedule import sync_schedule


def test_no_automatic_publication_without_configured_channel():
    def factory():
        raise AssertionError("No database access needed without a channel")
    asyncio.run(prepare_schedule_posts(factory, BotSettings("demo", 101, frozenset(), -1000000000303)))


@pytest.mark.parametrize("timeout", [False, True])
def test_publication_sends_once_and_keeps_timeouts_uncertain(monkeypatch, timeout):
    post = SimpleNamespace(id=1, status="pending", chat_id=1, schedule_date=date(2030, 4, 8),
        message_ids=[], content_hash=None, channel_id=-1000000000101, destination_type="channel", keyboard_date=date(2030, 4, 8))
    class Session:
        calls = 0
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        def begin(self):
            return self
        async def scalar(self, statement):
            self.calls += 1
            return post if self.calls == 1 else None
        async def get(self, model, key):
            return post
    async def render(*args):
        return ["Demo schedule"]
    monkeypatch.setattr("app.channel.render", render)
    class Bot:
        sends = 0
        async def get_chat(self, chat_id):
            return SimpleNamespace(type="channel")
        async def send_message(self, chat_id, text, **kwargs):
            self.sends += 1
            if timeout:
                raise TelegramNetworkError(method=SendMessage(chat_id=chat_id, text=text), message="demo timeout")
            return SimpleNamespace(message_id=11)
    bot = Bot()
    assert asyncio.run(publish_one(Session, bot, 1)) == ("uncertain" if timeout else "sent")
    assert post.status == ("uncertain" if timeout else "sent") and bot.sends == 1
    assert asyncio.run(publish_one(Session, bot, 1)) == ("skipped" if timeout else "unchanged")
    assert bot.sends == 1


@pytest.mark.parametrize("structured", [False, True])
def test_migrated_postgresql_replay_preserves_owner_changes(tmp_path, structured):
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL for PostgreSQL integration")
    assert url != os.getenv("DATABASE_URL") and url.rsplit("/", 1)[-1].endswith("_test")
    chat_number = secrets.randbelow(2**31) + 2**32
    raw = {"id": 1, "type": "message", "date_unixtime": "1901870400",
           "from_id": "user101", "text": "Algebra homework: solve exercise 1"}
    export = {"id": chat_number, "name": "Fictional replay test", "type": "private_group", "messages": [raw]}
    path = tmp_path / "demo.json"
    import json
    async def exercise():
        db = create_async_engine(url)
        chat_id = None
        try:
            path.write_text(json.dumps(export), encoding="utf-8")
            async with AsyncSession(db) as session:
                await import_export(session, path)
            async with AsyncSession(db) as session:
                await sync_homework(session)
            async with AsyncSession(db) as session:
                chat = await session.scalar(select(SourceChat).where(SourceChat.desktop_export_chat_id == chat_number))
                chat_id = chat.id
                task = await session.scalar(select(Homework).where(Homework.chat_id == chat_id))
                task_id = task.id
                override = ({"fields": {"text": "Owner correction"}, "meta": {}}
                            if structured else {"text": "Owner correction"})
                task.owner_override = override
                await session.commit()
            raw["text"] = "Geometry homework: draw a rectangle"
            raw["edited_unixtime"] = "1901870460"
            path.write_text(json.dumps(export), encoding="utf-8")
            async with AsyncSession(db) as session:
                await import_export(session, path)
            async with AsyncSession(db) as session:
                await sync_homework(session)
            async with AsyncSession(db) as session:
                task = await session.get(Homework, task_id)
                assert task.subject_key == "geometry" and effective(task, "text") == "Owner correction" and task.owner_override == override
                source = await session.scalar(select(Message).where(Message.chat_id == chat_id))
                source.deleted_at = datetime.now(timezone.utc)
                await session.commit()
            async with AsyncSession(db) as session:
                await sync_homework(session)
            async with AsyncSession(db) as session:
                task = await session.get(Homework, task_id)
                assert task.reason == "source_deleted" and effective(task, "text") == "Owner correction" and task.owner_override == override
        finally:
            if chat_id:
                async with AsyncSession(db) as session, session.begin():
                    chat = await session.get(SourceChat, chat_id)
                    await session.delete(chat)
            await db.dispose()
    asyncio.run(exercise())
