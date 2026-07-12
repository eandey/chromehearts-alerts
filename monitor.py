"""
Chrome Hearts NY (West Village) appointment watcher.

Loads the Waitwhile booking page in a headless browser, watches the
availability API responses, and sends a push notification via ntfy.sh
when new appointment slots appear. Tapping the notification opens the
booking page.
"""
import json
import os
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

# The page the script checks (goes straight into the booking flow)
BOOKING_URL = os.environ.get(
    "BOOKING_URL", "https://waitwhile.com/locations/chromehearts/bookings/add"
)
# The page that opens when you tap the notification
CLICK_URL = os.environ.get(
    "CLICK_URL", "https://waitwhile.com/locations/chromehearts"
)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
STATE_FILE = Path("state.json")

TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM|am|pm)\b")
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

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 480, "height": 960})

        def on_response(resp):
            u = resp.url.lower()
            if "waitwhile" in u and any(
                k in u for k in ("booking-times", "availab", "slots", "times")
            ):
                try:
                    api_hits.append({"url": resp.url, "data": resp.json()})
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)

        # Try to advance through the booking flow (service selection screens)
        for _ in range(3):
            clicked = False
            for pattern in (r"book", r"appointment", r"continue", r"next"):
                try:
                    btn = page.get_by_role(
                        "button", name=re.compile(pattern, re.I)
                    ).first
                    if btn.is_visible():
                        btn.click(timeout=3_000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                try:  # fall back: first service card / list item
                    item = page.locator("[role=listitem], main button").first
                    if item.is_visible():
                        item.click(timeout=3_000)
                        clicked = True
                except Exception:
                    pass
            page.wait_for_timeout(4_000)
            if not clicked:
                break

        body_text = page.inner_text("body")
        page.screenshot(path="screenshot.png", full_page=True)
        browser.close()

    # Prefer structured API data; fall back to times visible on the page
    slots: set = set()
    for hit in api_hits:
        walk_for_slots(hit["data"], slots)
    dom_times = set(TIME_RE.findall(body_text))
    if not slots:
        slots = dom_times

    # Debug dump (uploaded as a workflow artifact)
    Path("found.json").write_text(
        json.dumps(
            {
                "api_urls": [h["url"] for h in api_hits],
                "slots": sorted(slots),
                "dom_times": sorted(dom_times),
                "saw_no_availability_text": bool(NO_AVAIL_RE.search(body_text)),
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
