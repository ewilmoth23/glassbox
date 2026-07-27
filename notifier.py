"""
notifier.py — unified outbound channel for watchlist alerts + SITREPs.

Channels (all optional, gracefully degrade):
  - Slack webhook (per-target or global via SLACK_WEBHOOK_URL env)
  - Email via SMTP (if SMTP_HOST/USER/PASSWORD set)
  - Brain event log (always)

One function to call them all:
    dispatch(
        subject="Watchlist 'Japan M5+' fired",
        body_md="...",
        emails=["alice@example.com"],
        slack_webhooks=["https://hooks.slack.com/..."],
    )
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from typing import List, Optional


log = logging.getLogger("notifier")


def _post_slack(webhook_url: str, text: str) -> bool:
    try:
        body = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            r.read()
        return True
    except Exception as e:
        log.info(f"slack post failed: {e}")
        return False


def _send_email(to_email: str, subject: str, body: str, html: Optional[str] = None) -> bool:
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT") or "587")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASSWORD")
    from_addr = os.environ.get("SMTP_FROM") or user
    if not (host and user and password and from_addr):
        log.info("SMTP not configured — skipping email")
        return False
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = from_addr
        msg["To"] = to_email
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        ctx = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=15) as s:
            s.ehlo()
            if port != 465:
                s.starttls(context=ctx)
                s.ehlo()
            s.login(user, password)
            s.send_message(msg)
        return True
    except Exception as e:
        log.warning(f"email send failed: {e}")
        return False


def dispatch(
    subject: str,
    body_md: str,
    emails: Optional[List[str]] = None,
    slack_webhooks: Optional[List[str]] = None,
    html: Optional[str] = None,
) -> dict:
    """
    Fire notification across all configured channels. Returns a summary.
    Missing channels silently skip — this is best-effort; the caller
    persists the fact of firing regardless.
    """
    result = {"slack_sent": 0, "email_sent": 0, "attempted": 0}
    for url in (slack_webhooks or []):
        if not url: continue
        result["attempted"] += 1
        if _post_slack(url, f"*{subject}*\n{body_md}"):
            result["slack_sent"] += 1
    global_slack = os.environ.get("SLACK_WEBHOOK_URL")
    if global_slack and not (slack_webhooks and global_slack in slack_webhooks):
        result["attempted"] += 1
        if _post_slack(global_slack, f"*{subject}*\n{body_md}"):
            result["slack_sent"] += 1
    for e in (emails or []):
        if not e or "@" not in e: continue
        result["attempted"] += 1
        if _send_email(e, subject, body_md, html=html):
            result["email_sent"] += 1
    return result
