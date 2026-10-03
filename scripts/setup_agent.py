"""Create (or re-sync) the Procurement Manager agent on ZooWork. Run once, or after changing the tools.

    python scripts/setup_agent.py                 create the agent, or update it if ZOOWORK_AGENT_ID is set
    python scripts/setup_agent.py --write-env     also save the new agent id into .env

Needs ZOOWORK_API_KEY in .env (a Project key from https://platform.zoowork.ai, starts with zwp_live_).
This makes real API calls to your ZooWork project (creating an agent is billable/tenant-changing).
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402,F401  (importing config loads .env)
from app.agent.zoowork_service import provision_agent  # noqa: E402
from zoowork import ZooworkError, create_zoowork_client  # noqa: E402


async def main(write_env: bool) -> None:
    key = os.environ.get("ZOOWORK_API_KEY")
    if not key:
        sys.exit("ZOOWORK_API_KEY is not set. Add it to .env first (never paste it into chat).")
    existing = os.environ.get("ZOOWORK_AGENT_ID") or None
    async with create_zoowork_client(key, base_url=os.environ.get("ZOOWORK_BASE_URL") or None) as client:
        try:
            agent_id, action = await provision_agent(client, existing)
        except ZooworkError as e:
            sys.exit(f"ZooWork error {e.status} {e.type or ''}: {e}")
    print(f"Agent {action}: {agent_id} (running)")
    if write_env and not existing:
        env = ROOT / ".env"
        text = env.read_text(encoding="utf-8") if env.exists() else ""
        lines = [l for l in text.splitlines() if not l.startswith("ZOOWORK_AGENT_ID=")]
        lines.append(f"ZOOWORK_AGENT_ID={agent_id}")
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print("Saved ZOOWORK_AGENT_ID to .env")
    elif not existing:
        print(f"Add this to .env:  ZOOWORK_AGENT_ID={agent_id}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--write-env", action="store_true")
    asyncio.run(main(ap.parse_args().write_env))
