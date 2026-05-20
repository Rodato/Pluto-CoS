"""Extracción de tareas accionables desde el cuerpo completo de la nota.

Usa LLM (OpenRouter) para identificar tareas con contexto, no solo regex
sobre la sección "Próximos Pasos". El LLM ve el cuerpo completo y devuelve
JSON estructurado.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from llm.planner import _client, DEFAULT_MODEL

log = logging.getLogger(__name__)


@dataclass
class ExtractedTask:
    title: str
    context: str
    estimated_minutes: Optional[int]
    deadline_hint: Optional[str]
    source_note: str
    source: str = "granola"          # granola | gmail | slack | ...
    project: Optional[str] = None    # si None, el builder lo calcula del path


_SYSTEM_PROMPT = """Sos un asistente que extrae tareas accionables de notas de reuniones para Daniel (CTO de Estudio Plural).

Una "tarea accionable" es algo que DANIEL tiene que HACER. No es:
- Información general discutida
- Tareas que le tocan a otra persona (a menos que requieran follow-up de Daniel)
- Decisiones ya tomadas sin acción pendiente
- Datos contextuales o referencias

Una tarea SÍ es:
- Una respuesta que tiene que dar
- Un entregable que tiene que producir
- Una decisión que tiene que tomar
- Un seguimiento que tiene que hacer
- Una conversación que tiene que iniciar
- Algo que se comprometió a hacer

Formato de respuesta — JSON estricto sin texto adicional:
{
  "tasks": [
    {
      "title": "Verbo + objeto, máximo 80 chars. Ej: 'Responder a Aly sobre propuesta de timeline'",
      "context": "1-2 líneas del contexto de la reunión para que Daniel recuerde de qué venía",
      "estimated_minutes": null o entero (estimación realista, null si no es claro),
      "deadline_hint": null o string libre (ej. 'antes del viernes', 'esta semana', 'urgente')
    }
  ]
}

Si la nota no tiene tareas accionables, devolvé: {"tasks": []}

Respondé SIEMPRE en español rioplatense. Sé conciso."""


def extract_tasks(note_body: str, note_path: str, note_title: Optional[str] = None) -> List[ExtractedTask]:
    """Llama al LLM para extraer tareas del cuerpo completo de la nota."""
    if not note_body.strip():
        return []

    user_content = f"Nota: {note_title or note_path}\n\n{note_body}"

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
        log.exception("LLM falló extrayendo tareas de %s", note_path)
        return []

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("LLM devolvió JSON inválido para %s: %r", note_path, content[:200])
        return []

    raw_tasks = payload.get("tasks") or []
    result: List[ExtractedTask] = []
    for t in raw_tasks:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        result.append(
            ExtractedTask(
                title=title[:200],
                context=(t.get("context") or "").strip()[:500],
                estimated_minutes=_safe_int(t.get("estimated_minutes")),
                deadline_hint=_safe_str(t.get("deadline_hint")),
                source_note=note_path,
            )
        )

    return result


def _safe_int(v) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s[:200] if s else None
