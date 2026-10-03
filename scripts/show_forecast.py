"""Print the forecast in plain English.

    python scripts/show_forecast.py                          mozzarella, default scenario, today
    python scripts/show_forecast.py --sku OAT-001
    python scripts/show_forecast.py --all                    every item that needs ordering
    python scripts/show_forecast.py --scenario supplier_delay --today 2026-10-02
    python scripts/show_forecast.py --event saturday_rush    switch a demand event on
"""
import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DEMAND_EVENTS  # noqa: E402  (also loads .env)
from app.data import get_base_repo  # noqa: E402
from app.data.scenario_repo import ScenarioRepository  # noqa: E402
from app.data.scenarios import SCENARIOS, DEFAULT_KEY  # noqa: E402
from app.demand_events import active_event_multiplier, set_enabled  # noqa: E402
from app.forecast import forecast_all, format_forecast  # noqa: E402

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
ap.add_argument("--sku", default="MOZ-001")
ap.add_argument("--all", action="store_true", help="show every item that requires procurement")
ap.add_argument("--scenario", default=DEFAULT_KEY, choices=list(SCENARIOS))
ap.add_argument("--event", action="append", default=[], choices=[e["key"] for e in DEMAND_EVENTS])
ap.add_argument("--today", help="YYYY-MM-DD (default: today)")
args = ap.parse_args()

for key in args.event:
    set_enabled(key, True)
today = date.fromisoformat(args.today) if args.today else date.today()
repo = ScenarioRepository(get_base_repo(), lambda: args.scenario)
results = forecast_all(repo, today=today, event_multiplier=active_event_multiplier)

print(f"Scenario: {args.scenario}   Date: {today} ({today:%A})   Events on: {args.event or 'none'}   "
      f"Backend: {repo.backend_name}\n")
chosen = [f for f in results if f["requires_procurement"]] if args.all else \
         [f for f in results if f["item"]["sku"] == args.sku]
if not chosen:
    sys.exit("Nothing to show." if args.all else f"Unknown sku {args.sku!r}")
for f in chosen:
    print(format_forecast(f))
