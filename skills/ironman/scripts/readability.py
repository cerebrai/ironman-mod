#!/usr/bin/env python3
"""
ironman readability analyzer.

Deterministically scores a markdown reply for how easy it is to read for a
non-expert: reading grade, undefined jargon (rare words and acronyms that are
neither in the personal glossary, nor in the user's prompt, nor defined on
first use), and a few soft structure hints.

Run with:  uv run --quiet --with textstat --with wordfreq readability.py [opts] < reply.md
Both optional deps degrade gracefully: without textstat a hand-rolled
Flesch-Kincaid is used; without wordfreq the bundled common_words.txt is used.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = Path.home() / ".claude" / "ironman"
DEFAULT_CONFIG = {
    "max_fk_grade": 9.0,
    "zipf_rare_cutoff": 3.0,
    "soft_word_guide": 200,
    "soft_avg_sentence_guide": 20,
    "min_prose_words_to_check": 25,
    "skip_if_code_ratio_above": 0.6,
    "max_retries_per_turn": 2,
    "default_mode": "discuss",
}

# ----------------------------------------------------------------- optional deps
try:  # pragma: no cover - depends on environment
    import textstat as _textstat
except Exception:  # noqa: BLE001
    _textstat = None

try:  # pragma: no cover
    from wordfreq import zipf_frequency as _zipf
except Exception:  # noqa: BLE001
    _zipf = None

_COMMON_WORDS: set[str] | None = None


def _common_words() -> set[str]:
    global _COMMON_WORDS
    if _COMMON_WORDS is None:
        p = SKILL_DIR / "scripts" / "common_words.txt"
        _COMMON_WORDS = set(p.read_text().split()) if p.exists() else set()
    return _COMMON_WORDS


def _variants(w: str) -> list[str]:
    """Crude inflection stripping so 'misfires' is judged by 'misfire' too."""
    out = [w]
    for suf, rep in (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ed", "e"), ("ing", ""), ("ing", "e"), ("ly", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            out.append(w[: -len(suf)] + rep)
    return out


def word_is_rare(word: str, cutoff: float) -> bool:
    """True if `word` (and its base forms) is uncommon in everyday English."""
    w = word.lower()
    if _zipf is not None:
        return max(_zipf(v, "en") for v in _variants(w)) < cutoff
    common = _common_words()
    return bool(common) and not any(v in common for v in _variants(w))


# ----------------------------------------------------------------- loading
def load_config(path: Path | None = None) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    p = path or SKILL_DIR / "config.json"
    if p.exists():
        try:
            cfg.update(json.loads(p.read_text()))
        except json.JSONDecodeError:
            pass
    return cfg


def load_known_terms(path: Path | None = None) -> set[str]:
    p = path or SKILL_DIR / "known_terms.txt"
    terms: set[str] = set()
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.add(line.lower())
    return terms


# ----------------------------------------------------------------- preprocessing
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s+\S", re.MULTILINE)
BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S", re.MULTILINE)
QUOTE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
SOURCES_RE = re.compile(r"\n\s*\**sources:?\**\s*\n.*\Z", re.IGNORECASE | re.DOTALL)
# `*`/`**` anywhere; `_`/`__` only at word edges so snake_case survives
EMPHASIS_RE = re.compile(r"\*{1,2}|(?<!\w)_{1,2}(?=\S)|(?<=\S)_{1,2}(?!\w)")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)


def preprocess(text: str) -> dict:
    """Strip the parts that must be preserved verbatim and never scored."""
    total_chars = max(len(text), 1)
    code_chars = sum(len(m.group(0)) for m in FENCE_RE.finditer(text))
    no_fence = FENCE_RE.sub(" ", text)
    inline_codes = [m.group(0) for m in INLINE_CODE_RE.finditer(no_fence)]
    code_chars += sum(len(c) for c in inline_codes)
    s = INLINE_CODE_RE.sub(" ", no_fence)
    s = SOURCES_RE.sub("\n", s)
    s = QUOTE_RE.sub(" ", s)
    s = TABLE_ROW_RE.sub(" ", s)
    s = MD_LINK_RE.sub(r"\1", s)
    s = URL_RE.sub(" ", s)
    headers = len(HEADER_RE.findall(s))
    bullets = len(BULLET_RE.findall(s))
    s = HEADER_RE.sub(lambda m: m.group(0).lstrip(" #"), s)
    s = BULLET_RE.sub(lambda m: re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", m.group(0)), s)
    s = EMPHASIS_RE.sub("", s).replace("**", "")
    return {
        "prose": s,
        "code_ratio": code_chars / total_chars,
        "headers": headers,
        "bullets": bullets,
        "inline_code": inline_codes,
    }


# ----------------------------------------------------------------- tokenizing
SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n{2,}")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’\-]*[A-Za-z]|[A-Za-z]")
ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,}\+?\b")
SNAKE_RE = re.compile(r"\b[a-z0-9]+_[a-z0-9_]+\b")
CAMEL_RE = re.compile(r"\b[a-z]+[A-Z][A-Za-z]+\b")
DOTTED_RE = re.compile(r"\b[a-zA-Z_]\w*\.[a-zA-Z_]\w*(?:\(\))?\b")

DEFINED_AFTER_RE = re.compile(
    r"^\s*(?:"
    r"\(|"  # X (Event Filtering Service)
    r",\s*(?:which|i\.e\.|that is|or|meaning|short for)\b|"
    r"[—–-]{1,2}\s|"  # X — the thing that ...
    r":\s|"
    r"\b(?:means|stands for|is short for|refers to)\b|"
    r"\bis\s+(?:a|an|the|our|your|my|basically|just|what)\b|"
    r"\bare\s+(?:the|our|your)\b"
    r")"
)
DEFINED_BEFORE_RE = re.compile(
    r"(?:\bcalled|\bknown as|\bnamed|\bor\b|\bi\.e\.|\bthat is,)\s*(?:the\s+|a\s+|an\s+)?$",
    re.IGNORECASE,
)

HANDBACK_RE = re.compile(
    r"\?\s*$|"
    r"\b(?:want me to|shall i|should i|does that|make sense|makes sense|let me know|tell me|"
    r"stop me|say the word|your call|which (?:one|way)|ready for|go on\??|next\??|sound (?:right|good|ok))\b",
    re.IGNORECASE,
)

STOPWORD_ACRONYMS = {"OK", "AM", "PM", "US", "UK", "EU", "TV", "PC", "ID", "IT", "A", "I"}


def split_sentences(prose: str) -> list[str]:
    parts = [p.strip() for p in SENT_SPLIT_RE.split(prose)]
    return [p for p in parts if p and WORD_RE.search(p)]


def _syllables(word: str) -> int:
    w = word.lower()
    w = re.sub(r"[^a-z]", "", w)
    if not w:
        return 0
    if len(w) <= 3:
        return 1
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    return max(1, len(re.findall(r"[aeiouy]{1,2}", w)))


def fk_grade(prose: str, words: list[str], sentences: list[str]) -> float:
    if not words or not sentences:
        return 0.0
    if _textstat is not None:
        try:
            return round(float(_textstat.flesch_kincaid_grade(prose)), 2)
        except Exception:  # noqa: BLE001
            pass
    syl = sum(_syllables(w) for w in words)
    return round(0.39 * len(words) / len(sentences) + 11.8 * syl / len(words) - 15.59, 2)


# ----------------------------------------------------------------- term checks
def _is_defined(sentence: str, term_match: re.Match) -> bool:
    after = sentence[term_match.end():]
    before = sentence[: term_match.start()]
    return bool(DEFINED_AFTER_RE.match(after)) or bool(DEFINED_BEFORE_RE.search(before))


def find_undefined_terms(
    sentences: list[str],
    known: set[str],
    prompt_terms: set[str],
    cutoff: float,
) -> tuple[list[str], list[str]]:
    """Return (undefined_acronyms, undefined_rare_words), first-occurrence order."""
    seen: set[str] = set()
    acr_out: list[str] = []
    rare_out: list[str] = []
    for sent in sentences:
        for m in ACRONYM_RE.finditer(sent):
            tok = m.group(0)
            key = tok.lower()
            if key in seen or key in known or key in prompt_terms or tok in STOPWORD_ACRONYMS:
                continue
            seen.add(key)
            if not _is_defined(sent, m):
                acr_out.append(tok)
        for m in WORD_RE.finditer(sent):
            tok = m.group(0)
            if tok.isupper() or len(tok) < 4 or "'" in tok or "’" in tok:
                continue
            key = tok.lower()
            if key in seen or key in known or key in prompt_terms:
                continue
            # hyphenated: judge by each part
            parts = key.split("-")
            if not all(word_is_rare(p, cutoff) for p in parts if p):
                seen.add(key)
                continue
            seen.add(key)
            if not _is_defined(sent, m):
                rare_out.append(tok)
    return acr_out, rare_out


def find_bare_identifiers(prose: str) -> list[str]:
    found: list[str] = []
    for rx in (SNAKE_RE, CAMEL_RE, DOTTED_RE):
        for m in rx.finditer(prose):
            t = m.group(0)
            if t not in found and not re.fullmatch(r"\d+(\.\d+)+", t) and t.lower() not in {"e.g", "i.e", "etc"}:
                found.append(t)
    return found


def prompt_term_set(user_prompt: str | None) -> set[str]:
    if not user_prompt:
        return set()
    toks = {m.group(0).lower() for m in WORD_RE.finditer(user_prompt)}
    toks |= {m.group(0).lower() for m in ACRONYM_RE.finditer(user_prompt)}
    toks |= {m.group(0).lower() for m in SNAKE_RE.finditer(user_prompt)}
    return toks


# ----------------------------------------------------------------- main analysis
def analyze(
    text: str,
    user_prompt: str | None = None,
    mode: str | None = None,
    config: dict | None = None,
    known: set[str] | None = None,
) -> dict:
    cfg = config or load_config()
    known = known if known is not None else load_known_terms()
    pre = preprocess(text)
    prose = pre["prose"]
    sentences = split_sentences(prose)
    words = WORD_RE.findall(prose)
    n_words = len(words)
    n_sents = max(len(sentences), 1 if n_words else 0)
    avg_len = round(n_words / n_sents, 1) if n_sents else 0.0
    grade = fk_grade(prose, words, sentences)
    prompt_terms = prompt_term_set(user_prompt)
    acronyms, rare = find_undefined_terms(sentences, known, prompt_terms, cfg["zipf_rare_cutoff"])
    identifiers = find_bare_identifiers(prose)
    last_line = next((ln.strip() for ln in reversed(prose.splitlines()) if ln.strip()), "")
    handback = bool(HANDBACK_RE.search(last_line))
    paragraphs = len([p for p in re.split(r"\n\s*\n", prose) if p.strip()])

    skipped = None
    if pre["code_ratio"] > cfg["skip_if_code_ratio_above"]:
        skipped = f"mostly code ({pre['code_ratio']:.0%} of characters)"
    elif n_words < cfg["min_prose_words_to_check"]:
        skipped = f"only {n_words} prose words"

    blocking: list[str] = []
    advice: list[str] = []
    if not skipped:
        undefined = acronyms + rare
        if undefined:
            blocking.append(
                "Define on first use or replace with plain words: " + ", ".join(undefined) + "."
            )
        if grade > cfg["max_fk_grade"]:
            blocking.append(
                f"Reading grade {grade}; bring it to <= {cfg['max_fk_grade']:g} with shorter sentences and simpler words."
            )
        if n_words > cfg["soft_word_guide"]:
            advice.append(f"{n_words} words; keep only if this point really needs them.")
        if avg_len > cfg["soft_avg_sentence_guide"]:
            advice.append(f"Average sentence {avg_len} words; aim for about {cfg['soft_avg_sentence_guide']}.")
        if pre["headers"]:
            advice.append("Headers read like a document; talk instead.")
        if not handback:
            advice.append("End by handing the turn back (a question or a check-in).")
        if identifiers:
            advice.append("Put identifiers in backticks: " + ", ".join(identifiers[:6]) + ".")

    reason = " ".join(blocking)
    if advice:
        reason += (" " if reason else "") + "(Advice: " + " ".join(advice) + ")"
    reason = reason.strip()

    return {
        "mode": mode or cfg.get("default_mode"),
        "skipped": skipped,
        "words": n_words,
        "sentences": len(sentences),
        "avg_sentence_len": avg_len,
        "fk_grade": grade,
        "paragraphs": paragraphs,
        "bullets": pre["bullets"],
        "headers": pre["headers"],
        "code_ratio": round(pre["code_ratio"], 3),
        "undefined_acronyms": acronyms,
        "undefined_rare_words": rare,
        "undefined_terms": acronyms + rare,
        "bare_identifiers": identifiers,
        "ends_with_handback": handback,
        "blocking": blocking,
        "advice": advice,
        "pass": not blocking,
        "reason": reason,
        "engine": {
            "grade": "textstat" if _textstat else "builtin",
            "rarity": "wordfreq" if _zipf else ("common_words" if _common_words() else "acronyms-only"),
        },
    }


# ----------------------------------------------------------------- report
def report(log_path: Path | None = None, session: str | None = None) -> str:
    p = log_path or STATE_DIR / "log.jsonl"
    if not p.exists():
        return "No log yet."
    rows, errors = [], 0
    for line in p.read_text().splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "error" in r:  # hook crash rows, not replies
            errors += 1
            continue
        if session and r.get("session") != session:
            continue
        rows.append(r)
    scope = f"session {session}" if session else "all sessions"
    if not rows:
        return f"No replies logged for {scope}."
    checked = [r for r in rows if not r.get("skipped")]
    grades = [r["fk_grade"] for r in checked if "fk_grade" in r]
    words = [r["words"] for r in checked if "words" in r]
    failed = [r for r in checked if r.get("pass") is False]
    from collections import Counter

    terms = Counter(t for r in checked for t in r.get("undefined_terms", []))
    out = [
        f"scope: {scope}",
        f"replies logged: {len(rows)}  (checked: {len(checked)}, skipped: {len(rows) - len(checked)})",
        f"failed the check: {len(failed)}  ({len(failed) / max(len(checked), 1):.0%} of checked)",
    ]
    # self_checked only exists on rows logged since the draft check was added
    tracked = [r for r in checked if "self_checked" in r]
    if tracked:
        self_checked = [r for r in tracked if r["self_checked"]]
        out.append(f"self-checked before sending: {len(self_checked)} of {len(tracked)}  "
                   f"({len(self_checked) / len(tracked):.0%})")
        out.append(f"  skipped the check and failed: {sum(1 for r in tracked if not r['self_checked'] and r.get('pass') is False)}")
        out.append(f"  checked but still failed after rewrites: {sum(1 for r in self_checked if r.get('draft_passed') is False)}")
    if grades:
        out.append(f"grade  median {statistics.median(grades):.1f}  p90 {sorted(grades)[int(0.9 * (len(grades) - 1))]:.1f}")
    if words:
        out.append(f"words  median {statistics.median(words):.0f}  p90 {sorted(words)[int(0.9 * (len(words) - 1))]}")
    if terms:
        out.append("most-flagged terms: " + ", ".join(f"{t} x{n}" for t, n in terms.most_common(12)))
    if errors:
        out.append(f"hook errors in log (all sessions): {errors}")
    return "\n".join(out)


# ----------------------------------------------------------------- CLI
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Score a reply for readability and undefined jargon.")
    ap.add_argument("--file", help="markdown file to score (default: stdin)")
    ap.add_argument("--user-prompt", help="the user's prompt; its terms count as known")
    ap.add_argument("--user-prompt-file")
    ap.add_argument("--mode", choices=["discuss", "teach", "code"])
    ap.add_argument("--config", type=Path)
    ap.add_argument("--known", type=Path, help="alternate known_terms.txt")
    ap.add_argument("--json", action="store_true", help="print full JSON instead of a summary")
    ap.add_argument("--report", action="store_true", help="summarise ~/.claude/ironman/log.jsonl and exit")
    ap.add_argument("--session", help="with --report, only rows from this session id")
    args = ap.parse_args(argv)

    if args.report:
        print(report(session=args.session))
        return 0

    text = Path(args.file).read_text() if args.file else sys.stdin.read()
    up = args.user_prompt
    if args.user_prompt_file:
        up = Path(args.user_prompt_file).read_text()
    res = analyze(
        text,
        user_prompt=up,
        mode=args.mode,
        config=load_config(args.config) if args.config else None,
        known=load_known_terms(args.known) if args.known else None,
    )
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        verdict = "SKIPPED (" + res["skipped"] + ")" if res["skipped"] else ("PASS" if res["pass"] else "FAIL")
        print(
            f"{verdict}  words={res['words']} sentences={res['sentences']} avg_len={res['avg_sentence_len']} "
            f"grade={res['fk_grade']} handback={res['ends_with_handback']}"
        )
        if res["undefined_terms"]:
            print("undefined terms:", ", ".join(res["undefined_terms"]))
        if res["reason"]:
            print(res["reason"])
    return 0 if res["pass"] or res["skipped"] else 1


if __name__ == "__main__":
    sys.exit(main())
