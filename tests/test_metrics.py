"""The quality metrics themselves are tested, so the evaluation report can be trusted."""
from evals.metrics import compute_metrics
from evals.run_eval import to_form
from evals.scenarios import SCENARIOS
from test_places import honest_planner, run


def test_metrics_on_a_good_plan():
    out, _ = run([honest_planner, honest_planner])
    m = compute_metrics(out)
    assert m["mode"] == "places" and m["grounded_share"] == 1.0
    assert m["hotel"] == "Union Square Hotel" and m["hotel_km_to_sights"] < 2
    assert m["checks"]["real places only"] and m["checks"]["hotel rating trusted"]
    assert m["activities"] >= 8 and m["max_day_spread_km"] is not None


def test_metrics_flag_an_empty_plan():
    out, _ = run([{"days": [], "tips": []}, {"days": [], "tips": []}])
    assert compute_metrics(out)["empty_plannable_days"] == 0       # the fallback filled every day
    out["itinerary"] = {"days": [], "tips": []}                    # score a truly empty plan
    m = compute_metrics(out)
    assert m["activities"] == 0 and m["empty_plannable_days"] >= 5
    assert not m["checks"]["no empty days"] and not m["checks"]["real places only"]


def test_scenarios_make_valid_forms():
    from datetime import date
    for s in SCENARIOS:
        f = to_form(s, date(2026, 10, 26))
        assert f["return_date"] > f["departure_date"] and 1 <= f["adults"] <= 9
