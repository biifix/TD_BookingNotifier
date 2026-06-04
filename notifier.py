"""
BYD Sales Consultant Booking Notifier
Polls virtualyard.com for new test drive bookings and sends Telegram alerts.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

VY_BASE_URL = "https://dealers.virtualyard.com.au"
VY_LOGIN_URL = f"{VY_BASE_URL}/login.php"

# Bookings page URL — adjust after logging in and navigating to the test-drive
# bookings section. Common paths: /bookings, /test-drives, /appointments, /dashboard
VY_BOOKINGS_URL = f"{VY_BASE_URL}/bookings"

SEEN_FILE = Path(__file__).parent / "seen_bookings.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def load_seen(path: Path) -> set:
    """Load previously seen booking IDs from a JSON file."""
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            log.debug("Loaded %d seen booking IDs from %s", len(data), path)
            return set(data)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read seen file %s: %s — starting fresh.", path, exc)
    return set()


def save_seen(path: Path, seen: set) -> None:
    """Persist the set of seen booking IDs to a JSON file."""
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(sorted(seen), fh, indent=2)
        log.debug("Saved %d seen booking IDs to %s", len(seen), path)
    except OSError as exc:
        log.error("Could not save seen file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------


def send_telegram(bot_token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("Telegram message sent successfully.")
        return True
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Authentication — Playwright (handles JS-populated hidden fields)
# ---------------------------------------------------------------------------


def login(session: requests.Session, username: str, password: str) -> bool:
    """
    Log into dealers.virtualyard.com.au using a headless browser.

    The login form populates hidden `auth` and `duid` fields via JavaScript
    (localStorage), so a plain HTTP request cannot complete the login.
    Playwright drives a real browser, fills the form, submits it, then copies
    the resulting cookies into the requests.Session for subsequent polling.
    """
    log.info("Launching headless browser to log in…")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()

            log.info("Navigating to login page: %s", VY_LOGIN_URL)
            page.goto(VY_LOGIN_URL, wait_until="domcontentloaded", timeout=60_000)

            # Give any JS-rendered modals time to appear
            page.wait_for_timeout(2_000)

            # Dismiss Terms of Use modal — try every visible button/link inside the modal
            modal = page.query_selector(".modal, [role='dialog'], .popup, .overlay")
            if modal and modal.is_visible():
                log.info("Modal detected — dumping its text for debug:")
                log.info(modal.inner_text()[:300])
                # Try buttons inside the modal, click the last one (usually Accept/Continue)
                btns = modal.query_selector_all("button, a.btn, input[type='button'], input[type='submit']")
                if btns:
                    log.info("Modal buttons found: %s", [b.inner_text() for b in btns])
                    btns[-1].click()  # last button is typically the affirmative action
                else:
                    # Try clicking the × close button
                    close = modal.query_selector(".close, [aria-label='Close'], .btn-close")
                    if close:
                        close.click()
                page.wait_for_timeout(1_000)
            else:
                log.info("No modal detected on login page.")

            # Wait for the username input to be ready
            page.wait_for_selector("input[name='login'], input[name='username']", timeout=20_000)

            # Fill credentials
            username_sel = "input[name='login']" if page.query_selector("input[name='login']") else "input[name='username']"
            page.fill(username_sel, username)
            page.fill("input[name='password']", password)

            # Check the Terms of Use checkbox on the login form if present
            tos_checkbox = page.query_selector("input[type='checkbox']")
            if tos_checkbox and not tos_checkbox.is_checked():
                tos_checkbox.check()

            # Click submit and wait for either a URL change or the password field to disappear
            page.click("button[type='submit'], input[type='submit']")
            try:
                # Wait for navigation (URL change) — use 'load' which is more reliable than 'networkidle'
                page.wait_for_load_state("load", timeout=30_000)
            except PlaywrightTimeoutError:
                pass  # Some SPAs don't navigate — check the result below instead

            final_url = page.url
            log.info("Post-login URL: %s", final_url)

            # If password field is still visible, login failed
            if page.query_selector("input[name='password']"):
                screenshot_path = Path(__file__).parent / "login_failure.png"
                page.screenshot(path=str(screenshot_path))
                log.error("Login failed — screenshot saved to %s", screenshot_path)
                error_el = page.query_selector(".alert, .error, .invalid-feedback, .modal-body")
                error_msg = error_el.inner_text() if error_el else "no error message found on page"
                log.error("Site says: %s", error_msg)
                browser.close()
                return False

            # Transfer browser cookies into the requests.Session
            for cookie in context.cookies():
                session.cookies.set(
                    cookie["name"],
                    cookie["value"],
                    domain=cookie.get("domain", "").lstrip("."),
                )

            browser.close()

    except PlaywrightTimeoutError as exc:
        log.error("Browser timed out during login: %s", exc)
        return False
    except Exception as exc:
        log.error("Unexpected error during browser login: %s", exc)
        return False

    log.info("Login successful — cookies transferred to HTTP session.")
    return True


# ---------------------------------------------------------------------------
# Booking fetching & parsing
# ---------------------------------------------------------------------------


def fetch_bookings(session: requests.Session) -> list[dict]:
    """
    Fetch the bookings page and return a parsed list of booking dicts.

    ADJUST: Change VY_BOOKINGS_URL if the platform uses a different path.
    Also check if the data is loaded via a JSON API endpoint (XHR/fetch) —
    if so, call that API URL directly and parse JSON instead of HTML.
    """
    try:
        log.info("Fetching bookings page: %s", VY_BOOKINGS_URL)
        resp = session.get(VY_BOOKINGS_URL, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Could not fetch bookings page: %s", exc)
        return []

    content_type = resp.headers.get("Content-Type", "")
    if "application/json" in content_type:
        try:
            data = resp.json()
            log.info("Received JSON response — parsing as API data.")
            return parse_bookings_json(data)
        except ValueError as exc:
            log.error("Failed to parse JSON response: %s", exc)
            return []

    return parse_bookings(resp.text)


def parse_bookings(html: str) -> list[dict]:
    """
    Parse the bookings HTML page and return a list of booking dicts.

    SELECTOR ADJUSTMENT GUIDE
    -------------------------
    1. Open the bookings page in Chrome/Firefox DevTools (F12 → Elements).
    2. Find the container that holds all booking rows.
    3. Update BOOKING_ROW_SELECTOR to match those rows.
    4. Update field selectors in _extract_booking_fields() for name, date, vehicle, phone.
    """
    soup = BeautifulSoup(html, "html.parser")
    bookings = []

    # --- ADJUST: selector for individual booking rows/cards ---
    BOOKING_ROW_SELECTOR = (
        "tr.booking-row, "
        "div.booking-card, "
        "li.appointment-item, "
        "[data-booking-id]"
    )

    rows = soup.select(BOOKING_ROW_SELECTOR)

    if not rows:
        log.warning(
            "No booking rows found with selector %r. "
            "Inspect the bookings page HTML and update BOOKING_ROW_SELECTOR.",
            BOOKING_ROW_SELECTOR,
        )
        log.debug("Bookings page HTML snippet:\n%s", html[:3000])
        return []

    log.info("Found %d booking row(s) on page.", len(rows))

    for row in rows:
        try:
            booking = _extract_booking_fields(row)
            if booking:
                bookings.append(booking)
        except Exception as exc:
            log.warning("Could not parse a booking row: %s", exc)

    return bookings


def _extract_booking_fields(row) -> dict | None:
    """
    Extract fields from a single booking row element.
    ADJUST each selector to match the actual HTML structure.
    """
    booking_id = (
        row.get("data-booking-id")
        or row.get("data-id")
        or row.get("id")
    )
    if not booking_id:
        id_cell = row.select_one(".booking-id, td.id, [data-field='id']")
        booking_id = id_cell.get_text(strip=True) if id_cell else None

    name_cell = row.select_one(
        ".customer-name, .name, td.name, [data-field='customer'], "
        "[data-field='name'], .client-name"
    )
    name = name_cell.get_text(strip=True) if name_cell else "Unknown"

    dt_cell = row.select_one(
        ".booking-date, .date, .datetime, td.date, td.datetime, "
        "[data-field='date'], time"
    )
    booking_datetime = dt_cell.get_text(strip=True) if dt_cell else "Unknown"

    vehicle_cell = row.select_one(
        ".vehicle, .car, .model, td.vehicle, td.model, "
        "[data-field='vehicle'], [data-field='model']"
    )
    vehicle = vehicle_cell.get_text(strip=True) if vehicle_cell else "Unknown"

    phone_cell = row.select_one(
        ".phone, .mobile, .contact, td.phone, td.mobile, "
        "[data-field='phone'], [data-field='mobile']"
    )
    phone = phone_cell.get_text(strip=True) if phone_cell else None

    if not booking_id:
        booking_id = f"{name}-{booking_datetime}-{vehicle}".replace(" ", "_").lower()
        if booking_id == "unknown-unknown-unknown":
            log.debug("Skipping row with no identifiable content.")
            return None

    return {
        "id": str(booking_id),
        "name": name,
        "datetime": booking_datetime,
        "vehicle": vehicle,
        "phone": phone,
    }


def parse_bookings_json(data) -> list[dict]:
    """
    Parse bookings from a JSON API response.
    ADJUST: Inspect the actual JSON structure and map fields accordingly.
    """
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("bookings")
            or data.get("appointments")
            or data.get("data")
            or data.get("results")
            or []
        )
        if isinstance(items, dict):
            items = list(items.values())
    else:
        log.warning("Unexpected JSON structure: %s", type(data))
        return []

    bookings = []
    for item in items:
        if not isinstance(item, dict):
            continue
        booking_id = (
            item.get("id")
            or item.get("booking_id")
            or item.get("appointmentId")
        )
        name = (
            item.get("customer_name")
            or item.get("name")
            or item.get("customer")
            or "Unknown"
        )
        booking_datetime = (
            item.get("date")
            or item.get("datetime")
            or item.get("appointment_date")
            or item.get("scheduled_at")
            or "Unknown"
        )
        vehicle = (
            item.get("vehicle")
            or item.get("model")
            or item.get("car")
            or "Unknown"
        )
        phone = item.get("phone") or item.get("mobile") or item.get("contact_number")

        if not booking_id:
            booking_id = f"{name}-{booking_datetime}-{vehicle}".lower().replace(" ", "_")

        bookings.append({
            "id": str(booking_id),
            "name": name,
            "datetime": booking_datetime,
            "vehicle": vehicle,
            "phone": phone,
        })

    return bookings


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


def format_booking_message(booking: dict) -> str:
    """Format a booking dict into a Telegram message."""
    phone_line = f"\n📞 Phone: {booking['phone']}" if booking.get("phone") else ""
    return (
        f"🚗 <b>New Test Drive Booking!</b>\n"
        f"👤 Customer: {booking['name']}\n"
        f"📅 Date/Time: {booking['datetime']}\n"
        f"🚙 Vehicle: {booking['vehicle']}"
        f"{phone_line}\n"
        f"🆔 Booking ID: {booking['id']}"
    )


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------


def check_new_bookings(
    session: requests.Session,
    bot_token: str,
    chat_id: str,
    seen: set,
    username: str,
    password: str,
) -> None:
    """
    Single polling cycle: fetch bookings, find new ones, send alerts.
    Re-authenticates via Playwright if the session appears to have expired.
    """
    bookings = fetch_bookings(session)

    # If we got no bookings, check whether we've been logged out
    if not bookings:
        log.info("No bookings found this cycle (or fetch failed).")
        return

    new_count = 0
    for booking in bookings:
        bid = booking["id"]
        if bid in seen:
            log.debug("Already seen booking %s — skipping.", bid)
            continue

        log.info("New booking detected: %s", bid)
        message = format_booking_message(booking)
        success = send_telegram(bot_token, chat_id, message)
        if success:
            seen.add(bid)
            new_count += 1
        else:
            log.warning("Telegram send failed for booking %s — will retry next cycle.", bid)

    if new_count:
        save_seen(SEEN_FILE, seen)
        log.info("Notified %d new booking(s).", new_count)
    else:
        log.info("No new bookings this cycle.")


def main() -> None:
    """Entry point: initialise session, log in, start polling scheduler."""
    username = os.getenv("VY_USERNAME")
    password = os.getenv("VY_PASSWORD")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    missing = [
        name
        for name, val in [
            ("VY_USERNAME", username),
            ("VY_PASSWORD", password),
            ("TELEGRAM_BOT_TOKEN", bot_token),
            ("TELEGRAM_CHAT_ID", chat_id),
        ]
        if not val
    ]
    if missing:
        log.error(
            "Missing required environment variables: %s. "
            "Copy .env.example to .env and fill in your credentials.",
            ", ".join(missing),
        )
        sys.exit(1)

    log.info("Starting BYD Booking Notifier (poll every %ds).", poll_interval)

    seen: set = load_seen(SEEN_FILE)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
    )

    if not login(session, username, password):
        log.error("Initial login failed. Check credentials. Exiting.")
        sys.exit(1)

    check_new_bookings(session, bot_token, chat_id, seen, username, password)

    def job():
        try:
            check_new_bookings(session, bot_token, chat_id, seen, username, password)
        except Exception as exc:
            log.exception("Unexpected error in polling job: %s", exc)
            log.info("Attempting re-login after error…")
            try:
                login(session, username, password)
            except Exception as login_exc:
                log.error("Re-login also failed: %s", login_exc)

    schedule.every(poll_interval).seconds.do(job)

    log.info("Scheduler running. Press Ctrl+C to stop.")
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except KeyboardInterrupt:
            log.info("Shutting down — goodbye.")
            break


if __name__ == "__main__":
    main()
