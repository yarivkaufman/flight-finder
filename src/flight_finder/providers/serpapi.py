"""Google Flights prices through SerpApi (https://serpapi.com/google-flights-api).

Cost note: each API call uses one SerpApi search credit. A one-way search costs 1.
A round trip costs 1 + `return_options` (default 3), because Google Flights prices
round trips by first listing outbound flights, then the matching return flights.
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import Any

from ..models import Cabin, Offer, SearchRequest, Segment, Slice
from .base import ProviderError, http_json

ENDPOINT = "https://serpapi.com/search.json"
ACCOUNT = "https://serpapi.com/account.json"
CABIN_CODES = {Cabin.ECONOMY: 1, Cabin.PREMIUM_ECONOMY: 2, Cabin.BUSINESS: 3, Cabin.FIRST: 4}


def _stops_param(max_stops: int | None) -> int:
    # SerpApi: 0 any, 1 nonstop only, 2 one stop or fewer, 3 two stops or fewer
    if max_stops is None or max_stops > 2:
        return 0
    return max_stops + 1


def _time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def parse_slice(option: dict[str, Any]) -> Slice:
    segs = tuple(
        Segment(
            origin=f["departure_airport"]["id"],
            destination=f["arrival_airport"]["id"],
            departure=_time(f["departure_airport"]["time"]),
            arrival=_time(f["arrival_airport"]["time"]),
            airline=f.get("airline", ""),
            flight_number=f.get("flight_number", ""),
            duration_minutes=f.get("duration"),
        )
        for f in option["flights"]
    )
    return Slice(segs, total_minutes=option.get("total_duration"))


def options(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in payload.get("best_flights", []) + payload.get("other_flights", []) if "price" in o]


def searches_left(api_key: str, fetch=http_json) -> int:
    """Searches left this month on the (shared) SerpApi plan. Free: doesn't use a search.
    The account response includes the key and email - only this field leaves here."""
    data = fetch(f"{ACCOUNT}?{urllib.parse.urlencode({'api_key': api_key})}")
    return int(data.get("total_searches_left") or 0)


class SerpApiProvider:
    name = "serpapi"

    def __init__(self, api_key: str, return_options: int = 3, fetch=http_json) -> None:
        self.api_key = api_key
        self.return_options = return_options
        self._fetch = fetch

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("SERPAPI_API_KEY is not set")
        q = {"engine": "google_flights", "api_key": self.api_key, **params}
        data = self._fetch(f"{ENDPOINT}?{urllib.parse.urlencode(q)}")
        if "error" in data and not options(data):
            raise ProviderError(f"SerpApi: {data['error']}")
        return data

    def search(self, request: SearchRequest) -> list[Offer]:
        params = {
            "departure_id": request.origin,
            "arrival_id": request.destination,
            "outbound_date": request.depart_date.isoformat(),
            "type": 1 if request.round_trip else 2,
            "currency": request.currency,
            "adults": request.adults,
            "children": request.children,
            "travel_class": CABIN_CODES[request.cabin],
            "stops": _stops_param(request.preferences.max_stops),
            "hl": "en",
        }
        if request.round_trip:
            params["return_date"] = request.return_date.isoformat()
        data = self._call(params)
        url = data.get("search_metadata", {}).get("google_flights_url")
        outbound = options(data)

        if not request.round_trip:
            return [Offer(self.name, float(o["price"]), request.currency, (parse_slice(o),), url)
                    for o in outbound]

        offers: list[Offer] = []
        cheapest_first = sorted(outbound, key=lambda o: o["price"])
        for o in cheapest_first[: self.return_options]:
            token = o.get("departure_token")
            if not token:
                continue
            ret = self._call({**params, "departure_token": token})
            ret_url = ret.get("search_metadata", {}).get("google_flights_url") or url
            for r in options(ret):
                # On the return step, price is the total for the whole round trip.
                offers.append(Offer(self.name, float(r["price"]), request.currency,
                                    (parse_slice(o), parse_slice(r)), ret_url))
        return offers
