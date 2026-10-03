"""Forecast and reorder engine. Hand-calculated cases first, then the seeded demo data."""
from datetime import date, timedelta

import pytest

from app import create_app
from app.data import reset_repo
from app.data.local_repo import LocalRepository
from app.data.scenarios import reset_scenario
from app.demand_events import (active_event_multiplier, enabled_keys, event_multiplier_for,
                               list_events, reset_events, set_enabled)
from app.forecast import (COVERAGE_DAYS, _round_up, forecast_all, forecast_item, format_forecast,
                          usage_model)

TODAY = date(2026, 10, 3)  # a Saturday
WEEK = [date(2026, 9, 28) + timedelta(days=i) for i in range(7)]  # Mon..Sun


@pytest.fixture(autouse=True)
def _clean_state():
    reset_scenario(), reset_events()
    yield
    reset_scenario(), reset_events(), reset_repo()


def item(**kw):
    base = {"id": 1, "sku": "X-1", "name": "Test item", "unit": "kg", "current_stock": 100,
            "safety_stock": 20, "lead_time_days": 2, "incoming_quantity": 0, "incoming_date": None}
    base.update(kw)
    return base


def usage(recent=10.0, older=10.0, today=TODAY):
    """28 days ending yesterday: the last 7 days use `recent`, the 21 before use `older`."""
    return [{"usage_date": (today - timedelta(days=i)).isoformat(),
             "quantity_used": recent if i <= 7 else older} for i in range(28, 0, -1)]


def day(offset):
    return (TODAY + timedelta(days=offset)).isoformat()


# ---------- demand model ----------
def test_weekday_averages_and_trend():
    m = usage_model(usage(recent=12, older=10))
    assert all(v == pytest.approx(10.5) for v in m["weekday_averages"].values())  # (12+10+10+10)/4
    assert m["recent_7_day_average"] == 12 and m["previous_7_day_average"] == 10
    assert m["raw_trend_multiplier"] == pytest.approx(1.2)
    assert m["trend_multiplier"] == pytest.approx(1.2) and m["trend_capped"] is False


@pytest.mark.parametrize("recent, expected", [(20, 1.25), (5, 0.8)])
def test_trend_is_capped(recent, expected):
    m = usage_model(usage(recent=recent, older=10))
    assert m["trend_multiplier"] == expected and m["trend_capped"] is True


def test_trend_needs_two_weeks_of_history():
    m = usage_model(usage()[-10:])
    assert m["trend_multiplier"] == 1.0 and m["raw_trend_multiplier"] is None


def test_only_the_latest_28_days_are_used():
    longer = [{"usage_date": (TODAY - timedelta(days=i)).isoformat(), "quantity_used": 999}
              for i in range(60, 28, -1)] + usage()
    assert usage_model(longer)["history_days"] == 28
    assert usage_model(longer)["weekday_averages"]["Mon"] == pytest.approx(10)


def test_forecast_usage_is_weekday_x_trend_x_event():
    saturday_rush = lambda d: 1.2 if d.weekday() == 5 else 1.0  # noqa: E731
    f = forecast_item(item(), usage(recent=12, older=10), TODAY, saturday_rush)
    rows = f["daily_projection"]
    assert rows[0]["weekday"] == "Sat" and rows[0]["forecast_usage"] == pytest.approx(10.5 * 1.2 * 1.2)  # 15.12
    assert rows[1]["forecast_usage"] == pytest.approx(10.5 * 1.2)                                         # 12.6
    assert rows[7]["forecast_usage"] == pytest.approx(15.12)  # next Saturday
    assert f["seven_day_forecast"]["total_usage"] == pytest.approx(15.12 + 6 * 12.6, abs=0.01)  # 90.72
    assert [r["event_multiplier"] for r in rows[:2]] == [1.2, 1.0]


def test_default_event_multiplier_is_one():
    f = forecast_item(item(), usage(), TODAY)
    assert {r["event_multiplier"] for r in f["daily_projection"]} == {1.0}


# ---------- projected inventory ----------
def test_projected_inventory_formula_with_incoming():
    # stock + incoming-arrived-by-date - cumulative usage (10/day)
    f = forecast_item(item(incoming_quantity=50, incoming_date=day(3)), usage(), TODAY)
    p = [r["projected_inventory"] for r in f["daily_projection"]]
    assert p[:6] == [90, 80, 70, 110, 100, 90]  # d3: 100 + 50 - 40


def test_incoming_dated_before_today_is_ignored():
    f = forecast_item(item(incoming_quantity=50, incoming_date=day(-1)), usage(), TODAY)
    assert f["daily_projection"][0]["projected_inventory"] == 90


def test_breach_stockout_and_days_of_cover():
    f = forecast_item(item(), usage(), TODAY)  # 90, 80 ... 20 (d7), 10 (d8), 0 (d9)
    assert f["predicted_breach_date"] == day(8) and f["predicted_stockout_date"] == day(9)
    assert f["days_of_cover"] == pytest.approx(10.0)
    assert f["seven_day_forecast"]["total_usage"] == 70


def test_days_of_cover_is_fractional():
    f = forecast_item(item(current_stock=25), usage(), TODAY)  # 15, 5, -5  -> 2 + 5/10
    assert f["predicted_stockout_date"] == day(2) and f["days_of_cover"] == pytest.approx(2.5)


def test_no_breach_means_no_dates_and_no_cover():
    f = forecast_item(item(current_stock=10_000), usage(), TODAY)
    assert f["predicted_breach_date"] is None and f["predicted_stockout_date"] is None
    assert f["days_of_cover"] is None and f["status"] == "healthy"


# ---------- reorder quantity ----------
def test_required_quantity_hand_calculated():
    # lead 7: an order lands day 7 and must cover days 7..13 (P = 100 - 10(d+1)); lowest is d13 = -40
    f = forecast_item(item(lead_time_days=7), usage(), TODAY)
    assert f["status"] == "approaching" and f["requires_procurement"] is True
    assert f["required_quantity"] == 60  # safety 20 - (-40)
    assert f["required_by"] == day(8) and f["earliest_delivery_date"] == day(7)
    assert f["can_meet_required_by"] is True
    r = f["replenishment"]
    assert (r["window_start"], r["window_end"]) == (day(7), day(13))
    assert r["lowest_projected_in_window"] == -40 and r["lowest_projected_on"] == day(13)


def test_nothing_required_when_the_deadline_is_far_off():
    f = forecast_item(item(lead_time_days=2), usage(), TODAY)  # breach d8, order by d6
    assert f["requires_procurement"] is False and f["status"] == "healthy"
    assert f["required_quantity"] == 0 and f["required_by"] is None and f["replenishment"] is None


def test_incoming_stock_reduces_the_required_quantity():
    # lead 10: window is d10..d16, the lowest point is d16.
    without = forecast_item(item(lead_time_days=10), usage(), TODAY)
    with_delivery = forecast_item(
        item(lead_time_days=10, incoming_quantity=30, incoming_date=day(5)), usage(), TODAY)
    assert without["required_quantity"] == 90        # d16: 100 - 170 = -70 -> 20 - (-70)
    assert with_delivery["required_quantity"] == 60  # d16: 100 + 30 - 170 = -40 -> 20 - (-40)
    assert with_delivery["status"] == "approaching"  # the delivery pushes the breach out to d11


def test_projection_extends_to_cover_long_lead_times():
    f = forecast_item(item(lead_time_days=12), usage(), TODAY)
    assert len(f["daily_projection"]) == 12 + COVERAGE_DAYS
    assert f["status"] == "critical"
    assert f["required_quantity"] == 110  # window d12..d18, floor 100 - 190 = -90, safety 20


def test_lead_time_budget_tells_the_agent_how_fast_a_supplier_must_be():
    f = forecast_item(item(lead_time_days=10), usage(), TODAY)  # breach d8, stockout d9
    assert f["status"] == "critical" and f["can_meet_required_by"] is False
    assert f["lead_time_budget_days"] == {"protect_safety_stock": 8, "avoid_stockout": 9}


def test_rounding_is_up_and_unit_aware():
    assert _round_up(12.31, "kg") == 12.4 and _round_up(12.0, "kg") == 12.0
    assert _round_up(12.1, "each") == 13 and _round_up(0, "kg") == 0 and _round_up(-5, "L") == 0


def test_forecast_is_deterministic():
    assert forecast_item(item(), usage(7, 10), TODAY) == forecast_item(item(), usage(7, 10), TODAY)


# ---------- the seeded demo data ----------
@pytest.fixture
def seeded(tmp_path):
    def _seeded(today):
        repo = LocalRepository(tmp_path / f"db-{today}.json", today=today)
        return {f["item"]["sku"]: f for f in forecast_all(repo, today=today)}
    return _seeded


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_seeded_mozzarella_triggers_procurement(seeded, today):
    m = seeded(today)["MOZ-001"]
    assert m["requires_procurement"] is True and m["status"] == "critical"
    assert m["required_quantity"] > 0
    assert 35 <= m["required_quantity"] <= 70  # ~6.3/day for 3 + 7 days, +12 safety, -14 stock, -10 incoming
    assert m["predicted_breach_date"] == today.isoformat()  # already at the safety line
    assert m["predicted_stockout_date"] is not None and m["days_of_cover"] < m["lead_time_days"]
    assert m["can_meet_required_by"] is False  # the 3-day incumbent cannot beat the stockout
    assert m["lead_time_budget_days"]["avoid_stockout"] < m["lead_time_days"]
    assert "order" in m["explanation"] and "safety" in m["explanation"]


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_seeded_default_only_mozzarella_and_oat_need_ordering(seeded, today):
    needs = {sku for sku, f in seeded(today).items() if f["requires_procurement"]}
    assert needs == {"MOZ-001", "OAT-001"}
    for sku, f in seeded(today).items():
        assert (f["required_quantity"] > 0) == f["requires_procurement"], sku


@pytest.mark.parametrize("today", WEEK, ids=lambda d: d.strftime("%a"))
def test_required_quantity_matches_the_projection_it_came_from(seeded, today):
    for sku, f in seeded(today).items():
        rows, stock = f["daily_projection"], f["current_stock"]
        inc = f["incoming"]
        for r in rows:  # projected = stock + arrived - cumulative usage
            assert r["projected_inventory"] == pytest.approx(
                stock + r["incoming_arrived"] - r["cumulative_usage"], abs=0.01), sku
        assert [r["date"] for r in rows][0] == today.isoformat()
        if f["requires_procurement"]:
            lead = f["lead_time_days"]
            floor = min(r["projected_inventory"] for r in rows[lead:lead + COVERAGE_DAYS])
            need = f["safety_stock"] - floor
            step = 1.0 if f["item"]["unit"] == "each" else 0.1
            assert need - 0.01 <= f["required_quantity"] <= need + step + 0.01, sku
            assert inc is not None


def test_ranking_puts_mozzarella_first(seeded):
    repo_results = list(seeded(TODAY).values())
    assert repo_results[0]["item"]["sku"] == "MOZ-001"


# ---------- demand events ----------
def test_event_multiplier_function():
    events = [{"key": "a", "weekday": 5, "multiplier": 1.2},
              {"key": "b", "on_date": "2026-10-10", "multiplier": 1.5},
              {"key": "c", "weekday": 5, "multiplier": 2.0}]
    assert event_multiplier_for(date(2026, 10, 3), events, set()) == 1.0
    assert event_multiplier_for(date(2026, 10, 3), events, {"a"}) == 1.2
    assert event_multiplier_for(date(2026, 10, 3), events, {"a", "c"}) == pytest.approx(2.4)  # stack
    assert event_multiplier_for(date(2026, 10, 10), events, {"a", "b"}) == pytest.approx(1.8)
    assert event_multiplier_for(date(2026, 10, 4), events, {"a", "b", "c"}) == 1.0  # Sunday


def test_toggling_a_configured_event_changes_the_forecast(tmp_path):
    repo = LocalRepository(tmp_path / "db.json", today=TODAY)
    assert enabled_keys() == [] and active_event_multiplier(TODAY) == 1.0  # off by default
    off = {f["item"]["sku"]: f for f in forecast_all(repo, TODAY, active_event_multiplier)}

    set_enabled("saturday_rush", True)
    assert active_event_multiplier(TODAY) == 1.2  # Saturday x1.20, from config
    on = {f["item"]["sku"]: f for f in forecast_all(repo, TODAY, active_event_multiplier)}
    assert on["TOM-001"]["seven_day_forecast"]["total_usage"] > off["TOM-001"]["seven_day_forecast"]["total_usage"]
    sat_off = [r for r in off["TOM-001"]["daily_projection"] if r["weekday"] == "Sat"]
    sat_on = [r for r in on["TOM-001"]["daily_projection"] if r["weekday"] == "Sat"]
    assert all(b["forecast_usage"] == pytest.approx(a["forecast_usage"] * 1.2, abs=0.01)
               for a, b in zip(sat_off, sat_on))
    non_sat = [(a, b) for a, b in zip(off["TOM-001"]["daily_projection"], on["TOM-001"]["daily_projection"])
               if a["weekday"] != "Sat"]
    assert all(a["forecast_usage"] == b["forecast_usage"] for a, b in non_sat)

    reset_events()
    assert enabled_keys() == []
    with pytest.raises(ValueError):
        set_enabled("nope", True)


# ---------- report ----------
def test_format_forecast_explains_why(seeded):
    text = format_forecast(seeded(TODAY)["MOZ-001"])
    for expected in ("Mozzarella", "STATUS: CRITICAL", "ORDER REQUIRED", "DEMAND MODEL", "PROJECTION",
                     "REORDER CALCULATION", "Required quantity", "STOCKOUT", "EXPLANATION"):
        assert expected in text
    healthy = format_forecast(seeded(TODAY)["FLR-001"])
    assert "no order needed" in healthy and "REORDER CALCULATION" not in healthy


# ---------- API ----------
@pytest.fixture
def client():
    reset_repo()
    return create_app().test_client()


def test_events_api(client):
    body = client.get("/api/events").get_json()
    assert {e["key"] for e in body["events"]} >= {"saturday_rush"}
    assert all(e["enabled"] is False for e in body["events"])

    res = client.post("/api/events/saturday_rush", json={"enabled": True})
    assert res.status_code == 200
    assert next(e for e in res.get_json()["events"] if e["key"] == "saturday_rush")["enabled"] is True
    assert enabled_keys() == ["saturday_rush"]

    assert client.post("/api/events/saturday_rush", json={"enabled": "yes"}).status_code == 400
    assert client.post("/api/events/saturday_rush", json={}).status_code == 400
    assert client.post("/api/events/nope", json={"enabled": True}).status_code == 404

    client.post("/api/events/reset")
    assert enabled_keys() == [] and all(not e["enabled"] for e in list_events())


def test_forecast_api(client):
    body = client.get("/api/forecast?today=2026-10-02").get_json()
    assert body["scenario"] == "default" and body["events_enabled"] == []
    assert len(body["items"]) == 10 and body["items"][0]["item"]["sku"] == "MOZ-001"
    assert body["items"][0]["requires_procurement"] is True
    assert "daily_projection" in body["items"][0]

    slim = client.get("/api/forecast?today=2026-10-02&include_projection=0").get_json()
    assert all("daily_projection" not in i for i in slim["items"])

    client.post("/api/events/saturday_rush", json={"enabled": True})
    assert client.get("/api/forecast").get_json()["events_enabled"] == ["saturday_rush"]
    client.post("/api/scenarios/active", json={"key": "avocado_shortage"})
    assert client.get("/api/forecast").get_json()["items"][0]["item"]["sku"] == "AVO-001"

    assert client.get("/api/forecast?today=bad").status_code == 400
