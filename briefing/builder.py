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

log = logging.getLogger(__name__)

# Ventana de notas a procesar: desde el lunes de la SEMANA ANTERIOR (semana
# ISO local). Una nota con mtime previo a ese lunes se ignora.


@dataclass
class BriefingResult:
    briefing_date: date
    today_events: List[dict] = field(default_factory=list)
    prioritized: List[PrioritizedTask] = field(default_factory=list)
    notes_processed: int = 0
    notes_skipped_age: int = 0


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _user_id() -> str:
    return os.environ.get("USER_ID", "daniel")


def _today_events(today: datetime) -> List[dict]:
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    try:
        return cal_client.list_events(start, end)
    except Exception:
        log.exception("No se pudo leer Calendar para el briefing")
        return []


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


def _persist_prioritized(prioritized: List[PrioritizedTask], briefing_date: date) -> None:
    user_id = _user_id()
    for pt in prioritized:
        source_ref = f"{pt.source_note}::{pt.title.lower().strip()[:100]}"
        try:
            if db.task_exists_for_source("granola", source_ref):
                continue
            description = pt.context
            if pt.deadline_hint:
                description = (description + f"\n\nDeadline: {pt.deadline_hint}").strip()
            project = project_from_path(Path(pt.source_note)) if pt.source_note else "Varios"
            db.insert_task(
                user_id=user_id,
                title=pt.title,
                description=description or None,
                priority=pt.priority,
                project=project,
                source="granola",
                source_ref=source_ref,
                briefing_date=briefing_date,
            )
        except Exception:
            log.exception("Error persistiendo tarea %r", pt.title)


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

    # 1. Ingesta de notas nuevas dentro de la ventana (esta semana + anterior).
    window_start = _window_start(briefing_date, tz)
    log.info("Ventana de notas: desde %s", window_start.isoformat())
    extracted, processed, skipped = _extract_from_granola(window_start)
    if extracted:
        new_prioritized = prioritize(extracted, today_iso=briefing_date.isoformat())
        _persist_prioritized(new_prioritized, briefing_date)

    # 2. Repriorización del estado completo
    try:
        open_tasks = db.list_open_tasks(user_id)
    except Exception:
        log.exception("No se pudieron leer open tasks")
        open_tasks = []

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

    return BriefingResult(
        briefing_date=briefing_date,
        today_events=today_events,
        prioritized=prioritized,
        notes_processed=processed,
        notes_skipped_age=skipped,
    )
