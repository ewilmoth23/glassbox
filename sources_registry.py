"""
Sources Registry — backend startup gate that enforces infra/sources.yaml.

This is the structural compliance gate from LEGAL_COMPLIANCE_REGISTRY.md
(Operating Rule 13: "Defensible legal posture, not lawsuit-proof. Activities
that create real exposure are structurally impossible because the code refuses
to do them.")

How it works:
  1. At server startup, load infra/sources.yaml ONCE.
  2. Build a {source_id: source_dict} map.
  3. For each ingester instance, look up its `source_id` class attribute.
  4. Refuse to start that ingester if ANY of:
       - source_id is empty (ingester not registered)
       - source_id not in the registry (unknown source)
       - registry row has enabled=false
       - registry row has commercial_use_ok=false  (in v1.0 only)
  5. Log every refusal with the reason — never silent.

The gate runs in glassbox_server.py @app.on_event("startup") BEFORE any
ingester's run_forever() task is created. Refused ingesters are dropped from
the active list entirely.

Usage from glassbox_server.py:

    from sources_registry import SourcesRegistry, gate_ingester
    REGISTRY = SourcesRegistry.load()

    ingesters = [PlanesIngester(...), ShipsIngester(...), ...]
    active = []
    for ing in ingesters:
        ok, reason = gate_ingester(ing, REGISTRY)
        if ok:
            active.append(ing)
            asyncio.create_task(ing.run_forever())
        else:
            log.warning(f"[gate] REFUSED {ing.__class__.__name__}: {reason}")

If sources.yaml is missing or malformed at startup, the gate refuses ALL
ingesters and emits a fatal-style log line. This is intentional — better to
ship a quiet server than ship one that's leaking license violations.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None  # gate will refuse all if PyYAML not installed


log = logging.getLogger("sources-registry")


# ─── Registry container ───────────────────────────────────────────────────


@dataclass
class SourcesRegistry:
    """Loaded snapshot of infra/sources.yaml."""
    sources_by_id: Dict[str, Dict[str, Any]]
    operating_rules: Dict[str, Any]
    last_reviewed: str
    loaded_ok: bool
    load_error: Optional[str] = None

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "SourcesRegistry":
        """Load from infra/sources.yaml. Returns a registry object even on
        failure (with loaded_ok=False) so the server can decide what to do."""
        if yaml is None:
            return cls(
                sources_by_id={},
                operating_rules={},
                last_reviewed="",
                loaded_ok=False,
                load_error="PyYAML not installed; backend gate cannot enforce sources.yaml",
            )

        if path is None:
            # Default: 21_GLASSBOX_AI/sources_registry.py → ../infra/sources.yaml
            path = Path(__file__).resolve().parent.parent / "infra" / "sources.yaml"

        if not path.exists():
            return cls(
                sources_by_id={},
                operating_rules={},
                last_reviewed="",
                loaded_ok=False,
                load_error=f"sources.yaml missing at {path}",
            )

        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            return cls(
                sources_by_id={},
                operating_rules={},
                last_reviewed="",
                loaded_ok=False,
                load_error=f"failed to parse sources.yaml: {e}",
            )

        sources_list = data.get("sources", []) or []
        sources_by_id: Dict[str, Dict[str, Any]] = {}
        for entry in sources_list:
            sid = entry.get("id")
            if sid:
                sources_by_id[sid] = entry

        return cls(
            sources_by_id=sources_by_id,
            operating_rules=data.get("operating_rules", {}) or {},
            last_reviewed=str(data.get("last_reviewed", "")),
            loaded_ok=True,
        )

    def get(self, source_id: str) -> Optional[Dict[str, Any]]:
        return self.sources_by_id.get(source_id)

    def enabled_count(self) -> int:
        return sum(1 for s in self.sources_by_id.values() if s.get("enabled"))

    def disabled_count(self) -> int:
        return sum(1 for s in self.sources_by_id.values() if not s.get("enabled"))


# ─── The gate ─────────────────────────────────────────────────────────────


def gate_ingester(ingester: Any, registry: SourcesRegistry) -> Tuple[bool, str]:
    """Return (allowed, reason). reason is human-readable for logs.

    Compound ingesters:
      If `additional_source_ids` is set on the ingester class, ALL listed
      source_ids must pass the gate (AND logic). One bad row = whole ingester
      refused.
    """
    # Registry didn't load = refuse everything (Rule 13: structural fail-safe)
    if not registry.loaded_ok:
        return False, f"sources.yaml not loaded: {registry.load_error}"

    sid = getattr(ingester, "source_id", "") or ""
    extra = getattr(ingester, "additional_source_ids", ()) or ()
    all_ids = [sid] + list(extra)

    # Empty source_id is a programming error — the ingester didn't register.
    if not sid:
        cls_name = ingester.__class__.__name__
        return False, (
            f"{cls_name} has no source_id class attribute. Add "
            f"`source_id = '...'` matching a row in infra/sources.yaml."
        )

    # Refuse if Rule 12 (v1.0 only commercial_use_ok) is in effect
    v1_strict = bool(registry.operating_rules.get("v1_0_only_commercial_ok", True))

    for one_id in all_ids:
        row = registry.get(one_id)
        if row is None:
            return False, (
                f"source_id '{one_id}' not in sources.yaml. "
                f"Add an entry with HONEST values per LEGAL_COMPLIANCE_REGISTRY "
                f"Chapters 1-3, then restart."
            )
        if not row.get("enabled", False):
            why = row.get("disabled_reason", "marked enabled=false")
            return False, f"source_id '{one_id}' is disabled: {why}"
        if v1_strict and not row.get("commercial_use_ok", False):
            return False, (
                f"source_id '{one_id}' has commercial_use_ok=false; "
                f"v1.0 operating_rules.v1_0_only_commercial_ok blocks it. "
                f"License: {row.get('license', 'unknown')}"
            )

    return True, "OK"


# ─── Diagnostics for /api/sources endpoint ────────────────────────────────


def registry_summary(registry: SourcesRegistry) -> Dict[str, Any]:
    """Returns a dict suitable for JSON serialization to a /api/sources
    endpoint. Mission Control can render this in the License Gate panel."""
    if not registry.loaded_ok:
        return {
            "loaded_ok": False,
            "load_error": registry.load_error,
            "enabled_count": 0,
            "disabled_count": 0,
            "sources": [],
        }

    enabled_rows = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "license": s.get("license"),
            "attribution": s.get("attribution"),
            "commercial_use_ok": s.get("commercial_use_ok"),
        }
        for s in registry.sources_by_id.values()
        if s.get("enabled")
    ]
    disabled_rows = [
        {
            "id": s.get("id"),
            "name": s.get("name"),
            "disabled_reason": s.get("disabled_reason"),
            "migrate_to_pro": s.get("migrate_to_pro", False),
        }
        for s in registry.sources_by_id.values()
        if not s.get("enabled")
    ]

    return {
        "loaded_ok": True,
        "last_reviewed": registry.last_reviewed,
        "enabled_count": len(enabled_rows),
        "disabled_count": len(disabled_rows),
        "operating_rules": registry.operating_rules,
        "enabled": enabled_rows,
        "disabled": disabled_rows,
    }
