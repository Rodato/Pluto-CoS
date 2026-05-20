"""Único punto de acceso a Neon (Postgres).

REGLA DURA: SQL siempre parametrizado con %s, nunca f-strings con datos externos.
"""

import os
from contextlib import contextmanager
from datetime import datetime
from typing import Dict, List, Optional

import psycopg2
from psycopg2.extras import RealDictCursor


@contextmanager
def get_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_cursor():
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        try:
            yield cur
        finally:
            cur.close()


# ============================================================
# seen_invitations
# ============================================================

def is_invitation_seen(event_id: str) -> bool:
    """True si ya notificamos esta invitación en algún scan anterior."""
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM seen_invitations WHERE event_id = %s", (event_id,))
        return cur.fetchone() is not None


def mark_invitation_seen(
    event_id: str,
    user_id: str,
    telegram_message_id: Optional[int] = None,
) -> None:
    """Marca una invitación como notificada (idempotente)."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seen_invitations (event_id, user_id, telegram_message_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING
            """,
            (event_id, user_id, telegram_message_id),
        )


def set_invitation_rsvp(event_id: str, rsvp_status: str) -> None:
    """Registra la respuesta RSVP del usuario (accepted/declined/tentative)."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE seen_invitations
            SET rsvp_status = %s, rsvp_at = now()
            WHERE event_id = %s
            """,
            (rsvp_status, event_id),
        )


# ============================================================
# processed_notes (Fase 1 CoS — Granola dedup)
# ============================================================

def get_processed_notes() -> Dict[str, datetime]:
    """Devuelve {note_path: last_mtime} para skipear notas no modificadas."""
    with get_cursor() as cur:
        cur.execute("SELECT note_path, last_mtime FROM processed_notes")
        return {row["note_path"]: row["last_mtime"] for row in cur.fetchall()}


def mark_note_processed(note_path: str, last_mtime: datetime, tasks_extracted: int) -> None:
    """Upsert: registra que esta nota fue procesada con su mtime + cuántas tareas salieron."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO processed_notes (note_path, last_mtime, tasks_extracted)
            VALUES (%s, %s, %s)
            ON CONFLICT (note_path) DO UPDATE SET
                last_mtime = EXCLUDED.last_mtime,
                last_processed_at = now(),
                tasks_extracted = EXCLUDED.tasks_extracted
            """,
            (note_path, last_mtime, tasks_extracted),
        )


# ============================================================
# tasks (Fase 1 CoS — items priorizados P0–P3)
# ============================================================

def insert_task(
    user_id: str,
    title: str,
    priority: str,
    source: str,
    description: Optional[str] = None,
    source_ref: Optional[str] = None,
    project: Optional[str] = None,
    due_date: Optional[datetime] = None,
    briefing_date: Optional[datetime] = None,
) -> str:
    """Inserta una tarea/compromiso. Devuelve el id (UUID) generado."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO tasks
                (user_id, title, description, priority, project, source, source_ref, due_date, briefing_date)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (user_id, title, description, priority, project, source, source_ref, due_date, briefing_date),
        )
        return str(cur.fetchone()["id"])


def task_exists_for_source(source: str, source_ref: str) -> bool:
    """True si ya existe una tarea abierta para esta fuente (anti-duplicado entre briefings)."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM tasks
            WHERE source = %s AND source_ref = %s AND status = 'open'
            LIMIT 1
            """,
            (source, source_ref),
        )
        return cur.fetchone() is not None


def list_open_tasks(user_id: str) -> List[dict]:
    """Devuelve todas las tareas abiertas (no done/snoozed/dropped) de un usuario."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, description, priority, project, source, source_ref,
                   due_date, briefing_date, created_at
            FROM tasks
            WHERE user_id = %s AND status = 'open'
            ORDER BY created_at ASC
            """,
            (user_id,),
        )
        return [dict(row) for row in cur.fetchall()]


def update_task_priority(task_id: str, priority: str) -> None:
    """Actualiza priority + updated_at de una tarea (usado en repriorización diaria)."""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE tasks
            SET priority = %s, updated_at = now()
            WHERE id = %s
            """,
            (priority, task_id),
        )
