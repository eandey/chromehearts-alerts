"""
Chrome Hearts NY (West Village) appointment watcher — v4.

Screen-aware version. Each step it reads the page and reacts:
  welcome ("Schedule a booking") -> service ("Personal Shopping")
  -> party size (picks 1) -> date/time picker.
On the date/time screen it records enabled calendar days and any
visible times, then notifies via ntfy.sh when new availability appears.
Tapping the notification opens the booking page.
Saves shot-N.png at every step for debugging.
"""
import json
import os
import re
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
STATE_FILE = Path("state.json")
MAX_STEPS = 8

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b")
DAY_NUM_RE = re.compile(r"^\s*\d{1,2}\s*$")
DATE_NAME_RE = re.compile(
    r"(January|February|March|April|May|June|July|August"
    r"|September|October|November|December)",
    re.I,
)
NO_AVAIL_RE = re.compile(
    r"(no (times|availability|appointments)|fully booked|no slots)", re.I
)


def notify(title: str, message: str) -> None:
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set; skipping notification")
        return
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
    print("Notification sent")


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


def scan_calendar(page, page_texts: list, available_days: list) -> None:
    """Record enabled calendar days, open a few so times render."""
    candidates = (
        page.get_by_role("button", name=DAY_NUM_RE),
        page.get_by_role("gridcell", name=DAY_NUM_RE),
        page.get_by_role("radio", name=DAY_NUM_RE),
        page.get_by_role("option", name=DAY_NUM_RE),
        page.locator("button").filter(has_text=DAY_NUM_RE),
        page.get_by_role("button", name=DATE_NAME_RE),
    )
    clicked = 0
    for locator in candidates:
        try:
            count = min(locator.count(), 40)
        except Exception:
            continue
        for i in range(count):
            el = locator.nth(i)
            try:
                if not el.is_visible() or not el.is_enabled():
                    continue
                if el.get_attribute("aria-disabled") == "true":
                    continue
                label = (
                    el.get_attribute("aria-label") or el.inner_text().strip()
                )
                if label and label not in available_days:
                    available_days.append(label)
                if clicked < 3:
                    el.click(timeout=2_000)
                    page.wait_for_timeout(4_000)
                    page_texts.append(body_text(page))
                    clicked += 1
            except Exception:
                continue
        if available_days:
            break
    print(f"Enabled days seen: {len(available_days)}; opened {clicked}")


def main() -> None:
    page_texts = []
    available_days = []

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
                # Unknown screen — assume it's the date/time picker
                print(f"Step {n}: treating as date/time screen")
                scan_calendar(page, page_texts, available_days)
                break

            if not acted:
                print(f"Step {n}: recognized screen but couldn't click; stopping")
                break

        page.wait_for_timeout(2_000)
        page_texts.append(body_text(page))
        shot(page, "screenshot.png")
        browser.close()

    dom_times: set = set()
    for text in page_texts:
        dom_times.update(TIME_RE.findall(text))
    slots = set(dom_times) if dom_times else set(available_days)

    all_text = "\n".join(page_texts)
    Path("found.json").write_text(
        json.dumps(
            {
                "dom_times": sorted(dom_times),
                "available_days": available_days,
                "slots": sorted(slots),
                "saw_no_availability_text": bool(NO_AVAIL_RE.search(all_text)),
            },
            indent=2,
        )
    )

    old: set = set()
    if STATE_FILE.exists():
        try:
            old = set(json.loads(STATE_FILE.read_text()).get("slots", []))
        except Exception:
            pass

    new_slots = slots - old
    print(f"Found {len(slots)} slot(s); {len(new_slots)} new since last check")

    if new_slots:
        preview = ", ".join(sorted(new_slots)[:6])
        more = f" (+{len(new_slots) - 6} more)" if len(new_slots) > 6 else ""
        notify(
            "Chrome Hearts NY: appointment available",
            f"New availability: {preview}{more}. Tap to book.",
        )

    STATE_FILE.write_text(json.dumps({"slots": sorted(slots)}, indent=2))


if __name__ == "__main__":
    main()