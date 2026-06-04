# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in VY_USERNAME, VY_PASSWORD, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

## Running

```bash
python notifier.py
```

Enable debug logging by changing `level=logging.INFO` to `level=logging.DEBUG` in `notifier.py`'s `basicConfig` call.

## Architecture

Single-file script (`notifier.py`) with these layers:

1. **Auth** (`login`) — GETs the login page, extracts CSRF tokens from hidden form inputs, POSTs credentials. Uses heuristic field-name detection and falls back to common names (`email`, `password`).

2. **Fetching** (`fetch_bookings`) — GETs the bookings page. Auto-detects HTML vs JSON response by `Content-Type` and routes to `parse_bookings` or `parse_bookings_json` accordingly.

3. **Parsing** (`parse_bookings`, `_extract_booking_fields`, `parse_bookings_json`) — CSS selectors are placeholder guesses and **must be adjusted** to match the actual virtualyard.com HTML. The selectors have `# ADJUST` comments throughout. Booking IDs fall back to a content hash if no explicit ID is found.

4. **Deduplication** — `seen_bookings.json` persists seen booking IDs across restarts. Only new IDs trigger a Telegram alert.

5. **Notification** (`send_telegram`, `format_booking_message`) — Posts to the Telegram Bot API with HTML parse mode.

6. **Scheduler** (`main`) — Uses the `schedule` library to poll every `POLL_INTERVAL_SECONDS` (default 60s). Runs one cycle immediately on startup, then re-logins on unexpected errors.

## Adapting to the real site

The script is intentionally written as a template. When virtualyard.com's actual HTML structure is known:
- Update `VY_LOGIN_URL` and `VY_BOOKINGS_URL` constants at the top of the file
- Update `BOOKING_ROW_SELECTOR` in `parse_bookings()` to match real booking container elements
- Update field selectors in `_extract_booking_fields()` for name, date, vehicle, phone
- If bookings load via XHR/JSON, set `VY_BOOKINGS_URL` to the API endpoint — `fetch_bookings` handles JSON automatically
