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

NO es accionable:
- "ok", "gracias", "👍", emojis sueltos
- Confirmaciones automáticas
- Mensajes informativos sin pregunta concreta
- Notificaciones de bots, deploys, integraciones
- "FYI" sin acción esperada
- Avisos pasivos (ej. "ya quedó hecho")

SÍ es accionable:
- Pregunta directa que espera tu respuesta
- Pedido de aprobación / revisión / decisión
- Mención en un hilo donde discuten algo que requiere tu input
- Bloqueo: alguien dice que necesita algo tuyo para avanzar
- Solicitud explícita (acceso, info, contexto)

Recibís lista de mensajes con id, channel, author, text.

Devolvé JSON estricto con SOLO los accionables:
{
  "items": [
    {
      "id": <int>,
      "reason": "1 línea explicando por qué pide acción",
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
