"""Shared schedule/homework views for command replies and channel publication."""

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.homework import catalog, effective
from app.homework_selection import (Assignment, assignment_homework, confirmed_latest,
                                    day_assignments, homework_history)
from app.models import Homework, Message as StoredMessage, ScheduleDay, SourceChat
from app.telegram_settings import LOCAL_TIME
from app.telegram_sources import source_link

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SHORT_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
MESSAGE_LIMIT = 4000


def format_day(target: date, payload: dict, peer_id: int) -> str:
    lines = [f"{target:%d.%m.%Y}, {WEEKDAYS[target.weekday()]}"]
    if payload.get("state") != "verified":
        lines.append("Status: needs review")
    for slot in payload["slots"]:
        room = f" (room {slot['room']})" if slot.get("room") else ""
        lines.append(f"{slot['number']}. {slot['raw_subject']}{room}")
    return "\n".join(lines)


def format_missing(target: date) -> str:
    return f"{target:%d.%m.%Y}: The schedule for this date has not been published yet"


def format_estimate_missing(target: date) -> str:
    return (format_missing(target) + "\nAn estimate is unavailable: no confirmed "
            "schedule from 14 or 7 days before this date.")


def format_orientation(target: date, rows: list, latest: dict | None = None) -> str:
    previous = {day.schedule_date: (day, peer) for day, peer in rows
                if day.payload.get("state") == "verified"}
    sections = [format_missing(target)]
    shown = set()
    for weeks in (1, 2):
        past = target - timedelta(weeks=weeks)
        if past not in previous:
            continue
        day, peer = previous[past]
        sources = ", ".join(source_link(peer, message_id)
                            for message_id in day.payload.get("source_ids", [])) or "link unavailable"
        section = f"Reference ({weeks} weeks ago):\n{format_day(past, day.payload, peer)}\nSource: {sources}"
        for slot in day.payload["slots"]:
            subject = slot.get("subject_key")
            if subject and subject not in shown and latest and subject in latest:
                item, item_peer, published = latest[subject]
                section += ("\n\nFor reference: latest confirmed homework for the subject; "
                            "connection to the future lesson is unconfirmed\n"
                            + format_homework(item, item_peer, published, compact=True, relation="uncertain"))
                shown.add(subject)
        sections.append(section)
    if len(sections) > 1:
        sections.append("These are past posts; alternating weekly schedules may differ. The selected date is unconfirmed.")
    return "\n\n".join(sections)


def schedule_keyboard(source_callback: str | None = None, *, shown: bool = False,
                      today: date | None = None, week_start: date | None = None) -> InlineKeyboardMarkup:
    today = today or datetime.now(LOCAL_TIME).date()
    monday = today - timedelta(days=today.weekday())
    selected = week_start or monday
    if selected not in {monday, monday + timedelta(weeks=1)}:
        selected = monday
    next_week = selected == monday
    toggle = monday + timedelta(weeks=1) if next_week else monday
    buttons = [[
        InlineKeyboardButton(text="Next week" if next_week else "Previous week",
                             callback_data=f"schedule:week:{toggle.isoformat()}"),
    ]]
    weekdays = []
    for offset, name in enumerate(SHORT_WEEKDAYS):
        day = selected + timedelta(days=offset)
        label = " · today" if day == today else " · tomorrow" if day == today + timedelta(days=1) else ""
        weekdays.append(InlineKeyboardButton(
            text=f"{name} {day:%d.%m}{label}", callback_data=f"schedule:date:{day.isoformat()}",
        ))
    buttons.extend((weekdays[:3], weekdays[3:]))
    if source_callback:
        buttons.append([InlineKeyboardButton(
            text="Sources shown" if shown else "Sources", callback_data=source_callback,
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def schedule_texts(
    session: AsyncSession, source_chat_id: int, start: date, end: date | None = None,
) -> list[str]:
    rows = await schedule_rows(session, source_chat_id, start, end or start)
    return [format_day(day.schedule_date, day.payload, peer_id) for day, peer_id in rows]


async def schedule_rows(session: AsyncSession, source_chat_id: int, start: date, end: date):
    return (await session.execute(
        select(ScheduleDay, SourceChat.telegram_peer_id)
        .join(SourceChat, ScheduleDay.chat_id == SourceChat.id)
        .where(SourceChat.telegram_peer_id == source_chat_id,
               ScheduleDay.schedule_date >= start, ScheduleDay.schedule_date <= end)
        .order_by(ScheduleDay.schedule_date)
    )).all()


def format_homework(item: Homework, peer_id: int | None, published: date | None = None,
                    *, compact: bool = False, relation: str | None = None) -> str:
    names, _ = catalog()
    subject = names.get(effective(item, "subject_key"), "Subject unknown")
    due = effective(item, "due_date")
    relative_due = effective(item, "due_text")
    if isinstance(due, str):
        due = date.fromisoformat(due)
    lines = [f"📌 Homework: {effective(item, 'text')}" if compact else f"📌 {subject}: {effective(item, 'text')}"]
    if due:
        lines.append(f"Due: {due:%d.%m.%Y}")
    elif relative_due:
        lines.append(f"Teacher deadline: {relative_due}")
        if relation == "uncertain":
            lines.append("⚠️ Connection to this lesson is unconfirmed")
    elif relation == "likely":
        lines.append("probably for this lesson; teacher deadline unspecified")
    elif relation == "uncertain":
        lines.extend(("Homework for the subject",
                      "⚠️ Connection to this lesson is unconfirmed; teacher deadline unspecified"))
    else:
        lines.append("Latest homework found for the subject; deadline unspecified")
    if published:
        lines.append(f"Published: {published:%d-%m-%y}")
    lines.append(f"Source: {source_link(peer_id, item.root_message_id)}")
    if effective(item, "status") != "confirmed":
        lines.insert(0, "⚠️ Possible homework; unconfirmed")
    else:
        lines.append("✅ Homework confirmed")
    for media in item.attachments:
        name = media["name"] or media["kind"]
        availability = "file available" if sendable_path(media) else "file unavailable to the bot"
        link = f" · {source_link(peer_id, media['message_id'])}" if media["message_id"] != item.root_message_id else ""
        entry = f"Attachment: {name} ({availability}){link}"
        if entry not in lines:
            lines.append(entry)
    return "\n".join(lines)


def source_fingerprint(payload: dict, assignments: list[Assignment]) -> str:
    selected = []
    for assignment in assignments:
        choices = [entry for entry, _ in assignment_homework(assignment)] + [assignment.pending]
        for choice in choices:
            if choice:
                item = choice[0]
                selected.append((item.id, item.subject_key, item.text, item.due_date,
                                 item.due_text, item.status, item.sources, item.attachments,
                                 item.owner_override))
    raw = json.dumps([payload, selected], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def day_source_callback(day: ScheduleDay, mode: str, as_of: date, assignments: list[Assignment]) -> str:
    return f"src:d:{day.id}:{mode}:{as_of:%y%m%d}:{source_fingerprint(day.payload, assignments)}"


def homework_source_callback(item: Homework) -> str:
    return f"src:h:{item.id}:{source_fingerprint({}, [Assignment(slot=None, chosen=(item, None, None))])}"


def lesson_block(assignment: Assignment) -> str:
    slot = assignment.slot
    chosen, pending = assignment.chosen, assignment.pending
    repeated = assignment.repeated
    room = f" (room {slot['room']})" if slot.get("room") else ""
    lines = [f"{slot['number']}. {slot['raw_subject']}{room}"]
    if repeated:
        lines.append("Homework is shown with the first lesson for this subject.")
    elif chosen:
        shown_tasks = set()
        for entry, task_relation in assignment_homework(assignment):
            item, peer, published = entry
            task_key = (effective(item, "text"), str(effective(item, "due_date")),
                        effective(item, "due_text"), json.dumps(item.attachments, sort_keys=True))
            if task_key in shown_tasks:
                lines.append(f"Assignment reminder: {published:%d-%m-%y}; source: {source_link(peer, item.root_message_id)}")
                continue
            shown_tasks.add(task_key)
            lines.extend(format_homework(item, peer, published, compact=True, relation=task_relation).splitlines())
        if pending:
            lines.insert(1, "For reference: confirmed homework")
    else:
        lines.append("No current homework found.")
    if pending:
        item, peer, published = pending
        lines.extend(("", "⚠️ New homework is awaiting confirmation.",
                      f"Published: {published:%d-%m-%y}",
                      f"New homework source: {source_link(peer, item.root_message_id)}"))
    return "\n".join(lines)


def source_references(day: ScheduleDay | None, assignments: list[Assignment]) -> list[tuple[str, int]]:
    refs = []
    if day:
        refs.extend(("Schedule", message_id) for message_id in dict.fromkeys(day.payload.get("source_ids", [])))
    roles = {"instruction": "instruction", "context": "context", "addition": "addition",
             "material": "material", "pointer": "pointer"}
    for assignment in assignments:
        slot = assignment.slot
        choices = [entry for entry, _ in assignment_homework(assignment)] + [assignment.pending]
        for selected in choices:
            if not selected:
                continue
            item = selected[0]
            subject = slot["raw_subject"] if slot else catalog()[0].get(effective(item, "subject_key"), "Homework")
            refs.extend((f"Homework {subject}: {roles.get(source['role'], source['role'])}", source["message_id"])
                        for source in item.sources)
            for media in item.attachments:
                kind = "presentation" if (media["name"] or "").lower().endswith((".ppt", ".pptx")) else "file"
                refs.append((f"{kind} {media['name'] or media['kind']}", media["message_id"]))
    return list(dict.fromkeys(refs))


async def source_lines(session: AsyncSession, chat_id: int, peer_id: int | None,
                       refs: list[tuple[str, int]]) -> list[str]:
    ids = {message_id for _, message_id in refs}
    messages = (await session.scalars(select(StoredMessage).where(
        StoredMessage.chat_id == chat_id, StoredMessage.telegram_message_id.in_(ids)
    ))).all() if ids else []
    known = {item.telegram_message_id: item for item in messages}
    grouped = {}
    for label, message_id in refs:
        labels = grouped.setdefault((chat_id, message_id), [])
        if label not in labels:
            labels.append(label)
    lines = []
    for (_, message_id), labels in grouped.items():
        label = ", ".join(labels)
        source = known.get(message_id)
        if source is None or source.deleted_at:
            lines.append(f"{message_id} — {label} (message unavailable)")
        elif peer_id is None or not str(peer_id).startswith("-100"):
            lines.append(f"{message_id} — {label} (link unavailable)")
        else:
            lines.append(f"{message_id} — {label}: {source_link(peer_id, message_id)} (for source group members)")
    return lines or ["No sources found"]


def units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def split_day(header: str, blocks: list[str], *, separator: str = "\n\n") -> list[str]:
    """Split at lesson boundaries; keep every part within Telegram's UTF-16 limit."""
    continuation = header + ' (continued)'
    if units(continuation) >= MESSAGE_LIMIT - 80:
        raise ValueError("Message header is too long")
    parts, current = [], header
    for block in blocks:
        if units(continuation + separator + block) > MESSAGE_LIMIT:
            if current != header:
                parts.append(current)
            title, newline, body = block.partition("\n")
            # Homework's first line can contain the entire untrusted task, not a short title.
            if not newline or units(continuation + "\n" + title + ' — continued') >= MESSAGE_LIMIT - 80:
                title, body = 'Entry', block
            prefix = f"{header}\n{title}"
            while body:
                width = MESSAGE_LIMIT - units(prefix) - 1
                piece = body.encode("utf-16-le")[:width * 2].decode("utf-16-le", errors="ignore")
                parts.append(prefix + "\n" + piece)
                body = body[len(piece):]
                prefix = f"{continuation}\n{title}" + ' — continued'
            current = header
        else:
            if units(current + separator + block) > MESSAGE_LIMIT:
                parts.append(current)
                current = continuation
            current += separator + block
    if current != header or not parts:
        parts.append(current)
    return parts


def sendable_path(media: dict) -> Path | None:
    if not media.get("bot_sendable") or not media.get("storage_key"):
        return None
    root = Path(os.getenv("MEDIA_ROOT", "private/media")).resolve()
    path = (root / media["storage_key"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    if media.get("sha256"):
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != media["sha256"]:
                return None
    return path


async def response_view(session: AsyncSession, source_chat_id: int, target: date, kind: str = "schedule") -> dict:
    from app.schedule import displayed_day, estimate_ready

    today = datetime.now(LOCAL_TIME).date()
    as_of = min(target, today)
    chat = await session.scalar(select(SourceChat).where(SourceChat.telegram_peer_id == source_chat_id))
    if chat is None:
        return {"parts": [format_missing(target)], "callback": None, "media": []}
    history = await homework_history(session, source_chat_id, as_of)
    latest = confirmed_latest(history)
    media = []
    callback = None
    if kind == "homework":
        names, _ = catalog()
        blocks = []
        for subject, entries in history.items():
            selected = latest.get(subject)
            pending = None
            for entry in entries:
                if selected and entry[0].id == selected[0].id:
                    break
                due = effective(entry[0], "due_date")
                due = date.fromisoformat(due) if isinstance(due, str) else due
                if effective(entry[0], "status") == "needs_review" and (due is None or due >= target):
                    pending = entry
                    break
            if selected:
                due = effective(selected[0], "due_date")
                due = date.fromisoformat(due) if isinstance(due, str) else due
                if due and due < target:
                    selected = None
            slot = {"number": len(blocks) + 1, "raw_subject": names.get(subject, subject)}
            blocks.append(lesson_block(Assignment(slot=slot, chosen=selected, pending=pending, relation="uncertain")))
            if selected:
                media.append(selected[0])
        return {"parts": split_day("📚 Homework", blocks or ["No confirmed homework found."]),
                "callback": None, "media": media}
    day = await displayed_day(session, chat.id, target)
    if day:
        chronology = await schedule_rows(session, source_chat_id, date(2000, 1, 1), target)
        assignments = day_assignments(day, history, [row for row, _ in chronology])
        header = f"📅 {target:%d.%m.%Y}, {WEEKDAYS[target.weekday()]}"
        blocks = [lesson_block(assignment) for assignment in assignments]
        if day.payload.get("state") == "estimated":
            past = date.fromisoformat(day.payload["estimate_source_date"])
            blocks.insert(0, f"⚠️ Estimated schedule. Based on {past:%d.%m.%Y}.\n"
                          "The schedule for this date is not confirmed yet.")
            links = ", ".join(source_link(source_chat_id, number) for number in day.payload.get("source_ids", []))
            blocks.append(f"Estimated schedule source: {links or 'link unavailable'}")
        else:
            callback = day_source_callback(day, "d", as_of, assignments)
        shown = {slot.get("subject_key") for slot in day.payload["slots"]}
        for subject, selected in latest.items():
            due = effective(selected[0], "due_date")
            due = date.fromisoformat(due) if isinstance(due, str) else due
            if subject not in shown and due == target and due >= today:
                blocks.append("Homework with an explicit deadline outside the displayed schedule:\n" + format_homework(*selected))
                media.append(selected[0])
        media.extend(entry[0] for assignment in assignments for entry, _ in assignment_homework(assignment))
        return {"parts": split_day(header, blocks), "callback": callback, "media": media}
    if estimate_ready(target, datetime.now(LOCAL_TIME)):
        return {"parts": [format_estimate_missing(target)], "callback": None, "media": []}
    earlier = await schedule_rows(session, source_chat_id, target - timedelta(weeks=2), target - timedelta(weeks=1))
    text = format_orientation(target, earlier, latest)
    return {"parts": split_day("📅 Schedule", [text]) if units(text) > MESSAGE_LIMIT else [text],
            "callback": None, "media": []}
