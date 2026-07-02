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
| `SLACK_WEBHOOK_URL` | no | incoming webhook (outbound Slack run report) |
| `SLACK_BOT_TOKEN` | no | Slack bot token (interactive brain — see §4) |
| `SLACK_SIGNING_SECRET` | no | Slack signing secret (interactive brain — see §4) |
| `DASHBOARD_URL` | no | the Railway URL (shown in the Slack footer) |

## 4. Slack chat app (interactive Marketing Brain)

The interactive brain (mention the bot in a channel → it replies in-thread with
ideas / feedback / learnings / next tests) is optional and completely separate
from `SLACK_WEBHOOK_URL` (which stays as the outbound run-report path).

1. https://api.slack.com/apps → **Create New App** → *From scratch*.
2. **OAuth & Permissions** → *Bot Token Scopes* → add `chat:write`. Install the
   app to the workspace and copy the **Bot User OAuth Token** (starts with `xoxb-`).
3. **Basic Information** → copy the **Signing Secret**.
4. **Event Subscriptions** → *Enable Events* → Request URL:
   `https://<railway-url>/slack/events` (must return the challenge on save).
   Under *Subscribe to bot events*, add `app_mention`. Save.
5. In Railway → Variables, set:
   - `SLACK_BOT_TOKEN` = the `xoxb-...` token
   - `SLACK_SIGNING_SECRET` = the signing secret
6. Invite the bot to a channel (`/invite @<bot>`) and mention it:
   `@storelli-brain ideas` / `feedback https://...` / `learnings` / `tests`.

The bot never writes to the Sheet or triggers video analysis — it reads the
existing Sheet + `latest_learnings.md` + `data/guidelines/` and replies in the
same thread. `/slack/events` returns **503** cleanly when the two Slack env
vars are missing; existing dashboard endpoints are unaffected.

## 5. Post-deploy smoke test

Open the Railway URL, paste `RUN_SECRET`, then:

1. **Dashboard loads** — page renders, status shows `idle`.
2. **Generate Learnings** → status `completed`, Latest Learnings preview fills.
3. **Update Notion Brain** → `synced`; 5 databases updated under the parent page.
4. **Send Slack Report** → Slack receives the update.
5. **Upload Guidelines** — paste text, pick a type, Save → appears in the list.
6. **Run Social Media Learning, limit 5 only** → analyzes ≤5 rows (or stops
   cleanly on a Gemini 429). **Do not run 150 yet.** *Performance-safe
   learning run — requires PERFORMANCE on each row.*
6b. **Analyze All Untagged Videos, limit 5** → tags ≤5 rows regardless of
   PERFORMANCE. Rows without valid Storelli performance still stay out of
   correlations. Use this to build up the taxonomy signal library over the
   full sheet, independently of performance labeling.
7. **Slack ideas mode** — in a channel where the bot is invited, mention it:
   `@storelli-brain ideas for parents on BodyShield`. It should reply
   in-thread with 3–5 ideas that carry a `Sources: [S1], [S2]` line each and
   an aggregate `Sources:` block at the bottom.
8. *(optional)* **`POST /run/generate-social-ideas`** — with `X-Run-Secret`,
   should return 202 queued; on completion the ideas are either upserted into
   the Notion **Generated Social Ideas** DB (if Notion is configured) or
   written to `data/generated_social_ideas.jsonl` (fallback). Existing Notion
   sync for the five original databases is untouched.

### Optional: `Source Type` column

The Sheet may include an optional `Source Type` column (aliases:
`source_type`, `Source`). Values *External / Inspiration / Reference /
Competitor / Creator* mark **inspiration-only** rows — they are excluded from
correlations so external content never contaminates Storelli learnings. Rows
marked *Internal / Storelli / Owned* and sheets that don't include the column
at all continue to work exactly as before.

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
