"""Orquesta el briefing matutino CoS.

Pipeline:
1. Calendar: eventos del día (contexto, sin LLM).
2. Granola: lista notas nuevas/modificadas, filtra por edad, extrae tareas, prioriza.
3. Persiste tareas en `tasks` y notas procesadas en `processed_notes`.
4. Devuelve estructura `BriefingResult` lista para render.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from pathlib import Path

from briefing.prioritizer import PrioritizedTask, prioritize, prioritize_open_tasks
from calendar_api import client as cal_client
from db import client as db
from obsidian.parser import ExtractedTask, extract_tasks
from obsidian.reader import list_new_or_modified_notes, project_from_path, read_note

from gmail_api.client import (
    list_awaiting_reply as list_gmail_awaiting,
    list_pending_for_reply,
)
from llm.email_filter import filter_actionable as filter_emails
from llm.outbound_filter import AwaitingItem, filter_awaiting_reply
# --- DESACTIVADO (2026-06-02): Slack fuera del briefing por ahora.
# from llm.slack_filter import filter_actionable as filter_slack
# from slack_api.client import (
#     list_all_pending as list_slack_pending,
#     list_outbound_awaiting as list_slack_awaiting,
# )

log = logging.getLogger(__name__)

# Fuentes activas del briefing. Las tareas de fuentes inactivas (Granola/Slack)
# se excluyen de la repriorización diaria para que no reaparezcan cada mañana.
# Para reactivar Granola: sumá "granola" a este set + descomentar bloque 1a.
# Para reactivar Slack: sumá "slack" + descomentar bloque 1c.
_ACTIVE_TASK_SOURCES = {"gmail", "calendar", "manual"}

# Ventana de notas a procesar: desde el lunes de la SEMANA ANTERIOR (semana
# ISO local). Una nota con mtime previo a ese lunes se ignora.


@dataclass
class BriefingResult:
    briefing_date: date
    today_events: List[dict] = field(default_factory=list)
    prioritized: List[PrioritizedTask] = field(default_factory=list)
    notes_processed: int = 0
    notes_skipped_age: int = 0
    emails_pending: int = 0       # threads heurísticos
    emails_actionable: int = 0    # los que el LLM marcó como accionables
    slack_pending: int = 0        # mensajes heurísticos (DMs + menciones)
    slack_actionable: int = 0     # los que el LLM marcó como accionables
    awaiting_reply: List[AwaitingItem] = field(default_factory=list)  # outbound esperando respuesta


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _user_id() -> str:
    return os.environ.get("USER_ID", "daniel")


# Tipos de evento de Google que NO son reuniones reales (out-of-office, focus time, etc.).
_NON_MEETING_EVENT_TYPES = {"outOfOffice", "focusTime", "workingLocation"}


def _is_actionable_event(event: dict) -> bool:
    """Filtra bloques personales / no-reunión: out-of-office, focus time, working
    location y eventos marcados como 'libre' (transparency=transparent), como
    típicamente son ALMUERZO o bloques de gimnasio.
    """
    if event.get("eventType") in _NON_MEETING_EVENT_TYPES:
        return False
    if event.get("transparency") == "transparent":
        return False
    return True


def _today_events(today: datetime) -> List[dict]:
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    try:
        events = cal_client.list_events(start, end)
    except Exception:
        log.exception("No se pudo leer Calendar para el briefing")
        return []
    return [ev for ev in events if _is_actionable_event(ev)]


def _window_start(today_local: date, tz) -> datetime:
    """Lunes 00:00 (local) de la semana anterior a `today_local`."""
    days_since_monday = today_local.weekday()  # 0=lunes, 6=domingo
    monday_this_week = today_local - timedelta(days=days_since_monday)
    monday_last_week = monday_this_week - timedelta(days=7)
    return datetime.combine(monday_last_week, datetime.min.time(), tzinfo=tz)


def _note_relevance_time(note) -> datetime:
    """Fecha que representa la edición REAL de la nota.

    Prioriza `updated_at` del frontmatter (Granola la setea con la edición real)
    sobre mtime (afectado por sync de iCloud que toca todos los archivos a la vez).
    """
    return note.updated_at if note.updated_at else note.mtime


def _is_within_window(note, window_start_local: datetime) -> bool:
    return _note_relevance_time(note) >= window_start_local


def _extract_from_granola(window_start_local: datetime) -> tuple[List[ExtractedTask], int, int]:
    """Lista notas nuevas/modificadas dentro de la ventana, extrae tareas.

    Devuelve (tasks, notes_processed, notes_skipped_age).
    """
    try:
        since = db.get_processed_notes()
    except Exception:
        log.exception("No se pudo leer processed_notes — proceso todas las notas")
        since = {}

    paths = list_new_or_modified_notes(since_mtimes=since)
    all_tasks: List[ExtractedTask] = []
    processed = 0
    skipped = 0

    for path in paths:
        try:
            note = read_note(path)
        except Exception:
            log.exception("No se pudo leer nota %s", path)
            continue

        if not _is_within_window(note, window_start_local):
            skipped += 1
            continue

        tasks = extract_tasks(note.body, str(note.path), note.title)
        all_tasks.extend(tasks)
        try:
            db.mark_note_processed(str(note.path), note.mtime, len(tasks))
        except Exception:
            log.exception("No pude marcar como procesada %s", note.path)
        processed += 1

    return all_tasks, processed, skipped


def _persist_prioritized(
    prioritized: List[PrioritizedTask],
    briefing_date: date,
    source: str = "granola",
) -> None:
    user_id = _user_id()
    for pt in prioritized:
        source_ref = f"{pt.source_note}::{pt.title.lower().strip()[:100]}"
        try:
            if db.task_exists_for_source(source, source_ref):
                continue
            description = pt.context
            if pt.deadline_hint:
                description = (description + f"\n\nDeadline: {pt.deadline_hint}").strip()
            project = pt.project
            if not project:
                project = (
                    project_from_path(Path(pt.source_note)) if pt.source_note else "Varios"
                )
            db.insert_task(
                user_id=user_id,
                title=pt.title,
                description=description or None,
                priority=pt.priority,
                project=project,
                source=source,
                source_ref=source_ref,
                briefing_date=briefing_date,
            )
        except Exception:
            log.exception("Error persistiendo tarea %r", pt.title)


def _close_resolved_tasks(user_id: str, source: str, active_keys: set[str]) -> int:
    """Marca como done las tareas open de `source` cuyo source_ref ya no
    coincide con ninguno de los `active_keys` actuales (prefix match en `::`).

    Una task se considera resuelta si:
    - Su thread/mensaje original ya no aparece en la fuente como pendiente
      (Daniel respondió, o aged out fuera de la ventana de 7 días).

    Devuelve cuántas tareas se cerraron.
    """
    try:
        open_tasks = db.list_open_tasks_by_source(user_id, source)
    except Exception:
        log.exception("No pude listar open tasks de %s para auto-close", source)
        return 0

    closed = 0
    for t in open_tasks:
        sr = t.get("source_ref") or ""
        # source_ref = f"{source_note}::{title_lower}". El identifier del
        # thread/mensaje es todo lo previo al último ::, pero como source_note
        # puede contener :: (caso Slack: channel::permalink), comparamos por
        # prefix: la task se mantiene si algún active_key coincide con prefix.
        is_active = any(
            sr == k or sr.startswith(k + "::") for k in active_keys
        )
        if not is_active:
            try:
                db.update_task_status(t["id"], "done")
                closed += 1
            except Exception:
                log.exception("No pude cerrar task %s", t.get("id"))
    if closed:
        log.info("Auto-cerradas %d tasks de %s (thread/msg ya resuelto)", closed, source)
    return closed


def _extract_from_gmail(days: int = 7) -> tuple[List[ExtractedTask], int, int]:
    """Lista correos pendientes, filtra con LLM, devuelve como ExtractedTask.

    Devuelve (tasks, pending_count, actionable_count). Como efecto secundario,
    auto-cierra tasks de gmail cuyo thread ya no figura como pendiente.
    """
    try:
        threads = list_pending_for_reply(days=days)
    except Exception:
        log.exception("Error listando correos pendientes")
        return [], 0, 0

    active_thread_ids = {t.thread_id for t in threads}
    _close_resolved_tasks(_user_id(), "gmail", active_thread_ids)

    if not threads:
        return [], 0, 0

    try:
        actionable = filter_emails(threads)
    except Exception:
        log.exception("Error filtrando correos")
        return [], len(threads), 0

    tasks: List[ExtractedTask] = []
    for em in actionable:
        from_short = em.from_addr.split("<")[0].strip() or em.from_addr
        title = f"Responder: {em.subject[:80]}".strip()
        context = (
            f"De: {from_short}\n"
            f"Acción sugerida: {em.suggested_action}\n"
            f"Por qué: {em.reason}"
        )
        tasks.append(ExtractedTask(
            title=title,
            context=context,
            estimated_minutes=None,
            deadline_hint=None,
            source_note=em.thread_id,
            source="gmail",
            project="Correos",
        ))
    return tasks, len(threads), len(actionable)


def _extract_from_slack(days: int = 7) -> tuple[List[ExtractedTask], int, int]:
    """Lista mensajes Slack pendientes (DMs + menciones), filtra con LLM.

    Como efecto secundario, auto-cierra tasks de slack cuyo mensaje ya no
    figura como pendiente (Daniel respondió o aged out).
    """
    try:
        messages = list_slack_pending(days=days)
    except RuntimeError:
        log.info("Slack no está configurado (sin SLACK_USER_TOKEN); skip")
        return [], 0, 0
    except Exception:
        log.exception("Error listando mensajes Slack")
        return [], 0, 0

    active_keys = {
        f"{m.channel_id}::{m.permalink}" if m.permalink else m.channel_id
        for m in messages
    }
    _close_resolved_tasks(_user_id(), "slack", active_keys)

    if not messages:
        return [], 0, 0

    try:
        actionable = filter_slack(messages)
    except Exception:
        log.exception("Error filtrando Slack")
        return [], len(messages), 0

    tasks: List[ExtractedTask] = []
    for m in actionable:
        title = f"Responder en Slack: {m.text[:80]}".strip()
        context = (
            f"Canal: {m.channel_name}\n"
            f"De: {m.author_name}\n"
            f"Acción sugerida: {m.suggested_action}\n"
            f"Por qué: {m.reason}"
        )
        # source_note único: channel_id + ts (uno por mensaje)
        source_note = f"{m.channel_id}::{m.permalink}" if m.permalink else m.channel_id
        tasks.append(ExtractedTask(
            title=title,
            context=context,
            estimated_minutes=None,
            deadline_hint=None,
            source_note=source_note,
            source="slack",
            project="Slack",
        ))
    return tasks, len(messages), len(actionable)


def build_briefing(briefing_date: Optional[date] = None) -> BriefingResult:
    """Briefing matutino:

    1. Ingesta de nuevas tareas: notas Granola modificadas → extract → priorize → insert.
    2. Repriorización diaria: leer TODAS las open tasks y repriorizar según hoy.
    3. Update priorities en DB si cambiaron.

    Resultado: snapshot del estado de hoy, no solo lo nuevo desde ayer.
    """
    tz = _tz()
    today_local = datetime.now(tz)
    if briefing_date is None:
        briefing_date = today_local.date()

    today_events = _today_events(today_local)
    user_id = _user_id()

    # --- DESACTIVADO (2026-06-11): Granola/Obsidian fuera del briefing.
    # Preferimos trabajar las notas directamente con Claude, no desde el bot.
    # Para reactivar: descomentar los tres bloques de abajo.
    # window_start = _window_start(briefing_date, tz)
    # log.info("Ventana de notas: desde %s", window_start.isoformat())
    # granola_tasks, processed, skipped = _extract_from_granola(window_start)
    # if granola_tasks:
    #     new_prioritized = prioritize(granola_tasks, today_iso=briefing_date.isoformat())
    #     _persist_prioritized(new_prioritized, briefing_date, source="granola")
    processed, skipped = 0, 0

    # 1b. Ingesta Gmail: correos pendientes filtrados por LLM (últimos 7 días).
    gmail_tasks, emails_pending, emails_actionable = _extract_from_gmail(days=7)
    if gmail_tasks:
        new_prioritized = prioritize(gmail_tasks, today_iso=briefing_date.isoformat())
        _persist_prioritized(new_prioritized, briefing_date, source="gmail")

    # --- DESACTIVADO (2026-06-02): Slack fuera del briefing por ahora.
    # 1c. Ingesta Slack: DMs + menciones filtrados por LLM (últimos 7 días).
    # slack_tasks, slack_pending, slack_actionable = _extract_from_slack(days=7)
    # if slack_tasks:
    #     new_prioritized = prioritize(slack_tasks, today_iso=briefing_date.isoformat())
    #     _persist_prioritized(new_prioritized, briefing_date, source="slack")

    # 2. Repriorización del estado completo
    try:
        open_tasks = db.list_open_tasks(user_id)
    except Exception:
        log.exception("No se pudieron leer open tasks")
        open_tasks = []

    # Excluir tareas de fuentes inactivas (Granola/Slack): quedaron 'open' en DB
    # pero ya no se ingieren ni se auto-cierran, así que no deben repriorizarse.
    open_tasks = [
        t for t in open_tasks
        if (t.get("source") or "granola") in _ACTIVE_TASK_SOURCES
    ]

    prioritized: List[PrioritizedTask] = []
    if open_tasks:
        prioritized = prioritize_open_tasks(open_tasks, today_iso=briefing_date.isoformat())
        # 3. Persistir cambios de priority
        previous = {str(t["id"]): t.get("priority") for t in open_tasks}
        for pt in prioritized:
            if pt.task_id and previous.get(pt.task_id) != pt.priority:
                try:
                    db.update_task_priority(pt.task_id, pt.priority)
                except Exception:
                    log.exception("No pude actualizar priority de %s", pt.task_id)

    # 4. Outbound: mensajes que Daniel envió y aún esperan respuesta (Gmail).
    awaiting = _build_awaiting_reply()

    return BriefingResult(
        briefing_date=briefing_date,
        today_events=today_events,
        prioritized=prioritized,
        notes_processed=processed,
        notes_skipped_age=skipped,
        emails_pending=emails_pending,
        emails_actionable=emails_actionable,
        awaiting_reply=awaiting,
        # slack_pending/slack_actionable quedan en 0 mientras Slack esté desactivado.
    )


def _build_awaiting_reply() -> List[AwaitingItem]:
    """Recolecta outbound de Gmail (+ Slack cuando esté activo), filtra con LLM."""
    try:
        gmail_out = list_gmail_awaiting(days=14, min_age_hours=24)
    except Exception:
        log.exception("Error listando Gmail awaiting reply")
        gmail_out = []

    # --- DESACTIVADO (2026-06-02): Slack outbound.
    # try:
    #     slack_out = list_slack_awaiting(days=7, min_age_hours=6)
    # except RuntimeError:
    #     log.info("Slack no está configurado; skip outbound")
    #     slack_out = []
    # except Exception:
    #     log.exception("Error listando Slack awaiting reply")
    #     slack_out = []
    slack_out = []

    if not gmail_out and not slack_out:
        return []

    try:
        return filter_awaiting_reply(gmail_out, slack_out)
    except Exception:
        log.exception("Error filtrando outbound con LLM")
        return []
