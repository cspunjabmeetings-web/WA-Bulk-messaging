"""WasenderAPI client.

Docs: https://wasenderapi.com/api-docs
  POST /api/send-message   {to, text, imageUrl?}          Bearer <session API key>
  POST /api/upload         {base64: "data:<mime>;base64,..."} -> {success, publicUrl}  (URL valid 24h)
  GET  /api/status         -> {status: "connected" | ...}
Rate limits: 429 body contains `retry_after` (seconds); headers X-RateLimit-*.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app.config import settings

log = logging.getLogger("wasender")


class WasenderError(Exception):
    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None,
                 payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retry_after = retry_after
        self.payload = payload or {}

    @property
    def is_rate_limited(self) -> bool:
        return self.status_code == 429

    @property
    def is_retryable(self) -> bool:
        return self.status_code is None or self.status_code >= 500 or self.status_code == 429

    @property
    def is_auth_error(self) -> bool:
        return self.status_code in (401, 403)


@dataclass
class SendResult:
    ok: bool
    message_id: str | None = None
    raw: dict = field(default_factory=dict)
    rate_remaining: int | None = None


class WasenderClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 30.0):
        if not api_key:
            raise WasenderError("WasenderAPI API key is not set. Add it in Settings.", status_code=401)
        self.api_key = api_key
        self.base_url = (base_url or settings.wasender_base_url).rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        )

    async def __aenter__(self) -> "WasenderClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _parse_error(resp: httpx.Response) -> WasenderError:
        try:
            body = resp.json()
        except ValueError:
            body = {"message": resp.text[:300]}
        message = body.get("message") or body.get("error") or f"HTTP {resp.status_code}"
        if isinstance(body.get("errors"), dict):
            details = "; ".join(f"{k}: {', '.join(map(str, v))}" for k, v in body["errors"].items())
            message = f"{message} ({details})"
        retry_after = body.get("retry_after") or resp.headers.get("Retry-After") \
            or resp.headers.get("X-RateLimit-Reset")
        try:
            retry_after = float(retry_after) if retry_after is not None else None
        except ValueError:
            retry_after = None
        # Some limit errors come with a non-429 status but a retry_after in the body.
        status = 429 if retry_after is not None and resp.status_code in (400, 429) else resp.status_code
        return WasenderError(message, status_code=status, retry_after=retry_after, payload=body)

    async def _request(self, method: str, path: str, **kw) -> tuple[dict, httpx.Response]:
        try:
            resp = await self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:  # network / timeout
            raise WasenderError(f"Network error: {exc.__class__.__name__}: {exc}") from exc
        if resp.status_code >= 400:
            raise self._parse_error(resp)
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("success") is False:
            raise WasenderError(data.get("message", "Request failed"), status_code=resp.status_code, payload=data)
        return data, resp

    # --------------------------------------------------------------- endpoints
    async def status(self) -> dict:
        data, _ = await self._request("GET", "/api/status")
        return data

    async def upload_media(self, file_path: Path) -> str:
        mime = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        b64 = base64.b64encode(file_path.read_bytes()).decode()
        data, _ = await self._request("POST", "/api/upload", json={"base64": f"data:{mime};base64,{b64}"})
        url = data.get("publicUrl") or (data.get("data") or {}).get("publicUrl")
        if not url:
            raise WasenderError(f"Upload succeeded but no publicUrl returned: {data}")
        return url

    async def send_message(self, to: str, text: str | None = None, image_url: str | None = None) -> SendResult:
        payload: dict = {"to": to}
        if text:
            payload["text"] = text
        if image_url:
            payload["imageUrl"] = image_url
        data, resp = await self._request("POST", "/api/send-message", json=payload)
        inner = data.get("data") if isinstance(data.get("data"), dict) else {}
        msg_id = inner.get("msgId") or inner.get("id") or data.get("msgId")
        remaining = resp.headers.get("X-RateLimit-Remaining")
        return SendResult(ok=True, message_id=str(msg_id) if msg_id else None, raw=data,
                          rate_remaining=int(remaining) if remaining and remaining.isdigit() else None)


class MockWasenderClient(WasenderClient):
    """Offline simulator (WASENDER_MOCK=1). ~8% failures, occasional 429, no real messages."""

    def __init__(self, api_key: str = "mock", base_url: str | None = None, timeout: float = 30.0):
        self.api_key = api_key or "mock"
        self.base_url = "mock://wasender"

    async def close(self) -> None:
        return None

    async def status(self) -> dict:
        return {"success": True, "status": "connected", "mock": True}

    async def upload_media(self, file_path: Path) -> str:
        await asyncio.sleep(0.2)
        return f"https://mock.wasenderapi.local/media/{uuid.uuid4()}{file_path.suffix}"

    async def send_message(self, to: str, text: str | None = None, image_url: str | None = None) -> SendResult:
        await asyncio.sleep(random.uniform(0.05, 0.25))
        roll = random.random()
        if roll < 0.03:
            raise WasenderError("Simulated rate limit", status_code=429, retry_after=1)
        if roll < 0.08:
            raise WasenderError("The number is not on WhatsApp (simulated)", status_code=422)
        return SendResult(ok=True, message_id=str(random.randint(10**6, 10**7)), raw={"mock": True})


def make_client(api_key: str) -> WasenderClient:
    if settings.wasender_mock:
        return MockWasenderClient(api_key)
    return WasenderClient(api_key)
