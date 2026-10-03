"""Save useful web discoveries as suppliers and supplier offers, always tagged source_type='tavily_live'.

"Where sensible" means: only a result with a real price, a known pack size and decent confidence, from a
site that is not a consumer retail listing and not a price range. Everything else stays in
market_search_results only. Existing suppliers and offers from our own records are never modified.
"""
from __future__ import annotations

from datetime import datetime, timezone

MIN_CONFIDENCE = 0.40


def worth_saving(row: dict) -> bool:
    parsed = row.get("raw_result") or {}
    return (parsed.get("provider") == "tavily"
            and row.get("normalised_unit_price") is not None and row.get("pack_size") is not None
            and parsed.get("pack_price") is not None
            and (row.get("confidence") or 0) >= MIN_CONFIDENCE
            and not parsed.get("is_retail") and not parsed.get("price_is_range")
            and not parsed.get("price_suspect"))


def persist_discoveries(repo, inventory_item_id: int, saved_rows: list[dict]) -> dict:
    """saved_rows are market_search_results rows (with ids). Returns counts for the timeline."""
    counts = {"suppliers_created": 0, "offers_created": 0, "offers_updated": 0, "offers_skipped": 0,
              "rows_not_saved": 0}
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for row in saved_rows:
        if not worth_saving(row):
            counts["rows_not_saved"] += 1
            continue
        parsed = row["raw_result"]
        supplier, created = repo.upsert_supplier(row["supplier_name"], {
            "website": f"https://{row['source_domain']}", "average_lead_time_days": parsed.get("lead_time_days"),
            "notes": f"Discovered by Tavily web search (unverified). First seen at {row['source_url']}"})
        counts["suppliers_created"] += int(created)
        _, action = repo.upsert_supplier_product(supplier["id"], inventory_item_id, row["product_name"], {
            "unit_price": parsed["pack_price"], "unit": parsed.get("selling_unit") or "pack",
            "pack_size": row["pack_size"], "minimum_order_quantity": row.get("minimum_order_quantity"),
            "delivery_fee": parsed.get("delivery_fee"), "last_verified_at": now})
        counts[f"offers_{action}"] += 1
    return counts
