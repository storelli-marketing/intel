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
python src/main.py analyze        # analyze eligible rows, write taxonomy tags
python src/main.py correlations   # print signal/performance findings
python src/main.py notion-sync    # push findings to Notion (later)
python src/main.py run-all        # analyze -> correlations -> notion
python src/main.py run-all --reprocess   # re-tag rows (overwrite existing)
python src/main.py analyze --limit 5     # test mode: at most 5 rows
```

`--limit N` caps how many eligible rows are analyzed in a run — use it for cheap
test runs without calling Gemini on the whole sheet.

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

## Internal web trigger (FastAPI)

A tiny FastAPI app wraps the CLI so the analyze loop can be kicked off from a
browser. It's not a SaaS, dashboard app, or login system — just three endpoints
and a small HTML page. No database; run state is kept in memory and resets on
restart.

```
GET  /            Dashboard: title, limit dropdown (5/25/50/150/all),
                  run-secret input, status, last-run summary.
POST /run/social  Queues analyze (with --limit) + correlations + (if Notion
                  env vars are set) notion-sync, in a background thread.
                  Requires X-Run-Secret header == RUN_SECRET env. Returns 409
                  if a run is already in progress.
GET  /status      JSON: status (idle|queued|running|completed|failed), counts
                  (scanned / analyzed / needs_review / skipped / failed), top
                  winning + weak signals, notion sync state.
```

Run locally:

```bash
PYTHONPATH=src uvicorn web:app --reload   # http://127.0.0.1:8000
```

### Deploy on Railway

1. Push this repo to the GitHub remote Railway is connected to. The included
   `Procfile` runs `uvicorn web:app` against `$PORT`.
2. In Railway → Variables, set:
   - `GEMINI_API_KEY`, `GEMINI_MODEL` (default `gemini-2.5-flash`)
   - `GOOGLE_SHEET_ID`, `GOOGLE_WORKSHEET_NAME`
   - `GOOGLE_SERVICE_ACCOUNT_JSON_B64` — base64 of your service-account JSON
     (Railway can't read local files; on import, `config.py` decodes this to
     `/tmp/service-account.json` and points the loader at it). To produce the
     value locally: `base64 -i service-account.json | tr -d '\n'`.
   - `NOTION_API_KEY` + `NOTION_PARENT_PAGE_ID` — optional; the web trigger
     skips Notion sync if either is blank.
   - `RUN_SECRET` — required; the `/run/social` endpoint returns 503 until
     this is set. Generate one with `openssl rand -hex 24`.
3. Make sure the Google Sheet is shared with the service-account `client_email`
   as **Editor**.
4. Open the Railway URL, paste the run secret, pick a limit, click run.
