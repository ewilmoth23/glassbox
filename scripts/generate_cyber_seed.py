#!/usr/bin/env python3
"""
Cyber-layer seed-data generator — P2-A Phase 1 MVP.

Pulls the live CISA KEV catalog + Spamhaus DROP/EDROP feeds and writes
geojson snapshots to `21_GLASSBOX_AI/data/cyber_kev.geojson` and
`21_GLASSBOX_AI/data/cyber_spamhaus_drop.geojson`.

These files seed the `/api/v1/infrastructure/cyber-kev` and
`/api/v1/infrastructure/cyber-spamhaus-drop` endpoints. The live
ingesters (CisaKevIngester, SpamhausDropIngester) write the same data
to Postgres on their own polling schedule, but the static files give
the frontend something to render before the ingesters have run AND
keep the routes working under DB outage.

Re-run this script whenever you want to refresh the seed (e.g. before
a deploy). It's idempotent and writes nothing if the upstream feed is
unreachable.

Each Feature has SENTINEL Point geometry [0, 0] because the data is
not geographically positioned. atlas.js renders these layers via a
side-panel list view, NOT a globe overlay.

Run:
    cd 21_GLASSBOX_AI && .venv/bin/python scripts/generate_cyber_seed.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

CISA_KEV_URL = (
    "https://www.cisa.gov/sites/default/files/feeds/"
    "known_exploited_vulnerabilities.json"
)
SPAMHAUS_DROP_URL = "https://www.spamhaus.org/drop/drop.txt"
SPAMHAUS_EDROP_URL = "https://www.spamhaus.org/drop/edrop.txt"

USER_AGENT = "FulcrumGlassbox/2.0 (mewrcreate.com/glassbox)"
HTTP_TIMEOUT_S = 30

_SPAMHAUS_LINE_RE = re.compile(
    r"^\s*"
    r"(?P<cidr>[0-9a-fA-F:.]+/[0-9]+)"
    r"\s*;\s*"
    r"(?P<sbl>SBL[0-9A-Z]+)"
    r"\s*$"
)


def _http_get(url: str) -> Optional[bytes]:
    """Pull a URL via stdlib urllib — no aiohttp dep at script time."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as r:
            return r.read()
    except Exception as e:
        print(f"  FAIL {url} → {type(e).__name__}: {e}", file=sys.stderr)
        return None


# ─── CISA KEV ─────────────────────────────────────────────────────────────


def _kev_feature(v: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert one KEV vulnerability dict into a GeoJSON Feature."""
    cve_id = (v.get("cveID") or "").strip()
    if not cve_id:
        return None
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {
            "cve_id": cve_id,
            "vendor_project": v.get("vendorProject") or None,
            "product": v.get("product") or None,
            "vulnerability_name": v.get("vulnerabilityName") or None,
            "short_description": v.get("shortDescription") or None,
            "required_action": v.get("requiredAction") or None,
            "date_added": v.get("dateAdded") or None,
            "due_date": v.get("dueDate") or None,
            "known_ransomware_campaign_use": v.get("knownRansomwareCampaignUse") or None,
            "notes": v.get("notes") or None,
            "cwes": v.get("cwes") if isinstance(v.get("cwes"), list) else [],
            "link": (
                f"https://nvd.nist.gov/vuln/detail/{cve_id}"
                if cve_id.startswith("CVE-")
                else None
            ),
        },
    }


def generate_kev_geojson() -> Optional[Dict[str, Any]]:
    body = _http_get(CISA_KEV_URL)
    if body is None:
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception as e:
        print(f"  FAIL CISA KEV JSON parse: {e}", file=sys.stderr)
        return None

    vulns = payload.get("vulnerabilities") or []
    features = [f for v in vulns if (f := _kev_feature(v)) is not None]
    return {
        "type": "FeatureCollection",
        "name": "cyber_kev",
        "metadata": {
            "title": payload.get("title") or "CISA Catalog of Known Exploited Vulnerabilities",
            "catalogVersion": payload.get("catalogVersion") or None,
            "dateReleased": payload.get("dateReleased") or None,
            "count": len(features),
            "source": "https://www.cisa.gov/known-exploited-vulnerabilities-catalog",
            "license": "CC0 (US Government public domain, Title 17 USC § 105)",
            "attribution": "Known-exploited vulnerabilities: CISA KEV Catalog",
            "rendering_hint": "side_panel_list",
        },
        "features": features,
    }


# ─── Spamhaus DROP/EDROP ──────────────────────────────────────────────────


def _parse_spamhaus_lines(text: str) -> List[Tuple[str, str]]:
    """Extract (cidr, sbl_id) tuples from a DROP/EDROP plain-text feed."""
    out: List[Tuple[str, str]] = []
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s or s.startswith(";"):
            continue
        m = _SPAMHAUS_LINE_RE.match(s)
        if not m:
            continue
        out.append((m.group("cidr"), m.group("sbl")))
    return out


def _spamhaus_feature(list_name: str, cidr: str, sbl_id: str) -> Dict[str, Any]:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0, 0]},
        "properties": {
            "cidr": cidr,
            "sbl_id": sbl_id,
            "list_name": list_name,
            "title": f"{list_name} block {cidr} ({sbl_id})",
            "link": f"https://www.spamhaus.org/sbl/query/{sbl_id}",
        },
    }


def generate_spamhaus_geojson() -> Optional[Dict[str, Any]]:
    features: List[Dict[str, Any]] = []
    fetched: List[str] = []
    for list_name, url in (("DROP", SPAMHAUS_DROP_URL), ("EDROP", SPAMHAUS_EDROP_URL)):
        body = _http_get(url)
        if body is None:
            continue
        text = body.decode("utf-8", errors="replace")
        entries = _parse_spamhaus_lines(text)
        features.extend(_spamhaus_feature(list_name, c, s) for c, s in entries)
        fetched.append(list_name)
    if not features:
        return None
    return {
        "type": "FeatureCollection",
        "name": "cyber_spamhaus_drop",
        "metadata": {
            "lists_fetched": fetched,
            "count": len(features),
            "source": "https://www.spamhaus.org/drop/",
            "license": "Free with attribution; Spamhaus DROP/EDROP redistribution permitted",
            "attribution": "Block lists: Spamhaus",
            "rendering_hint": "side_panel_list",
        },
        "features": features,
    }


# ─── Driver ───────────────────────────────────────────────────────────────


def _write_geojson(path: Path, data: Dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  WROTE {path.relative_to(ROOT)} — {data['metadata']['count']} features")


def main() -> int:
    print(f"Generating cyber-layer seeds into {DATA_DIR}")

    kev = generate_kev_geojson()
    if kev is not None:
        _write_geojson(DATA_DIR / "cyber_kev.geojson", kev)
    else:
        print("  SKIP cyber_kev.geojson — upstream unreachable", file=sys.stderr)

    spam = generate_spamhaus_geojson()
    if spam is not None:
        _write_geojson(DATA_DIR / "cyber_spamhaus_drop.geojson", spam)
    else:
        print("  SKIP cyber_spamhaus_drop.geojson — upstream unreachable", file=sys.stderr)

    return 0 if (kev is not None and spam is not None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
