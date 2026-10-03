"""The human decision on a recommendation: approve or reject.

THE BOUNDARY (documented in ARCHITECTURE.md): approving does NOT place a purchase order and does NOT send
anything by itself. It records the decision, moves the run to 'approved', and produces an RFQ *draft*.
Sending is a separate human action (see send.py). The agent has no tool that can do any of this.

ZooWork's own approval policies gate an agent's tool calls. Our agent has no sensitive tool to gate (it cannot
send anything), so this check lives in our application, where the decision is recorded in our own database.

Safety properties, each covered by tests:
  - atomic: the status change is a compare-and-set, so two clicks (or two tabs) cannot both decide a run
  - idempotent: approving an already-approved run returns the same RFQ, it does not create another
  - only a run that is 'awaiting_approval' can be decided; a failed, running or rejected run cannot
  - if the draft cannot be saved after the status change, the status is rolled back (no "approved, no RFQ")
"""
from __future__ import annotations

from datetime import datetime, timezone

from .rfq import build_rfq

DECIDED_BY = "demo user"  # there is no login in the demo; this is a label, not an identity
MAX_NOTE = 500


class DecisionError(Exception):
    """The decision cannot be made. `status` is the HTTP status the API should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _clean_note(note) -> str | None:
    if note is None or note == "":
        return None
    if not isinstance(note, str) or len(note) > MAX_NOTE:
        raise DecisionError(400, f"The note must be text of at most {MAX_NOTE} characters.")
    return note.strip() or None


def _get(repo, run_id: int) -> dict:
    run = repo.get_procurement_run(run_id)
    if run is None:
        raise DecisionError(404, f"No procurement run {run_id}")
    return run


def approve_run(repo, run_id: int, note=None) -> tuple[dict, bool]:
    """Approve the next step (the RFQ draft). Returns (run, newly_approved)."""
    note = _clean_note(note)
    run = _get(repo, run_id)
    if run["status"] == "approved":
        return run, False  # idempotent: a second click is not an error and creates nothing new
    if run["status"] != "awaiting_approval" or not run.get("recommendation"):
        raise DecisionError(409, f"Run {run_id} is '{run['status']}', so it cannot be approved. "
                                 f"Only a run that is awaiting approval can be.")

    supplier_id = run["recommendation"]["recommended_supplier"]["supplier_id"]
    supplier = repo.get_supplier(supplier_id) if supplier_id is not None else None
    rfq = build_rfq(run, supplier, repo.list_market_search_results(run_id))  # pure: nothing has changed yet

    updated = repo.transition_procurement_run(run_id, "awaiting_approval", {
        "status": "approved", "decided_at": _now(), "decided_by": DECIDED_BY, "decision_note": note})
    if updated is None:  # someone decided between our read and our write
        current = _get(repo, run_id)
        if current["status"] == "approved":
            return current, False
        raise DecisionError(409, f"Run {run_id} was already decided ('{current['status']}').")

    try:
        repo.create_rfq_draft(rfq)
    except Exception:
        repo.transition_procurement_run(run_id, "approved", {"status": "awaiting_approval", "decided_at": None,
                                                              "decided_by": None, "decision_note": None})
        raise
    repo.add_agent_event(run_id, "RFQ approved by a person",
                         f"{DECIDED_BY} approved the recommendation" + (f": {note}" if note else "")
                         + ". This is approval of the next step only. No purchase order was placed.", "success")
    repo.add_agent_event(run_id, "RFQ draft created",
                         f"Draft to {rfq['supplier_name']}. Not sent yet — use Send RFQ when ready.", "info")
    return updated, True


def reject_run(repo, run_id: int, reason=None) -> tuple[dict, bool]:
    """Reject the recommendation. Returns (run, newly_rejected)."""
    reason = _clean_note(reason)
    run = _get(repo, run_id)
    if run["status"] == "rejected":
        return run, False
    if run["status"] != "awaiting_approval":
        raise DecisionError(409, f"Run {run_id} is '{run['status']}', so it cannot be rejected. "
                                 f"Only a run that is awaiting approval can be.")
    updated = repo.transition_procurement_run(run_id, "awaiting_approval", {
        "status": "rejected", "decided_at": _now(), "decided_by": DECIDED_BY, "decision_note": reason})
    if updated is None:
        current = _get(repo, run_id)
        if current["status"] == "rejected":
            return current, False
        raise DecisionError(409, f"Run {run_id} was already decided ('{current['status']}').")
    repo.add_agent_event(run_id, "Recommendation rejected by a person",
                         f"{DECIDED_BY} rejected it" + (f": {reason}" if reason else "")
                         + ". No RFQ was created and nothing was sent.", "error")
    return updated, True
