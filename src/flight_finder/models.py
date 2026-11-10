"""Core data types shared by providers, ranking and the CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum


class Cabin(str, Enum):
    ECONOMY = "economy"
    PREMIUM_ECONOMY = "premium_economy"
    BUSINESS = "business"
    FIRST = "first"


@dataclass(frozen=True)
class Preferences:
    """What the traveller cares about. Used for filtering and scoring."""

    max_stops: int | None = None
    max_price: float | None = None
    max_duration_minutes: int | None = None
    max_layover_minutes: int | None = None
    exclude_airlines: frozenset[str] = frozenset()
    prefer_airlines: frozenset[str] = frozenset()
    earliest_departure_hour: int | None = None  # local time, 0-23
    latest_departure_hour: int | None = None
    # How much money one hour of travel time / one stop is worth to you.
    value_per_hour: float = 15.0
    value_per_stop: float = 40.0


@dataclass(frozen=True)
class SearchRequest:
    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    adults: int = 1
    children: int = 0
    cabin: Cabin = Cabin.ECONOMY
    currency: str = "USD"
    preferences: Preferences = field(default_factory=Preferences)

    def __post_init__(self) -> None:
        object.__setattr__(self, "origin", self.origin.upper())
        object.__setattr__(self, "destination", self.destination.upper())
        if self.return_date and self.return_date < self.depart_date:
            raise ValueError("return date is before departure date")
        if self.adults < 1:
            raise ValueError("at least one adult is required")

    @property
    def round_trip(self) -> bool:
        return self.return_date is not None


@dataclass(frozen=True)
class Segment:
    """One flight leg, e.g. TLV -> ATH on a single aircraft."""

    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    airline: str  # display name or IATA code
    flight_number: str = ""
    duration_minutes: int | None = None

    @property
    def minutes(self) -> int:
        if self.duration_minutes is not None:
            return self.duration_minutes
        return int((self.arrival - self.departure).total_seconds() // 60)


@dataclass(frozen=True)
class Slice:
    """One direction of a trip (outbound or return): one or more segments."""

    segments: tuple[Segment, ...]
    total_minutes: int | None = None

    @property
    def stops(self) -> int:
        return max(len(self.segments) - 1, 0)

    @property
    def departure(self) -> datetime:
        return self.segments[0].departure

    @property
    def arrival(self) -> datetime:
        return self.segments[-1].arrival

    @property
    def minutes(self) -> int:
        if self.total_minutes is not None:
            return self.total_minutes
        return int((self.arrival - self.departure).total_seconds() // 60)

    @property
    def layovers_minutes(self) -> list[int]:
        return [
            int((b.departure - a.arrival).total_seconds() // 60)
            for a, b in zip(self.segments, self.segments[1:])
        ]

    @property
    def airlines(self) -> set[str]:
        return {s.airline for s in self.segments}


@dataclass(frozen=True)
class Offer:
    """A priced itinerary returned by a provider."""

    provider: str
    price: float
    currency: str
    slices: tuple[Slice, ...]
    booking_url: str | None = None
    offer_id: str | None = None

    @property
    def total_minutes(self) -> int:
        return sum(s.minutes for s in self.slices)

    @property
    def total_stops(self) -> int:
        return sum(s.stops for s in self.slices)

    @property
    def max_stops(self) -> int:
        return max((s.stops for s in self.slices), default=0)

    @property
    def airlines(self) -> set[str]:
        out: set[str] = set()
        for s in self.slices:
            out |= s.airlines
        return out

    def signature(self) -> tuple:
        """Identity of the itinerary regardless of provider, for de-duplication."""
        return tuple(
            (seg.origin, seg.destination, seg.departure.isoformat(), seg.flight_number or seg.airline)
            for sl in self.slices
            for seg in sl.segments
        )


@dataclass(frozen=True)
class RankedOffer:
    offer: Offer
    score: float
    reasons: tuple[str, ...] = ()
