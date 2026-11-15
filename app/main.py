"""FastAPI entry point.  Run:  python -m app.main   (or uvicorn app.main:app)"""
from __future__ import annotations

import hashlib
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import BASE_DIR, settings
from app.db import init_db
from app.services.campaign_runner import runner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
STATIC_DIR = BASE_DIR / "static"
COOKIE = "wa_portal_auth"


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    runner.recover_after_restart()
    yield
    await runner.shutdown()


app = FastAPI(title=settings.app_name, version=settings.version, lifespan=lifespan)


# ------------------------------------------------------------- simple login
def _token() -> str:
    return hmac.new(settings.secret_key.encode(), settings.portal_password.encode(), hashlib.sha256).hexdigest()


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    if not settings.portal_password:
        return await call_next(request)
    path = request.url.path
    if path in ("/login", "/api/health") or path.startswith("/static/vendor/"):
        return await call_next(request)
    if hmac.compare_digest(request.cookies.get(COOKIE, ""), _token()):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Not logged in"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login", include_in_schema=False)
def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login", include_in_schema=False)
def do_login(password: str = Form(...)):
    if not hmac.compare_digest(password, settings.portal_password):
        return RedirectResponse("/login?error=1", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(COOKIE, _token(), httponly=True, samesite="lax", max_age=60 * 60 * 12)
    return resp


@app.get("/logout", include_in_schema=False)
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------------ routes
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=False)
