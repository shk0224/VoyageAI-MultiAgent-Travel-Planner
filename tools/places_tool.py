"""Real attractions from Google Maps (via SerpApi, same SERPAPI_API_KEY).

Why this exists
---------------
Day plans used to come from ~6 short web snippets (Tavily). That is article
text, not a list of places: no addresses, hours or ratings. When the snippets
were weak (e.g. "things to do near JFK airport"), the plan was empty or odd.

This tool returns a list of real places, each with an ID (P1, P2, ...):
name, category, rating, review count, address, coordinates and weekly hours.
The LLM may only plan with places from this list, and the verifier checks it.

API (SerpApi Google Maps, documented fields):
    engine=google_maps, type=search, q=<text>
    local_results[]: title, place_id, gps_coordinates{latitude,longitude},
    rating, reviews, type, types[], address, operating_hours{monday: "9 AM–5 PM", ...},
    open_state, website, description
Cost: one SerpApi search per query: 1 base query (+1 for trips of 4+ nights) + up to 2 interest queries.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone

import requests

from core.models import Trip

SERPAPI_URL = "https://serpapi.com/search.json"
TIMEOUT_SECONDS = 35
MAX_INTEREST_QUERIES = 2          # each costs one SerpApi search
LONG_TRIP_NIGHTS = 4              # trips this long get one extra general search for more places
MAX_PLACES = 18
MIN_RATING, MIN_REVIEWS = 4.0, 50                 # normal quality bar
RELAXED_RATING, RELAXED_REVIEWS = 3.8, 20         # used only if too few places pass
MIN_PLACES_BEFORE_RELAXING = 6
MAX_KM_FROM_CENTER = 40.0         # drop matches far from where most results are

# Interest chips (from the form) -> what to search on Google Maps.
INTEREST_QUERIES = {
    "food": "best local restaurants", "museums": "museums", "beaches": "beaches",
    "outdoors": "parks and nature", "nightlife": "nightlife", "shopping": "shopping areas",
    "family-friendly": "family friendly attractions", "history": "historic sites",
    "live music": "live music venues", "sports": "sports venues",
}

# Place types that are not sightseeing.
EXCLUDED_TYPES = re.compile(
    r"\b(hotel|motel|inn|lodging|hostel|resort hotel|airport|car rental|parking|gas station|"
    r"atm|bank|pharmacy|hospital|clinic|real estate|apartment|storage|post office|"
    r"transit station|bus station|train station|subway station|travel agency|insurance)\b", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _result(status: str, message: str = "", places=None, queries=None) -> dict:
    return {"source": "Google Maps via SerpApi", "fetched_at": _now(), "status": status,
            "message": message, "queries": queries or [], "places": places or []}


def _num(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(int(str(value).replace(",", "").strip()), 0)
    except (TypeError, ValueError):
        return 0


def km_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def center_of(places: list[dict]) -> tuple[float, float] | None:
    """Median latitude/longitude: robust to a few far-away results."""
    pts = [(p["lat"], p["lng"]) for p in places if p.get("lat") is not None and p.get("lng") is not None]
    if not pts:
        return None
    lats, lngs = sorted(p[0] for p in pts), sorted(p[1] for p in pts)
    mid = len(pts) // 2
    return (lats[mid], lngs[mid])


def _queries(trip: Trip) -> list[tuple[str, str]]:
    place = trip.destination_search_place
    qs = [("sights", f"top tourist attractions in {place}")]
    if trip.nights >= LONG_TRIP_NIGHTS:
        qs.append(("sights", f"best things to do and neighborhoods to explore in {place}"))
    for interest in trip.interests[:MAX_INTEREST_QUERIES]:
        what = INTEREST_QUERIES.get(interest.strip().lower(), interest.strip())
        if what:
            qs.append((interest.strip().lower(), f"{what} in {place}"))
    return qs


def _parse(item: dict, category: str) -> dict | None:
    if not isinstance(item, dict):
        return None
    name = item.get("title")
    gps = item.get("gps_coordinates") or {}
    lat, lng = _num(gps.get("latitude")), _num(gps.get("longitude"))
    if not isinstance(name, str) or not name.strip() or lat is None or lng is None:
        return None
    kind = item.get("type") if isinstance(item.get("type"), str) else ""
    types = [t for t in (item.get("types") or []) if isinstance(t, str)]
    if EXCLUDED_TYPES.search(" ".join([kind] + types)):
        return None
    if "permanently closed" in str(item.get("open_state") or "").lower():
        return None
    hours = item.get("operating_hours")
    return {
        "name": name.strip(),
        "category": category,
        "type": kind or (types[0] if types else ""),
        "rating": _num(item.get("rating")),
        "reviews": _int(item.get("reviews")),
        "address": item.get("address") if isinstance(item.get("address"), str) else "",
        "lat": lat, "lng": lng,
        "hours": {k.lower(): v for k, v in hours.items() if isinstance(v, str)} if isinstance(hours, dict) else None,
        "website": item.get("website") if isinstance(item.get("website"), str) else None,
        "place_id": item.get("place_id") if isinstance(item.get("place_id"), str) else None,
    }


def _quality(places: list[dict], rating: float, reviews: int) -> list[dict]:
    return [p for p in places if (p["rating"] or 0) >= rating and p["reviews"] >= reviews]


def search_places(trip: Trip, session=None) -> dict:
    """Search Google Maps for attractions in the destination. Never raises."""
    key = os.getenv("SERPAPI_API_KEY")
    if not key:
        return _result("error", "Attraction search is not configured (SERPAPI_API_KEY missing).")

    found, queries, errors = [], [], []
    for category, q in _queries(trip):
        params = {"engine": "google_maps", "type": "search", "q": q, "hl": "en", "gl": "us", "api_key": key}
        queries.append(q)
        try:
            response = (session or requests).get(SERPAPI_URL, params=params, timeout=TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Unexpected response format from SerpApi.")
            if payload.get("error"):
                raise ValueError(f"SerpApi error: {payload['error']}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{q}: {exc}")
            continue
        for item in payload.get("local_results") or []:
            place = _parse(item, category)
            if place:
                found.append(place)

    if not found:
        msg = "No attractions were returned." + (f" Errors: {'; '.join(errors)}" if errors else "")
        return _result("error" if errors else "empty", msg, queries=queries)

    # De-duplicate (same place from two queries): by place_id, else by name.
    unique, seen = [], set()
    for p in found:
        key_ = p["place_id"] or re.sub(r"\W+", "", p["name"].lower())
        if key_ in seen:
            continue
        seen.add(key_)
        unique.append(p)

    # Drop results far from where most results are (wrong city with the same name).
    center = center_of(unique)
    if center:
        unique = [p for p in unique if km_between(p["lat"], p["lng"], *center) <= MAX_KM_FROM_CENTER]

    good = _quality(unique, MIN_RATING, MIN_REVIEWS)
    relaxed = False
    if len(good) < MIN_PLACES_BEFORE_RELAXING:
        good, relaxed = _quality(unique, RELAXED_RATING, RELAXED_REVIEWS), True

    if not good:
        return _result("empty", f"{len(unique)} places were found, but none had enough reviews and a good rating.",
                       queries=queries)

    # Keep a mix: best-reviewed first, but make sure each interest category is represented.
    good.sort(key=lambda p: (-(p["rating"] or 0) * math.log10(p["reviews"] + 10)))
    picked, by_cat = [], {}
    for p in good:
        by_cat.setdefault(p["category"], []).append(p)
    for cat_places in by_cat.values():            # top 3 of every category first
        picked.extend(cat_places[:3])
    for p in good:
        if len(picked) >= MAX_PLACES:
            break
        if p not in picked:
            picked.append(p)
    picked = picked[:MAX_PLACES]
    for i, p in enumerate(picked, 1):
        p["id"] = f"P{i}"

    result = _result("ok", places=picked, queries=queries)
    result["center"] = list(center_of(picked) or ())
    result["relaxed_quality_bar"] = relaxed
    result["errors"] = errors
    return result
