from datetime import date

import pytest

from flight_finder.config import Config, Settings
from flight_finder.db import DB
from flight_finder.emailer import Emailer
from flight_finder.parser import TripParser
from flight_finder.providers import FakeProvider
from flight_finder.tracker import Tracker, date_pairs, is_drop

TODAY = date(2026, 11, 10)


class FixedPrice:
    """Provider returning one nonstop offer whose price we control per date pair."""
    name = "duffel"

    def __init__(self, prices):
        self.prices = prices  # {depart_date: price} or a callable
        self.calls = []

    def search(self, req):
        self.calls.append(req)
        offers = FakeProvider(count=1, seed=1).search(req)
        price = self.prices(req) if callable(self.prices) else self.prices.get(req.depart_date, 999)
        o = offers[0]
        from dataclasses import replace
        return [replace(o, provider=self.name, price=price)]


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


def add_trip(db, **kw):
    fields = dict(name="EWR → TLV", origin="EWR", destination="TLV", depart_date="2026-12-20",
                  return_date="2027-01-03", flex_days=2)
    fields.update(kw)
    return db.trip(db.add_trip(**fields))


def test_is_drop_either_rule():
    assert is_drop(1000, 975, 25, 5)        # $25
    assert is_drop(400, 380, 25, 5)         # 5%
    assert not is_drop(1000, 990, 25, 5)
    assert not is_drop(1000, 1010, 25, 5)


def test_date_pairs_keep_length_and_skip_past(db):
    t = add_trip(db, depart_date="2026-11-12", return_date="2026-11-19", flex_days=3)
    pairs = date_pairs(t, TODAY)
    assert pairs[0] == (date(2026, 11, 12), date(2026, 11, 19))
    assert all((r - d).days == 7 for d, r in pairs)
    assert all(d > TODAY for d, _ in pairs)
    assert len(pairs) == 5  # +1..+3, -1 (Nov 11); -2/-3 are not after today


def test_first_check_sends_start_email_then_only_real_drops(db):
    prices = {date(2026, 12, 20): 1000}
    prov = FixedPrice(prices)
    tr = Tracker(Config(), db, {"duffel": prov})
    t = add_trip(db)
    res = tr.check_trip(t, "daily", TODAY)
    assert res.exact_low == 1000 and len(res.emails) == 1
    assert db.emails(t["id"])[0]["kind"] == "start"
    assert len(prov.calls) == 5  # exact + 2 days either side

    prices[date(2026, 12, 20)] = 990  # $10 / 1%: noise
    assert tr.check_trip(db.trip(t["id"]), "daily", TODAY).emails == []
    prices[date(2026, 12, 20)] = 960  # $40 below the emailed 1000
    res = tr.check_trip(db.trip(t["id"]), "daily", TODAY)
    assert len(res.emails) == 1
    e = db.emails(t["id"])[0]
    assert e["kind"] == "drop" and "$960" in e["subject"] and "was $1,000" in e["body"]
    assert db.trip(t["id"])["emailed_low"] == 960


def test_target_price_emails_once_and_rearms(db):
    prices = {date(2026, 12, 20): 1000}
    tr = Tracker(Config(), db, {"duffel": FixedPrice(prices)})
    t = add_trip(db, target_price=900, flex_days=0)
    tr.check_trip(t, "daily", TODAY)
    prices[date(2026, 12, 20)] = 899
    assert len(tr.check_trip(db.trip(t["id"]), "daily", TODAY).emails) == 1
    assert db.emails(t["id"])[0]["kind"] == "target"
    prices[date(2026, 12, 20)] = 895  # still under target, not a $25/5% drop
    assert tr.check_trip(db.trip(t["id"]), "daily", TODAY).emails == []
    prices[date(2026, 12, 20)] = 950  # back above: re-arms
    tr.check_trip(db.trip(t["id"]), "daily", TODAY)
    assert db.trip(t["id"])["target_hit"] == 0


def test_nearby_cheaper_dates_mentioned(db):
    prov = FixedPrice(lambda r: 700 if r.depart_date == date(2026, 12, 21) else 1000)
    tr = Tracker(Config(), db, {"duffel": prov})
    t = add_trip(db)
    tr.check_trip(t, "daily", TODAY)
    chk = db.latest_check(t["id"])
    assert chk["exact_low"] == 1000 and chk["flex_low"] == 700 and chk["flex_depart"] == "2026-12-21"
    assert "Nearby dates are cheaper: $700" in db.emails(t["id"])[0]["body"]


def test_google_respects_shared_quota(db):
    google = FixedPrice({date(2026, 12, 20): 800})
    google.name = "serpapi"
    tr = Tracker(Config(), db, {"duffel": FixedPrice({}), "serpapi": google}, google_left=lambda: 61)
    t = add_trip(db, flex_days=0)
    res = tr.check_trip(t, "google", TODAY)
    assert google.calls == [] and "reserve" in res.errors[0]
    tr.google_left = lambda: 500
    res = tr.check_trip(db.trip(t["id"]), "google", TODAY)
    assert len(google.calls) == 1 and res.exact_low == 800
    assert db.trip(t["id"])["last_google_at"]


def test_daily_uses_duffel_only_and_new_uses_both(db):
    duffel, google = FixedPrice({}), FixedPrice({})
    google.name = "serpapi"
    tr = Tracker(Config(), db, {"duffel": duffel, "serpapi": google})
    t = add_trip(db, flex_days=1)
    tr.check_trip(t, "daily", TODAY)
    assert len(duffel.calls) == 3 and google.calls == []
    tr.check_trip(db.trip(t["id"]), "new", TODAY)
    assert len(google.calls) == 1 and google.calls[0].depart_date == date(2026, 12, 20)


def test_past_trip_stops(db):
    tr = Tracker(Config(), db, {"duffel": FixedPrice({})})
    t = add_trip(db, depart_date="2026-11-01", return_date="2026-11-08")
    res = tr.check_trip(t, "daily", TODAY)
    assert res.errors and db.trip(t["id"])["active"] == 0


def test_weekly_summary(db):
    tr = Tracker(Config(), db, {"duffel": FixedPrice({date(2026, 12, 20): 1000})})
    t = add_trip(db, target_price=800)
    tr.check_trip(t, "daily", TODAY)
    email_id = tr.weekly_summary()
    e = db.emails()[0]
    assert e["id"] == email_id and e["kind"] == "summary"
    assert "EWR → TLV: $1,000" in e["body"] and "target: $800" in e["body"]


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        self.login_as = u

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


def test_emailer_sends_queue(db, tmp_path):
    db.add_email(None, "summary", "hello", "body")
    s = Settings(data_dir=tmp_path, config_path=tmp_path / "c.yaml", anthropic_configured=False,
                 smtp_host="smtp.example.com", smtp_user="me@example.com", smtp_password="x",
                 email_from="me@example.com", email_to=["me@example.com"])
    assert Emailer(s, db, smtp_factory=FakeSMTP).send_pending() == 1
    assert FakeSMTP.sent[-1]["Subject"] == "hello"
    assert db.unsent_emails() == []


def test_emailer_off_without_smtp(db, tmp_path):
    db.add_email(None, "summary", "hello", "body")
    s = Settings(data_dir=tmp_path, config_path=tmp_path / "c.yaml", anthropic_configured=False)
    assert Emailer(s, db).send_pending() == 0 and len(db.unsent_emails()) == 1


def test_parser_without_claude():
    cfg = Config()
    p = TripParser(cfg.parsing, cfg.defaults, use_claude=False)
    spec = p.parse("ewr tlv 2026-12-20 2027-01-03")
    assert spec.origin == "EWR" and spec.return_date == date(2027, 1, 3) and spec.flex_days == 3
    with pytest.raises(ValueError):
        p.parse("Newark to Tel Aviv in December")


def test_parser_with_claude_uses_structured_output():
    from types import SimpleNamespace
    seen = {}

    class Messages:
        def parse(self, **kw):
            seen.update(kw)
            spec = TripParser(Config().parsing, Config().defaults, False).compact("EWR TLV 2026-12-20")
            return SimpleNamespace(stop_reason="end_turn", parsed_output=spec)

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    cfg = Config()
    spec = TripParser(cfg.parsing, cfg.defaults, True, client=client).parse("newark to tel aviv dec 20", TODAY)
    assert spec.destination == "TLV"
    assert seen["model"] == "claude-opus-5-5" and seen["fallbacks"] == "default"
    assert "Today is 2026-11-10" in seen["messages"][0]["content"]
    assert "EWR" in seen["system"]
