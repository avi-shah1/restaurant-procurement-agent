# Restaurant Procurement Agent

See `ARCHITECTURE.md` for the design.

## Launch (Windows PowerShell)

```powershell
cd C:\Users\avido\Projects\restaurant-procurement-agent
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt    # runtime packages + pytest
copy .env.example .env      # then fill in the keys
python run.py
```

Open http://127.0.0.1:5000 (status page) or http://127.0.0.1:5000/health.
With `FLASK_DEBUG=1`, http://127.0.0.1:5000/debug/data shows the loaded data.

## Data

- **Demo mode (no Supabase):** set `DEMO_MODE=1` in `.env`. Data lives in `data/demo_db.json`;
  rebuild it with `python scripts/seed_data.py --local`.
- **Supabase:** run `supabase/schema.sql` in the SQL editor, put `SUPABASE_URL` and
  `SUPABASE_SERVICE_ROLE_KEY` in `.env`, set `DEMO_MODE=0`, then `python scripts/seed_data.py`
  (add `--reset` to wipe and reseed).
- Tests: `python -m pytest`

## Procurement agent

All prices are US dollars (demo numbers).

- `python scripts/run_procurement.py` runs one complete mozzarella procurement run and prints the
  timeline and recommendation. With no ZooWork credentials it uses the deterministic demo agent.
- `POST /api/runs {"sku": "MOZ-001"}` does the same through the API; `GET /api/runs/<id>` returns the
  status, recommendation and timeline events.
- Going live: put `ZOOWORK_API_KEY` in `.env`, run `python scripts/setup_agent.py --write-env` (this
  creates an agent in your ZooWork project), then run again. If the live agent ever fails, the run
  falls back to the demo agent automatically (`AGENT_FALLBACK=0` to disable).
- A recommendation awaits human approval. Nothing is sent until a person clicks **Send RFQ**.
- Market search: with `TAVILY_API_KEY` in `.env` the agent searches the live web (about 5 Tavily credits per
  run); without it, seeded demo data is used. Web prices are shown as unconfirmed, and anything a page did
  not state is shown as unknown. If Tavily fails the run continues with existing suppliers and says so.
- Running: the forecast table has a **Run** button on every at-risk row (the top **Run procurement** button runs the most
  urgent item). Rows that already have a run also get **View**, which re-opens that card without a new search.
- Approval: each finished run shows a **Procurement Action Card**. **Approve RFQ** records your decision and
  creates an RFQ *draft* (no order, no email). **Send RFQ** (Phase 7) then delivers the draft — mock/test by
  default (recorded in the DB), or SMTP if `SMTP_HOST` / `RFQ_EMAIL_MODE=smtp` is set. Use
  `TEST_EMAIL_OVERRIDE` so every demo send goes to an inbox you control (clearly labelled TEST/DEMO).
  Optional `RFQ_SIMULATE_REPLY=1` invents a clearly labelled simulated supplier reply for the demo story.
  **Reject** records the rejection. Set `RESTAURANT_NAME` in `.env` to put your restaurant's name on the draft.
- Tests never call real ZooWork or Tavily (blocked in `tests/conftest.py`). Run them with `python -m pytest`.
  The page itself is tested in a simulated browser (jsdom): one-time setup `cd tests/dom && npm install`.
  Those tests are skipped if Node or jsdom is missing. They prove the page's logic, not how it looks.

## Forecast

`python scripts/show_forecast.py` prints the mozzarella forecast and reorder calculation in plain
English (`--sku`, `--all`, `--scenario`, `--event saturday_rush`, `--today` are available).
`GET /api/forecast` returns the same data as JSON.

## Scenarios

`default`, `weekend_rush`, `coffee_spike`, `supplier_delay`, `avocado_shortage`.
With `FLASK_DEBUG=1`, preview what the forecast engine concludes without changing anything:
`/debug/forecast?scenario=supplier_delay&today=2026-10-02`.
Switch the active scenario: `POST /api/scenarios/active` with `{"key": "coffee_spike"}`;
restore the original with `POST /api/scenarios/reset`.


## Deploying to Vercel

`pyproject.toml` points Vercel at `run:app`. On Vercel the app runs in **demo mode** by default and needs no keys.

1. Import the GitHub repo in Vercel (or `npx vercel` after `npx vercel login`).
2. Set environment variables (Project Settings, Environment Variables):
   - `DEMO_PASSWORD` : **set this.** The site is public; without it anyone with the URL can click Run, Approve and Send.
   - `AGENT_MODE=mock` and `MARKET_SEARCH_PROVIDER=mock` : the free, deterministic demo. Do **not** add
     `ZOOWORK_API_KEY` / `TAVILY_API_KEY` to a public site without the password: every Run click would spend real money.
   - `DEMO_MODE=1`.
3. Be aware: Vercel runs each request in a short-lived function. Runs finish inside the request (`run_sync`), and the
   demo database lives in the instance's temp folder. It is **not shared between instances and disappears on a cold
   start**, so a run can occasionally vanish between clicks. For anything that must persist, use Supabase
   (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `DEMO_MODE=0`, and run `supabase/schema.sql` first).
