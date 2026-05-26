"""Renderers para el briefing — Telegram (narrativa LLM) y Markdown (vault).

- Telegram: `render_telegram` arma una narrativa conversacional con LLM
  agrupada por urgencia (P0/P1/P2) y proyecto. Excluye P3 y "Varios".
- Markdown: `render_markdown` mantiene el formato estructurado completo para
  el archivo de archivo en el vault (incluyendo P3 y Varios).
"""

from __future__ import annotations

import html
import json
import logging
from typing import Dict, List, Optional

from briefing.builder import BriefingResult
from briefing.prioritizer import PrioritizedTask
from llm.planner import DEFAULT_MODEL, _client
from telegram_bot.bot import _fmt_dt

log = logging.getLogger(__name__)

PRIORITY_ORDER = ("P0", "P1", "P2", "P3")
PRIORITY_EMOJI = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}

# Telegram limita los mensajes a 4096 chars. Dejamos margen para HTML.
TELEGRAM_CHUNK_LIMIT = 3800

# Proyectos que no aportan al briefing conversacional.
_HIDDEN_PROJECTS = {"Varios", ""}

# Label legible del canal de origen para que el narrador lo cite ("(correo)", etc.)
_CHANNEL_LABELS = {
    "gmail": "correo",
    "slack": "Slack",
    "granola": "reunión",
}


def _channel_label(source: Optional[str]) -> str:
    return _CHANNEL_LABELS.get((source or "").lower(), "")


def _fmt_task_with_channel(t) -> str:
    """Fallback render: 'título (canal)' si hay canal, sino solo el título."""
    label = _channel_label(t.source)
    title = html.escape(t.title)
    return f"{title} ({label})" if label else title


def _extract_identifier(source: Optional[str], context: Optional[str]) -> Optional[str]:
    """Saca el remitente/canal del context guardado en DB.

    Gmail context guardado por builder.py:
        De: <remitente>
        Acción sugerida: ...
        Por qué: ...

    Slack context:
        Canal: <channel>
        De: <author>
        Acción sugerida: ...
        Por qué: ...
    """
    if not source or not context:
        return None
    src = source.lower()
    if src not in {"gmail", "slack"}:
        return None
    for line in context.splitlines():
        line = line.strip()
        if src == "gmail" and line.lower().startswith("de:"):
            return line[3:].strip()[:80] or None
        if src == "slack":
            if line.lower().startswith("canal:"):
                return line[6:].strip()[:80] or None
    return None

_NARRATIVE_SYSTEM_PROMPT = """Sos el Chief-of-Staff de Daniel (CTO de Estudio Plural). Entregás el briefing matutino en formato conversacional, español rioplatense, cercano pero profesional. Hablás de vos.

Estructura OBLIGATORIA del mensaje (HTML para Telegram, máximo 3500 chars TOTAL):

☀️ <b>Buenos días, Daniel</b>
1-2 oraciones con la agenda del día en prosa fluida (ej: "Hoy arrancás con X a las 8, después Y a las 2, y a las 5 cortás"). Omití bloques personales sin valor. Si no hay agenda, decilo en una línea.

🔴 <b>Lo urgente hoy</b>
Prosa por proyecto. Para cada proyecto con tareas P0: 1-2 oraciones que parafraseen las tareas y por qué bloquean. Mencioná el proyecto en <b>negrita</b>. NO listas con bullets, NO comillas, NO repitas títulos textuales.

🟠 <b>Para atender pronto</b>
Igual, pero con las P1. Más conciso (1 oración por proyecto bastante).

🟡 <b>No lo perdás de vista</b>
Pasada rápida con las P2: una oración corta por proyecto, solo lo que merece estar arriba del radar. Si hay muchas, agrupá.

CITAR EL CANAL Y EL ORIGEN: cada item en el JSON trae `canal` (correo / Slack / reunión / vacío) y, cuando es correo o Slack, ALSO `de` con el remitente o canal de origen ("De: Rafael Phillips Alvarez", "Canal: #plural-dev", etc.). Cuando narres las tareas:
1. SIEMPRE mencioná el canal en la prosa.
2. Si hay `de`, mencionalo TAMBIÉN para que Daniel pueda identificar de qué se trata sin tener que adivinar. Usá el nombre/canal tal cual viene en `de`, no inventes.
3. Si el canal está vacío, no inventes; omití la referencia al canal y narrá la tarea.
4. Si en un proyecto las tareas vienen de canales mixtos, agrupá por canal ("Por correo... y en Slack...").

Ejemplos correctos:
- "En <b>Apapáchar</b>, por correo te quedó responderle a <b>Rafael Phillips</b> sobre datos unificados — necesita la confirmación para avanzar."
- "En <b>Lighting_AI</b>, en Slack #plural-legal te pidieron el contrato firmado."
- "En <b>Puddle</b>, de la reunión del lunes quedó preparar los insumos del benchmark."

Ejemplos INCORRECTOS (no hacer):
- "En Bootcamp Rio, por correo tenés que confirmar la cancelación" (¿de quién? ¿cuál correo?). MEJOR: "Por correo, <b>Eric Saiz</b> te escribió sobre Bootcamp Rio y necesita que confirmes la cancelación."

📤 <b>Esperando respuesta</b>
Solo si `esperando_respuesta` en el JSON tiene items. Por cada item, 1 línea de prosa: "<b>A &lt;persona&gt;</b> (correo|Slack): &lt;qué le pediste&gt; — &lt;hace cuánto&gt;". Agrupá por persona/canal si hay varios al mismo destinatario. NO mencionés el subject completo, parafraseá. Si hay >5 items, mostrá los 5 más viejos y agregá "+ N más esperando" al final.

REGLAS:
- HTML simple: solo <b> y <i>. NO uses markdown ni headers (#).
- Si una sección no tiene tareas, omitirla COMPLETAMENTE (ni el header).
- Tono cercano pero profesional. NO uses "che", "bro", "obvio", ni emojis sueltos en el cuerpo.
- Sé selectivo: si hay 10 P1 en un proyecto, mencioná solo las 2-3 más importantes y agregá "y otras X más" si querés. NO exhaustivo.
- NUNCA inventes tareas que no estén en el input.
- ⚠️ NUNCA inventes horas, fechas ni reuniones. Las ÚNICAS horas/eventos que podés mencionar son los que aparecen textualmente en `agenda` del JSON. Si una tarea dice "alineamiento", "reunión", "demo" o similar en su `por_que` o título, referite a la ACCIÓN (preparar insumos, cerrar protocolo, etc.) — NO digas "a las X PM" ni "la reunión de hoy" salvo que esa hora/evento esté en `agenda`.
- NO firmes el mensaje ni cierres con despedida."""


def _select_for_narrative(prioritized: List[PrioritizedTask]) -> List[PrioritizedTask]:
    """Excluye P3 y proyecto Varios; ordena por priority y proyecto."""
    priority_idx = {p: i for i, p in enumerate(PRIORITY_ORDER)}
    keep = [
        t for t in prioritized
        if t.priority in ("P0", "P1", "P2")
        and (t.project or "Varios") not in _HIDDEN_PROJECTS
    ]
    keep.sort(key=lambda t: (priority_idx.get(t.priority, 99), (t.project or "").lower()))
    return keep


def _build_narrative_payload(briefing: BriefingResult, tasks: List[PrioritizedTask]) -> dict:
    """Payload compacto para el LLM — solo lo necesario para narrar."""
    agenda = []
    for ev in briefing.today_events:
        agenda.append({
            "hora": _fmt_dt(ev.get("start", {})),
            "titulo": ev.get("summary") or "(sin título)",
        })

    by_priority: Dict[str, Dict[str, List[dict]]] = {"P0": {}, "P1": {}, "P2": {}}
    for t in tasks:
        proj = t.project or "Varios"
        bucket = by_priority[t.priority].setdefault(proj, [])
        entry = {
            "titulo": t.title,
            "por_que": t.rationale,
            "canal": _channel_label(t.source),
        }
        # Para tasks que vinieron de Gmail/Slack, el `context` guardado en DB
        # tiene "De: <remitente>" / "Canal: ..." — extraemos eso así el
        # narrador puede identificar la tarea sin que el usuario adivine.
        identifier = _extract_identifier(t.source, t.context)
        if identifier:
            entry["de"] = identifier
        bucket.append(entry)

    awaiting = [
        {
            "canal": _channel_label(a.kind),
            "a_quien": a.recipients_label,
            "esperando": a.waiting_for,
            "contexto": a.reason,
            "enviado": a.sent_at_iso,
        }
        for a in briefing.awaiting_reply
    ]

    return {
        "fecha": briefing.briefing_date.isoformat(),
        "agenda": agenda,
        "P0_urgente_hoy": by_priority["P0"],
        "P1_atender_pronto": by_priority["P1"],
        "P2_no_perder_de_vista": by_priority["P2"],
        "esperando_respuesta": awaiting,
    }



def _group_by_project(items: List[PrioritizedTask]) -> Dict[str, List[PrioritizedTask]]:
    grouped: Dict[str, List[PrioritizedTask]] = {}
    for it in items:
        proj = it.project or "Varios"
        grouped.setdefault(proj, []).append(it)
    # Dentro de cada proyecto, orden por priority (P0 primero) y luego por título.
    priority_idx = {p: i for i, p in enumerate(PRIORITY_ORDER)}
    for proj, lst in grouped.items():
        lst.sort(key=lambda t: (priority_idx.get(t.priority, 99), t.title.lower()))
    return grouped


def _project_sort_key(proj_name: str, items: List[PrioritizedTask]) -> tuple:
    """Ordena proyectos: los que tienen P0 primero, después por cantidad, después alfabético."""
    has_p0 = any(t.priority == "P0" for t in items)
    has_p1 = any(t.priority == "P1" for t in items)
    return (0 if has_p0 else 1 if has_p1 else 2, -len(items), proj_name.lower())


def _pack_chunks(blocks: List[str], limit: int = TELEGRAM_CHUNK_LIMIT) -> List[str]:
    """Acumula bloques en chunks <= limit. Cada bloque debe ser HTML autocontenido."""
    chunks: List[str] = []
    current = ""
    for block in blocks:
        sep = "\n" if current else ""
        if current and len(current) + len(sep) + len(block) > limit:
            chunks.append(current)
            current = block
        else:
            current += sep + block
    if current:
        chunks.append(current)
    return chunks


def render_telegram(briefing: BriefingResult) -> List[str]:
    """HTML conversacional para Telegram (narrado por LLM).

    Devuelve N chunks <= TELEGRAM_CHUNK_LIMIT chars. Si el LLM falla, cae a un
    render minimalista. Excluye P3 y proyecto "Varios"; el .md del vault sí
    los incluye.
    """
    selected = _select_for_narrative(briefing.prioritized)
    hidden_count = len(briefing.prioritized) - len(selected)

    if not selected and not briefing.today_events and not briefing.awaiting_reply:
        body = "☀️ <b>Buenos días, Daniel</b>\nHoy no tenés agenda ni tareas pendientes. Aprovechá."
        return _finalize_chunks(_pack_chunks([body, _footer(briefing, hidden_count)]))

    narrative = _generate_narrative(briefing, selected)
    blocks = [narrative, _footer(briefing, hidden_count)]
    return _finalize_chunks(_pack_chunks(blocks))


def _generate_narrative(briefing: BriefingResult, selected: List[PrioritizedTask]) -> str:
    """Llama al LLM con el payload estructurado. Si falla, fallback minimalista."""
    payload = _build_narrative_payload(briefing, selected)
    user_content = (
        "Generá el briefing matutino conversacional a partir de este JSON:\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )

    try:
        resp = _client.chat.completions.create(
            model=DEFAULT_MODEL,
            messages=[
                {"role": "system", "content": _NARRATIVE_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0.3,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text:
            return text
    except Exception:
        log.exception("LLM falló generando narrativa del briefing — uso fallback")

    return _fallback_narrative(briefing, selected)


def _fallback_narrative(briefing: BriefingResult, selected: List[PrioritizedTask]) -> str:
    """Render minimalista si el LLM falla: lista por urgencia + proyecto."""
    parts = [f"☀️ <b>Buenos días, Daniel</b> — {briefing.briefing_date.isoformat()}"]
    if briefing.today_events:
        agenda = ", ".join(
            f"{_fmt_dt(ev.get('start', {}))} {ev.get('summary') or '(sin título)'}"
            for ev in briefing.today_events
        )
        parts.append(f"<i>Agenda:</i> {html.escape(agenda)}")

    headers = {
        "P0": "🔴 <b>Lo urgente hoy</b>",
        "P1": "🟠 <b>Para atender pronto</b>",
        "P2": "🟡 <b>No lo perdás de vista</b>",
    }
    by_pri: Dict[str, List[PrioritizedTask]] = {"P0": [], "P1": [], "P2": []}
    for t in selected:
        by_pri[t.priority].append(t)

    for pri in ("P0", "P1", "P2"):
        items = by_pri[pri]
        if not items:
            continue
        parts.append("")
        parts.append(headers[pri])
        by_proj: Dict[str, List[PrioritizedTask]] = {}
        for t in items:
            by_proj.setdefault(t.project or "Varios", []).append(t)
        for proj, lst in by_proj.items():
            titles = "; ".join(
                _fmt_task_with_channel(t) for t in lst[:3]
            )
            extra = len(lst) - 3
            tail = f" (+{extra} más)" if extra > 0 else ""
            parts.append(f"<b>{html.escape(proj)}:</b> {titles}{tail}")

    if briefing.awaiting_reply:
        parts.append("")
        parts.append("📤 <b>Esperando respuesta</b>")
        for a in briefing.awaiting_reply[:8]:
            channel = "correo" if a.kind == "gmail" else "Slack"
            who = html.escape(a.recipients_label or a.subject_or_channel)
            waiting = html.escape(a.waiting_for or a.reason or "respuesta")
            parts.append(f"• <b>{who}</b> ({channel}): {waiting}")
        if len(briefing.awaiting_reply) > 8:
            parts.append(f"<i>+ {len(briefing.awaiting_reply) - 8} más</i>")
    return "\n".join(parts)


def _footer(briefing: BriefingResult, hidden_count: int) -> str:
    pieces = []
    if hidden_count > 0:
        pieces.append(
            f"📂 <i>{hidden_count} tareas menores en "
            f"<code>Briefings/{briefing.briefing_date.isoformat()}.md</code></i>"
        )
    pieces.append(
        f"<i>{briefing.notes_processed} notas procesadas · "
        f"{briefing.notes_skipped_age} ignoradas por antigüedad</i>"
    )
    return "\n".join(pieces)


def _finalize_chunks(chunks: List[str]) -> List[str]:
    """Agrega marker '(parte k/N)' a chunks 2..N."""
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [
        chunks[0],
        *(f"<i>(parte {i + 1}/{total})</i>\n{c}" for i, c in enumerate(chunks[1:], start=1)),
    ]


def render_markdown(briefing: BriefingResult) -> str:
    """Markdown para el archivo Briefings/YYYY-MM-DD.md del vault."""
    parts: List[str] = []
    parts.append(f"# Briefing — {briefing.briefing_date.isoformat()}\n")

    parts.append("## Agenda de hoy\n")
    if briefing.today_events:
        for ev in briefing.today_events:
            start = _fmt_dt(ev.get("start", {}))
            title = ev.get("summary") or "(sin título)"
            parts.append(f"- **{start}** — {title}")
    else:
        parts.append("_Hoy no tenés nada agendado._")
    parts.append("")

    grouped = _group_by_project(briefing.prioritized)
    projects_sorted = sorted(grouped.items(), key=lambda kv: _project_sort_key(kv[0], kv[1]))
    for proj, items in projects_sorted:
        parts.append(f"## 📁 {proj} ({len(items)})\n")
        for it in items:
            emoji = PRIORITY_EMOJI.get(it.priority, "•")
            parts.append(f"### {emoji} {it.title}")
            if it.rationale:
                parts.append(f"_{it.rationale}_\n")
            if it.context:
                parts.append(it.context + "\n")
            meta = [f"**{it.priority}**"]
            if it.estimated_minutes:
                meta.append(f"⏱ ~{it.estimated_minutes}min")
            if it.deadline_hint:
                meta.append(f"📅 {it.deadline_hint}")
            parts.append("  ·  ".join(meta) + "\n")
            parts.append(f"<sub>Fuente: `{it.source_note}`</sub>\n")

    if briefing.awaiting_reply:
        parts.append(f"## 📤 Esperando respuesta ({len(briefing.awaiting_reply)})\n")
        for a in briefing.awaiting_reply:
            channel_label = "📧 Correo" if a.kind == "gmail" else "💬 Slack"
            target = a.recipients_label or a.subject_or_channel
            parts.append(f"### {channel_label} — a {target}")
            parts.append(f"**Esperando:** {a.waiting_for}")
            if a.reason:
                parts.append(f"_{a.reason}_")
            parts.append(f"<sub>Enviado: {a.sent_at_iso}</sub>")
            if a.permalink:
                parts.append(f"<sub>[Abrir]({a.permalink})</sub>")
            parts.append("")

    parts.append("---")
    parts.append(
        f"_{briefing.notes_processed} notas procesadas, "
        f"{briefing.notes_skipped_age} ignoradas por antigüedad._"
    )
    return "\n".join(parts)
