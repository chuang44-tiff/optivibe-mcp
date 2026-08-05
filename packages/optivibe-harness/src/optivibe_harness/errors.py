"""errors.py — the harness error hierarchy + .NET-exception mapping.

The session/dispatch tier needs a small, typed error family so callers get a
clean domain error (with a stable ``error_family`` string) instead of a raw .NET
IPC traceback. Every class carries a class-level ``error_family`` so the dispatch
envelope can surface it verbatim.

``map_dotnet_exception`` keys on the ``qualified_type = "{module}.{name}"``
convention already used by ``interaction_log._exc_to_dict``: a
``System.Runtime.Remoting.RemotingException`` — THE shape of every use-after-close
/ double-close / transport-loss event the probe captured — maps to
``SessionClosedError``; any other ``System.*`` engine exception maps to the
``SessionError`` base; Python builtins (we called pythonnet wrong) are NOT mapped
(returns ``None``) so they propagate / are classified as internal upstream.

Live ZOS-API integration: N/A this tier (pure-Python error classes; the .NET
exception shapes are grounded by the captured probe fixture, not a live backend).
"""


class HarnessError(Exception):
    """Base class for every OptiVibe harness domain error."""

    error_family = "harness"


class SessionError(HarnessError):
    """A session-lifecycle / engine-transport error."""

    error_family = "session"


class SessionClosedError(SessionError):
    """The session is closed (use-after-close) or the transport was lost.

    Use-after-close, double-close, AND transport-loss ALL surface as
    a single ``System.Runtime.Remoting.RemotingException`` — so they ALL map to
    this one class (probe-justified; there is intentionally no separate
    ``SessionTransportError``).
    """

    error_family = "session_closed"


class SessionConnectError(SessionError):
    """Failed to acquire / validate a live engine during ``open()``."""

    error_family = "session_connect"


class SessionConnectTimeoutError(SessionConnectError):
    """``open()`` exceeded ``connect_timeout_s`` — the engine open WEDGED (a hung
    COM/remoting handshake or a wedged orphan). The MCP is ALIVE and returned
    control; a leaked daemon worker may complete late (reaped by the terminal /
    close-time sweep). DISTINCT from a prompt connect failure (None app / invalid
    license) so the agent tells 'open wedged, MCP alive' apart from 'license
    invalid'.

    Subclasses ``SessionConnectError`` so any ``except SessionConnectError`` still
    catches it (behavioural compat); its own ``error_family`` overrides the
    parent's ``"session_connect"``. No structured attrs — the message carries the
    timeout value (the hang-watchdog §2).
    """

    error_family = "engine_connect_timeout"


class SessionMisuseError(SessionError):
    """A session method was called in a way the contract forbids.

    Raised when a dispatch handler re-enters ``session.close()`` on the SAME
    thread that is mid-dispatch (the RLock is already owned by this thread). A
    handler closing the engine it is being dispatched ON, mid-call, is a
    programming error that must surface loudly — but dispatch's never-raise
    envelope catches it, so the server still returns a clean error envelope
    rather than crashing.
    """

    error_family = "session_misuse"


class ToolError(HarnessError):
    """Base class for dispatch/tool-layer errors."""

    error_family = "tool"


class UnknownToolError(ToolError):
    """The dispatched tool name is not in the manifest."""

    error_family = "unknown_tool"


class ToolParamError(ToolError):
    """A required parameter for the dispatched tool was missing."""

    error_family = "tool_param"


class SurfaceWriteError(ToolError):
    """A typed LDE/SystemData write did NOT take effect (read-back mismatch).

    LDE writes take effect IMMEDIATELY and the typed API does NOT
    validate semantics — a rejected/no-op write (e.g. ``IsStop=True`` on the
    image surface, or a count that did not change after insert/remove, or a
    failed ``SelectWavelengthPreset``) is SILENT. Every mutator therefore
    read-back-verifies; on a mismatch it raises this error so the dispatch
    envelope surfaces a structured ``error_family="surface_write"`` rather than
    burying the failed mutation inside an ``ok:true`` result.

    Carries structured attributes ``(field, intended, actual, surface)`` so the
    envelope error message is fully diagnostic.
    """

    error_family = "surface_write"

    def __init__(self, message, *, field=None, intended=None, actual=None, surface=None):
        super().__init__(message)
        self.field = field
        self.intended = intended
        self.actual = actual
        self.surface = surface


class ApertureWriteError(SurfaceWriteError):
    """A per-surface aperture write did NOT take effect / was not proven (surface-aperture).

    A ``SurfaceWriteError`` subclass so it inherits the structured
    ``(field, intended, actual, surface)`` attrs AND the existing
    ``except SurfaceWriteError`` envelope plumbing (no new dispatch wiring). Only
    the ``error_family`` is overridden to the DISTINCT ``"surface_aperture"`` so the
    agent can branch on "the aperture write failed" (the remedy is to re-author or
    clear the APERTURE on that surface, distinct from a generic LDE geometry write).

    Raised by ``aperture_surface`` on a read-back-as-proof failure: the live
    ``CurrentType`` does not match the requested type (a ChangeType no-op), a typed
    field did not read back == intended (the §1a base-interface silent no-op), or a
    User-type ``ApertureFile`` read back ``"None"`` (the engine silently dropped a
    non-resolvable path, probe §4). The ``_typed_view`` fail-closed resolver also
    raises this when the ``_S_<TypeName>`` view accessor is absent (engine drift) —
    refusing rather than falling through to the bare-settings silent-no-op trap.
    """

    error_family = "surface_aperture"


class AsphereWriteError(SurfaceWriteError):
    """An even-asphere coefficient write did NOT take effect / was not proven (surface-asphere).

    A ``SurfaceWriteError`` subclass so it inherits the structured
    ``(field, intended, actual, surface)`` attrs AND the existing
    ``except SurfaceWriteError`` envelope plumbing (no new dispatch wiring). Only
    the ``error_family`` is overridden to the DISTINCT ``"surface_asphere"`` so the
    agent can branch on "the asphere write failed" (the remedy is to re-author the
    EvenAspheric coefficients on that surface, distinct from a generic LDE geometry
    write).

    Raised by ``_asphere_cells`` / ``asphere_surface`` on a read-back-as-proof
    failure: a ChangeType to EvenAspheric that silently no-opped (the surface did not
    retype), a Par cell whose live Header/DataType drifted from the catalog, a
    coefficient that did not read back == intended (the typed-setter / collapse-to-
    zero silent no-op), or an unreadable ``row.Type``. The cell substrate refuses
    rather than write/read the wrong cell or claim an unverified coefficient.
    """

    error_family = "surface_asphere"


class GrinWriteError(SurfaceWriteError):
    """A GRIN coefficient write did NOT take effect / was not proven (surface-grin).

    A ``SurfaceWriteError`` subclass so it inherits the structured
    ``(field, intended, actual, surface)`` attrs AND the existing
    ``except SurfaceWriteError`` envelope plumbing (no new dispatch wiring). Only
    the ``error_family`` is overridden to the DISTINCT ``"surface_grin"`` so the
    agent can branch on "the GRIN write failed" (the remedy is to re-author the
    GRIN profile on that surface, distinct from a generic LDE geometry write).

    Raised by ``_grin_cells`` / ``grin_surface`` on a read-back-as-proof failure: a
    ChangeType to a GRIN type that silently no-opped (the surface did not retype), a
    Par cell whose live Header/DataType drifted from the ``GRIN_PARAMS`` catalog, a
    coefficient that did not read back == intended (the typed-setter / collapse-to-
    zero silent no-op — including the ZERO-BOUNDARY case where a 0.0 write no-ops
    over a stale-tiny term or a tiny intended collapses to 0.0), or an unreadable
    ``row.Type``. The cell substrate refuses rather than write/read the wrong cell
    or claim an unverified coefficient. (The shared ``_revert_to_standard_proven``
    reused by the Standard-revert arm raises the BASE ``SurfaceWriteError`` — family
    ``"surface_write"`` — so the GRIN family contract is a QUARTET: ``grin_param`` /
    ``surface_grin`` / ``grin_write`` / ``surface_write`` (revert only).)
    """

    error_family = "surface_grin"


class CatalogLoadError(SurfaceWriteError):
    """A material-catalog load did NOT take effect / was not proven (catalog-load).

    A ``SurfaceWriteError`` subclass so it inherits the structured
    ``(field, intended, actual, surface)`` attrs AND the existing
    ``except SurfaceWriteError`` envelope plumbing (no new dispatch wiring). Only
    the ``error_family`` is overridden to the DISTINCT ``"catalog_load"`` so the
    agent can branch on "the catalog load failed" (the remedy is to retry / pick a
    different catalog, distinct from a generic LDE geometry write).

    Raised by ``load_catalog`` on a read-back-as-proof failure: ``AddCatalog``'s
    bool return LIES (``AddCatalog("BOGUS")`` returns ``True`` yet
    no-ops; an already-in-use catalog returns ``False`` though it IS in use), so
    the tool validates the requested name against ``GetAvailableCatalogs()`` FIRST,
    then proves the load with ``IsCatalogInUse`` AFTER. If a VALIDATED name's
    ``AddCatalog`` silently no-ops (``IsCatalogInUse`` stays ``False``), this is
    raised rather than trusting the lying bool.
    """

    error_family = "catalog_load"


class ClearanceError(ToolError):
    """A ``check_clearance`` geometry-read failure (geometry-readouts cycle).

    ``check_clearance`` is a READ-ONLY audit that NEVER raises past its boundary:
    a bad ``min_air``/``min_glass`` param surfaces as a ``clearance_param``
    envelope; a total geometry-read failure (an unreadable LDE) surfaces as a
    ``clearance_unavailable`` envelope. This class exists for parity with the
    other tiers' typed families (and so a ``ToolParamError`` raised deep in the
    validation path keeps a ``clearance_param`` family at the handler boundary);
    the WIRE contract is the ``error_family`` string the handler constructs, never
    ``isinstance``. A degraded SINGLE surface (one wedged ``GetGlobalMatrix`` /
    sag read) is NOT an error — it degrades that field to ``None`` + a flag and
    the audit continues (``ok:true``).
    """

    error_family = "clearance_unavailable"


class PromoteClearanceViolationError(ToolError):
    """The save-time clearance gate REFUSED a promote: a confirmed thin gap.

    Parity class for the ``promote_clearance_violation`` family (mirrors
    ``ClearanceError`` — the WIRE contract is the ``error_family`` string the
    ``promote_best`` handler constructs in its ``{ok:false}`` envelope, never an
    ``isinstance`` check; this class is never raised). ``promote_best`` runs
    ``check_clearance`` at ``config="all"`` AFTER the seq-existence check and BEFORE
    the atomic copy; a thin (manufacturably-too-thin center/edge/air) verdict refuses
    the promote with this family unless the caller passes ``force=True``. DISTINCT
    from ``promote_clearance_indeterminate`` (could-not-audit) so the agent branches
    on "it IS thin" vs "we could not certify it".
    """

    error_family = "promote_clearance_violation"


class PromoteClearanceIndeterminateError(ToolError):
    """The save-time clearance gate REFUSED a promote: clearance could not be audited.

    Parity class for the ``promote_clearance_indeterminate`` family (mirrors
    ``ClearanceError`` / ``PromoteClearanceViolationError`` — the WIRE contract is the
    ``error_family`` string the handler constructs, never ``isinstance``; never
    raised). ``promote_best`` refuses a keeper it could not certify (a
    ``check_clearance`` throw, a malformed envelope, or an ``"all"`` sweep with
    incomplete coverage) unless ``force=True`` is passed. You cannot certify a keeper
    you could not audit; DISJOINT from ``promote_clearance_violation`` (a confirmed
    thin gap).
    """

    error_family = "promote_clearance_indeterminate"


class PromoteCandidateOwnerMismatchError(ToolError):
    """``promote_best`` REFUSED: the seq'd candidate belongs to another design_name.

    Parity class for the ``promote_candidate_owner_mismatch`` family. DISJOINT from
    ``promote_clearance_violation`` / ``promote_clearance_indeterminate``: those are
    verdicts about the GEOMETRY; this is a verdict about WHICH ARTIFACT — the tool
    refuses before any audit, because auditing the live session and then publishing
    another design's file is exactly the silent-wrong being closed. NOT overridable by
    ``force`` (which asserts "I accept this geometry", never "I accept these bytes").
    """

    error_family = "promote_candidate_owner_mismatch"


class AnalysisResultError(ToolError):
    """A results-extraction failure carrying a structured ``family``.

    The analysis tier distinguishes operationally distinct failure outcomes the
    agent must branch on (§1): ``analysis_empty`` (the analysis ran but
    produced nothing — a design/config signal), ``analysis_malformed`` (the
    analysis returned a structurally-wrong result — a misaligned X/Y length or an
    arity-drifted ray tuple — that must never green as ok:true), ``batch_unavailable``
    (the batch ray-trace tool was already open — an infrastructure signal),
    ``analysis_gate`` (a graphic durability gate failed — a capture signal), and
    ``analysis`` (a generic analysis error). A single exception type carries the
    discriminator on
    a ``family`` instance attribute so the type hierarchy stays one class; the
    WIRE contract is the ``error_family`` string, never ``isinstance``.

    The class-level ``error_family`` is the fallback family used if dispatch ever
    envelopes a raised instance; ``family`` (per-instance) is what the analysis tools
    surface in their own ``{ok:false}`` envelope (they construct the envelope and
    do NOT raise past their boundary for expected failures).
    """

    error_family = "analysis"

    def __init__(self, message, *, family="analysis"):
        super().__init__(message)
        self.family = family


class ScaleError(ToolError):
    """A ``scale_lens`` failure carrying a structured ``family``.

    Families: ``scale_param`` (bad mode/value, pre-mutation, zero engine touch),
    ``scale_efl_undefined`` (``to_efl`` on an afocal/sentinel/~0 current EFL, or a
    computed factor with a sign/degenerate mismatch — a geometric scale can never
    flip EFL sign), ``scale_noop`` (the native scale ran clean yet the read-back
    proves nothing moved — the by-units inert-no-op trap; a clean call is NOT
    proof), ``scale_readback_failed`` (a partial/wrong scale: EFL misses the target
    OR the EFL/TOTR ratio != the applied factor OR f/# drifted OR an armed leg's
    after-value is unreadable), ``scale_write`` (an engine throw during the apply,
    a busy ``OpenScale`` slot, or ``RunAndWaitForCompletion`` returning False). A
    single exception type carries the discriminator on a ``family`` instance
    attribute so the hierarchy stays one class; the WIRE contract is the
    ``error_family`` string the handler surfaces, never ``isinstance`` (mirrors
    ``OptimizeError`` / ``AnalysisResultError`` exactly).

    The class-level ``error_family`` is the fallback used if dispatch ever
    envelopes a raised instance; ``family`` (per-instance) is what the handler
    surfaces in its own ``{ok:false}`` envelope (the handler constructs the
    envelope and does NOT raise past its ``@_never_raise`` boundary for the
    semantic refusals).
    """

    error_family = "scale"

    def __init__(self, message, *, family="scale"):
        super().__init__(message)
        self.family = family


class OptimizeError(ToolError):
    """An optimize-tier failure carrying a structured ``family``.

    The optimization tier distinguishes operationally distinct failure
    outcomes the agent must branch on: ``optimize_no_variables``
    (the preflight found no variable cell to drive), ``optimize_no_merit`` (no
    merit operands / a 0.0 placeholder / a non-finite merit), ``optimize_unavailable``
    (``OpenLocalOptimization`` returned ``None`` — the single-instance optimizer is
    already open), ``optimize_run_failed`` (the engine reported a COMPLETED-but-failed
    run via ``RunAndWaitForCompletion``->False / ``opt.Succeeded``->False),
    ``optimize_param`` (a bad ``cycles``/``cores``/``max_passes``/``algorithm`` value),
    and ``optimize`` (a generic optimize error). A single exception type carries the
    discriminator on a ``family`` instance attribute so the type hierarchy stays one
    class; the WIRE contract is the ``error_family`` string, never ``isinstance``
    (mirrors ``AnalysisResultError`` exactly — the analysis precedent).

    The dummy-stop guard families (constructed DIRECTLY as ``error_envelope``
    dicts in ``optimize_run`` — these are short-circuit refusals that open NO
    optimizer, so they never raise an ``OptimizeError`` and need no reap):

    - ``optimize_stop_on_glass_vertex`` — the default guard (``require_free_stop``)
      refused: the aperture stop is on a glass vertex (mechanically unbuildable +
      denies the optimizer the stop-position DOF). Remedy: ``normalize_stop`` first,
      or pass ``auto_normalize=true``.
    - ``optimize_stop_unfixable`` — ``auto_normalize`` ran ``normalize_stop`` but it
      fail-closed (e.g. a cemented interior vertex with no insertable air gap ->
      ``normalize_no_airspace``); the optimizer still opened NOTHING.
    - ``optimize_stop_indeterminate`` — a surface ``Material`` read FAILED, so
      the stop classification is indeterminate. The guard REFUSES (it never guesses
      a vertex / never authorizes ``auto_normalize`` to mutate an already-valid
      system from a transient API hiccup); the optimizer opened NOTHING. Distinct
      from ``optimize_stop_on_glass_vertex`` precisely so ``auto_normalize`` (which
      fires only on a positive vertex detection) cannot mutate on an unreadable stop.

    The ``normalize_stop`` tool itself surfaces its OWN families on its error
    path (``normalize_no_stop`` / ``normalize_no_airspace`` / ``normalize_param`` /
    ``surface_write``) as ``error_envelope`` dicts — they are not modelled as a
    distinct exception class either (the read-back firewall raises the shared
    ``SurfaceWriteError`` mid-refactor; everything else is a structured envelope).

    NOTE: ``optimize_diverged`` is a VERDICT on a completed run, NOT an error — a
    diverged run returns ``ok=True`` with ``verdict="diverged"`` and does NOT raise
    or construct an ``OptimizeError`` (§d).

    The class-level ``error_family`` is the fallback family used if dispatch ever
    envelopes a raised instance; ``family`` (per-instance) is what the optimize tools
    surface in their own ``{ok:false}`` envelope (they construct the envelope and do
    NOT raise past their boundary for expected failures).
    """

    error_family = "optimize"

    def __init__(self, message, *, family="optimize"):
        super().__init__(message)
        self.family = family


# The single .NET transport/closed shape the probe captured. Matched on
# the qualified type so a wrapped engine exception is distinguished from a Python
# builtin — never on a fragile message substring.
_REMOTING_QUALIFIED_TYPE = "System.Runtime.Remoting.RemotingException"


def _qualified_type(exc: BaseException) -> str:
    """Return ``"{module}.{name}"`` for ``exc`` (interaction_log convention)."""
    etype = type(exc)
    module = getattr(etype, "__module__", "builtins")
    name = getattr(etype, "__name__", "?")
    return f"{module}.{name}"


def _is_dotnet_surfaced(exc) -> bool:
    """True only when ``exc`` is a pythonnet/.NET-surfaced exception.

    The module must be EXACTLY ``"System"`` or start with ``"System."`` AND the
    class must NOT be a Python type that merely lives in such a module. A genuine
    bridged .NET exception is a subclass of ``System.Exception`` (pythonnet maps
    .NET exceptions onto Python ``Exception`` subclasses), and crucially it is NOT
    one of the Python *builtins* — a stdlib/user class whose ``__module__`` happens
    to be ``"System"`` must not be mis-wrapped as an engine error.
    """
    etype = type(exc)
    module = getattr(etype, "__module__", "builtins")
    if module != "System" and not module.startswith("System."):
        return False
    # Refuse to treat a Python builtin (``builtins.*``) as .NET even if its module
    # string were spoofed — builtins is the canonical Python namespace, never .NET.
    if module == "builtins":
        return False
    # A real pythonnet exception subclasses Python ``Exception`` like any other,
    # but the discriminator the spec asks for is: it must NOT be a known Python
    # builtin exception type. Builtin exception types live in the ``builtins``
    # module, so a class whose ``__module__`` is ``System``/``System.*`` and is
    # NOT defined in ``builtins`` is the .NET-surfaced case we accept.
    import builtins as _builtins

    if getattr(_builtins, etype.__name__, None) is etype:
        return False
    return True


def map_dotnet_exception(exc):
    """Map a raised .NET exception to a typed ``HarnessError`` (or ``None``).

    - ``System.Runtime.Remoting.RemotingException`` -> ``SessionClosedError``
      (every use-after-close / double-close / transport-loss shape).
    - any other pythonnet/.NET-surfaced ``System.*`` (a wrapped engine exception)
      -> ``SessionError``.
    - anything else (Python builtins -> ``builtins.*``; a Python class that merely
      lives in a ``System``-named module; we called pythonnet wrong) -> ``None``
      (NOT a session error; the caller classifies it as internal).

    The returned error chains the original via ``raise mapped from exc`` so the
    underlying .NET traceback is preserved.

    Defense-in-depth (NEW-3): a hostile / broken surfaced exception can have a
    ``__str__``/``ToString()`` that itself RAISES, or a type whose introspection
    raises. This function must NEVER raise — every step (type introspection, the
    ``str(exc)`` message build) is guarded; on any introspection failure it
    returns a best-effort mapping (or ``None``) rather than propagating. The
    dispatcher's outer guard remains in place too (belt and suspenders).
    """
    try:
        qualified = _qualified_type(exc)
    except Exception:  # noqa: BLE001 — type introspection must not raise
        return None
    if qualified == _REMOTING_QUALIFIED_TYPE:
        mapped = SessionClosedError(_safe_str(exc))
        mapped.__cause__ = exc
        return mapped
    # Only map an "other System.*" exception when it is genuinely a pythonnet/.NET
    # surfaced type — a Python stdlib/user class whose module merely starts with
    # "System" must NOT be mis-wrapped as an engine/session error.
    try:
        surfaced = _is_dotnet_surfaced(exc)
    except Exception:  # noqa: BLE001 — introspection must not raise
        return None
    if surfaced:
        mapped = SessionError(_safe_str(exc))
        mapped.__cause__ = exc
        return mapped
    return None


def _safe_str(exc) -> str:
    """``str(exc)`` that NEVER raises (NEW-3).

    A surfaced .NET exception (or a hostile Python one) can have a throwing
    ``__str__``/``ToString()``. Fall back to a guarded ``repr``, then to a bare
    placeholder, so building a mapped error's message can never itself raise.
    """
    try:
        return str(exc)
    except Exception:  # noqa: BLE001 — __str__ raised; try a guarded repr
        try:
            return repr(exc)
        except Exception:  # noqa: BLE001 — repr raised too; bare placeholder
            return "<unprintable exception message>"
