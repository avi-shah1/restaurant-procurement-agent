"""Run one procurement run end to end, and never leave the frontend with a broken state.

Which agent runs (AGENT_MODE):
  auto  (default)  live ZooWork agent if ZOOWORK_API_KEY and ZOOWORK_AGENT_ID are set, else the demo agent
  live             always try the live agent (falls back if it fails, unless AGENT_FALLBACK=0)
  mock             always the deterministic demo agent

If the live agent fails for any reason (no network, API error, timeout, invalid output) and
AGENT_FALLBACK is not 0, the deterministic agent produces the recommendation instead, and the run is
marked agent_mode='fallback' with the reason in the timeline.

Run status: pending -> running -> awaiting_approval   (or failed, with the error saved)
"""
from __future__ import annotations

import logging
import os
import threading

from ..data.scenario_repo import ScenarioRepository
from ..data.scenarios import DEFAULT_KEY
from ..procurement.market import get_market_provider
from .mock_service import MockProcurementAgent
from .service import ProcurementAgent
from .tools import ToolContext
from .zoowork_service import ZooWorkAgent

log = logging.getLogger(__name__)


def configured_agent() -> ProcurementAgent:
    mode = os.environ.get("AGENT_MODE", "auto").lower()
    key, agent_id = os.environ.get("ZOOWORK_API_KEY"), os.environ.get("ZOOWORK_AGENT_ID")
    if mode == "mock" or (mode == "auto" and not (key and agent_id)):
        return MockProcurementAgent()
    return ZooWorkAgent(key, agent_id, base_url=os.environ.get("ZOOWORK_BASE_URL") or None)


def fallback_enabled() -> bool:
    return os.environ.get("AGENT_FALLBACK", "1") != "0"


def execute_run(base_repo, run_id: int, agent: ProcurementAgent | None = None, market=None) -> dict:
    """Synchronous. Returns the final run row. Safe to call from a worker thread."""
    run = base_repo.get_procurement_run(run_id)
    if run is None:
        raise KeyError(f"No procurement run {run_id}")
    scenario = (run.get("requirement") or {}).get("scenario") or DEFAULT_KEY
    repo = ScenarioRepository(base_repo, lambda: scenario)  # the run sees the world it was created in
    ctx = ToolContext(repo=repo, run_id=run_id, market=market or get_market_provider())
    agent = agent or configured_agent()

    base_repo.update_procurement_run(run_id, {"status": "running", "error": None})
    ctx.log("Run started", f"Procurement run {run_id}. Agent mode: {agent.mode}.", "info")
    mode, session_id = agent.mode, None
    try:
        ctx.agent_mode = agent.mode
        try:
            result = agent.run(ctx)
            session_id = result.session_id
            if ctx.submitted is None:
                raise RuntimeError("The agent finished without saving a recommendation.")
        except Exception as e:
            if ctx.submitted is not None:  # a recommendation was saved before the failure: keep it
                ctx.log("Agent run ended with an error after submitting", str(e), "info")
                session_id = ctx.session_id
            elif agent.mode == "live" and fallback_enabled():
                log.warning("Live agent failed; using the deterministic fallback: %s", e)
                ctx.log("Live agent failed", str(e), "error")
                ctx.log("Falling back to the deterministic demo agent",
                        "The recommendation below was produced by rules, not by the live agent.", "info")
                session_id = ctx.session_id
                ctx.agent_mode = mode = "fallback"
                ctx.submitted = None
                MockProcurementAgent().run(ctx)
                if ctx.submitted is None:
                    raise RuntimeError("The fallback agent finished without saving a recommendation.")
            else:
                raise
        rec = ctx.submitted
        ctx.log("Recommendation ready", f"{rec['recommended_supplier']['name']}. Pending human approval; "
                                        f"nothing has been sent.", "success")
        return base_repo.update_procurement_run(
            run_id, {"status": "awaiting_approval", "agent_mode": mode, "agent_session_id": session_id,
                     "error": None})
    except Exception as e:
        log.exception("Procurement run %s failed", run_id)
        ctx.log("Run failed", str(e), "error")
        return base_repo.update_procurement_run(
            run_id, {"status": "failed", "agent_mode": mode, "agent_session_id": session_id, "error": str(e)})


def launch_run(base_repo, run_id: int) -> threading.Thread:
    """Run in the background so the HTTP request returns immediately."""
    thread = threading.Thread(target=execute_run, args=(base_repo, run_id), name=f"procurement-run-{run_id}",
                              daemon=True)
    thread.start()
    return thread
