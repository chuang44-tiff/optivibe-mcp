"""server.py — the dispatch core (NO mcp import).

``Dispatcher`` is the backend-agnostic request router: it validates the tool name
+ required-parameter PRESENCE, serializes the call onto the session's single
``_lock`` (single-seat: one engine, requests must not race), wraps the
handler in the ``logger.record_call`` audit seam, and ALWAYS returns a uniform
envelope — it NEVER raises out to the caller.

Exception classification on a handler failure:
- a raised ``HarnessError`` (incl. ``SessionClosedError`` a handler hits on a
  closed engine) -> its ``error_family``;
- a raised .NET exception -> ``map_dotnet_exception`` (RemotingException ->
  ``session_closed``; other ``System.*`` -> ``session``);
- a Python builtin (``builtins.AttributeError`` / ``TypeError`` — we drove
  pythonnet wrong) -> ``error_family="internal"``.

``mcp`` is NOT imported here (it is not in the env). The MCP adapter lives
in ``server_mcp.py`` and imports ``mcp`` lazily.

Live ZOS-API integration: the dispatch path is exercised by the live test
(real ``get_system_info``); unit-tested here against a fake session/handler.
"""
import importlib
import math
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Tuple

from . import _io
from .errors import (
    HarnessError,
    SessionChannelDeadError,
    ToolParamError,
    UnknownToolError,
    map_dotnet_exception,
)
from .session import CHANNEL_DEAD, CHANNEL_DEAD_CANNOT_NAME_MESSAGE


def _channel_dead_refusal_text(session):
    """Best-effort refusal prose. NEVER raises; ALWAYS returns a non-empty str.

    Building the message is the gate's SECOND fallible callable,
    and it runs AFTER the observation. It must therefore NOT be able to turn an
    observed DEAD into a served call. Any failure — a missing attribute, a
    throwing descriptor, a throwing call, or a non-str / blank return — falls back
    to the module-level ``CHANNEL_DEAD_CANNOT_NAME_MESSAGE`` constant, which
    names the same remedy and touches ``session`` not at all.

    That constant is the honest text for this path: its "(it was not set, or could
    not be read)" clause is exactly the situation — the folder could not be read.
    Reusing it also keeps ALL refusal prose in one module behind one builder, so
    the wording review stays a single-site read.
    """
    try:
        text = session.channel_dead_message()
        if isinstance(text, str) and text.strip():
            return text
    except BaseException as exc:  # noqa: BLE001 — a failed build must never serve
        # ... but an ABORT still keeps travelling. Widening this catch without
        # this re-raise silently cancelled the house convention: the abort was
        # absorbed here, before ``_channel_gate``'s own handler ever saw it
        # (two correct fixes from one round cancelling
        # each other).
        if not isinstance(exc, Exception):
            raise
    return CHANNEL_DEAD_CANNOT_NAME_MESSAGE


def _dead_recorded(session):
    """True iff a DEAD observation is ON RECORD for this session. NEVER raises.

    The arbiter for BOTH of the gate's exits. It must answer "is a DEAD
    observation on record?" — NOT "can I read the public property, and is it
    True?". Those become different questions the moment the property is
    unreadable, and the whole design rests on this one being the durable answer,
    so it consults the RECORDED FIELD as well as the property.

    ===================================================================
    ABSENT IS NOT THE SAME AS UNREADABLE. This is the principle; check
    future edits against it rather than against a list of cases:

        ABSENT      -> "not applicable"  -> False (PROCEED)
        UNREADABLE  -> "unknown"         -> True  (REFUSE)

    ===================================================================

    A bare session double has no channel surface at all: nothing was ever
    observed, so nothing is on record, and it must dispatch exactly as before —
    that is the 208-site guarantee and it is preserved by the ABSENT arm.

    A member that is PRESENT but whose read THREW is a different situation: we
    cannot rule out that an observation WAS recorded, and serving a call on a
    possibly-dead channel is the failure this whole gate exists to prevent. So
    that arm fails CLOSED. An earlier revision collapsed both into ``False``,
    which — as one review put it — "converts failure into permission to serve".
    """
    unreadable = False

    # Both catches below RE-RAISE a non-``Exception``: this function is on an
    # abort's TRAVEL PATH (it reads live session state), so it obeys the same rule
    # as the four other widened catches. Without it the "PROPAGATES —
    # UNCONDITIONALLY" rule three paragraphs down in ``_channel_gate`` was false:
    # a property raising ``KeyboardInterrupt`` on the arbiter re-read produced a
    # refusal instead. Contrast ``_safe_error_text``, which
    # renders a string for an ALREADY-caught exception and therefore must not.
    def _abort_or_unknown(exc):
        if not isinstance(exc, Exception):
            raise exc
        return True  # PRESENT but threw -> unknown

    # 1. The public property, if it is readable and affirmative.
    try:
        if session.channel_dead is True:
            return True
    except AttributeError:
        pass  # ABSENT -> not applicable; the field below decides
    except BaseException as exc:  # noqa: BLE001
        unreadable = _abort_or_unknown(exc)

    # 2. The RECORDED FIELD — durable even when the property was shadowed,
    #    deleted or made unreadable after the latch landed.
    try:
        if getattr(session, "_channel_dead", False) is True:
            return True
    except BaseException as exc:  # noqa: BLE001
        unreadable = _abort_or_unknown(exc)

    return unreadable


def _channel_gate(session):
    """Return a ``SessionChannelDeadError`` to RAISE, or ``None`` to proceed.

    Never raises an ordinary ``Exception``. A non-``Exception`` abort
    (``KeyboardInterrupt``, ``SystemExit``, ``GeneratorExit``, a bare
    ``BaseException``) PROPAGATES — UNCONDITIONALLY, exactly as the table below
    says, with no qualifier. An earlier revision of this sentence added "when
    nothing has been established", contradicting the table 45 lines down on the
    one cell three separate fixes had already got wrong; a reader who stopped at
    the summary got the opposite rule.

    Two reads, in order:

      1. ``session.channel_dead is True``                   -> refuse (free flag read)
      2. ``session.is_open`` and ``observe_channel()`` DEAD -> refuse

    Step 2 does NOT run when the session is not open: a never-opened session must
    reach the handler and raise ``SessionClosedError`` exactly as today, and must
    not pay a probe it cannot answer. Step 2 also does not run once step 1 is True
    — a latched session refuses on a free flag read, engine untouched.

    ===================================================================
    THE INVARIANT, AS A PROPERTY. Check every future edit against THIS
    sentence, not against the control flow that currently implements it:

        A CALL IS SERVED ONLY IF NO DEAD OBSERVATION HAS BEEN RECORDED AT
        ANY POINT DURING THIS GATE INVOCATION.

    ===================================================================

    This is written as a property because the same defect was introduced **FIVE
    times by five different well-intentioned fixes**, each one patching the branch
    the latest finding named:

      1. the first spec fold            -> a consistency review finding
      2. the second spec fold           -> the sibling check's amendment
      3. that amendment's own fix       -> an external review finding
      4. round 2's ``_dead_recorded``   -> a later external review (the EXCEPTION
                                           arm's ``except BaseException: return
                                           False`` fails OPEN)
      5. the same round's normal arm    -> the internal review (that arm
                                           was never routed through the arbiter at
                                           all)

    (Counted in review, which found the earlier "THREE times" in this
    docstring undercounted by 40%. A warning about a dangerous locus that
    understates the danger is the exact defect class this cycle exists to close,
    so the number is stated with its instances rather than asserted.)

    Every one of them reasoned about WHEN an exception happened. That reasoning
    cannot work: **deciding "before or after" from control flow is impossible
    precisely because a throw is what destroys control flow.** Worse, the gate was
    deciding from TWO facts — what ``observe_channel`` returned, and whether DEAD
    was recorded — that ONE call both produces and records; any throw between the
    latch and the return splits them, and no ``except`` clause can know which side
    of that split it is on. ``except Exception:  # PRE-observation`` is a claim
    about causality made by a construct that cannot observe causality.

    A ``ZemaxSession`` subclass can call ``super().observe_channel()``, latch DEAD,
    and then raise — and any scheme that infers phase from "did the call return
    normally" will label that post-observation event PRE-observation and serve the
    call. So can one that latches and then returns ``ALIVE`` (instance 5), which is
    why BOTH arms consult the arbiter and neither trusts the returned verdict alone.

    So the FLAG arbitrates, not the control flow. On the exception path we do not
    ask when the throw happened; we ask the durable question ``_dead_recorded()``:
    *is a DEAD observation on record?* If yes, refuse — whatever threw, and
    whenever. If no, nothing is established, so proceed (or, for a deliberate
    abort, propagate).

    THE TABLE, stated exhaustively so no cell is left to inference:

    | throw class          | DEAD recorded | outcome                            |
    |----------------------|---------------|------------------------------------|
    | ``Exception``        | no            | PROCEED  (the 208-site guarantee)  |
    | ``Exception``        | YES           | REFUSE   (closed)                  |
    | KI / ``SystemExit``  | EITHER        | PROPAGATE                          |

    **THIS GATE NEVER TURNS AN ABORT INTO A REFUSAL.** That is a project-wide
    convention, asserted in these same files ("``except Exception`` (not
    ``BaseException``) so KI/SystemExit propagate — a deliberate abort is never
    swallowed into a misroute"), and it wins over any local reasoning here. An
    earlier revision made the recorded-DEAD cell REFUSE, because the outcome would
    have been a refusal anyway and it kept the error family. That was overruled,
    and rightly: a table with ONE cell contradicting the house rule is precisely
    the subtlety a later reader gets wrong.

    WHAT HAPPENS TO THE ABORT AFTERWARDS, stated accurately because an earlier
    version of this paragraph overclaimed it. It said a
    Ctrl-C here "must get their abort honoured" — but at the only production
    consumer it is NOT: ``Dispatcher.dispatch``'s ``except BaseException`` (which
    is pre-existing and not introduced by this cycle) catches the propagated abort
    and returns an ``ok:false`` envelope, so the interrupt does not reach the
    caller. What this gate guarantees is narrower and is all it can guarantee:
    **the abort is not converted into a REFUSAL here**, and it keeps travelling
    for whatever is above to decide.

    THE COST at that boundary, stated because it is real and must not be silent:
    the ``engine_channel_dead`` family and its remedy text are FORFEITED for that
    one call — dispatch envelopes it as ``internal`` instead. The handler still
    never runs, the session stays latched, and the very next call reports the
    proper family on a free flag read.

    The other half of the invariant lives inside ``observe_channel``: a failed
    LATCH WRITE likewise does not change the verdict.
    """
    try:
        dead = bool(
            session.channel_dead is True
            or (session.is_open and session.observe_channel() == CHANNEL_DEAD)
        )
        # The truth test lives INSIDE the guard: a verdict whose ``__bool__``
        # raises is a PRE-observation fault that must PROCEED, and evaluating it
        # outside made the gate itself raise.
        #
        # BOTH EXITS CONSULT THE ARBITER. Deciding on the RETURNED VERDICT alone
        # was the fourth occurrence of this defect: an observation can complete,
        # RECORD the latch, and then return something that is not DEAD (a wrapper
        # that normalises or re-labels the verdict). The property says "at any
        # point during this gate invocation", so the durable question is asked
        # here too — not only on the throwing path.
        if not dead and not _dead_recorded(session):
            return None
    except BaseException as exc:  # noqa: BLE001 — the table above decides
        # ORDER IS LOAD-BEARING: the abort test comes FIRST, so a deliberate abort
        # propagates whether or not a DEAD observation is on record. Swapping these
        # two blocks converts a Ctrl-C into a return value on a latched session.
        if not isinstance(exc, Exception):
            raise
        if _dead_recorded(session):
            return SessionChannelDeadError(_channel_dead_refusal_text(session))
        return None
    # A DEAD observation completed and returned normally.
    return SessionChannelDeadError(_channel_dead_refusal_text(session))


def _safe_error_text(exc) -> str:
    """Build a ``"{Type}: {message}"`` error string that NEVER raises.

    The never-raise envelope formerly used an f-string that invoked ``str(exc)``;
    a handler exception whose ``__str__`` itself raises (a custom Python exception,
    or a bridged .NET type with a broken ``ToString()``) would make the envelope
    construction raise INSIDE the ``except`` and escape ``dispatch()``. Here the
    type name is read defensively and the message is extracted via a guarded
    ``str(exc)`` -> ``safe_repr`` -> bare-type-name fallback chain.

    THE GUARDS CATCH ``BaseException``, not ``Exception``.
    ``dispatch`` catches ``BaseException``, so widening its net was pointless while
    the FORMATTER it calls inside that net caught only ``Exception``: a custom
    ``BaseException`` whose ``__str__`` raises ``KeyboardInterrupt`` escaped
    ``dispatch`` entirely — measured, and it defeats the never-raise envelope that
    every consumer relies on. This function does no work anyone would want to
    interrupt; it renders a string for an exception that has ALREADY been caught.
    """
    try:
        type_name = type(exc).__name__
    except BaseException:  # noqa: BLE001 — even type(exc).__name__ must not escape
        type_name = "?"

    message = None
    try:
        message = str(exc)
    except BaseException:  # noqa: BLE001 — exc.__str__ raised; try a guarded repr
        try:
            message = _io.safe_repr(exc)
        except BaseException:  # noqa: BLE001 — give up on the message, keep the type
            message = None

    # GUARD THE CONSUMPTION, NOT ONLY THE PRODUCTION — the exact sibling of the
    # earlier fix above. The
    # reads were widened to BaseException, but the f-string that CONSUMES their
    # output was not: a ``type(exc).__name__`` supplied by a hostile metaclass
    # READS fine and then raises when FORMATTED, which escaped ``dispatch`` and
    # defeated the never-raise envelope every consumer depends on.
    try:
        if message is None:
            return f"{type_name}: <unprintable exception message>"
        return f"{type_name}: {message}"
    except BaseException:  # noqa: BLE001 — deliberately NOT re-raised; see below
        # This function is the LAST step of an envelope for an exception that has
        # ALREADY been caught by ``dispatch``. It is not on an abort's travel path
        # — the abort was absorbed by dispatch's own ``except BaseException``
        # before this ran — so re-raising here would defeat the never-raise
        # envelope without honouring anything. That is why this one catch does not
        # carry the re-raise the others do.
        return "?: <unprintable exception>"


@dataclass(frozen=True)
class ToolSpec:
    """A dispatchable tool: its name, handler, required params, and description."""

    name: str
    handler: Callable
    required_params: Tuple[str, ...] = field(default_factory=tuple)
    description: str = ""
    # Per-param JSON-Schema type map: EVERY param the handler reads
    # (required AND optional) -> its JSON type token (number/integer/string/
    # boolean/array/object). OPTIONAL by default (empty) so existing/reference
    # specs without it still load; the MCP adapter emits a typed inputSchema from
    # it (and falls back to the legacy all-string schema when it is empty).
    param_types: Dict[str, str] = field(default_factory=dict)


# Tool modules exposing a single module-level ``TOOL_SPEC``.
_SINGLE_SPEC_MODULES = (
    "optivibe_harness.tools.get_system_info",
    "optivibe_harness.tools.save_snapshot",
    # The adaptive aperture ramp (ramp_aperture).
    "optivibe_harness.tools.aperture_ramp",
    # Geometric scale (scale_lens).
    "optivibe_harness.tools.scale_lens",
)
# Tool modules exposing a ``TOOL_SPECS`` tuple of several ToolSpecs (lens tier
# + analysis tier).
_MULTI_SPEC_MODULES = (
    "optivibe_harness.tools.lens_surface",
    "optivibe_harness.tools.lens_system",
    # Vignetting-factor authoring (set_vignetting).
    "optivibe_harness.tools.lens_vignetting",
    # Ray-aiming mode authoring (set_ray_aiming).
    "optivibe_harness.tools.ray_aiming",
    "optivibe_harness.tools.lens_glass",
    "optivibe_harness.tools.lens_spec",
    # The structural normalizer (the dummy-stop convention).
    "optivibe_harness.tools.lens_normalize",
    # Coordinate-break primitive floor (add_coordinate_break /
    # set_cb_variable / add_return_cb).
    "optivibe_harness.tools.cb_surface",
    # Reflective core (set_mirror / fold_beam).
    "optivibe_harness.tools.reflective",
    # Surface-aperture / obstruction primitive (set_surface_aperture).
    "optivibe_harness.tools.aperture_surface",
    # Even-asphere authoring (set_asphere / set_asphere_variable).
    "optivibe_harness.tools.asphere_surface",
    # GRIN authoring primitives (set_grin / set_grin_variable).
    "optivibe_harness.tools.grin_surface",
    # MCE multi-configuration primitive (add_configuration / set_config_operand
    # / set_config_value / set_config_variable / set_current_configuration /
    # describe_configurations).
    "optivibe_harness.tools.mce_config",
    # Multi-config reset (remove_configuration / reset_to_single_config).
    "optivibe_harness.tools.mce_reset",
    # Zoom/focus/conjugate/array composer (set_zoom).
    "optivibe_harness.tools.zoom_compose",
    # Zoom-ergonomics finalize helpers (freeze_semidiameters / verify_zoom).
    "optivibe_harness.tools.freeze_semi",
    # Geometry-readouts cycle: the manufacturability / detector-clearance audit.
    "optivibe_harness.tools.clearance",          # check_clearance
    # The rays-reach-the-optics falsifier.
    "optivibe_harness.tools.beam_verify",       # verify_beam_path
    # The perturb-and-return element composer.
    "optivibe_harness.tools.place_element",      # place_element
    # Afocal/collimation analysis: the collimation residual grader.
    "optivibe_harness.tools.collimation_verify",  # verify_collimation
    # Analysis (results-extraction) tier.
    "optivibe_harness.tools.analysis_mtf",
    "optivibe_harness.tools.analysis_spot",
    "optivibe_harness.tools.analysis_raytrace",
    "optivibe_harness.tools.analysis_operand",
    "optivibe_harness.tools.analysis_graphic",
    # First-class measurement/analysis tier (first-order / strehl /
    # wavefront / axial-color).
    "optivibe_harness.tools.analysis_measure",
    # Optimize (the capstone closed-loop) tier.
    "optivibe_harness.tools.optimize_variable",
    # Variable-lifecycle (list_variables / clear_all_variables).
    "optivibe_harness.tools.variable_lifecycle",
    "optivibe_harness.tools.optimize_merit",
    # Merit-builder SAVING layer: native save/load + clear/remove (+ recipe tools).
    "optivibe_harness.tools.optimize_merit_io",
    # Merit-builder MATH cycle: the intent->math composer (add_math_constraint).
    "optivibe_harness.tools.merit_math",
    "optivibe_harness.tools.optimize_run",
    # Tolerancing (sensitivity / monte_carlo).
    "optivibe_harness.tools.tolerance_run",
    # load_design (the empty-report recovery enabler).
    "optivibe_harness.tools.load_design",
    # Project-workspace + element-grounding cycle (describe / render / workspace).
    "optivibe_harness.tools.lens_describe",      # describe_surfaces
    "optivibe_harness.tools.layout_render",      # render_layout
    "optivibe_harness.tools.workspace",          # save_candidate, promote_best
)


def load_manifest():
    """Build the default tool manifest via importlib (avoids an import cycle).

    Imports each tool module and collects its ``TOOL_SPEC`` (single) or
    ``TOOL_SPECS`` (tuple), returning ``{name: ToolSpec}``. importlib (not a
    top-level import) keeps the dispatch core free of tool-module import-time
    coupling. THE one substrate change this cycle is extending these module
    lists to register the lens tools.
    """
    manifest = {}
    for mod_name in _SINGLE_SPEC_MODULES:
        module = importlib.import_module(mod_name)
        spec = module.TOOL_SPEC
        manifest[spec.name] = spec
    for mod_name in _MULTI_SPEC_MODULES:
        module = importlib.import_module(mod_name)
        for spec in module.TOOL_SPECS:
            manifest[spec.name] = spec
    return manifest


class Dispatcher:
    """Routes a tool request to its handler under the session lock; never raises."""

    def __init__(self, session, manifest=None, call_warn_threshold_s=60.0):
        self._session = session
        self._manifest = manifest if manifest is not None else load_manifest()
        # Share the session's single lock so dispatch serializes against close()
        # and against other dispatches (single-seat engine).
        self._lock = session._lock
        self._logger = getattr(session, "_logger", None)
        # the hang-watchdog §4.1/§4.3: a slow-call WARN threshold (a breadcrumb, not
        # an alarm). Prefer the explicit kwarg; fall back to the session's shared
        # ``slow_call_threshold_s`` (set at boot) so open() and dispatch share ONE knob.
        if call_warn_threshold_s is None:
            call_warn_threshold_s = getattr(session, "slow_call_threshold_s", 60.0)
        # Clamp a pathological threshold so the
        # one-shot ``threading.Timer`` interval is ALWAYS numeric+finite+positive. A
        # direct ``Dispatcher(call_warn_threshold_s="bad")`` (or nan/inf/<=0) otherwise
        # crashes the Timer thread (TypeError in stdlib ``Timer.run``) — unreachable via
        # the env path (``_env_float``-clamped) but reachable via direct construction.
        # Mirrors the ``_run_with_watchdog`` clamp. L26: NEVER rejects a valid positive
        # finite threshold (e.g. 0.05 passes through verbatim); only pathological inputs
        # are floored to the 60.0 s default.
        if not (
            isinstance(call_warn_threshold_s, (int, float))
            and not isinstance(call_warn_threshold_s, bool)
            and math.isfinite(call_warn_threshold_s)
            and call_warn_threshold_s > 0
        ):
            call_warn_threshold_s = 60.0
        self._call_warn_threshold_s = call_warn_threshold_s

    def list_tools(self):
        """Return ``[{name, description, required_params}]`` for every tool."""
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

        Envelope (success AND failure):
        ``{ok, tool, result|None, error|None, error_family|None}``.

        Serializes on the session lock. Validates the tool exists and that every
        required param is PRESENT (presence-only, no type validation). Wraps the
        handler in ``logger.record_call`` when a logger is present. Maps any raised
        exception to a typed family. NEVER raises.
        """
        if not isinstance(params, dict):  # dispatch backstop: coerce any non-dict (L26)
            params = {}
        with self._lock:
            try:
                spec = self._manifest.get(tool_name)
                if spec is None:
                    raise UnknownToolError(f"unknown tool: {tool_name!r}")

                missing = [p for p in spec.required_params if p not in params]
                if missing:
                    raise ToolParamError(
                        f"missing required param(s) for {tool_name!r}: {missing}"
                    )

                # GATE A: refuse an OBSERVED-dead channel BEFORE any
                # handler runs. Placed AFTER the unknown-tool / missing-param
                # raises so their behaviour is untouched, and inside the lock
                # dispatch already holds. The returned typed error is raised into
                # the existing ``except`` arm, which envelopes it with the right
                # family and message — zero envelope-building code here.
                gate = _channel_gate(self._session)
                if gate is not None:
                    raise gate

                result = self._invoke(spec, params)
                return {
                    "ok": True,
                    "tool": tool_name,
                    "result": result,
                    "error": None,
                    "error_family": None,
                }
            except BaseException as exc:  # noqa: BLE001 — dispatch must NEVER raise
                # Classification + error-text construction are BOTH guarded so the
                # never-raise envelope cannot itself raise: a broken
                # __str__/ToString() or a pathological type must still envelope.
                try:
                    error_family = self._classify(exc)
                except BaseException:  # noqa: BLE001 — see below; NOT re-raised
                    # ``BaseException``, and deliberately WITHOUT the re-raise the
                    # travel-path catches carry (P-2). ``exc`` has
                    # ALREADY been caught by the enclosing handler; this is
                    # envelope construction, not work on an abort's travel path,
                    # so re-raising here would defeat the never-raise envelope
                    # without honouring anything. Narrow, this falsified
                    # ``dispatch``'s own "NEVER raises": a classification whose
                    # ``__str__`` raised ``KeyboardInterrupt`` escaped.
                    error_family = "internal"
                return {
                    "ok": False,
                    "tool": tool_name,
                    "result": None,
                    "error": _safe_error_text(exc),
                    "error_family": error_family,
                }

    def _invoke(self, spec, params):
        """Run the handler, wrapped in ``logger.record_call`` when available.

        the hang-watchdog §4.1: a NON-KILLING one-shot daemon ``threading.Timer``
        names the slow tool after ``call_warn_threshold_s``. The Timer ONLY logs —
        it never touches the handler thread, the engine, the session, or ``_lock``
        (dispatch holds ``session._lock`` for the whole handler call; the Timer
        fires on a SEPARATE daemon thread and MUST NOT acquire ``_lock`` or it would
        block forever). It is daemon (never blocks process exit) and is cancelled in
        ``finally`` (a fast call cancels before it fires; cancel on an already-fired
        timer is a safe no-op). Fires AT MOST ONCE.
        """
        timer = threading.Timer(
            self._call_warn_threshold_s,
            lambda: self._warn_slow_call(spec.name, self._call_warn_threshold_s),
        )
        timer.daemon = True
        timer.start()
        try:
            if self._logger is None:
                return spec.handler(self._session, params)

            with self._logger.record_call(
                intent=f"dispatch {spec.name}",
                call=f"{spec.name}(session, params)",
                args=params,
            ) as holder:
                result = spec.handler(self._session, params)
                holder["result"] = result
            return result
        finally:
            try:
                timer.cancel()  # sub-threshold call: no warning fires
            except Exception:  # noqa: BLE001 — cancel must never break dispatch
                pass

    def _warn_slow_call(self, tool_name, threshold):
        """Emit a WARNING NAMING the slow tool + threshold (the hang-watchdog §4.1).

        Log-or-stderr fallback mirroring ``session._log``: try the logger, else
        stderr. NEVER raises; NEVER touches ``session._lock`` (load-bearing — it
        fires on a daemon Timer thread WHILE dispatch holds ``_lock``; acquiring it
        would deadlock). It reads only the captured ``tool_name`` string.

        L26 — what it OWNS: it touches NOTHING shared except the logger/stderr seam;
        every step is guarded so the breadcrumb can never break a dispatch.

        The ``msg`` f-string is built INSIDE the first ``try`` so a
        pathological ``threshold`` (e.g. a str that has no ``__format__`` for ``:.1f``)
        can never raise on the daemon Timer thread — making never-raise TOTAL (a bad
        threshold reaches here only via a direct ``Dispatcher(call_warn_threshold_s=...)``
        mis-construction; the env surface is float-guarded).
        """
        # Pre-bind a threshold-free fallback so the stderr path still surfaces a
        # breadcrumb even if the {threshold:.1f} format below raises (a pathological
        # non-numeric threshold) — then `msg` is never unbound on the print path.
        msg = "optivibe: SLOW CALL — a tool is still running (duration watchdog)."
        try:
            msg = (
                f"optivibe: SLOW CALL — tool={tool_name!r} still running after "
                f"{threshold:.1f}s (duration watchdog; the call is NOT killed)."
            )
            logger = self._logger
            if logger is not None and hasattr(logger, "log"):
                logger.log(intent="slow_call", call="duration_watchdog", args=msg)
                return
        except Exception:  # noqa: BLE001 — fall through to stderr
            pass
        try:
            print(msg, file=sys.stderr)
        except Exception:  # noqa: BLE001 — warning must never break dispatch
            pass

    @staticmethod
    def _classify(exc):
        """Classify a raised exception into an ``error_family`` string.

        ``HarnessError`` -> its own ``error_family``; a .NET exception ->
        ``map_dotnet_exception`` result's family; anything else (Python builtins
        such as ``AttributeError`` / ``TypeError`` — pythonnet driven wrong) ->
        ``"internal"``.
        """
        if isinstance(exc, HarnessError):
            return exc.error_family
        mapped = map_dotnet_exception(exc)
        if mapped is not None:
            return mapped.error_family
        return "internal"
