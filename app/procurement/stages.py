"""The run's stage timeline, derived from stored data (no event stream, no extra infrastructure).

Nine stages, in order. Each is built from what is in the database right now:
  Inventory analysed -> Shortage predicted -> Procurement triggered -> Incumbent checked -> Market search ->
  Supplier options found -> Recommendation created -> Awaiting approval -> RFQ approved (or Rejected)

A stage has a state: done | active | pending | warning | error.
  warning = it finished but not cleanly (e.g. live search unavailable, so existing suppliers only)
  error   = the run failed here
"""
from __future__ import annotations


def _event_time(events: list[dict], title_part: str) -> str | None:
    return next((e["created_at"] for e in events if title_part in e["title"]), None)


def build_stages(run: dict, events: list[dict], rfq: dict | None = None) -> list[dict]:
    req = run.get("requirement") or {}
    rec = run.get("recommendation")
    ms = run.get("market_search")
    status = run["status"]
    running = status in ("pending", "running")
    failed = status == "failed"
    decided = status in ("approved", "rejected")
    unit = (req.get("item") or {}).get("unit", "")
    inc = req.get("incumbent")
    rfq = rfq if rfq is not None else run.get("_rfq")

    def stage(key, label, state, detail="", at=None):
        return {"key": key, "label": label, "state": state, "detail": detail, "at": at}

    out = []
    m = req.get("usage_model") or {}
    out.append(stage("analysed", "Inventory analysed", "done" if req else "pending",
                     f"28 days of usage, weekday pattern, trend x{m.get('trend_multiplier', 1):g}" if req else "",
                     run.get("created_at")))
    out.append(stage("shortage", "Shortage predicted", "done" if req else "pending",
                     (f"Stock {req['current_stock']:g} {unit} vs safety {req['safety_stock']:g}; "
                      f"{'runs out ' + req['predicted_stockout_date'] if req.get('predicted_stockout_date') else 'dips below safety ' + req['predicted_breach_date']}; "
                      f"order {req['required_quantity']:g} {unit}") if req else "", run.get("created_at")))
    out.append(stage("triggered", "Procurement triggered", "done", f"Run {run['id']} started", run.get("created_at")))

    inc_seen = _event_time(events, "Agent: get_existing_supplier_options")
    out.append(stage("incumbent", "Incumbent checked", "done" if inc_seen else "pending",
                     (f"{inc['supplier_name']}: ${inc['estimated_total_landed_cost']:,.2f}, {inc['lead_time_days']}-day lead time"
                      if inc and inc_seen else ""), inc_seen))

    if ms:
        provider = "Tavily" if ms.get("provider") == "tavily" else "Demo market data"
        if ms["status"] == "unavailable":
            out.append(stage("search", f"{provider} search started", "warning",
                             f"Live search unavailable: {ms.get('reason') or 'unknown reason'}", ms.get("searched_at")))
        else:
            kind = "live web" if ms.get("is_live") else "demo data"
            out.append(stage("search", f"{provider} search started", "done",
                             f"{len(ms.get('queries') or [])} queries, {ms.get('sources_searched', 0)} sources ({kind})"
                             + (f"; problems: {ms['reason']}" if ms.get("reason") else ""), ms.get("searched_at")))
        if ms["status"] == "unavailable":
            out.append(stage("options", "Supplier options found", "warning",
                             "No live options. Existing suppliers only.", ms.get("searched_at")))
        else:
            out.append(stage("options", "Supplier options found", "done",
                             f"{ms.get('priced_options', 0)} priced options, {ms.get('unpriced_leads', 0)} pages without a usable price",
                             ms.get("searched_at")))
    else:
        out.append(stage("search", "Market search", "pending"))
        out.append(stage("options", "Supplier options found", "pending"))

    out.append(stage("recommendation", "Recommendation created", "done" if rec else "pending",
                     (f"{rec['recommended_supplier']['name']} at ${rec['estimated_total_landed_cost']['total']:,.2f} landed"
                      if rec else ""), (rec or {}).get("meta", {}).get("generated_at")))

    if decided:
        out.append(stage("awaiting", "Awaiting approval", "done", "A person reviewed the recommendation",
                         (rec or {}).get("meta", {}).get("generated_at")))
    elif status == "awaiting_approval":
        out.append(stage("awaiting", "Awaiting approval", "active", "Waiting for a person to approve or reject"))
    else:
        out.append(stage("awaiting", "Awaiting approval", "pending"))

    if status == "approved":
        if rfq and rfq.get("status") == "sent":
            mode = ((rfq.get("send_record") or {}).get("delivery_mode") or "mock")
            detail = f"RFQ sent via {mode}. No purchase order placed."
            if (rfq.get("send_record") or {}).get("is_test_redirect") or mode == "mock":
                detail += " TEST/DEMO delivery."
            out.append(stage("decision", "RFQ sent", "done", detail, rfq.get("sent_at") or run.get("decided_at")))
        else:
            out.append(stage("decision", "RFQ approved", "done",
                             "RFQ draft ready. Not sent yet. No purchase order placed.",
                             run.get("decided_at")))
    elif status == "rejected":
        out.append(stage("decision", "Rejected by a person", "warning",
                         run.get("decision_note") or "No RFQ was created", run.get("decided_at")))
    else:
        out.append(stage("decision", "RFQ approval", "pending"))

    # the first unfinished stage is "active" while the run is going, or "error" if it failed there
    if running or failed:
        for s in out:
            if s["state"] == "pending":
                s["state"] = "error" if failed else "active"
                if failed:
                    s["detail"] = run.get("error") or "The run failed"
                break
    return out
