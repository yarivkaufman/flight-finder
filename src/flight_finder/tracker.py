"""Checking trips: search each date pair, rank, store, and decide which emails to send."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .config import Config
from .db import DB
from .models import Cabin, Offer, Preferences, RankedOffer, SearchRequest
from .providers import LABELS, Provider, ProviderError
from .ranking import rank

log = logging.getLogger(__name__)

Pair = tuple[date, date | None]


def money(v: float | None, currency: str = "USD") -> str:
    if v is None:
        return "—"
    return f"${v:,.0f}" if currency == "USD" else f"{v:,.0f} {currency}"


def is_drop(prev: float, new: float, min_usd: float, min_pct: float) -> bool:
    """A new low counts when it beats the previous one by at least $X or Y% (either)."""
    drop = prev - new
    return drop > 0 and (drop >= min_usd or drop / prev * 100 >= min_pct)


def _csv(s: str | None) -> frozenset[str]:
    return frozenset(x.strip() for x in (s or "").split(",") if x.strip())


def date_pairs(trip: sqlite3.Row, today: date) -> list[Pair]:
    """The trip's own dates first, then the same trip length shifted up to flex_days either way
    (nearest shifts first). Departures before tomorrow are skipped."""
    dep = date.fromisoformat(trip["depart_date"])
    ret = date.fromisoformat(trip["return_date"]) if trip["return_date"] else None
    pairs: list[Pair] = [(dep, ret)]
    for d in range(1, trip["flex_days"] + 1):
        for shift in (-d, d):
            nd = dep + timedelta(days=shift)
            if nd > today:
                pairs.append((nd, ret + timedelta(days=shift) if ret else None))
    return pairs


def preferences(trip: sqlite3.Row) -> Preferences:
    return Preferences(
        max_stops=trip["max_stops"],
        max_price=trip["max_price"],
        max_duration_minutes=int(trip["max_hours"] * 60) if trip["max_hours"] else None,
        exclude_airlines=_csv(trip["exclude_airlines"]),
        prefer_airlines=_csv(trip["prefer_airlines"]),
        earliest_departure_hour=trip["depart_after"],
        latest_departure_hour=trip["depart_before"],
        value_per_hour=trip["value_per_hour"],
        value_per_stop=trip["value_per_stop"],
    )


def search_request(trip: sqlite3.Row, pair: Pair) -> SearchRequest:
    return SearchRequest(
        origin=trip["origin"], destination=trip["destination"], depart_date=pair[0],
        return_date=pair[1], adults=trip["adults"], children=trip["children"],
        cabin=Cabin(trip["cabin"]), currency=trip["currency"], preferences=preferences(trip))


def offer_row(r: RankedOffer, pair: Pair, position: int) -> dict:
    o: Offer = r.offer
    return {
        "depart_date": pair[0].isoformat(), "return_date": pair[1].isoformat() if pair[1] else None,
        "provider": o.provider, "rank": position, "price": o.price, "currency": o.currency,
        "score": round(r.score, 2), "minutes": o.total_minutes, "stops": o.total_stops,
        "airlines": ", ".join(sorted(o.airlines)), "tags": list(r.reasons),
        "booking_url": o.booking_url,
        "slices": [
            {"departure": sl.departure.isoformat(), "arrival": sl.arrival.isoformat(),
             "minutes": sl.minutes, "stops": sl.stops,
             "route": [sl.segments[0].origin] + [s.destination for s in sl.segments],
             "segments": [{"from": s.origin, "to": s.destination, "departure": s.departure.isoformat(),
                           "arrival": s.arrival.isoformat(), "airline": s.airline,
                           "flight": s.flight_number} for s in sl.segments]}
            for sl in o.slices
        ],
    }


@dataclass
class CheckResult:
    check_id: int | None = None
    offers: int = 0
    exact_low: float | None = None
    emails: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class Tracker:
    """kind: 'daily' (Duffel, all date pairs), 'google' (Google Flights, trip dates only),
    'new' (both: a trip's first check)."""

    def __init__(self, cfg: Config, db: DB, providers: dict[str, Provider],
                 google_left: Callable[[], int] | None = None):
        self.cfg, self.db, self.providers = cfg, db, providers
        self.google_left = google_left

    # --- searching ------------------------------------------------------------------
    def _google_allowed(self) -> str | None:
        """None if a Google search may run, else why not."""
        if "serpapi" not in self.providers:
            return "Google Flights is off (no SERPAPI_KEY)"
        if self.google_left is None:
            return None
        try:
            left = self.google_left()
        except Exception as e:
            return f"couldn't read the SerpApi balance: {e}"
        cost = 1 + self.cfg.google.return_options
        if left - cost < self.cfg.google.reserve:
            return (f"skipped to protect the shared SerpApi plan: {left} searches left, "
                    f"keeping {self.cfg.google.reserve} in reserve")
        return None

    def plan(self, trip: sqlite3.Row, kind: str, today: date) -> list[tuple[str, Pair]]:
        pairs = date_pairs(trip, today)
        jobs: list[tuple[str, Pair]] = []
        daily = "duffel" if "duffel" in self.providers else ("fake" if "fake" in self.providers else None)
        if kind in ("daily", "new") and daily:
            jobs += [(daily, p) for p in pairs]
        if kind in ("google", "new"):
            jobs.append(("serpapi", pairs[0]))
        return jobs

    def check_trip(self, trip: sqlite3.Row, kind: str = "daily", today: date | None = None) -> CheckResult:
        today = today or datetime.now(UTC).date()
        res = CheckResult()
        if date.fromisoformat(trip["depart_date"]) <= today:
            self.db.update_trip(trip["id"], active=0, last_error="departure date has passed")
            res.errors.append("departure date has passed - stopped watching")
            return res
        jobs = self.plan(trip, kind, today)
        if not jobs:
            res.errors.append("no flight source configured (set DUFFEL_ACCESS_TOKEN)")
            self.db.update_trip(trip["id"], last_error=res.errors[0])
            return res

        by_pair: dict[Pair, list[Offer]] = {}
        used: list[str] = []
        google_skip = None
        for source, pair in jobs:
            if source == "serpapi":
                google_skip = self._google_allowed()
                if google_skip:
                    res.errors.append(f"serpapi: {google_skip}")
                    continue
            try:
                found = self.providers[source].search(search_request(trip, pair))
            except (ProviderError, ValueError) as e:
                res.errors.append(f"{source}: {pair[0]}: {e}")
                continue
            if source not in used:
                used.append(source)
            by_pair.setdefault(pair, []).extend(found)

        prefs = preferences(trip)
        exact = date_pairs(trip, today)[0]
        rows: list[dict] = []
        flex: tuple[float, str, str | None] | None = None
        other_currency = set()
        for pair, offers in by_pair.items():
            same = [o for o in offers if o.currency == trip["currency"]]
            other_currency |= {o.currency for o in offers if o.currency != trip["currency"]}
            ranked = rank(same, prefs, limit=self.cfg.results_per_check)
            rows += [offer_row(r, pair, i) for i, r in enumerate(ranked, 1)]
            if not ranked:
                continue
            low = min(r.offer.price for r in ranked)
            if pair == exact:
                res.exact_low = low
            elif flex is None or low < flex[0]:
                flex = (low, pair[0].isoformat(), pair[1].isoformat() if pair[1] else None)

        if other_currency and not rows:
            res.errors.append(f"prices came back only in {', '.join(sorted(other_currency))}, "
                              f"not {trip['currency']}")
        res.offers = len(rows)
        if used or res.errors:
            res.check_id = self.db.add_check(trip["id"], kind, used, res.exact_low, flex, rows,
                                             res.errors)
        stamp = {"last_check_at": datetime.now(UTC).isoformat(timespec="seconds"),
                 "last_error": "; ".join(res.errors)[:500] or None}
        if "serpapi" in used:
            stamp["last_google_at"] = stamp["last_check_at"]
        self.db.update_trip(trip["id"], **stamp)
        if used:
            res.emails = self.price_emails(self.db.trip(trip["id"]), res.exact_low, flex, res.check_id)
        return res

    # --- emails -----------------------------------------------------------------------
    def trip_link(self, trip_id: int) -> str:
        return f"{self.cfg.web_base_url.rstrip('/')}/trips/{trip_id}"

    def _top_lines(self, check_id: int | None, trip: sqlite3.Row, n: int = 3) -> list[str]:
        if check_id is None:
            return []
        exact_dep = trip["depart_date"]
        rows = [r for r in self.db.offers(check_id) if r["depart_date"] == exact_dep][:n]
        lines = []
        for r in rows:
            slices = json.loads(r["slices"])
            legs = "; ".join(f"{'→'.join(s['route'])} {s['departure'][:16].replace('T', ' ')}"
                             for s in slices)
            stops = "nonstop" if r["stops"] == 0 else f"{r['stops']} stop(s)"
            lines.append(f"  {money(r['price'], r['currency'])}  {r['minutes'] // 60}h{r['minutes'] % 60:02d}  "
                         f"{stops}  {r['airlines']}  ({LABELS.get(r['provider'], r['provider'])})\n"
                         f"     {legs}" + (f"\n     Book: {r['booking_url']}" if r["booking_url"] else ""))
        return lines

    def price_emails(self, trip: sqlite3.Row, low: float | None,
                     flex: tuple[float, str, str | None] | None, check_id: int | None) -> list[int]:
        if low is None:
            return []
        e = self.cfg.email
        cur = trip["currency"]
        prev = trip["emailed_low"]
        reasons: list[str] = []
        kind = "drop"
        if prev is None:
            reasons.append(f"Now watching this trip. Best price today: {money(low, cur)}.")
            kind = "start"
        elif is_drop(prev, low, e.min_drop_usd, e.min_drop_pct):
            reasons.append(f"Price dropped to {money(low, cur)} (was {money(prev, cur)}, "
                           f"{(prev - low) / prev * 100:.0f}% less).")
        target = trip["target_price"]
        hit = bool(target is not None and low <= target)
        if hit and not trip["target_hit"]:
            reasons.append(f"Reached your target price of {money(target, cur)}.")
            kind = "target" if kind != "start" else kind
        if not reasons:
            if target is not None and not hit and trip["target_hit"]:
                self.db.update_trip(trip["id"], target_hit=0)  # back above target: re-arm
            return []

        self.db.update_trip(trip["id"], emailed_low=low if prev is None else min(prev, low),
                            target_hit=int(hit))
        if flex and is_drop(low, flex[0], e.min_drop_usd, e.min_drop_pct):
            when = flex[1] + (f" – {flex[2]}" if flex[2] else "")
            reasons.append(f"Nearby dates are cheaper: {money(flex[0], cur)} for {when}.")
        subject = {"start": "Watching", "drop": "Price drop", "target": "Target price"}[kind]
        subject = f"✈ {subject}: {trip['name']} {money(low, cur)}"
        body = "\n".join([trip["name"], "", *reasons, "", "Best options on your dates:",
                          *self._top_lines(check_id, trip), "", f"Details: {self.trip_link(trip['id'])}"])
        return [self.db.add_email(trip["id"], kind, subject, body)]

    def weekly_summary(self, today: date | None = None) -> int | None:
        trips = self.db.trips(active=True)
        if not trips:
            return None
        week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
        lines = []
        for t in trips:
            cur = t["currency"]
            last = self.db.latest_check(t["id"])
            then = self.db.low_since(t["id"], week_ago)
            now_low = last["exact_low"] if last else None
            change = ""
            if now_low is not None and then is not None and then["exact_low"]:
                d = now_low - then["exact_low"]
                change = " (no change this week)" if abs(d) < 1 else \
                    f" ({'down' if d < 0 else 'up'} {money(abs(d), cur)} this week)"
            lines.append(f"• {t['name']}: {money(now_low, cur)}{change}")
            if last and last["flex_low"] is not None and now_low is not None and last["flex_low"] < now_low:
                when = last["flex_depart"] + (f" – {last['flex_return']}" if last["flex_return"] else "")
                lines.append(f"    nearby dates: {money(last['flex_low'], cur)} for {when}")
            if t["target_price"]:
                lines.append(f"    target: {money(t['target_price'], cur)}")
            for src in ("duffel",):
                n = self.db.source_fail_streak(t["id"], src)
                if n >= self.cfg.email.failing_days:
                    lines.append(f"    ⚠ {LABELS[src]} failed the last {n} daily checks")
            lines.append(f"    {self.trip_link(t['id'])}")
        subject = f"✈ Weekly flight prices: {len(trips)} trip{'s' if len(trips) != 1 else ''}"
        body = "\n".join(["Your watched trips this week:", "", *lines])
        return self.db.add_email(None, "summary", subject, body)
