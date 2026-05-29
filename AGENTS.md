# Agentes LLM del sistema

Este documento describe los agentes LLM que filtran y extraen tareas de las distintas fuentes (Calendar, Granola, Gmail, Slack).

## Arquitectura general

```
Fuente → Heurística barata → Filtro LLM → Extractor LLM → Priorizador LLM → DB
```

1. **Heurística barata**: lectura rápida de API (últimos N días, sin responder, etc.) — sin LLM.
2. **Filtro LLM**: clasifica qué es accionable vs ruido (confirmaciones, "ok", FYI, etc.).
3. **Extractor LLM**: convierte lo accionable en tareas estructuradas (`ExtractedTask`).
4. **Priorizador LLM**: asigna P0–P3 según urgencia/impacto (`briefing/prioritizer.py`).
5. **Persistencia**: guarda en Neon (`tasks` table) con deduplicación por `source_ref`.

---

## 1. Slack — filtro de inbound (`llm/slack_filter.py`)

**Entrada**: `List[PendingSlackMessage]` (DMs + menciones de Slack).

**Salida**: `List[ActionableSlackMessage]` (solo lo que requiere respuesta de Daniel).

**Contexto que recibe**:
- `conversation_context`: últimos 5 mensajes del canal (agregado 2026-05-29 para detectar resoluciones).

**Reglas clave del prompt**:
1. **Análisis de dirección** — "necesito X":
   - "necesito dos minutos contigo" → remitente necesita tiempo DE Daniel → accionable.
   - "necesito terminar X antes de Y" → remitente habla de SU pendiente → NO accionable.
2. **Detección de resolución conversacional**:
   - Confirmaciones mutuas ("sisi", "dale", "A las 10 entonces") → cerrado, no accionable.
   - Último mensaje confirma horario/lugar → resuelto, no accionable.
3. **Awareness temporal**:
   - Si el mensaje menciona "a las 10" y ya son las 10:05 del mismo día → probablemente ya ocurrió.

**Por qué existe**:
Las heurísticas (último mensaje del DM no es de Daniel, o menciones sin respuesta) dejan pasar:
- Emojis, "ok", "gracias" → ruido.
- Conversaciones cerradas con confirmación implícita ("A las 10 entonces" + "sisi") → resuelto.
- Mensajes donde el remitente habla de SUS pendientes, no los de Daniel → no accionable.

---

## 2. Slack — filtro de outbound (`llm/outbound_filter.py`)

**Entrada**: `List[OutboundSlackMessage]` + `List[OutboundThread]` (Gmail).

**Salida**: `List[AwaitingItem]` (mensajes que Daniel envió y esperan respuesta).

**Contexto que recibe**:
- `sent_at` (ISO timestamp) para awareness temporal.
- `channel_type` (dm / group_dm / channel) para conservadurismo en canales.

**Reglas clave del prompt**:
1. **Contexto temporal**:
   - Si el mensaje menciona hora/fecha específica y ya pasó → probablemente resuelto offline.
   - Mensajes de más de 7 días probablemente perdieron vigencia (salvo follow-ups explícitos).
2. **Dirección — ¿Daniel está PIDIENDO o OFRECIENDO?**:
   - "necesito X de vos" / "confirmame" → Daniel espera respuesta.
   - "gracias", "perfecto", "dale" → Daniel está CERRANDO, no esperando.
   - "te paso X" / "acá está Y" → Daniel entregó, NO espera nada salvo que haya pregunta después.
3. **Cierres conversacionales**:
   - "A las 10 entonces", "Me citas por favor" → es un cierre, no espera respuesta adicional.
4. **Conservadurismo en canales**:
   - En `channel_type=channel`, la mayoría son anuncios/updates que NO esperan respuesta.
   - Solo extraer si es pregunta abierta o pedido explícito al canal.

**Por qué existe**:
La heurística "último mensaje del canal es de Daniel, sin réplicas" deja pasar:
- Confirmaciones finales ("ok", "perfecto", "gracias") que cierran conversaciones → no esperan respuesta.
- Anuncios/status updates en canales → no esperan respuesta de nadie.
- Coordinaciones de horario ya cerradas ("A las 10 entonces" sin "ok" del otro, pero ya se reunieron).

---

## 3. Gmail — filtro de inbound (`llm/email_filter.py`)

**Entrada**: `List[PendingThread]` (threads donde Daniel no es último remitente).

**Salida**: `List[ActionableEmail]` (correos que requieren respuesta).

**Reglas clave del prompt**:
- Descarta forwards automáticos, newsletters, notificaciones de bots.
- Descarta "gracias" finales sin pregunta nueva.
- Extrae solo si hay pregunta directa, pedido de decisión, o bloqueo del remitente.

---

## 4. Extractor de Granola (`obsidian/parser.py`)

**Entrada**: cuerpo de nota Granola (Markdown).

**Salida**: `List[ExtractedTask]` (tareas extraídas con contexto).

**Reglas clave del prompt**:
1. **Conservador**: bullets de reunión ≠ compromisos automáticos.
2. **Asignación explícita**: solo extraer si Daniel es el responsable explícito.
3. **No inferir**: si un bullet dice "Aly va a hacer X", NO es tarea de Daniel.

---

## 5. Priorizador (`briefing/prioritizer.py`)

**Entrada**: `List[ExtractedTask]` o `List[dict]` (open tasks de DB).

**Salida**: `List[PrioritizedTask]` con `priority` ∈ {P0, P1, P2, P3}.

**Lógica**:
- **P0**: urgente + importante (deadline hoy, bloqueo crítico).
- **P1**: importante pero no urgente (esta semana, impacto alto).
- **P2**: útil pero no crítico (puede esperar).
- **P3**: baja prioridad (nice-to-have, sin deadline).

**Repriorización diaria**: cada briefing matutino reprioriza TODAS las open tasks, no solo las nuevas.

---

## Detección de resolución automática

Desde 2026-05-26, el sistema **auto-cierra** tareas cuyo thread/mensaje original ya no aparece en la fuente como pendiente:

- **Gmail**: si el thread ya no figura en `list_pending_for_reply()` (Daniel respondió o aged out > 7 días) → marca la task como `done`.
- **Slack**: si el mensaje ya no figura en `list_all_pending()` (Daniel respondió o aged out > 7 días) → marca la task como `done`.

Función: `_close_resolved_tasks()` en `briefing/builder.py`.

**Por qué**: evita que el briefing acumule tareas obsoletas que Daniel ya resolvió pero no marcó como hechas manualmente.

---

## Mejora 2026-05-29: Contexto conversacional en Slack

**Problema detectado**: el sistema leía solo el último mensaje del canal, sin ver si hubo conversación previa que ya resolvió el tema.

**Caso real**:
```
[Alejandro] ohhh, o mejor por la mañana? te queda mejor?
[Alejandro] todavía no he salido, podría tipo 10, la logras?
[Daniel] A las 10 entonces. Me citas por favor!
[Alejandro] sisi
[Alejandro] necesito dos minutos!
```

Sistema marcaba como urgente: "te reclaman responder rápido" — **falso positivo**.

**Solución implementada**:

1. **Contexto conversacional** (`slack_api/client.py`):
   - `PendingSlackMessage` ahora incluye `conversation_context` (últimos 5 mensajes).
   - Nueva función `_get_conversation_context()` lee historial alrededor del mensaje target.

2. **Prompts reforzados** (`llm/slack_filter.py` + `llm/outbound_filter.py`):
   - **Análisis de dirección**: "Alejandro necesita 2 minutos" → él pide a Daniel (accionable) vs "Daniel necesita X" → Daniel pide a otro (no inbound, sino outbound).
   - **Detección de resolución**: "A las 10 entonces" + "sisi" → conversación cerrada.
   - **Awareness temporal**: si ya pasó la hora mencionada → probablemente resuelto offline.

**Resultado esperado**: reducción de falsos positivos donde el LLM marca como pendiente algo ya coordinado o resuelto.

---

## Telemetría y debugging

- `briefing/builder.py` loguea:
  - `notes_processed`, `notes_skipped_age`
  - `emails_pending`, `emails_actionable`
  - `slack_pending`, `slack_actionable`
  - Cuántas tasks se auto-cerraron (`_close_resolved_tasks`)

- Para ver qué está pasando el LLM, revisar:
  - `llm/slack_filter.py:84-94` — llamada a OpenRouter con prompt completo.
  - `briefing/builder.py:240-269` — Gmail extraction.
  - `briefing/builder.py:272-322` — Slack extraction.

---

## Roadmap de mejoras

1. **Historial de priorización**: guardar en DB cuándo cambió la priority de cada task (para métricas de precisión del priorizador).
2. **Feedback loop**: si Daniel marca una task como "no era mía" o "ya estaba hecha", realimentar al prompt del extractor.
3. **Multi-turn extraction**: para correos/mensajes complejos, permitir que el LLM pida contexto adicional (threads anteriores, attachments).
4. **Deduplicación inteligente**: detectar cuando Gmail + Slack + Granola hablan de la misma tarea (ej: "revisar PR #123").

---

## Referencias

- **Prompts**:
  - `llm/slack_filter.py` — inbound Slack
  - `llm/outbound_filter.py` — outbound Gmail + Slack
  - `llm/email_filter.py` — inbound Gmail
  - `obsidian/parser.py` — extractor de Granola
  - `briefing/prioritizer.py` — priorizador P0–P3

- **Código clave**:
  - `slack_api/client.py` — lectura de mensajes + contexto conversacional
  - `briefing/builder.py` — orquestación del briefing matutino
  - `db/client.py` — persistencia de tareas
