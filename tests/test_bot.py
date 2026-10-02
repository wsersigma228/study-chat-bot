import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot import (BotSettings, HELP_TEXT, SCHEDULE_USAGE, can_read_schedule,
                     ignore_stale_message, parse_user_date, period, schedule_keyboard, split_day)


def test_access_is_limited_to_owner_and_allowed_destination():
    settings = BotSettings("demo", 101, frozenset({-202}), -1000000000303)
    def msg(sender, chat, kind):
        return SimpleNamespace(from_user=SimpleNamespace(id=sender), chat=SimpleNamespace(id=chat, type=kind))
    assert can_read_schedule(settings, msg(101, 101, "private"))
    assert not can_read_schedule(settings, msg(102, 102, "private"))
    assert can_read_schedule(settings, msg(102, -202, "group"))
    assert not can_read_schedule(settings, msg(101, -1000000000303, "supergroup"))


def test_source_cannot_be_a_destination(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "demo-token")
    monkeypatch.setenv("BOT_OWNER_USER_ID", "101")
    monkeypatch.setenv("SOURCE_CHAT_ID", "-1000000000303")
    monkeypatch.setenv("BOT_ALLOWED_CHAT_IDS", "-1000000000303")
    with pytest.raises(ValueError, match="cannot be used"):
        BotSettings.read()


def test_dates_navigation_and_english_help():
    assert parse_user_date("08-04-30") == date(2030, 4, 8)
    for value in ("31-02-30", "2030-04-08", None):
        with pytest.raises(ValueError, match=SCHEDULE_USAGE):
            parse_user_date(value)
    assert period("tomorrow", date(2030, 4, 5)) == date(2030, 4, 8)
    keyboard = schedule_keyboard(today=date(2030, 4, 8)).inline_keyboard
    assert keyboard[0][0].text == "Next week"
    assert keyboard[1][0].text == "Mon 08.04 · today"
    assert keyboard[1][1].text == "Tue 09.04 · tomorrow"
    assert "/schedule" in HELP_TEXT and "homework" in HELP_TEXT


def test_long_replies_preserve_content():
    parts = split_day("Homework", ["Algebra\n" + "a" * 9000, "Physics\nSolve exercise 1"])
    assert len(parts) >= 3 and all(len(part) <= 4000 for part in parts)
    assert sum(part.count("a") for part in parts) >= 9000
    assert parts[-1] == "Homework\n\nPhysics\nSolve exercise 1"


def test_stale_commands_ignored_but_review_reply_retained():
    calls = []
    async def handler(message, data):
        calls.append(message.text)
    old = SimpleNamespace(date=datetime.now(timezone.utc) - timedelta(hours=1), text="/schedule 08-04-30",
                          chat=SimpleNamespace(type="private"), reply_to_message=None)
    asyncio.run(ignore_stale_message(handler, old, {}))
    assert calls == []
    old.text = "Corrected assignment"
    old.reply_to_message = SimpleNamespace(message_id=1)
    asyncio.run(ignore_stale_message(handler, old, {}))
    assert calls == ["Corrected assignment"]
