"""Run one complete procurement run from the command line and print the result.

    python scripts/run_procurement.py                      mozzarella, agent chosen by AGENT_MODE (default: auto)
    python scripts/run_procurement.py --sku OAT-001
    python scripts/run_procurement.py --mode mock          force the deterministic demo agent
    python scripts/run_procurement.py --mode live          force the live ZooWork agent (needs key + agent id)
    python scripts/run_procurement.py --json               print the saved recommendation as JSON

Nothing is sent to any supplier. The recommendation is saved as a draft awaiting approval.
"""
import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_dotenv  # noqa: E402,F401  (importing config loads .env)

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
ap.add_argument("--sku", default="MOZ-001")
ap.add_argument("--mode", choices=["auto", "live", "mock"], help="overrides AGENT_MODE")
ap.add_argument("--json", action="store_true")
args = ap.parse_args()
if args.mode:
    os.environ["AGENT_MODE"] = args.mode

from app.agent.runner import execute_run  # noqa: E402
from app.data import get_base_repo, get_repo  # noqa: E402
from app.demand_events import active_event_multiplier, enabled_keys  # noqa: E402
from app.money import money  # noqa: E402
from app.procurement.requirement import NoProcurementNeeded, create_run  # noqa: E402

base = get_base_repo()
item = next((i for i in base.list_inventory_items() if i["sku"] == args.sku), None)
if item is None:
    sys.exit(f"Unknown sku {args.sku!r}")
try:
    run = create_run(get_repo(), item["id"], date.today(), active_event_multiplier, enabled_keys())
except NoProcurementNeeded as e:
    sys.exit(str(e))

req = run["requirement"]
print(f"Run {run['id']}: {req['item']['name']} ({args.sku}) | as of {req['as_of']} | scenario {req['scenario']}")
print(f"Required: {req['required_quantity']:g} {req['item']['unit']} by {req['required_by']} | "
      f"stockout predicted {req['predicted_stockout_date']} | "
      f"incumbent {req['incumbent']['supplier_name']} {money(req['incumbent']['estimated_total_landed_cost'])}\n")

final = execute_run(base, run["id"])
print("TIMELINE")
for e in base.list_agent_events(run["id"]):
    print(f"  [{e['tone']:<7}] {e['title']}" + (f" - {e['detail']}" if e["detail"] else ""))

ms = final.get("market_search")
print("\nMARKET SEARCH")
if not ms:
    print("  (not run)")
else:
    kind = "LIVE WEB" if ms["is_live"] else "DEMO DATA"
    print(f"  Provider: {ms['provider']} ({kind})   Status: {ms['status'].upper()}"
          + (f"   Reason: {ms['reason']}" if ms.get("reason") else ""))
    print(f"  Queries run: {len(ms['queries'])}   Sources searched: {ms['sources_searched']}   "
          f"Pages extracted: {len(ms['extracted_urls'])}   Kept: {ms['results_kept']}   "
          f"Priced options: {ms.get('priced_options')}   Unpriced leads: {ms.get('unpriced_leads')}")
    for q in ms["queries"]:
        print(f"    query: {q}")
    if ms.get("discovery"):
        d = ms["discovery"]
        print(f"  Saved as tavily_live: {d['suppliers_created']} new suppliers, {d['offers_created']} new offers, "
              f"{d['offers_updated']} refreshed")
rows = base.list_market_search_results(run["id"])
if rows:
    print("  Results found (stored in market_search_results):")
    for r in rows:
        raw = r["raw_result"] or {}
        price = money(r["normalised_unit_price"]) + "/" + req["item"]["unit"] if r["normalised_unit_price"] is not None else "no unit price"
        print(f"    - {r['source_domain']}: {r['raw_price'] or 'no price stated'} -> {price}  conf {r['confidence']}  "
              f"unknown: {', '.join(raw.get('unknown_fields', [])) or 'none'}")
        print(f"        {r['source_url']}")

print(f"\nSTATUS: {final['status']}   AGENT MODE: {final['agent_mode']}")
rec = final.get("recommendation")
if final.get("error"):
    print(f"ERROR: {final['error']}")
if not rec:
    sys.exit(1)
if args.json:
    print(json.dumps(rec, indent=2))
    sys.exit(0)

t, q, u = rec["estimated_total_landed_cost"], rec["quantity"], rec["unit_price"]
d, inc, sav = rec["delivery_timing"], rec["incumbent_cost"], rec["potential_savings"]
unit = q["unit"]
print("\nRECOMMENDATION")
print(f"  Supplier:        {rec['recommended_supplier']['name']} "
      f"({'database supplier' if rec['recommended_supplier']['origin'] == 'database' else 'web search result'})")
print(f"  Quantity:        {q['order_quantity']:g} {unit} ({q['packs_to_order']} x {q['pack']}) for a requirement of {q['required']:g} {unit}")
print(f"  Unit price:      {money(u['per_unit'])} per {unit} ({money(u['per_pack'])} per {u['selling_unit']})"
      f"  [{'confirmed' if u['price_confirmed'] else 'UNCONFIRMED'}]")
print(f"  Landed cost:     {money(t['total'])} = {money(t['goods'])} goods + {money(t['delivery_fee'])} delivery")
print(f"  Delivery:        {d['lead_time_days']} day(s), arrives {d['arrives_by']} (stockout predicted {d['predicted_stockout_date']}; "
      f"avoids stockout: {d['avoids_stockout']}, meets required-by: {d['meets_required_by']})")
if inc:
    print(f"  Incumbent:       {inc['supplier']} {money(inc['estimated_total_landed_cost'])}, "
          f"{inc['lead_time_days']}-day lead time (avoids stockout: {inc['avoids_stockout']})")
if sav:
    print(f"  Potential saving:{money(sav['amount']):>10}   ({sav['note']})")
print("  Reasons:")
for r in rec["reasons"]:
    print(f"    - {r}")
print("  Risks / uncertainties:")
for r in rec["risks_and_uncertainties"]:
    print(f"    - {r}")
print("  Sources:")
for s in rec["sources"]:
    print(f"    - {s['label']}" + (f" <{s['url']}>" if s.get("url") else "") + (" [confirmed]" if s["confirmed"] else " [unconfirmed]"))
ev = rec["evidence"]
print(f"  Known fields:    {', '.join(ev['known_fields']) or 'none'}")
print(f"  Unknown fields:  {', '.join(ev['unknown_fields']) or 'none'}")
print("  Options compared:")
for o in rec["compared_options"]:
    where = "web" if o["origin"] == "market_search" else "on file"
    lead = f"{o['lead_time_days']}d" if o["lead_time_days"] is not None else "lead ?"
    tag = {"recommended": "<< RECOMMENDED", "backup": "(backup)"}.get(o["status"], f"[{o['status']}]")
    print(f"    {o['supplier_name']:<34} {money(o['estimated_total_landed_cost']):>9}  {lead:>7}  {where:<8} "
          f"{'incumbent ' if o['is_incumbent'] else ''}{tag}")
print(f"  Next action:     {rec['proposed_next_action']}")
