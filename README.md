# padel-routines

Repo stub para los **agentes Claude** (RemoteTrigger routines) que orquestan
operaciones de Padel Travellers. Los agentes corren en infraestructura cloud
de Anthropic y consumen una API HTTP autenticada expuesta por el Worker
`pt-whatsapp-bot` (Cloudflare).

**Los secrets viven en el Worker**, no aquí. Este repo solo contiene helpers
y la doc de cada routine.

## Variables de entorno (vienen en el prompt de la routine)

- `WORKER_BASE_URL` = `https://pt-whatsapp-bot.padeltravellers.workers.dev`
- `ROUTINES_API_TOKEN` = bearer compartido (rotable desde wrangler)

## Endpoints disponibles (`/api/*`)

Auth: `Authorization: Bearer $ROUTINES_API_TOKEN` en cada llamada.

### `GET /api/health`
Lista endpoints disponibles. Útil para smoke test.

### `GET /api/meta/insights?days=7&level=ad&include_paused=false`
Devuelve insights de Meta Ads Marketing API.
- `level`: `ad` | `adset` | `campaign`
- `days`: 1..365
- `include_paused`: incluye ads/adsets pausados

Respuesta:
```json
{
  "days": 7, "level": "ad", "since": "2026-04-27", "until": "2026-05-04",
  "count": 12,
  "items": [
    { "ad_id": "...", "ad_name": "...", "spend": 91.27, "impressions": 28917,
      "clicks": 432, "ctr": 1.5, "cpm": 3.79, "leads": 28, "cpl": 3.26 }
  ]
}
```

### `GET /api/conversations/recent?hours=24&limit=50&include_messages=true`
Conversations del bot WhatsApp activas en las últimas N horas.
- `include_messages`: si `true` incluye últimos 30 mensajes por conv

### `GET /api/leads/state`
Estado proactivo: leads sin contestar, llamadas próximas 24h, learned_rules pendientes.

```json
{
  "leads_awaiting_response": [...],
  "upcoming_calls_24h": [...],
  "rules_pending_review": [...],
  "counts": { "leads_awaiting": 1, "upcoming_calls": 0, "rules_pending": 0 }
}
```

### `GET /api/learned-rules?status=pending|active|all`
Lista reglas aprendidas (las que se inyectan en SYSTEM_PROMPT del bot).

### `POST /api/telegram/send`
Envía Telegram al chat por defecto (TELEGRAM_CHAT_ID en Worker) o a otro.
```json
{ "text": "...", "chat_id": "opcional", "topic_id": 123, "markdown": true }
```

## Routines configuradas

Ver `routines/` para los prompts/specs. Las routines se gestionan en
[claude.ai/code/routines](https://claude.ai/code/routines).

| Nombre | Schedule | Propósito |
|---|---|---|
| `pt-meta-ads-weekly` | Lun 06:00 UTC (08:00 ES) | Análisis semanal Meta Ads |
| `pt-bot-audit-daily` | 23:00 UTC (01:00 ES) | Auditoría diaria conversaciones bot |
| `pt-recordatorios-daily` | 07:00 UTC (09:00 ES) | Recordatorios condicionales |
