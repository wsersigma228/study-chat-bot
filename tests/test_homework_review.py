from types import SimpleNamespace

import pytest

from app.homework_review import needs_owner_review, parse_correction


def test_english_correction_keeps_deadline_unspecified_unless_supplied():
    assert parse_correction("Write a short report", "literature") == {
        "status": "confirmed", "subject_key": "literature", "text": "Write a short report"}
    assert parse_correction("Subject: Geometry\nDraw a square\nDue: 10.04.2030", None) == {
        "status": "confirmed", "subject_key": "geometry", "text": "Draw a square",
        "due_date": "2030-04-10", "due_text": None}
    assert parse_correction("Draw a square\nDue: unspecified", "geometry")["due_date"] is None
    for value, subject in [("Write a report", None), ("Subject: Imaginary\nWrite a report", None),
                           ("Draw a square\nDue: tomorrow", "geometry"), ("", "geometry")]:
        with pytest.raises(ValueError):
            parse_correction(value, subject)


def test_approved_review_reopens_when_source_changes():
    item = SimpleNamespace(status="needs_review", reason="unread_photo", sources=[{"revision": 1}], owner_override=None)
    assert needs_owner_review(item)
    item.owner_override = {"status": "confirmed", "_approved_sources": item.sources.copy()}
    assert not needs_owner_review(item)
    item.sources = [{"revision": 2}]
    assert needs_owner_review(item)


@pytest.mark.parametrize("structured", [False, True])
def test_override_formats_preserve_values_and_source_invalidation(structured):
    from datetime import date
    from app.homework import effective, update_owner_override
    from app.study_view import format_homework

    sources = [{"message_id": 1, "revision": 1, "role": "instruction"}]
    fields = {"text": "Owner correction", "status": "confirmed", "due_date": None, "due_text": None}
    raw = ({"fields": fields, "meta": {"approved_sources": sources, "note": "keep"}}
           if structured else {**fields, "_approved_sources": sources})
    item = SimpleNamespace(status="needs_review", reason="unread_photo", sources=sources,
        owner_override=raw, subject_key="algebra", text="Unread photo", due_date=date(2030, 4, 10),
        due_text="next lesson", root_message_id=1, attachments=[])
    assert effective(item, "due_date") is None and effective(item, "due_text") is None
    assert not needs_owner_review(item)
    rendered = format_homework(item, None)
    assert rendered == ("\U0001f4cc Algebra: Owner correction\n"
                        "Latest homework found for the subject; deadline unspecified\n"
                        "Source: message 1\n\u2705 Homework confirmed")
    new = update_owner_override(item, {"subject_key": "geometry"})
    assert new["fields"]["text"] == "Owner correction" and new["fields"]["due_date"] is None
    assert "subject_key" not in fields and item.owner_override is raw
    if structured:
        assert new["meta"]["note"] == "keep"
    item.sources = [{"message_id": 1, "revision": 2, "role": "instruction"}]
    assert needs_owner_review(item) and effective(item, "text") == "Unread photo"
    assert effective(item, "due_date") == date(2030, 4, 10)
    item.owner_override = update_owner_override(item, {"status": "confirmed"})
    assert effective(item, "text") == "Owner correction" and not needs_owner_review(item)


@pytest.mark.parametrize("structured", [False, True])
@pytest.mark.parametrize("action", ["accept", "dismiss", "edit"])
def test_owner_actions_write_structured_override_and_keep_corrections(structured, action):
    import asyncio
    from datetime import datetime, timezone
    from app.homework import effective
    from app.homework_review import accept_review, dismiss_review, finish_edit
    from app.models import Homework

    stamp = datetime.now(timezone.utc)
    fields = {"due_date": None, "due_text": None, "text": "Previous correction"}
    raw = {"fields": fields.copy(), "meta": {"note": "keep"}} if structured else fields.copy()
    item = SimpleNamespace(status="needs_review", reason="subject_not_established", sources=[{"revision": 1}],
        owner_override=raw, subject_key="algebra", text="Source task", updated_at=stamp)
    notice = SimpleNamespace(status="awaiting_edit" if action == "edit" else "sent",
        notification_message_id=10, source_updated_at=stamp, homework_id=1)
    class Session:
        def begin(self):
            return self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def get(self, model, key, **kwargs):
            return item if model is Homework else notice
        async def scalar(self, statement):
            return notice
    if action == "edit":
        assert asyncio.run(finish_edit(Session(), 10, "Solve exercise 2"))[0]
    else:
        function = accept_review if action == "accept" else dismiss_review
        assert asyncio.run(function(Session(), 1, 10))
    assert set(item.owner_override) == {"fields", "meta"}
    assert item.owner_override["meta"]["approved_sources"] == item.sources
    assert item.owner_override["fields"]["due_date"] is None
    assert "status" not in raw.get("fields", raw)
    if structured:
        assert item.owner_override["meta"]["note"] == "keep"
    assert effective(item, "status") == ("dismissed" if action == "dismiss" else "confirmed")
    assert effective(item, "text") == {"accept": "Source task", "dismiss": "Previous correction",
                                       "edit": "Solve exercise 2"}[action]
