"""Supplier price helpers. Pure Python, no database access. All money is US dollars."""
import math


def price_per_inventory_unit(product: dict) -> float:
    """Normalise a supplier_products row to price per inventory unit (e.g. per kg).

    unit_price is the price of one selling unit (a case); pack_size is how many inventory
    units are in it. So a 6 x 1kg case at 43.20 is 7.20 per kg.
    """
    return float(product["unit_price"]) / float(product["pack_size"])


def plan_order(required_qty: float, pack_size: float, pack_price: float, moq_packs: float = 1,
               delivery_fee: float = 0.0, min_order_value: float = 0.0) -> dict:
    """What it costs to buy `required_qty` inventory units from one supplier, landed.

    1. packs = enough whole packs to cover the requirement, but never fewer than the supplier's
       minimum order quantity (in packs).
    2. If the goods total is below the supplier's minimum order value, buy more packs until it is met.
    3. landed cost = goods + delivery fee.
    The agent never does this arithmetic; it chooses between options priced here.
    """
    packs = max(math.ceil(required_qty / pack_size - 1e-9), math.ceil(moq_packs - 1e-9), 1)
    moq_forced = packs > math.ceil(required_qty / pack_size - 1e-9)
    goods = packs * pack_price
    bumped = False
    if min_order_value and goods + 1e-9 < min_order_value:
        packs = math.ceil(min_order_value / pack_price - 1e-9)
        goods = packs * pack_price
        bumped = True
    order_quantity = packs * pack_size
    return {
        "packs_to_order": packs,
        "order_quantity": round(order_quantity, 3),
        "over_order_quantity": round(max(order_quantity - required_qty, 0.0), 3),
        "goods_cost": round(goods, 2),
        "delivery_fee": round(delivery_fee, 2),
        "estimated_total_landed_cost": round(goods + delivery_fee, 2),
        "forced_by_moq": moq_forced,
        "bumped_for_minimum_order_value": bumped,
    }
