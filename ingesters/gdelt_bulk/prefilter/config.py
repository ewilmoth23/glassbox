# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""
Pydantic models for the prefilter engine: the input event shape
(``GDELTEventForPrefilter``), per-rule configuration, priority weights,
and the top-level ``PreFilterConfig`` loaded from prefilter.yaml.

Kept deliberately small — only fields the rules + scorer actually consume.
The downstream ``gdelt_bulk.normalize()`` constructs richer
``GlassboxEvent`` instances for events that pass.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# Geocode precision buckets — matches the empire's GlassboxEvent.geocode_quality
# vocabulary (see 21_GLASSBOX_AI/ingesters/base.py). Ordered finest → coarsest;
# the GeographyRule uses the index to compare against precision_min.
PRECISION_LEVELS: tuple = ("exact", "city", "region", "country", "unknown")


class GDELTEventForPrefilter(BaseModel):
    """Input shape consumed by the prefilter chain.

    Constructed by ``gdelt_bulk.parse()`` from a single raw GDELT row after
    the HANDOFF_02 CAMEO lookup has populated category/subcategory/severity
    /flags. Anything the rules don't read isn't on this model — see
    ``GlassboxEvent`` for the post-pass canonical shape.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    timestamp: datetime
    code: str = Field(..., description="Raw CAMEO event code (3- or 4-digit)")
    category: str
    subcategory: str
    severity: float = Field(..., ge=0.0, le=1.0)
    goldstein: float = Field(..., ge=-10.0, le=10.0)
    flags: List[str] = Field(default_factory=list)
    title: str = ""
    source_url: str = ""
    actor1_name: Optional[str] = None
    actor2_name: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    geocode_quality: str = "unknown"
    iso_country: Optional[str] = None


# ─── Rule configs ────────────────────────────────────────────────────────


class CategoryFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["allow", "block"] = "allow"
    subcategories: List[str] = Field(default_factory=list)
    flags_required: List[str] = Field(default_factory=list)
    flags_blocked: List[str] = Field(default_factory=list)


class SeverityFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_severity: float = Field(0.6, ge=0.0, le=1.0)
    per_category_overrides: Dict[str, float] = Field(default_factory=dict)

    @field_validator("per_category_overrides")
    @classmethod
    def _bounded(cls, v: Dict[str, float]) -> Dict[str, float]:
        for cat, threshold in v.items():
            if not 0.0 <= threshold <= 1.0:
                raise ValueError(f"per_category_overrides[{cat}] = {threshold} out of [0,1]")
        return v


class GeographyFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    precision_min: Literal["exact", "city", "region", "country"] = "region"
    bbox_allowlist: List[List[float]] = Field(default_factory=list)
    iso_country_allowlist: List[str] = Field(default_factory=list)
    iso_country_blocklist: List[str] = Field(default_factory=list)

    @field_validator("bbox_allowlist")
    @classmethod
    def _bboxes_well_formed(cls, v: List[List[float]]) -> List[List[float]]:
        for box in v:
            if len(box) != 4:
                raise ValueError(f"bbox must be [west, south, east, north]; got {box}")
            w, s, e, n = box
            if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90):
                raise ValueError(f"bbox out of WGS84 bounds: {box}")
            if w > e or s > n:
                raise ValueError(f"bbox lower-left > upper-right: {box}")
        return v


class SourceQualityFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_score: float = Field(0.5, ge=0.0, le=1.0)
    unknown_domain_score: float = Field(0.3, ge=0.0, le=1.0)
    data_file: str = "data/source_quality.json"


class RecencyFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_age_hours: float = Field(6.0, gt=0.0)


class DedupFilterConfig(BaseModel):
    """Sliding-window dedup. v1.0 backing is an in-process per-subcategory
    deque (single Mac, single backend process — same architectural call
    as the deferred NATS streaming spine). Interface stays identical for
    a future Redis-backed implementation when multi-worker pressure is
    real."""
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    window_minutes: float = Field(60.0, gt=0.0)
    similarity_threshold: float = Field(0.85, ge=0.0, le=1.0)
    geo_threshold_km: float = Field(50.0, ge=0.0)
    fields_compared: List[str] = Field(
        default_factory=lambda: ["title", "actor1", "actor2", "subcategory"]
    )


# ─── Priority + queue + A/B ──────────────────────────────────────────────


class PriorityWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    severity: float = 0.30
    source: float = 0.20
    duplication: float = 0.20
    recency: float = 0.10
    category: float = 0.10
    geo_aoi: float = 0.10

    @field_validator("severity", "source", "duplication", "recency", "category", "geo_aoi")
    @classmethod
    def _non_negative(cls, v: float) -> float:
        if v < 0.0:
            raise ValueError("priority weights must be non-negative")
        return v


class PriorityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weights: PriorityWeights = Field(default_factory=PriorityWeights)
    category_priority_bonuses: Dict[str, float] = Field(default_factory=dict)


class QueueConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_depth: int = Field(500, ge=1)
    redis_key: str = "glassbox:prefilter:queue"


class ABTestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    config_a: str = "prefilter.yaml"
    config_b: str = "prefilter_experimental.yaml"
    hash_field: str = "event_id"
    split: float = Field(0.5, ge=0.0, le=1.0)


# ─── Top-level config + loader ───────────────────────────────────────────


class RulesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_filter: CategoryFilterConfig = Field(default_factory=CategoryFilterConfig)
    severity_filter: SeverityFilterConfig = Field(default_factory=SeverityFilterConfig)
    geography_filter: GeographyFilterConfig = Field(default_factory=GeographyFilterConfig)
    source_quality_filter: SourceQualityFilterConfig = Field(default_factory=SourceQualityFilterConfig)
    recency_filter: RecencyFilterConfig = Field(default_factory=RecencyFilterConfig)
    dedup_filter: DedupFilterConfig = Field(default_factory=DedupFilterConfig)


class PreFilterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    rules: RulesConfig = Field(default_factory=RulesConfig)
    priority: PriorityConfig = Field(default_factory=PriorityConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    ab_test: ABTestConfig = Field(default_factory=ABTestConfig)

    @classmethod
    def load_yaml(cls, path: str | Path) -> "PreFilterConfig":
        """Load + validate a prefilter YAML config."""
        with Path(path).open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)
