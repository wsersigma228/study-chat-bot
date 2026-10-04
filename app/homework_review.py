"""Owner-only review of homework that could not be read reliably."""

import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramNetworkError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, FSInputFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.homework import catalog, owner_override_parts, update_owner_override
from app.study_view import sendable_path
from app.telegram_sources import source_link
from app.models import Homework, HomeworkReviewNotice, SourceChat
from app.schedule import subject_aliases

REVIEW_REASONS = {"unread_photo", "subject_not_established"}


def needs_owner_review(item: Homework) -> bool:
    if item.status != "needs_review" or item.reason not in REVIEW_REASONS:
        return False
    fields, meta = owner_override_parts(item.owner_override)
    if "approved_sources" in meta:
        return meta["approved_sources"] != item.sources
    return not fields.get("status")


def parse_correction(text: str, current_subject: str | None) -> dict:
    """A plain reply is the task; optional Subject and Due lines change metadata."""
    lines = [line.strip() for line in text.strip().splitlines()]
    if not lines or len(text) > 3000:
        raise ValueError("Enter assignment text up to 3000 characters.")
    subject = current_subject
    if lines[0].casefold().startswith("subject:"):
        name = lines.pop(0).split(":", 1)[1].strip()
        subject = subject_aliases().get(" ".join(name.casefold().split()))
        if subject is None:
            raise ValueError("Subject not found in the catalog. Enter its full name.")
    due = None
    due_supplied = False
    if lines and lines[-1].casefold().startswith("due:"):
        value = lines.pop().split(":", 1)[1].strip()
        due_supplied = True
        if value.casefold() not in {"unspecified", "none"}:
            try:
                due = datetime.strptime(value, "%d.%m.%Y").date()
            except ValueError as exc:
                raise ValueError("Due: DD.MM.YYYY or Due: unspecified.") from exc
    task = "\n".join(lines).strip()
    if not task or not subject:
        raise ValueError("Enter the subject and assignment text.")
    result = {"status": "confirmed", "subject_key": subject, "text": task}
    if due_supplied:
        result.update(due_date=due.isoformat() if due else None, due_text=None)
    return result


async def notify_one(factory: async_sessionmaker[AsyncSession], bot: Bot, owner_id: int,
                     homework_id: int, *, force: bool = False) -> str:
    async with factory() as session, session.begin():
        item = await session.get(Homework, homework_id, with_for_update=True)
        if item is None or not needs_owner_review(item):
            return "resolved"
        chat = await session.get(SourceChat, item.chat_id)
        notice = await session.get(HomeworkReviewNotice, homework_id)
        now = datetime.now(timezone.utc)
        if notice and notice.source_updated_at == item.updated_at and not force:
            if notice.status not in {"pending", "sending", "uncertain"}:
                return notice.status
            if now - notice.updated_at < timedelta(minutes=5):
                return notice.status
        if notice is None:
            notice = HomeworkReviewNotice(homework_id=homework_id, source_updated_at=item.updated_at,
                                          status="sending", notification_message_id=None,
                                          reply_message_id=None, updated_at=now)
            session.add(notice)
        else:
            notice.source_updated_at = item.updated_at
            notice.status = "sending"
            notice.notification_message_id = None
            notice.reply_message_id = None
            notice.updated_at = now
        source_updated_at = item.updated_at
        names, _ = catalog()
        subject = names.get(item.subject_key, "unknown")
        link = source_link(chat.telegram_peer_id if chat else None, item.root_message_id)
        body = (f"Homework needs review · #{item.id}\nSubject: {subject}\n"
                f"Reason: {'text only in a photo' if item.reason == 'unread_photo' else 'subject unknown'}\n"
                f"{item.text[:1200]}\nSource: {link}\n"
                "Accept to confirm available content without setting a deadline. "
                "Edit to reply with assignment text.")
        photos = [sendable_path(media) for media in item.attachments if media.get("kind") == "photo"]
        can_accept = bool(item.subject_key)

    buttons = [InlineKeyboardButton(text="✏️ Edit", callback_data=f"hwreview:edit:{homework_id}"),
               InlineKeyboardButton(text="Not homework", callback_data=f"hwreview:dismiss:{homework_id}")]
    if can_accept:
        buttons.insert(0, InlineKeyboardButton(text="✅ Accept", callback_data=f"hwreview:accept:{homework_id}"))
    keyboard = InlineKeyboardMarkup(inline_keyboard=[buttons])
    try:
        sent = await bot.send_message(owner_id, body, reply_markup=keyboard)
    except TelegramNetworkError:
        status = "uncertain"  # A timed-out send may already be in the owner's chat.
        sent = None
    except TelegramForbiddenError:
        status = "unreachable"
        sent = None
    except TelegramAPIError:
        status = "pending"
        sent = None
    else:
        status = "sent"
    async with factory() as session, session.begin():
        notice = await session.get(HomeworkReviewNotice, homework_id)
        if notice and notice.status == "sending" and notice.source_updated_at == source_updated_at:
            notice.status = status
            notice.notification_message_id = sent.message_id if sent else None
            notice.updated_at = datetime.now(timezone.utc)
    if sent:
        for path in photos:
            if path:
                try:
                    await bot.send_photo(owner_id, FSInputFile(path), reply_to_message_id=sent.message_id)
                except TelegramAPIError:
                    pass  # The source link remains available in the review message.
    return status


async def notify_pending(factory: async_sessionmaker[AsyncSession], bot: Bot, owner_id: int) -> None:
    async with factory() as session:
        ids = (await session.scalars(select(Homework.id).where(
            Homework.status == "needs_review", Homework.reason.in_(REVIEW_REASONS),
        ).order_by(Homework.updated_at, Homework.id))).all()
    for homework_id in ids:
        await notify_one(factory, bot, owner_id, homework_id)


async def review_loop(factory: async_sessionmaker[AsyncSession], bot: Bot, owner_id: int) -> None:
    while True:
        try:
            await notify_pending(factory, bot, owner_id)
        except Exception as exc:
            print(f"Homework review notification failed: {type(exc).__name__}", flush=True)
        await asyncio.sleep(30)


async def accept_review(session: AsyncSession, homework_id: int, notification_id: int) -> bool:
    async with session.begin():
        item = await session.get(Homework, homework_id, with_for_update=True)
        notice = await session.get(HomeworkReviewNotice, homework_id, with_for_update=True)
        if (item is None or notice is None or not needs_owner_review(item)
                or notice.status != "sent" or notice.notification_message_id != notification_id
                or notice.source_updated_at != item.updated_at or not item.subject_key):
            return False
        item.owner_override = update_owner_override(item, {
            "status": "confirmed", "subject_key": item.subject_key,
            "text": ("Assignment in a photo; content confirmed by the owner"
                     if item.reason == "unread_photo" else item.text),
        })
        item.updated_at = datetime.now(timezone.utc)
        notice.status = "resolved"
        notice.updated_at = item.updated_at
        return True


async def dismiss_review(session: AsyncSession, homework_id: int, notification_id: int) -> bool:
    async with session.begin():
        item = await session.get(Homework, homework_id, with_for_update=True)
        notice = await session.get(HomeworkReviewNotice, homework_id, with_for_update=True)
        if (item is None or notice is None or not needs_owner_review(item)
                or notice.status != "sent" or notice.notification_message_id != notification_id
                or notice.source_updated_at != item.updated_at):
            return False
        item.owner_override = update_owner_override(item, {"status": "dismissed"})
        item.updated_at = datetime.now(timezone.utc)
        notice.status = "resolved"
        notice.updated_at = item.updated_at
        return True


async def begin_edit(session: AsyncSession, homework_id: int, notification_id: int) -> bool:
    async with session.begin():
        item = await session.get(Homework, homework_id, with_for_update=True)
        notice = await session.get(HomeworkReviewNotice, homework_id, with_for_update=True)
        if (item is None or notice is None or not needs_owner_review(item)
                or notice.status != "sent" or notice.notification_message_id != notification_id
                or notice.source_updated_at != item.updated_at):
            return False
        notice.status = "awaiting_edit"
        notice.updated_at = datetime.now(timezone.utc)
        return True


async def finish_edit(session: AsyncSession, prompt_id: int, text: str) -> tuple[bool, str]:
    async with session.begin():
        notice = await session.scalar(select(HomeworkReviewNotice).where(
            HomeworkReviewNotice.reply_message_id == prompt_id,
            HomeworkReviewNotice.status == "awaiting_edit",
        ).with_for_update())
        if notice is None:
            return False, "The correction request has expired."
        item = await session.get(Homework, notice.homework_id, with_for_update=True)
        if item is None or notice.source_updated_at != item.updated_at or not needs_owner_review(item):
            return False, "The source message changed. Open a new review."
        try:
            correction = parse_correction(text, item.subject_key)
        except ValueError as exc:
            return False, str(exc)
        item.owner_override = update_owner_override(item, correction)
        item.updated_at = datetime.now(timezone.utc)
        notice.status = "resolved"
        notice.updated_at = item.updated_at
        return True, "Homework confirmed with your corrections."
