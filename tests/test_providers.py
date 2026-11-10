from datetime import date

from flight_finder.models import Preferences, SearchRequest
from flight_finder.providers.duffel import DuffelProvider, iso_minutes
from flight_finder.providers.fake import FakeProvider
from flight_finder.providers.serpapi import SerpApiProvider

REQ_RT = SearchRequest("tlv", "jfk", date(2026, 12, 20), date(2027, 1, 3), preferences=Preferences(max_stops=1))


def serp_option(price, frm, to, dep, arr, token=None):
    o = {
        "price": price,
        "total_duration": 600,
        "flights": [{
            "departure_airport": {"id": frm, "time": dep},
            "arrival_airport": {"id": to, "time": arr},
            "airline": "El Al", "flight_number": "LY 1", "duration": 600,
        }],
    }
    if token:
        o["departure_token"] = token
    return o


def test_serpapi_round_trip_fetches_returns():
    calls = []

    def fetch(url, **kw):
        calls.append(url)
        if "departure_token" in url:
            return {"best_flights": [serp_option(1100, "JFK", "TLV", "2027-01-03 22:00", "2027-01-04 15:00")]}
        return {
            "search_metadata": {"google_flights_url": "https://google.com/flights"},
            "best_flights": [serp_option(1000, "TLV", "JFK", "2026-12-20 08:00", "2026-12-20 13:00", "tok")],
            "other_flights": [serp_option(1200, "TLV", "JFK", "2026-12-20 10:00", "2026-12-20 15:00")],
        }

    offers = SerpApiProvider("key", fetch=fetch).search(REQ_RT)
    assert len(calls) == 2
    assert "stops=2" in calls[0] and "type=1" in calls[0]
    assert len(offers) == 1
    assert offers[0].price == 1100
    assert [s.segments[0].origin for s in offers[0].slices] == ["TLV", "JFK"]
    assert offers[0].booking_url == "https://google.com/flights"


def test_serpapi_one_way():
    req = SearchRequest("TLV", "ATH", date(2026, 12, 20))
    fetch = lambda url, **kw: {"best_flights": [serp_option(150, "TLV", "ATH", "2026-12-20 08:00", "2026-12-20 10:00")]}
    offers = SerpApiProvider("key", fetch=fetch).search(req)
    assert offers[0].price == 150 and offers[0].slices[0].minutes == 600


def test_duffel_request_and_parse():
    seen = {}

    def fetch(url, **kw):
        seen.update(kw)
        seg = lambda o, d, dep, arr: {
            "origin": {"iata_code": o}, "destination": {"iata_code": d},
            "departing_at": dep, "arriving_at": arr, "duration": "PT12H",
            "marketing_carrier": {"name": "El Al", "iata_code": "LY"}, "marketing_carrier_flight_number": "1",
        }
        return {"data": {"offers": [{
            "id": "off_1", "total_amount": "980.50", "total_currency": "USD",
            "slices": [
                {"duration": "PT12H", "segments": [seg("TLV", "JFK", "2026-12-20T08:00:00", "2026-12-20T13:00:00")]},
                {"duration": "PT11H", "segments": [seg("JFK", "TLV", "2027-01-03T22:00:00", "2027-01-04T15:00:00")]},
            ],
        }]}}

    offers = DuffelProvider("duffel_test_x", fetch=fetch).search(REQ_RT)
    body = seen["body"]["data"]
    assert body["max_connections"] == 1
    assert len(body["slices"]) == 2 and body["slices"][1]["origin"] == "JFK"
    assert seen["headers"]["Duffel-Version"] == "v2"
    o = offers[0]
    assert o.price == 980.5 and o.offer_id == "off_1"
    assert o.slices[0].segments[0].flight_number == "LY1"
    assert o.total_minutes == 23 * 60


def test_iso_minutes():
    assert iso_minutes("PT1H30M") == 90
    assert iso_minutes("P1DT2H") == 1560
    assert iso_minutes(None) is None


def test_fake_is_deterministic():
    a = FakeProvider().search(REQ_RT)
    b = FakeProvider().search(REQ_RT)
    assert [o.price for o in a] == [o.price for o in b]
    assert all(len(o.slices) == 2 for o in a)
