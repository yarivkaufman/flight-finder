"""flight-finder: watches flight prices for planned trips and emails when they drop."""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date


def _print_offers(ranked, limit: int = 10) -> None:
    for i, r in enumerate(ranked[:limit], 1):
        o = r.offer
        tags = f"  [{', '.join(r.reasons)}]" if r.reasons else ""
        print(f"{i:>2}. {o.currency} {o.price:>8,.0f}  {o.total_minutes // 60:>2}h{o.total_minutes % 60:02d}  "
              f"{o.total_stops} stop(s)  {', '.join(sorted(o.airlines))}  via {o.provider}{tags}")
        for sl in o.slices:
            route = " → ".join([sl.segments[0].origin] + [s.destination for s in sl.segments])
            print(f"      {sl.departure:%a %d %b %H:%M} → {sl.arrival:%a %d %b %H:%M}  {route}")


def main() -> None:
    p = argparse.ArgumentParser(prog="flight-finder")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="dashboard + scheduler")
    sub.add_parser("check", help="run the daily check of every active trip once, then exit")
    sub.add_parser("weekly", help="queue (and send, if SMTP is set) the weekly summary now")
    add = sub.add_parser("add", help="add a trip described in words and check it, then exit")
    add.add_argument("text")
    probe = sub.add_parser("probe", help="one search on one source; prints ranked results")
    probe.add_argument("source", choices=["duffel", "serpapi", "fake"])
    probe.add_argument("origin")
    probe.add_argument("destination")
    probe.add_argument("depart", type=date.fromisoformat)
    probe.add_argument("ret", nargs="?", type=date.fromisoformat)
    probe.add_argument("--max-stops", type=int)
    sub.add_parser("searches-left", help="SerpApi searches left this month (doesn't use one)")
    args = p.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .config import Settings

    settings = Settings.from_env()
    if args.cmd == "serve":
        import uvicorn

        from .app import create_app
        uvicorn.run(create_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8097")))
        return
    if args.cmd == "searches-left":
        from .providers import searches_left
        if not settings.serpapi_key:
            raise SystemExit("SERPAPI_KEY is not set")
        print(f"{searches_left(settings.serpapi_key)} searches left")
        return
    if args.cmd == "probe":
        from .models import Preferences, SearchRequest
        from .providers import DuffelProvider, FakeProvider, SerpApiProvider
        from .ranking import rank
        if args.source == "duffel":
            if not settings.duffel_token:
                raise SystemExit("DUFFEL_ACCESS_TOKEN is not set")
            prov = DuffelProvider(settings.duffel_token)
        elif args.source == "serpapi":
            if not settings.serpapi_key:
                raise SystemExit("SERPAPI_KEY is not set")
            prov = SerpApiProvider(settings.serpapi_key)
        else:
            prov = FakeProvider()
        prefs = Preferences(max_stops=args.max_stops)
        offers = prov.search(SearchRequest(args.origin, args.destination, args.depart, args.ret,
                                           preferences=prefs))
        print(f"{len(offers)} offers")
        _print_offers(rank(offers, prefs))
        return

    import asyncio

    from .app import Agent
    agent = Agent(settings)
    if args.cmd == "add":
        asyncio.run(agent.add_trip(args.text))
    elif args.cmd == "weekly":
        print(asyncio.run(agent.run_weekly()))
        return
    else:
        asyncio.run(agent.run_daily())
    print(agent.last_message)
    for e in agent.db.unsent_emails():
        print(f"\n--- queued email: {e['subject']}\n{e['body']}")
