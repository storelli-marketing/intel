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

**PUBLIC MODE (primary owned path, no Meta).** Owned Storelli content is
discovered from PUBLIC Apify data via `src/owned_discovery.py`: a bounded scan of
the ONE trusted account (`config.STORELLI_INSTAGRAM_HANDLE`, default
`storellisoccer` — derived from the POC sheet's own links, 170/170 consistent)
using `directUrls` + `resultsType` + `onlyPostsNewerThan` ("since last successful
refresh + buffer"). Ownership is `creator_handle == trusted handle`, matched
EXACTLY and only AFTER acquisition — a lookalike handle or a brand mention in a
caption is EXTERNAL_INSPIRATION, never internal. The orchestrator's `owned_scan`
stage appends new owned reels, refreshes mutable PUBLIC metrics (views/likes/
comments/shares + FOLLOWERS_AT_MEASUREMENT), and passes Apify `videoUrl`s into
analysis. Acquisition hierarchy in `gemini_client.acquire()`: (1) Apify media URL
— no Instagram round-trip, no cookies; (2) public yt-dlp; (3) cookie-authenticated
yt-dlp. Cookies are a FALLBACK, not a dependency. Meta credentials are OPTIONAL
enrichment (`private_instagram_insights: NOT_CONFIGURED`) and never cause BLOCKED;
private-only metrics (saves/reach/impressions/demographics/follower-split) stay
absent and are NEVER inferred. Slack discusses refreshes THROUGH the stateful
strategist via `src/conversation_refresh.py` (topics: new findings / what matters
/ why / act-on-it / evidence / meta-gap), so "did we find anything new?" → "what
actually matters?" → "why?" → "what should we shoot?" all resolve in one thread
with varying answer shapes — not a templated status bot.

**Self-updating intelligence scheduler** (`src/intelligence_refresh.py`,
`src/query_economics.py`): orchestrates the EXISTING jobs (never duplicates them)
into two isolated bounded loops on a weekly cadence — INTERNAL (Storelli = proof:
IG metrics refresh → detect new → analyze → performance → correlate →
latest_learnings → winning profiles → Notion sync) and EXTERNAL (inspiration =
reference only: select queries → Apify discover → dedupe → analyze → match →
quality → semantic connections). `run_intelligence_refresh(mode, dry_run,
trigger)` runs every stage fail-soft (one bad video / missing Apify token /
Gemini quota stop / Notion outage never kills the rest), returns a structured
per-stage status, and writes an `INTELLIGENCE_REFRESH_RUNS` history row. A run
LOCK in that tab (with a stale timeout) stops a scheduled run and a dashboard run
from overlapping. Correlations/profiles/Notion rebuild ONLY when internal
evidence actually changed; ideas are NEVER auto-regenerated — the orchestrator
only computes SHOULD_REGENERATE_IDEAS. `query_economics` scores each discovery
query (40% quality yield + 30% new-row + 20% connection usage + 10% novelty),
keeps known-bad families (off-domain cross-sport, weak mindset, deprioritized
rings) paused, prefers goalkeeper/soccer/youth/coach queries, and recommends
RUN/PAUSE/REVIEW without ever auto-deleting. CLI `refresh-intelligence
[--internal-only|--external-only|--dry-run]`; dashboard buttons (Dry Run /
Internal / External / Full, all RUN_SECRET-gated); Slack answers "when did the
brain last update / what changed / should we regenerate ideas / new inspiration"
read the run history (external stays reference-only). Config:
INTELLIGENCE_REFRESH_ENABLED, INTELLIGENCE_REFRESH_CADENCE_DAYS (default 7,
clamp 5–14), INTELLIGENCE_REFRESH_STALE_LOCK_MIN. No scheduler is created in the
repo's deploy config, but recurrence IS now running: `src/scheduler.py` starts a
daemon thread from the FastAPI startup hook (never at import time, so importing
`web` spends no quota), wakes every INTELLIGENCE_SCHEDULER_CHECK_MINUTES, and
asks the INTELLIGENCE_REFRESH_RUNS log whether a successful/partial run finished
inside the cadence — so a restart neither loses nor double-fires the schedule and
two replicas cannot overlap (the orchestrator's lock row handles that; the loser
exits as locked_out). It runs mode=full with trigger=scheduler. Gated on
INTELLIGENCE_SCHEDULER_ENABLED + INTELLIGENCE_REFRESH_ENABLED + Sheets being
configured; it refuses to start with a stated reason rather than failing hourly,
and no failure inside it can take down the web process. GET /status exposes the
scheduler snapshot plus a `build` block (commit SHA from RAILWAY_GIT_COMMIT_SHA,
analysis_min_age_days, refresh_cadence_days) so "is the weekly job running?" and
"which build is live?" are answerable without host access. `python -m src.main
refresh-intelligence` still works for a manual run. NOTE: external discovery is a
guaranteed no-op with no ACTIVE row in APIFY_DISCOVERY_QUERIES, so that stage now
reports `skipped` with an actionable reason instead of "success, 0 discovered" —
ACTIVE is still never toggled automatically (that is the cost control).
NEW owned reels are auto-appended: the internal loop's `internal_append` stage
detects Storelli-owned IG media not in the POC and safely appends a row
(`SheetsClient.append_metadata_rows` + `social_metrics_ingest.append_owned_media_to_poc`)
— LINK + immutable metadata (POST_DATE, DURATION_SECONDS) + supported metrics
only, never Product/ICP/taxonomy/Status, dedup by shortcode/LINK, two-row header
preserved — so the row becomes analysis-eligible the same run. Lifecycle:
NEW_MEDIA (append→analyze) / KNOWN_UNANALYZED (analyze) / KNOWN_ANALYZED (skip) /
metrics-refresh for all known. `refresh-readiness` reports READY/BLOCKED per
capability (never prints secrets); `health_state()` returns HEALTHY / PARTIAL /
BLOCKED / STALE with specific reasons (incl. "Instagram video acquisition
credentials need refresh" when the yt-dlp cookie session is missing). Owned
TikTok: `STORELLI_TIKTOK_HANDLE` + `is_owned_tiktok()` — only the exact configured
handle can enter the internal pipeline (never inferred from content); owned-TikTok
metrics are limited (no official API here).

**Requested-count fidelity in idea retrieval** (`src/idea_retrieval.py`): "the top
10 ideas" returned 3 with no explanation and an identical scaffold per line. Three
causes: `parse_query` matched counts with `\b([1-9])\b` (single digit, so "10"
never parsed), `_cap` applied a ceiling of 5 even to an explicit ask, and a
shortfall was never explained. Now `_asked_count` reads a number in count position
only ("top 10", "give me 5", "5 BodyShield ideas" — never "critique idea 2" or a
`GK 3/4` size); an explicit count is honoured up to MAX_IDEAS=15 while the unasked
default stays 3; `_list_mode` renders long lists at MODE_DEEP so enforce_length
cannot trim them mid-list; and `supply()` + `shortfall_note()` explain any
shortfall with real numbers (all we have / N below the eligibility bar / N out of
scope / the readability cap plus the true pool size). The no-rated-ideas fallback
(`social_brain._mode_ideas` -> `interpretation.build_idea_candidates`) takes the
same count and states its own ceiling: the generator pairs the top 3 winning hooks
with the top 3 winning formats, so 9 is the signal set's true maximum (the old
internal clamp was 5). Presentation is deliberately less templated: the filler
risk "shootable, no big weakness" is omitted rather than repeated, priority shows
only when it differs from the top pick's, a duplicate hook falls back to the
concept instead of leaving a bare title, and per-line `proof [S#] / ref [E#]` was
removed in favour of the Sources block.

**Weekly digest** (`src/refresh_digest.py`): after each scheduled refresh, a short
readable summary of what changed is pushed to Slack via the existing
SLACK_WEBHOOK_URL and, when SMTP is configured, by email (stdlib smtplib, no new
dependency). `render_report()` is CLI-shaped (one line per stage, including
no-ops); the digest collapses no-ops, always names a FAILED stage, surfaces
"needs you" items (idea regeneration is never automatic; health warnings), and
says "nothing new this week" rather than padding. Every figure comes from the
run's own history row and external counts are never called proof. Delivery is
best-effort: an unconfigured destination is a silent no-op and a Slack/SMTP
failure never turns a successful refresh into a failed one. Config:
DIGEST_ENABLED (default true), DIGEST_EMAIL_TO + SMTP_HOST + SMTP_FROM (all
empty = no email is ever sent), SMTP_PORT/USERNAME/PASSWORD/USE_TLS/USE_SSL.
There is NO other email integration in this app — no inbox is read, and email is
outbound digest only.

**Analysis age gate** (`config.ANALYSIS_MIN_AGE_DAYS`, default = PERFORMANCE_MATURITY_DAYS
= one week): a reel is NOT analyzed until it is at least that old. Enforced at the
single choke point, `SheetsClient.should_process` / `should_tag` via
`performance.is_old_enough_to_analyze`, so `cmd_analyze`, `cmd_analyze_all` and the
refresh loop's `internal_analyze` all inherit it. Rationale: the older maturity
gate only withheld the PERFORMANCE label while still tagging the row and reading
its metrics, so a two-day-old reel entered the brain carrying numbers it had not
had time to earn (the live proof labelled a 2-day-old reel Underdog off 36k views).
Ingestion is unchanged — `owned_scan` still appends every new owned reel
immediately so its URL is tracked from day one; only analysis waits, and the row
becomes eligible automatically on the first run after it crosses the threshold.
`--reprocess` does NOT override the gate (idempotency override, not a licence to
read unearned metrics). Unknown age counts as old enough, so the undated
pre-existing library is never frozen. Held reels are reported, never silently
absent: `skipped_too_recent` in the run stats, `Held (younger than Nd)` in the CLI
summaries, `N held (younger than Nd)` on the internal_analyze stage reason, and
`performance.analysis_held_rows()` lists them. `performance.post_age_days` prefers
POST_TIMESTAMP and parses precise formats first — parsing the date-only column
first truncated a real timestamp to midnight and let a 6.9-day-old reel through a
7-day gate.

**Explicit-analytics precedence** (`src/analytics_query.py`, the "how many seconds"
fix): a clearly specified factual question about our own numbers answers the metric
it actually asks about, even inside a live decision frame. `parse()` returns a
structured contract (question_type / metric / dimensions / filters / cohort /
aggregation / scope_source / requires_private_data) so the model interprets results
rather than inventing the query; `social_brain.answer_conversation` calls it right
after the metric-registry limit check and before `conversation_agent`, and
`conversation_agent._maybe_frame` enforces the same rule locally. Routing
precedence: explicit analytics -> explicit scope change -> frame continuation ->
ambiguous follow-up -> generic strategy. The parser returns None (leaving existing
routes untouched) for prescriptive ("what should we shoot"), predictive ("most
likely to get comments"), ordinal ("the second one") and bare-transform ("shorter")
turns. `decision_frame._establishes_nothing` stops a missing-data answer from
anchoring a frame — that is what made an "I can't split trial vs standard" reply
become a live creative frame whose prior_recommendation was the words "Data check".
"Highest performing" always means reels currently classified Great via
`performance.buckets_for_rows`, stated in the answer, unless the user names a
yardstick ("highest views", "normalized performance"). The mirror image stays a
recommendation: `parse_recommendation` + `social_analytics.answer_duration_
recommendation` answer "how long should the concept we just discussed be?" as a
target range built on THAT concept's duration evidence (frame resolves the
referent), never a global median and never an idea list.

**Analytics computations** (`src/social_analytics.py`, contract-driven half):
`availability()` walks COLUMN_MISSING -> COLUMN_EXISTS -> DATA_EXISTS ->
ENOUGH_DATA (+ COMPARABLE_DATA per side) and nothing is computed until the ladder
allows it. `duration_profile` reports count/median/mean/range/buckets/coverage and
a comparison against non-Great reels, with a source hierarchy that is never
blended: exact DURATION_SECONDS -> Content-audit coarse bucket (stated as
approximate, NEVER as a median) -> absent (say so, name the backfill).
`temporal_fields` audits the temporal dimensions; `posting_time_profile` does
day-of-week/hour-of-day Great-rate and refuses to name a best window until at
least 3 performance-labelled posts sit in each window compared. Hours are UTC and
said to be UTC — `STORELLI_POSTING_TIMEZONE` is empty by default because the
intended local posting zone was never recorded. `social_metrics_ingest.
build_metric_values` now also writes POST_TIMESTAMP (full resolution) alongside
the date-only POST_DATE, which is the only thing that makes hour-of-day
answerable. REEL_TYPE (Trial/Standard) is still never inferred from public data.
`python src/main.py audit-analytics-coverage` prints the honest per-dimension
inventory (read-only).

**Source integrity** (`src/source_binding.py`): sources reach the reader only when
bound to a claim the answer actually makes. Roles: AGGREGATE_EVIDENCE,
EXAMPLE_CONTENT (illustrates, never proves an aggregate), SCHEMA_EVIDENCE,
REFRESH_HISTORY, EXTERNAL_REFERENCE, STRATEGIC_INFERENCE (needs no source).
Orphan sources (in the block, bound to nothing) are dropped; a missing-data answer
renders NO Sources block, because no reel supports an absence of data; so does a
pure clarification or inference. This replaced the `cited_norm or all_norm`
fallback in `social_strategist.compose_strategic_answer`, which attached the
strongest pack sources whenever the model cited none — that is how "we don't have
enough posting-time evidence" ended up citing three unrelated Great reels. Sources
are OPTIONAL: no block beats a misleading one. route_debug reports
explicit_analytics_query, analytics_metric/dimensions/filters/scope,
context_frame_ignored_reason, source_count_before/after_validation,
orphan_sources_removed.

**Stateful contextual follow-up layer** (the "why those?" fix): a follow-up to a
prior IDEA recommendation is answered in context instead of re-running a fresh
top-N list. `conversation_agent.answer()` is routed FIRST inside
`social_brain.answer_conversation` and ties together `conversation_state.py`
(compact per-conversation memory + 45-min TTL cache; reconstruction prioritizes
Slack thread history, then cache), `conversation_resolver.py` (dialogue-act
classifier + referent resolution — "these ideas"/"the first one"/"it" resolve
against the prior ASSISTANT output, walking back through recent turns via
`_recover_memory` so context survives an intervening explanation),
`conversation_evidence.py` (focused packs + natural renderers for explain-set /
explain-single / evidence / challenge / modify), and
`conversation_response_planner.py` (picks the answer SHAPE so structure varies by
intent). It ONLY owns genuine idea follow-ups (explain-a-set, "why the first one"
by ordinal, compare #1 vs #2, show-proof, challenge, reframe-for-parents,
shorter) and returns None for everything else — a fresh question, a reset (which
routes the new product fresh), a shoot-brief (strategy skills) or inspiration ask
(semantic layer), or an eval follow-up — so nothing else regresses. Evidence
retrieval stays deterministic; an optional validated LLM only rewords the
narrative (citations must be a subset of the pack, no external-as-proof, no
causal language) and falls back to the deterministic answer. External inspiration
stays reference-only throughout. route_debug reports dialogue_act /
contextual_followup / resolved_referents / response_shape / llm_used.

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
`STORELLI_IG_FOLLOWER_COUNT`, `DASHBOARD_URL`,
`STORELLI_POSTING_TIMEZONE` (empty = posting-time analytics stay UTC and say so),
`ANALYSIS_MIN_AGE_DAYS` (empty = follow PERFORMANCE_MATURITY_DAYS).

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
