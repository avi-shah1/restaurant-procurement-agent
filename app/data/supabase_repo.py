"""Supabase/Postgres backend. Same interface as LocalRepository."""
from __future__ import annotations

from .demo_data import TABLES
from .repository import Repository

_RUN_FIELDS = ["inventory_item_id", "required_quantity", "required_by", "status",
               "predicted_stockout_date", "current_supplier_cost", "recommended_supplier_id",
               "recommended_cost", "potential_saving", "agent_rationale", "requirement",
               "recommendation", "agent_mode", "agent_session_id", "error", "market_search",
               "decided_at", "decided_by", "decision_note"]
_PRODUCT_FIELDS = ["unit_price", "unit", "pack_size", "minimum_order_quantity", "delivery_fee", "last_verified_at"]
_SUPPLIER_FIELDS = ["website", "contact_email", "reliability_score", "average_lead_time_days",
                    "minimum_order_value", "notes"]
_RESULT_FIELDS = ["supplier_name", "product_name", "raw_price", "normalised_unit_price",
                  "pack_size", "minimum_order_quantity", "delivery_information", "source_url",
                  "source_domain", "confidence", "raw_result"]


class SupabaseRepository(Repository):
    backend_name = "supabase"

    def __init__(self, url: str | None = None, key: str | None = None, client=None):
        if client is None:
            from supabase import create_client
            client = create_client(url, key)
        self._c = client

    def _select(self, table, filters=None, order=None, desc=False):
        q = self._c.table(table).select("*")
        for col, val in (filters or {}).items():
            q = q.eq(col, val)
        if order:
            q = q.order(order, desc=desc)
        return q.execute().data or []

    def _one(self, table, row_id):
        rows = self._select(table, {"id": row_id})
        return rows[0] if rows else None

    # ---- reads ----
    def list_inventory_items(self):
        return self._select("inventory_items", order="id")

    def get_inventory_item(self, item_id):
        return self._one("inventory_items", item_id)

    def get_usage_history(self, item_id):
        return self._select("usage_history", {"inventory_item_id": item_id}, order="usage_date")

    def list_suppliers(self):
        return self._select("suppliers", order="id")

    def get_supplier(self, supplier_id):
        return self._one("suppliers", supplier_id)

    def list_supplier_products(self, inventory_item_id=None, supplier_id=None):
        filters = {}
        if inventory_item_id is not None:
            filters["inventory_item_id"] = inventory_item_id
        if supplier_id is not None:
            filters["supplier_id"] = supplier_id
        return self._select("supplier_products", filters, order="id")

    def list_procurement_runs(self):
        return self._select("procurement_runs", order="id", desc=True)

    def get_procurement_run(self, run_id):
        return self._one("procurement_runs", run_id)

    def list_market_search_results(self, procurement_run_id):
        return self._select("market_search_results",
                            {"procurement_run_id": procurement_run_id}, order="id")

    def list_agent_events(self, procurement_run_id, after_id=0):
        q = (self._c.table("agent_events").select("*")
             .eq("procurement_run_id", procurement_run_id).order("id"))
        return [r for r in (q.execute().data or []) if r["id"] > after_id]

    def counts(self):
        out = {}
        for t in TABLES:
            res = self._c.table(t).select("id", count="exact").limit(1).execute()
            out[t] = res.count or 0
        return out

    # ---- writes ----
    def create_procurement_run(self, data):
        row = {f: data[f] for f in _RUN_FIELDS if data.get(f) is not None}
        return self._c.table("procurement_runs").insert(row).execute().data[0]

    def update_procurement_run(self, run_id, fields):
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown procurement_runs columns: {sorted(unknown)}")
        res = self._c.table("procurement_runs").update(fields).eq("id", run_id).execute()
        if not res.data:
            raise KeyError(f"No procurement run {run_id}")
        return res.data[0]

    def transition_procurement_run(self, run_id, from_status, fields):
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"Unknown procurement_runs columns: {sorted(unknown)}")
        # one UPDATE ... WHERE id = ? AND status = ?  : the database makes the check-and-change atomic
        res = (self._c.table("procurement_runs").update(fields).eq("id", run_id).eq("status", from_status)
               .execute())
        return res.data[0] if res.data else None

    def get_rfq_draft(self, procurement_run_id):
        rows = self._select("rfq_drafts", {"procurement_run_id": procurement_run_id})
        return rows[0] if rows else None

    def create_rfq_draft(self, data):
        existing = self.get_rfq_draft(data["procurement_run_id"])
        if existing:
            return existing
        fields = ["procurement_run_id", "supplier_name", "supplier_id", "to_email", "subject", "body", "benchmark",
                  "send_note"]
        return self._c.table("rfq_drafts").insert({f: data.get(f) for f in fields}).execute().data[0]

    def transition_rfq_draft(self, procurement_run_id, from_status, fields):
        allowed = {"status", "sent_at", "send_note", "send_record", "supplier_reply", "to_email", "subject", "body"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"Unknown rfq_drafts columns: {sorted(unknown)}")
        res = (self._c.table("rfq_drafts").update(fields)
               .eq("procurement_run_id", procurement_run_id).eq("status", from_status).execute())
        return res.data[0] if res.data else None

    def add_agent_event(self, procurement_run_id, title, detail=None, tone="info", payload=None):
        row = {"procurement_run_id": procurement_run_id, "tone": tone, "title": title,
               "detail": detail, "payload": payload}
        return self._c.table("agent_events").insert(row).execute().data[0]

    def upsert_supplier(self, name, fields):
        found = self._select("suppliers", {"name": name})
        if found:
            return found[0], False
        row = {"name": name, **{f: None for f in _SUPPLIER_FIELDS},
               **{k: v for k, v in fields.items() if k in _SUPPLIER_FIELDS}}
        return self._c.table("suppliers").insert(row).execute().data[0], True

    def upsert_supplier_product(self, supplier_id, inventory_item_id, product_name, fields):
        values = {k: v for k, v in fields.items() if k in _PRODUCT_FIELDS}
        found = self._select("supplier_products", {"supplier_id": supplier_id,
                                                   "inventory_item_id": inventory_item_id,
                                                   "supplier_product_name": product_name})
        if found:
            if found[0]["source_type"] != "tavily_live":
                return found[0], "skipped"  # our own records are never overwritten by web data
            res = self._c.table("supplier_products").update(values).eq("id", found[0]["id"]).execute()
            return res.data[0], "updated"
        row = {"supplier_id": supplier_id, "inventory_item_id": inventory_item_id,
               "supplier_product_name": product_name, **values, "source_type": "tavily_live"}
        return self._c.table("supplier_products").insert(row).execute().data[0], "created"

    def add_market_search_results(self, procurement_run_id, rows):
        payload = [{"procurement_run_id": procurement_run_id,
                    **{f: r.get(f) for f in _RESULT_FIELDS}} for r in rows]
        if not payload:
            return []
        return self._c.table("market_search_results").insert(payload).execute().data
