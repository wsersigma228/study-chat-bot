"""Persist, publish and refresh bot-owned schedule posts and command responses."""

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import (PROCESS_INSTANCE, WEEKDAYS, day_assignments, format_estimate_missing, homework_history, lesson_block,
                     response_view, schedule_keyboard, schedule_rows, source_lines, source_references, units)
from app.models import BotResponse, ChannelPost, ScheduleDay, SourceChat
from app.schedule import LOCAL_TIME, displayed_day, estimate_ready, upcoming_monday

LIMIT = 4096


def publication_delay(now: datetime) -> float:
    """Keep the regular refresh cadence but wake at the forecast cutoff."""
    now = now.astimezone(LOCAL_TIME)
    cutoff = datetime.combine(now.date(), time(22), LOCAL_TIME)
    if cutoff <= now:
        cutoff += timedelta(days=1)
    return min(10, (cutoff - now).total_seconds())


def chunks(header: str, lines: list[str]) -> list[str]:
    """Keep source URLs intact and label every part of a long day."""
    continuation = header + " (continued)"
    if units(continuation) >= LIMIT - 80:
        raise ValueError("Publication header is too long")
    parts = []
    current = header
    for line in lines:
        if not line:
            if units(current) < LIMIT:
                current += "\n"
            continue
        while line:
            room = LIMIT - units(current) - 1
            if room < 80:
                parts.append(current)
                current = continuation
                continue
            if units(line) <= room:
                current += "\n" + line
                break
            # Keep links intact when they fit a fresh page; oversized input must still advance.
            if (line.startswith("https://") or "https://t.me/c/" in line) and units(line) <= LIMIT - units(continuation) - 1:
                parts.append(current)
                current = continuation
                continue
            offset = 0
            while offset < len(line) and units(line[:offset + 1]) <= room:
                offset += 1
            current += "\n" + line[:offset]
            line = line[offset:]
            parts.append(current)
            current = continuation
    parts.append(current)
    return parts


async def render(session: AsyncSession, post: ChannelPost) -> list[str] | None:
    day = await displayed_day(session, post.chat_id, post.schedule_date)
    chat = await session.get(SourceChat, post.chat_id)
    if chat is None or chat.telegram_peer_id is None:
        return None
    if day is None:
        if estimate_ready(post.schedule_date, datetime.now(LOCAL_TIME)):
            return [format_estimate_missing(post.schedule_date)]
        return None
    today = datetime.now(LOCAL_TIME).date()
    as_of = min(post.schedule_date, today)
    history = await homework_history(session, chat.telegram_peer_id, as_of)
    chronology = await schedule_rows(session, chat.telegram_peer_id, date(2000, 1, 1), day.schedule_date)
    assignments = day_assignments(day, history, [row for row, _ in chronology])
    lines = []
    if day.payload.get("state") == "estimated":
        past = date.fromisoformat(day.payload["estimate_source_date"])
        lines.extend((f"⚠️ Estimated schedule. Based on {past:%d.%m.%Y}.",
                      "The schedule for this date is not confirmed yet.", ""))
    for assignment in assignments:
        lines.extend(lesson_block(assignment).splitlines())
        lines.append("")
    refs = source_references(day, [])
    lines.append("Sources:")
    lines.extend(await source_lines(session, chat.id, chat.telegram_peer_id, refs))
    header = f"📅 {post.schedule_date:%d.%m.%Y}, {WEEKDAYS[post.schedule_date.weekday()]}"
    return chunks(header, lines)


async def publish_one(factory: async_sessionmaker[AsyncSession], bot: Bot, post_id: int,
                      *, response: bool = False, answer=None) -> str:
    model = BotResponse if response else ChannelPost
    # Claim in a committed transaction so another worker cannot send the same day.
    async with factory() as session, session.begin():
        post = await session.scalar(select(model).where(model.id == post_id).with_for_update())
        if post is None or post.status in {"sending", "uncertain", "missing"}:
            return "skipped"
        view = None
        if response:
            source = await session.get(SourceChat, post.chat_id)
            if source is None or source.telegram_peer_id is None:
                return "skipped"
            view = await response_view(session, source.telegram_peer_id, post.schedule_date, post.kind)
            parts = view["parts"]
        else:
            parts = await render(session, post)
        digest_input = [*parts, view["callback"] or ""] if response else parts
        digest = hashlib.sha256("\0".join(digest_input).encode()).hexdigest() if parts else None
        if digest == post.content_hash and post.status == "sent":
            return "unchanged"
        old_ids = list(post.message_ids)
        channel_id = post.channel_id
        destination_type = post.destination_type
        keyboard_date = post.keyboard_date
        if not response:
            try:
                chat = await bot.get_chat(channel_id)
            except TelegramAPIError as exc:
                raise ValueError(f"Publication stopped: cannot verify {channel_id} ({type(exc).__name__})") from exc
            matches = chat.type in {"group", "supergroup"} if destination_type == "group" else chat.type == destination_type
            if not matches:
                raise ValueError(f"Publication stopped: {channel_id} is {chat.type}, not {destination_type}")
        schedule_date = post.schedule_date
        day = await session.scalar(select(ScheduleDay).where(
            ScheduleDay.chat_id == post.chat_id, ScheduleDay.schedule_date == schedule_date,
        ))
        source_ids = day.payload.get("source_ids", []) if day else []
        post.status = "sending"
        post.updated_at = datetime.now(timezone.utc)

    new_sent = False
    try:
        if parts is None:
            for message_id in old_ids:
                try:
                    await bot.delete_message(channel_id, message_id)
                except TelegramBadRequest as exc:
                    if "message to delete not found" not in str(exc).lower():
                        raise
            await _finish(factory, post_id, "removed", [], None, model=model)
            return "removed"
        ids = old_ids.copy()
        for index, part in enumerate(parts):
            kwargs = {}
            if response:
                kwargs["reply_markup"] = schedule_keyboard(
                    view["callback"] if index == 0 else None,
                    today=keyboard_date,
                    week_start=schedule_date - timedelta(days=schedule_date.weekday()))
            elif destination_type == "group":
                kwargs["reply_markup"] = schedule_keyboard(today=keyboard_date,
                    week_start=schedule_date - timedelta(days=schedule_date.weekday()))
            if index < len(ids):
                try:
                    await bot.edit_message_text(part, chat_id=channel_id, message_id=ids[index], **kwargs)
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
            else:
                print(json.dumps({"event": "schedule_send", "trigger": "bot_response" if response else "schedule_post",
                                  "date": schedule_date.isoformat(), "chat_id": channel_id,
                                  "chat_type": destination_type, "task_id": post_id,
                                  "source_message_ids": source_ids,
                                  "process": PROCESS_INSTANCE}), flush=True)
                sent = await answer(part, **kwargs) if answer else await bot.send_message(channel_id, part, **kwargs)
                print(json.dumps({"event": "schedule_sent", "trigger": "bot_response" if response else "schedule_post",
                                  "task_id": post_id, "chat_id": channel_id,
                                  "message_id": sent.message_id}), flush=True)
                ids.append(sent.message_id)
                new_sent = True
        for message_id in ids[len(parts):]:
            await bot.delete_message(channel_id, message_id)
        ids = ids[:len(parts)]
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "message to edit not found" in message or "message_id_invalid" in message:
            await _finish(factory, post_id, "missing", model=model)
            return "missing"
        else:
            await _finish(factory, post_id, "uncertain" if new_sent else "pending", model=model)
            return "uncertain" if new_sent else "retry"
    except TelegramNetworkError:
        # A timed-out send might have succeeded. Require owner reconciliation.
        await _finish(factory, post_id, "uncertain", model=model)
        return "uncertain"
    except TelegramAPIError:
        await _finish(factory, post_id, "uncertain" if new_sent else "pending", model=model)
        return "uncertain" if new_sent else "retry"
    await _finish(factory, post_id, "sent", ids, digest, model=model)
    return "sent"


async def _finish(factory, post_id: int, status: str, ids=None, digest=None, *, model=ChannelPost) -> None:
    async with factory() as session, session.begin():
        post = await session.get(model, post_id)
        post.status = status
        if ids is not None:
            post.message_ids = ids
        if digest is not None:
            post.content_hash = digest
        post.updated_at = datetime.now(timezone.utc)


async def publish_pending(factory: async_sessionmaker[AsyncSession], bot: Bot, channel_id: int) -> dict[str, int]:
    async with factory() as session:
        ids = (await session.scalars(select(ChannelPost.id).where(
            ChannelPost.channel_id == channel_id,
            ChannelPost.destination_type == "channel",
            ChannelPost.status.in_(("pending", "sent", "missing_schedule")),
        ).order_by(ChannelPost.schedule_date))).all()
    result = {}
    for post_id in ids:
        try:
            outcome = await publish_one(factory, bot, post_id)
        except (TelegramAPIError, ValueError) as exc:
            # A failed destination check has not sent anything; keep the task retryable.
            print(f"Publication {post_id} failed: {type(exc).__name__}", flush=True)
            outcome = "failed"
        result[outcome] = result.get(outcome, 0) + 1
    return result


async def prepare_schedule_posts(factory, settings, now: datetime | None = None) -> None:
    """Automatic posts go only to the configured channel, never command groups."""
    if not settings.channel_id:
        return
    now = now or datetime.now(LOCAL_TIME)
    today = now.astimezone(LOCAL_TIME).date()
    targets = {today, today + timedelta(days=1), upcoming_monday(now)}
    destinations = [(settings.channel_id, "channel")]
    async with factory() as session, session.begin():
        chat = await session.scalar(select(SourceChat).where(SourceChat.telegram_peer_id == settings.source_chat_id))
        if chat is None:
            return
        for target in sorted(targets):
            if target.weekday() >= 5:
                continue
            day = await session.scalar(select(ScheduleDay).where(
                ScheduleDay.chat_id == chat.id, ScheduleDay.schedule_date == target))
            if target == today and await displayed_day(session, chat.id, target, now) is None:
                continue
            if not (day and day.payload.get("state") == "verified") and not estimate_ready(target, now):
                continue
            for destination, kind in destinations:
                existing = await session.scalar(select(ChannelPost).where(
                    ChannelPost.channel_id == destination, ChannelPost.schedule_date == target))
                if existing is None:
                    await session.execute(insert(ChannelPost).values(
                        channel_id=destination, destination_type=kind, chat_id=chat.id,
                        schedule_date=target, message_ids=[], status="pending",
                        keyboard_date=today, updated_at=datetime.now(timezone.utc),
                    ).on_conflict_do_nothing(index_elements=["channel_id", "schedule_date"]))
                elif existing.status in {"removed", "missing_schedule"}:
                    existing.status = "pending"


async def refresh_publications(factory, bot, settings) -> None:
    await prepare_schedule_posts(factory, settings)
    destinations = {settings.channel_id} if settings.channel_id else set()
    async with factory() as session, session.begin():
        # A crash after a send has an unknown outcome. Never retry it blindly.
        for model in (ChannelPost, BotResponse):
            stale = (await session.scalars(select(model).where(
                model.status == "sending", model.updated_at < datetime.now(timezone.utc) - timedelta(minutes=5),
                model.channel_id.in_(destinations if model is ChannelPost else
                                     set(settings.allowed_chat_ids) | {settings.owner_user_id}),
            ))).all()
            for post in stale:
                post.status = "uncertain"
        posts = (await session.scalars(select(ChannelPost.id).join(SourceChat).where(
            SourceChat.telegram_peer_id == settings.source_chat_id,
            ChannelPost.channel_id.in_(destinations),
            ChannelPost.destination_type == "channel",
            ChannelPost.status.in_(("pending", "sent", "removed", "missing_schedule")),
        ).order_by(ChannelPost.id))).all()
        responses = (await session.scalars(select(BotResponse.id).join(SourceChat).where(
            SourceChat.telegram_peer_id == settings.source_chat_id,
            BotResponse.channel_id.in_(set(settings.allowed_chat_ids) | {settings.owner_user_id}),
            BotResponse.status.in_(("pending", "sent")),
        ).order_by(BotResponse.id))).all()
    # ponytail: scan saved replies for one small group; prune/archive if this grows large.
    for response, ids in ((False, posts), (True, responses)):
        for post_id in ids:
            try:
                await publish_one(factory, bot, post_id, response=response)
            except (TelegramAPIError, ValueError) as exc:
                print(f"Publication {post_id} failed: {type(exc).__name__}", flush=True)
