"""Turn a U.S. city name into airport codes, deterministically. U.S. domestic only.

Scope: the 50 states + Washington, DC. U.S. territories (Puerto Rico, U.S.
Virgin Islands, Guam...) are not supported yet. Note: `airportsdata` stores
Puerto Rico airports with country "US", so we filter by state name, not country.

Rules used here, in order:
1. A valid 3-letter IATA code for a U.S. airport is used as-is.
2. A curated list of major U.S. cities (main airport first) is checked.
   Some names exist in several states (Portland OR / Portland ME): if the
   user didn't give a state, we ask instead of guessing.
3. Otherwise, `airportsdata` is searched for that city in the 50 states + DC.
   If matches are in several states and no state was given, we ask.
4. A city that only exists outside the U.S. gets a clear "domestic only" message.

The LLM never decides the airport. It only extracts the city and state the
user typed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import airportsdata
import pycountry

AIRPORTS = airportsdata.load("IATA")

# 50 states + DC, as full names (the form `airportsdata` uses in "subd").
_SUBDIVISIONS = [s for s in pycountry.subdivisions.get(country_code="US")
                 if s.type in ("State", "District")]
STATE_NAMES = {s.name for s in _SUBDIVISIONS}                       # "Oregon", "District of Columbia"
STATE_BY_ABBR = {s.code.split("-")[1]: s.name for s in _SUBDIVISIONS}  # "OR" -> "Oregon"
TERRITORIES = {"puerto rico", "pr", "u.s. virgin islands", "us virgin islands", "virgin islands",
               "guam", "american samoa", "northern mariana islands", "san juan"}

# Major U.S. cities: city -> {state: [airports, main first]}. Passenger airports only.
# The state is the CITY's state; some metro airports sit across a state line
# (EWR is in New Jersey, BWI in Maryland, CVG in Kentucky). That is intended.
MAJOR_US_CITIES: dict[str, dict[str, list[str]]] = {
    "atlanta": {"Georgia": ["ATL"]},
    "new york": {"New York": ["JFK", "LGA", "EWR"]}, "nyc": {"New York": ["JFK", "LGA", "EWR"]},
    "new york city": {"New York": ["JFK", "LGA", "EWR"]},
    "chicago": {"Illinois": ["ORD", "MDW"]},
    "los angeles": {"California": ["LAX", "BUR", "SNA", "LGB"]},
    "san francisco": {"California": ["SFO", "OAK", "SJC"]},
    "san jose": {"California": ["SJC"]}, "oakland": {"California": ["OAK"]},
    "san diego": {"California": ["SAN"]}, "sacramento": {"California": ["SMF"]},
    "washington": {"District of Columbia": ["DCA", "IAD", "BWI"]},
    "washington dc": {"District of Columbia": ["DCA", "IAD", "BWI"]},
    "washington d.c.": {"District of Columbia": ["DCA", "IAD", "BWI"]},
    "dc": {"District of Columbia": ["DCA", "IAD", "BWI"]},
    "boston": {"Massachusetts": ["BOS"]}, "seattle": {"Washington": ["SEA"]},
    "miami": {"Florida": ["MIA", "FLL"]}, "fort lauderdale": {"Florida": ["FLL"]},
    "orlando": {"Florida": ["MCO"]}, "tampa": {"Florida": ["TPA"]},
    "jacksonville": {"Florida": ["JAX"]}, "key west": {"Florida": ["EYW"]},
    "dallas": {"Texas": ["DFW", "DAL"]}, "houston": {"Texas": ["IAH", "HOU"]},
    "austin": {"Texas": ["AUS"]}, "san antonio": {"Texas": ["SAT"]}, "el paso": {"Texas": ["ELP"]},
    "denver": {"Colorado": ["DEN"]}, "las vegas": {"Nevada": ["LAS"]}, "reno": {"Nevada": ["RNO"]},
    "phoenix": {"Arizona": ["PHX"]}, "tucson": {"Arizona": ["TUS"]},
    "philadelphia": {"Pennsylvania": ["PHL"]}, "pittsburgh": {"Pennsylvania": ["PIT"]},
    "detroit": {"Michigan": ["DTW"]}, "minneapolis": {"Minnesota": ["MSP"]},
    "charlotte": {"North Carolina": ["CLT"]}, "raleigh": {"North Carolina": ["RDU"]},
    "nashville": {"Tennessee": ["BNA"]}, "memphis": {"Tennessee": ["MEM"]},
    "new orleans": {"Louisiana": ["MSY"]}, "salt lake city": {"Utah": ["SLC"]},
    "st. louis": {"Missouri": ["STL"]}, "st louis": {"Missouri": ["STL"]},
    "kansas city": {"Missouri": ["MCI"]}, "baltimore": {"Maryland": ["BWI"]},
    "cleveland": {"Ohio": ["CLE"]}, "cincinnati": {"Ohio": ["CVG"]},
    "indianapolis": {"Indiana": ["IND"]}, "milwaukee": {"Wisconsin": ["MKE"]},
    "honolulu": {"Hawaii": ["HNL"]}, "maui": {"Hawaii": ["OGG"]}, "kahului": {"Hawaii": ["OGG"]},
    "anchorage": {"Alaska": ["ANC"]}, "albuquerque": {"New Mexico": ["ABQ"]},
    "oklahoma city": {"Oklahoma": ["OKC"]}, "omaha": {"Nebraska": ["OMA"]},
    "boise": {"Idaho": ["BOI"]}, "savannah": {"Georgia": ["SAV"]},
    "richmond": {"Virginia": ["RIC"]}, "norfolk": {"Virginia": ["ORF"]},
    "buffalo": {"New York": ["BUF"]}, "hartford": {"Connecticut": ["BDL"]},
    "providence": {"Rhode Island": ["PVD"]}, "louisville": {"Kentucky": ["SDF"]},
    "birmingham": {"Alabama": ["BHM"]}, "myrtle beach": {"South Carolina": ["MYR"]},
    # Names shared by several states: the user must say which.
    "portland": {"Oregon": ["PDX"], "Maine": ["PWM"]},
    "columbus": {"Ohio": ["CMH"], "Georgia": ["CSG"]},
    "charleston": {"South Carolina": ["CHS"], "West Virginia": ["CRW"]},
    "springfield": {"Missouri": ["SGF"], "Illinois": ["SPI"]},
    "rochester": {"New York": ["ROC"], "Minnesota": ["RST"]},
    "wilmington": {"North Carolina": ["ILM"], "Delaware": ["ILG"]},
    "albany": {"New York": ["ALB"], "Georgia": ["ABY"]},
}

# Well-known cities outside the U.S., to give a clear "domestic only" message.
FOREIGN_CITIES = {
    "london", "paris", "rome", "tokyo", "dubai", "dhaka", "delhi", "new delhi", "mumbai",
    "toronto", "vancouver", "montreal", "cancun", "mexico city", "bangkok", "singapore",
    "hong kong", "seoul", "sydney", "istanbul", "amsterdam", "madrid", "barcelona", "lisbon",
    "frankfurt", "berlin", "doha", "kathmandu", "karachi", "lahore", "colombo", "bali",
}
FOREIGN_WORDS = {"uk", "united kingdom", "england", "europe", "asia", "africa", "canada",
                 "mexico", "japan", "india", "bangladesh", "china", "france", "italy",
                 "germany", "spain", "australia", "nepal", "pakistan", "thailand"}
COUNTRY_WORDS = {"usa", "us", "u.s.", "united states", "america", "the us", "the usa"}

_EXCLUDE_WORDS = ("air base", "afb", "heliport", "seaplane", "army", "naval", "nas ", "nolf",
                  "military", "training", "skyports")

DOMESTIC_ONLY = ("VoyageAI currently plans trips within the United States only "
                 "(the 50 states and Washington, DC).")


@dataclass
class AirportResolution:
    codes: list[str] = field(default_factory=list)   # airports to search, main first
    state: str | None = None                          # resolved state name, e.g. "Oregon"
    question: str = ""                                # set when we must ask the user
    note: str = ""                                    # how the codes were chosen
    city: str | None = None                           # real city to search hotels/sights in, e.g. "New York"


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip(" .,?!"))


def normalize_state(state: str | None) -> str | None:
    """'OR' / 'oregon' / 'Oregon' -> 'Oregon'. 'DC' -> 'District of Columbia'. Else None."""
    if not state:
        return None
    s = state.strip().strip(".")
    if s.upper() in STATE_BY_ABBR:
        return STATE_BY_ABBR[s.upper()]
    if s.upper() in ("D.C", "WASHINGTON DC", "WASHINGTON D.C"):
        return "District of Columbia"
    for name in STATE_NAMES:
        if name.lower() == s.lower():
            return name
    return None


def is_domestic_airport(code: str) -> bool:
    a = AIRPORTS.get(code)
    return bool(a) and a["country"] == "US" and a["subd"] in STATE_NAMES


_CITY_NAMES = {"nyc": "New York City", "new york": "New York City", "la": "Los Angeles",
               "dc": "Washington, DC", "washington dc": "Washington, DC", "washington d.c.": "Washington, DC",
               "washington": "Washington, DC", "st louis": "St. Louis", "maui": "Maui"}


def _city_title(name: str) -> str:
    """'new york' -> 'New York City', 'san francisco' -> 'San Francisco'."""
    return _CITY_NAMES.get(name, name.title())


def describe(code: str) -> str:
    """'ATL' -> 'ATL (Hartsfield/Jackson Atlanta International Airport, Atlanta, Georgia)'."""
    a = AIRPORTS.get(code)
    return f"{code} ({a['name']}, {a['city']}, {a['subd']})" if a else code


def distance_miles(a: str, b: str) -> float | None:
    """Straight-line (great-circle) distance between two airports, in miles."""
    p, q = AIRPORTS.get(a), AIRPORTS.get(b)
    if not p or not q:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (p["lat"], p["lon"], q["lat"], q["lon"]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _ask_state(place: str, states: list[str], codes_by_state: dict | None = None) -> AirportResolution:
    if codes_by_state:
        options = " or ".join(f"{place.title()}, {s} ({codes_by_state[s][0]})" for s in states)
    else:
        options = ", ".join(states[:6])
    return AirportResolution(question=f"Which {place.title()} do you mean: {options}?")


def resolve_airports(place: str | None, state: str | None = None, country: str | None = None,
                     label: str = "airport") -> AirportResolution:
    """Resolve a U.S. city (or IATA code) to airport codes, or explain what is missing."""
    name = _clean(place)
    state_name = normalize_state(state)

    if not name:
        return AirportResolution(question=f"Which U.S. city are you flying {label}?")
    if _clean(country) and _clean(country) not in COUNTRY_WORDS:
        return AirportResolution(question=f"{DOMESTIC_ONLY} '{place}, {country}' is outside the U.S. "
                                          f"Which U.S. city are you flying {label}?")
    if name in TERRITORIES or _clean(state) in TERRITORIES:
        return AirportResolution(question="U.S. territories such as Puerto Rico aren't supported yet. "
                                          f"Which city in the 50 states or DC are you flying {label}?")
    if name in COUNTRY_WORDS:
        return AirportResolution(question=f"Which U.S. city are you flying {label}?")
    if name in FOREIGN_WORDS:
        return AirportResolution(question=f"{DOMESTIC_ONLY} '{place}' is outside the U.S. "
                                          f"Which U.S. city are you flying {label}?")
    if name in FOREIGN_CITIES and not state_name:
        return AirportResolution(question=f"{DOMESTIC_ONLY} '{place.title()}' is usually a city outside the U.S. "
                                          f"If you mean a U.S. {place.title()}, which state is it in?")
    if state and not state_name:
        return AirportResolution(question=f"I don't recognise '{state}' as a U.S. state. Which state is {place} in?")

    # 1. The user typed an IATA code, e.g. "ATL" or "jfk".
    if len(name) == 3 and name.upper() in AIRPORTS and name not in MAJOR_US_CITIES:
        code = name.upper()
        if not is_domestic_airport(code):
            return AirportResolution(question=f"{DOMESTIC_ONLY} {describe(code)} is outside the 50 states/DC.")
        # The user typed an airport (e.g. JFK): searches must use its CITY, not the code.
        return AirportResolution(codes=[code], state=AIRPORTS[code]["subd"], note="airport code given by user",
                                 city=AIRPORTS[code]["city"])

    # 2. Curated major cities.
    if name in MAJOR_US_CITIES:
        by_state = MAJOR_US_CITIES[name]
        if state_name:
            if state_name in by_state:
                return AirportResolution(codes=by_state[state_name], state=state_name, note="major-city list",
                                         city=_city_title(name))
            # e.g. "Portland, Texas": not in the curated list, fall through to the full lookup.
        elif len(by_state) == 1:
            only = next(iter(by_state))
            return AirportResolution(codes=by_state[only], state=only, note="major-city list",
                                     city=_city_title(name))
        else:
            return _ask_state(name, list(by_state), by_state)

    # 3. Full U.S. airport lookup by city name.
    matches = [code for code, a in AIRPORTS.items()
               if _clean(a.get("city")) == name and is_domestic_airport(code)
               and not any(w in a["name"].lower() for w in _EXCLUDE_WORDS)]
    if state_name:
        matches = [c for c in matches if AIRPORTS[c]["subd"] == state_name]

    if not matches:
        foreign = any(_clean(a.get("city")) == name for a in AIRPORTS.values()
                      if not (a["country"] == "US" and a["subd"] in STATE_NAMES))
        if foreign:
            return AirportResolution(question=f"{DOMESTIC_ONLY} '{place}' looks like a city outside the U.S. "
                                              f"Which U.S. city are you flying {label}?")
        where = f", {state_name}" if state_name else ""
        return AirportResolution(question=f"I couldn't find an airport for '{place}{where}'. "
                                          f"What's the nearest larger city or the airport code?")

    states = sorted({AIRPORTS[c]["subd"] for c in matches})
    if len(states) > 1:
        return _ask_state(name, states)

    # Prefer international/regional airports (more likely to have airline service).
    def rank(code):
        n = AIRPORTS[code]["name"].lower()
        return ("international" not in n, "regional" not in n, code)
    matches.sort(key=rank)
    return AirportResolution(codes=matches[:3], state=states[0], note="airport database lookup",
                             city=AIRPORTS[matches[0]]["city"])


def search_airports(query: str, limit: int = 8) -> list[dict]:
    """Suggestions for the form's From/To boxes. U.S. domestic only.

    Returns items like:
      {"label": "New York City, New York (all airports: JFK, LGA, EWR)", "city": "New York City", "state": "New York"}
      {"label": "PDX · Portland International Airport, Portland, Oregon", "city": "PDX", "state": "Oregon"}
    `city` + `state` are exactly what resolve_airports() needs.
    """
    q = _clean(query)
    if len(q) < 2:
        return []
    results, seen = [], set()

    # 1. Major cities (all their airports), best for most users.
    for city, by_state in MAJOR_US_CITIES.items():
        if city in ("nyc", "la", "dc") or not city.startswith(q):
            continue
        for state, codes in by_state.items():
            title = city.title() if city != "new york" else "New York City"
            key = (title, state)
            if key in seen or (city.startswith("washington") and city != "washington dc"):
                continue
            seen.add(key)
            what = f"all airports: {', '.join(codes)}" if len(codes) > 1 else codes[0]
            if city == "washington dc":
                results.append({"label": f"Washington, DC ({what})", "city": "Washington DC", "state": state})
            else:
                results.append({"label": f"{title}, {state} ({what})", "city": title, "state": state})

    # 2. Exact airport code, then airports whose city or name matches.
    matches = []
    for code, a in AIRPORTS.items():
        if not is_domestic_airport(code) or any(w in a["name"].lower() for w in _EXCLUDE_WORDS):
            continue
        city, name = _clean(a["city"]), a["name"].lower()
        if code.lower() == q:
            rank = 0
        elif city.startswith(q):
            rank = 1
        elif len(q) >= 3 and re.search(r"\b" + re.escape(q), name):   # word start only (not "Gatlinburg")
            rank = 2
        else:
            continue
        major = "international" in name
        matches.append((rank, not major, a["city"], code))
    for rank, _, _, code in sorted(matches):
        if len(results) >= limit:
            break
        a = AIRPORTS[code]
        if any(r["city"] == code for r in results):
            continue
        results.append({"label": f"{code} · {a['name']}, {a['city']}, {a['subd']}", "city": code, "state": a["subd"]})
    return results[:limit]
