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


class SessionChannelDeadError(SessionClosedError):
    """The engine's remoting channel was OBSERVED dead; this session is TERMINAL.

    Raised by ``Dispatcher.dispatch``'s channel gate (before any handler runs) and
    by ``ZemaxSession._open_locked`` (at the create), so once a fault has been
    observed neither a served call nor a re-open is reachable.

    Subclasses ``SessionClosedError`` so every existing ``except SessionClosedError``
    still catches it (behavioural compat); its own ``error_family`` overrides the
    parent's ``"session_closed"`` on the wire.

    OptiVibe does NOT re-open after an observed channel fault. That is a design
    ruling, not a limitation discovered at runtime: of the two fault flavours
    measured, one leaves the engine process alive and re-openable and the other
    kills it and is not recoverable in-process, and nothing readable from here
    tells the two apart at refusal time. Re-opening would therefore succeed
    sometimes and mislead the rest of the time, so the session is TERMINAL either
    way and the remedy is a restart of the MCP process, which the message names.
    """

    error_family = "engine_channel_dead"


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


class SolveDrivenError(SurfaceWriteError):
    """A write was refused because the target cell is DRIVEN by a solve.

    A ``SurfaceWriteError`` subclass, on the house pattern of ``AsphereWriteError`` /
    ``GrinWriteError`` / ``CatalogLoadError``: it inherits the structured
    ``(field, intended, actual, surface)`` attrs AND the existing
    ``except SurfaceWriteError`` envelope plumbing, so it needs NO dispatch wiring and NO
    ``__init__``. Only ``error_family`` is overridden, to the DISTINCT ``solve_driven``,
    because the remedy is unlike any other write failure: nothing about the VALUE is
    wrong, and retrying will not help.

    WHY REFUSING BEATS THE READ-BACK ORACLE HERE — MEASURED, and it corrects an earlier
    note. That note said a write to a driven cell raises, so "the raise is therefore
    certain". A live probe falsified it twice. On ``semi_diameter`` under a
    ``SurfacePickup``, a bare write
    returns ``ok: true``, THE VALUE MOVES, and the solve is silently converted
    ``SurfacePickup -> Fixed``: a destroyed design relationship reported as success, which
    NO read-back oracle can catch because the value did land. Writing a driven cell its own
    current value does not raise either. So on that cell this guard is the ONLY thing
    standing between an ordinary-looking write and a silent relationship deletion.

    RAISE, NOT A REFUSAL DICT, and that is load-bearing: ``lens_spec.py``'s apply loop does
    not bind ``set_surface``'s return, so a refusal dict would be DISCARDED and the apply
    would report success for a surface it never wrote. The raise routes to apply's "ANY
    throw routes to rollback" broad-except instead.
    """

    error_family = "solve_driven"

    #: The remedy for the DRIVEN arm. ONE constant per arm, consumed once — a second copy
    #: of either is how two call sites come to promise different things.
    #:
    #: IT NAMES ONLY SHIPPED DOORS, and only doors that WORK FOR THIS ARM.
    #: A refusal whose remedy names an action with no door is inadmissible in every
    #: branch. ``set_solve`` / ``clear_solve`` DID NOT EXIST when this constant was
    #: written and the guard was a DENYLIST forbidding their names; **THIS RELEASE SHIPS
    #: BOTH**, so the denylist is INVERTED into the stronger manifest-derived rule — every
    #: tool name either REMEDY constant mentions must be in ``load_manifest()``, which
    #: guards future names too instead of only these two. ``clear_solve`` is
    #: named below as the door for REMOVING the relationship. A door that exists but
    #: cannot resolve THIS refusal is the same defect one step in, and one was shipped:
    #:
    #: THE FALSIFIED SENTENCE, DELETED HERE (external HIGH). This text used to end "…or
    #: re-author the design through apply_lens_spec, whose reset sets non-re-declared
    #: geometry solves Fixed". It is served on EVERY refusal, and it was a door that
    #: PROVABLY CANNOT WORK: ``Variable`` is in ``NON_DRIVING``, so a refusal is BY
    #: CONSTRUCTION always about a DRIVING solve, while the reset's solve arm clears only
    #: ``Variable`` ones. The sentence sent the agent round a loop ending at the same
    #: refusal.
    #:
    #: THE REPLACEMENT IS UNCONDITIONAL, AND IT TOOK A LIVE MEASUREMENT TO GET THERE.
    #: The first cut named the door WITH a condition — "…or when the apply reverts an
    #: omitted asphere on this surface back to Standard", on the audit's reading that a
    #: retype destroys the solve (a probe finding). MEASURED by a live probe:
    #: an asphere->Standard revert
    #: PRESERVES a driving solve on radius, thickness AND conic. The audit's reading was
    #: measured on the opposite transition (Standard -> CB/asphere). So there is no
    #: exception to name:
    #: apply's reset can NEVER clear the solve behind a refusal, and the sentence is
    #: shorter for it. See ``lens_spec._RESET_ARM_B_NOTE`` and its measurement block.
    REMEDY = (
        "Read `solves` on read_surface to see the relationship before writing. To make "
        "this cell an optimizer variable anyway, pass replace_solve=true to "
        "set_variable/vary (the prior solve is reported back). To REMOVE the solve, call "
        "clear_solve(surface=<n>, cell='<token>') — it FREEZES the cell at its CURRENT "
        "value (it does not restore a default or an earlier number), then write. "
        "Otherwise reload a design without the solve — re-authoring through "
        "apply_lens_spec will NOT clear it: its reset clears only Variable solves, so a "
        "driving solve survives and the apply rolls back on this same refusal."
    )

    #: The remedy for the UNKNOWN arm, and it is DIFFERENT ON PURPOSE.
    #:
    #: ``replace_solve=true`` is NOT a door here. It replaces a solve that was READ, and
    #: reports it back as ``replaced_solves``; there is nothing to report when the read
    #: failed, and the hole where the override converted UNKNOWN into mutation
    #: permission is CLOSED (the write once proceeded and returned an envelope
    #: BYTE-IDENTICAL to a clean undriven cell's). Naming it here would name a door this
    #: arm deliberately shuts.
    #: THIS ADDS THE EXPLICIT NEGATIVE, NOT A DOOR. ``set_solve``/``clear_solve`` now exist,
    #: and the instinct on reading this arm is to reach for one of them — so the text says
    #: outright that they refuse this state too (``clear_solve`` CAPTURES the prior solve
    #: before acting, so it hits the same unreadable reading). Ruled over silence because
    #: an earlier failure was served text UNDERSTATING the options at the moment an
    #: agent is stuck: a reader who is not told a door is closed will try it, and read the
    #: same refusal a second time with no new information.
    REMEDY_UNKNOWN = (
        "Read `solves` on read_surface: this cell will be listed under "
        "`solves_unreadable`. replace_solve=true does NOT apply to this refusal — it "
        "replaces a solve that was read, and reports what it replaced, so it is refused "
        "when the reading itself failed. set_solve and clear_solve also refuse this "
        "state — clear_solve captures the prior solve before acting, so it reads the same "
        "unreadable cell. Recover the reading first (reload the design "
        "and read it back); a solve that stays unreadable is an engine-state fault "
        "rather than a design decision."
    )

    @classmethod
    def from_probe(cls, probe, *, cell_token, surface, tool, intended=None):
        """Build the refusal from a ``refuse_if_driven`` probe. ONE factory, not N sites.

        Branches on ``probe["driven"] is True`` / ``is None`` by IDENTITY — the probe is
        TRI-STATE and truthiness would fold UNKNOWN into "not driven", the two-character
        fail-open this whole guard exists to avoid. ``probe`` is read with ``.get`` so a
        malformed probe cannot raise a ``KeyError`` while BUILDING a refusal (which would
        surface as an opaque ``internal`` in place of a precise diagnosis).

        NO ``str()`` COERCION of a solve reading anywhere: ``str(None)`` is the string
        ``"None"``, which is a REAL non-driving solve type, so coercing an unreadable
        reading would render an UNKNOWN cell as a named, harmless one.
        """
        probe = probe if isinstance(probe, dict) else {}
        driven = probe.get("driven")
        solve_type = probe.get("solve_type")
        if driven is True:
            # A REVIEW FINDING: the hazard was stated BACKWARDS, and on the one cell
            # where this guard is the ONLY protection. "Silently discarded" is the
            # radius/thickness behaviour; a live probe measured semi_diameter under a
            # SurfacePickup doing the OPPOSITE — ok:true, the value MOVES, and the solve
            # is converted SurfacePickup -> Fixed. Telling an agent its write would be
            # discarded understates that case into a harmless no-op, when it is the
            # silent DELETION of a design relationship that no read-back oracle can see
            # (the value did land). Both outcomes are named; neither is promised.
            message = (
                f"{cell_token} on surface {surface} is driven by a {solve_type} solve — "
                "the value is the engine's own recomputation, so writing it either goes "
                "nowhere (the read-back agrees with the solve, not with you) or lands "
                "and DESTROYS the relationship, converting the solve to Fixed. Which "
                "one is cell-dependent and neither is what you asked for. "
                f"{tool} refused before mutating anything. {cls.REMEDY}"
            )
        else:
            reason = probe.get("reason") or "no reason was recorded"
            message = (
                f"{cell_token} on surface {surface}: the solve could not be read "
                f"({reason}) — refusing rather than writing through a solve we cannot "
                f"see. {tool} refused before mutating anything. {cls.REMEDY_UNKNOWN}"
            )
        return cls(message, field=cell_token, intended=intended,
                   actual=solve_type, surface=surface)


class SolvePartialStateError(SurfaceWriteError):
    """A solve author MUTATED the cell and the RESTORE could not be proven.

    THIS IS THE ONE OUTCOME ``set_solve`` / ``clear_solve`` cannot make safe, so it is
    given its own family rather than being folded into ``surface_write``. Every OTHER
    post-mutation failure in those tools ends with the prior solve restored AND the
    restoration verified, and reports as ``surface_write`` saying so. This class means the
    opposite: the write is known to have been attempted, the rollback is NOT known to have
    landed, and the cell's solve state is therefore UNKNOWN rather than either state.

    WHY A FAMILY AND NOT A ``partial_state: true`` BOOLEAN, ruled at a human
    checkpoint. The envelope-shaped precedents (``apply_lens_spec``'s
    ``{ok:false, rolled_back, partial_state, checkpoint}``) reach that flag by CATCHING
    and RETURNING a dict, so they never raise past their own boundary.
    ``set_solve``/``clear_solve`` RAISE, and the dispatch failure envelope is the frozen
    5-key shape ``ok/tool/result/error/error_family`` which projects NO exception
    attributes — so a RAISING tool genuinely cannot put a boolean on that wire. Adding
    conditional plumbing to the highest-blast-radius module to duplicate a signal the
    family already carries was rejected. **The machine-readable partial-state signal
    is ``error_family == "solve_partial_state"``, 1:1 with the condition, and the served
    docs say so. No consumer may check for a ``partial_state`` boolean here.**

    THE MESSAGE'S FIRST CLAUSE STATES THE UNKNOWN, before anything else. An agent that
    reads only the opening of a refusal must not come away thinking the cell holds either
    the old or the new solve. It then names the prior type, the observed post-restore type
    and value where readable, WHICH restore step failed (``type`` / ``value`` / ``proof``),
    and the remedy.

    THE REMEDY IS THE NATIVE CHECKPOINT, and that is not a generic "reload": a
    ``.zmx`` written by ``save_snapshot``/``save_candidate`` is the only snapshot that
    preserves solve state, so ``load_design`` on the last native checkpoint is the
    recovery, and re-authoring from a LensSpec is NOT (the flat schema cannot carry a
    solve at all).
    """

    error_family = "solve_partial_state"

    #: ONE constant, consumed once — a second copy is how two call sites come to promise
    #: different things.
    REMEDY = (
        "Reload the last native checkpoint with load_design — a .zmx written by "
        "save_snapshot/save_candidate is the only snapshot that preserves solve state, "
        "and re-applying a lens spec will NOT restore it because the flat spec "
        "schema carries no solves. Read `solves` on read_surface first to see what this "
        "cell actually holds now."
    )


#: The EXPLICIT channel a handler attaches a partial-state finding to before letting an
#: abort travel unchanged. ONE definition, read by the writers
#: (``surface_solve._restore``, ``cb_surface``) and by the single reader
#: (``server.Dispatcher._classify``) — a second copy of the string is how the two ends
#: come to disagree silently.
#:
#: WHY NOT ``__context__``, which was used before. Python assigns ``__context__`` IMPLICITLY to
#: whatever exception happened to be in flight, so an abort raised anywhere inside a
#: ``HarnessError`` handler acquires one for free — and ``_classify`` then renamed that
#: unrelated abort with the context's family. A review reproduced it: a
#: ``ToolParamError`` being handled, an independent ``KeyboardInterrupt`` raised, and
#: ``_classify`` answering ``"tool_param"``. A marker only a handler writes cannot be
#: acquired by accident. ``__context__`` is still SET by those writers, for a human
#: reading a traceback; it is simply no longer what decides the family.
PARTIAL_STATE_ATTR = "_optivibe_partial_state"


class CbPartialStateError(SurfaceWriteError):
    """A coordinate-break author MUTATED the editor and an ABORT interrupted it.

    A ``SurfaceWriteError`` subclass so it inherits the structured attrs; only the
    ``error_family`` is distinct. It is NEVER raised into dispatch — the CB tools return
    never-raise envelopes for every ``Exception``, and this class exists solely for the
    one exit those envelopes cannot cover: a ``KeyboardInterrupt`` / ``SystemExit``
    arriving after a CB sub-step has been entered on the engine.

    On that path the abort MUST travel unchanged, so the tool's ``committed`` /
    ``attempted`` ledgers — the whole point of the two-ledger design — would otherwise
    die with the frame and dispatch would serve a bare ``internal``. This class is the
    finding, emitted durably to the interaction log and attached to the abort through
    ``PARTIAL_STATE_ATTR``, so the family reaching the MCP caller is TRUE instead of
    opaque while the exception that reaches a Python caller is still the one that
    actually happened.
    """

    error_family = "cb_partial_state"


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


class PromoteVerdictUnboundError(ToolError):
    """``promote_best`` REFUSED: **we could not ask** the criteria contract.

    Parity class for the ``promote_verdict_unbound`` family (the WIRE contract is
    the ``error_family`` string the handler constructs in its
    ``{ok:false}`` envelope, never an ``isinstance`` check; this class is never
    raised). Its members are every gate-emitted refusal: an unreadable contract, a
    challenger with no bound scorecard or one that would not bind, a champion that
    could not be read or digested, a champion no validated record binds, two
    validated records naming different cards, the gate's own never-raise net (which
    also catches a referee token outside the frozen vocabulary), and a set of bytes
    whose proven design identity is not the caller's ``design_name``.

    **Every member is UNKNOWN, so every member fails CLOSED.** DISJOINT from
    ``promote_referee_refused`` — that family had an ANSWER and the answer was no.
    DISJOINT from ``promote_candidate_owner_mismatch``, which asks *which artifact*;
    this family's name clause asks *which design*, a third question, which is why
    it is folded into neither.

    NOT overridable by ``force``: the contract guard is absolute (a deliberate decision,
    and ``force`` is not in scope where the question is asked). The in-band remedies
    are to produce a bound scorecard for these bytes (``save_candidate`` under the
    design's own name) or to unset ``OPTIVIBE_CRITERIA_ROOT``.
    """

    error_family = "promote_verdict_unbound"


class PromoteRefereeRefusedError(ToolError):
    """``promote_best`` REFUSED: **we asked, and the criteria referee said no.**

    Parity class for the ``promote_referee_refused`` family (the WIRE contract is
    the ``error_family`` string, never ``isinstance``; never
    raised). It carries the referee's own token — a gating regression, a protected
    margin traded away, an ineligible NOT-MEASURED ``required`` row, a tie, or one
    of the ``incomparable_*`` tokens — plus the full ``referee`` disclosure block.

    DISJOINT from ``promote_verdict_unbound`` (which could not ask at all), and
    DISJOINT from ``promote_clearance_violation`` / ``promote_clearance_indeterminate``,
    which are verdicts about the GEOMETRY; this is a verdict about whether the
    criteria contract permits the move. NOT overridable by ``force``.
    """

    error_family = "promote_referee_refused"


class AnalysisResultError(ToolError):
    """A results-extraction failure carrying a structured ``family``.

    The analysis tier distinguishes operationally distinct failure outcomes the
    agent must branch on (locked): ``analysis_empty`` (the analysis ran but
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
    outcomes the agent must branch on (locked Decision 1): ``optimize_no_variables``
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

    The guards catch ``BaseException`` and deliberately do NOT re-raise (
    P-2). This renders a string for an exception that has ALREADY been
    caught upstream — it is envelope construction, not work on an abort's travel
    path — so the ``_safe_error_text`` rule applies rather than the travel-path
    re-raise rule. Narrow, this falsified ``Dispatcher.dispatch``'s "NEVER
    raises": a ``__str__`` raising ``KeyboardInterrupt`` escaped the classifier.
    """
    try:
        return str(exc)
    except BaseException:  # noqa: BLE001 — __str__ raised; try a guarded repr
        try:
            return repr(exc)
        except BaseException:  # noqa: BLE001 — repr raised too; bare placeholder
            return "<unprintable exception message>"
