"""Airline offers through the Duffel API (https://duffel.com/docs/api).

Searching is free; Duffel charges only if you book. Use a test token
(duffel_test_...) to try it against Duffel's sandbox airline.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from ..models import Offer, SearchRequest, Segment, Slice
from .base import ProviderError, http_json

ENDPOINT = "https://api.duffel.com/air/offer_requests?return_offers=true"
_DURATION = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?")


def iso_minutes(value: str | None) -> int | None:
    if not value:
        return None
    m = _DURATION.fullmatch(value)
    if not m:
        return None
    d, h, mi = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def parse_offer(raw: dict[str, Any]) -> Offer:
    slices = []
    for sl in raw["slices"]:
        segs = tuple(
            Segment(
                origin=s["origin"]["iata_code"],
                destination=s["destination"]["iata_code"],
                departure=datetime.fromisoformat(s["departing_at"]),
                arrival=datetime.fromisoformat(s["arriving_at"]),
                airline=s["marketing_carrier"]["name"],
                flight_number=f'{s["marketing_carrier"].get("iata_code", "")}{s.get("marketing_carrier_flight_number", "")}',
                duration_minutes=iso_minutes(s.get("duration")),
            )
            for s in sl["segments"]
        )
        slices.append(Slice(segs, total_minutes=iso_minutes(sl.get("duration"))))
    return Offer("duffel", float(raw["total_amount"]), raw["total_currency"], tuple(slices),
                 offer_id=raw.get("id"))


class DuffelProvider:
    name = "duffel"

    def __init__(self, access_token: str, fetch=http_json) -> None:
        self.access_token = access_token
        self._fetch = fetch

    def build_body(self, request: SearchRequest) -> dict[str, Any]:
        slices = [{"origin": request.origin, "destination": request.destination,
                   "departure_date": request.depart_date.isoformat()}]
        if request.return_date:
            slices.append({"origin": request.destination, "destination": request.origin,
                           "departure_date": request.return_date.isoformat()})
        passengers = [{"type": "adult"}] * request.adults + [{"age": 10}] * request.children
        body: dict[str, Any] = {"slices": slices, "passengers": passengers,
                                "cabin_class": request.cabin.value}
        if request.preferences.max_stops is not None:
            body["max_connections"] = request.preferences.max_stops
        return {"data": body}

    def search(self, request: SearchRequest) -> list[Offer]:
        if not self.access_token:
            raise ProviderError("DUFFEL_ACCESS_TOKEN is not set")
        data = self._fetch(
            ENDPOINT,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Duffel-Version": "v2",
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
            },
            body=self.build_body(request),
        )
        offers = [parse_offer(o) for o in data.get("data", {}).get("offers", [])]
        # Duffel prices in the airline's currency; keep only the requested one when possible.
        matching = [o for o in offers if o.currency == request.currency]
        return matching or offers
