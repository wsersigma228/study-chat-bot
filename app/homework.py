"""Conservative homework projection from saved source revisions."""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Attachment, Homework, Message, MessageRevision, SourceChat
from app.schedule import LOCAL_TIME, subject_aliases

CUE = re.compile(r"(?:\bhomework\b|\bhw\b|\bassignment\b|\bexercise\s*\d|\bN\s*\d|\bWB\b|\blearn\b|\bcomplete\b|\bretell\b|\bnotes\b|\banswer\b|\bwrite\b|\bread\b|\bsolve\b|\bdraw\b)", re.I)
ACTION = re.compile(r"(?:\blearn\b|\bcomplete\b|\bretell\b|\bnotes\b|\banswer\b|\bwrite\b|\bread\b|\bsolve\b|\bdraw\b|\bWB\b|\bexercise\s*\d|\bN\s*\d)", re.I)
DATE = re.compile(r"(?:\bby|\bdue|\bsubmit\s+by|\bfor)\s+(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?(?=\D|$)", re.I)
RELATIVE_DUE = re.compile(r"\b(?:by|for|before|due(?:\s+by)?)\s+(?:the\s+)?next\s+(?:lesson|class)\b", re.I)
SPLIT = re.compile(r"^\s*(?:\d+[.)]|[-•])\s*(.+)$")
POINTER = re.compile(r"(?:homework|hw)\s*[👆↑\s]*", re.I)


def catalog():
    data = json.loads(Path("config/subjects.json").read_text(encoding="utf-8"))
    names = {item["key"]: item["name"] for item in data["subjects"]}
    aliases = subject_aliases()
    patterns = [(re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.I), key)
                for name, key in sorted(aliases.items(), key=lambda pair: -len(pair[0])) if len(name) > 2]
    return names, patterns


def explicit_subject(text: str, patterns) -> str | None:
    found = {key for pattern, key in patterns if pattern.search(text)}
    return next(iter(found)) if len(found) == 1 else None


def due_date(text: str, sent_at: datetime) -> date | None:
    for match in DATE.finditer(text):
        day, month, year = match.groups()
        year = int(year) if year else sent_at.astimezone(LOCAL_TIME).year
        if year < 100:
            year += 2000
        try:
            return date(year, int(month), int(day))
        except ValueError:
            pass
    return None


def profile_subjects(messages: list[Message]) -> dict[int, list[str]]:
    profiles = json.loads(Path("config/source_profiles.json").read_text(encoding="utf-8"))["profiles"]
    by_id = {item.telegram_message_id: item for item in messages}
    result = {}
    for profile in profiles:
        anchor = by_id.get(profile["anchor_message_id"])
        if anchor and anchor.sender_telegram_user_id and not profile["approval_required"]:
            result[anchor.sender_telegram_user_id] = profile["candidate_subjects"]
    return result


def attachment_record(message: Message, attachment: Attachment) -> dict:
    return {
        "message_id": message.telegram_message_id, "revision": message.current_revision,
        "ordinal": attachment.ordinal, "kind": attachment.kind, "name": attachment.name,
        "source_status": attachment.source_status, "local_status": attachment.local_status,
        "bot_sendable": attachment.local_status == "present" and bool(attachment.storage_key),
        "storage_key": attachment.storage_key, "sha256": attachment.sha256,
    }


def project(messages: list[Message], attachments: dict[int, list[Attachment]]) -> list[dict]:
    """Use explicit text first; context only attaches same-author nearby media or replies."""
    _, patterns = catalog()
    profiles = profile_subjects(messages)
    active = sorted((m for m in messages if m.message_type == "message" and m.deleted_at is None),
                    key=lambda m: (m.sent_at, m.telegram_message_id))
    by_id = {m.telegram_message_id: m for m in active}
    roots = []
    for message in active:
        raw = message.text.strip()
        if not raw or not CUE.search(raw) or "schedule" in raw.casefold() or "competition" in raw.casefold():
            continue
        if POINTER.fullmatch(raw):
            continue
        subject = explicit_subject(raw, patterns)
        profile = profiles.get(message.sender_telegram_user_id, [])
        if subject is None and len(profile) == 1:
            subject = profile[0]
        if subject is None:
            previous = by_id.get(message.reply_to_telegram_id)
            if previous and previous.sender_telegram_user_id == message.sender_telegram_user_id:
                subject = explicit_subject(previous.text, patterns)
        reason = None
        if subject is None:
            reason = "subject_not_established"
        if not ACTION.search(raw):
            reason = "instruction_or_material_missing"
        # A file can contain an assignment, but its unread content is never inferred.
        if "at the end" in raw.casefold() and "presentation" in raw.casefold():
            reason = "assignment_inside_unread_file"
        lines = [match.group(1).strip() for line in raw.splitlines() if (match := SPLIT.match(line))]
        # Numbered steps for one subject form one task. Split only separate explicit subjects.
        parts = [raw]
        if len(lines) >= 2:
            keyed = [(line, explicit_subject(line, patterns)) for line in lines]
            if all(key for _, key in keyed) and len({key for _, key in keyed}) > 1:
                parts = [line for line, _ in keyed]
        roots.append((message, parts, subject, reason))

    result = []
    linked_media_ids = set()
    root_ids = {root.telegram_message_id for root, _, _, _ in roots}
    for root, parts, subject, reason in roots:
        sources = [{"message_id": root.telegram_message_id, "revision": root.current_revision, "role": "instruction"}]
        parent = by_id.get(root.reply_to_telegram_id)
        if parent and parent.sender_telegram_user_id == root.sender_telegram_user_id:
            sources.append({"message_id": parent.telegram_message_id, "revision": parent.current_revision, "role": "context"})
        media = [attachment_record(root, item) for item in attachments.get(root.id, []) if item.kind in {"photo", "document"}]
        root_has_photo = any(item["kind"] == "photo" for item in media)
        additions = []
        for other in active:
            if other.id == root.id or other.telegram_message_id in root_ids or other.sender_telegram_user_id != root.sender_telegram_user_id:
                continue
            gap = other.sent_at - root.sent_at
            if gap < -timedelta(minutes=2) or gap > timedelta(minutes=15):
                continue
            pointer = timedelta(0) <= gap <= timedelta(minutes=2) and bool(POINTER.fullmatch(other.text.strip()))
            linked = other.reply_to_telegram_id == root.telegram_message_id or (
                not other.text.strip() and bool(attachments.get(other.id))
            ) or pointer
            if not linked:
                continue
            if other.reply_to_telegram_id == root.telegram_message_id and re.search(
                r"(?i)\b(?:also|additionally|bring|please bring)\b", other.text
            ):
                additions.append(other)
                sources.append({"message_id": other.telegram_message_id, "revision": other.current_revision, "role": "addition"})
            if gap >= timedelta(0) and any(
                middle.telegram_message_id in root_ids and middle.id != root.id
                and root.sent_at <= middle.sent_at <= other.sent_at
                and middle.telegram_message_id < other.telegram_message_id
                for middle in active
            ):
                continue
            related = [attachment_record(other, item) for item in attachments.get(other.id, []) if item.kind in {"photo", "document"}]
            if gap >= timedelta(0) and root_has_photo:
                continue
            if pointer:
                sources.append({"message_id": other.telegram_message_id, "revision": other.current_revision, "role": "pointer"})
            if related:
                media.extend(related)
                sources.append({"message_id": other.telegram_message_id, "revision": other.current_revision, "role": "material"})
        if "WB" in root.text.upper() and not any("WB" in (item["name"] or "").upper() for item in media):
            prior = [other for other in active if other.sender_telegram_user_id == root.sender_telegram_user_id
                     and timedelta(0) <= root.sent_at - other.sent_at <= timedelta(days=7)]
            for other in reversed(prior):
                related = [attachment_record(other, item) for item in attachments.get(other.id, [])
                           if item.kind == "document" and "WB" in (item.name or "").upper()]
                if related:
                    media.extend(related)
                    sources.append({"message_id": other.telegram_message_id, "revision": other.current_revision, "role": "material"})
                    break
        if subject is None:
            file_subjects = {explicit_subject(item["name"], patterns) for item in media if item["name"]}
            file_subjects.discard(None)
            if len(file_subjects) == 1:
                subject = next(iter(file_subjects))
                if reason == "subject_not_established":
                    reason = None
        for index, part in enumerate(parts):
            part_subject = explicit_subject(part, patterns) or subject
            part_reason = reason if part_subject else "subject_not_established"
            if part_subject and part_reason == "subject_not_established":
                part_reason = None
            result.append({
                "root_message_id": root.telegram_message_id, "part_index": index,
                "subject_key": part_subject,
                "text": part + "".join(f"\nAddition ({item.telegram_message_id}): {item.text.strip()}" for item in additions),
                "due_date": due_date(part, root.sent_at) or due_date(root.text, root.sent_at),
                "due_text": (match.group(0) if (match := RELATIVE_DUE.search(part)) else None),
                "status": "confirmed" if part_reason is None else "needs_review",
                "reason": part_reason, "sources": sources, "attachments": media,
            })
        linked_media_ids.update(item["message_id"] for item in media)
    anchors = {profile["anchor_message_id"] for profile in
               json.loads(Path("config/source_profiles.json").read_text(encoding="utf-8"))["profiles"]
               if len(profile["candidate_subjects"]) == 1}
    known_senders = {message.sender_telegram_user_id for message in active if message.telegram_message_id in anchors}
    for message in active:
        if message.telegram_message_id in linked_media_ids or message.sender_telegram_user_id not in known_senders:
            continue
        photos = [attachment_record(message, item) for item in attachments.get(message.id, []) if item.kind == "photo"]
        if photos and message.telegram_message_id not in root_ids:
            profile = profiles.get(message.sender_telegram_user_id, [])
            result.append({
                "root_message_id": message.telegram_message_id, "part_index": 0,
                "subject_key": profile[0] if len(profile) == 1 else None,
                "text": message.text.strip() or "Assignment only in a photo; text has not been read",
                "due_date": None, "status": "needs_review", "reason": "unread_photo",
                "sources": [{"message_id": message.telegram_message_id, "revision": message.current_revision, "role": "material"}],
                "attachments": photos,
            })
    for message in active:
        if message.telegram_message_id in root_ids or message.telegram_message_id in linked_media_ids:
            continue
        documents = [attachment_record(message, item) for item in attachments.get(message.id, [])
                     if item.kind == "document" and item.name and "for notes" in item.name.replace("_", " ").casefold()]
        if message.text.strip() or not documents:
            continue
        subjects = {explicit_subject(item["name"].replace("_", " "), patterns) for item in documents}
        subjects.discard(None)
        if len(subjects) != 1:
            continue
        result.append({
            "root_message_id": message.telegram_message_id, "part_index": 0,
            "subject_key": subjects.pop(), "text": "Possible notes material: " + documents[0]["name"],
            "due_date": None, "due_text": None, "status": "needs_review",
            "reason": "instruction_not_confirmed",
            "sources": [{"message_id": message.telegram_message_id,
                         "revision": message.current_revision, "role": "material"}],
            "attachments": documents,
        })
    # Consecutive parts of one teacher's assignment can both contain action words.
    by_root = {item["root_message_id"]: item for item in result if item["part_index"] == 0}
    for current_id, item in by_root.items():
        previous = by_root.get(current_id - 1)
        current, earlier = by_id.get(current_id), by_id.get(current_id - 1)
        if (previous and current and earlier and item["status"] == previous["status"] == "confirmed"
                and item["subject_key"] == previous["subject_key"]
                and current.sender_telegram_user_id == earlier.sender_telegram_user_id
                and timedelta(0) <= current.sent_at - earlier.sent_at <= timedelta(seconds=2)):
            item["text"] = previous["text"] + "\nAddition: " + item["text"]
            item["due_date"] = item["due_date"] or previous["due_date"]
            item["due_text"] = item["due_text"] or previous["due_text"]
            item["sources"] = [*previous["sources"], *item["sources"]]
            item["attachments"] = [*previous["attachments"], *item["attachments"]]
    return result


async def sync_homework(session: AsyncSession) -> dict[str, int]:
    """Replay current revisions atomically; preserve owner overrides and stable IDs."""
    stats = {"confirmed": 0, "needs_review": 0, "changed": 0}
    async with session.begin():
        for chat in (await session.scalars(select(SourceChat))).all():
            messages = (await session.scalars(select(Message).where(Message.chat_id == chat.id).order_by(
                Message.sent_at, Message.telegram_message_id,
            ))).all()
            current = {m.id: m.current_revision for m in messages}
            media = {m.id: [] for m in messages}
            if current:
                rows = (await session.execute(select(Attachment, MessageRevision.message_id, MessageRevision.revision_no)
                    .join(MessageRevision, Attachment.message_revision_id == MessageRevision.id)
                    .where(MessageRevision.message_id.in_(current)))).all()
                for attachment, message_id, revision in rows:
                    if revision == current[message_id]:
                        media[message_id].append(attachment)
            projected = project(messages, media)
            stored = {(h.root_message_id, h.part_index): h for h in (await session.scalars(
                select(Homework).where(Homework.chat_id == chat.id)
            )).all()}
            for payload in projected:
                stats[payload["status"]] += 1
                key = payload["root_message_id"], payload["part_index"]
                item = stored.pop(key, None)
                if item is None:
                    session.add(Homework(chat_id=chat.id, owner_override=None,
                        updated_at=datetime.now(timezone.utc), **payload))
                    stats["changed"] += 1
                elif any(getattr(item, field) != value for field, value in payload.items()):
                    for field, value in payload.items():
                        setattr(item, field, value)
                    item.updated_at = datetime.now(timezone.utc)
                    stats["changed"] += 1
            for stale in stored.values():
                if stale.owner_override is None:
                    await session.delete(stale)
                    stats["changed"] += 1
                elif stale.status != "needs_review" or stale.reason != "source_deleted":
                    stale.status = "needs_review"
                    stale.reason = "source_deleted"
                    stale.updated_at = datetime.now(timezone.utc)
                    stats["changed"] += 1
    return stats
