"""One-shot: crea la tabla tracked_events (watcher de cambios de Calendar).

Uso:
    .venv/bin/python3 scripts/migrate_tracked_events.py

Idempotente (CREATE TABLE IF NOT EXISTS). El DDL canónico vive en db/schema.sql.
"""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from db.client import get_cursor  # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS tracked_events (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    recurring_event_id TEXT,
    summary TEXT NOT NULL DEFAULT '',
    start_raw TEXT NOT NULL DEFAULT '{}',
    end_raw TEXT NOT NULL DEFAULT '{}',
    start_ts TIMESTAMPTZ,
    end_ts TIMESTAMPTZ,
    location TEXT NOT NULL DEFAULT '',
    description_hash TEXT NOT NULL DEFAULT '',
    attendees TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'confirmed',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tracked_events_user ON tracked_events(user_id);
"""


def main() -> int:
    with get_cursor() as cur:
        cur.execute(DDL)
        cur.execute("SELECT count(*) AS n FROM tracked_events")
        print(f"✓ tracked_events lista ({cur.fetchone()['n']} filas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
