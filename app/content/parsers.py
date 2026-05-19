"""Output parsers + length validators for Blog / IG / Twitter outputs.

These are forgiving: they extract what they can and report what's missing
via a parallel `validate_*` function.
"""
from __future__ import annotations

import re
from typing import Any


# ─── Blog ────────────────────────────────────────────────────────────────────

_H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
_META_RE = re.compile(r"\*Meta description[^:]*:\*\s*(.+?)$", re.IGNORECASE | re.MULTILINE)
_ALT_RE = re.compile(r"\*Suggested hero alt-text:\*\s*(.+?)$", re.IGNORECASE | re.MULTILINE)


def parse_blog(text: str) -> dict[str, Any]:
    """Split blog markdown into title/meta/alt/body."""
    text = text.strip()
    h1 = _H1_RE.search(text)
    title = h1.group(1).strip() if h1 else None

    meta = _META_RE.search(text)
    meta_description = meta.group(1).strip() if meta else None

    alt = _ALT_RE.search(text)
    alt_text = alt.group(1).strip() if alt else None

    return {
        "title": title,
        "meta_description": meta_description,
        "alt_text": alt_text,
        "body": text,
    }


def validate_blog(parsed: dict[str, Any]) -> tuple[bool, list[str]]:
    """Returns (ok, reasons)."""
    reasons: list[str] = []
    body = parsed.get("body") or ""
    word_count = len(re.findall(r"\b\w+\b", body))
    if word_count < 600:
        reasons.append(f"Blog too short: {word_count} words (need 800-1400)")
    elif word_count > 1700:
        reasons.append(f"Blog too long: {word_count} words (cap 1400)")
    if not parsed.get("title"):
        reasons.append("Missing H1 / title")
    if not parsed.get("meta_description"):
        reasons.append("Missing meta description line")
    return (not reasons, reasons)


# ─── Instagram ───────────────────────────────────────────────────────────────

_IG_LABEL_RE = re.compile(
    r"^(HOOK|BODY|CTA|HASHTAGS|MENTIONS)\s*:\s*",
    re.IGNORECASE | re.MULTILINE,
)


def parse_ig(text: str) -> dict[str, Any]:
    """Split IG output by HOOK:/BODY:/CTA:/HASHTAGS:/MENTIONS: labels."""
    text = text.strip()
    sections: dict[str, str] = {}
    matches = list(_IG_LABEL_RE.finditer(text))
    for i, m in enumerate(matches):
        label = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[label] = text[start:end].strip()

    hashtags_raw = sections.get("HASHTAGS", "")
    hashtags = re.findall(r"#[\w\-]+", hashtags_raw)

    mentions_raw = sections.get("MENTIONS", "")
    mentions: list[str] = []
    if mentions_raw and mentions_raw.lower().strip() != "none":
        mentions = re.findall(r"@[\w\.\-]+", mentions_raw)

    return {
        "hook": sections.get("HOOK", "").strip() or None,
        "body": sections.get("BODY", "").strip() or None,
        "cta": sections.get("CTA", "").strip() or None,
        "hashtags": hashtags,
        "mentions": mentions,
    }


def validate_ig(parsed: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    body = parsed.get("body") or ""
    hook = parsed.get("hook") or ""
    total_text = " ".join(x for x in (hook, body, parsed.get("cta") or "") if x)
    word_count = len(re.findall(r"\b\w+\b", total_text))
    if word_count < 50:
        reasons.append(f"IG caption too short: {word_count} words (need 80-220)")
    if word_count > 260:
        reasons.append(f"IG caption too long: {word_count} words (cap 220)")
    hook_words = len(hook.split())
    if hook_words > 14:
        reasons.append(f"IG hook too long: {hook_words} words (cap 12)")
    n_tags = len(parsed.get("hashtags") or [])
    if n_tags < 5 or n_tags > 10:
        reasons.append(f"IG hashtags out of range: {n_tags} (need 5-10)")
    if not hook:
        reasons.append("Missing HOOK")
    if not body:
        reasons.append("Missing BODY")
    return (not reasons, reasons)


# ─── Twitter ─────────────────────────────────────────────────────────────────

_TW_LABEL_RE = re.compile(
    r"^(REPLY|ALTERNATE)\s*:\s*",
    re.IGNORECASE | re.MULTILINE,
)


def parse_twitter(text: str) -> dict[str, Any]:
    """Split tweet output by REPLY:/ALTERNATE: labels."""
    text = text.strip()
    sections: dict[str, str] = {}
    matches = list(_TW_LABEL_RE.finditer(text))
    for i, m in enumerate(matches):
        label = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[label] = text[start:end].strip()

    reply = sections.get("REPLY", "").strip() or None
    alt = sections.get("ALTERNATE", "").strip() or None
    return {"reply": reply, "alternate": alt}


def validate_twitter(parsed: dict[str, Any], require_alternate: bool = False) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    reply = parsed.get("reply") or ""
    if not reply:
        reasons.append("Missing REPLY line")
    if len(reply) > 270:
        reasons.append(f"REPLY too long: {len(reply)} chars (cap 270)")
    if require_alternate and not parsed.get("alternate"):
        reasons.append("Crisis tone requires ALTERNATE line")
    return (not reasons, reasons)


# ─── Generic dispatcher ──────────────────────────────────────────────────────

def parse_output(content_type: str, text: str) -> dict[str, Any]:
    if content_type == "blog":
        return parse_blog(text)
    if content_type == "ig_post":
        return parse_ig(text)
    if content_type == "twitter_response":
        return parse_twitter(text)
    raise ValueError(f"Unknown content_type: {content_type}")


def validate_output(
    content_type: str,
    parsed: dict[str, Any],
    *,
    crisis: bool = False,
) -> tuple[bool, list[str]]:
    if content_type == "blog":
        return validate_blog(parsed)
    if content_type == "ig_post":
        return validate_ig(parsed)
    if content_type == "twitter_response":
        return validate_twitter(parsed, require_alternate=crisis)
    raise ValueError(f"Unknown content_type: {content_type}")
