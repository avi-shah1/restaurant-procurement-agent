"""Let runs saved by an earlier version of the app display fully on the current action card.

The recommendation JSON gained fields over time (percent saving, MOQ, supplier reliability, the incumbent's unit
price, the "stock not confirmed" risk). A run stored before that lacks them, and the card would show "unknown"
for facts we actually have. This fills each gap from data that is already stored (the supplier record, the
supplier price row, the saved market search result). Nothing is invented: if a fact cannot be found it stays
unknown. The database row is never modified; this only shapes what the API returns.
"""
from __future__ import annotations

import copy

STOCK_RISK = "Stock availability has not been confirmed with the supplier."


def upgrade_recommendation(repo, run: dict) -> dict | None:
    rec = run.get("recommendation")
    if not rec:
        return rec
    rec = copy.deepcopy(rec)
    req = run.get("requirement") or {}
    inc = rec.get("incumbent_cost")
    sup = rec["recommended_supplier"]

    if inc is not None and "price_per_unit" not in inc:
        inc["price_per_unit"] = (req.get("incumbent") or {}).get("price_per_unit")
    sav = rec.get("potential_savings")
    if sav is not None and "percent" not in sav:
        total = (inc or {}).get("estimated_total_landed_cost")
        sav["percent"] = round(sav["amount"] / total * 100, 1) if total else None

    option_id = str(rec.get("option_id") or "")
    product = row = None
    if option_id.startswith("db-") and req.get("item"):
        product = next((p for p in repo.list_supplier_products(inventory_item_id=req["item"]["id"])
                        if f"db-{p['id']}" == option_id), None)
    elif option_id.startswith("mkt-"):
        row = next((r for r in repo.list_market_search_results(run["id"]) if f"mkt-{r['id']}" == option_id), None)

    if "moq" not in rec:
        moq = {"known": False, "packs": None, "selling_unit": None, "pack_size": None}
        if product is not None:
            moq = {"known": product.get("minimum_order_quantity") is not None,
                   "packs": product.get("minimum_order_quantity"), "selling_unit": product["unit"],
                   "pack_size": product["pack_size"]}
        elif row is not None:
            moq = {"known": row.get("minimum_order_quantity") is not None, "packs": row.get("minimum_order_quantity"),
                   "selling_unit": (row.get("raw_result") or {}).get("selling_unit") or "pack",
                   "pack_size": row.get("pack_size")}
        rec["moq"] = moq

    if "reliability" not in sup:
        supplier = repo.get_supplier(sup["supplier_id"]) if sup.get("supplier_id") is not None else None
        sup["reliability"] = supplier.get("reliability_score") if supplier else None

    risks = rec.setdefault("risks_and_uncertainties", [])
    if STOCK_RISK not in risks:
        risks.insert(0, STOCK_RISK)
    return rec
