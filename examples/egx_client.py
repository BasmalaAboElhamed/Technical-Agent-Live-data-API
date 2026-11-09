"""Minimal httpx client for consuming the EGX API from your investment project.

    EGX_API_BASE_URL=http://127.0.0.1:8000

Rules this client enforces for you:
  * a connection failure raises EgxApiUnavailable (it never returns made-up or zero prices);
  * a quote is `usable` only if last_price is not None AND data_status == "fresh" (opt in to stale explicitly);
  * missing values stay None - never coerce them to 0.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Iterator, List, Optional

import httpx

BASE_URL = os.getenv("EGX_API_BASE_URL", "http://127.0.0.1:8000")


class EgxApiUnavailable(RuntimeError):
    pass


class EgxClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 10.0, transport: Optional[httpx.BaseTransport] = None):
        self._c = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

    def close(self) -> None:
        self._c.close()

    def _get(self, path: str, **params: Any) -> Dict[str, Any]:
        try:
            r = self._c.get(path, params={k: v for k, v in params.items() if v is not None})
        except httpx.TransportError as exc:
            raise EgxApiUnavailable(f"cannot reach EGX API at {self._c.base_url}: {exc!r}") from exc
        if r.status_code == 404:
            raise KeyError(r.json().get("error", {}).get("message", "not found"))
        if r.status_code >= 400:
            raise EgxApiUnavailable(f"EGX API returned HTTP {r.status_code}: {r.text[:200]}")
        return r.json()

    # ---- data ----
    def iter_quotes(self, page_size: int = 200, **filters: Any) -> Iterator[Dict[str, Any]]:
        offset = 0
        while True:
            body = self._get("/api/v1/quotes", limit=page_size, offset=offset, **filters)
            yield from body["data"]
            if not body["pagination"]["has_more"]:
                return
            offset += page_size

    def get_all_quotes(self) -> List[Dict[str, Any]]:
        return list(self.iter_quotes())

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return self._get(f"/api/v1/quotes/{symbol}")["data"]

    # ---- freshness ----
    def status(self) -> Dict[str, Any]:
        return self._get("/api/v1/status")

    def data_is_current(self) -> bool:
        s = self.status()
        return s["data_freshness"] == "fresh" and s["dataset_state"] == "complete"


def is_usable(quote: Dict[str, Any], allow_stale: bool = False) -> bool:
    if quote.get("last_price") is None:            # None means "unavailable", NOT zero
        return False
    if quote.get("data_status") == "fresh":
        return True
    return allow_stale and quote.get("data_status") == "stale"


if __name__ == "__main__":
    client = EgxClient()
    try:
        st = client.status()
        print(f"freshness={st['data_freshness']} ({st['data_freshness_reason']}) state={st['dataset_state']}")
        quotes = client.get_all_quotes()
        usable = [q for q in quotes if is_usable(q)]
        print(f"{len(quotes)} quotes, {len(usable)} usable now")
        print(client.get_quote("COMI"))
    except EgxApiUnavailable as exc:
        print("EGX API unavailable:", exc)
    except KeyError as exc:
        print("unknown symbol:", exc)
