# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
GdeltBulkIngester — the live wiring that turns the downloader + parser
+ CAMEO + prefilter chain into a daemon-managed ingester.

Cycle:
  1. Pull lastupdate.txt; pick the .export.CSV.zip URL.
  2. Skip silently if URL == last-processed (no new snapshot yet).
  3. Download + unzip + decode CSV bytes.
  4. Parse rows through the CAMEO lookup.
  5. Run each parsed event through the PreFilterEngine
     (engine.process(ev, enqueue=False) — the bounded queue is for
     the still-unbuilt LLM extraction worker; for now passing events
     just go straight to broadcast + dual-write).
  6. Persist the processed URL to the on-disk last-processed file
     so daemon restarts don't reprocess.

State file: $GLASSBOX_CACHE_DIR/gdelt_bulk/last_processed.txt
(default: /Volumes/Mac Mini Expanded Storage/ewilmoth/glassbox-cache/...).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from glassbox_taxonomy import CAMEOLookup

from ..base import GlassboxEvent, Ingester
from .downloader import (
    LastUpdate,
    download_export_csv,
    fetch_lastupdate,
)
from .parser import parse_events_csv
from .prefilter import PreFilterConfig, PreFilterEngine
from .prefilter.engine import FilteredEvent


_DEFAULT_CACHE_ROOT = "/Volumes/Mac Mini Expanded Storage/ewilmoth/glassbox-cache"
_PREFILTER_PKG = Path(__file__).resolve().parent / "prefilter"
_DEFAULT_CONFIG = _PREFILTER_PKG / "config" / "prefilter.yaml"
# Operators set GLASSBOX_PREFILTER_SHADOW_CONFIG to an absolute path
# pointing at an alternate prefilter YAML to spin up the A/B shadow
# engine. Engine.shadow_engine receives the resulting PreFilterEngine
# and a 4-cell confusion matrix accumulates on engine.health()['shadow'].
# Unset = no shadow (production default; zero overhead).
_SHADOW_CONFIG_ENV = "GLASSBOX_PREFILTER_SHADOW_CONFIG"


class GdeltBulkIngester(Ingester):
    """GDELT V2 bulk-CSV ingester.

    layer="news" (so the existing news consumer surfaces apply); source
    string identifies the bulk path explicitly so it sits next to the
    deprecated gdelt + gdelt_topical entries in audits without confusion.

    The CAMEO lookup, prefilter engine, and aiohttp session are owned by
    this ingester (constructed lazily on first cycle to keep import-time
    cheap and to honor smoke_mode).
    """

    layer = "news"
    source = "GDELT 2.0 Bulk CSV (Events + GKG)"
    source_id = "gdelt_bulk"
    poll_interval_sec = 300.0  # 5 min — GDELT publishes every 15 min, so
                               # we re-check 3× per publication cycle.

    def __init__(
        self,
        broadcaster=None,
        logger=None,
        classifier=None,
        smoke_mode: bool = False,
        db_writer=None,
        *,
        prefilter_config_path: Optional[Path] = None,
        shadow_config_path: Optional[Path] = None,
        cache_dir: Optional[Path] = None,
        cameo: Optional[CAMEOLookup] = None,
        engine: Optional[PreFilterEngine] = None,
        max_rows_per_cycle: Optional[int] = None,
    ) -> None:
        super().__init__(
            broadcaster=broadcaster,
            logger=logger,
            classifier=classifier,
            smoke_mode=smoke_mode,
            db_writer=db_writer,
        )
        cfg_path = prefilter_config_path or _DEFAULT_CONFIG
        self._cameo = cameo or CAMEOLookup()
        if engine is None:
            cfg = PreFilterConfig.load_yaml(cfg_path)
            shadow_engine = self._build_shadow_engine(shadow_config_path)
            engine = PreFilterEngine(cfg, _PREFILTER_PKG,
                                     shadow_engine=shadow_engine)
        self._engine = engine

        cache_root = Path(cache_dir or os.environ.get("GLASSBOX_CACHE_DIR",
                                                      _DEFAULT_CACHE_ROOT))
        self._state_dir = cache_root / "gdelt_bulk"
        self._state_file = self._state_dir / "last_processed.txt"
        self._max_rows_per_cycle = max_rows_per_cycle
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Diagnostics surfaced via Mission Control / health.
        self.last_lastupdate_url: Optional[str] = None
        self.last_processed_url: Optional[str] = self._read_last_processed()
        self.last_csv_byte_count: int = 0
        self.last_parsed_count: int = 0
        self.last_filtered_count: int = 0

    # ─── A/B shadow engine ───────────────────────────────────────────

    def _build_shadow_engine(
        self, kwarg_path: Optional[Path],
    ) -> Optional[PreFilterEngine]:
        """Resolve + load the optional A/B shadow config. Order of
        precedence:
          1. ``shadow_config_path`` ctor kwarg (test path).
          2. ``GLASSBOX_PREFILTER_SHADOW_CONFIG`` env var (operator
             path — set in the launchd plist).
          3. None (no shadow; production default).

        A failed load is logged and the ingester continues without a
        shadow — silent telemetry-skipping is preferable to refusing
        to start when an experimental config has a typo. Successful
        load + shadow wiring is logged at INFO so the operator
        confirms it's running.
        """
        path = kwarg_path
        if path is None:
            env_path = os.environ.get(_SHADOW_CONFIG_ENV)
            if env_path:
                path = Path(env_path)
        if path is None:
            return None
        try:
            shadow_cfg = PreFilterConfig.load_yaml(path)
        except Exception as e:  # noqa: BLE001
            self.log.warning(
                f"[gdelt_bulk] shadow config load failed at {path} "
                f"({type(e).__name__}: {e}); continuing without shadow"
            )
            return None
        shadow = PreFilterEngine(shadow_cfg, _PREFILTER_PKG)
        self.log.info(
            f"[gdelt_bulk] A/B shadow engine wired from {path} "
            f"(version={shadow_cfg.version}, "
            f"rules={[r.name for r in shadow._rules]})"
        )
        return shadow

    # ─── State persistence ───────────────────────────────────────────

    def _read_last_processed(self) -> Optional[str]:
        try:
            if self._state_file.exists():
                txt = self._state_file.read_text(encoding="utf-8").strip()
                return txt or None
        except OSError:
            return None
        return None

    def _write_last_processed(self, url: str) -> None:
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._state_file.with_suffix(".txt.tmp")
            tmp.write_text(url, encoding="utf-8")
            tmp.replace(self._state_file)
        except OSError as e:
            self.log.warning(f"[gdelt_bulk] state write failed (non-fatal): {e}")

    # ─── HTTP session ────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession(
                headers={"User-Agent":
                         "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox; hello@mewrcreate.com)"},
            )
        return self._http_session

    async def aclose(self) -> None:
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()

    # ─── fetch / normalize (Ingester contract) ───────────────────────

    async def fetch(self) -> List[Dict[str, Any]]:
        session = await self._get_session()
        update: LastUpdate = await fetch_lastupdate(session)
        if update.export is None:
            self.log.warning("[gdelt_bulk] lastupdate.txt has no export entry")
            return []

        self.last_lastupdate_url = update.export.url
        if update.export.url == self.last_processed_url:
            # No new snapshot since last cycle; cheap exit.
            return []

        csv_text = await download_export_csv(session, update.export)
        self.last_csv_byte_count = len(csv_text)

        parsed_count = 0
        filtered: List[FilteredEvent] = []
        for ev in parse_events_csv(csv_text, cameo=self._cameo,
                                   max_rows=self._max_rows_per_cycle):
            parsed_count += 1
            out = self._engine.process(ev, enqueue=False)
            if out is not None:
                filtered.append(out)

        self.last_parsed_count = parsed_count
        self.last_filtered_count = len(filtered)
        self._write_last_processed(update.export.url)
        self.last_processed_url = update.export.url

        self.log.info(
            "[gdelt_bulk] cycle: parsed %d, passed prefilter %d, "
            "url=%s",
            parsed_count, len(filtered), update.export.url,
        )
        # Carry FilteredEvent through fetch → normalize. Base.cycle()
        # treats the list as opaque between these two methods, so the
        # type-hint of List[Dict] is honored at the public contract
        # while we pass our own carrier internally.
        return filtered  # type: ignore[return-value]

    def normalize(self, raw_items: List[Any]) -> List[GlassboxEvent]:
        """Convert each FilteredEvent → GlassboxEvent. Severity comes
        from the prefilter's already-applied CAMEO lookup; we re-encode
        priority + duplicate_of in the payload so downstream queries
        can surface them."""
        out: List[GlassboxEvent] = []
        for fe in raw_items:
            ev = fe.event  # type: ignore[union-attr]
            payload: Dict[str, Any] = {
                "headline":            ev.title,
                "url":                 ev.source_url,
                "country":             ev.iso_country,
                "actor1":              ev.actor1_name,
                "actor2":              ev.actor2_name,
                "cameo_code":          ev.code,
                "cameo_subcategory":   ev.subcategory,
                "goldstein":           ev.goldstein,
                "flags":               list(ev.flags),
                "prefilter_priority":  fe.priority,
                "prefilter_rules_version": fe.rules_version,
                "_attribution":        "News events: GDELT Project (public domain)",
            }
            if fe.duplicate_of:
                payload["duplicate_of"] = fe.duplicate_of

            ts = ev.timestamp
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            out.append(GlassboxEvent(
                layer=self.layer,
                external_id=ev.event_id,
                kind="news",
                lat=ev.lat,
                lng=ev.lng,
                ts=ts.isoformat(),
                severity=int(round(ev.severity * 10)),
                source=self.source,
                payload=payload,
                domain="geo",
                geocode_quality=ev.geocode_quality,
                decay_half_life_min=720,  # 12h — matches gdelt_topical
            ))
        return out

    # ─── Diagnostics ─────────────────────────────────────────────────

    def status(self) -> Dict[str, Any]:
        base = super().status()
        base["last_lastupdate_url"]   = self.last_lastupdate_url
        base["last_processed_url"]    = self.last_processed_url
        base["last_csv_byte_count"]   = self.last_csv_byte_count
        base["last_parsed_count"]     = self.last_parsed_count
        base["last_filtered_count"]   = self.last_filtered_count
        base["prefilter_health"]      = self._engine.health()
        return base

    @property
    def engine(self) -> PreFilterEngine:
        return self._engine
