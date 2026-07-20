"""server.py — the reference-layer dispatch core (NO mcp import).

Re-implemented (NOT imported) from the harness ``server.py`` so the reference
package has zero cross-package coupling. The differences from the harness core
are deliberate and locked (§5):

- ``ToolSpec`` carries a ``kind`` discriminator (default ``'operand'``) reserved
  so the future four-enum ``lookup_operand`` cannot silently overwrite this one
  in ``load_manifest``'s last-wins ``{name: spec}`` dict (§1 manifest collision).
- The ref ``Dispatcher`` is SESSION-FREE: it opens/holds the catalog DB
  connection and threads it as POSITIONAL arg-0 to ``spec.handler(conn, params)``
  — the FORWARD analog of the harness session-as-arg-0 (PIN 4). There is no
  engine, no lock, no .NET classification — a handler answers from the threaded
  DB connection, never a module-global.

``mcp`` is NOT imported here (the MCP adapter lives in ``server_mcp.py`` and
imports ``mcp`` lazily). The dispatch envelope NEVER raises out to the caller.
"""
import importlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from typing import Callable, Dict, Tuple

from . import catalog_build, glass_build, manual_build, tolerance_build
from ._envelope import error_envelope
from .errors import ReferenceLayerError, ToolParamError, UnknownToolError

# A baked catalog JSON that EXISTS but is truncated / corrupt / stale-schema / a
# valid-JSON WRONG-SHAPE must degrade to ``operand_catalog_unavailable`` (the verbatim-local build
# amendment-1 finding 10 + external F2), NOT crash ``Dispatcher.__init__``. This is
# the SCOPED corruption set — the same fail-loud spirit as the ``isfile``-else-``None``
# absence guard, but for a present file whose CONTENT cannot build a catalog.
# Deliberately still NARROW (NOT a bare ``Exception``, never ``BaseException``):
# OSError (I/O), sqlite3.Error (bad DB build), json.JSONDecodeError (non-JSON bytes),
# ValueError (malformed shape — ``_validate_catalog_shape`` raises this for the
# well-formed cases first). F2 BROADENS it with KeyError/TypeError/AttributeError as a
# BELT so ANY residual wrong-shape corruption (``{}`` -> KeyError, ``[]`` -> TypeError,
# ``{"rows":"str"}`` -> AttributeError) degrades rather than crashes. SCOPED to
# ``_safe_open_catalog`` (which wraps ONLY the ``opener``/``build_db`` call) — the
# handler-dispatch path is a SEPARATE ``except BaseException`` in ``dispatch`` that
# still classifies a real handler ``KeyError`` as ``internal`` (never swallowed here).
# ``open_manual_corpus`` is out of scope for this fold (§4.1 #4).
_CATALOG_OPEN_DEGRADE_ERRORS = (
    OSError, sqlite3.Error, json.JSONDecodeError, ValueError,
    KeyError, TypeError, AttributeError,
)


def _safe_open_catalog(opener, *args, _what="catalog"):
    """Open a catalog via ``opener(*args)``; degrade a corrupt file to ``None``.

    Returns the connection, or ``None`` if ``opener`` raised a scoped corruption
    error (finding 10 / F2). The degrade reason is written to stderr (never swallowed
    silently) so a present-but-corrupt catalog is diagnosable; the caller's ``None``
    conn then answers ``operand_catalog_unavailable`` at dispatch time. The broadened
    tuple is safe ONLY because this wrapper wraps EXCLUSIVELY the open/build_db call,
    never handler dispatch (a real handler bug still surfaces via ``dispatch``).
    """
    try:
        return opener(*args)
    except _CATALOG_OPEN_DEGRADE_ERRORS as exc:  # scoped: corrupt/truncated/stale
        print(
            f"WARN: {_what} present but unreadable "
            f"({type(exc).__name__}: {exc}) — degrading to "
            "operand_catalog_unavailable",
            file=sys.stderr,
        )
        return None


def _safe_error_text(exc) -> str:
    """Build a ``"{Type}: {message}"`` error string that NEVER raises.

    Mirrors the harness guard: the never-raise envelope must not itself raise if
    a handler exception's ``__str__`` raises.
    """
    try:
        type_name = type(exc).__name__
    except Exception:  # noqa: BLE001 — even type(exc).__name__ must not escape
        type_name = "?"
    try:
        message = str(exc)
    except Exception:  # noqa: BLE001 — exc.__str__ raised
        try:
            message = repr(exc)
        except Exception:  # noqa: BLE001 — repr raised too
            message = None
    if message is None:
        return f"{type_name}: <unprintable exception message>"
    return f"{type_name}: {message}"


@dataclass(frozen=True)
class ToolSpec:
    """A dispatchable reference tool: name, handler, required params, kind.

    ``kind`` is the reserved discriminator (§1): the narrowed operand tool is
    ``kind='operand'``; the eventual four-enum tool would register a distinct
    kind so the manifest's last-wins dict does not silently overwrite it.
    """

    name: str
    handler: Callable
    required_params: Tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    kind: str = "operand"
    # Per-param JSON-Schema type map. EVERY param the handler reads
    # (required AND optional) -> its JSON type token (number/integer/string). OPTIONAL
    # by default (empty) so a spec without it still loads; the harness MCP adapter
    # emits a typed inputSchema from it and falls back to the legacy all-string schema
    # when it is empty (server_mcp._build_input_schema). Mirrors harness ToolSpec:78.
    param_types: Dict[str, str] = field(default_factory=dict)


# Tool modules exposing a ``TOOL_SPECS`` tuple of ToolSpecs.
_MULTI_SPEC_MODULES = (
    "optivibe_reference.tools.lookup_operand",
    "optivibe_reference.tools.search_reference",
    "optivibe_reference.tools.lookup_glass",
    "optivibe_reference.tools.find_glasses",
    "optivibe_reference.tools.find_glass_pair",
)

# The ``ToolSpec.kind`` that routes a handler to the gitignored manual-corpus
# connection (vs. the always-present catalog connection). Every other kind gets
# the catalog connection threaded as arg-0.
_MANUAL_RAG_KIND = "manual_rag"

# The ``ToolSpec.kind`` that routes a handler to the glass catalog connection (a
# DISTINCT keyed table, NOT the operand table). The glass conn is built from the
# USER-BUILT ``glass_catalog.json`` (scripts/build_glass_catalog.py, derived from
# the user's licensed install) — like the manual corpus it is OPTIONAL: absent on
# a fresh clone until the user runs the build, so the conn degrades to None and
# glass requests answer a typed ``glass_catalog_unavailable`` envelope.
_GLASS_KIND = "glass"

# The ``ToolSpec.kind`` for the operand tool. ``domain`` is an ORTHOGONAL runtime
# sub-selector WITHIN this kind (merit vs tolerance) — a different axis from
# ``kind`` (the static structural discriminator), never conflated.
_OPERAND_KIND = "operand"
# The default operand domain when a caller omits ``domain`` — the byte-identical
# merit path (every existing caller and all 30 regression cases pass no domain key).
_DEFAULT_DOMAIN = "merit"


def load_manifest():
    """Build the default tool manifest via importlib (avoids an import cycle).

    Imports each tool module and collects its ``TOOL_SPECS`` tuple, returning
    ``{name: ToolSpec}``. Last-wins on name collision — the ``kind`` field is the
    reserved guard against the future four-enum tool silently clobbering this one.
    """
    manifest = {}
    for mod_name in _MULTI_SPEC_MODULES:
        module = importlib.import_module(mod_name)
        for spec in module.TOOL_SPECS:
            manifest[spec.name] = spec
    return manifest


class Dispatcher:
    """Routes a reference tool request to its handler; never raises.

    SESSION-FREE: the dispatcher opens/holds TWO DB connections — the always-
    present, committed-JSON-built catalog connection AND an OPTIONAL gitignored
    manual-corpus connection — and threads the ``spec.kind``-correct one as
    POSITIONAL arg-0 to every handler (PIN 3). The handler answers from THAT
    connection, never a module-global (§5).

    The manual corpus may be absent (a fresh clone never ran the build): then
    ``manual_conn`` is ``None`` and a ``manual_rag`` request returns a structured
    ``corpus_unavailable`` envelope BEFORE the handler is called (so the handler
    never sees a ``None`` connection).
    """

    def __init__(self, manifest=None, db_path=":memory:", conn=None,
                 manual_conn=None, manual_db_path=None,
                 glass_conn=None, glass_db_path=None,
                 tolerance_conn=None, tolerance_db_path=None):
        # The catalog connection is the FORWARD analog of the harness session: it
        # is arg-0 to the operand handlers. Built from committed JSON unless an
        # explicit connection is injected (tests inject an in-memory build). The
        # catalog JSON is now gitignored + user-built, so a fresh clone that never
        # ran the vendor-data build has no baked JSON: guard with isfile-else-None
        # (the copy's glass degrade pattern) so __init__ NEVER crashes with a
        # FileNotFoundError — dispatch answers operand_catalog_unavailable instead.
        if conn is not None:
            self._conn = conn
        elif os.path.isfile(catalog_build.CATALOG_JSON_PATH):
            # finding 10: a present-but-corrupt/truncated catalog degrades to None
            # (operand_catalog_unavailable), never crashes __init__.
            self._conn = _safe_open_catalog(
                catalog_build.open_catalog, db_path, _what="merit catalog"
            )
        else:
            self._conn = None
        # The manual-corpus connection is OPTIONAL: use an injected one, else open
        # it from the gitignored ``.db`` path when given, else stay None (absent).
        if manual_conn is not None:
            self._manual_conn = manual_conn
        elif manual_db_path is not None:
            self._manual_conn = manual_build.open_manual_corpus(manual_db_path)
        else:
            self._manual_conn = None
        # The glass connection is OPTIONAL (user-built-JSON model, like the
        # gitignored manual corpus): use an injected one, else build from the
        # given .db path, else build from the user-built glass_catalog.json when
        # it exists — else stay None (fresh clone, build_glass_catalog.py not run
        # yet) so Dispatcher construction survives and the OTHER reference tools
        # stay alive; glass requests answer glass_catalog_unavailable.
        if glass_conn is not None:
            self._glass_conn = glass_conn
        elif glass_db_path is not None:
            self._glass_conn = glass_build.open_glass_catalog(glass_db_path)
        elif os.path.isfile(glass_build.GLASS_CATALOG_JSON):
            self._glass_conn = glass_build.open_glass_catalog()
        else:
            self._glass_conn = None
        # The tolerance connection is OPTIONAL (user-built-JSON model, like the
        # operand catalog and glass — NOT baked): inject one, else build from the
        # given .db path, else build from the user-built tolerance_operand_catalog
        # when it exists — else stay None (fresh clone, build not run) so a missing
        # baked catalog degrades to domain="tolerance" -> operand_catalog_unavailable
        # rather than a FileNotFoundError crash in __init__.
        if tolerance_conn is not None:
            self._tolerance_conn = tolerance_conn
        elif tolerance_db_path is not None:
            self._tolerance_conn = _safe_open_catalog(
                tolerance_build.open_tolerance_catalog, tolerance_db_path,
                _what="tolerance catalog",
            )
        elif os.path.isfile(tolerance_build.TOLERANCE_CATALOG_JSON_PATH):
            # finding 10: a present-but-corrupt tolerance catalog degrades to None.
            self._tolerance_conn = _safe_open_catalog(
                tolerance_build.open_tolerance_catalog, _what="tolerance catalog"
            )
        else:
            # Same fresh-clone degrade as the merit conn (user-built-JSON model): a
            # missing baked tolerance catalog -> None, not a FileNotFoundError crash.
            self._tolerance_conn = None
        # The ONLY place a domain token maps to a catalog connection.
        # Adding a future operand domain (CB / GRIN / enum) is one entry here + one
        # build module — no new dispatch branch, no new validation site.
        self._operand_domains = {
            "merit": self._conn,
            "tolerance": self._tolerance_conn,
        }
        self._manifest = manifest if manifest is not None else load_manifest()

    @property
    def conn(self):
        return self._conn

    @property
    def manual_conn(self):
        return self._manual_conn

    @property
    def glass_conn(self):
        return self._glass_conn

    @property
    def tolerance_conn(self):
        return self._tolerance_conn

    def _resolve_operand_domain(self, params):
        """Map ``params['domain']`` to ``(conn, domain)``.

        The SINGLE typed-validation point for the operand-domain sub-selector.
        Returns the ``(sqlite3.Connection, str)`` pair: the conn the handler answers
        from AND the AUTHORITATIVE domain label (the ``_operand_domains`` registry key
        that selected the conn). ONE resolution produces both, so the label can never
        disagree with the conn.

        - absent ``domain`` -> the ``merit`` default conn (byte-identical to today);
        - a non-str ``domain`` -> ``ToolParamError``;
        - a domain NOT registered in ``_operand_domains`` -> ``ToolParamError``
          naming the bad value AND the allowed set;
        - a REGISTERED domain whose conn is ``None`` (the baked catalog was never
          built on this machine) -> ``(None, domain)``. This is NOT "unknown" — it
          is an *unavailable* catalog; ``dispatch`` converts it to a structured
          ``operand_catalog_unavailable`` envelope BEFORE the handler. Membership
          is tested with ``in`` (NOT ``.get() is None``, which conflated an
          unregistered domain with a registered-but-degraded one).

        NEVER a silent merit fallback - a silent fallback would re-introduce the
        exact mis-route this ticket closes, disguised as a successful merit answer.
        """
        domain = params.get("domain", _DEFAULT_DOMAIN)
        if not isinstance(domain, str):
            raise ToolParamError(
                f"'domain' must be a string; got {type(domain).__name__}"
            )
        if domain not in self._operand_domains:
            allowed = sorted(self._operand_domains)
            raise ToolParamError(
                f"unknown operand domain {domain!r}; allowed: {allowed}"
            )
        # A registered domain whose conn is None is a DEGRADED (unbuilt) catalog,
        # not an unknown domain: return (None, domain) so dispatch can answer
        # operand_catalog_unavailable — never a ToolParamError disguising an
        # unbuilt catalog as a typo.
        conn = self._operand_domains[domain]
        return conn, domain

    def _conn_for(self, kind):
        """Select the handler arg-0 connection by ``spec.kind`` (PIN 3).

        Glass is a DISTINCT keyed table built from ``glass_catalog.json`` — NOT the
        operand conn. Without this explicit branch the fallthrough would silently
        route glass queries at the operand table (§0).
        """
        if kind == _MANUAL_RAG_KIND:
            return self._manual_conn
        if kind == _GLASS_KIND:
            return self._glass_conn
        return self._conn

    def list_tools(self):
        """Return ``[{name, description, required_params, param_types}]`` per tool."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "required_params": list(spec.required_params),
                "param_types": dict(spec.param_types),
            }
            for spec in self._manifest.values()
        ]

    def dispatch(self, tool_name, params):
        """Dispatch ``tool_name`` with ``params``; ALWAYS returns an envelope.

        Validates the tool exists and that every required param is PRESENT
        (presence-only). On a raised exception, maps it to a typed family and
        returns the never-raise envelope ``{ok, tool, result|error, error_family}``.
        """
        params = params or {}
        try:
            # A non-dict ``params`` (e.g. the bare string ``"name"``) would slip
            # past the presence check: ``"name" in "name"`` is a SUBSTRING test that
            # passes, then the handler raises a TypeError that misclassifies as
            # "internal". Reject a non-dict early as a typed tool_param error. Shared
            # across all three tools (intended).
            if not isinstance(params, dict):
                raise ToolParamError(
                    f"params for {tool_name!r} must be a dict; got "
                    f"{type(params).__name__}"
                )
            spec = self._manifest.get(tool_name)
            if spec is None:
                raise UnknownToolError(f"unknown tool: {tool_name!r}")

            missing = [p for p in spec.required_params if p not in params]
            if missing:
                raise ToolParamError(
                    f"missing required param(s) for {tool_name!r}: {missing}"
                )

            # Operand kind: the domain sub-selector picks the catalog conn at the
            # single validation point (merit default / tolerance), and returns the
            # AUTHORITATIVE label. Every other kind routes by spec.kind unchanged.
            operand_domain = None
            if spec.kind == _OPERAND_KIND:
                conn, operand_domain = self._resolve_operand_domain(params)
            else:
                conn = self._conn_for(spec.kind)
            if spec.kind == _OPERAND_KIND and conn is None:
                # The baked operand catalog for this domain was never built on this
                # machine (a fresh clone that never ran the vendor-data build) —
                # answer a structured operand_catalog_unavailable envelope WITHOUT
                # calling the handler (it must never see a None connection). Shape
                # parity with corpus_unavailable / glass_catalog_unavailable:
                # dispatch ok=True, result.ok=False, result.error_family set, and
                # the domain carried so the caller knows WHICH catalog is missing.
                result = error_envelope(
                    tool_name, "operand_catalog_unavailable",
                    f"{operand_domain} operand catalog not built; run "
                    "scripts/build_vendor_data.py (see PROVENANCE.md)",
                    domain=operand_domain,
                )
            elif spec.kind == _MANUAL_RAG_KIND and conn is None:
                # The gitignored manual corpus was never built on this machine —
                # answer a structured corpus_unavailable envelope WITHOUT calling
                # the handler (it must never see a None connection). Shape parity
                # with a handler-returned expected-failure envelope: ok=True at the
                # dispatch layer, result.ok=False.
                result = error_envelope(
                    tool_name, "corpus_unavailable",
                    "manual corpus not built on this machine",
                )
            elif spec.kind == _GLASS_KIND and conn is None:
                # The user-built glass catalog was never built on this machine —
                # same degrade contract as the manual corpus: a structured
                # envelope WITHOUT calling the handler (it must never see a None
                # connection).
                result = error_envelope(
                    tool_name, "glass_catalog_unavailable",
                    "glass catalog not built on this machine - run "
                    "packages/optivibe-reference/scripts/build_glass_catalog.py "
                    "(reads your licensed install's Glasscat .agf files; see "
                    "PROVENANCE.md)",
                )
            elif conn is None:
                # Defense-in-depth invariant: NO kind may reach the handler with a
                # None conn. The three branches above name the SHIPPED kinds; a
                # non-standard manifest kind (a custom/future ToolSpec.kind) routes
                # via _conn_for to the merit conn, which — since the local-build change — can be
                # None in the degraded (unbuilt operand catalog) state. Before 2b the
                # merit conn opened eagerly and was never None, so this fallthrough
                # was unreachable; the degrade change legalized it (L26). A general
                # guard answers operand_catalog_unavailable rather than calling the
                # handler with None. Both auditors cross-flagged this path.
                result = error_envelope(
                    tool_name, "operand_catalog_unavailable",
                    "reference catalog not built; run scripts/build_vendor_data.py "
                    "(see PROVENANCE.md)",
                )
            else:
                result = spec.handler(conn, params)
            # The Dispatcher is the SOLE author of the operand `domain` label. Stamp it
            # ONLY on a SUCCESS-shaped operand result: error envelopes (ok=False,
            # error_family present) stay byte-identical and domain-free, matching the
            # glass/manual error envelopes (cross-tool uniformity). The handler authors
            # no `domain`, so this is the only writer - no overwrite, no desync.
            if (
                spec.kind == _OPERAND_KIND
                and operand_domain is not None
                and isinstance(result, dict)
                and result.get("ok") is True
                and "error_family" not in result
            ):
                result["domain"] = operand_domain
            return {
                "ok": True,
                "tool": tool_name,
                "result": result,
                "error": None,
                "error_family": None,
            }
        except BaseException as exc:  # noqa: BLE001 — dispatch must NEVER raise
            try:
                error_family = self._classify(exc)
            except Exception:  # noqa: BLE001 — classification must not escape
                error_family = "internal"
            return {
                "ok": False,
                "tool": tool_name,
                "result": None,
                "error": _safe_error_text(exc),
                "error_family": error_family,
            }

    @staticmethod
    def _classify(exc):
        """Classify a raised exception into an ``error_family`` string.

        A ``ReferenceLayerError`` -> its own ``error_family``; anything else (a
        Python builtin — we drove the DB/handler wrong) -> ``"internal"``.
        """
        if isinstance(exc, ReferenceLayerError):
            return exc.error_family
        return "internal"
