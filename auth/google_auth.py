"""OAuth2 web flow para Google Calendar API (v1: lectura + RSVP).

Single-user: Daniel (daniel@estudio-plural.co). El token se guarda en DB
(tabla `oauth_tokens`) para sobrevivir restarts en Railway.

Scope `calendar.events`: necesario para leer y para responder RSVP
(events.patch sobre attendees[me].responseStatus).
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from db import client as db

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _client_config() -> dict:
    """Lee el JSON de credenciales Web OAuth desde env var."""
    raw = os.environ["GOOGLE_WEB_CREDENTIAL_JSON"]
    return json.loads(raw)


def _redirect_uri() -> str:
    return f"{os.environ['APP_BASE_URL'].rstrip('/')}/oauth/callback"


def _user_id() -> str:
    return os.environ.get("USER_ID", "daniel")


def build_auth_url(state: str) -> str:
    """URL a la que mandar al usuario para consentimiento Google."""
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=_redirect_uri()
    )
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # forzar entrega de refresh_token siempre
        state=state,
    )
    return auth_url


def exchange_code(code: str) -> Credentials:
    """Intercambia el `code` del callback por Credentials y los persiste en DB."""
    flow = Flow.from_client_config(
        _client_config(), scopes=SCOPES, redirect_uri=_redirect_uri()
    )
    flow.fetch_token(code=code)
    creds = flow.credentials
    _persist(creds)
    return creds


def _persist(creds: Credentials) -> None:
    expiry = creds.expiry
    if expiry is not None and expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    db.save_token(
        user_id=_user_id(),
        access_token=creds.token,
        refresh_token=creds.refresh_token,
        token_expiry=expiry or datetime.now(timezone.utc),
        scopes=list(creds.scopes or SCOPES),
    )


def load_credentials() -> Optional[Credentials]:
    """Reconstruye Credentials desde la DB. None si no hay token guardado."""
    row = db.get_token(_user_id())
    if not row:
        return None
    cfg = _client_config()
    web = cfg.get("web") or cfg.get("installed") or {}
    creds = Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri=web.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=web["client_id"],
        client_secret=web["client_secret"],
        scopes=list(row["scopes"]),
    )
    creds.expiry = row["token_expiry"].replace(tzinfo=None)  # google lib espera naive UTC
    return creds


def refresh_if_needed(creds: Credentials) -> Credentials:
    """Refresca el access_token si expiró y persiste el nuevo."""
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _persist(creds)
    return creds


def get_calendar_service():
    """Devuelve un servicio autenticado de Google Calendar API v3.

    Lanza RuntimeError si el usuario aún no autorizó (cliente debe pedir /autorizar).
    """
    creds = load_credentials()
    if creds is None:
        raise RuntimeError(
            "No hay credenciales guardadas. Corré /autorizar en Telegram primero."
        )
    creds = refresh_if_needed(creds)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)
