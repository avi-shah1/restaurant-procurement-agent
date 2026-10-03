"""Choose between priced options, and assemble the structured recommendation.

`choose()` is the deterministic rule set. It is used by the mock agent, as the fallback when the live
agent fails, and as the reference the live agent's choice is checked against.
`build_recommendation()` takes the agent's choice plus its words and fills every number from Python,
so the agent cannot invent a supplier, a price or a total.

Viability rules (in this order):
  1. options whose lead time protects safety stock (lead <= days until the predicted breach)
  2. else options that at least avoid a stockout (lead <= days until the predicted stockout)
  3. else the fastest options we know of
Options with an unknown lead time are never viable (we cannot show they arrive in time).
Among viable options the lowest estimated landed cost wins (ties: higher reliability).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..money import CURRENCY_CODE, money
from .options import find_option


def viable_options(requirement: dict, options: list[dict]) -> tuple[list[dict], str]:
    budget = requirement["lead_time_budget_days"]
    protect, avoid = budget["protect_safety_stock"], budget["avoid_stockout"]
    known = [o for o in options if o["lead_time_days"] is not None]
    if protect is not None:
        tier = [o for o in known if o["lead_time_days"] <= protect]
        if tier:
            return tier, f"protects safety stock (delivers within {protect} days)"
    if avoid is not None:
        tier = [o for o in known if o["lead_time_days"] <= avoid]
        if tier:
            return tier, (f"avoids a stockout (delivers within {avoid} day{'s' if avoid != 1 else ''}); "
                          f"safety stock cannot be fully protected")
    if known:
        fastest = min(o["lead_time_days"] for o in known)
        return ([o for o in known if o["lead_time_days"] == fastest],
                f"no known supplier can avoid the stockout; these are the fastest ({fastest} days)")
    return [], "no option has a known lead time"


def _rank_key(o: dict):
    return (o["estimated_total_landed_cost"], -(o["supplier_reliability"] or 0), o["option_id"])


def choose(requirement: dict, options: list[dict]) -> dict:
    """Deterministic selection. Returns chosen, backup, the viable set and why others were passed over."""
    viable, tier_label = viable_options(requirement, options)
    if not viable:
        return {"chosen": None, "backup": None, "viable": [], "tier": tier_label, "rejected": []}
    chosen = min(viable, key=_rank_key)
    others = [o for o in viable if o is not chosen]
    confirmed_others = [o for o in others if o["price_confirmed"]]
    backup = min(confirmed_others or others, key=_rank_key) if others else None
    rejected = [(o, _rejection_reason(requirement, o, chosen, viable)) for o in options
                if o is not chosen and o is not backup]
    return {"chosen": chosen, "backup": backup, "viable": viable, "tier": tier_label, "rejected": rejected}


def _rejection_reason(requirement: dict, o: dict, chosen: dict, viable: list[dict]) -> str:
    if o["lead_time_days"] is None:
        return "delivery time is not stated, so it cannot be shown to arrive in time"
    if o not in viable:
        budget = requirement["lead_time_budget_days"]["avoid_stockout"]
        return (f"{o['lead_time_days']}-day lead time is too slow"
                + (f" (needs {budget} or fewer)" if budget is not None else ""))
    return f"costs {money(o['estimated_total_landed_cost'] - chosen['estimated_total_landed_cost'])} more"


# ---- deterministic words (used by the mock agent and the fallback) ----
def draft_reasoning(requirement: dict, choice: dict) -> tuple[list[str], list[str], str]:
    """Words for the deterministic agent: at most 4 short reasons (each under 240 characters)."""
    chosen, backup = choice["chosen"], choice["backup"]
    unit = requirement["item"]["unit"]
    inc = requirement["incumbent"]
    reasons = []

    stockout = requirement["predicted_stockout_date"]
    budget = requirement["lead_time_budget_days"]["avoid_stockout"]
    if stockout:
        slow = (f" The incumbent, {inc['supplier_name']}, needs {inc['lead_time_days']} days, so it cannot."
                if inc and not inc["avoids_stockout"] else "")
        reasons.append(f"Stock is predicted to run out on {stockout}, so delivery is needed within "
                       f"{budget} day{'s' if budget != 1 else ''}.{slow}")
    reasons.append(
        f"{chosen['supplier_name']} delivers in {chosen['lead_time_days']} day{'s' if chosen['lead_time_days'] != 1 else ''} "
        f"({chosen['arrives_by']}) at {money(chosen['estimated_total_landed_cost'])} landed, the lowest cost among "
        f"options that arrive in time.")
    if inc and not chosen["is_incumbent"]:
        diff = chosen["estimated_total_landed_cost"] - inc["estimated_total_landed_cost"]
        reasons.append(f"That is {money(abs(diff))} {'more' if diff > 0 else 'less'} than the incumbent "
                       f"({money(inc['estimated_total_landed_cost'])})" + ("; the premium buys speed." if diff > 0 else "."))
    if choice["rejected"]:
        by_cost = sorted(choice["rejected"], key=lambda pair: pair[0]["estimated_total_landed_cost"])[:2]
        reasons.append("Passed over: " + "; ".join(
            f"{o['supplier_name']} ({money(o['estimated_total_landed_cost'])}, {why})" for o, why in by_cost) + ".")
    reasons = [r if len(r) <= 240 else r[:237].rstrip() + "..." for r in reasons[:4]]

    risks = []
    if backup:
        risks.append(f"Fallback if this falls through: {backup['supplier_name']} "
                     f"({money(backup['estimated_total_landed_cost'])}, {backup['lead_time_days']}-day lead time).")

    if chosen["price_confirmed"]:
        action = (f"Draft an RFQ to {chosen['supplier_name']} for {requirement['required_quantity']:g} {unit} "
                  f"for approval. Nothing is sent until you approve it.")
    else:
        action = (f"Draft an RFQ to {chosen['supplier_name']} asking them to confirm price, stock and "
                  f"{chosen['lead_time_days']}-day delivery for {requirement['required_quantity']:g} {unit}"
                  + (f", keeping {backup['supplier_name']} as the confirmed fallback" if backup else "")
                  + ". Nothing is sent until you approve it.")
    return reasons, risks, action


def automatic_risks(requirement: dict, o: dict, inc: dict | None) -> list[str]:
    """Risks Python can see from the data, added whatever the agent wrote."""
    unit = requirement["item"]["unit"]
    risks = []
    if not o["price_confirmed"]:
        risks.append(f"The {money(o['price_per_pack'])} price is an unconfirmed web search result "
                     f"(confidence {o['confidence']}), not a quote. Confirm with the supplier before buying.")
    risks.append("Stock availability has not been confirmed with the supplier.")
    if not o["lead_time_known"]:
        risks.append("Delivery time is not stated, so arrival before the deadline cannot be confirmed.")
    if not o["delivery_fee_known"]:
        risks.append("The delivery fee is not stated; the landed cost assumes $0.00 delivery.")
    if o.get("moq_known") is False:
        risks.append("The minimum order quantity is not stated; the cost assumes one pack is enough.")
    risks.extend(o.get("caveats") or [])  # e.g. retail listing, price range, listing-only delivery estimate
    if o["price_is_stale"]:
        risks.append(f"The supplier price on file is {o['price_age_days']} days old and may have changed.")
    if o["meets_required_by"] is False:
        late = (date_diff(o["arrives_by"], requirement["required_by"]))
        risks.append(f"Arrives {late} day{'s' if late != 1 else ''} after the required-by date "
                     f"({requirement['required_by']}), so stock will be below safety level in between.")
    if o["avoids_stockout"] is False:
        risks.append(f"A stockout is predicted on {requirement['predicted_stockout_date']}, before this "
                     f"delivery arrives.")
    elif requirement["predicted_stockout_date"] and o["avoids_stockout"]:
        margin = date_diff(requirement["predicted_stockout_date"], o["arrives_by"])
        when = ("on the day stock is predicted to run out" if margin <= 0 else
                f"{margin} day{'s' if margin != 1 else ''} before the predicted stockout")
        risks.append(f"Thin margin: delivery arrives {when} ({requirement['predicted_stockout_date']}). "
                     f"Any delay means a stockout.")
    if o["over_order_quantity"] > 0:
        why = ("minimum order quantity" if o["forced_by_moq"] else "pack size")
        risks.append(f"Buys {o['over_order_quantity']:g} {unit} more than required because of {why}.")
    if o["bumped_for_minimum_order_value"]:
        risks.append("Quantity was raised to reach the supplier's minimum order value.")
    if o["supplier_reliability"] is not None and o["supplier_reliability"] < 0.85:
        risks.append(f"Supplier reliability score is only {o['supplier_reliability']:.2f}.")
    if inc and o["estimated_total_landed_cost"] > inc["estimated_total_landed_cost"]:
        risks.append(f"Costs {money(o['estimated_total_landed_cost'] - inc['estimated_total_landed_cost'])} more "
                     f"than staying with the incumbent.")
    return risks


def date_diff(later_iso: str, earlier_iso: str) -> int:
    from datetime import date
    return (date.fromisoformat(later_iso) - date.fromisoformat(earlier_iso)).days


def compared_options(requirement: dict, options: list[dict], chosen_id: str, backup_id: str | None) -> list[dict]:
    """Every option that was weighed, with its verdict. The UI table is built from this."""
    viable, tier = viable_options(requirement, options)
    chosen = find_option(options, chosen_id)
    out = []
    for o in sorted(options, key=lambda o: (o["estimated_total_landed_cost"], o["option_id"])):
        if o["option_id"] == chosen_id:
            status, why = "recommended", "Chosen."
        elif o["option_id"] == backup_id:
            status, why = "backup", "Named as the fallback."
        elif o in viable:
            status, why = "viable", _rejection_reason(requirement, o, chosen, viable)
        else:
            status, why = "not_viable", _rejection_reason(requirement, o, chosen, viable)
        out.append({
            "option_id": o["option_id"], "supplier_name": o["supplier_name"], "origin": o["origin"],
            "is_incumbent": o["is_incumbent"], "status": status, "why": why,
            "price_per_unit": o["price_per_unit"], "estimated_total_landed_cost": o["estimated_total_landed_cost"],
            "order_quantity": o["order_quantity"], "lead_time_days": o["lead_time_days"], "arrives_by": o["arrives_by"],
            "price_confirmed": o["price_confirmed"], "evidence": o["evidence"],
            "known_fields": o.get("known_fields", []), "unknown_fields": o.get("unknown_fields", []),
            "caveats": o.get("caveats", []), "source_url": o.get("source_url"),
            "source_domain": o.get("source_domain"), "confidence": o.get("confidence"),
            "delivery_information": o.get("delivery_information"),
        })
    return out


def market_risks(market_search: dict | None) -> list[str]:
    """What the live market search did, as risks the reader must see."""
    if not market_search:
        return []
    if market_search["status"] == "unavailable":
        return [f"Live market search was unavailable ({market_search.get('reason') or 'unknown reason'}). "
                f"This recommendation uses only the supplier data we already had; no live market evidence was checked."]
    out = []
    if market_search["status"] == "partial":
        out.append(f"Live market search was only partly successful ({market_search.get('reason')}); "
                   f"some market evidence may be missing.")
    if market_search.get("priced_options", 1) == 0:
        out.append("The live market search found no alternatives with a usable price, so the comparison "
                   "is against existing suppliers only.")
    if not market_search.get("is_live"):
        out.append("The market options are seeded demo data, not results from the live web.")
    return out


def _summary(o: dict) -> dict:
    return {k: o[k] for k in ("option_id", "supplier_name", "supplier_id", "origin", "price_per_unit",
                              "estimated_total_landed_cost", "lead_time_days", "arrives_by",
                              "price_confirmed", "evidence")}


def build_recommendation(requirement: dict, options: list[dict], chosen_id: str, reasons: list[str],
                         risks: list[str], next_action: str, backup_id: str | None = None,
                         agent_mode: str = "mock", notes: list[str] | None = None,
                         market_search: dict | None = None) -> dict:
    """Assemble the final recommendation. Every number comes from `options`, never from the agent."""
    chosen = find_option(options, chosen_id)
    if chosen is None:
        raise ValueError(f"Unknown option_id {chosen_id!r}: it was not returned by the supplier tools.")
    backup = find_option(options, backup_id) if backup_id else None
    if backup_id and backup is None:
        raise ValueError(f"Unknown backup option_id {backup_id!r}.")
    inc = requirement.get("incumbent")
    unit = requirement["item"]["unit"]

    all_risks = list(dict.fromkeys([*market_risks(market_search), *automatic_risks(requirement, chosen, inc),
                                    *risks]))  # dedupe, keep order
    if chosen["origin"] == "market_search":
        sources = [{"label": f"{chosen['supplier_name']} - {chosen['product_name']}",
                    "url": chosen["source_url"], "domain": chosen["source_domain"],
                    "confidence": chosen["confidence"], "confirmed": False,
                    "note": "Web search result. Not a confirmed quote."}]
    else:
        sources = [{"label": f"{chosen['supplier_name']} price on file (supplier_products)",
                    "url": None, "confirmed": True, "last_verified_at": chosen["price_last_verified"]}]
    if backup and backup["origin"] == "market_search":
        sources.append({"label": f"Fallback: {backup['supplier_name']}", "url": backup["source_url"],
                        "domain": backup["source_domain"], "confidence": backup["confidence"],
                        "confirmed": False, "note": "Web search result. Not a confirmed quote."})

    savings = None if inc is None else round(
        inc["estimated_total_landed_cost"] - chosen["estimated_total_landed_cost"], 2)
    return {
        "option_id": chosen["option_id"],
        "recommended_supplier": {"name": chosen["supplier_name"], "supplier_id": chosen["supplier_id"],
                                 "origin": chosen["origin"], "is_incumbent": chosen["is_incumbent"],
                                 "reliability": chosen["supplier_reliability"]},   # None = not known
        "moq": {"known": chosen["moq_known"], "packs": chosen["minimum_order_packs"],
                "selling_unit": chosen["selling_unit"], "pack_size": chosen["pack_size"]},
        "quantity": {"required": requirement["required_quantity"], "order_quantity": chosen["order_quantity"],
                     "unit": unit, "packs_to_order": chosen["packs_to_order"],
                     "pack": f"{chosen['selling_unit']} ({chosen['pack_size']:g} {unit})"},
        "unit_price": {"per_unit": chosen["price_per_unit"], "per_pack": chosen["price_per_pack"],
                       "selling_unit": chosen["selling_unit"], "currency": CURRENCY_CODE,
                       "price_confirmed": chosen["price_confirmed"], "evidence": chosen["evidence"]},
        "estimated_total_landed_cost": {"total": chosen["estimated_total_landed_cost"],
                                        "goods": chosen["goods_cost"], "delivery_fee": chosen["delivery_fee"],
                                        "delivery_fee_known": chosen["delivery_fee_known"],
                                        "currency": CURRENCY_CODE},
        "delivery_timing": {"lead_time_days": chosen["lead_time_days"], "arrives_by": chosen["arrives_by"],
                            "required_by": requirement["required_by"],
                            "predicted_stockout_date": requirement["predicted_stockout_date"],
                            "meets_required_by": chosen["meets_required_by"],
                            "avoids_stockout": chosen["avoids_stockout"]},
        "incumbent_cost": None if inc is None else {
            "supplier": inc["supplier_name"], "price_per_unit": inc["price_per_unit"],
            "estimated_total_landed_cost": inc["estimated_total_landed_cost"],
            "lead_time_days": inc["lead_time_days"], "meets_required_by": inc["meets_required_by"],
            "avoids_stockout": inc["avoids_stockout"], "currency": CURRENCY_CODE},
        "potential_savings": None if savings is None else {
            "amount": savings, "currency": CURRENCY_CODE,
            "percent": round(savings / inc["estimated_total_landed_cost"] * 100, 1)
            if inc["estimated_total_landed_cost"] else None,
            "note": ("Saves money versus the incumbent." if savings > 0 else
                     "Same cost as the incumbent." if savings == 0 else
                     "Negative: costs more than the incumbent. The premium is the price of delivering in time.")},
        "reasons": reasons,
        "risks_and_uncertainties": all_risks,
        "sources": sources,
        "proposed_next_action": next_action,
        "backup_option": _summary(backup) if backup else None,
        "evidence": {"known_fields": chosen.get("known_fields", []), "unknown_fields": chosen.get("unknown_fields", []),
                     "caveats": chosen.get("caveats", []), "confidence": chosen.get("confidence"),
                     "price_confirmed": chosen["price_confirmed"]},
        "compared_options": compared_options(requirement, options, chosen_id, backup_id),
        "meta": {"agent_mode": agent_mode, "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
                 "numbers_calculated_by": "python", "notes": notes or [],
                 "market_search": None if not market_search else {
                     k: market_search.get(k) for k in ("provider", "status", "is_live", "reason", "sources_searched",
                                                       "priced_options", "unpriced_leads")}},
    }
