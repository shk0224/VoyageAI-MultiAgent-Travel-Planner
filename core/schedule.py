"""Which parts of each day are actually free, given the flight times. Plain Python.

Example from a real run: the return flight left at 06:00, but the LLM planned a
"morning stroll before heading to the airport". This module makes that impossible:
blocked time slots are told to the LLM AND enforced by the verifier.

Slots: Morning 08-12, Afternoon 12-17, Evening 17-22 (local time).
- Arrival day: a slot is free only if it starts at least 1.5 hours after landing
  (getting out of the airport and to the hotel).
- Departure day: a slot is free only if it ends at least 3 hours before takeoff
  (getting to the airport and through security).
"""
from __future__ import annotations

SLOTS = {"Morning": (8, 12), "Afternoon": (12, 17), "Evening": (17, 22)}
AFTER_LANDING_HOURS = 1.5   # landing 10:30 -> afternoon (12:00) is usable
BEFORE_TAKEOFF_HOURS = 3


def _hour(timestamp: str | None) -> float | None:
    """'2026-12-13 06:00' -> 6.0"""
    t = (timestamp or "")[11:16]
    try:
        h, m = t.split(":")
        return int(h) + int(m) / 60
    except ValueError:
        return None


def free_slots(day_number: int, num_days: int, arrival: str | None, departure: str | None,
               first_day: str | None = None) -> list[str]:
    """Time slots that can hold activities on this day."""
    free = list(SLOTS)
    if day_number == 1 and arrival:
        h = _hour(arrival)
        same_day = first_day is None or arrival[:10] == first_day
        if h is not None and same_day:
            free = [s for s in free if SLOTS[s][0] >= h + AFTER_LANDING_HOURS]
    if day_number == num_days and departure:
        h = _hour(departure)
        if h is not None:
            free = [s for s in free if SLOTS[s][1] <= h - BEFORE_TAKEOFF_HOURS]
    # Keep fixed slots compatible with core.hours and core.verifier.
    # Partial departure mornings require a coordinated app-wide change.
    return free


def schedule(num_days: int, arrival: str | None, departure: str | None, first_day: str | None = None) -> dict[int, list[str]]:
    return {n: free_slots(n, num_days, arrival, departure, first_day) for n in range(1, num_days + 1)}
