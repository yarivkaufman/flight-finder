"""The long-running service: dashboard + scheduler in one asyncio process."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .chart import history_svg
from .config import Config, Settings, load_config
from .db import DB, local_time
from .emailer import Emailer
from .parser import TripParser, TripSpec
from .providers import LABELS, DuffelProvider, FakeProvider, Provider, SerpApiProvider, searches_left
from .tracker import Tracker, money

log = logging.getLogger(__name__)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["money"] = money
templates.env.filters["fromjson"] = lambda s: json.loads(s) if s else []
templates.env.filters["hm"] = lambda m: f"{m // 60}h{m % 60:02d}m" if m is not None else ""
templates.env.filters["clock"] = lambda s: datetime.fromisoformat(s).strftime("%a %b %-d, %H:%M")
templates.env.globals["source_labels"] = LABELS


def build_providers(cfg: Config, settings: Settings) -> dict[str, Provider]:
    if settings.demo:
        return {"fake": FakeProvider(), "serpapi": FakeProvider(name="serpapi")}
    out: dict[str, Provider] = {}
    if settings.duffel_token:
        out["duffel"] = DuffelProvider(settings.duffel_token)
    if settings.serpapi_key:
        out["serpapi"] = SerpApiProvider(settings.serpapi_key, return_options=cfg.google.return_options)
    return out


def spec_fields(spec: TripSpec, cfg: Config, request: str | None) -> dict:
    return dict(
        name=spec.name, request=request, origin=spec.origin.upper(), destination=spec.destination.upper(),
        depart_date=spec.depart_date.isoformat(),
        return_date=spec.return_date.isoformat() if spec.return_date else None,
        adults=spec.adults, children=spec.children, cabin=spec.cabin, currency=cfg.defaults.currency,
        flex_days=spec.flex_days, max_stops=spec.max_stops, max_price=spec.max_price,
        max_hours=spec.max_hours, exclude_airlines=",".join(spec.exclude_airlines),
        prefer_airlines=",".join(spec.prefer_airlines), depart_after=spec.depart_after,
        depart_before=spec.depart_before, value_per_hour=cfg.defaults.value_per_hour,
        value_per_stop=cfg.defaults.value_per_stop, target_price=spec.target_price,
        notes=spec.notes or None)


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = DB(settings.data_dir / "flights.db")
        self.providers = build_providers(self.config(), settings)
        self.emailer = Emailer(settings, self.db)
        self.lock = asyncio.Lock()
        self.busy: list[str] = []  # what is running now (dashboard notice)
        self.last_message = ""

    def config(self) -> Config:
        return load_config(self.settings.config_path)  # re-read each run: edit without restart

    def tracker(self) -> Tracker:
        google_left = None
        if self.settings.serpapi_key and not self.settings.demo:
            google_left = lambda: searches_left(self.settings.serpapi_key)  # noqa: E731
        return Tracker(self.config(), self.db, self.providers, google_left)

    def parser(self) -> TripParser:
        cfg = self.config()
        return TripParser(cfg.parsing, cfg.defaults, self.settings.anthropic_configured)

    def today(self) -> date:
        return datetime.now(ZoneInfo(self.config().timezone)).date()

    async def _send(self) -> int:
        return await asyncio.to_thread(self.emailer.send_pending)

    async def run_daily(self) -> str:
        """Check every active trip (Duffel, all nearby dates)."""
        if self.lock.locked():
            return "A check is already running."
        async with self.lock:
            trips = self.db.trips(active=True)
            run_id = self.db.start_run("daily")
            offers = emails = 0
            errors: list[str] = []
            self.busy.append("daily check")
            try:
                tracker = self.tracker()
                for t in trips:
                    res = await asyncio.to_thread(tracker.check_trip, t, "daily", self.today())
                    offers += res.offers
                    emails += len(res.emails)
                    errors += [f"{t['name']}: {e}" for e in res.errors]
                await self._send()
                msg = f"Daily check done: {len(trips)} trips, {offers} offers, {emails} emails"
            except Exception as e:
                log.exception("daily check failed")
                msg = f"Daily check failed: {e}"
                errors.append(msg)
            finally:
                self.busy.remove("daily check")
            self.db.finish_run(run_id, len(trips), offers, emails, errors)
            self.last_message = msg + (f", {len(errors)} problems" if errors else "")
            return self.last_message

    async def run_weekly(self) -> str:
        async with self.lock:
            email_id = await asyncio.to_thread(self.tracker().weekly_summary)
            sent = await self._send()
            return "Weekly summary queued" + (" and sent" if sent else "") if email_id else "No trips"

    async def add_trip(self, text: str | None, fields: dict | None = None) -> None:
        label = (text or "new trip")[:60]
        self.busy.append(f"adding {label}")
        try:
            cfg = self.config()
            parser = self.parser()
            try:
                if text:
                    spec = await asyncio.to_thread(parser.parse, text, self.today())
                else:
                    spec = parser.from_fields(**fields)
            except Exception as e:
                log.exception("parsing %r failed", text)
                self.last_message = f"Couldn't add that trip: {str(e)[:200]}"
                return
            trip_id = self.db.add_trip(**spec_fields(spec, cfg, text))
            async with self.lock:
                res = await asyncio.to_thread(self.tracker().check_trip, self.db.trip(trip_id), "new",
                                              self.today())
                await self._send()
            self.last_message = (f"Added {spec.name}: {res.offers} offers found"
                                 + (f" ({'; '.join(res.errors)[:200]})" if res.errors else ""))
        finally:
            self.busy.remove(f"adding {label}")

    async def check_one(self, trip_id: int, kind: str) -> None:
        trip = self.db.trip(trip_id)
        if trip is None:
            return
        self.busy.append(f"checking {trip['name']}")
        try:
            async with self.lock:
                res = await asyncio.to_thread(self.tracker().check_trip, trip, kind, self.today())
                await self._send()
            self.last_message = (f"Checked {trip['name']}: {res.offers} offers"
                                 + (f" ({'; '.join(res.errors)[:200]})" if res.errors else ""))
        finally:
            self.busy.remove(f"checking {trip['name']}")


def _num(v: str, kind=float):
    v = (v or "").replace("$", "").replace(",", "").strip()
    if not v:
        return None
    try:
        return kind(float(v)) if kind is int else kind(v)
    except ValueError:
        raise HTTPException(400, f"not a number: {v!r}")


def _latest_offers(agent: Agent, trip_id: int) -> dict:
    """Latest results per source: {'daily': (check, offers), 'google': (check, offers)}."""
    out = {}
    for kind_label, kinds in (("duffel", ("daily", "new")), ("google", ("google", "new"))):
        for row in agent.db.checks(trip_id, 30):
            src = "serpapi" if kind_label == "google" else None
            offers = [o for o in agent.db.offers(row["id"])
                      if (o["provider"] == "serpapi") == (src == "serpapi")]
            if row["kind"] in kinds and offers:
                out[kind_label] = (row, offers)
                break
    return out


def create_app(settings: Settings | None = None) -> FastAPI:
    agent = Agent(settings or Settings.from_env())
    tz = agent.config().timezone
    templates.env.filters["localtime"] = lambda s: local_time(s, tz)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cfg = agent.config()
        sched = AsyncIOScheduler(timezone=cfg.timezone)
        h, m = cfg.check_at.split(":")
        sched.add_job(agent.run_daily, CronTrigger(hour=int(h), minute=int(m), timezone=cfg.timezone),
                      max_instances=1, coalesce=True)
        wh, wm = cfg.email.weekly_summary_at.split(":")
        sched.add_job(agent.run_weekly, CronTrigger(day_of_week=cfg.email.weekly_summary_day,
                                                    hour=int(wh), minute=int(wm), timezone=cfg.timezone),
                      max_instances=1, coalesce=True)
        sched.start()
        log.info("flight-finder up; daily check %s (%s); sources %s; email %s", cfg.check_at,
                 cfg.timezone, ", ".join(agent.providers) or "none",
                 "on" if agent.emailer.configured else "off (queued only)")
        yield
        sched.shutdown(wait=False)

    app = FastAPI(title="Flight Finder", lifespan=lifespan)
    app.state.agent = agent

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, view: str = "active"):
        trips = agent.db.trips(active=view != "stopped")
        latest = {t["id"]: agent.db.latest_check(t["id"]) for t in trips}
        return templates.TemplateResponse(request, "index.html", {
            "trips": trips, "latest": latest, "view": view,
            "n_active": len(agent.db.trips(True)), "n_stopped": len(agent.db.trips(False)),
            "busy": list(agent.busy), "last_message": agent.last_message,
            "last_run": agent.db.last_run(), "sources": [LABELS[p] for p in agent.providers],
            "email_on": agent.emailer.configured, "claude_on": agent.settings.anthropic_configured,
            "defaults": agent.config().defaults,
        })

    @app.post("/trips")
    async def add(text: str = Form(""), origin: str = Form(""), destination: str = Form(""),
                  depart: str = Form(""), ret: str = Form("")):
        if text.strip():
            asyncio.create_task(agent.add_trip(text.strip()))
        elif destination.strip() and depart:
            try:
                fields = dict(origin=origin.strip() or agent.config().defaults.origin,
                              destination=destination, depart=date.fromisoformat(depart),
                              ret=date.fromisoformat(ret) if ret else None)
            except ValueError:
                raise HTTPException(400, "dates must be YYYY-MM-DD")
            asyncio.create_task(agent.add_trip(None, fields))
        return RedirectResponse("/", status_code=303)

    @app.get("/trips/{trip_id}", response_class=HTMLResponse)
    def trip(request: Request, trip_id: int):
        row = agent.db.trip(trip_id)
        if row is None:
            raise HTTPException(404)
        latest = agent.db.latest_check(trip_id)
        dates = []
        if latest:  # cheapest per date pair from the latest Duffel check
            seen: dict[tuple, float] = {}
            for o in agent.db.offers(latest["id"]):
                k = (o["depart_date"], o["return_date"])
                seen[k] = min(seen.get(k, o["price"]), o["price"])
            dates = sorted(seen.items())
        return templates.TemplateResponse(request, "trip.html", {
            "trip": row, "latest": latest, "results": _latest_offers(agent, trip_id), "dates": dates,
            "chart": history_svg(agent.db.history(trip_id), tz), "emails": agent.db.emails(trip_id, 10),
            "checks": agent.db.checks(trip_id, 10), "busy": list(agent.busy),
            "google_on": "serpapi" in agent.providers,
        })

    @app.post("/trips/{trip_id}/settings")
    def settings_(trip_id: int, name: str = Form(...), target_price: str = Form(""),
                  max_price: str = Form(""), max_stops: str = Form(""), max_hours: str = Form(""),
                  flex_days: str = Form("3"), exclude_airlines: str = Form(""),
                  prefer_airlines: str = Form(""), depart_after: str = Form(""),
                  depart_before: str = Form(""), value_per_hour: str = Form("15"),
                  value_per_stop: str = Form("40"), adults: str = Form("1"), children: str = Form("0")):
        row = agent.db.trip(trip_id)
        if row is None:
            raise HTTPException(404)
        target = _num(target_price)
        agent.db.update_trip(
            trip_id, name=name.strip() or row["name"], target_price=target,
            target_hit=row["target_hit"] if target == row["target_price"] else 0,
            max_price=_num(max_price), max_stops=_num(max_stops, int), max_hours=_num(max_hours),
            flex_days=max(0, min(7, _num(flex_days, int) or 0)),
            exclude_airlines=exclude_airlines.strip(), prefer_airlines=prefer_airlines.strip(),
            depart_after=_num(depart_after, int), depart_before=_num(depart_before, int),
            value_per_hour=_num(value_per_hour) or 0, value_per_stop=_num(value_per_stop) or 0,
            adults=max(1, _num(adults, int) or 1), children=max(0, _num(children, int) or 0))
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    @app.post("/trips/{trip_id}/active")
    def set_active(trip_id: int, active: int = Form(...)):
        if not agent.db.update_trip(trip_id, active=int(bool(active))):
            raise HTTPException(404)
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    @app.post("/trips/{trip_id}/delete")
    def delete(trip_id: int):
        agent.db.delete_trip(trip_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/trips/{trip_id}/check")
    async def check(trip_id: int, kind: str = Form("daily")):
        if kind not in ("daily", "google"):
            raise HTTPException(400)
        asyncio.create_task(agent.check_one(trip_id, kind))
        return RedirectResponse(f"/trips/{trip_id}", status_code=303)

    @app.post("/run")
    async def trigger():
        asyncio.create_task(agent.run_daily())
        return RedirectResponse("/", status_code=303)

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        return templates.TemplateResponse(request, "runs.html", {
            "runs": agent.db.recent_runs(), "emails": agent.db.emails(None, 30),
            "email_on": agent.emailer.configured})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "running": agent.lock.locked(), "busy": len(agent.busy)}

    return app
