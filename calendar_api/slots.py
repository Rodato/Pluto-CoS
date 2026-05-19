"""Detección de huecos libres en la agenda.

Recibe lista de eventos de Google Calendar y devuelve slots libres dentro
del horario laboral (default L-V 9am-6pm America/Bogota).
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

import os


@dataclass
class FreeSlot:
    start: datetime
    end: datetime

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _parse_event_dt(event: dict, key: str) -> Optional[datetime]:
    """Parsea event['start']/'end']. Eventos all-day usan 'date', timed usan 'dateTime'."""
    node = event.get(key, {})
    if "dateTime" in node:
        dt = datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(_tz())
    if "date" in node:
        d = date.fromisoformat(node["date"])
        return datetime.combine(d, time(0, 0), tzinfo=_tz())
    return None


def find_free_slots(
    events: List[dict],
    horizon_days: int = 7,
    work_start: time = time(9, 0),
    work_end: time = time(18, 0),
    min_slot_minutes: int = 30,
    from_dt: Optional[datetime] = None,
    skip_weekends: bool = True,
) -> List[FreeSlot]:
    """Calcula slots libres en horario laboral para los próximos `horizon_days`.

    Algoritmo: para cada día laboral, partir el bloque (work_start → work_end),
    restar los eventos confirmados, y devolver los huecos >= min_slot_minutes.
    """
    tz = _tz()
    now = (from_dt or datetime.now(tz)).astimezone(tz)

    # Eventos como (start, end) ordenados, ignorando los que no tienen tiempo
    busy: List[tuple[datetime, datetime]] = []
    for event in events:
        # Ignorar declinados por el usuario
        if event.get("status") == "cancelled":
            continue
        # Marcar como busy solo si NO declinamos
        declined = False
        for attendee in event.get("attendees", []) or []:
            if attendee.get("self") and attendee.get("responseStatus") == "declined":
                declined = True
                break
        if declined:
            continue

        s = _parse_event_dt(event, "start")
        e = _parse_event_dt(event, "end")
        if s and e:
            busy.append((s, e))
    busy.sort()

    free: List[FreeSlot] = []
    min_delta = timedelta(minutes=min_slot_minutes)

    for offset in range(horizon_days):
        day = (now + timedelta(days=offset)).date()
        if skip_weekends and day.weekday() >= 5:  # 5=Sat, 6=Sun
            continue

        day_start = datetime.combine(day, work_start, tzinfo=tz)
        day_end = datetime.combine(day, work_end, tzinfo=tz)

        # Para el día actual, no proponer slots en el pasado
        cursor = max(day_start, now) if offset == 0 else day_start
        if cursor >= day_end:
            continue

        # Eventos que tocan este día laboral
        day_busy = [
            (max(s, day_start), min(e, day_end))
            for s, e in busy
            if e > day_start and s < day_end
        ]
        day_busy.sort()

        for b_start, b_end in day_busy:
            if b_start - cursor >= min_delta:
                free.append(FreeSlot(start=cursor, end=b_start))
            if b_end > cursor:
                cursor = b_end

        if day_end - cursor >= min_delta:
            free.append(FreeSlot(start=cursor, end=day_end))

    return free
