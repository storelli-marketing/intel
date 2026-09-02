"""Weekly digest — what the refresh actually changed, pushed where people read.

`intelligence_refresh.render_report()` already summarises a run, but it is
CLI-shaped: one line per stage, every stage, including the ones that did
nothing. Nobody reads that in Slack on a Monday.

This renders the same report as a short update a person would actually skim:
what moved, what it means for what to shoot, and what needs a human. Stages that
did nothing are collapsed into one line rather than listed; a stage that FAILED
is always named, because a quiet failure every week is how a pipeline dies
unnoticed.

Same discipline as the rest of the brain: no invented numbers (every figure comes
from the run's own history row), internal Storelli evidence and external
inspiration kept separate, and external counts never described as proof.

Delivery is best-effort and never affects the run: Slack via the existing
SLACK_WEBHOOK_URL, and email via plain SMTP when — and only when — SMTP settings
are configured. A missing destination is a no-op, not an error.
"""
from __future__ import annotations

import config
from logger import get_logger

log = get_logger()

# Stages whose numbers are worth a line of their own, in reading order.
_INTERNAL = ("owned_scan", "internal_append", "internal_analyze",
             "internal_metrics", "internal_maturity", "internal_recompute")
_EXTERNAL = ("external_discovery", "external_analyze", "external_match",
             "external_quality", "external_connections")


def _by_stage(report: dict) -> dict:
    return {s.get("stage"): s for s in (report.get("stages") or [])}


def _n(stage: dict, *keys) -> int:
    for k in keys:
        try:
            v = int(stage.get(k) or 0)
        except (TypeError, ValueError):
            v = 0
        if v:
            return v
    return 0


def build_digest(report: dict, health: dict | None = None) -> str:
    """A short, readable weekly update. Pure — safe to unit test offline."""
    if not report:
        return ""
    by = _by_stage(report)
    status = str(report.get("status", "")).lower()

    if report.get("locked_out"):
        return ("*Storelli brain — weekly refresh skipped*\n"
                f"{report.get('reason', 'another refresh was already running')}")

    head = {"success": "*Storelli brain — weekly refresh done*",
            "partial": "*Storelli brain — weekly refresh done, with gaps*",
            "failed": "*Storelli brain — weekly refresh failed*"}.get(
        status, "*Storelli brain — weekly refresh*")
    lines = [head]

    # --- our own feed (this is the evidence base) --------------------------
    new_media = _n(by.get("owned_scan", {}), "_new_media", "created")
    analyzed = _n(by.get("internal_analyze", {}), "created")
    metrics = (_n(by.get("owned_scan", {}), "updated")
               + _n(by.get("internal_metrics", {}), "updated"))
    labelled = _n(by.get("internal_maturity", {}), "_classified", "updated")
    held = _n(by.get("internal_analyze", {}), "_held_too_recent")

    ours = []
    if new_media:
        ours.append(f"{new_media} new reel{'s' if new_media != 1 else ''} picked up")
    if analyzed:
        ours.append(f"{analyzed} analyzed")
    if held:
        ours.append(f"{held} waiting out the {config.ANALYSIS_MIN_AGE_DAYS}-day window")
    if labelled:
        ours.append(f"{labelled} graded now they've settled")
    if metrics:
        ours.append(f"metrics refreshed on {metrics} cell{'s' if metrics != 1 else ''}")
    lines.append("• *Our feed:* " + (", ".join(ours) if ours else "nothing new this week"))

    # --- external inspiration (reference only, and said so) ---------------
    found = _n(by.get("external_discovery", {}), "created", "_added")
    scanned = _n(by.get("external_discovery", {}), "processed")
    usable = _n(by.get("external_quality", {}), "_quality_80")
    conns = (_n(by.get("external_connections", {}), "created")
             + _n(by.get("external_connections", {}), "updated"))
    ext = []
    if found:
        ext.append(f"{found} new candidate{'s' if found != 1 else ''} saved"
                   + (f" from {scanned} scanned" if scanned else ""))
    if usable:
        ext.append(f"{usable} clean enough to build on")
    if conns:
        ext.append(f"{conns} linked to something we've proven")
    disc = by.get("external_discovery", {})
    if not ext and str(disc.get("status", "")) == "skipped" and disc.get("reason"):
        ext.append(disc["reason"])
    lines.append("• *Inspiration:* " + (", ".join(ext) if ext else "nothing new")
                 + (" _(reference only — never proof it works for us)_" if found or usable else ""))

    # --- did the brain's conclusions actually move? -----------------------
    rec = by.get("internal_recompute", {})
    rebuilt = bool(rec.get("_correlations_rebuilt"))
    profiles = _n(rec, "_profiles_updated", "updated")
    brain = []
    if rebuilt:
        brain.append("patterns recomputed")
    if profiles:
        brain.append(f"{profiles} winning profile{'s' if profiles != 1 else ''} updated")
    notion = by.get("notion_sync", {})
    if str(notion.get("status", "")) == "success" and _n(notion, "created", "updated"):
        brain.append("Notion synced")
    lines.append("• *Brain:* " + (", ".join(brain) if brain
                                  else "conclusions unchanged — no new evidence to move them"))

    # --- anything a human has to do ---------------------------------------
    todo = []
    if report.get("should_regenerate_ideas"):
        reasons = ", ".join(report.get("idea_regen_reasons") or [])
        todo.append(f"Worth regenerating ideas{f' ({reasons})' if reasons else ''} — "
                    f"that step is never automatic.")
    failed = [s for s in (report.get("stages") or []) if s.get("status") == "failed"]
    for s in failed[:3]:
        todo.append(f"`{s.get('stage')}` failed: {str(s.get('reason', ''))[:140]}")
    if health and health.get("state") in ("BLOCKED", "PARTIAL", "STALE"):
        for r in (health.get("reasons") or [])[:2]:
            todo.append(r)
    if todo:
        lines.append("")
        lines.append("*Needs you:*")
        lines += [f"• {t}" for t in todo]

    if config.DASHBOARD_URL:
        lines += ["", f"Dashboard: {config.DASHBOARD_URL}"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# delivery — best effort, never raises, never blocks a run
# ---------------------------------------------------------------------------
def email_configured() -> bool:
    return bool(config.DIGEST_EMAIL_TO and config.SMTP_HOST and config.SMTP_FROM)


def send_email(subject: str, body: str) -> bool:
    """Plain SMTP (stdlib, no new dependency). Returns True when sent.

    Entirely opt-in: with no SMTP_HOST / DIGEST_EMAIL_TO configured this is a
    no-op, so nothing is ever sent from an unconfigured deploy.
    """
    if not email_configured():
        return False
    import smtplib
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.SMTP_FROM
    recipients = [a.strip() for a in config.DIGEST_EMAIL_TO.split(",") if a.strip()]
    msg["To"] = ", ".join(recipients)
    # Slack mrkdwn bold/italic markers would read as noise in an email client.
    msg.set_content(body.replace("*", "").replace("_", ""))
    try:
        if config.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
            if config.SMTP_USE_TLS:
                server.starttls()
        with server:
            if config.SMTP_USERNAME:
                server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.send_message(msg)
        log.info("weekly digest emailed to %d recipient(s)", len(recipients))
        return True
    except Exception as e:  # noqa: BLE001 - delivery never breaks the refresh
        log.warning("weekly digest email failed: %s: %s", type(e).__name__, e)
        return False


def deliver(report: dict, health: dict | None = None) -> dict:
    """Render and push the digest. Returns {text, slack, email} — never raises."""
    out = {"text": "", "slack": "not_configured", "email": "not_configured"}
    try:
        text = build_digest(report, health)
    except Exception as e:  # noqa: BLE001
        log.warning("weekly digest render failed: %s", e)
        return out
    if not text:
        return out
    out["text"] = text

    if config.SLACK_WEBHOOK_URL:
        try:
            import slack_report
            slack_report.post(text)
            out["slack"] = "posted"
        except Exception as e:  # noqa: BLE001
            log.warning("weekly digest Slack post failed: %s: %s", type(e).__name__, e)
            out["slack"] = "failed"

    if email_configured():
        subject = "Storelli brain — weekly update"
        out["email"] = "sent" if send_email(subject, text) else "failed"
    return out
