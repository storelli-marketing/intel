"""Storelli intelligence MVP — CLI entrypoint.

Reads the POC sheet, analyzes each reel with Gemini + a QA compiler pass, writes
1/0 taxonomy tags back (empty cells only), and correlates tags against the manual
PERFORMANCE column. ICP/Product stay human grouping fields (filled only if blank).

Commands:
  python src/main.py analyze                 analyze eligible rows, write tags
  python src/main.py correlations            print signal/performance findings
  python src/main.py notion-sync             push findings to Notion
  python src/main.py run-all                 analyze -> correlations -> notion
  python src/main.py analyze --limit 5       test mode: at most 5 rows
  python src/main.py run-all --reprocess     re-tag rows (overwrite existing)
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import config
import correlations as corr
import performance
import taxonomy
from logger import get_logger
from sheets_client import PROCESSED_STATUSES, SheetsClient

log = get_logger()

_FINDINGS_PROMPT = os.path.join(os.path.dirname(__file__), "..", "prompts", "findings_summary_prompt.md")


def _valid_icp(value: str) -> str:
    """Return a canonical ICP label if the suggestion matches one, else ''."""
    target = taxonomy.slug(value)
    for canonical in taxonomy.ICP:
        if taxonomy.slug(canonical) == target:
            return canonical
    return ""


# Layers gated by confidence (Product is a grouping field, handled separately).
_GATED_LAYERS = ("hook", "format")


def _drop_layers(cols: dict, layers) -> dict:
    """Remove signal columns belonging to the given layers (so they are not
    written) — used to suppress low-confidence taxonomy fields."""
    prefixes = tuple(f"signal_{layer}_" for layer in layers)
    return {k: v for k, v in cols.items() if not k.startswith(prefixes)}


# Existing PERFORMANCE values we recognize as a "valid" manual label and
# their canonical display form (preserved when we don't overwrite).
_PERF_DISPLAY = {"great": "Great", "good": "Good", "ok": "Ok", "underdog": "Underdog"}


def _to_int(value) -> int | None:
    try:
        return int(str(value or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _row_value_ci(row: dict, *aliases) -> str:
    """Case-insensitive lookup of a metadata column by alias."""
    lower = {k.lower(): k for k in row}
    for a in aliases:
        actual = lower.get(a.lower())
        if actual is not None:
            return row[actual]
    return ""


def _determine_performance(row: dict, reprocess: bool) -> tuple[str | None, str]:
    """Decide which PERFORMANCE label to use for this row.

    Returns (label, write_value). `label` is the display value used for
    correlations (or None to skip the row). `write_value` is non-empty only
    when we should persist a freshly-computed PERFORMANCE to the sheet
    (existing cell blank, or --reprocess).
    """
    existing = str(row.get("PERFORMANCE", "")).strip()
    el = existing.lower()
    if el == "non classified":
        return None, ""  # explicit human skip
    has_existing = el in _PERF_DISPLAY

    views = _to_int(_row_value_ci(row, "views"))
    followers = _to_int(_row_value_ci(row, "followers")) or config.STORELLI_IG_FOLLOWER_COUNT
    computed = None
    if views is not None and followers and followers > 0:
        computed = performance.ratio_to_performance(views / followers)

    if has_existing and not reprocess:
        return _PERF_DISPLAY[el], ""
    if computed:
        return computed, computed   # write to sheet (blank existing or reprocess)
    if has_existing:
        return _PERF_DISPLAY[el], ""  # can't compute -> keep existing
    return None, ""


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def cmd_analyze(reprocess: bool, limit: int | None = None,
                qa_enabled: bool = True) -> dict:
    from analyzer import analyze_and_compile
    from gemini_client import GeminiClient, QuotaExhaustedError, VideoDownloadError

    sheets = SheetsClient()
    sheets.validate_columns()

    rows = sheets.read_rows()
    eligible = [r for r in rows if SheetsClient.should_process(r, reprocess)]

    # Count rows skipped specifically because they were already analyzed
    # (idempotency). Under --reprocess nothing is skipped for this reason.
    skipped_already = 0
    if not reprocess:
        skipped_already = sum(
            1 for r in rows
            if str(r.get("LINK", "")).strip()
            and str(r.get("PERFORMANCE", "")).strip().lower() != "non classified"
            and SheetsClient.is_processed(r)
        )

    targets = eligible
    if limit is not None and limit >= 0 and len(eligible) > limit:
        log.info("Limiting this run to %d of %d eligible row(s)", limit, len(eligible))
        targets = eligible[:limit]

    stats = {
        "scanned": len(rows),
        "eligible": len(eligible),
        "skipped_already_analyzed": skipped_already,
        "skipped_no_performance": 0,
        "analyzed": 0,
        "needs_review": 0,
        "failed": 0,
        "quota_stopped": False,
    }

    gemini = GeminiClient() if targets else None

    for r in targets:
        link = str(r.get("LINK", "")).strip()
        row_idx = r["_row"]

        # Determine PERFORMANCE first — keep existing, or auto-compute from
        # views/followers. If neither works, skip the row entirely (no write,
        # no Gemini) so it stays eligible once performance is added later.
        perf_label, perf_write = _determine_performance(r, reprocess)
        if perf_label is None:
            log.info("Row %d: no determinable PERFORMANCE -> skipped", row_idx)
            stats["skipped_no_performance"] += 1
            continue

        log.info("Analyzing row %d (PERFORMANCE=%s): %s", row_idx, perf_label, link)
        try:
            cols = analyze_and_compile(
                gemini, link,
                product=str(r.get("Product", "")),
                icp=str(r.get("ICP", "")),
                notes=str(r.get("Storytelling structure", "")),
                qa_enabled=qa_enabled,
            )

            # Confidence guardrail: suppress low-confidence taxonomy fields and
            # flag the row for human review rather than auto-writing a guess.
            skip_layers = [layer for layer in _GATED_LAYERS
                           if cols.get(f"conf_{layer}") == "low"]
            product_low = cols.get("conf_product") == "low"
            needs_review = bool(skip_layers) or product_low

            write_values = _drop_layers(cols, skip_layers)
            # IMPORTANT: pass the row's ORIGINAL state as existing_row so the
            # empty-only guard fires correctly. Mutating r before the write
            # makes every cell look pre-filled and silently skips all writes.
            sheets.write_row(
                row_idx, existing_row=r, signal_values=write_values, reprocess=reprocess,
                icp_fill=_valid_icp(cols.get("icp_suggested", "")),
                product_fill="" if product_low else str(cols.get("product_suggested", "")).strip(),
                status_value="needs_review" if needs_review else "completed",
                performance_value=perf_write,
            )
            # Reflect the written values in-memory only AFTER the write.
            r["PERFORMANCE"] = perf_label
            r.update(write_values)
            if needs_review:
                stats["needs_review"] += 1
                log.info("Row %d flagged needs_review (low confidence: %s)",
                         row_idx, ", ".join(skip_layers + (["product"] if product_low else [])))
            else:
                stats["analyzed"] += 1
        except QuotaExhaustedError as e:
            # Quota/rate limit — stop the run rather than marking every
            # remaining row failed. The row is left unprocessed (no Status
            # written) so it stays eligible for the next run.
            log.error("Gemini quota exhausted at row %d — stopping run. %s", row_idx, e)
            stats["quota_stopped"] = True
            break
        except VideoDownloadError as e:
            log.error("Download failed row %d: %s", row_idx, e)
            sheets.set_status(row_idx, "failed")
            stats["failed"] += 1
        except Exception as e:  # noqa: BLE001
            log.error("Analysis failed row %d: %s", row_idx, e)
            sheets.set_status(row_idx, "failed")
            stats["failed"] += 1

    return stats


# ---------------------------------------------------------------------------
# correlations
# ---------------------------------------------------------------------------
def compute_findings(sheets: SheetsClient) -> tuple[list[dict], dict, list[dict]]:
    """Returns (analyzed_rows, buckets, correlation_results).

    Buckets come from the manual PERFORMANCE column; correlations run over rows
    that have been tagged AND carry a recognized performance value.
    """
    all_rows = sheets.read_rows()
    buckets = performance.buckets_for_rows(all_rows)
    analyzed = [r for r in all_rows
                if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
    results = corr.compute(analyzed, buckets)
    return analyzed, buckets, results


def cmd_correlations(print_summary: bool = True) -> list[dict]:
    sheets = SheetsClient()
    sheets.validate_columns()
    analyzed, buckets, results = compute_findings(sheets)

    if not analyzed:
        log.warning("No tagged rows with performance to correlate. Run `analyze` first.")
        return results

    win = corr.winning(results)

    print("\n=== Signal / performance associations (correlation, not causation) ===")
    print(f"Tagged videos with performance: {len(analyzed)} "
          f"(positive class = '{performance.POSITIVE_BUCKET}')\n")
    for r in win[:15]:
        print(f"Signal: {r['signal']}")
        print(f"  Videos with signal: {r['videos_with_signal']}")
        print(f"  Great rate with signal:    {corr.fmt_pct(r['high_rate_with'])}")
        print(f"  Great rate without signal: {corr.fmt_pct(r['high_rate_without'])}")
        print(f"  Lift: {corr.fmt_lift(r['lift'])}")
        print(f"  Confidence: {r['confidence']}\n")
    return results


# ---------------------------------------------------------------------------
# findings assembly (for Notion)
# ---------------------------------------------------------------------------
def _templated_finding(r: dict, positive: bool) -> str:
    verb = "associated with a higher" if positive else "associated with a lower"
    return (
        f"'{r['label']}' ({r['layer']}) is {verb} '{performance.POSITIVE_BUCKET}' rate "
        f"({corr.fmt_pct(r['high_rate_with'])} with vs "
        f"{corr.fmt_pct(r['high_rate_without'])} without)."
    )


def build_findings(results: list[dict], completed: list[dict], buckets: dict) -> dict:
    win = corr.winning(results)[:10]
    wk = corr.weak(results)[:10]

    findings = {
        "winning_signals": [{
            "signal": r["label"], "layer": r["layer"],
            "finding": _templated_finding(r, True),
            "lift": corr.fmt_lift(r["lift"]),
            "sample_size": r["videos_with_signal"],
            "confidence": r["confidence"],
            "recommended_action": f"Test more {r['layer']} content using '{r['label']}'.",
        } for r in win],
        "weak_signals": [{
            "signal": r["label"], "layer": r["layer"],
            "finding": _templated_finding(r, False),
            "lift": corr.fmt_lift(r["lift"]),
            "sample_size": r["videos_with_signal"],
            "confidence": r["confidence"],
            "recommended_action": f"De-prioritize '{r['label']}' until more data confirms.",
        } for r in wk],
        "icp_learnings": [],
        "product_learnings": [],
        "next_creative_tests": [],
    }

    # Enrich qualitative sections with Gemini if a key is configured.
    try:
        import config
        if config.GEMINI_API_KEY:
            from gemini_client import GeminiClient
            with open(_FINDINGS_PROMPT, encoding="utf-8") as f:
                tmpl = f.read()
            payload = {
                "winning_signals": findings["winning_signals"],
                "weak_signals": findings["weak_signals"],
                "by_icp": _group_counts(completed, buckets, "ICP"),
                "by_product": _group_counts(completed, buckets, "Product"),
            }
            prompt = tmpl.replace("{findings_json}", json.dumps(payload, indent=2))
            from analyzer import parse_model_json
            text = GeminiClient().summarize_findings(prompt)
            enriched = parse_model_json(text)
            for k in ("winning_signals", "weak_signals", "icp_learnings",
                      "product_learnings", "next_creative_tests"):
                if enriched.get(k):
                    findings[k] = enriched[k]
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini findings enrichment skipped: %s", e)

    return findings


def _group_counts(rows: list[dict], buckets: dict, key: str) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in rows:
        g = str(r.get(key, "")).strip() or "(unspecified)"
        d = groups.setdefault(g, {"n": 0, "hi": 0})
        d["n"] += 1
        if performance.is_positive(buckets.get(r["_row"], "")):
            d["hi"] += 1
    return [{key.lower(): g, "videos": d["n"],
             "great_rate": corr.fmt_pct(d["hi"] / d["n"] if d["n"] else 0)}
            for g, d in groups.items()]


# ---------------------------------------------------------------------------
# synthesize (Learning Synthesizer -> data/latest_learnings.md)
# ---------------------------------------------------------------------------
def cmd_synthesize() -> int:
    from datetime import datetime, timezone

    import synthesizer

    sheets = SheetsClient()
    sheets.validate_columns()
    analyzed, buckets, results = compute_findings(sheets)
    if not analyzed:
        print("No tagged rows with performance yet — nothing to synthesize. "
              "Run `analyze` first.")
        return 0
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path = synthesizer.write_learnings(analyzed, buckets, results, ts)
    great = sum(1 for r in analyzed if performance.is_positive(buckets.get(r["_row"], "")))
    print(f"Learnings written to {path}")
    print(f"({len(analyzed)} tagged videos, {great} Great; "
          f"positive class = {performance.POSITIVE_BUCKET})")
    return 0


# ---------------------------------------------------------------------------
# reset-incomplete
# ---------------------------------------------------------------------------
def cmd_reset_incomplete() -> int:
    """Clear Status on rows that are marked processed (completed/failed/
    needs_review) but carry no real taxonomy tags — re-queues rows left
    half-baked by an interrupted/failed run so plain `analyze` picks them up."""
    sheets = SheetsClient()
    sheets.validate_columns()
    rows = sheets.read_rows()
    broken = [r["_row"] for r in rows
              if str(r.get("Status", "")).strip().lower() in PROCESSED_STATUSES
              and not SheetsClient.is_analyzed(r)]
    if not broken:
        print("No incomplete rows found (processed but untagged). Nothing to reset.")
        return 0
    sheets.reset_statuses(broken)
    log.info("Reset Status on %d incomplete row(s): %s", len(broken), broken)
    print(f"Reset Status to blank on {len(broken)} incomplete row(s) "
          f"(processed but no taxonomy) — they are eligible again.")
    print(f"Rows: {broken}")
    return 0


# ---------------------------------------------------------------------------
# notion-sync (Notion Brain — structured synthesized intelligence only)
# ---------------------------------------------------------------------------
def notion_sync(sheets: SheetsClient | None = None) -> dict:
    """Push synthesized learnings into the five Notion Brain databases.

    Returns a summary dict. Raises RuntimeError with a clean message when
    prerequisites are missing (caller shows it; no crash).
    """
    import os

    import synthesizer

    if not (config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID):
        raise RuntimeError("Notion not configured (NOTION_API_KEY / NOTION_PARENT_PAGE_ID).")
    if not os.path.exists(synthesizer.LEARNINGS_PATH):
        raise RuntimeError("No latest_learnings.md yet — run `synthesize` first.")

    sheets = sheets or SheetsClient()
    sheets.validate_columns()
    analyzed, buckets, results = compute_findings(sheets)
    if not analyzed:
        raise RuntimeError("No tagged rows with performance yet — nothing to sync.")

    from datetime import datetime, timezone

    import notion_brain
    s = synthesizer.synthesize(analyzed, buckets, results)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return notion_brain.NotionBrain().sync(s, date_str)


def _fmt_notion_summary(summary: dict) -> str:
    def tot(name):
        d = summary.get(name, {})
        return d.get("created", 0) + d.get("updated", 0)
    return (f"learnings created: {summary.get('Marketing Learnings', {}).get('created', 0)} · "
            f"signals updated: {tot('Signal Library')} · "
            f"tests created: {summary.get('Next Creative Tests', {}).get('created', 0)} · "
            f"product learnings updated: {tot('Product Learnings')} · "
            f"ICP learnings updated: {tot('ICP Learnings')}")


def cmd_notion_sync() -> int:
    try:
        summary = notion_sync()
    except RuntimeError as e:
        print(f"Notion sync skipped: {e}")
        return 1
    except Exception as e:  # noqa: BLE001 - surface Notion API errors cleanly
        print(f"Notion sync failed: {e}")
        return 1
    print("Notion Brain updated.")
    print(_fmt_notion_summary(summary))
    return 0


# ---------------------------------------------------------------------------
# slack-report
# ---------------------------------------------------------------------------
def gather_slack_inputs(sheets: SheetsClient | None = None,
                        videos_analyzed=None, notion_updated: bool | None = None) -> dict:
    """Collect everything the Slack report needs from the sheet + synthesis."""
    import synthesizer
    sheets = sheets or SheetsClient()
    sheets.validate_columns()
    analyzed, buckets, results = compute_findings(sheets)
    s = synthesizer.synthesize(analyzed, buckets, results)
    win = [f"{r['label']} ({corr.fmt_lift(r['lift'])})" for r in corr.winning(results)[:3]]
    weak = [f"{r['label']} ({corr.fmt_lift(r['lift'])})" for r in corr.weak(results)[:3]]
    tests = [t["hypothesis"] for t in s["tests"][:3]]
    if notion_updated is None:
        notion_updated = bool(config.NOTION_API_KEY and config.NOTION_PARENT_PAGE_ID)
    return {
        "videos_analyzed": videos_analyzed if videos_analyzed is not None else len(analyzed),
        "total_tagged": len(analyzed),
        "new_learnings": len(corr.winning(results)),
        "notion_updated": notion_updated,
        "winning": win, "weak": weak, "tests": tests,
        "dashboard_url": config.DASHBOARD_URL, "notion_url": config.NOTION_DASHBOARD_URL,
    }


def cmd_slack_report() -> int:
    import slack_report
    if not config.SLACK_WEBHOOK_URL:
        print("Slack report skipped: SLACK_WEBHOOK_URL not configured.")
        return 1
    try:
        data = gather_slack_inputs()
        msg = slack_report.build_message(**data)
        slack_report.post(msg)
    except Exception as e:  # noqa: BLE001
        print(f"Slack report failed: {e}")
        return 1
    print("Slack report posted.")
    return 0


# ---------------------------------------------------------------------------
# summary printer
# ---------------------------------------------------------------------------
def print_run_summary(stats: dict, results: list[dict], notion_done: bool) -> None:
    win = corr.winning(results)[:3] if results else []
    wk = corr.weak(results)[:3] if results else []
    print("\nRun completed.\n")
    print(f"Eligible rows found:        {stats.get('eligible', 0)}")
    print(f"Skipped (already analyzed): {stats.get('skipped_already_analyzed', 0)}")
    print(f"Skipped (no performance):   {stats.get('skipped_no_performance', 0)}")
    print(f"Analyzed:                   {stats.get('analyzed', 0)}")
    print(f"Needs review:               {stats.get('needs_review', 0)}")
    print(f"Failed:                     {stats.get('failed', 0)}")
    if stats.get("quota_stopped"):
        print("** Run STOPPED early: Gemini quota exhausted (429). "
              "Remaining rows left unprocessed. **")
    print(f"(rows scanned: {stats.get('scanned', 0)})\n")
    print("Top winning signals:")
    for i, r in enumerate(win, 1):
        print(f"{i}. {r['signal']} ({corr.fmt_lift(r['lift'])}, {r['confidence']})")
    if not win:
        print("  (none)")
    print("\nTop weak signals:")
    for i, r in enumerate(wk, 1):
        print(f"{i}. {r['signal']} ({corr.fmt_lift(r['lift'])}, {r['confidence']})")
    if not wk:
        print("  (none)")
    print(f"\nNotion updated: {'yes' if notion_done else 'no'}")


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Storelli intelligence MVP")
    parser.add_argument("command",
                        choices=["analyze", "correlations", "synthesize", "notion-sync",
                                 "slack-report", "run-all", "reset-incomplete"])
    parser.add_argument("--reprocess", action="store_true",
                        help="re-analyze rows already marked completed")
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="analyze at most N candidate rows this run (test mode)")
    parser.add_argument("--no-qa", action="store_true",
                        help="skip the QA compiler pass (1 Gemini call/row instead "
                             "of 2; stretches a limited free-tier quota)")
    args = parser.parse_args()

    qa_enabled = config.QA_COMPILER_ENABLED and not args.no_qa

    try:
        if args.command == "analyze":
            stats = cmd_analyze(args.reprocess, args.limit, qa_enabled)
            results = cmd_correlations(print_summary=False)
            print_run_summary(stats, results, notion_done=False)

        elif args.command == "correlations":
            cmd_correlations()

        elif args.command == "synthesize":
            return cmd_synthesize()

        elif args.command == "reset-incomplete":
            return cmd_reset_incomplete()

        elif args.command == "notion-sync":
            return cmd_notion_sync()

        elif args.command == "slack-report":
            return cmd_slack_report()

        elif args.command == "run-all":
            stats = cmd_analyze(args.reprocess, args.limit, qa_enabled)
            results = cmd_correlations(print_summary=False)
            print_run_summary(stats, results, notion_done=False)
            cmd_synthesize()
            cmd_notion_sync()      # prints clean message if Notion not configured
            cmd_slack_report()     # prints clean message if Slack not configured
    except RuntimeError as e:
        log.error(str(e))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
