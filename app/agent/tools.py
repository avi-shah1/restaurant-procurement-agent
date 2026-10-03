"""The custom tools the Procurement Manager agent can call, and the Python that runs them.

ZooWork "application-executed" tools: the agent's run pauses on a call, our backend runs the function
below and returns the result. The agent's input is untrusted, so every tool validates it and only ever
touches the procurement run it was started for.

None of these tools can send anything. The only side effect is saving a recommendation (as a draft,
awaiting human approval) and recording market search results.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from ..data.repository import Repository
from ..money import CURRENCY_CODE, money
from ..procurement.discovery import persist_discoveries
from ..procurement.market import MarketSearchProvider
from ..procurement.options import (build_database_options, build_market_options, find_option,
                                   unpriced_lead)
from ..procurement.recommender import build_recommendation, choose

log = logging.getLogger(__name__)

MAX_ITEMS, MAX_TEXT = 10, 700
MAX_REASONS, MAX_REASON = 4, 240  # the approval card has room for 2-4 short reasons

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "get_procurement_requirement",
        "description": (
            "Read the procurement requirement for a run: what the deterministic Python forecast calculated "
            "(item, current stock, safety stock, required quantity, required-by date, predicted breach and "
            "stockout dates, days of cover, how fast a supplier must deliver, and the incumbent supplier's "
            "order cost). Call this FIRST. These numbers are authoritative; do not recalculate them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"procurement_run_id": {"type": "integer",
                                                  "description": "The procurement run id from the task message."}},
            "required": ["procurement_run_id"], "additionalProperties": False,
        },
        "timeoutMs": 120_000,
    },
    {
        "name": "get_existing_supplier_options",
        "description": (
            "List the suppliers we already know (incumbent and others in our database) that sell the item, "
            "each already priced for the required quantity: packs to order, goods cost, delivery fee, "
            "estimated_total_landed_cost, lead time, arrival date, and flags meets_required_by / "
            "avoids_stockout. Prices come from our own records (see price_age_days). Each option has an "
            "option_id you will need for submit_recommendation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"inventory_item_id": {"type": "integer",
                                                 "description": "The item id from the requirement."}},
            "required": ["inventory_item_id"], "additionalProperties": False,
        },
        "timeoutMs": 120_000,
    },
    {
        "name": "search_market_prices",
        "description": (
            "Search the live web (Tavily) for alternative suppliers and public prices for the item. Results are "
            "WEB SEARCH RESULTS, market intelligence and never confirmed quotes: price_confirmed is always false. "
            "Anything a page did not state (minimum order, delivery fee, lead time, pack size) is null and listed "
            "in unknown_fields; never assume it. Returns priced options in the same format as "
            "get_existing_supplier_options (with source_url, confidence, caveats) plus unpriced_leads, pages that "
            "could not be priced. The quantity and required-by date always come from the procurement requirement; "
            "you cannot change them. If live_search.status is 'unavailable' the search failed: continue with the "
            "existing supplier options only and say so in risks_and_uncertainties. Safe to call once per run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inventory_item_id": {"type": "integer", "description": "The item id from the requirement."},
                "location": {"type": "string", "maxLength": 100,
                             "description": "Optional delivery location to focus the search on, e.g. 'Austin, Texas'."},
                "quantity": {"type": "number",
                             "description": "Optional. Must equal the required quantity from the requirement."},
                "required_by": {"type": "string",
                                "description": "Optional. Must equal the required-by date from the requirement."},
            },
            "required": ["inventory_item_id"], "additionalProperties": False,
        },
        "timeoutMs": 180_000,
    },
    {
        "name": "submit_recommendation",
        "description": (
            "Submit your final recommendation. Choose ONE option_id from the options the tools returned. "
            "You supply the choice and the words; Python fills in the quantity, prices, total landed cost, "
            "timing and savings from that option, so do not include numbers of your own beyond what is in the "
            "options. This saves a DRAFT for human approval. It does not contact any supplier or place an "
            "order. Call it exactly once when you have compared the options."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recommended_option_id": {"type": "string", "description": "option_id of your chosen option."},
                "backup_option_id": {"type": "string", "description": "Optional option_id of a fallback."},
                "reasons": {"type": "array", "items": {"type": "string", "maxLength": MAX_REASON}, "minItems": 1,
                            "maxItems": MAX_REASONS,
                            "description": f"2-4 SHORT reasons (each under {MAX_REASON} characters): cost, timing, "
                                           "reliability, versus the incumbent. They are shown on an approval card."},
                "risks_and_uncertainties": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS,
                                            "description": "What could go wrong or is not confirmed."},
                "proposed_next_action": {"type": "string",
                                         "description": "e.g. draft an RFQ to X to confirm price. Human approval is required."},
            },
            "required": ["recommended_option_id", "reasons", "risks_and_uncertainties", "proposed_next_action"],
            "additionalProperties": False,
        },
        "timeoutMs": 120_000,
    },
]


class ToolInputError(ValueError):
    """The agent sent something we refuse. The message goes back to the agent so it can correct itself."""


@dataclass
class ToolContext:
    repo: Repository               # the repository with the run's scenario applied
    run_id: int
    market: MarketSearchProvider
    agent_mode: str | None = None  # live | mock | fallback; stamped on the saved recommendation
    session_id: str | None = None
    submitted: dict | None = field(default=None)

    def run(self) -> dict:
        run = self.repo.get_procurement_run(self.run_id)
        if run is None:
            raise ToolInputError(f"Procurement run {self.run_id} does not exist.")
        return run

    def requirement(self) -> dict:
        req = self.run().get("requirement")
        if not req:
            raise ToolInputError("This procurement run has no requirement snapshot.")
        return req

    def log(self, title: str, detail: str | None = None, tone: str = "info", payload: dict | None = None) -> None:
        try:
            self.repo.add_agent_event(self.run_id, title, detail, tone, payload)
        except Exception:  # the timeline must never break a run
            log.exception("Could not record timeline event %r", title)


# ---- helpers ----
def _int(args: dict, key: str) -> int:
    value = args.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolInputError(f"'{key}' must be an integer.")
    return value


def _text_list(args: dict, key: str, required: bool) -> list[str]:
    value = args.get(key)
    if value is None and not required:
        return []
    if not isinstance(value, list) or (required and not value) or len(value) > MAX_ITEMS:
        raise ToolInputError(f"'{key}' must be a list of 1-{MAX_ITEMS} strings." if required
                             else f"'{key}' must be a list of at most {MAX_ITEMS} strings.")
    out = []
    for entry in value:
        if not isinstance(entry, str) or not entry.strip():
            raise ToolInputError(f"Every entry in '{key}' must be a non-empty string.")
        out.append(entry.strip()[:MAX_TEXT] if key != "reasons" else entry.strip())
    return out


def _check_item(ctx: ToolContext, args: dict) -> dict:
    req = ctx.requirement()
    item_id = _int(args, "inventory_item_id")
    if item_id != req["item"]["id"]:
        raise ToolInputError(f"This run is for inventory item {req['item']['id']} only; "
                             f"you asked for {item_id}.")
    return req


def _market_rows(ctx: ToolContext, req: dict) -> list[dict]:
    return ctx.repo.list_market_search_results(ctx.run_id)


def all_options(ctx: ToolContext, req: dict) -> list[dict]:
    return [*build_database_options(ctx.repo, req), *build_market_options(req, _market_rows(ctx, req))]


# ---- the tools ----
def get_procurement_requirement(ctx: ToolContext, args: dict) -> dict:
    run_id = _int(args, "procurement_run_id")
    if run_id != ctx.run_id:
        raise ToolInputError(f"You are only authorised to read procurement run {ctx.run_id}.")
    run = ctx.run()
    return {"procurement_run_id": run_id, "run_status": run["status"], "currency": CURRENCY_CODE,
            "requirement": ctx.requirement()}


def get_existing_supplier_options(ctx: ToolContext, args: dict) -> dict:
    req = _check_item(ctx, args)
    options = build_database_options(ctx.repo, req)
    return {
        "inventory_item_id": req["item"]["id"], "item": req["item"]["name"],
        "required_quantity": req["required_quantity"], "unit": req["item"]["unit"],
        "required_by": req["required_by"], "currency": CURRENCY_CODE,
        "note": "Prices are from our own supplier records; check price_age_days. Sorted cheapest landed cost first.",
        "options": options,
    }


def search_market_prices(ctx: ToolContext, args: dict) -> dict:
    req = _check_item(ctx, args)
    location = args.get("location")
    if location is not None and (not isinstance(location, str) or len(location) > 100):
        raise ToolInputError("'location' must be a string of at most 100 characters.")
    notes = []
    if args.get("quantity") is not None and args["quantity"] != req["required_quantity"]:
        notes.append(f"quantity {args['quantity']!r} was ignored: the required quantity is "
                     f"{req['required_quantity']:g} {req['item']['unit']} (calculated by Python).")
    if args.get("required_by") is not None and args["required_by"] != req["required_by"]:
        notes.append(f"required_by {args['required_by']!r} was ignored: the required-by date is {req['required_by']}.")

    run = ctx.run()
    summary = run.get("market_search")
    rows = _market_rows(ctx, req)
    if summary is None:  # first search for this run: search, save, remember. Later calls reuse it (no repeat spend).
        item = ctx.repo.get_inventory_item(req["item"]["id"])
        result = ctx.market.search(item, req, location)
        rows = ctx.repo.add_market_search_results(ctx.run_id, result.rows) if result.rows else []
        summary = result.summary()
        if result.status == "unavailable":
            ctx.log("Live market search unavailable", result.reason, "error")
        elif result.is_live:
            ctx.log("Tavily search complete",
                    f"{len(result.queries)} queries, {result.sources_searched} sources, "
                    f"{len(result.extracted_urls)} pages extracted"
                    + (f". Problems: {result.reason}" if result.errors else ""), "info")
        if result.is_live and rows:
            discovered = persist_discoveries(ctx.repo, req["item"]["id"], rows)
            summary["discovery"] = discovered
            if discovered["suppliers_created"] or discovered["offers_created"] or discovered["offers_updated"]:
                ctx.log("Saved discovered suppliers",
                        f"{discovered['suppliers_created']} new suppliers, {discovered['offers_created']} new and "
                        f"{discovered['offers_updated']} refreshed offers (source_type tavily_live)", "info")
        options = build_market_options(req, rows)
        summary["priced_options"] = len(options)
        summary["unpriced_leads"] = len(rows) - len(options)
        ctx.repo.update_procurement_run(ctx.run_id, {"market_search": summary})
    options = build_market_options(req, rows)
    priced_ids = {int(o["option_id"].split("-")[1]) for o in options}
    leads = [unpriced_lead(r) for r in rows if r["id"] not in priced_ids]

    if summary["status"] == "unavailable":
        note = (f"LIVE MARKET SEARCH WAS UNAVAILABLE ({summary['reason']}). There are no market options. Continue "
                f"with the existing supplier options only, and state in risks_and_uncertainties that no live market "
                f"evidence was available. Do not invent market options.")
    elif not options:
        note = "The live search found no results with a usable price. Compare existing suppliers only and say so."
    else:
        note = ("Fields that are null were not stated by the source: do not assume them. unknown_fields lists them "
                "per option. These are public prices, not quotes.")
    return {
        "inventory_item_id": req["item"]["id"], "provider": summary["provider"], "currency": CURRENCY_CODE,
        "live_search": {k: summary.get(k) for k in ("status", "reason", "is_live", "queries", "sources_searched",
                                                    "results_kept", "extracted_urls", "errors", "priced_options",
                                                    "unpriced_leads")},
        "results_are": "web search results. They are NOT confirmed quotes. price_confirmed is false.",
        "note": note, "notes": notes, "options": options, "unpriced_leads": leads,
    }


_SUBMIT_KEYS = {"recommended_option_id", "backup_option_id", "reasons", "risks_and_uncertainties",
                "proposed_next_action"}


def submit_recommendation(ctx: ToolContext, args: dict) -> dict:
    extra = set(args) - _SUBMIT_KEYS
    if extra:  # numbers come from Python's options, never from the agent
        raise ToolInputError(f"Unexpected field(s) {sorted(extra)}. Send only {sorted(_SUBMIT_KEYS)}.")
    req = ctx.requirement()
    chosen_id = args.get("recommended_option_id")
    if not isinstance(chosen_id, str) or not chosen_id:
        raise ToolInputError("'recommended_option_id' must be a string.")
    backup_id = args.get("backup_option_id")
    if backup_id is not None and not isinstance(backup_id, str):
        raise ToolInputError("'backup_option_id' must be a string.")
    reasons = _text_list(args, "reasons", required=True)
    if len(reasons) > MAX_REASONS or any(len(r) > MAX_REASON for r in reasons):
        raise ToolInputError(f"Give at most {MAX_REASONS} reasons, each under {MAX_REASON} characters. They appear on "
                             f"a card a person reads before approving. Shorten and resubmit.")
    risks = _text_list(args, "risks_and_uncertainties", required=False)
    action = args.get("proposed_next_action")
    if not isinstance(action, str) or not action.strip():
        raise ToolInputError("'proposed_next_action' must be a non-empty string.")

    options = all_options(ctx, req)
    if find_option(options, chosen_id) is None:
        raise ToolInputError(f"Unknown option_id {chosen_id!r}. Use an option_id returned by "
                             f"get_existing_supplier_options or search_market_prices. Valid ids: "
                             f"{[o['option_id'] for o in options]}")
    if backup_id and find_option(options, backup_id) is None:
        raise ToolInputError(f"Unknown backup_option_id {backup_id!r}.")

    notes = []
    rule = choose(req, options)
    if rule["chosen"] and rule["chosen"]["option_id"] != chosen_id:
        r = rule["chosen"]
        notes.append(f"The rule-based check would have picked {r['supplier_name']} ({r['option_id']}, "
                     f"{money(r['estimated_total_landed_cost'])}); the agent chose {chosen_id}.")
    recommendation = build_recommendation(req, options, chosen_id, reasons, risks, action.strip()[:MAX_TEXT],
                                          backup_id, agent_mode=ctx.agent_mode or "mock", notes=notes,
                                          market_search=ctx.run().get("market_search"))
    ctx.repo.update_procurement_run(ctx.run_id, {
        "recommendation": recommendation,
        "recommended_supplier_id": recommendation["recommended_supplier"]["supplier_id"],
        "recommended_cost": recommendation["estimated_total_landed_cost"]["total"],
        "potential_saving": (recommendation["potential_savings"] or {}).get("amount"),
        "agent_rationale": " ".join(recommendation["reasons"]),
    })
    ctx.submitted = recommendation
    return {"accepted": True, "saved_as": "draft awaiting human approval",
            "recommended_supplier": recommendation["recommended_supplier"]["name"],
            "estimated_total_landed_cost": recommendation["estimated_total_landed_cost"]["total"],
            "notes": notes}


TOOLS: dict[str, Callable[[ToolContext, dict], dict]] = {
    "get_procurement_requirement": get_procurement_requirement,
    "get_existing_supplier_options": get_existing_supplier_options,
    "search_market_prices": search_market_prices,
    "submit_recommendation": submit_recommendation,
}


def _summarise(name: str, result: dict) -> str:
    if name == "get_procurement_requirement":
        r = result["requirement"]
        return (f"{r['item']['name']}: order {r['required_quantity']:g} {r['item']['unit']} by {r['required_by']}; "
                f"stockout predicted {r['predicted_stockout_date'] or 'none'}")
    if name == "get_existing_supplier_options":
        return f"{len(result['options'])} supplier options on file, priced for {result['required_quantity']:g} {result['unit']}"
    if name == "search_market_prices":
        live = result["live_search"]
        if live["status"] == "unavailable":
            return f"Live market search UNAVAILABLE ({live['reason']}). Continuing with existing suppliers only"
        kind = "live web" if live["is_live"] else "demo"
        return (f"{len(result['options'])} priced options and {len(result['unpriced_leads'])} unpriced leads from "
                f"{live['sources_searched']} {kind} sources ({result['provider']}); all unconfirmed")
    return f"Recommended {result['recommended_supplier']} at {money(result['estimated_total_landed_cost'])}"


def execute_tool(ctx: ToolContext, name: str, args: Any) -> tuple[dict, bool]:
    """Run one tool call. Returns (result, is_error). Never raises: errors go back to the agent."""
    handler = TOOLS.get(name)
    if handler is None:
        ctx.log(f"Agent: {name}", "Unknown tool requested", "error")
        return {"error": f"Unknown tool {name!r}. Available: {sorted(TOOLS)}"}, True
    try:
        if not isinstance(args, dict):
            raise ToolInputError("Tool input must be a JSON object.")
        result = handler(ctx, args)
    except ToolInputError as e:
        ctx.log(f"Agent: {name}", f"Rejected: {e}", "error")
        return {"error": str(e)}, True
    except Exception:
        log.exception("Tool %s failed", name)
        ctx.log(f"Agent: {name}", "Internal error while running the tool", "error")
        return {"error": "Internal error while running this tool. Try again or continue without it."}, True
    ctx.log(f"Agent: {name}", _summarise(name, result), "agent")
    return result, False
