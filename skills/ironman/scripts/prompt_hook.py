#!/usr/bin/env python3
"""
ironman UserPromptSubmit hook.

Runs each time the user sends a message. If this session has an ironman
marker (~/.claude/ironman/sessions/<session_id>), adds a short reminder to the
turn's context so the model runs check_draft.py before replying. The skill
body is loaded once and fades over a long conversation; this keeps the rule
fresh every turn. Sessions without a marker get nothing.

Standard library only, so it can run with plain python3 and add no latency.
Never raises: any error results in a silent exit 0.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SESSIONS = Path.home() / ".claude" / "ironman" / "sessions"
CHECK_DRAFT = Path(__file__).resolve().parent / "check_draft.py"
OFF_RE = re.compile(r"(^|\s)/?ironman\s+off\b", re.I)

REMINDER = (
    "ironman is on in this session (mode: {mode}). Before sending your reply, run the draft "
    "check from the ironman skill: pipe the full draft into "
    f"`uv run --quiet --with textstat --with wordfreq {CHECK_DRAFT}` "
    "via a quoted heredoc (include code fences as-is), fix what it flags on FAIL (max 2 rewrites), then send exactly the text "
    "that was last checked. Skip only for replies under 25 prose words or mostly code."
)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    session = str(payload.get("session_id") or "")
    marker = SESSIONS / session
    if not session or not marker.exists():
        return 0
    if OFF_RE.search(str(payload.get("prompt") or "")):
        return 0  # the user is turning it off this turn
    try:
        data = json.loads(marker.read_text() or "{}")
        mode = data.get("mode") if isinstance(data, dict) else None
    except json.JSONDecodeError:
        mode = None
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": REMINDER.format(mode=mode or "discuss"),
        }
    }))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - never trap the session
        sys.exit(0)
