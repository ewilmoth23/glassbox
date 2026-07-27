"""One-shot verifier for the OpenSky v2 OAuth2 client_credentials flow.

Run this BEFORE restarting glassbox_server.py. If this script prints live
state vectors, the credentials are valid and planes.py will start pulling
data on the next bridge tick.

Usage (Mac Mini terminal):

    cd "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/21_GLASSBOX_AI"
    export OPENSKY_CLIENT_ID="mewrcreate-api-client"
    export OPENSKY_CLIENT_SECRET="I3El1mjuSoR0vIA9cWNzC4BhwFKbTilm"
    python3 scripts/test_opensky_oauth.py

Exit codes:
    0 — token minted AND state vector fetch succeeded
    1 — credentials rejected or no access_token returned
    2 — token OK but state fetch failed
    3 — network / unexpected exception

Only uses the standard library (urllib) + json so there's nothing to
install. Keeps the check fully independent of the aiohttp ingester stack.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/"
    "protocol/openid-connect/token"
)
STATE_URL = "https://opensky-network.org/api/states/all"

CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "").strip()


def _fail(msg: str, code: int) -> None:
    print("FAIL: " + msg, file=sys.stderr)
    sys.exit(code)


def mint_token() -> Dict[str, Any]:
    if not (CLIENT_ID and CLIENT_SECRET):
        _fail(
            "Set OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET env vars first.",
            1,
        )
    data = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        }
    ).encode("ascii")
    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        _fail(
            "token endpoint returned HTTP %d (%s). Body: %s"
            % (e.code, e.reason, detail[:400]),
            1,
        )
    except Exception as e:
        _fail("token request exception: %r" % (e,), 3)

    if not body.get("access_token"):
        _fail(
            "token response had no access_token. Keys: %s"
            % ", ".join(body.keys()),
            1,
        )
    return body


def fetch_states(token: str) -> Dict[str, Any]:
    req = urllib.request.Request(
        STATE_URL,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
        },
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        _fail(
            "states endpoint returned HTTP %d (%s). Body: %s"
            % (e.code, e.reason, detail[:400]),
            2,
        )
    except Exception as e:
        _fail("states request exception: %r" % (e,), 3)
    return body


def main() -> None:
    print("== OpenSky OAuth2 verifier ==")
    print("client_id: " + (CLIENT_ID or "<MISSING>"))
    print("client_secret: " + ("<present, %d chars>" % len(CLIENT_SECRET) if CLIENT_SECRET else "<MISSING>"))
    print("")

    print("[1/2] Requesting access token ...")
    t0 = time.time()
    tok_body = mint_token()
    dt = time.time() - t0
    access_token = tok_body["access_token"]
    ttl = tok_body.get("expires_in", "?")
    print(
        "      OK. got token (%d chars), expires_in=%s, %.2fs"
        % (len(access_token), ttl, dt)
    )

    print("")
    print("[2/2] Fetching /api/states/all ...")
    t0 = time.time()
    states = fetch_states(access_token)
    dt = time.time() - t0
    raw = states.get("states") or []
    ts = states.get("time", "?")
    print(
        "      OK. %d state vectors @ server_ts=%s, %.2fs"
        % (len(raw), ts, dt)
    )

    print("")
    print("First 10 aircraft:")
    for i, s in enumerate(raw[:10], 1):
        if not isinstance(s, list) or len(s) < 11:
            continue
        icao = (s[0] or "").strip() or "?"
        call = (s[1] or "").strip() or "?"
        lng = s[5]
        lat = s[6]
        baro = s[7]
        on_ground = s[8]
        vel = s[9]
        hdg = s[10]
        print(
            "  %2d. %s (%-8s) lat=%s lng=%s alt_baro=%s vel=%s hdg=%s on_ground=%s"
            % (i, icao, call, lat, lng, baro, vel, hdg, on_ground)
        )

    print("")
    print("PASS — credentials are valid. Safe to restart glassbox_server.py.")
    sys.exit(0)


if __name__ == "__main__":
    main()
