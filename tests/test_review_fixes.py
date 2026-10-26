"""Regression tests for issues found in the external code review (Oct 2026).

Each test reproduces one reported problem. They were written to FAIL on the
old code first, then the code was fixed until they pass.
"""
import copy

from conftest import HOTELS as HOTELS_FIXTURE, OUTBOUND, TODAY, FakeSession
from core import render
from core.budget import compute_budget, compute_stay, pick_flight
from core.models import TripRequest
from core.trip_parser import merge_requests, validate_request
from core.verifier import verify_draft
from tools.flight_tool import search_flights
from tools.hotel_tool import search_hotels


def trip_of(**kw):
    data = dict(origin_city="Atlanta", destination_city="San Francisco",
                departure_date="2026-12-10", return_date="2026-12-17", adults=2, budget_usd=4000)
    data.update(kw)
    trip, q = validate_request(TripRequest(**data), TODAY)
    assert not q, q
    return trip


# --- Issue A: unconfirmed round trips must not be budgeted ------------------

def test_round_trip_without_confirmed_return_is_not_budgeted():
    trip = trip_of()
    flights = search_flights(trip, session=FakeSession(returns={}))     # every return lookup comes back empty
    assert flights["offers"] and all(o["return_status"] != "found" for o in flights["offers"])
    assert pick_flight(flights) is None
    budget = compute_budget(trip, flights, {"offers": []})
    assert budget["flight_usd"] is None and "flights" in budget["missing"]


# --- Issue B: changing destination must drop the old state ------------------

def test_destination_change_drops_old_state():
    old = TripRequest(origin_city="Atlanta", destination_city="Portland", destination_state="Oregon",
                      departure_date="2026-12-10", return_date="2026-12-17").model_dump()
    # The LLM copies the old state forward, as told to "keep earlier values".
    new = TripRequest(**{**old, "destination_city": "Boston"})
    merged = merge_requests(old, new)
    trip, q = validate_request(merged, TODAY)
    assert q == [] and trip.destination_airports == ["BOS"] and merged.destination_state is None
    assert trip.destination_state == "Massachusetts"


def test_destination_change_keeps_newly_stated_state():
    old = TripRequest(destination_city="Boston", destination_state="Massachusetts").model_dump()
    merged = merge_requests(old, TripRequest(destination_city="Portland", destination_state="Maine"))
    assert merged.destination_state == "Maine"


# --- One-way trips -------------------------------------------------------

def test_one_way_without_nights_asks_about_hotel_not_return():
    _, q = validate_request(TripRequest(origin_city="Atlanta", destination_city="Chicago",
                                        departure_date="2026-12-10", one_way=True), TODAY)
    assert q and "come back" not in q[0] and "nights" in q[0]


def test_one_way_with_zero_nights_skips_hotel():
    trip = trip_of(return_date=None, one_way=True, nights=0)
    assert trip.return_date is None and trip.nights == 0
    assert search_hotels(trip, session=FakeSession())["status"] == "skipped"


# --- Issue C: verifier coverage ------------------------------------------

def _act(text, sid=None):
    return {"time_of_day": "Morning", "description": text, "source_id": sid}


def test_verifier_flags_missing_and_duplicate_days():
    draft = {"days": [{"day_number": 1, "activities": [_act("Arrive.")]},
                      {"day_number": 1, "activities": [_act("Walk.")]}], "tips": []}
    result = verify_draft(draft, 3, set(), set(), {"offers": []})
    assert not result["ok"]
    assert any("duplicate" in p for p in result["problems"])
    assert any("missing" in p for p in result["problems"])
    # Duplicate day's activities are merged, not silently dropped.
    assert len(result["clean_draft"]["days"][0]["activities"]) == 2


def test_verifier_flags_opening_hours_claims():
    draft = {"days": [{"day_number": 1, "activities": [_act("The museum is open every Monday.")]}], "tips": []}
    result = verify_draft(draft, 1, set(), set(), {"offers": []})
    assert not result["ok"] and any("opening" in p for p in result["problems"])


def test_checks_wording_is_specific():
    text = render.checks_section({"removed": 0, "problems": []})
    assert "passed verification" not in text and "not checked" in text.lower()


# --- Issue D: date and budget feasibility --------------------------------

def test_late_night_arrival_books_previous_night():
    trip = trip_of()
    flight = {"outbound_legs": [{"arrives": "2026-12-11 01:30"}], "return_legs": []}   # red-eye
    stay = compute_stay(trip, flight)
    assert stay["check_in"] == "2026-12-10"
    assert any("01:30" in n for n in stay["notes"])


def test_over_budget_suggests_cheapest_real_combination():
    trip = trip_of(budget_usd=1500)
    flights = search_flights(trip, session=FakeSession())
    hotels = search_hotels(trip, session=FakeSession())
    b = compute_budget(trip, flights, hotels)
    assert b["over_budget"]
    alt = b["cheapest_option"]
    assert alt["flight_id"] and alt["hotel_id"] and alt["total_usd"] < b["subtotal_usd"]
    assert "cheapest" in render.budget_section(b).lower()


# --- Price labels -----------------------------------------------------------

def test_flight_price_basis_is_shown_as_unverified():
    trip = trip_of()
    text = render.flights_section(search_flights(trip, session=FakeSession()), "F1", trip)
    assert "not yet verified" in text and "2 traveller" in text


def test_hotel_estimated_total_is_marked():
    trip = trip_of()
    hotels = search_hotels(trip, session=FakeSession())
    text = render.hotels_section(hotels, "H1")
    assert "est." in text and "taxes" in text.lower()


def test_hours_check_does_not_flag_harmless_phrases():
    from core.verifier import HOURS_CLAIM
    for ok in ["Browse the open-air market on Sunday.", "Spend two hours on the beach.",
               "Settle in near your hotel.", "Visit Senso-ji, Tokyo's oldest temple."]:
        assert not HOURS_CLAIM.search(ok), ok
    for bad in ["The museum is open every Monday.", "It opens at 9am.", "Closed on Tuesdays.",
                "The market is open daily."]:
        assert HOURS_CLAIM.search(bad), bad



# --- U.S. domestic scope ------------------------------------------------------

def test_short_trip_mentions_driving():
    trip, q = validate_request(TripRequest(origin_city="Atlanta", destination_city="Charlotte",
                                           departure_date="2026-12-10", return_date="2026-12-12"), TODAY)
    assert not q
    text = render.trip_section(trip)
    assert "Short trip" in text and "Driving" in text


def test_long_trip_has_no_driving_note():
    assert "Short trip" not in render.trip_section(trip_of())


def test_us_hotel_fee_warning():
    text = render.hotels_section(search_hotels(trip_of(), session=FakeSession()), "H1")
    assert "resort fees" in text


# --- Found in the first real run (Atlanta -> New York, Oct 26 2026) ----------

from core.schedule import free_slots


def test_early_departure_blocks_last_day():
    # Real case: return flight at 06:00, the LLM planned a morning stroll.
    assert free_slots(3, 3, "2026-12-11 00:29", "2026-12-13 06:00") == []
    assert free_slots(3, 3, None, "2026-12-13 19:00") == ["Morning"]
    assert free_slots(3, 3, None, "2026-12-13 23:30") == ["Morning", "Afternoon"]


def test_arrival_time_blocks_early_slots():
    assert free_slots(1, 3, "2026-12-10 15:00", None, "2026-12-10") == ["Evening"]
    assert free_slots(1, 3, "2026-12-11 00:29", None, "2026-12-11") == ["Morning", "Afternoon", "Evening"]


def test_verifier_removes_activity_during_flight():
    draft = {"days": [{"day_number": 1, "activities": [_act("Walk around.")]},
                      {"day_number": 2, "activities": [_act("Stroll in the park before the airport.")]}],
             "tips": []}
    result = verify_draft(draft, 2, set(), set(), {"offers": []}, free_slots={1: ["Morning"], 2: []})
    assert any("clashes with your flight" in p for p in result["problems"])
    assert result["clean_draft"]["days"][1]["activities"] == []


def test_travel_day_is_labelled():
    text = render.itinerary_section("2026-12-11", {"days": [], "tips": []}, 2, [], None, "2026-12-12 06:00",
                                    {1: ["Morning"], 2: []})
    assert "Travel day" in text


def test_hostel_is_not_picked_for_budget():
    trip = trip_of()
    hostels = dict(HOTELS_FIXTURE)
    hostels["properties"] = [
        {"type": "hotel", "name": "Cheap Downtown Hostel", "overall_rating": 4.5, "reviews": 100,
         "rate_per_night": {"extracted_lowest": 40}, "total_rate": {"extracted_lowest": 280}},
        {"type": "hotel", "name": "Real Hotel", "overall_rating": 4.1, "reviews": 100,
         "rate_per_night": {"extracted_lowest": 150}, "total_rate": {"extracted_lowest": 1050}},
    ]
    result = search_hotels(trip, session=FakeSession(hotels=hostels))
    assert result["offers"][0]["name"] == "Real Hotel"
    assert "hostel" in render.hotels_section(result, "H1")
    b = compute_budget(trip, {"offers": []}, result)
    assert b["hotel_id"] == "H1" and b["hotel_usd"] == 1050


def test_flight_cell_uses_line_breaks():
    trip = trip_of()
    text = render.flights_section(search_flights(trip, session=FakeSession()), "F1", trip)
    assert "<br>dep " in text and "<br>arr " in text


# --- Found in the second real run (Atlanta -> Miami, Oct 26 2026) -----------

from conftest import FakeTavily
from tools.tavily_tool import destination_names, search_activities


def test_unrelated_sources_are_dropped():
    result = search_activities(trip_of(), client=FakeTavily())
    titles = [s["title"] for s in result["sources"]]
    assert "Billboard Hot 100" not in titles and len(titles) == 3
    assert result["dropped_unrelated"] == 1


def test_destination_name_variants():
    assert {"new york", "nyc", "new-york"} <= destination_names("New York City")


def test_vacation_rental_ranked_after_hotels_and_labelled():
    trip = trip_of()
    data = {"properties": [
        {"type": "vacation rental", "name": "Unit 2 - Private Queen Studio Near Airport", "overall_rating": 4.0,
         "rate_per_night": {"extracted_lowest": 88}, "total_rate": {"extracted_lowest": 616}},
        {"type": "hotel", "name": "Design District suite with free parking", "overall_rating": 4.3,
         "rate_per_night": {"extracted_lowest": 96}, "total_rate": {"extracted_lowest": 672}},
        {"type": "hotel", "name": "Hilton Garden Inn", "overall_rating": 4.1,
         "rate_per_night": {"extracted_lowest": 106}, "total_rate": {"extracted_lowest": 742}},
    ]}
    result = search_hotels(trip, session=FakeSession(hotels=data))
    assert [h["name"] for h in result["offers"]][0] == "Hilton Garden Inn"
    assert sum(h["is_rental"] for h in result["offers"]) == 2
    assert "vacation rental" in render.hotels_section(result, "H1")


def test_other_city_airport_is_named():
    from core.render import _airport
    assert _airport("FLL", "Miami") == "FLL (Fort Lauderdale)"
    assert _airport("MIA", "Miami") == "MIA"


def test_basic_economy_note_and_singular_night():
    trip = trip_of()
    assert "basic economy" in render.flights_section(search_flights(trip, session=FakeSession()), "F1", trip)
    assert "(1 night)" in render.trip_section(trip, {"check_in": "2026-11-09", "check_out": "2026-11-10",
                                                     "nights": 1, "notes": []})
