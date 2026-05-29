"""Wrappers sobre Slack Web API — usa USER token (xoxp-...).

Fuentes de "pendientes":
1. DMs donde el último mensaje no es del user.
2. Menciones explícitas (@user_id) sin que el user haya respondido después.

REGLA: solo lectura. El bot no envía mensajes a Slack, no marca como leído.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import List, Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

log = logging.getLogger(__name__)


@dataclass
class PendingSlackMessage:
    source_type: str            # "dm" | "mention"
    channel_id: str
    channel_name: str           # "DM with X" o "#canal"
    author_id: str
    author_name: str
    text: str                   # cuerpo del mensaje
    ts: str                     # timestamp Slack (formato "1234567890.123456")
    received_at: datetime       # parsed UTC
    permalink: str              # URL al mensaje
    conversation_context: str = ""  # últimos N mensajes del canal (para detección de resolución)


@dataclass
class OutboundSlackMessage:
    """Mensaje que envió Daniel y aún no le respondieron."""
    source_type: str            # "dm" | "group_dm" | "channel"
    channel_id: str
    channel_name: str           # "DM con X" / "Grupo con X, Y" / "#nombre-canal"
    recipients: List[str]       # display_names (DMs) o ["#canal"] para canales
    text: str
    ts: str
    sent_at: datetime
    permalink: str


def _client() -> WebClient:
    token = os.environ.get("SLACK_USER_TOKEN")
    if not token:
        raise RuntimeError(
            "SLACK_USER_TOKEN no está definido. "
            "Crear Slack App en workspace + User OAuth Token (xoxp-...)."
        )
    return WebClient(token=token)


@lru_cache(maxsize=1)
def _my_user_id() -> str:
    """Cacheado: el user_id del owner del token."""
    resp = _client().auth_test()
    return resp["user_id"]


@lru_cache(maxsize=256)
def _user_display_name(user_id: str) -> str:
    """Devuelve real_name o display_name del usuario; fallback al id."""
    try:
        resp = _client().users_info(user=user_id)
        u = resp["user"]
        profile = u.get("profile", {}) or {}
        return (
            profile.get("display_name")
            or profile.get("real_name")
            or u.get("real_name")
            or u.get("name")
            or user_id
        )
    except SlackApiError:
        return user_id


def _ts_to_datetime(ts: str) -> datetime:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


def _permalink(channel_id: str, ts: str) -> str:
    try:
        resp = _client().chat_getPermalink(channel=channel_id, message_ts=ts)
        return resp.get("permalink") or ""
    except SlackApiError:
        return ""


def _is_skippable_message(msg: dict) -> bool:
    """Mensajes automáticos que no son accionables para Daniel."""
    if msg.get("subtype") in {
        "bot_message",
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "pinned_item",
        "unpinned_item",
        "reminder_add",
    }:
        return True
    if msg.get("bot_id"):
        return True
    return False


def _get_conversation_context(channel_id: str, around_ts: str, limit: int = 5) -> str:
    """Lee los últimos N mensajes del canal alrededor de `around_ts` para
    tener contexto conversacional (detección de resolución, confirmaciones).

    Devuelve string con formato "[author] mensaje" por línea.
    """
    cli = _client()
    try:
        # Lee más mensajes de los necesarios para poder incluir contexto previo
        hist = cli.conversations_history(channel=channel_id, limit=limit * 2)
    except SlackApiError:
        log.exception("No se pudo leer historial para contexto de %s", channel_id)
        return ""

    msgs = hist.get("messages", []) or []
    if not msgs:
        return ""

    # Ordenar cronológicamente (API devuelve más recientes primero)
    msgs.sort(key=lambda m: float(m.get("ts", "0") or 0))

    # Encontrar el mensaje target
    target_idx = next(
        (i for i, m in enumerate(msgs) if m.get("ts") == around_ts),
        None
    )
    if target_idx is None:
        # Si no encontramos el mensaje exacto, tomar los últimos N
        relevant = msgs[-limit:]
    else:
        # Tomar los N mensajes alrededor del target
        start = max(0, target_idx - limit // 2)
        end = min(len(msgs), target_idx + limit // 2 + 1)
        relevant = msgs[start:end]

    # Formatear como conversación legible
    lines = []
    for m in relevant:
        if _is_skippable_message(m):
            continue
        user_id = m.get("user", "")
        author = _user_display_name(user_id) if user_id else "Sistema"
        text = (m.get("text") or "").strip()[:200]
        if text:
            lines.append(f"[{author}] {text}")

    return "\n".join(lines[-limit:]) if lines else ""


def list_pending_dms(days: int = 7, max_dms: int = 50) -> List[PendingSlackMessage]:
    """DMs (1:1) donde el último mensaje no es del user y es reciente."""
    cli = _client()
    me = _my_user_id()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()

    try:
        resp = cli.conversations_list(types="im", limit=200, exclude_archived=True)
    except SlackApiError:
        log.exception("Slack conversations.list (im) falló")
        return []

    channels = resp.get("channels", []) or []
    result: List[PendingSlackMessage] = []

    for ch in channels[:max_dms]:
        ch_id = ch.get("id")
        other_user = ch.get("user")
        if not ch_id or not other_user:
            continue
        if ch.get("is_user_deleted"):
            continue

        try:
            hist = cli.conversations_history(channel=ch_id, limit=1)
        except SlackApiError:
            log.exception("conversations.history falló para %s", ch_id)
            continue

        msgs = hist.get("messages", []) or []
        if not msgs:
            continue
        last = msgs[0]

        ts_float = float(last.get("ts", "0") or 0)
        if ts_float < cutoff:
            continue
        if last.get("user") == me:
            continue
        if _is_skippable_message(last):
            continue

        author_id = last.get("user") or other_user
        author_name = _user_display_name(author_id)
        text = (last.get("text") or "").strip()
        if not text:
            continue

        ts = last.get("ts") or ""
        context = _get_conversation_context(ch_id, ts, limit=5)
        result.append(PendingSlackMessage(
            source_type="dm",
            channel_id=ch_id,
            channel_name=f"DM con {author_name}",
            author_id=author_id,
            author_name=author_name,
            text=text[:1500],
            ts=ts,
            received_at=_ts_to_datetime(ts),
            permalink=_permalink(ch_id, ts),
            conversation_context=context,
        ))

    return result


def list_unread_mentions(days: int = 7, max_results: int = 30) -> List[PendingSlackMessage]:
    """Menciones explícitas <@me> en cualquier canal/DM en últimos N días."""
    cli = _client()
    me = _my_user_id()

    after = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    query = f"<@{me}> after:{after}"

    try:
        resp = cli.search_messages(
            query=query,
            sort="timestamp",
            sort_dir="desc",
            count=max_results,
        )
    except SlackApiError:
        log.exception("Slack search.messages falló — verificá scope search:read")
        return []

    matches = (resp.get("messages") or {}).get("matches") or []
    result: List[PendingSlackMessage] = []

    for m in matches:
        if m.get("user") == me:
            continue
        if _is_skippable_message(m):
            continue
        channel = m.get("channel", {}) or {}
        ch_id = channel.get("id") or ""
        ch_name = channel.get("name") or channel.get("name_normalized") or "(canal)"
        author_id = m.get("user") or ""
        author_name = _user_display_name(author_id) if author_id else "(desconocido)"
        text = (m.get("text") or "").strip()
        if not text:
            continue
        ts = m.get("ts") or ""
        context = _get_conversation_context(ch_id, ts, limit=5)

        result.append(PendingSlackMessage(
            source_type="mention",
            channel_id=ch_id,
            channel_name=f"#{ch_name}" if not ch_name.startswith("#") else ch_name,
            author_id=author_id,
            author_name=author_name,
            text=text[:1500],
            ts=ts,
            received_at=_ts_to_datetime(ts),
            permalink=m.get("permalink") or _permalink(ch_id, ts),
            conversation_context=context,
        ))

    return result


def list_outbound_awaiting(
    days: int = 7,
    min_age_hours: int = 6,
    max_channels: int = 150,
) -> List[OutboundSlackMessage]:
    """Mensajes que Daniel envió en DMs, group DMs y canales (públicos +
    privados) cuyo último mensaje en el canal sigue siendo de él.

    Filtros baratos en esta capa:
    - Último mensaje del canal es de Daniel (con `users.conversations` +
      `conversations.history` 1).
    - `reply_count == 0`: si ya hay réplicas en thread, la conversación
      siguió por ahí → no es outbound pendiente.
    - Enviado entre `min_age_hours` y `days` atrás.

    NO filtra "ruido vs solicitud" — eso lo hace después el LLM
    (llm/outbound_filter), porque en canales abundan los mensajes de
    status/anuncio que parecen tareas pero no esperan respuesta.
    """
    cli = _client()
    me = _my_user_id()
    now = datetime.now(timezone.utc)
    cutoff_old = (now - timedelta(days=days)).timestamp()
    cutoff_recent = (now - timedelta(hours=min_age_hours)).timestamp()

    # Probamos con todos los tipos; si al token le falta `groups:read`
    # (canales privados), caemos a la combinación que sí tenga permiso.
    # Slack pagina, así que iteramos cursores hasta agotar.
    type_options = [
        "im,mpim,public_channel,private_channel",
        "im,mpim,public_channel",
        "im,mpim",
    ]
    channels: List[dict] = []
    chosen_types = None
    for types_str in type_options:
        try:
            cursor = None
            page_count = 0
            while page_count < 10:  # safety cap
                kwargs = {"types": types_str, "limit": 200, "exclude_archived": True}
                if cursor:
                    kwargs["cursor"] = cursor
                resp = cli.conversations_list(**kwargs)
                channels.extend(resp.get("channels", []) or [])
                cursor = (resp.get("response_metadata") or {}).get("next_cursor") or None
                page_count += 1
                if not cursor:
                    break
            chosen_types = types_str
            break
        except SlackApiError as e:
            err = e.response.get("error") if hasattr(e, "response") else str(e)
            log.info("conversations.list types=%s falló (%s) — pruebo fallback", types_str, err)
            channels = []
            continue

    if chosen_types is None:
        log.exception("Slack conversations.list falló con todos los fallbacks")
        return []
    if chosen_types != type_options[0]:
        log.warning(
            "Slack: token no tiene scope para todos los tipos; uso fallback types=%s. "
            "Para incluir canales privados sumá `groups:read` al token.",
            chosen_types,
        )
    # Para canales, priorizamos los que Daniel es miembro (xoxp ya filtra
    # esto en `im`/`mpim`; para canales hay que chequear `is_member`).
    relevant = [
        ch for ch in channels
        if ch.get("is_im") or ch.get("is_mpim") or ch.get("is_member")
    ]
    result: List[OutboundSlackMessage] = []

    for ch in relevant[:max_channels]:
        ch_id = ch.get("id")
        if not ch_id:
            continue
        if ch.get("is_user_deleted"):
            continue

        try:
            hist = cli.conversations_history(channel=ch_id, limit=1)
        except SlackApiError:
            log.exception("conversations.history falló para %s", ch_id)
            continue

        msgs = hist.get("messages", []) or []
        if not msgs:
            continue
        last = msgs[0]

        ts_float = float(last.get("ts", "0") or 0)
        if ts_float < cutoff_old:
            continue
        if ts_float > cutoff_recent:
            continue
        if last.get("user") != me:
            continue
        if _is_skippable_message(last):
            continue
        # Si ya hubo réplicas en thread, la conversación siguió por ahí.
        if int(last.get("reply_count", 0) or 0) > 0:
            continue

        text = (last.get("text") or "").strip()
        if not text:
            continue

        # Resolver destinatarios + nombre del canal.
        if ch.get("is_im"):
            other_user = ch.get("user") or ""
            other_name = _user_display_name(other_user) if other_user else "(desconocido)"
            channel_name = f"DM con {other_name}"
            recipients = [other_name]
            source_type = "dm"
        elif ch.get("is_mpim"):
            try:
                mem_resp = cli.conversations_members(channel=ch_id, limit=20)
                member_ids = [u for u in (mem_resp.get("members") or []) if u != me]
            except SlackApiError:
                member_ids = []
            recipients = [_user_display_name(u) for u in member_ids[:5]]
            extra = len(member_ids) - len(recipients)
            tail = f" (+{extra})" if extra > 0 else ""
            channel_name = f"Grupo con {', '.join(recipients)}{tail}"
            source_type = "group_dm"
        else:
            ch_name = ch.get("name") or ch.get("name_normalized") or ch_id
            channel_name = f"#{ch_name}"
            recipients = [channel_name]
            source_type = "channel"

        ts = last.get("ts") or ""
        result.append(OutboundSlackMessage(
            source_type=source_type,
            channel_id=ch_id,
            channel_name=channel_name,
            recipients=recipients,
            text=text[:1500],
            ts=ts,
            sent_at=_ts_to_datetime(ts),
            permalink=_permalink(ch_id, ts),
        ))

    result.sort(key=lambda x: x.sent_at, reverse=True)
    return result


def list_all_pending(days: int = 7) -> List[PendingSlackMessage]:
    """Combina DMs y menciones, deduplicando por (channel_id, ts)."""
    dms = list_pending_dms(days=days)
    mentions = list_unread_mentions(days=days)
    seen = set()
    combined: List[PendingSlackMessage] = []
    for m in dms + mentions:
        key = (m.channel_id, m.ts)
        if key in seen:
            continue
        seen.add(key)
        combined.append(m)
    combined.sort(key=lambda x: x.received_at, reverse=True)
    return combined
