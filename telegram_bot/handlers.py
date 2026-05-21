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

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
        "/briefing — generar briefing matutino on-demand\n"
        "/correos — correos pendientes de respuesta\n"
        "/slack — mensajes Slack pendientes\n"
        "/pendientes — lista de tareas abiertas + botones ✅\n"
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


@authorized_only
async def cmd_briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Genera el briefing on-demand sin esperar al cron de las 8 AM."""
    from briefing.builder import build_briefing
    from briefing.render import render_markdown, render_telegram
    from briefing.vault_writer import write_briefing

    await update.message.chat.send_action("typing")
    try:
        briefing = await asyncio.to_thread(build_briefing)
    except Exception as exc:
        log.exception("Error construyendo briefing")
        await update.message.reply_text(f"⚠️ Error construyendo el briefing: {exc}")
        return

    try:
        await asyncio.to_thread(write_briefing, briefing.briefing_date, render_markdown(briefing))
    except Exception:
        log.exception("Error escribiendo briefing al vault (sigo con Telegram)")

    await update.message.reply_text(
        render_telegram(briefing),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@authorized_only
async def cmd_correos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista correos que requieren respuesta — heurística Gmail + filtro LLM."""
    from gmail_api.client import gmail_link, list_pending_for_reply
    from llm.email_filter import filter_actionable

    await update.message.chat.send_action("typing")
    try:
        threads = await asyncio.to_thread(list_pending_for_reply, 7, 50)
    except RuntimeError:
        await update.message.reply_text("No estás autorizado para Gmail. Reautorizá con /autorizar.")
        return
    except Exception as exc:
        log.exception("Error listando correos pendientes")
        await update.message.reply_text(f"⚠️ Error consultando Gmail: {exc}")
        return

    if not threads:
        await update.message.reply_text("📭 No hay correos pendientes en los últimos 7 días.")
        return

    actionable = await asyncio.to_thread(filter_actionable, threads)
    if not actionable:
        await update.message.reply_text(
            f"📭 De {len(threads)} correos pendientes ninguno parece pedir respuesta concreta."
        )
        return

    lines = [f"📬 <b>Correos que esperan respuesta</b> ({len(actionable)} de {len(threads)} pendientes)"]
    for em in actionable[:15]:
        subject = html.escape(em.subject[:80])
        from_short = html.escape(em.from_addr.split("<")[0].strip() or em.from_addr)
        link = gmail_link(em.thread_id)
        lines.append(
            f"\n• <a href=\"{link}\"><b>{subject}</b></a>\n"
            f"  <i>De: {from_short}</i>\n"
            f"  → {html.escape(em.suggested_action)}\n"
            f"  <i>{html.escape(em.reason)}</i>"
        )
    if len(actionable) > 15:
        lines.append(f"\n<i>+ {len(actionable) - 15} más en Gmail</i>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


PRIORITY_EMOJI_MAP = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}
PRIORITY_LABEL_MAP = {
    "P0": "Bloquea hoy",
    "P1": "Compromiso esta semana",
    "P2": "Foco CTO esta semana",
    "P3": "Medio plazo / admin",
}
PENDIENTES_TOP_PER_PRIORITY = 5
PENDIENTES_PRIORITIES = ("P0", "P1", "P2", "P3")


@authorized_only
async def cmd_pendientes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista top open tasks por priority con botones ✅ para marcar como done."""
    import os
    user_id = os.environ.get("USER_ID", "daniel")

    await update.message.chat.send_action("typing")
    try:
        all_open = await asyncio.to_thread(db.list_open_tasks, user_id)
    except Exception as exc:
        log.exception("Error listando open tasks")
        await update.message.reply_text(f"⚠️ Error: {exc}")
        return

    if not all_open:
        await update.message.reply_text("📭 No tenés tareas abiertas. Buen trabajo.")
        return

    # Agrupar y cap por priority
    grouped: dict = {p: [] for p in PENDIENTES_PRIORITIES}
    for t in all_open:
        prio = t.get("priority", "P2")
        grouped.setdefault(prio, []).append(t)

    lines = ["📋 <b>Tareas pendientes</b> — tocá ✅ para marcar hecha"]
    button_rows: list = []
    current_row: list = []
    counter = 0
    shown_tasks: list = []  # mantenemos orden para el mapping número→task_id

    for prio in PENDIENTES_PRIORITIES:
        items = grouped.get(prio, [])
        if not items:
            continue
        emoji = PRIORITY_EMOJI_MAP.get(prio, "•")
        total = len(items)
        shown_items = items[:PENDIENTES_TOP_PER_PRIORITY]
        header = f"\n{emoji} <b>{prio}</b>"
        if total > len(shown_items):
            header += f" <i>({len(shown_items)} de {total})</i>"
        lines.append(header)
        for t in shown_items:
            counter += 1
            shown_tasks.append(t)
            project = t.get("project") or "Varios"
            title = t.get("title") or "(sin título)"
            lines.append(
                f"<b>{counter}.</b> {html.escape(title)} <i>· {html.escape(project)}</i>"
            )
            # callback_data: done:<uuid>  (max 64 bytes; uuid es 36 chars)
            btn = InlineKeyboardButton(
                text=f"✅ {counter}",
                callback_data=f"done:{t.get('id')}",
            )
            current_row.append(btn)
            if len(current_row) == 4:
                button_rows.append(current_row)
                current_row = []
    if current_row:
        button_rows.append(current_row)

    reply_markup = InlineKeyboardMarkup(button_rows) if button_rows else None
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )


@authorized_only
async def on_task_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback handler: marca una tarea como done y quita el botón del keyboard."""
    query = update.callback_query
    await query.answer()
    try:
        _, task_id = query.data.split(":", 1)
    except ValueError:
        return

    try:
        await asyncio.to_thread(db.update_task_status, task_id, "done")
    except Exception as exc:
        log.exception("Error marcando task done")
        await query.answer(f"⚠️ {exc}", show_alert=True)
        return

    # Quitar este botón del keyboard pero mantener los demás.
    new_rows = []
    for row in (query.message.reply_markup.inline_keyboard if query.message.reply_markup else []):
        new_row = [b for b in row if b.callback_data != query.data]
        if new_row:
            new_rows.append(new_row)
    new_markup = InlineKeyboardMarkup(new_rows) if new_rows else None
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except Exception:
        pass

    await query.answer("✅ Marcada como hecha")


@authorized_only
async def cmd_slack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Lista mensajes de Slack que requieren tu respuesta — DMs + menciones + LLM filter."""
    from llm.slack_filter import filter_actionable
    from slack_api.client import list_all_pending

    await update.message.chat.send_action("typing")
    try:
        messages = await asyncio.to_thread(list_all_pending, 7)
    except RuntimeError as exc:
        await update.message.reply_text(f"⚠️ Slack no está configurado: {exc}")
        return
    except Exception as exc:
        log.exception("Error listando Slack")
        await update.message.reply_text(f"⚠️ Error consultando Slack: {exc}")
        return

    if not messages:
        await update.message.reply_text("📭 Slack tranquilo. Sin DMs ni menciones pendientes en 7 días.")
        return

    actionable = await asyncio.to_thread(filter_actionable, messages)
    if not actionable:
        await update.message.reply_text(
            f"📭 De {len(messages)} mensajes pendientes en Slack, ninguno parece pedir respuesta."
        )
        return

    lines = [
        f"💬 <b>Slack — mensajes que esperan respuesta</b> "
        f"({len(actionable)} de {len(messages)} pendientes)"
    ]
    for m in actionable[:15]:
        author = html.escape(m.author_name)
        channel = html.escape(m.channel_name)
        text_preview = html.escape(m.text[:140])
        link_label = "abrir en Slack"
        link_html = (
            f' <a href="{m.permalink}">[{link_label}]</a>'
            if m.permalink else ""
        )
        lines.append(
            f"\n• <b>{author}</b> en <i>{channel}</i>{link_html}\n"
            f"  → {html.escape(m.suggested_action)}\n"
            f"  <i>{html.escape(m.reason)}</i>\n"
            f"  <code>{text_preview}</code>"
        )
    if len(actionable) > 15:
        lines.append(f"\n<i>+ {len(actionable) - 15} más en Slack</i>")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
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
    app.add_handler(CommandHandler("briefing", cmd_briefing))
    app.add_handler(CommandHandler("correos", cmd_correos))
    app.add_handler(CommandHandler("slack", cmd_slack))
    app.add_handler(CommandHandler("pendientes", cmd_pendientes))
    app.add_handler(CallbackQueryHandler(on_task_done, pattern=r"^done:"))
    app.add_handler(CallbackQueryHandler(on_rsvp, pattern=r"^rsvp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
