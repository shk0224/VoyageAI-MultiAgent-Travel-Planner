"""Step 1 of the pipeline: understand the user's request.

Two separate jobs, on purpose:

1. `extract_request` uses the LLM to read free text ("me and my wife, Seattle,
   Dec 10-17") into a `TripRequest`. LLMs are good at reading language.
2. `validate_request` uses plain Python to check it: dates real and in the
   future, return after departure, airports that exist. LLMs are bad at
   guaranteeing rules, so they never get the final say.

If something required is missing or wrong, we return questions for the user
instead of guessing.
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from langchain_core.messages import HumanMessage, SystemMessage

from core.llm import structured
from core.models import Trip, TripRequest
from tools.airports import describe, resolve_airports

MAX_DAYS_AHEAD = 330   # Google Flights only shows roughly 11 months ahead
MAX_NIGHTS = 30

EXTRACTION_PROMPT = """You extract travel details from a user's message into a form.
This app plans trips within the United States only (50 states + DC).

Today's date is {today} ({weekday}).

Rules:
- Only fill a field if the user said it, or it follows directly from what they said.
  If they did not mention something, leave it null. Do not guess.
- Dates must be YYYY-MM-DD. If the user gives a month and day without a year,
  use the next future occurrence. Resolve relative dates ("next Friday",
  "this weekend") from today's date.
- "5-day trip" means nights = 4. "a week" means nights = 7.
- "me and my wife" means adults = 2.
- Budget: convert "$2k" to 2000. Only US dollars.
- States: fill origin_state / destination_state when the user gives one ("Portland, Maine")
  or the city is clearly unique (Atlanta -> Georgia). If a city name exists in several
  states (Portland, Columbus, Springfield) and the user didn't say, leave the state null.
- Countries: only fill origin_country / destination_country if the user named a country
  outside the U.S. (e.g. "Tokyo, Japan"). Never fill them for U.S. places.

The user may be refining an earlier request. The earlier details are below.
Return the COMPLETE updated form: keep earlier values unless the new message
changes them. If the new message is clearly a different trip, start fresh.

Earlier details (JSON, may be empty):
{previous}
"""


def extract_request(llm, message: str, previous: dict | None, today: date) -> TripRequest:
    """Ask the LLM to fill a TripRequest. Raises on LLM/network failure."""
    structured_llm = structured(llm, TripRequest)
    system = EXTRACTION_PROMPT.format(
        today=today.isoformat(), weekday=today.strftime("%A"),
        previous=json.dumps(previous or {}, default=str))
    result = structured_llm.invoke([SystemMessage(content=system), HumanMessage(content=message)])
    if isinstance(result, dict):           # some providers return a dict
        result = TripRequest(**result)
    return result


# When a city changes, its state/country no longer apply (Portland, OR -> Boston must not keep "Oregon").
DEPENDENT_FIELDS = {"origin_city": ("origin_state", "origin_country"),
                    "destination_city": ("destination_state", "destination_country")}


def _same_place(a, b) -> bool:
    return (a or "").strip().lower() == (b or "").strip().lower()


def merge_requests(previous: dict | None, new: TripRequest) -> TripRequest:
    """Combine the earlier trip with the new message.

    - A value the LLM dropped is kept from before (safety net).
    - If a city changed, the old state/country for it are dropped unless the
      user gave a different one with the new city.
    """
    if not previous:
        return new
    merged = dict(previous)
    incoming = new.model_dump()
    for key, value in incoming.items():
        if value not in (None, [], ""):
            merged[key] = value
    for city_key, dependents in DEPENDENT_FIELDS.items():
        old_city, new_city = previous.get(city_key), incoming.get(city_key)
        if new_city and old_city and not _same_place(old_city, new_city):
            for dep_key in dependents:
                new_value = incoming.get(dep_key)
                if not new_value or _same_place(new_value, previous.get(dep_key)):
                    merged[dep_key] = None     # it belonged to the old city
    return TripRequest(**merged)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def validate_request(req: TripRequest, today: date) -> tuple[Trip | None, list[str]]:
    """Return (Trip, []) if everything checks out, else (None, questions)."""
    questions: list[str] = []
    assumptions: list[str] = []

    # --- Airports -----------------------------------------------------------
    origin = resolve_airports(req.origin_city, req.origin_state, req.origin_country, label="from")
    dest = resolve_airports(req.destination_city, req.destination_state, req.destination_country, label="to")
    if origin.question:
        questions.append(origin.question)
    if dest.question:
        questions.append(dest.question)
    if origin.codes and dest.codes and set(origin.codes) & set(dest.codes):
        questions.append("The departure and destination airports are the same. Where are you going?")

    # --- Dates --------------------------------------------------------------
    dep = _parse_date(req.departure_date)
    ret = _parse_date(req.return_date)
    if dep is None:
        questions.append("What date do you want to leave? (e.g. 2026-12-10)")
    elif dep < today:
        questions.append(f"The departure date {dep} is in the past. What date do you want to leave?")
    elif dep > today + timedelta(days=MAX_DAYS_AHEAD):
        questions.append(f"{dep} is too far ahead for flight search (max about 11 months). Pick an earlier date?")

    nights = None
    if dep and ret:
        if ret <= dep:
            questions.append(f"The return date {ret} is not after the departure date {dep}. When do you return?")
        else:
            nights = (ret - dep).days
    elif dep and req.nights is not None:
        nights = req.nights
        if not req.one_way and nights > 0:
            ret = dep + timedelta(days=nights)
            assumptions.append(f"Return date set to {ret} ({nights} night{'s' if nights != 1 else ''} after departure).")
    elif dep and req.one_way:
        questions.append("How many nights do you need a hotel at your destination? (Say 0 if you don't need one.)")
    elif dep:
        questions.append("When do you come back, or how many nights will you stay?")

    min_nights = 0 if req.one_way else 1
    if nights is not None and not (min_nights <= nights <= MAX_NIGHTS):
        questions.append(f"A {nights}-night stay is outside what I can plan ({min_nights}-{MAX_NIGHTS} nights).")

    if req.one_way:
        ret = None

    # --- Travellers ---------------------------------------------------------
    adults = req.adults
    if adults is None:
        adults = 1
        assumptions.append("1 adult traveller (you didn't say how many people).")
    if not (1 <= adults <= 9):
        questions.append("Flight search supports 1-9 adults. How many adults are travelling?")
    children = req.children or 0
    if not (0 <= children <= 8):
        questions.append("How many children are travelling? (0-8)")

    if req.budget_usd is not None and req.budget_usd <= 0:
        questions.append("What is your total budget in US dollars?")

    if questions:
        return None, questions

    if len(origin.codes) > 1:
        assumptions.append(f"Searching all {req.origin_city} airports: {', '.join(origin.codes)}.")
    if len(dest.codes) > 1:
        assumptions.append(f"Searching all {req.destination_city} airports: {', '.join(dest.codes)}.")

    trip = Trip(
        origin_label=req.origin_city.strip(),
        destination_label=req.destination_city.strip(),
        origin_state=origin.state,
        destination_state=dest.state,
        origin_city_name=origin.city or req.origin_city.strip(),
        destination_city_name=dest.city or req.destination_city.strip(),
        origin_airports=origin.codes,
        destination_airports=dest.codes,
        departure_date=dep,
        return_date=ret,
        nights=nights,
        adults=adults,
        children=children,
        budget_usd=req.budget_usd,
        nonstop_only=bool(req.nonstop_only),
        travel_class=req.travel_class or "economy",
        interests=req.interests,
        min_hotel_rating=req.min_hotel_rating,
        allow_rentals=req.allow_rentals is not False,
        allow_hostels=req.allow_hostels is not False,
        assumptions=assumptions + [f"Airport: {describe(r.codes[0])}" for r in (origin, dest) if len(r.codes) == 1],
    )
    return trip, []
