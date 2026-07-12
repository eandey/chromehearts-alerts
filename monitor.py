"""
Chrome Hearts NY (West Village) appointment watcher — v3.

Waitwhile streams availability over Firebase (not plain API calls), so
this version reads what actually renders on the page: it clicks
"Schedule a booking" -> the chosen service -> the calendar, then looks
for bookable times. Enabled calendar days count as availability too.
Sends a push via ntfy.sh; tapping the notification opens the booking
page. Saves a screenshot at every step (shot-*.png) for debugging.
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
# Which service to watch — change to "Product Pickup", "Repair", etc. if needed
SERVICE_NAME = os.environ.get("SERVICE_NAME", "Personal Shopping")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = Path("state.json")

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


def click_first_match(page, patterns, roles=("button", "link")) -> bool:
    """Click the first visible element matching any pattern, by role then text."""
    for pattern in patterns:
        rx = re.compile(pattern, re.I)
        for role in roles:
            try:
                el = page.get_by_role(role, name=rx).first
                if el.is_visible():
                    el.click(timeout=5_000)
                    print(f"Clicked {role} matching /{pattern}/")
                    return True
            except Exception:
                continue
        try:
            el = page.get_by_text(rx).first
            if el.is_visible():
                el.click(timeout=5_000)
                print(f"Clicked text matching /{pattern}/")
                return True
        except Exception:
            continue
    return False


def main() -> None:
    page_texts = []
    available_days = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 480, "height": 960})

        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        page_texts.append(page.inner_text("body"))
        shot(page, "shot-1-loaded.png")

        # Step 1: welcome screen -> "Schedule a booking"
        if click_first_match(page, (r"schedule", r"book")):
            page.wait_for_timeout(5_000)
        shot(page, "shot-2-after-welcome.png")

        # Step 2: pick the service
        if click_first_match(page, (re.escape(SERVICE_NAME),)):
            page.wait_for_timeout(6_000)
        else:
            print(f"Service '{SERVICE_NAME}' not found on page")
        page_texts.append(page.inner_text("body"))
        shot(page, "shot-3-after-service.png")

        # Step 3: click through any intermediate screen if one appears
        if click_first_match(
            page, (r"^continue$", r"^next$", r"anyone|any team member|no preference")
        ):
            page.wait_for_timeout(5_000)
        page_texts.append(page.inner_text("body"))

        # Step 4: find enabled calendar days; open the first few
        clicked_days = 0
        for locator in (
            page.get_by_role("button", name=DAY_NUM_RE),
            page.get_by_role("gridcell", name=DAY_NUM_RE),
            page.get_by_role("button", name=DATE_NAME_RE),
        ):
            try:
                count = min(locator.count(), 31)
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
                        el.get_attribute("aria-label")
                        or el.inner_text().strip()
                    )
                    available_days.append(label)
                    if clicked_days < 3:
                        el.click(timeout=2_000)
                        page.wait_for_timeout(4_000)
                        page_texts.append(page.inner_text("body"))
                        clicked_days += 1
                except Exception:
                    continue
            if available_days:
                break
        print(
            f"Enabled days seen: {len(available_days)}; opened {clicked_days}"
        )
        shot(page, "screenshot.png")
        browser.close()

    # Availability = times seen on the page; fall back to enabled days
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