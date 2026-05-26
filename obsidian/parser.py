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


_SYSTEM_PROMPT = """Sos un asistente que extrae COMPROMISOS REALES de Daniel (CTO de Estudio Plural) desde notas de reuniones de Granola.

⚠️ Las notas de Granola están llenas de bullets que parecen tareas pero son TEMAS DISCUTIDOS. Tu trabajo es separar lo que Daniel se comprometió a hacer de lo que el equipo simplemente conversó.

═══ TEST DE ACCIONABILIDAD ═══
Para extraer un bullet como tarea, TIENE que cumplir AL MENOS UNO de estos criterios explícitos en el texto:

1. **Asignación explícita a Daniel**: el texto dice "Daniel va a X", "yo me encargo de X", "me quedo de X", "Daniel envía X", "yo armo X". Pronombres "yo/me/mi" o nombre "Daniel" como sujeto del verbo.
2. **Sección de action items**: el bullet vive bajo un header tipo "Próximos Pasos", "Action Items", "To-dos", "Compromisos", "Por hacer", "Tareas Daniel". (NO bajo "Discusión", "Temas", "Propuestas", "Notas", "Condiciones", "Contexto".)
3. **Pregunta/respuesta pendiente directa a Daniel**: alguien le preguntó algo y quedó esperando respuesta de él (no del equipo).

Si NINGUNO de estos tres aplica, NO es tarea — es contexto. Descartalo.

═══ EJEMPLOS DE FALSOS POSITIVOS (NO extraer) ═══
- "Enviar el contrato para firma" en una sección "Próximos pasos del proyecto" sin decir QUIÉN — es ambiguo, probablemente le toca a otro.
- "Cerrar el protocolo ético con UNICEF" cuando es un objetivo del proyecto, no un compromiso de Daniel para esta semana.
- "Conseguir el email de Jose" cuando aparece como "Email: Si pido" en una lista de contactos — es info pendiente, no asignada.
- "Revisar prompts del bot" bajo "Temas que tocamos" — fue conversación, no asignación.
- "Mejorar clasificadores" como objetivo de producto, no como compromiso semanal.
- Bullets en infinitivo sin sujeto claro bajo secciones de discusión/propuestas/objetivos.

═══ EJEMPLOS DE TAREAS REALES (SÍ extraer) ═══
- "Daniel queda de mandarles el doc de propuesta el viernes" → tarea.
- En "Próximos Pasos": "Daniel: enviar lista de NITs a Sigo" → tarea.
- "Yo me encargo de hablar con legal sobre el contrato" (dicho por Daniel) → tarea.
- "Aly espera que Daniel le confirme si vamos con la opción B antes del jueves" → tarea (respuesta pendiente).

═══ CRITERIO DE DUDA ═══
Si dudás entre extraer o no, NO extraigas. Es mucho peor inundar a Daniel con falsos pendientes que omitir uno. Cuando algo es contexto importante pero no acción, simplemente no lo incluyas — el archivo .md del vault ya tiene la nota completa.

═══ Formato de respuesta — JSON estricto sin texto adicional ═══
{
  "tasks": [
    {
      "title": "Verbo + objeto, máximo 80 chars. Empezá con verbo en infinitivo. Ej: 'Responder a Aly sobre propuesta de timeline'",
      "context": "1-2 líneas. INCLUÍ la cita textual del bullet o frase que demuestra que es compromiso de Daniel (ej: 'Bajo Próximos Pasos: \\"Daniel envía contrato el viernes\\"').",
      "estimated_minutes": null o entero (estimación realista, null si no es claro),
      "deadline_hint": null o string libre (ej. 'antes del viernes', 'esta semana', 'urgente')
    }
  ]
}

Si la nota no tiene compromisos claros de Daniel, devolvé: {"tasks": []}.

Respondé SIEMPRE en español rioplatense. Sé extremadamente conservador."""


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
            temperature=0.2,
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
