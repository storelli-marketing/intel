"""Strategist benchmark runner (Phases 19–22).

Runs every case through the REAL Slack entry point (social_brain.answer_conversation)
against a seeded brain, and asserts the SEMANTIC properties of the answer.

Two gates:
  * CRITICAL accuracy/safety assertions must be 100% (no fabrication, no
    external-as-proof, no causal claims, no confidence dumps).
  * Overall pass rate must be >= OVERALL_THRESHOLD.

Run standalone for the evaluation report:
    python -m tests.strategist_benchmark.test_benchmark --report
"""
import os
import sys
import unittest

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, "..", "..", "src"))
sys.path.insert(0, os.path.join(_HERE, ".."))

import config
import gemini_client
import inspiration_sheets
import intelligence_refresh as ir
import social_brain
import sheets_client
import test_conversation_stateful as fixtures      # reuse the seeded brain
from strategist_benchmark import cases as C
from strategist_benchmark.internal_fixture import FakeSheetsClient

OVERALL_THRESHOLD = 0.90

_IDEA_CONTEXT_SEED = "what are the strongest ideas to shoot?"


def _boom(*a, **k):
    raise RuntimeError("Gemini disabled in benchmark (deterministic paths only)")


class BenchmarkBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._patched = []
        cls._real = inspiration_sheets.InspirationSheets
        for _n, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "InspirationSheets", None) is cls._real:
                setattr(mod, "InspirationSheets", fixtures.FakeSheets)
                cls._patched.append(mod)
        inspiration_sheets.InspirationSheets = fixtures.FakeSheets
        # Realistic ANALYZED internal dataset so the strategist has evidence to
        # reason over (otherwise we'd only be measuring an empty world).
        cls._real_sc = sheets_client.SheetsClient
        cls._patched_sc = []
        for _n, mod in list(sys.modules.items()):
            if mod is not None and getattr(mod, "SheetsClient", None) is cls._real_sc:
                setattr(mod, "SheetsClient", FakeSheetsClient)
                cls._patched_sc.append(mod)
        sheets_client.SheetsClient = FakeSheetsClient
        cls._gem = gemini_client.GeminiClient
        gemini_client.GeminiClient = _boom
        cls._key = config.GEMINI_API_KEY
        config.GEMINI_API_KEY = ""
        # a realistic recent refresh so self-update questions have real state
        cls._runs = ir.last_runs
        ir.last_runs = lambda sheets=None, n=1: [{
            "FINISHED_AT": "2026-08-19 09:00 UTC", "STATUS": "success",
            "INTERNAL_NEW_MEDIA": "6", "INTERNAL_ANALYZED": "6", "EXTERNAL_ADDED": "27",
            "EXTERNAL_QUALITY_80": "5", "PROFILES_UPDATED": "0",
            "IDEA_REGEN_RECOMMENDED": "False", "FAILED_COUNT": "0"}]

    @classmethod
    def tearDownClass(cls):
        for mod in cls._patched:
            setattr(mod, "InspirationSheets", cls._real)
        inspiration_sheets.InspirationSheets = cls._real
        gemini_client.GeminiClient = cls._gem
        for mod in cls._patched_sc:
            setattr(mod, "SheetsClient", cls._real_sc)
        sheets_client.SheetsClient = cls._real_sc
        config.GEMINI_API_KEY = cls._key
        ir.last_runs = cls._runs

    # ---- helpers ----
    def _seed_context(self, kind):
        if kind != "ideas":
            return []
        first = social_brain.answer_conversation(_IDEA_CONTEXT_SEED, [])
        return [{"role": "user", "text": _IDEA_CONTEXT_SEED},
                {"role": "assistant", "text": first}]

    def run_case(self, case) -> dict:
        ctx = self._seed_context(case.get("context"))
        answer = social_brain.answer_conversation(case["question"], ctx)
        failed_must = C.check(answer, case["must"])
        failed_not = C.check_absent(answer, case["must_not"])
        failed_critical = C.check_absent(answer, case["critical"])
        is_help = "Storelli Marketing Brain. Ask me" in answer
        if is_help and not case.get("allow_help"):
            failed_must.append("fell_back_to_help_menu")
        return {"question": case["question"], "answer": answer,
                "failed_must": failed_must, "failed_must_not": failed_not,
                "failed_critical": failed_critical,
                "passed": not (failed_must or failed_not or failed_critical),
                "critical_ok": not failed_critical}


class TestStrategistBenchmark(BenchmarkBase):
    def test_critical_accuracy_is_perfect(self):
        """No fabrication, no external-as-proof, no causal claims, no metadata dumps."""
        results = [self.run_case(c) for c in C.CASES]
        bad = [(r["question"], r["failed_critical"]) for r in results if not r["critical_ok"]]
        self.assertEqual(bad, [], f"critical accuracy failures: {bad}")

    def test_overall_pass_rate(self):
        results = [self.run_case(c) for c in C.CASES]
        passed = sum(1 for r in results if r["passed"])
        rate = passed / len(results)
        failures = [(r["question"], r["failed_must"] + r["failed_must_not"])
                    for r in results if not r["passed"]]
        self.assertGreaterEqual(
            rate, OVERALL_THRESHOLD,
            f"pass rate {rate:.0%} < {OVERALL_THRESHOLD:.0%}; failures: {failures}")

    def test_response_variation_not_templated(self):
        """Different intents must not produce the same scaffold every time."""
        qs = ["What should we shoot this week?", "What are we missing because Meta isn't connected?",
              "Should we regenerate ideas?", "What new things did the brain find?"]
        answers = [social_brain.answer_conversation(q, []) for q in qs]
        self.assertEqual(len(set(answers)), len(answers), "identical answers across intents")
        # not every answer may carry the same "*Why:*"/"My move" scaffolding
        scaffolded = sum(1 for a in answers if "*Why:*" in a and "*My move:*" in a)
        self.assertLess(scaffolded, len(answers), "every answer used the same template")


class TestMultiTurnBenchmark(BenchmarkBase):
    def test_conversations(self):
        problems = []
        for convo in C.CONVERSATIONS:
            ctx = []
            for turn in convo["turns"]:
                answer = social_brain.answer_conversation(turn["say"], ctx)
                for name in C.check(answer, turn.get("must", [])):
                    problems.append(f"{convo['name']}::{turn['say']}::missing::{name}")
                for name in C.check_absent(answer, turn.get("must_not", [])):
                    problems.append(f"{convo['name']}::{turn['say']}::unexpected::{name}")
                for name in C.check_absent(answer, C._SAFETY):
                    problems.append(f"{convo['name']}::{turn['say']}::CRITICAL::{name}")
                if "Storelli Marketing Brain. Ask me" in answer:
                    problems.append(f"{convo['name']}::{turn['say']}::help_fallback")
                ctx.append({"role": "user", "text": turn["say"]})
                ctx.append({"role": "assistant", "text": answer})
        self.assertEqual(problems, [], f"multi-turn failures: {problems}")


def _report():
    """Print the evaluation report (question, answer, failures) — answer-quality
    audit only, no chain-of-thought."""
    BenchmarkBase.setUpClass()
    runner = TestStrategistBenchmark("test_overall_pass_rate")
    try:
        rows = [runner.run_case(c) for c in C.CASES]
    finally:
        BenchmarkBase.tearDownClass()
    passed = sum(1 for r in rows if r["passed"])
    crit = sum(1 for r in rows if r["critical_ok"])
    print(f"\nSTRATEGIST BENCHMARK — {len(rows)} cases")
    print(f"overall pass: {passed}/{len(rows)} ({passed / len(rows):.0%})")
    print(f"critical accuracy: {crit}/{len(rows)} ({crit / len(rows):.0%})\n")
    for r in rows:
        flag = "PASS" if r["passed"] else ("CRIT-FAIL" if not r["critical_ok"] else "FAIL")
        print(f"[{flag}] {r['question']}")
        if not r["passed"]:
            print(f"        missing={r['failed_must']} unexpected="
                  f"{r['failed_must_not']} critical={r['failed_critical']}")
            print(f"        answer: {r['answer'][:160].replace(chr(10), ' ')}")
    return 0 if passed / len(rows) >= OVERALL_THRESHOLD and crit == len(rows) else 1


if __name__ == "__main__":
    if "--report" in sys.argv:
        sys.exit(_report())
    unittest.main()
