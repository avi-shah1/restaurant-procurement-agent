"""Seeded alternative-supplier "search results" for DEMO. Every name, price and URL is invented.

Shaped like what a live search will return: a price as it appeared on the page, a pack size, and
delivery details that are sometimes missing (None). Missing details are deliberate: they force the
recommendation to say what is uncertain instead of guessing.
"""

# sku -> list of (supplier, product, selling_unit, pack_size, pack_price, pack_description, moq_packs,
#                delivery_fee or None, lead_time_days or None, delivery_information, url path, confidence)
_PRIME = "Prime Provisions Online"
_HARVEST = "Harvest & Co Wholesale"
_TRICOUNTY = "Tri-County Food Supply"
_PANTRY = "Pantry Direct Wholesale"

_P = "primeprovisions.example"
_H = "harvestco-wholesale.example"
_T = "tricounty-foodsupply.example"
_D = "pantrydirect.example"

_FREE_300 = "Free delivery over $300. Delivery time not stated on the listing."

MOCK_RESULTS: dict[str, list[tuple]] = {
    "MOZ-001": [
        (_PRIME, "Low-moisture mozzarella", "case", 6, 45.30, "case 6 x 1kg", 1, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee (per listing).",
         f"{_P}/catalog/mozzarella-lm-6x1kg", 0.62),
        (_HARVEST, "Mozzarella block", "block", 10, 62.00, "10kg catering block", 2, 0.00, None,
         _FREE_300, f"{_H}/dairy/mozzarella-10kg", 0.55),
        (_TRICOUNTY, "Mozzarella case", "case", 12, 81.60, "case 12 x 1kg", 1, 14.00, 4,
         "Delivery in 3-5 business days. $14 delivery fee.", f"{_T}/mozzarella-12x1kg", 0.70),
    ],
    "OAT-001": [
        (_PANTRY, "Barista oat milk", "case", 12, 21.00, "case 12 x 1L", 2, 6.00, 2,
         "2-day delivery. $6 delivery fee.", f"{_D}/oat-barista-12x1l", 0.66),
        (_TRICOUNTY, "Barista oat milk", "case", 6, 12.30, "case 6 x 1L", 2, 14.00, 4,
         "Delivery in 3-5 business days. $14 delivery fee.", f"{_T}/oat-barista-6x1l", 0.70),
    ],
    "AVO-001": [
        (_PRIME, "Hass avocados, ripe and ready", "tray", 20, 15.40, "tray of 20", 2, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee.", f"{_P}/produce/avocado-20", 0.60),
        (_HARVEST, "Avocados", "case", 40, 27.20, "case of 40", 1, 0.00, None,
         _FREE_300, f"{_H}/produce/avocado-40", 0.50),
    ],
    "TOM-001": [
        (_PRIME, "Vine tomatoes", "box", 5, 11.40, "5kg box", 2, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee.", f"{_P}/produce/tomato-5kg", 0.60),
        (_TRICOUNTY, "Tomatoes", "box", 10, 21.50, "10kg box", 2, 14.00, 3,
         "Delivery in 2-4 business days. $14 delivery fee.", f"{_T}/tomato-10kg", 0.68),
    ],
    "COF-001": [
        (_PRIME, "Espresso blend beans", "bag", 1, 15.90, "1kg bag", 3, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee.", f"{_P}/coffee/espresso-1kg", 0.58),
        (_HARVEST, "Espresso blend beans", "bag", 5, 76.00, "5kg bag", 1, 0.00, None,
         _FREE_300, f"{_H}/coffee/espresso-5kg", 0.50),
    ],
    "MLK-001": [
        (_TRICOUNTY, "Whole milk", "case", 24, 21.60, "case 24 x 1L", 3, 14.00, 3,
         "Delivery in 2-4 business days. $14 delivery fee.", f"{_T}/milk-24x1l", 0.68),
    ],
    "EGG-001": [
        (_PRIME, "Free-range large eggs", "tray", 30, 8.10, "tray of 30", 4, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee.", f"{_P}/eggs/fr-large-30", 0.60),
    ],
    "FLR-001": [
        (_HARVEST, "00 pizza flour", "sack", 25, 19.50, "25kg sack", 2, 0.00, None,
         _FREE_300, f"{_H}/dry/flour-00-25kg", 0.52),
    ],
    "OIL-001": [
        (_TRICOUNTY, "Extra virgin olive oil", "tin", 5, 38.00, "5L tin", 3, 14.00, 4,
         "Delivery in 3-5 business days. $14 delivery fee.", f"{_T}/oil-evoo-5l", 0.66),
    ],
    "CHK-001": [
        (_PRIME, "Chicken breast", "case", 10, 54.00, "10kg case", 2, 9.00, 1,
         "Next-day delivery on orders before 2pm. $9 flat delivery fee.", f"{_P}/meat/chicken-breast-10kg", 0.58),
        (_TRICOUNTY, "Chicken breast", "case", 20, 99.00, "20kg case", 2, 14.00, 3,
         "Delivery in 2-4 business days. $14 delivery fee.", f"{_T}/chicken-breast-20kg", 0.66),
    ],
}
