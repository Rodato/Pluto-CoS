"""Filtro LLM: ¿este mensaje que Daniel envió REALMENTE espera respuesta?

Heurística previa (último msg de Daniel, sin réplica) deja pasar mucho ruido:
- Confirmaciones ("ok", "perfecto", "dale", "gracias")
- Acknowledgments ("recibido", "lo veo y te aviso")
- Anuncios o updates que no piden nada de vuelta
- Mensajes de cortesía / cierre de conversación

El LLM filtra esa capa y devuelve solo lo que REALMENTE quedó pendiente de
respuesta de la otra parte.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import List, Literal, Optional

from gmail_api.client import OutboundThread
from llm.planner import _client, DEFAULT_MODEL
from slack_api.client import OutboundSlackMessage

log = logging.getLogger(__name__)


@dataclass
class AwaitingItem:
    kind: Literal["gmail", "slack"]
    source_id: str           # thread_id (gmail) o channel_id (slack)
    permalink: Optional[str] # link clickable cuando aplica
    subject_or_channel: str  # asunto del correo o nombre del canal
    recipients_label: str    # "a Rafael" / "a Cesar, Marian" / "Grupo con X, Y"
    sent_at_iso: str
    snippet: str             # resumen del mensaje enviado por Daniel
    reason: str              # 1 línea: por qué espera respuesta
    waiting_for: str         # qué espera Daniel del otro lado


_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Recibís una lista de mensajes que DANIEL envió (correos + Slack DMs/grupos) y a los cuales NO le respondieron todavía. Tu trabajo es separar lo que realmente está esperando respuesta de lo que NO.

❌ NO espera respuesta (descartá):
- Confirmaciones / ack: "ok", "perfecto", "dale", "listo", "gracias", "recibido"
- Anuncios sin pregunta: "ya está pusheado", "te dejo el link", "agendado"
- Cortesía / cierre: "nos vemos", "buen finde", "saludos"
- Compartir info sin pedir nada: forward de un link, paste de un documento
- Cuando Daniel cerró un thread con un "gracias" final que ya no requiere réplica

✅ SÍ espera respuesta (incluí):
- Daniel hizo una pregunta directa al otro lado
- Daniel pidió aprobación, revisión, confirmación, decisión
- Daniel propuso algo y necesita feedback / sí/no
- Daniel solicitó un dato, archivo, contacto, acceso
- Daniel está esperando que el otro le mande X
- Follow-ups donde se ve que el otro debía haberle respondido

Recibís items con: id, kind (gmail/slack), to (destinatarios), snippet (resumen).

Devolvé JSON estricto con SOLO los que SÍ esperan respuesta:
{
  "items": [
    {
      "id": <int>,
      "reason": "1 línea explicando por qué espera respuesta (qué fue lo que Daniel pidió/preguntó)",
      "waiting_for": "qué espera del otro lado — frase corta (ej: 'confirmación de presupuesto', 'el doc firmado', 'feedback del onepager')"
    }
  ]
}

Si ninguno espera respuesta, devolvé {"items": []}. Respondé en español rioplatense. Sé conservador — si dudás, descartá."""


def filter_awaiting_reply(
    gmail_items: List[OutboundThread],
    slack_items: List[OutboundSlackMessage],
) -> List[AwaitingItem]:
    """Filtra gmail+slack outbound y devuelve solo los que esperan respuesta."""
    if not gmail_items and not slack_items:
        return []

    items_for_llm = []
    for i, t in enumerate(gmail_items):
        items_for_llm.append({
            "id": i,
            "kind": "gmail",
            "to": ", ".join(t.to_addrs[:4]),
            "subject": t.subject,
            "sent_at": t.sent_at.isoformat(),
            "snippet": t.snippet[:400],
        })
    offset = len(gmail_items)
    for j, m in enumerate(slack_items):
        items_for_llm.append({
            "id": offset + j,
            "kind": "slack",
            "to": ", ".join(m.recipients[:4]),
            "channel": m.channel_name,
            "sent_at": m.sent_at.isoformat(),
            "snippet": m.text[:400],
        })

    user_content = (
        f"Filtrá estos {len(items_for_llm)} mensajes que Daniel envió "
        f"y todavía no le respondieron:\n\n"
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
            temperature=0.2,
        )
    except Exception:
        log.exception("LLM falló filtrando outbound (gmail=%d slack=%d)",
                      len(gmail_items), len(slack_items))
        return []

    content = resp.choices[0].message.content or "{}"
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        log.warning("outbound_filter devolvió JSON inválido: %r", content[:200])
        return []

    result: List[AwaitingItem] = []
    for item in payload.get("items") or []:
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue

        reason = (item.get("reason") or "").strip()[:200]
        waiting_for = (item.get("waiting_for") or "").strip()[:200]

        if 0 <= tid < len(gmail_items):
            t = gmail_items[tid]
            recipients_label = ", ".join(_short_name(a) for a in t.to_addrs[:3])
            result.append(AwaitingItem(
                kind="gmail",
                source_id=t.thread_id,
                permalink=f"https://mail.google.com/mail/u/0/#sent/{t.thread_id}",
                subject_or_channel=t.subject,
                recipients_label=recipients_label,
                sent_at_iso=t.sent_at.isoformat(),
                snippet=t.snippet,
                reason=reason,
                waiting_for=waiting_for,
            ))
            continue

        sid = tid - offset
        if 0 <= sid < len(slack_items):
            m = slack_items[sid]
            recipients_label = ", ".join(m.recipients[:3])
            result.append(AwaitingItem(
                kind="slack",
                source_id=m.channel_id,
                permalink=m.permalink or None,
                subject_or_channel=m.channel_name,
                recipients_label=recipients_label,
                sent_at_iso=m.sent_at.isoformat(),
                snippet=m.text,
                reason=reason,
                waiting_for=waiting_for,
            ))

    return result


def _short_name(addr: str) -> str:
    """De 'foo@bar.com' devolve 'foo'. De 'Foo Bar <foo@bar.com>' devuelve 'Foo Bar'."""
    raw = addr.strip()
    if "<" in raw and ">" in raw:
        name = raw[:raw.index("<")].strip().strip('"')
        if name:
            return name
        raw = raw[raw.index("<") + 1 : raw.rindex(">")]
    if "@" in raw:
        return raw.split("@", 1)[0]
    return raw
