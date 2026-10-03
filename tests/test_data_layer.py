from datetime import date, timedelta

import pytest

from app import create_app
from app.data import get_base_repo, reset_repo
from app.data.demo_data import HISTORY_DAYS, build_demo_data
from app.data.local_repo import LocalRepository
from app.data.summary import build_data_summary
from app.data.supabase_repo import SupabaseRepository
from app.suppliers import price_per_inventory_unit

TODAY = date(2026, 10, 3)  # a Saturday; any fixed day works


@pytest.fixture
def repo(tmp_path):
    return LocalRepository(tmp_path / "db.json", today=TODAY)


@pytest.fixture
def summary(repo):
    return {i["sku"]: i for i in build_data_summary(repo)["items"]}


# ---------- dataset ----------
def test_dataset_is_reproducible():
    assert build_demo_data(TODAY) == build_demo_data(TODAY)


def test_dataset_shape(repo):
    assert len(repo.list_inventory_items()) == 10
    assert len(repo.list_suppliers()) >= 5
    for item in repo.list_inventory_items():
        usage = repo.get_usage_history(item["id"])
        assert len(usage) == HISTORY_DAYS
        assert usage[-1]["usage_date"] == (TODAY - timedelta(days=1)).isoformat()
        dates = [u["usage_date"] for u in usage]
        assert dates == sorted(dates) and len(set(dates)) == HISTORY_DAYS


def test_foreign_keys_resolve(repo):
    sup_ids = {s["id"] for s in repo.list_suppliers()}
    item_ids = {i["id"] for i in repo.list_inventory_items()}
    assert {i["current_supplier_id"] for i in repo.list_inventory_items()} <= sup_ids
    for p in repo.list_supplier_products():
        assert p["supplier_id"] in sup_ids and p["inventory_item_id"] in item_ids
        assert p["source_type"] in {"incumbent", "historical", "tavily_live", "mock"}


def test_every_item_has_exactly_one_incumbent_offer(repo):
    for item in repo.list_inventory_items():
        inc = [p for p in repo.list_supplier_products(inventory_item_id=item["id"])
               if p["source_type"] == "incumbent"]
        assert len(inc) == 1
        assert inc[0]["supplier_id"] == item["current_supplier_id"]
        # incumbent offer price must agree with the item's current price
        assert price_per_inventory_unit(inc[0]) == pytest.approx(float(item["current_unit_price"]), rel=1e-3)


def test_weekday_pattern(summary):
    for it in summary.values():
        w = it["avg_use_by_weekday"]
        weekdays = max(w[d] for d in ("Mon", "Tue", "Wed", "Thu"))
        assert w["Sat"] > w["Fri"] > weekdays, it["sku"]
        assert w["Sun"] > weekdays and w["Sat"] > w["Sun"], it["sku"]


# ---------- scenarios ----------
def test_mozzarella_is_a_clear_issue(summary):
    m = summary["MOZ-001"]
    assert m["rough_days_until_safety_breach"] < 1           # already at the safety line
    assert m["rough_days_until_stockout"] < m["lead_time_days"]  # runs out before incumbent can deliver


def test_oat_milk_is_approaching(summary):
    o = summary["OAT-001"]
    assert o["lead_time_days"] < o["rough_days_until_safety_breach"] < o["lead_time_days"] + 3


def test_other_items_are_healthy(summary):
    for sku, it in summary.items():
        if sku not in ("MOZ-001", "OAT-001"):
            assert it["rough_days_until_safety_breach"] >= 5, sku


def test_cheapest_supplier_is_not_always_best(summary):
    # Mozzarella: the cheapest per-kg offer cannot arrive before stockout.
    m = summary["MOZ-001"]
    cheapest = m["offers_cheapest_first"][0]
    assert cheapest["supplier"] != m["incumbent"]
    assert cheapest["lead_time_days"] > m["rough_days_until_stockout"]
    # ...and at least one supplier that can arrive in time costs more.
    in_time = [o for o in m["offers_cheapest_first"] if o["lead_time_days"] < m["rough_days_until_stockout"]]
    assert in_time and min(o["price_per_unit"] for o in in_time) > cheapest["price_per_unit"]


def test_suppliers_genuinely_differ(repo):
    s = repo.list_suppliers()
    for field in ("reliability_score", "average_lead_time_days", "minimum_order_value"):
        assert len({x[field] for x in s}) >= 5, field
    assert len({p["delivery_fee"] for p in repo.list_supplier_products()}) >= 4


# ---------- repository behaviour ----------
def test_writes_roundtrip_and_persist(tmp_path):
    path = tmp_path / "db.json"
    r1 = LocalRepository(path, today=TODAY)
    run = r1.create_procurement_run({"inventory_item_id": 1, "required_quantity": 40})
    assert run["status"] == "pending" and run["id"] == 1
    r1.add_market_search_results(run["id"], [{"supplier_name": "X", "source_url": "https://x.example"}])

    r2 = LocalRepository(path, today=TODAY)  # reload from disk
    assert r2.get_procurement_run(1)["required_quantity"] == 40
    assert r2.list_market_search_results(1)[0]["supplier_name"] == "X"
    assert r2.counts()["procurement_runs"] == 1


def test_new_day_refreshes_seed_but_keeps_runs(tmp_path):
    path = tmp_path / "db.json"
    LocalRepository(path, today=TODAY).create_procurement_run({"inventory_item_id": 1, "required_quantity": 5})
    tomorrow = LocalRepository(path, today=TODAY + timedelta(days=1))
    assert tomorrow.get_usage_history(1)[-1]["usage_date"] == TODAY.isoformat()
    assert len(tomorrow.list_procurement_runs()) == 1


def test_returned_rows_are_copies(repo):
    repo.list_inventory_items()[0]["current_stock"] = -1
    assert repo.list_inventory_items()[0]["current_stock"] != -1


def test_get_repo_falls_back_to_local(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    reset_repo()
    assert isinstance(get_base_repo(), LocalRepository)
    reset_repo()


def test_demo_mode_wins_over_credentials(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "k")
    monkeypatch.setenv("DEMO_MODE", "1")
    reset_repo()
    assert isinstance(get_base_repo(), LocalRepository)
    reset_repo()


# ---------- Supabase backend, against a fake client (no network) ----------
class _Resp:
    def __init__(self, data, count=None):
        self.data, self.count = data, count


class _Query:
    def __init__(self, store, table):
        self.store, self.table = store, table
        self.filters, self.order_by, self.desc, self.n, self.insert_rows, self.want_count = {}, None, False, None, None, False

    def select(self, cols="*", count=None):
        self.want_count = bool(count)
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, col, desc=False):
        self.order_by, self.desc = col, desc
        return self

    def limit(self, n):
        self.n = n
        return self

    def insert(self, rows):
        self.insert_rows = rows
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        if self.insert_rows is not None:
            new = self.insert_rows if isinstance(self.insert_rows, list) else [self.insert_rows]
            out = [{"id": len(rows) + i + 1, **r} for i, r in enumerate(new)]
            rows.extend(out)
            return _Resp(out)
        out = [r for r in rows if all(r.get(k) == v for k, v in self.filters.items())]
        if self.order_by:
            out = sorted(out, key=lambda r: r[self.order_by], reverse=self.desc)
        total = len(out)
        if self.n is not None:
            out = out[: self.n]
        return _Resp(out, total if self.want_count else None)


class _FakeClient:
    def __init__(self, store):
        self.store = store

    def table(self, name):
        return _Query(self.store, name)


def test_supabase_backend_matches_local_interface(repo):
    store = {t: rows for t, rows in repo._db["tables"].items()}
    sb = SupabaseRepository(client=_FakeClient(store))

    assert sb.list_inventory_items() == repo.list_inventory_items()
    assert sb.get_inventory_item(1) == repo.get_inventory_item(1)
    assert sb.get_inventory_item(999) is None
    assert sb.get_usage_history(1) == repo.get_usage_history(1)
    assert sb.list_suppliers() == repo.list_suppliers()
    assert sb.list_supplier_products(inventory_item_id=1) == repo.list_supplier_products(inventory_item_id=1)
    assert sb.counts() == repo.counts()

    run = sb.create_procurement_run({"inventory_item_id": 1, "required_quantity": 40})
    assert sb.get_procurement_run(run["id"])["required_quantity"] == 40
    sb.add_market_search_results(run["id"], [{"supplier_name": "X"}])
    assert sb.list_market_search_results(run["id"])[0]["supplier_name"] == "X"


# ---------- Flask route ----------
def test_debug_data_route(monkeypatch, tmp_path):
    monkeypatch.setenv("DEMO_MODE", "1")
    reset_repo()
    app = create_app()
    client = app.test_client()

    monkeypatch.setenv("FLASK_DEBUG", "0")
    assert client.get("/debug/data").status_code == 404

    monkeypatch.setenv("FLASK_DEBUG", "1")
    res = client.get("/debug/data")
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["items"]) == 10 and body["row_counts"]["usage_history"] == 280
    reset_repo()

