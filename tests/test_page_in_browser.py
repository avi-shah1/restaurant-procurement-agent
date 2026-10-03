"""The real page, in a simulated browser (jsdom), against a real running server.

This exists because the card's HTML was tested on its own and passed, while the page's own show/hide logic hid
the whole card (including the Approve button). Here the page's JavaScript runs for real, and a script does what
a person does: look for the Approve button, click it, start a run, reject it, change scenario.

jsdom has no layout or CSS, so this proves the page's logic and DOM state, not how it looks.
Setup: `cd tests/dom && npm install jsdom`. Skipped if Node or jsdom is missing."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from app import create_app
from app.agent import runner
from app.procurement.tavily_market import TavilyMarketSearch
from tests.fake_tavily import FAST, LEAD, SLOW, FakeTavily

HERE = Path(__file__).parent
NODE = shutil.which("node")
JSDOM = HERE / "dom" / "node_modules" / "jsdom"
pytestmark = pytest.mark.skipif(NODE is None or not JSDOM.exists(), reason="Node.js or jsdom (tests/dom) is missing")


@pytest.fixture
def steps():
    """Start the real app on a free port with one finished run waiting for approval, drive the page, collect steps."""
    app = create_app(run_sync=True)
    client = app.test_client()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runner, "get_market_provider",
                   lambda: TavilyMarketSearch(api_key="tvly-test", client=FakeTavily(results=[FAST, SLOW, LEAD])))
        seeded = client.post("/api/runs", json={"sku": "MOZ-001"}).get_json()
    assert seeded["status"] == "awaiting_approval"

    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        done = subprocess.run([NODE, str(HERE / "dom" / "page_check.js"), f"http://127.0.0.1:{server.server_port}"],
                              capture_output=True, encoding="utf-8", timeout=90)
    finally:
        server.shutdown()
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert "fatal" not in out, out.get("fatal")
    out["by"] = {s["label"]: s for s in out["steps"]}
    out["seeded_id"] = seeded["id"]
    return out


def test_the_page_runs_without_any_javascript_errors(steps):
    assert steps["errors"] == []


def test_the_approve_button_is_visible_when_a_run_awaits_approval(steps):
    s = steps["by"]["loaded"]
    assert s["cardPresent"] and s["cardVisible"], "the action card must be shown"
    assert s["approveVisible"] and s["rejectVisible"] and s["approveDisabled"] is False
    assert s["emptyRecommendationVisible"] is False                      # not the "No recommendation yet" placeholder
    assert s["cardStatus"] == "Awaiting approval" and s["panelBadge"] == "Needs approval" and s["panelBadgeVisible"]
    assert s["phase"] == "recommendation_ready"


def test_the_whole_dashboard_loads_with_the_run(steps):
    s = steps["by"]["loaded"]
    assert s["inventoryRows"] == 10 and s["inventoryVisible"] and "Mozzarella" in s["firstRow"]
    assert s["supplierRows"] >= 5 and s["supplierListVisible"] and "Live web search" in s["marketBanner"]
    assert s["stages"] == 9 and s["stagesVisible"] and s["stagesDone"] == 7
    assert s["kpiPending"] == "1" and s["kpiRisks"] == "2" and s["scenarioLabel"] == "Default"
    assert s["timelineItems"] >= 5                                       # the detailed agent log is there too


def test_clicking_approve_records_the_decision_and_shows_the_rfq_draft(steps):
    s = steps["by"]["approved"]
    assert s["cardStatus"] == "RFQ approved" and s["panelBadge"] == "RFQ approved"
    assert s["approveVisible"] is False and s["rejectVisible"] is False  # a decided run cannot be decided again
    assert s["rfqVisible"] and s["rfqBodyStart"].startswith("Hello ")
    assert s["stagesDone"] == 9 and s["kpiPending"] == "0"
    assert any("Draft ready" in t or "Send RFQ" in t or "Nothing was sent" in t for t in s["toasts"])


def test_run_procurement_works_from_the_button_and_shows_a_fresh_card(steps):
    s = steps["by"]["new run"]
    assert s["cardVisible"] and s["approveVisible"] and s["cardStatus"] == "Awaiting approval"
    assert s["phase"] == "recommendation_ready" and s["stages"] == 9 and s["stagesDone"] == 7
    assert s["kpiPending"] == "1"
    assert "Demo market data" in s["marketBanner"]                       # no Tavily key here: seeded demo data, labelled


def test_reject_works_and_leaves_no_rfq(steps):
    s = steps["by"]["rejected"]
    assert s["cardStatus"] == "Rejected" and s["approveVisible"] is False and s["rfqVisible"] is False
    assert s["kpiPending"] == "0" and any("Nothing was sent" in t for t in s["toasts"])


def test_changing_scenario_reloads_the_forecast_and_keeps_the_card(steps):
    s = steps["by"]["scenario"]
    assert s["scenarioLabel"] == "Avocado shortage" and "Avocados" in s["firstRow"]   # avocados are now the top risk
    assert s["inventoryRows"] == 10 and s["inventoryVisible"]
    assert s["cardVisible"]                                                         # the last run's card is still there
    assert steps["by"]["reset"]["scenarioLabel"] == "Default" and "Mozzarella" in steps["by"]["reset"]["firstRow"]


def test_every_at_risk_row_has_a_run_button_and_healthy_rows_have_none(steps):
    for label in ("loaded", "many at risk"):
        s = steps["by"][label]
        assert sorted(s["runButtonSkus"]) == sorted(s["atRiskSkus"]), label
    many = steps["by"]["many at risk"]
    assert {"MOZ-001", "OAT-001", "AVO-001", "TOM-001"} <= set(many["runButtonSkus"])         # supplier_delay: 4+ items at risk
    assert len(many["runButtonSkus"]) == int(many["kpiRisks"]) >= 4


def test_running_a_row_that_is_not_the_most_urgent_shows_that_items_card(steps):
    s = steps["by"]["ran avocados"]
    assert "Avocados" in s["cardItem"] and s["cardStatus"] == "Awaiting approval" and s["approveVisible"]
    assert s["viewingSku"] == "AVO-001" and "AVO-001" in s["viewButtonSkus"]
    assert s["stages"] == 9 and s["stagesDone"] == 7
    assert s["runButtonsDisabled"] is False                                                  # buttons come back after the run


def test_view_reopens_an_earlier_card_without_starting_a_new_run(steps):
    s = steps["by"]["viewed mozzarella"]
    assert "Mozzarella" in s["cardItem"] and s["cardStatus"] == "Rejected" and s["approveVisible"] is False
    assert s["viewingSku"] == "MOZ-001"
    assert len(s["viewButtonSkus"]) == s["viewButtonsBefore"]                                # no new run appeared
