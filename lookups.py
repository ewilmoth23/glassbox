"""
On-demand OSINT lookup helpers — Phase 2 close-out (V2 plan items 2N/2O/2M).

Three lookup sources that don't fit the periodic-poll Ingester contract because
they're query-driven (caller supplies a domain / URL / IP / ASN). Each is
fronted by a thin REST endpoint in `api_v1.py` so consumers can hit them
without speaking to the upstream directly.

Sources (per `infra/sources.yaml`):
  - crt.sh       Certificate Transparency log subdomain enum (no auth)
  - Wayback CDX  Internet Archive historical-snapshot search (no auth)
  - RIPEstat     BGP / ASN / abuse-contact intel (no auth)

Each result is cached in-process with a TTL — repeated lookups within the
window return instantly. The cache is intentionally small/simple; Phase 4
will replace it with the proper Postgres-backed cache table.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
except ImportError:
    aiohttp = None  # endpoints will return 503 if aiohttp missing


_log = logging.getLogger("lookups")


# ─── Tiny TTL cache ───────────────────────────────────────────────────────


class _TTLCache:
    """Single-process async-safe TTL cache. Keys are arbitrary hashables."""

    def __init__(self, default_ttl_sec: int = 300) -> None:
        self._store: Dict[Any, Tuple[float, Any]] = {}
        self._lock = asyncio.Lock()
        self.default_ttl_sec = default_ttl_sec

    async def get(self, key: Any) -> Optional[Any]:
        async with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if time.time() >= expires_at:
                self._store.pop(key, None)
                return None
            return value

    async def set(self, key: Any, value: Any, ttl_sec: Optional[int] = None) -> None:
        ttl = ttl_sec if ttl_sec is not None else self.default_ttl_sec
        async with self._lock:
            self._store[key] = (time.time() + ttl, value)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()


_subdomain_cache = _TTLCache(default_ttl_sec=600)    # 10 min
_wayback_cache = _TTLCache(default_ttl_sec=900)      # 15 min
_ripe_cache = _TTLCache(default_ttl_sec=300)         # 5 min


# ─── crt.sh — Certificate Transparency subdomain enumeration ──────────────


CRT_SH_URL = "https://crt.sh/"
CRT_SH_TIMEOUT_SEC = 20
CRT_SH_UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"


async def lookup_subdomains(domain: str, *, max_results: int = 500) -> Dict[str, Any]:
    """Enumerate subdomains of `domain` via crt.sh CT logs.

    Returns:
        {
          "domain": "<input>",
          "subdomains": ["a.example.com", "b.example.com", ...],
          "count": <int>,
          "source": "crt.sh",
          "_attribution": "Subdomain data: crt.sh CT logs (Sectigo)",
          "cached": <bool>,
        }
    """
    if aiohttp is None:
        return {"error": "aiohttp not installed", "domain": domain,
                "subdomains": [], "count": 0}

    domain = (domain or "").strip().lower()
    if not domain or "." not in domain:
        return {"error": "invalid domain", "domain": domain,
                "subdomains": [], "count": 0}

    cache_key = f"crt:{domain}:{max_results}"
    cached = await _subdomain_cache.get(cache_key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return out

    params = {"q": f"%.{domain}", "output": "json"}
    try:
        timeout = aiohttp.ClientTimeout(total=CRT_SH_TIMEOUT_SEC)
        headers = {"User-Agent": CRT_SH_UA}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(CRT_SH_URL, params=params) as r:
                if r.status != 200:
                    return {"error": f"crt.sh HTTP {r.status}", "domain": domain,
                            "subdomains": [], "count": 0}
                data = await r.json(content_type=None)
    except asyncio.TimeoutError:
        return {"error": "crt.sh timeout", "domain": domain,
                "subdomains": [], "count": 0}
    except Exception as e:
        _log.info(f"crt.sh lookup failed for {domain}: {e}")
        return {"error": f"{type(e).__name__}: {e}", "domain": domain,
                "subdomains": [], "count": 0}

    # Each row is {"name_value": "*.example.com\nfoo.example.com", ...}
    seen = set()
    for row in (data or []):
        name = (row.get("name_value") or "")
        for entry in name.replace("\r", "").split("\n"):
            e = entry.strip().lower().lstrip("*.")
            if not e:
                continue
            if e == domain or e.endswith("." + domain):
                seen.add(e)
                if len(seen) >= max_results:
                    break
        if len(seen) >= max_results:
            break

    out = {
        "domain": domain,
        "subdomains": sorted(seen),
        "count": len(seen),
        "source": "crt.sh",
        "_attribution": "Subdomain data: crt.sh CT logs (Sectigo, public domain)",
        "cached": False,
    }
    await _subdomain_cache.set(cache_key, out)
    return out


# ─── Wayback CDX — historical snapshot listing ────────────────────────────


WAYBACK_URL = "https://web.archive.org/cdx/search/cdx"
# 2026-05-09: bumped from 25s to 45s — popular URLs (e.g. anthropic.com/)
# consistently exceeded the 25s ceiling on cold cache because the CDX
# server scans millions of capture rows per request. 45s is enough for
# all but pathological queries; the cache TTL means a single slow first
# call covers 15 min of subsequent ones.
WAYBACK_TIMEOUT_SEC = 45
WAYBACK_UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"


async def lookup_wayback(url: str, *, limit: int = 100) -> Dict[str, Any]:
    """Look up Wayback Machine snapshots for `url`.

    Returns:
        {
          "url": "<input>",
          "snapshots": [{"timestamp": "20240301120000", "url": "...", "status": "200"}, ...],
          "count": <int>,
          "source": "wayback_cdx",
          "_attribution": "Web archive: Wayback Machine / Internet Archive",
          "cached": <bool>,
        }
    """
    if aiohttp is None:
        return {"error": "aiohttp not installed", "url": url,
                "snapshots": [], "count": 0}

    url = (url or "").strip()
    if not url:
        return {"error": "invalid url", "url": url,
                "snapshots": [], "count": 0}

    cache_key = f"wb:{url}:{limit}"
    cached = await _wayback_cache.get(cache_key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return out

    # CDX server returns a JSON array of arrays. First row is the header.
    params = {
        "url":      url,
        "output":   "json",
        "fl":       "timestamp,original,statuscode,mimetype,digest",
        "limit":    str(limit),
    }
    try:
        timeout = aiohttp.ClientTimeout(total=WAYBACK_TIMEOUT_SEC)
        headers = {"User-Agent": WAYBACK_UA}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            async with s.get(WAYBACK_URL, params=params) as r:
                if r.status != 200:
                    return {"error": f"Wayback HTTP {r.status}", "url": url,
                            "snapshots": [], "count": 0}
                rows = await r.json(content_type=None)
    except asyncio.TimeoutError:
        return {"error": "Wayback timeout", "url": url,
                "snapshots": [], "count": 0}
    except Exception as e:
        _log.info(f"Wayback CDX lookup failed for {url}: {e}")
        return {"error": f"{type(e).__name__}: {e}", "url": url,
                "snapshots": [], "count": 0}

    snapshots: List[Dict[str, Any]] = []
    if rows and isinstance(rows, list):
        # First row is column header
        header = rows[0] if rows else None
        body = rows[1:] if rows and isinstance(header, list) else rows
        for row in body:
            if not isinstance(row, list) or len(row) < 3:
                continue
            ts = row[0]
            orig = row[1] if len(row) > 1 else None
            status = row[2] if len(row) > 2 else None
            snapshots.append({
                "timestamp": ts,
                "url":       orig,
                "status":    status,
                "mimetype":  row[3] if len(row) > 3 else None,
                "snapshot_url": (
                    f"https://web.archive.org/web/{ts}/{orig}"
                    if ts and orig else None
                ),
            })

    out = {
        "url": url,
        "snapshots": snapshots,
        "count": len(snapshots),
        "source": "wayback_cdx",
        "_attribution": "Web archive: Wayback Machine / Internet Archive",
        "cached": False,
    }
    await _wayback_cache.set(cache_key, out)
    return out


# ─── RIPEstat — BGP / ASN / abuse intel ───────────────────────────────────


RIPE_URL_BASE = "https://stat.ripe.net/data"
RIPE_TIMEOUT_SEC = 15
RIPE_UA = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"


async def lookup_asn(*, asn: Optional[str] = None, ip: Optional[str] = None) -> Dict[str, Any]:
    """RIPEstat lookup: ASN overview + announced prefixes (when given asn=)
    OR network info (when given ip=).

    Returns:
        {
          "query": {"asn"|"ip": "..."},
          "data": <RIPEstat data block>,
          "endpoints_called": [...],
          "source": "ripestat",
          "_attribution": "Network intel: RIPEstat (RIPE NCC, free)",
          "cached": <bool>,
        }
    """
    if aiohttp is None:
        return {"error": "aiohttp not installed", "data": None}

    if not asn and not ip:
        return {"error": "asn or ip parameter required", "data": None}
    if asn and ip:
        return {"error": "supply only one of asn/ip", "data": None}

    cache_key = f"ripe:{asn}:{ip}"
    cached = await _ripe_cache.get(cache_key)
    if cached is not None:
        out = dict(cached)
        out["cached"] = True
        return out

    endpoints_called: List[str] = []
    data: Dict[str, Any] = {}

    timeout = aiohttp.ClientTimeout(total=RIPE_TIMEOUT_SEC)
    headers = {"User-Agent": RIPE_UA}

    try:
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as s:
            if asn:
                # Strip leading 'AS' if user passed 'AS15169'
                asn_clean = asn.upper().lstrip("AS").strip()
                params = {"resource": asn_clean}
                # Two RIPEstat calls — overview + announced prefixes
                for endpoint in ("as-overview", "announced-prefixes"):
                    url = f"{RIPE_URL_BASE}/{endpoint}/data.json"
                    endpoints_called.append(endpoint)
                    try:
                        async with s.get(url, params=params) as r:
                            if r.status == 200:
                                j = await r.json()
                                data[endpoint] = (j or {}).get("data")
                            else:
                                data[endpoint] = {"error": f"HTTP {r.status}"}
                    except Exception as e:
                        data[endpoint] = {"error": f"{type(e).__name__}: {e}"}
                query = {"asn": f"AS{asn_clean}"}
            else:
                params = {"resource": ip}
                for endpoint in ("network-info", "abuse-contact-finder"):
                    url = f"{RIPE_URL_BASE}/{endpoint}/data.json"
                    endpoints_called.append(endpoint)
                    try:
                        async with s.get(url, params=params) as r:
                            if r.status == 200:
                                j = await r.json()
                                data[endpoint] = (j or {}).get("data")
                            else:
                                data[endpoint] = {"error": f"HTTP {r.status}"}
                    except Exception as e:
                        data[endpoint] = {"error": f"{type(e).__name__}: {e}"}
                query = {"ip": ip}
    except asyncio.TimeoutError:
        return {"error": "RIPEstat timeout", "data": None}
    except Exception as e:
        _log.info(f"RIPEstat lookup failed: {e}")
        return {"error": f"{type(e).__name__}: {e}", "data": None}

    out = {
        "query": query,
        "data": data,
        "endpoints_called": endpoints_called,
        "source": "ripestat",
        "_attribution": "Network intel: RIPEstat (RIPE NCC, free)",
        "cached": False,
    }
    await _ripe_cache.set(cache_key, out)
    return out
