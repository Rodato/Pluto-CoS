"""Cron jobs con APScheduler.

- v1: cada 15 min chequea invitaciones nuevas.
- Fase 1 CoS: diario 8 AM Bogotá genera el briefing matutino.
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

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
    horizon = now + timedelta(days=30)

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

    try:
        await tg_app.bot.send_message(
            chat_id=int(chat_id_env),
            text=render_telegram(briefing),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Error enviando briefing por Telegram")
