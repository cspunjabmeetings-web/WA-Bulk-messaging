"""ORM models."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CampaignStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RecipientStatus(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class Setting(Base):
    """Key/value store for settings editable from the UI (API key, delays...)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class BlockReason(str, enum.Enum):
    STOP = "stop"                      # replied STOP / unsubscribe
    ABUSIVE = "abusive"                # rude or vulgar reply
    NOT_INTERESTED = "not_interested"
    OTHER = "other"


class BlockedNumber(Base):
    """Numbers that must never receive a campaign message (opt-out / do-not-contact list)."""

    __tablename__ = "blocked_numbers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # E.164
    reason: Mapped[str] = mapped_column(String(20), default=BlockReason.OTHER.value)
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    image_public_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    image_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_filename: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default=CampaignStatus.DRAFT.value)
    total: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    recipients: Mapped[list["Recipient"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class Recipient(Base):
    __tablename__ = "recipients"
    __table_args__ = (Index("ix_recipient_campaign_status", "campaign_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id", ondelete="CASCADE"))
    phone: Mapped[str] = mapped_column(String(32))  # E.164, e.g. +923001234567
    raw_phone: Mapped[str] = mapped_column(String(64), default="")
    variables: Mapped[dict] = mapped_column(JSON, default=dict)  # row data for {placeholders}
    status: Mapped[str] = mapped_column(String(20), default=RecipientStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
