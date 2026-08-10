"""Social Analytics + Creative Test Planning layer (read-only).

The missing abstraction between the raw sheet and the Slack brain:

    raw metrics -> normalized analytics cohorts -> creative pattern extraction
                -> evidence / inspiration / bridge -> recommended tests

This module answers three concrete team questions extremely well, and — just as
importantly — refuses to invent data it doesn't have:

  1. "trial reels vs standard reels ... in terms of demographic?"  -> compare_trial_vs_standard
  2. "how many seconds long are our highest-performing reels?"     -> analyze_winning_reel_duration
  3. "give me a list of 20 ideas we should test"                   -> generate_creative_test_plan

Hard rules (inherited from the rest of the brain, enforced here too):
  * Internal Storelli rows are PROOF; external inspiration is execution
    reference only, never proof.
  * Metrics are never invented. If a field (demographics, duration, comments)
    isn't in the data, we say so plainly and name the exact field/backfill to add.
  * KPI outcomes aren't tracked, so every KPI line is an inferred / proxy bet.
  * Read-only: never writes the Sheet, never writes Notion, never triggers
    analysis, never touches cookies, never runs analyze-all.

The analytics functions read the *internal* POC worksheet through the existing
`SheetsClient` (same read-only path `social_brain` already uses); the creative
test plan draws on the existing brain (winning profiles, semantic connections,
refined ideas, calendar ratings, latest_learnings.md) via `InspirationSheets`.
Both data sources are fail-soft: an unreachable sheet degrades to an honest
"I can't reach that data" answer, never a crash or a fabricated one.
"""
from __future__ import annotations

import re
from typing import Optional

import decision_trace as dt
import performance
import slack_response_style as st
import taxonomy
from logger import get_logger

log = get_logger()

_NOT_PROOF = "_External inspiration is reference only — not proof it works for Storelli._"

# ---------------------------------------------------------------------------
# metric field vocabulary — each logical field maps to the sheet column-name
# aliases we recognize (case-insensitive, exact match on the header cell).
# ---------------------------------------------------------------------------
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "url": ("link", "url", "post url", "post_url", "reel url", "permalink"),
    "date": ("date", "posted", "post date", "published", "publish date", "posted at", "post_date"),
    "performance_label": ("performance", "performance label", "perf"),
    "views": ("views", "view count", "view_count", "plays", "play count", "reach"),
    "likes": ("likes", "like count", "like_count"),
    "comments": ("comments", "comment count", "comment_count"),
    "saves": ("saves", "saved", "bookmarks", "save count"),
    "shares": ("shares", "share count", "sends", "share_count"),
    "engagement_rate": ("engagement rate", "engagement_rate", "er", "engagement %",
                        "engagement percent", "engagement"),
    "followers": ("followers", "follower count", "follower_count", "audience size"),
    "duration": ("duration", "length", "seconds", "video length", "duration (s)",
                 "duration_sec", "runtime", "video_length", "reel length"),
    "post_type": ("post type", "post_type", "type", "content type", "media type", "media_type"),
    "reel_type": ("reel type", "reel_type", "trial", "trial reel", "trial/standard",
                  "distribution", "trial vs standard", "trial_or_standard"),
    "product": ("product",),
    "icp": ("icp",),
    "storytelling_structure": ("storytelling structure", "story structure", "storytelling",
                               "structure"),
    # demographics (IG audience-export fields) — almost never present today.
    "demo_age": ("age", "age range", "audience age", "age group"),
    "demo_gender": ("gender", "audience gender", "sex"),
    "demo_location": ("location", "country", "city", "top location", "audience location",
                      "geo"),
    "demo_follower_split": ("follower vs non-follower", "non-follower reach", "from followers",
                            "follower_vs_nonfollower", "followers vs non-followers",
                            "non follower reach"),
    "demo_reach_segment": ("reach by audience segment", "audience segment", "reach by segment",
                           "audience breakdown"),
}

_DEMO_FIELDS = ("demo_age", "demo_gender", "demo_location", "demo_follower_split",
                "demo_reach_segment")

# Performance metric hierarchy (best first). Tier 1: engagement rate. Tier 2:
# saves/comments/shares. Tier 3: views/likes. Tier 4: the manual Great/Good/
# Weak label when no raw metric exists.
_METRIC_PRIORITY = ("engagement_rate", "saves", "comments", "shares", "views", "likes")
_PERF_RANK = {"great": 3, "good": 2, "ok": 1, "underdog": 0}

# Duration buckets (seconds).
_DURATION_BUCKETS = ((0, 5, "0–5 sec"), (6, 10, "6–10 sec"), (11, 15, "11–15 sec"),
                     (16, 30, "16–30 sec"), (31, 45, "31–45 sec"), (46, 10**9, "46+ sec"))


# ---------------------------------------------------------------------------
# tiny helpers
# ---------------------------------------------------------------------------
def _lower(s) -> str:
    return str(s or "").strip().lower()


def _num(v):
    """Parse a possibly-messy numeric cell -> float, or None."""
    s = str(v if v is not None else "").strip().replace(",", "").replace("%", "")
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _row_get_ci(row: dict, *aliases) -> str:
    """Case-insensitive metadata lookup by any alias."""
    lower = {str(k).strip().lower(): k for k in row}
    for a in aliases:
        actual = lower.get(a.strip().lower())
        if actual is not None and str(row[actual]).strip() != "":
            return str(row[actual]).strip()
    return ""


def _column_for_field(columns: list[str], field: str) -> Optional[str]:
    """Return the actual sheet column name backing `field`, or None."""
    have = {str(c).strip().lower(): str(c).strip() for c in (columns or [])}
    for alias in _FIELD_ALIASES.get(field, ()):
        if alias in have:
            return have[alias]
    return None


def _parse_seconds(v) -> Optional[float]:
    """Parse a duration cell into seconds. Accepts 12, '12', '12s', '0:12',
    '00:01:03'. Returns None when unparseable/absent."""
    s = str(v or "").strip().lower().replace("sec", "").replace("s", "").strip()
    if not s:
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        secs = 0.0
        for n in nums:
            secs = secs * 60 + n
        return secs
    return _num(s)


def _duration_bucket(seconds: float) -> str:
    for lo, hi, label in _DURATION_BUCKETS:
        if lo <= seconds <= hi:
            return label
    return "46+ sec"


# ---------------------------------------------------------------------------
# data access (read-only, fail-soft) — the single indirection point so tests
# can inject fixtures and the golden harness can run fully offline.
# ---------------------------------------------------------------------------
def _internal_sheet() -> tuple[list[dict], list[str], str]:
    """Return (rows, column_names, error). Never raises.

    `column_names` are the metadata header cells actually present in the POC
    worksheet (this is how we know which raw-metric fields exist). `error` is a
    short string when the sheet is unreachable/unconfigured, else ''.
    """
    try:
        from sheets_client import SheetsClient
        sheets = SheetsClient()
        sheets.validate_columns()
        rows = sheets.read_rows()
        columns = list(sheets.meta_col.keys())
        return rows, columns, ""
    except Exception as e:  # noqa: BLE001 - Slack never sees a crash; degrade honestly
        log.warning("social_analytics: internal sheet unavailable: %s", e)
        return [], [], f"{type(e).__name__}: {e}"


def _brain():
    """Read-only snapshot of the existing brain (profiles / connections / ideas /
    calendar ratings / ad-hoc evals). Fail-soft -> empty lists."""
    def _rd(sheets, name):
        try:
            return getattr(sheets, name)()
        except Exception:  # noqa: BLE001
            return []
    try:
        from inspiration_sheets import InspirationSheets
        s = InspirationSheets()
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics: brain unavailable: %s", e)
        return {"profiles": [], "connections": [], "ideas": [], "calendar": []}
    return {
        "profiles": _rd(s, "read_profiles"),
        "connections": _rd(s, "read_semantic_connections"),
        "ideas": _rd(s, "read_ideas"),
        "calendar": _rd(s, "read_calendar_ratings"),
    }


# ---------------------------------------------------------------------------
# Part A — metrics audit
# ---------------------------------------------------------------------------
def detect_available_metrics(columns: Optional[list[str]] = None,
                             rows: Optional[list[dict]] = None) -> dict:
    """Report which logical metric fields exist in the current data.

    Returns {field: {"available": bool, "column": <sheet header or None>}}.
    `hook`/`format` are derived from the taxonomy tags (always modelled in the
    POC sheet), so they're reported available whenever the sheet is readable
    and any row carries a real (1) taxonomy tag.
    """
    if columns is None or rows is None:
        rows, columns, _err = _internal_sheet()
    out: dict[str, dict] = {}
    for field in _FIELD_ALIASES:
        col = _column_for_field(columns, field)
        out[field] = {"available": bool(col), "column": col}
    # hook / format come from the AI taxonomy layers, not a raw column.
    tagged = any(str(r.get(c, "")).strip() == "1"
                 for r in (rows or []) for c in taxonomy.all_signal_columns())
    for layer in ("hook", "format"):
        out[layer] = {"available": bool(tagged), "column": f"taxonomy:{layer}"}
    return out


def audit_metrics_schema(rows: Optional[list[dict]] = None,
                         columns: Optional[list[str]] = None) -> dict:
    """Full honest audit of what the internal data does / does not carry.

    Returns a structured dict (available / missing lists, demographics flag,
    duration flag, chosen performance metric, row count, error) plus a
    human-readable `report`.
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    avail = detect_available_metrics(columns, rows)

    # Everything the audit is asked to look for, in report order.
    order = ["url", "date", "performance_label", "views", "likes", "comments",
             "saves", "shares", "engagement_rate", "followers", "duration",
             "post_type", "reel_type", "product", "icp", "hook", "format",
             "storytelling_structure", "demo_age", "demo_gender", "demo_location",
             "demo_follower_split", "demo_reach_segment"]
    available = [f for f in order if avail.get(f, {}).get("available")]
    missing = [f for f in order if not avail.get(f, {}).get("available")]

    demographics_present = any(avail.get(f, {}).get("available") for f in _DEMO_FIELDS)
    duration_present = avail.get("duration", {}).get("available", False)
    trial_classifiable = _can_classify_reel_type(avail)

    result = {
        "ok": not err,
        "error": err,
        "row_count": len(rows or []),
        "available": available,
        "missing": missing,
        "fields": avail,
        "demographics_present": demographics_present,
        "duration_present": duration_present,
        "reel_type_classifiable": trial_classifiable,
        "chosen_metric": choose_metric(avail),
    }
    result["report"] = _render_audit_report(result)
    return result


def _render_audit_report(a: dict) -> str:
    if a["error"]:
        return f"Can't reach the analysis sheet right now ({a['error']})."
    lines = [f"Metrics audit — {a['row_count']} internal rows.",
             "Available: " + (", ".join(a["available"]) or "(none)"),
             "Missing: " + (", ".join(a["missing"]) or "(none)"),
             f"Demographics present: {'yes' if a['demographics_present'] else 'no'}",
             f"Duration present: {'yes' if a['duration_present'] else 'no'}",
             f"Trial vs standard classifiable: {'yes' if a['reel_type_classifiable'] else 'no'}",
             f"Best available performance metric: {a['chosen_metric']}"]
    return "\n".join(lines)


def _can_classify_reel_type(avail: dict) -> bool:
    """Trial vs standard is classifiable when there's an explicit reel/post-type
    column (a keyword-in-text fallback exists too, but that's decided per row)."""
    return bool(avail.get("reel_type", {}).get("available")
                or avail.get("post_type", {}).get("available"))


# ---------------------------------------------------------------------------
# Part A — reel-type classification + metric selection
# ---------------------------------------------------------------------------
def classify_reel_type(row: dict) -> str:
    """Classify a row as 'trial' | 'standard' | 'unknown'.

    Prefers an explicit Reel Type / Post Type / Trial column; falls back to a
    'trial' keyword in the storytelling-structure / notes / title text. Never
    guesses — an ambiguous row is 'unknown'.
    """
    explicit = _row_get_ci(row, "reel type", "reel_type", "trial", "trial reel",
                           "trial/standard", "distribution", "post type", "post_type",
                           "type")
    e = _lower(explicit)
    if e:
        if "trial" in e:
            return "trial"
        if any(k in e for k in ("standard", "normal", "regular", "organic", "default")):
            return "standard"
    text = " ".join(_lower(row.get(k, "")) for k in
                    ("Storytelling structure", "Notes", "notes", "Title", "title", "Caption"))
    if re.search(r"\btrial\b", text):
        return "trial"
    return "unknown"


def choose_metric(avail: dict) -> str:
    """Pick the best available performance metric per the hierarchy; falls back
    to the manual performance label when no raw metric exists."""
    for m in _METRIC_PRIORITY:
        if avail.get(m, {}).get("available"):
            return m
    return "performance_label"


def _metric_value(row: dict, metric: str) -> Optional[float]:
    """Numeric performance value for a row under the chosen metric. For
    'performance_label' it ranks Great>Good>Ok>Underdog."""
    if metric == "performance_label":
        return _PERF_RANK.get(_lower(row.get("PERFORMANCE", "")))
    aliases = _FIELD_ALIASES.get(metric, (metric,))
    return _num(_row_get_ci(row, *aliases))


# ---------------------------------------------------------------------------
# Part A / C — top performers + duration
# ---------------------------------------------------------------------------
def find_top_performing_posts(rows: Optional[list[dict]] = None,
                              columns: Optional[list[str]] = None,
                              limit: int = 5) -> list[dict]:
    """Rank INTERNAL posts by the best available metric and return the top ones.

    External / inspiration rows are excluded (never Storelli proof). Each result
    carries the link, the metric used, its value, and grouping fields.
    """
    if rows is None or columns is None:
        rows, columns, _err = _internal_sheet()
    avail = detect_available_metrics(columns, rows)
    metric = choose_metric(avail)
    scored = []
    for r in rows or []:
        if performance.is_reference_row(r):
            continue
        link = str(r.get("LINK", "")).strip()
        if not link:
            continue
        val = _metric_value(r, metric)
        if val is None:
            continue
        scored.append({
            "link": link,
            "metric": metric,
            "value": val,
            "performance": str(r.get("PERFORMANCE", "")).strip(),
            "product": str(r.get("Product", "")).strip(),
            "icp": str(r.get("ICP", "")).strip(),
            "structure": str(r.get("Storytelling structure", "")).strip(),
            "_row": r.get("_row"),
        })
    scored.sort(key=lambda d: d["value"], reverse=True)
    return scored[: max(1, limit)]


def analyze_winning_reel_duration(rows: Optional[list[dict]] = None,
                                  columns: Optional[list[str]] = None) -> dict:
    """Analyze how long the highest-performing reels are.

    Uses the best available metric to pick winners, then real duration data if
    present. Returns a structured dict; when duration is absent it says so and
    names the exact backfill needed (never invents seconds).
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    if err:
        return {"ok": False, "error": err, "duration_available": False}

    avail = detect_available_metrics(columns, rows)
    metric = choose_metric(avail)
    duration_col = _column_for_field(columns, "duration")
    # "Winners": top ~quartile by the chosen metric, at least 5.
    n_internal = sum(1 for r in rows or [] if not performance.is_reference_row(r)
                     and str(r.get("LINK", "")).strip())
    top_n = max(5, round(n_internal * 0.25)) if n_internal else 5
    winners = find_top_performing_posts(rows, columns, limit=top_n)

    if not duration_col:
        return {"ok": True, "error": "", "duration_available": False,
                "metric": metric, "winners": winners,
                "backfill_field": "duration_seconds",
                "recommended_source": "yt-dlp metadata (info['duration'])"}

    durations = []
    examples = []
    for w in winners:
        row = next((r for r in rows if r.get("_row") == w["_row"]), None)
        secs = _parse_seconds(row.get(duration_col)) if row else None
        if secs is None:
            continue
        durations.append(secs)
        examples.append({"link": w["link"], "seconds": secs,
                         "performance": w["performance"]})
    if not durations:
        return {"ok": True, "error": "", "duration_available": False,
                "metric": metric, "winners": winners,
                "backfill_field": "duration_seconds",
                "recommended_source": "yt-dlp metadata (info['duration'])",
                "note": "duration column exists but is empty for the top performers"}

    durations.sort()
    n = len(durations)
    median = durations[n // 2] if n % 2 else (durations[n // 2 - 1] + durations[n // 2]) / 2
    avg = sum(durations) / n
    counts: dict[str, int] = {}
    for s in durations:
        b = _duration_bucket(s)
        counts[b] = counts.get(b, 0) + 1
    common_bucket = max(counts.items(), key=lambda kv: kv[1])[0]
    return {"ok": True, "error": "", "duration_available": True, "metric": metric,
            "count": n, "median": round(median, 1), "average": round(avg, 1),
            "common_bucket": common_bucket, "bucket_counts": counts,
            "examples": sorted(examples, key=lambda e: e["seconds"])[:3],
            "winners": winners}


# ---------------------------------------------------------------------------
# Part B — trial vs standard cohorts
# ---------------------------------------------------------------------------
def compare_trial_vs_standard(rows: Optional[list[dict]] = None,
                              columns: Optional[list[str]] = None) -> dict:
    """Compare trial vs standard reels on whatever dimensions actually exist.

    Honesty gates, in order:
      1. Can we even classify trial vs standard? If not, say so + name the field.
      2. Are demographic fields present? If not, say so and compare what IS
         available (performance split, hook/format mix, product mix, duration,
         sample size).
    """
    if rows is None or columns is None:
        rows, columns, err = _internal_sheet()
    else:
        err = ""
    if err:
        return {"ok": False, "error": err}

    avail = detect_available_metrics(columns, rows)
    internal = [r for r in rows or []
                if not performance.is_reference_row(r) and str(r.get("LINK", "")).strip()]
    cohorts = {"trial": [], "standard": [], "unknown": []}
    for r in internal:
        cohorts[classify_reel_type(r)].append(r)

    classifiable = bool(cohorts["trial"] or cohorts["standard"])
    demographics_present = any(avail.get(f, {}).get("available") for f in _DEMO_FIELDS)

    result = {
        "ok": True, "error": "",
        "classifiable": classifiable,
        "demographics_present": demographics_present,
        "n_trial": len(cohorts["trial"]),
        "n_standard": len(cohorts["standard"]),
        "n_unknown": len(cohorts["unknown"]),
        "comparisons": {},
        "available_dims": [],
    }
    if not classifiable:
        return result

    dims = result["comparisons"]
    dims["performance"] = {c: _perf_split(cohorts[c]) for c in ("trial", "standard")}
    dims["hook_format"] = {c: _hook_format_mix(cohorts[c]) for c in ("trial", "standard")}
    dims["product"] = {c: _group_mix(cohorts[c], "Product") for c in ("trial", "standard")}
    if avail.get("duration", {}).get("available"):
        dcol = _column_for_field(columns, "duration")
        dims["duration"] = {c: _avg_duration(cohorts[c], dcol) for c in ("trial", "standard")}
    result["available_dims"] = ["performance", "hook/format mix", "product mix"] + \
        (["duration"] if "duration" in dims else []) + ["sample size"]
    return result


def _perf_split(cohort: list[dict]) -> dict:
    out = {"Great": 0, "Good": 0, "Ok": 0, "Underdog": 0, "n": len(cohort)}
    for r in cohort:
        label = str(r.get("PERFORMANCE", "")).strip().title()
        if label in out:
            out[label] += 1
    return out


def _hook_format_mix(cohort: list[dict]) -> dict:
    idx = taxonomy.signal_index()
    counts: dict[str, int] = {}
    for r in cohort:
        for col, meta in idx.items():
            if meta["layer"] in ("hook", "format") and str(r.get(col, "")).strip() == "1":
                counts[meta["label"]] = counts.get(meta["label"], 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3])


def _group_mix(cohort: list[dict], key: str) -> dict:
    counts: dict[str, int] = {}
    for r in cohort:
        g = str(r.get(key, "")).strip() or "(unspecified)"
        counts[g] = counts.get(g, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:3])


def _avg_duration(cohort: list[dict], dcol: str) -> Optional[float]:
    secs = [s for s in (_parse_seconds(r.get(dcol)) for r in cohort) if s is not None]
    return round(sum(secs) / len(secs), 1) if secs else None


# ---------------------------------------------------------------------------
# Part D — creative test plan (analysis -> inspiration -> bridge -> hypothesis)
# ---------------------------------------------------------------------------
def _split_urls(cell) -> list[str]:
    return [u.strip() for u in re.split(r"[;\n,]", str(cell or "")) if u.strip().startswith("http")]


def _active_profiles(brain) -> list[dict]:
    return [p for p in brain.get("profiles", [])
            if _lower(p.get("ACTIVE")) == "true"
            and _lower(p.get("CONFIDENCE")) in ("medium", "high")]


def _product_family(text: str) -> str:
    """Loose product family so 'BodyShield' matches 'BodyShield GK Leggings' etc."""
    t = _lower(text)
    if any(k in t for k in ("bodyshield", "leggings", "pants", "slider", "shorts", " leg")):
        return "leggings"
    if "glove" in t:
        return "gloves"
    if any(k in t for k in ("head guard", "headguard", "exoshield")):
        return "head"
    return ""


def _matches_focus(product: str, focus_product: str) -> bool:
    """True when no focus is set, or the product is in the same family as the
    focus (canonical-name mismatches like 'BodyShield' vs 'BodyShield GK
    Leggings' still match) — used to PREFER, never to hard-drop."""
    if not focus_product:
        return True
    ff = _product_family(focus_product)
    return not ff or _product_family(product) == ff


def _connection_for(product: str, brain) -> Optional[dict]:
    """Best same-family semantic connection, or None. Deliberately does NOT fall
    back to a different product's connection — a Gloves test must not borrow a
    BodyShield reference/structure as if it were its own."""
    fam = _product_family(product)
    pool = [c for c in brain.get("connections", [])
            if _product_family(c.get("PRODUCT", "")) == fam] if fam \
        else list(brain.get("connections", []))
    if not pool:
        return None
    return sorted(pool, key=lambda c: _num(c.get("CONNECTION_SCORE")) or 0, reverse=True)[0]


# Test-angle matrix: each angle turns internal proof into a distinct LEARNING
# goal, so a plan is hypotheses (not a generic idea dump). Extension angles are
# explicitly exploratory (thin — no internal proof yet).
_ANGLES = (
    ("baseline", "does our proven {hook} + {fmt} still win when tightened to one pain beat",
     "confirms the winning format holds", "High"),
    ("hook-swap", "does a sharper pain-confession hook beat our current {hook} open",
     "a stronger hook variant to standardize", "Medium"),
    ("length-test", "does a sub-10s cut hold retention better than our {fmt} norm",
     "the ideal reel length for this format", "Medium"),
    ("comment-driver", "does ending on a question drive more replies than a plain CTA",
     "whether question-CTAs lift comments (inferred)", "Medium"),
    ("format-variant", "does a Do/Don't cut of the same proof outperform the straight {fmt}",
     "a second format that carries the same proof", "Medium"),
    ("icp-extension", "does the {product} proof translate to a Parents/youth angle",
     "whether the format extends to a new ICP (no internal proof yet)", "Thin"),
)


def generate_creative_test_plan(count: int = 20, brain: Optional[dict] = None,
                                focus_product: str = "", focus_icp: str = "") -> dict:
    """Build a ranked creative TEST plan (not a generic idea dump).

    Every test follows: internal analysis anchor -> external inspiration
    (reference only) -> creative bridge (semantic connection / storytelling
    structure) -> test hypothesis -> KPI/proxy -> risk -> sources. Anchored to
    real internal winning profiles / refined ideas; extension angles that lack
    internal proof are labelled exploratory, never proven.
    """
    brain = brain if brain is not None else _brain()
    profiles = _active_profiles(brain)
    ideas = sorted(brain.get("ideas", []), key=lambda i: _num(i.get("IDEA_SCORE")) or 0,
                   reverse=True)
    # Prefer (never hard-drop) items matching the requested product, so a focused
    # ask surfaces the right ones first but the plan can still reach `count`.
    if focus_product:
        profiles.sort(key=lambda p: _matches_focus(str(p.get("PRODUCT", "")), focus_product),
                      reverse=True)
        ideas.sort(key=lambda i: _matches_focus(str(i.get("PRODUCT", "")), focus_product),
                   reverse=True)
    tests: list[dict] = []
    seen = set()

    def _add(t: dict) -> None:
        key = (t["product"], t["icp"], t["angle"])
        if key in seen:
            return
        seen.add(key)
        tests.append(t)

    # 1) Anchor a test on each refined/rated idea (strongest, real internal proof).
    for idea in ideas:
        if len(tests) >= count:
            break
        product = str(idea.get("PRODUCT", "")).strip() or focus_product or "the product"
        icp = str(idea.get("ICP", "")).strip() or focus_icp or "General"
        conn = _connection_for(product, brain)
        _add(_build_test(
            angle="idea-anchored",
            product=product, icp=icp,
            hook=_first(idea.get("HOOK_TAGS")) or "Curiosity Gap",
            fmt=_first(idea.get("FORMAT_TAGS")) or "Demo",
            structure=(conn or {}).get("STORYTELLING_STRUCTURE")
            or _first(idea.get("STORYTELLING_STRUCTURE")) or "Pain → Demo → Protected replay → CTA",
            title=idea.get("REFINED_IDEA_TITLE") or idea.get("IDEA_TITLE") or f"{product} test",
            hypothesis=f"does '{(idea.get('REFINED_IDEA_TITLE') or idea.get('IDEA_TITLE') or product)}' "
                       f"convert its proof into a repeatable win",
            proves="this specific idea is worth scaling",
            confidence="High",
            internal_urls=_split_urls(idea.get("INTERNAL_EVIDENCE_URLS")),
            profile_name=str(idea.get("SOURCE_PROFILE_NAME", "")),
            conn=conn))

    # 2) Fan out proven winning profiles across the test-angle matrix.
    for prof in profiles:
        product = str(prof.get("PRODUCT", "")).strip() or "the product"
        icp = str(prof.get("ICP", "")).strip() or focus_icp or "General"
        hook = _first(prof.get("HOOK_TAGS")) or "Curiosity Gap"
        fmt = _first(prof.get("FORMAT_TAGS")) or "Demo"
        conn = _connection_for(product, brain)
        structure = (conn or {}).get("STORYTELLING_STRUCTURE") or \
            "Pain → Demo → Protected replay → CTA"
        for angle, hyp_tmpl, proves, conf in _ANGLES:
            if len(tests) >= count:
                break
            eff_icp = "Parents" if angle == "icp-extension" else icp
            _add(_build_test(
                angle=angle, product=product, icp=eff_icp, hook=hook, fmt=fmt,
                structure=structure, title=f"{product} — {angle} test",
                hypothesis=hyp_tmpl.format(hook=hook, fmt=fmt, product=product),
                proves=proves, confidence=conf,
                internal_urls=_split_urls(prof.get("SUPPORTING_VIDEO_URLS")),
                profile_name=str(prof.get("PROFILE_NAME", "")), conn=conn))
        if len(tests) >= count:
            break

    return {"requested": count, "returned": len(tests), "tests": tests[:count],
            "short": len(tests) < count,
            "thin": not profiles and not ideas}


def _first(cell) -> str:
    for part in re.split(r"[;,]", str(cell or "")):
        if part.strip():
            return part.strip()
    return ""


def _build_test(angle, product, icp, hook, fmt, structure, title, hypothesis,
                proves, confidence, internal_urls, profile_name, conn) -> dict:
    ext_urls = _split_urls((conn or {}).get("EXTERNAL_REFERENCE_URLS"))
    return {
        "angle": angle, "title": title, "product": product, "icp": icp,
        "hook": hook, "format": fmt, "structure": structure,
        "internal_basis": profile_name or "Storelli winning profile",
        "internal_urls": internal_urls,
        "inspiration_ref": _handle(ext_urls[0]) if ext_urls else "",
        "external_urls": ext_urls[:1],
        "bridge": (conn or {}).get("CONCEPT_NAME", "") or structure,
        "hypothesis": hypothesis,
        "kpi_proxy": dt.kpi_value(structure or hook),
        "proves": proves,
        "fails": "the mechanism doesn't carry this angle — stop investing in it",
        "shootability": "High" if angle in ("baseline", "idea-anchored", "length-test") else "Medium",
        "risk": "keep the pain beat tasteful; don't overclaim injury prevention"
        if confidence != "Thin" else "no internal proof yet — treat as a cheap exploratory test",
        "confidence": confidence,
    }


def _handle(url: str) -> str:
    m = re.search(r"tiktok\.com/@([\w.\-]+)", url or "") or \
        re.search(r"instagram\.com/([\w.\-]+)/", url or "")
    return "@" + m.group(1) if m else "external reference"


# ---------------------------------------------------------------------------
# Slack rendering
# ---------------------------------------------------------------------------
class _Sources:
    """Ordered [S]/[E]/[C]/[N] source collector -> clickable Slack block."""

    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, kind: str, url: str, label: str) -> str:
        n = sum(1 for (t, _, _) in self.rows if t[0] == kind) + 1
        tag = f"{kind}{n}"
        self.rows.append((tag, str(url or "").strip(), label))
        return tag

    @property
    def has_external(self) -> bool:
        return any(t[0] == "E" for (t, _, _) in self.rows)

    def block(self) -> str:
        if not self.rows:
            return ""
        lines = ["*Sources:*"]
        for tag, url, label in self.rows:
            lines.append(f"  [{tag}] <{url}|{label}>" if url else f"  [{tag}] {label}")
        if self.has_external:
            lines.append(_NOT_PROOF)
        return "\n".join(lines)


def _fmt_split(split: dict) -> str:
    parts = [f"{k} {split[k]}" for k in ("Great", "Good", "Ok", "Underdog") if split.get(k)]
    return (", ".join(parts) or "no labels") + f" (n={split.get('n', 0)})"


def _render_trial_vs_standard(text: str, cmp: dict) -> str:
    mode = st.detect_response_mode(text)
    if not cmp.get("ok"):
        return dt.render(
            f"I can't reach the analysis sheet right now ({cmp.get('error', 'unknown')}).",
            [dt.step("Data check", "internal sheet unreachable", [], "risk", "Thin")],
            move="retry once the Sheet/service-account is configured.", mode=mode)

    if not cmp["classifiable"]:
        steps = [
            dt.step("Data check", "no trial/standard indicator in the data", [], "risk", "Thin"),
            dt.step("Cohort split", "can't separate trial vs standard reels", [], "inference", "Thin"),
        ]
        return dt.render(
            "I can't split trial vs standard yet — there's no trial/standard indicator in the data.",
            steps,
            move="add a 'Reel Type' column (Trial/Standard) — ideally the IG trial-reel flag — "
                 "then I can compare them.", mode=mode)

    dims = cmp["comparisons"]
    src = _Sources()
    # cite the first available internal example so the answer is grounded
    perf = dims.get("performance", {})
    steps = [
        dt.step("Data check", "demographic fields not in the data", [], "risk", "Thin"),
        dt.step("Cohort split",
                f"trial n={cmp['n_trial']} vs standard n={cmp['n_standard']}", [], "topic", "Medium"),
        dt.step("Performance",
                f"trial {_fmt_split(perf.get('trial', {}))}; standard {_fmt_split(perf.get('standard', {}))}",
                [], "internal", "Medium"),
    ]
    hf = dims.get("hook_format", {})
    steps.append(dt.step("Hook/format",
                         f"trial: {', '.join(hf.get('trial', {}) ) or 'n/a'}; "
                         f"standard: {', '.join(hf.get('standard', {})) or 'n/a'}",
                         [], "format", "Medium"))
    if "duration" in dims:
        d = dims["duration"]
        steps.append(dt.step("Duration",
                             f"trial {d.get('trial')}s vs standard {d.get('standard')}s avg",
                             [], "inference", "Medium"))
    steps.append(dt.kpi_step("engagement"))
    lead = ("I can compare trial vs standard on the data we have, but *not demographics* — "
            "demographic fields aren't in the current data.")
    dims_line = "; ".join(cmp["available_dims"])
    return dt.render(lead, steps,
                     move=f"comparing {dims_line}. Add IG audience-export fields "
                          "(age, gender, location, follower vs non-follower) for a real "
                          "demographic split.",
                     sources=src.block(), mode=mode)


def _render_demographics(text: str, cmp: dict) -> str:
    """A pure demographics ask (no trial/standard framing)."""
    mode = st.detect_response_mode(text)
    if cmp.get("ok") and cmp.get("demographics_present"):
        return _render_trial_vs_standard(text, cmp)
    steps = [
        dt.step("Data check", "no age/gender/location/follower-split fields", [], "risk", "Thin"),
        dt.step("What we have", "performance, product/ICP, hook/format, structure", [], "topic", "Medium"),
    ]
    return dt.render(
        "I don't have demographic-level data for this yet — age, gender, location and "
        "follower-vs-non-follower aren't in the current data.",
        steps,
        move="add the IG audience-export fields (or connect the IG Insights API) and I can "
             "break audience down for real.", mode=mode)


def _render_duration(text: str, d: dict) -> str:
    mode = st.detect_response_mode(text)
    if not d.get("ok"):
        return dt.render(f"I can't reach the analysis sheet right now ({d.get('error', 'unknown')}).",
                         [dt.step("Data check", "internal sheet unreachable", [], "risk", "Thin")],
                         move="retry once Sheets is configured.", mode=mode)
    if not d.get("duration_available"):
        steps = [
            dt.step("Data check", "no duration/seconds field on the reels", [], "risk", "Thin"),
            dt.step("Metric used", f"ranked winners by {d.get('metric')}", [], "internal", "Medium"),
        ]
        return dt.render(
            "I can't tell you the length of our best reels yet — there's no duration field in the data.",
            steps,
            move="store `duration_seconds` from yt-dlp metadata (`info['duration']`) going forward; "
                 "a safe one-time backfill can read duration from each reel's metadata only "
                 "(no video re-analysis, no re-tagging).", mode=mode)
    src = _Sources()
    for ex in d.get("examples", [])[:3]:
        src.add("S", ex["link"], f"Storelli reel — {ex['seconds']:.0f}s ({ex.get('performance') or 'top'})")
    steps = [
        dt.step("Metric used", f"top performers by {d['metric']}", [], "internal", "Medium"),
        dt.step("Pattern found",
                f"median {d['median']:.0f}s, avg {d['average']:.0f}s across {d['count']} winners",
                [t for (t, _, _) in src.rows[:1]], "internal", "High"),
        dt.step("Most common", f"{d['common_bucket']}", [], "topic", "Medium"),
        dt.kpi_step("retention"),
    ]
    lead = (f"Our highest-performing reels cluster around *{d['common_bucket']}* — "
            f"median {d['median']:.0f}s, average {d['average']:.0f}s.")
    return dt.render(lead, steps,
                     move=f"keep top reels in the {d['common_bucket']} range; test a tighter cut "
                          "against it.", sources=src.block(), mode=mode)


def _render_test_plan(text: str, plan: dict) -> str:
    """Compact, CEO-readable numbered test plan with sources grouped at bottom."""
    tests = plan.get("tests", [])
    if not tests:
        return dt.render(
            "I don't have enough internal evidence to build a grounded test plan yet.",
            [dt.step("Data check", "no active winning profiles or rated ideas", [], "risk", "Thin")],
            move="run build-winning-profiles / generate-ideas first, then I can plan tests "
                 "anchored to real proof.", mode=st.MODE_DEEP)

    src = _Sources()
    # one grouped source per distinct internal profile + external reference used
    prof_tag: dict[str, str] = {}
    ext_tag: dict[str, str] = {}
    lines = []
    for i, t in enumerate(tests, 1):
        pkey = t["internal_basis"]
        if pkey not in prof_tag:
            url = t["internal_urls"][0] if t["internal_urls"] else ""
            prof_tag[pkey] = src.add("S", url, f"Storelli proof — {pkey[:38]}")
        s_ref = prof_tag[pkey]
        refs = f"[{s_ref}]"
        if t["external_urls"]:
            ekey = t["external_urls"][0]
            if ekey not in ext_tag:
                ext_tag[ekey] = src.add("E", ekey, f"External reference — {t['inspiration_ref']}")
            refs += f"[{ext_tag[ekey]}]"
        tag = "" if t["confidence"] != "Thin" else " _(exploratory — no internal proof yet)_"
        # one line per test: anchor · product/ICP · structure · learning goal · KPI proxy
        lines.append(
            f"{i}. *{t['product']} / {t['icp']}* — {t['structure']}. "
            f"Test: {t['hypothesis']}; KPI: {t['kpi_proxy']} (proxy). {refs}{tag}")

    header = (f"Test plan — {len(tests)} experiments, each anchored to internal proof, "
              f"using external work as execution reference only:")
    if plan.get("short"):
        header += (f" (only {len(tests)} are grounded enough to propose — "
                   f"add more winning profiles/ideas to reach {plan.get('requested', len(tests))}.)")
    body = header + "\n" + "\n".join(lines)
    tail = src.block()
    text_out = body + ("\n\n" + tail if tail else "")
    # An explicit "list of N" is a scannable numbered list, not a 3-bullet CEO
    # answer — so we strip canned endings + number/linkify but do NOT apply the
    # word cap (which would trim items and desync the header count). Each item is
    # already one compact line.
    return st.format_trace_answer(st.remove_canned_endings(text_out))


# ---------------------------------------------------------------------------
# public Slack entrypoints
# ---------------------------------------------------------------------------
_DURATION_KW = ("how long", "how many seconds", "seconds long", "reel length", "best reel length",
                "duration", "length of our", "how long are our best", "highest performing reels duration",
                "best reels duration")
_DEMO_KW = ("demographic", "demographics", "audience split", "audience breakdown",
            "audience demographic", "who is watching", "who watches")
_TRIAL_KW = ("trial reel", "trial reels", "trial vs standard", "trial versus standard",
             "standard reels", "trial and standard")
_AUDIT_KW = ("metrics audit", "audit our metrics", "what metrics do we", "what data do we have",
             "what fields do we", "what analytics do we")

_TESTPLAN_STRONG = ("test plan", "creative test plan", "testing plan", "ideas to test",
                    "ideas we should test", "ideas we can test", "test ideas", "ideas to run as tests")
_TESTPLAN_ASK = ("what should we test", "what tests should we run", "what should we test next",
                 "which formats should we test", "what to test next", "tests should we run next",
                 "what should we test based", "what do we test next", "what can we test")


def is_social_analytics_query(text: str, context: Optional[list] = None) -> bool:
    t = " " + _lower(text) + " "
    if any(k in t for k in _TRIAL_KW):
        return True
    if any(k in t for k in _DEMO_KW):
        return True
    if any(k in t for k in _AUDIT_KW):
        return True
    # duration must be about reels/posts, not "how long should X be"
    if any(k in t for k in _DURATION_KW) and any(w in t for w in ("reel", "reels", "video", "post", "content", "best")):
        return True
    return False


def is_creative_test_plan_query(text: str, context: Optional[list] = None) -> bool:
    t = " " + _lower(text) + " "
    # Defer genuinely urgency-framed asks to the existing orchestrator.
    if "urgent" in t or "most urgent" in t:
        return False
    if any(k in t for k in _TESTPLAN_STRONG):
        return True
    if any(k in t for k in _TESTPLAN_ASK):
        return True
    # "give me a list of 20 ideas we should test" / "20 ideas to test"
    if re.search(r"\b\d+\b", t) and "idea" in t and ("test" in t or "experiment" in t):
        return True
    return False


def _parse_count(text: str, default: int = 20) -> int:
    m = re.search(r"\b(\d{1,3})\b", text or "")
    if not m:
        return default
    n = int(m.group(1))
    return max(1, min(n, 40))


def answer_social_analytics_question(text: str, context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for analytics questions (trial/standard, demographics,
    duration, metrics audit). Returns None if it doesn't own the question."""
    t = _lower(text)
    try:
        if any(k in t for k in _DURATION_KW):
            return _render_duration(text, analyze_winning_reel_duration())
        if any(k in t for k in _TRIAL_KW):
            return _render_trial_vs_standard(text, compare_trial_vs_standard())
        if any(k in t for k in _DEMO_KW):
            return _render_demographics(text, compare_trial_vs_standard())
        if any(k in t for k in _AUDIT_KW):
            a = audit_metrics_schema()
            mode = st.detect_response_mode(text)
            steps = [dt.step("Available", ", ".join(a["available"][:8]) or "none", [], "topic", "Medium"),
                     dt.step("Missing", ", ".join(a["missing"][:8]) or "none", [], "risk", "Medium"),
                     dt.step("Best metric", a["chosen_metric"], [], "internal", "Medium")]
            return dt.render(
                f"Here's what our data actually carries ({a['row_count']} internal rows):",
                steps,
                move=("add duration + IG audience-export fields to unlock length and demographic "
                      "analysis." if not (a["duration_present"] and a["demographics_present"])
                      else "data coverage looks good."), mode=mode)
    except Exception as e:  # noqa: BLE001 - never break the bot
        log.warning("social_analytics answer failed: %s", e)
        return None
    return None


def answer_creative_test_plan(text: str, context: Optional[list] = None) -> Optional[str]:
    """Slack entrypoint for 'give me N ideas to test' / 'what should we test next'."""
    try:
        import interpretation
        count = _parse_count(text, default=20 if any(k in _lower(text) for k in _TESTPLAN_STRONG) else 8)
        focus_product = interpretation.detect_product(text) or ""
        focus_icp = interpretation.detect_icp(text) or ""
        plan = generate_creative_test_plan(count=count, focus_product=focus_product,
                                           focus_icp=focus_icp)
        return _render_test_plan(text, plan)
    except Exception as e:  # noqa: BLE001
        log.warning("social_analytics test plan failed: %s", e)
        return None
