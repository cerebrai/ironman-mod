#!/usr/bin/env python3
"""Tests for readability.py and stop_hook.py. Run: uv run --with textstat --with wordfreq test_readability.py"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import readability as R  # noqa: E402

CFG = dict(R.DEFAULT_CONFIG)
KNOWN = {"cdn", "ttl", "api"}

PLAIN = (
    "Users get the old file because the copies kept near them last an hour. "
    "That is longer than the time between releases, so nothing asks for the new file. "
    "So the release is live but people still see last week's page. "
    "Want me to show where that hour is set?"
)
JARGON = (
    "The orchestrator enforces idempotent semantics so backpressure propagates deterministically. "
    "Hysteresis thresholds in the heuristic preclude reconciliation across heterogeneous clusters, "
    "which is why the DLQ is drained before the CDN ever sees it."
)


def run(text, **kw):
    return R.analyze(text, config=CFG, known=KNOWN, **kw)


class Preprocess(unittest.TestCase):
    def test_code_fences_and_inline_code_excluded(self):
        text = PLAIN + "\n\n```python\nfoo_bar = orchestrate(idempotent=True)\n```\nUse `hysteresis_thr` here."
        res = run(text)
        self.assertEqual(res["undefined_terms"], [])
        self.assertTrue(res["pass"])

    def test_urls_quotes_sources_tables_excluded(self):
        text = PLAIN + "\n> orchestrator idempotent quoted\n| hysteresis | x |\n|---|---|\nSee https://example.com/backpressure\n\nSources:\n- https://a.b/idempotent\n"
        res = run(text)
        self.assertEqual(res["undefined_terms"], [])

    def test_snake_case_survives_emphasis_stripping(self):
        pre = R.preprocess("The **bold** and _soft_ words, plus cache_ttl_s and my_var_name.")
        self.assertIn("cache_ttl_s", pre["prose"])
        self.assertIn("my_var_name", pre["prose"])
        self.assertNotIn("**", pre["prose"])
        self.assertNotIn("_soft_", pre["prose"])

    def test_headers_and_bullets_counted(self):
        pre = R.preprocess("# Title\n\n- one\n- two\n1. three\n")
        self.assertEqual(pre["headers"], 1)
        self.assertEqual(pre["bullets"], 3)


class Terms(unittest.TestCase):
    def test_jargon_flagged(self):
        res = run(JARGON)
        self.assertFalse(res["pass"])
        for t in ("orchestrator", "idempotent", "backpressure", "DLQ"):
            self.assertIn(t, res["undefined_terms"], t)
        self.assertNotIn("CDN", res["undefined_terms"])  # known

    def test_inline_definition_patterns_accepted(self):
        for s in (
            "The DLQ (the queue where failed messages wait) holds them.",
            "The DLQ, which is the queue where failed messages wait, holds them.",
            "The DLQ — the queue where failed messages wait — holds them.",
            "The DLQ is the queue where failed messages wait.",
            "The DLQ means the queue where failed messages wait.",
            "DLQ stands for dead letter queue.",
            "Failed messages go to a holding queue called a DLQ.",
            "Failed messages go to a holding queue, or DLQ, until someone looks.",
        ):
            res = run(s + " " + PLAIN)
            self.assertNotIn("DLQ", res["undefined_terms"], s)

    def test_defined_once_then_reused_is_fine(self):
        res = run("The DLQ (the failed-message queue) is small. The DLQ also has a size limit. " + PLAIN)
        self.assertNotIn("DLQ", res["undefined_terms"])

    def test_user_prompt_terms_are_known(self):
        res = run("Zorbulation happens when the DLQ is late. " + PLAIN, user_prompt="why does zorbulation of the DLQ happen?")
        self.assertNotIn("Zorbulation", res["undefined_terms"])
        self.assertNotIn("DLQ", res["undefined_terms"])

    def test_known_terms_file_respected(self):
        res = R.analyze("The LRU cache runs on the CDN node. " + PLAIN, config=CFG, known={"lru", "cdn"})
        self.assertEqual(res["undefined_terms"], [])

    def test_inflections_use_base_form(self):
        # 'timers' should be judged by 'timer' (common), not flagged
        res = run("The timers and filters and frames all agree. " + PLAIN)
        self.assertEqual(res["undefined_rare_words"], [])

    def test_common_acronyms_ignored(self):
        res = run("OK, at 5 PM in the US it works. " + PLAIN)
        self.assertEqual(res["undefined_acronyms"], [])

    def test_bare_identifiers_advice(self):
        res = run("Set rolling_stop_max_speed and call getStopSign() then read cfg.value. " + PLAIN)
        self.assertTrue(any("backticks" in a for a in res["advice"]))
        self.assertIn("rolling_stop_max_speed", res["bare_identifiers"])


class Grade(unittest.TestCase):
    def test_plain_passes_and_hands_back(self):
        res = run(PLAIN)
        self.assertTrue(res["pass"])
        self.assertLessEqual(res["fk_grade"], 9)
        self.assertTrue(res["ends_with_handback"])

    def test_high_grade_blocks(self):
        long_words = (
            "Notwithstanding the aforementioned considerations regarding organizational infrastructure, "
            "the implementation of comprehensive methodological frameworks necessitates substantial "
            "administrative coordination throughout the institutional hierarchy and its representatives."
        )
        res = run(long_words + " " + long_words)
        self.assertTrue(any("Reading grade" in b for b in res["blocking"]))

    def test_builtin_fk_close_to_textstat(self):
        words = R.WORD_RE.findall(PLAIN)
        sents = R.split_sentences(PLAIN)
        with mock.patch.object(R, "_textstat", None):
            builtin = R.fk_grade(PLAIN, words, sents)
        self.assertTrue(0 <= builtin <= 9, builtin)


class SoftRules(unittest.TestCase):
    def test_length_never_blocks(self):
        text = (PLAIN + " ") * 8 + "Want me to go on?"
        res = run(text)
        self.assertTrue(res["pass"])
        self.assertTrue(any("words" in a for a in res["advice"]))

    def test_mostly_code_skipped(self):
        code = "```\n" + "\n".join(f"x{i} = orchestrate(idempotent=True)" for i in range(30)) + "\n```"
        res = run("Here is the block.\n" + code)
        self.assertIsNotNone(res["skipped"])
        self.assertEqual(res["blocking"], [])

    def test_short_reply_skipped(self):
        res = run("Idempotent orchestrator. Sure?")
        self.assertIsNotNone(res["skipped"])
        self.assertEqual(res["blocking"], [])

    def test_no_handback_is_advice_not_block(self):
        res = run(PLAIN.rsplit(" Want me", 1)[0] + ".")
        self.assertTrue(res["pass"])
        self.assertTrue(any("handing the turn back" in a for a in res["advice"]))


class Hook(unittest.TestCase):
    """Runs stop_hook.py as a subprocess with HOME pointed at a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.state = self.home / ".claude" / "ironman"
        self.env = {**os.environ, "HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, payload):
        p = subprocess.run(
            [sys.executable, str(HERE / "stop_hook.py")],
            input=json.dumps(payload), capture_output=True, text=True, env=self.env, timeout=60,
        )
        return p.returncode, p.stdout.strip()

    def activate(self, session, mode="discuss"):
        (self.state / "sessions").mkdir(parents=True, exist_ok=True)
        (self.state / "sessions" / session).write_text(json.dumps({"mode": mode}))

    def log_rows(self):
        return [json.loads(l) for l in (self.state / "log.jsonl").read_text().splitlines()]

    def test_inactive_is_silent(self):
        code, out = self.call({"session_id": "s1", "last_assistant_message": JARGON})
        self.assertEqual((code, out), (0, ""))
        self.assertFalse((self.state / "log.jsonl").exists())

    def test_other_session_not_affected(self):
        self.activate("on-session")
        code, out = self.call({"session_id": "other-session", "last_assistant_message": JARGON})
        self.assertEqual((code, out), (0, ""))
        self.assertFalse((self.state / "log.jsonl").exists())

    def test_active_fail_is_logged_not_blocked(self):
        # max_retries_per_turn = 0: the hook scores and logs but never asks for a rewrite
        self.activate("s2")
        code, out = self.call({"session_id": "s2", "last_assistant_message": JARGON})
        self.assertEqual((code, out), (0, ""))
        row = self.log_rows()[-1]
        self.assertEqual(row["decision"], "log-only")
        self.assertIn("idempotent", row["undefined_terms"])

    def test_self_checked_draft_is_recorded(self):
        self.activate("s5")
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": "s5"}
        p = subprocess.run([sys.executable, str(HERE / "check_draft.py")], input=PLAIN,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        # whitespace differences still count as the same draft
        self.call({"session_id": "s5", "last_assistant_message": "  " + PLAIN.replace(" ", "  ")})
        self.call({"session_id": "s5", "last_assistant_message": PLAIN + " Extra sentence added after the check."})
        checked, unchecked = self.log_rows()[-2:]
        self.assertEqual((checked["self_checked"], checked["draft_passed"]), (True, True))
        self.assertEqual((unchecked["self_checked"], unchecked["draft_passed"]), (False, None))

    def test_fingerprint_ignores_code_fences(self):
        sys.path.insert(0, str(HERE))
        from check_draft import fingerprint
        body = "Run this:\nls -la\nThat lists files."
        fenced = "Run this:\n```bash\nls -la\n```\nThat lists files."
        self.assertEqual(fingerprint(body), fingerprint(fenced))
        self.assertNotEqual(fingerprint(body), fingerprint(body + " More."))

    def test_check_draft_fails_jargon(self):
        self.activate("s6")
        env = {**self.env, "CLAUDE_CODE_SESSION_ID": "s6"}
        p = subprocess.run([sys.executable, str(HERE / "check_draft.py")], input=JARGON,
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(p.returncode, 1)
        self.assertTrue(p.stdout.startswith("FAIL"))
        self.assertIn("idempotent", p.stdout)

    def test_active_allows_plain_and_resets_counter(self):
        self.activate("s3")
        self.call({"session_id": "s3", "last_assistant_message": JARGON})
        code, out = self.call({"session_id": "s3", "last_assistant_message": PLAIN})
        self.assertEqual((code, out), (0, ""))
        self.assertEqual((self.state / "retries" / "s3").read_text(), "0")

    def test_transcript_fallback_and_prompt_terms(self):
        self.activate("s4")
        tp = self.home / "t.jsonl"
        rows = [
            {"type": "user", "message": {"content": "why does zorbulation of the DLQ happen?"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "Zorbulation of the DLQ is rare. " + PLAIN}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x"}]}},
        ]
        tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        code, out = self.call({"session_id": "s4", "transcript_path": str(tp)})
        self.assertEqual((code, out), (0, ""))
        log = [json.loads(l) for l in (self.state / "log.jsonl").read_text().splitlines()]
        self.assertEqual(log[-1]["undefined_terms"], [])

    def test_recent_prompts_count_as_known_and_meta_is_ignored(self):
        self.activate("s8")
        tp = self.home / "t8.jsonl"
        rows = [
            {"type": "user", "message": {"content": "how does zorbulation work?"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}},
            {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "skill body mentions quuxification"}]}},
            {"type": "user", "message": {"content": "<system-reminder>blorptastic</system-reminder>and then?"}},
        ]
        tp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        reply = "Zorbulation, quuxification and blorptastic things happen here. " + PLAIN
        self.call({"session_id": "s8", "transcript_path": str(tp), "last_assistant_message": reply})
        terms = self.log_rows()[-1]["undefined_terms"]
        self.assertNotIn("zorbulation", terms)  # from an earlier user prompt
        self.assertIn("quuxification", terms)  # only in a meta (skill) message
        self.assertIn("blorptastic", terms)  # only in a system reminder

    def test_garbage_stdin_is_silent(self):
        self.activate("s7")
        p = subprocess.run([sys.executable, str(HERE / "stop_hook.py")], input="not json", capture_output=True, text=True, env=self.env)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ""))


class PromptHook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = {**os.environ, "HOME": str(self.home)}

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, payload):
        p = subprocess.run([sys.executable, str(HERE / "prompt_hook.py")], input=json.dumps(payload),
                           capture_output=True, text=True, env=self.env, timeout=30)
        return p.returncode, p.stdout.strip()

    def test_reminder_only_for_marked_session(self):
        d = self.home / ".claude" / "ironman" / "sessions"
        d.mkdir(parents=True)
        (d / "on").write_text(json.dumps({"mode": "teach"}))
        code, out = self.call({"session_id": "on", "prompt": "hi"})
        self.assertEqual(code, 0)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("check_draft.py", ctx)
        self.assertIn("teach", ctx)
        self.assertEqual(self.call({"session_id": "off", "prompt": "hi"}), (0, ""))
        self.assertEqual(self.call({"session_id": "on", "prompt": "/ironman off"}), (0, ""))


class CLI(unittest.TestCase):
    def test_cli_json_and_exit_codes(self):
        with mock.patch("sys.stdin", io.StringIO(PLAIN)), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = R.main(["--json"])
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["pass"])
        with mock.patch("sys.stdin", io.StringIO(JARGON)), mock.patch("sys.stdout", new_callable=io.StringIO):
            rc = R.main([])
        self.assertEqual(rc, 1)

    def test_report_session_filter_and_self_check(self):
        rows = [
            {"session": "a", "skipped": None, "pass": True, "fk_grade": 5, "words": 40, "self_checked": True, "draft_passed": True},
            {"session": "a", "skipped": None, "pass": False, "fk_grade": 12, "words": 60, "undefined_terms": ["zorb"], "self_checked": False, "draft_passed": None},
            {"session": "a", "skipped": None, "pass": False, "fk_grade": 11, "words": 50, "self_checked": True, "draft_passed": False},
            {"session": "b", "skipped": None, "pass": True, "fk_grade": 4, "words": 30, "self_checked": True, "draft_passed": True},
            {"error": "boom"},
        ]
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "log.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
            out_a = R.report(log, session="a")
            out_all = R.report(log)
            out_none = R.report(log, session="zzz")
        self.assertIn("replies logged: 3", out_a)
        self.assertIn("failed the check: 2", out_a)
        self.assertIn("self-checked before sending: 2 of 3", out_a)
        self.assertIn("skipped the check and failed: 1", out_a)
        self.assertIn("checked but still failed after rewrites: 1", out_a)
        self.assertIn("hook errors in log (all sessions): 1", out_a)
        self.assertIn("replies logged: 4", out_all)
        self.assertEqual(out_none, "No replies logged for session zzz.")

    def test_report_on_missing_log(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(R.report(Path(d) / "nope.jsonl"), "No log yet.")


if __name__ == "__main__":
    unittest.main(verbosity=1)
