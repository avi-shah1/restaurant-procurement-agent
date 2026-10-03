"""Human-readable snapshot of the loaded data, for /debug/data and quick checks."""
from __future__ import annotations

from datetime import date

from ..suppliers import price_per_inventory_unit
from .repository import Repository

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def build_data_summary(repo: Repository) -> dict:
    suppliers = {s["id"]: s for s in repo.list_suppliers()}
    items = []
    for it in repo.list_inventory_items():
        usage = repo.get_usage_history(it["id"])
        by_day: dict[str, list[float]] = {d: [] for d in _DAYS}
        for u in usage:
            by_day[_DAYS[date.fromisoformat(u["usage_date"]).weekday()]].append(float(u["quantity_used"]))
        total = sum(float(u["quantity_used"]) for u in usage)
        avg_daily = total / len(usage) if usage else 0.0
        offers = []
        for p in repo.list_supplier_products(inventory_item_id=it["id"]):
            s = suppliers[p["supplier_id"]]
            offers.append({
                "supplier": s["name"], "source_type": p["source_type"],
                "price_per_unit": round(price_per_inventory_unit(p), 3),
                "pack": f'{p["pack_size"]:g} {it["unit"]} per {p["unit"]}',
                "moq_packs": p["minimum_order_quantity"], "delivery_fee": p["delivery_fee"],
                "lead_time_days": s["average_lead_time_days"], "reliability": s["reliability_score"],
            })
        offers.sort(key=lambda o: o["price_per_unit"])
        stock, safety = float(it["current_stock"]), float(it["safety_stock"])
        items.append({
            "sku": it["sku"], "name": it["name"], "unit": it["unit"],
            "current_stock": stock, "safety_stock": safety,
            "incumbent": suppliers[it["current_supplier_id"]]["name"],
            "lead_time_days": it["lead_time_days"],
            "incoming": f'{it["incoming_quantity"]:g} on {it["incoming_date"]}' if it["incoming_date"] else None,
            "usage_days": len(usage),
            "avg_daily_use": round(avg_daily, 2),
            "avg_use_by_weekday": {d: round(sum(v) / len(v), 2) if v else None for d, v in by_day.items()},
            # Rough eyeball numbers only. The real forecast is built in Phase 2.
            "rough_days_until_safety_breach": round((stock - safety) / avg_daily, 1) if avg_daily else None,
            "rough_days_until_stockout": round(stock / avg_daily, 1) if avg_daily else None,
            "offers_cheapest_first": offers,
        })
    return {
        "backend": repo.backend_name,
        "scenario": getattr(repo, "scenario_key", None),
        "row_counts": repo.counts(),
        "suppliers": [{k: s[k] for k in ("id", "name", "reliability_score", "average_lead_time_days",
                                         "minimum_order_value")} for s in suppliers.values()],
        "items": items,
    }
