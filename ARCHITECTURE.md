# AI Procurement Agent for Restaurants - Architecture

One-day hackathon build. Python forecasts shortages deterministically, then hands the
requirement to a ZooWork Managed Agent that acts as the AI Procurement Manager. Nothing is
sent to a supplier without a human clicking Approve.

## Golden rule

**Deterministic business logic stays in Python. The agent decides, explains and drafts; it never does the arithmetic.**

| Layer | Owns |
|---|---|
| Python (`app/forecast.py`, `app/suppliers.py`) | demand forecast, projected inventory, safety-stock breaches, required order qty, price normalisation (e.g. per kg) |
| Supabase (Postgres) | inventory, usage history, suppliers, supplier products/prices, procurement runs, market-search results, recommendations, RFQ drafts |
| ZooWork agent | understand the requirement, choose tools, weigh trade-offs, explain the recommendation, draft the RFQ |
| Tavily | live supplier discovery, live prices, source URLs |
| Frontend (Jinja + vanilla JS) | forecast table, opportunity cards, agent rationale, approve/reject, activity timeline |

## Request flow

```
Browser ──click "Run procurement"──> Flask
  Flask: forecast.py -> shortages + required qty  (pure Python, tested)
  Flask: save procurement_run row in Supabase
  Flask (background thread): open ZooWork session, send the requirement as the user message
     agent loop:  agent calls a custom tool  ──> run PAUSES
                  Flask executes the tool in Python (Supabase read / Tavily search / save draft)
                  Flask resolves the call with the result ──> run continues
     every event is written to Supabase (agent_events) for the timeline
  Agent finishes: recommendation + RFQ draft saved with status "pending_approval"
Browser polls /api/runs/<id> ──> renders cards + timeline
Human clicks Approve ──> Flask marks approved + RFQ draft
Human clicks Send RFQ ──> Flask records send (mock/test by default; optional SMTP)
```

## ZooWork integration (verified against installed `zoowork` 0.5.2 source and the skill references)

- The SDK is **async** (`httpx`); Flask is sync. Each agent run executes `asyncio.run(...)`
  inside a worker thread so requests never block.
- Lifecycle, in this order: `list_models` -> pick row with `selectable != false` and
  `"model"` in `default_for` -> `create_agent(resource, idempotency_key=...)` ->
  `start_agent` -> `wait_until_running`. The agent is created **once** by
  `scripts/setup_agent.py` and its id stored in `ZOOWORK_AGENT_ID`. One **session per procurement run**.
- Tools are **application-executed custom tools** declared on the agent
  (`custom_tools`: name, description, `input_schema`). When the agent calls one, the event
  `agent.custom_tool_use` arrives (read with `custom_tool_use(event)`); Flask runs the Python
  function and answers with `resolve_custom_tool_call(agent_id, call_id, content=[{"type":"json","value":...}])`.
- The stream does **not** close at turn end: always break on `is_run_finished(event)` and
  check `run_outcome(event)`. Reply text comes from `assistant_text(event)`.
- Tools (built in Phase 3, see "Procurement agent" below): `get_procurement_requirement`,
  `get_existing_supplier_options`, `search_market_prices` (live Tavily, or seeded demo data without a key),
  `submit_recommendation`. RFQ drafting and sending come in a later phase.
- **Approval gate by design:** the agent has **no tool that sends anything**. Its only
  side effect is saving a recommendation as a draft awaiting approval. Sending will be a
  separate Flask endpoint reachable only from the Approve button. The agent cannot bypass it.
- `ZOOWORK_API_KEY` is read server-side only. The browser never sees it.
- Status of the live path: **offline-tested** (real SDK against a fake HTTP API in
  `tests/fake_zoowork.py`). Not yet **live-verified** against a real ZooWork project.

## File tree

`[x]` exists after Phase 0, `[ ]` is a stub or planned for a later phase.

```
restaurant-procurement-agent/
  ARCHITECTURE.md              [x]
  README.md                    [x] launch instructions
  requirements.txt             [x]
  .env.example                 [x]
  .gitignore                   [x]
  run.py                       [x] starts Flask
  app/
    __init__.py                [x] create_app(), routes: /, /health, /api/status
    config.py                  [x] loads env vars; reports which are set (never values)
    data/                      [x] Phase 1 data layer
      __init__.py              [x] get_repo(): Supabase, or local demo data (DEMO_MODE / no keys)
      repository.py            [x] the interface business logic talks to
      supabase_repo.py         [x] Supabase backend
      local_repo.py            [x] local JSON backend (same interface)
      demo_data.py             [x] deterministic DEMO/SIMULATED dataset
      summary.py               [x] snapshot used by /debug/data
      scenarios.py             [x] scenario definitions (data only) + active-scenario state + reset
      scenario_repo.py         [x] applies the active scenario to any Repository at read time
    forecast.py                [x] Phase 2 engine. Formulas documented at the top of the file
    demand_events.py           [x] on/off state for demand events defined in config.DEMAND_EVENTS
    suppliers.py               [x] price_per_inventory_unit(), plan_order() (packs, MOQ, landed cost)
    money.py                   [x] all money is US dollars; money() formatter
    procurement/               [x] Phase 3: everything numeric about a purchase
      requirement.py           [x] forecast -> requirement snapshot -> procurement_runs row
      options.py               [x] supplier offers -> fully priced, comparable options
      market.py                [x] MarketSearchProvider interface, MarketSearchResult, mock provider
      tavily_market.py         [x] Phase 4: live search + extract, failure handling, plausibility guard
      price_parser.py          [x] Phase 4: price / pack / MOQ / delivery from web text (None if unclear)
      discovery.py             [x] Phase 4: saves good finds as suppliers, source_type tavily_live
      decision.py              [x] Phase 5: approve / reject (atomic, idempotent, rollback). Sends nothing
      rfq.py                   [x] Phase 5: the RFQ DRAFT (fixed template) + the market-evidence rules
      stages.py                [x] Phase 5: the 9-stage run timeline, derived from stored data
      legacy.py                [x] Phase 5: fills new card fields on runs saved by older versions
      mock_market_data.py      [x] seeded alternative-supplier "search results" (invented)
      recommender.py           [x] rule-based choice + assembles the structured recommendation
    agent/                     [x] Phase 3: ZooWork integration
      tools.py                 [x] 4 custom tools: declarations + validated Python handlers
      service.py               [x] ProcurementAgent interface the rest of the app uses
      zoowork_service.py       [x] live agent (only file that touches the SDK) + provisioning
      mock_service.py          [x] deterministic stand-in: same tools, rule-based decision
      runner.py                [x] picks the agent, falls back on failure, saves status/events
    templates/
      base.html                [x]
      index.html               [x] status page now; dashboard later
    static/
      css/style.css            [x]
      js/app.js                [x] fetches /api/status now; dashboard logic later
  scripts/
    setup_agent.py             [x] create (or re-sync) the ZooWork agent; makes real API calls
    run_procurement.py         [x] one complete run from the command line, printed in plain English
    seed_data.py               [x] --local rebuilds demo JSON; default seeds Supabase (--reset wipes first)
  supabase/
    schema.sql                 [x] tables below; run once in the Supabase SQL editor
  tests/
    test_data_layer.py         [x] dataset, both backends, /debug/data
    test_scenarios.py          [x] every scenario on every weekday, reset, scenario routes
    test_forecast.py           [x] hand-calculated forecast / reorder cases, seeded mozzarella, events, API
    test_procurement.py        [x] costing, options, rules, tools, runner, fallback, run API
    test_zoowork_agent.py      [x] live agent + provisioning: real SDK against a fake ZooWork API
    fake_zoowork.py            [x] the fake API, with a scripted model that can misbehave
    test_approval.py           [x] approve/reject, RFQ wording and evidence rules, races, rollback, stages
    test_safety.py             [x] real Tavily / ZooWork cannot be reached from tests
    test_frontend_render.py    [x] render.js under Node: the action card in every state, RFQ, stages, XSS
    conftest.py                [x] tests ignore .env, use a temp database, and BLOCK real paid APIs
```

## Supabase tables

Phase 1 (built): `suppliers`, `inventory_items`, `usage_history`, `supplier_products`,
`procurement_runs`, `market_search_results`.
Phase 3 (built): `agent_events` (the dashboard timeline) and new `procurement_runs` columns:
`requirement` (what Python calculated, as a snapshot), `recommendation` (full structured result),
`agent_mode` (`live` / `mock` / `fallback`), `agent_session_id`, `error`.
Re-running `supabase/schema.sql` upgrades an existing database safely.
Phase 5 (built): `rfq_drafts` (one draft per run: supplier, subject, body, whether market evidence was used,
`status` draft/sent/cancelled, `send_note`) and `procurement_runs.decided_at / decided_by / decision_note`.

All money is **US dollars** (demo numbers).

`DEMO_MODE=1` (or missing Supabase keys) swaps in a local JSON backend with the identical
`Repository` interface, so nothing above the data layer changes.

## Scenarios

A scenario is data only: usage multipliers, stock multipliers, supplier delay days
(`app/data/scenarios.py`). It never states which items are at risk; the forecast engine, which
does not import anything scenario-related, works that out from the altered inputs.

- Applied as a **view** over the repository (`ScenarioRepository`), so the stored seed data is
  never modified and `reset_scenario()` restores the original exactly. Works identically on
  Supabase and the local backend; nothing is written to the database.
- Active scenario is in-process state (back to `default` on server restart; single worker).
- API for a future dropdown: `GET /api/scenarios`, `POST /api/scenarios/active {"key": ...}`,
  `POST /api/scenarios/reset`. Dev only: `GET /debug/forecast?scenario=KEY&today=YYYY-MM-DD`.
- Forecasts depend on the weekday they start on (weekend demand is higher), so tests check
  every scenario across all 7 start days.

## Forecast and reorder engine (`app/forecast.py`)

Full formulas are in the module docstring. In short:
`forecast_usage = weekday_average x trend (recent 7d / previous 7d, capped 0.8-1.25) x event_multiplier`;
`projected_inventory = stock + incoming arrived - cumulative forecast usage`;
`required_quantity = safety_stock - lowest projected stock in the 7 days after an order would land`.
An item "requires procurement" when its order deadline (breach date - lead time) is within 2 days.

- Demand events (e.g. Saturday x1.20) are defined in `config.DEMAND_EVENTS`, off by default.
  UI toggles: `GET /api/events`, `POST /api/events/<key> {"enabled": true}`, `POST /api/events/reset`.
- `GET /api/forecast` returns the full structured result for every item (active scenario + events).
- `python scripts/show_forecast.py --sku MOZ-001` prints the plain-English report.
- The result also carries `lead_time_budget_days` (how fast a supplier must deliver to protect safety
  stock / avoid a stockout): the agent uses this later to filter suppliers.

## Procurement agent (Phase 3)

Flow: `POST /api/runs {"sku": "MOZ-001"}` -> Python forecast -> requirement snapshot saved in
`procurement_runs` -> background thread runs the agent -> the agent calls tools, Python answers ->
`submit_recommendation` saves the draft -> status `awaiting_approval`.

**What does the arithmetic:** Python. `plan_order()` works out packs, MOQ, minimum order value and landed
cost; `options.py` adds lead time, arrival date and `meets_required_by` / `avoids_stockout` flags. The
agent only chooses between options Python already priced, by `option_id`, and writes the reasoning.
`submit_recommendation` refuses unknown option ids and any extra field (so an agent cannot inject a
number), and the saved totals always come from the chosen option.

**Which agent runs** (`AGENT_MODE`): `auto` (default) = live ZooWork agent if `ZOOWORK_API_KEY` and
`ZOOWORK_AGENT_ID` are set, else the deterministic demo agent; `live`; `mock`. If the live agent fails
for any reason and `AGENT_FALLBACK` is not `0`, the demo agent produces the recommendation, the run is
marked `agent_mode=fallback`, and the timeline says why. The frontend always gets the same JSON shape,
including for failed runs (`status: failed`, `error`).

**Recommendation fields:** recommended supplier, quantity, unit price (with `price_confirmed`), estimated
total landed cost, delivery timing, incumbent cost, potential savings (negative = costs more than the
incumbent), reasons, risks/uncertainties, sources (URL + confidence, `confirmed: false` for web results),
proposed next action, backup option.

**Market search (Phase 4, Tavily):** `MARKET_SEARCH_PROVIDER` = `auto` (default: Tavily if
`TAVILY_API_KEY` is set, else seeded demo data), `tavily`, or `mock`.
- 4 focused queries (`client.search`, basic depth, noise domains excluded, no crawling), then ONE
  `client.extract` call for the few most promising pages whose snippet had no usable price.
- `price_parser.py` normalises each result to dollars per inventory unit. Anything the page does not clearly
  state (pack size, MOQ, delivery fee, lead time) is `None`, listed in `unknown_fields`; conflicting sizes,
  price ranges and conditional free shipping are flagged, never resolved by guessing. Every row keeps the
  source text its numbers came from (`raw_result.source_text`).
- A price far from what we pay today (under 0.35x or over 3x the incumbent) is treated as a probable misread:
  kept as a lead, not offered as an option.
- Results are saved in `market_search_results`; good ones also become `suppliers` + `supplier_products` rows
  with `source_type = 'tavily_live'` (never overwriting our own records). The run stores a `market_search`
  summary (status, queries, sources searched, errors).
- If Tavily fails (bad key, limit, timeout, network) the result is `unavailable`/`partial` with a reason, the
  run carries on with existing suppliers, and the recommendation lists it as a risk. Demo data is never
  substituted for a failed live search. A second search in the same run reuses the first (no repeat spend).
- Public web pricing is market intelligence, never a confirmed quote (`price_confirmed` is always false).

**Dashboard:** `app/static/js/render.js` (pure data-to-HTML functions, escaped, http(s) links only, tested
under Node) and `app.js` (DOM + API). Shows sources searched, discovered options with source links,
known vs unknown fields, the recommended option and savings vs incumbent, and a clear banner when live
search was unavailable. Approve / Reject / Send RFQ are wired through the API + UI.

**Provisioning:** `python scripts/setup_agent.py` creates the agent (role in persona docs
`IDENTITY.md` / `AGENTS.md` / `TOOLS.md`, plus the 4 custom tools) or re-syncs it if `ZOOWORK_AGENT_ID` is set.

**Run it:** `python scripts/run_procurement.py` (add `--mode mock|live`).

## Approval and the RFQ draft (Phase 5)

**The boundary (read this first).** Approving a recommendation does **not** place a purchase order and does **not**
send anything by itself. It (1) records a person's decision in our database, (2) moves the run to `approved`, and
(3) creates an **RFQ draft**. Sending is a separate Phase 7 step.

**Why the approval gate is in our app, not ZooWork's approval policy.** ZooWork's approval policies pause an
agent's *tool calls* for a human decision (built-in and MCP tools; for custom tools its own docs say platform
approval "does not replace your application's user authorization or business checks"). Our agent has **no tool that
can send, approve or draft an RFQ**: its four tools only read data, search, and save a recommendation. A tool that
cannot exist cannot be called, which is a stronger guarantee than a tool that asks permission. The sensitive steps
run in `app/procurement/decision.py` and `app/procurement/send.py`, reachable only from human button routes.

## Supplier communication (Phase 7)

**Send only after approve.** `POST /api/runs/<id>/send` marks the draft `sent`, stores a `send_record`
(recipient, subject, body, timestamp, status, procurement_run_id, delivery mode), and writes timeline events.
Default delivery is **mock** (DB record only). Optional **SMTP** when `SMTP_HOST` is set / `RFQ_EMAIL_MODE=smtp`.
`TEST_EMAIL_OVERRIDE` redirects every send to an inbox you control; subject/body are clearly labelled TEST/DEMO.
`RFQ_SIMULATE_REPLY=1` (default) invents a clearly labelled **SIMULATED** supplier reply for the demo story.
No autonomous purchase order is ever created.

**Safety properties (each tested):** the status change is a compare-and-set (`transition_procurement_run`), so
20 simultaneous approvals produce exactly one decision and one RFQ; approving twice is harmless (same RFQ);
only a run that is `awaiting_approval` can be decided; an approved run cannot be rejected and vice versa; if
the draft cannot be saved after the status change, the status is rolled back.

**The RFQ is a fixed template in Python, not AI-written**, so the approval step is instant, works offline, and
cannot invent claims. Market evidence (`rfq.pick_benchmark`) is mentioned only if it is a live web result with
confidence >= 0.70, not retail, not a range, not flagged implausible, from a different supplier, and cheaper than
the recommended price. It is phrased as "publicly listed prices ... not quotes we hold": never as a competitor's
quote, and the source is not named. Otherwise the RFQ mentions no market prices at all.

**Routes:** `POST /api/runs/<id>/approve {"note"}`, `POST /api/runs/<id>/reject {"reason"}`,
`GET /api/runs/<id>/rfq`. The run view also returns `stages`, `decision` and `rfq`. `decided_by` is the fixed
label "demo user": there is no login in the demo.

**The Procurement Action Card** (`render.js: actionCardHtml`) shows: item; required quantity and date; current
supplier (unit price, estimated total, delivery); recommended supplier (unit price, landed cost, delivery, MOQ,
reliability; anything not stated is shown as "not stated" or "not known"); dollar and percent saving (a negative
saving is shown plainly as an extra cost); 2-4 short reasons; risks and unknowns; sources including web pages that
were weighed but not chosen; Approve / Reject. The stage timeline is derived from stored data
(`stages.build_stages`): Inventory analysed, Shortage predicted, Procurement triggered, Incumbent checked, Tavily
search started, Supplier options found, Recommendation created, Awaiting approval, RFQ approved. No event stream.

## Environment variables

| Variable | Needed for | Secret |
|---|---|---|
| `ZOOWORK_API_KEY` | ZooWork SDK (Project key from platform.zoowork.ai, starts `zwp_live_`) | yes |
| `ZOOWORK_BASE_URL` | optional; SDK default already includes `/service/v1` | no |
| `ZOOWORK_AGENT_ID` | id (`agt_...`) printed by `scripts/setup_agent.py` | no |
| `AGENT_MODE` | `auto` (default), `live` or `mock` | no |
| `AGENT_FALLBACK` | `0` turns off the fallback to the demo agent when the live agent fails | no |
| `ZOOWORK_RUN_TIMEOUT_S` | give up on the live agent after this long (default 180) | no |
| `MARKET_SEARCH_PROVIDER` | `auto` (default), `tavily` or `mock` | no |
| `TAVILY_API_KEY` | live supplier search | yes |
| `RESTAURANT_NAME` | sender name in the RFQ draft (default `[Your restaurant name]`) | no |
| `RESTAURANT_LOCATION` | where delivery is needed, used in queries (default `United States`) | no |
| `RESTAURANT_COUNTRY` | Tavily country boost (default `united states`; empty to disable) | no |
| `TAVILY_MAX_RESULTS`, `TAVILY_EXTRACT_MAX_URLS`, `TAVILY_TIMEOUT_S`, `TAVILY_BUDGET_S` | spend and time limits (6, 3, 20, 75) | no |
| `SUPABASE_URL` | database | no |
| `SUPABASE_SERVICE_ROLE_KEY` | server-side DB access (bypasses RLS; backend only) | yes |
| `FLASK_SECRET_KEY` | Flask sessions | yes |
| `FLASK_DEBUG` | `1` for local dev | no |

All secrets live in `.env` (git-ignored) and are read only by Python on the server.

## Explicitly out of scope

React, Next.js, Redis, LangGraph, Docker, microservices, ML forecasting libraries.
Forecast = simple, explainable moving average with a weekday factor.
