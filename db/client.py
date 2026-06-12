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


def get_event_id_by_message_id(telegram_message_id: int) -> Optional[str]:
    """Resuelve el event_id de Google Calendar desde el message_id de Telegram.

    Usado por el callback RSVP: el callback_data no puede llevar el event_id
    (excede el límite de 64 bytes en invitaciones recurrentes), así que
    lo recuperamos de la DB.
    """
    with get_cursor() as cur:
        cur.execute(
            "SELECT event_id FROM seen_invitations WHERE telegram_message_id = %s",
            (telegram_message_id,),
        )
        row = cur.fetchone()
        return row["event_id"] if row else None


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


def update_invitation_message_id(event_id: str, user_id: str, telegram_message_id: int) -> None:
    """Apunta el mapping message_id→event_id al mensaje más reciente.

    Usado cuando una reprogramación re-abre el RSVP: el aviso de cambio lleva
    botones nuevos y el callback debe resolver el event_id desde ese mensaje.
    """
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO seen_invitations (event_id, user_id, telegram_message_id)
            VALUES (%s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET telegram_message_id = EXCLUDED.telegram_message_id
            """,
            (event_id, user_id, telegram_message_id),
        )


# ============================================================
# tracked_events (watcher de cambios en Calendar)
# ============================================================

def get_tracked_events(user_id: str) -> Dict[str, dict]:
    """Devuelve {event_id: snapshot} de todos los eventos rastreados."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT event_id, recurring_event_id, summary, start_raw, end_raw,
                   start_ts, end_ts, location, description_hash, attendees, status
            FROM tracked_events
            WHERE user_id = %s
            """,
            (user_id,),
        )
        return {row["event_id"]: dict(row) for row in cur.fetchall()}


def upsert_tracked_event(user_id: str, snap: dict) -> None:
    """Inserta o actualiza el snapshot de un evento (idempotente)."""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO tracked_events
                (event_id, user_id, recurring_event_id, summary, start_raw, end_raw,
                 start_ts, end_ts, location, description_hash, attendees, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO UPDATE SET
                recurring_event_id = EXCLUDED.recurring_event_id,
                summary = EXCLUDED.summary,
                start_raw = EXCLUDED.start_raw,
                end_raw = EXCLUDED.end_raw,
                start_ts = EXCLUDED.start_ts,
                end_ts = EXCLUDED.end_ts,
                location = EXCLUDED.location,
                description_hash = EXCLUDED.description_hash,
                attendees = EXCLUDED.attendees,
                status = EXCLUDED.status,
                last_seen_at = now()
            """,
            (
                snap["event_id"], user_id, snap.get("recurring_event_id"),
                snap["summary"], snap["start_raw"], snap["end_raw"],
                snap.get("start_ts"), snap.get("end_ts"),
                snap["location"], snap["description_hash"], snap["attendees"],
                snap["status"],
            ),
        )


def delete_tracked_event(event_id: str) -> None:
    """Saca un evento del snapshot (cancelado o el usuario fue removido)."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM tracked_events WHERE event_id = %s", (event_id,))


def purge_past_tracked_events() -> int:
    """Borra snapshots de eventos que terminaron hace más de un día."""
    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM tracked_events WHERE end_ts IS NOT NULL AND end_ts < now() - interval '1 day'"
        )
        return cur.rowcount


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


def list_open_tasks_by_source(user_id: str, source: str) -> List[dict]:
    """Tasks abiertas filtradas por source (gmail/slack/granola). Incluye source_ref."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, source_ref
            FROM tasks
            WHERE user_id = %s AND status = 'open' AND source = %s
            """,
            (user_id, source),
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


def update_task_status(task_id: str, status: str) -> None:
    """Marca una tarea como done/snoozed/dropped. Setea completed_at si done."""
    if status not in {"open", "done", "snoozed", "dropped"}:
        raise ValueError(f"status inválido: {status!r}")
    completed_at = "now()" if status == "done" else "NULL"
    with get_cursor() as cur:
        cur.execute(
            f"""
            UPDATE tasks
            SET status = %s, updated_at = now(), completed_at = {completed_at}
            WHERE id = %s
            """,
            (status, task_id),
        )


def get_task(task_id: str) -> Optional[dict]:
    """Lee una tarea por id."""
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, title, priority, status, project, source
            FROM tasks
            WHERE id = %s
            """,
            (task_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
