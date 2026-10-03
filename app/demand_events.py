"""Demand events: optional multipliers on forecast usage (e.g. Saturday x1.20).

The definitions live in config.DEMAND_EVENTS. This module holds which ones are switched on
(in-process state, like scenarios) so a UI can toggle them. The forecast engine never imports
this; it just receives a function `day -> multiplier`.
"""
from __future__ import annotations

import threading
from datetime import date

from .config import DEMAND_EVENTS


def event_multiplier_for(day: date, events: list[dict], enabled_keys: set[str]) -> float:
    """Product of the multipliers of every enabled event that matches `day`. 1.0 if none do."""
    result = 1.0
    for ev in events:
        if ev["key"] not in enabled_keys:
            continue
        if "on_date" in ev:
            matches = date.fromisoformat(ev["on_date"]) == day
        else:
            matches = ev["weekday"] == day.weekday()
        if matches:
            result *= float(ev["multiplier"])
    return result


_lock = threading.Lock()
_enabled: set[str] = {e["key"] for e in DEMAND_EVENTS if e.get("enabled")}


def list_events() -> list[dict]:
    with _lock:
        return [{**e, "enabled": e["key"] in _enabled} for e in DEMAND_EVENTS]


def enabled_keys() -> list[str]:
    with _lock:
        return sorted(_enabled)


def set_enabled(key: str, enabled: bool) -> None:
    if key not in {e["key"] for e in DEMAND_EVENTS}:
        raise ValueError(f"Unknown demand event {key!r}")
    with _lock:
        (_enabled.add if enabled else _enabled.discard)(key)


def reset_events() -> None:
    """Back to the configured defaults (all off)."""
    with _lock:
        _enabled.clear()
        _enabled.update(e["key"] for e in DEMAND_EVENTS if e.get("enabled"))


def active_event_multiplier(day: date) -> float:
    """What the app passes to the forecast engine."""
    with _lock:
        return event_multiplier_for(day, DEMAND_EVENTS, set(_enabled))
