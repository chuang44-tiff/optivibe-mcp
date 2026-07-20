"""tools/_collimation.py — the collimation residual math + the SHARED detector.

NOT dispatchable (no ``TOOL_SPEC``). The probe-grounded correctness substrate that
BOTH the new ``verify_collimation`` tool AND the analyzer guard
(``analyze_strehl``/``analyze_wavefront``/``get_first_order``) consume so the
detector lives in EXACTLY one place (the ``_beam_reach`` /
``_layout_geometry.system_is_folded`` shared-predicate precedent; prevents L26
sibling drift between the tool and the guard):

- ``compute_collimation_residual(system, ...)`` — the per-field pupil-grid output
  residual (§1.2 steps 1-8): build an N×N pupil grid, trace it IN-PROCESS via
  ``trace_rays`` (zero new ray-trace code), exclude errored/vignetted/non-finite rays
  FAIL-CLOSED, reference each field's pupil-MEAN direction (NOT the chief), and report
  ``rms_angular_residual_mrad`` + ``chief_pointing_mrad`` + ``max_edge_slope_mrad``.
  A ``RANG ⟂ atan2(√(L²+M²),N)`` cross-check on the SAMPLED chief+edge per field is
  the L24 marshalling-bug defense — a disagreement DOWNGRADES the verdict to
  ``collimation_indeterminate`` (a transposed L,M,N / a wrong-slotted RANG can NEVER
  silently corrupt the number).

- ``detect_collimated_output(system, ...)`` — the SHARED collimated/afocal-output
  detector. The REDESIGN (live across 5 regimes). The OLD ``d = -Y/M`` at the
  USER IMAGE PLANE heuristic (+ slope_floor + 1e4 cap) is DELETED — both audits broke it
  (it false-positived a focused/past-focus/slow imager → silently nulled a valid Strehl,
  and false-negatived a near-afocal output). The new detector is a TWO-STAGE rule read at
  the LAST OPTICAL SURFACE (``NumberOfSurfaces-2``), which is past-focus-INVARIANT:

  * Read the near-axis marginal (Py=±0.1, Hy=0) at the last optical surface; the
    reconstructed image conjugate is ``s' = -Y / u'`` (``u' = M/N``), invariant to where
    the user placed the image plane. Both marginals are read (mirror symmetry verified;
    a wild disagreement → fail-closed to imaging).
  * STAGE 1: ``|s'/EFFL| < S_RATIO_THRESHOLD (20)`` → a focused/past-focus/slow/normal
    imager → ``collimated:False`` (NEVER null its Strehl). The empty gap from the probe is
    ``[1.92, 98]``; 20 sits centrally.
  * STAGE 2 (only if stage 1 said "focuses far/at infinity"): ``placement_ratio =
    back_airgap / |s'|``. If ``0.5 <= ratio <= 2.0`` the user placed the image plane AT the
    distant focus → a real long-conjugate imager → ``collimated:False`` (the
    long-BFL fix). Else (ratio ≪1, the plane is nowhere near the focus) → afocal/collimator
    output → ``collimated:True`` (image-plane Strehl is garbage → null).

  ``AFocalImageSpace`` is CORROBORATING (echoed, never sole). FAIL-CLOSED:
  any bad read / throw → ``collimated:False`` (imaging) — a detector fault must NEVER null
  a valid imager's Strehl. (This INVERTS the old fail-OPEN rationale wording but lands on
  the SAME safe ``collimated:False`` degrade — keep it.) This is the load-bearing safety
  property.

Live ZOS-API integration: exercised by the live test (the detector decider §2.3);
unit-tested against the probe-seeded fakes reproducing the captured cosines
(residual 106.56, chief 103.34 marginal, edge 158.71) + the RANG⟂cosine cross-check.
"""
import math

from .._io import safe_float
from . import _measurement_common as _mc
from .analysis_raytrace import trace_rays


# The detector traces a NEAR-AXIS (paraxial) marginal at Py=±0.1 (a good
# paraxial/SNR compromise — imagers are paraxially stable; the near-afocal s' is noisy
# but still ≫ the threshold, so it is a CLASSIFICATION signal, not a measurement).
_CONVERGENCE_PROBE_PY = 0.1
# STAGE-1 threshold (dimensionless): |s'/EFFL| >= this => the output focuses far / at
# infinity (collimated/near-afocal candidate); below it => a real imager (focused /
# past-focus / slow / normal) whose Strehl must NEVER be nulled. Measured across 5
# regimes (live): a CLEAN empty gap [1.92, 98] — imagers sit at s'/EFFL=1.92, the
# near-afocal case starts at 98 (over 50x separation). 20 sits centrally in the gap
# with ~5x margin on each side.
_S_RATIO_THRESHOLD = 20.0
# STAGE-2 placement-ratio window: back_airgap / |s'| in [0.5, 2.0] => the user placed
# the image plane AT the distant focus => a REAL long-conjugate imager (Strehl valid,
# the long-BFL fix). Outside it (ratio << 1) => the image plane is nowhere near the
# focus => afocal/collimator output (Strehl garbage -> null). Measured: collimator/
# near-afocal ratio 5e-10..0.01; in-focus imagers 1.00..1.35.
_PLACEMENT_RATIO_LO = 0.5
_PLACEMENT_RATIO_HI = 2.0
# A near-zero |u'| (parallel marginals) => s' -> +inf => stage 1 true (the exact
# collimator case, u' = -9.7e-12). Below this the marginal is treated as parallel.
_PARALLEL_SLOPE_EPS = 1.0e-9
# The two near-axis marginals are mirror-symmetric: |s'_upper| ≈ |s'_lower|. A wild
# disagreement (relative) signals an asymmetric/decentered output or a single-ray read
# fault -> fail-CLOSED to imaging (never trust the upper marginal alone).
_MARGINAL_SYMMETRY_REL_TOL = 0.5
# The RANG-vs-cosine cross-check absolute tolerance (rad) — the probe's RANG matched
# atan2(RAGA/B/C) EXACTLY; 1e-6 is a tight WITNESS bound (§1.2 step 8).
_CROSS_CHECK_ABS_TOL = 1.0e-6


# --------------------------------------------------------------------------- #
# Field reading (the _beam_reach._read_fields idiom, guarded).
# --------------------------------------------------------------------------- #
def _read_fields(system):
    """Read ``SystemData.Fields`` as a list of ``(index, y, hy)`` (meridional Hy).

    Now a one-line delegate to the canonical ``_measurement_common.read_field_hy``
    (L30 — this idiom's single home is the measurement layer; identical shape ->
    byte-identical behavior). NEVER raises. Returns a non-empty list.
    """
    return _mc.read_field_hy(system)


# --------------------------------------------------------------------------- #
# Pupil-grid build (§1.2 step 1).
# --------------------------------------------------------------------------- #
def _pupil_grid(density):
    """Build the UNIT-DISK pupil grid in ``(Px,Py) ∈ [-1,1]²`` with ``Px²+Py² <= 1``.

    Returns the grid points on the linear -1..+1 grid that lie INSIDE the unit pupil
    (§1.2 step 1: "drop Px²+Py² > 1"). The chief ``(0,0)`` is ALWAYS included
    (it is the bore-sight ray and is on the unit disk by construction). For
    ``density == 1`` returns a single chief (0,0) — the caller rejects a < 2 density
    BEFORE calling this (a degenerate grid is a param error, §1.5). Returns a list of
    ``(px, py)`` pairs.

    UNIT-DISK semantics: the residual is the well-defined unit-disk RMS,
    independent of whether the engine vignettes the r=√2 square corners. The old build
    traced the full N×N square (4 corners at r=√2, 8 edge-midpoints at r≈1.118 — nearly
    half the grid OUTSIDE the stated unit pupil), so the headline was a square-grid RMS
    biased by physically-absent over-aperture rays. Clipping to the unit disk makes
    ``rms_angular_residual_mrad`` the unit-DISK metric the field name promises.
    """
    if density < 2:
        return [(0.0, 0.0)]
    step = 2.0 / (density - 1)
    grid = []
    has_center = False
    for i in range(density):
        px = -1.0 + i * step
        for j in range(density):
            py = -1.0 + j * step
            if px == 0.0 and py == 0.0:
                has_center = True
            if px * px + py * py <= 1.0 + 1e-9:  # drop the over-unit-pupil corners
                grid.append((px, py))
    # ALWAYS include the (0,0) chief so chief_pointing_mrad is never silently
    # None on an EVEN density (which has no natural grid centre). The chief is the
    # bore-sight ray and a unit-disk point by construction.
    if not has_center:
        grid.insert(0, (0.0, 0.0))
    return grid


def _slope_mrad(L, M, N):
    """The ray's angle from the +Z axis in mrad: ``1000 · atan2(√(L²+M²), N)``."""
    return 1000.0 * math.atan2(math.sqrt(L * L + M * M), N)


def _is_finite_triple(L, M, N):
    return (
        isinstance(L, (int, float)) and not isinstance(L, bool)
        and isinstance(M, (int, float)) and not isinstance(M, bool)
        and isinstance(N, (int, float)) and not isinstance(N, bool)
        and math.isfinite(L) and math.isfinite(M) and math.isfinite(N)
    )


# --------------------------------------------------------------------------- #
# RANG cross-check (§1.2 step 8 — the L24 marshalling-bug defense).
# --------------------------------------------------------------------------- #
def _resolve_surface_index(system, to_surface):
    """Resolve ``to_surface`` (the trace's -1=image convention) to an ABSOLUTE surface
    number for the RANG operand's Surf slot.

    The LIVE divergence (L24): ``trace_rays(to_surface=-1)`` traces to the IMAGE
    surface, but ``RANG`` with ``Surf=-1`` reads a DIFFERENT surface — RANG's Surf
    slot needs the ABSOLUTE index. ``-1`` (or any negative) -> ``NumberOfSurfaces-1``
    (the image surface index). A positive ``to_surface`` is passed through. READ-
    GUARDED: an unreadable surface count -> ``to_surface`` verbatim (the cross-check
    then naturally finds no match -> a divergence is surfaced honestly, never silent).
    """
    if to_surface is not None and to_surface >= 0:
        return int(to_surface)
    try:
        n = int(system.LDE.NumberOfSurfaces)
        return n - 1
    except Exception:  # noqa: BLE001 — fall back to the raw value (the read stands)
        return int(to_surface) if to_surface is not None else -1


def _rang_slots(surf, wave, hx, hy, px, py):
    # RANG real-ray-angle 9-arg slot map (the standard real-ray layout):
    # 2:Surf 3:Wave 4:Hx 5:Hy 6:Px 7:Py. Surf is the ABSOLUTE surface index (the live
    # gate proved RANG with Surf=-1 reads the WRONG surface — it needs the resolved
    # image surface number, not the trace's -1 convention).
    return {
        2: ("Surf", int(surf)),
        3: ("Wave", int(wave)),
        4: ("Hx", float(hx)),
        5: ("Hy", float(hy)),
        6: ("Px", float(px)),
        7: ("Py", float(py)),
    }


def _cross_check_ray(system, surf, wave, hx, hy, px, py, L, M, N):
    """Witness ``RANG(rad) ≈ atan2(√(L²+M²),N)`` from the SAME ray's cosines.

    A ``≈`` WITNESS (NOT a re-derivation — the residual is computed from the cosines,
    §1.2 step 8). Returns ``(ok, detail)``: ``ok`` is True on agreement (within
    ``_CROSS_CHECK_ABS_TOL``) OR when the RANG read is unavailable (a missing operand
    is not a divergence — the cosines stand). A genuine DISAGREEMENT (RANG finite but
    != atan2) returns ``(False, "<msg>")``. NEVER raises.
    """
    expected = math.atan2(math.sqrt(L * L + M * M), N)
    try:
        raw, suspicious = _mc.read_operand_slots(
            system, "RANG", _rang_slots(surf, wave, hx, hy, px, py)
        )
    except Exception:  # noqa: BLE001 — RANG unresolvable -> the cosines stand (no divergence)
        return True, None
    if suspicious or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        # A suspicious/non-numeric RANG is not a usable witness — the cosines stand.
        return True, None
    rang_rad = float(raw)
    if math.isclose(rang_rad, expected, abs_tol=_CROSS_CHECK_ABS_TOL, rel_tol=0.0):
        return True, None
    return False, (
        f"RANG ({rang_rad:.6f} rad) disagrees with atan2(sqrt(L^2+M^2),N) "
        f"({expected:.6f} rad) at (Px={px},Py={py}) beyond {_CROSS_CHECK_ABS_TOL} "
        "(a transposed L,M,N / a wrong-slotted RANG)"
    )


# --------------------------------------------------------------------------- #
# compute_collimation_residual (§1.2 steps 1-8).
# --------------------------------------------------------------------------- #
def compute_collimation_residual(system, *, wave=1, pupil_density=5, to_surface=-1,
                                 tolerance_mrad=0.5, vignette_is_failure=True):
    """Per-field pupil-grid output-collimation residual (§1.2). Returns a dict.

    The pure per-config compute body (the ``_grade`` the tool's
    ``evaluate_over_configs`` driver calls). Builds an N×N pupil grid per field,
    traces it IN-PROCESS via ``trace_rays``, excludes errored/vignetted/non-finite
    rays FAIL-CLOSED, references the per-field pupil-MEAN direction (NOT the chief),
    and reports the RMS angular residual + chief pointing + max edge slope (mrad) +
    the RANG cross-check.

    Returns ``{ok:true, per_field, worst_field_residual_mrad, config_headline,
    cross_check_ok, verdict, flags, ...}`` on a successful trace, OR a propagated
    ``{ok:false, error_family:"collimation_empty", error:...}`` when ``trace_rays``
    could not yield rays. A field with < 2 surviving rays contributes NO pass (its
    residual is ``null`` + a flag). The caller is responsible for the never-raise
    envelope; a bad ``trace_rays`` ENVELOPE is propagated structured, never a throw.
    """
    fields = _read_fields(system)
    grid = _pupil_grid(int(pupil_density))
    # Resolve the trace's -1=image convention to the ABSOLUTE surface index the RANG
    # operand's Surf slot needs (the live L24 fix — RANG Surf=-1 reads the wrong surface).
    surf_index = _resolve_surface_index(system, to_surface)

    per_field = []
    flags = []
    cross_check_ok = True
    any_measurable = False
    any_over_tolerance = False
    worst_residual = None

    for field_idx, field_y, hy in fields:
        rays_payload = [
            {"wave": int(wave), "Hx": 0.0, "Hy": float(hy), "Px": px, "Py": py}
            for (px, py) in grid
        ]
        trace = trace_rays(
            system_session_shim(system),
            {"rays": rays_payload, "to_surface": to_surface, "rays_type": "Real"},
        )
        if not isinstance(trace, dict) or not trace.get("ok"):
            inner = (
                trace.get("error") if isinstance(trace, dict) else "non-dict trace"
            )
            return {
                "ok": False,
                "error_family": "collimation_empty",
                "error": (
                    f"trace_rays could not yield output rays for field {field_idx}: "
                    f"{inner}"
                ),
            }

        traced = trace.get("rays", [])
        # Pair each traced ray back with its (px, py) by index (trace preserves order;
        # a short_read truncates the tail — we only consume the returned prefix).
        survivors = []   # (px, py, L, M, N)
        n_excluded = 0
        for k, ray in enumerate(traced):
            px, py = grid[k] if k < len(grid) else (0.0, 0.0)
            errored = bool(ray.get("errored"))
            vignetted = bool(ray.get("vignetted"))
            L = ray.get("L")
            M = ray.get("M")
            N = ray.get("N")
            if errored or (vignette_is_failure and vignetted) \
                    or not _is_finite_triple(L, M, N):
                n_excluded += 1
                continue
            survivors.append((px, py, float(L), float(M), float(N)))

        if len(survivors) < 2:
            per_field.append({
                "field_index": field_idx,
                "field_y": safe_float(field_y),
                "Hy": safe_float(hy),
                "rays_traced": len(traced),
                "rays_used": len(survivors),
                "rays_excluded": n_excluded,
                "rms_angular_residual_mrad": None,
                "chief_pointing_mrad": None,
                "max_edge_slope_mrad": None,
                "mean_direction": None,
                "cross_check_ok": True,
                "collimated": False,
            })
            flags.append(f"field {field_idx}: no surviving ray to measure")
            continue

        any_measurable = True

        # Mean direction (the reference, §1.2 step 4).
        sL = sum(s[2] for s in survivors)
        sM = sum(s[3] for s in survivors)
        sN = sum(s[4] for s in survivors)
        mag = math.sqrt(sL * sL + sM * sM + sN * sN) or 1.0
        mL, mM, mN = sL / mag, sM / mag, sN / mag

        # RMS angular residual about the mean (collimation spread, step 5).
        sq = 0.0
        for (_px, _py, L, M, N) in survivors:
            dot = max(-1.0, min(1.0, L * mL + M * mM + N * mN))
            theta = math.acos(dot)
            sq += theta * theta
        rms_mrad = 1000.0 * math.sqrt(sq / len(survivors))

        # Chief pointing (the (Px=0,Py=0) ray, step 6) — SEPARATE from the residual.
        chief_mrad = None
        for (px, py, L, M, N) in survivors:
            if px == 0.0 and py == 0.0:
                chief_mrad = _slope_mrad(L, M, N)
                break

        # Max edge slope over the grid (step 7).
        max_edge_mrad = max(
            _slope_mrad(L, M, N) for (_px, _py, L, M, N) in survivors
        )

        # Cross-check (step 8) on the SAMPLED chief + the edge marginal of this field
        # ONLY (sampling is sufficient assurance — exhausting all rays doubles the
        # operand reads for no added correctness).
        field_cc_ok = True
        # Find the chief (0,0) and the edge (the max-slope ray).
        edge = max(survivors, key=lambda s: _slope_mrad(s[2], s[3], s[4]))
        samples = []
        for (px, py, L, M, N) in survivors:
            if px == 0.0 and py == 0.0:
                samples.append((px, py, L, M, N))
                break
        if edge not in samples:
            samples.append(edge)
        for (px, py, L, M, N) in samples:
            ok, detail = _cross_check_ray(
                system, surf_index, wave, 0.0, hy, px, py, L, M, N
            )
            if not ok:
                field_cc_ok = False
                cross_check_ok = False
                flags.append(f"field {field_idx}: {detail}")

        over_tol = rms_mrad > float(tolerance_mrad)
        any_over_tolerance = any_over_tolerance or over_tol
        if worst_residual is None or rms_mrad > worst_residual:
            worst_residual = rms_mrad

        per_field.append({
            "field_index": field_idx,
            "field_y": safe_float(field_y),
            "Hy": safe_float(hy),
            "rays_traced": len(traced),
            "rays_used": len(survivors),
            "rays_excluded": n_excluded,
            "rms_angular_residual_mrad": rms_mrad,
            "chief_pointing_mrad": chief_mrad,
            "max_edge_slope_mrad": max_edge_mrad,
            "mean_direction": [mL, mM, mN],
            "cross_check_ok": field_cc_ok,
            # A field is "collimated" iff its residual is within tolerance AND its
            # cross-check held (the residual is meaningless if the cross-check failed).
            "collimated": field_cc_ok and not over_tol,
        })

    # Verdict (§1.3): collimated iff EVERY measured field <= tolerance AND
    # cross_check_ok AND at least one field measurable; not_collimated iff any field
    # over tolerance; collimation_indeterminate iff nothing measurable OR any
    # cross-check failed.
    if not any_measurable or not cross_check_ok:
        verdict = "collimation_indeterminate"
    elif any_over_tolerance:
        verdict = "not_collimated"
    else:
        verdict = "collimated"

    return {
        "ok": True,
        "verdict": verdict,
        "per_field": per_field,
        "worst_field_residual_mrad": worst_residual,
        # The MCE per-config divergence signal: a collimator is only as
        # collimated as its worst field.
        "config_headline": worst_residual,
        "cross_check_ok": cross_check_ok,
        # The residual is a UNIT-DISK RMS (the grid is clipped to Px²+Py² <= 1) — NOT a
        # full-square average; independent of engine vignetting of the r=√2 corners.
        "pupil_domain": "unit_disk",
        "flags": flags,
    }


class _SystemSessionShim:
    """A thin ``.system`` shim so ``trace_rays(session, params)`` can be called with
    just a ``system`` (the substrate has the system, not the session). ``trace_rays``
    reads ``session.system`` only — this exposes exactly that. NOT a real session."""

    __slots__ = ("system",)

    def __init__(self, system):
        self.system = system


def system_session_shim(system):
    """Wrap a raw ``system`` in a minimal ``.system`` carrier for ``trace_rays``."""
    return _SystemSessionShim(system)


# --------------------------------------------------------------------------- #
# detect_collimated_output (§2.2 — the SHARED detector).
# --------------------------------------------------------------------------- #
def _read_afocal_image_space(system):
    """Read ``SystemData.Aperture.AFocalImageSpace`` (capital F).

    The natural-guess ``AfocalImageSpace`` AttributeErrors; guarded to
    ``None`` on any throw (a missing member / a wedged read). Returns ``bool`` or
    ``None``.
    """
    try:
        val = system.SystemData.Aperture.AFocalImageSpace
    except Exception:  # noqa: BLE001 — missing/wedged -> None (corroborating only)
        return None
    if isinstance(val, bool):
        return val
    return None


def _last_optical_surface(system):
    """The last OPTICAL surface index = ``NumberOfSurfaces - 2`` (the surface BEFORE the
    image), where the marginal Y and slope u' are well-defined for EVERY regime (a
    focused imager has a finite Y there, not Y≈0 at the image plane). Returns the
    int surface number, or ``None`` on a throw / a too-small system (< 3 surfaces)."""
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — fail-closed (the detector degrades to imaging)
        return None
    if n < 3:  # OBJECT + >=1 optical + IMAGE for a last-optical surface to exist
        return None
    return n - 2


def _trace_axial_marginals(system, wave, last_surface):
    """Trace the on-axis NEAR-AXIS upper/lower marginals to the LAST OPTICAL surface.

    Uses ``Py = ±_CONVERGENCE_PROBE_PY`` at the LAST OPTICAL surface (``last_surface``,
    NOT the user image plane — the root bug): the marginal height Y and
    slope ``u' = M/N`` there give ``s' = -Y/u'``, invariant to where the user placed the
    image surface. Returns ``(upper, lower)`` where each is
    ``(X, Y, Z, L, M, N)`` for a SURVIVING ray, or ``None`` if that marginal
    errored/vignetted/non-finite. NEVER raises (a trace fault -> ``(None, None)`` so the
    detector fails CLOSED to imaging).
    """
    py = _CONVERGENCE_PROBE_PY
    rays_payload = [
        {"wave": int(wave), "Hx": 0.0, "Hy": 0.0, "Px": 0.0, "Py": py},
        {"wave": int(wave), "Hx": 0.0, "Hy": 0.0, "Px": 0.0, "Py": -py},
    ]
    try:
        trace = trace_rays(
            system_session_shim(system),
            {"rays": rays_payload, "to_surface": int(last_surface),
             "rays_type": "Real"},
        )
    except Exception:  # noqa: BLE001 — a trace throw -> fail-closed
        return None, None
    if not isinstance(trace, dict) or not trace.get("ok"):
        return None, None
    out = [None, None]
    for k, ray in enumerate(trace.get("rays", [])[:2]):
        if bool(ray.get("errored")) or bool(ray.get("vignetted")):
            continue
        X, Y, Z = ray.get("X"), ray.get("Y"), ray.get("Z")
        L, M, N = ray.get("L"), ray.get("M"), ray.get("N")
        if not _is_finite_triple(L, M, N):
            continue
        if not (isinstance(X, (int, float)) and isinstance(Y, (int, float))
                and isinstance(Z, (int, float))
                and math.isfinite(X) and math.isfinite(Y) and math.isfinite(Z)):
            continue
        out[k] = (float(X), float(Y), float(Z), float(L), float(M), float(N))
    return out[0], out[1]


def _reconstruct_s_prime(marginal):
    """``s' = -Y / u'`` (``u' = M/N``) at the last optical surface, from a surviving
    marginal ``(X,Y,Z,L,M,N)``. Returns ``(s_prime, u_prime)``: a near-zero |u'|
    (parallel marginal) -> ``s_prime = +inf`` (image at infinity); a non-finite read ->
    ``(None, None)``. NEVER raises."""
    if marginal is None:
        return None, None
    _X, Y, _Z, _L, M, N = marginal
    try:
        u_prime = M / N if N else float("inf")
        if not math.isfinite(u_prime):
            return None, None
        if abs(u_prime) < _PARALLEL_SLOPE_EPS:
            return math.inf, u_prime
        s_prime = -Y / u_prime
        if not math.isfinite(s_prime):
            return None, None
        return s_prime, u_prime
    except Exception:  # noqa: BLE001 — any arithmetic fault -> unreadable
        return None, None


def _read_effl(system, wave):
    """Read ``EFFL`` (the dimensionless normalizer) via the named-slot 9-arg
    reader. Returns a positive finite float, or ``None`` (a suspicious / non-finite /
    non-positive read -> fail-closed; EFLX/EFLY carry a 1e10 sentinel and are NOT used).
    NEVER raises."""
    try:
        raw, suspicious = _mc.read_operand_slots(
            system, "EFFL", {3: ("Wave", int(wave))}
        )
    except Exception:  # noqa: BLE001 — EFFL unresolvable -> fail-closed
        return None
    if suspicious or not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    fv = float(raw)
    if not math.isfinite(fv) or fv <= 0.0:
        return None
    return fv


def _read_back_airgap(system, last_surface):
    """The last-optical → image-plane distance = the back-airgap thickness (the
    STAGE-2 placement-ratio numerator). Returns a finite non-negative float, or
    ``None`` on a throw / a non-finite read. NEVER raises."""
    try:
        thick = float(system.LDE.GetSurfaceAt(int(last_surface)).Thickness)
    except Exception:  # noqa: BLE001 — unreadable -> fail-closed
        return None
    if not math.isfinite(thick) or thick < 0.0:
        return None
    return thick


def _result(collimated, reason, signals, afocal):
    return {
        "collimated": bool(collimated),
        "reason": reason,
        "signals": signals,
        "afocal_image_space": afocal,
    }


def detect_collimated_output(system, *, wave=1, to_surface=-1):
    """The SHARED collimated/afocal-output detector (TWO-STAGE redesign).

    Returns ``{collimated: bool, reason: str|None, signals: {...},
    afocal_image_space: bool|None}``. NEVER raises. FAIL-CLOSED to
    ``collimated:False`` (imaging) on ANY bad read / throw — a detector fault must NEVER
    null a valid imager's Strehl (the load-bearing safety property; ``to_surface`` is
    accepted for signature compatibility but the detector ALWAYS reads at the last
    OPTICAL surface, never the user image plane — the root bug).

    Stage 1 — reconstruct ``s' = -Y/u'`` (the image conjugate from the last optical
    surface, past-focus-invariant) for BOTH near-axis marginals. PARALLEL marginals
    (both ``s' -> +inf``, an infinite-conjugate / afocal output: a beam-expander, an
    afocal telescope, a perfect collimator) SHORT-CIRCUIT to ``collimated:True`` BEFORE
    the EFFL read (``collimated_output_parallel_marginals``) — a true afocal has an
    INFINITE EFFL (the 1e10 sentinel -> ``_read_effl`` -> None), so gating it on EFFL
    would fail-CLOSE a clearly-collimated output to imaging. Otherwise, if
    ``|s'/EFFL| < _S_RATIO_THRESHOLD`` the output focuses near (a focused / past-focus /
    slow / normal imager) -> ``collimated:False``. (Opposite-sign FINITE marginals on
    an on-axis probe are a decentered/astigmatic output -> ``marginal_asymmetry``.)

    Stage 2 — the output focuses far / at infinity; ``placement_ratio = back_airgap /
    |s'|``: in ``[0.5, 2.0]`` the user placed the image plane AT the distant focus (a real
    long-conjugate imager, Strehl valid) -> ``collimated:False``; else (ratio ≪1, the
    plane is nowhere near the focus) -> afocal/collimator output -> ``collimated:True``.

    ``AFocalImageSpace`` is echoed as a CORROBORATING signal only (never sole — the
    dogfood collimator had it OFF). A declared-afocal-but-imaging system surfaces an
    honest ``afocal_declared_but_converging`` signal + ``collimated:False``.
    """
    afocal = _read_afocal_image_space(system)
    signals = {"afocal_image_space": afocal}

    last_surface = _last_optical_surface(system)
    if last_surface is None:
        signals["reason"] = "last_surface_unreadable"
        return _result(False, "last_surface_unreadable", signals, afocal)

    try:
        upper, lower = _trace_axial_marginals(system, wave, last_surface)
    except Exception:  # noqa: BLE001 — fail-CLOSED to imaging
        signals["reason"] = "detector_trace_fault"
        return _result(False, "detector_trace_fault", signals, afocal)

    s_upper, u_upper = _reconstruct_s_prime(upper)
    s_lower, u_lower = _reconstruct_s_prime(lower)
    if upper is not None:
        signals["upper_marginal"] = {"Y": upper[1], "M": upper[4], "N": upper[5]}
        signals["s_prime_upper"] = s_upper
        signals["u_prime_upper"] = u_upper
    if lower is not None:
        signals["lower_marginal"] = {"Y": lower[1], "M": lower[4], "N": lower[5]}
        signals["s_prime_lower"] = s_lower
        signals["u_prime_lower"] = u_lower

    # Both marginals must read; a single-ray read fault -> fail-CLOSED (imaging).
    if s_upper is None or s_lower is None:
        signals["reason"] = "marginal_unreadable"
        return _result(False, "marginal_unreadable", signals, afocal)

    abs_u, abs_l = abs(s_upper), abs(s_lower)
    # Mirror symmetry: |s'_upper| ≈ |s'_lower| (the two marginals reconstruct the SAME
    # conjugate). A wild disagreement (one finite, one infinite, or relative > tol)
    # signals an asymmetric/decentered output or a read fault -> fail-CLOSED (imaging).
    # This also guards against trusting the upper marginal alone.
    both_inf = math.isinf(abs_u) and math.isinf(abs_l)
    if not both_inf:
        if math.isinf(abs_u) != math.isinf(abs_l):
            signals["reason"] = "marginal_asymmetry"
            signals["marginal_asymmetry"] = True
            return _result(False, "marginal_asymmetry", signals, afocal)
        denom = max(abs_u, abs_l) or 1.0
        if abs(abs_u - abs_l) / denom > _MARGINAL_SYMMETRY_REL_TOL:
            signals["reason"] = "marginal_asymmetry"
            signals["marginal_asymmetry"] = True
            return _result(False, "marginal_asymmetry", signals, afocal)
        # Sign-agreement arm: the magnitude check above is magnitude-ONLY, so an
        # opposite-sign equal-magnitude FINITE pair (s_upper=+5000, s_lower=-5000) passes
        # it and would classify on a misleading signed average s'=0.0. For a
        # rotationally-symmetric on-axis system both marginals reconstruct the SAME-sign
        # conjugate; opposite signs => a decentered/astigmatic (non-rotationally-symmetric)
        # output -> fail-CLOSED to imaging, same as a magnitude disagreement. (Both s' are
        # FINITE here — the both_inf and one-inf cases are handled above.)
        if s_upper * s_lower < 0.0:
            signals["reason"] = "marginal_asymmetry"
            signals["marginal_asymmetry"] = True
            return _result(False, "marginal_asymmetry", signals, afocal)

    # The classification s' = the average of the two mirror-symmetric magnitudes
    # (both_inf -> inf). Past-focus-invariant; a CLASSIFICATION signal, not a precise
    # measurement for afocal systems.
    s_abs = math.inf if both_inf else 0.5 * (abs_u + abs_l)
    signals["s_prime"] = s_abs if math.isinf(s_abs) else 0.5 * (s_upper + s_lower)

    # The afocal beam-expander false-NEGATIVE: parallel near-axis marginals
    # (both s' -> +inf) are an infinite-conjugate / afocal output (a beam-expander, an
    # afocal telescope, a perfect collimator) — UNAMBIGUOUSLY collimated REGARDLESS of
    # EFFL. A true afocal has an INFINITE EFFL, so the engine returns the EFLX/EFLY-class
    # 1e10 sentinel -> _read_effl -> None: reading EFFL FIRST would fail-CLOSE a clearly-
    # collimated output to imaging (effl_unreadable, the re-opened silent-wrong). The EFFL
    # gate is UNNECESSARY when s_abs is already inf (|s'/EFFL| = inf >= threshold for any
    # EFFL; placement_ratio = back_airgap / inf = 0 -> collimated anyway), so short-circuit
    # BEFORE the EFFL read. The EFFL gate below is kept ONLY on the FINITE-s' path (which
    # genuinely needs EFFL to normalize the reconstructed conjugate).
    if both_inf:
        signals["s_prime_over_effl"] = math.inf
        signals["placement_ratio"] = 0.0
        signals["reason"] = "collimated_output_parallel_marginals"
        return _result(True, "collimated_output_parallel_marginals", signals, afocal)

    effl = _read_effl(system, wave)
    if effl is None:
        signals["reason"] = "effl_unreadable"
        return _result(False, "effl_unreadable", signals, afocal)
    signals["effl"] = effl

    s_over_effl = math.inf if math.isinf(s_abs) else (s_abs / effl)
    signals["s_prime_over_effl"] = s_over_effl
    signals["s_ratio_threshold"] = _S_RATIO_THRESHOLD

    # STAGE 1: a near focus (|s'/EFFL| < threshold) -> a real imager -> NOT collimated.
    if s_over_effl < _S_RATIO_THRESHOLD:
        signals["reason"] = "imaging_near_focus"
        if afocal is True:
            # Declared afocal but the marginals focus near -> honest disagreement.
            signals["afocal_declared_but_converging"] = True
        return _result(False, "imaging_near_focus", signals, afocal)

    # STAGE 2: the output focuses far / at infinity. Is the image plane AT that focus?
    back_airgap = _read_back_airgap(system, last_surface)
    signals["back_airgap"] = back_airgap
    if back_airgap is None:
        # Cannot resolve where the user placed the image plane. The output DOES focus
        # far (stage 1 passed), but without the placement we cannot rule out a real
        # long-conjugate imager -> fail-CLOSED to imaging (never null a Strehl on an
        # unresolved placement).
        signals["reason"] = "placement_unresolved"
        return _result(False, "placement_unresolved", signals, afocal)

    placement_ratio = (
        math.inf if math.isinf(s_abs) or s_abs == 0.0 else (back_airgap / s_abs)
    )
    # s_abs is inf for a perfect collimator -> ratio 0 (plane ≪ focus); a 0 s_abs can't
    # occur here (stage 1 required s'/EFFL >= 20). Guard both.
    if math.isinf(s_abs):
        placement_ratio = 0.0
    signals["placement_ratio"] = placement_ratio
    signals["placement_window"] = [_PLACEMENT_RATIO_LO, _PLACEMENT_RATIO_HI]

    if _PLACEMENT_RATIO_LO <= placement_ratio <= _PLACEMENT_RATIO_HI:
        # The image plane sits AT the distant focus -> a real long-conjugate imager.
        signals["reason"] = "imaging_at_distant_focus"
        if afocal is True:
            signals["afocal_declared_but_converging"] = True
        return _result(False, "imaging_at_distant_focus", signals, afocal)

    # The image plane is nowhere near the focus -> afocal / collimator output.
    signals["reason"] = "collimated_output"
    return _result(True, "collimated_output", signals, afocal)


__all__ = [
    "compute_collimation_residual",
    "detect_collimated_output",
    "system_session_shim",
]
