"""Real hotel data from Google Hotels via SerpApi.

Searches hotels using:
- Destination
- Exact check-in and check-out dates
- Number of adults and children
- User hotel preferences

Improvements:
- Safer price parsing
- Rating and review-count confidence
- Price-aware hotel ranking
- Separate treatment of hotels, rentals and hostels
- Clear distinction between reported and estimated totals
- Preserves existing backend-compatible response fields

Important:
Hotel location, attraction distance, taxes, and actual
booking availability are not independently verified here.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

import requests

from core.models import Trip


SERPAPI_URL = "https://serpapi.com/search.json"

TIMEOUT_SECONDS = 35

RENTAL_WORDS = re.compile(
    r"\b("
    r"unit \d|studio|apartment|apt\b|condo|airbnb|"
    r"room in|entire|suite with|guest ?house|cottage|villa"
    r")",
    re.I,
)

# Ranking configuration.
# These values are heuristics, not verified quality scores.
BASELINE_RATING = 4.0
QUALITY_PRIORITY_RATING = 4.0  # Rank 4.0+ properties before lower-rated alternatives.
CONFIDENCE_REVIEWS = 100
# A rating from very few reviews is not trustworthy (a real run picked
# "stay and relax": 5.0 from 1 review). Below this count a property is "unrated".
MIN_REVIEWS_FOR_RATING = 20
# Hotels within this straight-line distance of the main sights count as "near".
NEAR_SIGHTS_KM = 8.0
PRICE_WEIGHT = 0.30
RATING_WEIGHT = 0.70


def _result(
    status: str,
    message: str = "",
    offers=None,
    query=None,
) -> dict:
    """Return the existing backend-compatible result structure."""

    return {
        "source": "Google Hotels via SerpApi",
        "fetched_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "status": status,
        "message": message,
        "query": query or {},
        "offers": offers or [],
    }


def _number(value):
    """Convert a positive numeric value into a float."""

    if isinstance(value, bool):
        return None

    try:
        if isinstance(value, str):
            value = value.replace(",", "").replace("$", "").strip()

        number = float(value)

        if number > 0 and number != float("inf"):
            return number

    except (TypeError, ValueError, OverflowError):
        pass

    return None


def _number_or_none(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _review_count(value) -> int:
    """Convert a review count into a non-negative integer."""

    if isinstance(value, bool):
        return 0

    try:
        count = int(str(value).replace(",", "").strip())
        return max(count, 0)
    except (TypeError, ValueError):
        return 0


def _adjusted_rating(hotel: dict) -> float:
    """Reduce overconfidence in ratings with very few reviews."""

    rating = _number(hotel.get("rating"))
    reviews = _review_count(hotel.get("reviews"))

    if rating is None or not 0 < rating <= 5:
        return 0.0

    return (
        reviews * rating
        + CONFIDENCE_REVIEWS * BASELINE_RATING
    ) / (reviews + CONFIDENCE_REVIEWS)


def _hotel_score(
    hotel: dict,
    lowest_price: float,
) -> float:
    """Combine rating confidence and relative price.

    Higher score is better.

    Price score compares properties within the same search
    results. It does not estimate transportation expenses.
    """

    adjusted_rating = _adjusted_rating(hotel)

    rating_score = adjusted_rating / 5.0

    total = hotel["total_usd"]

    price_score = (
        lowest_price / total
        if total > 0
        else 0.0
    )

    return (
        RATING_WEIGHT * rating_score
        + PRICE_WEIGHT * price_score
    )


def trusted_rating(hotel: dict) -> float | None:
    """The rating, but only if enough people reviewed the property."""
    rating = _number(hotel.get("rating"))
    if rating is None or _review_count(hotel.get("reviews")) < MIN_REVIEWS_FOR_RATING:
        return None
    return rating


def _km(lat1, lon1, lat2, lon2) -> float:
    import math
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


def search_hotels(
    trip: Trip,
    limit: int = 5,
    session=None,
    check_in=None,
    check_out=None,
    sights_center: tuple[float, float] | None = None,
) -> dict:
    """Search and rank real hotel results from Google Hotels.

    Returns status='error' for API and parsing failures,
    rather than raising exceptions.
    """

    # --------------------------------------------------
    # STEP 1: Determine stay dates
    # --------------------------------------------------

    check_in = check_in or trip.departure_date
    check_out = check_out or trip.check_out_date

    nights = (check_out - check_in).days

    if nights < 1:
        return _result(
            "skipped",
            "No overnight stay in this trip.",
        )

    # --------------------------------------------------
    # STEP 2: Read SerpApi key
    # --------------------------------------------------

    key = os.getenv("SERPAPI_API_KEY")

    if not key:
        return _result(
            "error",
            "Hotel search is not configured "
            "(SERPAPI_API_KEY missing).",
        )

    # --------------------------------------------------
    # STEP 3: Build destination
    # --------------------------------------------------

    # Search by the real city: "JFK" would return airport hotels only.
    place = trip.destination_search_place

    # --------------------------------------------------
    # STEP 4: Build Google Hotels request
    # --------------------------------------------------

    params = {
        "engine": "google_hotels",
        "q": f"hotels in {place}",
        "check_in_date": check_in.isoformat(),
        "check_out_date": check_out.isoformat(),
        "adults": trip.adults,
        "children": trip.children,
        "currency": "USD",
        "gl": "us",
        "hl": "en",
        "api_key": key,
    }

    # Never expose API key in returned query.
    public_query = {
        k: v
        for k, v in params.items()
        if k != "api_key"
    }

    # --------------------------------------------------
    # STEP 5: Call SerpApi
    # --------------------------------------------------

    try:
        response = (session or requests).get(
            SERPAPI_URL,
            params=params,
            timeout=TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError(
                "Unexpected response format from SerpApi."
            )

        if payload.get("error"):
            raise ValueError(
                f"SerpApi error: {payload['error']}"
            )

    except (requests.RequestException, ValueError) as exc:
        return _result(
            "error",
            f"Hotel search failed: {exc}",
            query=public_query,
        )

    # --------------------------------------------------
    # STEP 6: Extract priced hotel offers
    # --------------------------------------------------

    offers = []

    for prop in payload.get("properties") or []:

        if not isinstance(prop, dict):
            continue

        nightly = _number(
            (prop.get("rate_per_night") or {}).get(
                "extracted_lowest"
            )
        )

        total = _number(
            (prop.get("total_rate") or {}).get(
                "extracted_lowest"
            )
        )

        # Skip properties without usable prices.
        if nightly is None and total is None:
            continue

        if total is not None:
            total_basis = (
                "total for stay, from Google Hotels"
            )
        else:
            total = round(nightly * nights, 2)
            total_basis = (
                f"nightly rate x {nights} nights "
                "(taxes may be extra)"
            )

        name = prop.get("name") or "Unnamed property"
        kind = prop.get("type") or ""

        name_lower = name.lower()
        kind_lower = kind.lower()

        is_hostel = (
            "hostel" in name_lower
            or "hostel" in kind_lower
        )

        is_rental = (
            not is_hostel
            and (
                "rental" in kind_lower
                or bool(RENTAL_WORDS.search(name))
            )
        )

        gps = prop.get("gps_coordinates") or {}
        lat, lng = _number_or_none(gps.get("latitude")), _number_or_none(gps.get("longitude"))
        offers.append(
            {
                "lat": lat,
                "lng": lng,
                "name": name,
                "type": kind,
                "nightly_usd": nightly,
                "total_usd": total,
                "total_basis": total_basis,
                "rating": prop.get("overall_rating"),
                "reviews": prop.get("reviews"),
                "hotel_class": prop.get(
                    "extracted_hotel_class"
                ),
                "link": prop.get("link"),
                "sponsored": bool(
                    prop.get("sponsored")
                ),
                "is_hostel": is_hostel,
                "is_rental": is_rental,
            }
        )

    # --------------------------------------------------
    # STEP 7: Handle no priced results
    # --------------------------------------------------

    if not offers:
        return _result(
            "empty",
            "Google Hotels returned no priced "
            "properties for these dates.",
            query=public_query,
        )

    # --------------------------------------------------
    # STEP 8: Apply user preferences
    # --------------------------------------------------

    before = len(offers)

    if not trip.allow_rentals:
        offers = [
            h for h in offers
            if not h["is_rental"]
        ]

    if not trip.allow_hostels:
        offers = [
            h for h in offers
            if not h["is_hostel"]
        ]

    if trip.min_hotel_rating:
        # Uses the trusted rating: 5.0 from 1 review does not pass "4.0+".
        offers = [
            h for h in offers
            if (trusted_rating(h) or 0) >= trip.min_hotel_rating
        ]

    if not offers:
        return _result(
            "empty",
            (
                f"None of the {before} priced properties "
                "matched your hotel preferences "
                "(rating, rentals, hostels). "
                "Try relaxing them."
            ),
            query=public_query,
        )

    # --------------------------------------------------
    # STEP 9: Calculate ranking scores
    # --------------------------------------------------

    lowest_price = min(
        h["total_usd"]
        for h in offers
    )

    for hotel in offers:

        hotel["adjusted_rating"] = round(
            _adjusted_rating(hotel),
            3,
        )

        hotel["ranking_score"] = round(
            _hotel_score(
                hotel,
                lowest_price,
            ),
            4,
        )

    # --------------------------------------------------
    # STEP 10: Rank hotel results
    # --------------------------------------------------

    # Preserve the existing preference for standard
    # hotels over rentals and hostels.
    #
    # Within each property category:
    # - Prefer 4.0+ rated properties, without excluding cheaper alternatives
    # - Among those in the same quality tier, prioritize lower total price
    # - Use review-adjusted rating and the existing score as tie-breakers
    #
    # Location is not included because verified
    # distance information is unavailable.

    # Distance to the main sights (straight line), when both positions are known.
    for hotel in offers:
        hotel["trusted_rating"] = trusted_rating(hotel)
        hotel["sights_km"] = (
            round(_km(hotel["lat"], hotel["lng"], *sights_center), 1)
            if sights_center and hotel["lat"] is not None and hotel["lng"] is not None
            else None
        )

    def location_tier(h):
        if h["sights_km"] is None:
            return 1                     # unknown
        return 0 if h["sights_km"] <= NEAR_SIGHTS_KM else 2

    offers.sort(
        key=lambda h: (
            h["is_hostel"],
            h["is_rental"],
            location_tier(h),
            -int((h["trusted_rating"] or 0) >= QUALITY_PRIORITY_RATING),
            h["total_usd"],
            -h["ranking_score"],
        )
    )

    # --------------------------------------------------
    # STEP 11: Limit and number results
    # --------------------------------------------------

    offers = offers[
        :max(1, min(limit, 10))
    ]

    for i, offer in enumerate(offers, 1):
        offer["id"] = f"H{i}"

    # --------------------------------------------------
    # STEP 12: Return results
    # --------------------------------------------------

    return {
        **_result(
            "ok",
            offers=offers,
            query=public_query,
        ),
        "nights": nights,
    }
