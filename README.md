# Storelli Intelligence MVP

A lightweight, **agent-run** marketing-intelligence workflow for Storelli
(goalkeeper protective gear). Not a SaaS app, dashboard, or custom panel — just
a small Python CLI that reads a Google Sheet, analyzes Instagram reels with
Gemini, writes structured 1/0 signal columns back, buckets performance,
computes signal↔performance **associations**, and publishes findings to Notion.

```
Google Sheet (IG links + metrics)
  -> Agent runner reads unprocessed rows
  -> Gemini analyzes each video (hook / format / visual style / problem /
     solution / conversion / offer / product presence / funnel stage)
  -> Sheet updated with 1/0 signal columns + performance bucket
  -> Correlation engine (signal vs performance)
  -> Notion findings dashboard
```

## How Gemini "watches" an Instagram link

Gemini can't fetch an Instagram URL directly. For each row the runner:

1. downloads the reel to a temp file with `yt-dlp`,
2. uploads it via the Gemini **Files API**,
3. asks the model to tag it against the taxonomy and return JSON.

If a reel can't be downloaded (private/removed/rate-limited), the row is marked
`failed` and the run continues.

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

## Required sheet columns

`ig_link, product, icp, views, reach, likes, comments, shares, saves,
date_posted, processed_status`

Missing columns produce a clear error. Only rows where `processed_status` is
empty or `pending` are analyzed (unless `--reprocess`).

## Commands

```bash
python src/main.py analyze        # analyze pending rows, write tags + buckets
python src/main.py correlations   # print signal/performance findings
python src/main.py notion-sync    # push findings to Notion
python src/main.py run-all        # analyze -> correlations -> notion
python src/main.py run-all --reprocess   # also re-analyze completed rows
python src/main.py analyze --limit 5     # test mode: analyze at most 5 rows
```

`--limit N` caps how many candidate rows are analyzed in a single run (applies
to `analyze` and `run-all`). Use it for cheap test runs — e.g. the first
milestone's "analyze 3–5 links" — without downloading/calling Gemini on the
whole sheet. Combine with `--reprocess` to re-run a small fixed batch.

## What gets written back

Per analyzed row (raw user columns are **never** overwritten):

- `signal_<layer>_<slug>` = `1`/`0` for every taxonomy label
  (e.g. `signal_hook_curiosity_gap`, `signal_format_do_dont`,
  `signal_visual_style_raw_ugc`, `signal_problem_type_acute_pain`,
  `signal_funnel_stage_awareness`)
- `ai_summary` plus a `primary_<layer>` for each of the 9 layers
  (`primary_hook`, `primary_format`, `primary_visual_style`,
  `primary_problem_type`, `primary_solution_type`, `primary_conversion`,
  `primary_offer`, `primary_product_presence`, `primary_funnel_stage`)
- `performance_bucket` (Bad / OK / Good / Great, relative to the dataset)
- `processed_status = completed`, `processed_at`

## Taxonomy (9 AI-tagged layers)

Multi-label (tag all that apply): **Hook**, **Format**, **Visual Style**.
Single-label (exactly one): **Problem Type**, **Solution Type**, **Conversion**,
**Offer**, **Product Presence**, **Funnel Stage**.

**ICP** and **Product** are not AI-tagged — they are the human-provided raw
columns, used as grouping dimensions for ICP/Product learnings. Canonical
vocabularies for all layers (and for ICP/Product) live in
[src/taxonomy.py](src/taxonomy.py).

## Performance score

Min-max normalized per metric, weighted:

```
views .30 + reach .20 + shares .20 + saves .20 + comments .10
```

Buckets by percentile rank: bottom 20% = Bad, 20–50% = OK, 50–80% = Good,
top 20% = Great. Missing metrics drop their weight rather than zeroing the row.

## Correlation engine

Per signal: count of videos with it, Good/Great rate with vs without, and
**lift** (the difference). Confidence by sample size: High ≥ 20, Medium 8–19,
Low < 8. These are **correlations / associations, never causation.**

## Guardrails

- Never overwrites raw user columns (only signal + AI meta columns).
- Invalid Gemini JSON is retried once; persistent failure marks the row failed.
- Idempotent: completed rows are skipped unless `--reprocess` is passed.
- No frontend, no database, no Zapier/Make/n8n.

## Sample data

`data/sample_input.csv` / `data/sample_output.csv` show the shape of the sheet
before and after a run.
