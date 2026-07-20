"""optivibe_harness.tools — dispatchable typed ZOS-API tools.

Each tool module exposes a ``handler(session, params) -> dict`` plus a
module-level ``TOOL_SPEC`` (a ``server.ToolSpec``) the dispatcher loads via
``importlib`` to build its manifest.
"""

from .get_system_info import TOOL_SPEC as GET_SYSTEM_INFO_SPEC
from .get_system_info import get_system_info

__all__ = [
    "get_system_info",
    "GET_SYSTEM_INFO_SPEC",
]
