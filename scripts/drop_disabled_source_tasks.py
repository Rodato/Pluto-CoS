"""One-shot: marca como `dropped` las tareas open de fuentes desactivadas.

Uso:
    .venv/bin/python3 scripts/drop_disabled_source_tasks.py            # dry-run (no escribe)
    .venv/bin/python3 scripts/drop_disabled_source_tasks.py --apply    # ejecuta

Contexto:
Gmail (Fase 2) y Slack (Fase 3) se apagaron el 2026-06-02. Se cortó la ingesta
y el auto-cierre, pero las tareas que ya estaban `open` quedaron huérfanas en la
tabla `tasks` y reaparecían en el briefing cada mañana (el paso 2 de
`build_briefing` relee todas las open sin filtrar por fuente).

`briefing/builder.py` ya filtra esas fuentes en runtime (_ACTIVE_TASK_SOURCES),
así que dejan de colarse al briefing. Este script es la limpieza prolija del
estado en DB: marca esas filas como `dropped` (NO DELETE — preserva audit).

Por defecto es DRY-RUN: solo muestra qué tocaría. Pasá --apply para ejecutar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Permitir correr desde scripts/ — agregar root al path y cargar .env
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
load_dotenv(_ROOT / ".env")

from db.client import get_cursor  # noqa: E402

# Fuentes apagadas cuyas tareas open deben dropearse. Mantener en sync con
# briefing/builder.py (_ACTIVE_TASK_SOURCES): lo que NO está activo, va acá.
_DISABLED_SOURCES = ("gmail", "slack")


def main() -> int:
    apply = "--apply" in sys.argv
    user_id = os.environ.get("USER_ID", "daniel")

    print(f"User: {user_id}")
    print(f"Fuentes desactivadas a limpiar: {', '.join(_DISABLED_SOURCES)}")
    print(f"Modo: {'APPLY (escribe)' if apply else 'DRY-RUN (no escribe)'}")
    print()

    with get_cursor() as cur:
        # Desglose por fuente y proyecto para que se vea qué se va a tocar.
        cur.execute(
            """
            SELECT source, project, count(*) AS n
            FROM tasks
            WHERE user_id = %s AND status = 'open' AND source = ANY(%s)
            GROUP BY source, project
            ORDER BY source, n DESC
            """,
            (user_id, list(_DISABLED_SOURCES)),
        )
        rows = cur.fetchall()
        total = sum(r["n"] for r in rows)

        if not rows:
            print("No hay tareas open de fuentes desactivadas. Nada que limpiar.")
            return 0

        for r in rows:
            print(f"  {r['source']:>6} · {r['project'] or '(sin proyecto)':<20} → {r['n']}")
        print(f"\nTotal a marcar dropped: {total}")

        if not apply:
            print("\nDry-run — no se modificó nada. Corré con --apply para ejecutar.")
            return 0

        cur.execute(
            """
            UPDATE tasks
            SET status = 'dropped', updated_at = now()
            WHERE user_id = %s AND status = 'open' AND source = ANY(%s)
            """,
            (user_id, list(_DISABLED_SOURCES)),
        )
        print(f"\n✓ {cur.rowcount} tareas marcadas dropped")

    print("\nListo. El briefing ya no las verá (el guard en builder.py también las filtra).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
