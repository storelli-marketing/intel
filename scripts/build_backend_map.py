"""Generate data/backend_map.json from the actual repo — source of truth for
the Dev Brain (src/dev_brain.py) so its file/route/command citations stay
accurate as the codebase changes, instead of drifting out of sync with a
hand-maintained doc.

What's auto-derived (read-only introspection, no execution of app code):
  - top-level function/class names per src/*.py file (via `ast`)
  - FastAPI routes in src/web.py (regex over @app.get/@app.post decorators)
  - CLI commands in src/main.py (the argparse `choices` list)
  - env var NAMES referenced via os.getenv(...) across src/*.py — never values

What's curated by hand below (policy, not derivable from syntax):
  - "layers" grouping of files into the architecture described in
    data/backend_context.md
  - "do_not_call_from_slack" — functions the Slack path must never invoke
    (video analysis, Sheet writes, Notion writes) — this is a safety
    contract Dev Brain answers must respect, not just a code fact.

Run manually after adding/removing files or routes:
    python scripts/build_backend_map.py
"""
from __future__ import annotations

import ast
import json
import os
import re

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SRC = os.path.join(_ROOT, "src")
_OUT = os.path.join(_ROOT, "data", "backend_map.json")

_ROUTE_RE = re.compile(r'@app\.(get|post)\("([^"]+)"')
_ENV_RE = re.compile(r'os\.getenv\("([A-Z][A-Z0-9_]*)"')

# Curated: functions the Slack path must never call, regardless of what
# social_brain.py/dev_brain.py happen to import — this is enforced by not
# calling them, and is checked for in code review, not by static analysis.
_DO_NOT_CALL_FROM_SLACK = [
    "analyzer.analyze_and_compile",
    "gemini_client.GeminiClient.analyze",
    "sheets_client.SheetsClient.write_row",
    "sheets_client.SheetsClient.set_status",
    "sheets_client.SheetsClient.reset_statuses",
    "notion_brain.NotionBrain.sync",
    "notion_brain.NotionBrain.sync_generated_ideas",
    "notion_brain.NotionBrain._upsert",
    "notion_brain.NotionBrain._ensure_db",
    "main.cmd_analyze",
    "main.cmd_analyze_all",
    "main.cmd_reset_incomplete",
]

# Curated: which files belong to which architectural layer (used to render
# the "5 layers" style summary Dev Brain answers with).
_LAYERS = [
    {
        "name": "Slack interface",
        "files": ["src/web.py", "src/slack_bot.py"],
        "role": "Receives Slack events (app_mention / message), verifies the "
                "request signature, ACKs immediately, and hands off to a "
                "background worker. Read-only: never writes to the Sheet or "
                "Notion, never triggers video analysis.",
    },
    {
        "name": "Strategy brain",
        "files": ["src/social_brain.py", "src/social_strategist.py",
                  "src/social_retrieval.py", "src/interpretation.py"],
        "role": "Routes a question to a topic, retrieves evidence via the "
                "layers below, and composes a strategist-voice answer "
                "(Gemini, validated) with a deterministic fallback.",
    },
    {
        "name": "Dev Brain",
        "files": ["src/dev_brain.py"],
        "role": "Backend self-awareness and build-request handoff — answers "
                "questions about this codebase and drafts (never applies) "
                "implementation plans, gated to approved Slack users.",
    },
    {
        "name": "Retrieval",
        "files": ["src/notion_retrieval.py", "src/content_context.py",
                  "src/sheets_client.py", "src/correlations.py",
                  "src/performance.py"],
        "role": "Notion Brain (primary), latest_learnings.md + Storelli "
                "brand context + guidelines, and a live Sheet + correlation "
                "computation as fallback.",
    },
    {
        "name": "Analysis pipeline",
        "files": ["src/main.py", "src/analyzer.py", "src/gemini_client.py",
                  "src/taxonomy.py", "src/synthesizer.py"],
        "role": "CLI-triggered only (never from Slack): downloads a reel, "
                "tags it via Gemini against the taxonomy, writes results "
                "back to the Sheet, and synthesizes learnings.",
    },
    {
        "name": "Publishing / sync",
        "files": ["src/notion_brain.py", "src/slack_report.py"],
        "role": "Pushes synthesized learnings into the 6 Notion Brain "
                "databases and posts an outbound Slack run summary. "
                "CLI/dashboard-triggered only, never from the Slack chat path.",
    },
]


def _functions_and_classes(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    funcs, classes = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(node.name)
    return {"functions": funcs, "classes": classes}


def _routes() -> list:
    web_path = os.path.join(_SRC, "web.py")
    with open(web_path, encoding="utf-8") as f:
        text = f.read()
    return [{"method": m.upper(), "path": p} for m, p in _ROUTE_RE.findall(text)]


def _cli_commands() -> list:
    main_path = os.path.join(_SRC, "main.py")
    with open(main_path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'"command",\s*\n\s*choices=\[(.*?)\]', text, re.DOTALL)
    if not m:
        return []
    return [c.strip().strip('"') for c in m.group(1).split(",") if c.strip()]


def _env_vars() -> list:
    names = set()
    for fn in sorted(os.listdir(_SRC)):
        if fn.endswith(".py"):
            with open(os.path.join(_SRC, fn), encoding="utf-8") as f:
                names.update(_ENV_RE.findall(f.read()))
    return sorted(names)


def build() -> dict:
    files = {}
    for fn in sorted(os.listdir(_SRC)):
        if fn.endswith(".py"):
            files[f"src/{fn}"] = _functions_and_classes(os.path.join(_SRC, fn))

    return {
        "generated_by": "scripts/build_backend_map.py",
        "note": "env_vars lists NAMES only, never values. Regenerate after "
                "adding/removing files, routes, or CLI commands.",
        "layers": _LAYERS,
        "files": files,
        "routes": _routes(),
        "cli_commands": _cli_commands(),
        "env_vars": _env_vars(),
        "do_not_call_from_slack": _DO_NOT_CALL_FROM_SLACK,
    }


if __name__ == "__main__":
    data = build()
    os.makedirs(os.path.dirname(_OUT), exist_ok=True)
    with open(_OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    print(f"Wrote {_OUT} ({len(data['files'])} files, {len(data['routes'])} routes, "
          f"{len(data['cli_commands'])} commands, {len(data['env_vars'])} env vars)")
