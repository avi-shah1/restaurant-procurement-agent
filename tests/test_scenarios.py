"""Scenarios only change INPUT data. These tests check that the generic forecast engine, which
knows nothing about scenarios, reaches the right conclusions from it on every day of the week."""
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

import app.forecast as forecast_module
from app import create_app
from app.data import reset_repo
from app.data.local_repo import LocalRepository
from app.data.scenario_repo import ScenarioRepository
from app.data.scenarios import (DEFAULT_KEY, SCENARIOS, get_active_key, get_scenario,
                                list_scenarios, reset_scenario, set_active)
from app.forecast import SEVERITY, forecast_all, forecast_item

MONDAY = date(2026, 9, 28)
WEEK = [MONDAY + timedelta(days=i) for i in range(7)]


@pytest.fixture(autouse=True)
def _clean_state():
    reset_scenario()
    yield
    reset_scenario()
    reset_repo()


@pytest.fixture
def run(tmp_path):
    """run(scenario_key, today) -> {sku: forecast}, forecasts ordered most urgent first."""
    def _run(key, today):
        base = LocalRepository(tmp_path / f"{key}-{today}.json", today=today)
        repo = ScenarioRepository(base, lambda: key)
        ranked = forecast_all(repo, today=today, include_projection=False)
        return {f["item"]["sku"]: f for f in ranked}, ranked
    return _run


def risky(by_sku):
    return {sku for sku, f in by_sku.items() if f["status"] != "healthy"}


def sev(f):
    return SEVERITY[f["status"]]


# ---------- the forecast engine itself (hand-calculated) ----------
def _flat_item(**kw):
    item = {"sku": "X", "name": "X", "unit": "kg", "current_stock": 100, "safety_stock": 20,
            "lead_time_days": 2, "incoming_quantity": 0, "incoming_date": None}
    item.update(kw)
    return item


def _flat_usage(today, qty=10):
    return [{"usage_date": (today - timedelta(days=i)).isoformat(), "quantity_used": qty}
            for i in range(1, 29)]


TODAY = date(2026, 10, 3)


def test_engine_dates_flat_demand():
    # end-of-day stock: 90, 80, ... 20 (day 7, not below 20), 10 (day 8), 0 (day 9)
    f = forecast_item(_flat_item(), _flat_usage(TODAY), TODAY)
    assert f["predicted_breach_date"] == (TODAY + timedelta(days=8)).isoformat()
    assert f["predicted_stockout_date"] == (TODAY + timedelta(days=9)).isoformat()
    assert f["order_by_date"] == (TODAY + timedelta(days=6)).isoformat()
    assert f["days_until_order_deadline"] == 6 and f["status"] == "healthy"


@pytest.mark.parametrize("lead, status", [(2, "healthy"), (7, "approaching"), (9, "at_risk"), (10, "critical")])
def test_engine_status_boundaries(lead, status):
    f = forecast_item(_flat_item(lead_time_days=lead), _flat_usage(TODAY), TODAY)
    assert f["status"] == status


def test_engine_counts_incoming_delivery():
    item = _flat_item(current_stock=50, incoming_quantity=100,
                      incoming_date=(TODAY + timedelta(days=2)).isoformat())
    without = forecast_item({**item, "incoming_quantity": 0}, _flat_usage(TODAY), TODAY)
    with_delivery = forecast_item(item, _flat_usage(TODAY), TODAY)
    assert without["predicted_breach_date"] == (TODAY + timedelta(days=3)).isoformat()
    assert with_delivery["predicted_breach_date"] == (TODAY + timedelta(days=13)).isoformat()


def test_engine_uses_weekday_demand():
    usage = [{"usage_date": (TODAY - timedelta(days=i)).isoformat(),
              "quantity_used": 30 if (TODAY - timedelta(days=i)).weekday() == 5 else 10}
             for i in range(1, 29)]
    f = forecast_item(_flat_item(), usage, TODAY)  # TODAY is a Saturday
    assert f["daily_projection"][0]["forecast_usage"] == 30 and f["daily_projection"][1]["forecast_usage"] == 10


def test_engine_does_not_know_about_scenarios():
    imports = [l for l in Path(forecast_module.__file__).read_text().splitlines()
               if re.match(r"\s*(from|import) ", l)]
    assert not any("scenario" in l.lower() or "demand_events" in l.lower() for l in imports)
    assert "scenario" not in forecast_item.__code__.co_names


# ---------- scenario definitions ----------
def test_scenario_catalog():
    keys = [s["key"] for s in list_scenarios()]
    assert keys[0] == DEFAULT_KEY
    assert {"default", "weekend_rush", "coffee_spike", "supplier_delay", "avocado_shortage"} <= set(keys)
    assert all(s["label"] and s["description"] for s in list_scenarios())
    with pytest.raises(ValueError):
        get_scenario("nope")


def test_scenarios_reference_real_skus_and_suppliers(tmp_path):
    base = LocalRepository(tmp_path / "db.json", today=TODAY)
    skus = {i["sku"] for i in base.list_inventory_items()}
    names = {s["name"] for s in base.list_suppliers()}
    for sc in SCENARIOS.values():
        for adj in (*sc.usage, *sc.stock):
            assert set(adj.skus) <= skus, sc.key
        for d in sc.delays:
            assert d.supplier in names, sc.key


# ---------- what the engine concludes, on every weekday ----------
@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_default_mozzarella_main_risk_oat_approaching(run, today):
    by, ranked = run("default", today)
    assert ranked[0]["item"]["sku"] == "MOZ-001" and ranked[0]["status"] == "critical"
    assert by["OAT-001"]["status"] == "approaching"
    assert risky(by) == {"MOZ-001", "OAT-001"}


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_avocado_shortage_makes_avocado_the_only_issue(run, today):
    by, ranked = run("avocado_shortage", today)
    assert ranked[0]["item"]["sku"] == "AVO-001" and ranked[0]["status"] == "critical"
    assert risky(by) == {"AVO-001"}


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_coffee_spike_puts_coffee_and_oat_at_the_top(run, today):
    default, _ = run("default", today)
    by, ranked = run("coffee_spike", today)
    assert default["COF-001"]["status"] == "healthy"
    assert by["COF-001"]["status"] != "healthy"
    assert sev(by["OAT-001"]) >= sev(default["OAT-001"])
    assert {ranked[0]["item"]["sku"], ranked[1]["item"]["sku"]} == {"COF-001", "OAT-001"}
    assert risky(by) == {"COF-001", "OAT-001"}


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_supplier_delay_creates_new_risks(run, today):
    default, _ = run("default", today)
    by, _ = run("supplier_delay", today)
    new = risky(by) - risky(default)
    assert {"TOM-001", "AVO-001"} <= new  # FreshRoute lines
    assert sev(by["OAT-001"]) > sev(default["OAT-001"])  # Metro line gets worse
    # items whose supplier is not delayed are unchanged
    for sku in ("COF-001", "MLK-001", "MOZ-001"):
        assert by[sku]["status"] == default[sku]["status"]
    # the delay itself reached the forecast through the data
    assert by["TOM-001"]["lead_time_days"] == default["TOM-001"]["lead_time_days"] + 3


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_weekend_rush_never_improves_anything(run, today):
    default, _ = run("default", today)
    by, _ = run("weekend_rush", today)
    for sku in default:
        assert sev(by[sku]) >= sev(default[sku]), sku
    assert by["FLR-001"]["status"] == "healthy"  # big flour buffer, the engine should see that


def test_weekend_rush_creates_a_tomato_risk_when_the_weekend_is_near(run):
    friday = date(2026, 10, 2)
    default, _ = run("default", friday)
    by, _ = run("weekend_rush", friday)
    assert default["TOM-001"]["status"] == "healthy"
    assert by["TOM-001"]["status"] != "healthy"


@pytest.mark.parametrize("key", list(SCENARIOS))
def test_scenarios_are_deterministic(run, key):
    a, _ = run(key, TODAY)
    b, _ = run(key, TODAY)
    assert a == b


# ---------- reset restores the original seeded state ----------
def test_reset_restores_original_and_never_touches_stored_data(tmp_path):
    base = LocalRepository(tmp_path / "db.json", today=TODAY)
    snapshot = (base.list_inventory_items(), base.list_suppliers(),
                [base.get_usage_history(i["id"]) for i in base.list_inventory_items()])
    view = ScenarioRepository(base)  # follows the active scenario

    def seen():
        return (view.list_inventory_items(), view.list_suppliers(),
                [view.get_usage_history(i["id"]) for i in view.list_inventory_items()])

    assert seen() == snapshot  # default scenario == untouched data
    for key in SCENARIOS:
        if key == DEFAULT_KEY:
            continue
        set_active(key)
        assert seen() != snapshot, key  # the scenario really changed the inputs
        reset_scenario()
        assert get_active_key() == DEFAULT_KEY
        assert seen() == snapshot, key
    assert (base.list_inventory_items(), base.list_suppliers(),
            [base.get_usage_history(i["id"]) for i in base.list_inventory_items()]) == snapshot


# ---------- API routes ----------
@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("FLASK_DEBUG", "1")
    reset_repo()
    return create_app().test_client()


def test_scenario_api(client):
    body = client.get("/api/scenarios").get_json()
    assert body["active"] == DEFAULT_KEY and len(body["scenarios"]) >= 5

    res = client.post("/api/scenarios/active", json={"key": "coffee_spike"})
    assert res.status_code == 200 and res.get_json()["active"] == "coffee_spike"
    assert client.get("/api/scenarios").get_json()["active"] == "coffee_spike"

    assert client.post("/api/scenarios/active", json={"key": "nope"}).status_code == 400
    assert client.post("/api/scenarios/active", json={}).status_code == 400
    assert get_active_key() == "coffee_spike"  # a bad request changes nothing

    assert client.post("/api/scenarios/reset").get_json()["active"] == DEFAULT_KEY


def test_debug_forecast_route(client, monkeypatch):
    res = client.get("/debug/forecast?today=2026-10-02")
    assert res.status_code == 200
    body = res.get_json()
    assert body["scenario"] == DEFAULT_KEY and len(body["items"]) == 10
    assert all("daily_projection" not in i for i in body["items"])

    preview = client.get("/debug/forecast?today=2026-10-02&scenario=avocado_shortage").get_json()
    assert preview["scenario"] == "avocado_shortage" and preview["items"][0]["item"]["sku"] == "AVO-001"
    assert get_active_key() == DEFAULT_KEY  # previewing does not switch the active scenario

    assert client.get("/debug/forecast?scenario=nope").status_code == 400
    assert client.get("/debug/forecast?today=not-a-date").status_code == 400

    # Flask reads FLASK_DEBUG when the app is created, so build a fresh "production" app.
    monkeypatch.setenv("FLASK_DEBUG", "0")
    assert create_app().test_client().get("/debug/forecast").status_code == 404

