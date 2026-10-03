"""The interface the rest of the app uses to run the Procurement Manager.

Callers depend on ProcurementAgent only, never on the ZooWork SDK. Implementations:
  ZooWorkAgent          live agent through the ZooWork SDK        (app/agent/zoowork_service.py)
  MockProcurementAgent  deterministic stand-in, same tools/flow   (app/agent/mock_service.py)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from .tools import ToolContext


class AgentRunFailed(Exception):
    """The agent run ended without a usable recommendation."""


@dataclass
class AgentRunResult:
    mode: str                  # live | mock
    session_id: str | None = None


class ProcurementAgent(ABC):
    mode: str = "abstract"

    @abstractmethod
    def run(self, ctx: ToolContext) -> AgentRunResult:
        """Drive one procurement run to a saved recommendation (ctx.submitted), or raise."""
