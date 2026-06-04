# BYD Sales Consultant Booking Notifier

Polls [virtualyard.com](https://virtualyard.com) for new test drive bookings and sends instant Telegram alerts to a BYD sales consultant.

---

## Features

- Logs into virtualyard.com automatically
- Polls for new test drive bookings every 60 seconds (configurable)
- Sends a Telegram message for each new booking
- Tracks seen bookings in `seen_bookings.json` to avoid duplicate alerts
- Handles session expiry with automatic re-login

---

## Prerequisites

- Python 3.10 or newer
- A Telegram bot (see below)
- Your virtualyard.com login credentials

---

## 1. Get a Telegram Bot Token

1. Open Telegram and search for **@BotFather**.
2. Start a chat and send `/newbot`.
3. Follow the prompts — choose a name and username for your bot.
4. BotFather will reply with an API token like `123456789:ABCdefGHIjklMNOpqrSTUvwxYZ`.
5. Copy this token — it goes in `.env` as `TELEGRAM_BOT_TOKEN`.

---

## 2. Find Your Telegram Chat ID

**Option A — Using @userinfobot:**
1. Search Telegram for **@userinfobot** and start a chat.
2. Send any message; it replies with your user ID.
3. Use that number as `TELEGRAM_CHAT_ID`.

**Option B — Using the Bot API directly:**
1. Start a conversation with your newly created bot (send it any message).
2. Open this URL in your browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
3. In the JSON response find `"chat":{"id": 123456789, ...}` — that number is your chat ID.

**For a group chat:** Add the bot to the group, send a message, then use the `getUpdates` method above. The chat ID for a group will be a negative number (e.g. `-1001234567890`).

---

## 3. Install and Run

```bash
# Clone / navigate to the project
cd /path/to/TD_BookingNotifier

# Install dependencies
pip install -r requirements.txt

# Set up your credentials
cp .env.example .env
nano .env          # fill in all values

# Run the notifier
python notifier.py
```

The notifier will:
1. Log in to virtualyard.com
2. Immediately check for bookings
3. Poll every `POLL_INTERVAL_SECONDS` seconds (default: 60)
4. Print timestamped log lines to stdout

Press **Ctrl+C** to stop.

---

## 4. Environment Variables (`.env`)

| Variable | Description |
|---|---|
| `VY_USERNAME` | Your virtualyard.com login email |
| `VY_PASSWORD` | Your virtualyard.com password |
| `TELEGRAM_BOT_TOKEN` | Token from BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram user ID or group chat ID |
| `POLL_INTERVAL_SECONDS` | How often to check for bookings (default: 60) |

---

## 5. Adjusting CSS Selectors (if bookings are not detected)

Because virtualyard.com's exact HTML structure is unknown, the scraper uses placeholder selectors that you may need to tune.

### Step-by-step:

1. **Log in manually** in Chrome or Firefox.
2. Navigate to the bookings/test-drive page.
3. Open **DevTools → Elements** (F12).
4. Right-click a booking row and choose **Inspect**.
5. Note the tag, class, and any `data-*` attributes of:
   - The booking container (table row, card div, list item, etc.)
   - The customer name cell
   - The date/time cell
   - The vehicle cell
   - The phone cell (if present)

6. Open `notifier.py` and update the constants in `parse_bookings()`:

```python
# Example: if bookings are in a table with class "appointments"
BOOKING_ROW_SELECTOR = "table.appointments tbody tr"
```

And in `_extract_booking_fields()`:

```python
# Example: if the name is in a <td> with class "cust-name"
name_cell = row.select_one("td.cust-name")
```

7. If the page loads bookings via **JavaScript/XHR** (the HTML source has no booking data):
   - Open DevTools → **Network** tab, filter by **Fetch/XHR**.
   - Reload the page and look for an API request returning booking JSON.
   - Set `VY_BOOKINGS_URL` in `notifier.py` to that API URL.
   - The `parse_bookings_json()` function will handle JSON responses automatically.

### Debugging tips

Run the script and check the log output. When selectors don't match, the script logs:

```
[WARNING] No booking rows found with selector '...'. Inspect the bookings page HTML...
```

It also logs an HTML snippet in DEBUG mode. To enable DEBUG logging, add this near the top of `notifier.py`:

```python
logging.basicConfig(level=logging.DEBUG, ...)
```

---

## File Structure

```
TD_BookingNotifier/
├── notifier.py          # Main script
├── requirements.txt     # Python dependencies
├── .env.example         # Credentials template
├── .env                 # Your actual credentials (never commit this)
├── seen_bookings.json   # Auto-created; tracks sent alerts
└── README.md            # This file
```

---

## Security Note

Never commit your `.env` file. It is listed in `.gitignore` by convention. Treat your bot token and password as secrets.
