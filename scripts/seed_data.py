"""Seed the demo data.

    python scripts/seed_data.py --local            rebuild the local JSON demo database
    python scripts/seed_data.py                    seed Supabase (needs SUPABASE_URL + key in .env)
    python scripts/seed_data.py --reset            Supabase: DELETE all rows in the app tables first

All data is DEMO / SIMULATED. Seeding Supabase refuses to run on non-empty tables unless --reset.
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_dotenv  # noqa: E402,F401  (importing config loads .env)
from app.data.demo_data import TABLES, build_demo_data  # noqa: E402
from app.data.local_repo import DEFAULT_PATH  # noqa: E402

# children before parents, so foreign keys never block a delete
_DELETE_ORDER = ["rfq_drafts", "agent_events", "market_search_results", "procurement_runs", "usage_history",
                 "supplier_products", "inventory_items", "suppliers"]


def seed_local() -> None:
    if DEFAULT_PATH.exists():
        DEFAULT_PATH.unlink()
    from app.data.local_repo import LocalRepository
    repo = LocalRepository()
    print(f"Local demo database rebuilt at {DEFAULT_PATH}")
    print(repo.counts())


def seed_supabase(reset: bool) -> None:
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not (url and key):
        sys.exit("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set. Use --local for demo mode.")
    from supabase import create_client
    c = create_client(url, key)

    existing = {t: (c.table(t).select("id", count="exact").limit(1).execute().count or 0) for t in TABLES}
    if any(existing.values()):
        if not reset:
            sys.exit(f"Tables already contain data {existing}. Re-run with --reset to delete and reseed.")
        for t in _DELETE_ORDER:
            c.table(t).delete().neq("id", 0).execute()
        print("Existing rows deleted.")

    data = build_demo_data()
    # Let the database assign ids, then map natural keys (name / sku) to them.
    sup = c.table("suppliers").insert(data["suppliers"]).execute().data
    sup_id = {r["name"]: r["id"] for r in sup}

    items = [{**{k: v for k, v in it.items() if k != "current_supplier"},
              "current_supplier_id": sup_id[it["current_supplier"]]} for it in data["inventory_items"]]
    item_id = {r["sku"]: r["id"] for r in c.table("inventory_items").insert(items).execute().data}

    usage = [{"inventory_item_id": item_id[u["sku"]], "usage_date": u["usage_date"],
              "quantity_used": u["quantity_used"]} for u in data["usage_history"]]
    c.table("usage_history").insert(usage).execute()

    products = [{**{k: v for k, v in p.items() if k not in ("supplier", "sku")},
                 "supplier_id": sup_id[p["supplier"]], "inventory_item_id": item_id[p["sku"]]}
                for p in data["supplier_products"]]
    c.table("supplier_products").insert(products).execute()

    print("Supabase seeded:", {"suppliers": len(sup_id), "inventory_items": len(item_id),
                               "usage_history": len(usage), "supplier_products": len(products)})


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--local", action="store_true", help="rebuild the local JSON demo database")
    ap.add_argument("--reset", action="store_true", help="Supabase: delete existing rows first")
    args = ap.parse_args()
    seed_local() if args.local else seed_supabase(args.reset)
