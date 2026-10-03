"""Market price search. One interface, swappable providers.

The agent's `search_market_prices` tool talks to a MarketSearchProvider:
  TavilyMarketSearch  live web search + extract           (app/procurement/tavily_market.py)
  MockMarketSearch    seeded, invented demo results       (below)

MARKET_SEARCH_PROVIDER: auto (default) = Tavily if TAVILY_API_KEY is set, else the mock;
                        tavily = always Tavily (reports "unavailable" if the key is missing, never the mock);
                        mock   = always the mock.

A provider returns a MarketSearchResult. Its `rows` match the market_search_results table:
  supplier_name, product_name, raw_price (text as found, or None), normalised_unit_price (dollars per
  inventory unit, or None), pack_size, minimum_order_quantity (in packs, or None), delivery_information,
  source_url, source_domain, confidence (0-1), and raw_result, a dict of the parsed fields:
  {pack_price, selling_unit, delivery_fee, lead_time_days, caveats, known_fields, unknown_fields, ...}.
A missing value is None. It is never filled in with a guess. These are search results, never confirmed quotes.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from ..money import CURRENCY_CODE, money
from .mock_market_data import MOCK_RESULTS

log = logging.getLogger(__name__)


@dataclass
class MarketSearchResult:
    provider: str
    status: str                       # ok | partial | unavailable
    rows: list[dict] = field(default_factory=list)
    reason: str | None = None         # why it is unavailable / partial
    is_live: bool = False             # True only for real web searches
    queries: list[str] = field(default_factory=list)
    sources_searched: int = 0         # distinct pages the search engine returned
    extracted_urls: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        """What is saved on the run (procurement_runs.market_search) for the UI and the audit trail."""
        return {
            "provider": self.provider, "status": self.status, "is_live": self.is_live, "reason": self.reason,
            "queries": self.queries, "sources_searched": self.sources_searched,
            "results_kept": len(self.rows), "extracted_urls": self.extracted_urls, "errors": self.errors,
            "searched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }


class MarketSearchProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def search(self, item: dict, requirement: dict, location: str | None = None) -> MarketSearchResult:
        """Search the market for this item. Must not raise: report failures in the result."""


class MockMarketSearch(MarketSearchProvider):
    """Seeded, deterministic alternative-supplier results. DEMO data, not the real web."""
    name = "mock"

    def search(self, item: dict, requirement: dict, location: str | None = None) -> MarketSearchResult:
        rows = []
        for (supplier, product, unit, pack_size, pack_price, pack_desc, moq, fee, lead, delivery,
             url_path, confidence) in MOCK_RESULTS.get(item["sku"], []):
            url = f"https://{url_path}"
            known = ["price", "pack_size", "moq"] + (["delivery_fee"] if fee is not None else []) \
                + (["lead_time"] if lead is not None else []) + ["delivery_information"]
            rows.append({
                "supplier_name": supplier,
                "product_name": f"{product} - {pack_desc}",
                "raw_price": f"{money(pack_price)} / {unit} ({pack_desc})",
                "normalised_unit_price": round(pack_price / pack_size, 4),
                "pack_size": pack_size,
                "minimum_order_quantity": moq,
                "delivery_information": delivery,
                "source_url": url,
                "source_domain": urlparse(url).netloc,
                "confidence": confidence,
                "raw_result": {
                    "provider": self.name, "confirmed": False, "currency": CURRENCY_CODE,
                    "pack_price": pack_price, "selling_unit": unit, "pack_description": pack_desc,
                    "delivery_fee": fee, "lead_time_days": lead, "caveats": ["Seeded demo data, not a real listing."],
                    "known_fields": known, "unknown_fields": [f for f in
                                                              ("price", "pack_size", "moq", "delivery_fee", "lead_time",
                                                               "delivery_information") if f not in known],
                    "is_retail": False, "price_is_range": False, "extracted": False,
                },
            })
        return MarketSearchResult(provider=self.name, status="ok", rows=rows, is_live=False,
                                  queries=["(seeded demo results)"], sources_searched=len(rows))


def get_market_provider() -> MarketSearchProvider:
    choice = os.environ.get("MARKET_SEARCH_PROVIDER", "auto").lower()
    if choice == "mock":
        return MockMarketSearch()
    if choice == "tavily" or (choice == "auto" and os.environ.get("TAVILY_API_KEY")):
        from .tavily_market import TavilyMarketSearch
        return TavilyMarketSearch()
    if choice not in ("auto", "mock", "tavily"):
        log.warning("Unknown MARKET_SEARCH_PROVIDER=%r; using auto.", choice)
    return MockMarketSearch()
