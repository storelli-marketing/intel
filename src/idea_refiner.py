"""Milestone 4C — Creative Director Refinement Layer.

Refines the EXISTING rated ideas (INSPIRATION_IDEAS) so titles/hooks/concepts/
shot lists read like sharp Storelli creative, not generic AI copy. It does NOT
generate new ideas, does NOT change scoring, and writes ONLY to the refinement
columns — the original idea fields and all source fields are preserved untouched.

Guardrails: strip generic hype language, keep the concept grounded in the
Storelli product, keep copyright/match-footage safety (re-checked on the refined
text), and never copy external scripts/captions or alter source fields.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from idea_generator import copyright_recheck
from idea_retrieval import critique_points, generic_language_flags
from inspiration_scanner import _finalize_and_log_run, _new_run
from inspiration_sheets import IDEA_REFINE_COLUMNS, InspirationSheets
from logger import get_logger

log = get_logger()

# Generic hype phrases scrubbed from refined copy (safety net on top of the LLM,
# and the core of the no-model fallback).
SCRUB_PHRASES = [
    "game-changer", "game changer", "dominator", "dominate", "unleash",
    "unbreakable", "inner keeper", "zero hesitation", "next level", "revolutionary",
    "unstoppable", "insane", "ultimate", "secret sauce",
]

# Fields that must NEVER appear in a refinement writeback (originals + sources).
_PROTECTED_FIELDS = frozenset({
    "IDEA_TITLE", "HOOK", "CONCEPT", "SHOT_LIST", "STORELLI_ADAPTATION", "CTA",
    "PRODUCT", "ICP", "EXTERNAL_REFERENCE_URL", "EXTERNAL_REFERENCE_URLS",
    "EXTERNAL_SOURCE_ID", "EXTERNAL_SOURCE_IDS", "INTERNAL_EVIDENCE_IDS",
    "INTERNAL_EVIDENCE_URLS", "SOURCE_PROFILE_ID", "SOURCE_PROFILE_NAME",
    "IDEA_RATIONALE", "IDEA_SCORE", "STRATEGIC_PRIORITY_SCORE",
})


def scrub_generic(text: str) -> str:
    out = str(text or "")
    for p in SCRUB_PHRASES:
        out = re.sub(re.escape(p), "", out, flags=re.IGNORECASE)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+([,.:;!?])", r"\1", out)
    return out.strip(" -–—:,.\t").strip()


def has_generic(text: str) -> bool:
    t = str(text or "").lower()
    return any(p in t for p in SCRUB_PHRASES)


# ---------------------------------------------------------------------------
# weakness diagnosis (deterministic — reuses the retrieval critique)
# ---------------------------------------------------------------------------
def original_weakness(idea: dict) -> str:
    weaknesses = []
    flags = generic_language_flags(idea)
    if flags:
        weaknesses.append(f"generic hype in title/hook ({', '.join(sorted(set(flags)))})")
    title = str(idea.get("IDEA_TITLE", "")).strip()
    hook = str(idea.get("HOOK", "")).strip()
    if len(hook) < 15 or hook.lower() in ("", "check this out", "watch this"):
        weaknesses.append("hook not specific/visual enough")
    if not re.search(r"\d", hook) and "how to" not in hook.lower() and "myth" not in hook.lower():
        # soft signal: no concrete stakes (number/how-to/myth framing)
        pass
    for p in critique_points(idea):
        # critique_points already phrases product-fit/exec/novelty/evidence gaps
        weaknesses.append(p.split(" — ")[0].rstrip(".").lower())
    if not weaknesses:
        weaknesses.append("solid — minor polish only")
    # de-dupe, keep order
    seen, out = set(), []
    for w in weaknesses:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return "; ".join(out)[:400]


# ---------------------------------------------------------------------------
# prompt + refinement (LLM with deterministic fallback)
# ---------------------------------------------------------------------------
def build_refine_prompt(idea: dict) -> str:
    return (
        "You are a sharp Storelli creative director. Refine this EXISTING social "
        "video idea so it reads like a crisp, specific, shootable Storelli concept "
        "— not generic AI copy. Keep it grounded in the exact Storelli product; do "
        "NOT copy any external script/caption; do NOT reference famous players, "
        "match/broadcast footage, or fan edits.\n\n"
        f"Product: {idea.get('PRODUCT')}\nICP: {idea.get('ICP')}\n"
        f"Original title: {idea.get('IDEA_TITLE')}\n"
        f"Original hook: {idea.get('HOOK')}\n"
        f"Original concept: {idea.get('CONCEPT')}\n"
        f"Original shot list: {idea.get('SHOT_LIST')}\n"
        f"Storelli adaptation: {idea.get('STORELLI_ADAPTATION')}\n\n"
        "Rules: remove generic hype words (game changer, dominate, unleash, "
        "unbreakable, inner keeper, zero hesitation, secret, ultimate, insane). "
        "Make the hook concrete, visual, and specific (a real pain, number, "
        "mistake, or myth), not hype. Tighten the shot list into clear, shootable "
        "beats. Keep the strategic mechanism intact.\n\n"
        "Respond with ONLY JSON: {\"refined_title\":str,\"refined_hook\":str,"
        "\"refined_concept\":str,\"refined_shot_list\":[str],"
        "\"creative_director_notes\":str}"
    )


def _fallback_refine(idea: dict) -> dict:
    """Deterministic no-model refinement: scrub hype, keep substance."""
    return {
        "refined_title": scrub_generic(idea.get("IDEA_TITLE", "")) or str(idea.get("IDEA_TITLE", "")),
        "refined_hook": scrub_generic(idea.get("HOOK", "")) or str(idea.get("HOOK", "")),
        "refined_concept": scrub_generic(idea.get("CONCEPT", "")),
        "refined_shot_list": [b.strip() for b in str(idea.get("SHOT_LIST", "")).split("|") if b.strip()],
        "creative_director_notes": "Auto-cleaned (no model): removed generic hype; "
                                   "review for sharper hook specifics.",
        "_status": "Auto-cleaned",
    }


def refine_row(idea: dict, gemini) -> dict:
    """Return the refinement writeback cells for one idea (refinement columns
    ONLY). Never returns original/source field keys."""
    now = _now_iso()
    status = "Refined"
    if gemini is None:
        out = _fallback_refine(idea)
        status = out.pop("_status", "Auto-cleaned")
    else:
        try:
            from analyzer import parse_model_json
            out = parse_model_json(gemini.summarize_findings(build_refine_prompt(idea)))
        except Exception as e:  # noqa: BLE001 - one idea must not abort the run
            log.warning("refine failed for %s: %s", idea.get("IDEA_ID"), e)
            out = _fallback_refine(idea)
            status = "Auto-cleaned"

    title = scrub_generic(out.get("refined_title") or idea.get("IDEA_TITLE", ""))
    hook = scrub_generic(out.get("refined_hook") or idea.get("HOOK", ""))
    concept = scrub_generic(out.get("refined_concept") or idea.get("CONCEPT", ""))
    shot = out.get("refined_shot_list") or []
    if isinstance(shot, list):
        shot = " | ".join(str(x).strip() for x in shot if str(x).strip())
    shot = str(shot).strip()
    notes = str(out.get("creative_director_notes", "")).strip()

    # Copyright safety net on the refined text: if the refinement introduced any
    # risk, discard it and keep a scrubbed original + flag it.
    ok, why = copyright_recheck(" ".join([title, hook, concept]))
    if not ok:
        title = scrub_generic(idea.get("IDEA_TITLE", ""))
        hook = scrub_generic(idea.get("HOOK", ""))
        concept = scrub_generic(idea.get("CONCEPT", ""))
        shot = str(idea.get("SHOT_LIST", ""))
        notes = f"Refinement rejected ({why}); kept scrubbed original."
        status = "Needs Review"

    cells = {
        "REFINED_IDEA_TITLE": title,
        "REFINED_HOOK": hook,
        "REFINED_CONCEPT": concept[:1500],
        "REFINED_SHOT_LIST": shot[:1500],
        "CREATIVE_DIRECTOR_NOTES": (notes or "Sharpened title/hook; tightened shot list.")[:900],
        "ORIGINAL_WEAKNESS": original_weakness(idea),
        "REFINEMENT_STATUS": status,
    }
    # Hard guard: never emit an original/source field.
    assert not (_PROTECTED_FIELDS & set(cells)), "refinement must not touch original/source fields"
    return cells


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
def refine_ideas(sheets: Optional[InspirationSheets] = None, gemini=None) -> dict:
    sheets = sheets or InspirationSheets()
    try:
        sheets.ensure_idea_columns(IDEA_REFINE_COLUMNS)
    except Exception as e:  # noqa: BLE001
        log.warning("ensure refine columns failed (continuing): %s", e)

    if gemini is None:
        try:
            from gemini_client import GeminiClient
            gemini = GeminiClient()
        except Exception as e:  # noqa: BLE001
            log.warning("Gemini unavailable for refinement; using deterministic scrub: %s", e)
            gemini = None

    ideas = sheets.read_ideas()
    run = _new_run("Refine", "creative-director")
    run["POSTS_DISCOVERED"] = len(ideas)

    writes, refinements = [], []
    for idea in ideas:
        cells = refine_row(idea, gemini)
        writes.append((idea["_row"], cells))
        refinements.append({"idea": idea, "refined": cells})

    errors: list[str] = []
    try:
        sheets.update_idea_cells_bulk(writes)
    except Exception as e:  # noqa: BLE001
        errors.append(f"bulk write failed: {e}")
        run["POSTS_FAILED"] = len(writes)

    run["POSTS_ANALYZED"] = len(ideas) - run["POSTS_FAILED"]
    run["POSTS_SHORTLISTED"] = sum(
        1 for r in refinements if r["refined"]["REFINEMENT_STATUS"] == "Refined")
    run["_refinements"] = refinements
    log.info("Idea refinement: %d refined (%d clean)", len(ideas), run["POSTS_SHORTLISTED"])
    return _finalize_and_log_run(sheets, run, errors, failed=run["POSTS_FAILED"], total=1)


def print_refine_summary(run: dict) -> None:
    print("\nCreative director refinement complete.\n")
    print(f"Run ID:                 {run.get('RUN_ID')}")
    print(f"Status:                 {run.get('STATUS')}")
    print(f"Ideas refined:          {run.get('POSTS_ANALYZED')}")
    print(f"Clean 'Refined':        {run.get('POSTS_SHORTLISTED')}")
    if run.get("ERROR_SUMMARY"):
        print(f"Errors:                 {run.get('ERROR_SUMMARY')}")
