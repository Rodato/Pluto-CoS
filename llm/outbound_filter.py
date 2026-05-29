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


_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Recibís una lista de mensajes que DANIEL envió (correos + Slack DMs/grupos + canales públicos/privados) y a los cuales NO le respondieron todavía. Tu trabajo es separar lo que realmente está esperando respuesta de lo que NO.

⚠️ CONTEXTO TEMPORAL Y CONVERSACIONAL:
- Fijate en el timestamp `sent_at`: si el mensaje menciona una hora/fecha específica ("a las 10", "mañana"), y ya pasó ese momento, probablemente YA SE RESOLVIÓ en persona/llamada.
- Si el snippet termina con confirmación de horario/lugar ("A las 10 entonces", "nos vemos a las X") → es un CIERRE conversacional, no espera respuesta adicional.
- Mensajes enviados hace MÁS de 7 días probablemente ya fueron resueltos offline o perdieron vigencia — descartá salvo que sea explícitamente un follow-up sin respuesta.

⚠️ DIRECCIÓN: ¿Daniel está PIDIENDO o OFRECIENDO?
- "necesito X de vos" → Daniel espera respuesta
- "avísame cuando puedas" / "confirmame" → Daniel espera respuesta
- "gracias", "perfecto", "dale" → Daniel está CERRANDO, no esperando
- "te paso X" / "acá está Y" → Daniel entregó, NO espera nada salvo que haya pregunta explícita después

⚠️ ATENCIÓN ESPECIAL A CANALES: en canales (channel_type=channel) la mayoría de los mensajes son anuncios, status updates, comentarios casuales o info compartida — NO esperan respuesta de nadie. Sé MUY conservador con esos. Solo extrae si el mensaje es claramente una pregunta abierta o un pedido explícito al canal.

❌ NO espera respuesta (descartá):
- Confirmaciones / ack: "ok", "perfecto", "dale", "listo", "gracias", "recibido", "👍"
- Cierres conversacionales: "A las 10 entonces", "Me citas por favor", "nos vemos"
- Anuncios / status: "ya está pusheado", "termino esto y sigo", "merged", "deployando", "subiendo"
- Updates de progreso sin pregunta: "voy por la mitad", "el cliente confirmó", "agendado para mañana"
- Cortesía / cierre: "nos vemos", "buen finde", "saludos", "lo veo mañana"
- Compartir info sin pedir nada: forward de un link, FYI, paste de un documento, "les dejo esto por si sirve"
- Comentarios en canal a un mensaje de otro (sin pregunta nueva)
- Cuando Daniel cerró un thread con un "gracias" final que ya no requiere réplica
- Heads-up / notificaciones generales al canal
- Mensajes de coordinación donde YA se acordó hora/lugar (aunque nadie haya respondido "ok" formalmente)

✅ SÍ espera respuesta (incluí):
- Daniel hizo una pregunta directa ("¿alguien sabe...", "¿puede alguien...", "¿cómo lo hacemos...")
- Daniel pidió aprobación, revisión, confirmación, decisión a alguien específico
- Daniel propuso algo y necesita feedback / sí/no
- Daniel solicitó un dato, archivo, contacto, acceso
- Daniel está esperando que el otro le mande X
- Follow-ups donde se ve que el otro debía haberle respondido y NO lo hizo
- Pedidos explícitos al canal ("necesito que alguien me ayude con X antes de Y")

Recibís items con: id, kind (gmail/slack), channel_type (dm/group_dm/channel), to (destinatarios o nombre de canal), sent_at (ISO timestamp), snippet.

Devolvé JSON estricto con SOLO los que SÍ esperan respuesta:
{
  "items": [
    {
      "id": <int>,
      "reason": "1 línea explicando por qué TODAVÍA espera respuesta (qué fue lo que Daniel pidió/preguntó y NO recibió)",
      "waiting_for": "qué espera del otro lado — frase corta (ej: 'confirmación de presupuesto', 'el doc firmado', 'feedback del onepager')"
    }
  ]
}

Si ninguno espera respuesta, devolvé {"items": []}. Respondé en español rioplatense. Ante la duda, descartá — sobre todo con mensajes en canales y cierres conversacionales."""


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
            "channel_type": m.source_type,  # dm | group_dm | channel
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
