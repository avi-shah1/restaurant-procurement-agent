"""Create a procurement run from the deterministic forecast, and snapshot what Python calculated."""
from __future__ import annotations

from datetime import date

from ..forecast import forecast_item
from ..money import CURRENCY_CODE
from .options import build_database_options


class NoProcurementNeeded(Exception):
    """The forecast says this item does not need ordering right now."""


def build_requirement(repo, item_id: int, today: date | None = None, event_multiplier=None,
                      events_enabled: list[str] | None = None) -> dict:
    """Run the forecast for one item and turn the result into a requirement snapshot.

    `repo` should already have the wanted scenario applied (a ScenarioRepository).
    """
    today = today or date.today()
    item = repo.get_inventory_item(item_id)
    if item is None:
        raise KeyError(f"No inventory item {item_id}")
    f = forecast_item(item, repo.get_usage_history(item_id), today, event_multiplier)
    if not f["requires_procurement"]:
        raise NoProcurementNeeded(
            f"{item['name']} does not need ordering today (status: {f['status']}). {f['explanation']}")

    m = f["usage_model"]
    requirement = {
        "as_of": today.isoformat(), "currency": CURRENCY_CODE,
        "scenario": getattr(repo, "scenario_key", None), "events_enabled": events_enabled or [],
        "item": {**f["item"], "id": item_id, "category": item.get("category")},
        "status": f["status"], "current_stock": f["current_stock"], "safety_stock": f["safety_stock"],
        "required_quantity": f["required_quantity"], "required_by": f["required_by"],
        "predicted_breach_date": f["predicted_breach_date"],
        "predicted_stockout_date": f["predicted_stockout_date"], "days_of_cover": f["days_of_cover"],
        "seven_day_forecast": f["seven_day_forecast"],
        "usage_model": {k: m[k] for k in ("trend_multiplier", "trend_capped", "recent_7_day_average",
                                          "previous_7_day_average")},
        "incoming": f["incoming"],
        "incumbent_lead_time_days": f["lead_time_days"],
        "earliest_delivery_date_with_incumbent": f["earliest_delivery_date"],
        "incumbent_can_meet_required_by": f["can_meet_required_by"],
        "order_by_date": f["order_by_date"],
        "lead_time_budget_days": f["lead_time_budget_days"],
        "forecast_explanation": f["explanation"],
    }
    incumbent = next((o for o in build_database_options(repo, requirement) if o["is_incumbent"]), None)
    requirement["incumbent"] = None if incumbent is None else {
        k: incumbent[k] for k in (
            "option_id", "supplier_name", "supplier_id", "product_name", "price_per_unit",
            "packs_to_order", "order_quantity", "goods_cost", "delivery_fee",
            "estimated_total_landed_cost", "lead_time_days", "arrives_by", "meets_required_by",
            "avoids_stockout", "supplier_reliability")}
    return requirement


def create_run(repo, item_id: int, today: date | None = None, event_multiplier=None,
               events_enabled: list[str] | None = None) -> dict:
    """Snapshot the requirement and insert a procurement_runs row (status 'pending')."""
    req = build_requirement(repo, item_id, today, event_multiplier, events_enabled)
    return repo.create_procurement_run({
        "inventory_item_id": item_id,
        "required_quantity": req["required_quantity"],
        "required_by": req["required_by"],
        "status": "pending",
        "predicted_stockout_date": req["predicted_stockout_date"],
        "current_supplier_cost": req["incumbent"]["estimated_total_landed_cost"] if req["incumbent"] else None,
        "requirement": req,
    })
