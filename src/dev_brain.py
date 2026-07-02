"""Dev Brain — backend self-awareness + Slack-to-code handoff.

Answers questions about THIS APP's own architecture (`answer_backend_question`)
and, only for explicitly-approved Slack users, drafts a structured build
request (`create_build_request`) that a human pastes into Claude Code — it
never writes code, never executes anything, and by default
(`BUILD_REQUEST_TARGET=slack_only`) never touches GitHub or Notion either.

Grounded in two files, never live code introspection at answer time:
  - data/backend_context.md  (curated: what things mean, safety rules)
  - data/backend_map.json    (generated: files/routes/commands/env-var-NAMES —
                               see scripts/build_backend_map.py)

Every LLM-composed answer is validated before being shown: file citations
must exist in the backend map (no invented files), and the text must not
contain anything secret-shaped. Any failure falls back to a deterministic
answer built directly from the same two files.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from logger import get_logger

log = get_logger()

_DATA = os.path.join(os.path.dirname(__file__), "..", "data")
_BACKEND_CONTEXT_PATH = os.path.join(_DATA, "backend_context.md")
_BACKEND_MAP_PATH = os.path.join(_DATA, "backend_map.json")

_EXTRA_CITEABLE = {"README.md", "DEPLOY.md", "data/backend_context.md", "data/backend_map.json"}

_SECRET_LOOKING_RE = re.compile(r"AIzaSy[\w-]{10,}|xox[bp]-[\w-]+|sk-[\w-]{10,}|ntn_[\w]{10,}")


# --- loaders ------------------------------------------------------------------
def _load_backend_context() -> str:
    try:
        with open(_BACKEND_CONTEXT_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _load_backend_map() -> dict:
    try:
        with open(_BACKEND_MAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("dev_brain: backend_map.json unavailable (%s)", e)
        return {}


def _known_files(map_data: dict) -> set:
    return set(map_data.get("files", {}).keys()) | _EXTRA_CITEABLE


def _render_map_summary(map_data: dict) -> str:
    if not map_data:
        return "(backend map unavailable)"
    lines = []
    for layer in map_data.get("layers", []):
        files = ", ".join(layer.get("files", []))
        lines.append(f"- {layer.get('name', '?')}: {files} — {layer.get('role', '')}")
    routes = ", ".join(f"{r['method']} {r['path']}" for r in map_data.get("routes", []))
    lines.append(f"\nRoutes: {routes or '(none)'}")
    lines.append("CLI commands: " + ", ".join(map_data.get("cli_commands", [])))
    lines.append("Env vars (names only): " + ", ".join(map_data.get("env_vars", [])))
    lines.append("Never call from Slack: " + ", ".join(map_data.get("do_not_call_from_slack", [])))
    return "\n".join(lines)


# --- dev-question routing ------------------------------------------------------
_DEV_PHRASES = (
    "how are you structured", "how is your backend", "backend structured",
    "your backend", "how would you build", "how would you add",
    "how would you implement", "what files", "what would break",
    "safest implementation", "push to code", "create build request",
    "tell claude code", "build request", "implementation plan",
    "how are you built", "under the hood",
)
_DEV_WORD_KW = ("backend", "repo", "repository", "codebase", "architecture", "slack bot")
_WHERE_IS_RE = re.compile(r"\bwhere\s+is\b.{0,40}\b(implement|handl|locat|defin)", re.IGNORECASE)


def is_dev_question(text: str) -> bool:
    """True if a message looks like a backend/build question rather than a
    marketing question — used by web.py to route to Dev Brain instead of the
    marketing strategist. Deliberately conservative about short/ambiguous
    tokens: "BE" only triggers when capitalized (an initialism for "backend"),
    never as a substring match of lowercase "be" in ordinary sentences."""
    raw = text or ""
    t = raw.lower()
    if re.search(r"\bBE\b", raw):
        return True
    if any(p in t for p in _DEV_PHRASES):
        return True
    if _WHERE_IS_RE.search(t):
        return True
    return any(re.search(rf"\b{re.escape(w)}\b", t) for w in _DEV_WORD_KW)


_PUSH_TO_CODE_KW = ("push to code", "create build request", "tell claude code", "build request")


def wants_build_request(text: str) -> bool:
    t = (text or "").lower()
    return any(k in t for k in _PUSH_TO_CODE_KW)


# --- backend Q&A ----------------------------------------------------------------
_BACKEND_PROMPT = """\
You are explaining YOUR OWN backend architecture to the person who built \
you — a developer question, not a marketing one. Be concise and accurate, \
using ONLY the context below. Never invent a file, function, route, or env \
var that isn't listed. Never state or guess an env var's VALUE — names \
only, never values. Never suggest running analyze/analyze-all from Slack, \
writing to the Sheet from Slack, or writing to Notion from Slack — these \
are hard safety rules of this system, not implementation choices, and Dev \
Brain must never soften or contradict them.

Backend context:
{backend_context}

Backend map (cite files EXACTLY as listed, e.g. [src/web.py]):
{map_summary}

Recent thread context:
{thread_summary}

User's message: {question}

Answer format — pick whichever fits:

If describing the current architecture:
My read: [one sentence]
1. [Layer] — [files, cited like [src/x.py]] — [role]
2. ...
(up to 5 layers)

If the user is asking HOW TO BUILD or ADD something new:
My read: [one sentence on the core idea]
Where I'd change it: [files, cited like [src/x.py]]
Risk: [one honest risk or tradeoff]
Next build request: [one sentence — what "push to code" would produce]

Rules:
- Cite files in square brackets exactly as they appear in the backend map \
above — never cite a file not listed there.
- No huge file dumps, no raw code, no JSON/database dumps — this is a \
conversation, not documentation.
- Under ~1200 characters.
- If the backend map/context doesn't cover what's asked, say so plainly \
instead of guessing.

Write only the reply text, no commentary about these instructions."""


_FILE_PATTERN = r"(?:src|data|scripts)/[\w.\-]+\.\w+|README\.md|DEPLOY\.md"


def _extract_file_citations(text: str) -> list:
    """Find cited file paths regardless of which markup the model used to set
    them off — the prompt asks for [brackets], but validating only that style
    would let a hallucinated file slip through un-checked if the model used
    `backticks` instead, which happens in practice."""
    bracketed = re.findall(rf"\[({_FILE_PATTERN})\]", text)
    backticked = re.findall(rf"`({_FILE_PATTERN})`", text)
    return bracketed + backticked


def _citations_valid(text: str, known_files: set) -> bool:
    return all(c in known_files for c in _extract_file_citations(text))


def _looks_like_secret(text: str) -> bool:
    return bool(_SECRET_LOOKING_RE.search(text))


def _deterministic_backend_answer(map_data: dict) -> str:
    layers = map_data.get("layers", [])
    lines = ["My read: this bot is built from a few clear layers — a read-only "
             "Slack interface, a strategy/evidence layer, Dev Brain, and a "
             "separate CLI-only analysis pipeline."]
    for i, layer in enumerate(layers, 1):
        files = ", ".join(f"[{f}]" for f in layer.get("files", []))
        lines.append(f"{i}. {layer.get('name', '?')} — {files} — {layer.get('role', '')}")
    lines.append("\nThe Slack path never writes to the Sheet or Notion and never "
                 "triggers video analysis. Ask something more specific and I can "
                 "point to exact files.")
    return "\n".join(lines)


def answer_backend_question(user_text: str, conversation_context: list | None = None) -> str:
    """Answer a backend/architecture question. Uses Gemini (validated) when
    Dev Brain mode + Gemini are both configured; otherwise (or on any
    validation failure) falls back to a deterministic answer built directly
    from data/backend_context.md + data/backend_map.json."""
    import config

    context = conversation_context or []
    map_data = _load_backend_map()

    if not (config.SLACK_DEV_MODE_ENABLED and config.GEMINI_API_KEY):
        return _deterministic_backend_answer(map_data)

    backend_context = _load_backend_context()
    known_files = _known_files(map_data)
    thread_summary = "\n".join(
        f"{m.get('role', 'user')}: {str(m.get('text', ''))[:300]}" for m in context[-6:]
    ) or "(none)"

    prompt = _BACKEND_PROMPT.format(
        backend_context=backend_context or "(none)",
        map_summary=_render_map_summary(map_data),
        thread_summary=thread_summary, question=user_text,
    )

    try:
        from gemini_client import GeminiClient
        answer = GeminiClient().summarize_findings(prompt).strip()
    except Exception as e:  # noqa: BLE001 - Dev Brain synthesis is optional, never fatal
        log.warning("dev_brain: backend answer synthesis failed (%s); using deterministic fallback.", e)
        return _deterministic_backend_answer(map_data)

    if not answer:
        return _deterministic_backend_answer(map_data)
    answer = re.sub(r"\*\*(.+?)\*\*", r"*\1*", answer)

    if not _citations_valid(answer, known_files):
        log.warning("dev_brain: backend answer cited an unknown file; using deterministic fallback.")
        return _deterministic_backend_answer(map_data)
    if _looks_like_secret(answer):
        log.warning("dev_brain: backend answer looked secret-shaped; using deterministic fallback.")
        return _deterministic_backend_answer(map_data)

    return answer


# --- build-request handoff ------------------------------------------------------
_UNAUTHORIZED_MSG = ("I can explain the backend, but I'm not allowed to create "
                     "build requests from your account.")


def _is_authorized(user_id: str) -> bool:
    import config
    return bool(user_id) and user_id in config.SLACK_DEV_ALLOWED_USER_IDS


@dataclass
class BuildRequest:
    title: str
    user_goal: str
    system_context: str
    proposed_implementation: str
    files_likely_to_change: list = field(default_factory=list)
    safety_constraints: list = field(default_factory=list)
    tests_checks: list = field(default_factory=list)
    deployment_notes: str = ""
    claude_code_prompt: str = ""

    def render_slack(self) -> str:
        files = "\n".join(f"- {f}" for f in self.files_likely_to_change) or "- (tbd)"
        safety = "\n".join(f"- {s}" for s in self.safety_constraints) or \
            "- (standard: no Sheet/Notion writes from Slack, no analyze-all, no secrets)"
        tests = "\n".join(f"- {t}" for t in self.tests_checks) or "- python -m py_compile src/*.py"
        return (
            "*Build request prepared. Paste this into Claude Code:*\n\n"
            f"*Title:* {self.title}\n\n"
            f"*User goal:* {self.user_goal}\n\n"
            f"*Current system context:* {self.system_context}\n\n"
            f"*Proposed implementation:* {self.proposed_implementation}\n\n"
            f"*Files likely to change:*\n{files}\n\n"
            f"*Safety constraints:*\n{safety}\n\n"
            f"*Tests/checks:*\n{tests}\n\n"
            f"*Deployment notes:* {self.deployment_notes or 'Small, reviewed PR — no direct merge to main.'}\n\n"
            f"*Exact Claude Code prompt:*\n```{self.claude_code_prompt}```"
        )

    def render_github_issue_body(self) -> str:
        files = "\n".join(f"- {f}" for f in self.files_likely_to_change) or "- (tbd)"
        safety = "\n".join(f"- {s}" for s in self.safety_constraints) or "- (standard Slack read-only rules)"
        tests = "\n".join(f"- {t}" for t in self.tests_checks) or "- python -m py_compile src/*.py"
        return (
            f"## User goal\n{self.user_goal}\n\n"
            f"## Current system context\n{self.system_context}\n\n"
            f"## Proposed implementation\n{self.proposed_implementation}\n\n"
            f"## Files likely to change\n{files}\n\n"
            f"## Safety constraints\n{safety}\n\n"
            f"## Tests/checks\n{tests}\n\n"
            f"## Deployment notes\n{self.deployment_notes}\n\n"
            f"## Suggested Claude Code prompt\n```\n{self.claude_code_prompt}\n```\n"
        )


_BUILD_REQUEST_PROMPT = """\
You are drafting a BUILD REQUEST for a human developer to hand to Claude \
Code. You are NOT writing code, NOT executing anything, and this draft is \
NEVER applied automatically. Base it only on the backend context/map below \
and the conversation. Never invent a file that isn't in the backend map \
file list. Always include, among the safety constraints, that the Slack \
path must stay read-only (no Sheet writes, no Notion writes, no video \
analysis, no analyze-all), that Instagram cookies are out of scope, and \
that no secrets may be printed or committed.

Backend context:
{backend_context}

Backend map (valid file paths — only use these in files_likely_to_change):
{map_summary}

Conversation so far (the feature/change being discussed):
{thread_summary}

User's final message: {question}

Return ONLY a JSON object, no markdown fences, no commentary, with exactly \
these keys:
{{
  "title": "short imperative title",
  "user_goal": "one sentence: what the user actually wants",
  "system_context": "1-2 sentences: relevant current architecture",
  "proposed_implementation": "2-4 sentences: the approach",
  "files_likely_to_change": ["src/x.py", "src/y.py"],
  "safety_constraints": ["constraint 1", "constraint 2"],
  "tests_checks": ["check 1", "check 2"],
  "deployment_notes": "1-2 sentences",
  "claude_code_prompt": "a complete, self-contained prompt a developer could paste into Claude Code to implement this, including explicit guardrails"
}}"""


def _deterministic_build_request(user_text: str, context: list) -> BuildRequest:
    goal = user_text or "(see conversation)"
    prior_user_msgs = "\n".join(m.get("text", "") for m in context if m.get("role") == "user")
    thread_summary = prior_user_msgs[-500:] if prior_user_msgs else "(none)"
    return BuildRequest(
        title="Build request: " + (goal[:60] if goal else "Slack-requested change"),
        user_goal=goal,
        system_context="See data/backend_context.md and data/backend_map.json for the current architecture.",
        proposed_implementation="Not auto-drafted (Gemini unavailable) — implement based on the "
                                "conversation below, following this repo's existing patterns.",
        files_likely_to_change=[],
        safety_constraints=[
            "No Sheet writes from Slack", "No Notion writes from Slack",
            "No video analysis triggered from Slack", "No analyze-all",
            "Instagram cookies out of scope", "No secrets printed or committed",
        ],
        tests_checks=["python -m py_compile src/*.py", "web.py imports cleanly"],
        deployment_notes="Small, reviewed PR — no direct merge to main.",
        claude_code_prompt=(
            f"Implement the following, requested via Slack:\n\n{goal}\n\n"
            f"Conversation context:\n{thread_summary}\n\n"
            "Guardrails: keep the Slack path read-only (no Sheet/Notion writes, "
            "no video analysis, no analyze-all), don't touch Instagram cookies, "
            "don't print or commit secrets, run `python -m py_compile src/*.py` "
            "before committing, keep the change small."
        ),
    )


def create_build_request(user_text: str, conversation_context: list | None = None,
                         requesting_user_id: str = "") -> BuildRequest | None:
    """Returns None when the requesting Slack user isn't in
    config.SLACK_DEV_ALLOWED_USER_IDS — caller shows the unauthorized message.
    Never writes anywhere itself; see deliver_build_request for the (also
    gated, also optional) GitHub handoff."""
    import config

    if not _is_authorized(requesting_user_id):
        return None

    context = conversation_context or []
    map_data = _load_backend_map()

    if not (config.SLACK_DEV_MODE_ENABLED and config.GEMINI_API_KEY):
        return _deterministic_build_request(user_text, context)

    backend_context = _load_backend_context()
    known_files = _known_files(map_data)
    thread_summary = "\n".join(
        f"{m.get('role', 'user')}: {str(m.get('text', ''))[:400]}" for m in context[-8:]
    ) or "(none)"

    prompt = _BUILD_REQUEST_PROMPT.format(
        backend_context=backend_context or "(none)",
        map_summary=_render_map_summary(map_data),
        thread_summary=thread_summary, question=user_text,
    )

    try:
        from gemini_client import GeminiClient
        from analyzer import parse_model_json
        raw = GeminiClient().summarize_findings(prompt)
        if _looks_like_secret(raw):
            log.warning("dev_brain: build request draft looked secret-shaped; using deterministic template.")
            return _deterministic_build_request(user_text, context)
        data = parse_model_json(raw)
        files = [f for f in data.get("files_likely_to_change", []) or [] if f in known_files]
        return BuildRequest(
            title=str(data.get("title") or "Build request")[:120],
            user_goal=str(data.get("user_goal") or user_text),
            system_context=str(data.get("system_context") or ""),
            proposed_implementation=str(data.get("proposed_implementation") or ""),
            files_likely_to_change=files,
            safety_constraints=[str(s) for s in (data.get("safety_constraints") or [])][:8],
            tests_checks=[str(t) for t in (data.get("tests_checks") or [])][:8],
            deployment_notes=str(data.get("deployment_notes") or ""),
            claude_code_prompt=str(data.get("claude_code_prompt") or ""),
        )
    except Exception as e:  # noqa: BLE001 - build-request synthesis is optional, never fatal
        log.warning("dev_brain: build request synthesis failed (%s); using deterministic template.", e)
        return _deterministic_build_request(user_text, context)


def _file_github_issue(br: BuildRequest) -> str | None:
    """Best-effort: create a GitHub issue (never a PR, never a commit).
    Returns the issue URL, or None on any failure/missing config."""
    import config
    if not (config.GITHUB_TOKEN and config.GITHUB_REPO):
        return None
    try:
        import httpx
        resp = httpx.post(
            f"https://api.github.com/repos/{config.GITHUB_REPO}/issues",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json"},
            json={"title": br.title, "body": br.render_github_issue_body()},
            timeout=15,
        )
        if resp.status_code >= 300:
            log.warning("dev_brain: GitHub issue creation failed: %s %s", resp.status_code, resp.text[:200])
            return None
        return resp.json().get("html_url")
    except Exception as e:  # noqa: BLE001 - GitHub handoff is optional, never fatal
        log.warning("dev_brain: GitHub issue creation failed: %s", e)
        return None


def _fire_github_dispatch(br: BuildRequest) -> bool:
    """Best-effort: trigger a repository_dispatch event. This only notifies a
    workflow (if one exists) — it never commits or opens a PR itself. Returns
    True on a successful (2xx) dispatch."""
    import config
    if not (config.GITHUB_TOKEN and config.GITHUB_REPO):
        return False
    try:
        import httpx
        resp = httpx.post(
            f"https://api.github.com/repos/{config.GITHUB_REPO}/dispatches",
            headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json"},
            json={"event_type": config.GITHUB_DISPATCH_EVENT,
                  "client_payload": {"title": br.title, "user_goal": br.user_goal,
                                     "claude_code_prompt": br.claude_code_prompt}},
            timeout=15,
        )
        return resp.status_code < 300
    except Exception as e:  # noqa: BLE001 - GitHub handoff is optional, never fatal
        log.warning("dev_brain: repository_dispatch failed: %s", e)
        return False


def deliver_build_request(br: BuildRequest) -> str:
    """Slack-ready text for the build request, optionally also filing to
    GitHub per config.BUILD_REQUEST_TARGET (default slack_only = no external
    write at all). A GitHub failure is noted but never blocks the Slack reply."""
    import config
    text = br.render_slack()
    target = config.BUILD_REQUEST_TARGET
    if target == "github_issue":
        url = _file_github_issue(br)
        text += (f"\n\n_Also filed as a GitHub issue: {url}_" if url else
                "\n\n_GitHub issue filing was configured but failed — see logs._")
    elif target == "github_dispatch":
        ok = _fire_github_dispatch(br)
        text += ("\n\n_Also triggered a repository_dispatch event for a review workflow "
                "(never commits to main directly)._" if ok else
                "\n\n_repository_dispatch was configured but failed — see logs._")
    return text


# --- public: top-level entrypoint -----------------------------------------------
def handle(user_text: str, conversation_context: list | None = None,
          requesting_user_id: str = "") -> str:
    """Top-level Dev Brain entrypoint used by web.py. Backend Q&A is
    read-only and open to any Slack user; the build-request handoff is
    gated to config.SLACK_DEV_ALLOWED_USER_IDS (empty by default = no one
    authorized)."""
    context = conversation_context or []
    if wants_build_request(user_text):
        br = create_build_request(user_text, context, requesting_user_id)
        if br is None:
            return _UNAUTHORIZED_MSG
        return deliver_build_request(br)
    return answer_backend_question(user_text, context)
