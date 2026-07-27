"""Concern-split route modules for `api_v1.py`'s factory router.

Each module here exposes a module-level `router = APIRouter()` (no
prefix; the parent `build_router(prefix=...)` in `api_v1.py` mounts it
under the active prefix via `include_router`). This lets the same set
of routes be dual-mounted at `/api/v1/*` AND `/api/intel/*` without
duplication.

See `21_GLASSBOX_AI/docs/API_V1_ROUTE_INVENTORY.md` for the extraction
plan, the per-cluster helper graph, and the test-coupling caveats
(several tests reach into `api_v1`'s private symbols — those must
stay re-exported from `api_v1` after extraction).
"""
