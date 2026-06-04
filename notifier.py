"""
BYD Sales Consultant Booking Notifier
Polls virtualyard.com for new test drive bookings and sends Telegram alerts.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import schedule
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

VY_BASE_URL = "https://virtualyard.com"

# Login endpoint — inspect the login form's `action` attribute and adjust if needed.
# Common paths: /login, /signin, /auth/login, /account/login
VY_LOGIN_URL = f"{VY_BASE_URL}/login"

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
    """
    Send a message via Telegram Bot API.

    Returns True on success, False on failure.
    """
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
# Authentication
# ---------------------------------------------------------------------------


def login(session: requests.Session, username: str, password: str) -> bool:
    """
    Log into virtualyard.com.

    Strategy:
    1. GET the login page to retrieve any CSRF token.
    2. POST credentials to the login endpoint.
    3. Confirm success by checking for a redirect or absence of login form.

    ADJUST: If login fails, inspect the login form HTML to find:
      - The correct POST URL (form `action` attribute)
      - The correct field names for username/password (input `name` attributes)
      - Any hidden CSRF fields that must be included
    """
    try:
        # Step 1: GET login page to capture CSRF token / cookies
        log.info("Fetching login page: %s", VY_LOGIN_URL)
        get_resp = session.get(VY_LOGIN_URL, timeout=20)
        get_resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Could not reach login page: %s", exc)
        return False

    soup = BeautifulSoup(get_resp.text, "html.parser")

    # --- ADJUST: locate the login form ---
    # Try common form selectors. If none match, print page source and inspect manually.
    form = (
        soup.find("form", {"id": "login-form"})
        or soup.find("form", {"class": "login"})
        or soup.find("form", action=lambda a: a and "login" in a.lower())
        or soup.find("form")  # fallback: first form on the page
    )

    if form is None:
        log.error(
            "Could not find a login form on %s. "
            "Set VY_LOGIN_URL to the correct login endpoint and adjust selectors.",
            VY_LOGIN_URL,
        )
        log.debug("Page HTML snippet:\n%s", get_resp.text[:2000])
        return False

    # Build POST data from all hidden inputs (catches CSRF tokens, etc.)
    post_data: dict = {}
    for hidden in form.find_all("input", {"type": "hidden"}):
        name = hidden.get("name")
        value = hidden.get("value", "")
        if name:
            post_data[name] = value

    # --- ADJUST: username/password field names ---
    # Common names: email, username, user, login / password, pass, pwd
    username_field = _find_input_name(form, ["email", "username", "user", "login"])
    password_field = _find_input_name(form, ["password", "pass", "pwd"])

    if not username_field:
        log.warning(
            "Could not auto-detect username field name. "
            "Set USERNAME_FIELD in the script to the correct input name."
        )
        username_field = "email"  # ADJUST fallback

    if not password_field:
        log.warning(
            "Could not auto-detect password field name. "
            "Set PASSWORD_FIELD in the script to the correct input name."
        )
        password_field = "password"  # ADJUST fallback

    post_data[username_field] = username
    post_data[password_field] = password

    # Determine POST URL: use form's action or fall back to VY_LOGIN_URL
    action = form.get("action", "")
    if action.startswith("http"):
        post_url = action
    elif action:
        post_url = VY_BASE_URL.rstrip("/") + "/" + action.lstrip("/")
    else:
        post_url = VY_LOGIN_URL

    log.info("Posting credentials to: %s", post_url)
    try:
        post_resp = session.post(post_url, data=post_data, timeout=20, allow_redirects=True)
        post_resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("Login POST failed: %s", exc)
        return False

    # Heuristic success check: login form should no longer be present
    post_soup = BeautifulSoup(post_resp.text, "html.parser")
    if post_soup.find("form", {"id": "login-form"}) or post_soup.find(
        "input", {"type": "password"}
    ):
        log.error(
            "Login appears to have failed — password field still present after POST. "
            "Check credentials and adjust login selectors."
        )
        log.debug("Post-login HTML snippet:\n%s", post_resp.text[:2000])
        return False

    log.info("Login successful.")
    return True


def _find_input_name(form, candidates: list) -> str | None:
    """Return the first input name from `candidates` that exists in the form."""
    for name in candidates:
        inp = form.find("input", {"name": name})
        if inp:
            return name
    return None


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

    # Check if the response is JSON (API endpoint)
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

    Each dict should contain at minimum:
      - id:       unique identifier (used to detect duplicates)
      - name:     customer name
      - datetime: appointment date/time string
      - vehicle:  vehicle model/name
      - phone:    customer phone (if available)

    SELECTOR ADJUSTMENT GUIDE
    -------------------------
    1. Open the bookings page in Chrome/Firefox DevTools (F12 → Elements).
    2. Find the container that holds all booking rows (e.g. a <table>, <ul>, <div>).
    3. Update BOOKING_ROW_SELECTOR to match those rows.
    4. For each field, update the corresponding selector/attribute below.

    Common patterns to look for:
      - Table rows:  soup.select("table.bookings tbody tr")
      - Card divs:   soup.select("div.booking-card")
      - List items:  soup.select("ul.appointments li")
    """
    soup = BeautifulSoup(html, "html.parser")
    bookings = []

    # --- ADJUST: selector for individual booking rows/cards ---
    BOOKING_ROW_SELECTOR = (
        "tr.booking-row, "          # table row variant
        "div.booking-card, "        # card variant
        "li.appointment-item, "     # list variant
        "[data-booking-id]"         # data-attribute variant
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

    ADJUST each selector/attribute to match the actual HTML structure.
    """

    # --- ADJUST: unique booking ID ---
    # Try data attributes first, then fall back to a visible ID field.
    booking_id = (
        row.get("data-booking-id")
        or row.get("data-id")
        or row.get("id")
    )
    if not booking_id:
        # Try to find an ID cell
        id_cell = row.select_one(".booking-id, td.id, [data-field='id']")
        booking_id = id_cell.get_text(strip=True) if id_cell else None

    # --- ADJUST: customer name ---
    name_cell = row.select_one(
        ".customer-name, .name, td.name, [data-field='customer'], "
        "[data-field='name'], .client-name"
    )
    name = name_cell.get_text(strip=True) if name_cell else "Unknown"

    # --- ADJUST: appointment date/time ---
    dt_cell = row.select_one(
        ".booking-date, .date, .datetime, td.date, td.datetime, "
        "[data-field='date'], time"
    )
    booking_datetime = dt_cell.get_text(strip=True) if dt_cell else "Unknown"

    # --- ADJUST: vehicle model ---
    vehicle_cell = row.select_one(
        ".vehicle, .car, .model, td.vehicle, td.model, "
        "[data-field='vehicle'], [data-field='model']"
    )
    vehicle = vehicle_cell.get_text(strip=True) if vehicle_cell else "Unknown"

    # --- ADJUST: customer phone (optional) ---
    phone_cell = row.select_one(
        ".phone, .mobile, .contact, td.phone, td.mobile, "
        "[data-field='phone'], [data-field='mobile']"
    )
    phone = phone_cell.get_text(strip=True) if phone_cell else None

    # Generate a fallback ID from content if no explicit ID found
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
    `data` may be a list of booking objects or a dict with a nested list.
    """
    # Common patterns:
    #   data = [{"id": 1, "customer": "...", ...}, ...]
    #   data = {"bookings": [...]}
    #   data = {"data": {"appointments": [...]}}

    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        # Try common wrapper keys
        items = (
            data.get("bookings")
            or data.get("appointments")
            or data.get("data")
            or data.get("results")
            or []
        )
        if isinstance(items, dict):
            # Nested further — ADJUST as needed
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
    Re-authenticates if the session appears to have expired.
    """
    bookings = fetch_bookings(session)

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

    # Validate required env vars
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

    # Load previously seen bookings
    seen: set = load_seen(SEEN_FILE)

    # Create a persistent HTTP session (retains cookies between requests)
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

    # Initial login
    if not login(session, username, password):
        log.error("Initial login failed. Check credentials and VY_LOGIN_URL. Exiting.")
        sys.exit(1)

    # Run once immediately before handing off to the scheduler
    check_new_bookings(session, bot_token, chat_id, seen, username, password)

    def job():
        try:
            check_new_bookings(session, bot_token, chat_id, seen, username, password)
        except Exception as exc:
            log.exception("Unexpected error in polling job: %s", exc)
            # Attempt to re-login on unexpected errors (session may have expired)
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
