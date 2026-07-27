# Glassbox V2 — Phase 1 deploy (venv-based, fixed)

Your pip3 doesn't support `--break-system-packages`. Use a venv instead —
that's what the launcher now does automatically.

## 1. Copy launcher to $HOME (one time)

```bash
cp "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/09_SETUP_GUIDES/supervisor/start-glassbox-server.sh" "$HOME/start-glassbox-server.sh"
chmod +x "$HOME/start-glassbox-server.sh"
```

The launcher will create `21_GLASSBOX_AI/.venv/` on first run and install
fastapi, uvicorn, sse-starlette, aiohttp into it. No system Python pollution.

## 2. Test it manually first

```bash
bash "$HOME/start-glassbox-server.sh"
# First run: you'll see "[glassbox-server] bootstrapping venv..." — takes ~30s
# Then: uvicorn boots on 127.0.0.1:8790
```

In another terminal:

```bash
curl http://127.0.0.1:8790/api/health | python3 -m json.tool
# Expect: ok:true, 1 ingester (planes), last_fetch_count growing each 5s.

curl -N http://127.0.0.1:8790/api/glassbox/stream
# Expect: "hello" event, then per-plane "event" events, "ping" every 20s.
```

Ctrl-C the manual run once you see events.

## 3. Hand off to supervisor

```bash
bash "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/09_SETUP_GUIDES/supervisor/supervisor.sh" start glassbox-server
bash "/Volumes/Mac Mini Expanded Storage/ewilmoth/MEWR Creative Enterprises LLC/09_SETUP_GUIDES/supervisor/supervisor.sh" status
# → glassbox-server should show ● running with a pid
```

If anything goes wrong:

```bash
tail -f "$HOME/mewr-logs/glassbox-server.log"
```

## 4. Point glassbox.html at the server (later, separate task)

Task #67 will patch `glassbox.html` so that when `window.GLASSBOX_SERVER_URL`
is set it subscribes to the SSE stream instead of hitting OpenSky directly.
For local testing you can inject it in DevTools:

```js
window.GLASSBOX_SERVER_URL = 'http://127.0.0.1:8790';
location.reload();
```

## What went wrong on your first attempt (diagnosis)

1. `pip3 install --break-system-packages ...` failed because your pip is
   older than pip 23.0.1 (when that flag landed). Fixed by using a venv.
2. `cat: /Volumes/Mac: No such file or directory` was a supervisor.sh bug —
   `cat $pf` was unquoted so the path split on spaces. Fixed: now `cat "$pf"`.
3. The server didn't actually start (because step 1 failed), so curl to :8790
   connected to nothing. Both will be resolved by re-running step 2 above.
