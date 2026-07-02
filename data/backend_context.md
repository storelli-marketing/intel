# Storelli Marketing Brain — Backend Context

This file grounds "Dev Brain" (`src/dev_brain.py`) — the Slack-facing mode
that answers questions about this app's own architecture and drafts (never
applies) build requests. It's paired with the machine-generated
`data/backend_map.json` (files/routes/commands/env-var-names — regenerate via
`python scripts/build_backend_map.py` after structural changes). This file is
the curated, human-written layer: what things mean, not just what exists.

## What the app does

A Python CLI + FastAPI service that turns a Google Sheet of Storelli
Instagram reels into structured marketing intelligence: Gemini tags each
video against a fixed taxonomy, a correlation engine associates tags with
performance, a synthesizer turns that into learnings, and those learnings
sync into Notion (the durable "Marketing Brain") and are reachable
conversationally through a Slack bot. There is no database — Sheets is the
warehouse, Notion is the synced memory layer, and a small in-memory cache
holds Slack thread context (resets on restart).

## Major flows

**Slack request path**: Slack → `POST /slack/events` (`src/web.py`) →
signature verified (`slack_bot.verify_request`, HMAC against
`SLACK_SIGNING_SECRET`) → 200 ACK'd immediately → background worker
(`_converse` in `web.py`) → routed to either the marketing brain
(`social_brain.answer_conversation`) or Dev Brain
(`dev_brain.answer_backend_question` / `dev_brain.create_build_request`),
based on whether the message looks like a backend/build question → reply
posted via `slack_bot.post_message`. This path never writes to the Sheet,
never writes to Notion (except an explicitly-configured build-request
handoff — see below), and never triggers video analysis.

**Notion retrieval path**: `src/notion_retrieval.py` connects with
`NOTION_API_KEY` / `NOTION_PARENT_PAGE_ID`, finds the 6 Marketing Brain
databases by title under the parent page, queries rows, and normalizes them
into chunks. This is read-only (`databases.query` / `blocks.children.list`
only) and is tried first by the marketing brain before falling back to a
live Sheet + correlation computation.

**Strategist synthesis path**: `src/social_strategist.py` takes the
evidence a deterministic mode in `social_brain.py` already retrieved (never
queries anything itself), plus `data/storelli_context.md` brand grounding,
and asks Gemini to compose a strategist-voice judgment from it — validated
afterward (citations must exist in the evidence pack, no invented numbers,
no causal language, no leaked backend language, no markdown tables) and
discarded in favor of the deterministic answer on any failure.

**Analysis pipeline path** (CLI only, never Slack): `python src/main.py
analyze` / `analyze-all` → `src/sheets_client.py` reads eligible rows →
`src/analyzer.py` + `src/gemini_client.py` download the reel (yt-dlp),
upload to Gemini, tag it against `src/taxonomy.py`, optionally QA-review it
→ `sheets_client.write_row` writes 1/0 taxonomy columns back (empty cells
only, unless `--reprocess`). `src/synthesizer.py` turns tagged rows +
`src/correlations.py` output into `data/latest_learnings.md`.

**Deploy/runtime assumptions**: single Railway service (`Procfile` runs
`uvicorn web:app`), Python 3.11 pinned, no persistent volume — `data/*.md`
and the in-memory Slack thread cache are ephemeral and reset on redeploy.
Secrets live in Railway environment variables, never in the repo. Gemini's
free-tier quota (~20 requests/day) is shared across video tagging and any
Gemini-backed Slack synthesis (strategist mode, Dev Brain).

## Env vars (names only — Dev Brain must NEVER state or guess a value)

Gemini: `GEMINI_API_KEY`, `GEMINI_MODEL`. Sheets: `GOOGLE_SHEET_ID`,
`GOOGLE_SERVICE_ACCOUNT_JSON_PATH`, `GOOGLE_SERVICE_ACCOUNT_JSON_B64`,
`GOOGLE_WORKSHEET_NAME`. Notion: `NOTION_API_KEY`, `NOTION_PARENT_PAGE_ID`,
`NOTION_DASHBOARD_URL`. Slack: `SLACK_WEBHOOK_URL`, `SLACK_BOT_TOKEN`,
`SLACK_SIGNING_SECRET`. Run trigger: `RUN_SECRET`. Feature flags:
`QA_COMPILER_ENABLED`, `SLACK_LLM_POLISH_ENABLED`,
`SLACK_STRATEGIST_MODE_ENABLED`, `SLACK_DEV_MODE_ENABLED`. Dev Brain
build-request handoff: `SLACK_DEV_ALLOWED_USER_IDS`, `BUILD_REQUEST_TARGET`,
`GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_DISPATCH_EVENT`. Cookies (optional,
Instagram auth): `YTDLP_COOKIES_B64`, `YTDLP_COOKIES_PATH`. Misc:
`STORELLI_IG_FOLLOWER_COUNT`, `DASHBOARD_URL`.

## Current safety rules (Dev Brain must state these accurately, never soften them)

- The Slack path is read-only: no Sheet writes, no Notion writes, no video
  analysis — ever, regardless of what the user asks for in a message.
- `analyze` / `analyze-all` only run from the CLI or the dashboard's
  `RUN_SECRET`-protected `POST /run/*` endpoints — never from Slack, and Dev
  Brain must never offer to run them.
- Instagram cookie configuration (`YTDLP_COOKIES_B64`) is out of scope for
  any Slack-triggered action.
- A "push to code" build request is a DRAFT ONLY by default
  (`BUILD_REQUEST_TARGET=slack_only`) — it never edits this repo, never
  commits, never opens a PR. Only when explicitly configured does it file a
  GitHub issue (still no code change) or fire a `repository_dispatch` event
  (which only notifies an external workflow — that workflow, if one exists,
  must open a branch/PR requiring human review, never push to main).
- Build requests are gated to `SLACK_DEV_ALLOWED_USER_IDS` — empty by
  default, meaning no one is authorized until explicitly configured.
- Dev Brain never executes code, never reads live secret values, and only
  cites files that actually exist in `data/backend_map.json`.
