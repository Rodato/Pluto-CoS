# CLAUDE.md — calendar-planner

## Documentación (Obsidian)
Notas en: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Documentición codigo/Calendar Planner/`
Actualizar cuando cambien: esquema Supabase, flujo OAuth, comandos del bot, deployment Railway, stack, **scope vigente (v1 vs v2)**.
No actualizar por: bugfixes menores, ajustes de mensajes del bot, cambios de copy.

## Scope vigente: v1 (2026-05-11 →)

Bot Telegram **single-user** (Daniel, daniel@estudio-plural.co) con alcance **acotado al calendar**:
1. Lee Google Calendar (read).
2. Detecta invitaciones nuevas (`responseStatus=needsAction`) y las notifica por Telegram.
3. Permite responder RSVP (✅ aceptar / ❌ rechazar / ❓ tentativo) con botones inline.
4. Responde consultas on-demand sobre la agenda: `/hoy`, `/semana`, `/libre <fecha hora>`, + lenguaje natural resuelto por LLM.

**No incluido en v1** (queda para v2): pipeline del vault de Granola, extracción de tareas, propuestas de bloques, **creación / edición / borrado de eventos**, envío de mensajes libres a attendees (Gmail).

## Stack
- **FastAPI + Uvicorn** — servidor OAuth callback
- **APScheduler** — cron cada 15 min (chequeo de invitaciones nuevas)
- **python-telegram-bot 21.6** — bot
- **Google Calendar API v3** + OAuth2 **web flow** (scope `calendar.events` para soportar RSVP)
- **Supabase** (psycopg2) — `oauth_tokens`, `seen_invitations` (v1). `processed_notes`, `pending_proposals` reservadas para v2.
- **OpenRouter** vía SDK `openai` (base_url=`https://openrouter.ai/api/v1`)
- **Deploy:** Railway

## Correr en local

```bash
cd /Users/daniel/Desktop/Dev/calendar-planner

# Primera vez
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Bot completo (FastAPI + Telegram + scheduler en un solo proceso)
.venv/bin/python main.py
```

## Variables de entorno clave
- `DATABASE_URL` — Supabase connection string
- `TELEGRAM_TOKEN` — token del bot (BotFather)
- `TELEGRAM_CHAT_ID` — único chat autorizado (Daniel). Cualquier otro chat se ignora.
- `OPENROUTER_API_KEY` — API key OpenRouter
- `OPENROUTER_MODEL` — default `anthropic/claude-sonnet-4-6`
- `APP_BASE_URL` — URL pública Railway para callback OAuth
- `GOOGLE_WEB_CREDENTIAL_JSON` — contenido completo del `credentials.json` (web OAuth)
- `USER_ID` — identificador del usuario en `oauth_tokens` (default `"daniel"`)
- `USER_EMAIL` — email del usuario en Google Calendar (default `daniel@estudio-plural.co`). Se usa para identificar `attendees[me]`.
- `TZ_NAME` — timezone para slots/horario laboral (default `America/Bogota`)

Reservadas para v2 (no requeridas en v1): `NOTAS_REPO_URL`, `NOTAS_LOCAL_PATH`.

## Reglas duras (no romper)

### LLM
- **Solo OpenRouter.** Nunca SDK `anthropic` ni API directa de OpenAI.
- El cliente OpenRouter vive en `llm/planner.py` (helper `chat()`); importarlo desde ahí, no instanciar `OpenAI` en otros módulos.

### Calendar — v1
- El bot **solo LEE y responde RSVP**. **Nunca** crea, edita ni borra eventos.
- La única escritura permitida en v1 es `events.patch` modificando exclusivamente `attendees[me].responseStatus` (RSVP).
- Toda escritura requiere confirmación explícita por botón inline en Telegram.
- Cuando se entre a v2 se relajará esta regla para creación con `events.insert` (siempre desde una propuesta confirmada).

### DB
- **SQL siempre parametrizado con `%s`**, nunca f-strings con datos externos.
- `db/client.py` es el **único** punto de acceso a la DB.
- Usar `with get_cursor() as cur:` — el commit/rollback ya está manejado.

### Secretos
- No leer `.env`, `credentials.json`, `token.json`. Si hace falta inspeccionar, pedir al usuario que copie/pegue.
- No crear `.env.example` — documentar env vars en README.

### Naming
- El módulo de Calendar se llama `calendar_api/` (no `calendar/`) para evitar colisión con el stdlib `calendar`. **No renombrar a `calendar/`.**

### Single-user
- Cada handler de Telegram debe rechazar mensajes cuyo `update.effective_chat.id` no sea `TELEGRAM_CHAT_ID`.
- No diseñar para multi-tenant: el schema lo soporta (`user_id`) pero el bot vive con un solo usuario.

## Pipeline del chequeo (cron v1)
```
Cada 15 min
  ├─ calendar_api.client.list_pending_invitations(now)
  │    → eventos donde sos attendee con responseStatus=needsAction
  ├─ por cada invitación NO presente en seen_invitations:
  │    ├─ telegram_bot.bot.send_invitation() con botones ✅/❌/❓
  │    └─ DB: insert seen_invitations(event_id, notified_at)
  └─ (callback inline RSVP → events.patch + DB update rsvp_status)
```

## Comandos del bot (v1)
| Comando | Qué hace |
|---|---|
| `/hoy` | Eventos de hoy |
| `/semana` | Agenda de la semana |
| `/libre <fecha hora>` | "/libre martes 3pm" → ¿hay algo a esa hora? |
| `/revisar` | Fuerza chequeo manual de invitaciones (sin esperar al cron) |
| `/autorizar` | Arranca OAuth flow (devuelve link a `APP_BASE_URL/oauth/login`) |

Mensajes libres → `llm/query.py` resuelve consultas en lenguaje natural sobre la agenda (tool calling sobre wrappers de calendar).

## Repos GitHub
- `Rodato/calendar-planner` (este repo, código)
- `Rodato/notas-granola` (privado, notas — solo v2)

## Onboarding (primera vez)
1. `/autorizar` en Telegram → link al OAuth flow
2. Browser → consentimiento Google → callback a `APP_BASE_URL/oauth/callback`
3. Tokens guardados en `oauth_tokens` (refresh_token persistente)
4. A partir de ahí el bot puede leer Calendar y responder RSVP sin más intervención

## Roadmap a v2 (vault de Granola)
Cuando v1 esté estable, retomar:
- `obsidian/reader.py` — listar .md nuevos/modificados
- `obsidian/parser.py` — extraer tareas con LLM del cuerpo completo
- `llm/planner.propose_work_blocks` — mapear tareas → slots libres → propuestas
- `pending_proposals` + `processed_notes` (ya en schema, comentadas como v2)
- Relajar regla "solo lee + RSVP" para permitir `events.insert` tras confirmación
- Cron extra 8:00 / 12:00 L-V para el scan de notas
- Sync `launchd` en Mac → repo `Rodato/notas-granola`
