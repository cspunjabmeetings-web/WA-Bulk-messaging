"""Runtime settings persisted in the DB (editable from the portal UI)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy.orm import Session

from app.models import Setting


@dataclass
class SendingSettings:
    api_key: str = ""                 # WasenderAPI *session* API key
    min_delay_sec: float = 8.0        # random delay between two messages (min)
    max_delay_sec: float = 15.0       # random delay between two messages (max)
    batch_size: int = 40              # after this many messages...
    batch_pause_sec: int = 180        # ...take a longer break
    daily_limit: int = 500            # hard safety cap per campaign per day (0 = off)
    max_retries: int = 3              # retries on network / 5xx errors

    def masked(self) -> dict:
        d = asdict(self)
        key = d.pop("api_key")
        d["api_key_set"] = bool(key)
        d["api_key_hint"] = f"…{key[-4:]}" if len(key) >= 8 else ""
        return d


_NUMERIC = {
    "min_delay_sec": float,
    "max_delay_sec": float,
    "batch_size": int,
    "batch_pause_sec": int,
    "daily_limit": int,
    "max_retries": int,
}


def load(db: Session) -> SendingSettings:
    s = SendingSettings()
    for row in db.query(Setting).all():
        if row.key == "api_key":
            s.api_key = row.value
        elif row.key in _NUMERIC:
            try:
                setattr(s, row.key, _NUMERIC[row.key](row.value))
            except ValueError:
                pass
    return s


def save(db: Session, **values) -> SendingSettings:
    for key, value in values.items():
        if value is None or (key not in _NUMERIC and key != "api_key"):
            continue
        row = db.get(Setting, key) or Setting(key=key)
        row.value = str(value).strip()
        db.add(row)
    db.commit()
    return load(db)
