"""Wrappers sobre Gmail API v1 — solo lectura.

REGLA: el bot NO envía correos, NO marca como leído, NO archiva. Solo lee
para detectar threads que requieren respuesta.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from auth.google_auth import get_gmail_service

log = logging.getLogger(__name__)


@dataclass
class PendingThread:
    """Thread donde el user es To/Cc y el último mensaje no es suyo."""
    thread_id: str
    last_msg_id: str
    subject: str
    from_addr: str             # remitente del último mensaje
    snippet: str               # snippet del último mensaje
    received_at: datetime      # timestamp del último mensaje (UTC)
    is_to: bool                # True si el user está en To, False si solo Cc


def _user_email() -> str:
    return os.environ.get("USER_EMAIL", "daniel@estudio-plural.co").lower()


def _header(payload: dict, name: str) -> str:
    name_lower = name.lower()
    for h in payload.get("headers", []) or []:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _parse_address_list(value: str) -> List[str]:
    """Extrae direcciones de campos To/Cc — devuelve solo emails en lowercase."""
    if not value:
        return []
    parts = []
    for raw in value.split(","):
        raw = raw.strip()
        if not raw:
            continue
        # Maneja "Name <email@x.com>"
        if "<" in raw and ">" in raw:
            email = raw[raw.index("<") + 1 : raw.rindex(">")]
        else:
            email = raw
        email = email.strip().lower()
        if email:
            parts.append(email)
    return parts


_IGNORED_FROM_DOMAINS = {
    # Invitaciones de Google Calendar: ya se manejan con botones RSVP del v1.
    "calendar-notification@google.com",
    "calendar-notification.google.com",
}

# Las invitaciones reenviadas por el organizador vienen con subject estándar
# de Google Calendar. Las filtramos porque el v1 ya las maneja vía RSVP buttons.
_INVITATION_SUBJECT_PREFIXES = (
    "invitación:",
    "invitación actualizada:",
    "invitation:",
    "updated invitation:",
    "canceled event:",
    "cancelado:",
)


def _is_calendar_invite_subject(subject: str) -> bool:
    s = (subject or "").strip().lower()
    return s.startswith(_INVITATION_SUBJECT_PREFIXES)


def list_pending_for_reply(days: int = 7, max_threads: int = 50) -> List[PendingThread]:
    """Lista threads donde:
    - El user (USER_EMAIL) está en To o Cc
    - El último mensaje del thread NO es del user
    - Recibidos dentro de los últimos `days` días
    - En INBOX (no archivados, no spam, no trash)
    - El From no es de un emisor que ya manejamos por otro flow (Calendar RSVP)

    Filtra heurísticamente — el LLM hace el filtro final de "realmente pide respuesta".
    """
    service = get_gmail_service()
    me = _user_email()

    # Query Gmail: en INBOX, recibidos en últimos N días, donde sos To/CC,
    # y el último mensaje no es tuyo (-from:me). Excluye también invitaciones
    # de Calendar (ya cubiertas por RSVP buttons del v1).
    after_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())
    query = (
        f"(to:me OR cc:me) -from:me in:inbox "
        f"-from:calendar-notification@google.com "
        f"after:{after_ts}"
    )

    resp = service.users().threads().list(
        userId="me",
        q=query,
        maxResults=max_threads,
    ).execute()
    threads = resp.get("threads", []) or []

    result: List[PendingThread] = []
    for t in threads:
        thread_id = t.get("id")
        if not thread_id:
            continue
        try:
            full = service.users().threads().get(
                userId="me",
                id=thread_id,
                format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "Date"],
            ).execute()
        except Exception:
            log.exception("No se pudo leer thread %s", thread_id)
            continue

        messages = full.get("messages", []) or []
        if not messages:
            continue
        last = messages[-1]
        payload = last.get("payload", {}) or {}
        from_addr_raw = _header(payload, "From")
        from_addrs = _parse_address_list(from_addr_raw)
        # Si el último mensaje es del propio user, skip (ya respondió).
        if any(me == addr or addr.endswith(me) for addr in from_addrs):
            continue
        # Defensa adicional client-side por si la query no atrapó algún variante.
        if any(addr in _IGNORED_FROM_DOMAINS for addr in from_addrs):
            continue
        # Invitaciones de Calendar reenviadas por el organizador humano —
        # ya las cubre el flow RSVP del v1.
        subject = _header(payload, "Subject")
        if _is_calendar_invite_subject(subject):
            continue

        to_list = _parse_address_list(_header(payload, "To"))
        cc_list = _parse_address_list(_header(payload, "Cc"))
        is_to = me in to_list
        is_cc = me in cc_list
        if not (is_to or is_cc):
            # Puede ser un BCC; aún así puede valer la pena. Lo dejamos pasar
            # para no perder cosas, pero marcamos como to=False.
            pass

        internal_ms = int(last.get("internalDate") or 0)
        received_at = datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc)

        result.append(PendingThread(
            thread_id=thread_id,
            last_msg_id=last.get("id", ""),
            subject=subject or "(sin asunto)",
            from_addr=from_addr_raw,
            snippet=(last.get("snippet") or "").strip(),
            received_at=received_at,
            is_to=is_to,
        ))

    # Más recientes primero.
    result.sort(key=lambda x: x.received_at, reverse=True)
    return result


def gmail_link(thread_id: str) -> str:
    """Link para abrir el thread en Gmail web."""
    return f"https://mail.google.com/mail/u/0/#inbox/{thread_id}"
