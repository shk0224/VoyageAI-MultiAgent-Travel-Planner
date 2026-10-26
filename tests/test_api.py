"""FastAPI tests: session cookie, history, new trip, errors. Uses the fake graph."""
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

import app as app_module
import backend
from conftest import TODAY, FakeLLM, FakeSession, FakeTavily
from test_pipeline import GOOD_DRAFT, REQUEST


@pytest.fixture
def client(monkeypatch):
    llm = FakeLLM({"TripRequest": [dict(destination_city="San Francisco"), REQUEST, REQUEST],
                   "ItineraryDraft": [GOOD_DRAFT, GOOD_DRAFT]})
    graph = backend.build_graph(backend.Services(llm=llm, http_session=FakeSession(),
                                                 tavily_client=FakeTavily(), today=lambda: TODAY),
                                MemorySaver())
    monkeypatch.setattr(backend, "_graph", graph)
    return TestClient(app_module.app)


def test_conversation_continues_with_server_cookie(client):
    first = client.post("/api/travel", json={"message": "San Francisco"})
    assert first.status_code == 200 and first.json()["questions"]
    cookie = first.cookies.get("voyage_thread")
    assert cookie and cookie.startswith("web_")
    assert "thread_id" not in first.json()              # id is never exposed to JS

    second = client.post("/api/travel", json={"message": "From Atlanta, Dec 10-17"})
    assert "## Flights" in second.json()["answer"]

    history = client.get("/api/history").json()["messages"]
    assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_other_user_cannot_see_conversation(client):
    client.post("/api/travel", json={"message": "San Francisco"})
    stranger = TestClient(app_module.app)
    assert stranger.get("/api/history").json() == {"messages": []}


def test_forged_cookie_is_ignored(client):
    client.cookies.set("voyage_thread", "../../etc")
    r = client.post("/api/travel", json={"message": "San Francisco"})
    assert r.cookies.get("voyage_thread").startswith("web_")


def test_new_trip_clears_session(client):
    client.post("/api/travel", json={"message": "San Francisco"})
    client.post("/api/new-trip")
    client.cookies.clear()
    assert client.get("/api/history").json() == {"messages": []}


def test_empty_message_rejected(client):
    assert client.post("/api/travel", json={"message": "  "}).status_code == 400


def test_internal_error_hides_details(client, monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("secret database password in traceback")
    monkeypatch.setattr(backend, "run_travel_agent", boom)
    r = client.post("/api/travel", json={"message": "San Francisco"})
    assert r.status_code == 500 and "secret" not in r.text and "error id" in r.json()["error"]


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(app_module, "limiter", app_module.RateLimiter(limit=2))
    codes = [client.post("/api/travel", json={"message": "San Francisco"}).status_code for _ in range(3)]
    assert codes[:2] == [200, 200] and codes[2] == 429


def test_non_json_post_rejected(client):
    r = client.post("/api/travel", content='{"message": "San Francisco"}', headers={"content-type": "text/plain"})
    assert r.status_code in (415, 422)


def test_assets_are_cache_busted(client):
    html = client.get("/").text
    assert "/static/script.js?v=" in html and "/static/script.css?v=" in html
    assert "{{" not in html
