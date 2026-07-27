# Glassbox MCP servers

First-class agent access to Glassbox's `/api/v1/*` REST surface, exposed
via the [Model Context Protocol](https://modelcontextprotocol.io/).
Built per [HANDOFF_04](../../00_MASTER_DOCS/research_2026_05_09/HANDOFF_04_mcp_servers.md)
(R2). **All three servers live as of 2026-05-10.**

| Server | Status | Tools | Cost |
|---|---|---|---|
| `glassbox-entities-mcp`     | ✅ live | viewport, detail, detail_ftm, aliases | cheap |
| `glassbox-events-mcp`       | ✅ live | search, similar_to, timeseries, in_bbox, algorithm_findings, detail | cheap |
| `glassbox-investigation-mcp` | ✅ live | brief, match_sanctions, entity_resolution, cross_domain | normal–expensive |

## Architecture (empire fit)

Per the empire's "single-Mac, in-process for v1.0" call (same logic as
the deferred NATS streaming spine):

- **REST not GraphQL** (per RESEARCH_INTEGRATION_PROPOSAL §8). Servers
  wrap `/api/v1/*` via `httpx`.
- **Separate venv at `.venv/`** (Python 3.11+; `mcp` SDK requires ≥3.10
  and the main `21_GLASSBOX_AI/.venv/` stays on 3.9 with its existing
  asyncpg / sentence-transformers / splink dep tree intact).
- **stdio transport.** Servers are invoked as subprocesses by MCP clients
  (Claude Desktop, Cowork-Claude, future LangGraph). No long-running
  daemon plist — one process per active client session.
- **Audit log in Postgres** (`mcp_audit_log`, applied by migration 004).
  Every tool call writes one row: server / tool / agent_id / latency /
  cost_class / success / payload / response summary.
- **In-process rate limit** (when added — not in v1 slice). No Redis.

## Bootstrap

```bash
# One-time venv setup (Python 3.11+)
/opt/homebrew/bin/python3.11 -m venv 21_GLASSBOX_AI/mcp_servers/.venv
21_GLASSBOX_AI/mcp_servers/.venv/bin/pip install \
    'mcp>=1.0' 'httpx>=0.27' 'asyncpg>=0.29' 'pydantic>=2.0' \
    'pytest>=8.0' 'pytest-asyncio>=0.23' 'python-dotenv>=1.0'

# Apply the audit-log migration (one-time)
21_GLASSBOX_AI/.venv/bin/python -c "
import asyncio, os
from pathlib import Path
for line in Path('.env.glassbox').read_text().splitlines():
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1); os.environ.setdefault(k, v.strip().strip('\"\\''))
import asyncpg
async def main():
    conn = await asyncpg.connect(host=os.environ['GLASSBOX_DB_HOST'],
        port=int(os.environ['GLASSBOX_DB_PORT']),
        database=os.environ['GLASSBOX_DB_NAME'],
        user=os.environ['GLASSBOX_DB_USER'],
        password=os.environ['GLASSBOX_DB_PASSWORD'])
    await conn.execute(Path('infra/postgres/migrations/004_mcp_audit_log.sql').read_text())
    await conn.close()
asyncio.run(main())
"
```

## Wiring into Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "glassbox-entities": {
      "command": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI/mcp_servers/.venv/bin/python",
      "args": ["-m", "mcp_servers.entities.server"],
      "env": {
        "PYTHONPATH": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI",
        "GLASSBOX_API_URL": "http://127.0.0.1:8790",
        "GLASSBOX_DB_ENV_FILE": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/.env.glassbox",
        "GLASSBOX_MCP_AGENT_ID": "claude-desktop"
      }
    },
    "glassbox-events": {
      "command": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI/mcp_servers/.venv/bin/python",
      "args": ["-m", "mcp_servers.events.server"],
      "env": {
        "PYTHONPATH": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI",
        "GLASSBOX_API_URL": "http://127.0.0.1:8790",
        "GLASSBOX_DB_ENV_FILE": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/.env.glassbox",
        "GLASSBOX_MCP_AGENT_ID": "claude-desktop"
      }
    },
    "glassbox-investigation": {
      "command": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI/mcp_servers/.venv/bin/python",
      "args": ["-m", "mcp_servers.investigation.server"],
      "env": {
        "PYTHONPATH": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI",
        "GLASSBOX_API_URL": "http://127.0.0.1:8790",
        "GLASSBOX_DB_ENV_FILE": "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/.env.glassbox",
        "GLASSBOX_MCP_AGENT_ID": "claude-desktop"
      }
    }
  }
}
```

The `glassbox-server` daemon must be running (the REST endpoints power
the MCP tools). Restart Claude Desktop after editing the config.

## Tests

```bash
cd 21_GLASSBOX_AI/mcp_servers
.venv/bin/python -m pytest tests/ -v
```

82 tests today (21 entities + 34 events + 22 investigation + 5
helper/integration); mock the REST client + bypass the audit DB pool
gracefully so the suite runs in 0.3s without external dependencies.

### Resolved upstream issue (2026-05-10 follow-up)

`glassbox.events.search` (`/api/v1/events/similar?q=`) was timing out
at 30s on the first call because sentence-transformers was lazy-loaded
on the first embed. The fix shipped as
`embeddings.warm_up()` + an `_warm_embeddings()` startup task in
`glassbox_server.py` that calls it via `asyncio.to_thread` so the
event loop stays unblocked while the 80MB MiniLM model loads.
Operator must reload the daemon (`launchctl unload …; launchctl
load …`) to pick up the change. After reload, the
`[embeddings.warmup] sentence-transformers warmed in <ms>ms` log
line confirms warm-up completed off-loop. Set
`GLASSBOX_EMBED_WARMUP_DISABLED=1` to skip in test boots.

## Ops

```bash
# Per-agent audit summary (last 24h)
psql -d glassbox -c "
  SELECT agent_id, server_name, tool_name, COUNT(*) AS calls,
         AVG(latency_ms)::int AS avg_ms,
         SUM(CASE WHEN success THEN 0 ELSE 1 END) AS failures
  FROM mcp_audit_log
  WHERE created_at >= NOW() - INTERVAL '24 hours'
  GROUP BY agent_id, server_name, tool_name
  ORDER BY calls DESC;"
```

## What's next

- ✅ `glassbox-events-mcp` — shipped 2026-05-10 (search by text +
  by event-id similarity + alerts timeseries).
- ✅ In-process token-bucket rate limiter — shipped 2026-05-10
  (300 calls/min/agent on entities + events).
- ✅ `glassbox-investigation-mcp` — shipped 2026-05-10 with brief +
  match_sanctions + entity_resolution tools. cost_class='expensive'
  on brief which counts 5× toward the 30/min/agent budget.
- ⏳ Investigation server's other 2 spec'd tools (`cross_domain`,
  `nl_query`) need infrastructure that doesn't exist yet — see the
  server's docstring for what's missing.
- ✅ `events.in_bbox` — shipped 2026-05-10 (wraps `/api/v1/viewport`
  with `types=infrastructure` to skip the LATERAL track join + applies
  optional `event_types` whitelist client-side; 77ms global-bbox
  upstream query in live testing).
- ✅ `events.algorithm_findings` — shipped 2026-05-10 (wraps in_bbox
  with the 14-type algorithm whitelist prefilled; default global
  bbox + 24h window; 1671 derived events / 24h in live testing).
- ✅ `events.detail` — shipped 2026-05-10 (paired with new
  `GET /api/v1/event/{id}` endpoint; full event row with geom
  decomposed + properties normalized; embedding column excluded).
- ✅ Warm sentence-transformers at server startup — shipped 2026-05-10
  (see "Resolved upstream issue" above).
- ✅ `entities.aliases` — shipped 2026-05-10 (Splink ER edges).
- ✅ `investigation.cross_domain` — shipped 2026-05-10 with new
  `/api/v1/entities/{id}/cross_domain` endpoint surfacing
  multi-entity algorithm findings + resolved partner metadata.
- ⏳ Investigation-server remaining deferred: `nl_query` (needs
  LangGraph or query planner — that's R12), `search_documents`
  (needs OpenAleph; multi-day with prereqs).
- ⏳ launchd plist if/when we adopt long-running TCP-served MCP
  (today stdio-per-client-session is the right fit).
