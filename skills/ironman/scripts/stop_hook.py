#!/usr/bin/env python3
"""
ironman Stop hook.

Reads the Claude Code Stop-hook JSON on stdin. Does nothing unless
~/.claude/ironman/sessions/<session_id> exists, so ironman is scoped to the
session that turned it on. When active, scores the last assistant
message with readability.py; if hard checks fail, asks the model to rewrite
by returning {"decision": "block", "reason": ...}. Capped retries per turn;
with max_retries_per_turn=0 it only logs. Also logs whether the reply was
self-checked by check_draft.py first.
Every verdict is appended to ~/.claude/ironman/log.jsonl.

Never raises: any internal error results in a silent exit 0 so a bug here
can never trap a session.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readability  # noqa: E402

STATE_DIR = readability.STATE_DIR
SESSIONS = STATE_DIR / "sessions"
RETRIES = STATE_DIR / "retries"
LOG = STATE_DIR / "log.jsonl"


def read_active(session_id: str) -> dict | None:
    marker = SESSIONS / session_id
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text() or "{}")
    except json.JSONDecodeError:
        data = {"mode": marker.read_text().strip() or None}
    return data if isinstance(data, dict) else {}


SYSTEM_TAG_RE = re.compile(r"<(system-reminder|local-command-[a-z]+|command-[a-z]+)>.*?</\1>", re.S)


def _user_text(d: dict) -> str | None:
    """Text the user actually typed, or None for tool results, skill bodies and reminders."""
    if d.get("isMeta"):
        return None
    content = (d.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
    else:
        return None
    text = SYSTEM_TAG_RE.sub("", text).strip()
    return text or None


def last_messages_from_transcript(
    path: str | None, wait: float = 1.0, n_user: int = 1
) -> tuple[str | None, str | None]:
    """Return (last_assistant_text, last n_user user prompts joined) from the transcript JSONL."""
    if not path:
        return None, None
    p = Path(path)
    if wait:
        time.sleep(wait)  # the final assistant message can lag the Stop event
    if not p.exists():
        return None, None
    assistant: str | None = None
    users: list[str] = []
    for line in reversed(p.read_text(errors="replace").splitlines()):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = d.get("type")
        content = (d.get("message") or {}).get("content")
        if typ == "assistant" and assistant is None and isinstance(content, list):
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                assistant = "\n".join(texts)
        elif typ == "user" and len(users) < n_user:
            text = _user_text(d)
            if text:
                users.append(text)
        if assistant is not None and len(users) >= n_user:
            break
    return assistant, ("\n".join(reversed(users)) or None)


def user_prompt_only(path: str | None, n_user: int = 1) -> str | None:
    _, user = last_messages_from_transcript(path, wait=0, n_user=n_user)
    return user


def retry_count(session_id: str) -> int:
    f = RETRIES / session_id
    try:
        return int(f.read_text().strip() or 0)
    except (FileNotFoundError, ValueError):
        return 0


def set_retry_count(session_id: str, n: int) -> None:
    RETRIES.mkdir(parents=True, exist_ok=True)
    (RETRIES / session_id).write_text(str(n))


def log(row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0

    session = str(payload.get("session_id") or "")
    active = read_active(session) if session else None
    if active is None:
        return 0
    transcript = payload.get("transcript_path")
    cfg = readability.load_config()
    n_user = int(cfg.get("recent_user_prompts", 5))
    text = payload.get("last_assistant_message")
    user_prompt = None
    if text:
        user_prompt = user_prompt_only(transcript, n_user)
    else:
        text, user_prompt = last_messages_from_transcript(transcript, n_user=n_user)
    if not text:
        return 0

    mode = active.get("mode") or cfg.get("default_mode")
    res = readability.analyze(text, user_prompt=user_prompt, mode=mode, config=cfg)

    n_prev = retry_count(session)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "session": session,
        "mode": mode,
        "retry": n_prev,
        **{k: res[k] for k in ("skipped", "words", "sentences", "avg_sentence_len", "fk_grade",
                                "undefined_terms", "ends_with_handback", "blocking", "advice", "pass")},
    }
    # Did the model run check_draft.py on exactly this reply before sending it?
    from check_draft import fingerprint, load_drafts
    fp = fingerprint(text)
    match = next((r for r in load_drafts(session) if r.get("fp") == fp), None)
    row["self_checked"] = match is not None
    row["draft_passed"] = match.get("pass") if match else None

    if res["skipped"] or res["pass"]:
        set_retry_count(session, 0)
        row["decision"] = "allow"
        log(row)
        return 0

    cap = int(cfg.get("max_retries_per_turn", 2))
    if n_prev >= cap:
        set_retry_count(session, 0)
        row["decision"] = "log-only" if cap == 0 else "allow-retry-cap"
        log(row)
        return 0

    set_retry_count(session, n_prev + 1)
    row["decision"] = "block"
    log(row)
    reason = (
        "ironman check failed. Rewrite your last message for a non-expert reader: "
        + res["reason"]
        + " Keep every fact and all code, commands, paths and numbers verbatim. "
        f"See {Path(__file__).resolve().parent.parent / 'references' / 'voice.md'} if unsure."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - never trap the session
        try:
            log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "error": repr(exc)})
        except Exception:  # noqa: BLE001
            pass
        sys.exit(0)
