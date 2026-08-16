"""optivibe_reference — lookup tables + RAG + intent->ZOS-API grounding index.

The grounding layer that makes a smaller local orchestrator viable: an FTS5 RAG
over reference material plus an intent->ZOS-API mapping index the agent consults
FIRST (probe-first).

Layer 1 (this increment): the ``MeritOperandType`` operand catalog (438 live
codes, authored/manual descriptions pending, live-probed parameter-cell layout
battery) + the typed, session-free, never-raise ``lookup_operand`` MCP tool that
grounds design intent onto a merit-function operand. The catalog is built from a
committed normalized-LF JSON at runtime; the binary ``.db`` is never committed.
"""

__version__ = "0.1.6"
