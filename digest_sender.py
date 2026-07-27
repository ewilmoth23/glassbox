"""Glassbox daily-digest email sender.

Reads verified subscribers from `signals_subscription`, fetches today's
signals (filtered per-subscriber by severity_floor + category_ids),
renders a clean HTML+plaintext email, and ships it through the Postmark
HTTP API. Logs every send (success + failure) to `signals_digest_log`.

Run as a CLI:
    python -m digest_sender                  # send to all verified subs
    python -m digest_sender --to me@x.com    # one-off test send
    python -m digest_sender --dry-run        # render but don't send

Env vars (read from .env.glassbox at module import):
    GLASSBOX_SMTP_PROVIDER  must be 'postmark' (other providers TBD)
    GLASSBOX_SMTP_TOKEN     Postmark Server API token (UUID)
    GLASSBOX_SMTP_FROM      verified sender, e.g. signals@mewrcreate.com

Wired into a launchd schedule by ../09_SETUP_GUIDES/launchd/com.mewr.glassbox-digest.plist
(daily at 11:00 UTC ≈ 7am ET).
"""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from html import escape as h
from pathlib import Path
from typing import Any

import httpx

# Make `from db import …` work regardless of how this is invoked
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import init_pools, close_pools, fetch_write  # noqa: E402

log = logging.getLogger("glassbox.digest")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
)

POSTMARK_URL    = "https://api.postmarkapp.com/email"
GLASSBOX_PUBLIC = os.environ.get("GLASSBOX_PUBLIC_URL", "https://mewrcreate.com")

SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}


# ─── Subscriber + signal fetching ────────────────────────────────────

async def fetch_subscribers(only_email: str | None = None,
                            test_synthetic: bool = False) -> list[dict[str, Any]]:
    """Return all verified subscribers (or a single one, by email).

    `test_synthetic=True` returns a single synthetic subscriber for the
    given email regardless of whether it exists in the table — useful
    for one-off SMTP verification without polluting the subscriber list.
    """
    if test_synthetic and only_email:
        return [{
            "email": only_email.strip().lower(),
            "filters": {"severity_floor": "low", "category_ids": []},
            "unsubscribe_token": "TEST-SEND-NO-UNSUB",
        }]
    if only_email:
        rows = await fetch_write(
            "SELECT email, filters, unsubscribe_token "
            "FROM signals_subscription "
            "WHERE email=$1 AND verified=true",
            only_email.strip().lower(),
        )
    else:
        rows = await fetch_write(
            "SELECT email, filters, unsubscribe_token "
            "FROM signals_subscription "
            "WHERE verified=true "
            "ORDER BY created_at DESC",
        )
    return [dict(r) for r in rows]


async def fetch_signals_today(window_hours: int = 24) -> dict[str, Any]:
    """Pull today's signals from the live API surface — same shape the
    public /signals page consumes. Done HTTP-side rather than DB-side
    so the digest stays in sync with the public ranking logic."""
    base = os.environ.get("GLASSBOX_INTERNAL_URL", "http://127.0.0.1:8790")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            f"{base}/api/v1/signals/today",
            params={"window_hours": window_hours, "per_category": 12},
        )
        r.raise_for_status()
        return r.json()


def filter_for_subscriber(sig: dict[str, Any],
                          filters: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply per-subscriber severity_floor + category_ids filter to the
    `categories` array, returning a flat list of items they qualify for.

    Per-category cap is 3 (not 8) — the previous default was producing
    digests with 8 near-identical shadow-fleet entries per pivot vessel,
    making the email read like a JSON dump. Three top items per category
    forces editorial focus."""
    floor = SEVERITY_RANK.get((filters.get("severity_floor") or "high"), 3)
    cat_ids = filters.get("category_ids") or []
    out: list[dict[str, Any]] = []
    for cat in sig.get("categories") or []:
        sev = cat.get("severity", "medium")
        if SEVERITY_RANK.get(sev, 0) < floor:
            continue
        if cat_ids and cat["id"] not in cat_ids:
            continue
        cat_items_total = len(cat.get("items") or [])
        for it in (cat.get("items") or [])[:3]:
            out.append({**it, "category": cat.get("label"),
                        "severity": sev, "category_id": cat["id"],
                        "category_total_count": cat_items_total})
    # Stable sort by ts desc, then severity desc.
    out.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
    out.sort(key=lambda x: -SEVERITY_RANK.get(x["severity"], 0))
    return out[:25]


def _strip_title_prefix(title: str) -> str:
    """Strip the algorithmic 'CRITICAL — ' / 'ALERT — ' prefix that
    duplicates the severity badge."""
    return (title or "(untitled)") \
        .replace("CRITICAL — ", "") \
        .replace("ALERT — ", "") \
        .replace("HIGH — ", "")


def _group_by_category(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group flat items list into [{category, severity, items, total}, ...]
    preserving original order (which is already severity-then-ts sorted)."""
    out: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for it in items:
        cid = it.get("category_id") or "uncategorized"
        if cid not in seen:
            grp = {
                "category_id": cid,
                "category":    it.get("category") or "Other",
                "severity":    it.get("severity") or "medium",
                "items":       [],
                "total":       it.get("category_total_count") or 0,
            }
            seen[cid] = grp
            out.append(grp)
        seen[cid]["items"].append(it)
    return out


def _build_narrative(items: list[dict[str, Any]],
                     groups: list[dict[str, Any]]) -> str:
    """Two-sentence editorial summary built from facts, not LLM. Reads
    like a brief: what's the most pressing development, and what's the
    overall shape of the past 24h?"""
    if not items:
        return ("No findings cleared the analyst threshold in the past 24 hours. "
                "Either the world is quiet — or your filters are tight.")
    critical = [i for i in items if i["severity"] == "critical"]
    high     = [i for i in items if i["severity"] == "high"]
    lead     = critical[0] if critical else (high[0] if high else items[0])
    lead_cat = lead.get("category") or "an active category"
    lead_title = _strip_title_prefix(lead.get("title") or "")
    if len(lead_title) > 90:
        lead_title = lead_title[:87] + "…"
    n_cats = len(groups)
    parts = [
        f"Today's most pressing signal is in <b>{h(lead_cat)}</b>: {h(lead_title)}.",
    ]
    if len(critical) > 1:
        parts.append(
            f"{len(critical)} critical findings across {n_cats} "
            f"{'category' if n_cats == 1 else 'categories'}"
            f" warrant attention; {len(high)} additional high-severity items follow."
        )
    elif high:
        parts.append(
            f"{len(high)} high-severity findings across {n_cats} "
            f"{'category' if n_cats == 1 else 'categories'} follow."
        )
    else:
        parts.append(
            f"{len(items)} findings across {n_cats} "
            f"{'category' if n_cats == 1 else 'categories'}."
        )
    return " ".join(parts)


# ─── Email rendering ─────────────────────────────────────────────────

SEV_COLOR = {
    "critical": "#ff5b5b", "high": "#ff9f3a",
    "medium":   "#ffd166", "low":  "#54e29c",
}
SEV_LABEL = {
    "critical": "CRITICAL", "high": "HIGH",
    "medium":   "MEDIUM",   "low":  "LOW",
}


def _hero_block(item: dict[str, Any]) -> str:
    """The lead-story card. First critical (or first overall) item
    gets larger treatment — bigger headline, longer description, facts
    row, deep link."""
    sev = item.get("severity", "medium")
    title = _strip_title_prefix(item.get("title") or "")
    desc = (item.get("description") or "")[:420]
    # Authority comes back as a dict {name, url, ...} or as a plain string,
    # depending on the algorithm. Render the displayable name in either case.
    authority_raw = item.get("authority") or ""
    if isinstance(authority_raw, dict):
        authority = authority_raw.get("name") or authority_raw.get("label") or ""
    else:
        authority = str(authority_raw)
    facts = item.get("facts") or {}
    # Build a small fact strip — location coords + authority + when
    fact_chips = []
    if item.get("lat") is not None and item.get("lng") is not None:
        fact_chips.append(f"{float(item['lat']):.2f}, {float(item['lng']):.2f}")
    if authority:
        fact_chips.append(h(authority))
    if item.get("ts"):
        try:
            ts = datetime.fromisoformat(item["ts"].replace("Z", "+00:00"))
            fact_chips.append(ts.strftime("%H:%M UTC"))
        except Exception:
            pass
    fact_row = " &nbsp;·&nbsp; ".join(fact_chips) if fact_chips else ""
    entity_url = ""
    if item.get("entity_id"):
        entity_url = f"{GLASSBOX_PUBLIC}/entity/{item['entity_id']}"
    cta = (f'<a href="{entity_url}" '
           f'style="color:#7d6e45;font-weight:600;text-decoration:none">'
           f'View entity profile →</a>') if entity_url else ""
    return f"""
    <tr><td style="padding:0 24px 4px">
      <div style="font:700 10px/1 -apple-system,Helvetica,sans-serif;
                  text-transform:uppercase;letter-spacing:0.18em;
                  color:{SEV_COLOR.get(sev, '#807a6c')};margin-top:18px">
        ▍&nbsp;LEAD STORY &nbsp;·&nbsp; {h(SEV_LABEL.get(sev, sev.upper()))}
      </div>
    </td></tr>
    <tr><td style="padding:6px 24px 14px">
      <div style="font:700 19px/1.25 Georgia,serif;color:#1a1814;margin:4px 0 8px">
        {h(title)}
      </div>
      <div style="font:400 13px/1.55 Georgia,serif;color:#3a352b;margin-bottom:10px">
        {h(desc)}{'…' if len(item.get('description') or '') > 420 else ''}
      </div>
      <div style="font:500 11px/1.4 -apple-system,Helvetica,sans-serif;
                  color:#807a6c;letter-spacing:0.04em">
        {fact_row}
      </div>
      {'<div style="margin-top:10px;font:600 12px/1 -apple-system,Helvetica,sans-serif">' + cta + '</div>' if cta else ''}
    </td></tr>
    <tr><td style="padding:0 24px"><div style="height:1px;background:#e5e2da;margin:6px 0"></div></td></tr>"""


def _category_section(group: dict[str, Any], skip_first_item: bool = False) -> str:
    """A category section: small section header + scannable bullet list.
    `skip_first_item` set when this category's lead is already in the
    hero block above (avoids duplication)."""
    items = group["items"]
    if skip_first_item:
        items = items[1:]
    if not items:
        return ""
    sev = group["severity"]
    label = group["category"]
    total = group.get("total") or len(items)
    extra = total - len(items)
    bullet_rows = []
    for it in items:
        title = _strip_title_prefix(it.get("title") or "")
        # Compact secondary description (1 sentence max, ~110 chars)
        desc = (it.get("description") or "")
        first_period = desc.find(". ")
        if 30 < first_period < 130:
            desc = desc[:first_period + 1]
        else:
            desc = desc[:130]
            if len(it.get("description") or "") > 130:
                desc = desc.rstrip() + "…"
        bullet_rows.append(f"""
        <div style="padding:8px 0;border-bottom:1px dotted #ebe7dc">
          <div style="font:600 13px/1.35 Georgia,serif;color:#23201a">
            <span style="color:{SEV_COLOR.get(sev, '#807a6c')};
                         font:700 9px/1 -apple-system,sans-serif;
                         vertical-align:middle;letter-spacing:0.1em;
                         margin-right:6px">●</span>{h(title)}
          </div>
          <div style="font:400 11.5px/1.5 -apple-system,Helvetica,sans-serif;
                      color:#5e5847;margin:2px 0 0 14px">
            {h(desc)}
          </div>
        </div>""")
    more_note = ""
    if extra > 0 and skip_first_item is False:
        more_note = f"""
        <div style="font:italic 11px/1.4 Georgia,serif;color:#a59a82;
                    padding:4px 0 0 14px">
          + {extra} more in this category — see the live dashboard
        </div>"""
    return f"""
    <tr><td style="padding:14px 24px 4px">
      <div style="font:700 10px/1 -apple-system,Helvetica,sans-serif;
                  text-transform:uppercase;letter-spacing:0.18em;color:#807a6c">
        {h(label.upper())}
        &nbsp;<span style="color:{SEV_COLOR.get(sev, '#807a6c')}">·
          {h(SEV_LABEL.get(sev, sev.upper()))}</span>
      </div>
    </td></tr>
    <tr><td style="padding:4px 24px 6px">
      {''.join(bullet_rows)}
      {more_note}
    </td></tr>"""


def render_html(items: list[dict[str, Any]],
                email: str,
                unsubscribe_token: str,
                window_hours: int = 24) -> str:
    """Editorial-style daily brief. Hero block for the lead story,
    grouped sections below, narrative paragraph up top. Inline styles
    only — most email clients strip <style>."""
    when = datetime.now(timezone.utc).strftime("%a %d %b %Y · %H:%M UTC")
    groups = _group_by_category(items)
    narrative = _build_narrative(items, groups)
    if items:
        hero = _hero_block(items[0])
        # First section's first item is in the hero — skip it there
        sections = []
        if groups:
            sections.append(_category_section(groups[0], skip_first_item=True))
        for g in groups[1:]:
            sections.append(_category_section(g))
        body = hero + "".join(s for s in sections if s)
    else:
        hero = ""
        body = """
        <tr><td style="padding:28px 14px;text-align:center;color:#807a6c;
                       font:italic 13px/1.6 Georgia,serif">
          No findings cleared the analyst threshold in the past 24 hours.<br>
          The world is quiet — for now.
        </td></tr>"""
    unsub_url = f"{GLASSBOX_PUBLIC}/api/v1/signals/unsubscribe?t={unsubscribe_token}"
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f6f1e7;font-family:Georgia,serif">
<table role="presentation" cellpadding="0" cellspacing="0" border="0"
       width="100%" style="background:#f6f1e7;padding:30px 0">
  <tr><td align="center">
    <table role="presentation" cellpadding="0" cellspacing="0" border="0"
           width="620" style="background:#fff;border:1px solid #e5e2da">
      <tr><td style="padding:22px 24px 16px;border-bottom:3px solid #ffb547">
        <div style="font:800 22px/1.0 Georgia,serif;color:#1a1814">
          GLASS<span style="color:#ffb547">BOX</span>
          <span style="font:500 13px/1 Georgia,serif;color:#807a6c;
                       letter-spacing:0.06em;margin-left:8px">·&nbsp;daily intelligence</span>
        </div>
        <div style="font:500 10.5px/1.5 -apple-system,Helvetica,sans-serif;
                    text-transform:uppercase;letter-spacing:0.18em;
                    color:#a59a82;margin-top:8px">
          {h(when)} &nbsp;·&nbsp; {len(items)} findings &nbsp;·&nbsp; last {window_hours}h window
        </div>
      </td></tr>
      <tr><td style="padding:18px 24px 4px">
        <div style="font:400 13.5px/1.65 Georgia,serif;color:#23201a">
          {narrative}
        </div>
      </td></tr>
      {body}
      <tr><td style="padding:20px 24px;background:#fafaf6;
                     border-top:1px solid #e5e2da;font:400 11px/1.55 -apple-system,sans-serif;
                     color:#807a6c">
        Algorithm-derived from public OSINT sources by Glassbox running
        on-prem.&nbsp;
        <a href="{GLASSBOX_PUBLIC}/signals"
           style="color:#7d6e45;font-weight:600;text-decoration:none">Live dashboard ↗</a>
        &nbsp;·&nbsp;
        <a href="{unsub_url}"
           style="color:#807a6c;text-decoration:underline">Unsubscribe</a>
        <br><span style="font-size:10px;color:#a59a82">Sent to {h(email)} ·
        © {datetime.now(timezone.utc).year} MEWR Creative Enterprises LLC</span>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""


def _strip_html_in_narrative(s: str) -> str:
    """The narrative is built for HTML (contains <b>tags). Strip them for plaintext."""
    return s.replace("<b>", "").replace("</b>", "")


def render_text(items: list[dict[str, Any]],
                email: str,
                unsubscribe_token: str,
                window_hours: int = 24) -> str:
    """Plaintext fallback for clients that don't render HTML. Same
    editorial structure: narrative paragraph, lead-story block, then
    grouped category sections."""
    when = datetime.now(timezone.utc).strftime("%a %d %b %Y · %H:%M UTC")
    groups = _group_by_category(items)
    narrative = _strip_html_in_narrative(_build_narrative(items, groups))
    lines = [
        f"GLASSBOX — daily intelligence",
        f"{when} · {len(items)} findings · last {window_hours}h",
        "─" * 64,
        "",
        narrative,
        "",
    ]
    if items:
        lead = items[0]
        lead_title = _strip_title_prefix(lead.get("title") or "")
        lead_desc = (lead.get("description") or "")[:420]
        lines += [
            "─" * 64,
            f"LEAD STORY · {lead.get('severity', 'medium').upper()}",
            f"  {lead_title}",
            f"  {lead_desc}{'…' if len(lead.get('description') or '') > 420 else ''}",
            "",
        ]
        # Group sections (skip the first item in the first group — it's the lead)
        for gi, g in enumerate(groups):
            its = g["items"][1:] if gi == 0 else g["items"]
            if not its:
                continue
            lines.append("─" * 64)
            lines.append(f"{g['category'].upper()} · {g['severity'].upper()}")
            for it in its:
                t = _strip_title_prefix(it.get("title") or "")
                d = (it.get("description") or "")
                if len(d) > 130:
                    fp = d.find(". ")
                    d = d[:fp + 1] if 30 < fp < 130 else d[:130].rstrip() + "…"
                lines.append(f"  · {t}")
                if d:
                    lines.append(f"    {d}")
            extra = (g.get("total") or 0) - len(g["items"])
            if extra > 0 and gi != 0:
                lines.append(f"    + {extra} more in this category")
            lines.append("")
    else:
        lines.append("No findings cleared the analyst threshold in the past 24 hours.")
        lines.append("")
    lines += [
        "─" * 64,
        f"Live dashboard: {GLASSBOX_PUBLIC}/signals",
        f"Unsubscribe:    {GLASSBOX_PUBLIC}/api/v1/signals/unsubscribe?t={unsubscribe_token}",
        f"Sent to {email} · © {datetime.now(timezone.utc).year} MEWR Creative Enterprises LLC",
    ]
    return "\n".join(lines)


# ─── Postmark transport ──────────────────────────────────────────────

async def send_via_postmark(to_email: str, subject: str,
                            html_body: str, text_body: str) -> dict[str, Any]:
    token   = os.environ.get("GLASSBOX_SMTP_TOKEN", "")
    sender  = os.environ.get("GLASSBOX_SMTP_FROM", "signals@mewrcreate.com")
    if not token:
        raise RuntimeError("GLASSBOX_SMTP_TOKEN not set in .env.glassbox")
    payload = {
        "From": f"Glassbox <{sender}>",
        "To": to_email,
        "Subject": subject,
        "HtmlBody": html_body,
        "TextBody": text_body,
        "MessageStream": "outbound",
        "Tag": "daily-digest",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            POSTMARK_URL,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Postmark-Server-Token": token,
            },
        )
        return {"status": r.status_code, "body": r.json()}


# ─── Logging table ───────────────────────────────────────────────────

LOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS signals_digest_log (
    id           BIGSERIAL PRIMARY KEY,
    sent_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    email        TEXT NOT NULL,
    findings     INTEGER NOT NULL,
    success      BOOLEAN NOT NULL,
    provider     TEXT NOT NULL DEFAULT 'postmark',
    message_id   TEXT,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS signals_digest_log_email_idx
    ON signals_digest_log (email, sent_at DESC);
"""

async def ensure_log_table() -> None:
    from db import acquire_write
    async with acquire_write() as conn:
        await conn.execute(LOG_TABLE_DDL)


async def record_send(email: str, findings: int,
                      success: bool, message_id: str | None,
                      error: str | None) -> None:
    from db import execute_write
    await execute_write(
        "INSERT INTO signals_digest_log "
        "(email, findings, success, message_id, error) "
        "VALUES ($1, $2, $3, $4, $5)",
        email, findings, success, message_id, error,
    )


# ─── Orchestration ───────────────────────────────────────────────────

async def send_digest(only_email: str | None = None,
                      dry_run: bool = False,
                      test_send: bool = False,
                      window_hours: int = 24) -> dict[str, int]:
    """Main entry point. Returns a {sent, failed, skipped} count dict.

    test_send=True bypasses the subscriber table and ships a sample
    digest to `only_email` — used for verifying SMTP config end-to-end.
    """
    await init_pools()
    try:
        await ensure_log_table()
        sig  = await fetch_signals_today(window_hours=window_hours)
        subs = await fetch_subscribers(only_email=only_email,
                                       test_synthetic=test_send)
        log.info("starting digest run: %d subscribers, %d categories",
                 len(subs), len(sig.get("categories") or []))
        sent = failed = skipped = 0
        for sub in subs:
            email   = sub["email"]
            filters = sub["filters"] if isinstance(sub["filters"], dict) \
                      else (json.loads(sub["filters"]) if sub["filters"] else {})
            unsub   = sub["unsubscribe_token"]
            items   = filter_for_subscriber(sig, filters)
            html    = render_html(items, email, unsub, window_hours)
            text    = render_text(items, email, unsub, window_hours)
            crit    = sum(1 for i in items if i["severity"] == "critical")
            subject = (f"Glassbox · {len(items)} findings"
                       + (f" · {crit} critical" if crit else ""))
            if dry_run:
                log.info("DRY-RUN to=%s items=%d", email, len(items))
                skipped += 1
                continue
            try:
                resp = await send_via_postmark(email, subject, html, text)
                ok   = resp["status"] == 200
                msg_id = resp["body"].get("MessageID") if ok else None
                err  = None if ok else json.dumps(resp["body"])[:500]
                await record_send(email, len(items), ok, msg_id, err)
                if ok:
                    sent += 1
                    log.info("sent to=%s items=%d msg_id=%s",
                             email, len(items), msg_id)
                else:
                    failed += 1
                    log.error("failed to=%s status=%s body=%s",
                              email, resp["status"], err)
            except Exception as e:
                failed += 1
                err = f"exception: {type(e).__name__}: {str(e)[:300]}"
                log.exception("send error to=%s", email)
                try:
                    await record_send(email, len(items), False, None, err)
                except Exception:
                    pass
        return {"sent": sent, "failed": failed, "skipped": skipped,
                "subscribers": len(subs)}
    finally:
        await close_pools()


def _load_env() -> None:
    """Read .env.glassbox sitting next to the project root, no dep on
    python-dotenv. Variables already in os.environ win (so process-level
    overrides still work)."""
    env_path = Path(__file__).resolve().parent.parent / ".env.glassbox"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Glassbox daily-digest sender")
    parser.add_argument("--to", help="Send only to this email (must be a verified subscriber)")
    parser.add_argument("--test-send", help="Bypass subscriber table — ship a sample digest to this email for SMTP verification")
    parser.add_argument("--dry-run", action="store_true",
                        help="Render but do not send or log")
    parser.add_argument("--window-hours", type=int, default=24)
    args = parser.parse_args()
    _load_env()
    target = args.test_send or args.to
    result = asyncio.run(send_digest(
        only_email=target,
        dry_run=args.dry_run,
        test_send=bool(args.test_send),
        window_hours=args.window_hours,
    ))
    log.info("digest run complete: %s", result)
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
