"""Web layer for glassbox_server — concern-split routers extracted from
the monolithic `glassbox_server.py` per backlog item P3-H.

Each module under `web/routes/` owns one URL prefix and exports a
FastAPI `APIRouter`. The main `glassbox_server.py` mounts them via
`app.include_router(...)`.
"""
