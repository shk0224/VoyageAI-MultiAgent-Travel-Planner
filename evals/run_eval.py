"""Run the sample trips and score plan quality.

    python -m evals.run_eval --dry-run          # offline, test fixtures (free, checks the harness)
    python -m evals.run_eval --limit 3 --yes    # LIVE: real APIs, first 3 trips
    python -m evals.run_eval --yes              # LIVE: all 12 trips

LIVE runs use real quota: about 4-7 SerpApi searches and 1 Tavily credit (fallback only)
per trip, plus 1-2 Groq calls. Results go to evals/results/ (JSON + a Markdown report).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import backend  # noqa: E402
from evals.metrics import compute_metrics  # noqa: E402
from evals.scenarios import SCENARIOS  # noqa: E402

SEARCHES_PER_TRIP = "4-7"


def to_form(s: dict, today: date) -> dict:
    depart = today + timedelta(days=s["depart_in_days"])
    form = {k: v for k, v in s.items() if k not in ("name", "depart_in_days", "nights")}
    form.update({"departure_date": depart.isoformat(),
                 "return_date": (depart + timedelta(days=s["nights"])).isoformat(),
                 "min_hotel_rating": 4.0, "allow_rentals": False, "allow_hostels": False})
    form.setdefault("children", 0)
    return form


def dry_run_graph():
    """Offline graph with test fixtures and an honest fake planner."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    import os
    os.environ.setdefault("SERPAPI_API_KEY", "dry-run")
    os.environ.setdefault("TAVILY_API_KEY", "dry-run")
    from langgraph.checkpoint.memory import MemorySaver
    from conftest import PLACES, FakeLLM, FakeSession, FakeTavily
    from test_places import honest_planner
    llm = FakeLLM({"ItineraryDraft": [honest_planner] * 200})
    return backend.build_graph(backend.Services(llm=llm, http_session=FakeSession(places=PLACES),
                                                tavily_client=FakeTavily(), today=date.today), MemorySaver())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="offline with test fixtures")
    ap.add_argument("--limit", type=int, default=len(SCENARIOS))
    ap.add_argument("--yes", action="store_true", help="confirm a LIVE run (uses API quota)")
    args = ap.parse_args()

    scenarios = SCENARIOS[:args.limit]
    if not args.dry_run and not args.yes:
        print(f"LIVE run of {len(scenarios)} trips uses about {SEARCHES_PER_TRIP} SerpApi searches per trip.")
        print("Add --yes to confirm, or use --dry-run.")
        return
    graph = dry_run_graph() if args.dry_run else None
    today = date.today()

    rows = []
    for s in scenarios:
        form = to_form(s, today)
        started = time.time()
        try:
            out = backend.run_travel_agent(f"eval: {s['name']}", f"eval_{uuid.uuid4().hex[:8]}",
                                           graph=graph, form=form)
            m = compute_metrics(out) if out.get("context") else {"error": "; ".join(out.get("questions") or [])}
        except Exception as exc:                       # one failed trip must not stop the run
            out, m = {}, {"error": f"{type(exc).__name__}: {exc}"}
        m["seconds"] = round(time.time() - started, 1)
        m["name"] = s["name"]
        rows.append({"scenario": s, "form": form, "metrics": m, "answer": out.get("answer", "")})
        status = m.get("error") or f"{m['passed']}/{m['total_checks']} checks"
        print(f"- {s['name']}: {status} ({m['seconds']}s)")

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-dry" if args.dry_run else "")
    (out_dir / f"{stamp}.json").write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    (out_dir / f"{stamp}.md").write_text(report(rows, args.dry_run), encoding="utf-8")
    print(f"\nReport: evals/results/{stamp}.md")


def report(rows: list[dict], dry: bool) -> str:
    ok = [r["metrics"] for r in rows if "error" not in r["metrics"]]
    lines = [f"# VoyageAI plan-quality report{' (DRY RUN: test fixtures)' if dry else ''}",
             f"_{datetime.now():%Y-%m-%d %H:%M}_ · {len(rows)} trips · {len(ok)} produced a plan\n"]
    if ok:
        total = sum(m["total_checks"] for m in ok)
        passed = sum(m["passed"] for m in ok)
        lines.append(f"**Quality checks passed: {passed}/{total} ({100 * passed // max(total, 1)}%)**\n")
        names = list(ok[0]["checks"])
        lines.append("| Check | Passed |\n|---|---|")
        for n in names:
            lines.append(f"| {n} | {sum(m['checks'][n] for m in ok)}/{len(ok)} |")
    lines.append("\n| Trip | Mode | Places | Acts/full day | Empty days | Real places | Hours confirmed | "
                 "Max spread km | Hotel km | Flight warnings | Budget fits | Seconds |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        m = r["metrics"]
        if "error" in m:
            lines.append(f"| {m['name']} | ❌ {m['error'][:80]} |||||||||||")
            continue
        lines.append(f"| {m['name']} | {m['mode']} | {m['places_found']} | {m['activities_per_full_day']} | "
                     f"{m['empty_plannable_days']} | {m['grounded_share']} | {m['hours_confirmed_share']} | "
                     f"{m['max_day_spread_km']} | {m['hotel_km_to_sights']} | {m['flight_timing_warnings']} | "
                     f"{m['budget_fits']} | {m['seconds']} |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
