"""Deterministic demand forecast and reorder engine. Pure Python: no I/O, no randomness, no LLM, no ML.

It knows nothing about scenarios or where demand events come from. Scenarios alter the INPUT data;
events arrive as a plain function `day -> multiplier`. The engine works out the risks itself.

THE MODEL (every number below can be reproduced by hand)
--------------------------------------------------------
1. History: the latest 28 days of usage_history.
2. weekday_average[w]   = mean usage on weekday w over those 28 days (4 samples each).
3. recent_7_day_average = mean of the last 7 days.   previous_7_day_average = mean of the 7 before.
   raw_trend    = recent_7_day_average / previous_7_day_average
   trend        = raw_trend clamped to [0.8, 1.25]   (so noisy data cannot create absurd forecasts)
4. forecast_usage(day)  = weekday_average[weekday(day)] x trend x event_multiplier(day)
5. projected_inventory(day) = current_stock
                            + incoming_quantity if it has arrived by that day (arrives at start of day)
                            - cumulative forecast_usage up to and including that day
   (it can go negative: the negative part is the unmet demand / shortfall).
6. predicted_breach_date   = first day projected_inventory < safety_stock
   predicted_stockout_date = first day projected_inventory <= 0
   days_of_cover           = days until stock runs out (fractional), None if not within the horizon
7. Order deadline: order_by_date = breach date - lead_time_days (latest order that still arrives in time).
   Status:  critical    stockout happens before the earliest possible delivery (today + lead time)
            at_risk     safety breach happens before the earliest possible delivery
            approaching order deadline is within REVIEW_DAYS (2) days
            healthy     otherwise
   requires_procurement = status != "healthy".
8. Required replenishment quantity (only when requires_procurement):
   An order placed today lands on day `lead_time_days`. We want stock to stay at or above safety stock
   for COVERAGE_DAYS (7) days after it lands, i.e. on days [lead, lead + 7 - 1]. So:

       required_quantity = max(0, safety_stock - lowest projected_inventory in that window)

   rounded up to a sensible unit (0.1 for kg/L, whole numbers for "each"). Supplier pack sizes and
   minimum order quantities are applied later, when we choose a supplier.
9. required_by = the predicted breach date: the day replenishment must have landed to protect safety stock.
   can_meet_required_by says whether the incumbent's lead time makes that possible;
   lead_time_budget_days says how fast a supplier would have to be to still protect safety stock / avoid
   a stockout, which is what the agent later uses to filter suppliers.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Callable

HISTORY_DAYS = 28
RECENT_DAYS = 7
TREND_MIN, TREND_MAX = 0.8, 1.25
SEVEN_DAY_HORIZON = 7
MIN_PROJECTION_DAYS = 14   # the projection is extended if lead time + coverage is longer
REVIEW_DAYS = 2            # "approaching" = the order deadline is within this many days
COVERAGE_DAYS = 7          # how long one replenishment should keep stock above safety

SEVERITY = {"healthy": 0, "approaching": 1, "at_risk": 2, "critical": 3}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

EventMultiplier = Callable[[date], float]


def _no_events(_day: date) -> float:
    return 1.0


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def usage_model(usage: list[dict]) -> dict:
    """Weekday averages and the capped recent-trend multiplier, from usage_history rows."""
    rows = sorted(usage, key=lambda r: r["usage_date"])[-HISTORY_DAYS:]
    by_weekday: list[list[float]] = [[] for _ in range(7)]
    for r in rows:
        by_weekday[date.fromisoformat(r["usage_date"]).weekday()].append(float(r["quantity_used"]))
    quantities = [float(r["quantity_used"]) for r in rows]
    overall = _mean(quantities)
    weekday_avg = {WEEKDAYS[w]: (_mean(v) if v else overall) for w, v in enumerate(by_weekday)}

    recent = quantities[-RECENT_DAYS:]
    previous = quantities[-2 * RECENT_DAYS:-RECENT_DAYS]
    recent_avg = _mean(recent)
    previous_avg = _mean(previous)
    if len(quantities) >= 2 * RECENT_DAYS and previous_avg > 0:
        raw_trend = recent_avg / previous_avg
        trend = min(max(raw_trend, TREND_MIN), TREND_MAX)
    else:  # not enough history to measure a trend
        raw_trend, trend = None, 1.0
    return {
        "history_days": len(rows),
        "weekday_averages": {d: round(v, 3) for d, v in weekday_avg.items()},
        "recent_7_day_average": round(recent_avg, 3),
        "previous_7_day_average": round(previous_avg, 3),
        "raw_trend_multiplier": round(raw_trend, 4) if raw_trend is not None else None,
        "trend_multiplier": round(trend, 4),
        "trend_capped": raw_trend is not None and trend != raw_trend,
        "_weekday_avg": weekday_avg,  # unrounded, for the calculation
        "_trend": trend,
    }


def _round_up(quantity: float, unit: str) -> float:
    if quantity <= 0:
        return 0.0
    if unit == "each":
        return float(math.ceil(quantity - 1e-9))
    return math.ceil(quantity * 10 - 1e-9) / 10


def forecast_item(item: dict, usage: list[dict], today: date | None = None,
                  event_multiplier: EventMultiplier | None = None,
                  min_projection_days: int = MIN_PROJECTION_DAYS,
                  review_days: int = REVIEW_DAYS, coverage_days: int = COVERAGE_DAYS) -> dict:
    today = today or date.today()
    event_multiplier = event_multiplier or _no_events
    model = usage_model(usage)
    weekday_avg, trend = model.pop("_weekday_avg"), model.pop("_trend")

    stock = float(item["current_stock"])
    safety = float(item["safety_stock"])
    lead = int(item["lead_time_days"])
    unit = item["unit"]
    incoming_qty = float(item.get("incoming_quantity") or 0)
    incoming_date = date.fromisoformat(item["incoming_date"]) if item.get("incoming_date") else None
    horizon = max(min_projection_days, lead + coverage_days)

    rows, projected_values, usages = [], [], []
    cumulative = 0.0
    breach = stockout = None
    days_of_cover = None
    for k in range(horizon):
        day = today + timedelta(days=k)
        arrived = incoming_qty if (incoming_date and today <= incoming_date <= day) else 0.0
        ev = float(event_multiplier(day))
        base = weekday_avg[WEEKDAYS[day.weekday()]]
        usage_k = base * trend * ev
        level_before = stock + arrived - cumulative
        cumulative += usage_k
        projected = stock + arrived - cumulative
        if breach is None and projected < safety:
            breach = day
        if stockout is None and projected <= 0:
            stockout = day
            days_of_cover = k + (max(level_before, 0.0) / usage_k if usage_k > 0 else 0.0)
            breach = breach or day
        usages.append(usage_k)
        projected_values.append(projected)
        rows.append({
            "date": day.isoformat(), "weekday": WEEKDAYS[day.weekday()],
            "weekday_average": round(base, 3), "trend_multiplier": round(trend, 4),
            "event_multiplier": round(ev, 4), "forecast_usage": round(usage_k, 3),
            "incoming_arrived": arrived, "cumulative_usage": round(cumulative, 3),
            "projected_inventory": round(projected, 3),
        })

    earliest_delivery = today + timedelta(days=lead)
    order_by = breach - timedelta(days=lead) if breach else None
    days_to_deadline = (order_by - today).days if order_by else None

    if breach is None:
        status = "healthy"
    elif stockout and stockout < earliest_delivery:
        status = "critical"
    elif breach < earliest_delivery:
        status = "at_risk"
    elif days_to_deadline <= review_days:
        status = "approaching"
    else:
        status = "healthy"
    requires = status != "healthy"

    required_qty, replenishment = 0.0, None
    window = list(range(lead, min(lead + coverage_days, horizon)))
    if requires and window:
        worst = min(window, key=lambda i: projected_values[i])
        need = safety - projected_values[worst]
        required_qty = _round_up(need, unit)
        replenishment = {
            "delivery_day": rows[lead]["date"],
            "window_start": rows[window[0]]["date"], "window_end": rows[window[-1]]["date"],
            "coverage_days": coverage_days,
            "lowest_projected_in_window": round(projected_values[worst], 3),
            "lowest_projected_on": rows[worst]["date"],
            "formula": "required = safety_stock - lowest projected inventory in the window, rounded up",
        }

    total_7 = sum(usages[:SEVEN_DAY_HORIZON])
    result = {
        "item": {"id": item.get("id"), "sku": item["sku"], "name": item["name"], "unit": unit},
        "current_stock": stock, "safety_stock": safety, "lead_time_days": lead,
        "incoming": {"quantity": incoming_qty, "date": incoming_date.isoformat() if incoming_date else None},
        "usage_model": model,
        "seven_day_forecast": {"total_usage": round(total_7, 2),
                               "average_per_day": round(total_7 / SEVEN_DAY_HORIZON, 2),
                               "daily_usage": [round(u, 2) for u in usages[:SEVEN_DAY_HORIZON]]},
        "daily_projection": rows,
        "predicted_breach_date": breach.isoformat() if breach else None,
        "predicted_stockout_date": stockout.isoformat() if stockout else None,
        "days_of_cover": round(days_of_cover, 2) if days_of_cover is not None else None,
        "status": status,
        "requires_procurement": requires,
        "required_quantity": required_qty,
        "required_by": breach.isoformat() if requires else None,
        "earliest_delivery_date": earliest_delivery.isoformat(),
        "order_by_date": order_by.isoformat() if order_by else None,
        "days_until_order_deadline": days_to_deadline,
        "can_meet_required_by": (earliest_delivery <= breach) if requires else None,
        "lead_time_budget_days": {
            "protect_safety_stock": (breach - today).days if requires else None,
            "avoid_stockout": (stockout - today).days if requires and stockout else None,
        },
        "replenishment": replenishment,
    }
    result["explanation"] = _explain(result, today, horizon)
    return result


def _explain(f: dict, today: date, horizon: int) -> str:
    it, u = f["item"], f["item"]["unit"]
    m = f["usage_model"]
    parts = [
        f"{it['name']}: {f['current_stock']:g} {u} on hand, safety stock {f['safety_stock']:g} {u}.",
        f"Forecast usage over the next 7 days is {f['seven_day_forecast']['total_usage']:g} {u} "
        f"(weekday pattern x recent trend {m['trend_multiplier']:g}"
        f"{' (capped)' if m['trend_capped'] else ''}).",
    ]
    events = sorted({r["event_multiplier"] for r in f["daily_projection"] if r["event_multiplier"] != 1})
    if events:
        parts.append("Demand event multipliers applied: " + ", ".join(f"x{e:g}" for e in events) + ".")
    if f["incoming"]["quantity"] and f["incoming"]["date"]:
        parts.append(f"{f['incoming']['quantity']:g} {u} is already due on {f['incoming']['date']}.")

    breach, stockout = f["predicted_breach_date"], f["predicted_stockout_date"]
    if breach is None:
        parts.append(f"Stock stays above safety stock for the whole {horizon}-day projection. No order needed.")
    else:
        parts.append(f"Stock is projected to fall below safety stock on {breach}"
                     + (f" and run out on {stockout} ({f['days_of_cover']:g} days of cover)." if stockout else "."))
        lead, earliest = f["lead_time_days"], f["earliest_delivery_date"]
        if f["requires_procurement"]:
            timing = ("in time" if f["can_meet_required_by"]
                      else f"too late: that is after {f['required_by']}")
            parts.append(f"With a {lead}-day lead time the earliest delivery is {earliest}, {timing}.")
            r = f["replenishment"]
            parts.append(f"To stay above safety stock from {r['window_start']} to {r['window_end']} "
                         f"order {f['required_quantity']:g} {u} (safety {f['safety_stock']:g} minus lowest "
                         f"projected stock {r['lowest_projected_in_window']:g} on {r['lowest_projected_on']}).")
        else:
            parts.append(f"The order deadline ({f['order_by_date']}) is {f['days_until_order_deadline']} days "
                         f"away, so no order is needed yet.")
    return " ".join(parts)


def rank_forecasts(forecasts: list[dict]) -> list[dict]:
    """Most urgent first: severity, then nearest order deadline, then sku (stable and deterministic)."""
    return sorted(forecasts, key=lambda f: (
        -SEVERITY[f["status"]],
        f["days_until_order_deadline"] if f["days_until_order_deadline"] is not None else 10**6,
        f["item"]["sku"]))


def forecast_all(repo, today: date | None = None, event_multiplier: EventMultiplier | None = None,
                 include_projection: bool = True, **kwargs) -> list[dict]:
    today = today or date.today()
    out = []
    for item in repo.list_inventory_items():
        f = forecast_item(item, repo.get_usage_history(item["id"]), today, event_multiplier, **kwargs)
        if not include_projection:
            del f["daily_projection"]
        out.append(f)
    return rank_forecasts(out)


def format_forecast(f: dict) -> str:
    """Plain-text report showing exactly why the system does (or does not) want to order."""
    it, u, m = f["item"], f["item"]["unit"], f["usage_model"]
    rows = f.get("daily_projection")
    L = []
    verdict = "ORDER REQUIRED" if f["requires_procurement"] else "no order needed"
    L += ["=" * 78, f"{it['sku']}  {it['name']}", f"STATUS: {f['status'].upper()}   ->   {verdict}", "=" * 78, ""]

    L += ["1. INPUTS",
          f"   On hand: {f['current_stock']:g} {u}     Safety stock: {f['safety_stock']:g} {u}"
          f"     Lead time: {f['lead_time_days']} days"]
    inc = f["incoming"]
    L += [f"   Incoming: {inc['quantity']:g} {u} on {inc['date']}" if inc["quantity"] and inc["date"]
          else "   Incoming: none", ""]

    L += [f"2. DEMAND MODEL (last {m['history_days']} days of usage)",
          "   Average usage by weekday: " + "  ".join(f"{d} {v:g}" for d, v in m["weekday_averages"].items()),
          f"   Recent 7-day average: {m['recent_7_day_average']:g}    Previous 7-day average: "
          f"{m['previous_7_day_average']:g}",
          f"   Trend = {m['recent_7_day_average']:g} / {m['previous_7_day_average']:g} = "
          f"{m['raw_trend_multiplier']}" if m["raw_trend_multiplier"] is not None else "   Trend: not enough history",
          f"   Trend multiplier used: {m['trend_multiplier']:g}"
          f"{'  (capped to 0.8-1.25)' if m['trend_capped'] else '  (within the 0.8-1.25 cap)'}", ""]

    if rows:
        L += ["3. PROJECTION   forecast usage = weekday average x trend x event",
              "   date        day   avg x trend x event = usage   incoming   projected stock"]
        for r in rows[:max(SEVEN_DAY_HORIZON, f["lead_time_days"] + COVERAGE_DAYS)]:
            flag = ""
            if r["date"] == f["predicted_stockout_date"]:
                flag = "  <-- STOCKOUT"
            elif r["date"] == f["predicted_breach_date"]:
                flag = "  <-- below safety"
            inc_txt = f"+{r['incoming_arrived']:g}" if r["incoming_arrived"] and r["date"] == inc["date"] else ""
            L.append(f"   {r['date']}  {r['weekday']}  {r['weekday_average']:>6.2f} x {r['trend_multiplier']:.2f} x "
                     f"{r['event_multiplier']:.2f} = {r['forecast_usage']:>6.2f}   {inc_txt:>8}   "
                     f"{r['projected_inventory']:>9.2f}{flag}")
        L.append("")

    s = f["seven_day_forecast"]
    L += ["4. FINDINGS",
          f"   Total forecast usage, next 7 days: {s['total_usage']:g} {u}  ({s['average_per_day']:g} per day)",
          f"   Falls below safety stock on: {f['predicted_breach_date'] or 'not within the projection'}",
          f"   Runs out on: {f['predicted_stockout_date'] or 'not within the projection'}"
          + (f"   ({f['days_of_cover']:g} days of cover)" if f["days_of_cover"] is not None else ""),
          f"   Earliest possible delivery: {f['earliest_delivery_date']}   Order-by date: "
          f"{f['order_by_date'] or 'n/a'}", ""]

    if f["requires_procurement"]:
        r, b = f["replenishment"], f["lead_time_budget_days"]
        L += ["5. REORDER CALCULATION",
              f"   An order placed today lands on {r['delivery_day']}. Keep stock >= safety ({f['safety_stock']:g} {u}) "
              f"for {r['coverage_days']} days: {r['window_start']} to {r['window_end']}.",
              f"   Lowest projected stock in that window: {r['lowest_projected_in_window']:g} {u} on {r['lowest_projected_on']}",
              f"   Required quantity = {f['safety_stock']:g} - ({r['lowest_projected_in_window']:g}) "
              f"= {f['required_quantity']:g} {u}  (rounded up)",
              f"   Required by: {f['required_by']}   Can the current supplier deliver by then? "
              f"{'yes' if f['can_meet_required_by'] else 'NO'}",
              f"   To protect safety stock a supplier must deliver within {b['protect_safety_stock']} days; "
              f"to avoid a stockout within {b['avoid_stockout']} days." if b["avoid_stockout"] is not None else
              f"   To protect safety stock a supplier must deliver within {b['protect_safety_stock']} days.", ""]
    L += ["EXPLANATION", "   " + f["explanation"], ""]
    return "\n".join(L)
