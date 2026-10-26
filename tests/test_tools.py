from conftest import TODAY, FakeResponse, FakeSession, FakeTavily
from core.budget import compute_budget
from core.models import TripRequest
from core.trip_parser import validate_request
from core.verifier import check_text
from tools.flight_tool import search_flights
from tools.hotel_tool import search_hotels
from tools.tavily_tool import search_activities


def sf_trip(**kw):
    data = dict(origin_city="Atlanta", destination_city="San Francisco",
                departure_date="2026-12-10", return_date="2026-12-17", adults=2, budget_usd=4000)
    data.update(kw)
    trip, q = validate_request(TripRequest(**data), TODAY)
    assert not q, q
    return trip


def test_flights_filter_and_round_trip_prices():
    session = FakeSession()
    result = search_flights(sf_trip(), session=session)
    assert result["status"] == "ok"
    ids = {o["id"]: o for o in result["offers"]}
    # LAX (not a San Francisco airport) and the unpriced offer are filtered out -> 2 offers.
    assert len(ids) == 2
    cheapest = result["offers"][0]
    assert cheapest["return_status"] == "found"
    assert cheapest["price_usd"] == 470                     # price after choosing the return flight
    assert cheapest["return_legs"][-1]["to"] == "ATL"
    assert any("departure_token" in c for c in session.calls)
    assert all("api_key" not in result["query"] for _ in [0])


def test_flights_nonstop_filter():
    result = search_flights(sf_trip(nonstop_only=True), session=FakeSession())
    assert [o["outbound_stops"] for o in result["offers"]] == [0]


def test_flights_http_error_is_reported_not_raised():
    class Broken(FakeSession):
        def get(self, *a, **k):
            return FakeResponse({}, status=500)
    result = search_flights(sf_trip(), session=Broken())
    assert result["status"] == "error" and "failed" in result["message"]


def test_flights_missing_key(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY")
    assert search_flights(sf_trip(), session=FakeSession())["status"] == "error"


def test_hotels_prices_and_ranking():
    result = search_hotels(sf_trip(), session=FakeSession())
    names = [h["name"] for h in result["offers"]]
    assert "Mystery Hotel" not in names                      # no price -> dropped
    assert names[0] == "Marina Inn"                   # 4.0+ rated, cheapest total
    marina = result["offers"][0]
    assert marina["total_usd"] == 180 * 7 and "nightly rate x 7" in marina["total_basis"]
    assert names[-1] == "Budget Inn SF"                   # rated below 4.0 -> last


def test_activities_have_ids_and_urls():
    result = search_activities(sf_trip(), client=FakeTavily())
    assert [s["id"] for s in result["sources"]] == ["S1", "S2", "S3"]


def test_budget_math():
    trip = sf_trip()
    b = compute_budget(trip, search_flights(trip, session=FakeSession()), search_hotels(trip, session=FakeSession()))
    assert b["flight_usd"] == 470 and b["hotel_usd"] == 1260
    assert b["subtotal_usd"] == 1730 and b["remaining_usd"] == 2270
    assert b["remaining_per_day_usd"] == round(2270 / 7, 2) and b["over_budget"] is False


def test_verifier_catches_made_up_facts():
    allowed = {470.0, 1260.0}
    assert check_text("Dinner at the wharf for $300.", allowed, {"S1"}, {"DL1234"}, {"DL"})
    assert check_text("Walk the bridge [S9].", allowed, {"S1"}, set(), set())
    assert check_text("Take DL 999 home.", allowed, set(), {"DL1234"}, {"DL"})
    assert check_text("Walk the Golden Gate Bridge [S1]. Your hotel costs $1,260 total.", allowed, {"S1"}, set(), set()) == []
