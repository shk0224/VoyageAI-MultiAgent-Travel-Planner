"""Validate an LLM-generated travel itinerary.

Checks:
- Unsupported dollar amounts
- Invalid source IDs
- Unknown flight numbers
- Unverified opening-hours claims
- Invalid or missing trip days
- Activities in flight-blocked time slots
- Duplicate activities across days
- Empty or malformed activities

Limitations:
This is structural and pattern-based validation, NOT full
factual verification. Attraction existence, source relevance,
travel times, and opening hours require additional evidence.
"""

from __future__ import annotations

import re
import unicodedata


MONEY = re.compile(
    r"(?:\$|USD\s?)\s?(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s?(?:USD|dollars)\b",
    re.I,
)

FLIGHT_NO = re.compile(r"\b([A-Z0-9]{2})\s?(\d{1,4})\b")
CITATION = re.compile(r"\[(S\d+|F\d+|H\d+)\]")

HOURS_CLAIM = re.compile(
    r"\b(open|opens|opening|closes|closed)(?![-\w])[^.]{0,40}?"
    r"\b(daily|every|on|from|until|till|at|"
    r"\d{1,2}(:\d{2})?\s?(am|pm)|"
    r"mon|tue|wed|thu|fri|sat|sun|"
    r"weekdays?|weekends?)",
    re.I,
)

VALID_SLOTS = {"Morning", "Afternoon", "Evening"}

# Transit directions and card names are a common LLM error (real run: "Use a MetroCard",
# discontinued at the end of 2025; "Take the E train from JFK", which needs the AirTrain first).
# We have no verified transit data, so these are not allowed in LLM text.
TRANSIT_DIRECTIONS = re.compile(
    r"\b(take|ride|hop on|catch|board|transfer to)\b[^.]{0,30}?\b(train|subway|bus|metro|tram|streetcar|"
    r"airtrain|light rail|line \d+|\d+ line|[a-z] line|"
    r"(red|blue|green|orange|yellow|silver|purple|brown|pink|gold) line)\b"
    r"|\b[A-Z0-9]\s(train|line)\b", re.I)
TRANSIT_CARDS = re.compile(r"\b(metro ?card|omny|clipper card|clipper|smartrip|charlie ?card|ventra|tap card|"
                           r"orca card|breeze card|presto)\b", re.I)
FREE_ENTRY = re.compile(r"\bfree\s+(admission|entry|entrance|to enter|of charge)\b|\badmission is free\b", re.I)


def money_values(text: str) -> list[float]:
    """Extract explicit dollar amounts from text."""
    values = []
    for match in MONEY.finditer(text or ""):
        raw = (match.group(1) or match.group(2) or "").replace(",", "")
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values


def known_flight_numbers(flights: dict) -> tuple[set[str], set[str]]:
    """Return known flight numbers and airline prefixes."""
    numbers = set()
    prefixes = set()
    for offer in flights.get("offers") or []:
        legs = (offer.get("outbound_legs") or []) + (offer.get("return_legs") or [])
        for leg in legs:
            number = (leg.get("flight_number") or "").replace(" ", "").upper()
            if number:
                numbers.add(number)
                prefixes.add(number[:2])
    return numbers, prefixes


def normalize_activity(text: str) -> str:
    """Normalize activity text for duplicate detection."""
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = re.sub(r"\[[SFH]\d+\]", "", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def check_text(
    text: str,
    allowed_money: set[float],
    source_ids: set[str],
    flight_numbers: set[str],
    airline_prefixes: set[str],
) -> list[str]:
    """Return problems detected in LLM-written text."""
    problems = []
    for value in money_values(text):
        if not any(abs(value - amount) <= 1 for amount in allowed_money):
            problems.append(f"unsupported dollar amount {value:g}")

    for citation in CITATION.findall(text or ""):
        if citation.startswith("S") and citation not in source_ids:
            problems.append(f"citation [{citation}] does not exist")

    if HOURS_CLAIM.search(text or ""):
        problems.append("opening-hours claim (no verified hours data)")

    if TRANSIT_DIRECTIONS.search(text or ""):
        problems.append("transit directions (not verified; leave them out)")

    if TRANSIT_CARDS.search(text or ""):
        problems.append("transit card or fare product name (not verified; leave it out)")

    if FREE_ENTRY.search(text or ""):
        problems.append("price claim 'free admission' (not verified)")

    without_citations = CITATION.sub("", text or "")
    for prefix, digits in FLIGHT_NO.findall(without_citations):
        flight = f"{prefix}{digits}"
        if prefix in airline_prefixes and flight not in flight_numbers:
            problems.append(f"flight {flight} is not in the flight results")
    return problems


def verify_draft(
    draft: dict,
    num_days: int,
    allowed_money: set[float],
    source_ids: set[str],
    flights: dict,
    free_slots: dict[int, list[str]] | None = None,
    places: dict[str, dict] | None = None,
    availability: dict[int, dict[str, list[str]]] | None = None,
) -> dict:
    """Validate and sanitize an itinerary draft.

    places mode (places is not None): every activity must use a listed place_id,
    that place must be open (or have unknown hours) in that slot, and no place
    may be used twice. Soft checks (empty days, spread-out days) ask the LLM to
    retry but never remove anything.

    Maintains the existing backend-compatible return structure.
    """
    numbers, prefixes = known_flight_numbers(flights)
    problems = []
    removed = 0
    clean_by_day: dict[int, list] = {day: [] for day in range(1, num_days + 1)}
    seen_days = set()
    seen_activities = set()
    used_places: set[str] = set()
    soft: list[str] = []        # retry hints that do not remove items
    warnings: list[str] = []    # shown to the user

    for day in draft.get("days") or []:
        n = day.get("day_number")
        if not isinstance(n, int) or isinstance(n, bool):
            problems.append(f"invalid day number: {n}")
            continue
        if not (1 <= n <= num_days):
            problems.append(f"day {n} is outside the trip (1-{num_days})")
            continue
        if n in seen_days:
            problems.append(f"day {n} is duplicated (activities were merged)")
        seen_days.add(n)

        for act in day.get("activities") or []:
            desc = (act.get("description") or "").strip()
            sid = act.get("source_id")
            slot = act.get("time_of_day")
            issues = []

            if not desc:
                issues.append("activity description is empty")
            issues.extend(check_text(desc, allowed_money, source_ids, numbers, prefixes))
            # In places mode the place_id is the evidence; a stray source_id is ignored.
            if places is None and sid is not None and sid not in source_ids:
                issues.append(f"source_id {sid} does not exist")
            if slot not in VALID_SLOTS:
                issues.append(f"invalid time slot: {slot}")
            if free_slots is not None and slot not in free_slots.get(n, []):
                issues.append(f"{slot} clashes with your flight or travel schedule")

            pid = act.get("place_id")
            if places is not None:
                if not pid or pid not in places:
                    issues.append(f"place_id {pid} is not in the place list")
                elif pid in used_places:
                    issues.append(f"{places[pid]['name']} ({pid}) is used more than once")
                elif availability is not None and pid not in (availability.get(n) or {}).get(slot, []):
                    issues.append(f"{places[pid]['name']} ({pid}) is not open in the {slot} on day {n}")

            activity_key = normalize_activity(desc)
            if activity_key and activity_key in seen_activities:
                issues.append("duplicate activity description")

            if issues:
                problems.extend(f"Day {n}: {issue}" for issue in issues)
                removed += 1
                continue

            if activity_key:
                seen_activities.add(activity_key)
            if places is not None and pid:
                used_places.add(pid)
            clean_by_day[n].append(act)

    missing = [day for day in range(1, num_days + 1) if day not in seen_days]
    if missing:
        problems.append("days missing from the plan: " + ", ".join(map(str, missing)))

    clean_days = [
        {"day_number": day, "activities": clean_by_day[day]}
        for day in range(1, num_days + 1)
    ]

    if places is not None and availability is not None:
        from core.places_plan import MAX_DAY_SPREAD_KM, day_spread_km
        for day in range(1, num_days + 1):
            acts = clean_by_day[day]
            open_slots = availability.get(day) or {}
            used_slots = {a.get("time_of_day") for a in acts}
            # Slots that could be filled with an unused, open place but were left empty.
            fillable = [slot for slot, ids in open_slots.items()
                        if slot not in used_slots and any(i not in used_places for i in ids)]
            # Only an EMPTY day is a problem: filling every slot would use up all places early
            # and leave later days empty. 2 activities per full day is a good plan.
            if not acts and fillable:
                soft.append(f"Day {day} has no activities, but these slots have open, unused places: "
                            f"{', '.join(fillable)}. Spread places across all days (about 2 per full day).")
            day_places = [places[a["place_id"]] for a in acts if a.get("place_id") in places]
            spread = day_spread_km(day_places)
            if spread > MAX_DAY_SPREAD_KM:
                soft.append(f"Day {day}: places are {spread} km apart; keep the day in one or two nearby zones")
                warnings.append(f"Day {day} covers a wide area (about {spread} km between stops); allow extra travel time.")

    clean_tips = []
    for tip in draft.get("tips") or []:
        if not isinstance(tip, str) or not tip.strip():
            problems.append("empty or invalid travel tip")
            removed += 1
            continue
        issues = check_text(tip, allowed_money, source_ids, numbers, prefixes)
        if issues:
            problems.extend(f"Tip: {issue}" for issue in issues)
            removed += 1
            continue
        clean_tips.append(tip)

    return {
        "ok": not problems and not soft,
        "problems": problems + soft,
        "warnings": warnings,
        "removed": removed,
        "missing_days": missing,
        "clean_draft": {"days": clean_days, "tips": clean_tips},
    }
