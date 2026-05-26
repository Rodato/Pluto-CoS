"""Clasifica tareas extraídas en P0–P3 con el criterio CTO de Daniel.

Una sola llamada al LLM por briefing — recibe todas las tareas y devuelve
todas priorizadas, así no quemamos N llamadas para N tareas.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from llm.planner import _client, DEFAULT_MODEL
from obsidian.parser import ExtractedTask

log = logging.getLogger(__name__)


@dataclass
class PrioritizedTask:
    title: str
    priority: str
    rationale: str
    source_note: str
    context: str
    estimated_minutes: Optional[int]
    deadline_hint: Optional[str]
    task_id: Optional[str] = None  # set cuando viene de DB
    project: Optional[str] = None  # nombre de la carpeta Granola/<project>/
    source: Optional[str] = None   # gmail | slack | granola — canal de origen


_VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}

_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Clasificás tareas en cuatro niveles de prioridad:

🔴 P0 — Bloquea a alguien HOY
- PR review crítico, decisión técnica que para a un dev, incidente activo, respuesta a cliente esperando, compromiso externo del día

🟠 P1 — Esta semana, compromiso externo o de cliente
- Entrega prometida, demo, milestone con cliente, respuesta a un stakeholder externo

🟡 P2 — Esta semana, foco CTO (mayor leverage)
- Arquitectura, hiring, mentoring del equipo, deuda técnica crítica, decisiones de producto

🟢 P3 — Medio plazo, burocracia + admin
- Vendor mgmt, papeleo, tooling no urgente, tareas de mantenimiento

Recibís una lista de tareas con contexto. Tu trabajo:
1. Asignar P0/P1/P2/P3 a cada una basándote en deadline_hint, context y la naturaleza de la tarea.
2. Escribir una rationale CORTA (1 línea, máximo 100 chars) que explique por qué.
3. Si una tarea ya no tiene sentido (es vieja, ya pasó, ya no aplica), marcala como P3 y aclaralo en la rationale.

IMPORTANTE: respondé SIEMPRE en español rioplatense. JSON estricto sin texto adicional. Mantené el id que te paso para cada tarea.

Formato:
{
  "items": [
    {"id": 0, "priority": "P0|P1|P2|P3", "rationale": "1 línea explicando por qué"}
  ]
}"""


def prioritize(tasks: List[ExtractedTask], today_iso: str) -> List[PrioritizedTask]:
    """Llama al LLM una sola vez con todas las tareas y devuelve cada una priorizada."""
    if not tasks:
        return []

    items_for_llm = [
        {
            "id": i,
            "title": t.title,
            "context": t.context,
            "deadline_hint": t.deadline_hint,
            "estimated_minutes": t.estimated_minutes,
            "source_note": t.source_note,
        }
        for i, t in enumerate(tasks)
    ]
    user_content = (
        f"Hoy es {today_iso}. Clasificá estas tareas:\n\n"
        f"{json.dumps(items_for_llm, ensure_ascii=False, indent=2)}"
    )

    try:
        resp = _client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        log.exception("LLM falló priorizando %d tareas", len(tasks))
        return _fallback(tasks, "P2", "LLM falló — default P2")

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("Prioritizer devolvió JSON inválido: %r", content[:200])
        return _fallback(tasks, "P2", "JSON inválido — default P2")

    by_id = {}
    for item in payload.get("items") or []:
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        priority = (item.get("priority") or "").upper().strip()
        if priority not in _VALID_PRIORITIES:
            priority = "P2"
        by_id[tid] = (priority, (item.get("rationale") or "").strip()[:200])

    result: List[PrioritizedTask] = []
    for i, t in enumerate(tasks):
        priority, rationale = by_id.get(i, ("P2", "Sin clasificar — default P2"))
        result.append(
            PrioritizedTask(
                title=t.title,
                priority=priority,
                rationale=rationale,
                source_note=t.source_note,
                context=t.context,
                estimated_minutes=t.estimated_minutes,
                deadline_hint=t.deadline_hint,
                project=t.project,
                source=t.source,
            )
        )
    return result


def _fallback(tasks: List[ExtractedTask], priority: str, rationale: str) -> List[PrioritizedTask]:
    return [
        PrioritizedTask(
            title=t.title,
            priority=priority,
            rationale=rationale,
            source_note=t.source_note,
            context=t.context,
            estimated_minutes=t.estimated_minutes,
            deadline_hint=t.deadline_hint,
            project=t.project,
            source=t.source,
        )
        for t in tasks
    ]


def prioritize_open_tasks(open_tasks: List[dict], today_iso: str) -> List[PrioritizedTask]:
    """Repriorización diaria — recibe filas de `tasks` (Neon) y las reordena según hoy.

    El LLM ve todas las tareas abiertas y reasigna P0/P1/P2/P3 con la mirada del día.
    Una tarea que ayer era P1 puede pasar a P0 si su deadline se acercó, o a P3 si quedó stale.
    """
    if not open_tasks:
        return []

    items_for_llm = [
        {
            "id": i,
            "title": t.get("title"),
            "context": t.get("description") or "",
            "current_priority": t.get("priority"),
            "source": t.get("source"),
            "created_at": t.get("created_at").isoformat() if t.get("created_at") else None,
            "due_date": t.get("due_date").isoformat() if t.get("due_date") else None,
        }
        for i, t in enumerate(open_tasks)
    ]
    user_content = (
        f"Hoy es {today_iso}. Repriorizá estas tareas abiertas según la mirada de hoy.\n"
        f"`current_priority` es la priority con la que se guardó originalmente — "
        f"podés mantenerla, subirla o bajarla según el contexto temporal.\n\n"
        f"{json.dumps(items_for_llm, ensure_ascii=False, indent=2)}"
    )

    try:
        resp = _client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
    except Exception:
        log.exception("LLM falló repriorizando %d open tasks", len(open_tasks))
        return _fallback_from_db(open_tasks, "Sin repriorizar — LLM falló")

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("Reprioritizer devolvió JSON inválido: %r", content[:200])
        return _fallback_from_db(open_tasks, "Sin repriorizar — JSON inválido")

    by_id = {}
    for item in payload.get("items") or []:
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        priority = (item.get("priority") or "").upper().strip()
        if priority not in _VALID_PRIORITIES:
            priority = (open_tasks[tid].get("priority") if 0 <= tid < len(open_tasks) else "P2")
        by_id[tid] = (priority, (item.get("rationale") or "").strip()[:200])

    result: List[PrioritizedTask] = []
    for i, t in enumerate(open_tasks):
        priority, rationale = by_id.get(
            i, (t.get("priority", "P2"), "Sin clasificar — mantengo priority original")
        )
        result.append(
            PrioritizedTask(
                title=t.get("title", ""),
                priority=priority,
                rationale=rationale,
                source_note=t.get("source_ref") or "",
                context=t.get("description") or "",
                estimated_minutes=None,
                deadline_hint=None,
                task_id=str(t.get("id")) if t.get("id") else None,
                project=t.get("project") or "Varios",
                source=t.get("source"),
            )
        )
    return result


def _fallback_from_db(open_tasks: List[dict], rationale: str) -> List[PrioritizedTask]:
    return [
        PrioritizedTask(
            title=t.get("title", ""),
            priority=t.get("priority", "P2"),
            rationale=rationale,
            source_note=t.get("source_ref") or "",
            context=t.get("description") or "",
            estimated_minutes=None,
            deadline_hint=None,
            task_id=str(t.get("id")) if t.get("id") else None,
            project=t.get("project") or "Varios",
            source=t.get("source"),
        )
        for t in open_tasks
    ]
