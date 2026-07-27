# Copyright (c) 2026 Glassbox contributors
# SPDX-License-Identifier: MIT
"""Path bootstrap so `import mcp_servers.entities.server` works from the
MCP-server venv when pytest is invoked from anywhere."""

import sys
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent.parent  # → 21_GLASSBOX_AI/
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
