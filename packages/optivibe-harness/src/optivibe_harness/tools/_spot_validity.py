"""tools/_spot_validity.py — private substrate: the get_spot trace-validity verdict.

NOT dispatchable (no ``TOOL_SPEC``). The single source of truth for the S2
spot-validity cross-check (§1, §4.1). The spot result object has NO
native validity member (probe headline #1), so ``get_spot`` falls back to a
full-pupil batch ray-trace cross-check: a ray is BAD iff ``errorCode != 0 OR
vignetteCode != 0`` (the EXACT combined truncation signal ``analysis_raytrace`` /
``_layout_rays`` already use). The verdict ladder lives HERE so both ``get_spot``
and the fake's reference computation key on it (L30, no drift).

The cross-check is ONE ``OpenBatchRayTrace`` covering ALL fields x the 9-ray pupil
grid, reusing the SHARED ``_layout_rays._trace_surface_all_rays`` batch reader
(widened to 5-tuple ``(wave, hx, hy, px, py)`` specs) — the ``tup[0]`` success
terminator + the 15-arity guard + ``Close()`` in ``finally`` are inherited from
that reader VERBATIM. The deadline (``OPTIVIBE_SPOT_VALIDITY_BUDGET_S``) is checked
BEFORE the single open (a single in-flight .NET open is un-interruptible — the
budget bounds whether we ATTEMPT the open, the hang-watchdog reality).

The verdict NEVER raises past its caller (wrapped like ``_layout_rays``): a batch
``None`` / throw / empty stream -> every field ``reason:"validity_indeterminate"``
(value KEPT, ``ok:true`` upstream). A validity-check failure must never turn a
working ``get_spot`` into an error.
"""
import math
import os
from time import perf_counter

from ..enums import _resolve_enum
from ._layout_rays import _read_fields, _trace_surface_all_rays
from .analysis_raytrace import _opd_mode_member, _rays_type_enum

# --------------------------------------------------------------------------- #
# Module constants (§1.2, §1.4, §4.1).
# --------------------------------------------------------------------------- #
# The beyond-capture ceiling (spot-radius units = um for a mm lens). A fully-traced
# BUT huge spot (probe Z3: 152581, 1.48e6) is M2 beyond_capture. 1e7 um = 10 m
# radius is physically impossible for a real image (R6). A module constant, NOT a param.
_SPOT_HUGE = 1.0e7

# The 9-ray pupil grid: center + cross + diagonal over [-1, +1] (probe Z5).
# Spans the 2-D pupil (NOT a degenerate meridional Px=0 line). (Px, Py) pairs.
_D = 0.7071067811865476  # 1/sqrt(2): the diagonal sample radius (|(Px,Py)| = 1)
_SPOT_GRID = (
    (0.0, 0.0),
    (1.0, 0.0), (-1.0, 0.0),
    (0.0, 1.0), (0.0, -1.0),
    (_D, _D), (_D, -_D), (-_D, _D), (-_D, -_D),
)
_SPOT_GRID_N = len(_SPOT_GRID)  # 9 — a module constant, NOT a param.

# The geometric cross-check traces the PRIMARY wave (vignetting/obscuration is
# geometric, wave-independent to first order). Disclosed as top-level validity_wave.
_VALIDITY_WAVE = 1

# The validity-cross-check budget (seconds). Mirrors layout_render._ray_budget_s.
_DEFAULT_VALIDITY_BUDGET_S = 12.0


def validity_budget_s():
    """Read ``OPTIVIBE_SPOT_VALIDITY_BUDGET_S`` (default 12.0) with the watchdog clamp.

    POSITIVE-FINITE clamp (mirrors ``layout_render._ray_budget_s`` /
    ``__main__._env_float``): a non-positive value (0 / negative) would make the
    deadline already-elapsed and skip EVERY healthy cross-check; a non-finite value
    (nan / inf) would defeat the budget (``perf_counter() >= nan`` is False ->
    unbounded). Any of those falls back to the default.
    """
    raw = os.environ.get("OPTIVIBE_SPOT_VALIDITY_BUDGET_S")
    if raw is None:
        return _DEFAULT_VALIDITY_BUDGET_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_VALIDITY_BUDGET_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_VALIDITY_BUDGET_S
    return value


# The validity-ladder principle (S2 adjudication — the _SPOT_HUGE band):
#   ``valid`` means the TRACE SUCCEEDED and the RMS is the REAL spot size — it does
#   NOT mean the spot is good. A finite, clean-traced (9/9, no vignette, errorCode 0)
#   huge RMS is a real (badly-aberrated) spot and is reported ``valid:true`` WITH the
#   real value so the agent can grade it against its target (a 152 mm spot is honestly,
#   gradeably terrible — not misleading). The silent-wrong this ladder guards is a
#   DIFFERENT thing: (1) a FAILED trace reading as a small/perfect number — caught by
#   the always-on full-pupil grid cross-check (n_traced) — and (2) a non-finite /
#   safe_float-sentinel garbage value, or a physically-impossible >= _SPOT_HUGE (10 m)
#   reading — caught by the M2 ``beyond_capture`` arm. The genuine huge-garbage cases
#   the probe documented actually VIGNETTE and are caught by the grid; a finite clean
#   trace is real data.
def _verdict_for_field(n_traced, n_grid, rms_value):
    """The per-field verdict ladder (§1.4). PURE — both the SUT and the fake key on it.

    ``n_traced`` is the count of grid rays with ``errorCode == 0 AND vignetteCode ==
    0`` for this field; ``n_grid`` is the sampled grid size; ``rms_value`` is the
    field's polychromatic RMS (used ONLY for the M2 beyond-capture gate, and ONLY
    when fully traced). Returns ``(valid, traced, reason)`` where ``valid`` /
    ``traced`` are bool and ``reason`` is a string or ``None``.

    Ladder:
      - n_traced == 0                 -> blocked (all rays bad; geometric total stop)
      - 0 < n_traced < n_grid (M3)    -> partial_vignette (a clipped sub-pupil)
      - n_traced == n_grid AND huge   -> beyond_capture (M2)
      - n_traced == n_grid AND finite -> VALID (the genuine spot, incl. a tiny 2.83e-13)
    """
    if n_traced <= 0:
        # All grid rays are bad. The geometric verdict is "blocked" (total stop);
        # the blocked-vs-errored taxonomy nuance (vignette vs errorCode) is folded
        # into "blocked" at the field level — both mean zero rays reached the image.
        return False, False, "blocked"
    if n_traced < n_grid:
        # M3: a partial vignette — the surviving sub-pupil RMS is NOT the spot the
        # design delivers; null the headline + disclose traced_fraction.
        return False, True, "partial_vignette"
    # n_traced == n_grid: fully traced. Only here does the RMS magnitude matter.
    # The magnitude witness may be a real float OR a safe_float sentinel STRING
    # ("nan"/"inf") for a non-finite engine reading. A fully-traced field whose RMS
    # is non-finite (string sentinel) OR huge (>= _SPOT_HUGE) is beyond_capture (a
    # garbage reading despite every ray nominally tracing). A finite, non-huge RMS
    # — INCLUDING a genuine tiny 2.83e-13 — is VALID (no over-refusal).
    if isinstance(rms_value, (int, float)) and not isinstance(rms_value, bool):
        if not math.isfinite(rms_value):
            return False, True, "beyond_capture"
        if abs(rms_value) >= _SPOT_HUGE:
            return False, True, "beyond_capture"
        return True, True, None
    # A string sentinel ("nan"/"inf") or a non-number reading on a fully-traced
    # field is a garbage magnitude -> beyond_capture (never silently "valid").
    if isinstance(rms_value, str) and rms_value in ("nan", "inf", "-inf"):
        return False, True, "beyond_capture"
    # A None magnitude (e.g. a GEO-only kind with no RMS witness) cannot be magnitude-
    # gated; with the full grid traced it is treated as VALID (the grid is the
    # authority; magnitude is a secondary M2 flag).
    return True, True, None


def evaluate_spot_validity(system, field_hys, *, deadline=None):
    """Run the ONE-batch full-pupil cross-check; return per-field tallies (§4.1).

    ``field_hys`` is the ordered list of per-field normalized ``Hy`` values
    (1-based field i -> ``field_hys[i-1]``). ONE ``OpenBatchRayTrace`` traces
    ``n_fields * 9`` rays (every field x the 9-ray grid) to the image
    (``toSurface=-1``) at the primary wave, then computes per-field
    ``(n_traced, n_grid, traced_fraction)``. Returns:

      ``{"available": bool, "budget_exhausted": bool,
         "fields": [{"n_traced", "n_grid", "traced_fraction"} | None, ...]}``

    where ``fields[i]`` is the tally for field ``i+1`` (a ``None`` entry means the
    cross-check could not grade that field — the caller marks it indeterminate). On
    a budget overrun (deadline already elapsed) ``available=False`` +
    ``budget_exhausted=True`` (the whole cross-check is skipped, no open issued). On
    a batch ``None`` / throw / empty stream ``available=False`` (every field
    indeterminate). NEVER raises.
    """
    n_fields = len(field_hys)
    out = {
        "available": False,
        "budget_exhausted": False,
        "fields": [None] * n_fields,
    }
    if n_fields <= 0:
        return out

    # The deadline is checked BEFORE the single open (§1.3): a single in-flight
    # .NET open cannot be interrupted, so the budget bounds whether we ATTEMPT it.
    if deadline is not None and perf_counter() >= deadline:
        out["budget_exhausted"] = True
        return out

    try:
        # Resolve the batch-trace enum members ONCE (the .Real MEMBER, not the class
        # — gotcha #92/L35; OPDMode.None via getattr). A resolution failure routes to
        # the never-raise total-failure path below (every field indeterminate).
        rays_real = _resolve_enum(_rays_type_enum(system), "Real")
        opd_none = _opd_mode_member(system, "None")

        # Build the n_fields * 9 ray specs, field-major then grid order. Each spec is
        # (wave, hx, hy, px, py) — the 5-tuple the widened reader takes.
        trace_specs = []
        for hy in field_hys:
            for px, py in _SPOT_GRID:
                trace_specs.append((_VALIDITY_WAVE, 0.0, float(hy), float(px), float(py)))

        # ONE open, to the image surface (toSurface=-1).
        results = _trace_surface_all_rays(
            system, rays_real, opd_none, -1, trace_specs
        )
        if results is None:
            # Batch None / throw -> every field indeterminate (out.fields stays None).
            return out

        # Tally per field. Distinguish a ray that RETURNED a (bad) result (err/vig
        # readable -> a real geometric stop: M1 blocked / M3 partial) from a ray the
        # stream did NOT return (a None entry: an early terminator / arity drift / a
        # mid-stream read fault). A field where NO ray returned a readable result is
        # INDETERMINATE (the cross-check could not grade it -> the value is preserved
        # upstream, §1.5 / T8), NOT "blocked" (which is a PROVEN geometric stop). A
        # field with SOME readable rays is GRADED by them; an un-returned ray within
        # such a field counts as BAD (fail-closed, §1.4).
        for fi in range(n_fields):
            n_good = 0
            n_readable = 0
            base = fi * _SPOT_GRID_N
            for gi in range(_SPOT_GRID_N):
                idx = base + gi
                res = results[idx] if idx < len(results) else None
                if res is None:
                    continue  # un-returned ray -> not readable (bad if graded)
                n_readable += 1
                err, vig, _x, _y, _z = res
                if err == 0 and vig == 0:
                    n_good += 1
            if n_readable == 0:
                # Nothing came back for this field -> the cross-check could not grade
                # it (a truncated stream / fault). Leave it None -> indeterminate.
                out["fields"][fi] = None
            else:
                out["fields"][fi] = {
                    "n_traced": n_good,
                    "n_grid": _SPOT_GRID_N,
                    "traced_fraction": n_good / _SPOT_GRID_N,
                }
        out["available"] = True
        return out
    except BaseException:  # noqa: BLE001 — a validity-check failure NEVER raises (§1.5)
        return out


def read_field_hys(system):
    """Per-field normalized ``Hy`` (1-based) via ``_layout_rays._read_fields``. Never raises.

    Returns the ``Hy`` list (``Y / max|Y|`` with the ``max|Y| == 0 -> 0`` division
    guard) or ``None`` on any read failure (the caller degrades to indeterminate).
    """
    try:
        hy_list, _raw_y, _flags = _read_fields(system)
        return list(hy_list)
    except BaseException:  # noqa: BLE001 — fail-closed: a field read failure -> None
        return None
