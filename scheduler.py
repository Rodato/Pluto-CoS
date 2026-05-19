"""Cron jobs con APScheduler — v1.

Trigger: cada 15 min. Chequea invitaciones nuevas y las notifica por Telegram.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from telegram.ext import Application

from calendar_api import client as cal_client
from db import client as db
from telegram_bot import bot as tg_bot

log = logging.getLogger(__name__)


def build_scheduler(callback) -> AsyncIOScheduler:
    """Devuelve scheduler configurado con el check de invitaciones cada 15 min."""
    scheduler = AsyncIOScheduler(timezone=os.environ.get("TZ_NAME", "America/Bogota"))
    scheduler.add_job(
        callback,
        IntervalTrigger(minutes=15),
        id="invitations_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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
