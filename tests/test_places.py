"""Attractions from Google Maps, opening hours, place-based planning and its checks."""
import json
import re
from datetime import date

from langgraph.checkpoint.memory import MemorySaver

import backend
from conftest import HOTELS, PLACES, TODAY, FakeLLM, FakeSession, FakeTavily
from core import render
from core.hours import parse_day, slot_status
from core.models import TripRequest
from core.places_plan import assign_zones, availability, day_spread_km
from core.trip_parser import validate_request
from core.verifier import verify_draft
from tools.hotel_tool import search_hotels
from tools.places_tool import search_places


def trip_of(**kw):
    data = dict(origin_city="Atlanta", destination_city="San Francisco", departure_date="2026-12-10",
                return_date="2026-12-17", adults=2, budget_usd=4000)
    data.update(kw)
    trip, q = validate_request(TripRequest(**data), TODAY)
    assert not q, q
    return trip


# ---------------- opening hours ----------------

def test_hours_formats():
    assert parse_day("9 AM–5 PM") == [(9.0, 17.0)]
    assert parse_day("11 AM–2:30 PM, 5–10 PM") == [(11.0, 14.5), (17.0, 22.0)]
    assert parse_day("10 AM–5 PM") == [(10.0, 17.0)]          # Google's narrow spaces
    assert parse_day("5 PM–2 AM") == [(17.0, 26.0)]                     # past midnight
    assert parse_day("12–9 PM") == [(12.0, 21.0)]
    assert parse_day("Open 24 hours") == [(0.0, 24.0)]
    assert parse_day("Closed") == []
    assert parse_day("Hours might differ") is None                       # unknown, never guessed
    assert parse_day(None) is None


def test_slot_status():
    museum = {"hours": {"monday": "Closed", "tuesday": "10 AM–5 PM", "friday": "10 AM–9 PM"}}
    assert slot_status(museum, date(2026, 12, 14), "Morning") == "closed"      # Monday
    assert slot_status(museum, date(2026, 12, 15), "Morning") == "open"
    assert slot_status(museum, date(2026, 12, 15), "Evening") == "closed"      # closes at 5
    assert slot_status(museum, date(2026, 12, 18), "Evening") == "open"        # Friday late
    assert slot_status(museum, date(2026, 12, 16), "Morning") == "unknown"     # Wednesday not listed
    assert slot_status({"hours": None}, date(2026, 12, 16), "Morning") == "unknown"


# ---------------- places tool ----------------

def test_places_are_real_and_filtered():
    result = search_places(trip_of(), session=FakeSession(places=PLACES))
    names = [p["name"] for p in result["places"]]
    assert result["status"] == "ok" and len(names) == 8
    for junk in ("Fairmont San Francisco", "Tiny Corner Gallery", "Griffith Observatory", "Old Closed Diner"):
        assert junk not in names, junk                       # hotel, 3 reviews, Los Angeles, closed
    assert all(re.fullmatch(r"P\d+", p["id"]) for p in result["places"])
    assert result["center"] and abs(result["center"][0] - 37.79) < 0.05


def test_places_search_uses_real_city_and_interests():
    session = FakeSession(places=PLACES)
    search_places(trip_of(interests=["Food", "Museums", "Beaches"]), session=session)
    qs = [c["q"] for c in session.calls if c["engine"] == "google_maps"]
    assert qs == ["top tourist attractions in San Francisco, CA",
                  "best things to do and neighborhoods to explore in San Francisco, CA",   # 7 nights: +1
                  "best local restaurants in San Francisco, CA",
                  "museums in San Francisco, CA"]          # max 2 interest searches (quota)
    short = FakeSession(places=PLACES)
    search_places(trip_of(return_date="2026-12-12"), session=short)    # 2 nights: no extra search
    assert len([c for c in short.calls if c["engine"] == "google_maps"]) == 1


def test_airport_code_destination_searches_the_city():
    trip = trip_of(destination_city="JFK")
    assert trip.destination_search_place == "New York, NY"
    session = FakeSession(places=PLACES)
    search_places(trip, session=session)
    search_hotels(trip, session=session)
    sent = [c["q"] for c in session.calls]
    assert "top tourist attractions in New York, NY" in sent
    assert "hotels in New York, NY" in sent and not any("JFK" in q for q in sent)


def test_places_missing_key_and_empty(monkeypatch):
    assert search_places(trip_of(), session=FakeSession(places={"local_results": []}))["status"] == "empty"
    monkeypatch.delenv("SERPAPI_API_KEY")
    assert search_places(trip_of(), session=FakeSession(places=PLACES))["status"] == "error"


# ---------------- planning helpers ----------------

def _places():
    return [dict(p) for p in search_places(trip_of(), session=FakeSession(places=PLACES))["places"]]


def test_zones_group_nearby_places():
    places = _places()
    assign_zones(places)
    zone = {p["name"]: p["zone"] for p in places}
    assert zone["Ferry Building Marketplace"] == zone["Exploratorium"] == zone["Tadich Grill"]   # waterfront
    assert zone["de Young Museum"] == zone["Golden Gate Park"]                                   # the park
    assert zone["Golden Gate Park"] != zone["Ferry Building Marketplace"]
    far = [p for p in places if p["name"] in ("Golden Gate Bridge", "Ferry Building Marketplace")]
    assert day_spread_km(far) > 7


def test_availability_respects_hours():
    places = _places()
    by_name = {p["name"]: p["id"] for p in places}
    avail = availability(places, "2026-12-10", {5: ["Morning", "Evening"]})     # day 5 = Monday Dec 14
    assert by_name["Exploratorium"] not in avail[5]["Morning"]                   # closed Mondays
    assert by_name["Golden Gate Park"] in avail[5]["Evening"]                    # open 24 hours
    assert by_name["Lombard Street"] in avail[5]["Morning"]                      # hours unknown -> allowed


# ---------------- verifier (places mode) ----------------

def _verify(acts_by_day, num_days=2, avail=None):
    places = {p["id"]: p for p in _places()}
    ids = {p["name"]: p["id"] for p in places.values()}
    draft = {"days": [{"day_number": d, "activities": a(ids)} for d, a in acts_by_day.items()], "tips": []}
    avail = avail or {d: {"Morning": list(places), "Afternoon": list(places), "Evening": list(places)}
                      for d in range(1, num_days + 1)}
    return verify_draft(draft, num_days, set(), set(), {"offers": []},
                        free_slots={d: ["Morning", "Afternoon", "Evening"] for d in range(1, num_days + 1)},
                        places=places, availability=avail), ids


def act(slot, pid, text="See it."):
    return {"time_of_day": slot, "place_id": pid, "description": text}


def test_verifier_rejects_invented_closed_duplicate_and_transit():
    result, ids = _verify({
        1: lambda i: [act("Morning", "P99"),                                           # not on the list
                      act("Afternoon", i["Coit Tower"], "Take the 39 bus up the hill."),  # transit directions
                      act("Evening", i["Coit Tower"], "Night views.")],                 # used twice
        2: lambda i: [act("Morning", i["Ferry Building Marketplace"], "Use a Clipper card."),  # card name
                      act("Afternoon", i["Golden Gate Park"])]})
    problems = " | ".join(result["problems"])
    assert "P99 is not in the place list" in problems
    assert "transit directions" in problems and "transit card" in problems
    assert result["removed"] == 3        # P99, bus directions, Clipper card ("used twice" never reached: first Coit removed)
    kept = [a["place_id"] for d in result["clean_draft"]["days"] for a in d["activities"]]
    assert ids["Golden Gate Park"] in kept and "P99" not in kept


def test_verifier_rejects_place_closed_in_that_slot():
    places = _places()
    ids = {p["name"]: p["id"] for p in places}
    avail = availability(places, "2026-12-10", {5: ["Morning"]})
    draft = {"days": [{"day_number": 5, "activities": [act("Morning", ids["Exploratorium"])]}], "tips": []}
    result = verify_draft(draft, 5, set(), set(), {"offers": []}, free_slots={5: ["Morning"]},
                          places={p["id"]: p for p in places}, availability=avail)
    assert any("not open in the Morning on day 5" in p for p in result["problems"])


def test_verifier_soft_checks_empty_slots_and_wide_days(monkeypatch):
    import core.places_plan
    monkeypatch.setattr(core.places_plan, "MAX_DAY_SPREAD_KM", 5.0)   # bridge <-> ferry building is ~7.9 km
    result, ids = _verify({1: lambda i: [act("Morning", i["Golden Gate Bridge"], "Walk the bridge."),
                                         act("Afternoon", i["Ferry Building Marketplace"], "Lunch at the market.")],
                           2: lambda i: []})
    problems = " | ".join(result["problems"])
    assert "Day 1 has no activities" not in problems           # day 1 has 2 activities: fine
    assert "Day 2 has no activities" in problems               # day 2 left empty while places are open
    assert result["removed"] == 0 and not result["ok"]        # soft: retry, nothing removed
    assert any("wide area" in w for w in result["warnings"])


# ---------------- hotels ----------------

def test_one_review_rating_is_not_trusted():
    data = {"properties": [
        {"type": "hotel", "name": "stay and relax", "overall_rating": 5, "reviews": 1,
         "rate_per_night": {"extracted_lowest": 140}, "total_rate": {"extracted_lowest": 420}},
        {"type": "hotel", "name": "Real Hotel", "overall_rating": 4.2, "reviews": 800,
         "rate_per_night": {"extracted_lowest": 160}, "total_rate": {"extracted_lowest": 480}}]}
    trip = trip_of(min_hotel_rating=4.0)
    result = search_hotels(trip, session=FakeSession(hotels=data))
    assert [h["name"] for h in result["offers"]] == ["Real Hotel"]      # 4.0+ filter uses trusted rating
    loose = search_hotels(trip_of(), session=FakeSession(hotels=data))
    assert loose["offers"][0]["name"] == "Real Hotel"                    # and it is not ranked first
    assert "few reviews" in render.hotels_section(loose, "H1")


def test_hotels_near_sights_preferred():
    center = (37.795, -122.405)                         # downtown San Francisco
    result = search_hotels(trip_of(), session=FakeSession(), sights_center=center)
    first = result["offers"][0]
    assert first["name"] == "Union Square Hotel" and first["sights_km"] < 2
    marina = next(h for h in result["offers"] if h["name"] == "Marina Inn")
    assert marina["sights_km"] > 15                      # by the airport: ranked lower despite being cheaper
    assert "To main sights" in render.hotels_section(result, first["id"])


# ---------------- end to end ----------------

def honest_planner(messages):
    """Fake LLM that behaves like a good planner: only listed places, only open slots, no repeats."""
    data = json.loads(messages[1].content)
    names = {p["id"]: p["name"] for p in data["places"]}
    used, days = set(), []
    for day_key, slots in data["options"].items():
        day = int(day_key.split()[1])
        acts = []
        for slot, ids in list(slots.items())[-2:]:          # at most 2 per day, like the prompt asks
            pid = next((i for i in ids if i not in used), None)
            if pid:
                used.add(pid)
                acts.append({"time_of_day": slot, "place_id": pid, "description": f"Spend time at {names[pid]}."})
        days.append({"day_number": day, "activities": acts})
    return {"days": days, "tips": ["Layers help: San Francisco evenings are cool."]}


def sloppy_planner(messages):
    """Fake LLM that breaks rules on the first try."""
    good = honest_planner(messages)
    good["days"][1]["activities"].append({"time_of_day": "Morning", "place_id": "P42",
                                          "description": "Visit the famous Invented Museum."})
    good["days"][1]["activities"][0]["description"] = "Take the N line to get there."
    return good


def run(planner_answers, interests=None):
    llm = FakeLLM({"TripRequest": [], "ItineraryDraft": planner_answers})
    graph = backend.build_graph(backend.Services(llm=llm, http_session=FakeSession(places=PLACES),
                                                 tavily_client=FakeTavily(), today=lambda: TODAY), MemorySaver())
    form = {"origin_city": "Atlanta", "destination_city": "San Francisco", "departure_date": "2026-12-10",
            "return_date": "2026-12-17", "adults": 2, "interests": interests or []}
    return backend.run_travel_agent("form", "t-places", graph=graph, form=form), llm


def test_full_plan_uses_only_real_places():
    out, _ = run([honest_planner, honest_planner])
    answer = out["answer"]
    assert out["context"]["mode"] == "places"
    real = {p["name"] for p in out["place_results"]["places"]}
    shown = set(re.findall(r"\*\*(?:Morning|Afternoon|Evening): \[([^\]]+)\]", answer))
    assert shown and shown <= real                                 # every stop is a real place
    assert "google.com/maps/search" in answer and "Hours on" in answer
    assert "Every stop below is a real place from Google Maps" in answer
    assert "Day 8" in answer and "Travel day" in answer            # return at 12:00 -> travel day
    assert out["activity_results"]["status"] == "skipped"          # Tavily not needed
    assert "Places, ratings, addresses and hours: Google Maps via SerpApi" in answer


def test_rule_breaking_draft_is_retried_then_cleaned():
    out, llm = run([sloppy_planner, sloppy_planner])
    answer = out["answer"]
    assert "Invented Museum" not in answer and "N line" not in answer
    assert len([c for c in llm.calls if c[0] == "ItineraryDraft"]) == 2      # retried once
    assert "were removed because they failed these checks" in answer


def test_places_failure_falls_back_to_web_sources():
    llm = FakeLLM({"TripRequest": [], "ItineraryDraft": [{"days": [], "tips": []}] * 2})
    graph = backend.build_graph(backend.Services(llm=llm, http_session=FakeSession(places=None),
                                                 tavily_client=FakeTavily(), today=lambda: TODAY), MemorySaver())
    form = {"origin_city": "Atlanta", "destination_city": "San Francisco", "departure_date": "2026-12-10",
            "return_date": "2026-12-17", "adults": 1}
    out = backend.run_travel_agent("form", "t-fallback", graph=graph, form=form)
    assert out["context"]["mode"] == "sources" and out["activity_results"]["status"] == "ok"


def test_stray_source_id_in_places_mode_is_ignored():
    result, ids = _verify({1: lambda i: [{"time_of_day": "Morning", "place_id": i["Coit Tower"],
                                          "description": "Views of the bay.", "source_id": "S1"}]}, num_days=1)
    assert result["removed"] == 0


def test_empty_llm_plan_is_filled_from_real_places():
    out, _ = run([{"days": [], "tips": []}, {"days": [], "tips": []}])
    answer = out["answer"]
    real = {p["name"] for p in out["place_results"]["places"]}
    shown = set(re.findall(r"\*\*(?:Morning|Afternoon|Evening): \[([^\]]+)\]", answer))
    assert shown and shown <= real
    assert "added automatically" in answer and "Free day" not in answer      # 8 places cover all 7 days


def test_llm_down_still_gives_a_place_plan():
    out, _ = run([RuntimeError("Groq down"), RuntimeError("Groq down")])
    assert "added automatically" in out["answer"] and "$" in out["answer"]


def test_fill_gaps_keeps_days_compact():
    from core.places_plan import fill_gaps
    places = _places()
    assign_zones(places)
    avail = availability(places, "2026-12-10", {2: ["Morning", "Afternoon", "Evening"]})
    draft, added = fill_gaps({"days": [], "tips": []}, places, avail, {2: ["Morning", "Afternoon", "Evening"]})
    stops = [next(p for p in places if p["id"] == a["place_id"]) for a in draft["days"][0]["activities"]]
    assert added == 2 and len({p["zone"] for p in stops}) == 1        # both stops in the same area


def test_over_packed_early_days_are_rebalanced():
    from core.places_plan import fill_gaps
    places = _places()
    assign_zones(places)
    free = {d: ["Morning", "Afternoon", "Evening"] for d in (2, 3, 4)}
    avail = availability(places, "2026-12-10", free)
    ids = [p["id"] for p in places]
    # The LLM used ALL 8 places on days 2 and 3, leaving day 4 empty.
    packed = {"days": [{"day_number": 2, "activities": [act("Morning", ids[0], "a"), act("Afternoon", ids[1], "b"),
                                                        act("Evening", ids[2], "c"), act("Morning", ids[3], "d")]},
                       {"day_number": 3, "activities": [act("Morning", ids[4], "e"), act("Afternoon", ids[5], "f"),
                                                        act("Evening", ids[6], "g"), act("Morning", ids[7], "h")]}],
              "tips": []}
    draft, _ = fill_gaps(packed, places, avail, free)
    counts = {d["day_number"]: len(d["activities"]) for d in draft["days"]}
    assert counts[4] >= 1 and sum(counts.values()) == 8              # nothing lost, day 4 not empty
