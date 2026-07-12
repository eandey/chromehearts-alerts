"""
Chrome Hearts NY (West Village) appointment watcher — v7.

Changes vs v6:
- Runs several checks per invocation (CHECKS_PER_RUN, SLEEP_SECONDS)
  so GitHub's 5-minute cron floor still yields ~1-minute coverage
- Notifies immediately mid-run when a new slot appears

Flow per check: welcome -> service -> party size -> "Select date and
time". If the page says "No available times for the next N days",
stop quietly. Otherwise click each day chip across up to 3 pages of
the date strip, record times per day (e.g. "Mon 13 Jul 6:00 PM"),
and push new ones via ntfy.sh. Tapping the notification opens the
booking page.
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BOOKING_URL = os.environ.get(
    "BOOKING_URL", "https://waitwhile.com/locations/chromehearts/bookings/add"
)
CLICK_URL = os.environ.get(
    "CLICK_URL", "https://waitwhile.com/locations/chromehearts"
)
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Personal Shopping")
PARTY_SIZE = os.environ.get("PARTY_SIZE", "1")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
CHECKS_PER_RUN = int(os.environ.get("CHECKS_PER_RUN", "1"))
SLEEP_SECONDS = int(os.environ.get("SLEEP_SECONDS", "0"))
STATE_FILE = Path("state.json")
MAX_STEPS = 8
DATE_PAGES = 3  # pages of the date strip to scan

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b")
DAY_CHIP_RE = re.compile(r"\b(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", re.I)
NO_AVAIL_RE = re.compile(
    r"(no available (times|appointments)|no (times|availability|appointments)"
    r"|fully booked|no slots)",
    re.I,
)
NO_AVAIL_WINDOW_RE = re.compile(
    r"no available times for the next \d+ days", re.I
)


def notify(title: str, message: str) -> bool:
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping notification")
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
                    "Priority": "urgent",
                    "Tags": "bell",
                },
                timeout=30,
            )
            r.raise_for_status()
            print(f"Notification sent (attempt {attempt})")
            return True
        except Exception as e:
            print(f"Notify attempt {attempt} failed: {e}")
            time.sleep(5 * attempt)
    return False


def shot(page, name: str) -> None:
    try:
        page.screenshot(path=name, full_page=True)
    except Exception as e:
        print(f"Screenshot {name} failed: {e}")


def body_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def click_regex(page, pattern: str) -> bool:
    """Click the first visible element matching pattern (button, link, or text)."""
    rx = re.compile(pattern, re.I)
    for get in (
        lambda: page.get_by_role("button", name=rx).first,
        lambda: page.get_by_role("link", name=rx).first,
        lambda: page.get_by_text(rx).first,
    ):
        try:
            el = get()
            if el.is_visible():
                el.click(timeout=5_000)
                print(f"Clicked /{pattern}/")
                return True
        except Exception:
            continue
    return False


def scan_day_chips(page, page_texts: list, slots: set) -> int:
    """Click each visible day chip and record 'day + time' slots."""
    scanned = 0
    for role in ("button", "tab", "radio", "option"):
        loc = page.get_by_role(role, name=DAY_CHIP_RE)
        try:
            count = min(loc.count(), 14)
        except Exception:
            continue
        if count == 0:
            continue
        for i in range(count):
            el = loc.nth(i)
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                if el.get_attribute("aria-disabled") == "true":
                    continue
                label = (
                    el.get_attribute("aria-label") or el.inner_text()
                ).strip().replace("\n", " ")
                el.click(timeout=2_000)
                page.wait_for_timeout(2_500)
                text = body_text(page)
                page_texts.append(text)
                for t in sorted(set(TIME_RE.findall(text))):
                    slots.add(f"{label} {t}")
                scanned += 1
            except Exception:
                continue
        if scanned:
            break
    return scanned


def run_check() -> tuple[set, bool]:
    """One full pass through the booking flow. Returns (slots, fully_booked)."""
    page_texts = []
    slots: set = set()
    fully_booked = False

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 480, "height": 960})
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)

        for n in range(1, MAX_STEPS + 1):
            page.wait_for_timeout(5_000)
            text = body_text(page)
            page_texts.append(text)
            shot(page, f"shot-{n}.png")
            low = text.lower()

            if "select service" in low:
                acted = click_regex(page, re.escape(SERVICE_NAME))
            elif "party size" in low:
                acted = click_regex(page, rf"^\s*{re.escape(PARTY_SIZE)}\s*$")
            elif "schedule a booking" in low:
                acted = click_regex(page, r"schedule a booking|schedule|book")
            else:
                # Date/time screen (or something new — screenshots will show)
                print(f"Step {n}: treating as date/time screen")
                if NO_AVAIL_WINDOW_RE.search(text):
                    fully_booked = True
                    print("Page says no availability in the whole window")
                else:
                    for page_num in range(DATE_PAGES):
                        scan_day_chips(page, page_texts, slots)
                        if page_num < DATE_PAGES - 1:
                            if not click_regex(page, r"next|forward"):
                                break
                            page.wait_for_timeout(2_500)
                break

            if not acted:
                print(f"Step {n}: recognized screen but couldn't click; stopping")
                break

        page.wait_for_timeout(2_000)
        page_texts.append(body_text(page))
        shot(page, "screenshot.png")
        browser.close()

    all_text = "\n".join(page_texts)
    Path("found.json").write_text(
        json.dumps(
            {
                "slots": sorted(slots),
                "fully_booked_message": fully_booked,
                "saw_no_availability_text": bool(NO_AVAIL_RE.search(all_text)),
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        )
    )
    return slots, fully_booked


def load_state() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()).get("slots", []))
        except Exception:
            pass
    return set()


def main() -> None:
    notify_failed = False

    for i in range(CHECKS_PER_RUN):
        if i:
            time.sleep(SLEEP_SECONDS)
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"--- Check {i + 1}/{CHECKS_PER_RUN} at {stamp} UTC ---")

        try:
            slots, _ = run_check()
        except Exception as e:
            print(f"Check failed: {e}")
            continue

        old = load_state()
        new_slots = slots - old
        print(f"Found {len(slots)} slot(s); {len(new_slots)} new")

        if new_slots:
            preview = ", ".join(sorted(new_slots)[:5])
            more = f" (+{len(new_slots) - 5} more)" if len(new_slots) > 5 else ""
            sent = notify(
                "Chrome Hearts NY: appointment available",
                f"{preview}{more}. Tap to book.",
            )
            if not sent:
                # Keep old state so the next check re-alerts these slots
                notify_failed = True
                print("All notify attempts failed; keeping old state")
                continue

        STATE_FILE.write_text(json.dumps({"slots": sorted(slots)}, indent=2))

    if notify_failed:
        sys.exit(1)


if __name__ == "__main__":
    main()