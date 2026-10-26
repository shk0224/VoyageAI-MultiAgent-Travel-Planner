# VoyageAI — Multi-Agent U.S. Travel Planner

A LangGraph travel planner for **U.S. domestic trips** (the 50 states and Washington, DC). It turns a request like *"a 4-day trip from Atlanta to New York starting December 10, 2026, for 2 adults, budget $1,500"* into a plan with real flights, real hotels, a budget and a day-by-day itinerary.

**Why U.S. only:** a narrower scope means every output can be checked: one currency (USD), no visas or international rules, and a complete U.S. airport list. City names shared by several states (Portland, Columbus, Springfield) are resolved by asking which state. Hawaii and Alaska are included; U.S. territories such as Puerto Rico are not yet supported.

**Design rule: the LLM never supplies a fact.** Every price, flight, hotel and date comes from an API or from Python code. The LLM reads the request and writes activity suggestions, and a separate checking step rejects anything it writes that the data doesn't support.

## How it works

```
understand ──(missing info?)──► ask the user
    │
    ├──► flights ─────────────┐
    └──► attractions ─────────┴──► hotels ──► budget ──► write plan ──► verify ──► compose
         (Google Maps places;                              ▲             │       (+ fill gaps
          web sources only as fallback)                    └─ retry once ┘        from real places)
```

| Step | What it does | LLM? |
|---|---|---|
| understand | Form tab: values used as-is. Chat: LLM reads the message into a fixed form (`TripRequest`). Python then checks dates, airports and traveller counts | chat only |
| ask | If something required is missing or invalid, asks instead of guessing | no |
| flights | Google Flights via SerpApi, with the round-trip return lookup; always shows a nonstop option if one exists | no |
| attractions | **Real places from Google Maps** (SerpApi): name, rating, reviews, address, coordinates, weekly hours. Airport codes are searched by their city (JFK → New York). Tavily web sources are only a fallback | no |
| hotels | Google Hotels via SerpApi; check-in = real landing date; ranked by distance to the main sights; ratings from fewer than 20 reviews don't count | no |
| budget | Flight + hotel totals and what's left of the budget, in plain Python | no |
| write plan | LLM **chooses** places from the list for each day and slot (it is told which places are open when) and writes 1-2 sentence descriptions | yes |
| verify | Every stop must be on the list, used once, and open in that slot; no prices, opening-hours claims, transit directions or card names in the text; empty or spread-out days trigger one retry | no |
| compose | Python writes the answer. Names, ratings, addresses, hours and map links come from the data. Days the LLM left empty are filled from the same place list (marked "added automatically") | no |

Memory: the conversation id is created by the server and kept in an httpOnly cookie, so users cannot reach each other's conversations. Trip details are saved per conversation thread (Postgres via `langgraph-checkpoint-postgres`), so a follow-up like *"make it 2 adults"* updates the existing trip.

**API usage per plan (approx.):** SerpApi 5-8 searches (flights 1 + up to 3 return lookups, hotels 1, attractions 1-4), Groq 1-3 calls, Tavily 0-1 credit (fallback only).

## Measuring plan quality

Tests check the rules; the evaluation checks whether plans are **good**. It runs 12 sample trips (NYC, JFK/MIA airport codes, Portland ME vs OR, Honolulu, interests, short and long trips) and scores: real places only, no empty days, stops per full day, day compactness, hotel distance to sights, trusted hotel rating, flight-time warnings and budget fit.

```powershell
python -m evals.run_eval --dry-run          # offline with test data (free)
python -m evals.run_eval --limit 3 --yes    # LIVE: first 3 trips (uses API quota)
python -m evals.run_eval --yes              # LIVE: all 12 trips (~60-90 SerpApi searches)
```
The report is written to `evals/results/` as Markdown + JSON.

## Run it (Windows, VS Code)

Open the project folder in VS Code, then open a terminal (**Terminal → New Terminal**, PowerShell):

```powershell
# 1. Create and activate a virtual environment (Python 3.11+)
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
#   If PowerShell blocks this: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned

# 2. Install packages
pip install -r requirements.txt

# 3. Add your API keys
copy .env.example .env
notepad .env

# 4. Run the tests (no keys or internet needed)
pytest -q

# 5a. Run the web app, then open http://127.0.0.1:8000
python app.py

# 5b. Or run in the terminal
python plan.py
```

With conda instead: `conda create -n travel python=3.11 -y`, `conda activate travel`, then steps 2–5.

In VS Code, press `Ctrl+Shift+P` → **Python: Select Interpreter** → choose `.venv`, so the editor uses the same packages.

**Keys:** `GROQ_API_KEY`, `SERPAPI_API_KEY` and `TAVILY_API_KEY` are required. `DATABASE_URL` is optional; without it, conversations are kept only while the server is running.

## Project layout

```
backend.py            LangGraph pipeline
app.py                FastAPI server (session cookie, history, new trip)
plan.py               terminal client
core/models.py        TripRequest (from LLM) and Trip (validated)
core/trip_parser.py   extraction + validation
core/budget.py        stay dates and budget math
core/itinerary.py     the LLM itinerary writer (structured output)
core/verifier.py      checks LLM text against the data
core/render.py        builds the final answer
tools/airports.py     U.S. city (+ state) -> airport codes; rejects non-U.S. places
tools/flight_tool.py  Google Flights (SerpApi)
tools/hotel_tool.py   Google Hotels (SerpApi)
tools/places_tool.py  real attractions from Google Maps (SerpApi)
tools/tavily_tool.py  web sources (fallback only)
core/hours.py         opening-hours parser ("11 AM–2:30 PM, 5–10 PM") and slot checks
core/places_plan.py   zones, availability per slot, day spread, gap filling
evals/                plan-quality evaluation (scenarios, metrics, runner)
tests/                pytest suite with fake APIs and a fake LLM (test fixtures only)
static/, templates/   web page; static/vendor holds pinned marked, DOMPurify, html2pdf
```

## Known limits

- Prices are search snapshots and can change before booking. Nothing is booked.
- The Google Flights price is treated as the total for all travellers. A real test (1 adult $63 → 2 adults $126 on the same flight) supports this; confirm once with identical dates, then set `PRICE_BASIS_VERIFIED = True` in `tools/flight_tool.py`.
- Round trips are budgeted only when the return flight is confirmed.
- Hotel totals marked "(est.)" are nightly rate × nights; taxes and fees may be extra. One room is assumed.
- Food, local transport, activities and tickets are not priced; the budget shows what's left for them.
- Opening hours come from Google Maps weekly hours: holiday closures and special events are not known. Places without listed hours are allowed but marked "check before going".
- Distances are straight lines, not travel time.
- The verifier checks place IDs, hours, prices, transit wording and flight numbers; it cannot fact-check every general claim in the short descriptions.
- Rate limiting is in-memory (one server process). There are no user accounts.
- U.S. domestic only (50 states + DC). For short trips (under ~300 miles straight-line) the plan mentions driving, but doesn't price it.
