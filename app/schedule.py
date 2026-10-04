"""Deterministic text schedule parsing and projection from saved messages."""

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ChannelPost, ChannelState, Message, ScheduleDay, ScheduleReview, SourceChat
from app.telegram_settings import LOCAL_TIME


def upcoming_monday(now: datetime) -> date:
    today = now.astimezone(LOCAL_TIME).date()
    return today + timedelta(days=(-today.weekday()) % 7)


def estimate_ready(target: date, now: datetime) -> bool:
    cutoff = datetime.combine(target - timedelta(days=3 if target.weekday() == 0 else 1), time(22), LOCAL_TIME)
    return target.weekday() < 5 and now.astimezone(LOCAL_TIME) >= cutoff


async def displayed_day(session: AsyncSession, chat_id: int, target: date,
                        now: datetime | None = None) -> ScheduleDay | None:
    """An estimate is a view, never a verified row in schedule_days."""
    day = await session.scalar(select(ScheduleDay).where(
        ScheduleDay.chat_id == chat_id, ScheduleDay.schedule_date == target))
    if day and day.payload.get("state") == "verified":
        return day
    if not estimate_ready(target, now or datetime.now(LOCAL_TIME)):
        return None
    for weeks in (2, 1):
        past = target - timedelta(weeks=weeks)
        source = await session.scalar(select(ScheduleDay).where(
            ScheduleDay.chat_id == chat_id, ScheduleDay.schedule_date == past))
        if source is not None and source.payload.get("state") == "verified":
            return ScheduleDay(chat_id=chat_id, schedule_date=target,
                               payload={**source.payload, "state": "estimated", "estimate_source_date": past.isoformat()})
    return None


NUMBERED = re.compile(r"^\s*(\d+)(?:\s*[.)]\s*|\s+)(.+?)\s*$")
REPLACEMENT = re.compile(r"^\s*(?:lesson\s+)?(\d+)\s+(?:will\s+be\s+in|in)\s+room\s+(\d{2,4})\s*(.*?)\s*$", re.I)
ROOM = r"(?:\d{2,4}|gym)"
TRAILING_ROOM = re.compile(rf"^(.+?)\s*(?:[-–—]\s*(?:room\s+)?|\s+in\s+(?:room\s+)?|\s+(?:room\s+)?)({ROOM})\.?\s*$", re.I)
EXPLICIT_DATE = re.compile(r"schedule\s+for\s+(\d{1,2})\.(\d{1,2})\.(\d{4})", re.I)
TOMORROW = re.compile(r"schedule\s+for\s+tomorrow", re.I)
TODAY = re.compile(r"schedule\s+for\s+today", re.I)


@dataclass
class Event:
    message: Message
    kind: str
    schedule_date: date | None
    date_basis: str | None
    slots: list[dict]
    context_ids: list[int]
    trusted: bool
    reason: str | None = None


def subject_aliases(path: Path = Path("config/subjects.json")) -> dict[str, str]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    aliases = {}
    for subject in catalog["subjects"]:
        for name in (subject["name"], *subject["aliases"]):
            normalized = " ".join(name.casefold().split())
            if normalized in aliases and aliases[normalized] != subject["key"]:
                raise ValueError(f"Conflicting subject alias: {name}")
            aliases[normalized] = subject["key"]
    return aliases


def lesson(raw: str, aliases: dict[str, str]) -> dict:
    raw = raw.strip()
    match = TRAILING_ROOM.fullmatch(raw)
    subject = match.group(1).strip() if match else raw
    room = match.group(2).strip() if match else None
    key = aliases.get(" ".join(subject.casefold().split()))
    return {"subject_key": key, "raw_subject": subject, "room": room}


def full_date(message: Message, prior: Message | None) -> tuple[date | None, str | None, list[int], str | None]:
    text = message.text
    match = EXPLICIT_DATE.search(text)
    if match:
        try:
            return date(int(match[3]), int(match[2]), int(match[1])), "explicit_text", [], None
        except ValueError:
            return None, "explicit_text", [], "invalid_date"
    local_date = message.sent_at.astimezone(LOCAL_TIME).date()
    if TOMORROW.search(text):
        return local_date + timedelta(days=1), "explicit_relative", [], None
    if TODAY.search(text):
        return local_date, "explicit_relative", [], None
    if prior and prior.deleted_at is None:
        gap = message.sent_at - prior.sent_at
        question = prior.text.casefold()
        if (timedelta(0) <= gap <= timedelta(minutes=5)
                and "schedule" in question and "tomorrow" in question and "?" in question):
            return prior.sent_at.astimezone(LOCAL_TIME).date() + timedelta(days=1), "context", [prior.telegram_message_id], None
    return None, None, [], "date_not_established"


def parse_message(
    message: Message, prior: Message | None, full_dates: dict[date, int],
    aliases: dict[str, str], publisher_ids: set[int],
) -> Event | None:
    if message.message_type != "message" or message.deleted_at is not None or not message.text.strip():
        return None
    lines = message.text.splitlines()
    numbered = [match for line in lines if (match := NUMBERED.fullmatch(line))]
    replacement = next((match for line in lines if (match := REPLACEMENT.fullmatch(line))), None)
    has_header = "schedule" in message.text.casefold()
    trusted = message.sender_telegram_user_id in publisher_ids

    if len(numbered) >= 2:
        slots = [{"number": int(match[1]), **lesson(match[2], aliases)} for match in numbered]
        recognized = sum(slot["subject_key"] is not None for slot in slots)
        if not has_header and recognized < 2:
            return None
        target, basis, context, date_error = full_date(message, prior)
        reasons = []
        if [slot["number"] for slot in slots] != list(range(1, len(slots) + 1)):
            reasons.append("nonconsecutive_slots")
        if any(slot["subject_key"] is None for slot in slots):
            reasons.append("unknown_subject")
        if date_error:
            reasons.append(date_error)
        if not trusted:
            reasons.append("unconfirmed_publisher")
        return Event(message, "full", target, basis, slots, context, trusted, ", ".join(reasons) or None)

    if replacement:
        slot = {"number": int(replacement[1]), **lesson(replacement[3], aliases)} if replacement[3] else {
            "number": int(replacement[1]), "subject_key": None, "raw_subject": None, "room": None,
        }
        slot["room"] = replacement[2]
    elif len(numbered) == 1:
        slot = {"number": int(numbered[0][1]), **lesson(numbered[0][2], aliases)}
    else:
        return None
    if not trusted and not (has_header or "replacement" in message.text.casefold()):
        return None

    local_date = message.sent_at.astimezone(LOCAL_TIME).date()
    explicit, basis, context, date_error = full_date(message, prior) if has_header else (None, None, [], None)
    target = explicit or (local_date if local_date in full_dates else None)
    if target and not explicit:
        basis, context = "context_same_day", [full_dates[target]]
    reasons = []
    if has_header:
        reasons.append("incomplete_full_schedule")
    if target is None:
        reasons.append(date_error or "no_full_schedule_for_day")
    if slot["subject_key"] is None and slot["room"] is None:
        reasons.append("unknown_subject_and_room")
    elif slot["raw_subject"] and slot["subject_key"] is None:
        reasons.append("unknown_subject")
    if not trusted:
        reasons.append("unconfirmed_publisher")
    return Event(message, "patch", target, basis, [slot], context, trusted, ", ".join(reasons) or None)


def project(chat: SourceChat, messages: list[Message], aliases: dict[str, str]) -> tuple[dict[date, dict], list[Event], dict[str, int]]:
    """Rebuild current days in source order; old runs cannot undo later patches."""
    days: dict[date, dict] = {}
    reviews: list[Event] = []
    full_dates: dict[date, int] = {}
    revisions = {str(item.telegram_message_id): item.current_revision for item in messages}
    counts = {"full": 0, "patch": 0}
    publisher_ids = set(getattr(chat, "additional_schedule_publisher_user_ids", None) or [])
    if chat.schedule_publisher_user_id is not None:
        publisher_ids.add(chat.schedule_publisher_user_id)
    prior = None
    for message in messages:
        event = parse_message(message, prior, full_dates, aliases, publisher_ids)
        if message.message_type == "message" and message.text.strip() and message.deleted_at is None:
            prior = message
        if event is None:
            continue
        if event.reason:
            reviews.append(event)
            if event.trusted and event.schedule_date in days:
                days[event.schedule_date]["state"] = "needs_review"
                days[event.schedule_date]["warnings"].append({"message_id": event.message.telegram_message_id, "reason": event.reason})
            continue
        if event.kind == "full":
            full_dates[event.schedule_date] = message.telegram_message_id
            days[event.schedule_date] = {
                "state": "verified", "date_basis": event.date_basis,
                "slots": [{**slot, "source_ids": [message.telegram_message_id]} for slot in event.slots],
                "source_ids": [message.telegram_message_id],
                "context_ids": event.context_ids.copy(),
                "inferences": ([{"message_id": message.telegram_message_id, "basis": event.date_basis,
                                "evidence_ids": event.context_ids}] if event.context_ids else []),
                "source_revisions": {str(key): revisions[str(key)] for key in [message.telegram_message_id, *event.context_ids]},
                "warnings": [],
            }
            counts["full"] += 1
        else:
            day = days.get(event.schedule_date)
            slot = next((item for item in day["slots"] if item["number"] == event.slots[0]["number"]), None) if day else None
            if slot is None:
                event.reason = "slot_missing_in_full_schedule"
                reviews.append(event)
                if day:
                    day["state"] = "needs_review"
                    day["warnings"].append({"message_id": event.message.telegram_message_id, "reason": event.reason})
                continue
            patch = event.slots[0]
            if patch["subject_key"] is not None:
                slot["subject_key"], slot["raw_subject"] = patch["subject_key"], patch["raw_subject"]
            if patch["room"] is not None:
                slot["room"] = patch["room"]
            slot["source_ids"].append(message.telegram_message_id)
            day["source_ids"].append(message.telegram_message_id)
            day["context_ids"] = list(dict.fromkeys([*day["context_ids"], *event.context_ids]))
            if event.context_ids:
                day["inferences"].append({"message_id": message.telegram_message_id,
                                          "basis": event.date_basis, "evidence_ids": event.context_ids})
            day["source_revisions"][str(message.telegram_message_id)] = message.current_revision
            counts["patch"] += 1

    return days, reviews, counts


async def sync_schedule(session: AsyncSession, channel_id: int | None = None) -> dict[str, int]:
    """Reparse this small chat from saved current revisions; one worker at a time."""
    aliases = subject_aliases()
    stats = {"full": 0, "patch": 0, "reviews": 0, "days": 0, "changed_days": 0,
             "changed_reviews": 0, "recovered_posts": 0, "uncertain_posts": 0}
    async with session.begin():
        today = datetime.now(LOCAL_TIME).date()
        active = False
        if channel_id is not None:
            state = await session.get(ChannelState, channel_id)
            active = state is not None
            if state is None:
                session.add(ChannelState(channel_id=channel_id, activated_at=datetime.now(timezone.utc)))
        chats = (await session.scalars(select(SourceChat))).all()
        for chat in chats:
            # ponytail: scan the one small chat; use queued changed IDs if this grows large.
            messages = (await session.scalars(select(Message).where(Message.chat_id == chat.id).order_by(
                Message.sent_at, Message.telegram_message_id,
            ))).all()
            days, reviews, counts = project(chat, messages, aliases)
            stats["full"] += counts["full"]
            stats["patch"] += counts["patch"]
            stats["reviews"] += len(reviews)
            stats["days"] += len(days)
            stored_days = {item.schedule_date: item for item in (await session.scalars(
                select(ScheduleDay).where(ScheduleDay.chat_id == chat.id)
            )).all()}
            for target, payload in days.items():
                stored = stored_days.pop(target, None)
                if stored is None:
                    session.add(ScheduleDay(chat_id=chat.id, schedule_date=target, payload=payload, updated_at=datetime.now(timezone.utc)))
                    stats["changed_days"] += 1
                elif stored.payload != payload:
                    stored.payload = payload
                    stored.updated_at = datetime.now(timezone.utc)
                    stats["changed_days"] += 1
                if (channel_id is not None and chat.telegram_peer_id is not None
                        and payload.get("state") == "verified" and (active or target >= today)):
                    post = await session.scalar(select(ChannelPost).where(
                        ChannelPost.channel_id == channel_id, ChannelPost.schedule_date == target,
                    ))
                    if post is None and target >= today:
                        created = await session.scalar(insert(ChannelPost).values(
                            channel_id=channel_id, chat_id=chat.id, schedule_date=target,
                            message_ids=[], content_hash=None, status="pending",
                            updated_at=datetime.now(timezone.utc),
                        ).on_conflict_do_nothing(index_elements=["channel_id", "schedule_date"]).returning(ChannelPost.id))
                        stats["recovered_posts"] += int(created is not None)
                    elif post is not None and post.status in {"removed", "missing_schedule"}:
                        post.status = "pending"
            for stale in stored_days.values():
                if active and channel_id is not None:
                    post = await session.scalar(select(ChannelPost).where(
                        ChannelPost.channel_id == channel_id,
                        ChannelPost.schedule_date == stale.schedule_date,
                    ))
                    if post and post.status == "sent":
                        post.status = "pending"
                await session.delete(stale)
                stats["changed_days"] += 1

            stored_reviews = {item.message_id: item for item in (await session.scalars(
                select(ScheduleReview).join(Message).where(Message.chat_id == chat.id)
            )).all()}
            for event in reviews:
                stored = stored_reviews.pop(event.message.id, None)
                fields = (event.message.current_revision, event.schedule_date, event.reason, event.context_ids)
                if stored is None:
                    session.add(ScheduleReview(
                        message_id=event.message.id, source_revision=fields[0], schedule_date=fields[1],
                        reason=fields[2], context_ids=fields[3], updated_at=datetime.now(timezone.utc),
                    ))
                    stats["changed_reviews"] += 1
                elif (stored.source_revision, stored.schedule_date, stored.reason, stored.context_ids) != fields:
                    stored.source_revision, stored.schedule_date, stored.reason, stored.context_ids = fields
                    stored.updated_at = datetime.now(timezone.utc)
                    stats["changed_reviews"] += 1
            for stale in stored_reviews.values():
                await session.delete(stale)
                stats["changed_reviews"] += 1
        if active:
            stale_sends = (await session.scalars(select(ChannelPost).where(
                ChannelPost.channel_id == channel_id, ChannelPost.status == "sending",
                ChannelPost.updated_at < datetime.now(timezone.utc) - timedelta(minutes=5),
            ))).all()
            for post in stale_sends:
                post.status = "uncertain"
                post.updated_at = datetime.now(timezone.utc)
                stats["uncertain_posts"] += 1
    return stats
