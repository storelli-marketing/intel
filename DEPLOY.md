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
| `YTDLP_COOKIES_B64` | no | base64 of an exported Instagram `cookies.txt` — set if anonymous downloads start failing with an "empty media response" error (see §5 notes) |
| `YTDLP_COOKIES_PATH` | no | path to a `cookies.txt` file directly; if `YTDLP_COOKIES_B64` is also set, it's decoded into this path (overwriting it) at startup |
| `SLACK_LLM_POLISH_ENABLED` | no | `true` = let Gemini reword Slack conversational replies (validated, discarded if it breaks grounding); default **off** to protect the shared Gemini quota |

## 4. Slack chat app (interactive Marketing Brain — threaded, conversational)

The interactive brain (mention the bot → it replies in-thread and understands
follow-ups like "expand #2" or "make it for parents") is optional and
completely separate from `SLACK_WEBHOOK_URL` (which stays as the outbound
run-report path).

1. https://api.slack.com/apps → **Create New App** → *From scratch*.
2. **OAuth & Permissions** → *Bot Token Scopes* → add `chat:write` and
   `app_mentions:read`. Install the app to the workspace and copy the **Bot
   User OAuth Token** (starts with `xoxb-`).
3. **Basic Information** → copy the **Signing Secret** (not the Verification
   Token — that's a legacy mechanism this app doesn't use).
4. **Event Subscriptions** → *Enable Events* → Request URL:
   `https://<railway-url>/slack/events` (must return the challenge on save —
   it only will once `SLACK_BOT_TOKEN` / `SLACK_SIGNING_SECRET` are already
   set in Railway, since the endpoint 503s without them).
   Under *Subscribe to bot events*, add `app_mention`. Save.
5. In Railway → Variables, set:
   - `SLACK_BOT_TOKEN` = the `xoxb-...` token
   - `SLACK_SIGNING_SECRET` = the signing secret
6. Invite the bot to a channel (`/invite @<bot>`) and mention it:
   `@storelli-brain ideas` / `feedback https://...` / `learnings` / `tests` /
   `summarize the brain`.
7. **Optional — DMs**: add the `message.im` bot event *and* the `im:history`
   scope together (Slack requires both to deliver the event at all), then
   **reinstall the app** (any scope change requires reinstalling). DMs are
   answered the same way as mentions.
8. **Optional — follow-ups without re-mentioning in channels**: add
   `message.channels` (+ `channels:history`), and `message.groups` (+
   `groups:history`) for private channels, then reinstall. The bot already
   only acts on messages inside a thread it has already replied in — it will
   not start responding to unrelated channel chatter even with this enabled.

The bot stays read-only throughout — never writes to the Sheet, never writes
to Notion, never triggers video analysis. It reads Notion Brain first, then
the Sheet + `latest_learnings.md` + `data/guidelines/`, and replies in the
same thread. Conversation context comes from live Slack thread history when
the optional history scopes above are granted, falling back to a small
in-memory-only cache (resets on restart, no database) otherwise — either way,
a missing scope degrades to single-turn answers rather than failing.
`/slack/events` returns **503** cleanly when `SLACK_BOT_TOKEN` /
`SLACK_SIGNING_SECRET` are missing; existing dashboard endpoints are unaffected.

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
7. **Slack conversational retrieval (Notion-first)** — in a channel where the
   bot is invited, mention it with a range of phrasings, not just exact
   keywords: `@storelli-brain what is working?`,
   `@storelli-brain ideas for parents on BodyShield`,
   `@storelli-brain what hooks work for parents?`,
   `@storelli-brain what should we avoid?`,
   `@storelli-brain summarize the brain`,
   `@storelli-brain what are the next creative tests?`,
   `@storelli-brain show me examples of Great videos`. When Notion is
   configured and synced, learnings/signal/test answers should read "(Notion
   Brain)" and cite Notion entries first; with Notion unset or a database
   empty, the same questions should still work via the Sheet/learnings.md
   fallback. Each reply should carry
   a `Sources:` line citing only what it actually retrieved (`[S1]` Sheet
   rows, `[S2]` learnings file, `[S3]` guidelines, `[S4]` Notion Brain when
   configured) — never an invented link, metric, or row. The bot stays
   read-only throughout: it never writes to the Sheet and never triggers
   video analysis, regardless of phrasing.
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
- **Instagram may require authenticated cookies**: Instagram has started
  serving an empty/auth-walled response to some anonymous downloads regardless
  of the specific video (yt-dlp error mentions "empty media response ... use
  --cookies"). If downloads fail this way, set `YTDLP_COOKIES_B64` (or
  `YTDLP_COOKIES_PATH` locally) to an exported `cookies.txt` from a **dedicated
  Storelli/service Instagram account** — not a personal one. Cookies expire and
  need periodic re-export. Never commit `cookies.txt` or its base64. After
  fixing cookies, run `reset-incomplete` to re-queue the rows that were marked
  `failed` by the download error (it only clears rows with no taxonomy tags
  written, so tagged rows are untouched) — then re-run `analyze-all`.
- **`data/` is ephemeral on Railway** (no volume): `latest_learnings.md` and
  `data/guidelines/*.md` live only within a deploy. Regenerate via the dashboard
  after a redeploy, or add a Railway volume / external store for persistence.
- Notion transient `5xx`/rate-limit errors are retried (1s/3s/6s); a persistent
  failure shows cleanly in the dashboard without crashing.
