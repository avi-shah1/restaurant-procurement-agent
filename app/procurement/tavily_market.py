"""Live market search with Tavily (https://docs.tavily.com).

How a search goes (calls checked against tavily-python 0.8.4 and the Tavily docs):
  1. Build several focused queries for the item (not one broad one; Tavily recommends focused queries).
  2. client.search(query, search_depth="basic", max_results=N, exclude_domains=[noise]) for each query
     (1 credit each). No crawling: every result comes from a targeted search.
  3. Drop results that are not about the item, then parse the snippet with price_parser (no guessing).
  4. For the most promising results whose snippet had no usable price, call client.extract(urls, query=...)
     ONCE (1 credit per 5 URLs) to get cleaner page text, and parse again.
  5. Return rows in the market_search_results shape. Missing values stay None.

Failure handling: this class never raises. A missing/invalid key, usage limit, timeout or network error is
reported in the MarketSearchResult (status "unavailable" or "partial" plus a reason), so the run can carry on
with the supplier data we already have. It never substitutes demo data.

Public web pricing is market intelligence, not a confirmed commercial quote.
"""
from __future__ import annotations

import logging
import os
import re
import time

from ..money import CURRENCY_CODE
from .market import MarketSearchProvider, MarketSearchResult
from .price_parser import confidence, domain_of, parse_listing

log = logging.getLogger(__name__)

# sku -> (phrase used in queries, word that must appear for a result to count as being about the item)
SEARCH_TERMS: dict[str, tuple[str, str]] = {
    "MOZ-001": ("low-moisture mozzarella cheese", "mozzarella"),
    "COF-001": ("espresso blend coffee beans", "coffee"),
    "OAT-001": ("barista oat milk", "oat"),
    "MLK-001": ("whole milk", "milk"),
    "TOM-001": ("vine ripe tomatoes", "tomato"),
    "FLR-001": ("00 pizza flour", "flour"),
    "EGG-001": ("large eggs", "egg"),
    "AVO-001": ("hass avocados", "avocado"),
    "OIL-001": ("extra virgin olive oil", "olive oil"),
    "CHK-001": ("chicken breast", "chicken"),
}

_UNIT_WORDS = {"kg": "lb", "L": "gallon", "each": "dozen"}  # what US listings quote prices per

NOISE_DOMAINS = ["pinterest.com", "reddit.com", "youtube.com", "facebook.com", "instagram.com", "tiktok.com",
                 "wikipedia.org", "quora.com", "linkedin.com", "x.com", "twitter.com"]
NOT_SUPPLIERS = ("marketreportsworld.com", "grandviewresearch.com", "mordorintelligence.com",
                 "researchandmarkets.com", "imarcgroup.com")  # market-research pages: no products for sale

DEFAULT_COUNTRY = "united states"

# A unit price far from what we already pay for the same item is far more likely to be a misread than a bargain.
PLAUSIBLE_MIN, PLAUSIBLE_MAX = 0.35, 3.0  # as a multiple of the incumbent's price per unit


def search_terms(item: dict) -> tuple[str, str]:
    if item["sku"] in SEARCH_TERMS:
        return SEARCH_TERMS[item["sku"]]
    phrase = re.sub(r"\([^)]*\)", "", item["name"]).strip().lower()
    keyword = next((w for w in re.findall(r"[a-z]{4,}", phrase)), phrase)
    return phrase, keyword


def build_queries(item: dict, location: str, max_queries: int = 4) -> list[str]:
    phrase, _ = search_terms(item)
    per = _UNIT_WORDS.get(item["unit"], "unit")
    queries = [
        f"{phrase} wholesale price case {location}",
        f"{phrase} foodservice restaurant supplier bulk price per {per}",
        f"buy {phrase} bulk case restaurant supply online delivery",
        f"{phrase} wholesale distributor next day delivery {location}",
    ]
    return queries[:max_queries]


def _windows(text: str, keyword: str, limit: int = 25) -> list[str]:
    """Slices of a long page around each mention of the item, so a category page with many products
    is parsed one product at a time instead of as one confusing blob."""
    out, lowered = [], text.lower()
    start = 0
    while len(out) < limit:
        pos = lowered.find(keyword.lower(), start)
        if pos < 0:
            break
        out.append(text[max(0, pos - 160):pos + 360])
        start = pos + 360
    return out


def _clean_title(title: str) -> str:
    return re.sub(r"\s+", " ", title or "").strip()[:140] or "(untitled)"


def _merge(snippet: dict, extracted: dict) -> dict:
    """Extracted page text wins where it knows something; the snippet fills the rest."""
    merged = dict(snippet)
    for key, value in extracted.items():
        if key in ("caveats", "known_fields", "unknown_fields"):
            continue
        if value is not None and value is not False:
            merged[key] = value
    merged["caveats"] = list(dict.fromkeys(
        [*extracted["caveats"], *snippet["caveats"], "Price and pack details come from the extracted page text."]))
    known = {f: (f in extracted["known_fields"]) or (f in snippet["known_fields"]) for f in snippet["known_fields"] + snippet["unknown_fields"]}
    merged["known_fields"] = [f for f, k in known.items() if k]
    merged["unknown_fields"] = [f for f, k in known.items() if not k]
    return merged


class TavilyMarketSearch(MarketSearchProvider):
    name = "tavily"

    def __init__(self, api_key: str | None = None, client=None, max_results: int | None = None,
                 max_extract: int | None = None, search_timeout: float | None = None,
                 extract_timeout: float = 30.0, budget_s: float | None = None, country: str | None = None,
                 max_rows: int = 12, clock=time.monotonic):
        self.api_key = api_key if api_key is not None else os.environ.get("TAVILY_API_KEY")
        self._client = client
        self.max_results = max_results or int(os.environ.get("TAVILY_MAX_RESULTS", 6))
        self.max_extract = max_extract if max_extract is not None else int(os.environ.get("TAVILY_EXTRACT_MAX_URLS", 3))
        self.search_timeout = search_timeout or float(os.environ.get("TAVILY_TIMEOUT_S", 20))
        self.extract_timeout = extract_timeout
        self.budget_s = budget_s or float(os.environ.get("TAVILY_BUDGET_S", 75))
        self.country = country if country is not None else os.environ.get("RESTAURANT_COUNTRY", DEFAULT_COUNTRY)
        self.max_rows = max_rows
        self._clock = clock

    # ---- client ----
    def _get_client(self):
        if self._client is None:
            from tavily import TavilyClient
            self._client = TavilyClient(api_key=self.api_key)
        return self._client

    @staticmethod
    def _describe(error: Exception) -> tuple[str, bool]:
        """(reason, fatal). Fatal errors mean every further call would fail too."""
        import tavily
        from tavily import errors as te
        name = type(error).__name__
        if isinstance(error, (tavily.InvalidAPIKeyError, tavily.MissingAPIKeyError)):
            return "Tavily rejected the API key (invalid or missing)", True
        if isinstance(error, tavily.UsageLimitExceededError):
            return "Tavily usage limit or plan credits exceeded", True
        if isinstance(error, te.ForbiddenError):
            return "Tavily refused the request (forbidden)", True
        if isinstance(error, te.TimeoutError):
            return f"Tavily timed out ({error})", False
        if isinstance(error, tavily.BadRequestError):
            return f"Tavily rejected the request: {error}", False
        return f"{name}: {error}"[:200], False

    # ---- the search ----
    def search(self, item: dict, requirement: dict, location: str | None = None) -> MarketSearchResult:
        location = location or os.environ.get("RESTAURANT_LOCATION", "United States")
        queries = build_queries(item, location)
        if not self.api_key and self._client is None:
            return MarketSearchResult(self.name, "unavailable", reason="TAVILY_API_KEY is not set", is_live=True,
                                      queries=queries)
        try:
            return self._search(item, requirement, queries)
        except Exception as e:  # last line of defence: this method must never raise
            log.exception("Tavily market search crashed")
            return MarketSearchResult(self.name, "unavailable", reason=f"Unexpected error: {e}"[:200],
                                      is_live=True, queries=queries, errors=[str(e)[:200]])

    def _search(self, item: dict, requirement: dict, queries: list[str]) -> MarketSearchResult:
        client = self._get_client()
        phrase, keyword = search_terms(item)
        started = self._clock()
        errors: list[str] = []
        ran: list[str] = []
        by_url: dict[str, dict] = {}
        fatal = False
        country = self.country or None

        for q in queries:
            if self._clock() - started > self.budget_s:
                errors.append("Search time budget used up; remaining queries skipped.")
                break
            params = dict(search_depth="basic", max_results=self.max_results, topic="general",
                          exclude_domains=NOISE_DOMAINS, timeout=self.search_timeout)
            try:
                try:
                    response = client.search(q, **params, **({"country": country} if country else {}))
                except Exception as e:
                    if country and type(e).__name__ == "BadRequestError":
                        country = None  # an unsupported country value must not sink the whole search
                        response = client.search(q, **params)
                    else:
                        raise
                ran.append(q)
                for r in response.get("results", []):
                    url = r.get("url")
                    if url and (url not in by_url or r.get("score", 0) > by_url[url].get("score", 0)):
                        by_url[url] = r
            except Exception as e:
                reason, fatal = self._describe(e)
                errors.append(reason)
                if fatal:
                    break

        if not ran:
            return MarketSearchResult(self.name, "unavailable", is_live=True, queries=queries,
                                      reason="; ".join(dict.fromkeys(errors)) or "No search completed",
                                      errors=errors)

        # ---- keep only results about this item, and parse them ----
        kept: list[dict] = []
        for url, r in by_url.items():
            domain = domain_of(url)
            haystack = f"{r.get('title', '')} {r.get('content', '')} {url}".lower()
            if (keyword.lower() not in haystack or url.lower().split("?")[0].endswith(".pdf")
                    or any(domain == d or domain.endswith("." + d) for d in (*NOT_SUPPLIERS, *NOISE_DOMAINS))):
                continue
            parsed = parse_listing(r.get("content", ""), item["unit"], title=r.get("title", ""), url=url)
            kept.append({"url": url, "domain": domain, "title": r.get("title", ""), "score": float(r.get("score", 0)),
                         "parsed": parsed, "extracted": False, "text": r.get("content", "")})

        # ---- Extract: only for promising pages whose snippet gave no usable unit price ----
        extracted_urls: list[str] = []
        wanted = sorted((k for k in kept if k["parsed"]["normalised_unit_price"] is None and k["score"] >= 0.45
                         and not k["parsed"]["is_retail"]), key=lambda k: -k["score"])[:self.max_extract]
        if wanted and self.max_extract and self._clock() - started < self.budget_s and not fatal:
            try:
                response = client.extract(urls=[k["url"] for k in wanted], extract_depth="basic", format="text",
                                          query=f"{phrase} price per case pack size", chunks_per_source=3,
                                          timeout=self.extract_timeout)
                pages = {p["url"]: p.get("raw_content", "") for p in response.get("results", [])}
                for fail in response.get("failed_results", []):
                    errors.append(f"Extract failed for {domain_of(fail.get('url', ''))}: {fail.get('error', 'unknown')}"[:160])
                for k in wanted:
                    text = pages.get(k["url"])
                    if not text:
                        continue
                    extracted_urls.append(k["url"])
                    best = None
                    for window in _windows(text, keyword):
                        cand = parse_listing(window, item["unit"], title=k["title"], url=k["url"])
                        rank = (cand["normalised_unit_price"] is not None, cand["advertised_price"] is not None)
                        if best is None or rank > best[0]:
                            best = (rank, cand, window)
                    if best and best[0][1]:  # the page text stated a price: use it
                        k["parsed"] = _merge(k["parsed"], best[1])
                        k["extracted"] = True
                        k["text"] = best[2]
            except Exception as e:
                reason, _ = self._describe(e)
                errors.append(f"Extract: {reason}")

        # ---- rows ----
        reference = ((requirement or {}).get("incumbent") or {}).get("price_per_unit")
        rows = []
        for k in kept:
            p = k["parsed"]
            useful = p["advertised_price"] is not None or k["score"] >= 0.5
            if not useful:
                continue
            conf = confidence(k["score"], p)
            suspect = False
            if reference and p["normalised_unit_price"] is not None:
                ratio = p["normalised_unit_price"] / reference
                if ratio < PLAUSIBLE_MIN or ratio > PLAUSIBLE_MAX:
                    suspect = True
                    conf = round(conf * 0.5, 2)
                    p["caveats"] = [*p["caveats"], (
                        f"Implausible price: ${p['normalised_unit_price']:.2f} per {item['unit']} is {ratio:.2f}x what we "
                        f"pay today (${reference:.2f}). It is most likely a misread of the page, so it is not offered "
                        f"as an option.")]
            rows.append({
                "supplier_name": k["domain"], "product_name": _clean_title(k["title"]),
                "raw_price": p["price_text"], "normalised_unit_price": p["normalised_unit_price"],
                "pack_size": p["pack_size"], "minimum_order_quantity": p["moq_packs"],
                "delivery_information": p["delivery_information"],
                "source_url": k["url"], "source_domain": k["domain"], "confidence": conf,
                "raw_result": {
                    "provider": self.name, "confirmed": False, "currency": CURRENCY_CODE,
                    "advertised_price": p["advertised_price"], "price_basis": p["price_basis"],
                    "price_is_range": p["price_is_range"], "pack_price": p["pack_price"],
                    "selling_unit": p["selling_unit"], "pack_description": p["pack_description"],
                    "moq_text": p["moq_text"], "moq_inventory_units": p["moq_inventory_units"],
                    "delivery_fee": p["delivery_fee"], "lead_time_days": p["lead_time_days"],
                    "is_retail": p["is_retail"], "caveats": p["caveats"],
                    "known_fields": p["known_fields"], "unknown_fields": p["unknown_fields"],
                    "tavily_score": k["score"], "extracted": k["extracted"], "price_suspect": suspect,
                    "source_text": re.sub(r"\s+", " ", k["text"]).strip()[:600],  # what the numbers were read from
                },
            })
        rows.sort(key=lambda r: (r["normalised_unit_price"] is None, -r["confidence"], r["source_url"]))
        rows = rows[:self.max_rows]

        status = "partial" if errors else "ok"
        return MarketSearchResult(
            self.name, status, rows=rows, is_live=True, queries=ran, sources_searched=len(by_url),
            extracted_urls=extracted_urls, errors=errors,
            reason=("; ".join(dict.fromkeys(errors)) if errors else None))
