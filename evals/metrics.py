"""Plan-quality metrics. Tests check rules; these check whether a plan is GOOD for a user.

compute_metrics(out) takes the dict returned by backend.run_travel_agent() and
returns numbers plus pass/fail checks. Used by evals/run_eval.py.
"""
from __future__ import annotations

from datetime import date, timedelta

from core.hours import slot_status
from core.places_plan import day_spread_km

# Targets for a plan we'd be happy to show a user.
TARGETS = {
    "grounded_share": 1.0,            # every stop is a real listed place (places mode)
    "empty_plannable_days": 0,        # no day with free time but zero activities
    "activities_per_full_day": 1.5,   # at least ~2 things on a full day
    "max_day_spread_km": 12.0,        # a day shouldn't zig-zag across the region
    "hotel_km_to_sights": 8.0,        # hotel reasonably close to the sights
}


def compute_metrics(out: dict) -> dict:
    ctx = out.get("context") or {}
    mode = ctx.get("mode", "sources")
    draft = out.get("itinerary") or {"days": []}
    free = {int(k): v for k, v in (ctx.get("free_slots") or {}).items()}
    places = {p["id"]: p for p in ctx.get("places", [])}
    first_day = ctx.get("first_day")

    by_day = {d["day_number"]: d.get("activities", []) for d in draft.get("days", [])}
    acts = [a for d in by_day.values() for a in d]
    full_days = [d for d, slots in free.items() if len(slots) >= 2]
    plannable = [d for d, slots in free.items() if slots]

    grounded = [a for a in acts if (a.get("place_id") in places) if mode == "places"] if mode == "places" \
        else [a for a in acts if a.get("source_id")]
    hours_open = 0
    spreads = []
    if mode == "places" and first_day:
        start = date.fromisoformat(first_day)
        for d, day_acts in by_day.items():
            day_places = [places[a["place_id"]] for a in day_acts if a.get("place_id") in places]
            spreads.append(day_spread_km(day_places))
            for a in day_acts:
                p = places.get(a.get("place_id"))
                if p and slot_status(p, start + timedelta(days=d - 1), a["time_of_day"]) == "open":
                    hours_open += 1

    budget = out.get("budget") or {}
    hotel = next((h for h in (out.get("hotel_results") or {}).get("offers", [])
                  if h.get("id") == budget.get("hotel_id")), None)
    flight = next((f for f in (out.get("flight_results") or {}).get("offers", [])
                   if f.get("id") == budget.get("flight_id")), None)
    from core.render import timing_warnings

    m = {
        "mode": mode,
        "places_found": len(places),
        "plannable_days": len(plannable),
        "full_days": len(full_days),
        "activities": len(acts),
        "activities_per_full_day": round(sum(len(by_day.get(d, [])) for d in full_days) / len(full_days), 2)
        if full_days else None,
        "empty_plannable_days": sum(1 for d in plannable if not by_day.get(d)),
        "grounded_share": round(len(grounded) / len(acts), 2) if acts else None,
        "hours_confirmed_share": round(hours_open / len(acts), 2) if acts and mode == "places" else None,
        "max_day_spread_km": max(spreads) if spreads else None,
        "hotel": hotel.get("name") if hotel else None,
        "hotel_km_to_sights": hotel.get("sights_km") if hotel else None,
        "hotel_trusted_rating": hotel.get("trusted_rating") if hotel else None,
        "flight_timing_warnings": len(timing_warnings(flight)) if flight else None,
        "budget_fits": (None if budget.get("user_budget_usd") is None or budget.get("subtotal_usd") is None
                        else budget["subtotal_usd"] <= budget["user_budget_usd"]),
        "removed_items": (out.get("verification") or {}).get("removed"),
        "llm_calls": out.get("llm_calls"),
    }
    checks = {
        "real places only": mode == "places" and m["grounded_share"] == TARGETS["grounded_share"],
        "no empty days": m["empty_plannable_days"] == TARGETS["empty_plannable_days"],
        "enough per day": (m["activities_per_full_day"] or 0) >= TARGETS["activities_per_full_day"]
        if full_days else True,
        "compact days": m["max_day_spread_km"] is None or m["max_day_spread_km"] <= TARGETS["max_day_spread_km"],
        "hotel near sights": m["hotel_km_to_sights"] is None or m["hotel_km_to_sights"] <= TARGETS["hotel_km_to_sights"],
        "hotel rating trusted": m["hotel"] is None or m["hotel_trusted_rating"] is not None,
    }
    m["checks"] = checks
    m["passed"] = sum(checks.values())
    m["total_checks"] = len(checks)
    return m
