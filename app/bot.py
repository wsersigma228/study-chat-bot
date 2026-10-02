"""Owner-controlled schedule bot. It reads PostgreSQL and never opens Telethon."""

import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile
from aiogram.types import (
    BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginChannel,
)
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import engine
from app.homework import catalog
from app.models import BotResponse, ChannelPost, Homework, HomeworkReviewNotice, Message as StoredMessage, ScheduleDay, SourceChat
from app.schedule import LOCAL_TIME
from app.telegram_settings import load_env


SCHEDULE_USAGE = "Usage: /schedule 08-04-30"
HOMEWORK_USAGE = "Usage: /homework or /homework 08-04-30"
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
SHORT_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
MESSAGE_LIMIT = 4000
MAX_MESSAGE_AGE = timedelta(minutes=5)
PROCESS_INSTANCE = f"{os.getenv('HOSTNAME', 'local')}:{os.getpid()}"


def is_stale(sent_at: datetime) -> bool:
    sent_at = sent_at.replace(tzinfo=timezone.utc) if sent_at.tzinfo is None else sent_at
    return datetime.now(timezone.utc) - sent_at > MAX_MESSAGE_AGE


async def ignore_stale_message(handler, message: Message, data: dict):
    review_reply = (getattr(getattr(message, "chat", None), "type", None) == "private"
                    and getattr(message, "reply_to_message", None) is not None
                    and bool(getattr(message, "text", None))
                    and not message.text.startswith("/"))
    if is_stale(message.date) and not review_reply:
        return None
    return await handler(message, data)


@dataclass(frozen=True)
class BotSettings:
    token: str
    owner_user_id: int
    allowed_chat_ids: frozenset[int]
    source_chat_id: int
    channel_id: int | None = None

    @classmethod
    def read(cls) -> "BotSettings":
        load_env()
        token = os.getenv("BOT_TOKEN", "").strip()
        owner = os.getenv("BOT_OWNER_USER_ID", "").strip()
        source = os.getenv("SOURCE_CHAT_ID", "").strip()
        if not token:
            raise ValueError("Set BOT_TOKEN locally in .env")
        if not owner.isdigit() or int(owner) <= 0:
            raise ValueError("BOT_OWNER_USER_ID must be a positive numeric user ID")
        if not source.lstrip("-").isdigit() or int(source) >= 0:
            raise ValueError("SOURCE_CHAT_ID must be the confirmed negative source group ID")

        allowed = set()
        for value in filter(None, re.split(r"[\s,]+", os.getenv("BOT_ALLOWED_CHAT_IDS", "").strip())):
            if not value.lstrip("-").isdigit() or int(value) >= 0:
                raise ValueError("BOT_ALLOWED_CHAT_IDS must contain negative group IDs separated by commas")
            allowed.add(int(value))
        if int(source) in allowed:
            raise ValueError("SOURCE_CHAT_ID cannot be used as a BOT_ALLOWED_CHAT_IDS destination")
        raw_channel = os.getenv("BOT_CHANNEL_ID", "").strip()
        if raw_channel and (len(raw_channel) <= 4 or not raw_channel.startswith("-100") or not raw_channel[1:].isdigit()):
            raise ValueError("BOT_CHANNEL_ID must be a numeric channel ID beginning with -100")
        channel_id = int(raw_channel) if raw_channel else None
        if channel_id in {int(source), *allowed}:
            raise ValueError("BOT_CHANNEL_ID must differ from source and command groups")
        return cls(token, int(owner), frozenset(allowed), int(source), channel_id)


def can_read_schedule(settings: BotSettings, message: Message, user_id: int | None = None) -> bool:
    user_id = user_id if user_id is not None else message.from_user.id if message.from_user else None
    if message.chat.type == "private":
        return user_id == settings.owner_user_id
    return message.chat.type in {"group", "supergroup"} and message.chat.id in settings.allowed_chat_ids


def is_owner(settings: BotSettings, message: Message) -> bool:
    return message.from_user is not None and message.from_user.id == settings.owner_user_id


def parse_user_date(value: str | None) -> date:
    if not value or not re.fullmatch(r"\d{2}-\d{2}-\d{2}", value.strip()):
        raise ValueError(SCHEDULE_USAGE)
    value = value.strip()
    day, month, year = map(int, value.split("-"))
    try:
        return date(2000 + year, month, day)
    except ValueError as exc:
        raise ValueError(SCHEDULE_USAGE) from exc


def source_link(peer_id: int | None, message_id: int) -> str:
    if peer_id is not None and str(peer_id).startswith("-100"):
        return f"https://t.me/c/{-peer_id - 1_000_000_000_000}/{message_id}"
    return f"message {message_id}"


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


def period(kind: str, today: date | None = None) -> date:
    today = today or datetime.now(LOCAL_TIME).date()
    if kind == "today":
        return today
    return today + timedelta(days=1 if today.weekday() < 4 else 7 - today.weekday())


HELP_TEXT = (
    "Schedules and confirmed homework from the database.\n"
    "/schedule 08-04-30 — selected date\n"
    "/today — today\n"
    "/tomorrow — next school day (including Monday after a weekend)\n"
    "/week — choose a day this school week or next\n"
    "Mon–Fri buttons include dates and today/tomorrow labels, refreshed when you use the bot. If the schedule is unconfirmed, past weeks provide a reference. Use /schedule for weekends.\n"
    "/homework [DD-MM-YY] — latest confirmed homework by subject\n"
    "Past dates show only history available on that day.\n"
    "/help — help"
)


async def register_commands(bot: Bot) -> None:
    commands = [
        BotCommand(command="start", description="Open schedule"),
        BotCommand(command="help", description="Command help"),
        BotCommand(command="schedule", description="Schedule for date DD-MM-YY"),
        BotCommand(command="today", description="Today's schedule"),
        BotCommand(command="tomorrow", description="Next school day"),
        BotCommand(command="week", description="This school week and next"),
    ]
    await bot.delete_my_commands(scope=BotCommandScopeDefault())
    await bot.set_my_commands(
        [*commands, BotCommand(command="review", description="Review unclear homework (owner)")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [*commands, BotCommand(command="chatid", description="Group ID for owner")],
        scope=BotCommandScopeAllGroupChats(),
    )


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


def effective(item: Homework, field: str):
    override = item.owner_override or {}
    if "_approved_sources" in override and override["_approved_sources"] != item.sources:
        return getattr(item, field)
    return override.get(field, getattr(item, field))


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


async def homework_history(session: AsyncSession, source_chat_id: int, as_of: date):
    """Return confirmed current revisions in publication order for each subject."""
    rows = (await session.execute(
        select(Homework, SourceChat.telegram_peer_id, StoredMessage)
        .join(SourceChat, Homework.chat_id == SourceChat.id)
        .join(StoredMessage, (StoredMessage.chat_id == Homework.chat_id) &
              (StoredMessage.telegram_message_id == Homework.root_message_id))
        .where(SourceChat.telegram_peer_id == source_chat_id)
        .order_by(StoredMessage.sent_at.desc(), StoredMessage.telegram_message_id.desc(),
                  Homework.part_index.desc())
    )).all()
    history = {}
    source_ids = {entry["message_id"] for item, _, _ in rows for entry in item.sources}
    source_rows = (await session.scalars(select(StoredMessage).where(
        StoredMessage.telegram_message_id.in_(source_ids),
        StoredMessage.chat_id.in_({item.chat_id for item, _, _ in rows}),
    ))).all() if source_ids else []
    sources = {(source.chat_id, source.telegram_message_id): source for source in source_rows}
    for item, peer, source in rows:
        published = source.sent_at.astimezone(LOCAL_TIME).date()
        if published > as_of or source.deleted_at or effective(item, "status") not in {"confirmed", "needs_review"}:
            continue
        # Current projection cannot reconstruct an edit or addition made after a past date.
        if as_of < datetime.now(LOCAL_TIME).date() and any(
            (linked := sources.get((item.chat_id, entry["message_id"]))) is None
            or linked.sent_at.astimezone(LOCAL_TIME).date() > as_of
            or (linked.edited_at and linked.edited_at.astimezone(LOCAL_TIME).date() > as_of)
            or linked.deleted_at
            for entry in item.sources
        ):
            continue
        subject = effective(item, "subject_key")
        if not subject:
            continue
        history.setdefault(subject, []).append((item, peer, published))
    return history


async def homework_rows(session: AsyncSession, source_chat_id: int, as_of: date):
    history = await homework_history(session, source_chat_id, as_of)
    return confirmed_latest(history)


def confirmed_latest(history: dict):
    latest = {}
    for subject, items in history.items():
        if selected := next((entry for entry in items if effective(entry[0], "status") == "confirmed"), None):
            latest[subject] = selected
    return latest


def day_assignments(day: ScheduleDay, history: dict, days: list[ScheduleDay]):
    """Select by current verified subject chronology without changing teacher due dates."""
    result = []
    shown = set()
    target = day.schedule_date
    for slot in day.payload["slots"]:
        subject = slot.get("subject_key")
        repeated = bool(subject and subject in shown)
        if subject:
            shown.add(subject)
        if repeated or not subject:
            result.append((slot, None, None, repeated, None))
            continue
        earlier = [row.schedule_date for row in days if row.schedule_date < target
                   and row.payload.get("state") == "verified"
                   and any(part.get("subject_key") == subject for part in row.payload["slots"])]
        previous_lesson = max(earlier, default=None)
        complete = previous_lesson is not None and day.payload.get("state") == "verified"
        if complete:
            known = {row.schedule_date for row in days if row.payload.get("state") == "verified"}
            check = previous_lesson + timedelta(days=1)
            while check < target:
                if check.weekday() < 5 and check not in known:
                    complete = False
                    break
                check += timedelta(days=1)
        entries = history.get(subject, [])
        chosen = None
        relation = None
        for entry in entries:
            item, _, published = entry
            due = effective(item, "due_date")
            due = date.fromisoformat(due) if isinstance(due, str) else due
            if due == target and effective(item, "status") == "confirmed":
                chosen, relation = entry, "explicit"
                break
        if chosen is None:
            for entry in entries:
                item, _, published = entry
                if effective(item, "status") == "confirmed" and effective(item, "due_date") is None and effective(item, "due_text"):
                    if previous_lesson is None or previous_lesson <= published < target:
                        chosen, relation = entry, "explicit" if complete else "uncertain"
                        break
        if chosen is None:
            for entry in entries:
                item, _, published = entry
                due = effective(item, "due_date")
                if isinstance(due, str):
                    due = date.fromisoformat(due)
                if (effective(item, "status") == "confirmed" and due is None
                        and not effective(item, "due_text") and complete and previous_lesson <= published < target):
                    chosen, relation = entry, "likely"
                    break
        if chosen is None:
            # A dated task for another lesson is not a fallback for this one.
            chosen, relation = next(((entry, "uncertain") for entry in entries
                                     if effective(entry[0], "status") == "confirmed"
                                     and effective(entry[0], "due_date") is None), (None, None))
        pending = None
        for entry in entries:
            if chosen and entry[0].id == chosen[0].id:
                break
            due = effective(entry[0], "due_date")
            if isinstance(due, str):
                due = date.fromisoformat(due)
            if effective(entry[0], "status") == "needs_review" and due in {None, target}:
                pending = entry
                break
        if pending and chosen:
            relation = "uncertain"
        extras = []
        for entry in entries:
            item, _, published = entry
            if effective(item, "status") != "confirmed" or (chosen and item.id == chosen[0].id):
                continue
            due = effective(item, "due_date")
            due = date.fromisoformat(due) if isinstance(due, str) else due
            in_interval = previous_lesson is not None and previous_lesson <= published < target
            if due == target:
                extras.append((entry, "explicit"))
            elif due is None and in_interval:
                extra_relation = ("explicit" if effective(item, "due_text") else "likely") if complete else "uncertain"
                extras.append((entry, "uncertain" if pending else extra_relation))
        result.append((slot, chosen, pending, False, relation, extras))
    return result


def assignment_homework(assignment):
    """Include additional tasks while accepting existing five-field assignments."""
    if assignment[1]:
        yield assignment[1], assignment[4]
    if len(assignment) > 5:
        yield from assignment[5]


def source_fingerprint(payload: dict, assignments: list) -> str:
    selected = []
    for assignment in assignments:
        choices = [entry for entry, _ in assignment_homework(assignment)] + [assignment[2]]
        for choice in choices:
            if choice:
                item = choice[0]
                selected.append((item.id, item.subject_key, item.text, item.due_date,
                                 item.due_text, item.status, item.sources, item.attachments,
                                 item.owner_override))
    raw = json.dumps([payload, selected], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:10]


def day_source_callback(day: ScheduleDay, mode: str, as_of: date, assignments: list) -> str:
    return f"src:d:{day.id}:{mode}:{as_of:%y%m%d}:{source_fingerprint(day.payload, assignments)}"


def homework_source_callback(item: Homework) -> str:
    return f"src:h:{item.id}:{source_fingerprint({}, [(None, (item, None, None), None, False, None)])}"


def lesson_block(assignment) -> str:
    slot, chosen, pending, repeated, relation = assignment[:5]
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


def source_references(day: ScheduleDay | None, assignments: list) -> list[tuple[str, int]]:
    refs = []
    if day:
        refs.extend(("Schedule", message_id) for message_id in dict.fromkeys(day.payload.get("source_ids", [])))
    roles = {"instruction": "instruction", "context": "context", "addition": "addition",
             "material": "material", "pointer": "pointer"}
    for assignment in assignments:
        slot = assignment[0]
        choices = [entry for entry, _ in assignment_homework(assignment)] + [assignment[2]]
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


def split_day(header: str, blocks: list[str], *, separator: str = "\n\n") -> list[str]:
    """Split at lesson boundaries; repeat the lesson name when a single lesson is long."""
    parts, current = [], header
    for block in blocks:
        if len(header) + len(block) + 2 > MESSAGE_LIMIT:
            if current != header:
                parts.append(current)
            title, newline, body = block.partition("\n")
            if not newline:
                title, body = "Entry", block
            prefix = f"{header}\n{title}"
            while body:
                width = MESSAGE_LIMIT - len(prefix) - 1
                parts.append(prefix + "\n" + body[:width])
                body = body[width:]
                prefix = f"{header} (continued)\n{title} — continued"
            current = header
        else:
            if len(current) + len(block) + 2 > MESSAGE_LIMIT:
                parts.append(current)
                current = f"{header} (continued)"
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


async def send_homework(message: Message, items: list[tuple[Homework, int, date]]) -> None:
    sent_media = set()
    for item, peer, published in items:
        callback = homework_source_callback(item)
        parts = split_day("📚 Homework", [format_homework(item, peer, published)])
        for index, part in enumerate(parts):
            await message.answer(part, reply_markup=schedule_keyboard(callback if index == 0 else None))
        await send_media(message, [item], sent_media)


async def send_media(message: Message, items: list[Homework], sent_media: set) -> None:
    for item in items:
        for media in item.attachments:
            key = media["message_id"], media["revision"], media["ordinal"]
            path = sendable_path(media)
            if path is None or key in sent_media:
                continue
            sent_media.add(key)
            file = FSInputFile(path)
            if media["kind"] == "photo":
                await message.answer_photo(file)
            else:
                await message.answer_document(file)


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
            blocks.append(lesson_block((slot, selected, pending, False, "uncertain")))
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
    return {"parts": split_day("📅 Schedule", [text]) if len(text) > MESSAGE_LIMIT else [text],
            "callback": None, "media": []}


async def send_saved_response(message: Message, settings: BotSettings, factory, target: date,
                              kind: str = "schedule") -> None:
    from app.channel import publish_one

    async with factory() as session:
        chat = await session.scalar(select(SourceChat).where(SourceChat.telegram_peer_id == settings.source_chat_id))
        if chat is None:
            await message.answer(format_missing(target), reply_markup=schedule_keyboard())
            return
        response = BotResponse(chat_id=chat.id, channel_id=message.chat.id, destination_type=message.chat.type,
                               schedule_date=target, kind=kind, message_ids=[], status="pending",
                               keyboard_date=datetime.now(LOCAL_TIME).date(),
                               updated_at=datetime.now(timezone.utc))
        session.add(response)
        await session.flush()
        response_id = response.id
        await session.commit()
    outcome = await publish_one(factory, getattr(message, "bot", None), response_id,
                                response=True, answer=message.answer)
    if outcome == "sent":
        async with factory() as session:
            view = await response_view(session, settings.source_chat_id, target, kind)
        await send_media(message, view["media"], set())


def create_router(settings: BotSettings, session_factory: async_sessionmaker[AsyncSession]) -> Router:
    router = Router()
    router.message.outer_middleware(ignore_stale_message)

    async def refresh_menu(message: Message) -> None:
        if not getattr(message, "reply_markup", None):
            return
        buttons = [button for row in message.reply_markup.inline_keyboard for button in row]
        dates = [date.fromisoformat(button.callback_data.removeprefix("schedule:date:"))
                 for button in buttons if button.callback_data and button.callback_data.startswith("schedule:date:")]
        selected = min(dates) if dates else None
        source = next((button for button in buttons if button.callback_data and button.callback_data.startswith("src:")), None)
        today = datetime.now(LOCAL_TIME).date()
        try:
            await message.edit_reply_markup(reply_markup=schedule_keyboard(
                source.callback_data if source else None, shown=bool(source and source.text == "Sources shown"),
                today=today, week_start=selected))
        except TelegramAPIError:
            pass
        async with session_factory() as session, session.begin():
            for model in (BotResponse, ChannelPost):
                responses = (await session.scalars(select(model).where(
                    model.channel_id == message.chat.id, model.message_ids.contains([message.message_id]),
                ))).all()
                for response in responses:
                    response.keyboard_date = today

    async def send_dates(message: Message, start: date, user_id: int | None = None,
                         *, trigger: str, update_id: str | int) -> None:
        if not can_read_schedule(settings, message, user_id):
            return
        print(json.dumps({"event": "schedule_request", "trigger": trigger,
                          "date": start.isoformat(), "update": update_id,
                          "chat_id": message.chat.id, "process": PROCESS_INSTANCE}), flush=True)
        await send_saved_response(message, settings, session_factory, start)

    async def send_homework_view(message: Message, target: date | None = None,
                                 user_id: int | None = None) -> None:
        if can_read_schedule(settings, message, user_id):
            await send_saved_response(message, settings, session_factory,
                                      target or datetime.now(LOCAL_TIME).date(), "homework")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if not can_read_schedule(settings, message):
            return
        await message.answer(HELP_TEXT, reply_markup=schedule_keyboard())

    @router.message(Command("help"))
    async def help_command(message: Message) -> None:
        if can_read_schedule(settings, message):
            await message.answer(HELP_TEXT, reply_markup=schedule_keyboard())

    @router.message(Command("schedule"))
    async def schedule(message: Message, command: CommandObject) -> None:
        if not can_read_schedule(settings, message):
            return
        try:
            target = parse_user_date(command.args)
        except ValueError:
            await message.answer(SCHEDULE_USAGE)
            return
        await send_dates(message, target, trigger="command:schedule", update_id=message.message_id)

    @router.message(Command("today"))
    async def today(message: Message) -> None:
        await send_dates(message, period("today"), trigger="command:today", update_id=message.message_id)

    @router.message(Command("tomorrow"))
    async def tomorrow(message: Message) -> None:
        await send_dates(message, period("tomorrow"), trigger="command:tomorrow", update_id=message.message_id)

    @router.message(Command("week"))
    async def week(message: Message) -> None:
        if can_read_schedule(settings, message):
            await message.answer("Choose a day this school week or next.",
                                 reply_markup=schedule_keyboard())

    @router.message(Command("homework"))
    async def homework(message: Message, command: CommandObject) -> None:
        if not can_read_schedule(settings, message):
            return
        try:
            target = parse_user_date(command.args) if command.args else None
        except ValueError:
            await message.answer(HOMEWORK_USAGE)
            return
        await send_homework_view(message, target)

    @router.callback_query(lambda query: bool(query.data) and (
        query.data in {"schedule:today", "schedule:tomorrow", "schedule:week", "schedule:sat", "schedule:sun"}
        or query.data.startswith(("schedule:date:", "schedule:week:"))
    ))
    async def schedule_button(query: CallbackQuery) -> None:
        message = query.message
        if message is None or not hasattr(message, "answer") or not can_read_schedule(settings, message, query.from_user.id):
            await query.answer("Access denied", show_alert=True)
            return
        if (message.chat.type in {"group", "supergroup"} and is_stale(message.date)
                and not query.data.startswith(("schedule:date:", "schedule:week:"))):
            await query.answer("This button has expired. Send a new command.", show_alert=True)
            return
        if query.data in {"schedule:week", "schedule:sat", "schedule:sun"}:
            await query.answer("This button has expired. Use /start to choose a day.", show_alert=True)
            return
        await refresh_menu(message)
        if query.data.startswith("schedule:week:"):
            raw_date = query.data.removeprefix("schedule:week:")
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                    raise ValueError
                selected = date.fromisoformat(raw_date)
                if selected.weekday() != 0:
                    raise ValueError
            except ValueError:
                await query.answer("Invalid week", show_alert=True)
                return
            await query.answer()
            await message.answer(f"School week {selected:%d.%m.%Y}–{selected + timedelta(days=4):%d.%m.%Y}. Choose a day.",
                                 reply_markup=schedule_keyboard(week_start=selected))
            return
        if query.data.startswith("schedule:date:"):
            raw_date = query.data.removeprefix("schedule:date:")
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date):
                    raise ValueError
                target = date.fromisoformat(raw_date)
            except ValueError:
                await query.answer("Invalid date", show_alert=True)
                return
            if target.weekday() >= 5:
                await query.answer("This weekend button has expired. Use /schedule DD-MM-YY.", show_alert=True)
                return
        else:
            target = period(query.data.removeprefix("schedule:"))
        await query.answer()
        await send_dates(message, target, user_id=query.from_user.id,
                         trigger="callback", update_id=query.id)

    @router.callback_query(lambda query: bool(query.data) and query.data.startswith("src:"))
    async def sources_button(query: CallbackQuery) -> None:
        message = query.message
        if message is None or not hasattr(message, "answer") or not can_read_schedule(settings, message, query.from_user.id):
            await query.answer("Access denied", show_alert=True)
            return
        await refresh_menu(message)
        if message.reply_markup and any(
            button.text == "Sources shown" for row in message.reply_markup.inline_keyboard for button in row
        ):
            await query.answer("Sources already shown", show_alert=True)
            return
        parts = query.data.split(":")
        async with session_factory() as session:
            chat = await session.scalar(select(SourceChat).where(SourceChat.telegram_peer_id == settings.source_chat_id))
            refs = None
            if chat and len(parts) == 6 and parts[1] == "d" and parts[2].isdigit() and parts[3] in {"d", "w"}:
                try:
                    as_of = datetime.strptime(parts[4], "%y%m%d").date()
                except ValueError:
                    as_of = None
                day = await session.get(ScheduleDay, int(parts[2])) if as_of else None
                if day and day.chat_id == chat.id:
                    history = await homework_history(session, settings.source_chat_id, as_of)
                    days = await schedule_rows(session, settings.source_chat_id, date(2000, 1, 1), day.schedule_date)
                    assignments = day_assignments(day, history, [row for row, _ in days])
                    if day_source_callback(day, parts[3], as_of, assignments) == query.data:
                        refs = source_references(day, assignments)
            elif chat and len(parts) == 4 and parts[1] == "h" and parts[2].isdigit():
                item = await session.get(Homework, int(parts[2]))
                if item and item.chat_id == chat.id and homework_source_callback(item) == query.data:
                    refs = source_references(None, [(None, (item, None, None), None, False, None)])
            if refs is None:
                await query.answer("This response has expired; request the schedule again", show_alert=True)
                return
            lines = await source_lines(session, chat.id, chat.telegram_peer_id, refs)
        await query.answer()
        for part in split_day("Sources", lines, separator="\n"):
            await message.answer(part)
        await message.edit_reply_markup(reply_markup=schedule_keyboard(query.data, shown=True))

    @router.message(Command("chatid"))
    async def chat_id(message: Message) -> None:
        if is_owner(settings, message):
            await message.answer(f"Chat ID: {message.chat.id}")

    @router.message(Command("channelid"))
    async def channel_id(message: Message) -> None:
        if not is_owner(settings, message) or message.chat.type != "private":
            return
        origin = message.reply_to_message.forward_origin if message.reply_to_message else None
        if not isinstance(origin, MessageOriginChannel):
            await message.answer("Forward a channel post here and reply with /channelid. If forwarding is restricted, temporarily enable it in channel settings.")
            return
        me = await message.bot.get_me()
        member = await message.bot.get_chat_member(origin.chat.id, me.id)
        can_post = member.status == "administrator" and bool(getattr(member, "can_post_messages", False))
        await message.answer(f"Channel ID: {origin.chat.id}\nBot can publish: {'yes' if can_post else 'no'}")

    @router.message(Command("review"))
    async def review_command(message: Message, command: CommandObject) -> None:
        if not is_owner(settings, message) or message.chat.type != "private":
            return
        from app.homework_review import needs_owner_review, notify_one
        if command.args and command.args.strip().isdigit():
            result = await notify_one(session_factory, message.bot, settings.owner_user_id,
                                      int(command.args.strip()), force=True)
            await message.answer("Review request sent." if result == "sent" else f"Review: {result}.")
            return
        async with session_factory() as session:
            rows = (await session.execute(select(Homework, SourceChat.telegram_peer_id)
                .join(SourceChat, Homework.chat_id == SourceChat.id)
                .order_by(Homework.updated_at.desc()).limit(30))).all()
        pending = [(item, peer) for item, peer in rows if needs_owner_review(item)][:10]
        if not pending:
            await message.answer("No homework awaiting review found.")
            return
        await message.answer("Homework awaiting review:\n" + "\n".join(
            f"#{item.id} · {source_link(peer, item.root_message_id)} — {item.reason}"
            for item, peer in pending) + "\nOpen: /review ID")

    @router.callback_query(lambda query: bool(query.data) and query.data.startswith("hwreview:"))
    async def review_button(query: CallbackQuery) -> None:
        if (query.from_user.id != settings.owner_user_id or query.message is None
                or query.message.chat.type != "private" or query.message.chat.id != settings.owner_user_id):
            await query.answer("Access denied", show_alert=True)
            return
        from app.homework_review import accept_review, begin_edit, dismiss_review
        parts = query.data.split(":")
        if len(parts) != 3 or parts[1] not in {"accept", "edit", "dismiss"} or not parts[2].isdigit():
            await query.answer("Invalid button", show_alert=True)
            return
        homework_id = int(parts[2])
        async with session_factory() as session:
            if parts[1] == "accept":
                result = await accept_review(session, homework_id, query.message.message_id)
            elif parts[1] == "dismiss":
                result = await dismiss_review(session, homework_id, query.message.message_id)
            else:
                result = await begin_edit(session, homework_id, query.message.message_id)
        if not result:
            await query.answer("Review expired or already resolved", show_alert=True)
            return
        await query.answer({"accept": "Homework confirmed", "dismiss": "Marked as not homework",
                            "edit": "Awaiting correction"}[parts[1]])
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except TelegramAPIError:
            pass
        if parts[1] == "edit":
            prompt = await query.message.answer(
                "Reply to this message with the assignment text. To change its subject, start with: "
                "Subject: full name. If the teacher gave a deadline, end with: "
                "Due: DD.MM.YYYY. Otherwise the deadline remains unspecified.",
                reply_markup=ForceReply(selective=True),
            )
            async with session_factory() as session, session.begin():
                notice = await session.get(HomeworkReviewNotice, homework_id)
                if notice and notice.status == "awaiting_edit":
                    notice.reply_message_id = prompt.message_id

    @router.message(lambda message: bool(message.reply_to_message))
    async def review_reply(message: Message) -> None:
        if not is_owner(settings, message) or message.chat.type != "private" or not message.text:
            return
        from app.homework_review import finish_edit
        async with session_factory() as session:
            handled, result = await finish_edit(session, message.reply_to_message.message_id, message.text)
        if handled or result != "The correction request has expired.":
            await message.answer(result)

    return router


async def poll(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.delete_webhook(drop_pending_updates=False)
    await register_commands(bot)
    await dispatcher.start_polling(
        bot, allowed_updates=dispatcher.resolve_used_update_types(), close_bot_session=False,
    )


async def run() -> None:
    settings = BotSettings.read()
    db = engine()
    bot = None
    try:
        bot = Bot(settings.token)
        dispatcher = Dispatcher()
        factory = async_sessionmaker(db, expire_on_commit=False)
        dispatcher.include_router(create_router(settings, factory))
        from app.homework_review import review_loop
        review_task = asyncio.create_task(review_loop(factory, bot, settings.owner_user_id))
        async def update_posts() -> None:
            from app.channel import publication_delay, refresh_publications
            while True:
                try:
                    await refresh_publications(factory, bot, settings)
                except Exception as exc:
                    print(f"Publication refresh failed: {type(exc).__name__}", flush=True)
                await asyncio.sleep(publication_delay(datetime.now(LOCAL_TIME)))
        update_task = asyncio.create_task(update_posts())
        try:
            await poll(bot, dispatcher)
        finally:
            review_task.cancel()
            update_task.cancel()
            await asyncio.gather(review_task, update_task, return_exceptions=True)
    finally:
        if bot is not None:
            await bot.session.close()
        await db.dispose()


def main() -> None:
    try:
        asyncio.run(run())
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Bot stopped: {exc}") from None
    except KeyboardInterrupt:
        print("Bot stopped.")
    except Exception as exc:
        raise SystemExit(f"Bot stopped after {type(exc).__name__}; token was not printed.") from None


if __name__ == "__main__":
    main()
