#!/usr/bin/env python3
"""
ironman draft check, run by the model before it sends a reply.

Reads the draft reply on stdin, scores it with readability.py using the same
config as the Stop hook, and prints a verdict. The user's recent prompts are
pulled from this session's transcript (found via $CLAUDE_CODE_SESSION_ID) so
terms the user used lately count as known. Each checked draft's fingerprint is saved
to ~/.claude/ironman/drafts/<session_id>.json so the Stop hook can log whether
the final reply was self-checked.

Exit 0 on PASS/SKIPPED, 1 on FAIL.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readability  # noqa: E402

STATE_DIR = readability.STATE_DIR
SESSIONS = STATE_DIR / "sessions"
DRAFTS = STATE_DIR / "drafts"
KEEP_DRAFTS = 10


FENCE_RE = re.compile(r"^[ \t]*(```|~~~)[^\n]*$", re.M)


def fingerprint(text: str) -> str:
    """Hash that ignores whitespace and code-fence lines, so a draft sent to the
    checker without its ``` fences still matches the reply that has them."""
    norm = re.sub(r"\s+", " ", FENCE_RE.sub("", text)).strip()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def load_drafts(session_id: str) -> list[dict]:
    try:
        data = json.loads((DRAFTS / f"{session_id}.json").read_text())
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_draft(session_id: str, fp: str, passed: bool) -> None:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    rows = [r for r in load_drafts(session_id) if r.get("fp") != fp]
    rows.append({"fp": fp, "pass": passed, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    (DRAFTS / f"{session_id}.json").write_text(json.dumps(rows[-KEEP_DRAFTS:]))


def find_transcript(session_id: str) -> str | None:
    hits = sorted((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return str(hits[0]) if hits else None


def session_mode(session_id: str) -> str | None:
    try:
        data = json.loads((SESSIONS / session_id).read_text() or "{}")
        return data.get("mode") if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def main() -> int:
    from stop_hook import last_messages_from_transcript  # shared transcript parser

    text = sys.stdin.read()
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    cfg = readability.load_config()
    user_prompt = None
    if session:
        _, user_prompt = last_messages_from_transcript(
            find_transcript(session), wait=0, n_user=int(cfg.get("recent_user_prompts", 5))
        )

    mode = session_mode(session) or cfg.get("default_mode")
    res = readability.analyze(text, user_prompt=user_prompt, mode=mode, config=cfg)
    ok = bool(res["pass"] or res["skipped"])
    if session:
        save_draft(session, fingerprint(text), ok)

    verdict = "SKIPPED (" + res["skipped"] + ")" if res["skipped"] else ("PASS" if res["pass"] else "FAIL")
    print(f"{verdict}  grade={res['fk_grade']} words={res['words']}")
    if res["undefined_terms"]:
        print("undefined terms:", ", ".join(res["undefined_terms"]))
    if not ok and res["reason"]:
        print(res["reason"])
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
