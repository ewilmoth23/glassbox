"""On-demand OSINT lookups — `/lookup/*` (extraction #1 of P3-H Phase 2).

Three pure-delegation handlers that defer all work to functions in the
sibling `lookups` module:

  - GET /lookup/subdomains  → crt.sh CT-log subdomain enumeration
  - GET /lookup/wayback     → Wayback Machine CDX snapshot listing
  - GET /lookup/asn         → RIPEstat BGP/network intel

Each is cached server-side (10 / 15 / 5 minutes respectively). The
handlers themselves carry no state and own no helpers; they exist only
to translate FastAPI query params into the `lookups.py` function args.

Mounted by `api_v1.build_router()` at BOTH `/api/v1/*` AND `/api/intel/*`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from lookups import lookup_asn, lookup_subdomains, lookup_wayback

router = APIRouter()


@router.get("/lookup/subdomains")
async def lookup_subdomains_endpoint(
    domain: str = Query(..., description="Apex domain (e.g. 'example.com')"),
    max_results: int = Query(500, ge=1, le=10000),
):
    """crt.sh CT-log subdomain enumeration. Cached 10 min per (domain, max)."""
    return await lookup_subdomains(domain, max_results=max_results)


@router.get("/lookup/wayback")
async def lookup_wayback_endpoint(
    url: str = Query(..., description="URL to look up snapshots for"),
    limit: int = Query(100, ge=1, le=1000),
):
    """Wayback Machine CDX snapshot listing. Cached 15 min per (url, limit)."""
    return await lookup_wayback(url, limit=limit)


@router.get("/lookup/asn")
async def lookup_asn_endpoint(
    asn: Optional[str] = Query(None, description="AS number, e.g. 'AS15169' or '15169'"),
    ip: Optional[str] = Query(None, description="IPv4/IPv6 address"),
):
    """RIPEstat BGP/network intel. Supply EITHER asn OR ip (not both).
    Cached 5 min per query."""
    if not asn and not ip:
        raise HTTPException(status_code=400,
                            detail="must supply asn= or ip=")
    if asn and ip:
        raise HTTPException(status_code=400,
                            detail="supply only one of asn/ip")
    return await lookup_asn(asn=asn, ip=ip)
