"""Único punto de acceso a Neon (Postgres).

REGLA DURA: SQL siempre parametrizado con %s, nunca f-strings con datos externos.
"""

import os
from contextlib import contextmanager
from typing import Optional

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
