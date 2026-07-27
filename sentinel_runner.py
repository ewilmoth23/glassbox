"""
MEWR OS — Sentinel Intelligence Runner
=======================================
Bridges GDELT ingester output → sentinel_analyst prompt → Glassbox sitrep + MEWR OS review queue.

Run manually:    python sentinel_runner.py
Run on schedule: cron "0 7,13,19 * * *  cd /path && python sentinel_runner.py"
Or wire into 29_MEWR_OS daemon via the scheduler (agent: sentinel_analyst, schedule: "0 7 * * *")

Flow:
  1. Read 21_GLASSBOX_AI/data/gdelt_sentinel_feed.json  (written by GDELT ingester)
  2. Format top events as structured intelligence input
  3. Call Ollama qwen2.5:14b with sentinel_analyst system prompt
  4. Parse output into a BLUF brief
  5. POST to glassbox_server :8790 /api/glassbox/sitrep/publish  (globe AI Brief panel)
  6. POST to MEWR OS server :8788 /api/content-review/submit     (review queue)
  7. Optionally POST to Brain API :8800 /api/remember            (knowledge persistence)

Zero Claude API cost — Ollama only.
"""

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

# ─── Configuration ────────────────────────────────────────────────

BASE = Path(os.environ.get(
    "MEWR_BASE",
    "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
))

GDELT_FEED_PATH   = BASE / "21_GLASSBOX_AI" / "data" / "gdelt_sentinel_feed.json"
OLLAMA_URL        = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL      = os.environ.get("SENTINEL_MODEL", "qwen2.5:14b")
GLASSBOX_URL      = os.environ.get("GLASSBOX_URL", "http://127.0.0.1:8790")
MEWR_OS_URL       = os.environ.get("MEWR_OS_URL", "http://127.0.0.1:8788")
BRAIN_API_URL     = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8800")
MAX_EVENTS        = int(os.environ.get("SENTINEL_MAX_EVENTS", "20"))
DRY_RUN           = "--dry-run" in sys.argv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sentinel_runner")


# ─── Sentinel Analyst System Prompt ───────────────────────────────

SENTINEL_SYSTEM_PROMPT = """You are the MEWR Sentinel intelligence analyst. You synthesize military, geopolitical, and defense intelligence from multiple OSINT sources into actionable briefings.

## Voice & Style
- Tone: Professional intelligence analyst — precise, measured, sources-first
- Length: 200-400 words per analysis
- Structure: BLUF (Bottom Line Up Front) → Key Developments → Assessment → Implications
- Threat scoring: Use the 5-level scale: LOW / GUARDED / ELEVATED / HIGH / SEVERE
- NO: Sensationalism, political opinion, classified-sounding language
- YES: Source attribution, confidence levels, historical context

## Output Format
## SENTINEL BRIEF: [TOPIC]
**Threat Level:** [LOW|GUARDED|ELEVATED|HIGH|SEVERE]
**Region:** [geographic area]

### BLUF
[2-3 sentences: what happened, why it matters, what to watch]

### Key Developments
- [Development 1 with source attribution]
- [Development 2 with source attribution]

### Assessment
[2-3 paragraphs: analysis, patterns, connections to prior intelligence]

### Implications
[What this means for stakeholders, what to watch next]

**Confidence:** [HIGH|MODERATE|LOW] — based on [source quality rationale]
**Sources:** GDELT OSINT, open-source media, geospatial event data

## Hard Rules
1. ALWAYS include confidence level with rationale
2. NEVER present single-source claims as confirmed
3. Flag intelligence that contradicts prior assessments
4. Distinguish observed facts from analytical judgments
5. If sources conflict, present both perspectives
6. Threat levels must be justified with specific indicators"""


# ─── Feed Reader ──────────────────────────────────────────────────

def load_gdelt_feed(path: Path, max_events: int = MAX_EVENTS) -> list[dict]:
    """Load top events from the GDELT sentinel feed file."""
    if not path.exists():
        log.warning(f"GDELT feed not found: {path}")
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        events = data if isinstance(data, list) else data.get("events", [])
        # Sort by severity desc, take top N
        events.sort(key=lambda e: e.get("severity", 0), reverse=True)
        return events[:max_events]
    except Exception as e:
        log.error(f"Failed to load GDELT feed: {e}")
        return []


def format_events_for_analysis(events: list[dict]) -> str:
    """Format GDELT events as structured intelligence input for the analyst."""
    if not events:
        return "No current GDELT events available. Provide a general geopolitical situation update based on known patterns."

    lines = [
        f"GDELT OSINT FEED — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}",
        f"Events: {len(events)} (sorted by severity)",
        "=" * 60,
    ]

    for i, ev in enumerate(events, 1):
        sev = ev.get("severity", 0)
        kind = ev.get("kind", "unknown")
        lat = ev.get("lat", 0)
        lng = ev.get("lng", 0)
        ts_raw = ev.get("ts", "")
        payload = ev.get("payload", {})
        title = payload.get("title", "Untitled")
        url = payload.get("url", "")
        theme = payload.get("gdelt_theme", "")
        articles = payload.get("article_count", 1)
        country = payload.get("country_code", "")

        lines.append(
            f"\n[{i}] Severity {sev}/10 | {kind.upper()} | {country}"
        )
        lines.append(f"    Location: {lat:.3f}°N, {lng:.3f}°E")
        lines.append(f"    Event: {title}")
        lines.append(f"    Theme: {theme} | Articles: {articles} | {ts_raw[:16]}")
        if url:
            lines.append(f"    Source: {url}")

    return "\n".join(lines)


def extract_headline(brief_text: str) -> str:
    """Pull headline from the BLUF section of the brief."""
    # Try to get the BLUF content
    bluf_match = re.search(
        r"###\s*BLUF\s*\n+(.*?)(?=###|\Z)", brief_text, re.DOTALL | re.IGNORECASE
    )
    if bluf_match:
        bluf = bluf_match.group(1).strip()
        # First sentence of BLUF
        first_sentence = bluf.split(".")[0].strip()
        return first_sentence[:200] if first_sentence else bluf[:200]

    # Fallback: pull topic from the header
    header_match = re.search(r"##\s*SENTINEL BRIEF:\s*(.+)", brief_text)
    if header_match:
        return f"SENTINEL BRIEF: {header_match.group(1).strip()}"

    return "MEWR Sentinel Intelligence Brief"


def extract_threat_level(brief_text: str) -> str:
    """Extract threat level from the brief."""
    match = re.search(
        r"\*\*Threat Level:\*\*\s*(LOW|GUARDED|ELEVATED|HIGH|SEVERE)",
        brief_text, re.IGNORECASE
    )
    return match.group(1).upper() if match else "GUARDED"


# ─── Ollama Generation ────────────────────────────────────────────

async def generate_brief(session: aiohttp.ClientSession, events_text: str) -> str:
    """Call Ollama to generate a Sentinel brief from the GDELT events."""
    full_prompt = (
        SENTINEL_SYSTEM_PROMPT
        + "\n\n--- INTELLIGENCE INPUT ---\n"
        + events_text
        + "\n\n--- TASK ---\n"
        "Analyze the above GDELT geopolitical events and produce a SENTINEL BRIEF. "
        "Focus on the highest-severity events and their regional/global implications. "
        "Identify patterns, connections between events, and emerging threat vectors. "
        "Your output must follow the format specified in your instructions exactly."
    )

    log.info(f"Calling Ollama ({OLLAMA_MODEL}) for Sentinel brief generation...")
    try:
        async with session.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": OLLAMA_MODEL,
                "prompt": full_prompt,
                "stream": False,
                "options": {
                    "temperature": 0.35,
                    "num_predict": 1200,
                    "top_p": 0.9,
                }
            },
            timeout=aiohttp.ClientTimeout(total=180),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                brief = data.get("response", "").strip()
                tokens_in = data.get("prompt_eval_count", 0)
                tokens_out = data.get("eval_count", 0)
                log.info(f"Brief generated: {tokens_in}→{tokens_out} tokens, {len(brief)} chars")
                return brief
            else:
                body = await resp.text()
                log.error(f"Ollama error {resp.status}: {body[:300]}")
                return ""
    except asyncio.TimeoutError:
        log.error("Ollama timed out after 180s")
        return ""
    except Exception as e:
        log.error(f"Ollama call failed: {e}")
        return ""


# ─── Publishers ───────────────────────────────────────────────────

async def publish_to_glassbox(session: aiohttp.ClientSession, brief: str,
                               events: list[dict]) -> bool:
    """POST brief to Glassbox server sitrep endpoint."""
    headline = extract_headline(brief)
    threat_level = extract_threat_level(brief)

    payload = {
        "sitrep": {
            "headline": headline,
            "brief": brief,
            "threat_level": threat_level,
            "source": "sentinel_runner",
            "model": OLLAMA_MODEL,
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "anomalies": [
            {
                "id": ev.get("external_id", f"ev_{i}"),
                "lat": ev.get("lat", 0),
                "lng": ev.get("lng", 0),
                "severity": ev.get("severity", 5),
                "kind": ev.get("kind", "event"),
                "summary": (ev.get("payload") or {}).get("title", ""),
            }
            for i, ev in enumerate(events[:10])
        ],
        "correlations": [],
    }

    if DRY_RUN:
        log.info(f"[DRY RUN] Would POST to Glassbox: headline='{headline}', threat={threat_level}")
        return True

    try:
        async with session.post(
            f"{GLASSBOX_URL}/api/glassbox/sitrep/publish",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                log.info(f"Glassbox sitrep published: threat={threat_level}")
                return True
            else:
                body = await resp.text()
                log.warning(f"Glassbox publish failed {resp.status}: {body[:200]}")
                return False
    except Exception as e:
        log.warning(f"Glassbox publish error: {e}")
        return False


async def submit_to_review_queue(session: aiohttp.ClientSession, brief: str,
                                  events: list[dict]) -> str | None:
    """Submit brief to MEWR OS content-review queue for Boss Man review."""
    headline = extract_headline(brief)
    threat_level = extract_threat_level(brief)
    event_count = len(events)
    top_region = (events[0].get("payload") or {}).get("country_code", "Global") if events else "Global"

    payload = {
        "title": f"Sentinel Brief — {threat_level} | {top_region} | {datetime.now(timezone.utc).strftime('%b %d %H:%MZ')}",
        "content": brief,
        "content_type": "brief",
        "agent": "sentinel_analyst",
        "pipeline": "sentinel_runner",
        "tags": ["sentinel", "geopolitical", "intelligence", threat_level.lower()],
        "metadata": {
            "headline": headline,
            "threat_level": threat_level,
            "event_count": event_count,
            "top_region": top_region,
            "model": OLLAMA_MODEL,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    }

    if DRY_RUN:
        log.info(f"[DRY RUN] Would submit to review queue: '{payload['title']}'")
        return "dry_run_item_id"

    try:
        async with session.post(
            f"{MEWR_OS_URL}/api/content-review/submit",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                item_id = (data.get("item") or {}).get("id", "unknown")
                log.info(f"Review queue submission: id={item_id}")
                return item_id
            else:
                body = await resp.text()
                log.warning(f"Review submit failed {resp.status}: {body[:200]}")
                return None
    except Exception as e:
        log.warning(f"Review submit error: {e}")
        return None


async def persist_to_brain(session: aiohttp.ClientSession, brief: str,
                            threat_level: str) -> bool:
    """Write a summary fact to the Brain knowledge base."""
    summary = brief[:400].replace("\n", " ").strip()
    payload = {
        "namespace": "sentinel",
        "subject": "sentinel_analyst",
        "predicate": "produced_brief",
        "object": summary,
        "actor": "sentinel_runner",
    }

    if DRY_RUN:
        log.info("[DRY RUN] Would write to Brain")
        return True

    try:
        async with session.post(
            f"{BRAIN_API_URL}/api/remember",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            ok = resp.status in (200, 201)
            if ok:
                log.info("Brain write-back: sentinel brief stored")
            return ok
    except Exception as e:
        log.debug(f"Brain write-back skipped (Brain API may be offline): {e}")
        return False  # Non-fatal


# ─── Main ─────────────────────────────────────────────────────────

async def run():
    log.info("=" * 60)
    log.info(f"Sentinel Intelligence Runner — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%MZ')}")
    if DRY_RUN:
        log.info("DRY RUN mode — no writes will occur")
    log.info("=" * 60)

    # 1. Load GDELT feed
    events = load_gdelt_feed(GDELT_FEED_PATH, max_events=MAX_EVENTS)
    log.info(f"Loaded {len(events)} events from GDELT feed")

    if not events:
        log.warning("No events in feed — using fallback prompt")

    events_text = format_events_for_analysis(events)

    async with aiohttp.ClientSession() as session:
        # 2. Generate brief via Ollama
        brief = await generate_brief(session, events_text)

        if not brief:
            log.error("Failed to generate brief — aborting")
            return

        log.info(f"Brief generated ({len(brief)} chars)")
        if DRY_RUN or "--verbose" in sys.argv:
            print("\n" + "=" * 60)
            print(brief)
            print("=" * 60 + "\n")

        threat_level = extract_threat_level(brief)
        headline = extract_headline(brief)
        log.info(f"Threat Level: {threat_level}")
        log.info(f"Headline: {headline[:100]}")

        # 3. Publish to Glassbox globe (non-blocking failure)
        gb_ok = await publish_to_glassbox(session, brief, events)
        log.info(f"Glassbox publish: {'OK' if gb_ok else 'FAILED (non-fatal)'}")

        # 4. Submit to review queue
        review_id = await submit_to_review_queue(session, brief, events)
        log.info(f"Review queue: {'id=' + review_id if review_id else 'FAILED (non-fatal)'}")

        # 5. Persist to Brain (best-effort)
        await persist_to_brain(session, brief, threat_level)

    log.info("Sentinel runner complete.")


if __name__ == "__main__":
    asyncio.run(run())
