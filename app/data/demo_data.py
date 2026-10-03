"""Deterministic DEMO / SIMULATED restaurant data.

Everything here is invented. Rows use natural keys (supplier name, item sku) so the same dataset
can be loaded into the local JSON store or into Supabase, each assigning its own ids.
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta, timezone

SEED = 42
HISTORY_DAYS = 28

# Mon..Sun. Weekdays lower, Friday higher, Saturday highest, Sunday moderately high.
WEEKDAY_FACTORS = {0: 0.80, 1: 0.85, 2: 0.90, 3: 0.95, 4: 1.25, 5: 1.50, 6: 1.15}
_MEAN_FACTOR = sum(WEEKDAY_FACTORS.values()) / 7  # so avg_daily stays the true weekly average
NOISE = 0.06  # +/- 6% random variation

_DEMO = "DEMO/SIMULATED. "

# short key -> (name, website, email, reliability 0-1, avg lead days, min order value, delivery fee)
_SUPPLIERS = {
    "METRO": ("Metro Foodservice Wholesale", "metro-foodservice", 0.94, 2, 120.00, 0.00,
              "Broad-line wholesaler. Fair prices on everything, free delivery over the minimum."),
    "ALPINE": ("Alpine Dairy Direct", "alpine-dairy", 0.97, 3, 80.00, 8.00,
               "Dairy specialist. Excellent quality and reliability, premium prices, 3-day lead."),
    "BARGAIN": ("BargainBox Cash & Carry", "bargainbox", 0.78, 4, 250.00, 15.00,
                "Cheapest on most lines but big pack sizes, high minimum order, slow and patchy delivery."),
    "FRESH": ("FreshRoute Produce", "freshroute", 0.91, 1, 60.00, 12.00,
              "Produce and eggs. Next-day delivery, good freshness, mid pricing."),
    "BEAN": ("Bean & Barrel Roasters", "beanbarrel", 0.96, 2, 90.00, 10.00,
             "Coffee roaster and speciality dry goods. Very reliable."),
    "CONT": ("Continental Italian Imports", "continental-imports", 0.88, 5, 300.00, 25.00,
             "Italian importer. Cheap on mozzarella, oil and flour but 5-day lead and a large minimum."),
    "QUICK": ("QuickStock Express", "quickstock", 0.92, 1, 40.00, 5.00,
              "Emergency / next-day supplier. Small packs, low minimum, expensive per unit."),
}

# sku, name, category, unit, avg daily use, stock, safety, incumbent, price/unit, lead days,
# incoming qty, incoming in N days
_ITEMS = [
    ("MOZ-001", "Mozzarella (low-moisture)", "Dairy", "kg", 6.3, 14, 12, "ALPINE", 7.20, 3, 10, 5),
    ("COF-001", "Coffee beans (espresso blend)", "Beverages", "kg", 3.1, 30, 8, "BEAN", 16.50, 2, 0, None),
    ("OAT-001", "Oat milk (barista)", "Dairy alternatives", "L", 12.6, 78, 30, "METRO", 1.85, 2, 0, None),
    ("MLK-001", "Whole milk", "Dairy", "L", 26.0, 260, 60, "ALPINE", 0.95, 3, 0, None),
    ("TOM-001", "Tomatoes (vine)", "Produce", "kg", 10.5, 90, 25, "FRESH", 2.40, 1, 0, None),
    ("FLR-001", "Flour (00 pizza)", "Dry goods", "kg", 8.4, 250, 60, "METRO", 0.85, 2, 0, None),
    ("EGG-001", "Eggs (free-range, large)", "Dairy & eggs", "each", 94, 1200, 300, "FRESH", 0.28, 1, 0, None),
    ("AVO-001", "Avocados (ripe & ready)", "Produce", "each", 36.8, 320, 70, "FRESH", 0.85, 1, 0, None),
    ("OIL-001", "Olive oil (extra virgin)", "Dry goods", "L", 3.2, 60, 15, "METRO", 8.90, 2, 0, None),
    ("CHK-001", "Chicken breast", "Meat", "kg", 14.7, 130, 35, "METRO", 5.60, 2, 40, 2),
]

# sku, supplier, price per inventory unit, selling unit, pack size (inventory units), description, MOQ in packs
_OFFERS = [
    ("MOZ-001", "ALPINE", 7.20, "case", 6, "case 6 x 1kg", 1),
    ("MOZ-001", "METRO", 7.45, "case", 6, "case 6 x 1kg", 1),
    ("MOZ-001", "BARGAIN", 6.35, "block", 10, "10kg catering block", 2),
    ("MOZ-001", "CONT", 6.10, "case", 12, "case 12 x 1kg", 2),
    ("MOZ-001", "QUICK", 8.10, "bag", 2, "2kg bag", 1),
    ("COF-001", "BEAN", 16.50, "bag", 1, "1kg bag", 5),
    ("COF-001", "METRO", 17.20, "bag", 1, "1kg bag", 3),
    ("COF-001", "BARGAIN", 14.90, "bag", 5, "5kg bag", 2),
    ("COF-001", "QUICK", 18.90, "bag", 1, "1kg bag", 1),
    ("OAT-001", "METRO", 1.85, "case", 12, "case 12 x 1L", 2),
    ("OAT-001", "BARGAIN", 1.62, "layer", 72, "layer 72 x 1L", 1),
    ("OAT-001", "ALPINE", 1.98, "case", 12, "case 12 x 1L", 2),
    ("OAT-001", "QUICK", 2.25, "case", 6, "case 6 x 1L", 1),
    ("MLK-001", "ALPINE", 0.95, "crate", 24, "crate 12 x 2L", 2),
    ("MLK-001", "METRO", 1.02, "case", 12, "case 12 x 1L", 2),
    ("MLK-001", "BARGAIN", 0.88, "case", 24, "case 24 x 1L", 5),
    ("MLK-001", "QUICK", 1.20, "case", 12, "case 6 x 2L", 1),
    ("TOM-001", "FRESH", 2.40, "box", 6, "6kg box", 2),
    ("TOM-001", "METRO", 2.55, "box", 5, "5kg box", 2),
    ("TOM-001", "BARGAIN", 2.10, "box", 10, "10kg box", 3),
    ("TOM-001", "QUICK", 3.05, "tray", 3, "3kg tray", 1),
    ("FLR-001", "METRO", 0.85, "sack", 16, "16kg sack", 2),
    ("FLR-001", "BARGAIN", 0.72, "sack", 25, "25kg sack", 4),
    ("FLR-001", "CONT", 0.98, "sack", 25, "25kg sack, imported 00", 2),
    ("EGG-001", "FRESH", 0.28, "tray", 30, "tray of 30", 4),
    ("EGG-001", "METRO", 0.30, "tray", 30, "tray of 30", 4),
    ("EGG-001", "BARGAIN", 0.24, "case", 360, "case of 360", 1),
    ("EGG-001", "QUICK", 0.35, "box", 30, "box of 30", 1),
    ("AVO-001", "FRESH", 0.85, "tray", 20, "tray of 20", 2),
    ("AVO-001", "METRO", 0.92, "case", 20, "case of 20", 2),
    ("AVO-001", "BARGAIN", 0.74, "case", 40, "case of 40", 3),
    ("AVO-001", "QUICK", 1.10, "tray", 10, "tray of 10", 1),
    ("OIL-001", "METRO", 8.90, "case", 6, "case 6 x 1L", 2),
    ("OIL-001", "CONT", 7.60, "tin", 5, "5L tin", 4),
    ("OIL-001", "BEAN", 9.40, "case", 4, "case 4 x 1L", 2),
    ("OIL-001", "BARGAIN", 7.95, "tin", 5, "5L tin", 6),
    ("CHK-001", "METRO", 5.60, "case", 10, "10kg case", 2),
    ("CHK-001", "BARGAIN", 4.95, "case", 20, "20kg case", 3),
    ("CHK-001", "QUICK", 6.40, "pack", 2.5, "2.5kg pack", 2),
]

TABLES = ["suppliers", "inventory_items", "usage_history", "supplier_products",
          "procurement_runs", "market_search_results", "agent_events", "rfq_drafts"]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def build_demo_data(today: date | None = None) -> dict[str, list[dict]]:
    """Return the dataset keyed by table. Same `today` -> identical output."""
    today = today or date.today()
    now = datetime.combine(today, time(8, 0), tzinfo=timezone.utc)

    suppliers = []
    fees = {}
    for key, (name, domain, rel, lead, min_order, fee, notes) in _SUPPLIERS.items():
        fees[key] = fee
        suppliers.append({
            "name": name,
            "website": f"https://{domain}.example",
            "contact_email": f"orders@{domain}.example",
            "reliability_score": rel,
            "average_lead_time_days": lead,
            "minimum_order_value": min_order,
            "notes": _DEMO + notes,
        })

    items, usage = [], []
    rng = random.Random(SEED)
    incumbent = {}
    item_by_sku = {}
    for (sku, name, category, unit, avg_daily, stock, safety, sup, price, lead,
         inc_qty, inc_days) in _ITEMS:
        incumbent[sku] = sup
        item_by_sku[sku] = name
        items.append({
            "sku": sku, "name": name, "category": category, "unit": unit,
            "current_stock": stock, "safety_stock": safety,
            "current_supplier": _SUPPLIERS[sup][0],
            "current_unit_price": price, "lead_time_days": lead,
            "incoming_quantity": inc_qty,
            "incoming_date": (today + timedelta(days=inc_days)).isoformat() if inc_days else None,
            "updated_at": _iso(now),
        })
        for offset in range(HISTORY_DAYS, 0, -1):  # 28 days ending yesterday
            day = today - timedelta(days=offset)
            qty = avg_daily * WEEKDAY_FACTORS[day.weekday()] / _MEAN_FACTOR
            qty *= rng.uniform(1 - NOISE, 1 + NOISE)
            usage.append({"sku": sku, "usage_date": day.isoformat(),
                          "quantity_used": round(qty) if unit == "each" else round(qty, 1)})

    age_days = {"incumbent": 3, "historical": 45, "mock": 14}
    products = []
    for sku, sup, per_unit, sell_unit, pack, desc, moq in _OFFERS:
        if sup == incumbent[sku]:
            source = "incumbent"
        elif sup == "METRO":
            source = "historical"  # we have bought from the wholesaler before
        else:
            source = "mock"
        products.append({
            "supplier": _SUPPLIERS[sup][0], "sku": sku,
            "supplier_product_name": f"{item_by_sku[sku]} - {desc}",
            "unit_price": round(per_unit * pack, 2), "unit": sell_unit, "pack_size": pack,
            "minimum_order_quantity": moq, "delivery_fee": fees[sup],
            "last_verified_at": _iso(now - timedelta(days=age_days[source])),
            "source_type": source,
        })

    return {"suppliers": suppliers, "inventory_items": items, "usage_history": usage,
            "supplier_products": products, "procurement_runs": [], "market_search_results": []}


def to_relational(data: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Replace natural keys with sequential integer ids (what the local store keeps)."""
    sup_id = {s["name"]: i for i, s in enumerate(data["suppliers"], 1)}
    item_id = {it["sku"]: i for i, it in enumerate(data["inventory_items"], 1)}
    out = {t: [] for t in TABLES}
    out["suppliers"] = [{"id": sup_id[s["name"]], **s} for s in data["suppliers"]]
    for it in data["inventory_items"]:
        row = {k: v for k, v in it.items() if k != "current_supplier"}
        out["inventory_items"].append(
            {"id": item_id[it["sku"]], **row, "current_supplier_id": sup_id[it["current_supplier"]]})
    for n, u in enumerate(data["usage_history"], 1):
        out["usage_history"].append({
            "id": n, "inventory_item_id": item_id[u["sku"]],
            "usage_date": u["usage_date"], "quantity_used": u["quantity_used"]})
    for n, p in enumerate(data["supplier_products"], 1):
        row = {k: v for k, v in p.items() if k not in ("supplier", "sku")}
        out["supplier_products"].append({
            "id": n, "supplier_id": sup_id[p["supplier"]],
            "inventory_item_id": item_id[p["sku"]], **row})
    return out
