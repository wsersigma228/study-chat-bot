import hashlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot import effective, format_homework, sendable_path
from app.homework import due_date, project
from tests.helpers import attachment, message


def test_english_homework_subjects_and_deadlines():
    root = message(1, "Homework due by 10.04.2030:\n1. Algebra: solve exercise 1\n2. Geometry: draw a triangle")
    tasks = project([root], {})
    assert [(item["subject_key"], item["due_date"], item["status"]) for item in tasks] == [
        ("algebra", date(2030, 4, 10), "confirmed"), ("geometry", date(2030, 4, 10), "confirmed")]
    root.text = "Algebra homework: complete exercise 3 before the next lesson"
    task = project([root], {})[0]
    assert task["due_date"] is None and task["due_text"] == "before the next lesson"
    assert due_date("Write about events dated 01.01.1900", root.sent_at) is None
    assert due_date("due 31.02.2030", root.sent_at) is None


def test_subject_uncertainty_and_unread_files_are_visible():
    rows = [message(1, "Homework: read chapter 1"),
            message(2, "Literature homework: answer the task at the end of the presentation", sender=102)]
    tasks = project(rows, {})
    assert tasks[0]["reason"] == "subject_not_established" and tasks[0]["status"] == "needs_review"
    assert tasks[1]["reason"] == "assignment_inside_unread_file" and tasks[1]["status"] == "needs_review"
    tasks = project([message(3, "")], {3: [attachment("document", "Physics for notes.pdf")]})
    assert tasks[0]["subject_key"] == "physics" and tasks[0]["status"] == "needs_review"


def test_reply_and_nearby_media_link_only_to_the_same_author():
    rows = [message(1, "Algebra homework: solve exercise 4"),
            message(2, "Also bring a notebook", reply=1),
            message(3, ""), message(4, "", sender=102),
            message(5, "Solve exercise 5", reply=1)]
    tasks = project(rows, {3: [attachment()], 4: [attachment()]})
    first = next(item for item in tasks if item["root_message_id"] == 1)
    assert "Also bring a notebook" in first["text"]
    assert [item["message_id"] for item in first["attachments"]] == [3]
    reply = next(item for item in tasks if item["root_message_id"] == 5)
    assert reply["subject_key"] == "algebra" and reply["status"] == "confirmed"
    assert any(source["message_id"] == 1 and source["role"] == "context" for source in reply["sources"])


def test_attachment_delivery_checks_path_and_hash(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    path = tmp_path / "demo.pdf"
    path.write_bytes(b"fictional document")
    media = {"kind": "document", "name": "demo.pdf", "message_id": 2,
             "bot_sendable": True, "local_status": "present", "storage_key": "demo.pdf",
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    assert sendable_path(media) == path
    assert sendable_path({**media, "storage_key": "../outside.pdf"}) is None
    assert sendable_path({**media, "sha256": "0" * 64}) is None
    path.write_bytes(b"changed")
    assert sendable_path(media) is None


def test_owner_approval_does_not_hide_changed_source():
    sources = [{"message_id": 1, "revision": 2, "role": "instruction"}]
    item = SimpleNamespace(root_message_id=1, subject_key="algebra", text="Solve exercise 1",
        due_date=None, due_text=None, status="needs_review", sources=sources, attachments=[],
        owner_override={"status": "confirmed", "text": "Owner correction",
                        "_approved_sources": [{"message_id": 1, "revision": 1, "role": "instruction"}]})
    assert effective(item, "status") == "needs_review"
    rendered = format_homework(item, None)
    assert "unconfirmed" in rendered and "Owner correction" not in rendered
    item.owner_override["_approved_sources"] = sources
    assert "Owner correction" in format_homework(item, None)
