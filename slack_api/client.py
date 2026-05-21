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
        ))

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
