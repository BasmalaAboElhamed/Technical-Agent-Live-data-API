"""One collection cycle against the real source, stored into the local SQLite DB.

    python scripts/run_collector_once.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.collectors.factory import open_collector  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database.repository import Repository  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402
from app.services.polling import Poller  # noqa: E402


async def main() -> int:
    s = Settings.from_env()
    setup_logging(s.log_level)
    repo = Repository.from_settings(s)
    repo.init_schema()
    async with open_collector(s) as collector:
        poller = Poller(collector, repo, s)
        outcome = await poller.run_once()
        await poller.stop()
    run = repo.latest_run()
    print(f"outcome={outcome}  db={s.database_path}")
    if run:
        print(f"status={run['status']} stocks={run['stock_count']} pages={run['pages']} requests={run['requests']} "
              f"retries={run['retries']} rejected={run['rejected']} duplicates={run['duplicates']} "
              f"duration={run['duration_ms']}ms source_ts_max={run['source_timestamp_max']} error={run['error']}")
    return 0 if outcome in ("success", "partial") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
