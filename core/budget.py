"""Budget calculations for VoyageAI.

All arithmetic is performed in Python, not by the LLM.

Flight and hotel costs come from external search results.
Food, activities, baggage, taxes not included in the quoted
rates, and local transportation are NOT independently priced.

Important:
A positive remaining budget does not guarantee that the
entire trip is affordable.
"""

from __future__ import annotations

from datetime import timedelta

from core.models import Trip
from tools.flight_tool import PRICE_IS_TOTAL_FOR_ALL_ADULTS


EARLY_ARRIVAL_HOUR = 6


def usable_flights(flights: dict) -> list[dict]:
    """Return complete flight offers with usable prices."""

    return [
        offer
        for offer in flights.get("offers") or []
        if (
            offer.get("return_status")
            in ("found", "not applicable")
            and isinstance(
                offer.get("price_usd"),
                (int, float),
            )
            and offer["price_usd"] > 0
        )
    ]


def pick_flight(flights: dict) -> dict | None:
    """Select the cheapest complete flight offer."""

    offers = usable_flights(flights)

    return (
        min(
            offers,
            key=lambda offer: offer["price_usd"],
        )
        if offers
        else None
    )


def _flight_cost(
    flight: dict | None,
    trip: Trip,
) -> float | None:
    """Calculate flight cost using the existing pricing flag."""

    if not flight:
        return None

    price = float(flight["price_usd"])

    if PRICE_IS_TOTAL_FOR_ALL_ADULTS:
        return round(price, 2)

    travellers = trip.adults + trip.children

    return round(price * travellers, 2)


def _hotel_offers(hotels: dict) -> list[dict]:
    """Return hotel offers with valid positive totals."""

    return [
        hotel
        for hotel in hotels.get("offers") or []
        if (
            isinstance(
                hotel.get("total_usd"),
                (int, float),
            )
            and hotel["total_usd"] > 0
        )
    ]


def pick_hotel(
    hotels: dict,
) -> dict | None:
    """Select the first hotel from the hotel tool's ranking."""

    offers = _hotel_offers(hotels)

    return offers[0] if offers else None


def _date_of(timestamp: str | None):
    """Extract the local date from a flight timestamp."""

    from datetime import date

    try:
        return date.fromisoformat(
            (timestamp or "")[:10]
        )
    except (TypeError, ValueError):
        return None


def compute_stay(
    trip: Trip,
    flight: dict | None,
) -> dict:
    """Calculate stay dates from the actual flight schedule."""

    check_in = trip.departure_date

    check_out = (
        trip.return_date
        or trip.check_out_date
    )

    notes = []
    first_day = None

    if flight:

        outbound = flight.get("outbound_legs") or []

        if outbound:
            arrives = outbound[-1].get("arrives") or ""

            landed = _date_of(arrives)

            hour = (
                int(arrives[11:13])
                if (
                    len(arrives) >= 13
                    and arrives[11:13].isdigit()
                )
                else None
            )

            if (
                landed
                and hour is not None
                and hour < EARLY_ARRIVAL_HOUR
            ):
                first_day = landed

                night_before = (
                    landed - timedelta(days=1)
                )

                notes.append(
                    f"Your flight lands at "
                    f"{arrives[11:16]} on "
                    f"{landed:%a %b %d}, so the hotel "
                    f"is booked from the night before "
                    f"({night_before:%a %b %d}) "
                    f"to have a room on arrival."
                )

                check_in = night_before

            elif landed and landed != check_in:

                notes.append(
                    f"Your flight lands on "
                    f"{landed:%a %b %d} "
                    f"(local time), so the hotel "
                    f"and Day 1 start on that date."
                )

                check_in = landed

        return_legs = flight.get("return_legs") or []

        if return_legs:

            leaves = _date_of(
                return_legs[0].get("departs")
            )

            if leaves and leaves != check_out:

                notes.append(
                    f"Your return flight leaves on "
                    f"{leaves:%a %b %d}, so the "
                    f"hotel checks out then."
                )

                check_out = leaves

    if check_out <= check_in:
        check_out = check_in

    first_day = first_day or check_in

    return {
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "nights": (check_out - check_in).days,
        "first_day": first_day.isoformat(),
        "num_days": max(
            (check_out - first_day).days + 1,
            1,
        ),
        "notes": notes,
    }


def compute_budget(
    trip: Trip,
    flights: dict,
    hotels: dict,
    nights: int | None = None,
) -> dict:
    """Calculate a transparent flight-and-hotel budget."""

    flight = pick_flight(flights)

    flight_cost = _flight_cost(
        flight,
        trip,
    )

    nights = (
        trip.nights
        if nights is None
        else nights
    )

    hotel_offers = _hotel_offers(hotels)

    selected_hotel = (
        hotel_offers[0]
        if hotel_offers
        else None
    )

    # --------------------------------------------------
    # STEP 1: Select the highest-ranked eligible hotel.
    # --------------------------------------------------
    # The hotel tool ranks standard hotels, quality, and price.
    # Do not silently switch to a lower-rated hotel solely to
    # make the partial flight + hotel subtotal fit the budget.
    # If the preferred combination exceeds the budget, report
    # it transparently and offer the cheapest returned alternative.
    #
    # Hotel preferences (including any explicit minimum rating)
    # have already been applied by search_hotels().
    #
    hotel = (
        selected_hotel
        if nights > 0
        else None
    )

    hotel_cost = (
        hotel["total_usd"]
        if hotel
        else None
    )

    # --------------------------------------------------
    # STEP 2: Calculate known subtotal
    # --------------------------------------------------

    known_costs = [
        cost
        for cost in (
            flight_cost,
            hotel_cost,
        )
        if cost is not None
    ]

    subtotal = (
        round(sum(known_costs), 2)
        if known_costs
        else None
    )

    missing = []

    if flight_cost is None:
        missing.append("flights")

    if hotel_cost is None and nights > 0:
        missing.append("hotel")

    # --------------------------------------------------
    # STEP 3: Build response
    # --------------------------------------------------

    budget = {
        "flight_id": (
            flight.get("id")
            if flight
            else None
        ),
        "hotel_id": (
            hotel.get("id")
            if hotel
            else None
        ),
        "flight_usd": flight_cost,
        "hotel_usd": hotel_cost,
        "subtotal_usd": subtotal,
        "missing": missing,
        "user_budget_usd": trip.budget_usd,
        "remaining_usd": None,
        "remaining_per_day_usd": None,
        "over_budget": None,
        "hotel_total_basis": (
            hotel.get("total_basis")
            if hotel
            else None
        ),
        "unpriced_expenses": [
            "food",
            "local transportation",
            "activities and admission fees",
            "baggage and optional flight fees",
            "hotel taxes or fees not included "
            "in the quoted total",
        ],
        "budget_is_complete": False,
    }

    # --------------------------------------------------
    # STEP 4: Calculate remaining budget
    # --------------------------------------------------

    if (
        trip.budget_usd is not None
        and subtotal is not None
    ):

        remaining = round(
            trip.budget_usd - subtotal,
            2,
        )

        budget["remaining_usd"] = remaining

        # Retain the existing nights-based calculation
        # for compatibility with the current UI.
        budget["remaining_per_day_usd"] = (
            round(
                remaining / max(nights, 1),
                2,
            )
            if remaining > 0
            else 0
        )

        budget["over_budget"] = (
            remaining < 0
        )

    # --------------------------------------------------
    # STEP 5: Cheapest available combination
    # --------------------------------------------------

    cheap_flight = pick_flight(flights)

    standard_hotels = [
        h
        for h in hotel_offers
        if (
            not h.get("is_hostel")
            and not h.get("is_rental")
        )
    ]

    non_hostels = [
        h
        for h in hotel_offers
        if not h.get("is_hostel")
    ]

    eligible_hotels = (
        standard_hotels
        or non_hostels
        or hotel_offers
    )

    cheapest_hotel = (
        min(
            eligible_hotels,
            key=lambda h: h["total_usd"],
        )
        if eligible_hotels
        else None
    )

    if (
        cheap_flight
        and (
            cheapest_hotel
            or nights == 0
        )
    ):

        cheapest_flight_cost = _flight_cost(
            cheap_flight,
            trip,
        )

        cheapest_total = round(
            cheapest_flight_cost
            + (
                cheapest_hotel["total_usd"]
                if cheapest_hotel and nights > 0
                else 0
            ),
            2,
        )

        budget["cheapest_option"] = {
            "flight_id": cheap_flight.get("id"),
            "hotel_id": (
                cheapest_hotel.get("id")
                if cheapest_hotel and nights > 0
                else None
            ),
            "total_usd": cheapest_total,
        }

    else:
        budget["cheapest_option"] = None

    return budget


def allowed_amounts(
    trip: Trip,
    flights: dict,
    hotels: dict,
    budget: dict,
) -> set[float]:
    """Return supported amounts for text verification."""

    values = set()

    for offer in flights.get("offers") or []:

        price = offer.get("price_usd")

        if isinstance(price, (int, float)):
            values.add(float(price))

    for hotel in hotels.get("offers") or []:

        for key in (
            "nightly_usd",
            "total_usd",
        ):

            value = hotel.get(key)

            if isinstance(value, (int, float)):
                values.add(float(value))

    for key in (
        "flight_usd",
        "hotel_usd",
        "subtotal_usd",
        "user_budget_usd",
        "remaining_usd",
        "remaining_per_day_usd",
    ):

        value = budget.get(key)

        if isinstance(value, (int, float)):
            values.add(float(abs(value)))

    cheapest = budget.get("cheapest_option")

    if cheapest:
        values.add(
            float(cheapest["total_usd"])
        )

    return values
