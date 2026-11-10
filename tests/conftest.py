from datetime import datetime

import pytest

from flight_finder.models import Offer, Segment, Slice


def make_offer(price, legs, provider="test", currency="USD"):
    """legs: list of slices; each slice is a list of (from, to, 'YYYY-MM-DD HH:MM', minutes, airline)."""
    slices = []
    for leg in legs:
        segs = []
        for frm, to, dep, minutes, airline in leg:
            d = datetime.strptime(dep, "%Y-%m-%d %H:%M")
            from datetime import timedelta
            segs.append(Segment(frm, to, d, d + timedelta(minutes=minutes), airline, f"{airline[:2]}1"))
        slices.append(Slice(tuple(segs)))
    return Offer(provider, price, currency, tuple(slices))


@pytest.fixture
def offer_factory():
    return make_offer
