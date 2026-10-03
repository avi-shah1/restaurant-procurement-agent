"""Build the RFQ (request for quote) DRAFT that is produced when a person approves a recommendation.

This is plain Python and a fixed template, not AI-written text, on purpose:
  - it is instant and works offline, so the approval step of a demo cannot fail on a network call;
  - it cannot invent claims. The wording is fixed, and the only numbers in it come from stored data.

Nothing here sends anything. Sending is a separate Phase 7 step (`send_rfq`) that only runs after a person
approves and then clicks Send.

The negotiation rule: a public price may be mentioned as "publicly listed prices", never as a quote,
and only when the evidence is sufficiently reliable (see `pick_benchmark`).
"""
from __future__ import annotations

import os
from datetime import date

BENCHMARK_MIN_CONFIDENCE = 0.70
SEND_NOTE = ("Not sent yet. Approving only created this draft. Use Send RFQ when you are ready "
             "(mock/test by default; optional SMTP). No purchase order is placed.")


def long_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%A} {d.day} {d:%B %Y}"


def pick_benchmark(rec: dict, market_rows: list[dict]) -> dict:
    """Decide whether public market evidence may be mentioned in the RFQ. Returns {used, reason, ...}.

    All of these must hold for a result to be used:
      - it is a live web result (not demo data), with a price per unit and confidence >= 0.70
      - it is not a retail listing, a price range, or a price flagged as implausible
      - it is from a different supplier than the one we are writing to
      - it is cheaper than the recommended supplier's price (otherwise it gives no negotiating leverage)
    """
    recommended = rec["recommended_supplier"]["name"]
    ours = rec["unit_price"]["per_unit"]
    candidates, rejected_for = [], []
    for r in market_rows:
        raw = r.get("raw_result") or {}
        if raw.get("provider") != "tavily":
            continue
        price = r.get("normalised_unit_price")
        why = None
        if price is None:
            why = "no usable unit price"
        elif (r.get("confidence") or 0) < BENCHMARK_MIN_CONFIDENCE:
            why = f"confidence {r.get('confidence')} is below {BENCHMARK_MIN_CONFIDENCE}"
        elif raw.get("is_retail"):
            why = "a consumer retail listing"
        elif raw.get("price_is_range"):
            why = "a price range"
        elif raw.get("price_suspect"):
            why = "flagged as implausible"
        elif r["supplier_name"] == recommended:
            why = "the same supplier"
        elif price >= ours:
            why = "not cheaper than the recommended price"
        if why:
            rejected_for.append(why)
        else:
            candidates.append(r)
    if not candidates:
        reason = ("No sufficiently reliable public price was found, so no market evidence is mentioned."
                  if market_rows else "No market search results, so no market evidence is mentioned.")
        return {"used": False, "reason": reason}
    best = min(candidates, key=lambda r: r["normalised_unit_price"])
    return {"used": True, "price_per_unit": round(best["normalised_unit_price"], 2), "confidence": best["confidence"],
            "source_url": best.get("source_url"), "source_domain": best.get("source_domain"),
            "reason": "A reliable, cheaper public listing exists. It is mentioned as a public price, never as a quote."}


def build_rfq(run: dict, supplier: dict | None, market_rows: list[dict]) -> dict:
    """Return the fields of an rfq_drafts row (without id / created_at)."""
    rec, req = run["recommendation"], run["requirement"]
    unit = req["item"]["unit"]
    product = req["item"]["name"]
    name = rec["recommended_supplier"]["name"]
    qty = rec["quantity"]["required"]
    # the hard limit is the day stock runs out; if none is predicted, the day safety stock is breached
    deadline_iso = req.get("predicted_stockout_date") or req["required_by"]

    benchmark = pick_benchmark(rec, market_rows)
    lines = [
        f"Hello {name} team,",
        "",
        f"We are looking to purchase about {qty:g} {unit} of {product} for delivery by {long_date(deadline_iso)} "
        f"(earlier if possible). We are reviewing current market options and would appreciate your best delivered "
        f"price, including minimum order quantity (MOQ), delivery cost and expected delivery timing.",
    ]
    if benchmark["used"]:
        lines += ["", f"For context, publicly listed prices for comparable {product} are currently around "
                      f"${benchmark['price_per_unit']:.2f} per {unit}. These are public listings only, not quotes we "
                      f"hold, so we would welcome your best price."]
    lines += [
        "",
        "Please reply with:",
        f"- your price per {unit} and the total delivered price",
        "- your minimum order quantity and pack size",
        "- the delivery fee",
        "- whether stock is available now, and the earliest delivery date",
        "",
        "This is a request for a quote only. It is not a purchase order.",
        "",
        "Thank you,",
        os.environ.get("RESTAURANT_NAME", "[Your restaurant name]"),
    ]
    return {
        "procurement_run_id": run["id"],
        "supplier_name": name,
        "supplier_id": rec["recommended_supplier"]["supplier_id"],
        "to_email": (supplier or {}).get("contact_email") or None,
        "subject": f"Request for quote: {qty:g} {unit} {product}, delivery by {long_date(deadline_iso)}",
        "body": "\n".join(lines),
        "benchmark": benchmark,
        "send_note": SEND_NOTE,
    }
