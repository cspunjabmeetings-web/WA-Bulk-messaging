# WA Bulk Portal — v1.1

A small, production-style web portal for sending WhatsApp messages (text + optional image) to a list of numbers from a CSV/Excel file, using **[WasenderAPI](https://wasenderapi.com/api-docs)**. It runs on your own PC like a desktop app (opens in the browser) or on a server.

## Features (v1)
- **Upload CSV / Excel** (.csv, .xlsx, .xls), drag and drop
- **Finds the phone column automatically** (you can change it), cleans numbers to international format
  (`03001234567`, `0300-1234567`, `923001234567`, `0092…` → `+923001234567`), removes duplicates, lists invalid rows
- **Personalised messages** with `{name}`, `{City}` or any column from the file, and WhatsApp formatting (`*bold*`, `_italic_`)
- **Image attachment** (JPG/PNG, max 5 MB). It is uploaded once to WasenderAPI and re-uploaded automatically after 20 h, because their media links expire after 24 h
- **Live WhatsApp-style preview** and a **test message** to your own number
- **Safe background sending**: one message at a time, a random delay between messages, a longer break after every batch, a daily limit, automatic handling of WasenderAPI `429 retry_after`, and retries with backoff on network errors
- **Live report**: Sent / Failed / Pending counts, a progress bar, a per-number status table with search and filter, and the error reason for each failed number
- **Pause / Resume / Cancel / Retry failed**, and **Export report as CSV** (opens in Excel)
- **Survives a restart**: all progress is kept in SQLite. A campaign that was running comes back as *paused* and continues from where it stopped
- **Blocklist (do-not-contact list)**: add numbers that replied STOP or sent an abusive reply (typed in, imported from a file, or with the ⛔ Block button in the report). Blocked numbers are removed when a file is uploaded, and checked once more right before each message is sent
- Optional **password login** (needed when it runs on a server)
- **Test mode** (`WASENDER_MOCK=1`): try everything without sending real messages

## Quick start (Windows)
1. Install **Python 3.11+** from python.org. Tick "Add Python to PATH" during install.
2. Unzip this folder and double-click **`run.bat`**. The first run installs dependencies, then opens `http://127.0.0.1:8000`.
3. In WasenderAPI: create a session → scan the QR code from WhatsApp (*Linked devices*) → copy the session's **API Key**.
4. In the portal: **Settings → paste the API key → Save → Test connection** (it should show "WhatsApp connected").
5. **New campaign** → upload the file → write the message / attach an image → *Send test* → **Start sending**.

Linux/Mac: `./run.sh`. Manual start: `pip install -r requirements.txt && python -m app.main`

> For a first try with no real sending: in `.env` set `WASENDER_MOCK=1`.

## Tech stack & structure
| Layer | Choice | Why |
|---|---|---|
| Backend | **FastAPI** (Python, async) | Fast, typed, auto API docs at `/docs` |
| Sending | asyncio background task + `httpx` | Non-blocking, one worker per campaign |
| Database | **SQLite** + SQLAlchemy 2 | No setup needed; can switch to Postgres later by changing the URL |
| Files | pandas + openpyxl, `phonenumbers` (Google's libphonenumber) | Reliable Excel/CSV parsing and number validation |
| Frontend | HTML + **Alpine.js** + **Tailwind** (bundled locally) | No build step, works offline |

```
app/
  main.py                  FastAPI app, login middleware, static files
  config.py                .env settings
  db.py, models.py         SQLite + tables (campaigns, recipients, settings)
  api/routes.py            REST API used by the UI
  services/
    wasender.py            WasenderAPI client (+ mock client)
    contacts.py            CSV/Excel reading, number cleaning, {placeholders}
    campaign_runner.py     background sender: delays, 429, retries, pause/resume
    settings_store.py      API key and speed settings (saved in the DB)
    blocklist.py           do-not-contact list helpers
static/                    index.html, app.js, login.html, vendor/ (alpine, css)
frontend/                  Tailwind source (rebuild only if you change the UI classes)
```

## WasenderAPI endpoints used
- `POST /api/send-message` — `{ "to": "+92…", "text": "…", "imageUrl": "…" }`
- `POST /api/upload` — base64 image → `publicUrl` (valid for 24 h)
- `GET /api/status` — session connection status

All three use the header `Authorization: Bearer <session API key>`.

**Rate limits** (from WasenderAPI docs): Trial = **1 msg/min, 50/day**. Paid = 256/min. With *Account Protection* on = 1 msg / 5 s. If you are on the trial plan, set the minimum delay in Settings to **60 s or more**.

## Honest note on "no restrictions"
No tool can promise that WhatsApp will never restrict a number. WasenderAPI connects through WhatsApp Web (it is not Meta's official Business API), and WhatsApp bans numbers mostly because people **report or block** them. This portal lowers the risk (slow random sending, batches, daily limit, personalised text, one worker), but these habits matter most:
- Send only to people who know you or have agreed to receive messages, and add a "Reply STOP" line
- With a new number, start at 50–100 messages a day and increase slowly
- Don't send the same text to thousands of strangers

## Security
- Keep the `.env` file and the `data/` folder private. The API key is stored in `data/portal.db`.
- If you put the portal on a server: set `PORTAL_PASSWORD` and `SECRET_KEY`, and use HTTPS (Nginx/Caddy).

## Changelog
- **1.1** Blocklist tab, 'Skipped' count in reports, phone search accepts 03xx format, uvicorn without extras (Python 3.15 install fix)
- **1.0** First release

## Ideas for v2
Auto-add to the blocklist when someone replies STOP (WasenderAPI webhook) · one combined daily limit for the whole number · Multiple users/roles · scheduled campaigns · several WhatsApp numbers · contact lists/groups with an opt-out list · delivery/read receipts through the WasenderAPI webhook · video/PDF attachments · message templates · dashboard charts · Docker + Postgres · a real desktop installer (.exe) using PyInstaller or Tauri
