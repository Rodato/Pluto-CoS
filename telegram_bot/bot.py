"""Bootstrap del bot de Telegram + envío de invitaciones.

Single-user: handlers rechazan mensajes que no vengan de TELEGRAM_CHAT_ID.
"""

import html
import os
from datetime import datetime
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # type: ignore

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application


def build_app() -> Application:
    token = os.environ["TELEGRAM_TOKEN"]
    return Application.builder().token(token).build()


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TZ_NAME", "America/Bogota"))


def _fmt_dt(value: dict) -> str:
    """Formatea event.start/end ({'dateTime'} o {'date'}) para humanos."""
    if "dateTime" in value:
        dt = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")).astimezone(_tz())
        return dt.strftime("%a %d %b · %H:%M")
    if "date" in value:
        return f"{value['date']} (todo el día)"
    return "?"


def _fmt_organizer(event: dict) -> str:
    org = event.get("organizer") or {}
    name = org.get("displayName") or org.get("email") or "—"
    return name


def _fmt_attendees(event: dict, limit: int = 6) -> str:
    attendees = event.get("attendees") or []
    others = [
        (a.get("displayName") or a.get("email", "?"))
        for a in attendees
        if not a.get("self")
    ]
    if not others:
        return "—"
    shown = others[:limit]
    extra = len(others) - len(shown)
    text = ", ".join(shown)
    if extra > 0:
        text += f" (+{extra} más)"
    return text


def format_invitation(event: dict) -> str:
    title = event.get("summary") or "(sin título)"
    organizer = _fmt_organizer(event)
    start = _fmt_dt(event.get("start", {}))
    end = _fmt_dt(event.get("end", {}))
    attendees = _fmt_attendees(event)
    desc = (event.get("description") or "").strip()
    location = (event.get("location") or "").strip()

    lines = [
        "📩 <b>Nueva invitación</b>",
        f"<b>{html.escape(title)}</b>",
        f"🗓 {html.escape(start)} → {html.escape(end)}",
        f"👤 Organiza: {html.escape(organizer)}",
        f"👥 Invitados: {html.escape(attendees)}",
    ]
    if location:
        lines.append(f"📍 {html.escape(location[:200])}")
    if desc:
        snippet = desc[:500] + ("…" if len(desc) > 500 else "")
        lines.append(f"\n{html.escape(snippet)}")
    return "\n".join(lines)


def rsvp_keyboard() -> InlineKeyboardMarkup:
    # callback_data corto (Telegram limita a 64 bytes). El event_id no entra:
    # event_ids de instancias recurrentes superan el límite. El handler resuelve
    # el event_id desde DB usando el message_id del callback.
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Aceptar", callback_data="rsvp:accepted"),
        InlineKeyboardButton("❓ Tentativo", callback_data="rsvp:tentative"),
        InlineKeyboardButton("❌ Rechazar", callback_data="rsvp:declined"),
    ]])


async def send_invitation(app: Application, event: dict) -> Optional[int]:
    """Envía una invitación al chat autorizado. Devuelve el message_id."""
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    msg = await app.bot.send_message(
        chat_id=chat_id,
        text=format_invitation(event),
        parse_mode=ParseMode.HTML,
        reply_markup=rsvp_keyboard(),
        disable_web_page_preview=True,
    )
    return msg.message_id
