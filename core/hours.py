"""Opening hours: turn Google Maps text into numbers, and check a time slot.

Google Maps (via SerpApi) gives `operating_hours` per weekday, e.g.
    {"monday": "9 AM–5 PM", "tuesday": "11 AM–2:30 PM, 5–10 PM",
     "wednesday": "Open 24 hours", "thursday": "Closed", "friday": "5 PM–2 AM"}

parse_day("11 AM–2:30 PM, 5–10 PM")  -> [(11.0, 14.5), (17.0, 22.0)]
parse_day("Closed")                  -> []          (closed all day)
parse_day("Open 24 hours")           -> [(0, 24)]
parse_day("Hours might differ")      -> None        (unknown: we do NOT guess)

slot_status(place, day, "Morning") -> "open" | "closed" | "unknown"
"""
from __future__ import annotations

import re
from datetime import date

SLOT_WINDOWS = {"Morning": (8.0, 12.0), "Afternoon": (12.0, 17.0), "Evening": (17.0, 22.0)}
# Minimum hours a place must be open inside the slot to count as "open".
MIN_OPEN_HOURS = {"Morning": 2.0, "Afternoon": 2.0, "Evening": 1.5}

_TIME = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap])?\.?\s*m?\.?\s*$", re.I)
_DASHES = re.compile(r"\s*[–—-]\s*|\s+to\s+")


def _clean(text: str) -> str:
    # Google uses narrow no-break spaces ( ) and thin spaces in times.
    return (text or "").replace(" ", " ").replace(" ", " ").replace("\xa0", " ").strip()


def _to_hours(h: int, m: int, meridiem: str | None) -> float:
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "a":
            h = 0 if h == 12 else h
        else:
            h = 12 if h == 12 else h + 12
    return h + m / 60


def _parse_range(text: str) -> tuple[float, float] | None:
    parts = _DASHES.split(text.strip(), maxsplit=1)
    if len(parts) != 2:
        return None
    a, b = _TIME.match(parts[0]), _TIME.match(parts[1])
    if not a or not b:
        return None
    ah, am, amer = int(a.group(1)), int(a.group(2) or 0), a.group(3)
    bh, bm, bmer = int(b.group(1)), int(b.group(2) or 0), b.group(3)
    if ah > 12 or bh > 12 or am > 59 or bm > 59:
        return None
    if bmer is None:
        return None                       # end time must say AM/PM; otherwise unknown
    end = _to_hours(bh, bm, bmer)
    if amer is None:
        # "5–10 PM": start shares the end's AM/PM, unless that would put it after the end
        start = _to_hours(ah, am, bmer)
        if start > end:
            start = _to_hours(ah, am, "a" if bmer.lower() == "p" else "p")
    else:
        start = _to_hours(ah, am, amer)
    if end <= start:
        end += 24                         # past midnight, e.g. "5 PM–2 AM" -> (17, 26)
    return (start, end)


def parse_day(text: str | None) -> list[tuple[float, float]] | None:
    """Opening ranges for one day, [] if closed, None if unknown."""
    t = _clean(text or "").lower()
    if not t:
        return None
    if t.startswith("closed"):
        return []
    if "open 24 hours" in t:
        return [(0.0, 24.0)]
    ranges = []
    for chunk in t.split(","):
        r = _parse_range(chunk)
        if r is None:
            return None                   # one unreadable piece -> don't trust the day
        ranges.append(r)
    return ranges or None


def hours_text(place: dict, day: date) -> str | None:
    """The raw Google text for that weekday, e.g. '10 AM–5 PM'."""
    hours = place.get("hours")
    if not isinstance(hours, dict):
        return None
    value = hours.get(day.strftime("%A").lower())
    return _clean(value) if isinstance(value, str) else None


def slot_status(place: dict, day: date, slot: str) -> str:
    """'open' if the place is open long enough inside the slot, 'closed' if not, 'unknown' if no data."""
    ranges = parse_day(hours_text(place, day))
    if ranges is None:
        return "unknown"
    start, end = SLOT_WINDOWS[slot]
    overlap = sum(max(0.0, min(end, b) - max(start, a)) for a, b in ranges)
    return "open" if overlap >= MIN_OPEN_HOURS[slot] else "closed"
