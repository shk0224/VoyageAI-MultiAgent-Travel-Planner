"""Real API responses sometimes leave fields out or set them to null.
This test strips fields at random (300 variations) and checks the pipeline
never crashes. It found two real bugs (Oct 2026): Tavily "results": null, and
a flight leg with no airport code breaking the flight table. Covers Google Maps places too."""
import json
import logging
import random
from datetime import date

from langgraph.checkpoint.memory import MemorySaver

import backend
from conftest import HOTELS, OUTBOUND, PLACES, RETURNS, FakeLLM, FakeSession
from test_pipeline import GOOD_DRAFT

FORM = {"origin_city": "ATL", "origin_state": "Georgia", "destination_city": "San Francisco",
        "destination_state": "California", "departure_date": "2026-12-10", "return_date": "2026-12-17",
        "adults": 1, "children": 0, "budget_usd": 800}


def strip(obj, rnd):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            r = rnd.random()
            if r < 0.12:
                continue                                   # field missing
            if r < 0.24 and k not in ("best_flights", "other_flights", "properties", "flights", "local_results"):
                out[k] = None                              # field null
                continue
            out[k] = strip(v, rnd)
        return out
    if isinstance(obj, list):
        return [strip(x, rnd) for x in obj]
    return obj


class MessyTavily:
    def __init__(self, rnd):
        self.rnd = rnd

    def search(self, query, max_results=5):
        return strip({"results": [{"title": "San Francisco guide", "url": "https://x.com/sf", "content": "SF"},
                                  {"title": None, "url": "https://y.com/san-francisco", "content": None}]}, self.rnd)


def test_pipeline_survives_messy_provider_data():
    logging.disable(logging.CRITICAL)
    try:
        for seed in range(300):
            rnd = random.Random(seed)
            session = FakeSession(outbound=strip(OUTBOUND, rnd),
                                  returns={k: strip(v, rnd) for k, v in RETURNS.items()},
                                  hotels=strip(HOTELS, rnd),
                                  places=strip(PLACES, rnd) if seed % 2 else None)   # both paths
            graph = backend.build_graph(backend.Services(
                llm=FakeLLM({"ItineraryDraft": [GOOD_DRAFT] * 4}), http_session=session,
                tavily_client=MessyTavily(rnd), today=lambda: date(2026, 10, 26)), MemorySaver())
            out = backend.run_travel_agent("x", f"t{seed}", graph=graph, form=FORM)
            assert "## Your trip" in out["answer"], seed
    finally:
        logging.disable(logging.NOTSET)
