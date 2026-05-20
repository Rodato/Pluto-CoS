-- Schema (Neon) para calendar-planner
-- v1: seen_invitations
-- Fase 1 CoS agent: + tasks + processed_notes
-- v2 (futuro): pending_proposals (comentada al final)
--
-- OAuth: el token se guarda en token.json (local) o GOOGLE_TOKEN_JSON
-- (Railway env var). NO en DB.

-- ============================================================
-- v1 — Calendar read + RSVP + notificaciones de invitaciones
-- ============================================================

-- Invitaciones que el bot ya notificó por Telegram (anti-spam)
-- + estado del RSVP que hizo el usuario desde los botones inline.
CREATE TABLE IF NOT EXISTS seen_invitations (
    event_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    notified_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    telegram_message_id BIGINT,
    rsvp_status TEXT,  -- accepted | declined | tentative | NULL
    rsvp_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_seen_invitations_user ON seen_invitations(user_id);

-- ============================================================
-- Fase 1 CoS — Briefing matutino P0–P3
-- ============================================================

-- Tareas / compromisos detectados por el agente (fuente de verdad)
CREATE TABLE IF NOT EXISTS tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    priority TEXT NOT NULL CHECK (priority IN ('P0','P1','P2','P3')),
    project TEXT,                     -- carpeta dentro de Granola/ (AMA, Puddle, etc.) o "Varios"
    source TEXT NOT NULL CHECK (source IN ('calendar','granola','sparring','gmail','slack','manual')),
    source_ref TEXT,                  -- event_id | note_path | email_id | slack_ts
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','snoozed','dropped')),
    due_date DATE,
    snoozed_until TIMESTAMPTZ,
    briefing_date DATE,               -- fecha del briefing donde apareció primero
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_briefing_date ON tasks(briefing_date);
CREATE INDEX IF NOT EXISTS idx_tasks_source_lookup ON tasks(source, source_ref);

-- Notas del vault ya procesadas (para no re-extraer en cada corrida)
CREATE TABLE IF NOT EXISTS processed_notes (
    note_path TEXT PRIMARY KEY,
    last_mtime TIMESTAMPTZ NOT NULL,
    last_processed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tasks_extracted INT NOT NULL DEFAULT 0
);

-- ============================================================
-- v2 (reservadas) — propuestas de bloques de tiempo en Calendar
-- ============================================================
-- Descomentar cuando se implemente events.insert (relaja la regla
-- "solo lee + RSVP" del v1).

-- CREATE TABLE IF NOT EXISTS pending_proposals (
--     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
--     user_id TEXT NOT NULL,
--     title TEXT NOT NULL,
--     description TEXT,
--     proposed_start TIMESTAMPTZ NOT NULL,
--     proposed_end TIMESTAMPTZ NOT NULL,
--     source_note TEXT,
--     rationale TEXT,
--     status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | edited
--     telegram_message_id BIGINT,
--     created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
--     resolved_at TIMESTAMPTZ,
--     calendar_event_id TEXT
-- );
-- CREATE INDEX IF NOT EXISTS idx_proposals_status ON pending_proposals(status);
-- CREATE INDEX IF NOT EXISTS idx_proposals_user ON pending_proposals(user_id);
