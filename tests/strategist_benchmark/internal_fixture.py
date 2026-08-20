"""A realistic ANALYZED internal Storelli dataset for the benchmark.

Without this the brain has no internal evidence at all, so questions like "what
hook should we use?" can only abstain — which would measure the fixture, not the
strategist. These rows mirror the real POC shape: two-row-header metadata +
1/0 taxonomy cells + a human PERFORMANCE label + public Views.
"""
from __future__ import annotations

import taxonomy

_META = ["ID", "LINK", "PERFORMANCE", "Storytelling structure", "ICP", "Product",
         "Status", "VIEWS", "FOLLOWERS_AT_MEASUREMENT"]


def _row(n, perf, product, icp, hook, fmt, views, structure="Demo"):
    r = {"_row": n, "ID": str(n - 2),
         "LINK": f"https://www.instagram.com/storellisoccer/reel/BM{n:03d}/",
         "PERFORMANCE": perf, "Storytelling structure": structure, "ICP": icp,
         "Product": product, "Status": "completed", "VIEWS": str(views),
         "FOLLOWERS_AT_MEASUREMENT": "171204"}
    for c in taxonomy.all_signal_columns():
        r[c] = "0"
    r[taxonomy.column_for("hook", hook)] = "1"
    r[taxonomy.column_for("format", fmt)] = "1"
    r[taxonomy.column_for("visual_style", "Raw / UGC")] = "1"
    r[taxonomy.column_for("problem_type", "Acute Pain")] = "1"
    r[taxonomy.column_for("solution_type", "Prevention")] = "1"
    r[taxonomy.column_for("funnel_stage", "Awareness")] = "1"
    return r


# BodyShield/leggings: Curiosity Gap + Demo is the strong pattern (repeatable).
# Gloves/Aspiring Pro: Education/Authority is stronger -> a real CONTRADICTION.
# Parents: deliberately thin (2 rows) -> must trigger honest abstention.
ROWS = [
    _row(3, "Great", "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Demo", 24000),
    _row(4, "Great", "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Demo", 21000),
    _row(5, "Great", "BodyShield Leggings", "Adult Amateur", "Fear / Risk", "Demo", 19500),
    _row(6, "Good", "BodyShield Leggings", "Adult Amateur", "Curiosity Gap", "Story", 12000),
    _row(7, "Great", "BodyShield Leggings", "Aspiring Pro", "Curiosity Gap", "Demo", 26000),
    _row(8, "Underdog", "BodyShield Leggings", "Adult Amateur", "Humor", "Reaction", 2600),
    _row(9, "Underdog", "BodyShield Leggings", "General", "Aspiration", "Polished" if False else "Story", 3100),
    _row(10, "Ok", "BodyShield Leggings", "Adult Amateur", "Social Proof", "Comparison", 7400),
    _row(11, "Great", "GK Gloves", "Aspiring Pro", "Education", "Tutorial", 22000),
    _row(12, "Great", "GK Gloves", "Aspiring Pro", "Authority", "Tutorial", 20500),
    _row(13, "Good", "GK Gloves", "Aspiring Pro", "Education", "Do / Don't", 11800),
    _row(14, "Underdog", "GK Gloves", "Aspiring Pro", "Curiosity Gap", "Demo", 2900),
    _row(15, "Ok", "GK Gloves", "Adult Amateur", "Education", "Tutorial", 6900),
    _row(16, "Good", "ExoShield Head Guard", "Parents", "Fear / Risk", "Story", 9800),
    _row(17, "Ok", "ExoShield Head Guard", "Parents", "Education", "Tutorial", 5200),
    _row(18, "Great", "Sliders", "Adult Amateur", "Curiosity Gap", "Demo", 18000),
    _row(19, "Underdog", "Sliders", "General", "Humor", "Reaction", 2200),
    _row(20, "Ok", "Sliders", "Adult Amateur", "Education", "Demo", 6100),
]


class FakeSheetsClient:
    """Stands in for sheets_client.SheetsClient (read-only surface)."""

    def __init__(self, *a, **k):
        self.values = [[""] * len(_META), list(_META)]
        self.meta_col = {name: i + 1 for i, name in enumerate(_META)}
        self.signal_col = {c: len(_META) + i + 1
                           for i, c in enumerate(taxonomy.all_signal_columns())}

    def validate_columns(self):
        return None

    def read_rows(self):
        return [dict(r) for r in ROWS]

    # write surface: must never be exercised by a question
    def write_row(self, *a, **k):
        raise AssertionError("benchmark must not write to the internal sheet")

    def set_status(self, *a, **k):
        raise AssertionError("benchmark must not write to the internal sheet")

    def next_id(self):
        return len(ROWS) + 1


# The eligibility/analysis predicates are pure staticmethods on the real client;
# reuse them verbatim so the fixture can't drift from production semantics.
def _adopt_real_predicates():
    from sheets_client import SheetsClient as _Real
    for name in ("is_processed", "should_process", "should_tag", "is_analyzed"):
        setattr(FakeSheetsClient, name, staticmethod(getattr(_Real, name).__func__
                                                     if hasattr(getattr(_Real, name), "__func__")
                                                     else getattr(_Real, name)))


_adopt_real_predicates()
