"""Run a request against one or more providers and rank the combined results."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .models import Offer, RankedOffer, SearchRequest
from .providers import Provider, ProviderError
from .ranking import rank


@dataclass
class SearchResult:
    ranked: list[RankedOffer]
    total_offers: int
    errors: dict[str, str] = field(default_factory=dict)


def search(request: SearchRequest, providers: Sequence[Provider], limit: int | None = 10) -> SearchResult:
    offers: list[Offer] = []
    errors: dict[str, str] = {}

    def run(p: Provider) -> list[Offer]:
        return p.search(request)

    with ThreadPoolExecutor(max_workers=max(len(providers), 1)) as pool:
        futures = {p.name: pool.submit(run, p) for p in providers}
        for name, fut in futures.items():
            try:
                offers.extend(fut.result())
            except ProviderError as e:
                errors[name] = str(e)

    if not offers and errors:
        raise ProviderError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return SearchResult(rank(offers, request.preferences, limit), len(offers), errors)
