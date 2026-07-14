# Storelli Intelligence MVP

A lightweight, **agent-run** marketing-intelligence workflow for Storelli
(goalkeeper protective gear). Not a SaaS app, dashboard, or custom panel — just
a small Python CLI that reads the POC Google Sheet, analyzes each Instagram reel
with Gemini + a QA compiler pass, writes structured 1/0 taxonomy tags back, and
correlates those tags against the manual `PERFORMANCE` column.

```
Google Sheet (LINK + manual PERFORMANCE)
  -> Agent runner reads eligible rows
  -> Gemini analyzes each video (hook / format / visual style / problem /
     solution / conversion / offer / product presence / funnel stage)
  -> QA compiler pass reviews + corrects the tags
  -> Sheet updated with 1/0 taxonomy columns (empty cells only) + Status
  -> Correlation engine (signal vs PERFORMANCE)
  -> Notion findings dashboard (later)
```

## How Gemini "watches" an Instagram link

Gemini can't fetch an Instagram URL directly. For each row the runner:

1. downloads the reel to a temp file with `yt-dlp`,
2. uploads it via the Gemini **Files API**,
3. asks the model to tag it against the taxonomy and return JSON (pass 1),
4. runs a **QA compiler** pass (text-only) that reviews pass-1 tags for internal
   consistency, Storelli product grounding, and hook/format/problem/solution
   fit, returning corrected tags before anything is written.

If a reel can't be downloaded (private/removed/rate-limited), the row's `Status`
is set to `failed` and the run continues.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
```

`.env` values:

| var | meaning |
|-----|---------|
| `GEMINI_API_KEY` | Gemini API key |
| `GEMINI_MODEL` | default `gemini-2.5-flash` |
| `GOOGLE_SHEET_ID` | the sheet's ID (from its URL) |
| `GOOGLE_SERVICE_ACCOUNT_JSON_PATH` | path to a service-account JSON; **share the sheet with that service account email (Editor)** |
| `GOOGLE_WORKSHEET_NAME` | tab name, default `Sheet1` |
| `NOTION_API_KEY` | Notion internal integration token |
| `NOTION_PARENT_PAGE_ID` | page the integration can write to (share the page with the integration) |
| `YTDLP_COOKIES_B64` | optional; base64 of an exported `cookies.txt` (see below) |
| `YTDLP_COOKIES_PATH` | optional; path to a `cookies.txt` file directly, instead of the base64 form |

### Instagram cookies (if anonymous downloads start failing)

Instagram has started serving an empty/auth-walled response to some anonymous,
scraper-style requests instead of the reel media, which makes `yt-dlp` fail
with an "empty media response ... use --cookies" error regardless of the video.
When that happens, `yt-dlp` needs an authenticated session's cookies:

1. Log into Instagram in a browser with a **dedicated Storelli/service
   account** — not a personal account.
2. Export cookies in Netscape format (e.g. a "cookies.txt" browser extension)
   to a `cookies.txt` file.
3. Either set `YTDLP_COOKIES_PATH` to that file's path, or base64-encode it
   and set `YTDLP_COOKIES_B64` (same pattern as
   `GOOGLE_SERVICE_ACCOUNT_JSON_B64` — required for Railway, which can't read
   local files). `config.py` decodes it fail-soft to a temp file at startup; a
   malformed value logs a warning instead of crashing, and only the download
   step fails cleanly until it's fixed.
4. Neither var set = current anonymous-download behavior, unchanged.

Cookies **expire** (the session gets logged out or Instagram invalidates it)
and will need periodic re-export. **Never commit `cookies.txt` or its base64**
— treat it exactly like the service-account JSON: local `.env` /
Railway variable only.

## POC sheet structure

The sheet uses a **two-row header** (data starts on row 3):

- **Row 1** = category groups (HOOK, FORMAT, …) spanning their columns.
- **Row 2** = column names: 7 metadata columns then 48 bare taxonomy option
  labels.

Metadata columns: `ID, LINK, PERFORMANCE, Storytelling structure, ICP, Product,
Status`. A taxonomy column is identified by its **(category, option) pair**,
because bare labels collide (e.g. "None" under both CONVERSION and PRODUCT
PRESENCE). Required columns: `LINK`, `PERFORMANCE`, `Status` — missing ones
raise a clear error.

### Which rows are processed (idempotency)

A row is **eligible** only when `LINK` is set, `PERFORMANCE` is not
`Non classified`, and the row is **not already analyzed**. A row counts as
already analyzed when **any** of these hold:

- `Status` = `completed`, `needs_review`, or `failed`, or
- any taxonomy 1/0 cell already carries a value (0 or 1).

Default behavior is therefore safe to re-run:
- already-analyzed rows are skipped,
- existing taxonomy values are never overwritten (empty cells only),
- completed rows are never reprocessed.

The **only** exception is `--reprocess`, which re-runs eligible rows and
overwrites taxonomy + PERFORMANCE. Rows with no determinable performance are
skipped without writing anything, so they become eligible automatically once
views/PERFORMANCE are added.

Each run prints a summary: eligible found · skipped (already analyzed) ·
skipped (no performance) · analyzed · needs_review · failed.

## Commands

```bash
python src/main.py analyze         # performance-safe learning run (requires PERFORMANCE)
python src/main.py analyze-all     # taxonomy-tag every LINK, PERFORMANCE not required
python src/main.py correlations    # print signal/performance findings
python src/main.py synthesize      # write data/latest_learnings.md (no API calls)
python src/main.py notion-sync     # upsert synthesized learnings into Notion Brain
python src/main.py slack-report    # post a run summary to Slack
python src/main.py run-all         # analyze -> correlations -> synthesize -> notion -> slack
python src/main.py run-all --reprocess   # re-tag rows (overwrite existing)
python src/main.py analyze --limit 5     # test mode: at most 5 rows
python src/main.py analyze-all --limit 18 --no-qa   # tag 18 fresh rows, 1 Gemini call/row
python src/main.py analyze-all --limit 150 --no-qa  # tag up to 150 fresh rows
python src/main.py reset-incomplete      # re-queue processed-but-untagged rows
python src/main.py scan-inspiration              # scan ACTIVE MONITORED CHANNELS (metadata)
python src/main.py process-inspiration-queue     # ingest pasted URLs from INSPIRATION_URL_QUEUE
python src/main.py analyze-inspiration            # tag EXTERNAL_INSPIRATION rows with the taxonomy
python src/main.py discover-inspiration           # Apify research discovery -> INSPIRATION_CONTENT
python src/main.py build-winning-profiles         # Storelli winning format profiles (internal only)
python src/main.py match-inspiration              # match safe external rows to active profiles
python src/main.py quality-review-inspiration     # QC external candidates for idea-gen readiness
python src/main.py generate-ideas                 # rated Storelli creative ideas (internal-anchored)
```

(`python -m src.main <command>` works too.)

### `analyze` vs. `analyze-all`

Two tagging modes share the same Gemini video pipeline and the same write
guardrails (no human-column overwrites, no filled-cell overwrites unless
`--reprocess`, 429 stops cleanly, failed downloads mark only the row).

- **`analyze`** — the performance-safe learning run. A row is eligible only
  when LINK is set, PERFORMANCE is set (or auto-computable), and the row
  isn't already analyzed. Rows with `Non classified` are hard-skipped. Every
  row it tags is a candidate for correlations. **This mode's behavior is
  unchanged.**
- **`analyze-all`** — the full-sheet tagging mode. Every row with a LINK is
  eligible, regardless of PERFORMANCE. Blank / Non classified / Reference /
  External / Inspiration rows all get the 9-layer taxonomy. These rows still
  **do not** enter correlations — the correlation engine filters by valid
  Storelli performance and by `Source Type` (see below) at the bucket layer,
  not at the tagging layer. Use it to build up a rich signal library across
  the whole sheet before / independently of performance labeling.

`--limit N`, `--reprocess`, and `--no-qa` behave the same across both.

### Learning Synthesizer (`synthesize`)

Turns the correlation results + tagged rows + performance buckets into a
structured markdown brief at **`data/latest_learnings.md`** with seven sections:
Winning Patterns, Weak Patterns, Product Learnings, ICP Learnings, Next Tests,
Formats To Scale, Formats To Kill. It's pure computation (no Gemini calls), so
it's free and repeatable, and it prints a thin-data warning when there are too
few `Great` videos for the lifts to be reliable. The file is a generated
artifact (gitignored).

`--limit N` caps how many eligible rows are analyzed in a run — use it for cheap
test runs without calling Gemini on the whole sheet.

`--no-qa` skips the QA compiler pass (1 Gemini call/row instead of 2). Same as
setting `QA_COMPILER_ENABLED=false`. Pass-1 still emits confidence, so the
`needs_review` guardrail keeps working. Use it to stretch a limited free-tier
quota (~20 calls/day ≈ 20 rows/day with QA off vs ~10 with it on).

### Free-tier daily batching

With no paid Gemini tier, process the sheet across several days. Idempotency
means each run resumes where the last stopped, and a 429 stops the run cleanly:

```bash
# once per day, with QA off to maximize rows/day:
python src/main.py analyze --limit 18 --no-qa
```

Already-analyzed rows are skipped, so you don't redo work. ~18 rows/day covers
~150 rows in ~8 days.

## What gets written back

Per analyzed row (human columns `ID`/`LINK`/`PERFORMANCE`/`Storytelling
structure` are **never** overwritten):

- the 9-layer taxonomy columns as `1`/`0` (empty cells only, unless
  `--reprocess`)
- `Status` = `completed` (or `failed` if the reel can't be analyzed)
- `ICP` and `Product` — **only if blank** and the QA pass is confident; they are
  grouping fields, not AI signals, so the sheet's ICP/PRODUCT one-hot column
  groups are intentionally left untouched

There are no `ai_summary` / `primary_*` / `performance_bucket` columns in the
POC sheet, so those stay internal (used by the QA pass and logging).

## Taxonomy (9 AI-tagged layers)

Multi-label (tag all that apply): **Hook**, **Format**, **Visual Style**.
Single-label (exactly one): **Problem Type**, **Solution Type**, **Conversion**,
**Offer**, **Product Presence**, **Funnel Stage**.

**ICP** and **Product** are grouping dimensions (not AI signal layers). Canonical
vocabularies for every layer, plus the Storelli product context used to ground
Product reasoning, live in [src/taxonomy.py](src/taxonomy.py).

## Performance (manual or auto-computed)

The runner picks the row's `PERFORMANCE` in this order:

1. **Existing manual value** (`Great` / `Good` / `Ok` / `Underdog`) — kept as-is
   unless `--reprocess` is passed.
2. **Auto-computed from views/followers ratio** when the sheet has a `Views`
   column (and optionally a `Followers` column; otherwise the env var
   `STORELLI_IG_FOLLOWER_COUNT`, default `170000`, is used):
   - `r > 1.0` → `Great`
   - `0.5 ≤ r ≤ 1.0` → `Good`
   - `r < 0.5` → `Underdog`

If neither is available, the row is set to `Status = needs_review` and skipped
(no Gemini call). `Non classified` is always treated as a hard skip.

Read-side mapping for correlations (positive class = `Great`):

```
Great                -> Great  (positive)
Good / Ok            -> OK     (average)
Underdog             -> Bad    (low)
Non classified / blank -> skipped
```

## Correlation engine

Per signal: count of videos with it, **`Great` rate** with vs without the
signal, and **lift** (the difference). Confidence by sample size: High ≥ 20,
Medium 8–19, Low < 8. These are **correlations / associations, never
causation.**

## Guardrails

- **Confidence gate (quality over automation):** the QA pass returns confidence
  for **Hook**, **Format**, and **Product**. If any is `low`, the uncertain
  fields are **not** auto-written (Hook/Format signal columns are skipped;
  `Product` is not filled) and the row's `Status` is set to **`needs_review`**
  for a human, instead of `completed`.
- Only ever writes taxonomy cells, `Status`, and blank `ICP`/`Product` — never
  other human columns.
- Two Gemini passes (analysis + QA compiler); each retries invalid JSON once,
  then the row is marked `failed`.
- Transient Gemini **503 UNAVAILABLE** errors are retried up to 3 times with
  10s / 30s / 60s backoff; if still failing, the row is marked `failed` and the
  run continues. Non-503 errors are not retried.
- Gemini **429 RESOURCE_EXHAUSTED** (quota/rate limit) **stops the run** instead
  of marking rows failed — the current row is left unprocessed (no Status) so it
  stays eligible for the next run. Note: each row uses **2 Gemini calls**
  (analysis + QA), so the free tier (~20 requests/day) only covers ~10 rows/day;
  a paid tier is required for larger batches.
- Idempotent: filled taxonomy cells are skipped unless `--reprocess`.
- No frontend, no database, no Zapier/Make/n8n.

## Sample data

`data/sample_input.csv` / `data/sample_output.csv` show the shape of the sheet
before and after a run.

## Notion Brain + Slack

**Notion is the Marketing Brain** (structured synthesized intelligence only —
**never raw video rows**); Google Sheets stays the raw analysis warehouse; Slack
is the reporting layer. `notion-sync` upserts the synthesized learnings into five
databases under `NOTION_PARENT_PAGE_ID`, locating them by title each run (no
local state) and updating rows in place:

- **Marketing Learnings**, **Signal Library**, **Next Creative Tests**,
  **Product Learnings**, **ICP Learnings**.

Prereqs: run `synthesize` first (it gates the sync), and set `NOTION_API_KEY` +
`NOTION_PARENT_PAGE_ID`. **The parent must be a normal Notion PAGE** (the five
databases are created as its children) — a database/`?v=` view URL won't work.
You can paste a full page URL or bare id; it's sanitized to a UUID. Missing
config or a Notion API error is shown cleanly in the dashboard, never crashes it.

`slack-report` posts a run summary to `SLACK_WEBHOOK_URL`. It's built to be
**actionable**: winning signals carry their confidence; **weak signals include an
example IG link** where the signal appeared (Slack only — never Notion); and
**creative tests are confidence-gated** — only surfaced when enough `Great`
videos support the pattern (labels: Directional / Medium confidence / Strong
confidence). Below the bar it shows *"No strong creative tests yet — more tagged
videos needed."*; above it, each test includes Product, ICP and a concrete
Execution line. The dashboard also auto-posts after a successful social run when
the webhook is set.

### Slack chat (interactive Marketing Brain — Notion-first)

An optional Slack app turns the brain into a **chat interface**: mention the
bot in any channel and it replies in-thread. It's read-only throughout — never
writes to the Sheet, never writes to Notion, never triggers video analysis,
never posts anywhere on its own.

**Notion Brain is the primary memory layer.** `src/notion_retrieval.py` reads
the same six databases `notion-sync` writes (Marketing Learnings, Signal
Library, Next Creative Tests, Product Learnings, ICP Learnings, Generated
Social Ideas) and normalizes rows into simple chunks. Learnings/signal/test
questions try Notion first — it's a synced snapshot of the same underlying
data, so answering from it is fast and doesn't need live Sheets access.
**`data/latest_learnings.md`, the analyzed Sheet, and `data/guidelines/*.md`
are the fallback/source layers** — used automatically whenever Notion isn't
configured, a database hasn't been synced yet, or the specific segment asked
about (e.g. an ICP with too few tagged videos) has no Notion entry. Either
way the underlying computation is the same correlation engine; Notion just
lets the bot skip recomputing it live when a synced answer already exists.

Modes (deterministic routing on the message text — `src/social_retrieval.py`
parses Product/ICP/taxonomy-layer/performance-bucket filters out of free
text; `social_brain.py` routes, queries Notion-first, and renders):

| Ask… | You get |
|---|---|
| `ideas` / *what should we post* / *ideas for BodyShield* / *ideas for parents* | 3–5 grounded Storelli video ideas — title, hook, storytelling structure, product/ICP, story blocks, visual beats, why, confidence, sources (plus a Notion Brain note when a related Product/ICP Learning or prior Generated Idea exists) |
| `feedback <IG link>` | Sheet lookup: performance bucket, Product, ICP, signals, diagnosis, next recommendation |
| `learnings` / *what's working* / *what should we avoid* / *why did this perform well?* | Winning + weak signals from Notion Signal Library, falling back to a live Sheet computation |
| *what hooks work for parents?* / *what formats should we avoid?* / *what did we learn about ExoShield?* | Notion ICP/Product Learnings or Signal Library for that segment/layer, falling back to a live Sheet computation segmented the same way |
| *show me examples* / *examples of Great videos* | Concrete example rows (link + signals) from the Sheet — Notion doesn't store per-video rows, so this mode is always Sheet-based |
| `tests` / *what are the next creative tests* | Notion's Next Creative Tests DB (including any operator-set Status), falling back to the synthesizer |
| *summarize the brain* | A compact cross-database overview: top winning/weak signal, learnings synced, next test, products/ICPs/ideas covered |

All modes only ever use "associated with" / "correlated with" language, cite
exactly the sources they retrieved — dynamically numbered `[S1]`, `[S2]`... in
retrieval-priority order (Notion Brain entries first, then
`latest_learnings.md`, then Sheet rows, then guidelines) — and never invent a
row, link, metric, or conclusion. An unmatched IG link, an empty Notion
database, or a segment with too little tagged data says so plainly instead of
guessing, and answers stay compact (a handful of bullets, not a data dump).

#### Threaded conversational mode

The bot feels like a chat, not a one-shot command responder — reply in-thread
and it understands follow-ups without re-stating context:

> "give me ideas for BodyShield" → "expand #2" → "make it for parents" →
> "show me sources" → "turn this into a content brief"

Recognized follow-up patterns: *expand #N*, *make it for \<segment\>*, *show
me sources*, *turn this into a content brief*, *shorter*, *why?*, *give me N
more*, *give me the risky version*, *what should we do next?*. Each either
deterministically transforms the **previous assistant message** (expand,
re-cite, compress, reformat — no new retrieval, so nothing is invented) or
re-runs one of the modes above with a segment pulled from the conversation.
Anything that doesn't match a follow-up pattern is treated as a fresh
question via the normal routing above.

**Context sourcing**, in `src/slack_bot.py`: a live fetch of Slack thread
history (`conversations.replies`) is tried first — best-effort, returns
cleanly with no context if the scope isn't granted — falling back to a small
in-memory-only cache of the last ~10 turns per thread (resets on restart, no
database). This cache is also what lets a thread be recognized as one "the
bot is already participating in," so a plain follow-up reply works without
re-mentioning the bot.

**Where it listens** — only three cases trigger a reply, never general channel
chatter: an `app_mention`, a direct message, or a reply inside a thread the
bot has already replied in. Required Slack app configuration:

| Capability | Event | Scope | Status |
|---|---|---|---|
| Mentions (existing) | `app_mention` | `app_mentions:read` | already enabled |
| Replying (existing) | — | `chat:write` | already enabled |
| Thread history backfill | — | `channels:history` (+ `groups:history` for private channels) | optional — code degrades to the in-memory cache without it |
| DMs | `message.im` | `im:history` | **not assumed — add both together if you want DM support** (Slack requires the scope to receive the event at all) |
| Channel follow-ups without re-mention | `message.channels` (+ `message.groups` for private channels) | `channels:history` (+ `groups:history`) | optional — code already filters to active-bot-threads only, safe to enable broadly |

#### Strategist mode (`src/social_strategist.py`)

By default, when `GEMINI_API_KEY` is set, the bot answers like a marketing
strategist rather than a retrieval tool: it still retrieves evidence first
via the exact same deterministic modes above (Notion-first, Sheet fallback,
the ideas engine — nothing new is queried here), then hands Gemini a compact,
already-cited evidence pack and asks for real judgment — "My read: ...",
what to do, why, what to avoid, a next action — instead of a data dump.
Follow-ups ("why?", "expand #2", "what are you least sure about?", "what
would you do if you were me?") re-derive the same underlying evidence (the
retrieval is deterministic, so re-running it reproduces the same facts) and
let Gemini resolve "#2" / "that" from the thread itself, rather than relying
on a rigid parser.

Controlled by `config.SLACK_STRATEGIST_MODE_ENABLED` — defaults to **on**
whenever `GEMINI_API_KEY` is configured (explicit `true`/`false` always
wins). Every answer is validated before it's ever shown:
- every `[S#]` it cites must already be a key in the evidence pack's own
  source list — an invented or unknown id discards the answer;
- every number/percentage it states must already appear in the evidence —
  an invented metric discards the answer;
- causal language ("causes", "leads to", ...) discards the answer;
- any leaked implementation language ("Notion row", "database", "retrieved
  context", raw JSON) discards the answer — the user should only ever see a
  strategist, never a description of the backend;
- a markdown table discards the answer (Slack renders those badly).

Any failure — disabled, Gemini errors, or a validation check above — falls
straight through to the fully deterministic engine (the same
expand/sources/shorter/brief/risky transforms and Notion-first modes this
bot already had), which is itself a complete, correctly-cited answer on its
own. Gemini only ever **words** an answer here; it never re-retrieves or
invents the underlying facts.

**Storelli brand context** (`data/storelli_context.md`) — plain-text brand
and strategy grounding (what Storelli makes, positioning, ICPs, content
goals, tone, claims discipline) that's fed into every strategist prompt so
it can explain *why* a signal matters for Storelli specifically ("lean into
BodyShield protection-proof content for parents"), not just restate the
signal name. Edit this file directly to sharpen that judgment — it loads
gracefully as empty if missing, so it's optional but recommended.

**Answer shape and conciseness are enforced, not just requested.** Each
question type gets a fixed contract (a "My read: ... / ranked learnings with
a reason each / next action / confidence" shape for strategy questions, a
numbered-ideas shape, or a "Diagnosis: ..." shape for feedback), the model is
told to collapse evidence into at most 3-5 ranked conclusions and cite only
the 1-3 strongest sources, and the reply is post-processed to cap it at 5
bullets / 5 cited sources (3 bullets when the user says "concise" / "short" /
"tl;dr" / "quick" / "top 3") regardless of what the model produced — a
targeted trim that preserves the closing sources/confidence lines rather
than a hard truncation.

**Proof links, not a source-id dump.** Every citation is resolved into a
normalized `Source` (`social_strategist.Source`) before it's shown, with a
strict link priority: a direct video/post URL (Notion property or a
Sheet-sourced IG link) first, else the Notion page URL, else no link at all
(title-only — never a fake one). The final answer shows only the **1-3
strongest** proof links by default (5 if you explicitly ask for "more
sources" / "all sources"), rendered as real Slack `<url|label>` links:
```
Sources:
- [S1] <https://...|Notion: Signal Library — Prevention>
- [S2] <https://...|Notion: Product Learnings — BodyShield>
```
This block is rebuilt deterministically after the model answers — inline
`Proof: [Sx]` citations in the model's own text are trimmed to match exactly
what's resolved at the bottom, so there's never a dangling citation with no
matching link. Ask **"show me the sources you used"** / **"source debug"**
for the raw view instead (id, title, chosen URL, Notion page URL, why it was
selected) — operator debugging only, never shown in normal answers.

**Slack progress UI.** While an answer is being composed, the bot shows
short, PUBLIC status stages in-thread — never private chain-of-thought, just
what's actually happening: "🧠 Thinking… reading Storelli context" → "🔎
Checking Notion Brain" → "🧩 Choosing strongest evidence" → "✍️ Writing
concise recommendation" → replaced by the final answer. `src/slack_bot.py`'s
`ProgressReporter` prefers Slack's native `assistant.threads.setStatus`
(needs the `assistant:write` scope — this app's default scopes don't include
it, so it probes once and silently falls back) to posting one message and
editing it in place (`chat.postMessage` + `chat.update`), so a duplicate
"thinking" message is never left behind — the same message becomes the
answer. If something fails mid-answer, that message is updated with *"I hit
an error while answering. The backend is alive, but ..."* instead of
disappearing silently.

**Optional plain LLM polish** (`config.SLACK_LLM_POLISH_ENABLED`, default
**off**, only used when strategist mode is off): a simpler, older mode where
Gemini just rewords the deterministic answer verbatim rather than composing
new judgment from a structured pack. Same validation, same fallback.

Both of these spend one Gemini call per Slack reply when active — worth
knowing since that quota (~20 req/day on the free tier) is shared with video
tagging. Strategist mode is on by default per the above, so budget
accordingly if you're mid-tagging-batch.

**Idea interpretation layer** (`src/interpretation.py`). The `ideas` mode is
backed by a small deterministic layer that joins winning signals × formats ×
top product/ICP into concrete reel briefs. If the user's message mentions a
Storelli product (e.g. "BodyShield", "gloves", "head guard") or an ICP
(e.g. "parents", "aspiring pro"), the ideas are biased to that segment. Every
idea comes with a sources block — analyzed Sheet rows (`[S1] row 12 — Great`),
the learnings file (`[S2] data/latest_learnings.md`), and any guideline files
loaded (`[S3] social_content_guidelines.md`). Metrics and links are never
invented; the "why" line always uses "associated with" / "correlated with".

Every substantive reply carries **inline sources** — `[S1] Sheet rows: …`,
`[S2] data/latest_learnings.md`, `[S3] guidelines: …` — and only cites what was
actually retrieved. Language is always associational ("associated with"),
never causal. If the Sheet isn't reachable, the bot says so cleanly; if
learnings haven't been generated yet, it points you to *Generate Learnings*.

Setup: create a Slack app, add bot scope `chat:write`, subscribe to
`app_mention`, and point the Request URL at
`https://<host>/slack/events`. Set `SLACK_BOT_TOKEN` (`xoxb-…`) and
`SLACK_SIGNING_SECRET` — see `DEPLOY.md` §4 for the exact steps.
`SLACK_WEBHOOK_URL` remains optional and independent (it powers the outbound
run report above).

### Evidence vs. inspiration (optional `Source Type` column)

The sheet can optionally include a **`Source Type`** column (also accepted as
`source_type` / `Source`). Rows whose value is *External / Inspiration /
Reference / Competitor / Creator* are treated as **inspiration only** — they
never enter correlations, never justify a lift, and can be cited as an
inspiration source (never as evidence). Rows marked *Internal / Storelli /
Owned* — and rows in sheets that don't have the column at all — behave
exactly as before. This is the only change to correlation behavior, and it
protects the learning layer against explicit external contamination.

## Inspiration Layer (external monitoring — no Apify)

External competitor/creator content lives in a **separate set of worksheets**
and is **never** Storelli proof: it can't enter performance buckets,
correlations, the Signal Library, Marketing Learnings, or any "what works for
Storelli" calculation. Isolation is structural (the internal pipeline only ever
reads the POC worksheet, `GOOGLE_WORKSHEET_NAME`) plus a defensive
`SOURCE_TYPE = EXTERNAL_INSPIRATION` guard on every row.

**Why no Apify:** yt-dlp reliably fetches metadata for an *individual* reel/post
URL (with cookies) but cannot reliably enumerate a profile's recent posts. So
instead of a scraper, we use a **human-in-the-loop URL queue**.

### Inspiration URL Queue (recommended path)

1. Open the **`INSPIRATION_URL_QUEUE`** tab (auto-created on first run if
   missing).
2. Paste one promising reel/post URL per row and fill the context columns:
   `CHANNEL_HANDLE`, `POST_URL` (required), `MACRO_INDUSTRY`, `SUBCATEGORY`,
   `REASON_FOR_ADDING`, `TARGET_PRODUCT`, `TARGET_ICP`. Leave `STATUS` blank or
   set it to `Queued`.
3. Run `python src/main.py process-inspiration-queue` (or click **Process
   Inspiration URL Queue** on the dashboard).

For each pending row the system fetches that single post's metadata via
**yt-dlp + the existing cookie config** (no profile enumeration, no Apify),
normalizes it, deduplicates by `SOURCE_ID` / `POST_ID` / `POST_URL`, and appends
a row to **`INSPIRATION_CONTENT`** with `SOURCE_TYPE = EXTERNAL_INSPIRATION`. The
queue row is then marked **Processed**, **Duplicate**, or **Failed** with
`PROCESSED_AT`, `SOURCE_ID`, and `ERROR_MESSAGE`. `CHANNEL_HANDLE`,
`MACRO_INDUSTRY`, and `SUBCATEGORY` are copied onto the content row;
`TARGET_PRODUCT` / `TARGET_ICP` / `REASON_FOR_ADDING` stay on the queue row
(linked by `SOURCE_ID`) for later milestones. Every run is logged to
`INSPIRATION_RUNS` with `RUN_TYPE = Queue`. One bad URL never aborts the run.

### Channel scan (metadata only)

`python src/main.py scan-inspiration` reads **ACTIVE** rows from the
**`MONITORED CHANNELS`** tab and appends new external posts to
`INSPIRATION_CONTENT` (`RUN_TYPE = Scan`). Note: profile enumeration via yt-dlp
is currently unreliable on Instagram, so the URL queue above is the recommended
ingestion path until a hosted provider is added.

### Research + Discovery Layer (Apify — IG/TikTok)

`python src/main.py discover-inspiration` (or **Discover Inspiration from Apify**
on the dashboard) finds *safe, high-signal* external candidates and appends them
to `INSPIRATION_CONTENT`. It runs **before** analysis and never touches internal
Storelli data.

**Setup:** set `APIFY_TOKEN` (required). Optional actor overrides:
`APIFY_INSTAGRAM_ACTOR_ID` (default `apify/instagram-scraper`),
`APIFY_TIKTOK_ACTOR_ID` (default `clockworks/tiktok-scraper`). If `APIFY_TOKEN`
is missing, discovery fails cleanly. The token is a secret — never commit it.

**Workflow:** add research queries to the **`APIFY_DISCOVERY_QUERIES`** tab
(auto-created on first run), set `ACTIVE=TRUE` on the ones to run, then run
discovery. Flow:

```
research queries → Apify IG/TikTok → copyright + relevance filter
  → view/follower ratio ranking → INSPIRATION_CONTENT candidates
  → (later) analyze-inspiration → (later) matching / scoring
```

**Matryoshka research rings** (`RESEARCH_RING` / `SEMANTIC_DISTANCE`), from
closest to Storelli outward: 1 goalkeeper pain/confidence/training · 2 youth
soccer/parent safety/coaching · 3 adjacent protection sports (hockey, lacrosse,
rugby, MTB, skate, moto) · 4 injury prevention/prehab/athlete safety · 5 gear
proof/product demo/UGC · 6 confidence/fear/psychology · 7 creator-led education
formats (do/don't, "watch before you buy", "3 things athletes need").

**Copyright / match-footage guardrails:** candidates are rejected (from
caption/hashtags/handle/query context — never face recognition) when they
involve famous/named players, match/broadcast/highlight footage, league or
national-team content (UCL/EPL/World Cup/etc.), fan or celebrity edits, save
compilations, gambling, or adult/violent/political content. Anything matching a
query's `SHOULD_AVOID` is also rejected.

**View/follower ratio priority (discovery signal only):** for each candidate we
compute `VIEW_FOLLOWER_RATIO = VIEW_COUNT / FOLLOWER_COUNT`, plus `RATIO_SCORE`,
`ABSOLUTE_VIEW_SCORE`, `MECHANISM_RELEVANCE_SCORE`, `COPYRIGHT_SAFETY_SCORE`, and
`PRIORITY_SCORE = 0.40·mechanism + 0.30·ratio + 0.15·absolute + 0.15·safety`.
Small/medium creators with unusually high ratios rank above giant accounts when
relevance is comparable. **This is a discovery-priority signal only — external
engagement is never Storelli proof and never enters correlations, the Signal
Library, Marketing Learnings, or "what works" calculations.**

**Cost/safety caps:** default `MAX_RESULTS=10` per query when blank; hard cap 25
per query (`APIFY_MAX_RESULTS_PER_QUERY`); hard cap 100 per full run
(`APIFY_MAX_RESULTS_PER_RUN`). A failed query never aborts the run. Runs are
logged to `INSPIRATION_RUNS` (`RUN_TYPE = Discovery`).

**Manual queue vs Apify discovery:** the manual `INSPIRATION_URL_QUEUE` (paste
individual URLs) still works and is unchanged — use it for hand-picked posts.
Discovery is the automated, research-driven feeder. Both converge on the same
`INSPIRATION_CONTENT` schema and dedup keyspace.

### Winning Format Profiles (internal evidence only)

`python src/main.py build-winning-profiles` (or **Build Winning Format
Profiles**) distills the completed/tagged Storelli evidence base into reusable
creative profiles in the **`WINNING_FORMAT_PROFILES`** tab. Each profile groups
the "Great" performers for a (Product, ICP) and records the dominant
hook/format/visual/problem/solution/funnel pattern, `INTERNAL_SAMPLE_SIZE`,
`PERFORMANCE_SIGNAL`, and `CONFIDENCE` (High = ≥5 supporting rows and ≥50% Great
rate; Medium = ≥3; below 3 → no profile). `PROFILE_ID` is stable per
(Product, ICP), so reruns update in place rather than duplicating.

**Only internal evidence is used.** External inspiration rows
(`SOURCE_TYPE=EXTERNAL_INSPIRATION`) are dropped defensively before counting —
external views / follower ratio / priority score never contribute to a profile's
sample size, confidence, or proof. Runs log to `INSPIRATION_RUNS`
(`RUN_TYPE=Profiles`). The builder only writes to `WINNING_FORMAT_PROFILES`; it
never modifies internal completed rows.

### Match inspiration to winning profiles

`python src/main.py match-inspiration` (or **Match Inspiration to Winning
Profiles**) scores each SAFE, ANALYZED external row against the ACTIVE
(Medium/High) winning profiles and shortlists the strongest references. It
writes match fields onto the external `INSPIRATION_CONTENT` rows only — it never
modifies `WINNING_FORMAT_PROFILES` or internal rows. Runs log to
`INSPIRATION_RUNS` (`RUN_TYPE=Match`).

- **MATCH_SCORE** (0–100) = 25% format + 20% hook + 15% problem/solution + 15%
  visual + 10% funnel + 10% product/ICP + 5% curation-context overlap (taxonomy
  fit only — engagement never affects it).
- **NOVELTY_SCORE** = same strategic mechanism (problem/solution/funnel) with a
  fresh execution (hook/format/visual). Near-copies and no-mechanism rows score
  low.
- **FINAL_SCORE** = 70% MATCH + 15% NOVELTY + 15% discovery `PRIORITY_SCORE`
  (secondary ranking only; renormalized when priority is absent). Discovery
  priority / views / follower ratio is **never Storelli proof** and never enters
  profiles, the Signal Library, Marketing Learnings, or correlations.
- **SHORTLISTED=TRUE** only when Safe + Analyzed + MATCH_SCORE ≥ 60 +
  FINAL_SCORE ≥ 60 + active matched profile + not famous-player/match/highlight/
  off-domain (a copyright safety net re-checks the caption at shortlist time).
  Otherwise `SHORTLIST_REASON` explains why not.

Reruns update the same rows in place (idempotent — no duplication). Not built
yet: idea generation, idea scoring.

### Inspiration candidate quality review

`python src/main.py quality-review-inspiration` (or **Quality Review Inspiration
Candidates**) is a quality-control gate (NOT idea scoring) over SAFE, ANALYZED
external candidates. For each it writes `CREATIVE_MECHANISM`, `ADAPTABILITY_SCORE`,
`STORELLI_RELEVANCE_SCORE`, `COPYRIGHT_RISK_SCORE`, `FAMOUS_PLAYER_RISK` /
`MATCH_FOOTAGE_RISK` / `OFF_DOMAIN_RISK`, `INSPIRATION_QUALITY_SCORE`,
`REVIEW_METHOD`, and `USE_FOR_IDEA_GEN`.

- **INSPIRATION_QUALITY_SCORE** = 35% adaptability + 30% Storelli relevance +
  20% mechanism clarity + 15% view/follower-ratio signal − copyright/off-domain
  penalty. The ratio is a prioritization signal only — high views alone can't
  pass a low-relevance candidate, and never make external content Storelli proof.
- **USE_FOR_IDEA_GEN=TRUE** only when Safe + Analyzed + quality ≥ 70 + copyright
  risk ≤ 30 + famous/match/off-domain risk all Low + a clear creative mechanism.
- **REVIEW_METHOD** = `Full Video` when the media was actually downloaded/
  inspected (best-effort on the top candidates via yt-dlp, one bad video never
  fails the run) or `Metadata Only` otherwise. A metadata-only review never
  claims full-video confidence. Runs log to `INSPIRATION_RUNS`
  (`RUN_TYPE=QualityReview`). Writes only to `INSPIRATION_CONTENT`.

### Slack rated-idea retrieval (Milestone 4B — read-only)

Ask the Marketing Brain in Slack for ideas and it retrieves, ranks, explains,
and critiques the rated ideas from `INSPIRATION_IDEAS` (`src/idea_retrieval.py`)
— **read-only**: it never generates ideas live, never writes to the sheet.
Idea asks are recognized semantically (not just exact keywords) and answered
deterministically (bypassing the LLM strategist so citations stay exact).

Supported asks include: *"give me 5 BodyShield ideas"*, *"what are the best
ideas we have?"*, *"show me parent-facing ideas"*, *"which ideas are worth
shooting?"*, *"critique the top ideas"*, *"what should we shoot first?"* (ranked
by production practicality, not just IDEA_SCORE), *"which ideas are too
generic?"*, *"show me the evidence behind the top idea"*.

Answers stay to the top 3–5, cite internal proof `[S#]` and external inspiration
`[E#]` as **separate** clickable Slack links, and never present external views as
proof. A light generic-language check flags hype phrases (game-changer, unleash,
dominate, inner keeper, zero hesitation, unbreakable…) and suggests sharper
rewrites — without touching the sheet. If no rated ideas exist, it falls back to
the older live signal-grounded idea path. All other Slack retrieval paths are
unchanged.

### Rated creative idea generation (Milestone 4A)

`python src/main.py generate-ideas` (or **Generate Rated Creative Ideas**)
produces Storelli-specific short-form video ideas by **adapting** high-quality
external creative *mechanisms* onto internal winning profiles — then rates each
one. Writes to the `INSPIRATION_IDEAS` tab; logs to `INSPIRATION_RUNS`
(`RUN_TYPE=Ideas`).

- **Every idea is anchored to an active internal winning profile** (the proof
  the format works for Storelli). An idea with no internal evidence is never
  written; external inspiration alone cannot produce an idea.
- Eligible external references only: Safe + Analyzed + `USE_FOR_IDEA_GEN=TRUE` +
  `INSPIRATION_QUALITY_SCORE ≥ 80` + all risks Low + a clear creative mechanism.
- **Citations are separated**: internal evidence as `[S#]`, external inspiration
  as `[E#]`. External views are never claimed as Storelli proof; captions/
  scripts/footage are never copied; no famous players / match / broadcast / fan
  edits / off-domain content.
- **IDEA_SCORE** = 25% evidence-fit + 20% inspiration-fit + 15% product-fit +
  10% ICP-fit + 10% execution-clarity + 10% novelty + 5% feasibility + 5%
  copyright-safety. `STRATEGIC_PRIORITY_SCORE` is a separate ranking field (not
  folded into IDEA_SCORE). Evidence-fit is anchored to the internal profile;
  copyright-safety is re-checked on the generated text.
- **Self-critique gate**: the model self-critiques and revises; a deterministic
  gate then drops generic hooks, copyright hits, missing shot lists, and
  sub-threshold ideas. Weak ideas are not written.

Not built: idea execution/publishing, Slack changes.

### External inspiration analysis (tagging)

`python src/main.py analyze-inspiration` (or **Analyze Inspiration Content** on
the dashboard) tags eligible `EXTERNAL_INSPIRATION` rows with the Storelli
creative taxonomy so they can *later* be matched against internal winning
formats. Inputs, in order: caption + thumbnail + structural metadata + human
queue context (`REASON_FOR_ADDING` / `TARGET_PRODUCT` / `TARGET_ICP`, used as
hints only). Set `INSPIRATION_FULL_VIDEO_ANALYSIS=true` to also download the reel
for richer analysis (off by default). Writes `*_TAGS`, `TAXONOMY_VERSION`,
`ANALYSIS_CONFIDENCE`, `ANALYSIS_STATUS`, `ERROR_MESSAGE`, `LAST_UPDATED_AT`;
logged to `INSPIRATION_RUNS` (`RUN_TYPE = Analyze`).

Confidence: **LOW** (caption/limited metadata → flagged `Needs Review`),
**MEDIUM** (caption + thumbnail + useful metadata), **HIGH** (only when full
video was analyzed). External engagement (likes/views/comments) is stored as
metadata only — it never influences confidence and is never Storelli proof.

Not built yet (later milestones): matching to internal learnings, winning-format
profiles, idea generation, idea scoring.

### Generated Social Ideas (Notion / jsonl)

`interpretation.build_idea_candidates()` output can be persisted:

- Notion path (preferred, when `NOTION_API_KEY` + `NOTION_PARENT_PAGE_ID` are
  set): a 6th database **Generated Social Ideas** is created under the parent
  page. Schema: Title (key), Channel, Product, ICP, Hook, Format, Storytelling
  Structure, Story Blocks, Visual Beats, Why This Should Work, Confidence,
  Sources, Status *(default Proposed)*, Created At, Posted URL, Result,
  Feedback. Upsert by Title. **Status / Posted URL / Result / Feedback are
  preserved on update** so operator edits survive resyncs. The five existing
  Notion databases are untouched by this flow.
- Fallback (when Notion is unavailable): one JSON line per idea appended to
  `data/generated_social_ideas.jsonl`. Never crashes the caller.

Trigger via `POST /run/generate-social-ideas` (requires `X-Run-Secret`) or by
importing `notion_brain.sync_or_persist_ideas(ideas, date_str)` directly.
Slack chat never writes to the Sheet and does not auto-persist ideas.

### Dev Brain mode (backend self-awareness + Slack-to-code handoff)

The same Slack bot can also explain its own architecture and draft (never
apply) implementation plans — `src/dev_brain.py`, routed to automatically
whenever a message looks like a backend/build question (`is_dev_question()`
checks for words like "backend", "architecture", "repo", capitalized "BE",
"where is X implemented/handled", "how would you add/build", "what files",
or the build-request trigger phrases below) rather than a marketing one;
everything else still goes to `social_brain`/`social_strategist`.

Grounded in two files kept in sync with the real repo:
- `data/backend_context.md` — curated: what things mean, safety rules, env
  var *names* (never values).
- `data/backend_map.json` — generated via `python scripts/build_backend_map.py`
  (introspects `src/*.py` for functions/classes via `ast`, routes, CLI
  commands, and `os.getenv(...)` names — regenerate after structural
  changes). Includes a curated `do_not_call_from_slack` list.

Backend answers cite files like `[src/web.py]` — validated against the
actual file list in the map (either `[bracket]` or `` `backtick` `` style;
citing a file that doesn't exist there is treated the same as an invented
fact and discards the answer in favor of a deterministic one built directly
from the same two files). Also checked: no secret-shaped strings (an
extra net — the context these prompts see never contains a real value, only
names, so this should never actually trigger).

**"push to code"** (or "create build request" / "tell claude code") never
edits this repo. It drafts a structured build request — title, user goal,
current system context, proposed implementation, files likely to change
(only ever real ones), safety constraints, tests, deployment notes, and a
complete Claude Code prompt — and returns it as Slack text prefixed "Build
request prepared. Paste this into Claude Code:". Gated to
`SLACK_DEV_ALLOWED_USER_IDS` (**empty by default = no one is authorized**);
an unauthorized request gets exactly: *"I can explain the backend, but I'm
not allowed to create build requests from your account."* Non-sensitive
backend Q&A stays open to any Slack user regardless.

`BUILD_REQUEST_TARGET` controls what happens beyond the Slack reply —
default `slack_only` does nothing else. `github_issue` also files a GitHub
issue (still just a ticket, never a PR or commit) via `GITHUB_TOKEN` +
`GITHUB_REPO`. `github_dispatch` also fires a `repository_dispatch` event
(`GITHUB_DISPATCH_EVENT`) — this only notifies an external workflow; **no
such workflow is implemented here**, and whoever wires one up must make it
open a branch/PR requiring human review, never push to main directly. Both
GitHub paths fail cleanly (the Slack reply still shows the build request)
if misconfigured or the API call errors.

## Upload Guidelines

The dashboard's **Upload Guidelines** section lets the operator paste
brand/content guidelines (Social Content / Email / Ads / Brand Voice / Product
Messaging). `POST /guidelines` saves one markdown file per type under
`data/guidelines/` (gitignored); `GET /guidelines` lists them. These are not
used for generation yet — `src/content_context.py` bundles
`data/latest_learnings.md` + `data/guidelines/*.md` (and, later, the Notion Brain)
as the context a future content/email/ad generator will draw on.

## Dashboard (FastAPI control panel)

A stupid-simple internal control panel (dark + brand-yellow, Saira). Not a SaaS
or login system — one HTML page, no DB, run state in memory (resets on restart).
Errors are shown in the page rather than crashing. One run at a time (409 while
busy). Read-only `GET`s are open; actions require the `X-Run-Secret` header.

Sections: **Run Controls** (limit dropdown 5/18/25/50/150/All, QA on/off toggle,
run-secret), **Run Status** (idle/running/completed/failed + scanned / analyzed /
skipped / needs_review / failed + top winning/weak signals), **Latest Learnings**
(live preview of `data/latest_learnings.md`), **Notion** (Open Notion Dashboard).

```
GET  /                 dashboard HTML
GET  /status           JSON run state + counts + top signals + notion_url
GET  /learnings        {exists, content} from data/latest_learnings.md
POST /run/social       analyze {limit, qa} in background        (X-Run-Secret)
POST /run/correlations recompute correlations in background     (X-Run-Secret)
POST /run/synthesize   regenerate latest_learnings.md           (X-Run-Secret)
```

Run locally:

```bash
RUN_SECRET=dev PYTHONPATH=src uvicorn web:app --reload   # http://127.0.0.1:8000
```

### Deploy on Railway

1. Push to the GitHub remote Railway is connected to. The included `Procfile`
   runs `uvicorn web:app` against `$PORT`. The brand logo in `static/` is served
   at `/static/logo-accent.png`.
2. In Railway → Variables, set:
   - `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`)
   - `GOOGLE_SHEET_ID`, `GOOGLE_WORKSHEET_NAME`
   - `GOOGLE_SERVICE_ACCOUNT_JSON_B64` — base64 of your service-account JSON
     (Railway can't read local files; on import, `config.py` decodes this to
     `/tmp/service-account.json`). Produce it locally:
     `base64 -i service-account.json | tr -d '\n'`.
   - `RUN_SECRET` — **required**; the `POST /run/*` endpoints return 503 until
     set. Generate one with `openssl rand -hex 24`.
   - `QA_COMPILER_ENABLED` — `false` to run 1 Gemini call/row (free-tier friendly).
   - `NOTION_DASHBOARD_URL` — optional; the "Open Notion Dashboard" button links
     here (button shows "not configured" until set).
   - `NOTION_API_KEY` + `NOTION_PARENT_PAGE_ID` — optional (Notion sync, later).
3. Share the Google Sheet with the service-account `client_email` as **Editor**.
4. Open the Railway URL, paste the run secret, pick a limit, toggle QA, click a
   button. Note: a real `Run Social Media Learning` consumes Gemini quota; the
   429 guardrail stops the run cleanly when the free tier is exhausted.
