import os
from datetime import date

from flask import Flask, abort, current_app, jsonify, render_template, request

from .config import env_status
from .data import get_base_repo, get_repo
from .data.scenario_repo import ScenarioRepository
from .data.scenarios import (get_active_key, get_scenario, list_scenarios, reset_scenario,
                             set_active)
from .data.summary import build_data_summary
from .demand_events import (active_event_multiplier, enabled_keys, list_events, reset_events,
                            set_enabled)
from .forecast import forecast_all
from .agent.runner import execute_run, launch_run
from .procurement.decision import DecisionError, approve_run, reject_run
from .procurement.legacy import upgrade_recommendation
from .procurement.requirement import NoProcurementNeeded, create_run
from .procurement.send import SendError, send_rfq
from .procurement.stages import build_stages


def _run_view(base_repo, run_id: int, after_event_id: int = 0) -> dict | None:
    """Everything the dashboard needs about one run, in one stable shape (also for failed runs)."""
    run = base_repo.get_procurement_run(run_id)
    if run is None:
        return None
    events = base_repo.list_agent_events(run_id, after_event_id)
    rfq = base_repo.get_rfq_draft(run_id)
    req = run.get("requirement") or {}
    market_results = [{k: r.get(k) for k in ("id", "supplier_name", "product_name", "raw_price", "normalised_unit_price",
                                            "pack_size", "minimum_order_quantity", "delivery_information",
                                            "source_url", "source_domain", "confidence")}
                      | {"caveats": (r.get("raw_result") or {}).get("caveats", []),
                         "known_fields": (r.get("raw_result") or {}).get("known_fields", []),
                         "unknown_fields": (r.get("raw_result") or {}).get("unknown_fields", [])}
                      for r in base_repo.list_market_search_results(run_id)]
    decided = run["status"] in ("approved", "rejected")
    return {
        "id": run["id"], "status": run["status"], "agent_mode": run.get("agent_mode"),
        "error": run.get("error"), "created_at": run["created_at"],
        "item": req.get("item"), "requirement": req or None,
        "recommendation": upgrade_recommendation(base_repo, run),   # also fills fields older saved runs lack
        "market_search": run.get("market_search"), "market_results": market_results,
        # the stage timeline always uses the run's full history, even when only new events are requested
        "stages": build_stages(run, base_repo.list_agent_events(run_id, 0), rfq=rfq),
        "decision": {"outcome": run["status"] if decided else None, "decided_at": run.get("decided_at"),
                     "decided_by": run.get("decided_by"), "note": run.get("decision_note")},
        "rfq": rfq,
        "events": events, "last_event_id": events[-1]["id"] if events else after_event_id,
    }


def _require_dev() -> None:
    """Development only: 404 unless Flask debug mode (FLASK_DEBUG=1) is on."""
    if not (current_app.debug or os.environ.get("FLASK_DEBUG") == "1"):
        abort(404)


def create_app(run_sync: bool = False) -> Flask:
    """run_sync=True runs procurement runs inline instead of in a background thread (tests)."""
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "dev-only")
    app.config["RUN_SYNC"] = run_sync

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/status")
    def status():
        return jsonify(env=env_status())

    # ---- scenarios (a frontend dropdown / buttons will call these) ----
    @app.get("/api/scenarios")
    def scenarios():
        return jsonify(active=get_active_key(), scenarios=list_scenarios())

    @app.post("/api/scenarios/active")
    def activate_scenario():
        key = (request.get_json(silent=True) or {}).get("key")
        try:
            sc = set_active(key)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(active=sc.key, label=sc.label, description=sc.description)

    @app.post("/api/scenarios/reset")
    def reset_scenario_route():
        sc = reset_scenario()
        return jsonify(active=sc.key, label=sc.label)

    # ---- demand events (toggle switches for the UI) ----
    @app.get("/api/events")
    def events():
        return jsonify(events=list_events())

    @app.post("/api/events/reset")
    def reset_events_route():
        reset_events()
        return jsonify(events=list_events())

    @app.post("/api/events/<key>")
    def toggle_event(key):
        enabled = (request.get_json(silent=True) or {}).get("enabled")
        if not isinstance(enabled, bool):
            return jsonify(error='Send JSON like {"enabled": true}'), 400
        try:
            set_enabled(key, enabled)
        except ValueError as e:
            return jsonify(error=str(e)), 404
        return jsonify(events=list_events())

    # ---- forecast ----
    @app.get("/api/forecast")
    def api_forecast():
        """Forecast for every item under the active scenario and enabled demand events.
        Optional: ?today=YYYY-MM-DD, ?include_projection=0 to drop the day-by-day tables."""
        try:
            today = date.fromisoformat(request.args["today"]) if "today" in request.args else date.today()
        except ValueError as e:
            return jsonify(error=str(e)), 400
        items = forecast_all(get_repo(), today=today, event_multiplier=active_event_multiplier,
                             include_projection=request.args.get("include_projection") != "0")
        return jsonify(scenario=get_active_key(), events_enabled=enabled_keys(),
                       today=today.isoformat(), items=items)

    # ---- procurement runs ----
    @app.post("/api/runs")
    def start_run():
        """Body: {"inventory_item_id": 1} or {"sku": "MOZ-001"}. Starts the agent in the background."""
        body = request.get_json(silent=True) or {}
        base = get_base_repo()
        item_id = body.get("inventory_item_id")
        if item_id is None and body.get("sku"):
            item_id = next((i["id"] for i in base.list_inventory_items() if i["sku"] == body["sku"]), None)
        if not isinstance(item_id, int) or isinstance(item_id, bool):
            return jsonify(error='Send {"inventory_item_id": <int>} or {"sku": "<sku>"}'), 400
        try:
            run = create_run(get_repo(), item_id, today=date.today(), event_multiplier=active_event_multiplier,
                             events_enabled=enabled_keys())
        except KeyError as e:
            return jsonify(error=str(e.args[0])), 404
        except NoProcurementNeeded as e:
            return jsonify(error=str(e)), 409
        if current_app.config["RUN_SYNC"]:
            execute_run(base, run["id"])
        else:
            launch_run(base, run["id"])
        return jsonify(_run_view(base, run["id"])), 202

    @app.get("/api/runs")
    def list_runs():
        rows = get_base_repo().list_procurement_runs()
        return jsonify(runs=[{
            "id": r["id"], "status": r["status"], "agent_mode": r.get("agent_mode"),
            "item": (r.get("requirement") or {}).get("item"), "required_quantity": r["required_quantity"],
            "recommended_cost": r.get("recommended_cost"), "potential_saving": r.get("potential_saving"),
            "created_at": r["created_at"]} for r in rows])

    @app.get("/api/runs/<int:run_id>")
    def get_run(run_id):
        try:
            after = int(request.args.get("after_event_id", 0))
        except ValueError:
            return jsonify(error="after_event_id must be an integer"), 400
        view = _run_view(get_base_repo(), run_id, after)
        return (jsonify(view), 200) if view else (jsonify(error=f"No procurement run {run_id}"), 404)

    # ---- the human decision. Approving creates an RFQ DRAFT. Sending is a separate step. ----
    def _decide(run_id, action, key):
        body = request.get_json(silent=True)
        body = body if isinstance(body, dict) else {}
        base = get_base_repo()
        try:
            _, changed = action(base, run_id, body.get(key))
        except DecisionError as e:
            return jsonify(error=str(e)), e.status
        return jsonify({**_run_view(base, run_id), "changed": changed}), 200

    @app.post("/api/runs/<int:run_id>/approve")
    def approve(run_id):
        """Optional JSON {"note": "..."}. Approves the NEXT STEP (an RFQ draft). No purchase order; does not send."""
        return _decide(run_id, approve_run, "note")

    @app.post("/api/runs/<int:run_id>/reject")
    def reject(run_id):
        """Optional JSON {"reason": "..."}."""
        return _decide(run_id, reject_run, "reason")

    @app.post("/api/runs/<int:run_id>/send")
    def send(run_id):
        """Send the approved RFQ draft (mock by default; SMTP if configured). Never places a purchase order."""
        base = get_base_repo()
        try:
            _, changed = send_rfq(base, run_id)
        except SendError as e:
            return jsonify(error=str(e)), e.status
        return jsonify({**_run_view(base, run_id), "changed": changed}), 200

    @app.get("/api/runs/<int:run_id>/rfq")
    def get_rfq(run_id):
        base = get_base_repo()
        if base.get_procurement_run(run_id) is None:
            return jsonify(error=f"No procurement run {run_id}"), 404
        draft = base.get_rfq_draft(run_id)
        if draft is None:
            return jsonify(error=f"Run {run_id} has no RFQ draft. One is created when a person approves the run."), 404
        return jsonify(draft)

    # ---- development-only inspection ----
    @app.get("/debug/data")
    def debug_data():
        _require_dev()
        return jsonify(build_data_summary(get_repo()))

    @app.get("/debug/forecast")
    def debug_forecast():
        """What the forecast engine concludes. Optional ?scenario=KEY (preview, does not change the
        active scenario) and ?today=YYYY-MM-DD (to see how the weekday changes things)."""
        _require_dev()
        key = request.args.get("scenario")
        try:
            if key:
                get_scenario(key)
            today = date.fromisoformat(request.args["today"]) if "today" in request.args else date.today()
        except ValueError as e:
            return jsonify(error=str(e)), 400
        repo = ScenarioRepository(get_base_repo(), lambda: key) if key else get_repo()
        items = forecast_all(repo, today=today, event_multiplier=active_event_multiplier,
                             include_projection=False)
        return jsonify(scenario=repo.scenario_key, today=today.isoformat(), items=items)

    return app
