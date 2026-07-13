"""
Golden Diner (NYC) Resy reservation watcher.

Same pattern as the Chrome Hearts watcher (monitor.py): a long-lived
daemon that polls Resy's public API — the same endpoints resy.com's
own frontend calls — and pushes an ntfy alert the moment a day flips
from sold-out to available.

Per poll, one GET to /4/venue/calendar covers the whole booking
window (RESY_DAYS days). When a new date shows availability, a
follow-up GET to /4/find fetches its actual times for the alert.

The API key is Resy's public web-client key, embedded in their
frontend JS bundle (not a user credential).
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

VENUE_ID = os.environ.get("RESY_VENUE_ID", "9520")
VENUE_NAME = os.environ.get("RESY_VENUE_NAME", "Golden Diner")
PARTY_SIZE = os.environ.get("RESY_PARTY_SIZE", "2")
DAYS = int(os.environ.get("RESY_DAYS", "30"))
POLL_SECONDS = float(os.environ.get("RESY_POLL_SECONDS", "10"))
API_KEY = os.environ.get(
    "RESY_API_KEY", "VbWk7s3L4KiK5fzlO7JD3Q5EYolJI7n5"
)
# resy.com/link is Resy's universal link: it opens the app on phones
# and forwards date/seats, landing on the venue page with that day
# preselected and its open slots one tap from booking.
CLICK_URL = os.environ.get(
    "RESY_CLICK_URL",
    f"https://resy.com/link?venue_id={VENUE_ID}&seats={PARTY_SIZE}",
)


def day_link(day: str) -> str:
    return f"{CLICK_URL}&date={day}" if "resy.com/link" in CLICK_URL else CLICK_URL
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
STORE_TZ = ZoneInfo(os.environ.get("STORE_TZ", "America/New_York"))
ERROR_ALERT_AFTER_SECONDS = int(os.environ.get("ERROR_ALERT_AFTER_SECONDS", "900"))
STATUS_LOG_EVERY = int(os.environ.get("STATUS_LOG_EVERY", "60"))  # polls

CALENDAR_URL = "https://api.resy.com/4/venue/calendar"
FIND_URL = "https://api.resy.com/4/find"
STATE_FILE = Path("resy_state.json")
FOUND_FILE = Path("resy_found.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp} UTC] {msg}", flush=True)


def notify(
    title: str, message: str, priority: str = "urgent", click_url: str = None
) -> bool:
    click_url = click_url or CLICK_URL
    if not NTFY_TOPIC:
        log(f"NTFY_TOPIC not set; would have sent: {title} — {message} -> {click_url}")
        return True
    for attempt in range(1, 4):
        try:
            r = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    # Tapping the notification opens the Resy page:
                    "Click": click_url,
                    "Priority": priority,
                    "Tags": "fork_and_knife",
                },
                timeout=30,
            )
            r.raise_for_status()
            log(f"Notification sent (attempt {attempt})")
            return True
        except Exception as e:
            log(f"Notify attempt {attempt} failed: {e}")
            time.sleep(5 * attempt)
    return False


def fetch_available_dates(session: requests.Session) -> set:
    """One calendar check. Returns dates ('2026-07-15') whose
    reservation inventory is 'available'. Raises on any failure."""
    today = datetime.now(STORE_TZ).date()
    params = {
        "venue_id": VENUE_ID,
        "num_seats": PARTY_SIZE,
        "start_date": today.isoformat(),
        "end_date": (today + timedelta(days=DAYS)).isoformat(),
    }
    r = session.get(CALENDAR_URL, params=params, timeout=15)
    r.raise_for_status()
    days = r.json().get("scheduled", [])
    return {
        d["date"]
        for d in days
        if d.get("inventory", {}).get("reservation") == "available"
    }


def fetch_day_times(session: requests.Session, day: str) -> list:
    """Slot start times for one day, e.g. ['1:00 PM', '2:30 PM']."""
    params = {
        "lat": "0",
        "long": "0",
        "day": day,
        "party_size": PARTY_SIZE,
        "venue_id": VENUE_ID,
    }
    r = session.get(FIND_URL, params=params, timeout=15)
    r.raise_for_status()
    times = []
    for venue in r.json().get("results", {}).get("venues", []):
        for slot in venue.get("slots", []):
            start = slot.get("date", {}).get("start")  # '2026-07-15 13:00:00'
            if start:
                try:
                    dt = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
                    times.append(dt.strftime("%-I:%M %p"))
                except ValueError:
                    times.append(start)
    return times


def pretty_day(day: str) -> str:
    """'2026-07-15' -> 'Wed Jul 15' (falls back to raw)."""
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%a %b %-d")
    except ValueError:
        return day


def describe(session: requests.Session, days: list) -> str:
    """'Wed Jul 15 (1:00 PM, 2:30 PM), Wed Aug 5 (6:15 PM)' for up to
    the first 3 days; times are best-effort."""
    parts = []
    for day in days[:3]:
        label = pretty_day(day)
        try:
            times = fetch_day_times(session, day)
        except Exception as e:
            log(f"Fetching times for {day} failed: {e}")
            times = []
        if times:
            shown = ", ".join(times[:4])
            more = f" +{len(times) - 4}" if len(times) > 4 else ""
            parts.append(f"{label} ({shown}{more})")
        else:
            parts.append(label)
    if len(days) > 3:
        parts.append(f"+{len(days) - 3} more day(s)")
    return ", ".join(parts)


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()).get("dates", []))
        except Exception:
            pass
    return set()


def save_state(dates: set) -> None:
    STATE_FILE.write_text(json.dumps({"dates": sorted(dates)}, indent=2))


def save_found(dates: set) -> None:
    FOUND_FILE.write_text(
        json.dumps(
            {
                "dates": sorted(dates),
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def main() -> None:
    log(
        f"Resy watcher starting: {VENUE_NAME} (venue {VENUE_ID}), party of "
        f"{PARTY_SIZE}, next {DAYS} days, poll every {POLL_SECONDS:g}s, "
        f"ntfy={'set' if NTFY_TOPIC else 'NOT SET'}"
    )
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Authorization": f'ResyAPI api_key="{API_KEY}"',
            "Origin": "https://resy.com",
            "Referer": "https://resy.com/",
        }
    )

    started = time.monotonic()
    known = load_state()
    polls = 0
    failures_since = None
    error_alerted = False
    backoff = 0.0

    while True:
        polls += 1
        try:
            available = fetch_available_dates(session)
        except Exception as e:
            now_mono = time.monotonic()
            if failures_since is None:
                failures_since = now_mono
            failing_for = now_mono - failures_since
            log(f"Poll failed ({failing_for:.0f}s into streak): {e}")
            if (
                ERROR_ALERT_AFTER_SECONDS
                and not error_alerted
                and failing_for >= ERROR_ALERT_AFTER_SECONDS
            ):
                error_alerted = notify(
                    f"{VENUE_NAME} monitor: checks failing",
                    f"Resy checks have failed for {failing_for / 60:.0f} "
                    f"minutes. Last error: {e}",
                    priority="high",
                )
            backoff = min(max(backoff * 2, POLL_SECONDS * 2), 300)
            time.sleep(backoff)
            continue

        if failures_since is not None:
            log("Polls recovered")
        failures_since = None
        error_alerted = False
        backoff = 0.0

        new_dates = available - known
        if new_dates:
            log(f"NEW AVAILABILITY: {sorted(new_dates)}")
            save_found(available)
            first_day = sorted(new_dates)[0]
            sent = notify(
                f"{VENUE_NAME}: reservation available",
                f"{describe(session, sorted(new_dates))}. Tap to book.",
                click_url=day_link(first_day),
            )
            if sent:
                known = available
                save_state(known)
            else:
                log("All notify attempts failed; will re-alert next poll")
        elif available != known:
            # Days sold out again — clear them so a reopening re-alerts.
            log(f"Availability changed: {sorted(known)} -> {sorted(available)}")
            save_found(available)
            known = available
            save_state(known)
        elif polls % STATUS_LOG_EVERY == 1:
            log(f"Poll #{polls}: {len(available)} day(s) available, no change")

        if MAX_RUNTIME_SECONDS and time.monotonic() - started >= MAX_RUNTIME_SECONDS:
            log(f"MAX_RUNTIME_SECONDS={MAX_RUNTIME_SECONDS} reached; exiting")
            return

        time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
