"""FastAPI server for VoyageAI.

Routes
------
GET  /               the web page
POST /api/travel     send a message; continues the current trip conversation
GET  /api/history    messages of the current conversation (so a page reload keeps the chat)
POST /api/new-trip   start a new conversation
GET  /health         health check for deployment

Session safety
--------------
- Rate limit: each plan costs paid API calls, so each client gets
  RATE_LIMIT_PER_HOUR plans per hour (default 20). This is in-memory, so it
  resets on restart and is per server process. Use Redis for multiple servers.
- CSRF: the cookie is SameSite=Lax (browsers don't send it on cross-site POSTs)
  and /api/travel only accepts application/json, which a plain HTML form on
  another site cannot send.
- Not included: user accounts / login. Conversations are anonymous.

The conversation id (thread_id) is created by the SERVER and stored in an
httpOnly cookie. The browser cannot choose or read it from JavaScript, so one
user cannot write into another user's conversation by sending its id.
"""
from pathlib import Path
import logging
import os
import re
import threading
import time
import uuid
from collections import defaultdict, deque

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from langchain_core.messages import AIMessage, HumanMessage
from typing import Literal

from pydantic import BaseModel, Field

from tools.airports import search_airports

import backend

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("voyageai.api")

BASE_DIR = Path(__file__).resolve().parent


def _asset_version() -> str:
    """Changes whenever a file in static/ changes. Added to asset URLs (?v=...) so
    browsers fetch the new script.js/script.css instead of an old cached copy."""
    newest = max((f.stat().st_mtime for f in (BASE_DIR / "static").rglob("*") if f.is_file()), default=0)
    return str(int(newest))
COOKIE = "voyage_thread"
THREAD_RE = re.compile(r"^web_[0-9a-f]{32}$")
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"     # set to 1 when served over HTTPS
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "20"))
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"          # set to 1 behind Render/NGINX to read X-Forwarded-For


class RateLimiter:
    """Sliding one-hour window per key. Thread-safe (routes run in a thread pool)."""

    def __init__(self, limit: int, window_seconds: int = 3600):
        self.limit, self.window = limit, window_seconds
        self.hits: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                return False, int(self.window - (now - q[0])) + 1
            q.append(now)
            return True, 0


limiter = RateLimiter(RATE_LIMIT_PER_HOUR)


def _client_key(request: Request) -> str:
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        return request.headers["x-forwarded-for"].split(",")[0].strip()
    return request.client.host if request.client else "unknown"

app = FastAPI(
    title="VoyageAI",
    description="LangGraph multi-agent travel planner with verified, source-backed output",
    version="2.1.0",
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


class TravelRequest(BaseModel):
    message: str = Field(..., max_length=2000)


class TripForm(BaseModel):
    """What the form tab sends. Pydantic checks types and ranges before anything runs;
    validate_request() then checks dates and airports exactly as for chat."""
    origin_city: str = Field(..., min_length=2, max_length=80)
    origin_state: str | None = Field(None, max_length=40)
    destination_city: str = Field(..., min_length=2, max_length=80)
    destination_state: str | None = Field(None, max_length=40)
    departure_date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    return_date: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    one_way: bool = False
    nights: int | None = Field(None, ge=0, le=30)
    adults: int = Field(1, ge=1, le=9)
    children: int = Field(0, ge=0, le=8)
    budget_usd: float | None = Field(None, gt=0, le=1_000_000)
    nonstop_only: bool = False
    travel_class: Literal["economy", "premium_economy", "business", "first"] = "economy"
    min_hotel_rating: float | None = Field(None, ge=0, le=5)
    allow_rentals: bool = False
    allow_hostels: bool = False
    interests: list[str] = Field(default_factory=list, max_length=10)

    def summary(self) -> str:
        """Readable version for the chat history, e.g. 'Atlanta → Miami · Nov 9–12, 2026 · 2 adults'."""
        dates = self.departure_date + (f" to {self.return_date}" if self.return_date and not self.one_way else " (one way)")
        people = f"{self.adults} adult(s)" + (f", {self.children} child(ren)" if self.children else "")
        parts = [f"{self.origin_city} → {self.destination_city}", dates, people]
        if self.budget_usd:
            parts.append(f"budget ${self.budget_usd:,.0f}")
        if self.nonstop_only:
            parts.append("nonstop only")
        if self.interests:
            parts.append("interests: " + ", ".join(self.interests))
        return "📝 Trip form: " + " · ".join(parts)


def _thread_from_cookie(request: Request) -> str | None:
    value = request.cookies.get(COOKIE)
    return value if value and THREAD_RE.match(value) else None


def _set_cookie(response: Response, thread_id: str) -> None:
    response.set_cookie(COOKIE, thread_id, httponly=True, samesite="lax",
                        secure=COOKIE_SECURE, max_age=60 * 60 * 24 * 30)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    response = templates.TemplateResponse(request=request, name="index.html",
                                          context={"v": _asset_version()})
    response.headers["Cache-Control"] = "no-cache"    # always re-check the page itself
    return response


# `def` (not `async def`): the pipeline makes blocking network calls, so FastAPI
# runs this in a worker thread and the server stays responsive for other users.
@app.post("/api/travel")
def travel_planner(request_data: TravelRequest, request: Request):
    if not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse(status_code=415, content={"success": False, "error": "Send JSON."})
    user_message = request_data.message.strip()
    if not user_message:
        return JSONResponse(status_code=400, content={"success": False, "error": "Message cannot be empty."})

    allowed, retry_after = limiter.allow(_client_key(request))
    if not allowed:
        return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={
            "success": False,
            "error": f"Too many trip requests. Please try again in about {retry_after // 60 + 1} minutes."})

    thread_id = _thread_from_cookie(request) or f"web_{uuid.uuid4().hex}"
    try:
        result = backend.run_travel_agent(user_input=user_message, thread_id=thread_id)
    except Exception:
        error_id = uuid.uuid4().hex[:8]
        log.exception("Request failed (error id %s)", error_id)   # full details stay in server logs
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": f"Something went wrong on our side (error id {error_id}). Please try again.",
        })

    result.pop("thread_id", None)
    response = JSONResponse(content={"success": True, **result})
    _set_cookie(response, thread_id)
    return response


@app.post("/api/plan")
def plan_from_form(form: TripForm, request: Request):
    """Form tab: same pipeline as chat, but the trip details come straight from the
    form, so the LLM extraction step is skipped."""
    allowed, retry_after = limiter.allow(_client_key(request))
    if not allowed:
        return JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content={
            "success": False,
            "error": f"Too many trip requests. Please try again in about {retry_after // 60 + 1} minutes."})

    thread_id = _thread_from_cookie(request) or f"web_{uuid.uuid4().hex}"
    form_data = form.model_dump()
    form_data["interests"] = [i.strip()[:40] for i in form.interests if i.strip()]
    try:
        result = backend.run_travel_agent(user_input=form.summary(), thread_id=thread_id, form=form_data)
    except Exception:
        error_id = uuid.uuid4().hex[:8]
        log.exception("Form request failed (error id %s)", error_id)
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": f"Something went wrong on our side (error id {error_id}). Please try again.",
        })
    result.pop("thread_id", None)
    response = JSONResponse(content={"success": True, "summary": form.summary(), **result})
    _set_cookie(response, thread_id)
    return response


@app.get("/api/airports")
def airports(q: str = ""):
    """Autocomplete for the form's From/To boxes."""
    return {"results": search_airports(q[:60])}


@app.get("/api/history")
def history(request: Request):
    thread_id = _thread_from_cookie(request)
    if not thread_id:
        return {"messages": []}
    try:
        state = backend.get_graph().get_state({"configurable": {"thread_id": thread_id}})
        messages = (state.values or {}).get("messages", [])
    except Exception:
        log.exception("Could not load history")
        return {"messages": []}
    out = []
    for m in messages:
        if isinstance(m, HumanMessage):
            out.append({"role": "user", "content": m.content})
        elif isinstance(m, AIMessage):
            out.append({"role": "assistant", "content": m.content})
    return {"messages": out}


@app.post("/api/new-trip")
def new_trip():
    response = JSONResponse(content={"success": True})
    response.delete_cookie(COOKIE)
    return response


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
