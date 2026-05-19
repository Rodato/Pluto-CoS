"""Wrappers sobre Google Calendar API v3.

Reglas v1:
- Solo LEE eventos.
- La única escritura permitida es RSVP: events.patch modificando exclusivamente
  attendees[me].responseStatus.
- NO crea, NO edita campos del evento, NO borra.
"""

import os
from datetime import datetime
from typing import List, Optional

from auth.google_auth import get_calendar_service

RSVP_VALID = {"accepted", "declined", "tentative"}


def _user_email() -> str:
    """Email del usuario autorizado (para identificar attendees[me])."""
    return os.environ.get("USER_EMAIL", "daniel@estudio-plural.co")


def _to_rfc3339(dt: datetime) -> str:
    """Google Calendar acepta RFC3339; si dt es naive, asumir UTC."""
    if dt.tzinfo is None:
        return dt.isoformat() + "Z"
    return dt.isoformat()


def list_events(
    time_min: datetime,
    time_max: datetime,
    max_results: int = 100,
) -> List[dict]:
    """Lista eventos del calendar primario en el rango dado, ordenados por hora."""
    service = get_calendar_service()
    resp = service.events().list(
        calendarId="primary",
        timeMin=_to_rfc3339(time_min),
        timeMax=_to_rfc3339(time_max),
        singleEvents=True,
        orderBy="startTime",
        maxResults=max_results,
    ).execute()
    return resp.get("items", [])


def list_pending_invitations(
    time_min: datetime,
    time_max: datetime,
) -> List[dict]:
    """Eventos en los que el usuario es attendee con responseStatus=needsAction.

    Usamos `list_events` y filtramos client-side porque la API no expone un
    filtro directo por responseStatus.
    """
    me = _user_email().lower()
    pending: List[dict] = []
    for event in list_events(time_min, time_max):
        for attendee in event.get("attendees", []) or []:
            if attendee.get("self") or attendee.get("email", "").lower() == me:
                if attendee.get("responseStatus") == "needsAction":
                    pending.append(event)
                break
    return pending


def get_event(event_id: str) -> dict:
    service = get_calendar_service()
    return service.events().get(calendarId="primary", eventId=event_id).execute()


def respond_invitation(event_id: str, response: str) -> dict:
    """RSVP a una invitación.

    response: 'accepted' | 'declined' | 'tentative'

    Implementación: PATCH sobre attendees, modificando SOLO el responseStatus
    del usuario actual. No tocamos ningún otro campo del evento.
    """
    if response not in RSVP_VALID:
        raise ValueError(f"response inválido: {response!r} (debe ser {RSVP_VALID})")

    service = get_calendar_service()
    event = service.events().get(calendarId="primary", eventId=event_id).execute()
    me = _user_email().lower()

    attendees = event.get("attendees", []) or []
    updated = False
    for attendee in attendees:
        if attendee.get("self") or attendee.get("email", "").lower() == me:
            attendee["responseStatus"] = response
            updated = True
            break

    if not updated:
        raise RuntimeError(
            f"No se encontró al usuario {me} en attendees del evento {event_id}"
        )

    return service.events().patch(
        calendarId="primary",
        eventId=event_id,
        body={"attendees": attendees},
        sendUpdates="all",
    ).execute()


# ============================================================
# v2 — bloqueado por regla dura del CLAUDE.md
# ============================================================
# def create_event(...): se implementará cuando entremos a v2 con el vault.
