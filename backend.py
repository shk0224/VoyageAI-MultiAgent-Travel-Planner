"""VoyageAI backend: a LangGraph pipeline where facts come from APIs, not the LLM.

    understand ──(missing info?)──► ask the user ──► END
        │
        ├──► flights ──┐
        ├──► hotels  ──┼──► budget ──► write itinerary ──► verify ──► compose ──► END
        └──► activities┘                      ▲               │
                                              └── retry once ─┘ (if checks fail)

- understand: LLM reads the message into a form; Python validates it.
- flights / hotels / activities: run in parallel, return real data with sources.
- budget: plain Python math.
- write itinerary: the only LLM-written content (activities and tips).
- verify: rejects prices, flight numbers or citations the data doesn't support.
- compose: Python writes the final answer from the data.

Memory: the trip details are saved per thread (Postgres if DATABASE_URL is set,
otherwise in memory), so follow-ups like "make it 2 adults" update the trip.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Annotated, Any, Callable, Optional, TypedDict
import operator

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from core import render
from core.budget import allowed_amounts, compute_budget, compute_stay, pick_flight
from core.itinerary import write_itinerary
from core.models import Trip, TripRequest
from core.trip_parser import extract_request, merge_requests, validate_request
from core.schedule import schedule
from core.verifier import verify_draft
from tools.flight_tool import search_flights
from tools.hotel_tool import search_hotels
from tools.tavily_tool import search_activities
from tools.places_tool import center_of, search_places
from core.places_plan import add_hotel_distance, assign_zones, availability, fill_gaps

load_dotenv()

# Some Windows/corporate networks need certifi's certificate list for HTTPS.
# Your original code did this always; now it's opt-in with USE_CERTIFI=1 in .env.
if os.getenv("USE_CERTIFI", "0") == "1":
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
log = logging.getLogger("voyageai")

MAX_WRITE_ATTEMPTS = 2
MIN_PLACES_FOR_PLACES_MODE = 4   # fewer real places than this -> fall back to web sources


# =========================
# Dependencies (swappable in tests)
# =========================

@dataclass
class Services:
    llm: Any
    http_session: Any = None            # requests.Session-like; None = real requests
    tavily_client: Any = None           # None = real TavilyClient from TAVILY_API_KEY
    today: Callable[[], date] = field(default=date.today)


# =========================
# State
# =========================

class TravelState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], operator.add]
    user_input: str
    request: dict            # what the user has told us so far (kept across turns)
    trip: Optional[dict]     # validated trip, or None
    questions: list[str]
    flights: dict
    places: dict
    hotels: dict
    activities: dict
    stay: dict
    budget: dict
    context: dict
    draft: Optional[dict]
    verification: Optional[dict]
    attempts: int
    answer: str
    plan: Optional[dict]           # the final day plan shown to the user (after gap filling)
    form_request: Optional[dict]   # set when the request came from the form tab
    error: str
    llm_calls: int


def _trip(state: TravelState) -> Trip:
    return Trip(**state["trip"])


# =========================
# Graph
# =========================

def build_graph(services: Services, checkpointer=None):

    def understand(state: TravelState):
        # Form tab: the user filled every field themselves, so no LLM is needed.
        # The form replaces the earlier trip completely (no merging).
        if state.get("form_request"):
            request = TripRequest(**state["form_request"])
            trip, questions = validate_request(request, services.today())
            return {"request": request.model_dump(),
                    "trip": trip.model_dump(mode="json") if trip else None,
                    "questions": questions, "llm_calls": 0, "error": ""}
        new = None
        for attempt in (1, 2):      # one retry: Groq sometimes fails once and works on the next call
            try:
                new = extract_request(services.llm, state["user_input"], state.get("request"), services.today())
                break
            except Exception:
                log.exception("Extraction failed (attempt %s of 2)", attempt)
        if new is None:
            return {"trip": None, "llm_calls": 1, "error": "unavailable",
                    "questions": ["Sorry, I couldn't read that request right now (the language model "
                                  "is unavailable). Please try again in a moment."]}
        request = merge_requests(state.get("request"), new)
        trip, questions = validate_request(request, services.today())
        return {"request": request.model_dump(),
                "trip": trip.model_dump(mode="json") if trip else None,
                "questions": questions, "llm_calls": 1, "error": ""}

    def route_after_understand(state: TravelState):
        if state.get("questions"):
            return "ask"
        return ["flights", "activities"]       # parallel; hotels wait for both (dates + sights location)

    def ask(state: TravelState):
        if state.get("error"):
            text = state["questions"][0]
            return {"answer": text, "messages": [AIMessage(content=text)]}
        known = {k: v for k, v in (state.get("request") or {}).items() if v not in (None, [], "")}
        text = "To plan this trip I need a bit more information:\n\n"
        text += "\n".join(f"- {q}" for q in state["questions"])
        if known:
            text += "\n\n_So far I have:_ " + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in known.items())
        return {"answer": text, "messages": [AIMessage(content=text)]}

    def _tool_failed(name: str, source: str) -> dict:
        """Same shape as a tool result, for unexpected crashes (e.g. odd API data)."""
        log.exception("%s step crashed", name)
        return {"source": source, "fetched_at": "", "status": "error", "message":
                f"The {name} step hit an unexpected problem with the provider's data. "
                f"Details are in the server log.", "query": {}, "offers": [], "sources": [], "places": []}

    def flights(state: TravelState):
        try:
            return {"flights": search_flights(_trip(state), session=services.http_session)}
        except Exception:
            return {"flights": _tool_failed("flight search", "Google Flights via SerpApi")}

    def hotels(state: TravelState):
        # Runs after flights (check-in = real landing date) and after the attraction
        # search (hotels are ranked by distance to the main sights).
        trip = _trip(state)
        stay = compute_stay(trip, pick_flight(state["flights"]))
        places = state.get("places") or {}
        center = tuple(places["center"]) if places.get("status") == "ok" and places.get("center") else None
        try:
            result = search_hotels(trip, session=services.http_session,
                                   check_in=date.fromisoformat(stay["check_in"]),
                                   check_out=date.fromisoformat(stay["check_out"]),
                                   sights_center=center)
        except Exception:
            result = _tool_failed("hotel search", "Google Hotels via SerpApi")
        return {"hotels": result, "stay": stay}

    def activities(state: TravelState):
        # 1st choice: real places from Google Maps. Fallback: web sources from Tavily.
        trip = _trip(state)
        try:
            places = search_places(trip, session=services.http_session)
        except Exception:
            places = _tool_failed("attraction search", "Google Maps via SerpApi")
        if places.get("status") == "ok" and len(places.get("places", [])) >= MIN_PLACES_FOR_PLACES_MODE:
            return {"places": places, "activities": {"status": "skipped", "sources": [],
                                                     "message": "Not needed: real places were found."}}
        try:
            sources = search_activities(trip, client=services.tavily_client)
        except Exception:
            sources = _tool_failed("activity search", "Tavily web search")
        return {"places": places, "activities": sources}

    def budget(state: TravelState):
        trip, stay = _trip(state), state["stay"]
        b = compute_budget(trip, state["flights"], state["hotels"], nights=stay["nights"])

        # Resolve the exact offers selected by compute_budget. In particular,
        # an affordable hotel may differ from the first ranked hotel.
        flight = next(
            (offer for offer in state["flights"].get("offers", [])
             if offer.get("id") == b.get("flight_id")),
            None,
        )
        hotel = next(
            (offer for offer in state["hotels"].get("offers", [])
             if offer.get("id") == b.get("hotel_id")),
            None,
        )

        outbound_legs = (flight or {}).get("outbound_legs") or []
        return_legs = (flight or {}).get("return_legs") or []
        context = {
            "num_days": stay["num_days"],
            "first_day": stay["first_day"],
            "last_day": stay["check_out"],
            "arrival_time": outbound_legs[-1].get("arrives") if outbound_legs else None,
            "departure_time": return_legs[0].get("departs") if return_legs else None,
            "hotel_name": hotel.get("name") if hotel else None,
            "sources": state["activities"].get("sources", []),
        }
        context["free_slots"] = schedule(
            context["num_days"], context["arrival_time"],
            context["departure_time"], context["first_day"],
        )
        places_result = state.get("places") or {}
        place_list = [dict(p) for p in places_result.get("places", [])]
        if places_result.get("status") == "ok" and len(place_list) >= MIN_PLACES_FOR_PLACES_MODE:
            assign_zones(place_list)
            add_hotel_distance(place_list, hotel)
            context["mode"] = "places"
            context["places"] = place_list
            context["availability"] = availability(place_list, context["first_day"], context["free_slots"])
        else:
            context["mode"] = "sources"
            context["places"] = []
            context["availability"] = {}
        return {"budget": b, "context": context}

    def write(state: TravelState):
        attempts = state.get("attempts", 0) + 1
        feedback = (state.get("verification") or {}).get("problems") if attempts > 1 else None
        try:
            draft = write_itinerary(services.llm, _trip(state), state["context"], feedback)
        except Exception:
            log.exception("Itinerary writing failed (attempt %s)", attempts)
            # On a failed retry, keep the earlier draft: the verifier already cleaned it.
            draft = state.get("draft") if attempts > 1 else None
        return {"draft": draft, "attempts": attempts, "llm_calls": state.get("llm_calls", 0) + 1}

    def verify(state: TravelState):
        if state.get("draft") is None:
            return {"verification": None}
        trip = _trip(state)
        result = verify_draft(
            state["draft"], state["context"]["num_days"],
            allowed_amounts(trip, state["flights"], state["hotels"], state["budget"]),
            {s["id"] for s in state["context"]["sources"]},
            state["flights"],
            free_slots={int(k): v for k, v in state["context"]["free_slots"].items()},
            places=({p["id"]: p for p in state["context"]["places"]}
                    if state["context"].get("mode") == "places" else None),
            availability=({int(k): v for k, v in state["context"]["availability"].items()}
                          if state["context"].get("mode") == "places" else None))
        if not result["ok"]:
            log.warning("Verification problems (attempt %s): %s", state["attempts"], result["problems"])
        return {"verification": result}

    def route_after_verify(state: TravelState):
        v = state.get("verification")
        if v and not v["ok"] and state.get("attempts", 0) < MAX_WRITE_ATTEMPTS:
            return "write"
        return "compose"

    def compose(state: TravelState):
        trip, ctx, b = _trip(state), state["context"], state["budget"]
        v = state.get("verification")
        sources = ctx["sources"]
        free = {int(k): s for k, s in ctx["free_slots"].items()}
        places = {p["id"]: p for p in ctx.get("places", [])}
        plan_draft = v["clean_draft"] if v else None
        auto_added = 0
        if ctx.get("mode") == "places":
            # Safety net: if the LLM left days empty (or failed), fill them from the real place list.
            plan_draft, auto_added = fill_gaps(plan_draft, ctx["places"],
                                               {int(k): s for k, s in ctx["availability"].items()}, free)
        sections = [
            ("Your trip", lambda: render.trip_section(trip, state["stay"])),
            ("Flights", lambda: render.flights_section(state["flights"], b["flight_id"], trip)),
            ("Hotels", lambda: render.hotels_section(state["hotels"], b["hotel_id"])),
            ("Budget", lambda: render.budget_section(b)),
            ("Day-by-day plan", lambda: (
                render.places_itinerary_section(ctx["first_day"], plan_draft, ctx["num_days"],
                                                places, ctx["arrival_time"], ctx["departure_time"], free,
                                                {int(k): s for k, s in ctx["availability"].items()},
                                                (v or {}).get("warnings", []))
                if ctx.get("mode") == "places" else
                render.itinerary_section(ctx["first_day"], v["clean_draft"] if v else None, ctx["num_days"],
                                         sources, ctx["arrival_time"], ctx["departure_time"], free))),
            ("Sources", lambda: render.places_source_section(state.get("places") or {})
                if ctx.get("mode") == "places" else render.sources_section(sources)),
            ("Checks", lambda: render.checks_section(v, ctx.get("mode", "sources"), auto_added)),
        ]
        parts = []
        for title, build in sections:
            try:
                parts.append(build())
            except Exception:
                # One broken section must not hide the rest of a valid plan.
                log.exception("Rendering the %s section failed", title)
                parts.append(f"## {title}\n_This section couldn't be displayed (unexpected data). "
                             f"Details are in the server log._")
        text = "\n\n".join(p for p in parts if p)
        return {"answer": text, "messages": [AIMessage(content=text)], "plan": plan_draft}

    g = StateGraph(TravelState)
    for name, fn in [("understand", understand), ("ask", ask), ("flights", flights),
                     ("hotels", hotels), ("activities", activities), ("budget", budget),
                     ("write", write), ("verify", verify), ("compose", compose)]:
        g.add_node(name, fn)

    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", route_after_understand,
                            ["ask", "flights", "activities"])
    g.add_edge("ask", END)
    g.add_edge(["flights", "activities"], "hotels")   # hotels need landing date + sights location
    g.add_edge("hotels", "budget")
    g.add_edge("budget", "write")
    g.add_edge("write", "verify")
    g.add_conditional_edges("verify", route_after_verify, ["write", "compose"])
    g.add_edge("compose", END)
    return g.compile(checkpointer=checkpointer)


# =========================
# Production wiring
# =========================

def _make_checkpointer():
    url = os.getenv("DATABASE_URL")
    if not url:
        from langgraph.checkpoint.memory import MemorySaver
        log.warning("DATABASE_URL not set: conversation memory is in-process only.")
        return MemorySaver()
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    pool = ConnectionPool(url, max_size=10, open=True,
                          kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
    saver = PostgresSaver(pool)
    saver.setup()
    return saver


def _make_llm():
    from langchain_groq import ChatGroq
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is missing. Add it to your .env file.")
    return ChatGroq(model=os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
                    api_key=key, temperature=0, max_retries=2)


_graph = None


def get_graph():
    """Build the graph once, on first use (so importing this file needs no keys)."""
    global _graph
    if _graph is None:
        _graph = build_graph(Services(llm=_make_llm()), _make_checkpointer())
    return _graph


def run_travel_agent(user_input: str, thread_id: str | None = None, graph=None,
                     form: dict | None = None) -> dict:
    """Run one turn. `form` (a TripRequest-shaped dict) comes from the form tab and
    skips LLM extraction; `user_input` is then a readable summary for the chat history."""
    thread_id = thread_id or f"user_{uuid.uuid4().hex}"
    graph = graph or get_graph()
    # Per-turn fields are reset; `request` is kept from earlier turns by the checkpointer.
    result = graph.invoke(
        {"messages": [HumanMessage(content=user_input)], "user_input": user_input,
         "questions": [], "flights": {}, "hotels": {}, "activities": {}, "places": {}, "stay": {}, "budget": {},
         "context": {}, "draft": None, "verification": None, "attempts": 0,
         "answer": "", "error": "", "llm_calls": 0, "form_request": form, "plan": None},
        config={"configurable": {"thread_id": thread_id}},
    )
    return {
        "thread_id": thread_id,
        "answer": result.get("answer", ""),
        "trip": result.get("trip"),
        "questions": result.get("questions", []),
        "flight_results": result.get("flights", {}),
        "hotel_results": result.get("hotels", {}),
        "activity_results": result.get("activities", {}),
        "place_results": result.get("places", {}),
        "context": result.get("context", {}),
        "stay": result.get("stay", {}),
        "budget": result.get("budget", {}),
        "itinerary": result.get("plan") or (result.get("verification") or {}).get("clean_draft"),
        "verification": {k: v for k, v in (result.get("verification") or {}).items() if k != "clean_draft"},
        "llm_calls": result.get("llm_calls", 0),
    }
