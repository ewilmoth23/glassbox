/* Glassbox satellite-position Web Worker.
 *
 * Loads satellite.js (SGP4 propagator), parses TLE text from the
 * /api/v1/satellites/tle proxy, and emits position arrays back to the
 * main thread on a 30s cadence. SGP4 for ~100 sats takes ~3-8ms per
 * tick; off the render thread, the Cesium globe stays at 60fps even
 * when we scale to celestrak's 5000+ sats.
 *
 * Message protocol:
 *   in:  { cmd: 'init', tleUrl: '/api/v1/satellites/tle' }
 *   out: { type: 'ready', count: N }
 *   out: { type: 'positions', t: <ms>, sats: [{name, lat, lng, alt_km, vel_km_s}, ...] }
 *   out: { type: 'error', msg: '...' }
 */

importScripts('/satellite.min.js');

let SATS = [];          // [{ name, satrec }]
let TICK_MS = 30000;    // propagate every 30s
let TIMER = null;

function _parseTLE(text) {
  // TLE format: name on line 1, "1 ..." on line 2, "2 ..." on line 3
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(l => l);
  const out = [];
  for (let i = 0; i < lines.length - 2; i += 3) {
    const name = lines[i];
    const l1 = lines[i + 1];
    const l2 = lines[i + 2];
    if (!l1.startsWith('1 ') || !l2.startsWith('2 ')) continue;
    try {
      const satrec = satellite.twoline2satrec(l1, l2);
      out.push({ name, satrec });
    } catch (e) {
      // skip malformed entries
    }
  }
  return out;
}

function _propagateAll() {
  const now = new Date();
  const gmst = satellite.gstime(now);
  const out = [];
  for (const s of SATS) {
    const pv = satellite.propagate(s.satrec, now);
    if (!pv || !pv.position) continue;
    const geo = satellite.eciToGeodetic(pv.position, gmst);
    if (!isFinite(geo.latitude) || !isFinite(geo.longitude)) continue;
    const vel = pv.velocity
      ? Math.sqrt(pv.velocity.x ** 2 + pv.velocity.y ** 2 + pv.velocity.z ** 2)
      : null;
    out.push({
      name: s.name,
      lat: geo.latitude  * 180 / Math.PI,
      lng: geo.longitude * 180 / Math.PI,
      alt_km: geo.height,
      vel_km_s: vel,
    });
  }
  postMessage({ type: 'positions', t: now.getTime(), sats: out });
}

self.onmessage = async (ev) => {
  const data = ev.data || {};
  if (data.cmd === 'init') {
    try {
      const r = await fetch(data.tleUrl || '/api/v1/satellites/tle');
      if (!r.ok) {
        postMessage({ type: 'error', msg: `TLE fetch HTTP ${r.status}` });
        return;
      }
      const text = await r.text();
      SATS = _parseTLE(text);
      postMessage({ type: 'ready', count: SATS.length });
      _propagateAll();
      if (TIMER) clearInterval(TIMER);
      TIMER = setInterval(_propagateAll, TICK_MS);
    } catch (e) {
      postMessage({ type: 'error', msg: 'init failed: ' + e.message });
    }
  } else if (data.cmd === 'stop') {
    if (TIMER) { clearInterval(TIMER); TIMER = null; }
  }
};
