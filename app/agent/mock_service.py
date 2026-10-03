"""Deterministic stand-in for the ZooWork agent (DEMO mode and fallback).

It goes through the same steps and the same tools as the live agent, in the same order:
requirement -> existing suppliers -> market search -> compare -> submit. The "comparing" is the
rule set in procurement/recommender.py, so the same inputs always give the same recommendation.
"""
from __future__ import annotations

from ..procurement.recommender import choose, draft_reasoning
from .service import AgentRunFailed, AgentRunResult, ProcurementAgent
from .tools import ToolContext, execute_tool


class MockProcurementAgent(ProcurementAgent):
    mode = "mock"

    def run(self, ctx: ToolContext) -> AgentRunResult:
        ctx.log("Procurement Manager (demo mode)",
                "Using the deterministic demo agent: same tools, rule-based decision.", "info")
        req_result = self._call(ctx, "get_procurement_requirement", {"procurement_run_id": ctx.run_id})
        item_id = req_result["requirement"]["item"]["id"]
        existing = self._call(ctx, "get_existing_supplier_options", {"inventory_item_id": item_id})
        market = self._call(ctx, "search_market_prices", {"inventory_item_id": item_id})

        requirement = req_result["requirement"]
        options = [*existing["options"], *market["options"]]
        choice = choose(requirement, options)
        if choice["chosen"] is None:
            raise AgentRunFailed(f"No supplier option can deliver in time ({choice['tier']}).")
        ctx.log("Agent (demo): compared options",
                f"{len(options)} options; {len(choice['viable'])} viable. Chose {choice['chosen']['supplier_name']}.",
                "agent")

        reasons, risks, action = draft_reasoning(requirement, choice)
        args = {"recommended_option_id": choice["chosen"]["option_id"], "reasons": reasons,
                "risks_and_uncertainties": risks, "proposed_next_action": action}
        if choice["backup"]:
            args["backup_option_id"] = choice["backup"]["option_id"]
        self._call(ctx, "submit_recommendation", args)
        return AgentRunResult(mode=self.mode)

    @staticmethod
    def _call(ctx: ToolContext, name: str, args: dict) -> dict:
        result, is_error = execute_tool(ctx, name, args)
        if is_error:
            raise AgentRunFailed(f"{name} failed: {result.get('error')}")
        return result
