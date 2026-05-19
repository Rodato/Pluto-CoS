"""Consultas en lenguaje natural sobre la agenda.

Usa tool calling: el LLM elige entre `list_events_in_range` y `find_free_slots_in_range`,
ejecuta, y compone una respuesta natural en español.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from calendar_api import client as cal_client
from calendar_api import slots as cal_slots
from llm.planner import _client, DEFAULT_MODEL


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_events_in_range",
            "description": (
                "Lista los eventos del usuario entre dos fechas ISO 8601. "
                "Útil para preguntas como '¿qué tengo hoy?', '¿qué tengo el martes?', "
                "'¿cómo viene la semana?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "ISO 8601 con timezone, p.ej. 2026-05-12T00:00:00-05:00"},
                    "end": {"type": "string", "description": "ISO 8601 con timezone"},
                },
                "required": ["start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_free_slots_in_range",
            "description": (
                "Devuelve los huecos libres del usuario en horario laboral "
                "(L-V 9am-6pm America/Bogota) dentro de los próximos N días. "
                "Útil para 'cuándo tengo libre', '¿puedo el viernes?', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "horizon_days": {"type": "integer", "description": "Cuántos días hacia adelante mirar (1-14)"},
                    "min_minutes": {"type": "integer", "description": "Duración mínima del slot en minutos (default 30)"},
                },
                "required": ["horizon_days"],
            },
        },
    },
]


def _exec_tool(name: str, args: dict) -> str:
    """Ejecuta la tool y devuelve su resultado serializado para el LLM."""
    if name == "list_events_in_range":
        start = datetime.fromisoformat(args["start"])
        end = datetime.fromisoformat(args["end"])
        events = cal_client.list_events(start, end)
        return json.dumps([_event_summary(e) for e in events], ensure_ascii=False)

    if name == "find_free_slots_in_range":
        horizon = int(args.get("horizon_days", 7))
        min_minutes = int(args.get("min_minutes", 30))
        tz = _tz()
        now = datetime.now(tz)
        events = cal_client.list_events(now, now + timedelta(days=horizon))
        slots = cal_slots.find_free_slots(
            events, horizon_days=horizon, min_slot_minutes=min_minutes
        )
        return json.dumps(
            [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "duration_minutes": s.duration_minutes,
                }
                for s in slots
            ],
            ensure_ascii=False,
        )

    return json.dumps({"error": f"tool desconocida: {name}"})


def _event_summary(event: dict) -> dict:
    return {
        "id": event.get("id"),
        "summary": event.get("summary"),
        "start": event.get("start"),
        "end": event.get("end"),
        "location": event.get("location"),
        "organizer": (event.get("organizer") or {}).get("displayName")
        or (event.get("organizer") or {}).get("email"),
        "attendees_count": len(event.get("attendees") or []),
        "my_response": _my_response(event),
    }


def _my_response(event: dict) -> Optional[str]:
    for a in event.get("attendees") or []:
        if a.get("self"):
            return a.get("responseStatus")
    return None


def answer_query(question: str) -> str:
    """Resuelve una consulta en lenguaje natural sobre la agenda."""
    tz = _tz()
    now = datetime.now(tz)
    system = (
        "Sos un asistente de calendario para Daniel (single-user). Respondé en español "
        "rioplatense, conciso. Usá las tools para consultar agenda real — nunca inventes "
        "eventos ni horas. Si la pregunta requiere fechas, resolvelas a ISO 8601 con "
        f"timezone America/Bogota. Ahora es {now.isoformat()}. Trabajamos en horario "
        "laboral L-V 9am-6pm. Si no hay eventos o slots, decilo directamente."
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]

    # Loop de tool calls (máx 4 iteraciones)
    for _ in range(4):
        resp = _client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=messages,
            tools=TOOLS,
        )
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or "(sin respuesta)"

        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
                result = _exec_tool(tc.function.name, args)
            except Exception as exc:
                result = json.dumps({"error": str(exc)})
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

    return "No pude resolver la consulta tras varios intentos."
