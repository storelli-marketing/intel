"""Global Slack CEO-conversation style layer.

Reusable helpers that make Marketing Brain's Slack answers read like a sharp,
friendly employee: concise (CEO-readable in 30-45s), 3-5 bullets, one clear
next move, sources at the bottom only when used. Presentation only — it never
changes scoring, retrieval, or any Sheet/Notion write.

Source labels: [S#] Storelli internal proof · [E#] external inspiration (never
proof) · [N#] Notion/calendar source. Sources block is always preserved intact.
"""
from __future__ import annotations

import re

MODE_CONCISE = "concise"
MODE_DEFAULT = "default"
MODE_DEEP = "deep"

# Word caps for the BODY (the Sources block is never counted or trimmed).
WORD_CAP = {MODE_CONCISE: 120, MODE_DEFAULT: 180, MODE_DEEP: 400}

_CONCISE_KW = ("be concise", "concise", "briefly", "brief", "quickly", "keep it short",
               "keep it brief", "tl;dr", "tldr", "one line", "in a sentence", "short answer",
               "short version", "just the highlights")
_DEEP_KW = ("go deep", "full report", "deep dive", "in depth", "in-depth", "detailed",
            "give me everything", "long version", "thorough")

# Canned/robotic endings to strip (rule: never end every answer with "want me to…").
_CANNED = re.compile(
    r"(?im)^\s*[>*_•\-\s]*(want me to|shall i|should i|would you like me to|"
    r"let me know if|do you want me to|i can also|happy to|need anything else|"
    r"anything else|want to dig|want a deeper)\b.*$")

_SOURCES_RE = re.compile(r"\n*[_*]*\s*Sources:?[_*]*\s*\n", re.IGNORECASE)


def detect_response_mode(message: str) -> str:
    t = (message or "").lower()
    if any(k in t for k in _DEEP_KW):
        return MODE_DEEP
    if any(k in t for k in _CONCISE_KW):
        return MODE_CONCISE
    return MODE_DEFAULT


def split_sources(text: str) -> tuple[str, str]:
    """Return (body, sources_block). sources_block is '' when none."""
    m = _SOURCES_RE.search(text or "")
    if not m:
        return (text or "").strip(), ""
    return text[:m.start()].strip(), text[m.start():].strip()


def _wc(s: str) -> int:
    return len(re.findall(r"\S+", s or ""))


def remove_canned_endings(text: str) -> str:
    body, src = split_sources(text)
    lines = body.splitlines()
    while lines and (not lines[-1].strip() or _CANNED.match(lines[-1])):
        lines.pop()
    body = "\n".join(lines).rstrip()
    return body + ("\n\n" + src if src else "")


def enforce_length(text: str, mode: str = MODE_DEFAULT) -> str:
    """Trim the BODY to the mode's word cap on whole-line boundaries; the Sources
    block is always kept in full."""
    cap = WORD_CAP.get(mode, WORD_CAP[MODE_DEFAULT])
    body, src = split_sources(text)
    if _wc(body) <= cap:
        return body + ("\n\n" + src if src else "")
    out, count = [], 0
    for ln in body.splitlines():
        w = _wc(ln)
        if out and count + w > cap:
            break
        out.append(ln)
        count += w
    trimmed = "\n".join(out).rstrip()
    # A single over-long line can't be trimmed on line boundaries — word-trim it.
    if _wc(trimmed) > cap:
        trimmed = " ".join(trimmed.split()[:cap]) + "…"
    return trimmed + ("\n\n" + src if src else "")


def simplify_bullets(bullets, max_n: int = 5) -> list:
    return [b for b in (bullets or []) if str(b).strip()][:max_n]


def compact_sources(items) -> str:
    """items: iterable of (tag, url, label) with tag in {S#, E#, N#}. Renders a
    Slack-clickable Sources block; '' when empty."""
    rows = [(t, u, l) for (t, u, l) in items if u]
    if not rows:
        return ""
    lines = ["*Sources:*"]
    for tag, url, label in rows:
        lines.append(f"  [{tag}] <{url}|{label}>")
    return "\n".join(lines)


def render_ceo_summary(lead: str, why=None, move: str = "", sources: str = "",
                       mode: str = MODE_DEFAULT) -> str:
    """Assemble the preferred CEO shape: lead → Why bullets → My move → Sources,
    then de-can and length-enforce."""
    parts = [str(lead).strip()]
    bullets = simplify_bullets(why, 5)
    if bullets:
        parts.append("*Why:*\n" + "\n".join(f"• {b}" for b in bullets))
    if str(move).strip():
        parts.append(f"*My move:* {str(move).strip()}")
    body = "\n\n".join(p for p in parts if p.strip())
    text = body + ("\n\n" + sources if sources else "")
    return enforce_length(remove_canned_endings(text), mode)


def compact_slack_response(text: str, mode: str = MODE_DEFAULT) -> str:
    """Generic post-processor for any Slack answer: strip canned endings and
    enforce the mode's length, always preserving the Sources block."""
    return enforce_length(remove_canned_endings(text or ""), mode)


_SRC_LINK_RE = re.compile(r"\[([A-Za-z]+\d+)\]\s*<([^|>]+)\|")


def _source_links(src_block: str) -> dict:
    """tag -> url parsed from a rendered Sources block (`[S1] <url|label>`)."""
    return {m.group(1): m.group(2) for m in _SRC_LINK_RE.finditer(src_block or "")}


def _strip_bullet(line: str) -> str:
    return re.sub(r"^\s*•\s?", "", line).lstrip()


def format_trace_answer(text: str) -> str:
    """Final formatting pass (runs AFTER length enforcement so it can't be
    trimmed): render the 'Why' evidence trail as a NUMBERED sequential workflow,
    and turn inline source letters like [S1] into clickable Slack links using the
    Sources block — links, not just letters. Idempotent; leaves the Sources block
    and already-numbered lists untouched.
    """
    body, src = split_sources(text)
    lines, out, n, in_why = body.splitlines(), [], 0, False
    for ln in lines:
        s = ln.lstrip()
        if s.startswith("*Why"):
            in_why, n = True, 0
            out.append(ln)
            continue
        if in_why:
            if s.startswith("•"):
                # Skip lists that already carry their own numbering (e.g. videos).
                if not re.match(r"\*?\d+\.", _strip_bullet(ln)):
                    n += 1
                    ln = re.sub(r"^(\s*)•\s?", rf"\g<1>{n}. ", ln)
            elif s.startswith("*"):
                in_why = False
        out.append(ln)
    body = "\n".join(out)
    links = _source_links(src)
    if links:
        body = re.sub(r"\[([A-Za-z]+\d+)\]",
                      lambda m: f"<{links[m.group(1)]}|{m.group(1)}>" if m.group(1) in links else m.group(0),
                      body)
    return body + ("\n\n" + src if src else "")
