"""Turn supplier offers into fully priced, comparable options for one procurement requirement.

An option is one concrete way to buy the required quantity: who, what pack, how many packs, the
landed cost, and whether it arrives in time. Python calculates every field; the agent reads them.

Unknowns are explicit. A value the source did not state is None, never a guess, and each option lists
`known_fields` / `unknown_fields` so the agent and the UI can see exactly what is and is not established.
Where a landed-cost calculation needs a value we do not have, the *_known flag says what was assumed
(a missing delivery fee counts as $0, a missing MOQ as one pack) and the recommendation lists it as a risk.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ..money import CURRENCY_CODE
from ..suppliers import plan_order, price_per_inventory_unit

STALE_PRICE_DAYS = 30
FIELDS = ("price", "pack_size", "moq", "delivery_fee", "lead_time", "delivery_information")


def _age_days(iso_ts: str | None, as_of: date) -> int | None:
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (as_of - ts.date()).days


def _timing(requirement: dict, lead_time_days: int | None) -> dict:
    """Does a delivery with this lead time arrive in time? None means we do not know."""
    if lead_time_days is None:
        return {"lead_time_days": None, "lead_time_known": False, "arrives_by": None,
                "meets_required_by": None, "avoids_stockout": None}
    as_of = date.fromisoformat(requirement["as_of"])
    arrives = as_of + timedelta(days=lead_time_days)
    avoid_budget = requirement["lead_time_budget_days"]["avoid_stockout"]
    return {
        "lead_time_days": lead_time_days, "lead_time_known": True,
        "arrives_by": arrives.isoformat(),
        "meets_required_by": arrives <= date.fromisoformat(requirement["required_by"]),
        "avoids_stockout": True if avoid_budget is None else lead_time_days <= avoid_budget,
    }


def _field_status(known: dict[str, bool]) -> dict:
    return {"known_fields": [f for f in FIELDS if known.get(f)], "unknown_fields": [f for f in FIELDS if not known.get(f)]}


def database_option(requirement: dict, product: dict, supplier: dict, incumbent_supplier_id: int) -> dict:
    """An option built from a supplier_products row (a supplier we already know)."""
    as_of = date.fromisoformat(requirement["as_of"])
    moq = product.get("minimum_order_quantity")
    fee = product.get("delivery_fee")
    lead = supplier.get("average_lead_time_days")
    plan = plan_order(
        requirement["required_quantity"], float(product["pack_size"]), float(product["unit_price"]),
        float(moq) if moq is not None else 1.0, float(fee) if fee is not None else 0.0,
        float(supplier.get("minimum_order_value") or 0))
    age = _age_days(product.get("last_verified_at"), as_of)
    is_incumbent = product["supplier_id"] == incumbent_supplier_id
    reliability = supplier.get("reliability_score")
    return {
        "option_id": f"db-{product['id']}",
        "origin": "database",
        "supplier_name": supplier["name"], "supplier_id": supplier["id"], "is_incumbent": is_incumbent,
        "evidence": "incumbent_price_on_file" if is_incumbent else "supplier_price_on_file",
        "price_confirmed": product["source_type"] != "tavily_live",
        "price_last_verified": product.get("last_verified_at"), "price_age_days": age,
        "price_is_stale": age is not None and age > STALE_PRICE_DAYS,
        "product_name": product["supplier_product_name"], "selling_unit": product["unit"],
        "pack_size": float(product["pack_size"]), "price_per_pack": float(product["unit_price"]),
        "price_per_unit": round(price_per_inventory_unit(product), 4),
        "minimum_order_packs": float(moq) if moq is not None else None, "moq_known": moq is not None,
        "supplier_minimum_order_value": float(supplier.get("minimum_order_value") or 0),
        **plan, "delivery_fee_known": fee is not None,
        **_timing(requirement, int(lead) if lead is not None else None),
        "supplier_reliability": float(reliability) if reliability is not None else None,
        "delivery_information": None, "source_url": None, "source_domain": None, "confidence": None,
        "is_retail": False, "price_is_range": False, "caveats": [],
        **_field_status({"price": True, "pack_size": True, "moq": moq is not None, "delivery_fee": fee is not None,
                         "lead_time": lead is not None, "delivery_information": False}),
        "currency": CURRENCY_CODE,
    }


def market_option(requirement: dict, row: dict) -> dict | None:
    """An option from a market_search_results row (a web result, NOT a confirmed quote).

    Returns None when the row cannot be priced (no price, or no pack size to convert it): such rows are
    kept as leads, but they are not options.
    """
    parsed = row.get("raw_result") or {}
    if row.get("normalised_unit_price") is None or row.get("pack_size") is None or parsed.get("pack_price") is None:
        return None
    if parsed.get("price_suspect"):  # far from known prices: almost certainly a misread, so not an option
        return None
    pack_size = float(row["pack_size"])
    pack_price = float(parsed["pack_price"])
    fee = parsed.get("delivery_fee")
    moq = row.get("minimum_order_quantity")
    plan = plan_order(requirement["required_quantity"], pack_size, pack_price,
                      float(moq) if moq is not None else 1.0, float(fee) if fee is not None else 0.0)
    lead = parsed.get("lead_time_days")
    known = set(parsed.get("known_fields") or [])
    return {
        "option_id": f"mkt-{row['id']}",
        "origin": "market_search",
        "supplier_name": row["supplier_name"], "supplier_id": None, "is_incumbent": False,
        "evidence": "web_search_result_unconfirmed",
        "price_confirmed": False, "price_last_verified": None, "price_age_days": None,
        "price_is_stale": False,
        "product_name": row["product_name"], "selling_unit": parsed.get("selling_unit") or "pack",
        "pack_size": pack_size, "price_per_pack": pack_price,
        "price_per_unit": round(float(row["normalised_unit_price"]), 4),
        "minimum_order_packs": float(moq) if moq is not None else None, "moq_known": moq is not None,
        "supplier_minimum_order_value": None,
        **plan, "delivery_fee_known": fee is not None,
        **_timing(requirement, lead),
        "supplier_reliability": None,
        "delivery_information": row.get("delivery_information"),
        "source_url": row.get("source_url"), "source_domain": row.get("source_domain"),
        "confidence": row.get("confidence"),
        "is_retail": bool(parsed.get("is_retail")), "price_is_range": bool(parsed.get("price_is_range")),
        "caveats": list(parsed.get("caveats") or []),
        "known_fields": [f for f in FIELDS if f in known], "unknown_fields": [f for f in FIELDS if f not in known],
        "currency": CURRENCY_CODE,
    }


def unpriced_lead(row: dict) -> dict:
    """A search result we kept but could not turn into a priced option, and why."""
    parsed = row.get("raw_result") or {}
    if parsed.get("price_suspect"):
        why = "the price looks implausible next to what we already pay, so it is probably a misread of the page"
    elif parsed.get("advertised_price") is None and row.get("raw_price") is None and not parsed.get("pack_price"):
        why = "no price found in the search result"
    elif row.get("pack_size") is None:
        why = "a price was found but the pack size is not stated, so it cannot be converted to a unit price"
    else:
        why = "could not be converted to a unit price"
    return {"supplier_name": row["supplier_name"], "product_name": row["product_name"],
            "source_url": row.get("source_url"), "source_domain": row.get("source_domain"),
            "advertised_price": row.get("raw_price"), "why_not_an_option": why,
            "caveats": list(parsed.get("caveats") or []), "confidence": row.get("confidence")}


def build_database_options(repo, requirement: dict, include_discovered: bool = False) -> list[dict]:
    """Every supplier we already know that sells this item, priced for the required quantity.

    Web-discovered offers (source_type 'tavily_live') are excluded by default: they are search results
    and arrive through search_market_prices, so including them here would list the same offer twice.
    """
    item_id = requirement["item"]["id"]
    item = repo.get_inventory_item(item_id)
    suppliers = {s["id"]: s for s in repo.list_suppliers()}
    options = [database_option(requirement, p, suppliers[p["supplier_id"]], item["current_supplier_id"])
               for p in repo.list_supplier_products(inventory_item_id=item_id)
               if include_discovered or p["source_type"] != "tavily_live"]
    return sorted(options, key=lambda o: (o["estimated_total_landed_cost"], o["option_id"]))


def build_market_options(requirement: dict, rows: list[dict]) -> list[dict]:
    options = [o for o in (market_option(requirement, r) for r in rows) if o is not None]
    return sorted(options, key=lambda o: (o["estimated_total_landed_cost"], o["option_id"]))


def find_option(options: list[dict], option_id: str) -> dict | None:
    return next((o for o in options if o["option_id"] == option_id), None)
