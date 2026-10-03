"""The live ZooWork agent and provisioning, driven by the REAL zoowork SDK against a fake HTTP API.

"Offline-tested": the SDK code paths are real, but the server is ours (tests/fake_zoowork.py), built
from the installed SDK source and the ZooWork docs. A real run against ZooWork is still needed to
call it live-verified.
"""
import asyncio
import json
from datetime import date

import pytest
from zoowork import create_zoowork_client

from app.agent.runner import execute_run
from app.agent.service import AgentRunFailed
from app.agent.tools import TOOL_DECLARATIONS, ToolContext
from app.agent.zoowork_service import (PERSONA_DOCS, ZooWorkAgent, pick_default_model, provision_agent)
from app.data.local_repo import LocalRepository
from app.data.scenario_repo import ScenarioRepository
from app.procurement.market import MockMarketSearch
from app.procurement.requirement import create_run
from tests.fake_zoowork import FakeZooWork

TODAY = date(2026, 10, 3)


@pytest.fixture
def base(tmp_path):
    return LocalRepository(tmp_path / "db.json", today=TODAY)


def new_run(base):
    item_id = next(i["id"] for i in base.list_inventory_items() if i["sku"] == "MOZ-001")
    return create_run(ScenarioRepository(base, lambda: "default"), item_id, TODAY)


def live_agent(fake, **kw):
    kw.setdefault("reconnect_delay_s", 0)
    return ZooWorkAgent("zwp_live_fake", fake.agent_id, transport=fake.transport(), **kw)


def run_live(base, fake, **kw):
    run = new_run(base)
    final = execute_run(base, run["id"], agent=live_agent(fake, **kw), market=MockMarketSearch())
    return run, final, [e["title"] for e in base.list_agent_events(run["id"])]


# ---------- a normal live run ----------
def test_complete_live_run(base):
    fake = FakeZooWork("normal")
    run, final, titles = run_live(base, fake)

    assert final["status"] == "awaiting_approval" and final["agent_mode"] == "live"
    assert final["agent_session_id"] == "ses_1"
    rec = final["recommendation"]
    assert rec["meta"]["agent_mode"] == "live" and rec["recommended_supplier"]["name"] == "Prime Provisions Online"
    assert rec["estimated_total_landed_cost"]["total"] == 416.70
    assert final["recommended_cost"] == 416.70 and final["potential_saving"] == -19.90

    # the session was opened with our task message, with an idempotency key
    create = next(r for r in fake.requests if r["method"] == "POST" and r["path"].endswith("/sessions"))
    assert f"Procurement run {run['id']}" in create["body"]["initial_events"][0]["content"]
    assert create["headers"]["idempotency-key"].startswith(f"procurement-run-{run['id']}-")
    assert "zwp_live_fake" in str(create["headers"])  # the key goes in a header, never in the URL or body
    assert "zwp_live_fake" not in json.dumps(create["body"])

    # every tool call was resolved exactly once, in order, with a JSON content block
    assert [r["name"] for r in fake.resolutions] == [
        "get_procurement_requirement", "get_existing_supplier_options", "search_market_prices",
        "submit_recommendation"]
    for r in fake.resolutions:
        assert r["content"][0]["type"] == "json" and r["resolved_by"] == "procurement-app"
        assert "is_error" not in r  # only sent when something failed
    assert fake.resolutions[0]["content"][0]["value"]["requirement"]["required_quantity"] == 51
    assert fake.nudges == 0

    # nothing but the expected endpoints was called: no way to send anything to a supplier
    paths = {(r["method"], r["path"].split("/call_")[0]) for r in fake.requests}
    assert all("email" not in p and "send" not in p for _, p in paths)
    assert "Procurement Manager" in titles and titles[-1] == "Recommendation ready"


def test_the_agent_cannot_slip_its_own_numbers_into_the_recommendation(base):
    """Totals come from Python's priced options. A submission carrying its own total is refused."""
    fake = FakeZooWork("inject_numbers_then_good")
    _, final, _ = run_live(base, fake)
    submits = [r for r in fake.resolutions if r["name"] == "submit_recommendation"]
    assert submits[0]["is_error"] is True and "Unexpected field" in submits[0]["content"][0]["value"]["error"]
    rec = final["recommendation"]
    assert rec["estimated_total_landed_cost"]["total"] == 416.70 and rec["estimated_total_landed_cost"]["goods"] == 407.70
    assert final["recommended_cost"] == 416.70  # never the $1.00 the agent tried to claim
    assert rec["unit_price"]["price_confirmed"] is False


def test_a_new_attempt_never_reattaches_to_an_old_session(tmp_path):
    """Run ids restart after a database reset. The session key must still be different each attempt."""
    keys = []
    for n in range(2):
        base = LocalRepository(tmp_path / f"db{n}.json", today=TODAY)  # a fresh database: run id 1 both times
        fake = FakeZooWork("normal")
        assert new_run(base)["id"] == 1
        run_live_with_run(base, fake, 1)
        keys.append(next(r["headers"]["idempotency-key"] for r in fake.requests if r["path"].endswith("/sessions")))
    assert keys[0] != keys[1]


def run_live_with_run(base, fake, run_id):
    return execute_run(base, run_id, agent=live_agent(fake), market=MockMarketSearch())


# ---------- the agent misbehaves ----------
def test_a_bad_option_id_is_rejected_and_the_agent_corrects_itself(base):
    fake = FakeZooWork("bad_option_then_good")
    run, final, titles = run_live(base, fake)
    submits = [r for r in fake.resolutions if r["name"] == "submit_recommendation"]
    assert len(submits) == 2
    assert submits[0]["is_error"] is True and "Unknown option_id" in submits[0]["content"][0]["value"]["error"]
    assert "is_error" not in submits[1]
    assert final["agent_mode"] == "live" and final["status"] == "awaiting_approval"
    assert any(e["tone"] == "error" and "Rejected" in e["detail"] for e in base.list_agent_events(run["id"]))


def test_an_agent_that_forgets_to_submit_is_nudged_once(base):
    fake = FakeZooWork("no_submit_then_nudge")
    _, final, titles = run_live(base, fake)
    assert fake.nudges == 1
    nudge = [r for r in fake.requests if r["path"].endswith("/events") and r["method"] == "POST"][0]
    assert "submit_recommendation" in nudge["body"]["events"][0]["content"]
    assert final["agent_mode"] == "live" and final["status"] == "awaiting_approval"
    assert "Prompting the agent to submit" in titles


def test_an_agent_that_never_submits_falls_back(base):
    fake = FakeZooWork("never_submit")
    run, final, titles = run_live(base, fake)
    assert fake.nudges == 1
    assert final["agent_mode"] == "fallback" and final["status"] == "awaiting_approval"
    assert "Live agent failed" in titles and final["recommendation"]["recommended_supplier"]["name"]
    failure = next(e for e in base.list_agent_events(run["id"]) if e["title"] == "Live agent failed")
    assert "without submitting" in failure["detail"]


def test_a_failed_run_falls_back(base):
    run, final, _ = run_live(base, FakeZooWork("run_failed"))
    assert final["agent_mode"] == "fallback" and final["status"] == "awaiting_approval"
    failure = next(e for e in base.list_agent_events(run["id"]) if e["title"] == "Live agent failed")
    assert "insufficient_credits" in failure["detail"]  # ZooWork's own reason reaches the timeline


def test_without_fallback_the_failure_names_zooworks_reason(base, monkeypatch):
    monkeypatch.setenv("AGENT_FALLBACK", "0")
    _, final, _ = run_live(base, FakeZooWork("run_failed"))
    assert final["status"] == "failed" and "insufficient_credits" in final["error"]


# ---------- the network misbehaves ----------
def test_api_errors_fall_back_to_the_demo_agent(base):
    _, final, titles = run_live(base, FakeZooWork(fail_create_session=True))
    assert final["agent_mode"] == "fallback" and final["status"] == "awaiting_approval"
    assert "Live agent failed" in titles


def test_a_dropped_stream_is_resumed(base):
    fake = FakeZooWork("normal", fail_first_stream=True)
    _, final, _ = run_live(base, fake)
    assert final["agent_mode"] == "live" and final["status"] == "awaiting_approval"
    assert fake.stream_requests > 1


def test_resume_uses_the_cursor_so_calls_are_not_repeated(base):
    fake = FakeZooWork("normal")
    run_live(base, fake)
    cursors = [r["query"].get("cursor") for r in fake.requests if r["path"].endswith("/events/stream")]
    assert cursors[0] is None and all(c and c.startswith("pse1:") for c in cursors[1:])
    assert len(fake.resolutions) == 4


def test_replayed_events_never_resolve_a_call_twice(base):
    fake = FakeZooWork("normal", replay_all=True)  # the server ignores our cursor and replays history
    _, final, _ = run_live(base, fake)
    assert final["agent_mode"] == "live"
    assert [r["call_id"] for r in fake.resolutions] == ["call_1", "call_2", "call_3", "call_4"]


def test_a_silent_agent_times_out_and_falls_back(base):
    fake = FakeZooWork("silent")
    _, final, _ = run_live(base, fake, timeout_s=0.3, reconnect_delay_s=0.01)
    assert final["agent_mode"] == "fallback" and final["status"] == "awaiting_approval"


def test_missing_credentials_fail_clearly_then_fall_back(base):
    ctx = ToolContext(ScenarioRepository(base, lambda: "default"), new_run(base)["id"], MockMarketSearch())
    with pytest.raises(AgentRunFailed, match="ZOOWORK_API_KEY"):
        ZooWorkAgent(None, None).run(ctx)
    run = new_run(base)
    final = execute_run(base, run["id"], agent=ZooWorkAgent(None, None), market=MockMarketSearch())
    assert final["agent_mode"] == "fallback" and final["status"] == "awaiting_approval"


# ---------- provisioning ----------
def test_pick_default_model_skips_unselectable_rows():
    rows = [{"model": "old", "selectable": False, "default_for": ["model"]},
            {"model": "other", "selectable": True, "default_for": ["image"]},
            {"model": "good", "selectable": True, "default_for": ["model"]}]
    assert pick_default_model(rows) == "good"
    with pytest.raises(RuntimeError):
        pick_default_model(rows[:2])


def test_provisioning_creates_and_starts_the_agent():
    fake = FakeZooWork()

    async def go():
        async with create_zoowork_client("zwp_live_fake", transport=fake.transport()) as client:
            return await provision_agent(client)

    assert asyncio.run(go()) == ("agt_fake", "created")
    steps = [(r["method"], r["path"]) for r in fake.requests]
    assert steps[:2] == [("GET", "/models"), ("POST", "/agents")]
    assert ("POST", "/agents/agt_fake/start") in steps and fake.started

    body = next(r for r in fake.requests if (r["method"], r["path"]) == ("POST", "/agents"))
    resource = body["body"]["resource"]
    assert resource["model"] == {"primary": "fake/chat-1"}                    # the selectable default
    assert [d["name"] for d in resource["persona"]["docs"]] == ["IDENTITY.md", "AGENTS.md", "TOOLS.md"]
    assert [t["name"] for t in resource["custom_tools"]] == [t["name"] for t in TOOL_DECLARATIONS]
    assert body["headers"]["idempotency-key"].startswith("procurement-manager-")
    assert resource["persona"]["docs"] == json.loads(json.dumps(PERSONA_DOCS))


def test_provisioning_an_existing_agent_only_updates_it():
    fake = FakeZooWork()
    fake.started = True

    async def go():
        async with create_zoowork_client("zwp_live_fake", transport=fake.transport()) as client:
            return await provision_agent(client, "agt_fake")

    assert asyncio.run(go()) == ("agt_fake", "updated")
    put = next(r for r in fake.requests if r["method"] == "PUT")
    assert set(put["body"]) == {"persona", "custom_tools"}
    assert not any((r["method"], r["path"]) == ("POST", "/agents") for r in fake.requests)
    assert not any(r["path"].endswith("/start") for r in fake.requests)  # already running


def test_the_persona_carries_the_required_rules():
    identity = PERSONA_DOCS[0]["content"]
    for phrase in ("AI Procurement Manager", "minimising total landed purchasing cost",
                   "Never fabricate supplier information", "Never claim a web price is confirmed",
                   "explicit human approval", "Prefer the lowest total viable cost"):
        assert phrase in identity
