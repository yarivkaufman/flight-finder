from flight_finder.models import Preferences
from flight_finder.ranking import dedupe, rank, rejection_reason


def nonstop(f, price):
    return f(price, [[("TLV", "JFK", "2026-12-20 08:00", 12 * 60, "El Al")]])


def one_stop(f, price, airline="Lufthansa", layover_min=90):
    from datetime import datetime, timedelta
    dep2 = (datetime(2026, 12, 20, 6) + timedelta(minutes=4 * 60 + layover_min)).strftime("%Y-%m-%d %H:%M")
    return f(price, [[("TLV", "FRA", "2026-12-20 06:00", 4 * 60, airline),
                      ("FRA", "JFK", dep2, 9 * 60, airline)]])


def test_nonstop_beats_slightly_cheaper_long_connection(offer_factory):
    a = nonstop(offer_factory, 900)
    b = one_stop(offer_factory, 860, layover_min=240)  # 17h total, 1 stop
    ranked = rank([a, b], Preferences())
    assert ranked[0].offer is a
    assert "nonstop" in ranked[0].reasons
    assert "cheapest" in ranked[1].reasons


def test_cheap_wins_when_time_is_worth_little(offer_factory):
    a = nonstop(offer_factory, 900)
    b = one_stop(offer_factory, 600)
    ranked = rank([a, b], Preferences(value_per_hour=5, value_per_stop=10))
    assert ranked[0].offer is b


def test_hard_filters(offer_factory):
    b = one_stop(offer_factory, 600, layover_min=300)
    assert rejection_reason(b, Preferences(max_stops=0))
    assert rejection_reason(b, Preferences(max_price=500))
    assert rejection_reason(b, Preferences(max_layover_minutes=120))
    assert rejection_reason(b, Preferences(exclude_airlines=frozenset({"lufthansa"})))
    assert rejection_reason(b, Preferences(earliest_departure_hour=7))
    assert rejection_reason(b, Preferences()) is None


def test_dedupe_keeps_cheapest_across_providers(offer_factory):
    a = nonstop(offer_factory, 900)
    b = offer_factory(850, [[("TLV", "JFK", "2026-12-20 08:00", 12 * 60, "El Al")]], provider="other")
    assert dedupe([a, b]) == [b]


def test_limit(offer_factory):
    offers = [nonstop(offer_factory, 900 + i) for i in range(5)]
    offers = [offer_factory(900 + i, [[("TLV", "JFK", f"2026-12-20 0{i}:00", 720, "El Al")]]) for i in range(5)]
    assert len(rank(offers, Preferences(), limit=3)) == 3
