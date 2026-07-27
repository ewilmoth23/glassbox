# Glassbox AI — integration plan

**Glassbox** is Fulcrum Labs' 3-D OSINT globe at `mewrcreate.com/glassbox`. 80+ live data layers (USGS earthquakes, GDELT conflict events, NOAA weather, ADS-B flights, Cloudflare Radar, space weather, nuclear facilities, and more), plus an existing Cognitive Fusion Engine (Session 71) that correlates cross-layer signals, and an AI Intelligence Pipeline (Session 73) that emits threat assessments, hotspot predictions, and narrative analyses.

Today Glassbox shows data and runs analysis *transiently* — every globe load starts from scratch. **The gap: no persistent memory, no feedback loop, no accumulated wisdom.** Same prediction bug that happened last month will happen again because nothing remembers.

This folder closes that gap.

---

## What "AI-powered Glassbox" means concretely

Four capabilities, all backed by the Holding Brain (`20_HOLDING_BRAIN/memory/brain.db`):

1. **Persistent intelligence.** Every threat assessment, hotspot prediction, narrative analysis, and fusion correlation gets written to the Brain with `namespace="glassbox"`. They survive globe reloads, cron cycles, and session endings.
2. **Graded predictions.** Hotspot predictions have a `due_at`. After the due date, a grader checks against real events (USGS magnitude spikes, GDELT event counts, news) and writes outcomes. Calibration emerges naturally from reality.
3. **Semantic recall for operators.** "Show me every time we called a Pacific tsunami risk in the last 90 days" → Brain returns graded predictions with outcomes.
4. **Cross-agency citations.** MEWR Sentinel (geopolitics agency) can cite Glassbox fusion events directly; Glassbox anomalies can trigger Sentinel alerts. Shared namespace, same source of truth.

---

## Architecture

```
    ┌───────────────────────────────────────────────────────────────────┐
    │                      The live globe (CesiumJS)                     │
    │  Fetches 80+ feeds in the browser, renders to 3-D globe            │
    │  mewrcreate.com/glassbox                                           │
    └────────────────────────────┬──────────────────────────────────────┘
                                 │
                                 │  n8n workflows (Session 73-75) run
                                 │  server-side analysis on the same feeds
                                 ▼
    ┌────────────────────────────────────────────────────────────────────┐
    │  Cloudflare Worker endpoints (v8+)                                 │
    │    /api/intel/threat-assessment    /api/intel/hotspot-prediction   │
    │    /api/intel/narrative-intel      /api/intel/correlation-analysis │
    │    /api/intel/anomaly-report       /api/intel/daily-briefing       │
    └────────────────────────────┬──────────────────────────────────────┘
                                 │
                                 │  glassbox_bridge.py polls every 15 min
                                 │  (new artefacts only → Brain)
                                 ▼
    ┌────────────────────────────────────────────────────────────────────┐
    │        Holding Brain (namespace='glassbox')                        │
    │    • predictions (hotspot-prediction, threat-forecast)             │
    │    • facts (narratives, anomaly reports, daily briefings)          │
    │    • patterns (recurring cascade scenarios)                        │
    │    • events (timeline of everything Glassbox has ever said)        │
    └────────────────────────────┬──────────────────────────────────────┘
                                 │
                    ┌────────────┼────────────┬───────────────┐
                    ▼            ▼            ▼               ▼
           ┌──────────────┐ ┌─────────┐ ┌──────────┐ ┌─────────────────┐
           │Mission Control│ │Sentinel │ │ Best Bets│ │ Future: enterprise │
           │ dashboard tile│ │ can cite│ │ can weight│ │ Glassbox API     │
           │               │ │ fusion   │ │ by global │ │ ($$$ product)  │
           │               │ │ events   │ │ risk climate│ │                │
           └──────────────┘ └─────────┘ └──────────┘ └─────────────────┘
```

The key insight: **Glassbox already produces intelligence (via the existing n8n workflows from Session 73-75). The bridge just makes it durable.**

---

## What's in this folder

| File | Purpose | Run cadence |
|---|---|---|
| `glassbox_bridge.py` | Polls `/api/intel/*` endpoints, diffs against Brain, writes new records | every 15 min (cron / launchd) |
| `glassbox_grader.py` | Grades past predictions whose `due_at` has passed, records outcome | hourly |
| `README.md` | This file | — |

Future additions:
- `glassbox_anomaly_detector.py` — runs Brain's `similar_past_predictions` against live Cognitive Fusion output to surface novel vs recurring patterns
- `glassbox_api.py` — exposes a curated Brain view as a paid B2B API (Fulcrum Labs revenue lane #3)

---

## Start

**One-time:** pull the embedding model + init the Brain (both already done).

**Every cron cycle:**
```bash
cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC"
python3 21_GLASSBOX_AI/glassbox_bridge.py
```

**Every hour (grader):**
```bash
python3 21_GLASSBOX_AI/glassbox_grader.py
```

**Persistent launch (recommended):** add to your Mac Mini `launchd` or cron. See `20_HOLDING_BRAIN/ORG_STRUCTURE.md` for the full persistent-service pattern.

---

## Revenue path

Glassbox today is a free marketing asset for Fulcrum Labs. Once the Brain-backed layer has 30+ days of graded predictions, Glassbox becomes a credible enterprise product:

| Tier | Price | What you get |
|---|---|---|
| Free | $0 | Current public globe, no history, no predictions |
| Pro | $49/mo | Predicted hotspots with confidence, daily briefing email, 30-day history |
| Enterprise | $499/mo | API access, custom regions, webhook alerts, grading methodology transparency, full brain access |

The dollars don't come from traffic — they come from the *credibility* that only a public, graded, searchable prediction history produces. The bridge is what generates that history.
