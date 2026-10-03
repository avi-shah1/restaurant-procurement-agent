"""Data layer entry point: get_repo() returns the right backend.

DEMO_MODE=1, or missing Supabase credentials, selects the local JSON backend. Business logic
only ever sees the Repository interface, so nothing else changes when the backend does.
"""
from __future__ import annotations

import logging
import os
import threading

from .repository import Repository

log = logging.getLogger(__name__)
_repo: Repository | None = None
_lock = threading.Lock()


def _build() -> Repository:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if os.environ.get("DEMO_MODE") != "1" and url and key:
        from .supabase_repo import SupabaseRepository
        return SupabaseRepository(url, key)
    from .local_repo import LocalRepository
    reason = "DEMO_MODE=1" if os.environ.get("DEMO_MODE") == "1" else "no Supabase credentials"
    log.warning("Using local demo data (%s).", reason)
    return LocalRepository()


def get_base_repo() -> Repository:
    """The raw backend (Supabase or local), with no scenario applied."""
    global _repo
    with _lock:
        if _repo is None:
            _repo = _build()
        return _repo


def get_repo() -> Repository:
    """What application code should use: the backend with the active scenario applied."""
    from .scenario_repo import ScenarioRepository
    return ScenarioRepository(get_base_repo())


def reset_repo() -> None:
    """Forget the cached backend (used by tests)."""
    global _repo
    with _lock:
        _repo = None


__all__ = ["Repository", "get_repo", "get_base_repo", "reset_repo"]
