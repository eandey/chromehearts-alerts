"""
Chrome Hearts NY (West Village) appointment watcher — v2.

Selects a service on the Waitwhile booking page, opens the calendar,
and sends a push notification via ntfy.sh when new appointment times
appear. Tapping the notification opens the booking page.
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


def walk_for_slots(node, out: set) -> None:
    """Heuristically pull available slot labels out of Waitwhile API JSON."""
    if isinstance(node, dict):
        if node.get("available") is False or node.get("isAvailable") is False:
            return
        for key in ("time", "startTime", "start", "startDate"):
            v = node.get(key)
            if isinstance(v, str):
                out.add(v)
        times = node.get("times")
        if isinstance(times, list):
            for t in times:
                if isinstance(t, str):
                    out.add(t)
        for v in node.values():
            walk_for_slots(v, out)
    elif isinstance(node, list):
        for v in node:
            walk_for_slots(v, out)


def main() -> None:
    api_hits = []
    all_api_urls = []
    page_texts = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 480, "height": 960})

        def on_response(resp):
            u = resp.url
            if "waitwhile" not in u:
                return
            if u not in all_api_urls:
                all_api_urls.append(u)
            if any(k in u.lower() for k in
                   ("time", "availab", "slot", "booking", "date")):
                try:
                    api_hits.append({"url": u, "data": resp.json()})
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        page_texts.append(page.inner_text("body"))

        # Step 1: pick the service explicitly
        try:
            page.get_by_text(
                re.compile(SERVICE_NAME, re.I)
            ).first.click(timeout=10_000)
            print(f"Clicked service: {SERVICE_NAME}")
            page.wait_for_timeout(5_000)
        except Exception as e:
            print(f"Could not click service '{SERVICE_NAME}': {e}")

        # Step 2: click through any intermediate screen if one appears
        for pattern in (r"^continue$", r"^next$",
                        r"anyone|any team member|no preference"):
            try:
                btn = page.get_by_role(
                    "button", name=re.compile(pattern, re.I)
                ).first
                if btn.is_visible():
                    btn.click(timeout=3_000)
                    page.wait_for_timeout(4_000)
            except Exception:
                pass
        page_texts.append(page.inner_text("body"))

        # Step 3: open the first few selectable days on the calendar
        clicked_days = 0
        for locator in (
            page.get_by_role("button", name=DAY_NUM_RE),
            page.get_by_role("button", name=DATE_NAME_RE),
        ):
            try:
                count = min(locator.count(), 31)
            except Exception:
                continue
            for i in range(count):
                if clicked_days >= 3:
                    break
                b = locator.nth(i)
                try:
                    if (
                        b.is_visible()
                        and b.is_enabled()
                        and b.get_attribute("aria-disabled") != "true"
                    ):
                        b.click(timeout=2_000)
                        page.wait_for_timeout(3_000)
                        page_texts.append(page.inner_text("body"))
                        clicked_days += 1
                except Exception:
                    continue
            if clicked_days:
                break
        print(f"Opened {clicked_days} calendar day(s)")

        page.screenshot(path="screenshot.png", full_page=True)
        browser.close()

    # Prefer structured API data; fall back to times visible on the page
    slots: set = set()
    for hit in api_hits:
        walk_for_slots(hit["data"], slots)
    dom_times: set = set()
    for text in page_texts:
        dom_times.update(TIME_RE.findall(text))
    if not slots:
        slots = dom_times

    all_text = "\n".join(page_texts)
    Path("found.json").write_text(
        json.dumps(
            {
                "all_api_urls": all_api_urls[:50],
                "parsed_api_urls": [h["url"] for h in api_hits][:50],
                "slots": sorted(slots),
                "dom_times": sorted(dom_times),
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
            f"New times: {preview}{more}. Tap to book.",
        )

    STATE_FILE.write_text(json.dumps({"slots": sorted(slots)}, indent=2))


if __name__ == "__main__":
    main()