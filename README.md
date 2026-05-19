# calendar-planner

Bot de Telegram **single-user** que se conecta con tu Google Calendar:

- Te avisa cuando llegan invitaciones nuevas, con título, organizer, hora, attendees y descripción.
- Te deja responder RSVP (✅ aceptar / ❌ rechazar / ❓ tentativo) con botones inline.
- Responde consultas sobre tu agenda en lenguaje natural (`/hoy`, `/semana`, `/libre`, o cualquier mensaje libre).

> **v1**: solo lee el calendar y permite RSVP. No crea, edita ni borra eventos. La extracción de tareas desde notas de Granola y la creación de bloques de trabajo quedan para v2.

## Stack
- **FastAPI + Uvicorn** — servidor OAuth callback
- **APScheduler** — cron cada 15 min para detectar invitaciones nuevas
- **python-telegram-bot 21.6** — interfaz Telegram
- **Google Calendar API v3** + OAuth2 web flow (scope `calendar.events`)
- **Supabase** (psycopg2) — tokens OAuth + tracking de invitaciones notificadas
- **OpenRouter** (SDK `openai` apuntando a `https://openrouter.ai/api/v1`) — consultas naturales
- **Deploy:** Railway

## Variables de entorno

| Variable | Descripción |
|---|---|
| `DATABASE_URL` | Connection string de Supabase |
| `TELEGRAM_TOKEN` | Token del bot (BotFather) |
| `TELEGRAM_CHAT_ID` | Chat ID autorizado (single-user) |
| `OPENROUTER_API_KEY` | API key de OpenRouter |
| `OPENROUTER_MODEL` | Modelo (default `anthropic/claude-sonnet-4-6`) |
| `APP_BASE_URL` | URL pública de Railway (para callback OAuth) |
| `GOOGLE_WEB_CREDENTIAL_JSON` | Contenido completo del `credentials.json` (Web OAuth) |
| `USER_ID` | Identificador en `oauth_tokens` (default `daniel`) |
| `USER_EMAIL` | Email del usuario en Google Calendar (default `daniel@estudio-plural.co`) |
| `TZ_NAME` | Timezone (default `America/Bogota`) |
| `PORT` | Lo setea Railway. En local default `8080`. |

## Setup local

```bash
cd /Users/daniel/Desktop/Dev/calendar-planner

# 1) venv con Python 3.11 de Homebrew (NO el 3.9 del sistema)
/opt/homebrew/bin/python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2) Crear .env con las vars de arriba (copiar/pegar valores)

# 3) Correr (FastAPI + bot + scheduler en un solo proceso)
.venv/bin/python main.py
```

## Setup externo (primera vez)

1. **BotFather** (Telegram) → crear bot → `TELEGRAM_TOKEN`. Mandarle `/start` desde tu cuenta y anotar tu `chat_id` (lo da `@userinfobot`).
2. **OpenRouter** → registrarte → API key.
3. **Supabase** → crear proyecto → correr `db/schema.sql` desde el SQL editor → copiar `DATABASE_URL`.
4. **Google Cloud Console** → crear OAuth 2.0 client de tipo **Web application**:
   - Authorized redirect URIs: `https://<APP_BASE_URL>/oauth/callback` (Railway) y `http://localhost:8080/oauth/callback` (dev).
   - Habilitar Google Calendar API en el proyecto.
   - Descargar JSON → pegar contenido completo en env var `GOOGLE_WEB_CREDENTIAL_JSON`.
5. **Railway** → conectar el repo → setear todas las env vars → deploy.
6. **En Telegram**: `/autorizar` → seguir el link → consentimiento → vuelve a Telegram.

## Comandos del bot

| Comando | Qué hace |
|---|---|
| `/hoy` | Eventos de hoy |
| `/semana` | Próximos 7 días |
| `/libre [<texto>]` | Slots libres. `/libre` solo: próximos 7 días. `/libre martes 3pm`: chequea ese momento. |
| `/revisar` | Fuerza chequeo de invitaciones (sin esperar al cron) |
| `/autorizar` | Link al OAuth flow de Google |

Mensajes libres → el LLM responde consultas naturales sobre la agenda (usa tool calling sobre Calendar API real, no inventa datos).

## Roadmap v2
- Pipeline del vault de Granola (extraer tareas → proponer bloques → crear eventos)
- Sync `launchd` Mac ↔ repo `Rodato/notas-granola`
- Posible: mensajes libres a attendees vía Gmail API
