"""REST API consumed by the portal UI."""
from __future__ import annotations

import csv
import io
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import BlockedNumber, Campaign, CampaignStatus, Recipient, RecipientStatus
from app.services import blocklist, settings_store
from app.services.campaign_runner import runner
from app.services.contacts import ContactFileError, ParsedContacts, normalize_phone, parse_contacts, render_message
from app.services.wasender import WasenderError, make_client

router = APIRouter(prefix="/api")

ALLOWED_CONTACT_EXT = {".csv", ".xlsx", ".xls", ".txt"}
ALLOWED_IMAGE_EXT = {".jpg", ".jpeg", ".png"}
MAX_IMAGE_MB = 5  # WasenderAPI image limit for send-message


# ------------------------------------------------------------------ helpers
async def _save_upload(file: UploadFile, allowed: set[str], max_mb: int) -> Path:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"File type {ext or '?'} not allowed. Allowed: {', '.join(sorted(allowed))}")
    dest = settings.uploads_dir / f"{uuid.uuid4().hex}{ext}"
    size = 0
    with dest.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_mb * 1024 * 1024:
                out.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File is larger than {max_mb} MB.")
            out.write(chunk)
    return dest


def _upload_path(upload_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}\.(csv|xlsx|xls|txt)", upload_id):
        raise HTTPException(400, "Invalid upload id")
    path = settings.uploads_dir / upload_id
    if not path.exists():
        raise HTTPException(404, "Uploaded file expired. Please upload again.")
    return path


def _apply_blocklist(db: Session, parsed: ParsedContacts) -> list[dict]:
    """Remove blocklisted numbers from parsed.valid (in place). Returns the removed rows."""
    blocked = blocklist.blocked_set(db, (v["phone"] for v in parsed.valid))
    removed = [v for v in parsed.valid if v["phone"] in blocked]
    parsed.valid = [v for v in parsed.valid if v["phone"] not in blocked]
    return removed


def _preview_response(db: Session, parsed: ParsedContacts) -> dict:
    removed = _apply_blocklist(db, parsed)
    return {**parsed.summary(), "blocked_count": len(removed),
            "sample_blocked": [{"phone": v["phone"], "raw": v["raw"]} for v in removed[:10]]}


def _phone_search_term(q: str) -> str:
    """'0300-123' -> '92300123' so a local-format search matches stored +92... numbers."""
    digits = re.sub(r"\D", "", q)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "92" + digits[1:]  # TODO: use DEFAULT_COUNTRY's calling code for non-PK users
    return digits or q.strip()


def _stats(db: Session, campaign_id: int) -> dict:
    rows = db.execute(select(Recipient.status, func.count()).where(Recipient.campaign_id == campaign_id)
                      .group_by(Recipient.status)).all()
    counts = {s.value: 0 for s in RecipientStatus}
    counts.update({status: n for status, n in rows})
    counts["pending"] += counts.pop("sending", 0)
    return counts


def _campaign_dict(db: Session, c: Campaign) -> dict:
    stats = _stats(db, c.id)
    done = stats["sent"] + stats["failed"] + stats["skipped"]
    return {
        "id": c.id, "name": c.name, "message": c.message, "status": c.status,
        "has_image": bool(c.image_path), "source_filename": c.source_filename,
        "total": c.total, "stats": stats,
        "progress": round(100 * done / c.total, 1) if c.total else 0,
        "last_error": c.last_error, "is_active": runner.is_running(c.id),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
    }


def _get_campaign(db: Session, campaign_id: int) -> Campaign:
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "Campaign not found")
    return c


# ------------------------------------------------------------------ settings
class SettingsIn(BaseModel):
    api_key: str | None = None
    min_delay_sec: float | None = Field(None, ge=1, le=600)
    max_delay_sec: float | None = Field(None, ge=1, le=600)
    batch_size: int | None = Field(None, ge=0, le=10_000)
    batch_pause_sec: int | None = Field(None, ge=0, le=7_200)
    daily_limit: int | None = Field(None, ge=0, le=100_000)
    max_retries: int | None = Field(None, ge=0, le=10)


@router.get("/settings")
def get_settings(db: Session = Depends(get_db)):
    data = settings_store.load(db).masked()
    data["mock_mode"] = settings.wasender_mock
    return data


@router.put("/settings")
def update_settings(body: SettingsIn, db: Session = Depends(get_db)):
    values = body.model_dump(exclude_none=True)
    if values.get("api_key", "x") == "":
        values.pop("api_key")
    return settings_store.save(db, **values).masked()


@router.get("/connection")
async def connection_status(db: Session = Depends(get_db)):
    cfg = settings_store.load(db)
    if not cfg.api_key and not settings.wasender_mock:
        return {"connected": False, "status": "no_api_key", "message": "Add your WasenderAPI session API key."}
    try:
        client = make_client(cfg.api_key)
        async with client:
            data = await client.status()
        status = str(data.get("status") or (data.get("data") or {}).get("status") or "unknown")
        return {"connected": status.lower() == "connected", "status": status, "raw": data}
    except WasenderError as exc:
        return {"connected": False, "status": "error", "message": exc.message}


class TestMessageIn(BaseModel):
    phone: str
    message: str = "Test message from WA Bulk Portal ✅"


@router.post("/test-message")
async def send_test_message(body: TestMessageIn, db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone, settings.default_country)
    if not phone:
        raise HTTPException(400, "Invalid phone number")
    cfg = settings_store.load(db)
    try:
        async with make_client(cfg.api_key) as client:
            res = await client.send_message(phone, text=body.message)
        return {"ok": True, "phone": phone, "message_id": res.message_id}
    except WasenderError as exc:
        raise HTTPException(exc.status_code if exc.status_code and exc.status_code < 600 else 502, exc.message)


# ------------------------------------------------------------------ contacts
@router.post("/contacts/preview")
async def preview_contacts(file: UploadFile = File(...), phone_column: str | None = Form(None),
                           db: Session = Depends(get_db)):
    path = await _save_upload(file, ALLOWED_CONTACT_EXT, settings.max_upload_mb)
    try:
        parsed = parse_contacts(path, phone_column or None, settings.default_country)
    except ContactFileError as exc:
        path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc))
    return {"upload_id": path.name, "filename": file.filename, **_preview_response(db, parsed)}


@router.post("/contacts/preview/{upload_id}")
def repreview_contacts(upload_id: str, phone_column: str = Form(...), db: Session = Depends(get_db)):
    path = _upload_path(upload_id)
    try:
        parsed = parse_contacts(path, phone_column, settings.default_country)
    except ContactFileError as exc:
        raise HTTPException(422, str(exc))
    return {"upload_id": upload_id, **_preview_response(db, parsed)}


# ------------------------------------------------------------------ campaigns
@router.post("/campaigns")
async def create_campaign(
    name: str = Form(..., min_length=1, max_length=200),
    upload_id: str = Form(...),
    phone_column: str = Form(...),
    filename: str = Form(""),
    message: str = Form(""),
    start_now: bool = Form(True),
    image: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    message = message.strip()
    has_image = image is not None and bool(image.filename)
    if not message and not has_image:
        raise HTTPException(422, "Write a message or attach an image.")
    if len(message) > 4096:
        raise HTTPException(422, "Message is too long (max 4096 characters).")

    try:
        parsed = parse_contacts(_upload_path(upload_id), phone_column, settings.default_country)
    except ContactFileError as exc:
        raise HTTPException(422, str(exc))
    _apply_blocklist(db, parsed)
    if not parsed.valid:
        raise HTTPException(422, "No numbers left to send: all are invalid or in the blocklist.")

    image_path = None
    if has_image:
        image_path = str(await _save_upload(image, ALLOWED_IMAGE_EXT, MAX_IMAGE_MB))

    c = Campaign(name=name.strip(), message=message, image_path=image_path,
                 source_filename=filename, total=len(parsed.valid))
    db.add(c)
    db.flush()
    db.add_all(Recipient(campaign_id=c.id, phone=v["phone"], raw_phone=v["raw"], variables=v["vars"])
               for v in parsed.valid)
    db.commit()

    if start_now:
        cfg = settings_store.load(db)
        if not cfg.api_key and not settings.wasender_mock:
            raise HTTPException(400, "Campaign saved, but API key missing. Add it in Settings, then press Start.")
        runner.start(c.id)
        db.refresh(c)
    return _campaign_dict(db, c)


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    items = db.scalars(select(Campaign).order_by(Campaign.id.desc()).limit(200)).all()
    return [_campaign_dict(db, c) for c in items]


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: int, db: Session = Depends(get_db)):
    return _campaign_dict(db, _get_campaign(db, campaign_id))


@router.get("/campaigns/{campaign_id}/preview-message")
def preview_message(campaign_id: int, db: Session = Depends(get_db)):
    c = _get_campaign(db, campaign_id)
    r = db.scalars(select(Recipient).where(Recipient.campaign_id == c.id).limit(1)).first()
    return {"text": render_message(c.message, r.variables if r else {})}


@router.post("/campaigns/{campaign_id}/start")
async def start_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = _get_campaign(db, campaign_id)
    if c.status in (CampaignStatus.COMPLETED.value, CampaignStatus.CANCELLED.value):
        raise HTTPException(409, f"Campaign is {c.status}.")
    cfg = settings_store.load(db)
    if not cfg.api_key and not settings.wasender_mock:
        raise HTTPException(400, "Add your WasenderAPI API key in Settings first.")
    runner.start(campaign_id)
    db.expire_all()
    return _campaign_dict(db, _get_campaign(db, campaign_id))


@router.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: int, db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    runner.pause(campaign_id)
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/cancel")
def cancel_campaign(campaign_id: int, db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    runner.cancel(campaign_id)
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/retry-failed")
def retry_failed(campaign_id: int, db: Session = Depends(get_db)):
    c = _get_campaign(db, campaign_id)
    if runner.is_running(campaign_id):
        raise HTTPException(409, "Pause the campaign first.")
    n = 0
    for r in db.scalars(select(Recipient).where(Recipient.campaign_id == c.id,
                                                Recipient.status == RecipientStatus.FAILED.value)):
        r.status, r.error, r.attempts = RecipientStatus.PENDING.value, None, 0
        n += 1
    if n:
        c.status, c.finished_at = CampaignStatus.PAUSED.value, None
    db.commit()
    return {"requeued": n}


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = _get_campaign(db, campaign_id)
    if runner.is_running(campaign_id):
        raise HTTPException(409, "Stop the campaign before deleting it.")
    if c.image_path:
        Path(c.image_path).unlink(missing_ok=True)
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/campaigns/{campaign_id}/recipients")
def list_recipients(campaign_id: int, status: str | None = None, q: str | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                    db: Session = Depends(get_db)):
    _get_campaign(db, campaign_id)
    stmt = select(Recipient).where(Recipient.campaign_id == campaign_id)
    if status:
        stmt = stmt.where(Recipient.status == status)
    if q:
        stmt = stmt.where(Recipient.phone.contains(_phone_search_term(q)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Recipient.id).offset((page - 1) * page_size).limit(page_size)).all()
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [{"id": r.id, "phone": r.phone, "name": (r.variables or {}).get("name", ""),
                   "status": r.status, "attempts": r.attempts, "message_id": r.message_id,
                   "error": r.error, "sent_at": r.sent_at.isoformat() if r.sent_at else None} for r in rows],
    }


@router.get("/campaigns/{campaign_id}/report.csv")
def export_report(campaign_id: int, db: Session = Depends(get_db)):
    c = _get_campaign(db, campaign_id)
    buf = io.StringIO()
    buf.write("﻿")  # Excel-friendly UTF-8
    w = csv.writer(buf)
    w.writerow(["Phone", "Name", "Status", "Attempts", "Message ID", "Error", "Sent At (UTC)"])
    for r in db.scalars(select(Recipient).where(Recipient.campaign_id == c.id).order_by(Recipient.id)):
        w.writerow([r.phone, (r.variables or {}).get("name", ""), r.status, r.attempts, r.message_id or "",
                    r.error or "", r.sent_at.isoformat() if r.sent_at else ""])
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", c.name)[:50] or "campaign"
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="report_{c.id}_{safe}.csv"'})


# ------------------------------------------------------------------ blocklist
REASONS = {"stop", "abusive", "not_interested", "other"}


class BlockIn(BaseModel):
    numbers: str = Field(..., min_length=1, description="One per line, or comma separated")
    reason: str = "other"
    note: str = ""


@router.get("/blocklist")
def list_blocked(q: str | None = None, reason: str | None = None,
                 page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500),
                 db: Session = Depends(get_db)):
    stmt = select(BlockedNumber)
    if q:
        stmt = stmt.where(BlockedNumber.phone.contains(_phone_search_term(q)) | BlockedNumber.note.contains(q))
    if reason:
        stmt = stmt.where(BlockedNumber.reason == reason)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(BlockedNumber.id.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "all_total": db.scalar(select(func.count()).select_from(BlockedNumber)),
            "items": [{"id": b.id, "phone": b.phone, "reason": b.reason, "note": b.note,
                       "created_at": b.created_at.isoformat() if b.created_at else None} for b in rows]}


@router.post("/blocklist")
def add_blocked(body: BlockIn, db: Session = Depends(get_db)):
    if body.reason not in REASONS:
        raise HTTPException(422, "Unknown reason")
    return blocklist.add_numbers(db, blocklist.split_raw_numbers(body.numbers), body.reason, body.note,
                                 settings.default_country)


@router.post("/blocklist/import")
async def import_blocked(file: UploadFile = File(...), reason: str = Form("other"), note: str = Form(""),
                         db: Session = Depends(get_db)):
    path = await _save_upload(file, ALLOWED_CONTACT_EXT, settings.max_upload_mb)
    try:
        parsed = parse_contacts(path, None, settings.default_country)
    except ContactFileError as exc:
        raise HTTPException(422, str(exc))
    finally:
        path.unlink(missing_ok=True)
    result = blocklist.add_numbers(db, [v["phone"] for v in parsed.valid], reason, note, settings.default_country)
    result["invalid_count"] += len(parsed.invalid)
    return result


@router.delete("/blocklist/{block_id}")
def remove_blocked(block_id: int, db: Session = Depends(get_db)):
    b = db.get(BlockedNumber, block_id)
    if not b:
        raise HTTPException(404, "Not found")
    db.delete(b)
    db.commit()
    return {"ok": True}


@router.get("/blocklist/export.csv")
def export_blocked(db: Session = Depends(get_db)):
    buf = io.StringIO()
    buf.write("\ufeff")
    w = csv.writer(buf)
    w.writerow(["Phone", "Reason", "Note", "Added (UTC)"])
    for b in db.scalars(select(BlockedNumber).order_by(BlockedNumber.id)):
        w.writerow([b.phone, b.reason, b.note, b.created_at.isoformat() if b.created_at else ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="blocklist.csv"'})


@router.get("/sample-contacts.csv")
def sample_csv():
    content = "﻿name,phone,city\nAli Khan,03001234567,Lahore\nSara Ahmed,+923211234567,Karachi\n"
    return StreamingResponse(iter([content]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="sample-contacts.csv"'})


@router.get("/health")
def health():
    return {"ok": True, "version": settings.version, "mock": settings.wasender_mock}
