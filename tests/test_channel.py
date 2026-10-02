import asyncio
from datetime import date, datetime
from types import SimpleNamespace

from app.channel import chunks, publication_delay, render, units
from app.schedule import LOCAL_TIME


def test_split_preserves_emoji_and_source_links():
    parts = chunks("Demo schedule", ["Algebra", "📚" * 6000, "https://t.me/c/101/1"])
    assert len(parts) > 1 and all(units(part) <= 4096 for part in parts)
    assert sum(part.count("📚") for part in parts) == 6000
    assert any("https://t.me/c/101/1" in part for part in parts)


def test_refresh_wakes_at_estimate_cutoff():
    assert publication_delay(datetime(2030, 4, 8, 21, 59, 58, tzinfo=LOCAL_TIME)) == 2
    assert publication_delay(datetime(2030, 4, 8, 22, tzinfo=LOCAL_TIME)) == 10


def test_estimate_render_discloses_unconfirmed_date(monkeypatch):
    target = date(2030, 4, 8)
    day = SimpleNamespace(schedule_date=target, payload={"state": "estimated", "estimate_source_date": "2030-03-25",
        "source_ids": [1], "slots": [{"number": 1, "subject_key": "algebra", "raw_subject": "Algebra", "room": None}]})
    async def display(*args):
        return day
    async def history(*args):
        return {}
    async def chronology(*args):
        return []
    async def lines(*args):
        return ["Schedule source: demo message 1"]
    monkeypatch.setattr("app.channel.displayed_day", display)
    monkeypatch.setattr("app.channel.homework_history", history)
    monkeypatch.setattr("app.channel.schedule_rows", chronology)
    monkeypatch.setattr("app.channel.source_lines", lines)
    class Session:
        async def get(self, model, key):
            return SimpleNamespace(telegram_peer_id=-1000000000101, id=1)
    text = "\n".join(asyncio.run(render(Session(), SimpleNamespace(chat_id=1, schedule_date=target))))
    assert "Estimated schedule" in text and "25.03.2030" in text and "not confirmed" in text
    assert "1. Algebra" in text and "Sources:" in text
