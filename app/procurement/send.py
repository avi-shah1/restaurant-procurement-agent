"""Phase 7: send an approved RFQ draft.

THE BOUNDARY:
  - Approving still only creates a draft (see decision.py).
  - Sending is a separate human action: POST /api/runs/<id>/send.
  - The ZooWork agent has no send tool and cannot reach this code.
  - Nothing here places a purchase order.

Delivery modes (chosen at send time from env):
  - mock (default): record the outbound message in the database; no network call.
  - smtp: if SMTP_HOST is set, deliver via stdlib smtplib (credentials stay server-side).

TEST_EMAIL_OVERRIDE redirects every RFQ to an address we control. Test/demo emails are
clearly labelled in subject and body. Prefer this for the hackathon demo.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from email.message import EmailMessage

TEST_SUBJECT_PREFIX = "[TEST/DEMO — not a real supplier send] "
TEST_BODY_BANNER = (
    "=== TEST / DEMO EMAIL ===\n"
    "This message was redirected by TEST_EMAIL_OVERRIDE.\n"
    "It was NOT delivered to the real supplier address below.\n"
    "Intended supplier recipient: {intended}\n"
    "=========================\n\n"
)

_EMAIL_RE = re.compile(r"^[^\s@<>\"'()]+@[^\s@<>\"'()]+\.[^\s@<>\"'()]+$")


class SendError(Exception):
    """The RFQ cannot be sent. `status` is the HTTP status the API should answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _truthy(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def delivery_mode() -> str:
    """mock unless SMTP_HOST is configured (and RFQ_EMAIL_MODE is not forced to mock)."""
    forced = (os.environ.get("RFQ_EMAIL_MODE") or "").strip().lower()
    if forced in ("mock", "smtp"):
        return forced
    return "smtp" if (os.environ.get("SMTP_HOST") or "").strip() else "mock"


def resolve_recipient(draft: dict) -> tuple[str, str | None, bool]:
    """Return (actual_recipient, intended_supplier_email, is_test_redirect)."""
    intended = draft.get("to_email") or None
    override = (os.environ.get("TEST_EMAIL_OVERRIDE") or "").strip()
    if override:
        if not _EMAIL_RE.match(override):
            raise SendError(400, "TEST_EMAIL_OVERRIDE is set but is not a valid email address.")
        return override, intended, True
    if intended and _EMAIL_RE.match(intended):
        return intended, intended, False
    raise SendError(
        400,
        "No recipient address. Set TEST_EMAIL_OVERRIDE for the demo, or ensure the supplier has a contact_email.",
    )


def prepare_outbound(draft: dict, recipient: str, intended: str | None, is_test: bool) -> tuple[str, str]:
    """Subject/body as they will leave the system (with test labels when redirected)."""
    subject = draft["subject"]
    body = draft["body"]
    if is_test:
        subject = TEST_SUBJECT_PREFIX + subject
        body = TEST_BODY_BANNER.format(intended=intended or "(none on file)") + body
    return subject, body


def _deliver_mock(recipient: str, subject: str, body: str) -> dict:
    return {
        "ok": True,
        "mode": "mock",
        "provider": "mock",
        "detail": "Recorded only. No SMTP/API call was made.",
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }


def _deliver_smtp(recipient: str, subject: str, body: str) -> dict:
    host = (os.environ.get("SMTP_HOST") or "").strip()
    if not host:
        raise SendError(503, "RFQ_EMAIL_MODE=smtp but SMTP_HOST is not set.")
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = (os.environ.get("SMTP_USER") or "").strip() or None
    password = os.environ.get("SMTP_PASSWORD") or None
    mail_from = (os.environ.get("SMTP_FROM") or user or "").strip()
    if not mail_from:
        raise SendError(503, "SMTP_FROM (or SMTP_USER) is required to send mail.")
    # Lazy import so the default mock path never needs the SMTP stack at import time.
    import smtplib

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = recipient
    msg.set_content(body)
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.ehlo()
        if _truthy("SMTP_STARTTLS", "1"):
            smtp.starttls()
            smtp.ehlo()
        if user:
            smtp.login(user, password or "")
        smtp.send_message(msg)
    return {
        "ok": True,
        "mode": "smtp",
        "provider": "smtp",
        "detail": f"Delivered via SMTP {host}:{port}",
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "from": mail_from,
    }


def deliver(recipient: str, subject: str, body: str) -> dict:
    mode = delivery_mode()
    if mode == "smtp":
        try:
            return _deliver_smtp(recipient, subject, body)
        except SendError:
            raise
        except Exception as e:
            raise SendError(502, f"SMTP delivery failed: {e}") from e
    return _deliver_mock(recipient, subject, body)


def build_simulated_reply(run: dict, draft: dict, send_record: dict) -> dict:
    """A clearly labelled fake supplier reply for the demo story. Never presented as real."""
    rec = run["recommendation"]
    req = run["requirement"]
    unit = req["item"]["unit"]
    product = req["item"]["name"]
    qty = rec["quantity"]["required"]
    # Slightly under the cheaper of incumbent vs recommendation so the demo always shows a win.
    packs = max(1, int(rec.get("moq", {}).get("packs") or 1))
    incumbent_total = float(rec["incumbent_cost"]["estimated_total_landed_cost"])
    recommended_total = float(rec["estimated_total_landed_cost"]["total"])
    total = round(min(incumbent_total, recommended_total) * 0.96, 2)
    offered = round(total / qty, 2) if qty else round(float(rec["unit_price"]["per_unit"]) * 0.96, 2)
    savings = round(incumbent_total - total, 2)
    body = (
        f"[SIMULATED SUPPLIER REPLY — not a real email]\n\n"
        f"Hello,\n\n"
        f"Thanks for your RFQ regarding {qty:g} {unit} of {product}.\n"
        f"We can offer ${offered:.2f} per {unit} (delivered estimate ${total:.2f} for {qty:g} {unit}), "
        f"MOQ {packs} pack(s), earliest delivery in "
        f"{int(rec['delivery_timing'].get('lead_time_days') or 3)} day(s).\n\n"
        f"This is a simulated reply generated by the demo app after a test send. "
        f"It is not from {draft['supplier_name']}.\n\n"
        f"Regards,\n"
        f"[Simulated] {draft['supplier_name']} sales desk"
    )
    return {
        "simulated": True,
        "label": "SIMULATED supplier reply",
        "from_name": f"[Simulated] {draft['supplier_name']}",
        "received_at": _now(),
        "offered_unit_price": offered,
        "offered_total": total,
        "unit": unit,
        "quantity": qty,
        "savings_vs_incumbent": savings,
        "body": body,
        "in_reply_to_recipient": send_record["recipient"],
    }


def send_rfq(repo, run_id: int) -> tuple[dict, bool]:
    """Send the approved RFQ draft. Returns (draft, newly_sent)."""
    run = repo.get_procurement_run(run_id)
    if run is None:
        raise SendError(404, f"No procurement run {run_id}")
    if run["status"] != "approved":
        raise SendError(
            409,
            f"Run {run_id} is '{run['status']}'. Only an approved run's RFQ draft can be sent.",
        )
    draft = repo.get_rfq_draft(run_id)
    if draft is None:
        raise SendError(404, f"Run {run_id} has no RFQ draft. Approve the recommendation first.")
    if draft["status"] == "sent":
        return draft, False  # idempotent
    if draft["status"] != "draft":
        raise SendError(409, f"RFQ draft is '{draft['status']}', so it cannot be sent.")

    recipient, intended, is_test = resolve_recipient(draft)
    subject, body = prepare_outbound(draft, recipient, intended, is_test)
    delivery = deliver(recipient, subject, body)
    ts = _now()

    send_record = {
        "procurement_run_id": run_id,
        "recipient": recipient,
        "intended_recipient": intended,
        "subject": subject,
        "body": body,
        "timestamp": ts,
        "status": "sent",
        "delivery_mode": delivery["mode"],
        "delivery_detail": delivery["detail"],
        "is_test_redirect": is_test,
        "label": "TEST/DEMO" if is_test or delivery["mode"] == "mock" else "live",
    }

    reply = None
    if _truthy("RFQ_SIMULATE_REPLY", "1"):
        reply = build_simulated_reply(run, draft, send_record)
        send_record["supplier_reply"] = reply

    note_bits = [
        f"Sent at {ts} via {delivery['mode']}.",
        f"Recipient: {recipient}.",
    ]
    if is_test:
        note_bits.append(f"TEST redirect (intended supplier: {intended or 'none on file'}).")
    if delivery["mode"] == "mock":
        note_bits.append("Mock delivery: message recorded in the database only.")
    if reply:
        note_bits.append("A SIMULATED supplier reply was generated for the demo.")

    updated = repo.transition_rfq_draft(run_id, "draft", {
        "status": "sent",
        "sent_at": ts,
        "send_note": " ".join(note_bits),
        "send_record": send_record,
        "supplier_reply": reply,
    })
    if updated is None:
        current = repo.get_rfq_draft(run_id)
        if current and current["status"] == "sent":
            return current, False
        raise SendError(409, f"RFQ for run {run_id} could not be marked sent (status changed).")

    repo.add_agent_event(
        run_id,
        "RFQ sent" + (" (TEST/DEMO)" if is_test or delivery["mode"] == "mock" else ""),
        f"To {recipient}. Mode={delivery['mode']}. "
        + ("Redirected by TEST_EMAIL_OVERRIDE. " if is_test else "")
        + "No purchase order was placed.",
        "success",
        payload={"send_record": {k: v for k, v in send_record.items() if k != "body"}},
    )
    if reply:
        repo.add_agent_event(
            run_id,
            "SIMULATED supplier reply",
            f"Offered ${reply['offered_unit_price']:.2f}/{reply['unit']} "
            f"(~${reply['offered_total']:.2f} total). "
            f"Savings vs incumbent ≈ ${reply['savings_vs_incumbent']:.2f}. "
            f"This reply is simulated — not a real supplier email.",
            "info",
            payload={"supplier_reply": reply},
        )
    return updated, True
