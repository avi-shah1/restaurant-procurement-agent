"""Procurement logic, tools, the runner (demo agent + fallback), and the run API."""
import re
import time
from datetime import date

import pytest

from app import create_app
from app.agent import runner
from app.agent.mock_service import MockProcurementAgent
from app.agent.runner import configured_agent, execute_run
from app.agent.service import AgentRunResult, ProcurementAgent
from app.agent.tools import TOOL_DECLARATIONS, ToolContext, execute_tool
from app.agent.zoowork_service import ZooWorkAgent
from app.data import get_base_repo
from app.data.local_repo import LocalRepository
from app.data.scenario_repo import ScenarioRepository
from app.data.scenarios import reset_scenario, set_active
from app.money import money
from app.procurement.market import MockMarketSearch
from app.procurement.options import build_database_options, build_market_options
from app.procurement.recommender import build_recommendation, choose
from app.procurement.requirement import NoProcurementNeeded, build_requirement, create_run
from app.suppliers import plan_order

TODAY = date(2026, 10, 3)  # Saturday; the seeded mozzarella needs 51 kg


@pytest.fixture(autouse=True)
def _clean():
    reset_scenario()
    yield
    reset_scenario()


@pytest.fixture
def base(tmp_path):
    return LocalRepository(tmp_path / "db.json", today=TODAY)


def sku_id(repo, sku):
    return next(i["id"] for i in repo.list_inventory_items() if i["sku"] == sku)


def view(base, scenario="default"):
    return ScenarioRepository(base, lambda: scenario)


@pytest.fixture
def moz(base):
    """A pending mozzarella run plus a tool context for it."""
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    ctx = ToolContext(view(base), run["id"], MockMarketSearch(), agent_mode="mock")
    return run, ctx


# ---------- money / costing ----------
def test_money_is_dollars():
    assert money(12.5) == "$12.50" and money(-29.4) == "-$29.40" and money(1234.5) == "$1,234.50"
    assert money(None) == "n/a"


def test_plan_order_alpine_mozzarella():
    p = plan_order(51, pack_size=6, pack_price=43.20, moq_packs=1, delivery_fee=8)
    assert (p["packs_to_order"], p["order_quantity"]) == (9, 54)  # ceil(51/6) = 9 cases
    assert p["goods_cost"] == 388.80 and p["estimated_total_landed_cost"] == 396.80
    assert p["over_order_quantity"] == 3 and not p["forced_by_moq"]


def test_plan_order_moq_and_minimum_order_value():
    assert plan_order(5, 6, 10, moq_packs=3)["packs_to_order"] == 3
    assert plan_order(5, 6, 10, moq_packs=3)["forced_by_moq"] is True
    bumped = plan_order(10, 2, 16.20, min_order_value=100)  # 5 packs = $81 < $100
    assert bumped["packs_to_order"] == 7 and bumped["bumped_for_minimum_order_value"] is True
    assert bumped["goods_cost"] == pytest.approx(113.40)


# ---------- requirement ----------
def test_requirement_snapshot_for_mozzarella(base):
    req = build_requirement(view(base), sku_id(base, "MOZ-001"), TODAY)
    assert req["required_quantity"] == 51 and req["required_by"] == "2026-10-03"
    assert req["predicted_stockout_date"] == "2026-10-04" and req["currency"] == "USD"
    assert req["lead_time_budget_days"] == {"protect_safety_stock": 0, "avoid_stockout": 1}
    inc = req["incumbent"]
    assert inc["supplier_name"] == "Alpine Dairy Direct"
    assert inc["estimated_total_landed_cost"] == 396.80  # 9 cases x $43.20 + $8.00
    assert inc["avoids_stockout"] is False and inc["meets_required_by"] is False


def test_no_run_when_nothing_is_needed(base):
    with pytest.raises(NoProcurementNeeded, match="Flour"):
        build_requirement(view(base), sku_id(base, "FLR-001"), TODAY)
    with pytest.raises(KeyError):
        build_requirement(view(base), 9999, TODAY)


def test_create_run_stores_the_python_numbers(base):
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    assert run["status"] == "pending" and run["required_quantity"] == 51
    assert run["current_supplier_cost"] == 396.80
    assert run["requirement"]["item"]["sku"] == "MOZ-001"


# ---------- options ----------
def test_existing_and_market_options_for_mozzarella(moz, base):
    run, ctx = moz
    req = run["requirement"]
    existing = {o["supplier_name"]: o for o in build_database_options(view(base), req)}
    assert len(existing) == 5
    assert existing["Alpine Dairy Direct"]["is_incumbent"] and existing["Alpine Dairy Direct"]["avoids_stockout"] is False
    assert existing["QuickStock Express"]["avoids_stockout"] is True
    assert existing["QuickStock Express"]["estimated_total_landed_cost"] == 426.20  # 26 x $16.20 + $5
    assert all(o["price_confirmed"] for o in existing.values())

    rows = ctx.market.search(base.get_inventory_item(req["item"]["id"]), req).rows
    market = {o["supplier_name"]: o for o in build_market_options(req, [{**r, "id": n} for n, r in enumerate(rows, 1)])}
    assert not any(o["price_confirmed"] for o in market.values())          # web results are never confirmed
    assert all(o["evidence"] == "web_search_result_unconfirmed" for o in market.values())
    harvest = market["Harvest & Co Wholesale"]
    assert harvest["lead_time_known"] is False and harvest["meets_required_by"] is None
    assert harvest["estimated_total_landed_cost"] == 372.00                # the cheapest of all
    assert market["Prime Provisions Online"]["estimated_total_landed_cost"] == 416.70


def test_the_rule_set_does_not_chase_the_cheapest_price(moz, base):
    run, ctx = moz
    req = run["requirement"]
    rows = [{**r, "id": n} for n, r in enumerate(ctx.market.search(base.get_inventory_item(req["item"]["id"]), req).rows, 1)]
    options = [*build_database_options(view(base), req), *build_market_options(req, rows)]
    cheapest = min(options, key=lambda o: o["estimated_total_landed_cost"])
    choice = choose(req, options)
    assert cheapest["supplier_name"] == "Harvest & Co Wholesale"  # cheapest, but no stated delivery time
    assert choice["chosen"]["supplier_name"] == "Prime Provisions Online"
    assert choice["chosen"]["estimated_total_landed_cost"] > cheapest["estimated_total_landed_cost"]
    assert choice["backup"]["supplier_name"] == "QuickStock Express"  # the confirmed fallback
    reasons = {o["supplier_name"]: why for o, why in choice["rejected"]}
    assert "not stated" in reasons["Harvest & Co Wholesale"]
    assert "too slow" in reasons["Continental Italian Imports"] and "too slow" in reasons["Alpine Dairy Direct"]


def test_recommendation_numbers_come_from_python(moz, base):
    run, ctx = moz
    req = run["requirement"]
    rows = [{**r, "id": n} for n, r in enumerate(ctx.market.search(base.get_inventory_item(req["item"]["id"]), req).rows, 1)]
    options = [*build_database_options(view(base), req), *build_market_options(req, rows)]
    prime = next(o for o in options if o["supplier_name"] == "Prime Provisions Online")
    rec = build_recommendation(req, options, prime["option_id"], ["because"], [], "Draft an RFQ.")
    assert rec["estimated_total_landed_cost"]["total"] == 416.70
    assert rec["quantity"]["order_quantity"] == 54 and rec["quantity"]["required"] == 51
    assert rec["unit_price"]["per_unit"] == 7.55 and rec["unit_price"]["price_confirmed"] is False
    assert rec["incumbent_cost"]["estimated_total_landed_cost"] == 396.80
    assert rec["potential_savings"]["amount"] == -19.90 and "costs more" in rec["potential_savings"]["note"]
    assert any("unconfirmed web search result" in r for r in rec["risks_and_uncertainties"])
    assert rec["sources"][0]["url"].startswith("https://primeprovisions.example") and not rec["sources"][0]["confirmed"]
    with pytest.raises(ValueError, match="Unknown option_id"):
        build_recommendation(req, options, "made-up", ["x"], [], "y")


# ---------- tools ----------
def test_tool_declarations_follow_the_zoowork_limits():
    names = [t["name"] for t in TOOL_DECLARATIONS]
    assert names == ["get_procurement_requirement", "get_existing_supplier_options",
                     "search_market_prices", "submit_recommendation"]
    assert len(names) <= 32
    for t in TOOL_DECLARATIONS:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", t["name"])
        assert 0 < len(t["description"]) <= 4096
        assert t["input_schema"]["type"] == "object" and len(str(t["input_schema"])) <= 16 * 1024
        assert t["timeoutMs"] <= 86_400_000
    assert not any(re.search(r"send|email|purchase|order|pay", n) for n in names)  # the agent cannot send anything


def test_tool_get_procurement_requirement(moz):
    run, ctx = moz
    result, is_error = execute_tool(ctx, "get_procurement_requirement", {"procurement_run_id": run["id"]})
    assert not is_error and result["requirement"]["required_quantity"] == 51 and result["currency"] == "USD"


@pytest.mark.parametrize("tool, args, message", [
    ("get_procurement_requirement", {"procurement_run_id": 999}, "only authorised"),
    ("get_procurement_requirement", {"procurement_run_id": "1"}, "integer"),
    ("get_procurement_requirement", {}, "integer"),
    ("get_existing_supplier_options", {"inventory_item_id": 999}, "this run is for inventory item"),
    ("search_market_prices", {"inventory_item_id": True}, "integer"),
    ("submit_recommendation", {"recommended_option_id": "db-1", "reasons": ["a"], "risks_and_uncertainties": [],
                               "proposed_next_action": "x", "total_cost": 1.0}, "Unexpected field"),
    ("submit_recommendation", {"recommended_option_id": "db-1", "reasons": [], "risks_and_uncertainties": [],
                               "proposed_next_action": "x"}, "reasons"),
    ("submit_recommendation", {"recommended_option_id": "nope", "reasons": ["a"], "risks_and_uncertainties": [],
                               "proposed_next_action": "x"}, "Unknown option_id"),
    ("send_email", {"to": "x@y.z"}, "Unknown tool"),
])
def test_tools_reject_bad_or_unauthorised_input(moz, tool, args, message):
    run, ctx = moz
    result, is_error = execute_tool(ctx, tool, args)
    assert is_error and message.lower() in result["error"].lower()
    assert ctx.submitted is None  # nothing was saved
    assert ctx.repo.get_procurement_run(run["id"])["recommendation"] is None


def test_tools_reject_non_object_input(moz):
    _, ctx = moz
    assert execute_tool(ctx, "get_procurement_requirement", ["not", "an", "object"])[1] is True


def test_market_search_is_saved_once(moz):
    run, ctx = moz
    item_id = run["requirement"]["item"]["id"]
    first, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    second, _ = execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    assert [o["option_id"] for o in first["options"]] == [o["option_id"] for o in second["options"]]
    rows = ctx.repo.list_market_search_results(run["id"])
    assert len(rows) == 3 and rows[0]["source_domain"].endswith(".example") and rows[0]["raw_price"].startswith("$")
    assert rows[0]["raw_result"]["confirmed"] is False and "NOT confirmed" in first["results_are"]


def test_a_disagreeing_choice_is_saved_but_flagged(moz):
    run, ctx = moz
    item_id = run["requirement"]["item"]["id"]
    existing, _ = execute_tool(ctx, "get_existing_supplier_options", {"inventory_item_id": item_id})
    execute_tool(ctx, "search_market_prices", {"inventory_item_id": item_id})
    quick = next(o for o in existing["options"] if o["supplier_name"] == "QuickStock Express")
    result, is_error = execute_tool(ctx, "submit_recommendation", {
        "recommended_option_id": quick["option_id"], "reasons": ["Known supplier."],
        "risks_and_uncertainties": [], "proposed_next_action": "Draft a purchase order."})
    assert not is_error and result["accepted"] is True
    assert ctx.submitted["meta"]["notes"] and "rule-based check" in ctx.submitted["meta"]["notes"][0]
    saved = ctx.repo.get_procurement_run(run["id"])
    assert saved["recommended_cost"] == 426.20 and saved["status"] == "pending"  # the runner sets status


# ---------- the runner: demo agent ----------
def test_complete_mozzarella_run_in_demo_mode(base):
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    final = execute_run(base, run["id"], agent=MockProcurementAgent())

    assert final["status"] == "awaiting_approval" and final["agent_mode"] == "mock" and final["error"] is None
    assert final["recommended_cost"] == 416.70 and final["potential_saving"] == -19.90
    assert final["current_supplier_cost"] == 396.80 and final["agent_rationale"]
    rec = final["recommendation"]
    assert rec["recommended_supplier"]["name"] == "Prime Provisions Online"
    assert rec["quantity"]["order_quantity"] == 54 and rec["delivery_timing"]["avoids_stockout"] is True
    assert rec["backup_option"]["supplier_name"] == "QuickStock Express"
    for key in ("reasons", "risks_and_uncertainties", "sources", "proposed_next_action", "incumbent_cost",
                "potential_savings", "estimated_total_landed_cost", "unit_price", "delivery_timing", "quantity"):
        assert rec[key], key

    events = base.list_agent_events(run["id"])
    titles = [e["title"] for e in events]
    order = [titles.index(f"Agent: {t}") for t in ("get_procurement_requirement", "get_existing_supplier_options",
                                                    "search_market_prices", "submit_recommendation")]
    assert order == sorted(order)
    assert events[-1]["tone"] == "success" and "nothing has been sent" in events[-1]["detail"].lower()


def test_demo_run_is_deterministic(base):
    ids = [create_run(view(base), sku_id(base, "MOZ-001"), TODAY)["id"] for _ in range(2)]
    recs = []
    for run_id in ids:
        rec = execute_run(base, run_id, agent=MockProcurementAgent())["recommendation"]
        rec["meta"].pop("generated_at")
        rec.pop("option_id")  # market option ids are database row ids, which differ between runs by design
        if rec["backup_option"]:
            rec["backup_option"].pop("option_id")
        for compared in rec["compared_options"]:
            compared.pop("option_id")
        recs.append(rec)
    assert recs[0] == recs[1]


def test_other_items_can_be_run_too(base):
    final = execute_run(base, create_run(view(base), sku_id(base, "OAT-001"), TODAY)["id"], agent=MockProcurementAgent())
    assert final["status"] == "awaiting_approval" and final["recommendation"]["recommended_supplier"]["name"]


def test_requirement_is_a_snapshot_of_the_scenario_it_was_created_in(base):
    set_active("avocado_shortage")
    run = create_run(ScenarioRepository(base), sku_id(base, "AVO-001"), TODAY)
    reset_scenario()  # the world changes before the run executes
    final = execute_run(base, run["id"], agent=MockProcurementAgent())
    assert final["requirement"]["scenario"] == "avocado_shortage" and final["status"] == "awaiting_approval"
    assert final["recommendation"]["quantity"]["required"] == run["required_quantity"]


# ---------- the runner: fallback and failure ----------
class _FailingLiveAgent(ProcurementAgent):
    mode = "live"

    def __init__(self, submit_first: bool = False):
        self.submit_first = submit_first

    def run(self, ctx):
        if self.submit_first:
            MockProcurementAgent().run(ctx)
        raise RuntimeError("ZooWork is down")


def test_live_failure_falls_back_to_the_demo_agent(base):
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    final = execute_run(base, run["id"], agent=_FailingLiveAgent())
    assert final["status"] == "awaiting_approval" and final["agent_mode"] == "fallback"
    assert final["recommendation"]["meta"]["agent_mode"] == "fallback"
    assert final["recommended_cost"] == 416.70
    events = base.list_agent_events(run["id"])
    assert any(e["title"] == "Live agent failed" and "ZooWork is down" in e["detail"] and e["tone"] == "error" for e in events)
    assert any("Falling back" in e["title"] for e in events)


def test_with_fallback_off_a_live_failure_is_a_clean_failed_run(base, monkeypatch):
    monkeypatch.setenv("AGENT_FALLBACK", "0")
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    final = execute_run(base, run["id"], agent=_FailingLiveAgent())
    assert final["status"] == "failed" and "ZooWork is down" in final["error"]
    assert final["recommendation"] is None
    assert base.list_agent_events(run["id"])[-1]["title"] == "Run failed"


def test_a_recommendation_saved_before_a_late_failure_is_kept(base):
    run = create_run(view(base), sku_id(base, "MOZ-001"), TODAY)
    final = execute_run(base, run["id"], agent=_FailingLiveAgent(submit_first=True))
    assert final["status"] == "awaiting_approval" and final["recommendation"]
    assert final["agent_mode"] == "live"
    assert any("ended with an error after submitting" in e["title"] for e in base.list_agent_events(run["id"]))


def test_agent_selection_from_environment(monkeypatch):
    assert isinstance(configured_agent(), MockProcurementAgent)  # no credentials: demo agent
    monkeypatch.setenv("ZOOWORK_API_KEY", "zwp_live_x")
    assert isinstance(configured_agent(), MockProcurementAgent)  # key alone is not enough
    monkeypatch.setenv("ZOOWORK_AGENT_ID", "agt_x")
    assert isinstance(configured_agent(), ZooWorkAgent)          # both set: live
    monkeypatch.setenv("AGENT_MODE", "mock")
    assert isinstance(configured_agent(), MockProcurementAgent)  # explicit override


# ---------- API ----------
@pytest.fixture
def client():
    return create_app(run_sync=True).test_client()


def test_run_api_end_to_end(client):
    res = client.post("/api/runs", json={"sku": "MOZ-001"})
    assert res.status_code == 202
    body = res.get_json()
    assert body["status"] == "awaiting_approval" and body["agent_mode"] == "mock"
    assert body["recommendation"]["recommended_supplier"]["name"] and body["item"]["sku"] == "MOZ-001"
    assert body["events"] and body["last_event_id"] == body["events"][-1]["id"]

    got = client.get(f"/api/runs/{body['id']}").get_json()
    assert got["recommendation"] == body["recommendation"]
    tail = client.get(f"/api/runs/{body['id']}?after_event_id={body['events'][2]['id']}").get_json()
    assert len(tail["events"]) == len(body["events"]) - 3

    listed = client.get("/api/runs").get_json()["runs"]
    assert listed[0]["id"] == body["id"] and listed[0]["recommended_cost"] == body["recommendation"]["estimated_total_landed_cost"]["total"]


def test_run_api_errors(client):
    assert client.post("/api/runs", json={"sku": "FLR-001"}).status_code == 409   # nothing to order
    assert client.post("/api/runs", json={"inventory_item_id": 999}).status_code == 404
    assert client.post("/api/runs", json={}).status_code == 400
    assert client.post("/api/runs", json={"sku": "NOPE"}).status_code == 400
    assert client.post("/api/runs", json={"inventory_item_id": True}).status_code == 400
    assert client.get("/api/runs/999").status_code == 404
    assert client.get("/api/runs/1?after_event_id=x").status_code == 400


def test_the_frontend_gets_a_stable_shape_when_a_run_fails(client, monkeypatch):
    monkeypatch.setenv("AGENT_MODE", "live")      # live requested but no credentials...
    monkeypatch.setenv("AGENT_FALLBACK", "0")     # ...and no fallback
    body = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert body["status"] == "failed" and body["error"] and body["recommendation"] is None
    assert body["item"]["sku"] == "MOZ-001" and body["events"][-1]["tone"] == "error"
    assert set(body) >= {"id", "status", "agent_mode", "error", "item", "requirement", "recommendation", "events"}


def test_the_demo_fallback_keeps_the_frontend_working(client, monkeypatch):
    monkeypatch.setattr(runner, "configured_agent", lambda: _FailingLiveAgent())
    body = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert body["status"] == "awaiting_approval" and body["agent_mode"] == "fallback"
    assert body["recommendation"]["recommended_supplier"]["name"] == "Prime Provisions Online"


def test_runs_execute_in_the_background_without_blocking(monkeypatch):
    client = create_app().test_client()           # run_sync False: real worker thread
    res = client.post("/api/runs", json={"sku": "MOZ-001"})
    assert res.status_code == 202
    run_id = res.get_json()["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").get_json()
        if body["status"] == "awaiting_approval":
            break
        time.sleep(0.05)
    assert body["status"] == "awaiting_approval" and body["recommendation"]
    assert get_base_repo().get_procurement_run(run_id)["status"] == "awaiting_approval"

