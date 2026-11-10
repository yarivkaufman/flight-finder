"""Deterministic offline provider for demos and tests. No API key needed."""

from __future__ import annotations

import random
import zlib
from datetime import datetime, timedelta

from ..models import Offer, SearchRequest, Segment, Slice

AIRLINES = ["El Al", "Aegean", "Lufthansa", "Turkish Airlines", "Wizz Air", "United"]
HUBS = ["ATH", "FRA", "IST", "VIE", "ZRH"]


class FakeProvider:
    name = "fake"

    def __init__(self, count: int = 12, seed: int | None = None, name: str = "fake") -> None:
        self.name = name
        self.count = count
        self.seed = seed

    def _slice(self, rng: random.Random, origin: str, dest: str, day) -> Slice:
        stops = rng.choice([0, 0, 1, 1, 2])
        dep = datetime.combine(day, datetime.min.time()) + timedelta(hours=rng.randint(5, 22))
        points = [origin] + rng.sample(HUBS, stops) + [dest]
        segs = []
        t = dep
        for a, b in zip(points, points[1:]):
            dur = rng.randint(90, 600)
            arr = t + timedelta(minutes=dur)
            airline = rng.choice(AIRLINES)
            segs.append(Segment(a, b, t, arr, airline, f"{airline[:2].upper()}{rng.randint(100, 999)}", dur))
            t = arr + timedelta(minutes=rng.randint(60, 300))
        return Slice(tuple(segs))

    def search(self, request: SearchRequest) -> list[Offer]:
        key = f"{self.name}{request.origin}{request.destination}{request.depart_date}{request.return_date}"
        seed = self.seed if self.seed is not None else zlib.crc32(key.encode())
        rng = random.Random(seed)
        offers = []
        for i in range(self.count):
            slices = [self._slice(rng, request.origin, request.destination, request.depart_date)]
            if request.return_date:
                slices.append(self._slice(rng, request.destination, request.origin, request.return_date))
            stops = sum(s.stops for s in slices)
            base = rng.randint(250, 900) * (2 if request.return_date else 1)
            price = round(base * (1.25 if stops == 0 else 1.0) * request.adults, 2)
            offers.append(Offer(self.name, price, request.currency, tuple(slices),
                                booking_url="https://www.google.com/travel/flights" if self.name == "serpapi" else None,
                                offer_id=f"fake-{i}"))
        return offers
