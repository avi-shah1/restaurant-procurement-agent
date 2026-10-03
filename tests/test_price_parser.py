"""The price parser: real Tavily snippets first, then hand-worked cases. The rule under test is that
anything the text does not clearly state comes back as None, never as a guess."""
import json
from pathlib import Path

import pytest

from app.procurement.price_parser import confidence, parse_listing

LB, OZ = 0.45359237, 0.028349523125
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tavily_mozzarella_sample.json").read_text())
REAL = {r["url"]: r for q in FIXTURE for r in q["results"]}


def real(fragment: str) -> dict:
    """Parse the real Tavily result whose URL contains `fragment`."""
    r = next(v for u, v in REAL.items() if fragment in u)
    return parse_listing(r["content"], "kg", title=r["title"], url=r["url"])


# ---------- real search results (Tavily, mozzarella, 2026-10-03) ----------
def test_real_supermarket_listing_is_normalised_and_flagged_retail():
    p = real("tomthumb")
    assert p["advertised_price"] == 7.99 and p["pack_size"] == pytest.approx(32 * OZ)
    assert p["normalised_unit_price"] == pytest.approx(7.99 / (32 * OZ), rel=1e-3)  # about $8.81 per kg
    assert p["is_retail"] is True and any("retail" in c for c in p["caveats"])
    assert p["moq_packs"] is None and p["delivery_fee"] is None and p["lead_time_days"] is None  # never invented
    assert {"moq", "delivery_fee", "lead_time"} <= set(p["unknown_fields"])


def test_real_listing_with_conflicting_sizes_has_unknown_pack():
    p = real("wisconsincheesemart")  # "$9.75", but both 1lb and 3 lb are mentioned
    assert p["advertised_price"] == 9.75 and p["pack_size"] is None and p["normalised_unit_price"] is None
    assert any("Conflicting pack sizes" in c for c in p["caveats"])


def test_real_price_range_without_a_unit_is_not_normalised():
    p = real("alibaba")  # "$3-4. Min. Order: 500 kilograms."
    assert p["price_is_range"] is True and p["advertised_price"] == 4.0   # upper bound, flagged
    assert p["normalised_unit_price"] is None and p["pack_size"] is None
    assert p["moq_inventory_units"] == 500 and p["moq_packs"] is None     # stated, but cannot become packs
    assert any("range" in c for c in p["caveats"])


def test_real_listings_with_no_price_stay_unpriced():
    for fragment in ("webstaurantstore", "puredairyfoodservice", "amazon.com", "tridge", "tiktok"):
        p = real(fragment)
        assert p["advertised_price"] is None and p["normalised_unit_price"] is None, fragment
    assert real("puredairyfoodservice")["pack_size"] == 12.0  # "6 x 2kg" is stated, so the pack IS known


GFS = json.loads((Path(__file__).parent / "fixtures" / "tavily_gfs_sample.json").read_text())


@pytest.mark.parametrize("snippet, per_lb", list(zip(GFS, (2.29, 2.39, 2.39))), ids=["435776", "432984", "category"])
def test_real_gfs_prices_per_pound_with_punctuation_before_the_slash(snippet, per_lb):
    """Regression. "$2.29. /lb per loaf." is a price PER POUND. It was read as the price of the whole 6 lb loaf,
    giving $0.84/kg (six times too low) at 0.81 confidence."""
    p = parse_listing(snippet["content"], "kg", title=snippet["title"], url=snippet["url"])
    assert p["price_basis"] == "per_unit" and p["advertised_price"] == per_lb
    assert p["pack_size"] == pytest.approx(LB)                                   # one pound, not the 6 lb loaf
    assert p["normalised_unit_price"] == pytest.approx(per_lb / LB, rel=1e-4)    # about $5.05 per kg


def test_how_many_real_results_could_be_priced():
    priced = [u for u, r in REAL.items()
              if parse_listing(r["content"], "kg", title=r["title"], url=u)["normalised_unit_price"] is not None]
    assert len(REAL) == 12 and len(priced) == 2  # the parser prefers "unknown" to a wrong number


# ---------- prices and pack sizes ----------
def test_price_per_pound_is_converted_to_per_kg():
    p = parse_listing("Fresh mozzarella wholesale $4.99/lb", "kg")
    assert p["price_basis"] == "per_unit" and p["pack_size"] == pytest.approx(LB)
    assert p["normalised_unit_price"] == pytest.approx(4.99 / LB, rel=1e-4)


def test_case_price_with_a_multipack():
    p = parse_listing("Mozzarella $38.40 / case of 6 x 1 kg", "kg")
    assert p["pack_size"] == 6.0 and p["pack_price"] == 38.40 and p["normalised_unit_price"] == pytest.approx(6.4)


def test_cents_are_never_mistaken_for_a_case_count():
    """Regression: "$52.99 / Case" used to read the 99 as '99 per case', which broke the 6/Case multiplier."""
    p = parse_listing("Mozzarella 5 lb. bag - 6/Case $52.99 / Case.", "kg")
    assert p["pack_size"] == pytest.approx(30 * LB) and p["normalised_unit_price"] == pytest.approx(52.99 / (30 * LB), rel=1e-4)
    assert parse_listing("Eggs, 30 count tray $12.30 / Tray", "each")["pack_size"] == 30  # not "30" and "30" clashing


def test_count_per_case_multiplies_a_single_size():
    p = parse_listing("Mozzarella 5 lb loaf, 6 per case. $52.99", "kg")
    assert p["pack_size"] == pytest.approx(30 * LB) and p["normalised_unit_price"] == pytest.approx(52.99 / (30 * LB), rel=1e-4)


def test_volume_and_count_units():
    assert parse_listing("Oat milk 12 x 1L case $22.20", "L")["normalised_unit_price"] == pytest.approx(1.85)
    assert parse_listing("Milk $3.99 per gallon", "L")["normalised_unit_price"] == pytest.approx(3.99 / 3.785411784, rel=1e-4)
    assert parse_listing("Large eggs $4.50 per dozen", "each")["normalised_unit_price"] == pytest.approx(0.375)
    assert parse_listing("Eggs, 30 count tray $8.10", "each")["normalised_unit_price"] == pytest.approx(0.27)


def test_ounces_mean_fluid_ounces_for_liquids_and_weight_for_solids():
    assert parse_listing("Oat milk 32 oz carton $3.20", "L")["pack_size"] == pytest.approx(32 * 0.0295735295625)
    assert parse_listing("Cheese 32 oz bag $7.00", "kg")["pack_size"] == pytest.approx(32 * OZ)


def test_conflicting_or_missing_sizes_mean_unknown():
    p = parse_listing("Choose a 5 lb bag or a 10 lb bag. $20.00", "kg")
    assert p["pack_size"] is None and p["normalised_unit_price"] is None
    p = parse_listing("Great mozzarella for pizza. $20.00", "kg")
    assert p["advertised_price"] == 20.0 and p["pack_size"] is None and p["normalised_unit_price"] is None
    assert parse_listing("No numbers here at all", "kg")["advertised_price"] is None


def test_prices_that_are_not_the_product_price_are_ignored():
    assert parse_listing("Was $12.99, now $9.99 for 1 lb", "kg")["advertised_price"] == 9.99
    assert parse_listing("Pay over time for orders over $35.00. Price $9.75 for 1 lb", "kg")["advertised_price"] == 9.75
    assert parse_listing("Save $5 on your first order. $20.00 for 2 lb", "kg")["advertised_price"] == 20.0
    assert parse_listing("Mozzarella 2 lb $12.00 plus $9 flat rate shipping", "kg")["advertised_price"] == 12.0


def test_a_disagreeing_unit_price_is_flagged():
    p = parse_listing("Mozzarella $10.00 for 1 kg. That is $2.00 per kg!", "kg")
    assert any("disagree" in c for c in p["caveats"])


# ---------- minimum order ----------
def test_minimum_order_in_packs_and_in_units():
    assert parse_listing("Case of 6 x 1kg $40. Min. Order: 20 cases", "kg")["moq_packs"] == 20
    assert parse_listing("Mozzarella 6 x 1kg case $40. Minimum order of 2 cases", "kg")["moq_packs"] == 2
    p = parse_listing("Mozzarella $3/kg. MOQ: 500 kg", "kg")
    assert p["moq_inventory_units"] == 500 and p["moq_packs"] == 500  # per-kg pack, so 500 packs
    assert parse_listing("Mozzarella 6 x 1kg case $40", "kg")["moq_packs"] is None   # not stated: unknown


# ---------- delivery ----------
@pytest.mark.parametrize("text, lead, fee", [
    ("Next-day delivery available. $9 flat rate shipping.", 1, 9.0),
    ("Ships in 2-3 business days.", 3, None),
    ("Delivery in 3-5 business days. $14 delivery fee.", 5, 14.0),
    ("Free delivery on every order.", None, 0.0),
    ("Same-day delivery. Shipping: $12.50", 0, 12.5),
    ("Order today, we ship within 24 hours.", 1, None),
    ("Nothing about delivery here.", None, None),
])
def test_delivery_details(text, lead, fee):
    d = parse_listing("Mozzarella 6 x 1kg case $40. " + text, "kg")
    assert d["lead_time_days"] == lead and d["delivery_fee"] == fee


def test_conditional_free_shipping_leaves_the_fee_unknown():
    p = parse_listing("Mozzarella 2 lb $12. Free shipping on orders over $99.", "kg")
    assert p["delivery_fee"] is None and "delivery_fee" in p["unknown_fields"]
    assert any("conditional" in c for c in p["caveats"])


def test_a_delivery_estimate_is_labelled_as_the_listings_own():
    p = parse_listing("Mozzarella 2 lb $12. Delivery in 2-3 days.", "kg")
    assert p["lead_time_days"] == 3 and any("not a commitment" in c for c in p["caveats"])


# ---------- confidence ----------
def test_confidence_rewards_stated_facts_and_penalises_retail_and_ranges():
    full = parse_listing("Mozzarella 6 x 1kg $40. Next-day delivery. $9 shipping.", "kg", url="https://wholesaler.example/x")
    bare = parse_listing("Mozzarella is great.", "kg", url="https://wholesaler.example/y")
    retail = parse_listing("Mozzarella 6 x 1kg $40. Next-day delivery. $9 shipping.", "kg", url="https://www.walmart.com/x")
    ranged = parse_listing("Mozzarella $3-4 per kg", "kg", url="https://wholesaler.example/z")
    assert confidence(0.7, full) > confidence(0.7, bare)
    assert confidence(0.7, full) > confidence(0.7, retail)
    assert confidence(0.7, ranged) < confidence(0.7, parse_listing("Mozzarella $4 per kg", "kg"))
    assert all(0.05 <= confidence(s, p) <= 0.95 for s in (0, 0.5, 1.0, None) for p in (full, bare, retail, ranged))


def test_known_and_unknown_fields_always_partition_the_same_fields():
    p = parse_listing("Mozzarella 6 x 1kg $40.", "kg")
    assert sorted(p["known_fields"] + p["unknown_fields"]) == sorted(
        ["price", "pack_size", "moq", "delivery_fee", "lead_time", "delivery_information"])
    assert "price" in p["known_fields"] and "pack_size" in p["known_fields"]
