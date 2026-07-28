"""tools/optimize_run.py — dry_run (preflight) + optimize (THE CAPSTONE).

- ``dry_run``  — NON-MUTATING preflight (§d/§e): counts variables + checks
  the merit exists. Opens NO optimizer. Returns ``ready:True`` or a
  ``no_variables`` / ``no_merit`` envelope.
- ``optimize`` — THE CAPSTONE closed loop (§a/§c). Embeds the dry_run gates
  (short-circuit BEFORE opening anything), opens ``Tools.OpenLocalOptimization()``
  ONCE, runs ``max_passes`` bounded passes of ``RunAndWaitForCompletion(cycles)``,
  snapshots ``pass00_before`` (once, pre-open) + ``pass{NN}_after`` (per pass) via
  the run's ``ArtifactSink`` (never-raise, verdict-DECOUPLED), reads the merit,
  classifies the verdict, cross-checks the MFE recompute (the tripwire,
  non-fatal), and reaps the optimizer in ``finally`` (the single-seat reap).

The verdict dict carries ``ok=True`` even on ``stable`` / ``diverged`` — a
COMPLETED run that did not improve is a RESULT, not a dispatch error. Only an
engine-level run failure (``RunAndWaitForCompletion``->False / ``opt.Succeeded``->
False) returns ``ok=False`` (``optimize_run_failed``).

Live ZOS-API integration: exercised by the live closed-loop acceptance test;
unit-tested against the fixture-seeded MUTATING fake optimizer.
"""
import glob
import math
import os
import tempfile
import time
import uuid
from dataclasses import asdict

from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import OptimizeError, ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _grin_index_common as _gic  # GRIN — cycle-safe (never imports optimize_run)
from . import _optimize_common as _oc
from . import analysis_spot  # cycle-safe (analysis_spot never imports optimize_run)
from . import clearance  # cycle-safe (clearance never imports optimize_run)

# Audit floors — mirror clearance._DEFAULT_MIN_GLASS / _DEFAULT_MIN_AIR.
_DEFAULT_MIN_GLASS = 1.0
_DEFAULT_MIN_AIR = 0.5

# merit_reality_divergence_warning constants — the poly-vs-worst-per-wave
# chromatic-blindness audit thresholds (§2; FINAL values, NOT tool params).
_MRD_RATIO_THRESHOLD = 10.0   # poly/per-wave ratio at/above which a (config,field) point diverges.
_MRD_POLY_FLOOR_UM = 10.0     # um: absolute "reality is genuinely bad" gate (poly must clear this).
_MRD_DENOM_FLOOR_UM = 0.05    # um: per-wave denominator floor (diffraction-limited per-wave clamp).

# The default + cap for the bounded multi-pass loop.
_DEFAULT_MAX_PASSES = 1
_MAX_PASSES_CAP = 10
# The default cycle count (§6 — matches the probe baseline of 10 cycles).
_DEFAULT_CYCLES = 10
# The two algorithm tokens (§6 — the ONLY two members).
_ALGORITHM_TOKEN_TO_MEMBER = {
    "DLS": "DampedLeastSquares",
    "OD": "OrthogonalDescent",
}
# The Hammer (global-search) escape. Hammer is NOT an
# OptimizationAlgorithm member (it opens a different Tools resource), so it is validated
# as a token but resolves to member None (the HARD fork in _optimize_impl).
_ALGORITHM_TOKEN_HAMMER = "Hammer"
# The Hammer wall-time cap (minutes): default 1.0, hard cap 10.0 (single-seat protection).
_DEFAULT_RUN_TIME_M = 1.0
_RUN_TIME_M_CAP = 10.0
# The temp-checkpoint prefix (best-restore SaveAs/LoadFile, #73 forward-slash + #59 glob-reap).
_HAMMER_CKPT_PREFIX = "optivibe_hammer_"


def dry_run(session, params):
    """NON-MUTATING preflight: are variables + a merit present? Opens NO optimizer.

    Delegates to ``_optimize_common._preflight``. A pass returns
    ``{ok:True, variables, number_of_operands, merit, ready:True}``; a gate fail
    returns the ``no_variables`` / ``no_merit`` / ``stop_on_glass_vertex`` envelope
    (``ok=False``, no ``ready`` key). dry_run reports the stop-convention problem
    too (the caller learns the problem WITHOUT opening the optimizer);
    ``require_free_stop`` (default True) gates it.

    BREAKING CHANGE: ``dry_run`` REFUSES a stop on a glass vertex by
    DEFAULT (``require_free_stop=True``) — it returns the
    ``optimize_stop_on_glass_vertex`` envelope (``ok=False``, NO ``ready`` key)
    instead of ``ready:True``. To preflight a vertex-stop system anyway, pass
    ``require_free_stop=False`` (skips the guard), or run ``normalize_stop`` first to
    make the stop free-standing. (§6.3.)

    an UNCOMPUTABLE merit (a finite ``>= 1e9`` could-not-compute sentinel — a
    ray fails to trace at the current design) returns the
    ``optimize_merit_uncomputable`` envelope (``ok=False``, NO ``ready`` key) — the
    SAME agent-facing name ``optimize`` reports, mirroring the stop-vertex
    special-case. The legacy bare families (``no_variables``/``no_merit``) stay BARE
    in dry_run (out of scope for this cycle); the asymmetry (legacy bare; stop +
    merit-uncomputable prefixed-on-both) is intentional + precedented.
    """
    system = session.system
    require_free_stop = _bool_param(params, "require_free_stop", True)
    (ok, family, variables, number_of_operands, merit,
     stop_idx, stop_material) = _oc._preflight(
        system, require_free_stop=require_free_stop
    )
    if not ok:
        if family == "stop_on_glass_vertex":
            return _stop_vertex_envelope("dry_run", stop_idx, stop_material)
        if family == "merit_uncomputable":
            # Agent-facing consistency (§2a): dry_run reports the SAME full name as
            # optimize (`optimize_merit_uncomputable`), mirroring the stop-vertex
            # special-case. NON-MUTATING — no optimizer opened, no `ready` key.
            return _oc.error_envelope(
                "dry_run",
                "optimize_merit_uncomputable",
                _gate_message("merit_uncomputable", variables, number_of_operands, merit),
                variables=variables,
                number_of_operands=number_of_operands,
                merit=safe_float(merit),
                # the suspect-ranked failing-row enumeration (CONFIRMED readable —
                # all rows read cleanly WHILE 9e9, optimizer NOT open). Spread as
                # ONE additive key (or {} on any fault -> today's payload; never collides).
                **_oc._uncomputable_row_diagnostics(system),
            )
        return _oc.error_envelope(
            "dry_run",
            family,
            _gate_message(family, variables, number_of_operands, merit),
            variables=variables,
            number_of_operands=number_of_operands,
            merit=safe_float(merit),
        )
    # the per-config THIC<=0 gate (DISJOINT family optimize_per_config_thin),
    # AFTER the _preflight ok-pass, via the ONE shared predicate. Runs even under
    # require_free_stop=False (independent of the stop gate). NON-MUTATING.
    thin = _oc._scan_per_config_thin(system)
    if thin:
        return _per_config_thin_envelope("dry_run", thin)
    # the inert-DOF gate (DISJOINT family optimize_inert_dof), AFTER the
    # per-config-thin block via the ONE shared predicate. An air<->air radius/conic Variable
    # is a zero-merit-sensitivity DOF (the curved-stop silent-wrong) — refuse so the agent
    # learns the problem WITHOUT opening the optimizer. Runs even under require_free_stop=False.
    inert = _oc._scan_inert_dofs(system)
    if inert:
        return _inert_dof_envelope("dry_run", inert)
    # MCE: disclose the merit's config span so the agent preflights it
    # BEFORE running (NEVER a refusal). A single-config merit over a multi-config
    # system gets the merit_single_config WARN.
    span = _config_span_disclosure(system)
    result = {
        "ok": True,
        "variables": variables,
        "number_of_operands": number_of_operands,
        "merit": safe_float(merit),
        "ready": True,
        "merit_spans_configs": span["merit_spans_configs"],
        "n_configs": span["n_configs"],
        "configs_covered": span["configs_covered"],
    }
    if span["warning"] is not None:
        result["warning"] = span["warning"]
    # non-blocking ray-free-merit WARN — after the preflight
    # ok-pass (NumberOfOperands > 0), via the ONE shared predicate. Merged with any
    # config-span warning (either may be None).
    merged = _merge_warning(result.get("warning"), _oc._scan_rayfree_merit(system))
    if merged is not None:
        result["warning"] = merged
    return result


def _config_span_disclosure(system):
    """The MCE-config span disclosure for dry_run/optimize (§2.4). NEVER raises.

    Reads the config count (THROW-guarded -> 1) + the CONF-Cfg# coverage set (the §2.2
    factored scan) and computes ``merit_spans_configs`` (True on a 1-config system OR when
    the merit covers every config). A multi-config system whose merit covers only some
    configs gets the ``merit_single_config`` WARN (NEVER a refusal).
    Returns ``{merit_spans_configs, n_configs, configs_covered, warning}``.
    """
    n_configs = _ccfg.safe_number_of_configurations(system)
    covered = _oc._merit_configs_covered(system)
    merit_spans_configs = (
        (n_configs <= 1) or (set(covered) == set(range(1, n_configs + 1)))
    )
    warning = None
    if n_configs > 1 and not merit_spans_configs:
        warning = (
            f"the merit controls only configs {covered} of {n_configs}; optimize will "
            "improve only those — rebuild with build_merit(span_configs=true) to span all "
            "configs"
        )
    return {
        "merit_spans_configs": merit_spans_configs,
        "n_configs": n_configs,
        "configs_covered": covered,
        "warning": warning,
    }


# The uncomputable-merit ESCAPE recipe + the sampling-density-flip
# caveat. Shared by BOTH the dry_run (_gate_message) and optimize (_merit_uncomputable_run_message)
# arms so the same guidance reaches the agent on either path. The escape is
# live-probe-confirmed (9e9 -> ~340-372 finite): a wide-field/fast GQ merit reads
# uncomputable when a corner lower-pupil ray fails to trace; vignetting launches the
# smaller (vignetted) pupil so it traces. "Rebuild" (not "recompute") is the
# always-correct guidance — mandatory if the FIELD SET changes, never harmful.
# The structural-anchor clause appended to the ONE shared escape
# constant (below) so it ships in BOTH the dry_run and optimize uncomputable messages
# A ray-FREE sparse merit (EFFL + boundaries only) with many free radii has NO
# ray-based restoring force and DLS collapses the geometry to near-zero radii.
_SPARSE_MERIT_ANCHOR = (
    " STRUCTURAL ANCHOR (critical): a sparse merit MUST keep a ray-based restoring "
    "force — either retain a few low-pupil on-axis ray operands (which DO trace), OR "
    "freeze the structural (radius/conic) variables and free only focus/scale during "
    "the sparse pass. A ray-FREE sparse merit (EFFL + boundaries only) with many free "
    "radii has NO ray restoring force and DLS collapses the geometry to near-zero radii "
    "— reload a candidate to recover."
)

_MERIT_UNCOMPUTABLE_ESCAPE = (
    "ESCAPE for a wide-field/fast merit: a corner lower-pupil ray that cannot trace at "
    "full pupil makes the whole Gaussian-Quadrature merit uncomputable. Apply realistic "
    "corner vignetting (set_vignetting mode=from_rays for the auto path, or mode=set with "
    "a vcy~0.45 taper on the wide field), REBUILD the merit (build_merit — its GQ operands "
    "re-evaluate against the new vignetting), and re-seed from a gentler (pre-convergence) "
    "form, then re-run. "
    "NOTE: a denser GQ ring/arm count can flip a near-vignetting design uncomputable (a "
    "finer pupil sample lands on a clipped corner ray) — the design may compute at fewer "
    "rings, but a corner ray IS failing; prefer fixing vignetting (above) over hiding it "
    "with coarse sampling."
    + _SPARSE_MERIT_ANCHOR
)


def _gate_message(family, variables, number_of_operands, merit):
    """Build the human-readable message for a failed preflight gate."""
    if family == "no_variables":
        return (
            "no optimization variables set (no LDE cell has a Variable solve); "
            "set_variable on at least one radius/thickness first"
        )
    if family == "merit_uncomputable":
        return (
            "the merit function cannot be evaluated: CalculateMeritFunction() returns "
            f"the could-not-compute sentinel (merit={merit!r}, >= 1e9). A ray FAILS to "
            "trace at the current design (e.g. a lower-pupil ray at a wide field), so "
            "the optimizer would reject it as 'Input settings are invalid'. Reach a "
            "TRACEABLE basin and optimize a SPARSE merit first (e.g. EFFL + a few "
            "traceable constraints), then rebuild the full RMS merit; or fix the "
            "offending geometry. "
            + _MERIT_UNCOMPUTABLE_ESCAPE
        )
    if family == "stop_indeterminate":
        # A Material read failed -> the stop classification is indeterminate.
        # Refuse (never guess a vertex / never auto-mutate); retry once the engine
        # is responsive, or pass require_free_stop=false to skip the guard.
        return (
            "the aperture stop classification is indeterminate (a surface Material "
            "read failed); refusing rather than guessing a vertex stop. Retry when "
            "the engine is responsive, or pass require_free_stop=false to skip the "
            "dummy-stop guard."
        )
    return (
        "no usable merit function "
        f"(NumberOfOperands={number_of_operands}, merit={merit!r}); "
        "build_merit (or add_operand) first"
    )


def _merit_uncomputable_run_message(err_msg):
    """The optimize opt.IsValid-False message (§4.2): the engine's own readiness verdict."""
    return (
        "the optimizer reports the merit function is not evaluable "
        f"(opt.IsValid is False; engine: {err_msg!r}). A ray FAILS to trace at the "
        "current design, so the run is refused (no cycles executed). Reach a traceable "
        "basin and optimize a sparse merit (e.g. EFFL + a few traceable constraints) "
        "first, then rebuild the full RMS merit; or fix the offending geometry. "
        + _MERIT_UNCOMPUTABLE_ESCAPE
    )


def _safe_error_message(opt, default):
    """Read ``opt.ErrorMessage`` defensively -> a NON-EMPTY str, never raising.

    A degraded engine can make the ``ErrorMessage`` PROPERTY throw on read OR return a
    proxy whose ``__bool__``/``__str__`` throws. Every failure mode -> the caller's
    ``default``. Shared by BOTH ErrorMessage read sites so the same acceptance
    rule governs the ``optimize_merit_uncomputable`` and ``optimize_run_failed`` families.

    The sibling this closes: the prior call sites applied ``err_msg or <default>``
    to the RAW proxy, so a ``__bool__``-raising ErrorMessage made the ``or`` throw and
    ESCAPED the branch into the opaque ``internal`` family. Here the truthiness test
    (``if not msg``) is INSIDE the try/except, so a raising ``__bool__`` is caught and
    degrades to ``default``; the falsy-fallback semantics (None / "" / 0 / False / empty
    container -> default) the tests pin are preserved, and the final
    ``str(msg) or default`` operates on a REAL str (never a raising proxy).
    """
    try:
        msg = getattr(opt, "ErrorMessage", None)
        if not msg:               # None / "" / 0 / False / empty -> default;
            return default        # a __bool__-raising proxy throws HERE -> caught below
        return str(msg) or default  # a __str__-raising proxy is caught; str() is safe to `or`
    except Exception:  # noqa: BLE001 — a degraded ErrorMessage read must never escape
        return default


def _bool_param(params, key, default):
    """Pull an optional bool param; reject a non-bool (a client miswrite).

    A bad value raises ``OptimizeError(family="optimize_param")`` (the
    param-class family).
    """
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, bool):
        raise OptimizeError(
            f"{key!r} must be a bool, got {type(value).__name__} {value!r}",
            family="optimize_param",
        )
    return value


def _per_config_thin_envelope(tool, offenders, nudged=None, un_nudgeable=None):
    """Build the ``optimize_per_config_thin`` refusal envelope (§b; §2.3 amend).

    A per-config THIC <= 0 is mechanically unbuildable (a collapsed/negative air or glass
    gap in a NON-current config, so the LDE / current-config readout looks fine — the HIGH
    silent-wrong #2). Refuse BEFORE opening the optimizer, naming the offending
    (surface, config, value) rows and the OPT-IN recovery + its required PAIRING: author
    per-config floors + make the collapsed THIC an optimizer DOF, then re-run optimize with
    ``recover_thin=true``. ``nudged``/``un_nudgeable`` are additive — present only on
    the ``recover_thin`` refuse-after-nudge path (dry_run + the default optimize refuse pass
    neither).
    """
    parts = ", ".join(
        f"surface {o.get('surface')} config {o.get('config')} = {o.get('value')!r}"
        for o in offenders
    )
    extra = {}
    if nudged is not None:
        extra["per_config_thin_nudged"] = nudged
    if un_nudgeable is not None:
        extra["un_nudgeable"] = un_nudgeable
    return _oc.error_envelope(
        tool,
        "optimize_per_config_thin",
        (
            "a per-config thickness is <= 0 (mechanically unbuildable): "
            f"{parts}. A collapsed/negative air or glass gap in a NON-current config is a "
            "silent garbage basin (the current-config readout looks fine). Author per-config "
            "thickness floors (build_merit(span_configs=true, min_air=..., min_glass=...) "
            "spans floors per config), make the collapsed per-config THIC an optimizer DOF "
            "(set_config_variable), then re-run optimize with recover_thin=true to nudge the "
            "collapsed gap past this guard so DLS + the heavy floor walk it up."
        ),
        per_config_thin=offenders,
        **extra,
    )


def _apply_nudge_disclosure(result, nudge_disclosure):
    """Add the success-path disclosure to a SUCCESS ``result`` (§2.5). NEVER flips ok.

    ``nudge_disclosure`` is the ``(nudged, un_nudgeable)`` pair stashed when the opt-in nudge
    cleared the per-config-thin guard, or ``None``. When a nudge happened it adds
    ``per_config_thin_nudged`` (the nudged list) + merges a ``warning`` naming the required
    PAIRING (a heavy per-config floor via ``build_merit(span_configs=true)`` + the THIC as a
    ``set_config_variable`` DOF) and — when any nudged cell was Fixed — the knife-edge caveat.
    ABSENT (byte-identical to today's success path) when nothing was nudged.
    """
    if nudge_disclosure is None:
        return result
    nudged, _un_nudgeable = nudge_disclosure
    if not nudged:
        return result
    result["per_config_thin_nudged"] = nudged
    warn = (
        "recover_thin nudged a collapsed per-config THIC to 0.001 to open the optimizer; "
        "this ONLY yields a physical gap when paired with a heavy per-config floor "
        "(build_merit(span_configs=true)) AND the THIC as an optimizer DOF "
        "(set_config_variable)"
    )
    if any(n.get("fixed") for n in nudged):
        warn += (
            "; a nudged Fixed (non-DOF) cell stays a knife-edge 0.001 — make it a "
            "per-config variable to recover a real gap"
        )
    result["warning"] = _merge_warning(result.get("warning"), warn)
    return result


def _inert_dof_envelope(tool, offenders):
    """Build the ``optimize_inert_dof`` refusal envelope (§4).

    An air<->air surface's radius/conic Variable is a zero-merit-sensitivity DOF (a flat
    zero-power surface contributes nothing to the merit), so the optimizer drives it to
    garbage — the curved-stop silent-wrong (a dummy-stop radius made Variable). Refuse
    BEFORE opening the optimizer, naming each offending (surface, cell) and the recovery
    (clear_variable on it), and noting that a MIRROR/CB DOF is NEVER flagged. DISJOINT
    family. optimize opens NOTHING on this refusal.
    """
    parts = ", ".join(
        f"surface {o.get('surface')} ({o.get('cell')})" for o in offenders
    )
    return _oc.error_envelope(
        tool,
        "optimize_inert_dof",
        (
            "an inert optimizer variable: a radius/conic Variable on an air<->air surface "
            f"({parts}) has ZERO merit sensitivity (a flat zero-power surface contributes "
            "nothing to the merit), so the optimizer would drive it to garbage (the "
            "curved-stop silent-wrong). Clear it with clear_variable(surface=..., "
            "cell='radius'|'conic'), or give the surface real adjacent glass. (A MIRROR / "
            "coordinate-break DOF is NEVER flagged — a fold mirror's radius is a real DOF.)"
        ),
        inert_dofs=offenders,
    )


def _stop_vertex_envelope(tool, stop_idx, stop_material):
    """Build the exact §6.4 ``optimize_stop_on_glass_vertex`` refusal envelope."""
    return _oc.error_envelope(
        tool,
        "optimize_stop_on_glass_vertex",
        (
            f"the aperture stop is on a glass vertex (surface {stop_idx}, material "
            f"'{stop_material}'); a real aperture stop is a free-standing dummy AIR "
            "surface in an airspace, not coincident with a lens vertex (mechanically "
            "unbuildable + denies the optimizer the stop-position DOF). Run "
            "normalize_stop first, or pass auto_normalize=true."
        ),
        stop_surface=stop_idx,
        stop_material=stop_material,
        remedy="normalize_stop",
    )


def _merge_warning(first, second):
    """Combine two optional warning strings into one (either may be None)."""
    parts = [w for w in (first, second) if w]
    if not parts:
        return None
    return "; ".join(parts)


def _auto_normalize(session, params):
    """Run ``normalize_stop`` on the session as the auto_normalize first step (§6.3).

    Imported LAZILY to avoid an import cycle (optimize_run <- normalize would
    otherwise pull lens_normalize at module import). Forwards the dummy-stop
    convention params (``bound_kind`` / ``min_air`` / ``add_bounds`` / ``free_gaps``)
    so a caller can tune the refactor via the same ``optimize`` call. Returns the
    normalize_stop result dict (its ``{ok:...}`` envelope drives the §6.3 branch).
    """
    from . import lens_normalize

    normalize_params = {
        k: params[k]
        for k in ("bound_kind", "min_air", "add_bounds", "free_gaps")
        if k in params
    }
    return lens_normalize.normalize_stop(session, normalize_params)


def _require_pos_int(params, key, default, *, cap=None):
    """Pull an optional positive-int param; reject bool / non-int / <= 0 / > cap.

    A bad value raises ``OptimizeError(family="optimize_param")`` (the
    param-class family). An integral float (a JSON round-trip can float an int) is
    accepted and coerced.
    """
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool):
        raise OptimizeError(
            f"{key!r} must be an integer, not a bool ({value!r})",
            family="optimize_param",
        )
    if isinstance(value, float):
        if value == int(value):
            value = int(value)
        else:
            raise OptimizeError(
                f"{key!r} must be an integer, got non-integral float {value!r}",
                family="optimize_param",
            )
    if not isinstance(value, int):
        raise OptimizeError(
            f"{key!r} must be an integer, got {type(value).__name__} {value!r}",
            family="optimize_param",
        )
    if value < 1:
        raise OptimizeError(
            f"{key!r} must be >= 1, got {value}", family="optimize_param"
        )
    if cap is not None and value > cap:
        raise OptimizeError(
            f"{key!r} must be <= {cap}, got {value}", family="optimize_param"
        )
    return value


def _require_pos_float(params, key, default, *, cap=None):
    """Pull an optional positive-float param; reject bool / non-number / nan / inf / <=0 / >cap.

    The float sibling of ``_require_pos_int`` (the Hammer ``run_time_m``
    wall-cap in minutes). A bad value raises ``OptimizeError(family="optimize_param")`` (the
    param-class family). An int is accepted and coerced to float; a bool / string /
    non-number is rejected; a non-finite (nan / +-inf) is rejected; ``<= 0`` is rejected; a
    value ``> cap`` (when a cap is given) is rejected.
    """
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool):
        raise OptimizeError(
            f"{key!r} must be a number, not a bool ({value!r})",
            family="optimize_param",
        )
    if not isinstance(value, (int, float)):
        raise OptimizeError(
            f"{key!r} must be a number, got {type(value).__name__} {value!r}",
            family="optimize_param",
        )
    value = float(value)
    if not math.isfinite(value):
        raise OptimizeError(
            f"{key!r} must be a finite number, got {value!r}", family="optimize_param"
        )
    if value <= 0:
        raise OptimizeError(
            f"{key!r} must be > 0, got {value}", family="optimize_param"
        )
    if cap is not None and value > cap:
        raise OptimizeError(
            f"{key!r} must be <= {cap}, got {value}", family="optimize_param"
        )
    return value


def _resolve_algorithm(system, params):
    """Resolve the ``algorithm`` token to a live ``OptimizationAlgorithm`` member.

    ``algorithm`` defaults to ``"DLS"`` (``DampedLeastSquares``); ``"OD"`` is
    ``OrthogonalDescent`` (the ONLY two members); ``"Hammer"`` is the
    global-search escape — validated as a token but NOT an ``OptimizationAlgorithm`` member
    (it opens a different ``Tools`` resource), so it returns ``("Hammer", None)`` and the
    caller HARD-forks on it. Anything else -> ``OptimizeError(family="optimize_param")``.
    Returns ``(token, member)``.
    """
    token = params.get("algorithm", "DLS")
    if token == _ALGORITHM_TOKEN_HAMMER:
        # Hammer is NOT an OptimizationAlgorithm member — validate the token, do NOT resolve
        # it against the enum (§2.1). The caller forks to _optimize_hammer_impl on member None.
        return _ALGORITHM_TOKEN_HAMMER, None
    if token not in _ALGORITHM_TOKEN_TO_MEMBER:
        raise OptimizeError(
            "algorithm must be one of "
            f"{sorted(set(_ALGORITHM_TOKEN_TO_MEMBER) | {_ALGORITHM_TOKEN_HAMMER})}, "
            f"got {token!r}",
            family="optimize_param",
        )
    enum_type = _oc._optimization_algorithm_enum(system)
    try:
        member = _resolve_enum(enum_type, _ALGORITHM_TOKEN_TO_MEMBER[token])
    except ToolParamError as exc:
        raise OptimizeError(str(exc), family="optimize_param")
    return token, member


# --------------------------------------------------------------------------- #
# Best-restore checkpoint helpers. INLINE (a deliberate small
# duplication of the aperture_ramp / apply_lens_spec / tolerance SaveAs->LoadFile pattern —
# aperture_ramp imports optimize, so optimize_run must NOT import back from it (cycle);
# there is no shared library primitive, so this is NOT a duplication violation). All NEVER raise.
# --------------------------------------------------------------------------- #
def _forward_slash(path):
    """Normalize to a forward-slash path (#73: SaveAs/LoadFile want forward slashes)."""
    return str(path).replace(os.sep, "/").replace("\\", "/")


def _ckpt_reap(path):
    """Remove the temp checkpoint ``.zmx`` AND the engine's ``.ZDA`` companion (#59).

    The live engine's ``SaveAs`` writes a native ``.ZDA`` binary (same stem), so an unlink
    of the ``.zmx`` placeholder alone LEAKS it. Globs the unique mkstemp token stem in the
    temp dir (scoped to this token, so it can never delete a foreign file). NEVER raises —
    a reap failure must never mask the run outcome.
    """
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:  # noqa: BLE001 — a reap failure must not mask the run outcome
        pass
    try:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, _ext = os.path.splitext(base)
        for hit in glob.glob(os.path.join(directory, stem + "*")):
            try:
                if os.path.exists(hit):
                    os.remove(hit)
            except Exception:  # noqa: BLE001 — a per-file reap failure never masks the outcome
                pass
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


def _resolve_sink(session):
    """Resolve the optimizer's artifact sink (persistence-workspace). NEVER raises.

    Prefer an explicitly-wired ``session.artifact_sink`` (back-compat); else FALL BACK
    to the session-default workspace sink (``workspace._get_default_sink`` — the SAME
    ``<root>/candidates/zmx`` sink ``save_candidate`` uses, so the ``pass00_before`` /
    ``passNN_after`` ``.zmx`` trail shares one manifest/seq). The fallback BUILD touches
    makedirs only (NO engine — its save_as defers ``session.system`` to call time);
    only an unwritable root makes it raise -> we degrade to ``None`` (the caller emits
    the warning ONLY in that case); the trail now always lands.
    """
    sink = getattr(session, "artifact_sink", None)
    if sink is not None:
        return sink
    from .workspace import _get_default_sink
    try:
        return _get_default_sink(session)
    except Exception:  # noqa: BLE001 — unwritable root -> no sink; caller warns
        return None


def _snapshot(session, label, meta, trail):
    """Take one artifact snapshot, append its trail row; NEVER raises (decoupled).

    Reads the ``SnapshotResult`` via ``dataclasses.asdict`` so the source never
    forms the dotted ``result.<the snapshot-index field>`` literal the release guard
    guard flags as a legacy converter file-extension. Resolves the sink via
    ``_resolve_sink`` (explicit -> session-default workspace sink fallback); a
    sink that is STILL None (the workspace-root unwritable case) yields no row here
    (the caller emits the warning once, up front). Returns the row dict (or ``None``
    when no sink), so the caller can detect a failed snapshot.
    """
    sink = _resolve_sink(session)
    if sink is None:
        return None
    # boundary guard: the spec mandates snapshot() NEVER aborts optimize. The
    # ArtifactSink honors its never-raise contract, but a non-conforming/custom sink
    # (or a future regression) that DOES raise must NOT propagate out of optimize and
    # leak the optimizer (the per-pass points are inside the reaping context). Degrade
    # any raise to an ok=False trail row instead.
    try:
        result = sink.snapshot(label, meta)
        fields = asdict(result)
        row = {
            "label": fields["label"],
            "ok": fields["ok"],
            "path": fields["path"],
            "bytes": fields["bytes"],
            "seq": fields["seq"],
            "error": fields["error"],
        }
    except Exception as exc:  # noqa: BLE001 — a snapshot raise never aborts optimize
        row = {
            "label": label,
            "ok": False,
            "path": None,
            "bytes": 0,
            "seq": None,
            "error": f"snapshot raised: {exc!r}",
        }
    trail.append(row)
    return row


# The OptimizeError families that are EXPECTED-failure classes the handler returns
# as an ``{ok:false}`` envelope — NOT raised past its boundary.
# ``optimize_run_failed`` is constructed directly as an envelope in the run loop;
# the families here are the param/unavailable gates that raise from helpers and are
# caught + converted to the same envelope shape at the handler boundary.
_ENVELOPE_FAMILIES = frozenset({
    "optimize_param",
    "optimize_unavailable",
    "optimize_stop_on_glass_vertex",
    "optimize_stop_unfixable",
    "optimize_merit_uncomputable",   # NEW — defensive; current paths build it directly
})


def optimize(session, params):
    """THE CAPSTONE closed loop: preflight -> open-once -> run -> verdict -> reap.

    Locked §a/§c flow:

    1. Embed the dry_run preflight (§d gate). ``no_variables`` / ``no_merit`` /
       ``merit_uncomputable`` (a finite ``>= 1e9`` could-not-compute sentinel)
       short-circuit to the ``optimize_*`` envelope WITHOUT opening (or reaping)
       anything (``merit_uncomputable`` -> ``optimize_merit_uncomputable``).
    2. Validate ``cycles`` / ``cores`` / ``max_passes`` / ``algorithm`` ->
       ``optimize_param`` on a bad value (before opening).
    3. Snapshot ``pass00_before`` (BEFORE the optimizer opens — the forensic
       starting ``.zmx`` predates any mutation).
    4. ``_optimizer_session`` opens ``OpenLocalOptimization()`` ONCE (``None`` ->
       ``optimize_unavailable``, no try/finally entered), configures Algorithm +
       cores, and ``Close()``s in ``finally`` (the reap). FIRST in the open body
       (§2b): a guarded ``opt.IsValid`` read — ``False`` short-circuits to
       ``optimize_merit_uncomputable`` (carrying ``opt.ErrorMessage``, ``passes=0``,
       no cycles run, still reaped by the finally); a read-throw degrades to letting
       the run proceed (the ``optimize_run_failed`` path is the backstop).
    5. Read ``merit_before`` from ``mfe.CalculateMeritFunction()``. Run
       ``max_passes`` bounded passes of ``RunAndWaitForCompletion(cycles)``; after
       each pass read ``opt.CurrentMeritFunction``, snapshot ``pass{NN}_after``,
       classify the pass verdict, EARLY-STOP on ``stable``. An engine run failure
       (``RunAndWaitForCompletion``->False / ``opt.Succeeded``->False) ->
       ``optimize_run_failed`` (``ok=False``).
    6. After Close, re-read ``mfe.CalculateMeritFunction()`` and cross-check it
       against the optimizer's ``CurrentMeritFunction`` (the tripwire;
       non-fatal — flags ``merit_readback_disagreement`` + a warning, never gates
       the verdict).
    7. classify_verdict(merit_before, merit_after) and return the verdict dict
       (``ok=True`` even on stable/diverged).

    EXPECTED failures (``optimize_param`` bad-arg, ``optimize_unavailable`` second-
    open-None) are returned as the ``{ok:false}`` envelope (not
    raised past this boundary). An unexpected/internal raise (a true engine fault,
    or pythonnet driven wrong) propagates to dispatch's never-raise envelope.

    BREAKING CHANGE: ``optimize`` REFUSES a stop on a glass vertex by
    DEFAULT (``require_free_stop=True``) — it returns the
    ``optimize_stop_on_glass_vertex`` envelope (``ok=False``) and opens NOTHING (no
    optimizer, no reap) instead of running. A real aperture stop is a free-standing
    dummy AIR surface, not coincident with a lens vertex. For a vertex-stop system,
    either pass ``auto_normalize=True`` (refactor the stop free, then optimize) or
    ``require_free_stop=False`` (skip the guard and optimize the raw vertex-stop
    system) — or run ``normalize_stop`` first. Earlier callers that optimized a
    vertex-stop sample directly (e.g. the Cooke 40-degree triplet) must now opt out
    explicitly. (§6.3.)
    """
    try:
        return _optimize_impl(session, params)
    except OptimizeError as exc:
        family = getattr(exc, "family", "optimize")
        if family in _ENVELOPE_FAMILIES:
            return _oc.error_envelope("optimize", family, str(exc))
        raise


def _optimize_impl(session, params):
    """The optimize body (see ``optimize`` for the contract)."""
    system = session.system
    wall_start = time.time()

    # (0) the dummy-stop guard params. Bad value -> optimize_param.
    require_free_stop = _bool_param(params, "require_free_stop", True)
    auto_normalize = _bool_param(params, "auto_normalize", False)
    # the opt-in per-config-thin nudge. Read EARLY (before any gate) so a bad
    # value is rejected -> optimize_param even with NO offender (zero mutation, engine unopened).
    # Default False PRESERVES the fail-closed optimize_per_config_thin hard-refuse.
    recover_thin = _bool_param(params, "recover_thin", False)
    # GRIN §4.1: the opt-in no-floor spread-check envelope. Read EARLY (before the
    # preflight) so a bad value refuses opening NOTHING (the recover_thin precedent). None
    # when absent -> the floored path's box audit still runs (weight-/param-independent).
    grin_dn_max = _grin_dn_max_param(params)
    guard_warning = None

    # (1) embed the preflight gate — open NOTHING on a fail. The stop-convention
    # gate rides here (after no_variables/no_merit) when require_free_stop is True.
    (ok, family, variables, number_of_operands, merit_pre,
     stop_idx, stop_material) = _oc._preflight(
        system, require_free_stop=require_free_stop
    )
    if not ok and family == "stop_on_glass_vertex":
        # A vertex stop. With auto_normalize, refactor first then proceed;
        # else refuse, opening NOTHING (the §6.4 envelope; consistent with the
        # no_variables/no_merit short-circuit — ZERO optimizers opened, no reap).
        if not auto_normalize:
            return _stop_vertex_envelope("optimize", stop_idx, stop_material)
        normalize_result = _auto_normalize(session, params)
        if not normalize_result.get("ok", False):
            # The refactor failed-closed (e.g. normalize_no_airspace) -> re-tag as
            # optimize_stop_unfixable, still opening NOTHING.
            return _oc.error_envelope(
                "optimize",
                "optimize_stop_unfixable",
                "auto_normalize could not make the stop free-standing: "
                f"{normalize_result.get('error')}",
                stop_surface=stop_idx,
                stop_material=stop_material,
                normalize_error_family=normalize_result.get("error_family"),
            )
        guard_warning = (
            f"auto_normalize refactored the stop ({normalize_result.get('action')}) "
            "before optimizing"
        )
        # Re-run the preflight on the NOW-free system (variables/merit unchanged by
        # the refactor, but normalize may have added bound operands + freed gaps).
        (ok, family, variables, number_of_operands, merit_pre,
         stop_idx, stop_material) = _oc._preflight(
            system, require_free_stop=require_free_stop
        )
    elif not require_free_stop:
        # §6.3: the guard was deliberately skipped — record the choice in the result.
        guard_warning = (
            "require_free_stop=False: the dummy-stop guard was skipped (a vertex "
            "stop, if any, was NOT refused)"
        )

    if not ok:
        # the failing-row enumeration rides ONLY the merit_uncomputable gate
        # (CONFIRMED readable — same offline pre-open state as dry_run). GATED on the family
        # so the bare no_variables/no_merit gates stay bare (never a stray key on a healthy
        # structural refusal). {} on any fault -> today's payload.
        extra = (
            _oc._uncomputable_row_diagnostics(system)
            if family == "merit_uncomputable" else {}
        )
        return _oc.error_envelope(
            "optimize",
            f"optimize_{family}",
            _gate_message(family, variables, number_of_operands, merit_pre),
            variables=variables,
            number_of_operands=number_of_operands,
            merit=safe_float(merit_pre),
            **extra,
        )

    # (1b) the per-config THIC<=0 gate (DISJOINT optimize_per_config_thin),
    # AFTER the preflight ok-pass and BEFORE opening (or reaping) anything — the ONE
    # shared predicate, consistent with dry_run. Refuse a system carrying an
    # unbuildable per-config gap rather than optimize into / out of a garbage basin.
    # the OPT-IN nudge. Default (recover_thin=False) -> today's byte-identical
    # hard refuse (the fail-closed guard). recover_thin=True -> nudge the collapsed
    # cells to 0.001, RE-SCAN (fail-closed), and refuse if any residual survives (naming what
    # was nudged + what could not be); the disclosure is stashed for the success echo (§2.5).
    nudge_disclosure = None
    thin = _oc._scan_per_config_thin(system)
    if thin:
        if not recover_thin:
            return _per_config_thin_envelope("optimize", thin)
        nudged, un_nudgeable = _oc._nudge_per_config_thin(system, thin)
        thin2 = _oc._scan_per_config_thin(system)
        if thin2:
            return _per_config_thin_envelope(
                "optimize", thin2, nudged=nudged, un_nudgeable=un_nudgeable
            )
        nudge_disclosure = (nudged, un_nudgeable)

    # (1c) the inert-DOF gate (DISJOINT optimize_inert_dof), AFTER the
    # per-config-thin gate and BEFORE opening (or reaping) anything — the ONE shared
    # predicate, consistent with dry_run. Refuse a system carrying an air<->air radius/conic
    # Variable (a zero-merit-sensitivity DOF the optimizer would drift to garbage — the
    # curved-stop silent-wrong) rather than open and drift it. optimize opens NOTHING here.
    inert = _oc._scan_inert_dofs(system)
    if inert:
        return _inert_dof_envelope("optimize", inert)

    # (2) validate the run params (before opening) — bad value -> optimize_param.
    cycles = _require_pos_int(params, "cycles", _DEFAULT_CYCLES)
    max_passes = _require_pos_int(
        params, "max_passes", _DEFAULT_MAX_PASSES, cap=_MAX_PASSES_CAP
    )
    cores = None
    if "cores" in params:
        cores = _require_pos_int(params, "cores", None)
    algorithm_token, algorithm_member = _resolve_algorithm(system, params)
    # The Hammer wall-time cap (minutes). Validated here (before opening)
    # for every algorithm — the default 1.0 is valid so DLS/OD callers are unaffected; a bad
    # value -> optimize_param with NO engine opened. USED only on the Hammer fork below.
    run_time_m = _require_pos_float(
        params, "run_time_m", _DEFAULT_RUN_TIME_M, cap=_RUN_TIME_M_CAP
    )
    run_id = params.get("run_id") or f"optimize_{uuid.uuid4().hex[:12]}"

    mfe = system.MFE
    artifact_trail = []
    warning = None
    # (persistence-workspace): the optimizer trail now ALWAYS lands — _resolve_sink
    # falls back to the session-default workspace sink when no explicit one is wired.
    # The bug-3 warning fires ONLY when even that fallback build fails (an unwritable
    # workspace root), not merely because session.artifact_sink was unset.
    if _resolve_sink(session) is None:
        warning = (
            "could not build an artifact sink (workspace root unwritable?); "
            ".zmx trail not captured"
        )

    # (3) snapshot pass00_before BEFORE the optimizer opens (predates any mutation).
    before_row = _snapshot(
        session,
        "pass00_before",
        {"run_id": run_id, "pass": 0, "cycles": cycles, "algorithm": algorithm_token},
        artifact_trail,
    )
    if before_row is not None and not before_row["ok"]:
        warning = f"pass00_before snapshot failed: {before_row.get('error')}"

    # (3b) The HARD Hammer fork. AFTER the shared preflight gates, the
    # param validation, the sink-warning, AND the pass00_before snapshot — Hammer opens a
    # DIFFERENT Tools resource + commits in place (best-restore), so it is a distinct impl,
    # NOT reached on the DLS path (the _resolve_cycles_member call + the _optimizer_session
    # block below are DLS-only). Same agent surface + envelope shape.
    if algorithm_token == _ALGORITHM_TOKEN_HAMMER:
        return _optimize_hammer_impl(
            session, system, mfe,
            run_time_m=run_time_m,
            cores=cores,
            cycles=cycles,
            run_id=run_id,
            artifact_trail=artifact_trail,
            guard_warning=guard_warning,
            warning=warning,
            variables=variables,
            wall_start=wall_start,
            nudge_disclosure=nudge_disclosure,
            grin_dn_max=grin_dn_max,
        )

    # (4) open the optimizer ONCE; (5) run the bounded passes; (4-finally) reap.
    # The cycle COUNT is set on opt.Cycles (an OptimizationCycles enum member, snapped
    # to the nearest fixed rung) BEFORE the run — RunAndWaitForCompletion() takes NO
    # arguments on the live engine.
    try:
        cycles_member, cycles_member_name = _oc._resolve_cycles_member(system, cycles)
    except ToolParamError as exc:
        raise OptimizeError(str(exc), family="optimize_param")
    # §c step 3: capture merit_before from mfe.CalculateMeritFunction() AND
    # cross-check it against opt.InitialMeritFunction (equal). The MFE read is
    # taken here (the system pre-mutation snapshot); the optimizer's own
    # InitialMeritFunction is read after open, below.
    merit_before = mfe.CalculateMeritFunction()
    opt_current = merit_before
    passes_run = 0
    desync = False
    with _oc._optimizer_session(
        system, algorithm_member=algorithm_member, cores=cores,
        cycles_member=cycles_member,
    ) as (opt, cores_used):
        # (4b) Authoritative pre-run readiness (§2b). The preflight sentinel already
        # short-circuited the common 9e9 case before opening; THIS catches the rare
        # merit < ceiling yet engine-invalid borderline, 100% faithful (the engine's own
        # IsValid + ErrorMessage). GUARDED: an IsValid read-throw degrades to letting the
        # run proceed (the optimize_run_failed path is the backstop) — never crash. The
        # optimizer is still reaped by _optimizer_session's finally (return-inside-with).
        try:
            is_valid = bool(opt.IsValid)
        except Exception:  # noqa: BLE001 — a read-throw -> backstop: let the run proceed
            is_valid = True
        if not is_valid:
            # §4.2: the IsValid:False detection is SELF-CONTAINED — it must always
            # produce optimize_merit_uncomputable. A degraded engine that throws a
            # NON-AttributeError (.NET remoting fault) on read, or returns a proxy whose
            # __bool__/__str__ throws, would otherwise escape the branch and degrade the
            # family to opaque `internal`. _safe_error_message owns EVERY failure mode ->
            # a non-empty str (no trailing `or` to re-expose a __bool__-raising proxy).
            err_msg = _safe_error_message(opt, "the merit function cannot be evaluated")
            return _oc.error_envelope(
                "optimize",
                "optimize_merit_uncomputable",
                _merit_uncomputable_run_message(err_msg),
                run_id=run_id,
                passes=0,
                artifacts=artifact_trail,
                # BEST-EFFORT — post-open MFE readability is UNPROVEN (§4.1). The
                # never-raise helper attaches the key if the read succeeds, else {} and the
                # envelope ships without it. Non-mutating; the optimizer is still reaped by
                # _optimizer_session's finally (return-inside-with).
                **_oc._uncomputable_row_diagnostics(system),
            )

        # The optimizer's OWN starting merit — the consistent-source baseline for the
        # verdict. Cross-check it against the MFE before-read (equal). If they
        # disagree beyond the readback tol, the engine is desynced/pre-baked: flag it
        # and classify CONSERVATIVELY from the optimizer's own Initial-vs-Current pair
        # (never report "improved" on a desync — a stale MFE delta is not proof of a
        # real run).
        opt_initial = getattr(opt, "InitialMeritFunction", merit_before)
        if _oc._readback_disagreement(opt_initial, merit_before):
            desync = True

        # The early-stop baseline tracks the PREVIOUS pass's merit: a plateau
        # over the previous pass is "no further improvement" -> early-stop. Seed it
        # with the optimizer's own start.
        prev_merit = opt_initial
        for pass_index in range(max_passes):
            succeeded_flag = opt.RunAndWaitForCompletion()
            passes_run += 1
            # Engine-level run failure (a COMPLETED-but-failed run, distinct from a
            # diverged result): RunAndWaitForCompletion->False OR Succeeded->False.
            opt_succeeded = bool(getattr(opt, "Succeeded", True))
            if not bool(succeeded_flag) or not opt_succeeded:
                err_msg = _safe_error_message(opt, "run failed")
                # The finally in _optimizer_session still Close()s the optimizer.
                return _oc.error_envelope(
                    "optimize",
                    "optimize_run_failed",
                    str(err_msg),
                    run_id=run_id,
                    passes=passes_run,
                    artifacts=artifact_trail,
                )

            opt_current = opt.CurrentMeritFunction
            after_row = _snapshot(
                session,
                f"pass{pass_index:02d}_after",
                {
                    "run_id": run_id,
                    "pass": pass_index,
                    "merit_before": safe_float(merit_before),
                    "merit_after": safe_float(opt_current),
                    "cycles": cycles,
                    "algorithm": algorithm_token,
                },
                artifact_trail,
            )
            if after_row is not None and not after_row["ok"]:
                warning = (
                    f"pass{pass_index:02d}_after snapshot failed: "
                    f"{after_row.get('error')}"
                )

            # early-stop on a PASS-TO-PREVIOUS stable verdict (no further
            # improvement over the prior pass — the true convergence gate). Comparing
            # to the ORIGINAL merit_before would never early-stop a plateau.
            pass_verdict = _oc.classify_verdict(prev_merit, opt_current)
            prev_merit = opt_current
            if pass_verdict == "stable":
                break

        # Capture the canonical run result (the optimizer's own post-run value)
        # WHILE the handle is still live (before the finally Close()).
        merit_after = opt.CurrentMeritFunction
        status = str(getattr(opt, "Status", ""))
        succeeded = bool(getattr(opt, "Succeeded", True))

    # (6) tripwire: re-read the MFE AFTER Close (it survives teardown)
    # and cross-check the optimizer's captured CurrentMeritFunction. Non-fatal.
    mfe_recalc = mfe.CalculateMeritFunction()
    disagreement = _oc._readback_disagreement(merit_after, mfe_recalc)
    if disagreement:
        warning = (
            "merit read-back disagreement: opt.CurrentMeritFunction="
            f"{merit_after!r} vs mfe.CalculateMeritFunction()={mfe_recalc!r}"
        )

    # (7) classify the verdict from the OPTIMIZER's OWN pair (Initial-vs-Current) —
    # a consistent source (§c step 3). On a desync (the optimizer's InitialMeritFunction
    # disagreed with the MFE before-read), classify conservatively: if the optimizer's
    # own Initial≈Current it is stable/no-op REGARDLESS of the MFE delta — a stale MFE
    # before-value must never be reported as "improved" on a true no-op. We compute the
    # verdict from (opt_initial, merit_after) and also cross-check it against the MFE
    # before-read; the optimizer's own pair is authoritative.
    verdict = _oc.classify_verdict(opt_initial, merit_after)
    if desync:
        warning = (
            "optimizer InitialMeritFunction desync: opt.InitialMeritFunction="
            f"{opt_initial!r} vs mfe.CalculateMeritFunction()={merit_before!r}; "
            "verdict classified from the optimizer's own Initial-vs-Current pair"
        )
    # The reported delta uses the optimizer's own pre/post pair (the consistent source)
    # so improvement_abs/pct never mixes the MFE before with the optimizer after.
    improvement_abs = _improvement_abs(opt_initial, merit_after)
    improvement_pct = _improvement_pct(opt_initial, merit_after)

    # Surface the guard note (auto_normalize ran / require_free_stop skip)
    # alongside any run-time warning — neither should clobber the other (§6.3).
    warning = _merge_warning(guard_warning, warning)

    # MCE: the config-span disclosure + the single-config-over-multi-config
    # WARN (NEVER a refusal). Merge the span warning with the run-time warning.
    span = _config_span_disclosure(system)
    warning = _merge_warning(warning, span["warning"])

    # non-blocking ray-free-merit WARN — the merit exists here
    # (a run completed). Merged with the run-time + span warnings (all may be None).
    warning = _merge_warning(warning, _oc._scan_rayfree_merit(system))

    # Post-optimize edge / buried-center audit — ONCE on the RESULT,
    # after the pass loop + optimizer Close(). Reuses check_clearance (agreement by
    # construction: the edge number is check_clearance's min(semi) geometry, NOT
    # MNEG.Value — the SEQ wizard floors at Surf1's larger aperture, ~40% divergent),
    # fully guarded, additive keys, never flips ok, never raises. Only the SUCCESS
    # return is hooked (a failed/refused/uncomputable run has no completed geometry).
    audit_floor = _resolve_audit_glass_floor(system)
    edge_audit = _edge_audit_warnings(session, audit_floor, _DEFAULT_MIN_AIR)

    result = {
        "ok": True,
        "verdict": verdict,
        "improved": verdict == "improved",
        "merit_before": safe_float(opt_initial),
        "merit_after": safe_float(merit_after),
        "improvement_abs": safe_float(improvement_abs),
        "improvement_pct": safe_float(improvement_pct),
        "algorithm": algorithm_token,
        "cycles": cycles,
        "cycles_member": cycles_member_name,
        "cores": cores_used,
        "succeeded": succeeded,
        "status": status,
        "wall_time_s": safe_float(round(time.time() - wall_start, 4)),
        "variables_count": variables,
        "passes": passes_run,
        "run_id": run_id,
        "merit_desync": bool(desync),
        "mfe_merit_before": safe_float(merit_before),
        "merit_readback_disagreement": bool(disagreement),
        "merit_spans_configs": span["merit_spans_configs"],
        "n_configs": span["n_configs"],
        "configs_covered": span["configs_covered"],
        "readback": {
            "opt_current": safe_float(merit_after),
            "mfe_recalc": safe_float(mfe_recalc),
        },
        "artifacts": artifact_trail,
        "warning": warning,
    }
    result.update(edge_audit)  # 0-3 additive keys; never overwrites a base key
    # GRIN §4.4: the post-optimize GRIN index audit — ONCE on the RESULT, after the
    # edge audit (the both-tails drift-pin). Additive keys, never flips ok,
    # never raises. The box audit runs regardless of grin_dn_max (weight-/param-independent).
    result.update(_grin_index_audit_warnings(session, grin_dn_max))
    # the post-optimize merit<->reality (chromatic-blindness) audit — ONCE on
    # the RESULT, additive keys, never flips ok, never raises. merit_scalar = the in-hand
    # merit_after (human context; NO extra CalculateMeritFunction call).
    result.update(_merit_reality_divergence_warning(session, safe_float(merit_after)))
    # (§2.5): the opt-in-nudge success disclosure (per_config_thin_nudged +
    # the pairing/knife-edge warning). Absent (byte-identical) when nothing was nudged.
    result = _apply_nudge_disclosure(result, nudge_disclosure)
    return result


def _optimize_hammer_impl(session, system, mfe, *, run_time_m, cores, cycles, run_id,
                          artifact_trail, guard_warning, warning, variables, wall_start,
                          nudge_disclosure=None, grin_dn_max=None):
    """The Hammer (global-search) fork of ``optimize`` (§2.4/§2.5).

    Hammer opens ``Tools.OpenHammerOptimization()`` (NOT the local optimizer), sets
    ``AutomaticOptimization``+``TargetRunTimeM``, runs ONE ``RunAndWaitForCompletion()`` that
    COMMITS to the live LDE, and best-restores the pre-run design if the result came out
    WORSE (never return worse than entry). The result dict is a PARALLEL build that CALLS the
    same leaf helpers the DLS tail uses (no DLS refactor), so the
    ``negative_air_gap_warning`` (via ``_edge_audit_warnings``) rides a Hammer result too.

    All reaps run in the ``finally``: the checkpoint ``.zmx``/``.ZDA`` is glob-reaped (#59)
    and ``_hammer_session`` ``Close()``s the optimizer. Failure classes reuse the
    ``_ENVELOPE_FAMILIES`` (``optimize_unavailable`` / ``optimize_merit_uncomputable`` /
    ``optimize_run_failed``).
    """
    opt_initial = merit_before = mfe.CalculateMeritFunction()

    # Best-restore checkpoint BEFORE Hammer mutates in place (Hammer commits to the live LDE).
    # A SaveAs hiccup -> best-restore unavailable (checkpoint:false + a warning), NOT a refusal
    # (Hammer AUTO keeps the best found — a documented degrade). Precedent: the SaveAs
    # repoints system.SystemFile to the temp path (benign — save_candidate/promote_best do
    # their own SaveAs to the workspace).
    checkpoint_ok = False
    ckpt_fwd = None
    try:
        fd, raw = tempfile.mkstemp(suffix=".zmx", prefix=_HAMMER_CKPT_PREFIX)
        os.close(fd)
        ckpt_fwd = _forward_slash(raw)
        system.SaveAs(ckpt_fwd)                       # #73 forward slash
        checkpoint_ok = True
    except Exception:  # noqa: BLE001 — a SaveAs hiccup -> best-restore unavailable, proceed
        checkpoint_ok = False

    hammer_restored = False
    warning_restore = None
    merit_after = merit_before
    status = ""
    cores_used = None
    succeeded = True
    run_failed = False
    run_failed_msg = None
    try:
        with _oc._hammer_session(
            system, run_time_m=run_time_m, cores=cores
        ) as (ham, cores_used):
            # sibling: wrap the run + post-run reads in ONE try/except so ANY mid-run
            # throw (RAWFC faults, or the CurrentMeritFunction/Status read throws after the
            # run committed) routes to the SAME run_failed restore path — a failed run must
            # restore the checkpoint, whether it failed by bool, by Succeeded, OR by throw
            # (never an opaque `internal` leak, never a mutated design left behind). The
            # restore is DEFERRED to after the `with` exit (Hammer Closes first) — we NEVER
            # LoadFile while the handle is open. (An exception from _hammer_session.__enter__
            # — the OpenHammerOptimization None-raise / a config-set throw — is OUTSIDE this
            # try, so it still surfaces as optimize_unavailable / propagates, un-swallowed.)
            try:
                # Pre-run readiness (parity with the DLS opt.IsValid guard): a guarded
                # read-throw degrades to proceeding (the optimize_run_failed path is the
                # backstop).
                try:
                    is_valid = bool(ham.IsValid)
                except Exception:  # noqa: BLE001 — a read-throw -> backstop: let the run proceed
                    is_valid = True
                if not is_valid:
                    # Pre-run: RAWFC was NOT called, so the design is UNMUTATED — return
                    # without a restore (the outer finally still reaps the checkpoint once).
                    err_msg = _safe_error_message(
                        ham, "the merit function cannot be evaluated"
                    )
                    return _oc.error_envelope(
                        "optimize", "optimize_merit_uncomputable",
                        _merit_uncomputable_run_message(err_msg),
                        run_id=run_id, passes=0, artifacts=artifact_trail,
                        # BEST-EFFORT (§4.1) — the design is UNMUTATED here (RAWFC not
                        # yet called). Attach the enumeration if readable, else {}; reaped by
                        # the outer finally either way.
                        **_oc._uncomputable_row_diagnostics(system),
                    )
                ok = ham.RunAndWaitForCompletion()    # -> True commits to the live LDE
                succeeded = bool(getattr(ham, "Succeeded", True))
                if not bool(ok) or not succeeded:
                    # a genuinely-failed run may have committed a PARTIAL/worse
                    # mutation. Capture the failure + EXIT the `with` (Hammer closes) so the
                    # checkpoint can be restored BEFORE returning.
                    run_failed = True
                    run_failed_msg = _safe_error_message(ham, "hammer run failed")
                else:
                    merit_after = ham.CurrentMeritFunction  # read WHILE the handle is live
                    status = str(getattr(ham, "Status", ""))
            except Exception as exc:  # noqa: BLE001 — a mid-run/read throw routes to restore
                run_failed = True
                run_failed_msg = f"hammer run raised: {exc!r}"
        # --- after Close (the Hammer handle is released) ---
        if run_failed:
            # restore the entry design (the failed run may have mutated it), guarded
            # + never-raise. A missing checkpoint (SaveAs failed) can only warn.
            restore_note = None
            if checkpoint_ok:
                try:
                    system.LoadFile(ckpt_fwd, False)
                    mfe_check = mfe.CalculateMeritFunction()
                    if not _oc._readback_disagreement(mfe_check, merit_before):
                        restore_note = "the design was restored to entry"
                    else:
                        restore_note = (
                            "the restore read-back did not match entry; the live design may "
                            "be mutated — reload your last saved .zmx"
                        )
                except Exception:  # noqa: BLE001 — a restore throw never raises past the handler
                    restore_note = (
                        "the restore LoadFile threw; the live design may be mutated — reload "
                        "your last saved .zmx"
                    )
            else:
                restore_note = (
                    "no checkpoint was available (SaveAs failed); the live design may be "
                    "mutated — reload your last saved .zmx"
                )
            msg = run_failed_msg or "hammer run failed"
            return _oc.error_envelope(
                "optimize", "optimize_run_failed", f"{msg} ({restore_note})",
                run_id=run_id, passes=0, artifacts=artifact_trail,
            )
        # NEVER return worse than entry. Restore on a WORSE result EVEN when
        # classify_verdict bins it "stable" (within tol), OR on a NON-FINITE result — NOT
        # merely verdict=="diverged" (which misses a marginally-worse-but-stable result).
        # An IMPROVED (merit_after < merit_before) or exactly-equal result never restores.
        should_restore = checkpoint_ok and (
            not _finite(merit_after)
            or (_finite(merit_before) and merit_after > merit_before)
        )
        if should_restore:
            try:
                system.LoadFile(ckpt_fwd, False)      # restore the entry design
                mfe_check = mfe.CalculateMeritFunction()
                if not _oc._readback_disagreement(mfe_check, merit_before):
                    merit_after = merit_before
                    hammer_restored = True
                else:
                    warning_restore = (
                        "Hammer produced a worse result and the restore read-back did not "
                        "match entry; the live design may hold a worse result — reload your "
                        "last .zmx"
                    )
            except Exception:  # noqa: BLE001 — a restore throw never raises past the handler
                warning_restore = (
                    "Hammer produced a worse result and the restore LoadFile threw; reload "
                    "your last saved .zmx"
                )
    finally:
        if ckpt_fwd is not None:
            _ckpt_reap(ckpt_fwd)                      # #59 glob the stem incl. .ZDA

    # Post-run snapshot (trail parity with the DLS pass loop).
    _snapshot(
        session,
        "pass00_after",
        {
            "run_id": run_id,
            "pass": 0,
            "merit_before": safe_float(merit_before),
            "merit_after": safe_float(merit_after),
            "algorithm": _ALGORITHM_TOKEN_HAMMER,
            "run_time_m": safe_float(run_time_m),
        },
        artifact_trail,
    )

    # (PARALLEL tail — CALL the same leaf helpers, NO DLS refactor.)
    verdict = _oc.classify_verdict(opt_initial, merit_after)
    if hammer_restored:
        verdict = "stable"                            # merit_after == merit_before
    mfe_recalc = mfe.CalculateMeritFunction()
    disagreement = _oc._readback_disagreement(merit_after, mfe_recalc)
    improvement_abs = _improvement_abs(opt_initial, merit_after)
    improvement_pct = _improvement_pct(opt_initial, merit_after)

    # Merge the guard note + run-time warning + the checkpoint/restore/disagreement notes.
    warning = _merge_warning(guard_warning, warning)
    warning = _merge_warning(warning, warning_restore)
    if not checkpoint_ok:
        warning = _merge_warning(
            warning,
            "could not checkpoint before the Hammer run (SaveAs failed); best-restore "
            "unavailable — Hammer AUTO keeps the best found",
        )
    if hammer_restored:
        warning = _merge_warning(
            warning,
            "Hammer produced a worse result; the pre-run design was restored (no change)",
        )
    if disagreement:
        warning = _merge_warning(
            warning,
            "merit read-back disagreement: ham.CurrentMeritFunction="
            f"{merit_after!r} vs mfe.CalculateMeritFunction()={mfe_recalc!r}",
        )
    span = _config_span_disclosure(system)
    warning = _merge_warning(warning, span["warning"])
    warning = _merge_warning(warning, _oc._scan_rayfree_merit(system))

    # The post-optimize edge / negative-air-gap audit rides a Hammer
    # result too (drift-pin — the same leaf helper as the DLS tail).
    audit_floor = _resolve_audit_glass_floor(system)
    edge_audit = _edge_audit_warnings(session, audit_floor, _DEFAULT_MIN_AIR)

    result = {
        "ok": True,
        "verdict": verdict,
        "improved": verdict == "improved",
        "merit_before": safe_float(opt_initial),
        "merit_after": safe_float(merit_after),
        "improvement_abs": safe_float(improvement_abs),
        "improvement_pct": safe_float(improvement_pct),
        "algorithm": _ALGORITHM_TOKEN_HAMMER,
        "run_time_m": safe_float(run_time_m),
        "cycles": cycles,
        "cycles_member": None,
        "cores": cores_used,
        "succeeded": succeeded,
        "status": status,
        "hammer_restored": hammer_restored,
        "checkpoint": checkpoint_ok,
        "wall_time_s": safe_float(round(time.time() - wall_start, 4)),
        "variables_count": variables,
        "passes": 1,
        "run_id": run_id,
        "merit_desync": False,
        "mfe_merit_before": safe_float(merit_before),
        "merit_readback_disagreement": bool(disagreement),
        "merit_spans_configs": span["merit_spans_configs"],
        "n_configs": span["n_configs"],
        "configs_covered": span["configs_covered"],
        "note": (
            "Hammer is wall-time bounded (run_time_m); cycles/max_passes are not used."
        ),
        "readback": {
            "opt_current": safe_float(merit_after),
            "mfe_recalc": safe_float(mfe_recalc),
        },
        "artifacts": artifact_trail,
        "warning": warning,
    }
    result.update(edge_audit)  # 0-4 additive keys; never overwrites a base key
    # GRIN §4.4: the post-optimize GRIN index audit rides a Hammer result too (the
    # both-tails drift-pin — the same leaf helper as the DLS tail).
    result.update(_grin_index_audit_warnings(session, grin_dn_max))
    # the post-optimize merit<->reality (chromatic-blindness) audit rides a
    # Hammer result too (the same leaf helper as the DLS tail). merit_after is in scope.
    result.update(_merit_reality_divergence_warning(session, safe_float(merit_after)))
    # (§2.5): the opt-in-nudge disclosure rides a Hammer result too (the nudge
    # ran BEFORE the algorithm fork), so a recover_thin nudge is never silently undisclosed.
    result = _apply_nudge_disclosure(result, nudge_disclosure)
    return result


def _improvement_abs(before, after):
    """``before - after`` (positive when improved); non-finite-guarded."""
    try:
        if not _finite(before) or not _finite(after):
            return float("nan")
        return before - after
    except Exception:  # noqa: BLE001 — a non-numeric merit -> nan
        return float("nan")


def _improvement_pct(before, after):
    """``(before - after) / before * 100``; non-finite / zero-before guarded."""
    try:
        if not _finite(before) or not _finite(after) or before == 0:
            return float("nan")
        return (before - after) / before * 100.0
    except Exception:  # noqa: BLE001 — a non-numeric merit -> nan
        return float("nan")


def _finite(value):
    """True if ``value`` is a finite real number (not bool/non-number/non-finite)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _finite_below(v, floor):
    """True iff ``v`` and ``floor`` are both finite real numbers and ``v < floor``."""
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and isinstance(floor, (int, float))
        and not isinstance(floor, bool)
        and math.isfinite(v)
        and math.isfinite(floor)
        and v < floor
    )


def _resolve_audit_glass_floor(system):
    """The (d) post-optimize audit's glass floor (§D-FLOOR OPTION-ii / MIN).

    The design's OWN authored positive floor — the MIN positive MNEG target (else the
    MIN positive MNCG target) read through the shared ``_min_positive_target`` reader —
    with the standard 1.0 mm fallback when none is authored. Reading the authored floor
    kills the micro-optic false-positive (a design built at min_glass=0.3 audited at a
    flat 1.0 would false-warn). MIN is the loosest authored intent, so it never
    over-warns a legitimately-thin design. NEVER raises -> _DEFAULT_MIN_GLASS on a throw.

    ``_min_positive_target`` returns a STRICTLY-positive float or ``None`` (never a
    falsy-but-valid 0.0), so the ``or``-chain is correct.
    """
    try:
        mfe = system.MFE
        return (
            _oc._min_positive_target(mfe, "MNEG")
            or _oc._min_positive_target(mfe, "MNCG")
            or _DEFAULT_MIN_GLASS
        )
    except Exception:  # noqa: BLE001 — advisory; fall back to the standard floor
        return _DEFAULT_MIN_GLASS


def _edge_audit_warnings(session, glass_floor, air_floor):
    """0-3 additive STRING keys from ONE ``check_clearance`` on the RESULT.

    ``thin_edge_warning`` / ``buried_center_warning`` / ``negative_bfl_warning``, each
    present only when firing. Numbers are READ from the returned envelope (agreement
    with an independent ``check_clearance`` by construction — NOT ``MNEG.Value``, which
    the SEQ wizard clamps at target and evaluates at Surf1's larger aperture, ~40%
    divergent from ``check_clearance``'s ``min(semi)`` convention). NEVER raises
    -> ``{}`` on any throw / non-dict / ``ok:false`` / missing field. Runs ONCE.
    """
    # (§2.4 "NEVER raises -> {} on any throw / non-dict / missing field"): the
    # WHOLE body is guarded, not just the check_clearance CALL. A malformed-but-truthy
    # envelope (a firing glass gap missing surface/next_surface, or a truthy NON-DICT
    # global_bfd) would otherwise raise in the classifier / message f-strings / bfd.get()
    # and ABORT a successful optimize at this hook. Any failure -> {} (additive-nothing),
    # never propagates. (The inner .get() / isinstance guards are belt-and-suspenders; the
    # outer try is the load-bearing never-raise guarantee.)
    try:
        env = clearance.check_clearance(
            session, {"min_glass": glass_floor, "min_air": air_floor}
        )
        if not isinstance(env, dict) or not env.get("ok"):
            return {}  # degraded / malformed -> no key

        out = {}
        gaps = env.get("gaps") or []  # [] on a folded system

        # --- thin_edge_warning: any GLASS gap whose EDGE < that gap's own threshold ---
        thin = [
            g
            for g in gaps
            if isinstance(g, dict)
            and g.get("kind") == "glass"
            and _finite_below(g.get("edge_thickness"), g.get("threshold"))
        ]
        if thin:
            out["thin_edge_warning"] = (
                "thin/negative glass EDGE(s) in the optimized result: "
                + "; ".join(
                    f"S{g.get('surface')}->S{g.get('next_surface')} edge "
                    f"{g.get('edge_thickness'):.3f} mm (< {g.get('threshold')})"
                    for g in thin
                )
                + ". Author/raise an edge floor (build_merit glass=true) and re-optimize."
            )

        # --- buried_center_warning: a glass gap immediately followed by another glass gap
        #     (surface i AND i+1 both glass => a glass->glass cement interior) center < floor ---
        buried = []
        for j in range(len(gaps) - 1):
            g, gnext = gaps[j], gaps[j + 1]
            if (
                isinstance(g, dict)
                and isinstance(gnext, dict)
                and g.get("kind") == "glass"
                and gnext.get("kind") == "glass"
                and _finite_below(g.get("center_thickness"), g.get("threshold"))
            ):
                buried.append(g)
        if buried:
            out["buried_center_warning"] = (
                "sub-floor cemented interior element CENTER(s): "
                + "; ".join(
                    f"S{g.get('surface')}->S{g.get('next_surface')} center "
                    f"{g.get('center_thickness'):.3f} mm (< {g.get('threshold')})"
                    for g in buried
                )
                + ". A buried cement center reads fine on a surface glance but is "
                "sub-manufacturable."
            )

        # --- negative_air_gap_warning: an INTERIOR air gap whose CENTER is NEGATIVE
        #     (physical interpenetration — surfaces cross, nonphysical).
        #     This reads clean as a mid-loop `improved` verdict, so it is the ticket's named
        #     correctness hole (the -0.567 mm session bug). Guard (§1.3): kind=="air" AND NOT
        #     is_back_airgap (a negative back-airgap is owned by negative_bfl_warning — no
        #     double-warn) AND center < 0 via _finite_below. Keys on center<0 NEVER `violation`
        #     a dummy AIR stop reads center 0.0 / violation true and must not fire.
        #     Folded systems get it free (gaps==[] on a fold — we read `gaps`, never
        #     `folded_gaps`). ---
        neg_air = [
            g
            for g in gaps
            if isinstance(g, dict)
            and g.get("kind") == "air"
            and not g.get("is_back_airgap")
            and _finite_below(g.get("center_thickness"), 0.0)
        ]
        if neg_air:
            out["negative_air_gap_warning"] = (
                "interpenetrating (negative) interior AIR gap(s) in the optimized result: "
                + "; ".join(
                    f"S{g.get('surface')}->S{g.get('next_surface')} center "
                    f"{g.get('center_thickness'):.3f} mm"
                    for g in neg_air
                )
                + ". Surfaces cross (nonphysical) — reload a checkpoint and re-optimize with "
                "an air center floor (build_merit air=true, min_air>0). Note the wizard's MNCA "
                "floor authors at weight 1 and can lose to a heavy custom operand."
            )

        # --- negative_bfl_warning: image plane in FRONT of the last optical surface ---
        bfd = env.get("global_bfd")
        if not isinstance(bfd, dict):
            bfd = {}
        blo = bfd.get("behind_last_optic")
        if (
            isinstance(blo, (int, float))
            and not isinstance(blo, bool)
            and math.isfinite(blo)
            and blo < 0.0
        ):
            out["negative_bfl_warning"] = (
                f"the back focal distance is NEGATIVE ({blo:.3f} mm): the image plane falls "
                "IN FRONT of the last optical surface (rear group / field-flattener may be "
                "misplaced)."
            )

        # thread the positive audit-coverage + not-audited evidence
        # through the optimize result tail, so a CLEAN-passing GRIN is distinguishable from a
        # never-examined one (the exact ambiguity grin_geometric_audit closes). Additive
        # (non-string) keys; the whole body is already under the outer never-raise try, so a
        # malformed env degrades to no key.
        ga = env.get("grin_geometric_audit")
        if isinstance(ga, dict) and ga.get("audited"):
            out["grin_geometric_audit"] = ga
        gna = env.get("grin_not_audited")
        if isinstance(gna, list) and gna:
            out["grin_not_audited"] = gna
        return out
    except Exception:  # noqa: BLE001 — §2.4: the advisory audit NEVER raises -> {} on any fault
        return {}


def _mrd_classify_config(poly_env, pw_env, config):
    """Classify ONE config's (field) points into (hits, skipped, n_sampled) (§2.1).

    ``poly_env`` = a default (polychromatic) ``get_spot`` envelope; ``pw_env`` = the
    ``per_wave:true, kind:rms`` ``get_spot`` envelope for the SAME config. Pure: reads
    only the two dicts, never touches the engine. A point FIRES iff its poly RMS
    clears ``_MRD_POLY_FLOOR_UM`` AND ``poly / max(per_wave)`` clears
    ``_MRD_RATIO_THRESHOLD``. Every degenerate field is SKIPPED (recorded in ``skipped``
    with a reason), never read as a hit; ``n_sampled`` counts only the points that
    passed §2.1 steps 1-2 (valid poly AND >=1 valid per-wave) and were tested.
    """
    hits = []
    skipped = []
    # Guard h: a get_spot ok:false / non-dict envelope for a config -> that config skipped.
    if not (isinstance(poly_env, dict) and poly_env.get("ok")):
        return hits, skipped, 0
    if not (isinstance(pw_env, dict) and pw_env.get("ok")):
        # A dropped config (per-wave get_spot ok:false/throw) is DISCLOSED,
        # not silent — mirror the field-level skip disclosure at config granularity.
        skipped.append({"config": config, "reason": "per_wave_unavailable_config"})
        return hits, skipped, 0
    # Guard d (mono): read number_of_wavelengths off the POLY envelope; <=1 -> no-op
    # SILENT for the config (cross-wave divergence is undefined with a single wavelength).
    n_waves = poly_env.get("number_of_wavelengths")
    if not (isinstance(n_waves, int) and not isinstance(n_waves, bool) and n_waves > 1):
        return hits, skipped, 0
    poly_spots = poly_env.get("spots")
    pw_spots = pw_env.get("spots")
    if not isinstance(poly_spots, list) or not isinstance(pw_spots, list):
        return hits, skipped, 0

    n_sampled = 0
    for entry in poly_spots:
        if not isinstance(entry, dict):
            continue
        f = entry.get("field")
        # (step 1) poly term: accept ONLY a verified, finite, positive poly RMS. A
        # valid:false (ray-fail/vignetted, case c) OR valid:null (indeterminate/budget,
        # case e) field is SKIPPED — never read rms_raw, never fire on an unverified poly.
        p = entry.get("rms")
        if not (entry.get("valid") is True and _finite(p) and p > 0):
            skipped.append({
                "config": config, "field": f,
                "reason": entry.get("reason") or "poly_invalid_or_unverified",
            })
            continue
        # (step 2) per-wave term: the valid, finite, positive per-wave RMS for this field.
        W = [
            e.get("rms")
            for e in pw_spots
            if isinstance(e, dict)
            and e.get("field") == f
            and e.get("valid") is True
            and _finite(e.get("rms"))
            and e.get("rms") > 0
        ]
        if not W:
            # case f: no valid wave for the field -> the ratio cannot be formed -> SKIP.
            skipped.append({
                "config": config, "field": f, "reason": "per_wave_unavailable",
            })
            continue
        max_w = max(W)
        # (step 3) denominator = the WORST (blurriest) wave, guarded by the diffraction
        # floor so a sub-0.05 per-wave denom yields a sane, interpretable ratio (case b).
        denom = max(max_w, _MRD_DENOM_FLOOR_UM)
        ratio = p / denom
        n_sampled += 1
        # (step 5) FIRE iff poly clears the absolute floor AND the ratio clears threshold.
        if p >= _MRD_POLY_FLOOR_UM and ratio >= _MRD_RATIO_THRESHOLD:
            hits.append({
                "config": config, "field": f,
                # Emit the ACTUAL denominator used so ratio is self-consistent
                # (ratio == poly_rms_um / per_wave_denom_um ALWAYS). per_wave_rms_um keeps
                # the RAW worst-wave reading; denom_floored flags the diffraction clamp.
                "poly_rms_um": p, "per_wave_rms_um": max_w,
                "per_wave_denom_um": denom, "denom_floored": denom > max_w,
                "ratio": ratio,
            })
    return hits, skipped, n_sampled


def _mrd_headline(worst, n_hits, n_sampled, merit_disp):
    """The §4.1 human headline for the worst-over-(config,field) divergence hit."""
    c = worst.get("config")
    c_disp = 1 if c is None else c
    return (
        "merit-vs-reality divergence (chromatic blindness): config "
        f"{c_disp} field {worst.get('field')} polychromatic RMS "
        f"{worst.get('poly_rms_um'):.1f} um is {worst.get('ratio'):.0f}x the worst "
        f"per-wavelength RMS {worst.get('per_wave_rms_um'):.3f} um (threshold "
        f"{_MRD_RATIO_THRESHOLD:.0f}x, poly floor {_MRD_POLY_FLOOR_UM:.0f} um; merit "
        f"reads {merit_disp:.3f}). Each wavelength is individually sharp but they land "
        "apart (lateral/axial color the spot merit cannot see). "
        f"{n_hits} of {n_sampled} sampled (config,field) points diverge. Author real-ray "
        "chromatic operands (REAY(w_F)-REAY(w_C) DIFF via add_math_constraint) or a "
        "common-centroid criterion, then re-optimize — a small wizard merit here does "
        "NOT mean a corrected image."
    )


def _merit_reality_divergence_warning(session, merit_scalar):
    """Post-optimize merit<->reality (poly-vs-worst-per-wave) chromatic-blindness audit.

    Additive keys ``merit_reality_divergence_warning`` (str) + ``merit_reality_divergence``
    (dict), present ONLY when >=1 (config,field) point diverges; the keys are ABSENT
    otherwise. Fires iff poly_RMS >= _MRD_POLY_FLOOR_UM AND poly_RMS/max(per_wave_RMS) >=
    _MRD_RATIO_THRESHOLD, per (config, field). NEVER raises -> returns {} on any throw /
    ok:false / non-dict / missing field / no valid point. Runs ONCE on the SUCCESS result.
    ``merit_scalar`` = the tail's already-computed ``merit_after`` (human context only; NO
    extra CalculateMeritFunction call).

    Cost = 2xN ``get_spot`` calls (POLY + per-wave RSCE per config), once post-run. The
    per-wave call is REQUIRED: StandardSpot ignores the ``wave`` arg for the poly RMS, so
    RSCE ``per_wave`` is the only single-wavelength path. ``config="all"`` is REFUSED by
    ``get_spot`` (heavy class) — iterate single-config indices; ``get_spot`` self-wraps
    ``with_configuration`` and always restores, so the helper never switches configs itself.
    """
    try:
        system = session.system
        n = _ccfg.safe_number_of_configurations(system)
        configs = [None] if n <= 1 else list(range(1, n + 1))

        all_hits = []
        all_skipped = []
        n_sampled = 0
        for k in configs:
            base = {} if k is None else {"config": k}
            poly_env = analysis_spot.get_spot(session, dict(base))
            pw_params = {"per_wave": True, "kind": "rms"}
            pw_params.update(base)
            pw_env = analysis_spot.get_spot(session, pw_params)
            hits, skipped, sampled = _mrd_classify_config(poly_env, pw_env, k)
            all_hits.extend(hits)
            all_skipped.extend(skipped)
            n_sampled += sampled

        if not all_hits:
            return {}  # no divergence -> additive-nothing (both keys ABSENT)

        # The worst hit = the largest ratio; tie-break on the largest poly RMS.
        worst = max(all_hits, key=lambda h: (h["ratio"], h["poly_rms_um"]))
        hits_sorted = sorted(all_hits, key=lambda h: h["ratio"], reverse=True)
        # A non-finite / string ("nan"/"inf") merit becomes None in the
        # payload (JSON-clean); only a finite float is carried verbatim. Headline stays graceful.
        merit_val = merit_scalar if _finite(merit_scalar) else None
        merit_disp = merit_scalar if _finite(merit_scalar) else float("nan")
        headline = _mrd_headline(worst, len(all_hits), n_sampled, merit_disp)
        payload = {
            "units": "um",
            "ratio_threshold": _MRD_RATIO_THRESHOLD,
            "poly_floor_um": _MRD_POLY_FLOOR_UM,
            "denom_floor_um": _MRD_DENOM_FLOOR_UM,
            "merit_scalar": merit_val,
            "worst": worst,
            "n_hits": len(all_hits),
            "n_sampled": n_sampled,
            "hits": hits_sorted[:12],   # capped at the 12 largest-ratio hits
            "skipped": all_skipped,
        }
        return {
            "merit_reality_divergence_warning": headline,
            "merit_reality_divergence": payload,
        }
    except Exception:  # noqa: BLE001 — §3.3: the advisory audit NEVER raises -> {} on any fault
        return {}


# =========================================================================== #
# GRIN — the post-run index audit (detect side, §4). Additive STRING keys,
# never flips ``ok``, never overwrites a base key, runs ONCE per success on BOTH tails.
# =========================================================================== #
# §4.2 — the STATIC audit-failed constant (its construction cannot throw, so the outer
# except can always return it; NEVER {} — a detect-side failure is DISCLOSED).
_GRIN_AUDIT_FAILED_MSG = (
    "the post-optimize GRIN index audit FAILED to run — no GRIN warnings from this run "
    "are meaningful; verify the index profile manually."
)


def _grin_dn_max_param(params):
    """The §4.1 opt-in ``grin_dn_max`` param (the NO-FLOOR path's spread-check input).

    ``None`` if absent (the floored path needs NO param — the box audit discovers the
    authored box on the live MFE). Finite, non-bool, ``> 0`` -> ``float(v)``; else
    ``OptimizeError(family="optimize_param")`` — raised EARLY in ``_optimize_impl`` (before
    the preflight; a bad value opens NOTHING — the ``recover_thin`` precedent).
    """
    if "grin_dn_max" not in params:
        return None
    value = params["grin_dn_max"]
    if isinstance(value, bool):
        raise OptimizeError(
            f"'grin_dn_max' must be a number, not a bool ({value!r})",
            family="optimize_param",
        )
    if not isinstance(value, (int, float)):
        raise OptimizeError(
            f"'grin_dn_max' must be a number, got {type(value).__name__} {value!r}",
            family="optimize_param",
        )
    value = float(value)
    if not math.isfinite(value):
        raise OptimizeError(
            f"'grin_dn_max' must be a finite number, got {value!r}", family="optimize_param"
        )
    if value <= 0:
        raise OptimizeError(
            f"'grin_dn_max' must be > 0, got {value}", family="optimize_param"
        )
    return value


def _grin_axial_disclosure(surfaces):
    """The §4.3 ``grin_axial_monotonicity_not_audited`` LOUD disclosure — fires on
    EVERY axial-capable surface's existence (a DISCLOSURE, not
    a fired verdict), so a clean audit NEVER falsely certifies a monotonic OR fully-bounded
    axial profile."""
    names = ", ".join(f"S{s}" for s in surfaces)
    return (
        f"axial GRIN surface(s) {names}: axial n(z) monotonicity is NOT audited in this "
        "release, and n>=1 / index-range are verified at the 6 sampled points only — an "
        "interior-z extremum between samples is unaudited (both are sampled checks, "
        "not analytic ones); a "
        "clean audit does not certify a monotonic or fully-bounded axial profile."
    )


def _grin_box_violation_message(surf, violations):
    """Build the §4.3 ``grin_index_box_violated_warning`` message for one surface.

    ``violations`` = ``[(point, va, gt|None, lt|None)]`` — ALL in PHYSICAL INDEX. The I#VA
    readings report the physical index for every GRIN type and the floor authors its bounds
    in that same space, so NO conversion is applied here. Names each live index
    + its authored bound(s) — the floor was authored but the run finished outside it
    (slipped/drowned, weight-independent detection). A checkpoint-reload recovery pointer + the
    sampled-coverage note.
    """
    parts = []
    for (point, va, gt, lt) in violations:
        lo = "-inf" if gt is None else f"{float(gt):.4f}"
        hi = "+inf" if lt is None else f"{float(lt):.4f}"
        parts.append(f"point {point} index {float(va):.4f} outside [{lo}, {hi}]")
    return (
        f"GRIN index floor slipped/drowned on S{surf}: " + "; ".join(parts) + ". The floor "
        "was authored but the run finished outside it (weight-independent detection) — "
        "reload a checkpoint and re-optimize (raise the floor weight or reduce the "
        "competing operand)."
    )


def _grin_index_audit_warnings(session, grin_dn_max):
    """The §4.2/§4.3 post-run GRIN index audit -> ``dict[str, str]`` (0..6 additive keys).

    NEVER flips ``ok``, NEVER overwrites a base key, NEVER aborts a successful optimize;
    runs ONCE per successful return. Every fired key carries ``_GRIN_SAMPLED_COVERAGE_NOTE``
    (the audit never presents itself as a full-field verdict).

    STRUCTURE: a PER-SURFACE try — a per-surface throw / malformed summary routes THAT
    surface to ``grin_index_unread_warning`` and the loop CONTINUES (one bad surface never
    erases the others). The OUTER except (total-body throw) returns
    ``{"grin_index_audit_failed": _GRIN_AUDIT_FAILED_MSG}`` (a STATIC message) — NEVER ``{}``
    (a detect-side failure is DISCLOSED, never silently clean).
    """
    try:
        system = session.system
        entries, disc_faults = _gic.grin_surfaces(system)

        buckets = {
            "grin_index_nonphysical_warning": [],
            "grin_index_box_violated_warning": [],
            "grin_dn_exceeds_envelope_warning": [],
            "grin_index_unread_warning": [],
        }
        note = _gic._GRIN_SAMPLED_COVERAGE_NOTE
        tol = _gic._GRIN_BOX_AUDIT_TOL

        # a discovery fault reads as an UNREAD surface (never a clean optimize on a
        # skipped-at-discovery GRIN surface).
        for fault in disc_faults:
            s = fault.get("surface")
            reason = fault.get("reason")
            label = "an unknown surface" if s is None else f"S{s}"
            buckets["grin_index_unread_warning"].append(
                f"could not read/audit the GRIN index profile on {label} ({reason}) — the "
                "Δn / nonphysical audit did not cover it; verify manually."
            )

        # The axial disclosure fires on EVERY axial-capable surface's EXISTENCE (identity,
        # not a read — cannot throw), so a faulted / throwing per-surface audit still discloses.
        axial_surfaces = [s for (s, _info, is_axial) in entries if is_axial]

        for (surf, info, is_axial) in entries:
            try:
                summary = _gic.index_summary(system, surf, info, is_axial)
                if not isinstance(summary, dict):
                    raise ValueError("malformed index summary")

                if summary.get("fault"):
                    buckets["grin_index_unread_warning"].append(
                        f"could not read/audit the GRIN index vector on S{surf} — the Δn / "
                        "nonphysical audit did not cover it; verify manually."
                    )
                    continue

                # (a) n<1 nonphysical — UNCONDITIONAL (independent of grin_dn_max + the floor).
                # ``min_index`` is the PHYSICAL index (the I#VA reads verbatim),
                # so the n<1 grade is correct for every GRIN type with no conversion.
                mi = summary.get("min_index")
                if (isinstance(mi, (int, float)) and not isinstance(mi, bool)
                        and math.isfinite(mi) and mi < _gic._GRIN_NONPHYSICAL_FLOOR):
                    buckets["grin_index_nonphysical_warning"].append(
                        f"GRIN index nonphysical on S{surf}: min bulk index "
                        f"{mi:.4f} < 1.0 — a passive medium cannot have n<1."
                    )

                # (b) the box audit — weight- AND param-independent. Reads the
                # authored box off the live MFE and checks every live I#VA against the
                # authored bound. BOTH are PHYSICAL INDEX — the comparison is
                # space-consistent for every type with no conversion on either side.
                rvec = summary.get("index_vector")
                box_coverage = None     # §4.3 two-path detect: the (c) envelope defer gate.
                box_faulted = False
                if isinstance(rvec, list) and rvec:
                    box = _gic.read_authored_box(system.MFE, surf)
                    box_coverage = box.get("coverage")
                    box_faulted = bool(box.get("fault"))
                    if box_faulted:
                        # A box-scan / per-row read fault means the drowned-floor
                        # backstop could NOT run — DISCLOSE it, never a silent clean. Skip the
                        # box-violation check (the authored box is unreliable).
                        buckets["grin_index_unread_warning"].append(
                            f"could not read/audit the authored GRIN index floor box on "
                            f"S{surf} — the drowned-floor box audit did not run; verify "
                            "manually."
                        )
                    else:
                        audit_rows = box.get("audit_rows") or {}
                        violations = []
                        for point, bounds in audit_rows.items():
                            if not (1 <= point <= len(rvec)):
                                continue
                            gt, lt = bounds
                            va = rvec[point - 1]
                            low = None if gt is None else (gt - tol)
                            high = None if lt is None else (lt + tol)
                            if ((low is not None and va < low)
                                    or (high is not None and va > high)):
                                violations.append((point, va, gt, lt))
                        if violations:
                            buckets["grin_index_box_violated_warning"].append(
                                _grin_box_violation_message(surf, violations)
                            )

                # (c) the no-floor spread check — ONLY when grin_dn_max supplied, and
                # ONLY when a COMPLETE authored floor is NOT present for this surface (§4.3
                # two-path detect): a complete floor -> the box audit (leg b,
                # grin_index_box_violated_warning) governs — weight-independent, tolerance-banded
                # and it catches a drowned floor too — so the strict raw envelope-exceeds check
                # would only FALSE-FIRE at the DLS restoring-force equilibrium (raw Δn settling
                # just over the cap while HELD in the box). A box read fault already routed to
                # unread above (never a silent skip) -> defer there, skip the envelope.
                if (grin_dn_max is not None
                        and not box_faulted
                        and box_coverage != "complete"):
                    dn = summary.get("dn")
                    if (isinstance(dn, (int, float)) and not isinstance(dn, bool)
                            and math.isfinite(dn) and dn > grin_dn_max):
                        # The I#VA readings ARE the physical index for every
                        # GRIN type, so the Δn is a physical index swing with no conversion.
                        buckets["grin_dn_exceeds_envelope_warning"].append(
                            f"GRIN Δn exceeds the declared envelope on S{surf}: sampled Δn "
                            f"{dn:.4f} (source {summary.get('dn_source')}; I#VA is the "
                            "physical index; a sampled LOWER bound) > cap "
                            f"{grin_dn_max:.4f} — reload a checkpoint and re-optimize."
                        )

                # (d) the axial half unread — an axial surface whose DLTN faulted.
                if is_axial and summary.get("dltn_fault"):
                    buckets["grin_index_unread_warning"].append(
                        f"could not read the axial DLTN half of S{surf} — its axial Δn was "
                        "not audited; verify manually."
                    )
            except Exception:  # noqa: BLE001 — a per-surface throw -> unread, others survive
                buckets["grin_index_unread_warning"].append(
                    f"the GRIN index audit body threw on S{surf} — it was not audited; "
                    "verify manually."
                )
                continue

        out = {}
        for key, msgs in buckets.items():
            if msgs:
                out[key] = " ".join(msgs) + " " + note
        if axial_surfaces:
            out["grin_axial_monotonicity_not_audited"] = (
                _grin_axial_disclosure(axial_surfaces) + " " + note
            )
        return out
    except Exception:  # noqa: BLE001 — a TOTAL malfunction is DISCLOSED, never {} (clean)
        return {"grin_index_audit_failed": _GRIN_AUDIT_FAILED_MSG}


DRY_RUN_SPEC = ToolSpec(
    name="dry_run",
    handler=dry_run,
    required_params=(),
    param_types={"require_free_stop": "boolean"},
    description=(
        "Preflight an optimization without running it: confirm variables + a merit "
        "are set (ready:True) and the stop is optimizable, without opening the "
        "optimizer or mutating anything. Gotcha: require_free_stop defaults True, so "
        "this REFUSES a glass-vertex stop — run normalize_stop first (or pass "
        "auto_normalize). See optimize, normalize_stop, build_merit."
    ),
)

OPTIMIZE_SPEC = ToolSpec(
    name="optimize",
    handler=optimize,
    required_params=(),
    param_types={
        "require_free_stop": "boolean",
        "auto_normalize": "boolean",
        "cycles": "number",
        "max_passes": "number",
        "cores": "number",
        "algorithm": "string",
        "run_time_m": "number",
        "run_id": "string",
        "bound_kind": "string",
        "min_air": "number",
        "add_bounds": "boolean",
        "free_gaps": "boolean",
        "recover_thin": "boolean",
        "grin_dn_max": "number",
    },
    description=(
        "Run a bounded local optimization (the closed loop): preflight, run cycles, "
        "and classify the merit verdict (improved/stable/diverged) from the "
        "optimizer's own initial-vs-current pair, so a no-op never reads as improved. "
        "algorithm defaults to DLS (damped least squares); OD is orthogonal descent; "
        "algorithm='Hammer' runs a wall-time-bounded global search (run_time_m minutes, "
        "default 1.0, cap 10.0) that escapes a shallow local minimum and best-restores if "
        "it comes out worse. run_time_m is a wall-time CAP, not a target: Hammer runs in "
        "AUTO mode and early-terminates when the global search converges, so a fast return "
        "(well under a second on a small or already-good system) with real merit improvement "
        "is the expected early-stop, NOT a dropped cap. Gotcha: require_free_stop defaults True, so this REFUSES a "
        "glass-vertex stop with no optimizer opened — run normalize_stop first (or pass "
        "auto_normalize). Persist the result with save_candidate. "
        "If a run is refused optimize_merit_uncomputable (merit 9e9 — a corner ray cannot "
        "trace at full pupil), the envelope's merit_row_diagnostics.suspects names the "
        "wide-field/high-pupil rows (config + Hx/Hy/Px/Py, corner-first). ESCAPE: edit the "
        "offending geometry, then set_vignetting(mode='from_rays') PER CONFIG, rebuild the "
        "merit (build_merit — GQ operands re-evaluate against the new vignetting), re-add "
        "any custom operand suite (serialize_merit -> apply_merit_recipe append), and re-run. "
        "recover_thin (default false): when a per-config THIC has collapsed to <=0, opt in to "
        "nudge it to 0.001 so the optimizer can open — ONLY effective when PAIRED with a heavy "
        "per-config floor (build_merit(span_configs=true)) + the THIC as an optimizer DOF "
        "(set_config_variable); otherwise the gap re-collapses. Default refuses (fail-closed, "
        "optimize_per_config_thin). "
        "grin_dn_max (optional, > 0): a GRIN (gradient-index) design's post-run index audit "
        "reports grin_dn_exceeds_envelope_warning when the sampled index Δn on a GRIN surface "
        "exceeds this cap. The floored path (build_merit(grin_dn_max=Δ)) needs NO param here — "
        "its per-point box is audited automatically (grin_index_box_violated_warning); pass "
        "grin_dn_max at optimize only for the no-floor spread check. n<1 is always flagged. "
        "See dry_run, normalize_stop, build_merit, save_candidate."
    ),
)

TOOL_SPECS = (DRY_RUN_SPEC, OPTIMIZE_SPEC)
