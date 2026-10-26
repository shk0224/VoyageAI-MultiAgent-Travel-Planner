"""Build the final answer (Markdown) in Python, straight from the data.

The flights, hotels, budget and dates in the answer are written by this code,
not by the LLM. They reflect the API responses and calculation assumptions,
which may still omit fees or contain unverified pricing details.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from core.models import Trip
from tools.airports import AIRPORTS, distance_miles

SHORT_TRIP_MILES = 300   # below this straight-line distance, mention driving as an option


def _money(value) -> str:
    if value is None:
        return "n/a"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def _duration(minutes) -> str:
    if not isinstance(minutes, (int, float)):
        return "n/a"
    return f"{int(minutes) // 60}h {int(minutes) % 60:02d}m"


def _cell(text) -> str:
    return str(text if text not in (None, "") else "n/a").replace("|", "/").replace("\n", " ")


def _legs_summary(legs: list[dict], start_city: str | None = None, end_city: str | None = None) -> str:
    if not legs:
        return "n/a"
    airlines = sorted({l["airline"] for l in legs if l.get("airline")})
    numbers = ", ".join(str(l["flight_number"]) for l in legs if l.get("flight_number"))
    stops = [l.get("to") or "?" for l in legs]          # "?" if the provider left a code out
    stops[-1] = _airport(stops[-1], end_city)
    route = " → ".join([_airport(legs[0].get("from") or "?", start_city)] + stops)
    # <br> keeps each part on its own line (readable in narrow table cells and in the PDF)
    return (f"{route}<br>dep {legs[0].get('departs') or 'n/a'}<br>arr {legs[-1].get('arrives') or 'n/a'}<br>"
            f"{', '.join(airlines)} ({numbers})")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _airport(code: str, trip_city: str | None) -> str:
    """'FLL' -> 'FLL (Fort Lauderdale)' when the airport's city isn't the trip city."""
    a = AIRPORTS.get(code)
    if not a or not trip_city or a["city"].lower() in trip_city.lower() or trip_city.lower() in a["city"].lower():
        return code
    return f"{code} ({a['city']})"


def _place(city: str, state: str | None) -> str:
    return f"{city}, {state}" if state and state.lower() not in city.lower() else city


def trip_section(trip: Trip, stay: dict | None = None) -> str:
    end = trip.return_date or trip.check_out_date
    lines = [
        "## Your trip (as I understood it)",
        f"- **Route:** {_place(trip.origin_city_name or trip.origin_label, trip.origin_state)} "
        f"({', '.join(trip.origin_airports)}) → "
        f"{_place(trip.destination_city, trip.destination_state)} ({', '.join(trip.destination_airports)})",
        f"- **Travel dates:** {trip.departure_date:%a %b %d, %Y} → {end:%a %b %d, %Y} "
        f"({'round trip' if trip.is_round_trip else 'one way'})",
        f"- **Travellers:** {trip.adults} adult(s)" + (f", {trip.children} child(ren)" if trip.children else ""),
        f"- **Cabin:** {trip.travel_class.replace('_', ' ')}" + (" · nonstop only" if trip.nonstop_only else ""),
        f"- **Budget:** {_money(trip.budget_usd) if trip.budget_usd is not None else 'not given'}",
    ]
    prefs = []
    if trip.min_hotel_rating:
        prefs.append(f"rated {trip.min_hotel_rating:g}+")
    if not trip.allow_rentals:
        prefs.append("no vacation rentals")
    if not trip.allow_hostels:
        prefs.append("no hostels")
    if prefs:
        lines.append(f"- **Hotel preferences:** {', '.join(prefs)}")
    if trip.interests:
        lines.append(f"- **Interests:** {', '.join(trip.interests)}")
    if stay:
        lines.append(f"- **Hotel stay:** {stay['check_in']} → {stay['check_out']} ({_plural(stay['nights'], 'night')})")
        lines += [f"  - {n}" for n in stay.get("notes", [])]
    if trip.assumptions:
        lines.append("- **Assumptions (tell me if any are wrong):**")
        lines += [f"  - {a}" for a in trip.assumptions]
    miles = distance_miles(trip.origin_airports[0], trip.destination_airports[0])
    if miles is not None and miles < SHORT_TRIP_MILES:
        lines.append(f"- 🚗 **Short trip:** about {miles:,.0f} miles in a straight line between the airports "
                     f"(road distance is longer). Driving or a bus may be cheaper; those aren't priced here.")
    return "\n".join(lines)


def flights_section(flights: dict, chosen_id: str | None, trip: Trip | None = None) -> str:
    head = f"## Flights\n_Source: {flights['source']}, fetched {flights['fetched_at']} UTC_\n"
    if flights["status"] != "ok":
        return head + f"\n**No flight data:** {flights['message']}"
    origin_city = (trip.origin_city_name or trip.origin_label) if trip else None
    dest_city = trip.destination_city if trip else None
    rows = ["| ID | Price | Outbound | Stops | Time | Return |", "|---|---|---|---|---|---|"]
    for o in flights["offers"]:
        if o["return_status"] == "found":
            ret = _legs_summary(o["return_legs"], dest_city, origin_city)
        elif o["return_status"] == "not applicable":
            ret = "one way"
        else:
            ret = f"_{o['return_status']} — not used in the budget_"
        mark = " ✅" if o["id"] == chosen_id else ""
        rows.append(f"| {o['id']}{mark} | {_money(o['price_usd'])} ({o['price_covers']}) | "
                    f"{_cell(_legs_summary(o['outbound_legs'], origin_city, dest_city))} | {o['outbound_stops']} | "
                    f"{_duration(o['outbound_duration_min'])} | {_cell(ret)} |")
    note = ("\n\n✅ = used in the budget (cheapest complete option: round trips only count once "
            "the return flight is confirmed). Prices are Google Flights snapshots and can change before booking. "
            "The cheapest fares are often basic economy: bags and seat selection may cost extra and are not included.")
    if trip is not None:
        from tools.flight_tool import PRICE_BASIS_VERIFIED, PRICE_IS_TOTAL_FOR_ALL_ADULTS
        n = trip.adults + trip.children
        basis = (f"the total for all {n} traveller(s)" if PRICE_IS_TOTAL_FOR_ALL_ADULTS
                 else f"per person (the budget multiplies by {n} traveller(s))")
        status = "" if PRICE_BASIS_VERIFIED else " — this is not yet verified against the provider"
        note += f"\n\nPrices are treated as {basis}{status}."
    if chosen_id is None:
        note += "\n\n**No complete flight option to budget** (no confirmed return flight)."
    chosen = next((o for o in flights["offers"] if o["id"] == chosen_id), None)
    warns = timing_warnings(chosen) if chosen else []
    if warns:
        note += "\n\n⏰ **Flight times to know about (" + chosen_id + "):** " + " ".join(warns)
        better = [o for o in flights["offers"]
                  if o["id"] != chosen_id and o.get("return_status") in ("found", "not applicable")
                  and not timing_warnings(o)]
        if better:
            alt = min(better, key=lambda o: o["price_usd"])
            diff = alt["price_usd"] - chosen["price_usd"]
            note += (f" {alt['id']} has more comfortable times for {_money(abs(diff))} "
                     f"{'more' if diff >= 0 else 'less'}.")
    if trip is not None:
        # Search links reflect the user's route/dates; they are NOT provider price confirmations.
        outbound = trip.departure_date.strftime("%Y-%m-%d")
        inbound_date = trip.return_date or trip.check_out_date
        route = f"Flights from {trip.origin_airports[0]} to {trip.destination_airports[0]} on {outbound}"
        if trip.is_round_trip and inbound_date:
            route += f" returning {inbound_date:%Y-%m-%d}"
        search_url = "https://www.google.com/travel/flights?" + urlencode({"q": route})
        note += (f"\n\n**Verify flights:** [Search this route and travel dates on Google Flights]({search_url}). "
                 "Compare flight numbers, departure times, fares and baggage rules before booking; "
                 "the live result may differ from the retrieved snapshot.")
    return head + "\n" + "\n".join(rows) + note


def _clock(ts: str | None) -> float | None:
    t = (ts or "")[11:16]
    try:
        h, m = t.split(":")
        return int(h) + int(m) / 60
    except ValueError:
        return None


def _fmt_clock(hours: float) -> str:
    hours %= 24
    h, m = int(hours), int(round((hours - int(hours)) * 60))
    if m == 60:
        h, m = h + 1, 0
    suffix = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {suffix}"


def timing_warnings(offer: dict | None) -> list[str]:
    """Plain-English warnings for very early or very late flights (from the flight data)."""
    if not offer:
        return []
    out = []
    legs_out, legs_back = offer.get("outbound_legs") or [], offer.get("return_legs") or []
    if legs_out:
        dep, arr = _clock(legs_out[0].get("departs")), _clock(legs_out[-1].get("arrives"))
        if dep is not None and dep < 6:
            out.append(f"The outbound flight leaves at {_fmt_clock(dep)}, so you'd need to be at the airport around "
                       f"{_fmt_clock(dep - 2)}.")
        if arr is not None and (arr >= 23 or arr < 5):
            out.append(f"It lands at {_fmt_clock(arr)}; check your hotel's late check-in.")
    if legs_back:
        dep = _clock(legs_back[0].get("departs"))
        if dep is not None and dep < 7:
            out.append(f"The return flight leaves at {_fmt_clock(dep)}, so plan to leave for the airport around "
                       f"{_fmt_clock(dep - 2.75)} (about 2½–3 hours before); the last hotel night is short.")
    return out


def hotels_section(hotels: dict, chosen_id: str | None) -> str:
    head = f"## Hotels\n_Source: {hotels['source']}, fetched {hotels['fetched_at']} UTC_\n"
    if hotels["status"] == "skipped":
        return head + f"\n{hotels['message']}"
    if hotels["status"] != "ok":
        return head + f"\n**No hotel data:** {hotels['message']}"
    has_dist = any(h.get("sights_km") is not None for h in hotels["offers"])
    rows = ["| ID | Hotel | Rating | Class | Per night | Total stay |" + (" To main sights |" if has_dist else ""),
            "|---|---|---|---|---|---|" + ("---|" if has_dist else "")]
    for h in hotels["offers"]:
        hotel_url = h.get("link")
        if not isinstance(hotel_url, str) or not hotel_url.startswith(("https://", "http://")):
            hotel_url = "https://www.google.com/travel/hotels?" + urlencode({"q": str(h["name"])})
        name = f"[{_cell(h['name'])}]({hotel_url})"
        rating = (f"{h['rating']} ({h['reviews']:,} reviews)" if h.get("rating") and isinstance(h.get("reviews"), int)
                  else _cell(h.get("rating")))
        cls = f"{h['hotel_class']}-star" if h.get("hotel_class") else "n/a"
        mark = " ✅" if h["id"] == chosen_id else ""
        ad = " _(ad)_" if h.get("sponsored") else ""
        if h.get("is_hostel"):
            ad += " _(hostel: price may be per bed)_"
        if h.get("is_rental"):
            ad += " _(vacation rental: cleaning/service fees may apply)_"
        total = _money(h["total_usd"])
        if not h.get("total_basis", "").startswith("total for stay"):
            total += " (est.)"
        if isinstance(h.get("reviews"), int) and h["reviews"] < 20:
            rating += " _(few reviews: not counted)_"
        dist = ""
        if has_dist:
            km = h.get("sights_km")
            dist = f" ~{km * 0.621:.1f} mi |" if km is not None else " n/a |"
        rows.append(f"| {h['id']}{mark} | {name}{ad} | {rating} | {cls} | "
                    f"{_money(h['nightly_usd'])} | {total} |{dist}")
    note = ("\n\n✅ = hotel selected for the flight + hotel budget. Results are ranked using "
            "property type, review-adjusted rating and relative price; an affordable ranked "
            "alternative may be selected to fit the stated budget. "
            "Hotels close to the main sights are preferred; distances are straight-line from the centre of the "
            "attractions found, not travel time. Ratings from fewer than 20 reviews are not counted. "
            "Room type, availability and cancellation terms should be checked before booking. "
            "Taxes, parking, resort fees or other charges may be additional.")
    if any("(est.)" in r for r in rows):
        note += ("\n\n(est.) = Google Hotels gave only a nightly rate, so the total is nightly rate × nights; "
                 "taxes and fees may be extra.")
    return head + "\n" + "\n".join(rows) + note


def budget_section(budget: dict) -> str:
    """Show a transparent *partial* trip budget, not an all-in estimate."""
    lines = [
        "## Budget",
        "| Item | Cost |",
        "|---|---|",
        f"| Flights ({budget.get('flight_id') or 'none'}) | {_money(budget.get('flight_usd'))} |",
        f"| Hotel ({budget.get('hotel_id') or 'none'}) | {_money(budget.get('hotel_usd'))} |",
        f"| **Known flight + hotel subtotal** | **{_money(budget.get('subtotal_usd'))}** |",
    ]
    user_budget = budget.get("user_budget_usd")
    remaining = budget.get("remaining_usd")
    if user_budget is not None:
        lines.append(f"| Your stated budget | {_money(user_budget)} |")
        if remaining is not None:
            lines.append(f"| **Unallocated balance (not final savings)** | **{_money(remaining)}** |")
            per_day = budget.get("remaining_per_day_usd")
            if per_day is not None:
                lines.append(f"| Balance per hotel night (planning reference only) | {_money(per_day)} |")
    out = "\n".join(lines)
    if budget.get("missing"):
        out += "\n\n**Missing flight/hotel prices:** " + ", ".join(budget["missing"]) + "."
    basis = budget.get("hotel_total_basis")
    if basis:
        out += f"\n\n**Hotel price basis:** {basis}."
    if budget.get("over_budget"):
        out += "\n\n⚠️ **The known flight + hotel subtotal alone exceeds your budget.**"
        alt = budget.get("cheapest_option")
        if alt and alt.get("total_usd") is not None and alt["total_usd"] < (budget.get("subtotal_usd") or 0):
            hotel = f" + {alt['hotel_id']}" if alt.get("hotel_id") else ""
            fits = "falls within the stated budget for flights + hotel only" if (user_budget is not None and alt["total_usd"] <= user_budget) else "still exceeds the stated budget"
            out += (f" Cheapest eligible combination in these returned results: "
                    f"{alt.get('flight_id')}{hotel} at {_money(alt['total_usd'])}; it {fits}.")
        out += " Consider different dates, a lower-priced stay, or a higher budget."
    elif remaining is not None and remaining >= 0:
        out += ("\n\n**Budget caution:** This is the amount remaining after the quoted flight "
                "and hotel, not proof the entire trip fits your budget.")
    expenses = budget.get("unpriced_expenses") or [
        "food", "local transportation", "activities and admission fees",
        "baggage and optional flight fees", "hotel taxes or fees not included in quoted rates",
    ]
    out += "\n\n**Not included or not independently priced:** " + ", ".join(expenses) + "."
    out += " Travel insurance and other personal expenses may also apply."
    if budget.get("budget_is_complete") is False:
        out += "\n\n_This is a partial budget based on search snapshots, not a confirmed all-in quote._"
    return out


def itinerary_section(first_day: str, draft: dict | None, num_days: int, sources: list[dict],
                      arrival: str | None, departure: str | None,
                      free_slots: dict[int, list[str]] | None = None) -> str:
    if draft is None:
        return "## Day-by-day plan\nThe plan couldn't be generated right now. The flight, hotel and budget sections above still reflect the available search data, subject to their stated limitations."
    by_day = {d["day_number"]: d["activities"] for d in draft.get("days", [])}
    src_ids = {s["id"] for s in sources}
    lines = ["## Day-by-day plan"]
    for n in range(1, num_days + 1):
        day_date = date.fromisoformat(first_day) + timedelta(days=n - 1)
        extra = ""
        if n == 1 and arrival:
            extra = f" · flight arrives {arrival}"
        if n == num_days and departure:
            extra = f" · return flight departs {departure}"
        lines.append(f"\n### Day {n} — {day_date:%a %b %d}{extra}")
        acts = by_day.get(n) or []
        if free_slots is not None and not free_slots.get(n):
            lines.append("- Travel day: no activities planned because of the flight times.")
            continue
        if not acts:
            lines.append("- Free time (no verified suggestions for this day).")
        for a in acts:
            cite = f" [{a['source_id']}]" if a.get("source_id") in src_ids else ""
            lines.append(f"- **{a['time_of_day']}:** {a['description'].strip()}{cite}")
    tips = draft.get("tips") or []
    if tips:
        lines.append("\n### Tips")
        lines += [f"- {t}" for t in tips[:4]]
    return "\n".join(lines)


def sources_section(sources: list[dict]) -> str:
    if not sources:
        return ""
    return "## Sources\n" + "\n".join(f"- [{s['id']}] [{_cell(s['title'])}]({s['url']})" for s in sources)


def _maps_link(p: dict) -> str:
    from urllib.parse import quote
    url = f"https://www.google.com/maps/search/?api=1&query={quote(p['name'])}%20{p['lat']},{p['lng']}"
    if p.get("place_id"):
        url += f"&query_place_id={quote(p['place_id'])}"
    return url


def places_itinerary_section(first_day: str, draft: dict | None, num_days: int, places: dict[str, dict],
                             arrival: str | None, departure: str | None, free_slots: dict[int, list[str]],
                             availability: dict[int, dict[str, list[str]]], warnings: list[str]) -> str:
    """Day plan built from real places. Everything except the short description comes from Google Maps data."""
    from core.hours import hours_text
    if draft is None:
        draft = {"days": [], "tips": []}
    by_day = {d["day_number"]: d["activities"] for d in draft.get("days", [])}
    order = {"Morning": 0, "Afternoon": 1, "Evening": 2}
    lines = ["## Day-by-day plan",
             "_Every stop below is a real place from Google Maps, with its rating, address and that day's hours._"]
    for n in range(1, num_days + 1):
        day = date.fromisoformat(first_day) + timedelta(days=n - 1)
        extra = ""
        if n == 1 and arrival:
            extra = f" · flight arrives {arrival[11:16] or arrival}"
        if n == num_days and departure:
            extra = f" · return flight departs {departure[11:16] or departure}"
        lines.append(f"\n### Day {n} — {day:%a %b %d}{extra}")
        if not free_slots.get(n):
            lines.append("- Travel day: no activities planned because of the flight times.")
            continue
        acts = sorted(by_day.get(n) or [], key=lambda a: order.get(a.get("time_of_day"), 9))
        if not acts:
            lines.append("- Free day: every verified place we found is already in your plan, or none is open "
                         "today. Add interests in the form to get more options.")
            continue
        for a in acts:
            p = places.get(a.get("place_id"))
            if not p:
                continue
            facts = [p["type"]] if p.get("type") else []
            if p.get("rating"):
                facts.append(f"★ {p['rating']:g} ({p['reviews']:,} reviews)")
            if p.get("km_from_hotel") is not None:
                facts.append(f"~{p['km_from_hotel'] * 0.621:.1f} mi from hotel")
            hours = hours_text(p, day)
            hours_line = f"Hours on {day:%a}: {hours}" if hours else "Hours not listed: check before going"
            auto = " _(added automatically)_" if a.get("auto") else ""
            lines.append(f"- **{a['time_of_day']}: [{_cell(p['name'])}]({_maps_link(p)})** · {' · '.join(facts)}{auto}  ")
            lines.append(f"  {a['description'].strip()}  ")
            lines.append(f"  _{_cell(p.get('address') or 'Address not listed')} · {hours_line}_")
        used = {a.get("time_of_day") for a in acts}
        for slot in free_slots.get(n, []):
            if slot not in used and not (availability.get(n) or {}).get(slot):
                lines.append(f"- _{slot}: free time (no listed place is open then)._")
    if warnings:
        lines.append("\n" + "\n".join(f"⚠️ {w}" for w in warnings))
    tips = draft.get("tips") or []
    if tips:
        lines.append("\n### Tips")
        lines += [f"- {t}" for t in tips[:4]]
    return "\n".join(lines)


def places_source_section(places_result: dict) -> str:
    """A concise, traveler-facing guide to the actual source data and ways to explore it."""
    from urllib.parse import quote_plus
    if not places_result:
        return ""
    queries = [str(q).strip() for q in (places_result.get("queries") or []) if str(q).strip()]
    fetched_at = str(places_result.get("fetched_at") or "").strip()
    # A discovery search is not a citation or proof of editorial endorsement.
    destination_query = queries[0] if queries else "popular travel attractions"
    destination_url = "https://www.google.com/search?q=" + quote_plus(destination_query)
    videos_url = "https://www.youtube.com/results?search_query=" + quote_plus(destination_query + " travel guide")
    reviews_url = "https://www.tripadvisor.com/Search?q=" + quote_plus(destination_query)

    lines = [
        "## Explore & Verify Your Travel Recommendations",
        "_Your trip is built using retrieved travel listings, ratings and planning preferences. "
        "Want a second opinion? Explore the links below before deciding what to book or visit._",
        "### Discover your destination",
        "**Why these places?** The suggested stops come from Google Maps place results and are "
        "selected for your itinerary using available ratings, location and scheduling information. "
        "Click an attraction name in your day-by-day plan to view its own Google Maps listing.",
        f"**Explore more:** [Popular attractions and travel guides]({destination_url}) · "
        f"[Traveler reviews]({reviews_url}) · [Travel videos]({videos_url})",
        "### Check your flights & stay",
        "**Flights:** Review the flight options and fare details above, or "
        "[compare flights on Google Flights](https://www.google.com/travel/flights).",
        "**Hotels:** Click a hotel name in the comparison table to explore that property, or "
        "[compare stays on Google Hotels](https://www.google.com/travel/hotels).",
        "### Where the information comes from",
        "**Attractions:** [Google Maps](https://www.google.com/maps) listings retrieved via "
        "[SerpApi](https://serpapi.com/) provide names, addresses, ratings and listed hours. "
        "Flight and hotel search sources are also identified alongside their results above.",
    ]
    if fetched_at:
        lines.append("**Attraction data checked on:** " + _cell(fetched_at) + ".")
    if places_result.get("relaxed_quality_bar"):
        lines.append("_Some attractions were included using a broader rating/review threshold "
                     "because fewer highly reviewed places were available._")
    lines.append(
        "_A note on trust: These links let you explore live listings, reviews, videos and travel "
        "articles. They are not claims that a specific publisher or creator endorses a place. "
        "Prices, hours, availability and entry rules can change; confirm them before booking._"
    )
    return "\n\n".join(lines)


def checks_section(verification: dict | None, mode: str = "sources", auto_added: int = 0) -> str:
    """Summarize existing verification accurately; do not change verification logic."""
    if mode == "places":
        verification = verification or {}
        lines = [
            "**How we checked your plan:** Flight and hotel figures are taken from retrieved results; "
            "budget arithmetic is calculated in Python. Planned attractions are matched "
            "against retrieved Google Maps places, checked for duplicates and compared "
            "with listed opening hours when available. Descriptions are screened for "
            "unsupported prices, hours, transit directions and flight numbers."
        ]
        if verification.get("removed"):
            lines.append(f"**Verification result:** {verification['removed']} itinerary "
                         "item(s) were removed after failing checks.")
        if auto_added:
            lines.append(f"**To complete your itinerary:** {auto_added} stop(s) were added "
                         "from the retrieved places list and marked in the itinerary.")
        lines.append("**Before you go:** Please confirm holiday closures, date-specific events, "
                     "tickets or reservations, travel time between stops and any descriptive "
                     "claims not independently checked.")
        return "\n\n".join(lines)
    if not verification:
        return ""
    lines = [
        "**Data checks:** Flight and hotel prices are taken from retrieved search results; "
        "budget calculations use Python. Stay dates are calculated from flight timestamps. "
        "Itinerary text is screened for unsupported prices, flight numbers, opening-hours "
        "claims, invalid source IDs, duplicate descriptions and flight-blocked time slots."
    ]
    if verification.get("removed"):
        lines.append(f"**Verification result:** {verification['removed']} itinerary "
                     "item(s) were removed after failing checks.")
    lines.append("**Not independently verified:** Attraction existence, support for specific "
                 "activities in cited pages, opening on travel dates, distances and travel "
                 "times, full booking prices and other factual claims. Confirm before booking.")
    return "\n\n".join(lines)
