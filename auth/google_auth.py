"""OAuth2 Installed flow para Google Calendar API.

Patrón calcado de ai-mail-forwarder/auth.py, con un ajuste: en un bot
long-running NO abrimos browser desde el handler — si no hay token, raise.
La auth se hace con `oauth_local.py` antes de arrancar el bot.

- Local: `credentials.json` + `token.json` en la raíz del proyecto.
- Railway: `GOOGLE_CREDENTIALS_JSON` + `GOOGLE_TOKEN_JSON` como env vars
  (aceptan JSON crudo o base64).

Scope `calendar.events`: leer + events.patch sobre attendees[me].responseStatus.
Single-user — no se persiste en DB.
"""

import base64
import json
import os
import re
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
]

BASE_DIR = Path(__file__).resolve().parent.parent
TOKEN_PATH = BASE_DIR / "token.json"
CREDENTIALS_PATH = BASE_DIR / "credentials.json"


def _parse_env_json(value: str) -> dict:
    """Acepta JSON crudo o base64. Sanea control chars que algunos dashboards
    (Railway) insertan al pegar JSON largo."""
    value = value.strip()
    cleaned = re.sub(r"[\x00-\x1f]+", "", value)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return json.loads(base64.b64decode(value))


def _get_client_config() -> dict:
    creds_env = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_env:
        return _parse_env_json(creds_env)
    if CREDENTIALS_PATH.exists():
        return json.loads(CREDENTIALS_PATH.read_text())
    raise FileNotFoundError(
        "No hay credenciales de Google. Definí GOOGLE_CREDENTIALS_JSON "
        "o colocá credentials.json en la raíz del proyecto."
    )


def _save_token(creds: Credentials) -> None:
    # En Railway el token viene de env var; no escribimos al disco.
    if not os.getenv("GOOGLE_TOKEN_JSON"):
        TOKEN_PATH.write_text(creds.to_json())


def get_credentials() -> Credentials:
    creds: Optional[Credentials] = None

    token_env = os.getenv("GOOGLE_TOKEN_JSON")
    if token_env:
        creds = Credentials.from_authorized_user_info(_parse_env_json(token_env), SCOPES)
    elif TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    else:
        raise RuntimeError(
            "No hay token de Google. Corré primero:\n"
            "    .venv/bin/python oauth_local.py"
        )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _save_token(creds)
        else:
            raise RuntimeError(
                "Token de Google inválido y sin refresh_token. "
                "Regenerá con .venv/bin/python oauth_local.py"
            )

    return creds


def get_calendar_service():
    """Devuelve un servicio autenticado de Google Calendar API v3."""
    return build("calendar", "v3", credentials=get_credentials(), cache_discovery=False)


def get_gmail_service():
    """Devuelve un servicio autenticado de Gmail API v1."""
    return build("gmail", "v1", credentials=get_credentials(), cache_discovery=False)
