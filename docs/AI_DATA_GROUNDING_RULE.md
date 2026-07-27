# AI data-grounding rule — LLMs operate on the graph, not raw feeds

## The rule

> **LLMs in Glassbox must operate on structured graph data — entity properties, event records, aggregated counts, deterministic-brief output, or brain-memory hits — NEVER on raw upstream payloads (HTML, RSS XML, raw API JSON).**

The reason is twofold:
1. **Trust.** A normalized event row with `title`, `severity`, `lat`, `lng`, `layer` has been through the ingester's parser and the classifier. The LLM is reasoning over what we already *know* about the event, not what some upstream wrote in arbitrary marketing copy.
2. **Determinism.** Two runs of the LLM against the same graph state should produce closely-shaped outputs. If the LLM saw raw HTML, the formatting noise (timestamps, ads, layout) would change between scrapes and the model would chase phantom variation.

## Verified call sites (audit 2026-05-20 per backlog P3-O)

All 5 LLM call sites in the production code path were inspected. Every one passes the rule. Snapshot below — file:line + what gets passed to the LLM.

### 1. `21_GLASSBOX_AI/brief.py:1003` — analyst note (one-sentence prompt-to-act)

```python
text = await generate_text(
    prompt=_ANALYST_NOTE_PROMPT_TEMPLATE.format(deterministic_brief=deterministic_brief),
    ...
)
```

- `deterministic_brief` is the output of `generate_brief(viewport_response)` — a deterministic function that aggregates `viewport_response` (the API's structured snapshot of entities + events).
- Prompt template explicitly says: *"Do NOT add numbers or names not in the data. Do NOT pad with adjectives."*
- **What goes in:** structured aggregate.
- **What does NOT go in:** raw upstream feeds, raw scraped HTML.

### 2. `21_GLASSBOX_AI/forecaster.py` — 48-hour hotspot forecaster (via `narrate_hotspot`)

```python
prompt = json.dumps({
    "task": "...forecast next 48 hours...",
    "return_format": "JSON only: {...}",
    "hotspot": {
        "layer": hotspot["layer"],
        "region": hotspot["region"],
        "recency_weighted_score": hotspot["score"],
        "max_historical_severity": hotspot["max_historical_severity"],
        "evidence_count": hotspot["evidence_count"],
        "evidence_trail": hotspot["evidence_trail"],
    },
}, separators=(",", ":"))
```

- All hotspot fields come from `score_hotspots()` — a DB aggregation over historic anomalies grouped by `(layer, region)`.
- `evidence_trail` is itself a structured array of normalized events, not raw upstream text.
- **What goes in:** structured aggregate.

### 3. `21_GLASSBOX_AI/intelligence_loop.py:227` — SITREP composer (via `compose_sitrep`)

```python
prompt = _build_sitrep_prompt(anomalies, correlations, layer_counts, server_health)
# ... contains structured fields:
#   - total_events_last_cycle (int)
#   - events_per_layer (dict[str, int])
#   - top_anomalies (list, capped at 8 structured records)
#   - top_correlations (list, capped at 6 structured records)
#   - ingester_health (list of {layer, health, tracked, last_error})
```

- Every input is either a numeric aggregate or a structured record from the anomaly + correlation detection pipelines.
- **What goes in:** structured aggregate.

### 4. `21_GLASSBOX_AI/glassbox_server.py:3093` — `/api/intel/query` brain-grounded answer

```python
ctx = "\n".join([f"- [#{h['id']} · {h['namespace']}] {h['object']}" for h in hits])
system = "You are Glassbox... Answer the user's question using ONLY the memory hits provided. "
         "If the hits don't answer the question, say so honestly and don't invent information."
```

- `hits` are brain memory records — `(id, namespace, object)` rows from the brain's vector store. `object` content is what was previously *processed and stored* (intel notes, prior answers, structured summaries), not raw external feeds.
- Prompt explicitly forbids invention beyond the provided hits.
- **What goes in:** brain memory (graph-on-graph).

### 5. `21_GLASSBOX_AI/glassbox_server.py:3209` — Globe-context intel query

```python
context_lines = [
    f"LIVE GLASSBOX GLOBE — {ts}",
    f"Total cached events: {total_events}",
    f"Active layers: {', '.join(layer_summary[:12])}",
    "TOP SEVERITY EVENTS:",
]
for ev in top_severity:
    context_lines.append(
        f"  [{ev['layer'].upper()}] {ev['title']} "
        f"(severity:{ev['severity']}/5, lat:{ev['lat']} lng:{ev['lng']})"
    )
```

- `top_severity` is built from the in-memory event cache (`_hot_cache`) — already normalized event records with structured fields.
- `ev.title` is the ingester's normalized title (typically 80-char truncation of an already-classified summary), not raw upstream payload.
- **What goes in:** structured event records.

## Outputs are also structured

Two of the five call sites use `llm_json.parse_with_schema(...)` to validate the LLM's response against a pydantic schema (`_ForecastSchema` in forecaster.py, `_SitrepSchema` in intelligence_loop.py). If the LLM emits malformed JSON, the parse fails gracefully and a typed fallback is returned. This is the output-side mirror of the input-side rule: structured in, structured out.

## What this rule rules OUT

For future operators or contributors: do not introduce LLM calls that take any of the following as direct input:
- Raw HTML pages (e.g., the result of an `httpx.get()` against a news site)
- Raw RSS / Atom XML
- Raw upstream API JSON payloads (e.g., the raw `adsb.lol` aircraft state response)
- Raw user-submitted text blobs (these need to pass through the classifier first)
- Anything that hasn't been through a Glassbox ingester normalization step

If you need an LLM to summarize an article: first persist it through an ingester into the event table, then have the LLM read the persisted record.

## Where this lives in the ingester/algorithm chain

```
Upstream feed (HTML / RSS / JSON)
    ↓
Ingester normalize()     ← raw → structured. Last point raw payload exists.
    ↓
GlassboxEvent (dataclass with typed fields)
    ↓
writers.py UPSERT to entity / event / position_track
    ↓
DB (PostGIS / Timescale / pgvector)
    ↓
Algorithms (proximity, dark_ship, port_call, etc.) — read structured DB rows
    ↓
API layer (api_v1.py + glassbox_server.py page handlers) — assemble structured responses
    ↓
LLM call sites (the 5 above) — operate on the structured slice
    ↓
LLM output → schema-bound parse → structured response back to caller
```

The rule lives between "Algorithms" and "API layer" on one side, and the "LLM call sites" tier. Anything ABOVE that line has been classifier-touched; anything ABOVE has been DB-persisted. The LLM never reaches back through any of that.

## Future verification

If you add a new LLM call site:
1. Show that the prompt is built from structured fields, NOT a raw upstream response.
2. Add a one-liner here pointing at the new file:line.
3. If you must operate on raw text (e.g., a one-off semantic classifier), ensure it's behind an ingester boundary — i.e. its caller is `ingesters/<foo>.py::normalize`, not a downstream consumer.

## See also

- `21_GLASSBOX_AI/llm_ollama.py` — `generate_text` + `generate_json` (the two helpers all sites route through)
- `21_GLASSBOX_AI/llm_json.py` — `parse_with_schema` (output-side validation)
- `21_GLASSBOX_AI/ingesters/base.py::GlassboxEvent` — the canonical entry-point dataclass; everything downstream is structured-from-here
