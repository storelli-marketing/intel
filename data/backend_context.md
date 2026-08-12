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

**Slack progress UI**: while an answer is being composed, `_converse`
(`src/web.py`) shows short PUBLIC status stages via
`slack_bot.ProgressReporter` — never private chain-of-thought, just what's
literally happening ("checking Notion Brain", "choosing strongest
evidence", "writing concise recommendation"). It prefers Slack's native
`assistant.threads.setStatus` (needs the `assistant:write` scope, which this
app's default scopes don't include) and falls back to posting one message
and editing it in place (`chat.postMessage` + `chat.update`) so no duplicate
"thinking" message is ever left behind — the same message becomes the final
answer.

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
discarded in favor of the deterministic answer on any failure. Citations are
resolved into normalized `Source` objects (`social_strategist.Source`) with a
priority order for the link shown — a direct video/post URL from Notion
properties or a Sheet-sourced IG link, else the Notion page URL, else title
only, never a fake link — and the final answer shows only the 1-3 strongest
proof links (5 if the user asks for more), each a real, clickable Slack
`<url|label>` link, never a raw source ID dump.

**Social analytics + creative test planning path** (`src/social_analytics.py`,
Slack read-only): routed by `social_brain.answer_conversation` BEFORE the
generic idea/strategy routes. Handles three question families — trial vs
standard reels / demographics, highest-performing reel duration, and "give me N
ideas to test" / "what should we test next". It reads the internal POC sheet
through the same read-only `SheetsClient` path the rest of the brain uses, and
builds test plans from the existing brain (winning profiles, semantic
connections, refined ideas via `InspirationSheets`). Core discipline: it never
invents a metric — if demographics, duration, or comment counts are absent it
says so plainly and names the exact field/backfill to add (e.g. store
`duration_seconds` from yt-dlp metadata, no re-analysis); performance uses a
metric hierarchy (engagement rate → saves/comments/shares → views/likes → the
manual Great/Good/Weak label); every test is internal-anchored with external
inspiration as execution reference only (never proof); and every KPI line is a
labelled proxy/inferred bet (KPI outcomes aren't tracked). Answers are built
with `decision_trace` (Data check → Cohort split → Metric used → Pattern found →
KPI caveat for analytics; Analysis anchor → Inspo cue → Creative bridge → KPI
bet for tests). Read-only: no Sheet writes, no Notion writes, no analysis.
Recognizes both short aliases (duration, followers, age…) and the recommended
production column names (DURATION_SECONDS, FOLLOWERS_AT_POST, AGE_SPLIT,
GENDER_SPLIT, LOCATION_SPLIT, FOLLOWER_NONFOLLOWER_SPLIT, plus optional
REACH/IMPRESSIONS/PROFILE_VISITS/WEBSITE_CLICKS/PRODUCT_CLICKS/TRIAL_CLICKS/
QUALIFIED_DMS). When exact DURATION_SECONDS is absent it falls back to the
`Content audit` tab's coarse `overall_videoLength_*` buckets (clearly labelled a
proxy, not seconds), else says duration is missing. Demographic split strings
("F 58% / M 42%") are parsed and compared trial-vs-standard when the columns
exist; otherwise it names the exact missing columns. Two read-only/dry-run CLIs:
`audit-social-metrics` (available/missing/coverage/classifiability/recommended
columns) and `backfill-duration-metadata --dry-run` (lists rows that could
receive DURATION_SECONDS; metadata-only; NO write mode exists yet). New metric
columns must be inserted between `Status` and the first taxonomy category `HOOK`
with row 1 blank, or the header forward-fill misreads them as taxonomy columns.

**Metrics import + schema setup** (`src/social_analytics.py`): `preflight-social-schema`
(read-only) prints the exact insertion plan (columns between Status and HOOK, row
1 blank); `setup-metrics-staging` is the ONE write command — it creates the
`SOCIAL_METRICS_IMPORT_STAGING` tab (header only; a new tab, never touches POC/
taxonomy) and is operator-invoked only; `import-social-metrics --dry-run` matches
staging rows to POC by LINK and reports matched/unmatched/would-fill/already-
populated/parse-errors, writing NOTHING (no non-dry-run mode); `audit-duration-buckets`
(read-only) reports Content audit bucket coverage + the best bucket by
PERFORMANCE Great-rate. Import never overwrites a populated POC cell and never
imports an unmatched link. Slack answers: "what metrics are missing?", "how do I
import IG metrics?", "can we use the Content audit duration buckets?", "what
duration bucket performs best?" (all read-only, decision-traced).

**Automatic Instagram metrics ingestion** (PRIMARY path; manual staging is a
fallback). `src/instagram_insights_client.py` calls the official Meta Graph API
for the Storelli-OWNED IG business account only — never scrapes, never uses
cookies, never pulls competitor/inspiration media. `src/social_metrics_ingest.py`
maps owned media to POC rows by canonical LINK / Instagram shortcode ONLY (never
row order, never fuzzy), then `pull-instagram-metrics --dry-run` reports
media/matched/would-fill/already-populated/unavailable + a SAFE/NOT SAFE verdict
and writes nothing; `--apply` is gated on that verdict and fills EMPTY metric
cells only (never overwrites; only the metric columns; never taxonomy/Product/
ICP/Status; snapshots + verifies). Unavailable API metrics are skipped, never
fatal; nothing is fabricated. Account-level demographics go to a separate
`INSTAGRAM_ACCOUNT_INSIGHTS` tab and are NEVER written as per-post demographics.
Config: INSTAGRAM_ACCESS_TOKEN (or META_ACCESS_TOKEN) + INSTAGRAM_BUSINESS_ACCOUNT_ID
(or IG_USER_ID), META_API_VERSION (default v21.0); missing config fails cleanly.
Dashboard: "Pull Instagram Metrics — Dry Run" / "— Apply (gated, RUN_SECRET)".
Slack answers config status only and NEVER applies a write.

**Operational IG refresh + mutable-metric policy.** `verify-instagram-connection`
is a safe preflight (resolves the Storelli account, checks media/insights access,
lists available/unavailable metrics + token health; NEVER prints the token).
`refresh-instagram-metrics` is the incremental operational path (dry-run by
default; `--apply` gated on a SAFE verdict): verify -> pull owned media +
insights -> map by LINK -> tiered fill -> write empty/updatable metric cells ->
update the `INSTAGRAM_SYNC_STATE` ledger. The mutable-metric policy: IMMUTABLE
metadata (POST_DATE, DURATION_SECONDS) is filled once and never changed;
CUMULATIVE metrics (views/likes/comments/saves/shares/reach/…) are updated to the
latest API value ONLY when the current cell equals the value we last synced (an
API-owned cell) — if it no longer matches, a human edited it and it is never
overwritten; HUMAN/manual fields (REEL_TYPE, taxonomy, Product/ICP/Status) are
never written by the API. Incremental: media already in the ledger with unchanged
metrics are skipped; new/changed media are written; the run reports what changed.
Analytics read the sheet live, so duration/comments/saves/trial answers update
immediately after a refresh — no separate build step. Slack status (read-only, no
secrets): "are IG metrics connected?", "when were metrics last refreshed?", "how
many reels have metrics?", "what metrics are we tracking?", "are any reels missing
metrics?". Recommended schedule (once connected): a daily Railway cron running
`refresh-instagram-metrics --apply` (metrics keep changing for ~a few weeks post-
publish; daily is ample, hourly is unnecessary). No scheduler is created here.

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
