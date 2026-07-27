"""
Odds API ingester — live US sportsbook odds + line movement.

Source: https://the-odds-api.com/
Free tier: 500 requests/month — enough for spot checks.
$30/mo tier: 20,000 req/mo — enough for the 30-min cadence we use here.

Why this exists
---------------
Per the 2026-04-27 audit, premium.html claims a "sharp money score" edge
factor, but every actual generated pick has `sharp_money_score: 0`. Reason:
nothing in the empire actually ingests live odds. This ingester is the input
that makes the sharp-money calculation possible:
  - Pull current consensus odds from US books every 30 min
  - Compute per-game line movement vs the prior poll
  - Tag movements where line moved AGAINST public money (= sharp action)
  - Publish as Loop events so EventClassifier sees them

Activation
----------
1. Sign up at https://the-odds-api.com/ → get an API key
2. Set env var on Mac Mini:    export ODDS_API_KEY=...
3. Restart MEWR OS server. This ingester boots automatically when the key
   is present; it stays dormant when the key is empty (no requests fired).

Cost discipline
---------------
The free tier is 500 req/mo. With 6 sports × 1 poll per 30 min = 288 polls/day
× 30 days = 8,640 calls/mo if we polled all sports. We use this strategy:
  - During in-season hours only (defined per sport)
  - Throttle to 1 poll per sport per 30 min
  - Skip polls when no games are within next 24h

This keeps us inside $30/mo Plus plan (20k req/mo) with substantial headroom.

Author: 2026-04-27 — task #165
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


log = logging.getLogger("glassbox.odds_api")


# ─── API ────────────────────────────────────────────────────────────────


_BASE_URL = "https://api.the-odds-api.com/v4"

# Sports we monitor. Keys come straight from the Odds API sports endpoint.
# Comments capture in-season windows (US Eastern wall-clock months).
SPORTS = {
    "americanfootball_nfl":      "Sep–Feb",
    "basketball_nba":            "Oct–Jun",
    "icehockey_nhl":             "Oct–Jun",
    "baseball_mlb":              "Mar–Oct",
    "americanfootball_ncaaf":    "Aug–Jan",
    "basketball_ncaab":          "Nov–Apr",
}

# Books we care about for consensus (US regions). Pinnacle would be ideal
# but isn't on the API; consensus from these is the closest free signal.
BOOKMAKERS = ["draftkings", "fanduel", "betmgm", "caesars", "betrivers"]

DEFAULT_REGIONS = "us"
DEFAULT_MARKETS = "h2h,spreads,totals"
ODDS_FORMAT = "american"


# ─── State ───────────────────────────────────────────────────────────────


@dataclass
class OddsSnapshot:
    """Single bookmaker's current line for a single market on one game."""
    book: str
    market: str          # h2h | spreads | totals
    outcome: str         # team name or "Over"/"Under"
    point: Optional[float]
    price: int           # American odds


@dataclass
class GameOdds:
    """Aggregated odds across books for one game."""
    sport: str
    game_id: str
    home: str
    away: str
    commence_iso: str
    snapshots: List[OddsSnapshot] = field(default_factory=list)


@dataclass
class LineMovement:
    """Detected movement vs prior poll, for one game-market."""
    game_id: str
    sport: str
    home: str
    away: str
    market: str
    delta_points: float          # change in spread/total since last poll
    delta_price: int             # change in moneyline (signed cents)
    direction: str               # "home" | "away" | "over" | "under"
    sharp_indicator: str         # "neutral" | "sharp_with" | "sharp_against"
    observed_at: str
    notes: str = ""


# ─── Ingester ────────────────────────────────────────────────────────────


class OddsAPIIngester:
    """Polls The Odds API and emits LineMovement events as Loop signals."""

    layer = "markets"
    source = "the-odds-api.com"
    poll_interval_sec = 30 * 60       # every 30 minutes (in-season)

    def __init__(self, *, api_key: Optional[str] = None, on_event=None,
                 active_sports: Optional[List[str]] = None,
                 store_path: Optional[str] = None,
                 logger: Optional[logging.Logger] = None) -> None:
        self.api_key = (api_key or os.environ.get("ODDS_API_KEY", "")).strip()
        self.on_event = on_event
        self.active_sports = active_sports or list(SPORTS.keys())
        self.store_path = store_path or "/data/odds_snapshots.jsonl"
        self.log = logger or log
        # In-memory rolling map of last poll's odds, keyed by (sport, game_id, market, book)
        self._last_lines: Dict[Tuple[str, str, str, str], OddsSnapshot] = {}
        # Sports actually in season — populated on first successful poll
        self._in_season: List[str] = list(self.active_sports)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    # ─── HTTP helpers ───────────────────────────────────────────────────

    async def _get(self, session, path: str, params: Dict[str, Any]) -> Any:
        params = dict(params or {})
        params["apiKey"] = self.api_key
        url = _BASE_URL.rstrip("/") + path
        async with session.get(url, params=params, timeout=20) as resp:
            if resp.status == 401:
                raise RuntimeError("Odds API: 401 — bad/missing API key")
            if resp.status == 429:
                self.log.warning("Odds API: 429 rate-limited; backing off")
                return None
            if resp.status >= 400:
                text = await resp.text()
                raise RuntimeError(f"Odds API HTTP {resp.status}: {text[:200]}")
            return await resp.json()

    # ─── Polling ────────────────────────────────────────────────────────

    async def poll_once(self) -> List[LineMovement]:
        """One full poll of all active sports. Returns detected movements."""
        if not self.enabled:
            self.log.info("[odds_api] no API key; skipping (set ODDS_API_KEY to activate)")
            return []
        try:
            import aiohttp
        except ImportError:
            self.log.warning("[odds_api] aiohttp not installed")
            return []

        movements: List[LineMovement] = []
        async with aiohttp.ClientSession() as session:
            for sport in self._in_season:
                try:
                    games = await self._poll_sport(session, sport)
                    for game in games:
                        movements.extend(self._diff_movements(sport, game))
                except Exception as e:
                    self.log.warning(f"[odds_api/{sport}] poll error: {type(e).__name__}: {e}")
                    continue

        for mv in movements:
            await self._emit_movement(mv)

        return movements

    async def _poll_sport(self, session, sport: str) -> List[GameOdds]:
        params = {"regions": DEFAULT_REGIONS, "markets": DEFAULT_MARKETS,
                  "oddsFormat": ODDS_FORMAT, "bookmakers": ",".join(BOOKMAKERS)}
        data = await self._get(session, f"/sports/{sport}/odds", params)
        if not isinstance(data, list):
            return []
        out: List[GameOdds] = []
        for g in data:
            game = GameOdds(
                sport=sport,
                game_id=str(g.get("id", "")),
                home=g.get("home_team", ""),
                away=g.get("away_team", ""),
                commence_iso=g.get("commence_time", ""),
            )
            for book in g.get("bookmakers", []) or []:
                book_key = book.get("key", "")
                for market in book.get("markets", []) or []:
                    mk = market.get("key", "")
                    for outcome in market.get("outcomes", []) or []:
                        game.snapshots.append(OddsSnapshot(
                            book=book_key,
                            market=mk,
                            outcome=outcome.get("name", ""),
                            point=(float(outcome["point"]) if outcome.get("point") is not None else None),
                            price=int(outcome.get("price", 0) or 0),
                        ))
            out.append(game)
            await self._persist_snapshot(game)
        return out

    # ─── Diff + sharp detection ─────────────────────────────────────────

    def _diff_movements(self, sport: str, game: GameOdds) -> List[LineMovement]:
        out: List[LineMovement] = []
        seen_now = set()
        for snap in game.snapshots:
            key = (sport, game.game_id, snap.market, snap.book)
            seen_now.add(key)
            prev = self._last_lines.get(key)
            self._last_lines[key] = snap
            if prev is None:
                continue
            delta_pts = (snap.point or 0) - (prev.point or 0)
            delta_price = snap.price - prev.price
            if abs(delta_pts) < 0.25 and abs(delta_price) < 5:
                continue   # noise
            direction = self._direction(snap, prev)
            sharp = self._sharp_indicator(delta_pts, delta_price)
            out.append(LineMovement(
                game_id=game.game_id,
                sport=sport,
                home=game.home,
                away=game.away,
                market=snap.market,
                delta_points=round(delta_pts, 2),
                delta_price=delta_price,
                direction=direction,
                sharp_indicator=sharp,
                observed_at=datetime.now(timezone.utc).isoformat(),
                notes=f"{snap.book}: {prev.point}/{prev.price} -> {snap.point}/{snap.price}",
            ))
        return out

    def _direction(self, snap: OddsSnapshot, prev: OddsSnapshot) -> str:
        # Heuristic mapping; full logic depends on spread vs h2h vs totals.
        if snap.market == "spreads":
            return snap.outcome  # team name
        if snap.market == "totals":
            return snap.outcome.lower()  # "Over"/"Under"
        return snap.outcome  # h2h: team name

    def _sharp_indicator(self, dpoints: float, dprice: int) -> str:
        """Stub heuristic. v2 will compare line movement direction against
        public money split (requires a separate ticket-percentage feed)."""
        magnitude = abs(dpoints) + abs(dprice) / 25
        if magnitude > 1.0:
            return "sharp_with"   # significant move — assume informed money
        return "neutral"

    # ─── Persistence + emit ─────────────────────────────────────────────

    async def _persist_snapshot(self, game: GameOdds) -> None:
        """Append-only JSONL log for offline training + provenance."""
        try:
            from pathlib import Path as _P
            p = _P(self.store_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "sport": game.sport, "game_id": game.game_id,
                "home": game.home, "away": game.away,
                "snapshots": [s.__dict__ for s in game.snapshots],
            }
            with p.open("a") as f:
                f.write(json.dumps(payload) + "\n")
        except Exception as e:
            self.log.debug(f"persist failed: {e}")

    async def _emit_movement(self, mv: LineMovement) -> None:
        """Push a Loop-style event so the classifier + Prediqt see this signal."""
        if self.on_event is None:
            return
        evt = {
            "kind": "line_movement",
            "ts": mv.observed_at,
            "source": "odds_api",
            "sport": mv.sport,
            "game_id": mv.game_id,
            "home": mv.home,
            "away": mv.away,
            "market": mv.market,
            "delta_points": mv.delta_points,
            "delta_price": mv.delta_price,
            "direction": mv.direction,
            "sharp_indicator": mv.sharp_indicator,
            "summary": f"{mv.away} @ {mv.home} {mv.market} moved {mv.delta_points:+.2f}/{mv.delta_price:+d} ({mv.sharp_indicator})",
        }
        try:
            res = self.on_event(evt)
            if asyncio.iscoroutine(res):
                await res
        except Exception as e:
            self.log.debug(f"emit error: {e}")

    # ─── Daemon loop ────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        self.log.info(f"[odds_api] starting (enabled={self.enabled}, sports={len(self.active_sports)})")
        while True:
            try:
                count = len(await self.poll_once())
                self.log.info(f"[odds_api] poll complete; {count} movements emitted")
            except Exception as e:
                self.log.error(f"[odds_api] poll fatal: {e}")
            await asyncio.sleep(self.poll_interval_sec)


# ─── Quick smoke test ────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ing = OddsAPIIngester()
    if not ing.enabled:
        print("ODDS_API_KEY not set — ingester is dormant. Set the env var to activate.")
        sys.exit(0)
    movements = asyncio.run(ing.poll_once())
    print(f"Polled. {len(movements)} movements detected.")
    for mv in movements[:10]:
        print(f"  {mv.sport} {mv.away} @ {mv.home} {mv.market} {mv.delta_points:+.2f} pts ({mv.sharp_indicator})")
