import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import ScheduleDay
from app.schedule import LOCAL_TIME, displayed_day, estimate_ready, full_date, lesson, project, subject_aliases
from tests.helpers import message


def test_english_schedule_and_room_patch():
    rows = [message(1, "Schedule for today:\n1. Algebra - room 501\n2. English - gym"),
            message(2, "Lesson 1 will be in room 502 Geometry")]
    days, reviews, counts = project(SimpleNamespace(schedule_publisher_user_id=101), rows, subject_aliases())
    day = days[date(2030, 4, 8)]
    assert counts == {"full": 1, "patch": 1} and reviews == []
    assert [(slot["subject_key"], slot["room"]) for slot in day["slots"]] == [("geometry", "502"), ("english", "gym")]
    assert day["source_ids"] == [1, 2] and day["slots"][0]["source_ids"] == [1, 2]


def test_context_date_unknown_subject_and_untrusted_sender():
    question = message(1, "Schedule for tomorrow?")
    answer = message(2, "1. Algebra\n2. Programming")
    assert full_date(answer, question)[:3] == (date(2030, 4, 9), "context", [1])
    days, reviews, _ = project(SimpleNamespace(schedule_publisher_user_id=101), [question, answer], subject_aliases())
    assert days[date(2030, 4, 9)]["context_ids"] == [1] and not reviews
    answer.text = "Schedule for today:\n1. Algebra\n2. Unlisted Subject"
    answer.sender_telegram_user_id = 102
    days, reviews, _ = project(SimpleNamespace(schedule_publisher_user_id=101), [answer], subject_aliases())
    assert not days and "unknown_subject" in reviews[0].reason and "unconfirmed_publisher" in reviews[0].reason


def test_invalid_dates_and_numbers_do_not_become_verified():
    for text in ("Schedule for 31.02.2030:\n1. Algebra\n2. English",
                 "Schedule for today:\n1. Algebra\n3. English"):
        days, reviews, _ = project(SimpleNamespace(schedule_publisher_user_id=101), [message(1, text)], subject_aliases())
        assert not days and reviews
    assert lesson("Programming in room 505", subject_aliases())["subject_key"] == "programming"
    assert full_date(message(1, "Schedule for tomorrow"), None)[0] == date(2030, 4, 9)


@pytest.mark.parametrize("target", [date(2030, 4, 8), date(2030, 4, 9)])
def test_estimate_cutoff_and_weekends(target):
    days_before = 3 if target.weekday() == 0 else 1
    cutoff = datetime.combine(target - timedelta(days=days_before), datetime.min.time(), LOCAL_TIME).replace(hour=22)
    assert not estimate_ready(target, cutoff - timedelta(seconds=1))
    assert estimate_ready(target, cutoff)
    assert not estimate_ready(date(2030, 4, 6), cutoff)


def test_estimate_is_a_view_and_prefers_two_weeks():
    target = date(2030, 4, 8)
    past = ScheduleDay(chat_id=1, schedule_date=target - timedelta(days=14), payload={"state": "verified", "slots": []})
    class Session:
        answers = iter([None, past])
        async def scalar(self, statement):
            return next(self.answers)
    projected = asyncio.run(displayed_day(Session(), 1, target, datetime(2030, 4, 8, 12, tzinfo=LOCAL_TIME)))
    assert projected.payload["state"] == "estimated"
    assert projected.payload["estimate_source_date"] == past.schedule_date.isoformat()
    assert past.payload["state"] == "verified" and projected is not past
