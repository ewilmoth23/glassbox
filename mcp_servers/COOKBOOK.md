# Glassbox MCP cookbook

Worked examples for the 14 tools across the three MCP servers. Each
recipe is a real investigative pattern — the kind of thing a Claude
Desktop or LangGraph agent would do given a natural-language ask.

| Server | Tools |
|---|---|
| `glassbox-entities`     | viewport, detail, detail_ftm, aliases |
| `glassbox-events`       | search, similar_to, timeseries, in_bbox, algorithm_findings, detail |
| `glassbox-investigation`| brief, match_sanctions, entity_resolution, cross_domain |

Each recipe shows: (1) the human-language ask, (2) the tool sequence,
(3) what the agent gets back, and (4) per-call cost so you can see
how a recipe consumes the rate-limit budget.

> **Cost recap.** Entities + events servers: 300 calls/min/agent
> (1 token per call). Investigation: 30 calls/min/agent (1 token per
> call EXCEPT `brief` which costs 5).

---

## Recipe 1 — "What's happening in the Strait of Hormuz right now?"

Classic operational triage. Agent should:

```
1. events.in_bbox(west=54, south=24, east=58, north=27,
                  time_from=<now-2h>, time_to=<now>)
   → returns events of all types in the Strait
   COST: 1
```

If the agent wants a narrative on top:

```
2. investigation.brief(bbox={west:54,south:24,east:58,north:27},
                       time_range={start:<now-2h>, end:<now>})
   → 200-word LLM-augmented brief
   COST: 5  (LLM-bearing, counts 5× toward the 30/min budget)
```

Total cost: 6 of 30 tokens. Agent has plenty of headroom for follow-ups.

**Variant for derived events only** (skip raw NWS alerts, AQI, news;
focus on what algorithms have flagged):

```
events.algorithm_findings(west=54, south=24, east=58, north=27,
                          hours=2)
COST: 1
```

---

## Recipe 2 — "Is this AIS contact a sanctioned vessel under a
different name?"

Live AIS sometimes shows a vessel with a clean MMSI that's actually
on a sanctions list under a different identifier. The Splink ER
pipeline detects these.

```
1. entities.viewport(bbox=<region>, time_range=<recent>)
   → list of vessels currently in the region
   COST: 1

2. (for each suspicious vessel)
   entities.aliases(entity_id=<vessel uuid>, min_confidence=0.7)
   → Splink-resolved sanctioned-list aliases
   COST: 1 each
```

If aliases match, drill in:

```
3. entities.detail_ftm(entity_id=<vessel uuid>)
   → FollowTheMoney JSON, ready to hand to OCCRP / yente / Aleph
   COST: 1
```

For the matched sanctioned-list hit, also useful:

```
4. investigation.match_sanctions(query=<vessel name or IMO>)
   → which authority (OFAC / EU / UK), which regime, what evidence
   COST: 1
```

Agent has spent 3-5 tokens to map a vessel from "live AIS contact"
to "OFSI-sanctioned shadow-fleet hull, here's the OFAC SDN entry."

---

## Recipe 3 — "Map this vessel's network of contacts"

The new `cross_domain` tool surfaces multi-entity findings — events
where the queried vessel appears alongside another vessel/aircraft.
Useful for transshipment investigations.

```
1. investigation.cross_domain(entity_id=<vessel uuid>, within_hours=168)
   → events where this vessel is in properties.entity_ids
   COST: 1

   Each event has a `partners` array with:
     - entity_id (UUID for follow-up calls)
     - canonical_id (MMSI / IMO / ICAO24)
     - display_name
     - entity_type
```

For each partner, the agent can recurse:

```
2. (for each interesting partner)
   investigation.cross_domain(entity_id=<partner uuid>, within_hours=168)
   investigation.entity_resolution(entity_id=<partner uuid>)
   COST: 1 + 1 = 2 per partner
```

A 2-hop network from one vessel to ~10 partners costs ~25 tokens.
Stays within the 30/min/agent budget.

---

## Recipe 4 — "Find news similar to this incident"

Agent has an event id (e.g. from `algorithm_findings` or
`alerts/timeseries`) and wants to find related news coverage.

```
1. events.detail(event_id=<uuid>)
   → full event row + properties
   COST: 1

2. events.similar_to(event_id=<uuid>, limit=20, within_days=7)
   → top-20 semantically similar events from the last week
   COST: 1
```

If the agent wants to broaden to a free-text query:

```
3. events.search(query="<natural language>", limit=20)
   → semantic search over the embedding index
   COST: 1
```

`events.similar_to` (by id) uses stored embeddings — fast (~50-200ms).
`events.search` (by text) embeds the query on the fly — also fast
now that the warm-up commit shipped.

---

## Recipe 5 — "Rate-of-events trend for shadow fleet"

Operational dashboard pattern: is the rate of detections rising or
falling?

```
1. events.timeseries(hours=72, bucket_minutes=60)
   → 72 hourly buckets, per-event-type counts
   COST: 1
```

The response includes `event_types` + `buckets` + per-type count
arrays parallel to the buckets axis. Agent renders or computes
deltas.

---

## Recipe 6 — "What's our prefilter doing differently if we relax
severity?" (operator workflow, not agent)

Not strictly an MCP recipe, but the A/B shadow workflow that the
shipped scripts make turnkey:

```bash
# Activate the shadow with the shipped sandbox config
bash 09_SETUP_GUIDES/scripts/glassbox/start_shadow_experiment.sh

# Wait 24 hours of real GDELT traffic

# Read results — confusion matrix in the health snapshot:
curl -s http://127.0.0.1:8790/api/v1/health/full | jq '
  .ingesters.items[]
  | select(.layer == "news" and (.source | contains("GDELT 2.0 Bulk")))
  | .prefilter_health.shadow
'

# Or via the Prom counter:
curl -s http://127.0.0.1:8790/api/v1/metrics/prefilter \
  | grep glassbox_prefilter_shadow_outcome_total

# Decide
bash 09_SETUP_GUIDES/scripts/glassbox/stop_shadow_experiment.sh
```

---

## Audit trail — see what every agent has done

Every MCP tool call writes one row to `mcp_audit_log` (Postgres,
migration 004). Useful patterns:

```sql
-- Per-agent activity histogram, last 24h
SELECT agent_id, server_name, tool_name,
       COUNT(*) AS calls,
       AVG(latency_ms)::int AS avg_ms,
       SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failures
FROM mcp_audit_log
WHERE created_at >= NOW() - INTERVAL '24 hours'
GROUP BY agent_id, server_name, tool_name
ORDER BY calls DESC
LIMIT 20;

-- Find the agent that hit the rate-limiter most
SELECT agent_id, COUNT(*) AS rate_limit_429s
FROM mcp_audit_log
WHERE NOT success
  AND error_type = 'RateLimited'
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY agent_id
ORDER BY rate_limit_429s DESC;

-- Most expensive agent (LLM-bearing brief calls)
SELECT agent_id, COUNT(*) AS brief_calls, SUM(latency_ms) AS total_ms
FROM mcp_audit_log
WHERE tool_name = 'glassbox.investigation.brief'
  AND created_at >= NOW() - INTERVAL '24 hours'
GROUP BY agent_id
ORDER BY total_ms DESC;

-- Tool popularity, rolled up across agents
SELECT server_name, tool_name, COUNT(*) AS calls,
       ROUND(AVG(latency_ms)::numeric, 1) AS avg_ms
FROM mcp_audit_log
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY server_name, tool_name
ORDER BY calls DESC;
```

The `payload` JSONB column has the args the agent passed; the
`response_summary` JSONB has the tool's audit summary (result_count,
by_type histogram, etc. — bounded so the table doesn't bloat).

---

## What the audit summary shows for each tool

(Useful when reading the log columns above.)

| Tool | Audit summary keys |
|---|---|
| `entities.viewport` | result_count, types_seen |
| `entities.detail` | entity_type, track_point_count, related_event_count |
| `entities.detail_ftm` | ftm_schema, property_count |
| `entities.aliases` | alias_count, min_confidence, max_alias_confidence |
| `events.search` | result_count, by_type |
| `events.similar_to` | result_count, by_type |
| `events.timeseries` | event_type_count, bucket_count |
| `events.in_bbox` | result_count, filtered_from, by_type |
| `events.algorithm_findings` | result_count, filtered_from, by_type, algorithm_types |
| `events.detail` | event_type, severity, has_geom |
| `investigation.brief` | entity_count, brief_length, brief_preview (120-char) |
| `investigation.match_sanctions` | result_count, by_authority |
| `investigation.entity_resolution` | alias_count, min_confidence |
| `investigation.cross_domain` | result_count, by_type, unique_partner_count |

---

## Common errors and what they mean

| Exception | Meaning | Fix |
|---|---|---|
| `RateLimited(retry_after_sec, agent_id, cost)` | Agent's bucket is empty | Wait `retry_after_sec`, or batch fewer calls |
| `httpx.HTTPStatusError 404` | Entity / event UUID doesn't exist | Validate the id before drilling in |
| `httpx.HTTPStatusError 400` | Bad UUID format | Confirm you're passing the empire UUID, not a canonical_id |
| `httpx.HTTPStatusError 415` | (detail_ftm only) entity type has no FtM mapping | Use `entities.detail` for that type |
| `ValueError: unknown tool: …` | Typo in the tool name | Tool catalog is in this README's table |

---

## Multi-server workflow — the canonical "find shadow fleet" recipe

End-to-end agent workflow that uses tools from all three servers.
Shows how an investigation typically threads:

```
ASK: "Identify any shadow-fleet activity in the Persian Gulf this week."

1. events.algorithm_findings(west=48, south=22, east=58, north=30,
                             hours=168,
                             types=['shadow_fleet_cluster',
                                    'sanctioned_vessel_rendezvous'])
   COST: 1
   → list of cluster/rendezvous events

2. (for each cluster, inspect)
   events.detail(event_id=<cluster uuid>)
   COST: 1 each
   → full properties incl. cluster member entity_ids

3. (for each member entity)
   investigation.cross_domain(entity_id=<vessel uuid>, within_hours=168)
   COST: 1 each
   → that vessel's network of partners over the week

4. (for any unfamiliar partner)
   entities.detail_ftm(entity_id=<partner uuid>)
   investigation.entity_resolution(entity_id=<partner uuid>)
   COST: 1 + 1 each
   → FtM shape + Splink-resolved aliases

5. (synthesize)
   investigation.brief(bbox=<gulf>, time_range=<week>)
   COST: 5
   → LLM narrative tying the pattern together for the analyst

Total for a typical 1-cluster-with-3-members investigation:
  1 + 3 + 3 + 6 + 5 = 18 of 30 investigation tokens.
  Plus 7 of 300 entities tokens.
  Plus 4 of 300 events tokens.
  Per-minute budget all servers; comfortably within rate limits.
```

The pattern is: discover via algorithm_findings → inspect via detail
→ map network via cross_domain → enrich via FtM/aliases → synthesize
via brief. Five clean stages.
