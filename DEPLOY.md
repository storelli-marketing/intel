# Deploy — Storelli Marketing Brain (Railway)

The app is a single FastAPI service (`web:app`). No database. Deploys from
`main` via the included `Procfile`; Python pinned by `.python-version` (3.11).

## 1. Pre-deploy checklist

- [ ] On `main`, working tree clean, latest commit pushed.
- [ ] `Procfile` present: `web: PYTHONPATH=src uvicorn web:app --host 0.0.0.0 --port ${PORT:-8000}`
- [ ] `.python-version` = `3.11`
- [ ] `requirements.txt` pinned (fastapi, uvicorn, gspread, google-auth,
      google-genai, notion-client, python-dotenv, yt-dlp, httpx)
- [ ] `static/logo-accent.png` committed (served at `/static/logo-accent.png`)
- [ ] Google Sheet shared with the service-account `client_email` as **Editor**
- [ ] Notion `NOTION_PARENT_PAGE_ID` is a **normal page** (not a `?v=` database
      view) shared with the integration
- [ ] Slack incoming webhook created (if using Slack)

## 2. Railway environment variables

| Variable | Required | Notes |
|---|---|---|
| `GEMINI_API_KEY` | yes | Gemini API key |
| `GEMINI_MODEL` | no | default `gemini-2.5-flash` |
| `GOOGLE_SHEET_ID` | yes | the sheet id |
| `GOOGLE_WORKSHEET_NAME` | yes | `Marketing brain POC` |
| `GOOGLE_SERVICE_ACCOUNT_JSON_B64` | yes | `base64 -i service-account.json \| tr -d '\n'` (no file on Railway; decoded to `/tmp` at startup) |
| `STORELLI_IG_FOLLOWER_COUNT` | no | default `170000` |
| `RUN_SECRET` | yes | `openssl rand -hex 24`; `POST /run/*` return 503 until set |
| `QA_COMPILER_ENABLED` | no | `false` = 1 Gemini call/row (free-tier friendly) |
| `NOTION_API_KEY` | no | integration token (Notion Brain) |
| `NOTION_PARENT_PAGE_ID` | no | the **page** URL/id (auto-sanitized to a UUID) |
| `NOTION_DASHBOARD_URL` | no | page URL for the "Open Notion" button |
| `SLACK_WEBHOOK_URL` | no | incoming webhook (Slack report) |
| `DASHBOARD_URL` | no | the Railway URL (shown in the Slack footer) |

## 3. Post-deploy smoke test

Open the Railway URL, paste `RUN_SECRET`, then:

1. **Dashboard loads** — page renders, status shows `idle`.
2. **Generate Learnings** → status `completed`, Latest Learnings preview fills.
3. **Update Notion Brain** → `synced`; 5 databases updated under the parent page.
4. **Send Slack Report** → Slack receives the update.
5. **Upload Guidelines** — paste text, pick a type, Save → appears in the list.
6. **Run Social Media Learning, limit 5 only** → analyzes ≤5 rows (or stops
   cleanly on a Gemini 429). **Do not run 150 yet.**

Success = Generate Learnings → Update Notion Brain → Send Slack Report works
end-to-end from the deployed dashboard.

## Notes / known constraints

- **Gemini free tier ≈ 20 requests/day**, shared across local + Railway on the
  same key. `QA_COMPILER_ENABLED=false` = 1 call/row (~20 rows/day). A 429 stops
  the run cleanly (no false `failed`); idempotency resumes next run.
- **yt-dlp + Instagram from Railway**: datacenter IPs are often blocked/rate-
  limited, so *Run Social* may have a higher download-failure rate than local.
  Generate / Notion / Slack don't touch Instagram and are unaffected.
- **`data/` is ephemeral on Railway** (no volume): `latest_learnings.md` and
  `data/guidelines/*.md` live only within a deploy. Regenerate via the dashboard
  after a redeploy, or add a Railway volume / external store for persistence.
- Notion transient `5xx`/rate-limit errors are retried (1s/3s/6s); a persistent
  failure shows cleanly in the dashboard without crashing.
