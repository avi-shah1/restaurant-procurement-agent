"""Approval, the RFQ draft, and the stage timeline.

The rule under test: approving records a person's decision and creates an RFQ DRAFT. It places no purchase
order and sends nothing. The agent cannot do any of this; only these routes can."""
import copy
import re
import threading
from pathlib import Path

import pytest
import tavily

from app import create_app
from app.agent import runner
from app.agent.tools import MAX_REASON, MAX_REASONS, ToolContext, execute_tool
from app.data import get_base_repo
from app.procurement.decision import DecisionError, approve_run, reject_run
from app.procurement.market import MockMarketSearch
from app.procurement.rfq import BENCHMARK_MIN_CONFIDENCE, build_rfq, pick_benchmark
from app.procurement.stages import build_stages
from app.procurement.tavily_market import TavilyMarketSearch
from tests.fake_tavily import FAST, LEAD, SLOW, FakeTavily, result

# a clean, believable public price from a different site: $2.29/lb = $5.05/kg, cheaper than FastCheese's $7.50
BENCH = result("Mozzarella - Wholesale Depot", "https://benchmark-wholesale.example/mozzarella",
               "Cheese, Mozzarella, Low Moisture Whole Milk, 6 Pound Avg Loaf. $2.29. /lb per loaf. Add to Cart.", 0.8)

ROOT = Path(__file__).resolve().parents[1]


def new_run(fake=None, **env):
    """One real run through the API (own MonkeyPatch context, so the suite's safety setup stays in place)."""
    with pytest.MonkeyPatch.context() as mp:
        for k, v in env.items():
            mp.setenv(k, v)
        if fake is not None:
            mp.setattr(runner, "get_market_provider", lambda: TavilyMarketSearch(api_key="tvly-test", client=fake))
        client = create_app(run_sync=True).test_client()
        return client, client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()


@pytest.fixture
def run():
    client, view = new_run(FakeTavily(results=[FAST, SLOW, LEAD]))
    assert view["status"] == "awaiting_approval"
    return client, view


@pytest.fixture
def run_with_benchmark():
    client, view = new_run(FakeTavily(results=[FAST, SLOW, LEAD, BENCH]))
    assert view["recommendation"]["recommended_supplier"]["name"] == "fastcheese-supply.example"
    return client, view


# ---------- approve ----------
def test_approving_records_the_decision_and_creates_an_rfq_draft(run):
    client, view = run
    res = client.post(f"/api/runs/{view['id']}/approve", json={"note": "Looks right"})
    assert res.status_code == 200
    body = res.get_json()
    assert body["status"] == "approved" and body["changed"] is True
    assert body["decision"]["outcome"] == "approved" and body["decision"]["decided_by"] == "demo user"
    assert body["decision"]["note"] == "Looks right" and body["decision"]["decided_at"]

    stored = get_base_repo().get_procurement_run(view["id"])  # recorded in the database, not just the response
    assert stored["status"] == "approved" and stored["decision_note"] == "Looks right" and stored["decided_at"]

    rfq = body["rfq"]
    assert rfq["status"] == "draft" and rfq["sent_at"] is None and "Not sent yet" in rfq["send_note"]
    assert rfq["supplier_name"] == "fastcheese-supply.example" and rfq["to_email"] is None  # a web supplier: no address
    assert get_base_repo().get_rfq_draft(view["id"])["id"] == rfq["id"]

    titles = [e["title"] for e in get_base_repo().list_agent_events(view["id"])]
    assert "RFQ approved by a person" in titles and "RFQ draft created" in titles
    approved = next(e for e in get_base_repo().list_agent_events(view["id"]) if e["title"] == "RFQ approved by a person")
    assert "No purchase order was placed" in approved["detail"]


def test_the_rfq_follows_the_requested_wording_and_asks_for_a_quote_not_an_order(run):
    client, view = run
    rfq = client.post(f"/api/runs/{view['id']}/approve").get_json()["rfq"]
    req = view["requirement"]
    qty = f"{req['required_quantity']:g}"
    body = rfq["body"]
    assert f"We are looking to purchase about {qty} kg of Mozzarella (low-moisture) for delivery by" in body
    assert "We are reviewing current market options and would appreciate your best delivered price" in body
    assert "including minimum order quantity (MOQ)" in body and "expected delivery timing" in body
    assert "This is a request for a quote only. It is not a purchase order." in body
    assert rfq["subject"].startswith("Request for quote:") and qty in rfq["subject"]
    d = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday) \d{1,2} \w+ \d{4}", body)
    assert d, "the delivery date is written out in full"
    assert body.splitlines()[0] == "Hello fastcheese-supply.example team,"


def test_the_rfq_endpoint(run):
    client, view = run
    assert client.get(f"/api/runs/{view['id']}/rfq").status_code == 404        # nothing before a person approves
    assert client.get("/api/runs/9999/rfq").status_code == 404
    client.post(f"/api/runs/{view['id']}/approve")
    assert client.get(f"/api/runs/{view['id']}/rfq").get_json()["status"] == "draft"


def test_a_supplier_we_already_know_gets_its_contact_email_on_the_draft():
    client, view = new_run(FakeTavily(search_errors=[tavily.InvalidAPIKeyError("Unauthorized")]))
    assert view["recommendation"]["recommended_supplier"]["origin"] == "database"
    rfq = client.post(f"/api/runs/{view['id']}/approve").get_json()["rfq"]
    assert rfq["to_email"] and rfq["to_email"].endswith(".example") and rfq["supplier_id"] is not None


# ---------- negotiation evidence ----------
def test_a_reliable_public_price_may_be_used_but_never_as_a_competitor_quote(run_with_benchmark):
    client, view = run_with_benchmark
    rfq = client.post(f"/api/runs/{view['id']}/approve").get_json()["rfq"]
    assert rfq["benchmark"]["used"] is True and rfq["benchmark"]["price_per_unit"] == 5.05
    assert rfq["benchmark"]["confidence"] >= BENCHMARK_MIN_CONFIDENCE
    body = rfq["body"]
    assert "publicly listed prices for comparable Mozzarella (low-moisture) are currently around $5.05 per kg" in body
    assert "These are public listings only, not quotes we hold" in body
    lowered = body.lower()
    for forbidden in ("competitor", "binding", "has quoted", "quoted us", "offered us", "guarantee",
                      "benchmark-wholesale", "wholesale depot"):
        assert forbidden not in lowered, forbidden                      # no false claim, and no naming the source


def test_without_reliable_evidence_the_rfq_mentions_no_market_prices(run):
    client, view = run
    rfq = client.post(f"/api/runs/{view['id']}/approve").get_json()["rfq"]
    assert rfq["benchmark"]["used"] is False and "No sufficiently reliable public price" in rfq["benchmark"]["reason"]
    assert "publicly listed" not in rfq["body"] and "$" not in rfq["body"]


def _row(**kw):
    raw = {"provider": "tavily", "is_retail": False, "price_is_range": False, "price_suspect": False}
    raw.update(kw.pop("raw", {}))
    base = {"supplier_name": "other.example", "normalised_unit_price": 5.0, "confidence": 0.8,
            "source_url": "https://other.example/x", "source_domain": "other.example", "raw_result": raw}
    base.update(kw)
    return base


REC = {"recommended_supplier": {"name": "chosen.example"}, "unit_price": {"per_unit": 7.5}}


@pytest.mark.parametrize("row_kwargs, why", [
    ({"confidence": 0.69}, "below"), ({"raw": {"is_retail": True}}, "retail"), ({"raw": {"price_is_range": True}}, "range"),
    ({"raw": {"price_suspect": True}}, "implausible"), ({"supplier_name": "chosen.example"}, "same supplier"),
    ({"normalised_unit_price": 7.5}, "not cheaper"), ({"normalised_unit_price": 9.0}, "not cheaper"),
    ({"normalised_unit_price": None}, "unit price"),
])
def test_market_evidence_must_clear_every_bar_before_it_is_mentioned(row_kwargs, why):
    assert pick_benchmark(REC, [_row(**row_kwargs)])["used"] is False
    assert pick_benchmark(REC, [_row(), _row(**row_kwargs)])["used"] is True   # the one good row still counts


def test_demo_data_is_never_used_as_evidence_and_the_cheapest_good_one_wins():
    assert pick_benchmark(REC, [_row(raw={"provider": "mock"})])["used"] is False
    assert pick_benchmark(REC, [])["used"] is False and "No market search results" in pick_benchmark(REC, [])["reason"]
    best = pick_benchmark(REC, [_row(normalised_unit_price=6.0), _row(normalised_unit_price=4.5), _row(normalised_unit_price=5.5)])
    assert best["price_per_unit"] == 4.5


# ---------- idempotency, state rules, races ----------
def test_approving_twice_is_safe_and_creates_one_rfq(run):
    client, view = run
    first = client.post(f"/api/runs/{view['id']}/approve").get_json()
    second = client.post(f"/api/runs/{view['id']}/approve")
    assert second.status_code == 200 and second.get_json()["changed"] is False
    assert second.get_json()["rfq"]["id"] == first["rfq"]["id"]
    repo = get_base_repo()
    assert repo.counts()["rfq_drafts"] == 1
    assert sum(e["title"] == "RFQ approved by a person" for e in repo.list_agent_events(view["id"])) == 1


def test_a_rejected_run_stays_rejected_and_has_no_rfq(run):
    client, view = run
    res = client.post(f"/api/runs/{view['id']}/reject", json={"reason": "Too expensive"}).get_json()
    assert res["status"] == "rejected" and res["decision"]["note"] == "Too expensive" and res["rfq"] is None
    assert client.get(f"/api/runs/{view['id']}/rfq").status_code == 404
    assert client.post(f"/api/runs/{view['id']}/approve").status_code == 409     # cannot approve after rejecting
    assert client.post(f"/api/runs/{view['id']}/reject").status_code == 200       # rejecting again is harmless
    assert get_base_repo().counts()["rfq_drafts"] == 0


def test_an_approved_run_cannot_be_rejected(run):
    client, view = run
    client.post(f"/api/runs/{view['id']}/approve")
    res = client.post(f"/api/runs/{view['id']}/reject")
    assert res.status_code == 409 and "approved" in res.get_json()["error"]
    assert get_base_repo().get_procurement_run(view["id"])["status"] == "approved"


def test_only_a_run_awaiting_approval_can_be_decided(run):
    client, view = run
    repo = get_base_repo()
    for status in ("failed", "running", "pending"):
        repo.update_procurement_run(view["id"], {"status": status})
        for action in ("approve", "reject"):
            res = client.post(f"/api/runs/{view['id']}/{action}")
            assert res.status_code == 409 and status in res.get_json()["error"], (status, action)
    assert repo.counts()["rfq_drafts"] == 0
    assert client.post("/api/runs/9999/approve").status_code == 404 and client.post("/api/runs/9999/reject").status_code == 404


def test_notes_are_validated(run):
    client, view = run
    assert client.post(f"/api/runs/{view['id']}/approve", json={"note": "x" * 501}).status_code == 400
    assert client.post(f"/api/runs/{view['id']}/approve", json={"note": 123}).status_code == 400
    assert client.post(f"/api/runs/{view['id']}/approve", json=[1, 2]).status_code == 200   # a non-object body is ignored
    assert get_base_repo().get_procurement_run(view["id"])["status"] == "approved"


def test_twenty_simultaneous_approvals_make_exactly_one_decision(run):
    _, view = run
    repo = get_base_repo()
    results, errors, barrier = [], [], threading.Barrier(20)

    def click():
        barrier.wait()
        try:
            results.append(approve_run(repo, view["id"])[1])
        except Exception as e:  # pragma: no cover - would be a failure
            errors.append(e)

    threads = [threading.Thread(target=click) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and results.count(True) == 1 and results.count(False) == 19
    assert repo.counts()["rfq_drafts"] == 1
    assert sum(e["title"] == "RFQ approved by a person" for e in repo.list_agent_events(view["id"])) == 1


def test_approve_and_reject_racing_leave_one_winner(run):
    _, view = run
    repo = get_base_repo()
    outcomes, barrier = [], threading.Barrier(2)

    def go(fn):
        barrier.wait()
        try:
            fn(repo, view["id"])
            outcomes.append("ok")
        except DecisionError as e:
            outcomes.append(e.status)

    threads = [threading.Thread(target=go, args=(f,)) for f in (approve_run, reject_run)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(map(str, outcomes)) == ["409", "ok"]
    final = repo.get_procurement_run(view["id"])["status"]
    assert final in ("approved", "rejected") and repo.counts()["rfq_drafts"] == (1 if final == "approved" else 0)


def test_if_the_draft_cannot_be_saved_the_approval_is_rolled_back(run, monkeypatch):
    _, view = run
    repo = get_base_repo()
    monkeypatch.setattr(repo, "create_rfq_draft", lambda data: (_ for _ in ()).throw(RuntimeError("disk full")))
    with pytest.raises(RuntimeError, match="disk full"):
        approve_run(repo, view["id"])
    stored = repo.get_procurement_run(view["id"])
    assert stored["status"] == "awaiting_approval" and stored["decided_at"] is None and stored["decided_by"] is None
    assert "RFQ approved by a person" not in [e["title"] for e in repo.list_agent_events(view["id"])]


def test_the_compare_and_set_refuses_a_stale_status(run):
    _, view = run
    repo = get_base_repo()
    assert repo.transition_procurement_run(view["id"], "awaiting_approval", {"status": "approved"})["status"] == "approved"
    assert repo.transition_procurement_run(view["id"], "awaiting_approval", {"status": "rejected"}) is None
    assert repo.transition_procurement_run(9999, "awaiting_approval", {"status": "approved"}) is None
    with pytest.raises(ValueError):
        repo.transition_procurement_run(view["id"], "approved", {"not_a_column": 1})


# ---------- Phase 7: send only after approve; agent still cannot send ----------
def test_approving_alone_does_not_send():
    client, view = new_run(FakeTavily(results=[FAST]))
    rfq = client.post(f"/api/runs/{view['id']}/approve").get_json()["rfq"]
    assert rfq["status"] == "draft" and rfq["sent_at"] is None
    agent_tools = {t["name"] for t in __import__("app.agent.tools", fromlist=["TOOL_DECLARATIONS"]).TOOL_DECLARATIONS}
    assert not any(re.search(r"rfq|approve|send|email|order", n) for n in agent_tools)


def test_send_requires_approval_and_a_recipient(run, monkeypatch):
    client, view = run
    assert client.post(f"/api/runs/{view['id']}/send").status_code == 409  # not approved yet
    client.post(f"/api/runs/{view['id']}/approve")
    monkeypatch.delenv("TEST_EMAIL_OVERRIDE", raising=False)
    res = client.post(f"/api/runs/{view['id']}/send")
    assert res.status_code == 400 and "TEST_EMAIL_OVERRIDE" in res.get_json()["error"]


def test_mock_send_with_test_override_records_the_message_and_simulated_reply(run, monkeypatch):
    monkeypatch.setenv("TEST_EMAIL_OVERRIDE", "demo-inbox@example.com")
    monkeypatch.setenv("RFQ_EMAIL_MODE", "mock")
    monkeypatch.setenv("RFQ_SIMULATE_REPLY", "1")
    client, view = run
    client.post(f"/api/runs/{view['id']}/approve")
    res = client.post(f"/api/runs/{view['id']}/send")
    assert res.status_code == 200
    body = res.get_json()
    assert body["changed"] is True
    rfq = body["rfq"]
    assert rfq["status"] == "sent" and rfq["sent_at"]
    record = rfq["send_record"]
    assert record["recipient"] == "demo-inbox@example.com"
    assert record["is_test_redirect"] is True
    assert record["delivery_mode"] == "mock"
    assert record["procurement_run_id"] == view["id"]
    assert record["subject"].startswith("[TEST/DEMO")
    assert "TEST / DEMO EMAIL" in record["body"]
    assert rfq["supplier_reply"]["simulated"] is True
    assert "SIMULATED" in rfq["supplier_reply"]["label"]
    assert body["stages"][-1]["label"] == "RFQ sent"
    titles = [e["title"] for e in get_base_repo().list_agent_events(view["id"])]
    assert any("RFQ sent" in t for t in titles)
    assert "SIMULATED supplier reply" in titles
    # idempotent
    again = client.post(f"/api/runs/{view['id']}/send").get_json()
    assert again["changed"] is False and again["rfq"]["status"] == "sent"


# ---------- the data the card shows ----------
def test_the_recommendation_carries_what_the_card_needs(run):
    _, view = run
    rec = view["recommendation"]
    assert rec["potential_savings"]["percent"] == pytest.approx(round(rec["potential_savings"]["amount"] / rec["incumbent_cost"]["estimated_total_landed_cost"] * 100, 1))
    assert rec["incumbent_cost"]["price_per_unit"] == pytest.approx(7.20)
    assert rec["moq"] == {"known": False, "packs": None, "selling_unit": "pack", "pack_size": 6.0}      # not stated: unknown
    assert rec["recommended_supplier"]["reliability"] is None                                           # unknown for a web result
    assert any("Stock availability has not been confirmed" in r for r in rec["risks_and_uncertainties"])


def test_a_supplier_we_know_shows_reliability_and_moq():
    _, view = new_run(FakeTavily(search_errors=[tavily.InvalidAPIKeyError("Unauthorized")]))
    rec = view["recommendation"]
    assert 0 < rec["recommended_supplier"]["reliability"] <= 1 and rec["moq"]["known"] is True and rec["moq"]["packs"] >= 1


def test_reasons_are_two_to_four_short_points(run):
    _, view = run
    reasons = view["recommendation"]["reasons"]
    assert 2 <= len(reasons) <= MAX_REASONS and all(len(r) <= MAX_REASON for r in reasons)


def test_the_agent_cannot_submit_a_wall_of_reasons():
    from datetime import date
    from app.data.local_repo import LocalRepository
    from app.data.scenario_repo import ScenarioRepository
    from app.procurement.requirement import create_run
    import tempfile
    base = LocalRepository(Path(tempfile.mkdtemp()) / "db.json", today=date(2026, 10, 3))
    repo = ScenarioRepository(base, lambda: "default")
    item_id = next(i["id"] for i in base.list_inventory_items() if i["sku"] == "MOZ-001")
    r = create_run(repo, item_id, date(2026, 10, 3))
    ctx = ToolContext(repo, r["id"], MockMarketSearch())
    existing, _ = execute_tool(ctx, "get_existing_supplier_options", {"inventory_item_id": item_id})
    option = existing["options"][0]["option_id"]
    base_args = {"recommended_option_id": option, "risks_and_uncertainties": [], "proposed_next_action": "Draft an RFQ."}
    for reasons in (["short"] * (MAX_REASONS + 1), ["x" * (MAX_REASON + 1)]):
        out, is_error = execute_tool(ctx, "submit_recommendation", {**base_args, "reasons": reasons})
        assert is_error and "at most 4 reasons" in out["error"]
    out, is_error = execute_tool(ctx, "submit_recommendation", {**base_args, "reasons": ["A", "B"]})
    assert not is_error


# ---------- the stage timeline ----------
KEYS = ["analysed", "shortage", "triggered", "incumbent", "search", "options", "recommendation", "awaiting", "decision"]


def states(view):
    return [s["state"] for s in view["stages"]]


def test_a_finished_run_shows_all_nine_stages_with_the_approval_waiting(run):
    _, view = run
    assert [s["key"] for s in view["stages"]] == KEYS and len(view["stages"]) == 9
    assert states(view) == ["done"] * 7 + ["active", "pending"]
    labels = [s["label"] for s in view["stages"]]
    assert labels == ["Inventory analysed", "Shortage predicted", "Procurement triggered", "Incumbent checked",
                      "Tavily search started", "Supplier options found", "Recommendation created",
                      "Awaiting approval", "RFQ approval"]
    assert "2 priced options" in view["stages"][5]["detail"] and "3 sources" in view["stages"][4]["detail"]


def test_approving_completes_the_last_stage(run):
    client, view = run
    after = client.post(f"/api/runs/{view['id']}/approve").get_json()
    assert states(after) == ["done"] * 9 and after["stages"][-1]["label"] == "RFQ approved"
    assert "Not sent yet" in after["stages"][-1]["detail"] and after["stages"][-1]["at"] == after["decision"]["decided_at"]


def test_rejecting_ends_the_timeline_with_a_warning(run):
    client, view = run
    after = client.post(f"/api/runs/{view['id']}/reject", json={"reason": "Too slow"}).get_json()
    assert after["stages"][-1] == {**after["stages"][-1], "label": "Rejected by a person", "state": "warning", "detail": "Too slow"}
    assert states(after)[:8] == ["done"] * 8


def test_a_search_outage_is_a_warning_not_an_error():
    _, view = new_run(FakeTavily(search_errors=[tavily.InvalidAPIKeyError("Unauthorized")]))
    search, options = view["stages"][4], view["stages"][5]
    assert search["state"] == "warning" and "unavailable" in search["detail"] and "API key" in search["detail"]
    assert options["state"] == "warning" and "Existing suppliers only" in options["detail"]
    assert view["stages"][6]["state"] == "done"                                     # the run still produced a recommendation


def test_demo_market_data_is_labelled_as_demo():
    _, view = new_run()
    assert view["stages"][4]["label"] == "Demo market data search started" and "demo data" in view["stages"][4]["detail"]


def test_a_failed_run_stops_at_the_stage_that_failed():
    _, view = new_run(AGENT_MODE="live", AGENT_FALLBACK="0")
    assert view["status"] == "failed"
    s = view["stages"]
    assert s[0]["state"] == "done" and s[2]["state"] == "done"
    assert s[3]["state"] == "error" and "ZOOWORK_API_KEY" in s[3]["detail"]        # first stage that did not happen
    assert all(x["state"] == "pending" for x in s[4:])


def test_a_run_in_progress_has_one_active_stage():
    run_row = {"id": 1, "status": "running", "created_at": "2026-10-03T10:00:00+00:00", "requirement": {
        "item": {"unit": "kg"}, "current_stock": 14, "safety_stock": 12, "required_quantity": 51,
        "predicted_stockout_date": "2026-10-04", "predicted_breach_date": "2026-10-03",
        "usage_model": {"trend_multiplier": 1.0}, "incumbent": None}}
    s = build_stages(run_row, [{"title": "Agent: get_procurement_requirement", "created_at": "t"}])
    assert [x["state"] for x in s].count("active") == 1 and s[3]["state"] == "active" and s[2]["state"] == "done"
    s = build_stages(run_row, [{"title": "Agent: get_existing_supplier_options", "created_at": "2026-10-03T10:00:05+00:00"}])
    assert s[3]["state"] == "done" and s[4]["state"] == "active"                   # now the market search is running


def test_the_view_always_has_stages_decision_and_rfq(run):
    _, view = run
    assert set(view) >= {"stages", "decision", "rfq"} and view["decision"]["outcome"] is None and view["rfq"] is None


# ---------- runs saved by an earlier version still show a complete card ----------
def _strip_to_the_old_shape(repo, view):
    rec = copy.deepcopy(view["recommendation"])
    for gone in ("moq",):
        rec.pop(gone)
    rec["recommended_supplier"].pop("reliability")
    rec["incumbent_cost"].pop("price_per_unit")
    rec["potential_savings"].pop("percent")
    rec["risks_and_uncertainties"] = [r for r in rec["risks_and_uncertainties"] if "Stock availability" not in r]
    repo.update_procurement_run(view["id"], {"recommendation": rec})


@pytest.mark.parametrize("fake", [FakeTavily(results=[FAST, SLOW, LEAD]),
                                  FakeTavily(search_errors=[tavily.InvalidAPIKeyError("x")])],
                         ids=["web supplier", "supplier we know"])
def test_an_older_saved_run_gets_its_missing_card_fields_from_stored_data(fake):
    client, fresh = new_run(fake)
    repo = get_base_repo()
    _strip_to_the_old_shape(repo, fresh)
    assert "moq" not in repo.get_procurement_run(fresh["id"])["recommendation"]     # really the old shape in the database
    upgraded = client.get(f"/api/runs/{fresh['id']}").get_json()["recommendation"]
    original = fresh["recommendation"]
    assert upgraded["moq"] == original["moq"]
    assert upgraded["recommended_supplier"]["reliability"] == original["recommended_supplier"]["reliability"]
    assert upgraded["incumbent_cost"]["price_per_unit"] == original["incumbent_cost"]["price_per_unit"]
    assert upgraded["potential_savings"]["percent"] == original["potential_savings"]["percent"]
    assert any("Stock availability" in r for r in upgraded["risks_and_uncertainties"])
    assert "moq" not in repo.get_procurement_run(fresh["id"])["recommendation"]     # the stored row was not rewritten


def test_a_modern_run_is_returned_unchanged(run):
    _, view = run
    assert view["recommendation"] == get_base_repo().get_procurement_run(view["id"])["recommendation"]
