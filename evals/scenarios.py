"""Sample trips for the quality evaluation. Dates are relative to today so they never go stale.

Cover: big cities, airport-code destinations (JFK, MIA), a city name shared by two states
(Portland, Maine), Hawaii, interests, short and long trips, tight and loose budgets.
"""
SCENARIOS = [
    {"name": "NYC museums & food", "origin_city": "Atlanta", "destination_city": "New York City",
     "destination_state": "New York", "depart_in_days": 45, "nights": 3, "adults": 2, "budget_usd": 2000,
     "interests": ["Museums", "Food"]},
    {"name": "JFK code (airport bug check)", "origin_city": "ATL", "destination_city": "JFK",
     "depart_in_days": 50, "nights": 3, "adults": 1, "budget_usd": 1200, "nonstop_only": True},
    {"name": "MIA code, beaches", "origin_city": "ATL", "destination_city": "MIA", "depart_in_days": 40,
     "nights": 3, "adults": 2, "budget_usd": 800, "nonstop_only": True, "interests": ["Beaches"]},
    {"name": "Chicago long weekend", "origin_city": "Atlanta", "destination_city": "Chicago",
     "depart_in_days": 60, "nights": 3, "adults": 2, "budget_usd": 1500, "interests": ["History"]},
    {"name": "San Francisco week", "origin_city": "Atlanta", "destination_city": "San Francisco",
     "depart_in_days": 70, "nights": 6, "adults": 2, "budget_usd": 4000, "interests": ["Outdoors", "Food"]},
    {"name": "Honolulu", "origin_city": "Seattle", "destination_city": "Honolulu", "depart_in_days": 80,
     "nights": 5, "adults": 2, "budget_usd": 4000, "interests": ["Beaches"]},
    {"name": "Portland, Maine (two Portlands)", "origin_city": "Boston", "destination_city": "Portland",
     "destination_state": "Maine", "depart_in_days": 55, "nights": 2, "adults": 2, "budget_usd": 900},
    {"name": "Nashville live music", "origin_city": "Chicago", "destination_city": "Nashville",
     "depart_in_days": 35, "nights": 2, "adults": 2, "budget_usd": 1000, "interests": ["Live music"]},
    {"name": "Denver outdoors", "origin_city": "Dallas", "destination_city": "Denver", "depart_in_days": 65,
     "nights": 4, "adults": 1, "budget_usd": 1200, "interests": ["Outdoors"]},
    {"name": "Washington DC history", "origin_city": "Atlanta", "destination_city": "Washington DC",
     "depart_in_days": 75, "nights": 3, "adults": 2, "budget_usd": 1500, "interests": ["History", "Museums"]},
    {"name": "New Orleans food", "origin_city": "Atlanta", "destination_city": "New Orleans",
     "depart_in_days": 85, "nights": 3, "adults": 2, "budget_usd": 1400, "interests": ["Food", "Live music"]},
    {"name": "Las Vegas tight budget", "origin_city": "Denver", "destination_city": "Las Vegas",
     "depart_in_days": 30, "nights": 2, "adults": 2, "budget_usd": 500},
]
