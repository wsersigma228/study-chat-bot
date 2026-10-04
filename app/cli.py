"""Local archive and text schedule commands."""

import argparse
import asyncio
import os
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from aiogram import Bot

from app.db import engine
from app.importer import import_export
from app.homework import sync_homework
from app.models import Attachment, ChannelPost, Homework, Message, MessageRevision, ScheduleDay, ScheduleReview, SourceChat
from app.schedule import LOCAL_TIME, sync_schedule
from app.telegram_settings import load_env
from app.telegram_sources import source_link


async def summary(session: AsyncSession):
    messages = await session.scalar(select(func.count()).select_from(Message))
    revisions = await session.scalar(select(func.count()).select_from(MessageRevision))
    print(f"messages: {messages}")
    print(f"revisions: {revisions}")
    query = (
        select(Attachment.kind, Attachment.source_status, Attachment.local_status, func.count())
        .join(MessageRevision, Attachment.message_revision_id == MessageRevision.id)
        .join(Message, MessageRevision.message_id == Message.id)
        .where(MessageRevision.revision_no == Message.current_revision)
        .group_by(Attachment.kind, Attachment.source_status, Attachment.local_status)
        .order_by(Attachment.kind, Attachment.source_status, Attachment.local_status)
    )
    rows = (await session.execute(query)).all()
    print(f"current attachments: {sum(row[3] for row in rows)}")
    for kind, source, local, count in rows:
        print(f"  {kind}: source={source}, local={local}, count={count}")


async def show(session: AsyncSession, message_id: int):
    rows = (await session.execute(
        select(Message, SourceChat.desktop_export_chat_id)
        .join(SourceChat, Message.chat_id == SourceChat.id)
        .where(Message.telegram_message_id == message_id)
    )).all()
    if not rows:
        raise ValueError(f"Message {message_id} not found")
    if len(rows) != 1:
        raise ValueError(f"Message {message_id} exists in several chats; use one export per database")
    message, chat_id = rows[0]
    print(f"chat export ID: {chat_id}")
    print(f"message ID: {message.telegram_message_id}")
    print(f"type: {message.message_type}; revision: {message.current_revision}")
    print(f"sent (UTC): {message.sent_at.isoformat()}")
    print(f"edited (UTC): {message.edited_at.isoformat() if message.edited_at else '-'}")
    print(f"sender: {message.sender_name or '-'}; sender ID: {message.sender_telegram_user_id or '-'}")
    print(f"reply to ID: {message.reply_to_telegram_id or '-'}")
    print(f"forwarded from: {message.forwarded_from or '-'}")
    print("text:")
    print(message.text)
    attachments = (await session.execute(
        select(Attachment)
        .join(MessageRevision, Attachment.message_revision_id == MessageRevision.id)
        .where(MessageRevision.message_id == message.id, MessageRevision.revision_no == message.current_revision)
        .order_by(Attachment.ordinal)
    )).scalars().all()
    for item in attachments:
        print(
            f"attachment {item.ordinal}: {item.kind}; name={item.name or '-'}; "
            f"source={item.source_status}; local={item.local_status}; "
            f"declared_bytes={item.declared_size if item.declared_size is not None else '-'}"
        )


async def show_schedule(session: AsyncSession, target: date):
    chats = (await session.scalars(select(SourceChat))).all()
    if len(chats) != 1:
        raise ValueError("Expected exactly one source chat")
    chat = chats[0]
    day = await session.scalar(select(ScheduleDay).where(
        ScheduleDay.chat_id == chat.id, ScheduleDay.schedule_date == target,
    ))
    print(f"Schedule for {target.isoformat()} ({LOCAL_TIME.key})")
    if day is None:
        print("No verified text schedule found; this does not mean there are no classes.")
    else:
        payload = day.payload
        print(f"state: {payload['state']}; date basis: {payload['date_basis']}")
        for slot in payload["slots"]:
            room = f" — {slot['room']}" if slot["room"] else ""
            print(f"{slot['number']}. {slot['raw_subject']} [{slot['subject_key']}]{room}")
            for source_id in slot["source_ids"]:
                print(f"   source: {source_link(chat.telegram_peer_id, source_id)}")
        for inference in payload.get("inferences", []):
            evidence = ", ".join(source_link(chat.telegram_peer_id, item) for item in inference["evidence_ids"])
            print(f"context inference for {source_link(chat.telegram_peer_id, inference['message_id'])}: "
                  f"{inference['basis']} from {evidence}")
        for warning in payload["warnings"]:
            print(f"review needed: {source_link(chat.telegram_peer_id, warning['message_id'])}: {warning['reason']}")
    reviews = (await session.execute(
        select(ScheduleReview, Message.telegram_message_id)
        .join(Message, ScheduleReview.message_id == Message.id)
        .where(Message.chat_id == chat.id, ScheduleReview.schedule_date == target)
        .order_by(Message.sent_at, Message.telegram_message_id)
    )).all()
    for review, message_id in reviews:
        if day is None or not any(warning["message_id"] == message_id for warning in day.payload["warnings"]):
            print(f"unresolved source: {source_link(chat.telegram_peer_id, message_id)}: {review.reason}")


async def show_schedule_reviews(session: AsyncSession):
    rows = (await session.execute(
        select(ScheduleReview, Message.telegram_message_id, SourceChat.telegram_peer_id)
        .join(Message, ScheduleReview.message_id == Message.id)
        .join(SourceChat, Message.chat_id == SourceChat.id)
        .order_by(Message.sent_at, Message.telegram_message_id)
    )).all()
    print(f"unresolved schedule candidates: {len(rows)}")
    for review, message_id, peer_id in rows:
        print(f"{review.schedule_date or 'date unknown'} | {source_link(peer_id, message_id)} | {review.reason}")


async def show_homework(session: AsyncSession, reviews_only: bool = False):
    rows = (await session.execute(select(Homework, SourceChat.telegram_peer_id)
        .join(SourceChat, Homework.chat_id == SourceChat.id)
        .order_by(Homework.root_message_id, Homework.part_index))).all()
    for item, peer in rows:
        if reviews_only and item.status == "confirmed":
            continue
        sources = ", ".join(source_link(peer, source["message_id"]) for source in item.sources)
        print(f"{item.id} | {item.status} | {item.subject_key or 'unknown'} | "
              f"{item.due_date or item.due_text or 'Deadline unspecified'} | {sources}")
        if item.reason:
            print(f"  reason: {item.reason}")
        if not reviews_only:
            print(f"  text: {item.text}")
            for attachment in item.attachments:
                print(f"  attachment: {attachment['kind']} | {attachment['name'] or '-'} | "
                      f"{attachment['local_status']} | bot_sendable={attachment['bot_sendable']} | "
                      f"{source_link(peer, attachment['message_id'])}")


async def run(args):
    db = engine()
    load_env()
    channel = os.getenv("BOT_CHANNEL_ID", "").strip()
    if channel and (len(channel) <= 4 or not channel.startswith("-100") or not channel[1:].isdigit()):
        raise ValueError("BOT_CHANNEL_ID must be a numeric channel ID beginning with -100")
    channel_id = int(channel) if channel else None
    if channel_id and channel in {os.getenv("SOURCE_CHAT_ID", "").strip(),
                                  *os.getenv("BOT_ALLOWED_CHAT_IDS", "").replace(",", " ").split()}:
        raise ValueError("BOT_CHANNEL_ID must differ from source and command groups")
    try:
        async with AsyncSession(db) as session:
            if args.command == "import":
                result = await import_export(session, Path(args.export))
                print(f"imported: created={result.created}, changed={result.changed}, unchanged={result.unchanged}")
            elif args.command == "summary":
                await summary(session)
            elif args.command == "show":
                await show(session, args.id)
            elif args.command == "schedule-sync":
                print(await sync_schedule(session))
            elif args.command == "homework-sync":
                print(await sync_homework(session))
            elif args.command == "schedule-watch":
                if args.interval <= 0:
                    raise ValueError("--interval must be positive")
                first = True
                token = os.getenv("BOT_TOKEN", "").strip()
                if channel_id and not token:
                    raise ValueError("Set BOT_TOKEN when BOT_CHANNEL_ID is set")
                bot = Bot(token) if channel_id else None
                try:
                    from sqlalchemy.ext.asyncio import async_sessionmaker
                    from app.channel import publish_pending
                    factory = async_sessionmaker(db, expire_on_commit=False)
                    while True:
                        stats = await sync_schedule(session, channel_id)
                        homework_stats = await sync_homework(session)
                        if (first or stats["changed_days"] or stats["changed_reviews"]
                                or stats["recovered_posts"] or stats["uncertain_posts"] or homework_stats["changed"]):
                            print({"schedule": stats, "homework": homework_stats}, flush=True)
                        first = False
                        if bot:
                            outcomes = await publish_pending(factory, bot, channel_id)
                            if any(key not in {"unchanged", "skipped"} for key in outcomes):
                                print({"channel": outcomes}, flush=True)
                        await asyncio.sleep(args.interval)
                finally:
                    if bot:
                        await bot.session.close()
            elif args.command == "channel-post":
                if not channel_id or not os.getenv("BOT_TOKEN", "").strip():
                    raise ValueError("Set BOT_CHANNEL_ID and BOT_TOKEN locally")
                target = date.fromisoformat(args.date)
                async with session.begin():
                    day = await session.scalar(select(ScheduleDay).where(ScheduleDay.schedule_date == target))
                    if day is None or day.payload.get("state") != "verified":
                        raise ValueError("No verified schedule for this date")
                    post = await session.scalar(select(ChannelPost).where(
                        ChannelPost.channel_id == channel_id, ChannelPost.schedule_date == target,
                    ))
                    if post is None:
                        from datetime import datetime, timezone
                        post = ChannelPost(channel_id=channel_id, chat_id=day.chat_id, schedule_date=target,
                                           message_ids=[], content_hash=None, status="pending",
                                           updated_at=datetime.now(timezone.utc))
                        session.add(post)
                    elif post.status in {"sending", "uncertain", "missing"}:
                        if not args.replace:
                            raise ValueError("Publication is uncertain or missing; inspect the channel and use --replace explicitly")
                        post.message_ids = []
                        post.content_hash = None
                        post.status = "pending"
                    await session.flush()
                    post_id = post.id
                from sqlalchemy.ext.asyncio import async_sessionmaker
                from app.channel import publish_one
                async with Bot(os.environ["BOT_TOKEN"]) as bot:
                    print({"channel_post": await publish_one(async_sessionmaker(db), bot, post_id)})
            elif args.command == "schedule":
                await show_schedule(session, date.fromisoformat(args.date))
            elif args.command in {"homework", "homework-reviews"}:
                await show_homework(session, args.command == "homework-reviews")
            else:
                await show_schedule_reviews(session)
    finally:
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description="Offline Telegram Desktop archive")
    sub = parser.add_subparsers(dest="command", required=True)
    importer = sub.add_parser("import", help="Import a Telegram Desktop result.json")
    importer.add_argument("--export", required=True, help="Path to unpacked result.json")
    sub.add_parser("summary", help="Count messages, revisions, and current attachments")
    viewer = sub.add_parser("show", help="Show one saved message by Telegram ID")
    viewer.add_argument("--id", type=int, required=True)
    sub.add_parser("schedule-sync", help="Rebuild text schedules from saved messages once")
    watcher = sub.add_parser("schedule-watch", help="Keep text schedules current as collector saves messages")
    watcher.add_argument("--interval", type=int, default=10)
    publication = sub.add_parser("channel-post", help="Explicitly publish one historical date or reconcile a lost post")
    publication.add_argument("--date", required=True, help="YYYY-MM-DD")
    publication.add_argument("--replace", action="store_true", help="Replace an uncertain or missing post after checking the channel")
    schedule = sub.add_parser("schedule", help="Show the verified text schedule and sources for a date")
    schedule.add_argument("--date", required=True, help="YYYY-MM-DD")
    sub.add_parser("schedule-reviews", help="List unresolved schedule candidates without message text")
    sub.add_parser("homework-sync", help="Reparse saved message history into current homework")
    sub.add_parser("homework", help="Show saved homework and its source IDs locally")
    sub.add_parser("homework-reviews", help="List uncertain homework candidates without message text")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    except SQLAlchemyError as exc:
        parser.exit(1, f"Database error ({type(exc).__name__}); check DATABASE_URL and migrations.\n")
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
