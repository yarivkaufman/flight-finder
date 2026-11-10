"""Filter offers by preferences and rank them by "best value".

The value score is an effective cost in the offer's currency:

    score = price + hours_travelled * value_per_hour + stops * value_per_stop

Lower is better. So with the defaults (15/hour, 40/stop), a nonstop flight
that costs 50 more than a 1-stop flight that is 3 hours longer wins.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import Offer, Preferences, RankedOffer

PREFERRED_AIRLINE_BONUS = 0.03  # 3% of price off the score


def rejection_reason(offer: Offer, prefs: Preferences) -> str | None:
    """Return why an offer fails the hard preferences, or None if it passes."""
    if prefs.max_stops is not None and offer.max_stops > prefs.max_stops:
        return f"{offer.max_stops} stops > max {prefs.max_stops}"
    if prefs.max_price is not None and offer.price > prefs.max_price:
        return f"price {offer.price:.0f} > max {prefs.max_price:.0f}"
    if prefs.max_duration_minutes is not None:
        longest = max(s.minutes for s in offer.slices)
        if longest > prefs.max_duration_minutes:
            return "too long"
    if prefs.max_layover_minutes is not None:
        for s in offer.slices:
            if any(l > prefs.max_layover_minutes for l in s.layovers_minutes):
                return "layover too long"
    excluded = {a.lower() for a in prefs.exclude_airlines}
    if excluded and any(a.lower() in excluded for a in offer.airlines):
        return "excluded airline"
    hour = offer.slices[0].departure.hour
    if prefs.earliest_departure_hour is not None and hour < prefs.earliest_departure_hour:
        return "departs too early"
    if prefs.latest_departure_hour is not None and hour > prefs.latest_departure_hour:
        return "departs too late"
    return None


def score(offer: Offer, prefs: Preferences) -> tuple[float, tuple[str, ...]]:
    hours = offer.total_minutes / 60
    value = offer.price + hours * prefs.value_per_hour + offer.total_stops * prefs.value_per_stop
    reasons: list[str] = []
    preferred = {a.lower() for a in prefs.prefer_airlines}
    if preferred and any(a.lower() in preferred for a in offer.airlines):
        value -= offer.price * PREFERRED_AIRLINE_BONUS
        reasons.append("preferred airline")
    if offer.total_stops == 0:
        reasons.append("nonstop")
    return value, tuple(reasons)


def dedupe(offers: Iterable[Offer]) -> list[Offer]:
    """Keep the cheapest offer for each identical itinerary (across providers)."""
    best: dict[tuple, Offer] = {}
    for o in offers:
        key = o.signature()
        if key not in best or o.price < best[key].price:
            best[key] = o
    return list(best.values())


def rank(offers: Iterable[Offer], prefs: Preferences, limit: int | None = None) -> list[RankedOffer]:
    kept = [o for o in dedupe(offers) if rejection_reason(o, prefs) is None]
    ranked = []
    for o in kept:
        s, reasons = score(o, prefs)
        ranked.append(RankedOffer(offer=o, score=s, reasons=reasons))
    ranked.sort(key=lambda r: (r.score, r.offer.price))
    if ranked:
        cheapest = min(ranked, key=lambda r: r.offer.price)
        fastest = min(ranked, key=lambda r: r.offer.total_minutes)
        ranked = [
            RankedOffer(
                r.offer,
                r.score,
                r.reasons
                + (("cheapest",) if r is cheapest else ())
                + (("fastest",) if r is fastest else ()),
            )
            for r in ranked
        ]
    return ranked[:limit] if limit else ranked
