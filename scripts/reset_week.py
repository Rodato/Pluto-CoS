"""One-shot: limpia tareas open y resetea processed_notes de la ventana actual.

Uso:
    railway run python3 scripts/reset_week.py [--dry-run]

Qué hace:
1. Marca como `dropped` todas las tareas open del usuario (no DELETE — preserva audit).
2. Borra de processed_notes las filas cuyo mtime cae en la ventana actual
   (lunes semana anterior → ahora), para que el próximo briefing re-extraiga
   esas notas con el prompt nuevo.

Pensado para correr una sola vez después de cambiar el prompt del parser y
querer re-procesar las notas recientes.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

from db.client import get_cursor


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _window_start() -> datetime:
    """Lunes 00:00 de la semana anterior — misma lógica que briefing/builder.py."""
    tz = _tz()
    today_local = datetime.now(tz).date()
    days_since_monday = today_local.weekday()
    monday_this_week = today_local - timedelta(days=days_since_monday)
    monday_last_week = monday_this_week - timedelta(days=7)
    return datetime.combine(monday_last_week, datetime.min.time(), tzinfo=tz)


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    user_id = os.environ.get("USER_ID", "daniel")
    window_start = _window_start()

    print(f"User: {user_id}")
    print(f"Ventana de reset: notas con mtime >= {window_start.isoformat()}")
    print(f"Modo: {'DRY-RUN (no escribe)' if dry_run else 'EJECUTANDO'}")
    print()

    with get_cursor() as cur:
        cur.execute(
            "SELECT count(*) AS n FROM tasks WHERE user_id = %s AND status = 'open'",
            (user_id,),
        )
        open_count = cur.fetchone()["n"]
        print(f"Tareas open a marcar dropped: {open_count}")

        cur.execute(
            "SELECT count(*) AS n FROM processed_notes WHERE last_mtime >= %s",
            (window_start,),
        )
        notes_count = cur.fetchone()["n"]
        print(f"Filas processed_notes a borrar (mtime en ventana): {notes_count}")

        if dry_run:
            print("\nDry-run — no se modificó nada.")
            return 0

        cur.execute(
            """
            UPDATE tasks
            SET status = 'dropped', updated_at = now()
            WHERE user_id = %s AND status = 'open'
            """,
            (user_id,),
        )
        print(f"\n✓ {cur.rowcount} tareas marcadas dropped")

        cur.execute(
            "DELETE FROM processed_notes WHERE last_mtime >= %s",
            (window_start,),
        )
        print(f"✓ {cur.rowcount} filas borradas de processed_notes")

    print("\nListo. Mañana 8AM (o /briefing) re-extrae con el prompt nuevo.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
