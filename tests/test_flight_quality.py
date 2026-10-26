"""Flight list quality: a nonstop is always shown if one exists; early/late times are flagged."""
import copy

from conftest import OUTBOUND, RETURNS, TODAY, FakeSession, leg
from core import render
from core.models import TripRequest
from core.trip_parser import validate_request
from tools.flight_tool import search_flights


def trip_of(**kw):
    data = dict(origin_city="Atlanta", destination_city="San Francisco",
                departure_date="2026-12-10", return_date="2026-12-17", adults=1)
    data.update(kw)
    return validate_request(TripRequest(**data), TODAY)[0]


def test_nonstop_is_kept_even_if_not_among_cheapest():
    outbound = copy.deepcopy(OUTBOUND)
    # Six cheap one-stop flights, then the (pricier) nonstop Delta from the fixture.
    for i in range(6):
        outbound["other_flights"].append(
            {"flights": [leg("United", f"UA {10 + i}", "ATL", "DEN", "2026-12-10 06:00", "2026-12-10 07:30", 210),
                         leg("United", f"UA {20 + i}", "DEN", "SFO", "2026-12-10 08:30", "2026-12-10 10:00", 150)],
             "total_duration": 480, "price": 200 + i, "departure_token": f"tok_u{i}"})
    result = search_flights(trip_of(), session=FakeSession(outbound=outbound), limit=5)
    stops = [o["outbound_stops"] for o in result["offers"]]
    assert len(stops) == 5 and 0 in stops
    nonstop = next(o for o in result["offers"] if o["outbound_stops"] == 0)
    assert nonstop["return_status"] == "found"          # its return was looked up, so it can be budgeted


def test_early_return_flight_warning():
    returns = copy.deepcopy(RETURNS)
    returns["tok_aa"]["best_flights"][0]["flights"][0]["departure_airport"]["time"] = "2026-12-17 05:20"
    trip = trip_of()
    flights = search_flights(trip, session=FakeSession(returns=returns))
    text = render.flights_section(flights, "F1", trip)
    assert "return flight leaves at 5:20 AM" in text and "2:35 AM" in text
    assert "more comfortable times" in text          # F2 (Delta) has no early flight


def test_no_warning_for_normal_times():
    trip = trip_of()
    flights = search_flights(trip, session=FakeSession())
    assert "Flight times to know about" not in render.flights_section(flights, "F1", trip)
