"""
Glassbox ingester base — the contract every data source must satisfy.

Every layer on the globe (planes, ships, earthquakes, social, weather, ...) is
produced by exactly one Ingester subclass. The subclass owns:

    - layer:              short lowercase name matching the client-side layer key
    - source:             human-readable attribution string
    - poll_interval_sec:  how often fetch() is called

and implements:

    async def fetch(self) -> List[Dict[str, Any]]:
        # hit the upstream API / scrape / firehose

    def normalize(self, raw_items) -> List[GlassboxEvent]:
        # turn raw source shape into canonical GlassboxEvent

The base class handles dedup, broadcasting, polling, error capture, and exposes
a status() endpoint that the server surfaces to Mission Control.

Why this exists:
    V1 Glassbox made 80+ third-party calls from the browser. CORS failures,
    rate-limiting, no persistence. V2 centralizes all fetching here so the Mac
    Mini is the only API caller and clients get a uniform real-time stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional


# ─── The canonical event shape ────────────────────────────────────────────

@dataclass
class GlassboxEvent:
    """Every point of intelligence on the globe boils down to this.

    The Loop integration (2026-04-25) added 5 prediction-market classification
    fields below the core spatial/temporal data. They default to safe values so
    every existing ingester keeps working without modification — the classifier
    populates them later in the pipeline.
    """
    layer: str                         # "planes" | "ships" | "earthquakes" | ...
    external_id: str                   # source-supplied unique id (ICAO, MMSI, USGS id...)
    kind: str                          # "position" | "alert" | "state" | "outage"
    lat: float
    lng: float
    ts: str                            # ISO-8601 UTC, when observed
    severity: int = 0                  # 0-10 (0 = ambient, 10 = critical)
    altitude_m: Optional[float] = None # meters above sea level, if relevant
    heading_deg: Optional[float] = None
    velocity_ms: Optional[float] = None
    source: str = ""                   # which upstream reported it
    payload: Dict[str, Any] = field(default_factory=dict)

    # ─── 5-dim prediction-market classification (The Loop) ───────────
    # Populated by core/event_classifier.py after normalize(). Ingesters
    # that pre-classify their own output (e.g. sports news mapping to
    # specific games) can set these directly in normalize().
    market_tags: List[str] = field(default_factory=list)
    """Prediction-market identifiers this event might affect.
    Format: '<source>:<key>' or freeform tag string.
    Examples: 'NFL:KC@DEN:total_points', 'weather:CO:storm',
    'election:US:congress_d3', 'kalshi:HURRICANE-2026'.
    Empty list = not market-relevant. The Loop uses this to fan out a
    single event to N markets."""

    severity_for_market: int = 0
    """0-10 — distinct from `severity` (general humanitarian/safety scale).
    Measures how much THIS event should move a related market.
    0 = ambient/no signal, 10 = move-the-line urgent (e.g. star QB ruled out
    30 min before kickoff). Decoupled from `severity` because a 9.0 earthquake
    in an unpopulated region is severity=10 but severity_for_market=2."""

    geocode_quality: str = "unknown"
    """How precise the (lat, lng) is. Drives geo-market matching confidence.
    Bucket values: 'exact' (GPS-grade), 'city' (~5km), 'region' (~50km),
    'country', 'unknown'. Matters when matching to geographic markets —
    a region-quality storm location can't confidently trigger a city-level
    weather market."""

    domain: str = "unknown"
    """High-level grouping for fan-out routing.
    Bucket values: 'sports', 'politics', 'weather', 'macro', 'geo', 'tech',
    'unknown'. Cheap pre-filter before the more expensive market_tags lookup."""

    decay_half_life_min: int = 60
    """Minutes after which this signal's market relevance halves.
    Earthquakes fade fast (~30min — markets reprice quickly). Election polls
    last days (~1440min). Default 60 is the middle ground."""

    @property
    def id(self) -> str:
        """Deterministic dedup id across cycles: layer + external_id."""
        return f"{self.layer}:{self.external_id}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ─── The ingester contract ────────────────────────────────────────────────

BroadcastFn = Callable[[GlassboxEvent], None]
# Phase 1 (2026-05-07): db_writer hook. Async function that takes a list of
# events post-classify and persists them to Postgres. Optional; ingesters that
# don't supply one continue working unchanged (the durable archive is opt-in
# per-ingester as we migrate them in Phase 1 → Phase 2).
DbWriterFn = Callable[[List[GlassboxEvent]], Awaitable[int]]


class Ingester:
    """Abstract base. Subclasses override fetch() and normalize()."""

    # Override in subclass — these are identity
    layer: str = ""
    source: str = ""
    poll_interval_sec: float = 30.0

    # source_id maps the ingester to its row in infra/sources.yaml. The
    # backend startup gate (sources_registry.gate_ingester) refuses to start
    # any ingester whose source_id is missing from the registry, disabled,
    # or marked commercial_use_ok=false (in v1.0).
    #
    # Compound ingesters that pull from multiple sources (e.g. ships pulls
    # from Digitraffic + DMI + BarentsWatch) should set source_id to the
    # PRIMARY source and use a class attribute `additional_source_ids` for
    # the rest. The gate checks all of them.
    source_id: str = ""
    additional_source_ids: tuple = ()  # extra rows to gate against

    # smoke_mode = True signals this ingester is being run by the smoke
    # test script for verification, NOT production. Each ingester should
    # reduce work in smoke mode: fewer tiles, fewer queries, capped
    # result counts. This is what makes the smoke test ~30 seconds total
    # instead of 7 minutes (which is what full production pulls take when
    # done sequentially in one process).
    #
    # Production server (glassbox_server.py) instantiates with smoke_mode=False
    # (default). Smoke test (smoke_test_ingesters.py) sets smoke_mode=True.
    smoke_mode: bool = False

    # Optional per-ingester override of the SLA-breach threshold used by
    # api_v1.build_health_snapshot. Default None means "use the standard
    # formula (sla_multiplier × poll_interval_sec, floored at 60s)".
    # Streaming ingesters (websocket-style) whose `cycle()` legitimately
    # spans many minutes set this explicitly so they don't perpetually
    # trip the formula's "haven't called fetch in 90s" check.
    sla_breach_threshold_sec: Optional[float] = None

    def __init__(
        self,
        broadcaster: Optional[BroadcastFn] = None,
        logger: Optional[logging.Logger] = None,
        classifier: Optional[Any] = None,
        smoke_mode: bool = False,
        db_writer: Optional[DbWriterFn] = None,
    ) -> None:
        self._broadcaster = broadcaster
        # Phase 1 (2026-05-07): optional async function that persists events
        # to Postgres. None = no durable archive for this ingester (default
        # for any ingester not yet migrated to dual-write).
        self._db_writer = db_writer
        # Diagnostics for the durable-archive write path. Surfaced via status().
        self.last_db_write_count: int = 0
        self.last_db_write_ms: Optional[int] = None
        self.db_write_failures: int = 0
        self.last_db_error: Optional[str] = None
        # smoke_mode flag — set True when this ingester is being run by
        # smoke_test_ingesters.py for verification, not production. Each
        # ingester subclass checks self.smoke_mode and reduces work
        # accordingly (fewer tiles, single query, capped result count).
        self.smoke_mode = smoke_mode
        # The Loop integration (Step 3): optional EventClassifier injected at
        # construction. If present, cycle() runs classifier.apply(ev) on each
        # event AFTER dedup (so unchanged events don't burn LLM cycles).
        # None = backward-compat behavior (events get default classification).
        self._classifier = classifier
        # external_id -> content hash (detect when an entity *changes*, not just exists)
        self._dedup: Dict[str, str] = {}
        self._running = False
        # When this ingester was constructed. Used by the SLA monitor's
        # "first-cycle grace period" so a stream ingester with a 5-min
        # batch interval doesn't get flagged as breaching for the
        # first 5 min after a daemon reload.
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.last_fetch_ts: Optional[str] = None
        self.last_fetch_count: int = 0
        # last_emit_* tracks broadcast (post-dedup) — distinguishes "fetched but
        # all duplicates" from "didn't fetch at all". Surfaced via /api/glassbox/diagnostic.
        self.last_emit_ts: Optional[str] = None
        self.last_emit_count: int = 0
        self.last_cycle_ms: Optional[int] = None
        self.last_error: Optional[str] = None
        self.cycles_run: int = 0
        self.cycles_failed: int = 0
        # Diagnostics on classifier health — Mission Control surfaces this
        self.classifier_failures: int = 0
        self.last_classifier_error: Optional[str] = None
        self.log = logger or logging.getLogger(
            f"ingester.{self.layer or self.__class__.__name__}"
        )

    # ─── Abstract — subclasses implement ──────────────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        """Pull raw data from the source. Return list of dicts."""
        raise NotImplementedError

    def normalize(self, raw_items: List[Dict[str, Any]]) -> List[GlassboxEvent]:
        """Convert raw source shape into canonical GlassboxEvent list."""
        raise NotImplementedError

    # ─── Shared machinery ─────────────────────────────────────────────

    def _content_hash(self, event: GlassboxEvent) -> str:
        """Coarse hash — position rounded to ~11m so tiny jitter doesn't spam."""
        payload = {
            "lat": round(event.lat, 4),
            "lng": round(event.lng, 4),
            "severity": event.severity,
            "kind": event.kind,
            "alt": round(event.altitude_m, -1) if event.altitude_m else None,
        }
        return hashlib.md5(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12]

    def dedup(self, events: List[GlassboxEvent]) -> List[GlassboxEvent]:
        """Return only events that are new OR have changed since last cycle."""
        out: List[GlassboxEvent] = []
        for ev in events:
            key = ev.id
            h = self._content_hash(ev)
            if self._dedup.get(key) != h:
                self._dedup[key] = h
                out.append(ev)
        return out

    def broadcast(self, events: Iterable[GlassboxEvent]) -> int:
        """Broadcast all events. Prefers batch (single subscriber pass) when the
        registered broadcaster supports it. Falls back to per-event for old
        broadcasters. Review finding: 50-100× throughput improvement on satellite
        cycles emitting 1000+ changes."""
        if not self._broadcaster:
            return 0
        events_list = list(events)
        if not events_list:
            return 0
        # Try batch path first — server's _broadcast is polymorphic.
        try:
            self._broadcaster(events_list)
            return len(events_list)
        except TypeError:
            # Old broadcaster signature — fall back to per-event
            pass
        except Exception as e:
            self.log.warning(f"broadcast batch failed (falling back per-event): {e}")
        count = 0
        for ev in events_list:
            try:
                self._broadcaster(ev)
                count += 1
            except Exception as e:
                self.log.warning(f"broadcast failed for {ev.id}: {e}")
        return count

    def classify(self, events: Iterable[GlassboxEvent]) -> List[GlassboxEvent]:
        """Apply The Loop classification (5 prediction-market dimensions) to
        each event. No-op if no classifier was injected.

        Defensive: a classifier exception (Ollama timeout, bug in heuristics,
        etc.) must NEVER break the SSE pipeline. The cycle continues with
        whatever classification each event has — including defaults if the
        classifier failed before populating the fields.
        """
        events_list = list(events)
        if self._classifier is None:
            return events_list
        for ev in events_list:
            try:
                self._classifier.apply(ev)
            except Exception as e:
                self.classifier_failures += 1
                self.last_classifier_error = f"{type(e).__name__}: {e}"
                # First failure logs at warning, subsequent at debug to avoid spam
                level = logging.WARNING if self.classifier_failures == 1 else logging.DEBUG
                self.log.log(level, f"classifier failed for {ev.id}: {e}")
        return events_list

    async def cycle(self) -> int:
        """One fetch → normalize → dedup → CLASSIFY → broadcast cycle.
        Returns count broadcast.

        Classification runs AFTER dedup so unchanged events (re-emitted by the
        upstream API every poll) don't burn Ollama cycles. The classifier's
        own internal cache deduplicates within TTL too.
        """
        t0 = time.time()
        self.last_error = None
        self.cycles_run += 1
        try:
            raw = await self.fetch()
            events = self.normalize(raw)
            changed = self.dedup(events)
            classified = self.classify(changed)
            broadcast_count = self.broadcast(classified)
            self.last_fetch_ts = datetime.now(timezone.utc).isoformat()
            self.last_fetch_count = len(changed)
            # Track emit separately — only stamp last_emit_ts when something
            # actually broadcast. Lets diagnostic distinguish silent dedup
            # from upstream fetch failure.
            if broadcast_count > 0:
                self.last_emit_ts = self.last_fetch_ts
                self.last_emit_count = broadcast_count
            # Phase 1 (2026-05-07): durable-archive write to Postgres.
            # AFTER broadcast — SSE clients see events immediately; DB write
            # is best-effort. A DB outage cannot break the live broadcast.
            if self._db_writer is not None and classified:
                t_db = time.time()
                try:
                    written = await self._db_writer(classified)
                    self.last_db_write_count = written
                    self.last_db_write_ms = int((time.time() - t_db) * 1000)
                except Exception as e:
                    self.db_write_failures += 1
                    self.last_db_error = f"{type(e).__name__}: {e}"
                    self.last_db_write_ms = int((time.time() - t_db) * 1000)
                    level = logging.WARNING if self.db_write_failures == 1 else logging.DEBUG
                    self.log.log(level, f"db_writer failed: {self.last_db_error}")
            self.last_cycle_ms = int((time.time() - t0) * 1000)
            return broadcast_count
        except Exception as e:
            self.cycles_failed += 1
            self.last_error = f"{type(e).__name__}: {e}"
            self.last_cycle_ms = int((time.time() - t0) * 1000)
            self.log.warning(f"cycle failed: {self.last_error}")
            return 0

    async def run_forever(self) -> None:
        """Background poll loop. Spawn as asyncio task from the server."""
        self._running = True
        self.log.info(
            f"[{self.layer}] ingester up — interval={self.poll_interval_sec}s "
            f"source={self.source}"
        )
        while self._running:
            t0 = time.time()
            await self.cycle()
            elapsed = time.time() - t0
            # Always sleep at least 1s to avoid tight-loops on persistent errors
            await asyncio.sleep(max(1.0, self.poll_interval_sec - elapsed))

    def stop(self) -> None:
        self._running = False

    def status(self) -> Dict[str, Any]:
        """Snapshot for /api/health and Mission Control."""
        health = "ok"
        if self.last_error:
            health = "degraded" if self.last_fetch_count > 0 else "down"
        return {
            "layer": self.layer,
            "source": self.source,
            "poll_interval_sec": self.poll_interval_sec,
            "running": self._running,
            "health": health,
            "created_at": self.created_at,
            "last_fetch_ts": self.last_fetch_ts,
            "last_fetch_count": self.last_fetch_count,
            "last_emit_ts": self.last_emit_ts,
            "last_emit_count": self.last_emit_count,
            "last_cycle_ms": self.last_cycle_ms,
            "tracked_entities": len(self._dedup),
            "cycles_run": self.cycles_run,
            "cycles_failed": self.cycles_failed,
            "last_error": self.last_error,
            # SLA-monitor override; None means "use the standard formula"
            "sla_breach_threshold_sec": self.sla_breach_threshold_sec,
            # Phase 1 durable-archive diagnostics
            "db_write_enabled": self._db_writer is not None,
            "last_db_write_count": self.last_db_write_count,
            "last_db_write_ms": self.last_db_write_ms,
            "db_write_failures": self.db_write_failures,
            "last_db_error": self.last_db_error,
        }
