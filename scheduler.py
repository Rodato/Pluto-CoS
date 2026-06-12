"""Cron jobs con APScheduler.

- v1: cada 15 min chequea invitaciones nuevas.
- Fase 1 CoS: diario 8 AM Bogotá genera el briefing matutino.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from calendar_api import client as cal_client
from db import client as db
from telegram_bot import bot as tg_bot

log = logging.getLogger(__name__)


def build_scheduler(invitations_callback, briefing_callback=None) -> AsyncIOScheduler:
    """Scheduler con dos jobs: invitaciones cada 15 min + briefing diario."""
    tz = os.environ.get("TZ_NAME", "America/Bogota")
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        invitations_callback,
        IntervalTrigger(minutes=15),
        id="invitations_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    if briefing_callback is not None:
        briefing_hour = int(os.environ.get("BRIEFING_HOUR", "8"))
        scheduler.add_job(
            briefing_callback,
            CronTrigger(hour=briefing_hour, minute=0, timezone=tz),
            id="daily_briefing",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            # Si el bot estaba caído a las 8 AM, dispara al reanudar (hasta 1h después).
            misfire_grace_time=3600,
        )

    return scheduler


async def check_new_invitations(tg_app: Application) -> int:
    """Lista invitaciones pending del próximo horizonte y notifica las no vistas.

    Devuelve cuántas notificaciones envió (para logs).
    """
    user_id = os.environ.get("USER_ID", "daniel")
    now = datetime.now(timezone.utc)

    # Horizonte = final de la semana en curso (próximo lunes 00:00 hora local).
    tz = ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))
    local_now = now.astimezone(tz)
    days_until_next_monday = (7 - local_now.weekday()) % 7 or 7
    end_of_week_local = (local_now + timedelta(days=days_until_next_monday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    horizon = end_of_week_local.astimezone(timezone.utc)

    try:
        pending = cal_client.list_pending_invitations(now, horizon)
    except RuntimeError as exc:
        log.warning("No se pudo chequear invitaciones (¿falta /autorizar?): %s", exc)
        return 0
    except Exception:
        log.exception("Error listando invitaciones")
        return 0

    sent = 0
    for event in pending:
        event_id = event.get("id")
        if not event_id or db.is_invitation_seen(event_id):
            continue

        try:
            message_id = await tg_bot.send_invitation(tg_app, event)
            db.mark_invitation_seen(event_id, user_id=user_id, telegram_message_id=message_id)
            sent += 1
        except Exception:
            log.exception("Error notificando invitación %s", event_id)

    if sent:
        log.info("Invitaciones notificadas: %d", sent)
    return sent


TRACK_WINDOW_DAYS = 30


def _group_by_series(notices):
    """Colapsa avisos de instancias de la misma serie recurrente.

    notices: lista de tuplas cuyo primer elemento es la key de agrupación
    (recurringEventId o event_id). Devuelve [(primera_tupla, extra_count)].
    """
    grouped: dict = {}
    order: list = []
    for notice in notices:
        key = notice[0]
        if key in grouped:
            grouped[key] += 1
        else:
            grouped[key] = 0
            order.append(notice)
    return [(notice, grouped[notice[0]]) for notice in order]


async def check_event_changes(tg_app: Application) -> dict:
    """Diffea la agenda contra el snapshot (tracked_events) y notifica:

    - eventos nuevos donde te citaron sin RSVP pendiente (los needsAction
      ya los notifica check_new_invitations con botones),
    - reprogramaciones / cambios de invitados, lugar, título o mensaje,
    - cancelaciones y remociones de la lista de invitados.

    Devuelve conteos {"nuevos", "cambios", "cancelados"} para logs y /revisar.
    """
    import json

    from googleapiclient.errors import HttpError

    from calendar_api import tracker
    from telegram_bot.bot import (
        _fmt_dt,
        format_event_cancelled,
        format_event_change,
        format_event_removed,
        format_new_event,
        send_notice,
    )

    counts = {"nuevos": 0, "cambios": 0, "cancelados": 0}
    user_id = os.environ.get("USER_ID", "daniel")
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=TRACK_WINDOW_DAYS)

    try:
        events = cal_client.list_events_with_deleted(now, horizon)
    except RuntimeError as exc:
        log.warning("No se pudo chequear cambios (¿falta /autorizar?): %s", exc)
        return counts
    except Exception:
        log.exception("Error listando eventos para el watcher")
        return counts

    try:
        tracked = db.get_tracked_events(user_id)
    except Exception:
        log.exception("Error leyendo tracked_events")
        return counts

    fetched_ids = {e.get("id") for e in events}
    new_notices: list = []      # (key, event, response_status)
    change_notices: list = []   # (key, event, changes, needs_rsvp, event_id)
    cancel_notices: list = []   # (key, summary, start_label)
    removed_notices: list = []  # (summary, start_label)

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue

        if event.get("status") == "cancelled":
            row = tracked.get(event_id)
            if row:
                key = row.get("recurring_event_id") or event.get("recurringEventId") or event_id
                start_label = _fmt_dt(json.loads(row["start_raw"]))
                cancel_notices.append((key, row["summary"], start_label))
                db.delete_tracked_event(event_id)
            continue

        if not tracker.is_relevant(event):
            continue

        snap = tracker.snapshot_from_event(event)
        row = tracked.get(event_id)

        if row is None:
            db.upsert_tracked_event(user_id, snap)
            response = tracker.my_response_status(event)
            # needsAction lo cubre check_new_invitations; acá solo auto-aceptados etc.
            # created_recently evita spamear toda la agenda en el primer arranque.
            if (
                response != "needsAction"
                and tracker.created_recently(event)
                and not db.is_invitation_seen(event_id)
            ):
                key = event.get("recurringEventId") or event_id
                new_notices.append((key, event, response))
            continue

        changes = tracker.diff_snapshots(row, snap, event, _fmt_dt)
        if changes:
            db.upsert_tracked_event(user_id, snap)
            key = event.get("recurringEventId") or event_id
            needs_rsvp = tracker.my_response_status(event) == "needsAction"
            change_notices.append((key, event, changes, needs_rsvp, event_id))

    # Eventos rastreados que desaparecieron del listado con start futuro:
    # o los cancelaron (stub fuera de ventana) o te sacaron de la invitación.
    for event_id, row in tracked.items():
        if event_id in fetched_ids:
            continue
        if not row.get("start_ts") or row["start_ts"] <= now:
            continue
        start_label = _fmt_dt(json.loads(row["start_raw"]))
        try:
            current = cal_client.get_event(event_id)
        except HttpError as exc:
            if exc.resp.status in (403, 404, 410):
                removed_notices.append((row["summary"], start_label))
                db.delete_tracked_event(event_id)
            else:
                log.warning("No pude verificar evento ausente %s: %s", event_id, exc)
            continue
        except Exception:
            log.exception("Error verificando evento ausente %s", event_id)
            continue

        if current.get("status") == "cancelled":
            key = row.get("recurring_event_id") or event_id
            cancel_notices.append((key, row["summary"], start_label))
            db.delete_tracked_event(event_id)
        else:
            # Se movió fuera de la ventana de tracking → notificar como cambio.
            snap = tracker.snapshot_from_event(current)
            changes = tracker.diff_snapshots(row, snap, current, _fmt_dt)
            db.upsert_tracked_event(user_id, snap)
            if changes:
                key = current.get("recurringEventId") or event_id
                needs_rsvp = tracker.my_response_status(current) == "needsAction"
                change_notices.append((key, current, changes, needs_rsvp, event_id))

    # --- Envío (agrupando series recurrentes para no spamear) ---
    for (key, event, response), extra in _group_by_series(new_notices):
        text = format_new_event(event, response)
        if extra:
            text += f"\n🔁 <i>Serie recurrente: +{extra} fecha(s) más.</i>"
        try:
            await send_notice(tg_app, text)
            counts["nuevos"] += 1
        except Exception:
            log.exception("Error notificando evento nuevo %s", event.get("id"))

    for (key, event, changes, needs_rsvp, event_id), extra in _group_by_series(change_notices):
        text = format_event_change(event, changes, recurring_extra=extra)
        try:
            message_id = await send_notice(tg_app, text, with_rsvp=needs_rsvp)
            if needs_rsvp and message_id:
                db.update_invitation_message_id(event_id, user_id, message_id)
            counts["cambios"] += 1
        except Exception:
            log.exception("Error notificando cambio en %s", event_id)

    for (key, summary, start_label), extra in _group_by_series(cancel_notices):
        try:
            await send_notice(tg_app, format_event_cancelled(summary, start_label, extra))
            counts["cancelados"] += 1
        except Exception:
            log.exception("Error notificando cancelación de %s", key)

    for summary, start_label in removed_notices:
        try:
            await send_notice(tg_app, format_event_removed(summary, start_label))
            counts["cancelados"] += 1
        except Exception:
            log.exception("Error notificando remoción de evento")

    try:
        db.purge_past_tracked_events()
    except Exception:
        log.exception("Error purgando tracked_events viejos")

    total = sum(counts.values())
    if total:
        log.info("Watcher de cambios: %s", counts)
    return counts


async def run_daily_briefing(tg_app: Application) -> None:
    """Construye el briefing matutino y lo envía al chat autorizado."""
    from briefing.builder import build_briefing
    from briefing.render import render_markdown, render_telegram
    from briefing.vault_writer import write_briefing
    from telegram.constants import ParseMode

    chat_id_env = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id_env:
        log.warning("TELEGRAM_CHAT_ID no definido — no envío briefing")
        return

    try:
        briefing = await asyncio.to_thread(build_briefing)
    except Exception:
        log.exception("Error construyendo el briefing")
        return

    try:
        await asyncio.to_thread(write_briefing, briefing.briefing_date, render_markdown(briefing))
    except Exception:
        log.exception("Error escribiendo briefing al vault (sigo con Telegram)")

    chunks = await asyncio.to_thread(render_telegram, briefing)
    for chunk in chunks:
        try:
            await tg_app.bot.send_message(
                chat_id=int(chat_id_env),
                text=chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Error enviando chunk del briefing por Telegram")
            return
