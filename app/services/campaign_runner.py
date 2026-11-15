"""Background campaign sender.

Design:
* One asyncio task per running campaign, sending strictly one-by-one (concurrency = 1),
  as WasenderAPI recommends low concurrency to avoid number bans.
* Human-like random delay between messages + a longer pause after every batch.
* HTTP 429 -> wait `retry_after` and retry the same number (not counted as an attempt).
* Network/5xx -> exponential backoff, up to `max_retries`.
* 4xx validation errors (e.g. not on WhatsApp) -> recipient marked failed, campaign continues.
* 401/403 -> campaign paused (bad API key / disconnected session).
* State lives in SQLite, so a restart never loses progress; running campaigns resume as "paused".
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from app.db import session_scope
from app.models import Campaign, CampaignStatus, Recipient, RecipientStatus, utcnow
from app.services import blocklist, settings_store
from app.services.contacts import render_message
from app.services.wasender import WasenderClient, WasenderError, make_client

log = logging.getLogger("runner")
IMAGE_URL_TTL = timedelta(hours=20)  # Wasender media URLs live 24h; refresh a bit earlier


class CampaignRunner:
    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task] = {}
        self._stop_flags: dict[int, str] = {}  # campaign_id -> "pause" | "cancel"

    # ----------------------------------------------------------------- control
    def is_running(self, campaign_id: int) -> bool:
        t = self._tasks.get(campaign_id)
        return bool(t and not t.done())

    def start(self, campaign_id: int) -> None:
        if self.is_running(campaign_id):
            return
        self._stop_flags.pop(campaign_id, None)
        with session_scope() as db:
            c = db.get(Campaign, campaign_id)
            c.status = CampaignStatus.RUNNING.value
            c.last_error = None
            c.started_at = c.started_at or utcnow()
            # anything stuck in "sending" (crash) goes back to pending
            db.execute(update(Recipient)
                       .where(Recipient.campaign_id == campaign_id,
                              Recipient.status == RecipientStatus.SENDING.value)
                       .values(status=RecipientStatus.PENDING.value))
        self._tasks[campaign_id] = asyncio.create_task(self._run(campaign_id), name=f"campaign-{campaign_id}")

    def pause(self, campaign_id: int) -> None:
        self._stop_flags[campaign_id] = "pause"

    def cancel(self, campaign_id: int) -> None:
        self._stop_flags[campaign_id] = "cancel"
        if not self.is_running(campaign_id):
            self._finish(campaign_id, CampaignStatus.CANCELLED)

    async def shutdown(self) -> None:
        for cid in list(self._tasks):
            self._stop_flags[cid] = "pause"
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @staticmethod
    def recover_after_restart() -> None:
        with session_scope() as db:
            db.execute(update(Campaign).where(Campaign.status == CampaignStatus.RUNNING.value)
                       .values(status=CampaignStatus.PAUSED.value,
                               last_error="Portal was restarted – press Resume to continue."))
            db.execute(update(Recipient).where(Recipient.status == RecipientStatus.SENDING.value)
                       .values(status=RecipientStatus.PENDING.value))

    # --------------------------------------------------------------- internals
    def _finish(self, campaign_id: int, status: CampaignStatus, error: str | None = None) -> None:
        with session_scope() as db:
            c = db.get(Campaign, campaign_id)
            if not c:
                return
            c.status = status.value
            if error:
                c.last_error = error
            if status in (CampaignStatus.COMPLETED, CampaignStatus.CANCELLED, CampaignStatus.FAILED):
                c.finished_at = utcnow()

    async def _sleep_interruptible(self, campaign_id: int, seconds: float) -> bool:
        """Sleep but wake early if paused/cancelled. Returns False if interrupted."""
        end = asyncio.get_running_loop().time() + max(0.0, seconds)
        while True:
            if campaign_id in self._stop_flags:
                return False
            remaining = end - asyncio.get_running_loop().time()
            if remaining <= 0:
                return True
            await asyncio.sleep(min(0.5, remaining))

    async def _ensure_image_url(self, client: WasenderClient, campaign_id: int) -> str | None:
        with session_scope() as db:
            c = db.get(Campaign, campaign_id)
            path, url, uploaded = c.image_path, c.image_public_url, c.image_uploaded_at
        if not path:
            return None
        if url and uploaded and utcnow() - uploaded.replace(tzinfo=uploaded.tzinfo or timezone.utc) < IMAGE_URL_TTL:
            return url
        from pathlib import Path

        url = await client.upload_media(Path(path))
        with session_scope() as db:
            c = db.get(Campaign, campaign_id)
            c.image_public_url, c.image_uploaded_at = url, utcnow()
        log.info("Campaign %s: image uploaded -> %s", campaign_id, url)
        return url

    @staticmethod
    def _sent_today(campaign_id: int) -> int:
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with session_scope() as db:
            return db.scalar(select(func.count()).select_from(Recipient).where(
                Recipient.campaign_id == campaign_id,
                Recipient.status == RecipientStatus.SENT.value,
                Recipient.sent_at >= start)) or 0

    async def _run(self, campaign_id: int) -> None:
        with session_scope() as db:
            cfg = settings_store.load(db)
            template = db.get(Campaign, campaign_id).message
        client = make_client(cfg.api_key)
        sent_in_batch = 0
        try:
            image_url = await self._ensure_image_url(client, campaign_id)
            sent_today = self._sent_today(campaign_id)

            while True:
                flag = self._stop_flags.get(campaign_id)
                if flag == "pause":
                    self._finish(campaign_id, CampaignStatus.PAUSED)
                    return
                if flag == "cancel":
                    self._finish(campaign_id, CampaignStatus.CANCELLED)
                    return

                if cfg.daily_limit and sent_today >= cfg.daily_limit:
                    self._finish(campaign_id, CampaignStatus.PAUSED,
                                 f"Daily safety limit ({cfg.daily_limit}) reached. Resume tomorrow "
                                 f"or raise the limit in Settings.")
                    return

                with session_scope() as db:
                    rec = db.scalars(select(Recipient).where(
                        Recipient.campaign_id == campaign_id,
                        Recipient.status == RecipientStatus.PENDING.value).order_by(Recipient.id).limit(1)).first()
                    if rec is None:
                        break
                    # Last check right before sending: the number may have been blocked
                    # after this campaign was created (e.g. someone replied STOP today).
                    if blocklist.is_blocked(db, rec.phone):
                        rec.status = RecipientStatus.SKIPPED.value
                        rec.error = "In blocklist - not sent"
                        continue
                    rec.status = RecipientStatus.SENDING.value
                    rid, phone, variables = rec.id, rec.phone, dict(rec.variables or {})

                text = render_message(template, variables) if template else None
                ok = await self._send_one(client, campaign_id, rid, phone, text, image_url, cfg.max_retries)
                if ok is None:          # interrupted while waiting (pause/cancel)
                    continue
                if ok:
                    sent_today += 1
                sent_in_batch += 1

                if cfg.batch_size and sent_in_batch >= cfg.batch_size:
                    sent_in_batch = 0
                    log.info("Campaign %s: batch pause %ss", campaign_id, cfg.batch_pause_sec)
                    await self._sleep_interruptible(campaign_id, cfg.batch_pause_sec)
                else:
                    lo, hi = sorted((cfg.min_delay_sec, cfg.max_delay_sec))
                    await self._sleep_interruptible(campaign_id, random.uniform(lo, hi))

            self._finish(campaign_id, CampaignStatus.COMPLETED)
        except WasenderError as exc:
            log.warning("Campaign %s stopped: %s", campaign_id, exc.message)
            self._finish(campaign_id, CampaignStatus.PAUSED, f"Stopped: {exc.message}")
        except Exception as exc:  # never let the task die silently
            log.exception("Campaign %s crashed", campaign_id)
            self._finish(campaign_id, CampaignStatus.PAUSED, f"Unexpected error: {exc}")
        finally:
            await client.close()
            self._stop_flags.pop(campaign_id, None)

    async def _send_one(self, client: WasenderClient, campaign_id: int, rid: int, phone: str,
                        text: str | None, image_url: str | None, max_retries: int) -> bool | None:
        attempts = 0
        while True:
            try:
                result = await client.send_message(phone, text=text, image_url=image_url)
                self._mark(rid, RecipientStatus.SENT, attempts + 1, message_id=result.message_id)
                return True
            except WasenderError as exc:
                if exc.is_auth_error:
                    self._mark(rid, RecipientStatus.PENDING, attempts)
                    raise
                if exc.is_rate_limited:
                    wait = (exc.retry_after or 10) + random.uniform(0.5, 2)
                    log.info("Rate limited, waiting %.1fs", wait)
                    if not await self._sleep_interruptible(campaign_id, wait):
                        self._mark(rid, RecipientStatus.PENDING, attempts)
                        return None
                    continue
                attempts += 1
                if exc.is_retryable and attempts <= max_retries:
                    if not await self._sleep_interruptible(campaign_id, min(60, 2 ** attempts * 2)):
                        self._mark(rid, RecipientStatus.PENDING, attempts)
                        return None
                    continue
                self._mark(rid, RecipientStatus.FAILED, attempts, error=exc.message)
                return False

    @staticmethod
    def _mark(rid: int, status: RecipientStatus, attempts: int, message_id: str | None = None,
              error: str | None = None) -> None:
        with session_scope() as db:
            r = db.get(Recipient, rid)
            r.status, r.attempts = status.value, attempts
            if message_id:
                r.message_id = message_id
            r.error = error
            if status == RecipientStatus.SENT:
                r.sent_at = utcnow()


runner = CampaignRunner()
