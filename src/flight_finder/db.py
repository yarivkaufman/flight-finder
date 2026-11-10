"""SQLite store: watched trips, every check, the offers it found, and what was emailed."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    request TEXT,                 -- what Yariv typed, if added in words
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,             -- NULL = one way
    adults INTEGER NOT NULL DEFAULT 1,
    children INTEGER NOT NULL DEFAULT 0,
    cabin TEXT NOT NULL DEFAULT 'economy',
    currency TEXT NOT NULL DEFAULT 'USD',
    flex_days INTEGER NOT NULL DEFAULT 3,
    max_stops INTEGER,
    max_price REAL,
    max_hours REAL,
    exclude_airlines TEXT NOT NULL DEFAULT '',   -- comma list
    prefer_airlines TEXT NOT NULL DEFAULT '',
    depart_after INTEGER,
    depart_before INTEGER,
    value_per_hour REAL NOT NULL DEFAULT 15,
    value_per_stop REAL NOT NULL DEFAULT 40,
    target_price REAL,
    notes TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_check_at TEXT,
    last_google_at TEXT,
    last_error TEXT,
    emailed_low REAL,             -- lowest price already emailed (drop rule compares to this)
    target_hit INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,           -- daily | google | manual
    sources TEXT NOT NULL,        -- comma list of providers used
    exact_low REAL,               -- cheapest acceptable price on the trip's own dates
    flex_low REAL,                -- cheapest acceptable price on any nearby dates
    flex_depart TEXT,
    flex_return TEXT,
    offers INTEGER NOT NULL DEFAULT 0,
    errors TEXT                   -- JSON list
);
CREATE INDEX IF NOT EXISTS checks_trip ON checks(trip_id, at);
CREATE TABLE IF NOT EXISTS offers (   -- the ranked offers kept from each check
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id INTEGER NOT NULL,
    trip_id INTEGER NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT,
    provider TEXT NOT NULL,
    rank INTEGER NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL,
    score REAL NOT NULL,
    minutes INTEGER NOT NULL,
    stops INTEGER NOT NULL,
    airlines TEXT NOT NULL,
    tags TEXT NOT NULL,           -- JSON list: nonstop, cheapest, fastest, ...
    slices TEXT NOT NULL,         -- JSON: legs and segments for display
    booking_url TEXT
);
CREATE INDEX IF NOT EXISTS offers_check ON offers(check_id);
CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trip_id INTEGER,              -- NULL for the weekly summary
    at TEXT NOT NULL,
    kind TEXT NOT NULL,           -- drop | target | summary
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    kind TEXT NOT NULL,
    trips INTEGER,
    offers INTEGER,
    emails INTEGER,
    errors TEXT
);
"""

TRIP_FIELDS = {
    "name", "request", "origin", "destination", "depart_date", "return_date", "adults", "children",
    "cabin", "currency", "flex_days", "max_stops", "max_price", "max_hours", "exclude_airlines",
    "prefer_airlines", "depart_after", "depart_before", "value_per_hour", "value_per_stop",
    "target_price", "notes", "active", "last_check_at", "last_google_at", "last_error",
    "emailed_low", "target_hit",
}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def local_time(s: str | None, tz: str) -> str:
    """A stored UTC timestamp shown in the configured timezone, e.g. '2026-11-10 07:30 EST'."""
    if not s:
        return ""
    t = datetime.fromisoformat(s)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


class DB:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # --- trips ----------------------------------------------------------------------
    def add_trip(self, **fields: Any) -> int:
        bad = set(fields) - TRIP_FIELDS
        if bad:
            raise ValueError(f"unknown trip fields: {bad}")
        fields["created_at"] = now()
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.conn() as c:
            return c.execute(f"INSERT INTO trips ({cols}) VALUES ({marks})",
                             tuple(fields.values())).lastrowid

    def update_trip(self, trip_id: int, **fields: Any) -> bool:
        bad = set(fields) - TRIP_FIELDS
        if bad:
            raise ValueError(f"unknown trip fields: {bad}")
        if not fields:
            return True
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn() as c:
            return c.execute(f"UPDATE trips SET {sets} WHERE id = ?",
                             (*fields.values(), trip_id)).rowcount > 0

    def trip(self, trip_id: int) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM trips WHERE id = ?", (trip_id,)).fetchone()

    def trips(self, active: bool | None = True) -> list[sqlite3.Row]:
        with self.conn() as c:
            if active is None:
                return c.execute("SELECT * FROM trips ORDER BY depart_date").fetchall()
            return c.execute("SELECT * FROM trips WHERE active = ? ORDER BY depart_date",
                             (int(active),)).fetchall()

    def delete_trip(self, trip_id: int) -> None:
        with self.conn() as c:
            c.execute("DELETE FROM offers WHERE trip_id = ?", (trip_id,))
            c.execute("DELETE FROM checks WHERE trip_id = ?", (trip_id,))
            c.execute("DELETE FROM emails WHERE trip_id = ?", (trip_id,))
            c.execute("DELETE FROM trips WHERE id = ?", (trip_id,))

    # --- checks & offers ------------------------------------------------------------
    def add_check(self, trip_id: int, kind: str, sources: list[str], exact_low: float | None,
                  flex: tuple[float, str, str | None] | None, offers: list[dict],
                  errors: list[str]) -> int:
        at = now()
        with self.conn() as c:
            check_id = c.execute(
                "INSERT INTO checks (trip_id, at, kind, sources, exact_low, flex_low, flex_depart, "
                "flex_return, offers, errors) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trip_id, at, kind, ",".join(sources), exact_low, flex[0] if flex else None,
                 flex[1] if flex else None, flex[2] if flex else None, len(offers),
                 json.dumps(errors))).lastrowid
            c.executemany(
                "INSERT INTO offers (check_id, trip_id, depart_date, return_date, provider, rank, price, "
                "currency, score, minutes, stops, airlines, tags, slices, booking_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(check_id, trip_id, o["depart_date"], o["return_date"], o["provider"], o["rank"],
                  o["price"], o["currency"], o["score"], o["minutes"], o["stops"], o["airlines"],
                  json.dumps(o["tags"]), json.dumps(o["slices"]), o["booking_url"]) for o in offers])
        return check_id

    def checks(self, trip_id: int, limit: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM checks WHERE trip_id = ? ORDER BY id DESC"
        with self.conn() as c:
            if limit:
                return c.execute(q + " LIMIT ?", (trip_id, limit)).fetchall()
            return c.execute(q, (trip_id,)).fetchall()

    def history(self, trip_id: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT at, kind, exact_low, flex_low FROM checks WHERE trip_id = ? "
                             "ORDER BY at", (trip_id,)).fetchall()

    def latest_check(self, trip_id: int, kind: str | None = None) -> sqlite3.Row | None:
        with self.conn() as c:
            if kind:
                return c.execute("SELECT * FROM checks WHERE trip_id = ? AND kind = ? ORDER BY id DESC "
                                 "LIMIT 1", (trip_id, kind)).fetchone()
            return c.execute("SELECT * FROM checks WHERE trip_id = ? ORDER BY id DESC LIMIT 1",
                             (trip_id,)).fetchone()

    def offers(self, check_id: int) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM offers WHERE check_id = ? ORDER BY depart_date, return_date, "
                             "rank", (check_id,)).fetchall()

    def low_since(self, trip_id: int, since: str) -> sqlite3.Row | None:
        """The check with the lowest exact-date price since a time (weekly summary)."""
        with self.conn() as c:
            return c.execute("SELECT * FROM checks WHERE trip_id = ? AND at <= ? AND exact_low IS NOT NULL "
                             "ORDER BY at DESC LIMIT 1", (trip_id, since)).fetchone()

    def source_fail_streak(self, trip_id: int, source: str) -> int:
        """How many of the latest daily checks in a row had an error from this source."""
        n = 0
        for row in self.checks(trip_id, 30):
            if row["kind"] != "daily":
                continue
            if any(e.startswith(f"{source}:") for e in json.loads(row["errors"] or "[]")):
                n += 1
            else:
                break
        return n

    # --- emails ---------------------------------------------------------------------
    def add_email(self, trip_id: int | None, kind: str, subject: str, body: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO emails (trip_id, at, kind, subject, body) VALUES (?, ?, ?, ?, ?)",
                             (trip_id, now(), kind, subject, body)).lastrowid

    def mark_email(self, email_id: int, sent: bool, error: str | None = None) -> None:
        with self.conn() as c:
            c.execute("UPDATE emails SET sent = ?, error = ? WHERE id = ?", (int(sent), error, email_id))

    def unsent_emails(self) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM emails WHERE sent = 0 ORDER BY id").fetchall()

    def emails(self, trip_id: int | None = None, limit: int = 20) -> list[sqlite3.Row]:
        with self.conn() as c:
            if trip_id is None:
                return c.execute("SELECT * FROM emails ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return c.execute("SELECT * FROM emails WHERE trip_id = ? ORDER BY id DESC LIMIT ?",
                             (trip_id, limit)).fetchall()

    # --- runs -----------------------------------------------------------------------
    def start_run(self, kind: str) -> int:
        with self.conn() as c:
            return c.execute("INSERT INTO runs (started_at, kind) VALUES (?, ?)", (now(), kind)).lastrowid

    def finish_run(self, run_id: int, trips: int, offers: int, emails: int, errors: list[str]) -> None:
        with self.conn() as c:
            c.execute("UPDATE runs SET finished_at = ?, trips = ?, offers = ?, emails = ?, errors = ? "
                      "WHERE id = ?", (now(), trips, offers, emails, json.dumps(errors), run_id))

    def last_run(self) -> sqlite3.Row | None:
        with self.conn() as c:
            return c.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
                             ).fetchone()

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.conn() as c:
            return c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
