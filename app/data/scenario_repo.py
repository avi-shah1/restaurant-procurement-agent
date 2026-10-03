"""Applies a scenario on top of any Repository, at read time.

The wrapped repository (Supabase or local) is never modified, so the original seeded state is
always one `reset_scenario()` away. Same Repository interface, so nothing above it changes.
"""
from __future__ import annotations

from datetime import date
from typing import Callable

from .repository import Repository
from .scenarios import Scenario, get_active_key, get_scenario


class ScenarioRepository(Repository):
    def __init__(self, base: Repository, scenario_key: Callable[[], str] = get_active_key):
        self.base = base
        self._key = scenario_key

    @property
    def backend_name(self) -> str:  # type: ignore[override]
        return self.base.backend_name

    @property
    def scenario_key(self) -> str:
        return self._key()

    def _scenario(self) -> Scenario:
        return get_scenario(self._key())

    # ---- adjusted reads ----
    def _adjust_item(self, item: dict, sc: Scenario, suppliers: dict[int, dict]) -> dict:
        item = dict(item)
        for adj in sc.stock:
            if item["sku"] in adj.skus:
                item["current_stock"] = round(float(item["current_stock"]) * adj.factor, 3)
        supplier = suppliers.get(item["current_supplier_id"])
        for delay in sc.delays:
            if supplier and supplier["name"] == delay.supplier:
                item["lead_time_days"] = int(item["lead_time_days"]) + delay.extra_days
        return item

    def list_inventory_items(self):
        sc = self._scenario()
        suppliers = {s["id"]: s for s in self.base.list_suppliers()}
        return [self._adjust_item(i, sc, suppliers) for i in self.base.list_inventory_items()]

    def get_inventory_item(self, item_id):
        item = self.base.get_inventory_item(item_id)
        if item is None:
            return None
        suppliers = {s["id"]: s for s in self.base.list_suppliers()}
        return self._adjust_item(item, self._scenario(), suppliers)

    def get_usage_history(self, item_id):
        rows = self.base.get_usage_history(item_id)
        item = self.base.get_inventory_item(item_id)
        if item is None:
            return rows
        out = [dict(r) for r in rows]  # oldest first
        for adj in self._scenario().usage:
            if item["sku"] not in adj.skus:
                continue
            start = len(out) - adj.last_days if adj.last_days else 0
            for idx in range(max(start, 0), len(out)):
                row = out[idx]
                if adj.weekdays is not None and date.fromisoformat(row["usage_date"]).weekday() not in adj.weekdays:
                    continue
                qty = float(row["quantity_used"]) * adj.factor
                row["quantity_used"] = round(qty) if item["unit"] == "each" else round(qty, 1)
        return out

    def list_suppliers(self):
        return [self._adjust_supplier(s) for s in self.base.list_suppliers()]

    def get_supplier(self, supplier_id):
        s = self.base.get_supplier(supplier_id)
        return self._adjust_supplier(s) if s else None

    def _adjust_supplier(self, supplier: dict) -> dict:
        supplier = dict(supplier)
        for delay in self._scenario().delays:
            if supplier["name"] == delay.supplier:
                supplier["average_lead_time_days"] = int(supplier["average_lead_time_days"]) + delay.extra_days
        return supplier

    # ---- pass-through (a scenario does not change these) ----
    def list_supplier_products(self, inventory_item_id=None, supplier_id=None):
        return self.base.list_supplier_products(inventory_item_id, supplier_id)

    def list_procurement_runs(self):
        return self.base.list_procurement_runs()

    def get_procurement_run(self, run_id):
        return self.base.get_procurement_run(run_id)

    def list_market_search_results(self, procurement_run_id):
        return self.base.list_market_search_results(procurement_run_id)

    def list_agent_events(self, procurement_run_id, after_id=0):
        return self.base.list_agent_events(procurement_run_id, after_id)

    def counts(self):
        return self.base.counts()

    def create_procurement_run(self, data):
        return self.base.create_procurement_run(data)

    def update_procurement_run(self, run_id, fields):
        return self.base.update_procurement_run(run_id, fields)

    def transition_procurement_run(self, run_id, from_status, fields):
        return self.base.transition_procurement_run(run_id, from_status, fields)

    def create_rfq_draft(self, data):
        return self.base.create_rfq_draft(data)

    def get_rfq_draft(self, procurement_run_id):
        return self.base.get_rfq_draft(procurement_run_id)

    def transition_rfq_draft(self, procurement_run_id, from_status, fields):
        return self.base.transition_rfq_draft(procurement_run_id, from_status, fields)

    def add_agent_event(self, procurement_run_id, title, detail=None, tone="info", payload=None):
        return self.base.add_agent_event(procurement_run_id, title, detail, tone, payload)

    def upsert_supplier(self, name, fields):
        return self.base.upsert_supplier(name, fields)

    def upsert_supplier_product(self, supplier_id, inventory_item_id, product_name, fields):
        return self.base.upsert_supplier_product(supplier_id, inventory_item_id, product_name, fields)

    def add_market_search_results(self, procurement_run_id, rows):
        return self.base.add_market_search_results(procurement_run_id, rows)
