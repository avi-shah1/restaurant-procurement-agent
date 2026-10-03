"""Environment configuration. Secrets are read here and nowhere else."""
import os

from dotenv import load_dotenv

load_dotenv()

# name -> required for the full app to work
ENV_VARS = {
    "ZOOWORK_API_KEY": True,
    "ZOOWORK_AGENT_ID": False,  # printed by scripts/setup_agent.py
    "AGENT_MODE": False,  # auto (default) | live | mock
    "AGENT_FALLBACK": False,  # 0 = do not fall back to the demo agent when the live one fails
    "MARKET_SEARCH_PROVIDER": False,  # auto (default) | tavily | mock
    "TAVILY_API_KEY": True,
    "SUPABASE_URL": True,
    "SUPABASE_SERVICE_ROLE_KEY": True,
    "FLASK_SECRET_KEY": False,
    "DEMO_MODE": False,  # 1 = use local demo data even if Supabase keys exist
    # Phase 7 — supplier communication (server-side only; never exposed to the browser)
    "TEST_EMAIL_OVERRIDE": False,  # redirect every RFQ to this inbox for the demo
    "RFQ_EMAIL_MODE": False,  # mock (default) | smtp
    "RFQ_SIMULATE_REPLY": False,  # 1 (default) = invent a clearly labelled supplier reply after send
    "SMTP_HOST": False,
    "SMTP_PORT": False,
    "SMTP_USER": False,
    "SMTP_PASSWORD": False,
    "SMTP_FROM": False,
    "SMTP_STARTTLS": False,
    "RESTAURANT_NAME": False,
}


# Demo demand events. Each one multiplies forecast usage on matching days. All are OFF by default,
# so the default event multiplier is 1. Toggle at runtime via app/demand_events.py (API: /api/events).
#   weekday: 0=Mon .. 6=Sun, recurring      on_date: one specific ISO date (use instead of weekday)
DEMAND_EVENTS = [
    {"key": "saturday_rush", "label": "Saturday rush", "weekday": 5, "multiplier": 1.20, "enabled": False},
    {"key": "sunday_brunch", "label": "Sunday brunch crowd", "weekday": 6, "multiplier": 1.15, "enabled": False},
]


def env_status() -> dict[str, dict[str, bool]]:
    """Which variables are set. Returns booleans only, never the values."""
    return {
        name: {"set": bool(os.environ.get(name)), "required": required}
        for name, required in ENV_VARS.items()
    }
