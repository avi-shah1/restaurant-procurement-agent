"""The test suite must never be able to spend real money. See conftest.py.

Background: an earlier version of a frontend test undid the suite's safety setup and, without anyone
noticing, started real ZooWork agent runs and real Tavily searches (15 billable runs). These tests make
sure that can never happen silently again."""
import pytest
import tavily

from app.agent.service import AgentRunFailed
from app.agent.tools import ToolContext
import app.agent.zoowork_service as zoowork_service
from app.agent.zoowork_service import ZooWorkAgent
from app.data.local_repo import LocalRepository
from app.data.scenario_repo import ScenarioRepository
from app.procurement.market import MockMarketSearch
from app.procurement.tavily_market import TavilyMarketSearch


def test_the_real_tavily_client_cannot_be_created_in_tests(real_api_attempts):
    with pytest.raises(RuntimeError, match="BLOCKED"):
        tavily.TavilyClient(api_key="tvly-anything")
    assert real_api_attempts == ["tavily.TavilyClient"]
    real_api_attempts.clear()  # this test provoked it on purpose


def test_the_real_zoowork_api_cannot_be_reached_in_tests(real_api_attempts):
    with pytest.raises(RuntimeError, match="BLOCKED"):
        zoowork_service.create_zoowork_client("zwp_live_anything")  # via the module: the guard replaced it there
    assert real_api_attempts == ["zoowork.create_zoowork_client"]
    real_api_attempts.clear()


def test_a_provider_that_forgets_to_inject_a_fake_is_caught_even_though_it_never_raises(real_api_attempts, tmp_path):
    """TavilyMarketSearch swallows errors on purpose (so a run survives a Tavily outage), which would hide a
    test accidentally using the real client. The recorded attempt is what fails the test at teardown."""
    base = LocalRepository(tmp_path / "db.json")
    item = base.list_inventory_items()[0]
    result = TavilyMarketSearch(api_key="tvly-real-looking").search(item, {})
    assert result.status == "unavailable" and "BLOCKED" in result.reason
    assert real_api_attempts == ["tavily.TavilyClient"]
    real_api_attempts.clear()


def test_the_live_agent_without_a_fake_transport_is_caught(real_api_attempts, tmp_path):
    base = LocalRepository(tmp_path / "db.json")
    ctx = ToolContext(ScenarioRepository(base), 1, MockMarketSearch())
    with pytest.raises(RuntimeError, match="BLOCKED"):
        ZooWorkAgent("zwp_live_x", "agt_x").run(ctx)
    assert "zoowork.create_zoowork_client" in real_api_attempts
    real_api_attempts.clear()


def test_the_real_keys_are_not_visible_to_tests():
    import os
    for name in ("ZOOWORK_API_KEY", "ZOOWORK_AGENT_ID", "TAVILY_API_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        assert not os.environ.get(name), name
    with pytest.raises(AgentRunFailed, match="ZOOWORK_API_KEY"):
        ZooWorkAgent(None, None).run(None)
