"""tools/scale_lens.py — the catalog-bench S1 geometric-scale tool.

ONE dispatchable tool ``scale_lens(mode, value)`` driving the native single-slot
``Tools.OpenScale()`` BY-FACTOR (never by-units — the inert-no-op trap), proving
the scale by READ-BACK: EFL AND total-track scale by the factor AND f/# is
preserved (the native scale scales the aperture too). A clean call is NOT proof.

The structure (§2):

1. Pre-mutation validation (zero engine touch): ``mode`` ∈ {to_efl, factor} and
   ``value`` finite > 0 (reject bool / non-number / nan / inf / <= 0). Any
   violation -> ``scale_param`` (raised ScaleError, netted by the decorator).
2. Read the BEFORE triple (EFFL/WFNO/TOTR, wave 1) via the ``_measurement_common``
   named-slot reader — the SAME afocal/1e10-sentinel firewall ``get_first_order``
   uses (a suspicious reading -> None).
3. Resolve the factor + ``already_at_target`` BEFORE any apply: ``to_efl`` ->
   ``value/current_EFL`` (an afocal/~0/sentinel current EFL, or a sign/degenerate
   computed factor -> ``scale_efl_undefined``; ``|cur-target| <= tol*target`` ->
   benign ok:true no-op, apply SKIPPED); ``factor`` -> ``value`` (``|factor-1| <=
   tol`` -> benign no-op). ``already_at_target`` is decided PRE-apply, so it is
   structurally disjoint from ``scale_noop`` (decided POST-apply).
4. Drive the native scale: ``OpenScale`` (None -> ``scale_write``) ->
   ``ScaleByFactor=True`` / ``ScaleByUnits=False`` / ``ScaleFactor`` ->
   ``RunAndWaitForCompletion`` -> ``Close()`` in ``finally`` (L22).
5. Read the AFTER triple and PROVE (the read-back is the SOLE authority; Succeeded
   is a raw disclosure, never a gate): a mandatory/armed leg unreadable ->
   ``scale_readback_failed``; nothing moved (factor != 1) -> ``scale_noop``; moved
   wrong / f/# drift / target miss -> ``scale_readback_failed``; else ok:true.

Refusal families: ``scale_param`` | ``scale_efl_undefined`` | ``scale_noop`` |
``scale_readback_failed`` | ``scale_write`` (the frozen 5-family set).

Live ZOS-API integration: exercised by the live scale test
(LG1 positive to_efl/factor on cooke + a get_first_order cross-check; LG3 afocal
``scale_efl_undefined``); unit-tested against the fixture-seeded fakes
whose ``RunAndWaitForCompletion`` MUTATES a shared
state the read-back reads (a hollow echo would redden the mutate-fails).
``scale_noop`` / ``scale_readback_failed`` are offline-fake-authoritative (a
by-factor scale always mutates a real live system — the captured by-units no-op
is the make-it-bite, replayed by the fake ``noop_mode``).
"""
import functools
import math
from contextlib import contextmanager

from .._io import safe_float
from ..errors import ScaleError
from ..server import ToolSpec
from . import _config_common as _cc          # safe_current_configuration
from . import _measurement_common as _mc     # read_operand_slots (afocal firewall)
from ._analysis_common import error_envelope

# The read-back proof tolerance — an INTERNAL constant, NOT a caller param (axis
# 3 / Q4): every leg is checked at ``rel_tol=_SCALE_TOL`` (a real scale matched
# to ~1e-15; 1% is generous headroom, and relative is scale-invariant — the point of
# the tool). f/# preservation is RELATIVE (``|wfno_after-wfno_before| <= tol*wfno_before``).
_SCALE_TOL = 0.01
# |EFL| below this is degenerate/~0 -> scale-to-EFL is undefined (afocal/collimated).
_EFL_FLOOR = 1e-9

# The unexpected-engine-throw family (an engine fault that escapes the body net
# degrades to ``scale_write`` — the engine WAS touched; the caller must not retry
# blind onto an already-scaled system, but a raw throw is a write fault).
_ENGINE_ERROR_FAMILY = "scale_write"

# EFFL/WFNO/TOTR are read wave-1 with slot-2 unused (0) — the EXACT slot map
# ``get_first_order`` uses (D2 / Q6).
_TRIPLE_CODES = (("efl", "EFFL"), ("wfno", "WFNO"), ("totr", "TOTR"))
_WAVE_SLOTS = {3: ("Wave", 1)}


def _never_raise(tool_name):
    """Wrap the handler so it NEVER raises past its boundary (copied from tolerance_run).

    A ``ScaleError`` keeps its intended structured ``family`` (one of the five
    frozen families). ANY other ``Exception`` (pythonnet maps every .NET throw onto
    an ``Exception`` subclass; ``RecursionError`` is in the caught set) is netted to
    a typed ``scale_write`` envelope so a disconnected engine mid-scale becomes
    ``{ok:false}`` rather than a crash. ``BaseException`` (KeyboardInterrupt /
    SystemExit) is deliberately NOT caught — it propagates AFTER the slot-reaping
    ``finally`` (L22) has run.
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params):
            try:
                return handler(session, params)
            except ScaleError as exc:
                return error_envelope(tool_name, getattr(exc, "family", "scale"),
                                      str(exc))
            except Exception as exc:  # noqa: BLE001 — net any engine throw -> scale_write
                return error_envelope(
                    tool_name, _ENGINE_ERROR_FAMILY,
                    f"{tool_name} hit an unexpected engine error ({exc!r}); refusing "
                    "rather than claiming an unverified scale",
                )
        return _wrapped
    return _decorate


def _require_dict(params):
    """Never-raise: a non-dict ``params`` becomes ``{}`` (aperture_surface precedent)."""
    return params if isinstance(params, dict) else {}


def _finite_positive(value):
    """Require a FINITE, strictly-positive number -> float; else ScaleError(scale_param).

    Rejects a bool (an int subclass — ``True`` would silently "scale x1"), a
    non-number, a non-finite (nan/inf/-inf), and any value ``<= 0`` (copies the
    ``aperture_surface._finite`` firewall). PRE-mutation, zero engine touch.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScaleError(
            f"value must be a finite number > 0, got {type(value).__name__} {value!r}",
            family="scale_param",
        )
    # A Python int too large for a C double raises OverflowError from float() (a
    # >308-digit int literal reaches the handler intact via json.loads over the wire);
    # a hostile __float__ can raise ValueError/TypeError. It is malformed CALLER input
    # caught PRE-mutation (zero engine touch) -> scale_param, NEVER scale_write (§3c).
    try:
        coerced = float(value)
    except (OverflowError, ValueError, TypeError) as exc:
        raise ScaleError(
            f"value must be a finite number > 0, got an un-coercible "
            f"{type(value).__name__} {value!r}",
            family="scale_param",
        ) from exc
    if not math.isfinite(coerced):
        raise ScaleError(
            f"value must be a finite number (inf/-inf/nan are non-physical), got {value!r}",
            family="scale_param",
        )
    if coerced <= 0.0:
        raise ScaleError(f"value must be > 0, got {coerced}", family="scale_param")
    return coerced


def _read_scalar(system, code):
    """Read one first-order scalar (EFFL/WFNO/TOTR, wave 1) -> float | None.

    Uses ``read_operand_slots`` (the anti-FACT-1 named-slot firewall): a suspicious
    reading (the 1e10 afocal sentinel, a non-finite, a non-number) -> None; an
    unresolvable operand / MFE throw -> None (never leaked). ``None`` is the afocal
    signal the caller branches on.
    """
    try:
        raw, suspicious = _mc.read_operand_slots(system, code, _WAVE_SLOTS)
    except Exception:  # noqa: BLE001 — an unresolvable operand / MFE throw -> None
        return None
    if suspicious or isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    # L26 (BUG-A sweep): a huge Python int would overflow float() — a read-side fault
    # is UNREADABLE (None), never an escaping OverflowError (which would net to the
    # engine-touched scale_write family). The MFE normally marshals a C double, so this
    # is defense-in-depth for the same class the value gate closes pre-mutation.
    try:
        val = float(raw)
    except (OverflowError, ValueError, TypeError):
        return None
    if not math.isfinite(val):
        return None
    return val


def _read_triple(system):
    """Read the {efl, wfno, totr} first-order triple (each a float or None)."""
    return {key: _read_scalar(system, code) for key, code in _TRIPLE_CODES}


def _within_noop_band(before, factor, tol):
    """True iff a scale by ``factor`` is INDISTINGUISHABLE from a no-op at ``tol`` on
    the NONZERO legs the post-apply ``scale_noop`` gate checks (TOTR + EFL iff readable).

    BUG-B fix — the dead-zone crux: the pre-apply ``already_at_target`` skip and the
    post-apply ``scale_noop`` gate must be EXACT complements, or a factor the skip
    misses gets refused as ``scale_noop``. ``math.isclose(factor, 1.0)`` is NOT a
    reliable complement of the gate's ``math.isclose(after, before)`` — float rounding
    of ``before*factor`` diverges from ``factor-1`` at the boundary (``factor=0.99``:
    ``isclose(0.99, 1.0)`` is False yet ``isclose(0.99*before, before)`` is True — a
    mirror dead-zone). So this predicts the after per leg (``before*factor``) and runs
    the SAME ``math.isclose`` the gate runs; for the fake (``after == before*factor``
    bit-for-bit) it is IDENTICAL to the gate, so the two are provably complementary and
    no float-rounding dead-zone can open. (A genuine no-op — the by-units inert trap —
    leaves the REAL after == before while the PREDICTED after == before*factor is far
    off, so the gate still fires on the real after; predicted != real is what makes the
    trap detectable.)

    BUG-C fix (R3 HIGH, L26 sibling of the BUG-B fix): a leg reading EXACTLY ``0.0``
    gives ``0.0*factor == 0.0``, so ``math.isclose(0.0, 0.0)`` is True for ANY factor —
    a real factor-2 scale whose SOLE readable leg was ``TOTR == 0.0`` (efl unreadable)
    was silently skipped as ``already_at_target`` (a false ok:true, zero engine touch).
    A ``0.0`` leg carries ZERO multiplicative proof-information, so it is EXCLUDED from
    the band (the ``b != 0.0`` filter). When NO nonzero leg remains (the degenerate
    zero-track / afocal case), the band decision falls back to the pure factor test —
    so a material factor is NOT skipped, the apply runs, and the post-apply gate lands
    it in the LOUD ``scale_noop`` refusal (the read-back cannot prove a scale on a 0.0
    leg), never a false ok:true. The ``b != 0.0`` filter does not touch a normal
    system (TOTR/EFL are materially nonzero) — it is exactly the R3-HIGH hole, closed.
    """
    legs = [before[k] for k in ("totr", "efl")
            if before[k] is not None and before[k] != 0.0]
    if not legs:  # no NONZERO readable leg (degenerate) — fall back to the pure factor test.
        return math.isclose(factor, 1.0, rel_tol=tol)
    return all(math.isclose(b * factor, b, rel_tol=tol) for b in legs)


@contextmanager
def _scale_session(system):
    """Open the single Scale Tools slot; ``Close()`` in ``finally`` on EVERY path (L22).

    ``OpenScale()`` returning ``None`` = the single Tools slot is already open (a
    left-open optimizer/tolerancer) -> ``scale_write`` (Q1: the frozen 5-family set
    is kept; no new ``scale_unavailable``). Nothing to close on that path.
    """
    tool = system.Tools.OpenScale()
    if tool is None:
        raise ScaleError(
            "the single Tools slot is already open; close the open optimizer/"
            "tolerancer and retry",
            family="scale_write",
        )
    try:
        yield tool
    finally:
        try:
            tool.Close()
        except Exception:  # noqa: BLE001 — Close must run on every path, never raise
            pass


def _safe_error_message(tool):
    """Read ``tool.ErrorMessage`` guarded -> a str (empty on any read/None fault)."""
    try:
        msg = getattr(tool, "ErrorMessage", "")
    except Exception:  # noqa: BLE001 — a hostile property read must not raise
        return ""
    if msg is None:
        return ""
    try:
        return str(msg)
    except Exception:  # noqa: BLE001 — a throwing __str__ must not raise
        return ""


def _success(mode, *, factor, before, after, target_efl, succeeded,
             already_at_target, active_config, proof_skipped):
    """Build the success envelope (§3a) — every numeric through ``safe_float``."""
    return {
        "ok": True,
        "tool": "scale_lens",
        "mode": mode,
        "applied_factor": safe_float(factor),
        "efl_before": safe_float(before["efl"]),
        "efl_after": safe_float(after["efl"]),
        "target_efl": safe_float(target_efl) if target_efl is not None else None,
        "wfno_before": safe_float(before["wfno"]),
        "wfno_after": safe_float(after["wfno"]),
        "totr_before": safe_float(before["totr"]),
        "totr_after": safe_float(after["totr"]),
        "succeeded": succeeded,
        "already_at_target": already_at_target,
        "scaled_ok": True,
        "proof_skipped": proof_skipped,
        "scaled_config_only": active_config,
    }


def _readback_failed(mismatch, mode, factor, target_efl, before, after, succeeded,
                     active_config):
    """Build the ``scale_readback_failed`` envelope (§3b) — full before/after diagnostics."""
    return error_envelope(
        "scale_lens", "scale_readback_failed",
        f"the scale did not read back as proven ({mismatch}); refusing rather than "
        "claiming an unverified scale",
        mode=mode,
        applied_factor=safe_float(factor),
        target_efl=safe_float(target_efl) if target_efl is not None else None,
        efl_before=safe_float(before["efl"]), efl_after=safe_float(after["efl"]),
        wfno_before=safe_float(before["wfno"]), wfno_after=safe_float(after["wfno"]),
        totr_before=safe_float(before["totr"]), totr_after=safe_float(after["totr"]),
        succeeded=succeeded, mismatch=mismatch, scaled_config_only=active_config,
    )


def _prove_and_envelope(mode, factor, target_efl, before, after, succeeded,
                        active_config):
    """The read-back proof (§2 / C1 leg semantics) — the SOLE authority (C2).

    Legs: ``totr_ratio`` is MANDATORY (both modes); ``efl_ratio`` / ``wfno_held`` are
    ARMED iff their before-value was readable (an unarmed leg is SKIPPED + disclosed
    in ``proof_skipped`` — this is what makes A4 (factor mode on an afocal system)
    well-defined); ``efl_target`` runs in ``to_efl`` mode. Gate order (first match):
    (1) a mandatory/armed leg whose after-value is unreadable -> scale_readback_failed;
    (2) scale_noop (nothing moved, factor != 1) — checked FIRST among readable outcomes;
    (3) scale_readback_failed (a RUN leg fails); (4) ok:true.
    """
    tol = _SCALE_TOL
    efl_armed = before["efl"] is not None
    wfno_armed = before["wfno"] is not None

    # (1) Mandatory/armed legs whose AFTER value is unreadable -> scale_readback_failed
    # (NEVER scale_noop on unreadable data — noop is a positive "nothing moved"
    # diagnosis, not an unreadable fallback). Deterministic order: totr, efl, wfno.
    if before["totr"] is None or after["totr"] is None:
        return _readback_failed("unreadable:totr_ratio", mode, factor, target_efl,
                                before, after, succeeded, active_config)
    if efl_armed and after["efl"] is None:
        return _readback_failed("unreadable:efl_ratio", mode, factor, target_efl,
                                before, after, succeeded, active_config)
    if wfno_armed and after["wfno"] is None:
        return _readback_failed("unreadable:wfno_held", mode, factor, target_efl,
                                before, after, succeeded, active_config)

    # A before-unreadable leg is SKIPPED + disclosed (the afocal/factor-mode case).
    proof_skipped = []
    if not efl_armed:
        proof_skipped.append("efl_ratio")
    if not wfno_armed:
        proof_skipped.append("wfno_held")

    # (2) scale_noop — checked FIRST among readable outcomes. Every RUN ratio leg
    # reads UNCHANGED (after ~ before at rel_tol) while the requested factor is
    # materially != 1. The inert by-units no-op trap: Succeeded True yet nothing moved.
    # BUG-B/BUG-C fix: the outer guard is the SAME _within_noop_band predicate (on the
    # PREDICTED after over NONZERO legs) the pre-apply already_at_target skip uses, so
    # the two are provably complementary — a factor within-band is skipped BEFORE apply,
    # and only a factor materially outside the band can reach this gate; the REAL-after
    # unchanged check below then fires only on a genuine no-op (a by-units inert scale,
    # OR the degenerate case whose sole readable leg is 0.0 so it reads 0.0 both before
    # AND after and the band fell back to the factor test -> a SAFE scale_noop refusal,
    # never a false ok:true). No dead-zone; no zero-leg vacuous skip.
    if not _within_noop_band(before, factor, tol):
        unchanged = math.isclose(after["totr"], before["totr"], rel_tol=tol)
        checked_legs = ["TOTR"]
        if efl_armed:
            unchanged = unchanged and math.isclose(
                after["efl"], before["efl"], rel_tol=tol)
            checked_legs.insert(0, "EFL")
        if unchanged:
            return error_envelope(
                "scale_lens", "scale_noop",
                "the scale ran clean (Succeeded reported) but the read-back proves "
                f"nothing moved ({'/'.join(checked_legs)} unchanged) at the requested "
                f"factor {factor!r}; a clean call is NOT proof of a scale.",
                applied_factor=safe_float(factor),
                efl_before=safe_float(before["efl"]), efl_after=safe_float(after["efl"]),
                totr_before=safe_float(before["totr"]),
                totr_after=safe_float(after["totr"]),
                succeeded=succeeded, scaled_config_only=active_config,
            )

    # (3) scale_readback_failed — the first RUN leg that fails names the mismatch.
    mismatch = None
    if mode == "to_efl" and not math.isclose(after["efl"], target_efl, rel_tol=tol):
        mismatch = (f"efl_target: efl_after={after['efl']!r} misses target "
                    f"{target_efl!r}")
    elif efl_armed and not math.isclose(
            after["efl"], before["efl"] * factor, rel_tol=tol):
        mismatch = (f"efl_ratio: efl_after={after['efl']!r} != efl_before*factor "
                    f"({before['efl']!r}*{factor!r})")
    elif not math.isclose(after["totr"], before["totr"] * factor, rel_tol=tol):
        mismatch = (f"totr_ratio: totr_after={after['totr']!r} != totr_before*factor "
                    f"({before['totr']!r}*{factor!r})")
    elif wfno_armed and abs(after["wfno"] - before["wfno"]) > tol * abs(before["wfno"]):
        mismatch = (f"wfno_held: f/# drifted (wfno_before={before['wfno']!r} -> "
                    f"wfno_after={after['wfno']!r}, > {tol} relative)")
    if mismatch is not None:
        return _readback_failed(mismatch, mode, factor, target_efl, before, after,
                                succeeded, active_config)

    # (4) All RUN legs pass -> ok:true (succeeded disclosed RAW, may be False; C2).
    return _success(mode, factor=factor, before=before, after=after,
                    target_efl=target_efl, succeeded=succeeded,
                    already_at_target=False, active_config=active_config,
                    proof_skipped=proof_skipped)


@_never_raise("scale_lens")
def scale_lens(session, params):
    """Scale a lens geometrically and PROVE the scale by read-back. See the module docstring.

    ``mode`` (REQUIRED) is ``'to_efl'`` (scale so the effective focal length hits
    ``value`` mm, > 0 — factor = value/current_EFL) or ``'factor'`` (scale every
    length by ``value`` > 0). The native scale scales the aperture too, so f/# is
    PRESERVED. Refuses a scale-to-EFL on an afocal/collimated system
    (``scale_efl_undefined``), a scale that ran clean but moved nothing
    (``scale_noop`` — a clean call is NOT proof), and a partial/wrong scale
    (``scale_readback_failed``). ``already_at_target`` is a benign ok:true no-op
    (apply skipped), structurally disjoint from ``scale_noop``. The scale is global
    geometry; ``to_efl`` targets the ACTIVE config's EFL (``scaled_config_only``
    discloses it). NEVER raises past its boundary.
    """
    params = _require_dict(params)
    system = session.system

    # (A) mode — PRE-mutation, zero engine touch.
    mode = params.get("mode")
    if mode not in ("to_efl", "factor"):
        raise ScaleError(
            f"mode must be 'to_efl' or 'factor', got {mode!r}", family="scale_param")

    # (B) value — finite > 0 (reject bool / non-number / nan / inf / <= 0).
    value = _finite_positive(params.get("value"))

    # (C) read the BEFORE triple + active config (the afocal firewall basis).
    before = _read_triple(system)
    active_config = _cc.safe_current_configuration(system)

    # (D) resolve the factor + already_at_target — BEFORE any apply (the disjointness lock).
    if mode == "to_efl":
        target_efl = value
        cur = before["efl"]
        if cur is None or abs(cur) < _EFL_FLOOR:
            return error_envelope(
                "scale_lens", "scale_efl_undefined",
                "scale-to-EFL is undefined: the current EFL is non-finite / ~0 / the "
                "1e10 sentinel (afocal/reflective-collimated). Scale by 'factor', or "
                "grade collimation with verify_collimation.",
                efl_before=safe_float(cur), target_efl=safe_float(target_efl),
                scaled_config_only=active_config,
            )
        factor = target_efl / cur
        if not math.isfinite(factor) or factor <= 0.0:
            # A geometric scale can never flip EFL sign (Q3 — a sign/degenerate mismatch).
            # (Decided from the COMPUTED factor, so cur<0 routes here before the
            # already_at_target skip below ever runs.)
            return error_envelope(
                "scale_lens", "scale_efl_undefined",
                f"a positive geometric scale cannot reach the target EFL {target_efl!r} "
                f"from the current EFL {cur!r} (sign/degenerate mismatch); a geometric "
                "scale never changes EFL sign.",
                efl_before=safe_float(cur), target_efl=safe_float(target_efl),
                scaled_config_only=active_config,
            )
        if _within_noop_band(before, factor, _SCALE_TOL):
            # Benign ok:true no-op decided PRE-apply (disjoint from scale_noop). Apply
            # SKIPPED. BUG-B fix: the skip predicate is EXACTLY the post-apply no-op
            # gate's "unchanged" condition (math.isclose on the PREDICTED after per
            # NONZERO leg), so any factor the gate would later call "nothing moved" is
            # skipped HERE first — no dead-zone on EITHER side of 1.0 (the old
            # abs(cur-target)<=tol*target under-covered the downscale side). BUG-C: a
            # coincidentally-zero leg is excluded, so a material factor on a zero-track
            # system is NOT vacuously skipped as already_at_target.
            return _success(
                "to_efl", factor=1.0, before=before, after=before,
                target_efl=target_efl, succeeded=None, already_at_target=True,
                active_config=active_config, proof_skipped=[])
    else:  # factor mode
        target_efl = None
        factor = value
        if _within_noop_band(before, factor, _SCALE_TOL):
            # Identity scale = benign ok:true no-op (avoids the scale_noop ambiguity; Q2).
            # BUG-B fix: the predicted-after band (not abs(factor-1)<=tol) is COMPLEMENTARY
            # to the post-apply no-op gate, closing the dead-zone on BOTH sides of 1.0
            # (~0.99 and ~1.01) where the old skip missed a factor the gate then flagged
            # as scale_noop. BUG-C fix: a coincidentally-zero leg is excluded from the
            # band, so a material factor on a zero-track system is NOT vacuously skipped
            # as already_at_target (it applies, then lands in scale_noop).
            return _success(
                "factor", factor=1.0, before=before, after=before, target_efl=None,
                succeeded=None, already_at_target=True, active_config=active_config,
                proof_skipped=[])

    # (E) DRIVE the native scale — single Tools slot, Close() in finally (L22).
    with _scale_session(system) as tool:
        tool.ScaleByFactor = True
        tool.ScaleByUnits = False               # NEVER by-units (the inert no-op trap)
        tool.ScaleFactor = float(factor)
        ran = bool(tool.RunAndWaitForCompletion())
        succeeded = bool(getattr(tool, "Succeeded", False))  # raw disclosure, NEVER a gate
        err_msg = _safe_error_message(tool)

    # (F) read-back AFTER Close — the SOLE authority (C2).
    after = _read_triple(system)
    if not ran:
        # The run returned False: the scale may be partial/rejected. Disclose whether
        # anything moved before the caller retries (C2 — the engine WAS touched).
        return error_envelope(
            "scale_lens", "scale_write",
            "the native scale ran but returned no completion (ran=False); the scale "
            f"may be partial or rejected (ErrorMessage={err_msg!r}).",
            succeeded=succeeded, error_message=err_msg,
            efl_before=safe_float(before["efl"]), efl_after=safe_float(after["efl"]),
            scaled_config_only=active_config,
        )
    return _prove_and_envelope(mode, factor, target_efl, before, after, succeeded,
                               active_config)


SCALE_LENS_SPEC = ToolSpec(
    name="scale_lens",
    handler=scale_lens,
    required_params=("mode", "value"),
    param_types={"mode": "string", "value": "number"},
    description=(
        "Scale a lens geometrically. mode='to_efl' scales so the effective focal length "
        "hits value (mm, >0) — factor=value/current_EFL; mode='factor' scales every length "
        "by value (>0). The native scale scales the aperture too, so f/# is PRESERVED. "
        "Proves the scale by read-back (EFL AND total-track scale by the factor AND f/# "
        "unchanged). Gotcha: a scale-to-EFL on an afocal/collimated system (EFL undefined/"
        "sentinel) is refused (scale_efl_undefined); a scale that ran clean but moved nothing "
        "is refused (scale_noop) — a clean call is NOT proof. already_at_target (EFL already "
        "within tol) is a benign ok:true no-op, not a failure. The scale is global geometry; "
        "to_efl targets the ACTIVE config's EFL (scaled_config_only discloses it). "
        "See get_first_order, set_aperture, load_design."
    ),
)

TOOL_SPEC = SCALE_LENS_SPEC
