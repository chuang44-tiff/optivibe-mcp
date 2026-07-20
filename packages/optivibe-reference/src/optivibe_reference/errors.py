"""errors.py — the reference-layer error hierarchy.

Re-implemented (NOT imported) from the harness ``errors.py`` style so the
reference package has zero cross-package coupling. Every class carries a
class-level ``error_family`` so the dispatch envelope can surface it verbatim.

The reference layer is session-free (DB-backed), so it does NOT carry the
harness's .NET/session-transport classes — only the small tool-layer family the
``lookup_operand`` dispatch path needs.
"""


class ReferenceLayerError(Exception):
    """Base class for every OptiVibe reference-layer domain error.

    Named ``ReferenceLayerError`` (NOT ``ReferenceError``) so it does NOT shadow
    the Python builtin ``ReferenceError`` (raised on a dead weakref deref) — an
    ``except ReferenceError`` inside the package must keep catching the builtin,
    not this domain base (N-2).
    """

    error_family = "reference"


class ToolError(ReferenceLayerError):
    """Base class for dispatch/tool-layer errors."""

    error_family = "tool"


class ProvenanceGateError(ReferenceLayerError):
    """A fail-closed provenance gate is not satisfied (BUILD-TIME, not dispatch).

    Raised by the manual-corpus build when the PROVENANCE.md license-ledger row
    that permits indexing the manual is not on file — manual-text ingest must fail
    CLOSED until the grant is recorded. This guards against a future deletion of
    the ledger row silently re-enabling ingest.
    """

    error_family = "provenance_gate"


class UnknownToolError(ToolError):
    """The dispatched tool name is not in the manifest."""

    error_family = "unknown_tool"


class ToolParamError(ToolError):
    """A required parameter for the dispatched tool was missing or ill-typed.

    Covers both a missing required param (presence check in dispatch) and a
    param of the wrong type the handler rejects before coercion (e.g. a ``bool``
    where an ``int`` is expected — ``True == 1`` would otherwise slip through).
    """

    error_family = "tool_param"
