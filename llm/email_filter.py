"""Filtro LLM: ¿este correo REALMENTE pide respuesta de Daniel?

La heurística de Gmail (To/Cc + último msg no mío) deja pasar mucho ruido:
newsletters dirigidos personalmente, autoresponders, notificaciones, etc.
El LLM filtra esa segunda capa y devuelve solo los accionables.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Optional

from gmail_api.client import PendingThread
from llm.planner import _client, DEFAULT_MODEL

log = logging.getLogger(__name__)


@dataclass
class ActionableEmail:
    thread_id: str
    subject: str
    from_addr: str
    received_at_iso: str
    reason: str  # 1 línea: por qué pide respuesta
    suggested_action: str  # qué tendría que hacer Daniel


_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Filtrás correos para decidir cuáles REALMENTE piden una respuesta de Daniel.

NO es accionable:
- Newsletters, marketing, promos
- Notificaciones automáticas (GitHub, Vercel, Stripe, etc.)
- Autoresponders (out-of-office, "este es un correo automático")
- Confirmaciones de reservas/compras
- Recibos, facturas (a menos que pidan acción)
- Reportes periódicos sin pregunta concreta

SÍ es accionable:
- Alguien te hace una pregunta directa
- Alguien te pide aprobación, revisión, decisión
- Alguien espera respuesta a una propuesta
- Cliente pidiendo update / status
- Un compromiso o entrega que necesita confirmación tuya
- Threads donde el remitente claramente espera tu respuesta

Recibís lista de correos con: id, subject, from, snippet (resumen corto del cuerpo).

Devolvé JSON estricto con SOLO los accionables:
{
  "items": [
    {
      "id": <int>,
      "reason": "1 línea explicando por qué pide respuesta",
      "suggested_action": "qué tendría que hacer Daniel — verbo corto"
    }
  ]
}

Si ninguno es accionable, devolvé {"items": []}. Respondé en español rioplatense."""


def filter_actionable(threads: List[PendingThread]) -> List[ActionableEmail]:
    """Filtra threads con LLM y devuelve solo los que requieren respuesta."""
    if not threads:
        return []

    items_for_llm = [
        {
            "id": i,
            "subject": t.subject,
            "from": t.from_addr,
            "received_at": t.received_at.isoformat(),
            "snippet": t.snippet[:500],
        }
        for i, t in enumerate(threads)
    ]
    user_content = (
        f"Filtrá estos {len(threads)} correos pendientes:\n\n"
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
        log.exception("LLM falló filtrando %d threads", len(threads))
        return []

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("email_filter devolvió JSON inválido: %r", content[:200])
        return []

    result: List[ActionableEmail] = []
    for item in payload.get("items") or []:
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if not (0 <= tid < len(threads)):
            continue
        t = threads[tid]
        result.append(ActionableEmail(
            thread_id=t.thread_id,
            subject=t.subject,
            from_addr=t.from_addr,
            received_at_iso=t.received_at.isoformat(),
            reason=(item.get("reason") or "").strip()[:200],
            suggested_action=(item.get("suggested_action") or "").strip()[:200],
        ))
    return result
