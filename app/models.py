"""Source records and the text schedule projection."""

from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Identity, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SourceChat(Base):
    __tablename__ = "source_chats"
    __table_args__ = (UniqueConstraint("telegram_peer_id", name="uq_source_chats_telegram_peer_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    desktop_export_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    name: Mapped[str | None] = mapped_column(Text)
    chat_type: Mapped[str | None] = mapped_column(String(40))
    telegram_peer_id: Mapped[int | None] = mapped_column(BigInteger)
    collector_account_user_id: Mapped[int | None] = mapped_column(BigInteger)
    schedule_publisher_user_id: Mapped[int | None] = mapped_column(BigInteger)
    additional_schedule_publisher_user_ids: Mapped[list[int]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb"),
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_history_sync_id: Mapped[int | None] = mapped_column(BigInteger)
    last_history_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_live_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (UniqueConstraint("chat_id", "telegram_message_id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"))
    telegram_message_id: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str] = mapped_column(String(30))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sender_ref: Mapped[str | None] = mapped_column(Text)
    sender_telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(Text)
    reply_to_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    forwarded_from: Mapped[str | None] = mapped_column(Text)
    forwarded_from_id: Mapped[str | None] = mapped_column(Text)
    grouped_id: Mapped[int | None] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    current_revision: Mapped[int] = mapped_column(Integer)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRevision(Base):
    __tablename__ = "message_revisions"
    __table_args__ = (UniqueConstraint("message_id", "revision_no"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    revision_no: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    source_edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (UniqueConstraint("message_revision_id", "ordinal"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    message_revision_id: Mapped[int] = mapped_column(ForeignKey("message_revisions.id", ondelete="CASCADE"))
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str | None] = mapped_column(Text)
    mime_type: Mapped[str | None] = mapped_column(Text)
    export_path: Mapped[str | None] = mapped_column(Text)
    declared_size: Mapped[int | None] = mapped_column(BigInteger)
    actual_size: Mapped[int | None] = mapped_column(BigInteger)
    source_status: Mapped[str] = mapped_column(String(30))
    local_status: Mapped[str] = mapped_column(String(30))
    telegram_media_id: Mapped[int | None] = mapped_column(BigInteger)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(50))


class ScheduleDay(Base):
    __tablename__ = "schedule_days"
    __table_args__ = (UniqueConstraint("chat_id", "schedule_date"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"))
    schedule_date: Mapped[date] = mapped_column(Date)
    payload: Mapped[dict] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScheduleReview(Base):
    __tablename__ = "schedule_reviews"

    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True)
    source_revision: Mapped[int] = mapped_column(Integer)
    schedule_date: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text)
    context_ids: Mapped[list] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Homework(Base):
    __tablename__ = "homework"
    __table_args__ = (UniqueConstraint("chat_id", "root_message_id", "part_index"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"))
    root_message_id: Mapped[int] = mapped_column(BigInteger)
    part_index: Mapped[int] = mapped_column(Integer)
    subject_key: Mapped[str | None] = mapped_column(String(80))
    text: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date | None] = mapped_column(Date)
    due_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str | None] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSONB)
    attachments: Mapped[list] = mapped_column(JSONB)
    owner_override: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HomeworkReviewNotice(Base):
    __tablename__ = "homework_review_notices"

    homework_id: Mapped[int] = mapped_column(ForeignKey("homework.id", ondelete="CASCADE"), primary_key=True)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    notification_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reply_message_id: Mapped[int | None] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelState(Base):
    __tablename__ = "channel_states"

    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ChannelPost(Base):
    __tablename__ = "channel_posts"
    __table_args__ = (UniqueConstraint("channel_id", "schedule_date"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)
    destination_type: Mapped[str] = mapped_column(String(30), default="channel", server_default="channel")
    keyboard_date: Mapped[date | None] = mapped_column(Date)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"))
    schedule_date: Mapped[date] = mapped_column(Date)
    message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class BotResponse(Base):
    __tablename__ = "bot_responses"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("source_chats.id", ondelete="CASCADE"))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    destination_type: Mapped[str] = mapped_column(String(30))
    schedule_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(30))
    keyboard_date: Mapped[date] = mapped_column(Date)
    message_ids: Mapped[list[int]] = mapped_column(JSONB, default=list)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
