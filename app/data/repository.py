"""The data-access interface. Business logic talks to this and never to a backend directly.

Every method returns plain dicts whose keys match the column names in supabase/schema.sql.
Dates and timestamps are ISO strings in every backend.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Repository(ABC):
    backend_name: str = "abstract"

    # ---- reads ----
    @abstractmethod
    def list_inventory_items(self) -> list[dict]: ...

    @abstractmethod
    def get_inventory_item(self, item_id: int) -> dict | None: ...

    @abstractmethod
    def get_usage_history(self, item_id: int) -> list[dict]:
        """Oldest first."""

    @abstractmethod
    def list_suppliers(self) -> list[dict]: ...

    @abstractmethod
    def get_supplier(self, supplier_id: int) -> dict | None: ...

    @abstractmethod
    def list_supplier_products(self, inventory_item_id: int | None = None,
                               supplier_id: int | None = None) -> list[dict]: ...

    @abstractmethod
    def list_procurement_runs(self) -> list[dict]:
        """Newest first."""

    @abstractmethod
    def get_procurement_run(self, run_id: int) -> dict | None: ...

    @abstractmethod
    def list_market_search_results(self, procurement_run_id: int) -> list[dict]: ...

    @abstractmethod
    def list_agent_events(self, procurement_run_id: int, after_id: int = 0) -> list[dict]:
        """Timeline events for one run, oldest first, only those with id > after_id."""

    @abstractmethod
    def counts(self) -> dict[str, int]:
        """Row count per table."""

    # ---- writes (only the tables the agent workflow writes to) ----
    @abstractmethod
    def create_procurement_run(self, data: dict) -> dict: ...

    @abstractmethod
    def update_procurement_run(self, run_id: int, fields: dict) -> dict:
        """Change some columns of a run; returns the updated row."""

    @abstractmethod
    def transition_procurement_run(self, run_id: int, from_status: str, fields: dict) -> dict | None:
        """Atomically change a run ONLY if it is still in `from_status` (compare-and-set).
        Returns the updated row, or None if the status had already changed (or the run does not exist).
        This is what stops two clicks, or two browser tabs, from deciding the same run twice."""

    @abstractmethod
    def create_rfq_draft(self, data: dict) -> dict:
        """One draft per run. If the run already has one, the existing draft is returned unchanged."""

    @abstractmethod
    def get_rfq_draft(self, procurement_run_id: int) -> dict | None: ...

    @abstractmethod
    def transition_rfq_draft(self, procurement_run_id: int, from_status: str, fields: dict) -> dict | None:
        """Atomically update an RFQ draft ONLY if it is still in `from_status` (compare-and-set).
        Returns the updated row, or None if the status had already changed (or the draft does not exist)."""

    @abstractmethod
    def add_agent_event(self, procurement_run_id: int, title: str, detail: str | None = None,
                        tone: str = "info", payload: dict | None = None) -> dict: ...

    @abstractmethod
    def add_market_search_results(self, procurement_run_id: int, rows: list[dict]) -> list[dict]: ...

    @abstractmethod
    def upsert_supplier(self, name: str, fields: dict) -> tuple[dict, bool]:
        """Find a supplier by name or create it. An existing supplier is returned UNCHANGED.
        Returns (row, created)."""

    @abstractmethod
    def upsert_supplier_product(self, supplier_id: int, inventory_item_id: int, product_name: str,
                                fields: dict) -> tuple[dict, str]:
        """Create or refresh a WEB-DISCOVERED offer (source_type is always 'tavily_live').
        An existing offer from our own records (incumbent/historical/mock) is never touched.
        Returns (row, action) where action is 'created', 'updated' or 'skipped'."""
