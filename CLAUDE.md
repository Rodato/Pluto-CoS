# CLAUDE.md — calendar-planner

## Documentación (Obsidian)
Notas en: `~/Library/Mobile Documents/iCloud~md~obsidian/Documents/Documentición codigo/Calendar Planner/`
Actualizar cuando cambien: esquema Neon, flujo OAuth, comandos del bot, deployment Railway, stack, **scope/fase vigente (v1 / Fase 1 CoS / etc.)**.
No actualizar por: bugfixes menores, ajustes de mensajes del bot, cambios de copy.

## Scope vigente: Calendar + Gmail briefing (2026-06-11 →)

Bot Telegram **single-user** (Daniel, daniel@estudio-plural.co):

**Calendar (v1 — siempre activo)**
1. Lee Google Calendar (read).
2. Detecta invitaciones nuevas (`responseStatus=needsAction`) y las notifica por Telegram.
3. Permite responder RSVP (✅ aceptar / ❌ rechazar / ❓ tentativo) con botones inline.
4. Responde consultas on-demand: `/hoy`, `/semana`, `/libre <fecha hora>`, + lenguaje natural.

**Briefing CoS — 8 AM diario (activo)**
- Fuentes: Calendar (agenda del día) + Gmail (correos pendientes filtrados por LLM).
- Prioriza tareas P0–P3, persiste en Neon (`tasks`), entrega por Telegram.
- Granola/Obsidian: desactivado del bot — se trabaja directamente con Claude.
- Slack: desactivado por ahora.

**No incluido (queda para adelante):** creación/edición/borrado de eventos, Slack, sparring pages.

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

> **Ajuste (2026-06-11):** El briefing vive ahora con **Calendar + Gmail**. Granola/Obsidian
> se trabaja directamente con Claude (no desde el bot). Slack sigue desactivado.
> Código inactivo: `obsidian/` (ya no se llama en el pipeline), `slack_api/`,
> `llm/slack_filter.py`. Los call-sites están comentados con marcadores `DESACTIVADO` en
> `briefing/builder.py`, `main.py` y `telegram_bot/handlers.py`.
> Reactivar Gmail outbound = ya está activo. Reactivar Granola = descomentar bloque 1a en builder.py.

## Correr en local

```bash
cd /Users/daniel/Documents/Dev/Pluto-CoS

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

**Solo Railway:**
- `GOOGLE_CREDENTIALS_JSON` — base64 de `credentials.json` (shared con ai-mail-forwarder)
- `GOOGLE_TOKEN_JSON` — base64 de `token.json` (sale de correr `oauth_local.py` local)

**Inactivas (Obsidian desactivado del bot):**
- ~~`OBSIDIAN_VAULT_LOCAL_PATH`~~ — ya no se usa en el pipeline del bot
- ~~`RAILWAY_ENVIRONMENT`~~ — activaba `bootstrap_vault()`, que está comentado
- ~~`OBSIDIAN_VAULT_GIT_REPO`~~ — el vault ya no se clona en Railway al startup

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

### Telegram — límites duros que ya nos golpearon
- **4096 chars por mensaje.** El briefing trocea por proyecto vía `render_telegram` que devuelve `List[str]`. Cualquier render que pueda crecer (Gmail/Slack/pendientes a futuro) debe chequear largo.
- **64 bytes en `callback_data`.** Los `event_id` de invitaciones recurrentes de Google (`base_YYYYMMDDTHHMMSSZ`) los superan. Patrón actual: callback_data corto (`rsvp:accepted`) + resolver el contexto en DB usando `query.message.message_id` (ver `db.get_event_id_by_message_id`).

### Slack — contexto conversacional (2026-05-29)
- Cada `PendingSlackMessage` ahora incluye `conversation_context` (últimos 5 mensajes del canal) para detectar resoluciones conversacionales.
- Los prompts LLM (`llm/slack_filter.py` y `llm/outbound_filter.py`) fueron reforzados con:
  1. **Análisis de dirección**: distinguir "Alejandro necesita 2 minutos" (él pide a Daniel) vs "Daniel necesita X" (Daniel pide a otro).
  2. **Detección de resolución**: confirmaciones mutuas ("sisi", "A las 10 entonces") → conversación cerrada.
  3. **Awareness temporal**: si ya pasó el horario mencionado → probablemente resuelto offline.
- Objetivo: reducir falsos positivos donde el LLM marca como pendiente algo ya coordinado o resuelto.

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

## Comandos del bot
| Comando | Qué hace |
|---|---|
| `/hoy` | Eventos de hoy |
| `/semana` | Agenda de la semana |
| `/libre <fecha hora>` | "/libre martes 3pm" → ¿hay algo a esa hora? |
| `/revisar` | Fuerza chequeo manual de invitaciones (sin esperar al cron) |
| `/correos` | Correos que esperan respuesta (Gmail — filtro LLM) |
| `/briefing` | Genera el briefing matutino on-demand: Calendar + Gmail (sin esperar al cron de las 8 AM) |
| ~~`/slack`~~ | **DESACTIVADO (2026-06-02)** — Slack fuera de alcance por ahora |
| `/pendientes` | Tareas abiertas (`tasks` Neon) con botones ✅ para marcar hecho |
| `/autorizar` | Devuelve instrucciones para correr `oauth_local.py` (la auth es local, no se hace por Telegram) |

Mensajes libres → `llm/query.py` resuelve consultas en lenguaje natural sobre la agenda (tool calling sobre wrappers de calendar).

## Repos GitHub
- `Rodato/Pluto-CoS` (este repo, código — público). Local: `~/Documents/Dev/Pluto-CoS`. (Antes el dir local se llamaba `calendar-planner`; renombrado a `Pluto-CoS` para alinear con el repo.)
- `Rodato/obsidian-estudio-plural` (privado, vault de Obsidian — ya **no lo clona el bot**; se sincroniza por plugin Obsidian Git de forma independiente)

## Deploy
- **Railway** con `Dockerfile` (no nixpacks/railpack). Railway usa Railpack por default y un `nixpacks.toml` es silenciosamente ignorado. Si necesitás system deps (como `git` para clonar el vault), editá el `Dockerfile`.

## Onboarding (primera vez)
1. `cp ../ai-mail-forwarder/credentials.json .` (shared OAuth client)
2. `.venv/bin/python oauth_local.py` → browser → consentimiento Google → `token.json` queda guardado local + base64 mostrado
3. Para Railway: pegar el base64 como `GOOGLE_TOKEN_JSON` (y `GOOGLE_CREDENTIALS_JSON` también base64 con `base64 -i credentials.json | pbcopy`)
4. A partir de ahí el bot lee Calendar y responde RSVP sin más intervención. Refresh tokens no caducan (app "In Production" en Google Cloud).

## Historial de fases (pivot CoS 2026-05-19)

- ✅ **Fase 1** (2026-05-19): briefing 8 AM con Calendar + Granola/Obsidian.
- ✅ **Fase 2** (2026-06-11): Gmail activo; Granola/Obsidian removido del pipeline del bot (se trabaja con Claude directamente).
- ⏸ **Fase 3**: Slack (desactivado — no daba buena señal).
- ⏳ **Fase 4**: sparring pages en Obsidian + loop priorizado vs hecho.

Cuando se entre a creación de eventos, relajar la regla "solo lee + RSVP" para permitir `events.insert` tras confirmación. Descomentar `pending_proposals` en el schema.
