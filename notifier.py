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
VY_DEALER_PATH = "/HARMONY-NEW-ENERGY-AUTO-SERVICE-(WSP)-PTY-LTD"
VY_LOGIN_URL = f"{VY_BASE_URL}/login.php"

# ADJUST: navigate to the test-drive bookings page in your browser after login and paste the URL here
VY_BOOKINGS_URL = os.getenv("VY_BOOKINGS_URL", f"{VY_BASE_URL}{VY_DEALER_PATH}/bookings")

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

            # Wait for the login form to be ready
            page.wait_for_selector("input[name='login'], input[name='username']", timeout=20_000)

            # If the ToU alert is visible, click the "Terms of Use" link to visit the page,
            # then come back — the site requires you to read the ToU before it allows login.
            tos_alert = page.query_selector(".alert, .alert-info, .alert-warning, .alert-success")
            if tos_alert and tos_alert.is_visible() and "Terms of Use" in (tos_alert.inner_text() or ""):
                log.info("ToU alert visible — clicking 'Terms of Use' link to visit the page.")
                tos_link = page.query_selector("a:has-text('Terms of Use')")
                if tos_link:
                    # The link may open in a new tab — handle both cases
                    with context.expect_page(timeout=8_000) as new_page_info:
                        tos_link.click()
                    try:
                        tos_page = new_page_info.value
                        tos_page.wait_for_load_state("domcontentloaded", timeout=15_000)
                        log.info("ToU page opened: %s — closing and returning to login.", tos_page.url)
                        tos_page.close()
                    except Exception:
                        # Link navigated in the same tab — go back
                        log.info("ToU opened in same tab (%s) — going back.", page.url)
                        page.go_back(wait_until="domcontentloaded", timeout=15_000)
                        page.wait_for_selector("input[name='login'], input[name='username']", timeout=10_000)
                    page.wait_for_timeout(1_000)

            # Log all input fields on the page to help debug field name mismatches
            all_inputs = page.evaluate("""() =>
                Array.from(document.querySelectorAll('input')).map(i => ({
                    name: i.name, type: i.type, id: i.id, placeholder: i.placeholder
                }))
            """)
            log.info("Form inputs found: %s", all_inputs)

            # Detect the username field name from what actually exists in the DOM
            username_sel = None
            for candidate in ["login", "username", "user", "email", "name"]:
                if page.query_selector(f"input[name='{candidate}']"):
                    username_sel = f"input[name='{candidate}']"
                    break
            if not username_sel:
                log.error("Could not find username input field — inputs: %s", all_inputs)
                browser.close()
                return False
            log.info("Using username selector: %s", username_sel)

            page.fill(username_sel, username)
            page.fill("input[name='password']", password)

            # Verify fields were filled
            filled_user = page.input_value(username_sel)
            log.info("Username field value after fill: %r (expected %r)", filled_user, username)

            # Check the "I have read Terms of Use" checkbox
            tos_checkbox = page.query_selector("input[type='checkbox']")
            if tos_checkbox and not tos_checkbox.is_checked():
                log.info("Checking the 'I have read Terms of Use' checkbox.")
                tos_checkbox.check()
                page.wait_for_timeout(500)

            # Check hidden auth/duid fields populated by JS — if empty, wait for JS to run
            auth_val = page.input_value("#auth") or ""
            duid_val = page.input_value("#duid") or ""
            log.info("Hidden fields — auth: %r, duid: %r", auth_val[:20] if auth_val else "", duid_val[:20] if duid_val else "")
            if not auth_val or not duid_val:
                log.info("auth/duid not yet set — waiting for JS to populate them (up to 10s)…")
                for _ in range(20):
                    page.wait_for_timeout(500)
                    auth_val = page.input_value("#auth") or ""
                    duid_val = page.input_value("#duid") or ""
                    if auth_val and duid_val:
                        log.info("auth/duid now populated: auth=%r duid=%r", auth_val[:20], duid_val[:20])
                        break
                else:
                    log.warning("auth/duid still empty after 10s — JS may need a page interaction to trigger.")
                    # Try scrolling or moving mouse to trigger lazy JS
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1_000)
                    auth_val = page.input_value("#auth") or ""
                    duid_val = page.input_value("#duid") or ""
                    log.info("After scroll — auth: %r, duid: %r", auth_val[:20], duid_val[:20])

            # Log localStorage to see if any auth token exists
            ls = page.evaluate("() => Object.entries(localStorage)")
            log.info("localStorage contents: %s", ls)

            # Monkey-patch window.fetch and XMLHttpRequest BEFORE clicking submit
            # so we can see exactly what URL and payload the JS sends
            page.evaluate("""() => {
                window._fetchLog = [];
                const origFetch = window.fetch;
                window.fetch = function(url, opts) {
                    window._fetchLog.push({url: String(url), method: (opts||{}).method||'GET', body: String((opts||{}).body||'')});
                    return origFetch.apply(this, arguments);
                };
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                    this._logUrl = url; this._logMethod = method;
                    return origOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function(body) {
                    window._fetchLog.push({url: this._logUrl, method: this._logMethod, body: String(body||'')});
                    return origSend.apply(this, arguments);
                };
            }""")

            # Take a screenshot just before submitting so we can verify form state
            page.screenshot(path=str(Path(__file__).parent / "pre_submit.png"))
            log.info("Pre-submit screenshot saved.")

            # Click submit and capture the AJAX response from the known login endpoint
            with page.expect_response(
                lambda r: "ajax/auth/login.php" in r.url, timeout=15_000
            ) as resp_info:
                page.click("button[type='submit'], input[type='submit']")
            try:
                ajax_resp = resp_info.value
                ajax_body = ajax_resp.text()
                log.info("Login AJAX response [%s]: %s", ajax_resp.status, ajax_body[:200])
            except Exception as exc:
                log.warning("Could not read AJAX response: %s", exc)

            # AJAX login succeeded — wait for the JS to store the token and redirect
            log.info("Waiting for post-login redirect…")
            try:
                page.wait_for_url(
                    lambda url: "login.php" not in url,
                    timeout=8_000,
                )
                log.info("Redirected to: %s", page.url)
            except PlaywrightTimeoutError:
                page.wait_for_timeout(2_000)

            # Handle 2-step verification screen
            verify_input = page.query_selector("input[name='verifyCode']")
            if verify_input and verify_input.is_visible():
                print("\n" + "="*60)
                print("2-STEP VERIFICATION REQUIRED")
                print("A verification code has been sent to your email/mobile.")
                print("="*60)
                code = input("Enter the verification code: ").strip()
                verify_input.fill(code)
                with page.expect_response(
                    lambda r: "login.php" in r.url or "verify" in r.url.lower(),
                    timeout=15_000,
                ) as verify_resp_info:
                    # Click the visible Verify Code button specifically
                    page.locator("button[type='submit']:visible, input[type='submit']:visible").first.click()
                try:
                    vr = verify_resp_info.value
                    log.info("Verify response [%s]: %s", vr.status, vr.text()[:200])
                except Exception:
                    pass
                # Wait for redirect after verification
                try:
                    page.wait_for_url(lambda url: "login.php" not in url, timeout=15_000)
                    log.info("Verified — redirected to: %s", page.url)
                except PlaywrightTimeoutError:
                    page.wait_for_timeout(3_000)

            final_url = page.url
            log.info("Post-login URL: %s", final_url)

            # Success if we navigated away from login, OR if there's no longer a password field
            still_on_login = "login.php" in final_url and page.query_selector("input[name='password']")
            if still_on_login:
                screenshot_path = Path(__file__).parent / "login_failure.png"
                page.screenshot(path=str(screenshot_path))
                log.error("Login failed — screenshot saved to %s", screenshot_path)
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

# Cached API endpoint discovered during first navigation (avoids re-navigating each poll)
_bookings_api_url: str | None = None


def fetch_bookings(session: requests.Session) -> list[dict]:
    """
    Fetch test drive bookings.

    First call: uses Playwright to navigate the SPA (PROSPECTS → TEST DRIVES),
    intercepts the AJAX call, caches the API URL, and parses the response.
    Subsequent calls: hits the cached API URL directly via requests for speed.
    """
    global _bookings_api_url

    if _bookings_api_url:
        return _fetch_bookings_via_api(session)
    else:
        return _fetch_bookings_via_browser(session)


def _fetch_bookings_via_api(session: requests.Session) -> list[dict]:
    """Call the cached bookings API endpoint directly."""
    global _bookings_api_url
    try:
        log.info("Polling bookings API: %s", _bookings_api_url)
        resp = session.get(_bookings_api_url, timeout=20)
        if resp.status_code in (401, 403):
            log.warning("Session expired (HTTP %s) — will re-navigate next cycle.", resp.status_code)
            _bookings_api_url = None
            return []
        resp.raise_for_status()
        return parse_bookings_json(resp.json())
    except requests.RequestException as exc:
        log.error("Bookings API request failed: %s", exc)
        return []


def _fetch_bookings_via_browser(session: requests.Session) -> list[dict]:
    """Call the Test Drives DataTables API directly — menuId=899 is the Test Drives section."""
    global _bookings_api_url

    # Build the minimal DataTables request for Test Drives (menuId=899)
    api_url = f"{VY_BASE_URL}/ajax/datatable/search.php"
    params = {
        "menuId": "899", "tab": "0", "stage": "0",
        "draw": "1", "start": "0", "length": "100",
        "order[0][column]": "0", "order[0][dir]": "desc",
        "search[value]": "", "search[regex]": "false",
        "columns[0][data]": "0", "columns[0][searchable]": "true",
        "columns[0][orderable]": "true",
        "columns[0][search][value]": "", "columns[0][search][regex]": "false",
    }
    for i in range(1, 8):
        params.update({
            f"columns[{i}][data]": str(i),
            f"columns[{i}][searchable]": "true",
            f"columns[{i}][orderable]": "false",
            f"columns[{i}][search][value]": "",
            f"columns[{i}][search][regex]": "false",
        })

    try:
        log.info("Calling Test Drives API directly (menuId=899)…")
        resp = session.get(api_url, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        log.info("Test Drives API response keys: %s", list(data.keys()) if isinstance(data, dict) else type(data))
        _bookings_api_url = resp.url
        bookings = parse_bookings_json(data)
        if bookings:
            return bookings
        # If empty, log the raw response for debugging
        log.info("Raw API response (first 500 chars): %s", str(data)[:500])
    except Exception as exc:
        log.error("Direct API call failed: %s", exc)

    # Fallback: use Playwright to navigate and capture the real API call
    log.info("Falling back to browser navigation to find API…")
    bookings = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ))
            # Inject cookies from the authenticated requests.Session
            for name, value in session.cookies.items():
                context.add_cookies([{
                    "name": name, "value": value,
                    "domain": "dealers.virtualyard.com.au", "path": "/"
                }])

            page = context.new_page()

            # Intercept the test drives API call
            api_responses = []
            def capture(response):
                url = response.url
                if "google" in url.lower() or "maps" in url.lower() or "analytics" in url.lower():
                    return
                if ("test" in url.lower() or "drive" in url.lower() or
                        "prospect" in url.lower() or "appointment" in url.lower() or
                        "booking" in url.lower() or "ajax" in url.lower()):
                    try:
                        data = response.json()
                        api_responses.append((url, data))
                        log.info("Captured API response from: %s", url)
                    except Exception:
                        pass
            page.on("response", capture)

            ss = lambda name: page.screenshot(path=str(Path(__file__).parent / f"debug_{name}.png"))

            dealer_url = f"{VY_BASE_URL}{VY_DEALER_PATH}/"
            page.goto(dealer_url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            ss("1_after_login")
            log.info("Screenshot: debug_1_after_login.png")

            # Step 1: Open sidebar — keep clicking hamburger until back-arrow (←) appears
            # The hamburger toggle button text is "Toggle menubar"
            hamburger = page.locator("a:has-text('Toggle menubar'), .navbar-toggle, .hamburger").first
            for attempt in range(4):
                # Check if sidebar is open: back-arrow button appears, or PROSPECTS is visible
                sidebar_open = page.evaluate("""() => {
                    const backArrow = document.querySelector('.navbar-toggle.unfolded:not(.hided), a.back-arrow, [class*="back"]');
                    const prospects = Array.from(document.querySelectorAll('*'))
                        .find(el => el.textContent.includes('PROSPECTS') && el.getBoundingClientRect().width > 0);
                    // Check if sidebar menu is unfolded by looking at body/wrapper class
                    const body = document.body.className;
                    const sidebar = document.querySelector('.site-sidebar, #sidebar, nav.site-menu');
                    const sidebarVisible = sidebar && sidebar.getBoundingClientRect().width > 50;
                    return { sidebarVisible, bodyClass: body.substring(0, 80), prospectsFound: !!prospects };
                }""")
                log.info("Sidebar state (attempt %d): %s", attempt + 1, sidebar_open)
                ss(f"sidebar_attempt_{attempt + 1}")

                if sidebar_open.get("prospectsFound") or sidebar_open.get("sidebarVisible"):
                    log.info("Sidebar is open.")
                    break

                log.info("Sidebar not open yet — clicking hamburger (attempt %d)…", attempt + 1)
                try:
                    hamburger.click(force=True, timeout=5_000)
                except Exception:
                    page.evaluate("document.querySelector('.navbar-toggle, .hamburger, a[href=\"#\"]').click()")
                page.wait_for_timeout(2_000)
            else:
                log.warning("Could not confirm sidebar open after 4 attempts.")

            ss("2_after_hamburger")
            log.info("Screenshot: debug_2_after_hamburger.png")

            # Step 2: Click PROSPECTS using Playwright native text locator (handles icons in text)
            try:
                page.locator("text=PROSPECTS").first.click(force=True, timeout=5_000)
                log.info("PROSPECTS clicked via Playwright locator.")
            except Exception:
                # Fallback: JS contains-based match
                r = page.evaluate("""() => {
                    const el = Array.from(document.querySelectorAll('li, a, div, span'))
                        .find(el => el.textContent.includes('PROSPECTS') && !el.textContent.includes('TEST'));
                    if (el) { el.click(); return 'clicked via JS: ' + el.textContent.trim().substring(0, 30); }
                    return 'not found';
                }""")
                log.info("PROSPECTS JS fallback: %s", r)
            page.wait_for_timeout(1_500)
            ss("3_after_prospects")
            log.info("Screenshot: debug_3_after_prospects.png")

            # Click TEST DRIVES
            td_clicked = page.evaluate("""() => {
                const all = Array.from(document.querySelectorAll('a, li, span, div, button'));
                const el = all.find(el => el.textContent.trim().toUpperCase() === 'TEST DRIVES');
                if (el) { el.scrollIntoView(); el.click(); return 'clicked TEST DRIVES: ' + el.outerHTML.substring(0, 100); }
                const texts = all.map(e => e.textContent.trim()).filter(t => t.length > 2 && t.length < 25);
                return 'not found. texts: ' + [...new Set(texts)].slice(0, 30).join(' | ');
            }""")
            log.info("TEST DRIVES click: %s", td_clicked[:300])
            log.info("Waiting for bookings data to load…")
            page.wait_for_timeout(6_000)
            ss("4_after_test_drives")
            log.info("Screenshot: debug_4_after_test_drives.png")

            # Refresh cookies back into requests.Session
            for cookie in context.cookies():
                session.cookies.set(
                    cookie["name"], cookie["value"],
                    domain=cookie.get("domain", "").lstrip(".")
                )

            browser.close()

        if api_responses:
            url, data = api_responses[-1]
            _bookings_api_url = url
            log.info("Cached bookings API URL: %s", url)
            bookings = parse_bookings_json(data)
        else:
            log.warning("No bookings API call captured — trying to parse page HTML is not possible for this SPA.")

    except Exception as exc:
        log.error("Browser navigation for bookings failed: %s", exc)

    return bookings


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
    """Parse bookings from a Virtual Yard API JSON response."""
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = (
            data.get("records") or data.get("rows")
            or data.get("bookings") or data.get("appointments")
            or data.get("data") or data.get("results") or []
        )
        if isinstance(items, dict):
            items = list(items.values())
    else:
        log.warning("Unexpected JSON structure: %s", type(data))
        return []

    if not items and isinstance(data, dict):
        log.debug("API returned empty list. Response keys: %s", list(data.keys()))

    bookings = []
    for item in items:
        if not isinstance(item, dict):
            continue
        booking_id = (
            item.get("id") or item.get("testdriveId") or item.get("prospectId")
            or item.get("booking_id") or item.get("appointmentId")
        )
        name = (
            item.get("customerName") or item.get("customer_name")
            or item.get("name") or item.get("customer")
            or f"{item.get('firstName', '')} {item.get('lastName', '')}".strip()
            or "Unknown"
        )
        booking_datetime = (
            item.get("appointmentDate") or item.get("appointment_date")
            or item.get("date") or item.get("datetime")
            or item.get("scheduled_at") or item.get("startDate") or "Unknown"
        )
        vehicle = (
            item.get("vehicleName") or item.get("vehicle_name")
            or item.get("vehicle") or item.get("model") or item.get("car") or "Unknown"
        )
        phone = item.get("phone") or item.get("mobile") or item.get("mobileNumber") or item.get("contact_number")
        email = item.get("email") or item.get("emailAddress") or ""
        status = item.get("status") or item.get("bookingStatus") or ""

        if not booking_id:
            booking_id = f"{name}-{booking_datetime}".lower().replace(" ", "_")

        bookings.append({
            "id": str(booking_id),
            "name": name,
            "datetime": booking_datetime,
            "vehicle": vehicle,
            "phone": phone,
            "email": email,
            "status": status,
        })

    return bookings


# ---------------------------------------------------------------------------
# Notification formatting
# ---------------------------------------------------------------------------


def format_booking_message(booking: dict) -> str:
    """Format a booking dict into a Telegram message."""
    phone_line = f"\n📞 Phone: {booking['phone']}" if booking.get("phone") else ""
    email_line = f"\n✉️ Email: {booking['email']}" if booking.get("email") else ""
    status_line = f"\n📋 Status: {booking['status']}" if booking.get("status") else ""
    return (
        f"🚗 <b>New Test Drive Booking!</b>\n"
        f"🆔 ID: {booking['id']}\n"
        f"👤 Customer: {booking['name']}"
        f"{phone_line}{email_line}\n"
        f"📅 Date/Time: {booking['datetime']}\n"
        f"🚙 Vehicle: {booking['vehicle']}"
        f"{status_line}"
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
