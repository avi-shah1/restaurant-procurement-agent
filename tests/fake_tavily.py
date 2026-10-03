"""A scripted stand-in for tavily.TavilyClient. It records every call so tests can check how we used the API."""
from __future__ import annotations


class FakeTavily:
    def __init__(self, results=None, pages=None, search_errors=None, extract_error=None, failed_urls=None):
        self.results = results or []          # what every search returns
        self.pages = pages or {}              # url -> raw_content returned by extract
        self.search_errors = search_errors or []   # per-call: an exception to raise, or None
        self.extract_error = extract_error
        self.failed_urls = failed_urls or {}  # url -> error text, reported as failed_results
        self.search_calls: list[dict] = []
        self.extract_calls: list[dict] = []

    def search(self, query, **kwargs):
        index = len(self.search_calls)
        self.search_calls.append({"query": query, **kwargs})
        if index < len(self.search_errors) and self.search_errors[index] is not None:
            raise self.search_errors[index]
        return {"query": query, "results": [dict(r) for r in self.results], "response_time": 0.1}

    def extract(self, urls, **kwargs):
        self.extract_calls.append({"urls": list(urls), **kwargs})
        if self.extract_error is not None:
            raise self.extract_error
        return {
            "results": [{"url": u, "raw_content": self.pages[u]} for u in urls if u in self.pages],
            "failed_results": [{"url": u, "error": self.failed_urls[u]} for u in urls if u in self.failed_urls],
        }


def result(title, url, content, score=0.7):
    return {"title": title, "url": url, "content": content, "score": score}


# Three crafted results with answers worked out by hand (used by the Tavily and frontend tests):
FAST = result("Low Moisture Mozzarella 6 x 1kg - FastCheese Supply", "https://www.fastcheese-supply.example/mozzarella-6x1kg",
              "Low-moisture mozzarella cheese, 6 x 1kg case $45.00. Next-day delivery available. "
              "$10 flat rate delivery fee. Wholesale foodservice pricing.", 0.8)
SLOW = result("Bulk Mozzarella - SlowBulk", "https://slowbulk.example/mozzarella",
              "Mozzarella cheese $5.90 per lb. Ships in 5-7 days. Free shipping on orders over $200.", 0.6)
LEAD = result("Mozzarella cases - Cheese Depot", "https://cheesedepot.example/mozzarella",
              "Mozzarella 6 x 2kg case. Request a quote for pricing.", 0.55)
