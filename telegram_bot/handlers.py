"""Handlers de comandos y callbacks — v1.

Comandos:
- /start, /help → ayuda
- /hoy        → eventos de hoy
- /semana     → agenda de la semana
- /libre      → "/libre martes 3pm" → ¿hay algo a esa hora?
- /revisar    → fuerza chequeo manual de invitaciones (sin esperar al cron)
- /autorizar  → instrucciones para correr el OAuth flow local

Callbacks:
- rsvp:<accepted|declined|tentative>:<event_id>  → events.patch + DB update

Mensajes libres → llm/query.answer_query
"""

import asyncio
import html
import logging
import os
from datetime import datetime, timedelta
from functools import wraps
from typing import Callable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from calendar_api import client as cal_client
from db import client as db
from llm import query as llm_query

log = logging.getLogger(__name__)


def _tz():
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _authorized_chat_id() -> int:
    return int(os.environ["TELEGRAM_CHAT_ID"])


def authorized_only(func: Callable):
    """Rechaza cualquier interacción que no venga del chat autorizado."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat = update.effective_chat
        if chat is None or chat.id != _authorized_chat_id():
            log.warning("Chat no autorizado: %s", chat.id if chat else None)
            return
        return await func(update, context)
    return wrapper


# ============================================================
# Comandos
# ============================================================

@authorized_only
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>calendar-planner</b>\n"
        "Comandos disponibles:\n"
        "/hoy — eventos de hoy\n"
        "/semana — agenda de la semana\n"
        "/libre — slots libres\n"
        "/revisar — chequear invitaciones ya\n"
        "/autorizar — conectar Google Calendar\n\n"
        "También podés escribirme en lenguaje natural ('¿qué tengo el viernes?', "
        "'¿cuándo tengo libre esta semana?')."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_autorizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔐 La autorización de Google se hace localmente:\n\n"
        "1. En tu Mac, corré:\n"
        "   <code>.venv/bin/python oauth_local.py</code>\n"
        "2. Se abre el browser. Consentís con tu cuenta Google.\n"
        "3. Se guarda <code>token.json</code> local y se muestra el base64 para pegar en Railway como <code>GOOGLE_TOKEN_JSON</code>.\n\n"
        "Tokens no caducan (la app está In Production en Google Cloud).",
        parse_mode=ParseMode.HTML,
    )


def _fmt_event_line(event: dict) -> str:
    from telegram_bot.bot import _fmt_dt
    title = event.get("summary") or "(sin título)"
    start = _fmt_dt(event.get("start", {}))
    return f"• <b>{html.escape(start)}</b> — {html.escape(title)}"


@authorized_only
async def cmd_hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = _tz()
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    try:
        events = await asyncio.to_thread(cal_client.list_events, start, end)
    except RuntimeError:
        await update.message.reply_text("No estás autorizado. Corré /autorizar primero.")
        return

    if not events:
        await update.message.reply_text("📅 Hoy no tenés nada agendado.")
        return

    lines = ["📅 <b>Hoy</b>"] + [_fmt_event_line(e) for e in events]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_semana(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = _tz()
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=7)
    try:
        events = await asyncio.to_thread(cal_client.list_events, start, end)
    except RuntimeError:
        await update.message.reply_text("No estás autorizado. Corré /autorizar primero.")
        return

    if not events:
        await update.message.reply_text("📅 La semana viene vacía.")
        return

    lines = ["📅 <b>Próximos 7 días</b>"] + [_fmt_event_line(e) for e in events]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


@authorized_only
async def cmd_libre(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args).strip()
    if not arg:
        question = "¿Cuándo tengo huecos libres en los próximos 7 días?"
    else:
        question = f"¿Tengo libre {arg}?"
    answer = await asyncio.to_thread(llm_query.answer_query, question)
    await update.message.reply_text(answer)


@authorized_only
async def cmd_revisar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from scheduler import check_new_invitations
    sent = await check_new_invitations(context.application)
    await update.message.reply_text(
        f"🔎 Chequeo manual hecho. Invitaciones nuevas notificadas: {sent}."
    )


# ============================================================
# Callback RSVP
# ============================================================

RSVP_LABEL = {
    "accepted": "✅ Aceptaste",
    "declined": "❌ Rechazaste",
    "tentative": "❓ Marcaste como tentativo",
}


@authorized_only
async def on_rsvp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, response, event_id = query.data.split(":", 2)
    except ValueError:
        return

    try:
        await asyncio.to_thread(cal_client.respond_invitation, event_id, response)
        db.set_invitation_rsvp(event_id, response)
    except Exception as exc:
        log.exception("Error en RSVP")
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"⚠️ No pude responder: {exc}")
        return

    label = RSVP_LABEL.get(response, response)
    new_text = (query.message.text_html or query.message.text or "") + f"\n\n<i>{label}</i>"
    await query.edit_message_text(
        text=new_text, parse_mode=ParseMode.HTML, reply_markup=None
    )


# ============================================================
# Mensaje natural
# ============================================================

@authorized_only
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    await update.message.chat.send_action("typing")
    try:
        answer = await asyncio.to_thread(llm_query.answer_query, text)
    except Exception as exc:
        log.exception("Error en consulta natural")
        answer = f"⚠️ Error resolviendo la consulta: {exc}"
    await update.message.reply_text(answer)


# ============================================================
# Registro
# ============================================================

def register(app: Application) -> None:
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("autorizar", cmd_autorizar))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("semana", cmd_semana))
    app.add_handler(CommandHandler("libre", cmd_libre))
    app.add_handler(CommandHandler("revisar", cmd_revisar))
    app.add_handler(CallbackQueryHandler(on_rsvp, pattern=r"^rsvp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
