"""Real flight data from Google Flights (via SerpApi).

What changed from the old version
---------------------------------
- Input is a validated `Trip` (from core/trip_parser.py), not raw text. No regex.
- Output is structured data (a dict), not a pre-formatted string, so later
  steps can compute budgets and check the final answer against it.
- Round trips are supported. Google Flights returns outbound flights first;
  each one has a `departure_token`, and a second request with that token
  returns the matching return flights and the final round-trip price.
- Every offer gets an ID (F1, F2, ...) so the answer can point back to it.

Check once with your own key (open question in the API docs):
    Is `price` the total for ALL passengers, or per person? Run the same search
    with adults=1 and adults=2 and compare. Set PRICE_IS_TOTAL_FOR_ALL_ADULTS
    to match what you see. The budget math depends on it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import requests

from core.models import Trip

SERPAPI_URL = "https://serpapi.com/search.json"
PRICE_IS_TOTAL_FOR_ALL_ADULTS = True   # used by core/budget.py
PRICE_BASIS_VERIFIED = False           # set True after you confirm the line above with a real search
RETURN_LOOKUPS = 2          # how many outbound options get a return-flight lookup (each costs 1 API call)
TIMEOUT_SECONDS = 35
TRAVEL_CLASS = {"economy": "1", "premium_economy": "2", "business": "3", "first": "4"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result(status: str, message: str = "", offers=None, query=None, price_insights=None) -> dict:
    """Every tool returns this same shape. status: ok | empty | error | skipped."""
    return {
        "source": "Google Flights via SerpApi",
        "fetched_at": _now(),
        "status": status,
        "message": message,
        "query": query or {},
        "offers": offers or [],
        "price_insights": price_insights or {},
    }


def _call(params: dict, session) -> dict:
    response = (session or requests).get(SERPAPI_URL, params=params, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected response format from SerpApi.")
    if payload.get("error"):
        raise ValueError(f"SerpApi error: {payload['error']}")
    return payload


def _legs(raw_legs: list) -> list[dict]:
    legs = []
    for leg in raw_legs or []:
        dep = leg.get("departure_airport") or {}
        arr = leg.get("arrival_airport") or {}
        legs.append({
            "airline": leg.get("airline"),
            "flight_number": leg.get("flight_number"),
            "from": dep.get("id"), "from_name": dep.get("name"), "departs": dep.get("time"),
            "to": arr.get("id"), "to_name": arr.get("name"), "arrives": arr.get("time"),
            "duration_min": leg.get("duration"),
            "travel_class": leg.get("travel_class"),
        })
    return legs


def _matches_route(legs: list[dict], origins: set[str], destinations: set[str]) -> bool:
    return bool(legs) and legs[0]["from"] in origins and legs[-1]["to"] in destinations


def search_flights(trip: Trip, limit: int = 5, session=None) -> dict:
    """Search flights for a validated trip. Never raises; errors come back as status='error'."""
    key = os.getenv("SERPAPI_API_KEY")
    if not key:
        return _result("error", "Flight search is not configured (SERPAPI_API_KEY missing).")

    params = {
        "engine": "google_flights",
        "departure_id": ",".join(trip.origin_airports),
        "arrival_id": ",".join(trip.destination_airports),
        "outbound_date": trip.departure_date.isoformat(),
        "type": "1" if trip.is_round_trip else "2",
        "adults": trip.adults,
        "children": trip.children,
        "travel_class": TRAVEL_CLASS.get(trip.travel_class, "1"),
        "currency": "USD", "hl": "en", "gl": "us",
        "api_key": key,
    }
    if trip.is_round_trip:
        params["return_date"] = trip.return_date.isoformat()
    if trip.nonstop_only:
        params["stops"] = "1"   # SerpApi: 1 = nonstop only (we also filter below)

    public_query = {k: v for k, v in params.items() if k != "api_key"}

    try:
        payload = _call(params, session)
    except (requests.RequestException, ValueError) as exc:
        return _result("error", f"Flight search failed: {exc}", query=public_query)

    raw = (payload.get("best_flights") or []) + (payload.get("other_flights") or [])
    origins, destinations = set(trip.origin_airports), set(trip.destination_airports)

    candidates = []
    for item in raw:
        legs = _legs(item.get("flights"))
        if not _matches_route(legs, origins, destinations):
            continue                       # API sometimes adds nearby airports
        if trip.nonstop_only and len(legs) != 1:
            continue
        if not isinstance(item.get("price"), (int, float)):
            continue                       # no price = we can't use it for a budget
        candidates.append((item, legs))

    candidates.sort(key=lambda pair: pair[0]["price"])
    count = max(1, min(limit, 10))
    shortlist = candidates[:count]
    # Always show the cheapest NONSTOP option if Google returned one: the five cheapest
    # are often all one-stop flights (real run: ATL -> JFK showed no nonstops).
    nonstop_index = None
    if not any(len(legs) == 1 for _, legs in shortlist):
        nonstop = next(((item, legs) for item, legs in candidates if len(legs) == 1), None)
        if nonstop:
            shortlist = shortlist[:count - 1] + [nonstop]
            nonstop_index = len(shortlist) - 1
    candidates = shortlist

    offers = []
    for index, (item, legs) in enumerate(candidates):
        offer = {
            "id": f"F{len(offers) + 1}",
            "price_usd": item["price"],
            "price_covers": "round trip" if trip.is_round_trip else "one way",
            "outbound_legs": legs,
            "outbound_stops": len(legs) - 1,
            "outbound_duration_min": item.get("total_duration"),
            "return_legs": [],
            "return_status": "not applicable" if not trip.is_round_trip else "not looked up",
        }
        # Round trip: look up return flights for the cheapest few outbound options.
        if (trip.is_round_trip and (index < RETURN_LOOKUPS or index == nonstop_index)
                and item.get("departure_token")):
            try:
                back = _call({**params, "departure_token": item["departure_token"]}, session)
                back_raw = (back.get("best_flights") or []) + (back.get("other_flights") or [])
                back_ok = []
                for b in back_raw:
                    b_legs = _legs(b.get("flights"))
                    if _matches_route(b_legs, destinations, origins) and isinstance(b.get("price"), (int, float)):
                        if not trip.nonstop_only or len(b_legs) == 1:
                            back_ok.append((b, b_legs))
                if back_ok:
                    b, b_legs = min(back_ok, key=lambda pair: pair[0]["price"])
                    offer["return_legs"] = b_legs
                    offer["return_stops"] = len(b_legs) - 1
                    offer["return_duration_min"] = b.get("total_duration")
                    offer["price_usd"] = b["price"]   # final round-trip price after choosing the return
                    offer["return_status"] = "found"
                else:
                    offer["return_status"] = "no matching return flights returned"
            except (requests.RequestException, ValueError) as exc:
                offer["return_status"] = f"return lookup failed: {exc}"
        offers.append(offer)

    if not offers:
        return _result("empty", "No priced flights matched these airports, dates and stop settings. "
                                "This does not prove none exist; try other dates or airports.",
                       query=public_query)

    return _result("ok", offers=offers, query=public_query,
                   price_insights=payload.get("price_insights") or {})
