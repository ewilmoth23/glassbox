"""
MEWR Glassbox — Citizen OSINT Harvester Runner
===============================================
Orchestrates all citizen OSINT sources (YouTube, Bluesky, Reddit, Telegram, Nitter)
and routes events through the same pipeline as sentinel_runner.py:
  1. Run all citizen ingesters
  2. Confidence-score every event
  3. Write to citizen_feed.json (Glassbox server polls this)
  4. Submit top events to MEWR OS review queue
  5. Write summary to Brain

Run:
  python3 harvester_runner.py              # Live run
  python3 harvester_runner.py --dry-run    # Preview without writing
  python3 harvester_runner.py --verbose    # Print event list to console
  python3 harvester_runner.py --platform youtube  # Single platform only

Cron (every 30 minutes):
  */30 * * * * cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI" && python3 harvester_runner.py >> /tmp/harvester_runner.log 2>&1
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

# Local imports
sys.path.insert(0, str(Path(__file__).parent))
from ingesters.citizen_osint import CitizenOSINTIngester
from confidence_scorer import LABEL_THRESHOLDS

# ─── Config ──────────────────────────────────────────────────────────────────

GLASSBOX_URL  = os.getenv("GLASSBOX_URL",  "http://127.0.0.1:8790")
MEWR_OS_URL   = os.getenv("MEWR_OS_URL",   "http://127.0.0.1:8788")
BRAIN_API_URL = os.getenv("BRAIN_API_URL", "http://127.0.0.1:8800")

# Feed file read by glassbox_server.py news-manifest endpoint
FEED_FILE = Path(__file__).parent / "citizen_sentinel_feed.json"

# Minimum confidence to include on globe
MIN_CONFIDENCE = 0.35   # SPECULATIVE and below are skipped

# Maximum events to write per cycle (keep globe clean)
MAX_EVENTS = 200

# Minimum confidence to submit to review queue (only quality events)
REVIEW_THRESHOLD = 0.55

log = logging.getLogger("harvester_runner")

# ─── Feed Writer ─────────────────────────────────────────────────────────────

def write_feed(events: List[Dict[str, Any]], dry_run: bool = False) -> int:
    """Write events to citizen_sentinel_feed.json for Glassbox server to poll."""
    filtered = [
        e for e in events
        if e.get("confidence_score", 0) >= MIN_CONFIDENCE
        and e.get("has_coords")  # Only geolocated events on globe
    ]
    filtered = filtered[:MAX_EVENTS]

    payload = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "source": "citizen_osint_harvester",
        "count": len(filtered),
        "events": filtered,
    }

    if dry_run:
        log.info("[DRY-RUN] Would write %d events to %s", len(filtered), FEED_FILE)
    else:
        with open(FEED_FILE, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        log.info("Wrote %d events to citizen_sentinel_feed.json", len(filtered))

    return len(filtered)


# ─── Review Queue Submission ──────────────────────────────────────────────────

async def submit_to_review_queue(
    session: aiohttp.ClientSession,
    events: List[Dict[str, Any]],
    dry_run: bool = False,
) -> None:
    """Submit top events to MEWR OS review queue for Boss Man review."""
    # Only submit the highest-confidence events
    top_events = [
        e for e in events
        if e.get("confidence_score", 0) >= REVIEW_THRESHOLD
    ][:20]

    if not top_events:
        log.info("No events above review threshold %.2f", REVIEW_THRESHOLD)
        return

    # Build a summary digest for review
    by_platform: Dict[str, int] = {}
    for e in events:
        p = e.get("platform", "unknown")
        by_platform[p] = by_platform.get(p, 0) + 1

    platform_summary = ", ".join(
        f"{p}: {n}" for p, n in sorted(by_platform.items(), key=lambda x: -x[1])
    )

    high_conf = [e for e in events if e.get("confidence_score", 0) >= 0.65]
    top_lines = []
    for e in top_events[:10]:
        label = e.get("confidence_label", "?")
        title = e.get("title", "")[:80]
        src = e.get("source", "")
        coords = ""
        if e.get("has_coords"):
            coords = f" ({e['lat']:.2f}, {e['lng']:.2f})"
        top_lines.append(f"[{label}] {title}{coords} — {src}")

    content = f"""## CITIZEN OSINT HARVEST — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

**Total events:** {len(events)}
**Geolocated:** {sum(1 for e in events if e.get('has_coords'))}
**HIGH+ confidence:** {len(high_conf)}

**By platform:** {platform_summary}

### Top Events (confidence-ranked):
{chr(10).join(top_lines)}
"""

    payload = {
        "content_type": "citizen_osint_digest",
        "agent": "citizen_harvester",
        "pipeline": "harvester_runner",
        "content": content,
        "metadata": {
            "total_events": len(events),
            "geolocated": sum(1 for e in events if e.get("has_coords")),
            "high_confidence": len(high_conf),
            "platforms": by_platform,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        },
    }

    if dry_run:
        log.info("[DRY-RUN] Would submit digest to review queue")
        print("\n" + "="*60)
        print("REVIEW QUEUE SUBMISSION (dry-run):")
        print(content)
        return

    try:
        url = f"{MEWR_OS_URL}/api/content-review/submit"
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as resp:
            if resp.status in (200, 201):
                log.info("Submitted citizen OSINT digest to review queue")
            else:
                body = await resp.text()
                log.warning("Review queue submit HTTP %s: %s", resp.status, body[:200])
    except Exception as exc:
        log.warning("Review queue submit error (non-fatal): %s", exc)


# ─── Brain Write-back ─────────────────────────────────────────────────────────

async def persist_to_brain(
    session: aiohttp.ClientSession,
    events: List[Dict[str, Any]],
    dry_run: bool = False,
) -> None:
    """Write a summary of this harvest cycle to the Brain knowledge base."""
    by_platform: Dict[str, int] = {}
    for e in events:
        p = e.get("platform", "unknown")
        by_platform[p] = by_platform.get(p, 0) + 1

    top3 = sorted(events, key=lambda e: e.get("confidence_score", 0), reverse=True)[:3]
    top3_text = "; ".join(
        f"{e.get('title', '')[:60]} ({e.get('confidence_label', '?')})"
        for e in top3
    )

    content = (
        f"Citizen OSINT harvest at {datetime.now(timezone.utc).isoformat()}: "
        f"{len(events)} events across {len(by_platform)} platforms. "
        f"Geolocated: {sum(1 for e in events if e.get('has_coords'))}. "
        f"Top events: {top3_text}"
    )

    if dry_run:
        log.info("[DRY-RUN] Would write to Brain: %s", content[:100])
        return

    try:
        url = f"{BRAIN_API_URL}/api/remember"
        payload = {
            "namespace": "glassbox",
            "key": f"citizen_harvest_{int(time.time())}",
            "content": content,
            "tags": ["citizen_osint", "harvest", "glassbox"],
        }
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status in (200, 201):
                log.info("Harvest summary written to Brain")
            else:
                log.debug("Brain write HTTP %s (non-fatal)", resp.status)
    except Exception as exc:
        log.debug("Brain write error (non-fatal): %s", exc)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main(dry_run: bool = False, verbose: bool = False,
               platform_filter: Optional[str] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    log.info("=== Citizen OSINT Harvester starting ===")
    start = time.time()

    ingester = CitizenOSINTIngester()
    run_result = await ingester.run()

    # Filter to single platform if requested
    if platform_filter:
        pflt = platform_filter.lower()
        run_result = {k: v for k, v in run_result.items() if pflt in k}
        log.info("Platform filter applied: %s", pflt)

    all_events = ingester.all_events(run_result)

    if verbose:
        print(f"\n{'='*70}")
        print(f"CITIZEN OSINT HARVEST — {len(all_events)} events")
        print(f"{'='*70}")
        for platform, events in run_result.items():
            if events:
                print(f"\n  {platform.upper()}: {len(events)} events")
                for ev in events[:5]:
                    coord_str = (
                        f" ({ev['lat']:.3f},{ev['lng']:.3f})"
                        if ev.get("has_coords") else " [no coords]"
                    )
                    print(f"    [{ev['confidence_label']:>12}] {ev['title'][:70]}{coord_str}")

    # Write to feed file
    written = write_feed(all_events, dry_run=dry_run)

    # Route to MEWR OS and Brain
    async with aiohttp.ClientSession() as session:
        await submit_to_review_queue(session, all_events, dry_run=dry_run)
        await persist_to_brain(session, all_events, dry_run=dry_run)

    elapsed = time.time() - start
    log.info(
        "=== Harvest complete: %d events, %d written to globe, %.1fs ===",
        len(all_events), written, elapsed,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MEWR Citizen OSINT Harvester")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Preview without writing or submitting")
    parser.add_argument("--verbose",  action="store_true",
                        help="Print events to console")
    parser.add_argument("--platform", default=None,
                        help="Only run one platform: youtube|bluesky|reddit|telegram|twitter")
    args = parser.parse_args()

    asyncio.run(main(
        dry_run=args.dry_run,
        verbose=args.verbose,
        platform_filter=args.platform,
    ))
