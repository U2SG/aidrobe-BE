# Performance Baseline 2026-05

Source: `docs/backend-gaps-plan.md` section 4, Phase 0 performance/cost baseline.

| Dimension | Target | Tool | Measured (TBD) | Notes |
|---|---:|---|---|---|
| `GET /today` p95 (including dynamic score/context/obstacle) | < 200ms | wrk + APM | TBD — to fill after wrk run | End-to-end request latency |
| `GET /outfits/{id}` p95 | < 250ms | wrk + APM | TBD — to fill after wrk run | End-to-end request latency |
| `GET /wardrobe-items` p95 (200 items) | < 150ms | wrk + APM | TBD — to fill after wrk run | Use a user with about 200 wardrobe items |
| `GET /wardrobe-items/{id}/wear-history?days=60` p95 | < 100ms | wrk + APM | TBD — to fill after wrk run | 60-day aggregated history |
| Today payload (gzip) | < 30 KB | curl + gzip | TBD — to fill after wrk run | Measure compressed response body size |
| embedding worker backlog (queued > 5min count) | < 10 | `async_jobs` query | TBD — to fill after wrk run | Phase C async job table metric |
| outfit_board worker p95 e2e | < 60s | `async_jobs.duration` | TBD — to fill after wrk run | Phase C async job duration metric |
| LLM (Ask) p95 e2e | < 8s | `async_jobs.duration` | TBD — to fill after wrk run | Phase E/V4 Ask path |
| LLM single-call average `cost_cents` | < 2 | `async_jobs.duration` | TBD — to fill after wrk run | Average cost per LLM call |
| LLM failure rate (per day) | < 2% | `async_jobs.duration` | TBD — to fill after wrk run | Daily failed / total LLM calls |
| OCR (V4) p95 e2e | < 30s | `async_jobs.duration` | TBD — to fill after wrk run | Phase E/V4 OCR path |

## How to Measure

Placeholder commands for the first baseline pass:

```bash
wrk -t4 -c32 -d60s -H "X-User-Id: <user-id>" http://localhost:8000/v1/recommendations/today
wrk -t4 -c32 -d60s -H "X-User-Id: <user-id>" http://localhost:8000/v1/outfits/<outfit-id>
wrk -t4 -c32 -d60s -H "X-User-Id: <user-id>" http://localhost:8000/v1/wardrobe-items
wrk -t4 -c32 -d60s -H "X-User-Id: <user-id>" http://localhost:8000/v1/wardrobe-items/<item-id>/wear-history?days=60
curl -sS -H "X-User-Id: <user-id>" -H "Accept-Encoding: gzip" http://localhost:8000/v1/recommendations/today -o today.json.gz
```

Async metric placeholders:

```sql
-- embedding worker backlog queued for more than 5 minutes
SELECT count(*)
FROM async_jobs
WHERE job_type = 'embedding'
  AND status = 'queued'
  AND created_at < now() - interval '5 minutes';

-- worker p95 duration placeholders
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY duration)
FROM async_jobs
WHERE job_type = '<job-type>'
  AND status = 'succeeded';
```
