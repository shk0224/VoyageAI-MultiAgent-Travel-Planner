"""The only place the LLM writes content the user reads.

Two modes
---------
- "places" (normal): the LLM picks from a list of REAL places (P1, P2...) from
  Google Maps. For every day and time slot, Python says which places are open,
  so the LLM chooses; it does not invent. Place names, ratings, addresses and
  hours shown to the user come from the data, not from the LLM.
- "sources" (fallback when the places search fails): the older behaviour, where
  activities cite web search results (S1, S2...).

In both modes the LLM fills a fixed structure (days -> activities). Python sets
the dates, and core/verifier.py checks every activity before the user sees it.
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from core.llm import structured
from core.models import Trip


class PlannedActivity(BaseModel):
    time_of_day: Literal["Morning", "Afternoon", "Evening"]
    place_id: Optional[str] = Field(None, description="ID of the place from the list, e.g. 'P3'. Required in places mode.")
    description: str = Field(description="One or two sentences on what to do there. No prices, opening hours, "
                                          "transit directions or flight numbers.")
    source_id: Optional[str] = Field(None, description="Only in sources mode: the source ID, e.g. 'S2'.")


class DayPlan(BaseModel):
    day_number: int = Field(description="1 = arrival day.")
    activities: list[PlannedActivity]


class ItineraryDraft(BaseModel):
    days: list[DayPlan]
    tips: list[str] = Field(default_factory=list, description="Up to 4 practical tips. No prices.")


PLACES_SYSTEM = """You are a careful, evidence-grounded U.S. travel itinerary planner.

You plan a day-by-day itinerary using ONLY the real places listed in "places".
Every place there comes from Google Maps with its rating, location and hours.

STRICT RULES:
1. Plan exactly {num_days} days, numbered 1 through {num_days}.
2. Every activity MUST use a place_id from "places". Never mention a place that is not in the list.
3. Only put a place in a slot where "options" lists it for that day and slot
   (those are the slots where it is open, or where hours are unknown). If a day has no slots, its activities list is empty.
4. Use each place at most once in the whole trip.
5. Spread the places across ALL days so that no day with available slots is left empty.
   Aim for one worthwhile attraction in EACH available slot (up to three per day). Do not add filler simply to reach a number.
6. Keep each day geographically tight: prefer places in the same "zone" on the same day,
   and avoid crossing between far-apart zones in one day.
7. Restaurants and food places fit best in Afternoon (lunch) or Evening (dinner) slots.
8. Match the traveller's interests when possible; otherwise choose a balanced mix of the best-rated sights.
9. The description is 1-2 sentences about what to do or see there. Do NOT include prices, costs,
   opening hours, transit directions (train/bus/subway lines), transit card names, or flight numbers.
   Do NOT claim anything is open, bookable or happening on a specific date.
10. Day 1 is arrival day: keep it light. Respect the departure day's available slots.
11. Up to four practical tips relevant to the destination, season and itinerary (no prices, no transit card names).
12. Give travelers a satisfying, varied itinerary: prioritize iconic attractions and interests, then nearby highly rated places. Avoid repetitive or generic attractions.
13. The current app supports at most ONE activity per Morning, Afternoon, or Evening slot. Do not invent exact visit times, travel durations, ticket prices, or bookings.
14. Use specific, accurate descriptions of each place, never generic praise or unsupported claims such as "the best".
15. Never put activities in blocked slots. A departure day with no safe slots must remain empty.

Return the requested structured itinerary only.
"""

SOURCES_SYSTEM = """You are a careful, evidence-grounded U.S. travel itinerary planner.

Create a realistic day-by-day itinerary using ONLY the supplied sources.

STRICT RULES:
1. Plan exactly {num_days} days, numbered 1 through {num_days}.
2. Use ONLY the time slots provided in free_slots. If no slots are available for a day, return an empty activities list.
3. Treat the destination city as the destination. Do not prioritize airport attractions or layover activities.
4. Use only attractions supported by the supplied source snippets, with the correct source_id. Do not invent places.
5. Do not include prices, opening hours, transit directions, transit card names or flight numbers.
6. Arrival day should be light. Do not recommend the same attraction twice.
7. Up to four practical tips. Prefer fewer realistic activities over an unreliable itinerary.

Return the requested structured itinerary only.
"""


def place_options(context: dict) -> dict:
    """{'day 2': {'Morning': ['P1', 'P4'], ...}} built from availability computed in Python."""
    out = {}
    for day, slots in context.get("availability", {}).items():
        out[f"day {day}"] = {slot: ids for slot, ids in slots.items()}
    return out


def write_itinerary(llm, trip: Trip, context: dict, feedback: list[str] | None = None) -> dict:
    """Return an ItineraryDraft as a dict. Raises on LLM failure."""
    num_days = context["num_days"]
    mode = context.get("mode", "sources")
    base = {
        "destination": trip.destination_search_place,
        "dates": f"{context['first_day']} to {context['last_day']} (day 1 = {context['first_day']})",
        "travellers": {"adults": trip.adults, "children": trip.children},
        "interests": trip.interests,
        "arrival_time_day_1": context.get("arrival_time"),
        "departure_time_last_day": context.get("departure_time"),
        "hotel": context.get("hotel_name"),
    }
    if mode == "places":
        system = PLACES_SYSTEM.format(num_days=num_days)
        base["places"] = [
            {"id": p["id"], "name": p["name"], "type": p["type"], "category": p["category"],
             "rating": p["rating"], "reviews": p["reviews"], "zone": p.get("zone"),
             "km_from_hotel": p.get("km_from_hotel")}
            for p in context.get("places", [])
        ]
        base["options"] = place_options(context)
    else:
        system = SOURCES_SYSTEM.format(num_days=num_days)
        base["free_slots"] = {f"day {k}": v for k, v in context.get("free_slots", {}).items()}
        base["sources"] = [{"id": s["id"], "title": s["title"], "snippet": s["snippet"]}
                           for s in context.get("sources", [])]

    # Compact JSON (no indentation): about 40% fewer tokens. Groq's free tier allows
    # roughly 8K tokens per minute, and a retry doubles the usage.
    messages = [SystemMessage(content=system),
                HumanMessage(content=json.dumps(base, separators=(",", ":"), ensure_ascii=False))]
    if feedback:
        messages.append(HumanMessage(content=(
            "Your previous draft broke these rules. Fix them and return the full plan again:\n- "
            + "\n- ".join(feedback[:15]))))

    result = structured(llm, ItineraryDraft).invoke(messages)
    if isinstance(result, BaseModel):
        result = result.model_dump()
    return result
