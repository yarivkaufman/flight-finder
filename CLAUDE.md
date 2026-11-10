# Flight Finder

A flight-price watcher for Yariv, built and deployed **like price-agent and job-agent**
(github.com/yarivkaufman/price-agent, /job-agent: read their CLAUDE.md for the server and deploy steps).

## What it does
- Yariv adds a trip on the dashboard (in words, parsed by Claude, or with the form fields).
- It checks prices right away, then once a day, ranks offers by value, and emails him when the
  price drops or reaches his target. Weekly summary email on Sundays.

## Decided with Yariv (2026-11-10)
1. Sources: both Duffel and Google Flights (SerpApi), merged and de-duplicated.
2. Users: Yariv now, family later (no login yet; keep it easy to add people).
3. Runs like price-agent: Docker on the home server 192.168.1.67, `~/flight-finder`, port 8097,
   dashboard + scheduler + SQLite (`data/flights.db`), `config/config.yaml`, secrets in `.env` only.
4. **No Telegram for now** (may come later). Requests are entered on the dashboard.
5. Home airport: Newark (EWR) only by default; other airports per trip.
6. Dates: the trip's dates plus nearby dates (default ±3 days, same trip length).
7. Ranking: balanced value score = price + $15/travel hour + $40/stop (editable per trip);
   hard limits per trip: max price, max stops, max hours each way, avoid airlines, departure hours.
8. Watching: saved trips rechecked once a day; dashboard shows price history and "new low".
9. Search quota: Duffel (free) does the daily checks and nearby dates. Google Flights runs only on a
   trip's first check and on the "Check Google Flights" button, sharing price-agent's SERPAPI_KEY;
   skipped when fewer than `google.reserve` (60) searches are left this month.
10. Email (plain SMTP, creds in `.env`): a drop email when the cheapest acceptable price on the trip's
    dates beats the last *emailed* low by >= $25 OR >= 5%, or reaches the trip's target price (once,
    re-armed when the price goes back above the target). Cheaper nearby dates are mentioned in the
    email. A "now watching" email on a trip's first check. Weekly summary Sunday 08:00 with every
    trip, the change over the week, and Duffel failing 3+ daily checks in a row.
    Not using Home Assistant's notify email (job-agent notes it lands in spam).
11. Booking: link out only (Google Flights link; Duffel offers have no public booking page). Prices in
    USD, 1 adult by default, changeable per trip.

## Design notes
- `providers/`: duffel.py, serpapi.py (Google Flights; round trip = 1 outbound search + 1 per
  return option for the 3 cheapest outbounds), fake.py (demo/tests). New sources go here.
- `ranking.py`: hard filters, value score, cross-source de-duplication (same flights -> cheaper offer).
- `tracker.py`: date pairs, checks, email rules (`price_emails`, `weekly_summary`).
- `parser.py`: Claude (claude-opus-5-5, effort low, structured output, server-side fallback).
- Never log SerpApi URLs or account JSON (they contain the key). Provider errors strip query strings.
- Duffel may price some routes in another currency; those offers are dropped and the check reports it.

## State
- Code written 2026-11-10, tested with demo data (dashboard, checks, emails queued). **Not deployed.**
- Before deploying: Yariv needs a Duffel live access token and an SMTP account (e.g. Gmail app password);
  the Anthropic key and SerpApi key can be copied server-side from price-agent's `.env`.
- Check after deploy: `docker compose exec flight-finder flight-finder probe duffel EWR TLV <date> <date>`.

## Operating it
- Same as price-agent with port 8097 and service `flight-finder`.
- Tests: `uv run python -m pytest -q`.
- Confirm with Yariv before deploying to or changing anything on the server.
