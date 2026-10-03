"""The live Procurement Manager, running on ZooWork Managed Agents through the official `zoowork` SDK.

Everything that touches the SDK lives in this file. The calls used (checked against zoowork 0.5.2 and
https://zoowork.ai/docs/build/tools.md, /agents.md, /events.md):

  provisioning (once):  list_models -> create_agent(resource, idempotency_key) -> start_agent
                        -> wait_until_running      (or update_agent(agent_id, sections) to re-sync)
  each procurement run: create_session(agent_id, {"initial_events": [user.message]})
                        stream_events(agent_id, session_id, cursor=...)   (does NOT close at turn end)
                        custom_tool_use(event) -> run the tool in Python
                        resolve_custom_tool_call(agent_id, call_id, content=[{"type":"json",...}])
                        stop on is_run_finished(event), judge by run_outcome(event)

The agent's role lives in persona docs (IDENTITY.md, AGENTS.md, TOOLS.md are the canonical names the
platform reads). The agent has no tool that sends anything.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid

import httpx
from zoowork import (ZooworkError, assistant_text, create_zoowork_client, custom_tool_use,
                     is_run_finished, run_outcome)

from .service import AgentRunFailed, AgentRunResult, ProcurementAgent
from .tools import TOOL_DECLARATIONS, ToolContext, execute_tool

log = logging.getLogger(__name__)

IDENTITY_MD = """\
You are an AI Procurement Manager for a restaurant. Your goal is to prevent stockouts while minimising \
total landed purchasing cost without taking unnecessary supply risk.

Python has already calculated the demand forecast and required quantity. Do not override deterministic \
inventory calculations unless data is clearly inconsistent.

Consider:
- current supplier
- unit price
- required quantity
- delivery deadline
- lead time
- MOQ
- delivery costs
- supplier reliability
- available live-market evidence

Use available tools to gather missing procurement information.

Prefer the lowest total viable cost, not blindly the lowest advertised unit price.

Never fabricate supplier information.

Never claim a web price is confirmed if it is only a search result.

Outbound supplier communication or purchase commitments require explicit human approval. You have no tool \
that sends anything; you only recommend. All money is in US dollars.
"""

AGENTS_MD = """\
# Workflow for every procurement run

1. Call get_procurement_requirement with the procurement_run_id from the task message.
2. Call get_existing_supplier_options with the inventory_item_id from the requirement.
3. Call search_market_prices with the same inventory_item_id. It searches the live web, so it can take a \
little while. Look at live_search.status: if it is "unavailable" the search failed. Do not retry; continue \
with the existing supplier options only and say in risks_and_uncertainties that no live market evidence was \
available.
4. Compare the options. Every option is already priced by Python: use estimated_total_landed_cost, \
lead_time_days, arrives_by, meets_required_by, avoids_stockout, price_confirmed, supplier_reliability and \
price_age_days exactly as given. Do not recalculate or invent numbers.
   - An option is only viable if its lead time is known and it arrives in time (avoids_stockout, and ideally \
meets_required_by).
   - Among viable options prefer the lowest estimated_total_landed_cost, but weigh reliability, price \
confidence and uncertainty. A cheaper but unconfirmed or slower option is not automatically better.
   - A negative saving versus the incumbent is fine if it is the cost of avoiding a stockout. Say so plainly.
5. Call submit_recommendation once, choosing one option_id (and optionally a backup_option_id), with 2-4 SHORT \
reasons (each under 240 characters, shown on a card a person reads before approving), honest risks_and_uncertainties and a proposed_next_action (for example: draft an RFQ to confirm \
price). Then reply with a two-sentence summary.

Rules: option ids must come from the tools. Web search results are never confirmed quotes: they are public \
market intelligence found on third-party pages (check confidence, caveats, source_url, is_retail). If a \
field is null it was not stated by the source; read unknown_fields and say so, do not guess. Prefer options \
whose important fields (price, pack size, lead time, minimum order) are known. unpriced_leads are pages we \
could not price: you may mention them as leads worth a quote request, but you cannot recommend them. Never \
promise or imply that anything has been sent or ordered.
"""

TOOLS_MD = """\
# Tools
- get_procurement_requirement(procurement_run_id): the deterministic forecast and required quantity.
- get_existing_supplier_options(inventory_item_id): suppliers we already know, priced for the requirement.
- search_market_prices(inventory_item_id, optional location): live web search results (unconfirmed), same
  format, with source links, known/unknown fields and unpriced leads. Reports when the search is unavailable.
- submit_recommendation(...): save your chosen option and reasoning as a draft for human approval.
"""

PERSONA_DOCS = [
    {"name": "IDENTITY.md", "content": IDENTITY_MD},
    {"name": "AGENTS.md", "content": AGENTS_MD},
    {"name": "TOOLS.md", "content": TOOLS_MD},
]

AGENT_NAME = "procurement-manager"


def pick_default_model(models: list[dict]) -> str:
    """The selectable row whose default_for includes 'model' (per the ZooWork docs)."""
    for row in models:
        if row.get("selectable") is not False and "model" in (row.get("default_for") or []):
            return row["model"]
    raise RuntimeError("ZooWork returned no selectable default chat model.")


def build_agent_resource(model: str) -> dict:
    return {"name": AGENT_NAME, "model": {"primary": model}, "persona": {"docs": PERSONA_DOCS},
            "custom_tools": TOOL_DECLARATIONS, "labels": {"app": "restaurant-procurement"}}


async def provision_agent(client, agent_id: str | None = None) -> tuple[str, str]:
    """Create (or re-sync) the Procurement Manager and make sure it is running. Returns (id, action).

    With an existing agent id only persona and custom_tools are updated (arrays replace wholesale).
    """
    if agent_id:
        await client.update_agent(agent_id, {"persona": {"docs": PERSONA_DOCS},
                                             "custom_tools": TOOL_DECLARATIONS})
        action = "updated"
    else:
        model = pick_default_model(await client.list_models())
        resource = build_agent_resource(model)
        # Same key + same body returns the first response; a changed body needs a new key.
        digest = hashlib.sha1(json.dumps(resource, sort_keys=True).encode()).hexdigest()[:10]
        created = await client.create_agent(resource, idempotency_key=f"{AGENT_NAME}-{digest}")
        agent_id, action = created["agent_id"], "created"
    agent = await client.get_agent(agent_id)
    if (agent.get("status") or {}).get("desired_state") != "running":
        await client.start_agent(agent_id)
        await client.wait_until_running(agent_id)
    return agent_id, action


class ZooWorkAgent(ProcurementAgent):
    mode = "live"

    def __init__(self, api_key: str | None, agent_id: str | None, base_url: str | None = None,
                 timeout_s: float | None = None, reconnect_delay_s: float = 1.0, max_reconnects: int = 8,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.api_key, self.agent_id, self.base_url = api_key, agent_id, base_url
        self.timeout_s = timeout_s if timeout_s is not None else float(os.environ.get("ZOOWORK_RUN_TIMEOUT_S", 180))
        self.reconnect_delay_s, self.max_reconnects, self.transport = reconnect_delay_s, max_reconnects, transport

    # sync entry point: runs happen in a worker thread, so a private event loop is fine
    def run(self, ctx: ToolContext) -> AgentRunResult:
        if not self.api_key or not self.agent_id:
            raise AgentRunFailed("ZOOWORK_API_KEY and ZOOWORK_AGENT_ID must both be set for a live run.")
        return asyncio.run(self._run(ctx))

    async def _run(self, ctx: ToolContext) -> AgentRunResult:
        prompt = (f"Procurement run {ctx.run_id} needs a recommendation. Follow your workflow: call "
                  f"get_procurement_requirement with procurement_run_id={ctx.run_id}, then "
                  f"get_existing_supplier_options, then search_market_prices, then submit_recommendation. "
                  f"Do not contact or commit to any supplier; recommend only.")
        # Unique per attempt: ZooWork replays the first response for a repeated key, so a key built only
        # from the run id would silently re-attach to an OLD session whenever run ids restart (for
        # example after the demo database is reset). Created once per attempt, so it still guards
        # against a double-submit within one attempt.
        attempt = uuid.uuid4().hex[:10]
        async with create_zoowork_client(self.api_key, base_url=self.base_url, transport=self.transport) as client:
            session = await client.create_session(
                self.agent_id, {"initial_events": [{"type": "user.message", "content": prompt}]},
                idempotency_key=f"procurement-run-{ctx.run_id}-{attempt}")
            session_id = session["session_id"]
            ctx.session_id = session_id
            ctx.log("ZooWork session started", f"Session {session_id}", "info")
            try:
                await asyncio.wait_for(self._drive(client, ctx, session_id), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                raise AgentRunFailed(f"Live agent did not finish within {self.timeout_s:g}s") from None
        return AgentRunResult(mode=self.mode, session_id=session_id)

    async def _drive(self, client, ctx: ToolContext, session_id: str) -> None:
        cursor: str | None = None
        handled: set[str] = set()
        nudged = False
        failures = 0
        last_agent_error: str | None = None  # ZooWork's own explanation, for the failure message
        while True:
            restart = False
            try:
                stream = client.stream_events(self.agent_id, session_id, cursor=cursor)
                async with contextlib.aclosing(stream):
                    async for event in stream:
                        failures = 0
                        cursor = event.cursor or cursor
                        call = custom_tool_use(event)
                        if call is not None:
                            if call.phase == "requested" and call.call_id and call.call_id not in handled:
                                handled.add(call.call_id)
                                await self._resolve(client, ctx, call)
                            continue
                        if event.event_type == "agent.error":
                            last_agent_error = str(event.payload.get("errorMessage", "unknown"))
                            ctx.log("ZooWork agent error", last_agent_error, "error")
                        text = assistant_text(event).strip()
                        if text:
                            ctx.log("Procurement Manager", text[:600], "agent")
                        if is_run_finished(event):
                            outcome = run_outcome(event)
                            if ctx.submitted is not None:
                                return  # we have what we came for, whatever the run's own verdict
                            if outcome != "succeeded":
                                raise AgentRunFailed(
                                    f"The live agent run {outcome} without a recommendation."
                                    + (f" ZooWork said: {last_agent_error}" if last_agent_error else ""))
                            if nudged:
                                raise AgentRunFailed("The live agent finished without submitting a recommendation.")
                            nudged = True
                            ctx.log("Prompting the agent to submit", "It finished without calling submit_recommendation.",
                                    "info")
                            await client.post_events(self.agent_id, session_id, [{
                                "type": "user.message", "idempotency_key": f"{session_id}-nudge",
                                "content": "You have not called submit_recommendation. Choose one option_id from "
                                           "the tool results and call it now."}])
                            restart = True
                            break
            except ZooworkError as e:
                if 400 <= e.status < 500:
                    raise AgentRunFailed(f"ZooWork rejected the request ({e.status} {e.type or ''}): {e}") from e
                failures += 1
            except (httpx.HTTPError, OSError):
                failures += 1
            if restart:
                continue
            # The stream ended (idle close or network error) without a verdict: resume from the cursor.
            if failures > self.max_reconnects:
                raise AgentRunFailed("Lost the connection to ZooWork and could not reconnect.")
            await asyncio.sleep(self.reconnect_delay_s)

    async def _resolve(self, client, ctx: ToolContext, call) -> None:
        result, is_error = await asyncio.to_thread(execute_tool, ctx, call.name or "", call.input or {})
        await client.resolve_custom_tool_call(
            self.agent_id, call.call_id, content=[{"type": "json", "value": result}],
            is_error=True if is_error else None, resolved_by="procurement-app")
