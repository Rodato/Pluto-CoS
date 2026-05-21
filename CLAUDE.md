# CLAUDE.md — calendar-planner

## Documentación (Obsidian)
Notas en: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Documentición codigo/Calendar Planner/`
Actualizar cuando cambien: esquema Neon, flujo OAuth, comandos del bot, deployment Railway, stack, **scope/fase vigente (v1 / Fase 1 CoS / etc.)**.
No actualizar por: bugfixes menores, ajustes de mensajes del bot, cambios de copy.

## Scope vigente: v1 (2026-05-11 →)

Bot Telegram **single-user** (Daniel, daniel@estudio-plural.co) con alcance **acotado al calendar**:
1. Lee Google Calendar (read).
2. Detecta invitaciones nuevas (`responseStatus=needsAction`) y las notifica por Telegram.
3. Permite responder RSVP (✅ aceptar / ❌ rechazar / ❓ tentativo) con botones inline.
4. Responde consultas on-demand sobre la agenda: `/hoy`, `/semana`, `/libre <fecha hora>`, + lenguaje natural resuelto por LLM.

**No incluido en v1** (queda para v2): pipeline del vault de Granola, extracción de tareas, propuestas de bloques, **creación / edición / borrado de eventos**, envío de mensajes libres a attendees (Gmail).

## Stack
- **FastAPI + Uvicorn** — healthcheck (`/`) para Railway. **Ya no sirve OAuth**.
- **APScheduler** — cron cada 15 min (chequeo de invitaciones nuevas) + cron diario 8 AM (briefing CoS, Fase 1)
- **python-telegram-bot 21.6** — bot
- **Google Calendar API v3** + OAuth2 **Installed flow** (scope `calendar.events`). Token se genera local con `oauth_local.py` y se pega en Railway como env var.
- **Neon** (psycopg2) — `seen_invitations` (v1) + `tasks`, `processed_notes` (Fase 1 CoS). `pending_proposals` reservada para v2.
- **OpenRouter** vía SDK `openai` (base_url=`https://openrouter.ai/api/v1`)
- **Deploy:** Railway

## Pivot a agente CoS (2026-05-19)
El norte cambió de "v2 = pipeline Granola" a **briefing matutino tipo Chief-of-Staff**: cada mañana 8 AM, leer 5 fuentes (Calendar, Granola/Obsidian, sparring pages, Gmail, Slack), extraer tareas/compromisos con LLM, priorizar P0–P3, persistir en Neon (`tasks`), entregar por Telegram. Las fuentes se suman por fases:

- **Fase 1**: Calendar + Granola → Telegram briefing P0–P3
- **Fase 2**: + Gmail
- **Fase 3**: + Slack
- **Fase 4**: + sparring pages + loop de aprendizaje

El v1 (calendar read + RSVP cada 15 min) sigue corriendo en paralelo.

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
- `DATABASE_URL` — Neon connection string (con `?sslmode=require`)
- `TELEGRAM_TOKEN` — token del bot (BotFather)
- `TELEGRAM_CHAT_ID` — único chat autorizado (Daniel). Cualquier otro chat se ignora.
- `OPENROUTER_API_KEY` — API key OpenRouter
- `OPENROUTER_MODEL` — default `anthropic/claude-sonnet-4-6`
- `USER_ID` — identificador interno del usuario (default `"daniel"`)
- `USER_EMAIL` — email del usuario en Google Calendar (default `daniel@estudio-plural.co`). Se usa para identificar `attendees[me]`.
- `TZ_NAME` — timezone para slots/horario laboral (default `America/Bogota`)
- `BRIEFING_HOUR` — hora del briefing matutino CoS (default `8`)
- `OBSIDIAN_VAULT_LOCAL_PATH` — solo local, path al vault iCloud: `/Users/daniel/Library/Mobile Documents/iCloud~md~obsidian/Documents/Estudio Plural`

**Solo Railway** (en local se leen archivos del disco):
- `GOOGLE_CREDENTIALS_JSON` — base64 de `credentials.json` (shared con ai-mail-forwarder)
- `GOOGLE_TOKEN_JSON` — base64 de `token.json` (sale de correr `oauth_local.py` local)
- `RAILWAY_ENVIRONMENT` — lo setea Railway automáticamente; activa `bootstrap_vault()` + `pull_vault()` en `obsidian/git_sync.py`
- `OBSIDIAN_VAULT_GIT_REPO` — URL HTTPS+token del repo `Rodato/obsidian-estudio-plural` (formato `https://<user>:<token>@github.com/Rodato/obsidian-estudio-plural.git`). En local el vault vive en iCloud; en Railway se clona desde GitHub al startup.

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
| `/autorizar` | Devuelve instrucciones para correr `oauth_local.py` (la auth es local, no se hace por Telegram) |

Mensajes libres → `llm/query.py` resuelve consultas en lenguaje natural sobre la agenda (tool calling sobre wrappers de calendar).

## Repos GitHub
- `Rodato/Pluto-CoS` (este repo, código — público). Local: `~/Desktop/Dev/calendar-planner`. El nombre del directorio local se mantiene `calendar-planner` por historia.
- `Rodato/obsidian-estudio-plural` (privado, vault de Obsidian sincronizado por plugin Obsidian Git — Railway lo clona al startup via `OBSIDIAN_VAULT_GIT_REPO`)

## Deploy
- **Railway** con `Dockerfile` (no nixpacks/railpack). Railway usa Railpack por default y un `nixpacks.toml` es silenciosamente ignorado. Si necesitás system deps (como `git` para clonar el vault), editá el `Dockerfile`.

## Onboarding (primera vez)
1. `cp ../ai-mail-forwarder/credentials.json .` (shared OAuth client)
2. `.venv/bin/python oauth_local.py` → browser → consentimiento Google → `token.json` queda guardado local + base64 mostrado
3. Para Railway: pegar el base64 como `GOOGLE_TOKEN_JSON` (y `GOOGLE_CREDENTIALS_JSON` también base64 con `base64 -i credentials.json | pbcopy`)
4. A partir de ahí el bot lee Calendar y responde RSVP sin más intervención. Refresh tokens no caducan (app "In Production" en Google Cloud).

## Roadmap (pivot CoS 2026-05-19)

**Fase 1 — siguiente**: briefing matutino 8 AM Bogotá
- Cron diario en `scheduler.py` (existe el cron de 15 min de invitaciones; sumar uno diario)
- Pipeline: `calendar_api` + `obsidian/reader.py` + `obsidian/parser.py` (ya hay esqueleto) → LLM extrae tareas → priorizar P0–P3 → insert en `tasks` (Neon) → mensaje a Telegram
- Generar `Briefings/YYYY-MM-DD.md` en el vault (read-only desde el bot)

**Fase 2**: Gmail (compromisos en enviados + sin responder).
**Fase 3**: Slack (bloqueos del equipo).
**Fase 4**: sparring pages en Obsidian + loop priorizado vs hecho.

Cuando se entre a la fase de creación de eventos (post-Fase 4), relajar la regla "solo lee + RSVP" para permitir `events.insert` tras confirmación. Descomentar `pending_proposals` en el schema.
