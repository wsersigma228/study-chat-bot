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
