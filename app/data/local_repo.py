"""DEMO_MODE backend: the same seeded data, kept in a local JSON file. No Supabase needed."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path

from .demo_data import TABLES, build_demo_data, to_relational
from .repository import Repository



def _default_path() -> Path:
    """Where the demo database lives.

    Locally: data/demo_db.json in the project. On Vercel the project folder is read-only, so use the temp
    folder instead. That is per-instance and disappears on a cold start: fine for a demo, not for real data
    (use Supabase for anything that must persist). DEMO_DB_PATH overrides both.
    """
    if os.environ.get("DEMO_DB_PATH"):
        return Path(os.environ["DEMO_DB_PATH"])
    if os.environ.get("VERCEL"):
        return Path(tempfile.gettempdir()) / "procurement-demo" / "demo_db.json"
    return Path(__file__).resolve().parents[2] / "data" / "demo_db.json"


DEFAULT_PATH = _default_path()
_SEED_TABLES = ["suppliers", "inventory_items", "usage_history", "supplier_products"]
_RUN_FIELDS = ["inventory_item_id", "required_quantity", "required_by", "status",
               "predicted_stockout_date", "current_supplier_cost", "recommended_supplier_id",
               "recommended_cost", "potential_saving", "agent_rationale", "requirement",
               "recommendation", "agent_mode", "agent_session_id", "error", "market_search",
               "decided_at", "decided_by", "decision_note"]
_PRODUCT_FIELDS = ["unit_price", "unit", "pack_size", "minimum_order_quantity", "delivery_fee", "last_verified_at"]
_SUPPLIER_FIELDS = ["website", "contact_email", "reliability_score", "average_lead_time_days",
                    "minimum_order_value", "notes"]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LocalRepository(Repository):
    backend_name = "local-json (DEMO_MODE)"

    def __init__(self, path: Path | str | None = None, today: date | None = None):
        self._path = Path(path or DEFAULT_PATH)  # looked up now, so tests can redirect it
        self._lock = threading.Lock()
        today = today or date.today()
        self._db = self._load()
        # The usage window is relative to today, so refresh the seed tables on a new day.
        # Procurement runs / market results are kept.
        if self._db.get("meta", {}).get("generated_on") != today.isoformat():
            self._reseed(today)

    # ---- storage ----
    def _load(self) -> dict:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # corrupt file: regenerate below
        return {"meta": {}, "tables": {t: [] for t in TABLES}}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._db, indent=1), encoding="utf-8")
        tmp.replace(self._path)

    def _reseed(self, today: date) -> None:
        fresh = to_relational(build_demo_data(today))
        tables = self._db.setdefault("tables", {})
        for t in _SEED_TABLES:
            tables[t] = fresh[t]
        for t in TABLES:
            tables.setdefault(t, [])
        self._db["meta"] = {"generated_on": today.isoformat()}
        self._save()

    def _rows(self, table: str) -> list[dict]:
        # setdefault: a demo_db.json written before a table existed still works
        return self._db["tables"].setdefault(table, [])

    @staticmethod
    def _one(rows: list[dict], row_id: int) -> dict | None:
        return copy.deepcopy(next((r for r in rows if r["id"] == row_id), None))

    # ---- reads ----
    def list_inventory_items(self):
        return copy.deepcopy(sorted(self._rows("inventory_items"), key=lambda r: r["id"]))

    def get_inventory_item(self, item_id):
        return self._one(self._rows("inventory_items"), item_id)

    def get_usage_history(self, item_id):
        rows = [r for r in self._rows("usage_history") if r["inventory_item_id"] == item_id]
        return copy.deepcopy(sorted(rows, key=lambda r: r["usage_date"]))

    def list_suppliers(self):
        return copy.deepcopy(sorted(self._rows("suppliers"), key=lambda r: r["id"]))

    def get_supplier(self, supplier_id):
        return self._one(self._rows("suppliers"), supplier_id)

    def list_supplier_products(self, inventory_item_id=None, supplier_id=None):
        rows = [r for r in self._rows("supplier_products")
                if (inventory_item_id is None or r["inventory_item_id"] == inventory_item_id)
                and (supplier_id is None or r["supplier_id"] == supplier_id)]
        return copy.deepcopy(sorted(rows, key=lambda r: r["id"]))

    def list_procurement_runs(self):
        return copy.deepcopy(sorted(self._rows("procurement_runs"), key=lambda r: -r["id"]))

    def get_procurement_run(self, run_id):
        return self._one(self._rows("procurement_runs"), run_id)

    def list_market_search_results(self, procurement_run_id):
        rows = [r for r in self._rows("market_search_results")
                if r["procurement_run_id"] == procurement_run_id]
        return copy.deepcopy(sorted(rows, key=lambda r: r["id"]))

    def list_agent_events(self, procurement_run_id, after_id=0):
        rows = [r for r in self._rows("agent_events")
                if r["procurement_run_id"] == procurement_run_id and r["id"] > after_id]
        return copy.deepcopy(sorted(rows, key=lambda r: r["id"]))

    def counts(self):
        return {t: len(self._rows(t)) for t in TABLES}

    # ---- writes ----
    def _next_id(self, table: str) -> int:
        return max((r["id"] for r in self._rows(table)), default=0) + 1

    def create_procurement_run(self, data):
        with self._lock:
            row = {"id": self._next_id("procurement_runs")}
            row.update({f: copy.deepcopy(data.get(f)) for f in _RUN_FIELDS})
            row["status"] = row["status"] or "pending"
            row["created_at"] = _now()
            self._rows("procurement_runs").append(row)
            self._save()
            return copy.deepcopy(row)

    def update_procurement_run(self, run_id, fields):
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown procurement_runs columns: {sorted(unknown)}")
        with self._lock:
            row = next((r for r in self._rows("procurement_runs") if r["id"] == run_id), None)
            if row is None:
                raise KeyError(f"No procurement run {run_id}")
            row.update(copy.deepcopy(fields))
            self._save()
            return copy.deepcopy(row)

    def transition_procurement_run(self, run_id, from_status, fields):
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown procurement_runs columns: {sorted(unknown)}")
        with self._lock:
            row = next((r for r in self._rows("procurement_runs") if r["id"] == run_id), None)
            if row is None or row["status"] != from_status:
                return None
            row.update(copy.deepcopy(fields))
            self._save()
            return copy.deepcopy(row)

    def get_rfq_draft(self, procurement_run_id):
        return copy.deepcopy(next((r for r in self._rows("rfq_drafts")
                                   if r["procurement_run_id"] == procurement_run_id), None))

    def create_rfq_draft(self, data):
        fields = ["procurement_run_id", "supplier_name", "supplier_id", "to_email", "subject", "body", "benchmark",
                  "send_note"]
        with self._lock:
            existing = next((r for r in self._rows("rfq_drafts")
                             if r["procurement_run_id"] == data["procurement_run_id"]), None)
            if existing is not None:
                return copy.deepcopy(existing)
            row = {"id": self._next_id("rfq_drafts"), **{f: copy.deepcopy(data.get(f)) for f in fields},
                   "status": "draft", "created_at": _now(), "sent_at": None,
                   "send_record": None, "supplier_reply": None}
            self._rows("rfq_drafts").append(row)
            self._save()
            return copy.deepcopy(row)

    def transition_rfq_draft(self, procurement_run_id, from_status, fields):
        allowed = {"status", "sent_at", "send_note", "send_record", "supplier_reply", "to_email", "subject", "body"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown rfq_drafts columns: {sorted(unknown)}")
        with self._lock:
            row = next((r for r in self._rows("rfq_drafts")
                        if r["procurement_run_id"] == procurement_run_id), None)
            if row is None or row["status"] != from_status:
                return None
            row.update(copy.deepcopy(fields))
            self._save()
            return copy.deepcopy(row)

    def add_agent_event(self, procurement_run_id, title, detail=None, tone="info", payload=None):
        with self._lock:
            row = {"id": self._next_id("agent_events"), "procurement_run_id": procurement_run_id,
                   "tone": tone, "title": title, "detail": detail,
                   "payload": copy.deepcopy(payload), "created_at": _now()}
            self._rows("agent_events").append(row)
            self._save()
            return copy.deepcopy(row)

    def upsert_supplier(self, name, fields):
        with self._lock:
            existing = next((r for r in self._rows("suppliers") if r["name"] == name), None)
            if existing is not None:
                return copy.deepcopy(existing), False
            row = {"id": self._next_id("suppliers"), "name": name, **{f: None for f in _SUPPLIER_FIELDS}}
            row.update({k: v for k, v in fields.items() if k in _SUPPLIER_FIELDS})
            self._rows("suppliers").append(row)
            self._save()
            return copy.deepcopy(row), True

    def upsert_supplier_product(self, supplier_id, inventory_item_id, product_name, fields):
        with self._lock:
            row = next((r for r in self._rows("supplier_products")
                        if (r["supplier_id"], r["inventory_item_id"], r["supplier_product_name"])
                        == (supplier_id, inventory_item_id, product_name)), None)
            values = {k: v for k, v in fields.items() if k in _PRODUCT_FIELDS}
            if row is not None:
                if row["source_type"] != "tavily_live":
                    return copy.deepcopy(row), "skipped"  # our own records are never overwritten by web data
                row.update(values)
                self._save()
                return copy.deepcopy(row), "updated"
            row = {"id": self._next_id("supplier_products"), "supplier_id": supplier_id,
                   "inventory_item_id": inventory_item_id, "supplier_product_name": product_name,
                   "minimum_order_quantity": None, "delivery_fee": None, "last_verified_at": None,
                   **values, "source_type": "tavily_live"}
            self._rows("supplier_products").append(row)
            self._save()
            return copy.deepcopy(row), "created"

    def add_market_search_results(self, procurement_run_id, rows):
        fields = ["supplier_name", "product_name", "raw_price", "normalised_unit_price",
                  "pack_size", "minimum_order_quantity", "delivery_information", "source_url",
                  "source_domain", "confidence", "raw_result"]
        with self._lock:
            out = []
            for r in rows:
                row = {"id": self._next_id("market_search_results"),
                       "procurement_run_id": procurement_run_id}
                row.update({f: r.get(f) for f in fields})
                row["created_at"] = _now()
                self._rows("market_search_results").append(row)
                out.append(copy.deepcopy(row))
            self._save()
            return out
