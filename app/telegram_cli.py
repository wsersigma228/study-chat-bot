"""Owner-operated setup and one-group collector commands."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telethon.errors import FloodWaitError

from app.collector import (
    publisher_message_ids, authorized_account, client_for, confirm_publisher,
    confirm_additional_publisher, confirmed_chat,
    export_anchors, group_dialogs, login, match_export, peer_id,
    run_collector, select_group,
)
from app.db import engine
from app.models import Message, SourceChat
from app.telegram_settings import TelegramSettings, load_env


EXPORT_PATH = Path("private/export/result.json")


async def connected_client(settings: TelegramSettings):
    client = client_for(settings)
    await client.connect()
    try:
        account_id = await authorized_account(client, settings.account_user_id)
        return client, account_id
    except Exception:
        await client.disconnect()
        raise


async def bound_dialog(client, peer: int):
    for dialog in await group_dialogs(client):
        if peer_id(dialog) == peer:
            return dialog
    raise ValueError("Confirmed group is absent from this account's dialogs")


def preview(message) -> str:
    text = (message.raw_text or "").replace("\n", " ").replace("\r", " ")
    return json.dumps(text[:100] + ("…" if len(text) > 100 else ""), ensure_ascii=False)


def ask_number(prompt: str) -> int:
    value = input(prompt).strip()
    if not value.lstrip("-").isdigit():
        raise ValueError("Expected a numeric ID")
    return int(value)


async def choose_group(settings: TelegramSettings):
    client, account_id = await connected_client(settings)
    db = engine()
    try:
        dialogs = await group_dialogs(client)
        if not dialogs:
            raise ValueError("No groups found in the authorized account")
        for index, dialog in enumerate(dialogs, 1):
            print(f"{index}. {dialog.title} [peer {peer_id(dialog)}]")
        choice = ask_number("Group number: ")
        if not 1 <= choice <= len(dialogs):
            raise ValueError("Invalid group number")
        dialog = dialogs[choice - 1]
        selected_peer = peer_id(dialog)
        print(f"Selected: {dialog.title}; peer ID: {selected_peer}")
        print("Recent messages (local preview):")
        async for message in client.iter_messages(dialog.entity, limit=3):
            print(f"  {message.id} | {message.date.isoformat()} | sender {message.sender_id} | {preview(message)}")
        typed = ask_number("Type the displayed peer ID to confirm: ")
        async with AsyncSession(db, expire_on_commit=False) as session:
            chat = await select_group(client, account_id, EXPORT_PATH, dialog, typed, session)
            print(f"Matched the existing imported chat; database chat row {chat.id} is now bound.")
            print(f"Put SOURCE_CHAT_ID={selected_peer} in your local .env.")
    finally:
        await db.dispose()
        await client.disconnect()


async def approve_publisher(settings: TelegramSettings):
    client, account_id = await connected_client(settings)
    db = engine()
    try:
        async with AsyncSession(db) as session:
            chat = await confirmed_chat(session, account_id)
            dialog = await bound_dialog(client, chat.telegram_peer_id)
            export, _ = export_anchors(EXPORT_PATH)
            by_id = {item["id"]: item for item in export["messages"]}
            print(f"Schedule sender in {dialog.title}:")
            for message_id in publisher_message_ids():
                live = await client.get_messages(dialog.entity, ids=message_id)
                if live is None or message_id not in by_id:
                    raise ValueError(f"Cannot verify schedule message {message_id}")
                print(f"  message {message_id}: sender user ID {live.sender_id}; {preview(live)}")
            typed = ask_number("Type the publisher user ID to confirm: ")
            if settings.publisher_user_id is not None and settings.publisher_user_id != typed:
                raise ValueError("SCHEDULE_PUBLISHER_USER_ID in .env differs from the ID you entered")
            await confirm_publisher(client, dialog.entity, chat, EXPORT_PATH, typed, session)
            print("Publisher confirmed for this chat. Set SCHEDULE_PUBLISHER_USER_ID locally in .env.")
    finally:
        await db.dispose()
        await client.disconnect()


async def add_publisher(settings: TelegramSettings, message_id: int):
    if message_id <= 0:
        raise ValueError("Message ID must be positive")
    client, account_id = await connected_client(settings)
    db = engine()
    try:
        async with AsyncSession(db) as session:
            chat = await confirmed_chat(session, account_id)
            dialog = await bound_dialog(client, chat.telegram_peer_id)
            live = await client.get_messages(dialog.entity, ids=message_id)
            if live is None:
                raise ValueError("Evidence message is absent from the confirmed group")
            print(f"Message {live.id}; sender user ID {live.sender_id}; sent {live.date.isoformat()}")
            typed = ask_number("Type the confirmed publisher user ID: ")
            await confirm_additional_publisher(chat, live, typed, session)
            print("Additional schedule publisher confirmed for this group.")
    finally:
        await db.dispose()
        await client.disconnect()


async def collect(settings: TelegramSettings):
    client, account_id = await connected_client(settings)
    db = engine()
    try:
        async with db.connect() as lock_connection:
            locked = await lock_connection.scalar(text("SELECT pg_try_advisory_lock(726746201)"))
            await lock_connection.commit()
            if not locked:
                raise ValueError("A collector is already running for this database")
            try:
                factory = async_sessionmaker(db, expire_on_commit=False)
                async with factory() as session:
                    chat = await confirmed_chat(session, account_id)
                    if not chat.schedule_publisher_user_id:
                        raise ValueError("Confirm the schedule publisher before starting the collector")
                    if settings.publisher_user_id is not None and settings.publisher_user_id != chat.schedule_publisher_user_id:
                        raise ValueError("SCHEDULE_PUBLISHER_USER_ID differs from the confirmed publisher")
                    expected_peer = os.getenv("SOURCE_CHAT_ID", "")
                    if not expected_peer.lstrip("-").isdigit() or int(expected_peer) != chat.telegram_peer_id:
                        raise ValueError("SOURCE_CHAT_ID must equal the confirmed peer ID in .env")
                    dialog = await bound_dialog(client, chat.telegram_peer_id)
                print(f"Reading only: {dialog.title}; peer ID: {chat.telegram_peer_id}")
                await run_collector(client, dialog.entity, chat, settings, factory)
            finally:
                await lock_connection.scalar(text("SELECT pg_advisory_unlock(726746201)"))
    finally:
        await db.dispose()
        await client.disconnect()


async def status():
    db = engine()
    try:
        async with AsyncSession(db) as session:
            chats = (await session.scalars(select(SourceChat))).all()
            for chat in chats:
                count = await session.scalar(select(func.count()).select_from(Message).where(Message.chat_id == chat.id))
                maximum = await session.scalar(select(func.max(Message.telegram_message_id)).where(Message.chat_id == chat.id))
                print(f"chat row: {chat.id}; messages: {count}; max message ID: {maximum}")
                print(f"  confirmed peer: {chat.telegram_peer_id or '-'}; account: {chat.collector_account_user_id or '-'}")
                publishers = [item for item in [chat.schedule_publisher_user_id,
                    *(chat.additional_schedule_publisher_user_ids or [])] if item is not None]
                print(f"  publishers: {publishers or '-'}; history checkpoint: {chat.last_history_sync_id or '-'}")
                print(f"  last history sync: {chat.last_history_sync_at or '-'}; last live event: {chat.last_live_event_at or '-'}")
    finally:
        await db.dispose()


def main():
    parser = argparse.ArgumentParser(description="Local Telegram owner setup and collector")
    parser.add_argument("command", choices=("login", "select-group", "confirm-publisher", "add-publisher", "collect", "status"))
    parser.add_argument("--message-id", type=int)
    args = parser.parse_args()
    try:
        if args.command == "status":
            load_env()
            asyncio.run(status())
        else:
            settings = TelegramSettings.read()
            if args.command == "login":
                account_id = asyncio.run(login(settings))
                print(f"Authorized account user ID: {account_id}")
                print("Put this value in TELEGRAM_ACCOUNT_USER_ID in local .env.")
            elif args.command == "select-group":
                asyncio.run(choose_group(settings))
            elif args.command == "confirm-publisher":
                asyncio.run(approve_publisher(settings))
            elif args.command == "add-publisher":
                if args.message_id is None:
                    raise ValueError("add-publisher requires --message-id")
                asyncio.run(add_publisher(settings, args.message_id))
            else:
                asyncio.run(collect(settings))
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Stopped: {exc}\n")
    except FloodWaitError as exc:
        parser.exit(1, f"Telegram requested a {exc.seconds}-second pause; retry later.\n")
    except KeyboardInterrupt:
        print("Collector stopped.")
    except Exception as exc:
        parser.exit(1, f"Stopped after {type(exc).__name__}; no credentials printed.\n")


if __name__ == "__main__":
    main()
