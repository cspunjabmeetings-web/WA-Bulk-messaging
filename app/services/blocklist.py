"""Blocklist (do-not-contact list) helpers.

Every number is stored in E.164 format (+923001234567), the same format the contact
parser produces, so "0300-1234567" in a new file still matches "+923001234567" here.
"""
from __future__ import annotations

import re
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BlockedNumber, BlockReason
from app.services.contacts import normalize_phone

_CHUNK = 500  # SQLite has a limit on how many values fit in one "IN (...)" query


def blocked_set(db: Session, phones: Iterable[str]) -> set[str]:
    """Return the subset of `phones` that is in the blocklist (one query per 500 numbers)."""
    phones = list(dict.fromkeys(phones))
    found: set[str] = set()
    for i in range(0, len(phones), _CHUNK):
        chunk = phones[i:i + _CHUNK]
        found.update(db.scalars(select(BlockedNumber.phone).where(BlockedNumber.phone.in_(chunk))))
    return found


def is_blocked(db: Session, phone: str) -> bool:
    return db.scalar(select(BlockedNumber.id).where(BlockedNumber.phone == phone)) is not None


def split_raw_numbers(text: str) -> list[str]:
    """Numbers pasted one per line, or separated by commas/semicolons."""
    return [p.strip() for p in re.split(r"[\n,;]+", text or "") if p.strip()]


def add_numbers(db: Session, raw_numbers: Iterable[str], reason: str, note: str = "",
                default_region: str = "PK") -> dict:
    if reason not in {r.value for r in BlockReason}:
        reason = BlockReason.OTHER.value
    valid: dict[str, str] = {}
    invalid: list[str] = []
    for raw in raw_numbers:
        phone = normalize_phone(raw, default_region)
        if phone:
            valid.setdefault(phone, raw)
        else:
            invalid.append(str(raw))

    existing = blocked_set(db, valid)
    new = [p for p in valid if p not in existing]
    db.add_all(BlockedNumber(phone=p, reason=reason, note=(note or "").strip()[:300]) for p in new)
    db.commit()
    return {"added": len(new), "already": len(existing), "invalid": invalid[:50], "invalid_count": len(invalid)}
