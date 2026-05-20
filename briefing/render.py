"""Renderers para el briefing — Telegram (HTML) y Markdown (vault).

Agrupación PRINCIPAL: por proyecto (carpeta Granola/<project>/).
Dentro de cada proyecto: ordenado por prioridad (P0 → P3).
"""

from __future__ import annotations

import html
from typing import Dict, List

from briefing.builder import BriefingResult
from briefing.prioritizer import PrioritizedTask
from telegram_bot.bot import _fmt_dt

PRIORITY_ORDER = ("P0", "P1", "P2", "P3")
PRIORITY_EMOJI = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}

# Telegram: máximo de tareas mostradas por proyecto.
TELEGRAM_MAX_PER_PROJECT = 5


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


def render_telegram(briefing: BriefingResult, max_per_project: int = TELEGRAM_MAX_PER_PROJECT) -> str:
    """HTML para Telegram. Agrupado por proyecto, cap por proyecto."""
    parts: List[str] = []
    parts.append(f"☀️ <b>Briefing — {briefing.briefing_date.isoformat()}</b>")

    if briefing.today_events:
        parts.append("\n📅 <b>Tu agenda hoy</b>")
        for ev in briefing.today_events:
            start = _fmt_dt(ev.get("start", {}))
            title = ev.get("summary") or "(sin título)"
            parts.append(f"• <b>{html.escape(start)}</b> — {html.escape(title)}")
    else:
        parts.append("\n📅 Hoy no tenés nada agendado.")

    grouped = _group_by_project(briefing.prioritized)
    if not grouped:
        parts.append(
            f"\n📝 Sin tareas. "
            f"({briefing.notes_processed} notas procesadas, "
            f"{briefing.notes_skipped_age} ignoradas por antigüedad)"
        )
        return "\n".join(parts)

    extra_count = 0
    projects_sorted = sorted(grouped.items(), key=lambda kv: _project_sort_key(kv[0], kv[1]))
    for proj, items in projects_sorted:
        total = len(items)
        shown = items[:max_per_project]
        extra_count += max(0, total - max_per_project)
        header = f"\n📁 <b>{html.escape(proj)}</b>"
        if total > max_per_project:
            header += f" <i>(mostrando {len(shown)} de {total})</i>"
        else:
            header += f" <i>({total})</i>"
        parts.append(header)
        for it in shown:
            emoji = PRIORITY_EMOJI.get(it.priority, "•")
            line = f"{emoji} <b>{html.escape(it.title)}</b>"
            if it.rationale:
                line += f"\n  <i>{html.escape(it.rationale)}</i>"
            parts.append(line)

    footer = (
        f"\n<i>{briefing.notes_processed} notas procesadas · "
        f"{briefing.notes_skipped_age} ignoradas por antigüedad</i>"
    )
    if extra_count:
        footer = (
            f"\n📂 <i>{extra_count} tareas adicionales en "
            f"<code>Briefings/{briefing.briefing_date.isoformat()}.md</code></i>"
            + footer
        )
    parts.append(footer)
    return "\n".join(parts)


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

    parts.append("---")
    parts.append(
        f"_{briefing.notes_processed} notas procesadas, "
        f"{briefing.notes_skipped_age} ignoradas por antigüedad._"
    )
    return "\n".join(parts)
