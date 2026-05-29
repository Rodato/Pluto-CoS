"""Filtro LLM para mensajes de Slack — ¿este mensaje pide respuesta de Daniel?

La heurística (DMs + menciones) deja pasar ruido: emojis, "ok", confirmaciones
sin acción, mensajes automatizados, etc. El LLM decide cuáles son accionables.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from llm.planner import _client, DEFAULT_MODEL
from slack_api.client import PendingSlackMessage

log = logging.getLogger(__name__)


@dataclass
class ActionableSlackMessage:
    channel_id: str
    channel_name: str
    author_name: str
    text: str
    permalink: str
    reason: str
    suggested_action: str


_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Filtrás mensajes de Slack para decidir cuáles REALMENTE piden una acción/respuesta de Daniel.

⚠️ REGLAS DE DIRECCIÓN (quién debe hacer qué):
- Si el remitente dice "necesito X" → determinar QUIÉN necesita qué:
  • "necesito dos minutos contigo" → el remitente necesita tiempo DE Daniel → accionable para Daniel
  • "necesito terminar X antes de la reunión" → el remitente habla de SU pendiente → NO accionable
- Si hay una pregunta → ¿está dirigida a Daniel o es retórica/al aire?
- Si mencionan un deadline/hora → ¿ya pasó? Si ya ocurrió, probablemente resuelto.

⚠️ DETECCIÓN DE RESOLUCIÓN CONVERSACIONAL:
- Confirmaciones mutuas ("sisi", "dale", "perfecto", "A las 10 entonces") → conversación cerrada
- Último mensaje confirma un plan concreto (hora/lugar) → resuelto, no accionable
- Intercambio "¿cuándo?" → "a las X" → "ok" → cerrado

NO es accionable:
- "ok", "gracias", "👍", emojis sueltos
- Confirmaciones automáticas o mutuas de un plan ya coordinado
- Mensajes informativos sin pregunta concreta a Daniel
- Notificaciones de bots, deploys, integraciones
- "FYI" sin acción esperada
- Avisos pasivos (ej. "ya quedó hecho")
- Conversaciones donde el remitente habla de SUS propios pendientes sin pedirle nada a Daniel

SÍ es accionable:
- Pregunta directa A DANIEL que espera SU respuesta
- Pedido de aprobación / revisión / decisión de Daniel
- Mención en un hilo donde discuten algo que requiere input de Daniel
- Bloqueo: alguien dice que necesita algo DE DANIEL para avanzar
- Solicitud explícita a Daniel (acceso, info, contexto)

Recibís lista de mensajes con: id, channel, author, text, conversation_context (últimos mensajes del canal para ver si se resolvió).

**USÁ `conversation_context` para detectar resoluciones:**
- Si el contexto muestra que ya coordinaron hora/lugar → resuelto
- Si el contexto muestra confirmaciones mutuas → resuelto
- Si el contexto muestra que la conversación cerró naturalmente → resuelto

Devolvé JSON estricto con SOLO los accionables:
{
  "items": [
    {
      "id": <int>,
      "reason": "1 línea explicando por qué pide acción DE DANIEL (qué necesitan de él)",
      "suggested_action": "qué hacer — verbo corto"
    }
  ]
}

Si ninguno es accionable: {"items": []}. Español rioplatense."""


def filter_actionable(messages: List[PendingSlackMessage]) -> List[ActionableSlackMessage]:
    if not messages:
        return []

    items_for_llm = [
        {
            "id": i,
            "source": m.source_type,
            "channel": m.channel_name,
            "author": m.author_name,
            "text": m.text[:500],
            "conversation_context": m.conversation_context[:800] if m.conversation_context else "",
        }
        for i, m in enumerate(messages)
    ]
    user_content = (
        f"Filtrá estos {len(messages)} mensajes de Slack:\n\n"
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
        log.exception("LLM falló filtrando %d mensajes Slack", len(messages))
        return []

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("slack_filter devolvió JSON inválido: %r", content[:200])
        return []

    result: List[ActionableSlackMessage] = []
    for item in payload.get("items") or []:
        try:
            mid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not (0 <= mid < len(messages)):
            continue
        m = messages[mid]
        result.append(ActionableSlackMessage(
            channel_id=m.channel_id,
            channel_name=m.channel_name,
            author_name=m.author_name,
            text=m.text,
            permalink=m.permalink,
            reason=(item.get("reason") or "").strip()[:200],
            suggested_action=(item.get("suggested_action") or "").strip()[:200],
        ))
    return result
