"""Homework history and lesson assignment, independent of Telegram handlers."""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.homework import effective
from app.models import Homework, Message as StoredMessage, ScheduleDay, SourceChat
from app.telegram_settings import LOCAL_TIME

HomeworkEntry = tuple[Homework, int | None, date | None]
Relation = Literal["explicit", "likely", "uncertain"]


@dataclass(frozen=True)
class Assignment:
    slot: dict | None
    chosen: HomeworkEntry | None = None
    pending: HomeworkEntry | None = None
    repeated: bool = False
    relation: Relation | None = None
    extras: list[tuple[HomeworkEntry, Relation]] = field(default_factory=list)


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


def day_assignments(day: ScheduleDay, history: dict[str, list[HomeworkEntry]],
                    days: list[ScheduleDay]) -> list[Assignment]:
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
            result.append(Assignment(slot=slot, repeated=repeated))
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
        result.append(Assignment(slot=slot, chosen=chosen, pending=pending, relation=relation, extras=extras))
    return result


def assignment_homework(assignment: Assignment):
    """Yield the chosen task and additional tasks with their lesson relations."""
    if assignment.chosen:
        yield assignment.chosen, assignment.relation
    yield from assignment.extras
