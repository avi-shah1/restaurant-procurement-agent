"""Pull advertised prices, pack sizes, MOQ and delivery details out of web text.

Pure Python: no AI, no network, no randomness. Public web text is messy, so the rule here is:
**if the text does not clearly say it, the answer is None.** Nothing is ever guessed.

What it does when text is unclear:
  - two different pack sizes mentioned           -> pack size unknown (we cannot tell which one the price is for)
  - a price range ("$3-4")                       -> the upper bound is used and the caveat says so
  - a price with no unit and no pack size        -> not normalised (normalised_unit_price is None)
  - "free shipping on orders over $X"            -> shipping cost unknown (it is conditional)
  - delivery estimates ("2-3 business days")     -> the upper bound, reported as the listing's estimate only

Everything is normalised to the inventory item's own unit (kg, L or each), in US dollars.
"""
from __future__ import annotations

import math
import re
from urllib.parse import urlparse

_LB, _OZ = 0.45359237, 0.028349523125
_GAL, _QT, _PT, _FLOZ = 3.785411784, 0.946352946, 0.473176473, 0.0295735295625

_TABLES: dict[str, dict[str, float]] = {
    "kg": {"kg": 1.0, "kgs": 1.0, "kilogram": 1.0, "kilograms": 1.0, "g": 0.001, "gram": 0.001, "grams": 0.001,
           "lb": _LB, "lbs": _LB, "pound": _LB, "pounds": _LB, "oz": _OZ, "ounce": _OZ, "ounces": _OZ},
    "L": {"l": 1.0, "liter": 1.0, "liters": 1.0, "litre": 1.0, "litres": 1.0, "ml": 0.001,
          "gal": _GAL, "gallon": _GAL, "gallons": _GAL, "qt": _QT, "quart": _QT, "quarts": _QT,
          "pt": _PT, "pint": _PT, "pints": _PT, "floz": _FLOZ, "oz": _FLOZ},  # "oz" means fluid oz for liquids
    "each": {"each": 1.0, "ea": 1.0, "ct": 1.0, "count": 1.0, "pc": 1.0, "pcs": 1.0, "piece": 1.0,
             "pieces": 1.0, "dozen": 12.0, "doz": 12.0},
}

RETAIL_DOMAINS = ("tomthumb.com", "safeway.com", "kroger.com", "walmart.com", "target.com", "albertsons.com",
                  "heb.com", "publix.com", "instacart.com", "amazon.com", "ebay.com", "aldi.us", "wholefoodsmarket.com")

FIELDS = ("price", "pack_size", "moq", "delivery_fee", "lead_time", "delivery_information")


def _unit_regex(table: dict[str, float]) -> str:
    # "fl oz" is written with a space/dot; handled by normalising the token afterwards
    words = sorted((w for w in table if w != "floz"), key=len, reverse=True)
    parts = [re.escape(w) for w in words]
    if "floz" in table:
        parts.insert(0, r"fl\.?\s*oz")
    return "(?:" + "|".join(parts) + ")"


def _token(raw: str) -> str:
    return re.sub(r"[.\s]", "", raw.lower())


def _num(s: str) -> float:
    return float(s.replace(",", ""))


_PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)")
_RANGE = re.compile(r"\$\s?(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*\$?\s?(\d+(?:\.\d+)?)")


def _decimals(x: float) -> float:
    return round(x, 6)


def _sizes(text: str, table: dict[str, float], spans_to_skip: list[tuple[int, int]]) -> list[tuple[float, str]]:
    """Quantities like '5 Pound', '32 Oz', '1lb' -> (amount in inventory units, text). Skips MOQ phrases."""
    pattern = re.compile(r"(?<![\w.$])(\d+(?:\.\d+)?)\s*-?\s*((?i:" + _unit_regex(table) + r"))(?![a-z])")
    out = []
    for m in pattern.finditer(text):
        if any(a <= m.start() < b for a, b in spans_to_skip):
            continue
        factor = table.get(_token(m.group(2)))
        if factor:
            out.append((_decimals(float(m.group(1)) * factor), m.group(0).strip()))
    return out


def _multipacks(text: str, table: dict[str, float]) -> list[tuple[float, str]]:
    """'6 x 2kg', '10x180 g' -> (total in inventory units, text)."""
    pattern = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*-?\s*((?i:" + _unit_regex(table) + r"))(?![a-z])")
    out = []
    for m in pattern.finditer(text):
        factor = table.get(_token(m.group(3)))
        if factor:
            out.append((_decimals(int(m.group(1)) * float(m.group(2)) * factor), m.group(0).strip()))
    return out


def _case_counts(text: str) -> list[int]:
    # (?<![\d.$,]) keeps a count from being the tail of a number: in "$52.99 / Case" the "99" is cents, not 99 per case
    patterns = [r"(?<![\d.$,])(\d+)\s*(?:/|per)\s*(?:case|cs)\b", r"\bcase\s+of\s+(\d+)",
                r"\b(?:pack|tray|box|carton|flat)\s+of\s+(\d+)", r"(?<![\d.$,])(\d+)[\s-]*(?:count|ct)\b"]
    found = []
    for p in patterns:
        found += [int(m.group(1)) for m in re.finditer(p, text, re.IGNORECASE)]
    return sorted(set(found))


def _distinct(values: list[tuple[float, str]]) -> list[tuple[float, str]]:
    seen, out = set(), []
    for v, raw in values:
        key = round(v, 4)
        if key not in seen:
            seen.add(key)
            out.append((v, raw))
    return out


def _moq(text: str, table: dict[str, float]) -> tuple[dict | None, tuple[int, int] | None]:
    """'Min. Order: 500 kilograms', 'minimum order of 2 cases', 'MOQ 20'."""
    unit_alt = _unit_regex(table)
    pattern = re.compile(
        r"(?:min(?:imum)?\.?\s*order(?:\s*(?:quantity|qty))?|moq)\s*(?:of|:|-)?\s*(\d[\d,]*(?:\.\d+)?)\s*"
        r"((?i:" + unit_alt + r"|cases?|packs?|units?|boxes|box|bags?|pieces?))?(?![a-z])", re.IGNORECASE)
    m = pattern.search(text)
    if not m:
        return None, None
    qty, unit_word = _num(m.group(1)), (m.group(2) or "").lower()
    word = _token(unit_word) if unit_word else ""
    if word in table:
        return {"quantity": qty * table[word], "in_inventory_units": True, "text": m.group(0).strip()}, m.span()
    return {"quantity": qty, "in_inventory_units": False, "unit_word": unit_word or None,
            "text": m.group(0).strip()}, m.span()


def _delivery(text: str) -> dict:
    """Shipping fee, lead time and a quoted snippet. Conditional offers leave the fee unknown."""
    info: list[str] = []
    fee = lead = None
    caveats: list[str] = []

    for m in re.finditer(r"free\s+(?:shipping|delivery)[^.;\n]{0,50}", text, re.IGNORECASE):
        info.append(m.group(0).strip())
        if re.search(r"\b(?:over|above|on orders?|when you|with orders?|minimum|\$)", m.group(0), re.IGNORECASE):
            caveats.append("Free shipping is conditional (minimum order); the shipping cost is unknown.")
        else:
            fee = 0.0

    if fee is None:
        for pat in (r"\$\s?(\d+(?:\.\d{1,2})?)\s*(?:flat\s*(?:rate\s*)?)?(?:shipping|delivery)(?:\s*(?:fee|cost|charge))?",
                    r"(?:shipping|delivery)(?:\s*(?:fee|cost|charge))?\s*(?:of|:|-|is)?\s*\$\s?(\d+(?:\.\d{1,2})?)"):
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                fee = float(m.group(1))
                info.append(m.group(0).strip())
                break

    if re.search(r"same[-\s]?day\s+(?:delivery|shipping)", text, re.IGNORECASE):
        lead = 0
        info.append("same-day delivery")
    elif re.search(r"next[-\s]?(?:business[-\s])?day\s+(?:delivery|shipping|available|arrival)|ships?\s+next[-\s]?day",
                   text, re.IGNORECASE):
        lead = 1
        info.append("next-day delivery")
    else:
        m = re.search(r"(?:deliver\w*|ship\w*|transit|arriv\w*)[^.;\n]{0,40}?(\d+)\s*(?:-|–|to)\s*(\d+)\s*"
                      r"(?:business\s+)?days?", text, re.IGNORECASE)
        if m:
            lead = int(m.group(2))
            info.append(m.group(0).strip())
        else:
            m = re.search(r"(?:deliver\w*|ship\w*)\s+(?:in|within)\s+(\d+)\s*(?:business\s+)?(days?|hours?)",
                          text, re.IGNORECASE)
            if m:
                n = int(m.group(1))
                lead = n if m.group(2).lower().startswith("day") else math.ceil(n / 24)
                info.append(m.group(0).strip())
    if lead is not None:
        caveats.append("Delivery time is the listing's own estimate, not a commitment.")
    return {"delivery_fee": fee, "lead_time_days": lead,
            "delivery_information": "; ".join(dict.fromkeys(info))[:240] or None, "caveats": caveats}


def _prices(text: str, table: dict[str, float]) -> dict:
    """Classify every $ amount. Returns the chosen advertised price and what it is a price OF."""
    unit_alt = _unit_regex(table)
    range_spans = []
    ranges = []
    for m in _RANGE.finditer(text):
        lo, hi = float(m.group(1)), float(m.group(2))
        if lo < hi:
            ranges.append((m.start(), lo, hi))
            range_spans.append((m.start(), m.end()))

    cands = []
    for m in _PRICE.finditer(text):
        if any(a <= m.start() < b for a, b in range_spans):
            continue
        value = _num(m.group(1))
        pre = text[max(0, m.start() - 45):m.start()].lower()
        post = text[m.end():m.end() + 45].lower()
        if re.search(r"(?:over|above|orders?\s+of|minimum(?:\s+order)?(?:\s+of)?|save|under|off|credit)\s*$", pre):
            continue                                    # a threshold or discount, not a price
        if re.match(r"\s*(?:off\b|original\s+price|was\b)", post) or re.search(
                r"(?:original|was|list\s+price|msrp|compare\s+at|retail\s+value)\s*:?\s*$", pre):
            continue                                    # struck-through / list price
        if (re.match(r"\W{0,3}(?:flat\s+)?(?:rate\s+)?(?:shipping|delivery|freight)", post)
                or re.search(r"(?:shipping|delivery|freight)(?:\s*(?:fee|cost|charge))?\s*(?:of|:|-|is)?\s*$", pre)):
            continue                                    # a fee, handled by _delivery
        # sites write "$2.29. /lb per loaf" or "$2.29, per lb": allow punctuation between the price and the slash
        per = re.match(r"\s*[.,;:)]*\s*(?:/|per\b|a\b)\s*((?i:" + unit_alt + r"))(?![a-z])", post, re.IGNORECASE)
        if not per:
            per = re.search(r"price\s+per\s+((?i:" + unit_alt + r"))\s*$", pre, re.IGNORECASE)
        if per:
            factor = table.get(_token(per.group(1)))
            if factor:
                cands.append({"kind": "per_unit", "value": value, "factor": factor, "text": f"${value:g} per {per.group(1).strip()}",
                              "pos": m.start()})
                continue
        cands.append({"kind": "pack", "value": value, "text": m.group(0), "pos": m.start()})

    pack = next((c for c in cands if c["kind"] == "pack"), None)
    per_unit = next((c for c in cands if c["kind"] == "per_unit"), None)
    return {"pack": pack, "per_unit": per_unit, "range": ranges[0] if ranges else None}


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def parse_listing(text: str, unit: str, *, title: str = "", url: str = "") -> dict:
    """Parse one listing's text. `unit` is the inventory unit: 'kg', 'L' or 'each'.

    Returns a dict where every field the text did not state is None. See the module docstring.
    """
    table = _TABLES.get(unit, _TABLES["each"])
    body = f"{title}. {text}" if title else text
    caveats: list[str] = []

    moq, moq_span = _moq(body, table)
    skip = [moq_span] if moq_span else []
    slug = re.sub(r"[-_/]+", " ", urlparse(url).path) if url else ""

    # ---- pack size: from title + text (and the URL slug, which can only add conflict, not information) ----
    multipacks = _distinct(_multipacks(body, table))
    sizes = _distinct(_sizes(body, table, skip))
    slug_sizes = _distinct(_sizes(slug, table, []))
    case_counts = _case_counts(body)

    pack_units = pack_desc = None
    if len(multipacks) == 1:
        pack_units, pack_desc = multipacks[0]
    elif len(multipacks) > 1:
        caveats.append("Several different multi-pack sizes are mentioned, so the pack size is unknown.")
    else:
        everything = _distinct(sizes + slug_sizes)
        if len(everything) > 1:
            caveats.append("Conflicting pack sizes are mentioned (" + ", ".join(r for _, r in everything[:3])
                           + "), so the pack size is unknown.")
        elif len(sizes) == 1 or (not sizes and len(slug_sizes) == 1):
            pack_units, pack_desc = (sizes or slug_sizes)[0]
            if unit != "each" and len(case_counts) == 1:
                pack_units *= case_counts[0]
                pack_desc = f"{case_counts[0]} x {pack_desc}"
        elif unit == "each" and len(case_counts) == 1:
            pack_units, pack_desc = float(case_counts[0]), f"{case_counts[0]} count"

    # ---- price ----
    p = _prices(body, table)
    advertised = price_text = selling_unit = None
    pack_price = normalised = None
    basis = None
    is_range = False
    if p["pack"] is not None:
        advertised, price_text, basis = p["pack"]["value"], p["pack"]["text"], "pack"
    elif p["per_unit"] is not None:
        advertised, price_text, basis = p["per_unit"]["value"], p["per_unit"]["text"], "per_unit"
    elif p["range"] is not None:
        _, lo, hi = p["range"]
        advertised, price_text, basis, is_range = hi, f"${lo:g}-${hi:g}", "range", True
        caveats.append(f"The listing gives a price range (${lo:g}-${hi:g}); the upper bound is used.")

    if basis == "pack":
        if pack_units:
            pack_price, normalised, selling_unit = advertised, advertised / pack_units, "pack"
        else:
            caveats.append("The price is given but the pack size is not stated, so it cannot be converted to a unit price.")
    elif basis == "per_unit":
        factor = p["per_unit"]["factor"]
        pack_units, pack_price, normalised, selling_unit = factor, advertised, advertised / factor, "unit"
        pack_desc = pack_desc or "priced per unit"
    elif basis == "range":
        caveats.append("The unit the range applies to is not stated, so it cannot be converted to a unit price.")

    # cross-check a pack price against a stated per-unit price
    if basis == "pack" and normalised and p["per_unit"] is not None:
        alt = p["per_unit"]["value"] / p["per_unit"]["factor"]
        if abs(alt - normalised) / normalised > 0.20:
            caveats.append("The pack price and the per-unit price on the page disagree by more than 20%.")

    # ---- MOQ ----
    moq_units = moq_packs = None
    if moq:
        if moq["in_inventory_units"]:
            moq_units = moq["quantity"]
            if pack_units and basis != "range":
                moq_packs = max(1.0, math.ceil(moq_units / pack_units - 1e-9))
        else:
            moq_packs = moq["quantity"]
            if pack_units:
                moq_units = moq_packs * pack_units
    if moq and moq_packs is None:
        caveats.append("A minimum order is stated but could not be converted to packs.")

    delivery = _delivery(body)
    caveats += delivery["caveats"]

    domain = domain_of(url)
    is_retail = any(domain == d or domain.endswith("." + d) for d in RETAIL_DOMAINS) or bool(
        re.search(r"\byour price\b", body, re.IGNORECASE))
    if is_retail:
        caveats.append("Looks like a consumer retail listing, not wholesale or foodservice pricing.")

    known = {
        "price": advertised is not None, "pack_size": pack_units is not None, "moq": moq_packs is not None,
        "delivery_fee": delivery["delivery_fee"] is not None, "lead_time": delivery["lead_time_days"] is not None,
        "delivery_information": delivery["delivery_information"] is not None,
    }
    return {
        "advertised_price": advertised, "price_text": price_text, "price_basis": basis, "price_is_range": is_range,
        "pack_price": pack_price, "pack_size": pack_units, "pack_description": pack_desc,
        "selling_unit": selling_unit,
        "normalised_unit_price": round(normalised, 4) if normalised is not None else None,
        "moq_packs": moq_packs, "moq_inventory_units": moq_units, "moq_text": moq["text"] if moq else None,
        "delivery_fee": delivery["delivery_fee"], "lead_time_days": delivery["lead_time_days"],
        "delivery_information": delivery["delivery_information"],
        "is_retail": is_retail, "caveats": list(dict.fromkeys(caveats)),
        "known_fields": [f for f in FIELDS if known[f]], "unknown_fields": [f for f in FIELDS if not known[f]],
    }


def confidence(tavily_score: float | None, parsed: dict) -> float:
    """A rough 0-1 trust score for a search result. It is a heuristic, not a probability.

    Starts from Tavily's own relevance score, rewards what the text actually stated, and penalises
    retail listings and price ranges. A confidence score never makes a price a confirmed quote.
    """
    score = 0.5 * float(tavily_score or 0)
    if parsed["advertised_price"] is not None:
        score += 0.2
    if parsed["pack_size"] is not None:
        score += 0.15
    if parsed["normalised_unit_price"] is not None:
        score += 0.05
    if parsed["delivery_information"]:
        score += 0.05
    if parsed["is_retail"]:
        score -= 0.15
    if parsed["price_is_range"]:
        score -= 0.10
    return round(min(max(score, 0.05), 0.95), 2)
