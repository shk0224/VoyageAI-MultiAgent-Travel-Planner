# ✈️ VoyageAI: Multi-Agent U.S. Travel Planner

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-multi--agent-1C3C3C)
![LangSmith](https://img.shields.io/badge/LangSmith-tracing-F26B21)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-checkpointing-4169E1?logo=postgresql&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-backend-009688?logo=fastapi&logoColor=white)
![Groq](https://img.shields.io/badge/LLM-Groq-F55036)

**VoyageAI plans a complete U.S. trip from one sentence.** A team of LangGraph agents finds real flights, real hotels and real attractions, calculates the budget, writes a day-by-day itinerary and then *checks that itinerary against the data* before the user sees it.

> **You:** *Plan a 4-day trip from Atlanta to New York starting December 10, 2026, for 2 adults, budget $1,500.*
>
> **VoyageAI:** flight options with the return leg · ranked hotels · budget breakdown · a day-by-day plan of real Google Maps places that are open at the planned time · links to verify everything

### The core idea

> **The LLM never supplies a fact.** Prices, flights, hotels, ratings, opening hours and dates come from APIs or from Python code. The LLM does two jobs only: it **reads** the user's request, and it **chooses** places from a list of real ones. A verifier agent rejects anything the LLM writes that the data doesn't support.

This is the difference between a chatbot that *sounds* right and a system whose output can be checked.

<!-- Add a screenshot or GIF of a finished plan here:
![VoyageAI demo](docs/demo.png) -->

---

## Contents

- [System architecture](#-system-architecture)
- [The agents](#-the-agents)
- [Observability with LangSmith](#-observability-with-langsmith)
- [Persistent memory with PostgreSQL](#-persistent-memory-with-postgresql)
- [How hallucinations are prevented](#-how-hallucinations-are-prevented)
- [Measuring plan quality](#-measuring-plan-quality)
- [Tech stack](#-tech-stack)
- [Run it locally](#-run-it-locally)
- [Project structure](#-project-structure)
- [Known limits](#-known-limits)

---

## 🏗 System architecture

```mermaid
flowchart TB
    User(["👤 User"]) --> UI

    subgraph Client["🖥️ Web client"]
        UI["Form tab · Chat tab · airport autocomplete · PDF export"]
    end

    UI -->|JSON over HTTP| API

    subgraph Server["⚡ FastAPI server"]
        API["/api/plan · /api/travel · /api/history<br/>httpOnly session cookie · rate limit"]
    end

    API -->|thread_id| AG

    subgraph Graph["🧠 LangGraph multi-agent workflow"]
        AG["9 agents sharing one typed state<br/>parallel branches · conditional routing · verify-and-retry loop"]
    end

    PG[("🗄️ PostgreSQL<br/>PostgresSaver checkpointer")]
    LS["📈 LangSmith<br/>traces · latency · tokens"]

    AG <-->|load / save conversation state| PG
    AG -.->|every run traced| LS

    AG <-->|structured output| GROQ
    AG --> SERP
    AG -.->|fallback| TV

    GROQ["🤖 Groq LLM<br/>Pydantic schemas"]
    SERP["🔎 SerpApi<br/>Google Flights · Google Hotels · Google Maps"]
    TV["🌐 Tavily<br/>web search"]

    classDef store fill:#E8F0FE,stroke:#4169E1,color:#0B2A6F
    classDef obs fill:#FFF1E8,stroke:#F26B21,color:#6B2A00
    classDef llm fill:#F3E8FF,stroke:#7C3AED,color:#3B0764
    classDef data fill:#E6F4EA,stroke:#1E8E3E,color:#0D3B1E
    class PG store
    class LS obs
    class GROQ llm
    class SERP,TV data
```

**Request lifecycle:** the browser sends a form or chat message → FastAPI attaches the user's private conversation ID (from an httpOnly cookie) → LangGraph loads that conversation's saved state from **PostgreSQL**, runs the agents, and saves the new state → the composed plan is returned as Markdown and rendered safely in the browser (DOMPurify). Every step of the run is recorded in **LangSmith**.

---

## 🤖 The agents

The workflow has **9 agents** (LangGraph nodes). Each one has a single job and reads from and writes to a shared, typed state. Only **2 of them call the LLM**; the rest are API tools or plain Python. That's deliberate: language work goes to the LLM, while facts and arithmetic go to code that can be tested.

```mermaid
flowchart TD
    START(["User message or form"]) --> U

    U["🧭 1 · Trip Understanding<br/><i>LLM in chat mode</i>"]
    U -->|details missing or ambiguous| Q["❓ 2 · Clarifier<br/>asks the user"]
    Q --> END1(["Wait for reply"])

    U -->|trip is complete| F["✈️ 3 · Flight Search"]
    U -->|in parallel| P["📍 4 · Places Research"]

    F --> H["🏨 5 · Hotel Search"]
    P --> H
    H --> B["💵 6 · Budget & Schedule"]
    B --> W["📝 7 · Itinerary Planner<br/><i>LLM</i>"]
    W --> V{"🔍 8 · Verifier"}
    V -->|rule broken: feedback, retry once| W
    V -->|accepted, or invalid items removed| C["🧩 9 · Composer"]
    C --> END2(["Final plan"])

    classDef llm fill:#F3E8FF,stroke:#7C3AED,color:#3B0764
    classDef tool fill:#E6F4EA,stroke:#1E8E3E,color:#0D3B1E
    classDef py fill:#EEF2F7,stroke:#5F6B7A,color:#1F2937
    class U,W llm
    class F,P,H tool
    class Q,B,V,C py
```

<sub>🟪 LLM agent · 🟩 tool agent (live API) · ⬜ deterministic Python agent</sub>

| # | Agent | Job | Input → Output | LLM? |
|---|---|---|---|:---:|
| 1 | **Trip Understanding** | Turns free text into a structured `TripRequest` (Pydantic). Python then validates cities, airports, dates and traveler counts. In the form tab the fields arrive structured, so the LLM is skipped. | message → validated `Trip` | chat only |
| 2 | **Clarifier** | Asks instead of guessing when something is missing or ambiguous (e.g. *Portland, Maine or Portland, Oregon?*) | missing fields → question | ✗ |
| 3 | **Flight Search** | Google Flights via SerpApi, including the return leg (`departure_token`). Always keeps the cheapest nonstop in the shortlist and flags very early or late flights. | trip → priced flight offers | ✗ |
| 4 | **Places Research** | Google Maps via SerpApi: names, ratings, review counts, coordinates and weekly hours. Filters out hotels, airports and parking, drops far-away matches, applies a 4.0★ / 50-review bar. Falls back to Tavily web search if too few places are found. | trip → list of real places `P1…Pn` | ✗ |
| 5 | **Hotel Search** | Google Hotels via SerpApi. Runs after flights so check-in matches the real landing date, and after places so hotels can be ranked by **distance to the sights**. A rating counts only with 20+ reviews. | trip + flight + sights center → ranked hotels | ✗ |
| 6 | **Budget & Schedule** | Flight + hotel total and remaining budget, computed in Python. Works out which time slots are free each day from the flight times, groups nearby places into zones, and builds an **availability table**: which places are open in each day/slot. | offers + places → budget, free slots, availability | ✗ |
| 7 | **Itinerary Planner** | The LLM chooses place IDs for each day and slot, **only from the places open then**, and writes a 1–2 sentence description. Input is compact JSON to fit Groq's token limits. | availability + places → `ItineraryDraft` | ✓ |
| 8 | **Verifier** | Rule-based checks (no LLM judging an LLM). See below. On failure, sends the exact problems back to the planner for **one retry**, then removes anything that still fails. | draft + data → problems, clean draft | ✗ |
| 9 | **Composer** | Builds the final answer from the data. If a day is still empty, fills it with real open places marked *added automatically*. Each section renders independently, so one bad data source can't break the whole plan. | everything → final Markdown plan | ✗ |

### What the Verifier checks

- every planned place is on the retrieved list (no invented attractions)
- each place is used only once
- the place is open in its assigned slot (using Google's weekly hours)
- no unsupported dollar amounts, opening-hours claims or flight numbers in the text
- no made-up transit directions ("take the N line") or transit card names
- no empty days while open places are available; days that spread too far apart get a warning

---

## 📈 Observability with LangSmith

Every graph run is traced in **LangSmith**, so you can see exactly what each agent did.

| What you can see in a trace | Why it matters |
|---|---|
| Each agent (node) as its own step, in order, with timing | Find which agent is slow (usually the live API calls) |
| Exact prompts sent to Groq and the structured output returned | Debug a bad itinerary by seeing what the LLM was given |
| Token usage and latency per LLM call | Stay within Groq's per-minute token limits |
| The Verifier → Planner retry loop | See which rule the LLM broke and whether the retry fixed it |
| Errors with the full stack trace | Diagnose failures without reproducing them |

Tracing needs no code changes: LangGraph and LangChain report to LangSmith automatically when these variables are set in `.env`:

```dotenv
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_key
LANGSMITH_PROJECT=voyageai
```

<!-- Add a screenshot of a LangSmith trace here (it shows the 9 agents as steps):
![LangSmith trace](docs/langsmith-trace.png) -->

---

## 🗄 Persistent memory with PostgreSQL

Conversations are stored in **PostgreSQL** through LangGraph's `PostgresSaver` checkpointer, so the planner remembers each user's trip across messages **and across server restarts**.

- **One thread per user:** the server creates the conversation ID and keeps it in an `httpOnly`, `SameSite=Lax` cookie. Users can't read or guess each other's IDs, and forged IDs are rejected.
- **Follow-ups edit the trip instead of starting over:** *"make it 2 adults and nonstop only"* is merged into the saved trip, and the agents re-plan with the updated details.
- **Production settings:** a `psycopg` connection pool (`psycopg_pool`), `sslmode=require` for hosted databases, autocommit, and `prepare_threshold=0` for compatibility with connection poolers.
- **No database? Still works:** without `DATABASE_URL` it falls back to an in-memory checkpointer, so the app and tests run with zero setup.

```dotenv
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

The checkpoint tables are created automatically on first start (`saver.setup()`).

---

## 🛡 How hallucinations are prevented

| Risk | What VoyageAI does |
|---|---|
| LLM invents a place | The planner may only use place IDs from the retrieved list, and the Verifier rejects any other. |
| LLM gets arithmetic wrong | Budget, totals and dates are computed in Python, never by the LLM. |
| Plan visits a closed museum | Opening hours are parsed per weekday and checked against each time slot. |
| A fake 5★ listing wins | Ratings from fewer than 20 reviews aren't trusted. |
| Searching "JFK" returns airport hotels | Airport codes are converted to their real city ("New York, NY") before searching. |
| A 6 AM flight ruins day 1 | Early departures and late arrivals are flagged, and a better-timed option is suggested when one exists. |
| An API returns broken data | Every agent has a safety net. A fuzz test sends 300 randomly broken API responses through the full pipeline. |
| The LLM fails or under-plans | The Composer fills empty days with real places, clearly labelled. |

Every plan ends with an **Explore & Verify** section (links to the flight search, hotel pages and each place's Google Maps listing) and a plain statement of **what was checked and what wasn't**.

---

## 📊 Measuring plan quality

Unit tests show the code works; they don't show the plans are *good*. The `evals/` harness runs **12 realistic trips** and scores each plan on 6 checks:

| Check | Target |
|---|---|
| Real places only | 100% of stops come from the retrieved list |
| No empty days | 0 days with free time but no activities |
| Enough per day | ≥ 1.5 stops on average per full day |
| Compact days | no day spans more than 12 km |
| Hotel near sights | chosen hotel within 8 km of the sights' center |
| Trusted hotel rating | chosen hotel's rating has 20+ reviews |

The scenarios cover big cities (New York, Chicago, San Francisco, Washington DC), airport codes as destinations (JFK, MIA), a city name shared by two states (Portland, Maine), Hawaii (Honolulu), interest-based trips (food, museums, live music, outdoors, history) and a tight budget (Las Vegas).

```bash
python -m evals.run_eval --dry-run          # offline with test data (checks the harness, free)
python -m evals.run_eval --limit 3 --yes    # 3 trips with live APIs
python -m evals.run_eval --yes              # all 12 (about 60–100 SerpApi searches)
```

Reports are saved to `evals/results/` as Markdown and JSON.

<!-- After a live run, add your result here, e.g. "Live run, Oct 2026: 70/72 checks passed". -->

---

## 🧰 Tech stack

| Layer | Technology |
|---|---|
| Agent orchestration | **LangGraph** (`StateGraph`, parallel branches, conditional routing, retry loop) |
| LLM | **Groq** via `langchain-groq`, structured output into **Pydantic** models |
| Observability | **LangSmith** tracing |
| Memory / persistence | **PostgreSQL** + `langgraph-checkpoint-postgres`, `psycopg` connection pool |
| Backend | **FastAPI**, Uvicorn, Jinja2 |
| Live data | **SerpApi** (Google Flights, Google Hotels, Google Maps), **Tavily** (fallback) |
| Reference data | `airportsdata` (U.S. airports), `pycountry` |
| Frontend | HTML, CSS, JavaScript; `marked` + `DOMPurify` (safe Markdown), `html2pdf` (PDF export) |
| Testing | **pytest**: fake APIs and a fake LLM, so tests run offline in seconds |
| Evaluation | custom harness in `evals/` (12 scenarios, 6 quality checks) |

---

## 🚀 Run it locally

**Prerequisites:** Python 3.11+, API keys for [Groq](https://console.groq.com), [SerpApi](https://serpapi.com) and [Tavily](https://tavily.com). Optional: [LangSmith](https://smith.langchain.com) and a PostgreSQL database.

```powershell
git clone https://github.com/shk0224/VoyageAI-MultiAgent-Travel-Planner.git
cd VoyageAI-MultiAgent-Travel-Planner

py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
# If PowerShell blocks this: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# (conda alternative: conda create -n travel python=3.11 -y; conda activate travel)

pip install -r requirements.txt

Copy-Item .env.example .env
notepad .env              # add your keys (see below)

python check_setup.py     # checks your keys and which structured-output mode your Groq model supports
pytest -q                 # offline tests: no keys or internet needed
python app.py             # open http://127.0.0.1:8000
python plan.py            # or plan from the terminal
```

**`.env` settings**

| Variable | Required | Purpose |
|---|:---:|---|
| `GROQ_API_KEY` | ✓ | LLM |
| `SERPAPI_API_KEY` | ✓ | flights, hotels, places |
| `TAVILY_API_KEY` | ✓ | fallback web search |
| `GROQ_MODEL` | | default `openai/gpt-oss-20b` |
| `GROQ_STRUCTURED_METHOD` | | `function_calling` (default), `json_schema` or `json_mode` |
| `DATABASE_URL` | | PostgreSQL for persistent conversations |
| `LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, `LANGSMITH_PROJECT` | | LangSmith tracing |
| `RATE_LIMIT_PER_HOUR` | | plan requests per client per hour (default 20) |
| `TRUST_PROXY`, `COOKIE_SECURE` | | set to `1` when deployed behind a proxy / on HTTPS |

Never commit `.env`; it is listed in `.gitignore`.

### Cost per plan

| Provider | Usage |
|---|---|
| SerpApi | about 5–8 searches (flights + return, hotels, 1–4 place searches) |
| Groq | 1–3 calls (understanding, itinerary, possibly one retry) |
| Tavily | 0–1 searches (fallback only) |

---

## 📁 Project structure

```text
app.py               FastAPI server: routes, session cookie, rate limit, error IDs
backend.py           LangGraph workflow: the 9 agents, edges, retry, checkpointer
plan.py              terminal client
check_setup.py       setup checker for keys and Groq structured output
core/
  llm.py             structured-output helper for Groq
  models.py          TripRequest (from the LLM) and Trip (validated)
  trip_parser.py     Trip Understanding: extraction + validation
  schedule.py        free time slots from flight times
  budget.py          stay dates and budget math
  hours.py           opening-hours parser and slot check
  places_plan.py     zones, availability table, gap filling
  itinerary.py       Itinerary Planner prompts and schema
  verifier.py        Verifier rules
  render.py          Composer: builds the final plan
tools/
  airports.py        U.S. city/state ↔ airport codes; rejects non-U.S. places
  flight_tool.py     Google Flights (SerpApi)
  hotel_tool.py      Google Hotels (SerpApi)
  places_tool.py     Google Maps places (SerpApi)
  tavily_tool.py     Tavily fallback search
evals/               12 scenarios, metrics, runner
tests/               pytest suite (fake APIs + fake LLM, includes a 300-case fuzz test)
static/, templates/  web UI (vendored marked, DOMPurify, html2pdf)
```

---

## ⚠️ Known limits

- **Not a booking site.** Prices are search snapshots and can change. Nothing is booked.
- **Flight price basis:** treated as the total for all travelers (matches observed results; not yet confirmed by a controlled test).
- **Hotel totals** marked "(est.)" are nightly rate × nights; taxes and fees may be extra. One room is assumed.
- **Budget** covers flights and hotel only; food, local transport and tickets are not priced.
- **Opening hours** are Google's weekly hours; holidays and special events are not checked.
- **Distances** are straight-line from the hotel, not travel time.
- **The Verifier** is rule-based: it blocks unsupported facts but can't fact-check every general sentence.
- **Rate limiting** is in-memory (single server process). No user accounts.
- **Scope:** the 50 states and Washington, DC. U.S. territories and international trips are not supported.

## 🗺 Roadmap

- Order each day's stops by real travel time (Google Routes API)
- Estimate food and local-transport costs
- Custom LangSmith spans for each external API call
- Shared rate limiting (Redis) for multi-instance deployment
- International destinations

## License

See [LICENSE](LICENSE).