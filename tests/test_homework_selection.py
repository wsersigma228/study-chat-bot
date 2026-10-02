from datetime import date
from types import SimpleNamespace

from app.bot import assignment_homework, day_assignments, lesson_block, source_fingerprint, source_references


def day(when, *subjects):
    return SimpleNamespace(schedule_date=date.fromisoformat(when), payload={"state": "verified",
        "source_ids": [10], "slots": [{"number": index, "subject_key": key, "raw_subject": key}
                                     for index, key in enumerate(subjects, 1)]})


def task(number, when, text, due=None, status="confirmed"):
    return (SimpleNamespace(id=number, root_message_id=number, subject_key="algebra", text=text,
        due_date=due, due_text=None, status=status, owner_override=None, attachments=[],
        sources=[{"message_id": number, "revision": 1, "role": "instruction"}]),
        None, date.fromisoformat(when))


def test_complete_interval_and_repeated_subjects():
    earlier, target = day("2030-04-08", "algebra"), day("2030-04-11", "algebra", "algebra")
    days = [earlier, day("2030-04-09", "physics"), day("2030-04-10", "english"), target]
    first, newer = task(1, "2030-04-08", "Solve exercise 1"), task(2, "2030-04-10", "Solve exercise 2")
    main, repeat = day_assignments(target, {"algebra": [newer, first]}, days)
    assert list(assignment_homework(main)) == [(newer, "likely"), (first, "likely")]
    assert repeat[3] and repeat[1] is None
    assert "teacher deadline unspecified" in lesson_block(main)
    assert {key for _, key in source_references(target, [main])} == {1, 2, 10}
    before = source_fingerprint(target.payload, [main])
    first[0].text = "Solve exercise 3"
    assert source_fingerprint(target.payload, [main]) != before


def test_explicit_deadline_wins_and_gaps_remain_uncertain():
    target = day("2030-04-11", "algebra")
    dated = task(1, "2030-04-08", "Solve exercise 1", due=target.schedule_date)
    newer = task(2, "2030-04-10", "Solve exercise 2")
    chosen = day_assignments(target, {"algebra": [newer, dated]}, [target])[0]
    assert chosen[1] == dated and chosen[4] == "explicit"
    chosen = day_assignments(target, {"algebra": [newer]}, [target])[0]
    assert chosen[4] == "uncertain" and "connection" in lesson_block(chosen).lower()
    newer[0].due_date = date(2030, 4, 12)
    assert day_assignments(target, {"algebra": [newer]}, [target])[0][1] is None


def test_new_unread_homework_does_not_replace_confirmed_content():
    target = day("2030-04-11", "algebra")
    confirmed = task(1, "2030-04-08", "Solve exercise 1")
    unread = task(2, "2030-04-10", "Unread photo", status="needs_review")
    selected = day_assignments(target, {"algebra": [unread, confirmed]}, [target])[0]
    assert selected[1] == confirmed and selected[2] == unread and selected[4] == "uncertain"
    text = lesson_block(selected)
    assert "Solve exercise 1" in text and "awaiting confirmation" in text
    assert "Unread photo" not in text
