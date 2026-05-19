# calendar-planner

Bot de Telegram **single-user** que se conecta con tu Google Calendar:

- Te avisa cuando llegan invitaciones nuevas, con título, organizer, hora, attendees y descripción.
- Te deja responder RSVP (✅ aceptar / ❌ rechazar / ❓ tentativo) con botones inline.
- Responde consultas sobre tu agenda en lenguaje natural (`/hoy`, `/semana`, `/libre`, o cualquier mensaje libre).

> **v1**: solo lee el calendar y permite RSVP. No crea, edita ni borra eventos. La extracción de tareas desde notas y el briefing matutino P0–P3 del agente CoS son fases siguientes.

## Stack
- **FastAPI + Uvicorn** — healthcheck para Railway (PORT abierto)
- **APScheduler** — cron cada 15 min para detectar invitaciones nuevas
- **python-telegram-bot 21.6** — interfaz Telegram
- **Google Calendar API v3** + OAuth2 **Installed flow** (scope `calendar.events`)
- **Neon** (psycopg2) — tracking de invitaciones notificadas (los tokens OAuth viven en disco/env var, no en DB)
- **OpenRouter** (SDK `openai` apuntando a `https://openrouter.ai/api/v1`) — consultas naturales
- **Deploy:** Railway

## Variables de entorno

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Connection string de Neon (con `sslmode=require`) |
| `TELEGRAM_TOKEN` | Token del bot (BotFather) |
| `TELEGRAM_CHAT_ID` | Chat ID autorizado (single-user) |
| `OPENROUTER_API_KEY` | API key de OpenRouter |
| `OPENROUTER_MODEL` | Modelo (default `anthropic/claude-sonnet-4-6`) |
| `USER_ID` | Identificador interno del usuario (default `daniel`) |
| `USER_EMAIL` | Email del usuario en Google Calendar (default `daniel@estudio-plural.co`) |
| `TZ_NAME` | Timezone (default `America/Bogota`) |
| `PORT` | Lo setea Railway. En local default `8080`. |
| `GOOGLE_CREDENTIALS_JSON` | **Solo Railway**: contenido de `credentials.json` en base64 |
| `GOOGLE_TOKEN_JSON` | **Solo Railway**: contenido de `token.json` en base64 (generado por `oauth_local.py`) |

En local, `GOOGLE_CREDENTIALS_JSON` y `GOOGLE_TOKEN_JSON` no se setean: se leen de `credentials.json` y `token.json` en la raíz del proyecto.

## Setup local

```bash
cd /Users/daniel/Desktop/Dev/calendar-planner

# 1) venv con Python 3.11 de Homebrew (NO el 3.9 del sistema)
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) Crear .env con las vars de arriba (sin GOOGLE_*_JSON; eso es solo Railway)

# 3) Colocar credentials.json en la raíz (compartido con ai-mail-forwarder)

# 4) Generar token.json (una sola vez)
.venv/bin/python oauth_local.py
# Se abre browser → consentimiento → token.json guardado + base64 mostrado en pantalla

# 5) Correr (FastAPI + bot + scheduler en un solo proceso)
.venv/bin/python main.py
```

## Setup externo (primera vez)

1. **BotFather** (Telegram) → crear bot → `TELEGRAM_TOKEN`. Mandarle `/start` a `@userinfobot` para tu `TELEGRAM_CHAT_ID`.
2. **OpenRouter** → registrarte → API key.
3. **Neon** → crear proyecto → SQL editor → correr `db/schema.sql` → copiar `DATABASE_URL` (con `?sslmode=require`).
4. **Google Cloud Console** (reusa el proyecto de ai-mail-forwarder, que ya está In Production):
   - **APIs & Services → Library** → habilitar **Google Calendar API**.
   - **OAuth consent screen → Edit app → Scopes** → agregar `https://www.googleapis.com/auth/calendar.events`.
   - El OAuth client (Desktop) ya existe — copiá `credentials.json` desde ai-mail-forwarder a la raíz de este proyecto.
5. **Railway** → conectar el repo → setear env vars (las non-Google primero; las Google en base64 vía `base64 -i credentials.json | pbcopy`).
6. En Railway, pegar `GOOGLE_CREDENTIALS_JSON` y `GOOGLE_TOKEN_JSON` (este último sale de `oauth_local.py` al correrlo en local).

## Comandos del bot

| Comando | Qué hace |
|---|---|
| `/hoy` | Eventos de hoy |
| `/semana` | Próximos 7 días |
| `/libre [<texto>]` | Slots libres. `/libre` solo: próximos 7 días. `/libre martes 3pm`: chequea ese momento. |
| `/revisar` | Fuerza chequeo de invitaciones (sin esperar al cron) |
| `/autorizar` | Instrucciones para correr `oauth_local.py` (la auth se hace local, no por link público) |

Mensajes libres → el LLM responde consultas naturales sobre la agenda (usa tool calling sobre Calendar API real, no inventa datos).

## Regenerar el token

La OAuth app está **In Production** en Google Cloud, así que los refresh tokens **no caducan** automáticamente. Solo hay que regenerar si revocás manualmente, cambiás scopes, o si Google lo invalida.

```bash
.venv/bin/python oauth_local.py
```

Te muestra la base64 del nuevo `token.json` para pegar en Railway como `GOOGLE_TOKEN_JSON`.

## Roadmap

**Fase 1 (CoS agente) — siguiente**: briefing matutino 8 AM Bogotá con tareas priorizadas P0–P3, leídas de Calendar + Granola/Obsidian, entregadas por Telegram y persistidas en tabla `tasks`. Las tablas ya están en `db/schema.sql`.

**Fase 2**: Gmail (compromisos en enviados, emails sin responder).
**Fase 3**: Slack (bloqueos del equipo + conocimiento transversal).
**Fase 4**: Sparring pages en Obsidian + loop de aprendizaje (priorizado vs hecho).
