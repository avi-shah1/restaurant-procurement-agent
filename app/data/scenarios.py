"""Demo scenarios. A scenario is only DATA: multipliers on the input data.

It never says which items are at risk. The forecast engine works that out from the altered inputs.
The stored (seeded) data is never modified; a scenario is applied as a view when data is read
(see scenario_repo.py), so reset is exact.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

DEFAULT_KEY = "default"

METRO = "Metro Foodservice Wholesale"
FRESHROUTE = "FreshRoute Produce"


@dataclass(frozen=True)
class UsageAdjustment:
    """Multiply historical usage, which the forecast turns into higher or lower demand."""
    skus: tuple[str, ...]
    factor: float
    weekdays: tuple[int, ...] | None = None  # 0=Mon..6=Sun; None = every day
    last_days: int | None = None             # only the most recent N days of history


@dataclass(frozen=True)
class StockAdjustment:
    """Multiply current stock (e.g. 0.08 = a shortage, 4.0 = a big delivery just landed)."""
    skus: tuple[str, ...]
    factor: float


@dataclass(frozen=True)
class SupplierDelay:
    """Add days to a supplier's lead time, and to every item they are the incumbent for."""
    supplier: str
    extra_days: int


@dataclass(frozen=True)
class Scenario:
    key: str
    label: str
    description: str
    usage: tuple[UsageAdjustment, ...] = ()
    stock: tuple[StockAdjustment, ...] = ()
    delays: tuple[SupplierDelay, ...] = ()


SCENARIOS: dict[str, Scenario] = {s.key: s for s in [
    Scenario(
        key=DEFAULT_KEY, label="Default",
        description="The seeded baseline. No adjustments."),
    Scenario(
        key="weekend_rush", label="Weekend rush",
        description="Friday-Sunday demand for pizza ingredients (mozzarella, tomatoes, flour) is 2.5x normal.",
        usage=(UsageAdjustment(("MOZ-001", "TOM-001", "FLR-001"), 2.5, weekdays=(4, 5, 6)),)),
    Scenario(
        key="coffee_spike", label="Coffee spike",
        description=("Coffee bean demand jumps 150% and oat milk demand 80%, every day. Mozzarella is "
                     "assumed restocked so the coffee story stands out."),
        usage=(UsageAdjustment(("COF-001",), 2.5), UsageAdjustment(("OAT-001",), 1.8)),
        stock=(StockAdjustment(("MOZ-001",), 4.0),)),
    Scenario(
        key="supplier_delay", label="Supplier delay",
        description="Metro Foodservice and FreshRoute Produce each take 3 extra days to deliver.",
        delays=(SupplierDelay(METRO, 3), SupplierDelay(FRESHROUTE, 3))),
    Scenario(
        key="avocado_shortage", label="Avocado shortage",
        description=("Avocado stock is cut by 92%. Mozzarella and oat milk are assumed restocked so "
                     "the avocados are the only story."),
        stock=(StockAdjustment(("AVO-001",), 0.08),
               StockAdjustment(("MOZ-001",), 4.0),
               StockAdjustment(("OAT-001",), 2.0))),
]}


def get_scenario(key: str) -> Scenario:
    try:
        return SCENARIOS[key]
    except KeyError:
        raise ValueError(f"Unknown scenario {key!r}. Choose from: {', '.join(SCENARIOS)}") from None


def list_scenarios() -> list[dict]:
    """What a frontend dropdown needs."""
    return [{"key": s.key, "label": s.label, "description": s.description} for s in SCENARIOS.values()]


# ---- the active scenario (in-process state; resets to default when the server restarts) ----
_active = DEFAULT_KEY
_lock = threading.Lock()


def get_active_key() -> str:
    with _lock:
        return _active


def set_active(key: str) -> Scenario:
    scenario = get_scenario(key)  # validates
    global _active
    with _lock:
        _active = key
    return scenario


def reset_scenario() -> Scenario:
    """Back to the original seeded state."""
    return set_active(DEFAULT_KEY)
