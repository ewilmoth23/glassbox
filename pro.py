"""
pro.py — is_pro(email) gate. Used by glassbox_server.py to protect Pro
endpoints (watchlist create, history, Ask if we rate-limit it later).

Pro subscribers are tracked in the Brain as `pro_subscribers` namespace
with predicate="subscription". One fact per email. The facts hold JSON
with plan + started_at + last_verified_at. They're created by:

  a) Manual CLI seed during early private beta:
        python3 21_GLASSBOX_AI/bin/mark_pro.py --email X --plan pro
  b) Stripe webhook (Phase D.2) — flips the flag on
     customer.subscription.created / .updated and back off on .deleted.

`is_pro()` is the single source of truth. If the Brain is unreachable,
it returns `allow_fallback` (default False — fail closed).

A free tier allowance is also included: `is_free_allowed(email, quota)`
uses simple daily-count checks in KV-equivalent facts. Good enough for
MVP — becomes Redis/KV in production.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT / "20_HOLDING_BRAIN" / "memory") not in sys.path:
    sys.path.insert(0, str(_ROOT / "20_HOLDING_BRAIN" / "memory"))

try:
    from brain import Brain  # type: ignore
    _BRAIN_OK = True
except Exception:
    _BRAIN_OK = False

log = logging.getLogger("pro")


# Emails hard-coded as pro during early private beta (comma-separated in env).
# These bypass the Brain check — useful for bootstrapping and internal testing.
_ALLOW_LIST = [
    e.strip().lower() for e in os.environ.get("GLASSBOX_PRO_ALLOWLIST", "").split(",") if e.strip()
]


def normalize_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def is_pro(email: Optional[str], allow_fallback: bool = False) -> bool:
    """Return True if this email has an active Pro/Intel/Enterprise subscription."""
    e = normalize_email(email)
    if not e or "@" not in e:
        return False
    if e in _ALLOW_LIST:
        return True
    if not _BRAIN_OK:
        return bool(allow_fallback)
    try:
        brain = Brain()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT object FROM facts "
            "WHERE namespace='pro_subscribers' AND predicate='subscription' AND subject=? "
            "ORDER BY created_at DESC LIMIT 1",
            (e,),
        ).fetchall()
        con.close()
    except Exception as ex:
        log.info(f"is_pro lookup failed: {ex}")
        return bool(allow_fallback)
    if not rows:
        return False
    try:
        d = json.loads(rows[0]["object"])
        status = (d.get("status") or "").lower()
        plan = (d.get("plan") or "").lower()
        return status == "active" and plan in ("pro", "intel", "enterprise")
    except Exception:
        return False


def mark_pro(email: str, plan: str = "pro", stripe_customer_id: Optional[str] = None) -> bool:
    """Write an active subscription record to the Brain."""
    e = normalize_email(email)
    if not e or "@" not in e:
        return False
    if plan not in ("pro", "intel", "enterprise"):
        raise ValueError("plan must be pro | intel | enterprise")
    if not _BRAIN_OK:
        return False
    try:
        brain = Brain()
        brain.remember(
            namespace="pro_subscribers",
            predicate="subscription",
            subject=e,
            object=json.dumps({
                "email": e, "plan": plan, "status": "active",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "stripe_customer_id": stripe_customer_id,
            }),
            source="pro.py",
            tags=f"pro,{plan}",
        )
        brain.log_event(
            namespace="holding", kind="pro_subscription_activated",
            summary=f"Pro activated: {e} ({plan})",
            detail={"email": e, "plan": plan, "customer_id": stripe_customer_id},
            severity="info", source="pro.py",
        )
        return True
    except Exception as e_:
        log.warning(f"mark_pro failed: {e_}")
        return False


def cancel_pro(email: str) -> bool:
    """Flip status to 'canceled'. Keep the record for audit history."""
    e = normalize_email(email)
    if not _BRAIN_OK:
        return False
    try:
        brain = Brain()
        brain.remember(
            namespace="pro_subscribers",
            predicate="subscription",
            subject=e,
            object=json.dumps({
                "email": e, "status": "canceled",
                "canceled_at": datetime.now(timezone.utc).isoformat(),
            }),
            source="pro.py",
            tags="pro,canceled",
        )
        brain.log_event(
            namespace="holding", kind="pro_subscription_canceled",
            summary=f"Pro canceled: {e}", detail={"email": e},
            severity="info", source="pro.py",
        )
        return True
    except Exception:
        return False


def list_pro() -> List[Dict[str, Any]]:
    if not _BRAIN_OK:
        return []
    try:
        brain = Brain()
        con = sqlite3.connect(str(brain.db_path), timeout=10.0)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT subject, object, created_at FROM facts "
            "WHERE namespace='pro_subscribers' AND predicate='subscription'"
        ).fetchall()
        con.close()
    except Exception:
        return []
    out = []
    for r in rows:
        try:
            d = json.loads(r["object"])
            if d.get("status") != "active":
                continue
            out.append({
                "email": r["subject"], "plan": d.get("plan"),
                "started_at": d.get("started_at"),
                "stripe_customer_id": d.get("stripe_customer_id"),
                "created_at": r["created_at"],
            })
        except Exception:
            continue
    return out
