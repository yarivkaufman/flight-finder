from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from ..models import Offer, SearchRequest


class ProviderError(RuntimeError):
    pass


class Provider(Protocol):
    name: str

    def search(self, request: SearchRequest) -> list[Offer]: ...


def http_json(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
              body: Any = None, timeout: float = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise ProviderError(f"HTTP {e.code} from {url.split('?')[0]}: {detail}") from None
    except urllib.error.URLError as e:
        raise ProviderError(f"could not reach {url.split('?')[0]}: {e.reason}") from None
