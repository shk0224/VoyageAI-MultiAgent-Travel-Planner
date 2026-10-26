"""Destination-focused sightseeing sources from Tavily.

Each result has a stable per-search source ID (S1, S2, ...).
Snippets are inspiration, not verified opening hours or admission prices.
"""
from __future__ import annotations

import inspect
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from core.models import Trip

MAX_SNIPPET = 400


def _result(status: str, message: str = "", sources=None, query: str = "") -> dict:
    return {
        "source": "Tavily web search",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "status": status,
        "message": message,
        "query": query,
        "sources": sources or [],
    }


def destination_names(label: str) -> set[str]:
    base = (label or "").lower().strip()
    bases = {base, base.replace(" city", "")}
    names = set(bases)
    for name in bases:
        names.add(name.replace(" ", "-"))
        names.add(name.replace(" ", ""))
    if base.replace(" city", "") == "new york":
        names.update({"nyc", "manhattan"})
    if base in {"washington", "washington dc", "washington d.c.", "dc"}:
        names.update({"washington", "d.c.", "dc"})
    return {name for name in names if len(name) >= 3}


def _is_airport_focused(title: str, url: str) -> bool:
    text = re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", f"{title} {url}".lower()))
    airport_terms = (
        "layover", "airport guide", "airport terminal", "airport attractions",
        "things to do at the airport", "things to do near the airport",
        "things to do in the airport", "airport activities",
        "airport sightseeing", "airport lounge", "airline lounge", "lounge review",
        "near the airport", "near jfk", "near lax", "terminal guide",
    )
    return any(term in text for term in airport_terms)


def _search(client, query: str, max_results: int) -> dict:
    """Use advanced search when the client's signature supports it.

    Fake clients in tests may only implement search(query, max_results).
    Inspect the signature rather than catching TypeError from a real API error.
    """
    method = client.search
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        # Some callable wrappers do not expose signatures. Use the common API.
        return method(query=query, max_results=max_results)

    params = signature.parameters
    supports_advanced = (
        "search_depth" in params
        or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    )
    kwargs = {"query": query, "max_results": max_results}
    if supports_advanced:
        kwargs["search_depth"] = "advanced"
    return method(**kwargs)


def search_activities(trip: Trip, client=None, max_results: int = 6) -> dict:
    place = trip.destination_search_place        # real city, never an airport code
    interests = f" Focus on {', '.join(trip.interests)}." if trip.interests else ""
    query = (
        f"Best tourist attractions and sightseeing experiences in {place}. "
        "Include major city landmarks, museums, neighborhoods, cultural attractions "
        f"and outdoor activities.{interests} "
        "Exclude airport layover guides, airport terminals, airport exhibits "
        "and airport-only activities."
    )

    if client is None:
        key = os.getenv("TAVILY_API_KEY")
        if not key:
            return _result("error", "Activity search is not configured (TAVILY_API_KEY missing).", query=query)
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=key)
        except Exception as exc:
            return _result("error", f"Unable to initialize Tavily client: {exc}", query=query)

    try:
        response = _search(client, query, max(1, max_results) + 6)
    except Exception as exc:
        return _result("error", f"Activity search failed: {exc}", query=query)

    names = destination_names(trip.destination_city)
    sources = []
    seen_urls = set()
    dropped = 0
    for item in (response or {}).get("results") or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        url = item.get("url")
        if not isinstance(url, str) or not url.strip():
            dropped += 1
            continue
        title = str(item.get("title") or url)
        content = str(item.get("content") or "")
        parsed = urlsplit(url.strip())
        normalized_url = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))
        if normalized_url in seen_urls:
            dropped += 1
            continue
        seen_urls.add(normalized_url)

        haystack = f"{title} {content} {url}".lower()
        if not any(name in haystack for name in names):
            dropped += 1
            continue
        if _is_airport_focused(title, url):
            dropped += 1
            continue
        if len(sources) >= max_results:
            break
        snippet = content.strip()
        if len(snippet) > MAX_SNIPPET:
            snippet = snippet[:MAX_SNIPPET].rsplit(" ", 1)[0] + "..."
        sources.append({
            "id": f"S{len(sources) + 1}",
            "title": title,
            "url": url,
            "snippet": snippet,
        })

    if not sources:
        result = _result("empty", f"No relevant activity sources found ({dropped} unrelated results dropped).", query=query)
    else:
        result = _result("ok", sources=sources, query=query)
    result["dropped_unrelated"] = dropped
    return result
