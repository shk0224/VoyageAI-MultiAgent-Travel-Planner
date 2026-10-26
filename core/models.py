"""Data shapes used across the app.

`TripRequest` is what the LLM fills in from the user's words. Every field is
optional because the user may not have said it yet. Python then checks it
(see core/trip_parser.py) before anything is searched.

`Trip` is the validated, ready-to-search version: real dates, real airport
codes, real numbers. VoyageAI is U.S. domestic only (50 states + DC).
"""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


class TripRequest(BaseModel):
    """Travel details exactly as the user stated them. Never invent values."""

    origin_city: Optional[str] = Field(None, description="City or airport code the user departs from, e.g. 'Atlanta' or 'JFK'.")
    origin_state: Optional[str] = Field(None, description="U.S. state of the origin city (e.g. 'Oregon' or 'OR'), only if the user said it or the city is unambiguous.")
    origin_country: Optional[str] = Field(None, description="Country of the origin, ONLY if the user named a country outside the U.S.")
    destination_city: Optional[str] = Field(None, description="City or airport code the user is travelling to.")
    destination_state: Optional[str] = Field(None, description="U.S. state of the destination city, only if the user said it or the city is unambiguous.")
    destination_country: Optional[str] = Field(None, description="Country of the destination, ONLY if the user named a country outside the U.S.")
    departure_date: Optional[str] = Field(None, description="Departure date as YYYY-MM-DD. Resolve relative dates ('next Friday') using today's date.")
    return_date: Optional[str] = Field(None, description="Return date as YYYY-MM-DD, if the user gave one.")
    nights: Optional[int] = Field(None, description="Number of nights, if the user gave a trip length instead of a return date (e.g. '5-day trip' = 4 nights).")
    one_way: Optional[bool] = Field(None, description="True only if the user explicitly asked for a one-way trip.")
    adults: Optional[int] = Field(None, description="Number of adult travellers, if stated. 'me and my wife' = 2.")
    children: Optional[int] = Field(None, description="Number of children, if stated.")
    budget_usd: Optional[float] = Field(None, description="Total trip budget in US dollars, if stated.")
    nonstop_only: Optional[bool] = Field(None, description="True if the user asked for nonstop/direct flights.")
    travel_class: Optional[Literal["economy", "premium_economy", "business", "first"]] = Field(None, description="Cabin class, if stated.")
    interests: list[str] = Field(default_factory=list, description="Activities or interests the user mentioned, e.g. 'food', 'museums'.")
    min_hotel_rating: Optional[float] = Field(None, description="Minimum hotel rating (e.g. 4.0), only if the user asked for one.")
    allow_rentals: Optional[bool] = Field(None, description="False if the user wants hotels only / no vacation rentals.")
    allow_hostels: Optional[bool] = Field(None, description="False if the user doesn't want hostels.")


class Trip(BaseModel):
    """A validated trip. Every value here has been checked in Python."""

    origin_label: str
    destination_label: str
    origin_state: Optional[str] = None
    destination_state: Optional[str] = None
    # The real city used for hotel/attraction searches. If the user picked an
    # airport code (JFK), this is the airport's city ("New York"), not the code.
    origin_city_name: Optional[str] = None
    destination_city_name: Optional[str] = None
    origin_airports: list[str]
    destination_airports: list[str]
    departure_date: date
    return_date: Optional[date]          # None means one-way
    nights: int                          # hotel nights (0 if one-way with no stay)
    adults: int
    children: int
    budget_usd: Optional[float]
    nonstop_only: bool
    travel_class: str
    interests: list[str]
    assumptions: list[str]               # defaults we applied, shown to the user
    min_hotel_rating: Optional[float] = None
    allow_rentals: bool = True           # rentals are shown (ranked after hotels) unless excluded
    allow_hostels: bool = True

    @property
    def destination_city(self) -> str:
        """Real destination city (never an airport code)."""
        return self.destination_city_name or self.destination_label

    @property
    def destination_search_place(self) -> str:
        """Text used in hotel/attraction searches, e.g. 'New York, NY', 'Miami, FL', 'Washington, DC'."""
        from tools.airports import STATE_BY_ABBR      # local import: models stays light
        city, state = self.destination_city, self.destination_state
        if not state:
            return city
        if state == "District of Columbia":
            return "Washington, DC" if "washington" in city.lower() else f"{city}, DC"
        abbr = next((a for a, name in STATE_BY_ABBR.items() if name == state), None)
        return f"{city}, {abbr}" if abbr else f"{city}, {state}"

    @property
    def is_round_trip(self) -> bool:
        return self.return_date is not None

    @property
    def check_out_date(self) -> date:
        from datetime import timedelta
        return self.departure_date + timedelta(days=self.nights)
