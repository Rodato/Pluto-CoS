"""Watcher de cambios en Calendar — snapshots y diff.

El cron de 15 min guarda un snapshot por evento donde el usuario está citado
(tabla tracked_events) y en cada corrida compara contra lo que devuelve la API
para detectar reprogramaciones, cancelaciones y cambios de invitados/lugar/
descripción. Solo lectura: nunca toca el evento.
"""

import hashlib
import html
import json
from datetime import datetime, timezone
from typing import List, Optional

from calendar_api.client import _user_email


def is_relevant(event: dict) -> bool:
    """True si el evento es una cita de terceros: el usuario figura en attendees
    y NO es el organizador (sus propios cambios no se auto-notifican)."""
    if (event.get("organizer") or {}).get("self"):
        return False
    me = _user_email().lower()
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self") or attendee.get("email", "").lower() == me:
            return True
    return False


def my_response_status(event: dict) -> str:
    me = _user_email().lower()
    for attendee in event.get("attendees", []) or []:
        if attendee.get("self") or attendee.get("email", "").lower() == me:
            return attendee.get("responseStatus") or "needsAction"
    return "needsAction"


def _attendee_emails(event: dict) -> List[str]:
    """Emails de invitados (sin salas/recursos), ordenados para comparar."""
    emails = [
        a.get("email", "").lower()
        for a in event.get("attendees", []) or []
        if not a.get("resource") and a.get("email")
    ]
    return sorted(set(emails))


def _parse_when(raw: dict) -> Optional[datetime]:
    """Convierte event.start/end ({'dateTime'} o {'date'}) a datetime UTC."""
    if "dateTime" in raw:
        try:
            return datetime.fromisoformat(raw["dateTime"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            return None
    if "date" in raw:
        try:
            return datetime.fromisoformat(raw["date"]).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def snapshot_from_event(event: dict) -> dict:
    """Normaliza el evento a la fila que persiste en tracked_events."""
    start = event.get("start") or {}
    end = event.get("end") or {}
    description = (event.get("description") or "").strip()
    return {
        "event_id": event["id"],
        "recurring_event_id": event.get("recurringEventId"),
        "summary": (event.get("summary") or "").strip(),
        "start_raw": json.dumps(start, sort_keys=True),
        "end_raw": json.dumps(end, sort_keys=True),
        "start_ts": _parse_when(start),
        "end_ts": _parse_when(end),
        "location": (event.get("location") or "").strip(),
        "description_hash": hashlib.md5(description.encode()).hexdigest() if description else "",
        "attendees": json.dumps(_attendee_emails(event)),
        "status": event.get("status") or "confirmed",
    }


def created_recently(event: dict, hours: int = 48) -> bool:
    """True si el evento fue creado hace menos de `hours` (anti-spam al bootstrap)."""
    created = event.get("created")
    if not created:
        return False
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - created_dt).total_seconds() < hours * 3600


def diff_snapshots(old: dict, new: dict, event: dict, fmt_dt) -> List[str]:
    """Compara snapshot viejo vs nuevo y devuelve líneas HTML describiendo cambios.

    `fmt_dt` es telegram_bot.bot._fmt_dt (se inyecta para no importar telegram acá).
    """
    changes: List[str] = []

    if old["start_raw"] != new["start_raw"] or old["end_raw"] != new["end_raw"]:
        old_start = fmt_dt(json.loads(old["start_raw"]))
        old_end = fmt_dt(json.loads(old["end_raw"]))
        new_start = fmt_dt(json.loads(new["start_raw"]))
        new_end = fmt_dt(json.loads(new["end_raw"]))
        changes.append(
            f"🗓 <b>Nueva fecha/hora</b>\n"
            f"   Antes: {html.escape(old_start)} → {html.escape(old_end)}\n"
            f"   Ahora: {html.escape(new_start)} → {html.escape(new_end)}"
        )

    if old["summary"] != new["summary"]:
        changes.append(
            f"✏️ Título: «{html.escape(old['summary'] or '(sin título)')}» → "
            f"«{html.escape(new['summary'] or '(sin título)')}»"
        )

    if old["location"] != new["location"]:
        if new["location"]:
            changes.append(f"📍 Nuevo lugar: {html.escape(new['location'][:200])}")
        else:
            changes.append("📍 Quitaron el lugar")

    old_attendees = set(json.loads(old["attendees"]))
    new_attendees = set(json.loads(new["attendees"]))
    added = sorted(new_attendees - old_attendees)
    removed = sorted(old_attendees - new_attendees)
    if added:
        changes.append(f"👥 Se sumaron: {html.escape(', '.join(added))}")
    if removed:
        changes.append(f"👥 Salieron: {html.escape(', '.join(removed))}")

    if old["description_hash"] != new["description_hash"]:
        description = (event.get("description") or "").strip()
        if description:
            snippet = description[:300] + ("…" if len(description) > 300 else "")
            changes.append(f"📝 Mensaje actualizado:\n<i>{html.escape(snippet)}</i>")
        else:
            changes.append("📝 Quitaron el mensaje/descripción")

    return changes
