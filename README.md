# Flight Finder

Watches flight prices for trips you plan and emails you when they drop. Built and deployed like
price-agent and job-agent: one Docker service with the dashboard and the scheduler.

```
Add a trip on the dashboard: "Newark to Tel Aviv Dec 20, back Jan 3, nonstop if possible"
  -> Claude turns it into a trip (airports, dates, passengers, limits, target price)
  -> first check right away: Duffel (your dates ±3 days) + Google Flights (your dates)
Every day 07:30: Duffel checks every trip on its dates and nearby dates
  -> rank by value: price + $15 per travel hour + $40 per stop (per trip, editable)
  -> email on a new low ($25 or 5% under the last emailed price) or the target price
Sundays 08:00: one summary email of every trip
"Check Google Flights" on a trip page: Google prices on demand (shared SerpApi plan)
```

## Sources

- **Duffel** (`DUFFEL_ACCESS_TOKEN`): airline offers, free to search. Does the daily checks.
- **Google Flights via SerpApi** (`SERPAPI_KEY`, same key as price-agent): widest coverage, but
  the free plan is 250 searches a month shared with price-agent, so it runs only when a trip is
  added and when you click. A round trip costs 4 searches. It's skipped when fewer than
  `google.reserve` (60) searches are left.

## Run

```
cp .env.example .env            # Duffel token, SerpApi key, Anthropic key, SMTP (server only)
mkdir -p config && cp config.example/config.yaml config/
docker compose up -d --build    # dashboard on :8097
```

Try it without any keys: `FLIGHTS_DEMO=1` in `.env` uses offline demo data.

CLI (inside the container: `docker compose exec flight-finder flight-finder ...`):
`check` (all trips once) · `add "<trip in words>"` · `weekly` (summary now) ·
`probe duffel EWR TLV 2026-12-20 2027-01-03` (one search, ranked) · `searches-left` (SerpApi balance).

## Develop

`uv sync`, then `uv run python -m pytest -q`. Run locally with
`FLIGHTS_DEMO=1 DATA_DIR=<tmp> uv run flight-finder serve` and open http://localhost:8097.
