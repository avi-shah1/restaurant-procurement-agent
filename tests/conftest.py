import pytest
import tavily

import app.agent.zoowork_service as zoowork_service
import app.data.local_repo as local_repo
from app.data import reset_repo
from app.data.scenarios import reset_scenario
from app.demand_events import reset_events


@pytest.fixture
def real_api_attempts():
    """Every attempt a test made to reach a REAL billable API (Tavily / ZooWork). Always empty if tests behave."""
    return []


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch, tmp_path, real_api_attempts):
    """Tests must never depend on, or touch, the developer's .env, demo database, or paid APIs.

    1. app.config loads .env at import time, so without this a real Supabase key would send route tests to the
       real database, FLASK_DEBUG=1 would change what routes return, and runs saved by tests would pollute
       data/demo_db.json.
    2. The real Tavily and ZooWork APIs are BLOCKED. Tests inject fake clients (tests/fake_tavily.py, a
       transport for tests/fake_zoowork.py). A test that reaches for the real thing is recorded here and fails
       at teardown, even if the app swallowed the error, because those calls cost real money.
    """
    for name in ("FLASK_DEBUG", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "ZOOWORK_API_KEY",
                 "ZOOWORK_AGENT_ID", "ZOOWORK_BASE_URL", "AGENT_MODE", "AGENT_FALLBACK",
                 "MARKET_SEARCH_PROVIDER", "TAVILY_API_KEY",
                 "TEST_EMAIL_OVERRIDE", "RFQ_EMAIL_MODE", "RFQ_SIMULATE_REPLY",
                 "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr(local_repo, "DEFAULT_PATH", tmp_path / "demo_db.json")

    class BlockedTavilyClient:
        def __init__(self, *args, **kwargs):
            real_api_attempts.append("tavily.TavilyClient")
            raise RuntimeError("BLOCKED: a test tried to create a real TavilyClient (billable). Inject a fake client.")

    monkeypatch.setattr(tavily, "TavilyClient", BlockedTavilyClient)

    real_create = zoowork_service.create_zoowork_client

    def guarded_create(api_key=None, *, base_url=None, timeout=30.0, transport=None):
        if transport is None:
            real_api_attempts.append("zoowork.create_zoowork_client")
            raise RuntimeError("BLOCKED: a test tried to reach the real ZooWork API (billable). Pass a fake transport.")
        return real_create(api_key, base_url=base_url, timeout=timeout, transport=transport)

    monkeypatch.setattr(zoowork_service, "create_zoowork_client", guarded_create)

    reset_repo()
    reset_scenario()   # the active scenario and demand events live in the server process: never let one test leak into the next
    reset_events()
    yield
    reset_repo()
    reset_scenario()
    reset_events()
    assert not real_api_attempts, f"Test tried to reach a real paid API: {real_api_attempts}"
