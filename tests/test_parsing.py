from datetime import date

from conftest import TODAY
from core.models import TripRequest
from core.trip_parser import merge_requests, validate_request
from tools.airports import normalize_state, resolve_airports


# ---------- airports: U.S. domestic only ----------

def test_major_city():
    r = resolve_airports("Atlanta")
    assert r.codes == ["ATL"] and r.state == "Georgia"

def test_same_name_in_several_states_asks():
    r = resolve_airports("Portland")
    assert r.codes == [] and "Oregon" in r.question and "Maine" in r.question

def test_state_picks_the_right_city():
    assert resolve_airports("Portland", "ME").codes == ["PWM"]
    assert resolve_airports("Portland", "Oregon").codes == ["PDX"]
    assert resolve_airports("Columbus", "OH").codes == ["CMH"]

def test_non_curated_city_found_in_database():
    r = resolve_airports("Bozeman")
    assert r.codes == ["BZN"] and r.state == "Montana"

def test_foreign_city_rejected_with_clear_message():
    for place in ("Tokyo", "London", "Dhaka", "Cancun"):
        r = resolve_airports(place)
        assert r.codes == [] and "United States only" in r.question, place

def test_foreign_country_rejected():
    assert "outside the U.S." in resolve_airports("Kyoto", country="Japan").question

def test_us_namesake_of_foreign_city_works_with_state():
    assert resolve_airports("Paris", "Texas").codes == ["PRX"]

def test_territories_not_supported():
    assert "territories" in resolve_airports("San Juan").question
    assert resolve_airports("SJU").codes == []          # Puerto Rico airport code

def test_hawaii_and_alaska_included():
    assert resolve_airports("Honolulu").codes == ["HNL"]
    assert resolve_airports("Anchorage").codes == ["ANC"]

def test_airport_code_typed_directly():
    assert resolve_airports("jfk").codes == ["JFK"]

def test_empty_city_asks():
    r = resolve_airports("")
    assert r.codes == [] and r.question

def test_country_word_asks_for_city():
    assert resolve_airports("USA").question

def test_normalize_state():
    assert normalize_state("ga") == "Georgia" and normalize_state("DC") == "District of Columbia"
    assert normalize_state("Puerto Rico") is None and normalize_state("Narnia") is None


# ---------- validation ----------

def base(**kw):
    data = dict(origin_city="Atlanta", destination_city="San Francisco",
                departure_date="2026-12-10", return_date="2026-12-17")
    data.update(kw)
    return TripRequest(**data)

def test_valid_round_trip():
    trip, q = validate_request(base(adults=2), TODAY)
    assert q == [] and trip.nights == 7 and trip.is_round_trip and trip.adults == 2
    assert trip.destination_state == "California"

def test_international_trip_rejected():
    trip, q = validate_request(base(destination_city="Tokyo"), TODAY)
    assert trip is None and "United States only" in q[0]

def test_past_date_rejected():
    trip, q = validate_request(base(departure_date="2026-01-05"), TODAY)
    assert trip is None and "past" in q[0]

def test_return_before_departure_rejected():
    trip, q = validate_request(base(return_date="2026-12-01"), TODAY)
    assert trip is None and any("not after" in x for x in q)

def test_missing_return_asks():
    trip, q = validate_request(base(return_date=None), TODAY)
    assert trip is None and any("come back" in x for x in q)

def test_nights_sets_return_date():
    trip, q = validate_request(base(return_date=None, nights=4), TODAY)
    assert trip.return_date == date(2026, 12, 14)

def test_default_adult_is_stated_not_silent():
    trip, _ = validate_request(base(), TODAY)
    assert trip.adults == 1 and any("1 adult" in a for a in trip.assumptions)

def test_merge_keeps_old_values():
    merged = merge_requests(base().model_dump(), TripRequest(adults=2))
    assert merged.destination_city == "San Francisco" and merged.adults == 2
