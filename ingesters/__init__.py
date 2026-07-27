"""
Glassbox ingesters — server-side data fetchers that replace browser-side API calls.

Every ingester:
  1. Pulls from its source on its own cadence
  2. Normalizes to a uniform GlassboxEvent
  3. De-duplicates against last cycle
  4. Broadcasts changes to the in-memory cache + SSE subscribers
  5. (Future) Persists to the Holding Brain

Contract: see base.py
"""

from .base import GlassboxEvent, Ingester

__all__ = ["GlassboxEvent", "Ingester"]
