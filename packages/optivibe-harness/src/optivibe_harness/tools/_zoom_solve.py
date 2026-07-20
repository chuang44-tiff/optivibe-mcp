"""tools/_zoom_solve.py — the zoom/focus/conjugate SOLVE substrate.

NOT dispatchable (no ``TOOL_SPECS``). The pure physics behind ``set_zoom``'s EFL
family (zoom / focus / conjugate): a per-config 1-D BRACKETING SECANT on a MONOTONE
function (THIC -> EFFL / back-distance / PMAG), bounded + circularity-defended. It
NEVER raises out of its boundary except the family-carrying ``_ZoomUnverified``
exception the dispatchable ``zoom_compose`` catches and rolls back on.

Spine = falsify-not-trust (L28): the solver's LAST eval is NOT proof. ``set_zoom``
RE-READS the grading operand AFTER the cell write (the §4 FALSIFY-not-trust read);
this module owns the secant + the per-config reads + the INDEPENDENT circularity
gate, never the cell write (that is delegated to the S1 ``set_config_value`` handler).

Why a plain bracketing secant (NOT ``fold_beam``'s V-shaped outward march): the
probe (§2) proved THIC -> EFFL is MONOTONE, so a plain bracketing secant is correct;
the march is ``fold_beam``'s machinery for a V-shaped two-root deviation and is
gold-plating here (§11). We KEEP ``fold_beam``'s min-slope + finite-step + cap +
bidirectional-SIGNED-backstop guards (the sign-blind backstop pin).

The reads use the ``_measurement_common.read_operand_slots`` NAMED-slot firewall
(NOT a positional 9-arg guess) so a value can never land in the wrong arg (the silent
0.0 trap).
"""
import math

from ._measurement_common import read_operand_slots

# The bounded-secant iteration cap. A monotone EFL converges in <=8; the cap is
# the fail-closed floor, not the expected count.
_ZOOM_SOLVE_MAX_ITERS = 24

# The minimum |slope| for a safe secant step (the fold_beam ``_FOLD_SOLVE_MIN_SLOPE``
# precedent). A near-zero d(grading)/d(THIC) -> refuse-and-rollback rather than
# divide-by-near-zero (a flat region of the monotone map).
_ZOOM_SOLVE_MIN_SLOPE = 1e-9

# The initial bracket half-step (lens units of THIC) used to seed the secant's second
# eval off the current cell value. Small enough to stay local, large enough to give a
# finite slope estimate on the monotone map.
_ZOOM_SOLVE_SEED_STEP = 1.0

# Per-mode default convergence tolerance (the requested ``tolerance`` overrides). EFL /
# back-distance in lens units (mm); PMAG dimensionless.
_DEFAULT_TOL = {
    "zoom": 1e-3,        # EFL mm
    "focus": 1e-4,       # back-distance mm / REAY mm
    "conjugate": 1e-4,   # PMAG dimensionless
}

# The minimum spread the cross-config PMAG/PIMH-DIFFERS independent gate needs to read
# a real "the configs did not collapse" signal. Below this the per-config values are
# indistinguishable (a silent no-op config switch leaves them identical).
_DIFFERS_MIN_SPREAD = 1e-9

# The physical-minimum floor for a solved INTERNAL AIRGAP THIC. An airgap cannot be
# negative (a negative gap = overlapping elements, an unmanufacturable system), and an
# object distance (conjugate mode, surface 0) is also >= 0 — so a single >= 0 floor is
# correct for every mode the secant solves (it only ever drives an AIRGAP THIC, never a
# glass thickness). The §4.1 secant CLAMPS every candidate THIC to [_GAP_FLOOR, +inf);
# if the monotone target can only be reached BELOW the floor, the solve FAILS (the live
# bug: the two-positive-group fixture hit EFL 5 by driving the zoom gap to THIC -70).
_GAP_FLOOR = 0.0


class _ZoomUnverified(Exception):
    """The per-config secant / golden-section / circularity gate rejected the solve.

    Carries an ``error_family`` (``zoom_solve_unverified`` by default; the array
    substrate raises its own families) + an ``extra`` dict so the rollback envelope
    discloses the measured-vs-requested numbers honestly (the ``_FoldUnverified``
    precedent — a family override routes the SAME rollback path).
    """

    def __init__(self, message, *, family="zoom_solve_unverified", extra=None):
        super().__init__(message)
        self.error_family = family
        self.extra = extra or {}


class _ZoomFloored(Exception):
    """The target is sub-floor; the gap is authored at the physical floor (the nearest
    achievable). Carries the floored gap + the achievable grading + the shortfall so the
    caller COMMITS the floor and discloses, instead of rolling back (#10, converge-to-nearest).

    A NON-fatal "converged at the floor" signal — DISTINCT from ``_ZoomUnverified`` so the
    checkpoint NEVER sees it (it is caught in ``_compose_efl_family``'s per-config loop, NOT in
    ``_zoom_checkpointed`` whose generic ``except Exception`` would swallow it into a ROLLBACK —
    the opposite of intended, SC-7). The gap was ALREADY written to ``gap_floor`` by ``_eval_at``
    before the raise (SC-9), so committing it needs no re-write.
    """

    def __init__(self, *, gap_floor, g_floor, target, candidate, gap_surface):
        super().__init__(
            f"target {target} sub-floor on surface {gap_surface}; nearest at gap_floor "
            f"{gap_floor} (grading {g_floor})")
        self.gap_floor = float(gap_floor)
        self.g_floor = float(g_floor)
        self.target = float(target)
        self.candidate = float(candidate)         # the would-be NEGATIVE THIC (the dogfood -3.5)
        self.gap_surface = gap_surface
        self.shortfall = abs(float(g_floor) - float(target))


# --------------------------------------------------------------------------- #
# Surface resolution (mode -> the per-config THIC gap surface).
# --------------------------------------------------------------------------- #
def resolve_zoom_surface(system, mode, surface):
    """Resolve the per-config THIC gap surface for ``mode`` (the §2 table).

    - ``zoom``: ``surface`` is REQUIRED (the caller validated it is in range); no
      default — which internal airgap zooms is a DESIGN choice (§2).
    - ``focus``: default the back airgap (``resolve_back_airgap``); ``surface``
      overrides.
    - ``conjugate``: default the OBJECT gap (surface 0); ``surface`` overrides.

    Returns the integer surface number, or ``None`` if it could not be resolved
    (the caller refuses with ``zoom_param``). NEVER raises.
    """
    if surface is not None:
        return surface
    if mode == "conjugate":
        return 0
    if mode == "focus":
        from ._measurement_common import resolve_back_airgap
        return resolve_back_airgap(system)
    # zoom has NO safe default (the caller already validated surface is present).
    return None


# --------------------------------------------------------------------------- #
# Per-config grading reads (the NAMED-slot firewall — §4 / §12.1).
# --------------------------------------------------------------------------- #
def read_efl_for_config(system, wave=1):
    """Read ``EFFL`` for the ACTIVE config via the named-slot firewall. Raises on degrade.

    The caller switches the active config FIRST (``set_current_configuration``) so the
    read reflects THAT config. ``EFFL`` reads through the ``{Wave}`` slot map (a value
    can never land in the wrong arg, the silent 0.0 trap). A suspicious / non-finite
    read raises ``_ZoomUnverified`` (fail-closed — never solve toward a sentinel).
    """
    return _read_scalar(system, "EFFL", {3: ("Wave", int(wave))})


def read_wfno_for_config(system, wave=1):
    """Read ``WFNO`` (working f/number) for the ACTIVE config. Raises on degrade.

    The stop-zoom FALSIFIER quantity (the INDEPENDENT first-order f/#): after a
    ``hold_fnum`` zoom the WFNO must equal the target for EVERY config (the EPD floats per
    config under ImageSpaceFNum). ``WFNO`` reads through the SAME ``{Wave}`` slot map as
    ``EFFL`` (the first-order family, analysis_measure). A suspicious / non-finite read
    raises ``_ZoomUnverified`` (fail-closed — never claim a held f/# off a sentinel).
    """
    return _read_scalar(system, "WFNO", {3: ("Wave", int(wave))})


def read_pmag_for_config(system, wave=1):
    """Read ``PMAG`` (paraxial magnification) for the ACTIVE config. Raises on degrade.

    The conjugate-mode grading operand AND the independent DIFFERS gate's quantity.
    """
    return _read_scalar(system, "PMAG", {3: ("Wave", int(wave))})


def read_pimh_for_config(system, wave=1):
    """Read ``PIMH`` (paraxial image height) for the ACTIVE config. Raises on degrade.

    The fallback independent DIFFERS gate quantity when PMAG is degenerate (an
    infinite-conjugate system reads PMAG 0 every config).
    """
    return _read_scalar(system, "PIMH", {3: ("Wave", int(wave))})


def read_marginal_for_config(system, wave=1):
    """Read ``|REAY|`` (the marginal-ray height at the image) for the ACTIVE config.

    The focus-MINIMIZE metric (defocus proxy): the marginal ray height at the image
    plane is minimized at best focus. Reads the FULL-pupil marginal (Py=1). Raises on
    a degraded read.
    """
    raw = _read_scalar(
        system, "REAY",
        {3: ("Wave", int(wave)), 5: ("Hy", 0.0), 7: ("Py", 1.0)},
    )
    return abs(raw)


def _read_scalar(system, code, slots_by_name):
    """Read one operand scalar via the named-slot firewall; raise on a degraded read."""
    try:
        raw, suspicious = read_operand_slots(system, code, slots_by_name)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> fail closed (rollback)
        raise _ZoomUnverified(
            f"could not read the grading operand {code!r} ({exc!r}); the solve target "
            "is unverifiable — rolling back rather than solving toward an unread value"
        ) from exc
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or suspicious \
            or not math.isfinite(float(raw)):
        raise _ZoomUnverified(
            f"the grading operand {code!r} read a degraded value {raw!r} "
            "(non-finite or sentinel-magnitude); the solve target is unverifiable — "
            "rolling back rather than solving toward a sentinel",
            extra={"operand": code, "reading": _safe(raw)},
        )
    return float(raw)


# --------------------------------------------------------------------------- #
# The bounded bracketing secant (§4.1).
# --------------------------------------------------------------------------- #
def solve_config_thic_to_target(system, gap_surface, write_value_fn, read_grading_fn,
                                target, *, tol, max_iters=_ZOOM_SOLVE_MAX_ITERS,
                                seed_step=_ZOOM_SOLVE_SEED_STEP, gap_floor=_GAP_FLOOR):
    """Drive the per-config THIC cell so ``read_grading_fn() == target`` (monotone secant).

    ``write_value_fn(thic)`` writes the per-config THIC cell (delegated to the S1
    ``set_config_value`` handler by the caller — read-back-proven there) and returns
    the read-back THIC value (or raises a family-carrying exception the caller rolls back
    on). ``read_grading_fn()`` reads the grading operand for the ACTIVE config (EFFL /
    back-distance / PMAG) AFTER the write (the FALSIFY-not-trust read).

    Two seed evals (the current THIC and current +/- a step) bracket the monotone target;
    a secant step closes in. Bounded by ``max_iters`` + a ``_ZOOM_SOLVE_MIN_SLOPE`` floor
    + a finite-step guard + a bidirectional SIGNED bisection backstop (the
    sign-blind pin — a magnitude-only backstop green-passes a wrong-direction overshoot).

    ``gap_floor`` (default ``0.0``) is the PHYSICAL minimum for the solved INTERNAL AIRGAP
    THIC — a negative airgap is an overlapping-element, unmanufacturable system. Every
    candidate THIC is CLAMPED to ``[gap_floor, +inf)``; if the monotone target can only be
    reached BELOW the floor (the floor itself does not bracket the target on the reachable
    side), the solve RAISES ``_ZoomUnverified(zoom_solve_unverified)`` rather than authoring
    a negative gap (the live two-positive-group EFL-5 -> THIC -70 bug).

    Returns ``(solved_thic, achieved_grading, iterations)``. NON-convergence within the
    cap -> ``_ZoomUnverified(zoom_solve_unverified)`` (the caller rolls back). NEVER ships
    a config whose grading value did not reach target.
    """
    t0 = _current_thic(system, gap_surface)
    # Eval at the current THIC.
    g0 = _eval_at(write_value_fn, read_grading_fn, t0)
    if abs(g0 - target) <= tol:
        return t0, g0, 0

    # Seed the second bracket point off a +/- step (pick the side that does not
    # immediately fail; default +step). A finite step keeps the secant local.
    t1 = t0 + seed_step
    if not math.isfinite(t1):
        raise _ZoomUnverified(
            f"the zoom solve produced a non-finite seed THIC ({t1}) on surface "
            f"{gap_surface}; refusing rather than authoring a degenerate gap",
            extra={"requested": _safe(target)},
        )
    g1 = _eval_at(write_value_fn, read_grading_fn, t1)

    prev_t, prev_g = t0, g0
    cur_t, cur_g = t1, g1
    iterations = 1

    # PHASE 0: establish a BRACKET (the §4.1 "bracket the monotone target" precondition).
    # When the two seed points sit on a FLAT stretch of the monotone map far from the root
    # (a hyperbolic PMAG-vs-object-distance is flat at large object distance), a bare
    # secant overshoots wildly. So we first MARCH in the residual-reducing direction with
    # GROWING steps until the residual sign FLIPS (a true bracket straddling the root) —
    # then the secant below converges inside it. The march direction is the local slope
    # sense; the cap keeps it bounded (fail-closed on a non-bracketing map).
    bracketed = (cur_g - target) * (prev_g - target) < 0.0
    march_step = max(abs(seed_step), 1e-6)
    march_iters = 0
    while not bracketed and abs(cur_g - target) > tol:
        if iterations >= max_iters:
            raise _ZoomUnverified(
                f"the zoom solve did not bracket the target within {max_iters} "
                f"iterations on surface {gap_surface}: last grading {cur_g} vs target "
                f"{target}; the gap could not reach the target — rolling back",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        slope = (cur_g - prev_g) / (cur_t - prev_t) if cur_t != prev_t else 0.0
        if abs(slope) < _ZOOM_SOLVE_MIN_SLOPE:
            raise _ZoomUnverified(
                f"the zoom solve hit a near-zero slope ({slope}) while bracketing on "
                f"surface {gap_surface}; the grading is flat in this gap — rolling back",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        # March in the direction that REDUCES the residual (toward the root).
        residual = target - cur_g
        direction = math.copysign(1.0, residual / slope)
        new_t = cur_t + direction * march_step
        if not math.isfinite(new_t):
            raise _ZoomUnverified(
                f"the zoom bracket march produced a non-finite THIC ({new_t}) on surface "
                f"{gap_surface}; refusing rather than authoring a degenerate gap",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        # Floor the candidate at the physical minimum (no negative airgap). The floor is a
        # BRACKET-END (C-1): if the floored gap's grading and the current reachable-side
        # grading (cur_g) BRACKET the target, the root is REACHABLE in [floor, cur_t] and the
        # clamp returns gap_floor so the march/secant continues on that bracket; only a
        # genuinely sub-floor target (the floor on the SAME side as cur_g) raises _ZoomFloored.
        new_t = _clamp_floor_or_refuse(new_t, gap_floor, write_value_fn, read_grading_fn,
                                       target, tol, gap_surface, iterations, cur_g)
        new_g = _eval_at(write_value_fn, read_grading_fn, new_t)
        prev_t, prev_g = cur_t, cur_g
        cur_t, cur_g = new_t, new_g
        iterations += 1
        march_iters += 1
        march_step = min(march_step * 2.0, 1e6)
        bracketed = (cur_g - target) * (prev_g - target) <= 0.0
    while abs(cur_g - target) > tol:
        if iterations >= max_iters:
            raise _ZoomUnverified(
                f"the zoom secant did not converge within {max_iters} iterations on "
                f"surface {gap_surface}: last grading {cur_g} vs target {target} "
                f"(residual {abs(cur_g - target)} > {tol}); the gap could not reach the "
                "target — rolling back rather than shipping an under-solved config",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        denom = cur_g - prev_g
        slope = denom / (cur_t - prev_t) if cur_t != prev_t else 0.0
        if abs(slope) < _ZOOM_SOLVE_MIN_SLOPE:
            raise _ZoomUnverified(
                f"the zoom secant hit a near-zero slope ({slope}) on surface "
                f"{gap_surface}; the grading is flat in this gap (the secant step is "
                "singular) — rolling back rather than diverging",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        new_t = cur_t + (target - cur_g) / slope
        if not math.isfinite(new_t):
            raise _ZoomUnverified(
                f"the zoom secant produced a non-finite THIC ({new_t}) on surface "
                f"{gap_surface}; refusing rather than authoring a degenerate gap",
                extra={"requested": _safe(target), "achieved": _safe(cur_g),
                       "solve_iterations": iterations},
            )
        # Floor the secant candidate at the physical minimum (no negative airgap). C-1: the
        # floor is a BRACKET-END — when the floored grading and cur_g bracket the target the
        # reachable root is kept (the clamp returns gap_floor, the secant continues on
        # [floor, cur_t]); only a genuinely sub-floor target floors (_ZoomFloored).
        new_t = _clamp_floor_or_refuse(new_t, gap_floor, write_value_fn, read_grading_fn,
                                       target, tol, gap_surface, iterations, cur_g)
        new_g = _eval_at(write_value_fn, read_grading_fn, new_t)

        # The bidirectional SIGNED backstop (the fold_beam pin): if the secant
        # OVERSHOT (the residual grew in magnitude), take a SIGNED bisection step toward
        # the bracket midpoint instead of trusting the diverging secant — the direction
        # is derived from the BRACKET, never a magnitude-only step (a sign-blind
        # backstop green-passes a wrong-direction overshoot). The midpoint is already
        # >= the floor whenever both bracket ends are (the floor is enforced above).
        if abs(new_g - target) > abs(cur_g - target):
            mid_t = 0.5 * (cur_t + prev_t)
            if math.isfinite(mid_t) and mid_t >= gap_floor:
                new_t = mid_t
                new_g = _eval_at(write_value_fn, read_grading_fn, new_t)

        prev_t, prev_g = cur_t, cur_g
        cur_t, cur_g = new_t, new_g
        iterations += 1

    return cur_t, cur_g, iterations


def _clamp_floor_or_refuse(candidate, gap_floor, write_value_fn, read_grading_fn,
                           target, tol, gap_surface, iterations, cur_g=None):
    """Clamp a candidate THIC to the physical floor (a BRACKET-END), or signal NEAREST.

    A candidate ``>= gap_floor`` passes through unchanged. A candidate the secant/march
    wants to drive BELOW the floor (a negative airgap) is clamped to the floor and the
    grading is read THERE. The floor is treated as a **bracket-end** (C-1, the fix for the
    converge-nearest overshoot): the geometrically-DOUBLING march can step PAST a small
    positive root that is REACHABLE above the floor (e.g. root at THIC 0.5, the march steps
    4 -> -4) — flooring then would commit a wrong floored geometry as "nearest" for a target
    that is actually reachable. So BEFORE declaring the target sub-floor we PROVE it is
    unreachable on ``[gap_floor, +inf)``:

    - When the floored grading ``g_floor`` and the current reachable-side grading ``cur_g``
      BRACKET the target (lie on OPPOSITE sides of it), the root IS reachable in
      ``[gap_floor, cur_t]`` -> return ``gap_floor`` as the clamped bracket-end so the
      march/secant CONTINUES and converges to the reachable root (NOT floored).
    - When ``g_floor`` reaches the target within ``tol`` it is the root itself -> return the
      floor (reachable at the physical minimum).
    - ONLY when ``g_floor`` is on the SAME side of the target as ``cur_g`` (the monotone
      target genuinely requires a gap BELOW the floor) is the gap LEFT authored at the floor
      (``_eval_at`` already wrote it, SC-9) and a ``_ZoomFloored`` raised so the
      ``_compose_efl_family`` per-config loop COMMITS the NEAREST physical gap + discloses the
      shortfall, instead of refusing the whole zoom with an unphysical value (#10).

    ``cur_g`` is the grading at the point the secant/march is stepping FROM (the reachable
    side); ``None`` (no reachable side known) is treated conservatively — the floor only
    floors when it does not itself reach the target. ``candidate`` is the UNCLAMPED (would-be
    negative) THIC the disclosure carries.
    """
    if not math.isfinite(candidate) or candidate >= gap_floor:
        return candidate
    g_floor = _eval_at(write_value_fn, read_grading_fn, gap_floor)   # ALREADY authors gap_floor
    if abs(g_floor - target) <= tol:
        return gap_floor                                            # reachable AT the floor
    # C-1: the floor is a BRACKET-END. If g_floor and the reachable-side grading cur_g lie on
    # OPPOSITE sides of the target, a reachable root sits in [gap_floor, cur_t] -> return the
    # floor so the march/secant continues on that bracket (do NOT floor a reachable root).
    if cur_g is not None and math.isfinite(cur_g):
        if (g_floor - target) * (cur_g - target) < 0.0:
            return gap_floor                                       # reachable in [floor, cur_t]
    # The target is genuinely sub-floor (g_floor is on the SAME side as the reachable end, or
    # no reachable side is known and the floor does not reach the target): the NEAREST physical
    # gap (gap_floor) is ALREADY written (by _eval_at above); SIGNAL the nearest up the chain
    # (converge-to-nearest, NOT refuse). _ZoomFloored is DISTINCT from _ZoomUnverified -> the
    # checkpoint never rolls it back.
    raise _ZoomFloored(
        gap_floor=gap_floor, g_floor=g_floor, target=target,
        candidate=candidate, gap_surface=gap_surface,
    )


def _current_thic(system, gap_surface):
    """Read the current THIC (the LDE thickness) of ``gap_surface``. Raises on degrade."""
    try:
        return float(system.LDE.GetSurfaceAt(gap_surface).Thickness)
    except Exception as exc:  # noqa: BLE001 — an unreadable THIC -> fail closed
        raise _ZoomUnverified(
            f"could not read the current thickness of surface {gap_surface} ({exc!r}); "
            "the zoom gap is unverifiable — rolling back"
        ) from exc


def read_active_gap_thic(system, gap_surface):
    """Read the ACTIVE config's solved gap THIC from the LDE (the DISCRIMINATING signal).

    The §13.2 tightening: PMAG/PIMH read 0 on a normal infinite-conjugate on-axis system
    (degenerate), so they are NOT a reliable cross-config collapse/distinctness signal.
    The per-config ACTIVE-gap THIC IS — after ``set_current_configuration(k)`` the LDE
    thickness of the solved gap reflects config k's written value; a ``set_current_config``
    that no-ops leaves ALL configs reading the SAME active-gap geometry. This is INDEPENDENT
    of the EFFL/PMAG operand (it is LDE geometry, not the merit operand the solve drove) and
    is discriminating whenever the solve produced distinct per-config values.

    Returns the float active THIC, or ``float('nan')`` on a degraded read (the DIFFERS gate
    ignores NaNs — a degraded geometry read never fabricates a distinctness claim).
    """
    try:
        return float(system.LDE.GetSurfaceAt(gap_surface).Thickness)
    except Exception:  # noqa: BLE001 — unreadable active geometry -> NaN (ignored by gate)
        return float("nan")


def _eval_at(write_value_fn, read_grading_fn, thic):
    """Write ``thic`` to the per-config cell THEN RE-READ the grading (FALSIFY-not-trust).

    ``write_value_fn`` delegates to the S1 ``set_config_value`` handler (read-back-proven
    THERE) and must return the read-back THIC or raise a family-carrying exception the
    caller rolls back on. ``read_grading_fn`` reads the grading operand for the ACTIVE
    config AFTER the write — the solver's own last eval is never the proof.
    """
    write_value_fn(thic)
    return read_grading_fn()


# --------------------------------------------------------------------------- #
# Golden-section MINIMIZE (the focus-minimize path, §4.3) — REUSE the proven shape.
# --------------------------------------------------------------------------- #
def golden_section_min_defocus(system, write_value_fn, read_metric_fn, lo, hi, *,
                               iterations=24, gap_floor=_GAP_FLOOR):
    """Golden-section MINIMIZE ``read_metric_fn()`` (|REAY|) over [lo, hi] of the THIC cell.

    The ONLY place a golden-section appears (§4.3). Mirrors
    ``_measurement_common._golden_section_min``: a non-finite / degraded candidate
    mid-scan is REJECTED (the candidate is skipped, best-so-far kept). Returns
    ``(best_thic, best_value)`` or ``(None, None)`` if no finite candidate was found.
    ``write_value_fn`` delegates the per-config write (read-back-proven);
    ``read_metric_fn`` reads the defocus metric AFTER the write.

    ``gap_floor`` (default ``0.0``) is the PHYSICAL minimum for the solved back-gap THIC
    (the secant's ``_GAP_FLOOR`` discipline extended to the minimize path): the search
    window's lower bound is RAISED to the floor, AND every candidate is clamped to
    ``>= gap_floor`` before it is written/evaluated — so the committed best-focus plane can
    NEVER be a NEGATIVE back-gap (an unphysical overlapping-element system). An airgap at
    exactly 0 is physical (the elements touch); a negative one is not. If the unconstrained
    defocus minimum sits below the floor the minimize commits the FLOOR (the best reachable
    physical plane) and the caller discloses it.
    """
    invphi = (math.sqrt(5.0) - 1.0) / 2.0
    invphi2 = (3.0 - math.sqrt(5.0)) / 2.0

    # Floor the search window so no candidate below the physical minimum is ever
    # written/evaluated. If the window collapses (hi <= floor) the floor itself is the only
    # physical candidate -> evaluate it as the best plane (never park at a negative gap).
    if math.isfinite(gap_floor):
        lo = max(lo, gap_floor)
        if hi < lo:
            hi = lo

    best_t = None
    best_v = None

    def _consider(t):
        nonlocal best_t, best_v
        if not math.isfinite(t):
            return None
        # Clamp the candidate at the physical floor — a golden-section probe must never
        # write a negative back-gap (the EFL-5 -> THIC-70 unphysical class on the secant path).
        if math.isfinite(gap_floor) and t < gap_floor:
            t = gap_floor
        try:
            write_value_fn(t)
            v = read_metric_fn()
        except _ZoomUnverified:
            return None
        if v is None or not math.isfinite(v):
            return None
        if best_v is None or v < best_v:
            best_t, best_v = t, v
        return v

    a, b = lo, hi
    c = a + invphi2 * (b - a)
    d = a + invphi * (b - a)
    fc = _consider(c)
    fd = _consider(d)
    for _ in range(iterations):
        if fc is None and fd is None:
            break
        if fd is None or (fc is not None and fc < fd):
            b, d, fd = d, c, fc
            c = a + invphi2 * (b - a)
            fc = _consider(c)
        else:
            a, c, fc = c, d, fd
            d = a + invphi * (b - a)
            fd = _consider(d)
    return best_t, best_v


# --------------------------------------------------------------------------- #
# The circularity defense (§4.2) — the INDEPENDENT cross-config gate.
# --------------------------------------------------------------------------- #
def independent_order_ok(ordering_targets, achieved, independent_values, *,
                         independent_label="independent", request_targets=None):
    """The INDEPENDENT cross-config gate (§4.2, the fold_beam circularity guard).

    The solve DROVE the grading operand (EFFL); this gate checks quantities the solve
    NEVER drove, on the per-config switch the solve relied on — so a systematically-wrong
    grading read OR a no-op config switch CANNOT pass both the direct check and this gate.

    Returns ``(ok: bool, reason: str|None, status: str)`` where ``status`` is one of
    ``"ok"`` (discriminating + distinct), ``"degenerate"`` (the solve LEGITIMATELY produced
    identical per-config independent values — an all-same-target / same-best-focus case;
    NOT a collapse, so it WARNS not fails), or ``"collapsed"`` (the configs read the same
    independent value AND the requested per-config solve should have produced distinct ones
    — a silent ``set_current_configuration`` no-op).

    Two independent checks:

    1. ORDERING (``ordering_targets``, zoom only): when ``ordering_targets`` is strictly
       monotone, the ACHIEVED grading set MUST be strictly monotone the SAME way. This is
       the PRIMARY independent non-collapse proof for ordered targets — a no-op switch
       returns ONE repeated optical state, which cannot read N strictly-ordered distinct
       gradings. ``ordering_targets=None`` (focus/conjugate, unordered) SKIPS this check.
    2. The DISCRIMINATING corroborator (``independent_values`` — an operand/geometry the
       solve did NOT drive, chosen DISCRIMINATING per mode by the caller: the per-config
       ACTIVE-gap THIC for zoom/focus (LDE geometry, independent of EFFL/PMAG, distinct
       whenever the solve produced distinct per-config values), or PMAG for conjugate
       (genuinely differs on a finite object)). The values must NOT all collapse to one —
       UNLESS the REQUESTED per-config targets (``request_targets``) were THEMSELVES
       identical (then identical corroborator values are LEGITIMATE: ``status="degenerate"``,
       WARN not fail).

    ``request_targets`` is the REQUESTED per-config target list (zoom EFL / conjugate PMAG /
    focus back-distance; ``None`` for focus-minimize) used ONLY for the degeneracy test —
    distinct from ``ordering_targets`` (zoom-only) so a conjugate/focus request whose targets
    happen to be monotone does NOT trip the zoom ordering check.

    A single-config edge (len < 2) trivially passes (nothing to differ).
    """
    # (1) ordering — only when ordering_targets are supplied AND strictly monotone (zoom).
    if ordering_targets is not None and len(ordering_targets) >= 2:
        direction = _strict_monotone_direction(ordering_targets)
        if direction is not None:
            ach_dir = _strict_monotone_direction(achieved)
            if ach_dir != direction:
                return False, (
                    f"the achieved set {[_safe(a) for a in achieved]} is not strictly "
                    f"ordered the same way as the strictly-ordered targets "
                    f"{[_safe(t) for t in ordering_targets]} (achieved direction "
                    f"{ach_dir!r} vs requested {direction!r}); a systematically-wrong "
                    "grading read or a collapsed config — refusing"
                ), "collapsed"

    # (2) the DISCRIMINATING corroborator — the configs must not all read the SAME
    # independent value UNLESS the REQUESTED per-config targets were themselves identical
    # (a degenerate all-same-target case is NOT a collapse — identical corroborator values
    # are then LEGITIMATE; WARN, do not fail).
    vals = [v for v in (independent_values or []) if isinstance(v, (int, float))
            and not isinstance(v, bool) and math.isfinite(v)]
    if len(vals) >= 2:
        spread = max(vals) - min(vals)
        if spread < _DIFFERS_MIN_SPREAD:
            if _targets_are_degenerate(request_targets):
                return True, (
                    f"every configuration reads the SAME {independent_label} value "
                    f"(spread {spread}); this is the DEGENERATE case — the requested "
                    "per-config targets are identical, so identical per-config geometry is "
                    "correct, not a collapse. WARN: a per-config zoom/focus/conjugate over "
                    "identical targets produces identical configurations"
                ), "degenerate"
            return False, (
                f"every configuration reads the SAME {independent_label} value (spread "
                f"{spread} < {_DIFFERS_MIN_SPREAD}); the configurations COLLAPSED — a "
                "set_current_configuration that did not re-evaluate the optics (a silent "
                "no-op) — refusing rather than claiming a per-config solve"
            ), "collapsed"
    return True, None, "ok"


def _targets_are_degenerate(targets):
    """True iff the requested per-config targets are all (near-)identical (the legit-same case).

    ``targets=None`` (focus-minimize) is treated as POTENTIALLY degenerate — a multi-config
    system can legitimately converge every config to the SAME best-focus plane (so identical
    per-config back-gap THIC is correct, not a collapse). A supplied ``targets`` list is
    degenerate only when its spread is below the differs floor.
    """
    if targets is None:
        return True
    vals = [t for t in targets if isinstance(t, (int, float)) and not isinstance(t, bool)
            and math.isfinite(t)]
    if len(vals) < 2:
        return True
    return (max(vals) - min(vals)) < _DIFFERS_MIN_SPREAD


def _strict_monotone_direction(seq):
    """+1 if strictly increasing, -1 if strictly decreasing, else None (not monotone)."""
    vals = list(seq)
    if len(vals) < 2:
        return None
    inc = all(vals[i + 1] > vals[i] for i in range(len(vals) - 1))
    dec = all(vals[i + 1] < vals[i] for i in range(len(vals) - 1))
    if inc:
        return 1
    if dec:
        return -1
    return None


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel)."""
    from .._io import safe_float
    return safe_float(value)


__all__ = [
    "_ZoomUnverified",
    "_ZoomFloored",
    "resolve_zoom_surface",
    "read_efl_for_config",
    "read_pmag_for_config",
    "read_pimh_for_config",
    "read_marginal_for_config",
    "read_active_gap_thic",
    "solve_config_thic_to_target",
    "golden_section_min_defocus",
    "independent_order_ok",
    "_DEFAULT_TOL",
    "_ZOOM_SOLVE_MAX_ITERS",
]
