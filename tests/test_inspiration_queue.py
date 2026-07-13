"""Unit tests for the human-in-the-loop Inspiration URL Queue.

Proves: queued URLs are processed, duplicates are skipped safely, one failing
URL does not abort the run, SOURCE_TYPE is always EXTERNAL_INSPIRATION, and the
queue path cannot feed the internal Storelli learning layer.

Run: python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import inspiration_scanner as scan
from inspiration_scanner import InspirationPost, InspirationProvider
from inspiration_sheets import SOURCE_TYPE_EXTERNAL
import performance
import correlations as corr
from sheets_client import SheetsClient
import taxonomy

A_SIGNAL_COL = taxonomy.all_signal_columns()[0]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class FakeQueueProvider(InspirationProvider):
    name = "fakeq"

    def __init__(self, by_url):
        self._by_url = by_url

    def fetch_recent_posts(self, **kw):   # not used by the queue
        return []

    def fetch_post(self, url):
        if url not in self._by_url:
            raise RuntimeError(f"unfetchable {url}")
        return self._by_url[url]


class FakeQueueSheets:
    def __init__(self, queued, existing=None):
        self._queued = queued
        self._existing = existing or {"SOURCE_ID": set(), "POST_ID": set(), "POST_URL": set()}
        self.appended = []
        self.runs = []
        self.queue_updates = []
        self.ensured = False

    def ensure_queue_tab(self):
        self.ensured = True
        return False

    def read_queued_urls(self):
        return list(self._queued)

    def existing_content_keys(self):
        return {k: set(v) for k, v in self._existing.items()}

    def append_content_rows(self, rows):
        for r in rows:
            if r.get("SOURCE_TYPE") != SOURCE_TYPE_EXTERNAL:
                raise ValueError("unlabeled external row")
        self.appended.extend(rows)
        return len(rows)

    def append_run(self, run):
        self.runs.append(run)

    def update_queue_row(self, row_index, **kw):
        self.queue_updates.append((row_index, kw))


def _qrow(row, url, handle="@acct", **extra):
    d = {"_row": row, "POST_URL": url, "CHANNEL_HANDLE": handle,
         "MACRO_INDUSTRY": "Sports", "SUBCATEGORY": "Goalkeeper", "STATUS": ""}
    d.update(extra)
    return d


def _post(pid, url):
    return InspirationPost(post_id=pid, post_url=url, post_type="Reel",
                           caption="cap", scrape_status="Success")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestQueueProcessing(unittest.TestCase):
    def test_queued_urls_are_processed(self):
        q = [_qrow(2, "https://ig/reel/A/"), _qrow(3, "https://ig/reel/B/")]
        prov = FakeQueueProvider({
            "https://ig/reel/A/": _post("A", "https://ig/reel/A/"),
            "https://ig/reel/B/": _post("B", "https://ig/reel/B/"),
        })
        sheets = FakeQueueSheets(q)
        run = scan.process_queue(sheets=sheets, provider=prov)

        self.assertEqual(run["RUN_TYPE"], "Queue")
        self.assertEqual(run["POSTS_ADDED"], 2)
        self.assertEqual(run["POSTS_FAILED"], 0)
        self.assertEqual(run["STATUS"], "Completed")
        self.assertEqual(sorted(r["POST_ID"] for r in sheets.appended), ["A", "B"])
        # Both queue rows marked Processed with a SOURCE_ID.
        statuses = [kw["status"] for _, kw in sheets.queue_updates]
        self.assertEqual(statuses, ["Processed", "Processed"])
        self.assertTrue(all(kw.get("source_id") for _, kw in sheets.queue_updates))
        # Preserved context on the content row.
        self.assertEqual(sheets.appended[0]["MACRO_INDUSTRY"], "Sports")
        self.assertEqual(sheets.appended[0]["HANDLE"], "@acct")

    def test_source_type_always_external(self):
        q = [_qrow(2, "https://ig/reel/A/")]
        prov = FakeQueueProvider({"https://ig/reel/A/": _post("A", "https://ig/reel/A/")})
        sheets = FakeQueueSheets(q)
        scan.process_queue(sheets=sheets, provider=prov)
        self.assertTrue(all(r["SOURCE_TYPE"] == SOURCE_TYPE_EXTERNAL
                            for r in sheets.appended))

    def test_duplicate_url_skipped_safely(self):
        # A already exists in INSPIRATION_CONTENT (by SOURCE_ID).
        q = [_qrow(2, "https://ig/reel/A/")]
        prov = FakeQueueProvider({"https://ig/reel/A/": _post("A", "https://ig/reel/A/")})
        sheets = FakeQueueSheets(q, existing={
            "SOURCE_ID": {"instagram:A"}, "POST_ID": set(), "POST_URL": set()})
        run = scan.process_queue(sheets=sheets, provider=prov)
        self.assertEqual(run["POSTS_ADDED"], 0)
        self.assertEqual(run["POSTS_SKIPPED_EXISTING"], 1)
        self.assertEqual(sheets.appended, [])
        self.assertEqual(sheets.queue_updates[0][1]["status"], "Duplicate")

    def test_within_run_duplicate_skipped(self):
        # Same post pasted twice in one batch — second is a Duplicate.
        q = [_qrow(2, "https://ig/reel/A/"), _qrow(3, "https://ig/reel/A2/")]
        prov = FakeQueueProvider({
            "https://ig/reel/A/": _post("A", "https://ig/reel/A/"),
            "https://ig/reel/A2/": _post("A", "https://ig/reel/A/"),  # same id+url
        })
        sheets = FakeQueueSheets(q)
        run = scan.process_queue(sheets=sheets, provider=prov)
        self.assertEqual(run["POSTS_ADDED"], 1)
        self.assertEqual(run["POSTS_SKIPPED_EXISTING"], 1)

    def test_failed_url_does_not_abort_run(self):
        q = [_qrow(2, "https://ig/reel/BAD/"), _qrow(3, "https://ig/reel/OK/")]
        prov = FakeQueueProvider({"https://ig/reel/OK/": _post("OK", "https://ig/reel/OK/")})
        sheets = FakeQueueSheets(q)
        run = scan.process_queue(sheets=sheets, provider=prov)
        self.assertEqual(run["POSTS_FAILED"], 1)
        self.assertEqual(run["POSTS_ADDED"], 1)          # OK still ingested
        self.assertEqual(run["STATUS"], "Partial")
        self.assertIn("unfetchable", run["ERROR_SUMMARY"])
        # The failing row is marked Failed with an error message.
        bad = [kw for _, kw in sheets.queue_updates if kw["status"] == "Failed"]
        self.assertTrue(bad and bad[0]["error_message"])

    def test_run_is_logged_with_queue_type(self):
        q = [_qrow(2, "https://ig/reel/A/")]
        prov = FakeQueueProvider({"https://ig/reel/A/": _post("A", "https://ig/reel/A/")})
        sheets = FakeQueueSheets(q)
        scan.process_queue(sheets=sheets, provider=prov)
        self.assertEqual(len(sheets.runs), 1)
        self.assertEqual(sheets.runs[0]["RUN_TYPE"], "Queue")


class TestQueueContentCannotContaminateInternal(unittest.TestCase):
    """A row produced by the queue must never be usable as Storelli evidence."""

    def test_queue_row_excluded_from_buckets_and_correlations(self):
        prov = FakeQueueProvider({"https://ig/reel/A/": _post("A", "https://ig/reel/A/")})
        sheets = FakeQueueSheets([_qrow(2, "https://ig/reel/A/")])
        scan.process_queue(sheets=sheets, provider=prov)
        queue_row = dict(sheets.appended[0])
        # Simulate the worst case: someone hands this external row + a taxonomy
        # tag + a 'Great' performance to the internal learning functions.
        queue_row["_row"] = 999
        queue_row["PERFORMANCE"] = "Great"
        queue_row[A_SIGNAL_COL] = "1"

        internal = {"_row": 1, "PERFORMANCE": "Great", A_SIGNAL_COL: "1"}
        rows = [internal, queue_row]

        buckets = performance.buckets_for_rows(rows)
        self.assertIn(1, buckets)              # internal row bucketed
        self.assertNotIn(999, buckets)         # external queue row excluded

        analyzed = [r for r in rows
                    if SheetsClient.is_analyzed(r) and r["_row"] in buckets]
        self.assertNotIn(999, {r["_row"] for r in analyzed})

        # Correlations over the (correctly gated) analyzed set never see it.
        res = corr.compute(analyzed, buckets)
        self.assertEqual(res, corr.compute([internal], buckets))


if __name__ == "__main__":
    unittest.main()
