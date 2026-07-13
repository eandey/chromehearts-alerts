"""
Chrome Hearts NY (West Village) appointment watcher — v8.

Changes vs v7:
- Polls the Waitwhile public API directly (the same endpoint the
  booking page's own frontend calls) instead of driving a headless
  browser. One GET returns the first available slots, so a check
  takes <1s and we poll every few seconds (POLL_SECONDS).
- Runs as a long-lived daemon under systemd (Restart=always) and
  notifies via ntfy the moment a new slot appears.
- Set MAX_RUNTIME_SECONDS > 0 for bounded runs (GitHub Actions backup).

Endpoint discovered from the booking page's network traffic:
  GET https://api.waitwhile.com/v2/public/visits/{slug}/first-available-slots
      ?fromDate=...&toDate=...&maxNumSlots=N&serviceDuration=1800
      &partySize=1&serviceIds=<Personal Shopping id>
Returns [] when fully booked, or a list of slots (objects with a
"date" field, e.g. "2026-07-14T11:00" in the store's local time).
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

LOCATION_SLUG = os.environ.get("LOCATION_SLUG", "chromehearts")
API_URL = (
    f"https://api.waitwhile.com/v2/public/visits/{LOCATION_SLUG}"
    "/first-available-slots"
)
# Lands directly on the date/time picker (skips welcome/service/party
# screens); Waitwhile ignores date params, so the day can't be preselected.
CLICK_URL = os.environ.get(
    "CLICK_URL",
    f"https://waitwhile.com/locations/{LOCATION_SLUG}/time?registration=booking",
)
# "Personal Shopping" at Chrome Hearts NY West Village
SERVICE_ID = os.environ.get("SERVICE_ID", "WHmjBONC1Mcf8VSqjWar")
SERVICE_DURATION = int(os.environ.get("SERVICE_DURATION", "1800"))
PARTY_SIZE = os.environ.get("PARTY_SIZE", "1")
MAX_SLOTS = int(os.environ.get("MAX_SLOTS", "10"))
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
MAX_RUNTIME_SECONDS = int(os.environ.get("MAX_RUNTIME_SECONDS", "0"))
STORE_TZ = ZoneInfo(os.environ.get("STORE_TZ", "America/New_York"))
# Alert once if every poll has failed for this long (0 disables):
ERROR_ALERT_AFTER_SECONDS = int(os.environ.get("ERROR_ALERT_AFTER_SECONDS", "900"))
STATUS_LOG_EVERY = int(os.environ.get("STATUS_LOG_EVERY", "60"))  # polls

STATE_FILE = Path("state.json")
FOUND_FILE = Path("found.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp} UTC] {msg}", flush=True)


def notify(title: str, message: str, priority: str = "urgent") -> bool:
    if not NTFY_TOPIC:
        log(f"NTFY_TOPIC not set; would have sent: {title} — {message}")
        return True
    for attempt in range(1, 4):
        try:
            r = requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={
                    "Title": title,
                    # Tapping the notification opens the booking page:
                    "Click": CLICK_URL,
                    "Priority": priority,
                    "Tags": "bell",
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


def fetch_slots(session: requests.Session) -> list:
    """One availability check. Returns raw slot datetimes like
    '2026-07-14T11:00' (store-local). Raises on any failure."""
    now = datetime.now(STORE_TZ)
    params = {
        "fromDate": now.strftime("%Y-%m-%dT%H:%M"),
        "toDate": (now + timedelta(days=365)).strftime("%Y-%m-%dT00:00"),
        "maxNumSlots": MAX_SLOTS,
        "serviceDuration": SERVICE_DURATION,
        "partySize": PARTY_SIZE,
        "serviceIds": SERVICE_ID,
    }
    r = session.get(API_URL, params=params, timeout=15)
    r.raise_for_status()
    payload = r.json()
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected payload: {str(payload)[:200]}")
    dates = []
    for item in payload:
        if isinstance(item, str):
            dates.append(item)
        elif isinstance(item, dict):
            d = item.get("date") or item.get("startDate") or item.get("from")
            if d:
                dates.append(d)
    return dates


def pretty(slot: str) -> str:
    """'2026-07-14T11:00' -> 'Tue Jul 14, 11:00 AM' (falls back to raw)."""
    try:
        dt = datetime.fromisoformat(slot.replace("Z", "+00:00"))
        return dt.strftime("%a %b %-d, %-I:%M %p")
    except Exception:
        return slot


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()).get("slots", []))
        except Exception:
            pass
    return set()


def save_state(slots: set) -> None:
    STATE_FILE.write_text(json.dumps({"slots": sorted(slots)}, indent=2))


def save_found(slots: list) -> None:
    FOUND_FILE.write_text(
        json.dumps(
            {
                "slots": sorted(slots),
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )


def main() -> None:
    log(
        f"Watcher starting: poll every {POLL_SECONDS:g}s, service={SERVICE_ID}, "
        f"party={PARTY_SIZE}, ntfy={'set' if NTFY_TOPIC else 'NOT SET'}"
    )
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    started = time.monotonic()
    known = load_state()
    polls = 0
    failures_since = None  # monotonic time of first failure in a streak
    error_alerted = False
    backoff = 0.0

    while True:
        polls += 1
        try:
            slots = fetch_slots(session)
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
                    "Chrome Hearts monitor: checks failing",
                    f"Availability checks have failed for {failing_for / 60:.0f} "
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

        current = set(slots)
        new_slots = current - known
        if new_slots:
            log(f"NEW AVAILABILITY: {sorted(new_slots)}")
            save_found(slots)
            shown = [pretty(s) for s in sorted(new_slots)[:5]]
            more = f" (+{len(new_slots) - 5} more)" if len(new_slots) > 5 else ""
            sent = notify(
                "Chrome Hearts NY: appointment available",
                f"{', '.join(shown)}{more}. Tap to book.",
            )
            if sent:
                known = current
                save_state(known)
            else:
                log("All notify attempts failed; will re-alert next poll")
        elif current != known:
            # Slots disappeared (booked/withdrawn) — clear them so a
            # reappearance re-alerts.
            log(f"Availability changed: {sorted(known)} -> {sorted(current)}")
            save_found(slots)
            known = current
            save_state(known)
        elif polls % STATUS_LOG_EVERY == 1:
            log(f"Poll #{polls}: {len(current)} slot(s) known, no change")

        if MAX_RUNTIME_SECONDS and time.monotonic() - started >= MAX_RUNTIME_SECONDS:
            log(f"MAX_RUNTIME_SECONDS={MAX_RUNTIME_SECONDS} reached; exiting")
            return

        time.sleep(POLL_SECONDS * random.uniform(0.8, 1.2))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
