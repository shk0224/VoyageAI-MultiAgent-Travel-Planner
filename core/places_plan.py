"""Deterministic helpers that make place-based day plans realistic. Plain Python, no LLM.

- assign_zones():    groups places that are close together (A, B, C...), so a day can stay in one area.
- availability():    for each day and time slot, which places are open (or have unknown hours).
- day_spread_km():   how far apart a day's places are (largest straight-line distance).
"""
from __future__ import annotations

from datetime import date, timedelta

from core.hours import slot_status
from tools.places_tool import km_between

ZONE_LINK_KM = 3.0          # places closer than this end up in the same zone
MAX_DAY_SPREAD_KM = 12.0    # wider than this in one day -> warn (lots of travel)


def assign_zones(places: list[dict]) -> None:
    """Single-linkage clustering: link any two places within ZONE_LINK_KM. Sets p['zone']."""
    parent = list(range(len(places)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(places)):
        for j in range(i + 1, len(places)):
            a, b = places[i], places[j]
            if km_between(a["lat"], a["lng"], b["lat"], b["lng"]) <= ZONE_LINK_KM:
                parent[find(i)] = find(j)
    # Name zones by size (largest = A) so labels are stable and meaningful.
    groups: dict[int, list[int]] = {}
    for i in range(len(places)):
        groups.setdefault(find(i), []).append(i)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for n, members in enumerate(sorted(groups.values(), key=len, reverse=True)):
        for i in members:
            places[i]["zone"] = letters[n] if n < len(letters) else f"Z{n}"


def add_hotel_distance(places: list[dict], hotel: dict | None) -> None:
    for p in places:
        if hotel and hotel.get("lat") is not None and hotel.get("lng") is not None:
            p["km_from_hotel"] = round(km_between(p["lat"], p["lng"], hotel["lat"], hotel["lng"]), 1)
        else:
            p["km_from_hotel"] = None


def availability(places: list[dict], first_day: str, free_slots: dict[int, list[str]]) -> dict:
    """{day: {slot: [place ids]}} for places that are open or have unknown hours in that slot."""
    start = date.fromisoformat(first_day)
    out = {}
    for day, slots in free_slots.items():
        d = start + timedelta(days=int(day) - 1)
        out[int(day)] = {
            slot: [p["id"] for p in places if slot_status(p, d, slot) != "closed"]
            for slot in slots
        }
    return out


def day_spread_km(place_list: list[dict]) -> float:
    best = 0.0
    for i in range(len(place_list)):
        for j in range(i + 1, len(place_list)):
            a, b = place_list[i], place_list[j]
            best = max(best, km_between(a["lat"], a["lng"], b["lat"], b["lng"]))
    return round(best, 1)


TARGET_PER_FULL_DAY = 3


def fill_gaps(draft: dict | None, places: list[dict], availability: dict[int, dict[str, list[str]]],
              free_slots: dict[int, list[str]]) -> tuple[dict, int]:
    """Fill days the LLM left (nearly) empty, using unused places that are open in those slots.

    Deterministic, no LLM: if the LLM fails or under-plans, the user still gets real stops.
    Prefers places in the zone already used that day (or the zone with most unused places).
    Added activities are marked auto=True and described only with facts from the data.
    Returns (draft, number_added).
    """
    draft = {"days": [dict(d, activities=list(d.get("activities") or [])) for d in (draft or {}).get("days", [])],
             "tips": list((draft or {}).get("tips") or [])}
    by_id = {p["id"]: p for p in places}
    days = {d["day_number"]: d for d in draft["days"]}
    used = {a.get("place_id") for d in draft["days"] for a in d["activities"]}
    rank = {p["id"]: i for i, p in enumerate(places)}           # places arrive best-first
    added = 0
    plannable = [day for day, slots in sorted(free_slots.items()) if slots]
    for index, (day, slots) in enumerate(sorted(free_slots.items())):
        if not slots:
            continue
        d = days.setdefault(day, {"day_number": day, "activities": []})
        # Scarce places: give every remaining day at least one stop before any day gets two.
        remaining_days = len([x for x in plannable if x >= day])
        unused = len([i for i in by_id if i not in used])
        # Reserve a place for each remaining plannable day where possible.
        existing = len(d["activities"])
        capacity = max(0, unused - max(0, remaining_days - 1))
        target = max(existing, min(len(slots), TARGET_PER_FULL_DAY, existing + capacity))
        zones = [by_id[a["place_id"]].get("zone") for a in d["activities"] if a.get("place_id") in by_id]
        for slot in slots:
            if len(d["activities"]) >= target:
                break
            if any(a.get("time_of_day") == slot for a in d["activities"]):
                continue
            options = [i for i in (availability.get(day) or {}).get(slot, []) if i not in used and i in by_id]
            if not options:
                continue
            if zones:
                options.sort(key=lambda i: (by_id[i].get("zone") not in zones, rank[i]))
            else:
                counts = {}
                for i in by_id:
                    if i not in used:
                        counts[by_id[i].get("zone")] = counts.get(by_id[i].get("zone"), 0) + 1
                options.sort(key=lambda i: (-counts.get(by_id[i].get("zone"), 0), rank[i]))
            pid = options[0]
            p = by_id[pid]
            # Grounded wording: no invented claims or generic star-rating filler.
            desc = f"Explore {p['name']} and check its listing for visitor information."
            d["activities"].append({"time_of_day": slot, "place_id": pid, "description": desc, "auto": True})
            used.add(pid)
            zones.append(p.get("zone"))
            added += 1
    # Rebalance: a day still empty can borrow a stop from a day with 2+ stops,
    # if that place is open on the empty day in one of its free slots.
    for day, slots in sorted(free_slots.items()):
        if not slots or days[day]["activities"]:
            continue
        donors = sorted((d for d in days.values() if len(d["activities"]) >= 2),
                        key=lambda d: -len(d["activities"]))
        moved = False
        for donor in donors:
            for act in reversed(donor["activities"]):
                for slot in slots:
                    if act.get("place_id") in (availability.get(day) or {}).get(slot, []):
                        donor["activities"].remove(act)
                        days[day]["activities"].append(dict(act, time_of_day=slot))
                        moved = True
                        break
                if moved:
                    break
            if moved:
                break
    draft["days"] = sorted(days.values(), key=lambda d: d["day_number"])
    return draft, added
