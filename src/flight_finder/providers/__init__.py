"""Flight price sources. Each turns a SearchRequest into a list of Offers.

- duffel:  free to search; does the daily checks and the nearby-date searches.
- serpapi: Google Flights through SerpApi; costs searches from the shared free plan, so it runs
           only when a trip is added and when Yariv clicks "Check Google Flights".
- fake:    offline, deterministic; for tests and trying the app without keys.
"""

from __future__ import annotations

from .base import Provider, ProviderError
from .duffel import DuffelProvider
from .fake import FakeProvider
from .serpapi import SerpApiProvider, searches_left

__all__ = ["Provider", "ProviderError", "DuffelProvider", "FakeProvider", "SerpApiProvider",
           "searches_left", "LABELS"]

LABELS = {"duffel": "Duffel", "serpapi": "Google Flights", "fake": "Demo data"}
