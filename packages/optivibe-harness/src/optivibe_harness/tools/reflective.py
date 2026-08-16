"""tools/reflective.py — the REFLECTIVE optical acts.

TWO dispatchable tools — the optical reflective core — both DELEGATING every
coordinate act to the CB primitive (``cb_surface`` / ``_cb_cells``):

- ``set_mirror`` — the HONEST mirror primitive (Q-C). ``row.Material = "MIRROR"``
  + read-back proof it reads ``"MIRROR"``. Refuses OBJECT(0) and IMAGE LOUD. Owns
  ZERO sign bookkeeping — the engine keeps the user's thickness sign (no auto-flip);
  the caller owns the post-mirror thickness sign. Never-raise envelope.

- ``fold_beam`` — the θ/2 fold composition (Q-A), the ONLY tool that CLAIMS a fold.
  To fold a beam by a requested angle φ the mirror is tilted by **φ/2** (deviation =
  2·tilt, machine-exact, Q-A). It INSERTS the entry CB + mirror (+ a return CB if
  ``restore_axis``) via ``insert_surface`` (HARD-crash firewall: validate-before-
  insert), then DELEGATES the cell authoring to ``add_coordinate_break`` /
  ``set_mirror`` / ``add_return_cb`` on the POST-INSERT surface numbers, all inside
  ONE ``SaveAs``/``LoadFile`` atomic checkpoint (the ``apply_lens_spec`` precedent —
  nit 4b). For a COMPOUND same-plane fold the entry CB tilt is MEASURE-AND-SOLVED on the
  UNSIGNED total deviation (a march-then-secant; the signed in-plane deviation WRAPS for a
  skew frame so it is NOT the solve quantity). After authoring it FALSIFIES the achieved
  global chief-ray deviation (``RAGA/RAGB/RAGC`` incoming-vs-outgoing direction cosines
  across the mirror) with TWO INDEPENDENT proofs: the unsigned MAGNITUDE == φ within 1e-6
  deg (acos) AND an INDEPENDENT direction check — a raw-cosine in-plane cross-product SENSE
  whose sign must match the requested ``direction`` (so a fold of the right magnitude but
  the WRONG direction is caught). The direction proof reads the RAW cosines, NOT
  the signed-deviation oracle, so it stays an INDEPENDENT regression net even though the
  solve now drives the unsigned deviation (the circularity fix). An
  ORTHOGONAL dogleg (the entry beam OUT of the fold plane) is REFUSED EARLY
  (``fold_skew_entry``) before any insert — its in-plane direction is unverifiable. On any
  mismatch it ROLLS BACK and returns ``{ok:false, family:"fold_unverified"}``. Never claims
  a fold the geometry does not show (L28). The rollback POST-RESTORE read-back verifies the
  LoadFile actually restored the pre-fold form (a clean-but-didn't-restore LoadFile reports
  ``partial_state`` honestly).

The axis→fold-plane + sign map (Q-A, [Discovered-live-probed]):
- ``tilt_x`` (Tilt About X) folds in the y-z (meridional) plane; ``+tilt_x`` bends
  the outgoing beam toward **−y** (signed y-z deviation = −2·tilt_x).
- ``tilt_y`` (Tilt About Y) folds in the x-z (sagittal) plane; ``+tilt_y`` bends
  toward **+x** (signed x-z deviation = +2·tilt_y).
``fold_beam`` owns the requested-direction→tilt-sign mapping (the ``direction``
param picks which way to bend).

Live ZOS-API integration: the live CB test; unit-tested against the
fixture-seeded fake doubles (extended for mirror + global
direction cosines).
"""
import math

from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _cb_cells as _cb
from . import _lens_common as _lc
from . import cb_surface as _cbs
from . import lens_surface as _ls
from ._analysis_common import error_envelope
from ._beam_reach import beam_reaches_span as _beam_reaches_span

# Family tokens.
_MIRROR_WRITE = "mirror_write"        # a Material write / read-back failure
_MIRROR_PARAM = "mirror_param"        # a bad mirror param value
_GRATING_PARAM = "grating_param"      # a bad grating param value
_GRATING_WRITE = "grating_write"      # a grating ChangeType / cell / read-back failure
_FOLD_PARAM = "fold_param"            # a bad fold param value
_FOLD_UNVERIFIED = "fold_unverified"  # the achieved deviation != requested φ
_FOLD_WRITE = "fold_write"            # an authoring / checkpoint fault -> rolled back
_FOLD_SKEW_ENTRY = "fold_skew_entry"  # the entry beam is not in the chosen fold plane
# Fold-and-stay downstream-frame correction.
_FOLD_DOWNSTREAM_MISS = "fold_downstream_miss"  # the rays-reach span gate missed a downstream optic
_FOLD_DOWNSTREAM_FRAME_UNVERIFIED = "fold_downstream_frame_unverified"  # the frame correction did not converge

# The fold deviation proof gate (Q-A): the observed residual vs 2α was 7.1e-15 deg,
# so a gate at 1e-6 deg is nine orders of magnitude above the floor while far below
# any genuine authoring error.
_FOLD_TOL_DEG = 1e-6

# The measure-and-solve convergence tolerance (entry-frame-aware fold, compound fix).
# The secant step on the UNSIGNED total-deviation residual converges to machine epsilon
# in one-to-two steps on the live compound case (probe q4: iter-1 residual 4.4e-13 deg);
# a 1e-6 deg gate is far above that floor yet far below any genuine under-deviation (the
# naive φ/2 seed under-deviated by tens of degrees live).
_FOLD_SOLVE_TOL_DEG = 1e-6

# The measure-and-solve iteration cap. The probe converged in ONE step for the same-plane
# compound; a cap of 8 covers the bounded MARCH-then-secant the UNSIGNED solve needs (the
# unsigned total deviation is V-shaped in tilt for a SAME-plane refold — minimum where the
# fold cancels the entry incidence — so a plain secant from the φ/2 seed sits on the wrong
# arm; we MARCH outward (increasing |tilt|) to bracket the requested φ on the correct arm
# then secant inside the bracket). NON-convergence within the cap routes to fold_unverified
# + rollback (we never ship a fold that did not reach φ — L26 self-adversary).
_FOLD_SOLVE_MAX_ITERS = 8

# The bracketing MARCH step (deg of CB tilt) for the UNSIGNED solve. We step the tilt
# OUTWARD (away from zero, the seed's sign) by this increment until the unsigned total
# deviation brackets the requested φ on the correct (monotone-rising) arm of the V. The
# response slope is ≈±2 deg-deviation per deg-tilt, so a 5-deg march advances ≈10 deg of
# deviation per step — coarse enough to bracket φ in a few steps, fine enough to land on
# the near arm. (Bounded by _FOLD_SOLVE_MAX_ITERS so a non-bracketing frame fails closed.)
_FOLD_SOLVE_MARCH_DEG = 5.0

# The MAX bracketing-march step (deg). The march step grows geometrically (×2) to bracket
# quickly, but is CAPPED here so a single leap cannot vault past the deviation PEAK (D maxes
# at 180 deg then falls — a triangle wave in the same-plane tilt) and land the bracket on the
# wrong (falling) arm. 30 deg of tilt is ≈60 deg of deviation per step — far below the ~90 deg
# half-period — so a capped step stays on one arm. (An extreme secondary fold that cannot
# bracket within the cap fails closed, never silently wrong.)
_FOLD_SOLVE_MARCH_STEP_MAX = 30.0

# The minimum |slope| for a safe secant step. The unsigned response slope is ≈±2 near the
# operating point but UNKNOWN for a skew entry frame, so the solve ESTIMATES it from two
# evals (a secant) rather than assuming a fixed value. A slope magnitude below this floor
# (two near-equal evals — a flat/grazing region, or the V minimum where the slope passes
# through zero) would make the secant step diverge or divide-by-near-zero -> we refuse to
# step and roll back rather than loop on a near-singular update (L26 self-adversary).
_FOLD_SOLVE_MIN_SLOPE = 1e-3

# The EARLY skew-entry guard (Part C). ``fold_beam`` folds the beam IN the chosen plane
# (axis 'x' -> y-z, axis 'y' -> x-z); the measure-and-solve reaches φ in MAGNITUDE for any
# entry frame, but the in-plane DIRECTION (which way the beam bends, the ``direction`` job)
# is only resolvable when the ENTRY beam lies IN that plane. A same-plane compound (a
# periscope/Z-fold: the entry beam already turned within the SAME plane) stays in-plane and
# is SUPPORTED; an ORTHOGONAL dogleg (tilt_x then tilt_y) carries the entry beam OUT of the
# second fold's plane, so the bend is out-of-plane and its direction is unreadable from the
# in-plane cosines (probe q3: the signed in-plane deviation WRAPS / sign-flips). We detect
# this BEFORE any insert by reading the entry chief ray's out-of-fold-plane component: an
# entry skew beyond this tolerance is REFUSED early (``fold_skew_entry``) — the magnitude
# would solve but the direction cannot be honestly verified, so we never ship it.
_FOLD_SKEW_ENTRY_TOL_DEG = 1.0

# The minimum in-plane direction-component CHANGE the INDEPENDENT direction gate (Part B)
# needs to read a SIGN. The gate reads the raw outgoing-vs-incoming chief-ray cosine in the
# fold plane (NOT the signed-deviation oracle) and asserts the CHANGE sign matches the
# requested direction. If the change is below this floor the bend barely moved the in-plane
# component (a near-grazing / skew fold) and the sign is unverifiable -> fail closed (never
# a guessed direction pass).
_FOLD_DIR_MIN_DELTA = 1e-6

# The downstream-orientation-restored gate (restore_axis): the return CB restores the
# rotation block to identity (Q-A). Reuse the CB primitive's machine-epsilon gate.
_RESTORE_TOL_DEG = _cb.GLOBAL_FRAME_TOL_DEG

# Fold-and-stay: the downstream-frame-axis gate. The bounded direct-residual
# correction (the post-mirror CB that rotates the downstream LOCAL +z onto the reflected
# chief ray) drives the residual r = ∠(local +z, reflected ray) to 0.0 in ONE step
# (probe A2: slope 1.0, one-shot converges; final r = 0.0). Reuse the CB primitive's
# machine-epsilon gate (1e-9 deg) — far above the observed 0.0/floor, far below any
# genuine mis-correction.
_FRAME_AXIS_TOL_DEG = _cb.GLOBAL_FRAME_TOL_DEG

# The direct-residual frame correction is monotone-LINEAR with slope exactly
# 1.0 deg-frame-rotation per deg-CB-tilt in the matching plane (probe A2 live-pinned: a
# pure in-plane CB tilt rotates the downstream frame 1:1). The one-shot seed tilt =
# direction · r0 zeroes r in one step; the bounded Newton step (Δtilt = -r/slope) is the
# defense-in-depth backstop for the (probe-unobserved) overshoot case.
_FRAME_CORRECTION_SLOPE = 1.0

# The correction-step cap. Probe A2 converged in ONE step on every case
# (on-axis AND same-plane compound); the cap of 2 (the seed-shot + one bounded Newton
# step) is the fail-closed backstop — a residual still > tol after 2 corrections routes
# to fold_downstream_frame_unverified + rollback (never a frame-uncorrected stay fold).
_FRAME_CORRECTION_MAX_STEPS = 2

# A direction cosine whose |value| reaches this (or is non-finite) is a sentinel /
# vignette collapse — the deviation is unverifiable, never a pass (the _layout_rays
# truncation-magnitude precedent).
_COSINE_SENTINEL_MAGNITUDE = 1e9

# axis -> (the CB tilt param it authors, the signed-plane sign).
#   tilt_x folds y-z, +tilt_x -> beam toward -y (signed_yz = -2*tilt_x).
#   tilt_y folds x-z, +tilt_y -> beam toward +x (signed_xz = +2*tilt_y).
_AXIS_TO_TILT_PARAM = {"x": "tilt_x", "y": "tilt_y"}

# The documented signed-deviation convention per axis. The signed in-plane
# deviation expected for a fold = ``_AXIS_SIGNED_SIGN[axis] · direction · φ``:
#   axis 'x' (y-z plane): +tilt_x -> beam toward -y -> signed_yz = -2·tilt_x =
#     -direction·φ  (sign = -1).
#   axis 'y' (x-z plane): +tilt_y -> beam toward +x -> signed_xz = +2·tilt_y =
#     +direction·φ  (sign = +1).
# The signed oracle compares the MEASURED signed in-plane deviation against this.
_AXIS_SIGNED_SIGN = {"x": -1.0, "y": +1.0}


# --------------------------------------------------------------------------- #
# Shared validation.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}``."""
    return params if isinstance(params, dict) else {}


# =========================================================================== #
# §1. set_mirror — the honest primitive.
# =========================================================================== #
def set_mirror(session, params):
    """Make a surface a mirror: ``Material = "MIRROR"`` + read-back proof (Q-C).

    Params: ``surface`` (int, REQUIRED).

    Validate-then-open: bounds-check ``surface`` (the geometry firewall, client-
    side, BEFORE any engine touch) and REFUSE OBJECT(0) and the IMAGE surface LOUD
    (a mirror on either is nonsensical). Writes ``row.Material = "MIRROR"`` and
    read-back-proves it reads ``"MIRROR"`` (a silent no-op is caught). Owns ZERO sign
    bookkeeping — the engine keeps the user's thickness sign; the caller owns the
    post-mirror thickness sign. Never-raise envelope
    ``{ok, surface, material:"MIRROR", was_mirror}``.
    """
    params = _require_dict(params)
    try:
        return _set_mirror_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_mirror", _MIRROR_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_mirror", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mirror_write (L26)
        return error_envelope(
            "set_mirror", _MIRROR_WRITE,
            f"unexpected engine fault authoring the mirror ({exc!r}); refusing rather "
            "than shipping an unverified mirror",
        )


def _set_mirror_impl(session, params):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    # The geometry firewall (1 <= surface <= N-1) — refuses OBJECT(0); allows N-1.
    _lc._require_geometry_index(surface, n)
    # Explicitly REFUSE the IMAGE surface (a mirror on the image plane is nonsensical;
    # the geometry firewall allows N-1, so we refuse it here LOUD).
    if surface == n - 1:
        raise ToolParamError(
            f"surface {surface} is the IMAGE surface; a mirror on the image plane is "
            "nonsensical — refusing (choose an interior surface)"
        )

    row = lde.GetSurfaceAt(surface)
    was_mirror = _read_material(row).strip().upper() == "MIRROR"

    try:
        row.Material = "MIRROR"
    except Exception as exc:  # noqa: BLE001 — a Material write THROW -> mirror_write
        raise SurfaceWriteError(
            f"could not write Material='MIRROR' to surface {surface} ({exc!r}); the "
            "engine rejected the write — refusing rather than shipping an unverified "
            "mirror",
            field="material", intended="MIRROR", actual=None, surface=surface,
        ) from exc

    # Read-back-as-proof: re-fetch the row (never hold the proxy across the boundary)
    # and confirm the material reads MIRROR. A silent no-op is caught here.
    row = lde.GetSurfaceAt(surface)
    actual = _read_material(row)
    if actual.strip().upper() != "MIRROR":
        raise SurfaceWriteError(
            f"surface {surface} Material did not read back as MIRROR (reads "
            f"{actual!r}) — the write silently no-opped; refusing rather than claiming "
            "an unverified mirror",
            field="material", intended="MIRROR", actual=actual, surface=surface,
        )

    return {
        "ok": True,
        "surface": surface,
        "material": "MIRROR",
        # Honest: was the surface already a mirror (an idempotent re-author) — the
        # caller never reads a clean ok:true as a NEW mirror.
        "was_mirror": was_mirror,
    }


def _read_material(row):
    """Read ``row.Material`` -> str, THROW-guarded -> SurfaceWriteError."""
    try:
        return str(row.Material)
    except Exception as exc:  # noqa: BLE001 — a Material read THROW -> mirror_write
        raise SurfaceWriteError(
            f"could not read a surface Material ({exc!r}); refusing rather than "
            "guessing whether it is a mirror",
            field="material", intended=None, actual=None, surface=None,
        ) from exc


# =========================================================================== #
# §1b. set_diffraction_grating — the grating authoring primitive.
# =========================================================================== #
#
# NIT-1: the grating Par cells are a DIFFERENT cell table from the CB Par cells, so we
# do NOT reuse ``_cb_cells.write_cb_cell`` (it is hardwired to CB_PARAMS — Par1 "Decenter
# X"/Par2 "Decenter Y", with Par6 "Order" an INTEGER cell — and would reject the grating
# Headers and mis-type the order). We build a grating-LOCAL 2-row Par table here with its
# own DataType-keyed writer, reusing the ``cell.DataType`` discriminator PATTERN (the
# ``_cb_cells`` / ``_merit_cells`` / ``_tol_cells`` decoupled-table precedent) — NOT the
# CB table. Both grating cells are Double (recorded probe capture, g1: Par1
# "Lines/µm" Double, Par2 "Diffract Order" Double — the order cell is Double here, SURPRISE,
# unlike the CB Order Integer cell). Par2 is written via DoubleValue (NIT-3) — the integral-
# valued order semantic guard lives at the TOOL level, never as a cell-type discriminator.
#
# The grating Par cell table: (param token, ParN column, expected Header, expected kind).
# Headers are the live ``cell.Header`` strings (the µ is U+00B5, matching the capture).
_GRATING_PARAMS = (
    ("lines_per_micron", "Par1", "Lines/µm", "double"),
    ("order", "Par2", "Diffract Order", "double"),
)
_GRATING_PARAM_TO_COL = {p: col for p, col, _h, _k in _GRATING_PARAMS}
_GRATING_PARAM_TO_HEADER = {p: header for p, _c, header, _k in _GRATING_PARAMS}
_GRATING_PARAM_TO_KIND = {p: kind for p, _c, _h, kind in _GRATING_PARAMS}

# The grating-cell read-back proof floor (the _cb_cells precedent — tight rel tol with an
# abs floor near double-precision ULP so a write that collapsed to 0.0 is caught LOUD).
_GRATING_READBACK_ABS_TOL = 1e-15


def set_diffraction_grating(session, params):
    """Make a surface a diffraction grating: ``Lines/µm`` + ``Diffract Order`` cells (§1b).

    Params: ``surface`` (int, REQUIRED), ``lines_per_micron`` (float > 0, REQUIRED — the
    grating frequency ν, Par1), ``order`` (int|float, REQUIRED — the diffraction order m,
    Par2; an integral float ``1.0`` is accepted per the integral-float contract, a non-integral ``1.5`` is
    REFUSED LOUD as non-physical), ``reflective`` (bool, REQUIRED — NO default; a load-
    bearing user declaration of the grating TYPE: True = reflective (a mirror substrate, the
    beam reflects+diffracts; ALSO sets ``Material="MIRROR"`` via the set_mirror Material
    logic, with read-back proof), False = transmissive (the beam passes through+diffracts).
    Their deviation differs fundamentally, so the tool will NOT guess: an ABSENT ``reflective``
    (key missing or None) is REFUSED LOUD pre-mutation (zero mutation) — the agent should ASK
    the user when the spec does not state the grating type, never retry with a guess).

    Validate-then-open: bounds-check ``surface`` (the geometry firewall, client-side,
    BEFORE any engine touch) and REFUSE OBJECT(0) and the IMAGE surface LOUD (a grating on
    either is nonsensical); reject a non-finite / ``lines_per_micron <= 0`` LOUD; reject a
    NaN/inf/non-integer-valued ``order`` LOUD. Then ChangeType ->
    ``SurfaceType.DiffractionGrating`` (the cb_surface ChangeType recipe), write Par1 +
    Par2 (both Double, DataType-keyed) and READ-BACK-AS-PROOF: the cells persisted AND
    ``row.Type == DiffractionGrating``. If ``reflective=True``, also write+read-back
    ``Material="MIRROR"`` (delegating to ``set_mirror`` so the reflective flag inherits the
    honest Material read-back). The ACTUAL diffraction follows ``sin θ_out = sin θ_in +
    m·λ·ν``; that is falsified in the live test (the per-call proof is the cells + type, as
    this is an AUTHORING tool, not a beam-bending claim like fold_beam).

    Returns ``{ok, surface, lines_per_micron, order, reflective, type:"DiffractionGrating",
    material, was_mirror}``. ``material``/``was_mirror`` disclose the surface's ACTUAL Material
    read back after ChangeType (a grating authored over a pre-existing MIRROR carries the
    MIRROR material forward — so a ``reflective=False`` grating on a former mirror is in fact
    a REFLECTIVE grating; the return never implies a transmissive grating when it is not).
    Never-raise envelope.
    """
    params = _require_dict(params)
    try:
        return _set_diffraction_grating_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_diffraction_grating", _GRATING_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_diffraction_grating",
            getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> grating_write (L26)
        return error_envelope(
            "set_diffraction_grating", _GRATING_WRITE,
            f"unexpected engine fault authoring the diffraction grating ({exc!r}); "
            "refusing rather than shipping an unverified grating",
        )


def _set_diffraction_grating_impl(session, params):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    # --- Validate every param BEFORE any engine mutation. ---
    surface = _lc._require_int_index(params, "surface")
    # The geometry firewall (1 <= surface <= N-1) — refuses OBJECT(0); allows N-1.
    _lc._require_geometry_index(surface, n)
    # Explicitly REFUSE the IMAGE surface (a grating on the image plane is nonsensical;
    # the geometry firewall allows N-1, so we refuse it here LOUD — the set_mirror precedent).
    if surface == n - 1:
        raise ToolParamError(
            f"surface {surface} is the IMAGE surface; a diffraction grating on the image "
            "plane is nonsensical — refusing (choose an interior surface)"
        )

    lines_per_micron = _require_lines_per_micron(params.get("lines_per_micron"))
    order = _require_grating_order(params.get("order"))
    # reflective is a REQUIRED user declaration — a grating's behavior differs
    # fundamentally by type (reflective: a mirror substrate, the beam reflects AND
    # diffracts; transmissive: the beam passes through AND diffracts) and their
    # deviation differs fundamentally, so the tool will NOT guess. Refuse LOUD when
    # absent (key missing OR value None), BEFORE any engine mutation (zero-mutation on
    # refusal — this validates before ChangeType). A loud refusal tells the agent to
    # ASK the human, not retry with an implicit guess.
    if "reflective" not in params or params.get("reflective") is None:
        raise ToolParamError(
            "reflective is REQUIRED: state whether this grating is reflective (a "
            "mirror substrate — the beam reflects+diffracts) or transmissive (the beam "
            "passes through+diffracts); their deviation differs fundamentally and the "
            "tool will not guess. If the design spec does not state the grating type, "
            "ask the user."
        )
    reflective = _require_bool(params.get("reflective"), "reflective")

    # --- ChangeType -> DiffractionGrating (the cb_surface ChangeType recipe). ---
    grating_member = _surface_type_diffraction_grating(system)
    row = lde.GetSurfaceAt(surface)
    try:
        settings = row.GetSurfaceTypeSettings(grating_member)
        row.ChangeType(settings)
    except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> grating_write
        raise SurfaceWriteError(
            f"could not ChangeType surface {surface} to a diffraction grating ({exc!r}); "
            "refusing rather than authoring on an un-retyped surface",
            field="changetype", intended="DiffractionGrating", actual=None,
            surface=surface,
        ) from exc
    # Read-back-as-proof: the surface is genuinely a DiffractionGrating (a ChangeType that
    # silently no-opped would leave the old type whose Par cells are wrong — caught here
    # before any cell write).
    row = lde.GetSurfaceAt(surface)
    if not _is_diffraction_grating(row):
        raise SurfaceWriteError(
            f"surface {surface} is not a diffraction grating after ChangeType — the "
            "retype silently no-opped; refusing rather than writing grating cells to the "
            "wrong surface type",
            field="surface_type", intended="DiffractionGrating", actual=None,
            surface=surface,
        )

    # --- Write Par1 (Lines/µm) + Par2 (Diffract Order), both Double, read-back proven. ---
    _write_grating_cell(system, row, "lines_per_micron", float(lines_per_micron))
    # NIT-3: order is written via DoubleValue (Par2 IS a Double cell) — the integer-valued
    # check already happened at the tool level (a semantic guard, not a cell discriminator);
    # an integral float order (1.0) is accepted and stored as a Double.
    _write_grating_cell(system, row, "order", float(order))

    result = {
        "ok": True,
        "surface": surface,
        "lines_per_micron": _safe(float(lines_per_micron)),
        "order": _safe(float(order)),
        "reflective": reflective,
        "type": "DiffractionGrating",
    }

    # --- reflective=True: ALSO set Material="MIRROR" via the set_mirror logic + read-back. ---
    if reflective:
        # Delegate to set_mirror so the reflective flag inherits the honest Material
        # read-back (a degraded/silent-no-op Material write is caught there). A delegate
        # refusal converts to a structured raise -> grating_write (fail closed: never claim
        # a reflective grating whose MIRROR write did not take).
        mir = set_mirror(session, {"surface": surface})
        if not isinstance(mir, dict) or mir.get("ok") is not True:
            fam = mir.get("error_family") if isinstance(mir, dict) else None
            err = mir.get("error") if isinstance(mir, dict) else repr(mir)
            raise SurfaceWriteError(
                f"set_diffraction_grating authored the grating on surface {surface} but "
                f"the reflective Material='MIRROR' write did not take (delegate "
                f"family={fam!r}: {err}); refusing rather than claiming an unverified "
                "reflective grating",
                field="material", intended="MIRROR", actual=fam, surface=surface,
            )

    # A ChangeType to DiffractionGrating does NOT clear the
    # surface's Material on the live engine, so a grating authored OVER a pre-existing
    # MIRROR surface carries the MIRROR material forward — the surface IS a reflective
    # grating even when called with ``reflective=False``. The ``reflective`` flag only
    # tracks whether WE just set the mirror; it does NOT prove a transmissive grating. So
    # disclose the TRUE state: read back the actual Material after ChangeType (and after the
    # optional set_mirror, so reflective=True reads MIRROR too) and surface ``material`` +
    # ``was_mirror``. The return therefore never IMPLIES a transmissive grating when the
    # surface is in fact reflective. Read is fail-closed via _read_material (a Material read
    # THROW raises SurfaceWriteError -> grating_write, never a silent guess). Behavior is
    # UNCHANGED — this only DISCLOSES the true state, it does not clear the carried material.
    row = lde.GetSurfaceAt(surface)
    actual_material = _read_material(row)
    result["material"] = actual_material
    result["was_mirror"] = actual_material.strip().upper() == "MIRROR"

    return result


def _require_lines_per_micron(value):
    """Require a FINITE grating frequency ν > 0 (reject bool/non-number/<=0/NaN/inf)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"lines_per_micron (the grating frequency ν) must be a finite number, got "
            f"{type(value).__name__} {value!r}"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"lines_per_micron must be a finite number (inf/-inf/nan are non-physical), "
            f"got {value!r}"
        )
    if coerced <= 0.0:
        raise ToolParamError(
            f"lines_per_micron (the grating frequency ν) must be > 0, got {coerced}"
        )
    return coerced


def _require_grating_order(value):
    """Require an integer-VALUED diffraction order m (NIT-3 — a TOOL-level semantic guard).

    The Par2 cell is a Double (probe), so any finite value persists; but a non-integer order
    (1.5) is non-physical, so this is a TOOL-level semantic guard (reject 1.5 / NaN / inf /
    a string / None / a bool), NOT a cell-type discriminator. An integral float (``1.0``) is
    ACCEPTED per the "number = handler accepts integral float" contract and
    written as a Double; a true ``int`` (``1``, ``-1``, ``0``, ``2``) is accepted too. The
    order CAN be negative (−1 bends the opposite way) or zero (specular), so there is no
    sign/range bound here — only the integer-valued + finite requirement.
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"order (the diffraction order m) must be an integer-valued number, not a "
            f"bool ({value!r})"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"order (the diffraction order m) must be an integer-valued number "
            f"(e.g. -1, 0, 1, 2); got the non-integer-valued float {value!r} (a "
            "fractional order is non-physical)"
        )
    raise ToolParamError(
        f"order (the diffraction order m) must be an integer-valued number, got "
        f"{type(value).__name__} {value!r}"
    )


def _surface_type_diffraction_grating(system):
    """Resolve the live ``SurfaceType.DiffractionGrating`` member (the §1b authoring enum).

    Injected via ``_enum_types["SurfaceType"]`` (a FakeEnum with a ``DiffractionGrating``
    member) for unit tests; otherwise the live ``ZOSAPI.Editors.LDE.SurfaceType`` namespace.
    A resolution failure surfaces as a ``ToolParamError`` (a param-class problem), never an
    internal crash. Mirrors ``_cb._surface_type_coordinate_break``.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceType" in injected:
        from ..enums import _resolve_enum
        return _resolve_enum(injected["SurfaceType"], "DiffractionGrating")
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return _lde.SurfaceType.DiffractionGrating
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve SurfaceType.DiffractionGrating from "
            f"ZOSAPI.Editors.LDE: {exc}"
        )


def _is_diffraction_grating(row) -> bool:
    """True iff ``row`` is a diffraction-grating surface (keys on the Type substring).

    Mirrors ``_cb.is_coordinate_break``: keys on the ``DiffractionGrating`` substring of
    ``str(row.Type)`` (the same ``row.Type`` idiom). A ``row.Type`` read THROW ->
    ``SurfaceWriteError`` ("refuse rather than guess the type") so a guard never silently
    treats an unreadable row as a non-grating.
    """
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> grating_write
        raise SurfaceWriteError(
            f"could not read a surface Type ({exc!r}); cannot classify it as a "
            "diffraction grating — refusing rather than guessing",
            field="surface_type", intended=None, actual=None, surface=None,
        ) from exc
    return "DiffractionGrating" in type_name


# --- The grating-LOCAL DataType-keyed cell writer (NIT-1; the _cb_cells PATTERN). ---
def _grating_cell(system, row, param):
    """Fetch ``row.GetSurfaceCell(SurfaceColumn.ParN)`` for a grating ``param``, guarded.

    Resolves the ``ParN`` SurfaceColumn member off the live enum (reusing the CB layer's
    ``_surface_column_enum``, which is the LDE-wide Par-column enum — NOT the CB Par table),
    fetches the cell, and returns it. An unknown ``param`` or a fetch THROW -> a structured
    ``SurfaceWriteError`` ("refuse rather than guess").
    """
    if param not in _GRATING_PARAM_TO_COL:
        raise SurfaceWriteError(
            f"unknown grating parameter {param!r}; valid: "
            f"{list(_GRATING_PARAM_TO_COL)}",
            field="grating_param", intended=param, actual=None, surface=None,
        )
    col_name = _GRATING_PARAM_TO_COL[param]
    enum_type = _cb._surface_column_enum(system)
    try:
        from ..enums import _resolve_enum
        col_member = _resolve_enum(enum_type, col_name)
        return row.GetSurfaceCell(col_member)
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001 — a fetch THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not fetch the {param!r} ({col_name}) cell of a diffraction-grating "
            f"surface ({exc!r}); the cell layout is unreadable — refusing rather than "
            "guessing",
            field="grating_cell", intended=param, actual=None, surface=None,
        ) from exc


def _grating_cell_kind(cell):
    """Classify a LIVE grating cell -> ``"int"`` / ``"double"`` from ``cell.DataType``.

    The SAME Int/Double discriminator the merit/CB/tol layers key on (the PATTERN, NIT-1):
    Integer -> ``"int"``, else ``"double"``. A DataType read THROW -> ``SurfaceWriteError``.
    """
    try:
        return "int" if str(cell.DataType) == "Integer" else "double"
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read a grating cell DataType ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing",
            field="grating_cell_datatype", intended=None, actual=None, surface=None,
        ) from exc


def _read_grating_cell(system, row, param):
    """Type-aware READ of one grating Par cell (DataType-keyed, layout-verified)."""
    cell = _grating_cell(system, row, param)
    kind = _expect_grating_layout(cell, param)
    try:
        if kind == "int":
            return int(cell.IntegerValue)
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read the {kind} value of grating cell {param!r} ({exc!r}); the "
            "cell read is unverifiable — refusing rather than guessing",
            field="grating_cell_value", intended=None, actual=None, surface=None,
        ) from exc


def _expect_grating_layout(cell, param):
    """Verify the live cell's Header + DataType match the grating-table expectation.

    A live Header that does not match -> ``SurfaceWriteError`` (the layout drifted — refuse,
    do not write the wrong cell). A live DataType that disagrees with the table kind ->
    ``SurfaceWriteError`` (the table drifted from the engine — the ``_cb_cells``/``_tol_cells``
    drift guard). Returns the verified live kind.
    """
    expected_header = _GRATING_PARAM_TO_HEADER[param]
    try:
        live_header = str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read a grating cell Header ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing",
            field="grating_cell_header", intended=None, actual=None, surface=None,
        ) from exc
    if live_header != expected_header:
        raise SurfaceWriteError(
            f"diffraction-grating cell layout mismatch for {param!r}: expected Header "
            f"{expected_header!r} but the live cell Header is {live_header!r} — refusing "
            "rather than reading/writing the wrong cell",
            field="grating_cell_layout", intended=expected_header, actual=live_header,
            surface=None,
        )
    expected_kind = _GRATING_PARAM_TO_KIND[param]
    live_kind = _grating_cell_kind(cell)
    if live_kind != expected_kind:
        raise SurfaceWriteError(
            f"diffraction-grating cell {live_header!r} ({param}) is a {live_kind} cell "
            f"live but the grating catalog declared a {expected_kind} cell — the table "
            "drifted from the engine; refusing rather than the wrong accessor",
            field="grating_cell_datatype", intended=expected_kind, actual=live_kind,
            surface=None,
        )
    return live_kind


def _grating_readback_ok(intended, actual):
    """Read-back equality for a grating cell value (double tight tolerance with abs floor)."""
    return (
        actual is not None
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and math.isfinite(actual)
        and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=_GRATING_READBACK_ABS_TOL)
    )


def _write_grating_cell(system, row, param, value):
    """Type-aware WRITE of one grating Par cell, read-back-proven (the §1b firewall).

    Both grating cells are Double (probe), so the value is written via ``DoubleValue``
    (NIT-3: the order is a Double cell — never ``IntegerValue``). Steps mirror
    ``_cb_cells.write_cb_cell``: verify Header+DataType (a drifted layout RAISES), write the
    Double accessor (a write THROW -> surface_write), read it back and verify (a silent no-op
    / a collapse-to-zero -> surface_write). NEVER trusts the write without the read-back.
    """
    cell = _grating_cell(system, row, param)
    kind = _expect_grating_layout(cell, param)
    coerced = float(value)
    if not math.isfinite(coerced):
        raise SurfaceWriteError(
            f"grating cell {param!r} value {value!r} is non-finite — refusing pre-write",
            field="grating_cell_value", intended=value, actual=None, surface=None,
        )
    try:
        if kind == "int":
            cell.IntegerValue = int(coerced)
        else:
            cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not write the value {coerced!r} to grating cell {param!r} ({exc!r}); "
            "the engine rejected the write — refusing rather than shipping an unverified "
            "cell",
            field="grating_cell_write", intended=coerced, actual=None, surface=None,
        ) from exc

    actual = _read_grating_cell(system, row, param)
    if not _grating_readback_ok(coerced, actual):
        raise SurfaceWriteError(
            f"grating cell {param!r} write did not read back: wrote {coerced!r}, read "
            f"{actual!r} — the engine silently rejected the write (a no-op); refusing "
            "rather than shipping an unverified grating cell",
            field="grating_cell_value", intended=coerced, actual=actual, surface=None,
        )
    return actual


# =========================================================================== #
# §2. fold_beam — the θ/2 fold composition.
# =========================================================================== #
def fold_beam(session, params):
    """Fold the beam by φ at ``surface`` via an entry CB(tilt=φ/2) + MIRROR (Q-A).

    Params: ``surface`` (int, REQUIRED — where the fold mirror lands), ``angle``
    (float >0, the requested fold deviation φ in degrees), ``axis`` ('x'|'y',
    default 'x' = meridional y-z fold), ``direction`` (+1/−1, default +1 — which way
    to bend; maps to the tilt sign), ``restore_axis`` (bool, default False).

    Composition (ALL inside ONE SaveAs/LoadFile atomic checkpoint — nit 4b). BOTH branches
    insert THREE surfaces (``index_shift.delta = 3``); the 3rd differs:

      1. ``insert_surface`` at ``surface`` for the entry CB (the existing surface and
         every surface after it shift up by one; index map disclosed).
      2. ``insert_surface`` at ``surface+1`` for the mirror.
      3. ``insert_surface`` at ``surface+2`` for the 3rd CB (the RETURN CB when
         ``restore_axis=True``; the CORRECTION CB when ``restore_axis=False``).
      4. ``add_coordinate_break(surface)``: ``tilt_<axis> = direction · (φ/2)`` (the
         SEED — exact only for a +z-incident beam).
      4b. MEASURE-AND-SOLVE: measure the achieved UNSIGNED total deviation and march-then-
         secant-adjust the entry CB tilt to hit φ exactly on a COMPOUND same-plane fold (the
         beam enters the second CB rotated by the upstream fold, so the seed under-deviates).
         On-axis -> 0 iters. (An ORTHOGONAL entry frame is refused EARLY, before any insert.)
      5. ``set_mirror(surface+1)``.
      6a. (restore_axis=True) ``add_return_cb(entry_surface=surface,
         return_surface=surface+2)`` — the inverse (scale −1 pickups + Order
         flip) restores the downstream ORIENTATION to identity.
      6b. (restore_axis=False, fold-and-stay) the CORRECTION CB at ``surface+2``:
         the fold deviates the RAY by φ but rotates the downstream LOCAL frame by only φ/2,
         so a locally-authored downstream optic lands on the φ/2 bisector NOT the reflected
         ray (the §0 bug). A BOUNDED DIRECT-RESIDUAL correction rotates the downstream local
         +z onto the reflected ray (residual r = ∠(local +z, reflected ray); author tilt =
         direction · r; one-shot, slope 1.0; cap 2 steps).

    Then FALSIFY the achieved fold with TWO INDEPENDENT proofs (the load-bearing proof,
    L28): (1) the UNSIGNED global chief-ray deviation (incoming vs outgoing direction
    cosines RAGA/RAGB/RAGC across the mirror) == φ within ``_FOLD_TOL_DEG``; (2) an
    INDEPENDENT direction check — the raw-cosine in-plane cross-product SENSE sign matches
    the requested ``direction`` (NOT via the signed-deviation oracle, so the gate stays a
    regression net for the direction even though the solve drives the unsigned deviation —
    the circularity fix). For ``restore_axis=True`` ALSO assert the downstream
    rotation restored to identity; for ``restore_axis=False`` ALSO (a) the frame-axis
    residual ≤ tol and (b) ``beam_reaches_span(mirror, IMAGE, frame_required=True)`` reports
    the rays reach a downstream optic (a §0-style mis-place rolls back). On ANY mismatch /
    fault -> atomic LoadFile ROLLBACK + a structured ``{ok:false, family:...}``
    (``fold_unverified`` | ``fold_write`` | ``fold_skew_entry`` | ``fold_downstream_miss`` |
    ``fold_downstream_frame_unverified``).

    Returns ``{ok, surface, angle, achieved_deviation, signed_deviation, direction_ok,
    in_plane_delta, expected_direction_sign, axis, direction, restore_axis,
    inserted_surfaces, index_shift, mirror_surface, tilt_value, solve_iterations}``. For
    ``restore_axis=False`` ALSO ``post_mirror_cb_surface`` (the correction CB slot =
    mirror+1), ``frame_axis_residual_deg`` (the falsifier value), ``frame_corrections``
    (1 on every probed case), ``downstream_reaches`` (True), ``reach_span`` ([mirror,IMAGE]);
    for ``restore_axis=True`` ALSO ``return_cb_surface`` + ``restore_residual``.
    ``achieved_deviation`` is the UNSIGNED magnitude the solve drove to φ; ``direction_ok``/
    ``in_plane_delta`` are the INDEPENDENT direction proof (the raw-cosine sense; ``direction
    _ok`` is None for a ~180° reversal, whose direction is geometrically undefined);
    ``signed_deviation`` is the SIGNED in-plane deviation (REPORTING ONLY, not the proof);
    ``tilt_value`` is the FINAL (solved) entry CB tilt; ``solve_iterations`` is the number of
    measure-and-solve steps beyond the φ/2 seed (0 for an on-axis fold).
    """
    params = _require_dict(params)
    try:
        (surface, angle, axis, direction, restore_axis,
         replace_solve) = _validate_fold_params(session, params)
    except ToolParamError as exc:
        return error_envelope("fold_beam", _FOLD_PARAM, str(exc))
    except Exception as exc:  # noqa: BLE001 — a pre-mutation read fault -> fold_param
        return error_envelope(
            "fold_beam", _FOLD_PARAM,
            f"could not validate the fold request ({exc!r})",
        )

    # Part C — the EARLY skew-entry guard (BEFORE any insert/mutation/checkpoint): refuse
    # an entry beam that does NOT lie in the chosen fold plane (an orthogonal dogleg), since
    # the in-plane direction would be unverifiable. A degraded/unreadable entry direction is
    # NOT treated as skew (we cannot prove skew) — it falls through to the checkpointed body
    # whose own falsifier fails closed. Never raises.
    skew = _early_skew_refusal(session, surface, axis)
    if skew is not None:
        return skew

    # The whole insert + author + falsify runs inside ONE SaveAs/LoadFile checkpoint
    # (the heavier apply_lens_spec atomicity model — nit 4b): the delegated tools'
    # own partial-state ledgers become irrelevant once the outer LoadFile rolls back.
    return _fold_beam_checkpointed(
        session, surface, angle, axis, direction, restore_axis, replace_solve
    )


def _validate_fold_params(session, params):
    """Validate every fold param BEFORE any mutation (fold_param refusals). nit 7a.

    The surface-bounds FIREWALL (``_surface_in_geometry_range``) runs HERE, before
    any ``InsertNewSurfaceAt`` — an out-of-range insert HARD-CRASHES the engine, so
    the bound can NEVER be an exception handler (the insert_surface precedent). The
    fold inserts up to THREE surfaces starting at ``surface``, and ``add_return_cb``
    needs a co-located return downstream; we require enough room (``surface`` is a
    valid interior insert point and the system has an IMAGE surface after the fold).
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    # fold_beam's OWN validate-before-insert firewall (nit 7a): reuse the
    # geometry-range guard. ``surface`` must be a valid INTERIOR insert point — the
    # entry CB is inserted BEFORE it, so 1 <= surface <= N-1 (insert_surface's
    # _require_insert_at bound). We refuse the OBJECT (0) — a fold cannot precede the
    # object — and require an IMAGE surface to remain downstream of the fold.
    _cbs._surface_in_geometry_range(lde, surface)
    if surface < 1:
        raise ToolParamError(
            f"fold surface must be an interior surface (>= 1), got {surface}"
        )
    # The fold needs the inserted CB+MIRROR(+return) to sit BEFORE the IMAGE surface
    # (downstream optics live past the fold). With the entry CB inserted at ``surface``
    # the mirror lands at surface+1; require that surface stays interior (< the IMAGE
    # index n-1) so the post-mirror frame has a downstream surface to measure. A fold
    # ON the IMAGE surface (surface == n-1) is REFUSED — the entry CB would usurp the
    # image slot and the mirror would land at the image plane with NO downstream optic
    # (``>=`` not ``>``; set_mirror already refuses a mirror on the IMAGE
    # surface, so fold_beam is now consistent).
    if surface >= n - 1:
        raise ToolParamError(
            f"fold surface {surface} is out of range (1..{n - 2}); the fold needs a "
            "downstream IMAGE surface to redirect the beam into (a fold ON the image "
            "plane is nonsensical — choose an interior surface)"
        )

    angle = params.get("angle")
    angle = _require_fold_angle(angle)

    axis = params.get("axis", "x")
    if axis not in _AXIS_TO_TILT_PARAM:
        raise ToolParamError(
            f"axis must be 'x' (meridional y-z fold) or 'y' (sagittal x-z fold), got "
            f"{axis!r}"
        )

    direction = _require_direction(params.get("direction", 1))
    restore_axis = _require_bool(params.get("restore_axis", False), "restore_axis")
    # THE SOLVE-LOSS OPT-IN, half 1. The fold
    # delegates every retype to the CB doors, so the ``cb_solve_loss`` refusal reached
    # a ``fold_beam`` caller with a remedy — "pass replace_solve=true" — naming a
    # parameter ``fold_beam`` did not accept. A refusal with no route out is how a user
    # reaches for something worse.
    #
    # THE SAME VALIDATOR THE DOORS USE, not a second reading of what a deliberate
    # override is. A truthy-but-non-bool must refuse identically here and there; two
    # readings of "did the caller opt in" is how one of them starts accepting ``"no"``.
    from .optimize_variable import _require_replace_solve
    replace_solve = _require_replace_solve(params)

    return surface, angle, axis, direction, restore_axis, replace_solve


def _require_fold_angle(value):
    """Require a FINITE fold angle φ > 0 and <= 180 deg (reject 0/neg/NaN/>180)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"angle (the fold deviation φ) must be a finite number, got "
            f"{type(value).__name__} {value!r}"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"angle must be a finite number (inf/-inf/nan are non-physical), got "
            f"{value!r}"
        )
    if coerced <= 0.0:
        raise ToolParamError(
            f"angle (the fold deviation φ) must be > 0, got {coerced}"
        )
    if coerced > 180.0:
        raise ToolParamError(
            f"angle (the fold deviation φ) must be <= 180 deg, got {coerced} (a fold "
            "beyond a half-turn is non-physical for a single plane mirror)"
        )
    return coerced


def _require_direction(value):
    """Require a direction flag in {+1, −1}, coercing an integral float."""
    if isinstance(value, bool):
        raise ToolParamError(
            f"direction must be +1 or -1, not a bool ({value!r})"
        )
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            coerced = int(value)
        else:
            raise ToolParamError(
                f"direction must be +1 or -1, got non-integral float {value!r}"
            )
    else:
        raise ToolParamError(
            f"direction must be +1 or -1, got {type(value).__name__} {value!r}"
        )
    if coerced not in (1, -1):
        raise ToolParamError(
            f"direction must be +1 or -1 (which way to bend the beam), got {coerced}"
        )
    return coerced


def _require_bool(value, label):
    """Require a real bool (reject 0/1 ints + 'true'/'false' strings)."""
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{label} must be a bool (true/false), got {type(value).__name__} {value!r}"
        )
    return value


# --------------------------------------------------------------------------- #
# The atomic checkpointed body.
# --------------------------------------------------------------------------- #
def _fold_beam_checkpointed(session, surface, angle, axis, direction, restore_axis,
                            replace_solve=False):
    """Run the fold inside ONE SaveAs/LoadFile checkpoint (nit 4b). Never raises.

    On ANY fault inside the boundary — a delegated-tool refusal, an engine throw, OR
    the fold-deviation falsifier rejecting the achieved bend — the system is restored
    from the temp ``.zmx`` checkpoint (so a half-fold is NEVER left committed; the
    delegated tools' partial-state ledgers are moot once the outer LoadFile rolls
    back). The temp + its native ``.ZDA`` companion are reaped on every path (the
    apply_lens_spec #59 precedent).
    """
    import glob
    import os
    import tempfile

    from .optimize_merit_io import _unlink_quiet

    system = session.system

    checkpoint_path = None
    try:
        try:
            fd, checkpoint_path = tempfile.mkstemp(
                suffix=".zmx", prefix="optivibe_fold_ckpt_"
            )
            os.close(fd)
            # Capture a cheap pre-mutation snapshot (the SaveAs path already
            # touches the system) so the rollback can POST-RESTORE verify the LoadFile
            # actually restored the pre-fold form (a clean LoadFile that loaded-but-
            # didn't-restore is the silent-state-corruption class the checkpoint exists
            # to prevent). The snapshot is taken BEFORE SaveAs so it reflects the exact
            # pre-fold state the .zmx records.
            pre_snapshot = _fold_pre_snapshot(system)
            system.SaveAs(checkpoint_path)
        except Exception as exc:  # noqa: BLE001 — a checkpoint SaveAs throw -> fail-closed
            _reap_fold_checkpoint(checkpoint_path, glob, os, _unlink_quiet)
            checkpoint_path = None
            return error_envelope(
                "fold_beam", _FOLD_WRITE,
                f"could not checkpoint the system before folding ({exc!r}); the system "
                "was NOT mutated — nothing was applied",
                rolled_back=False, checkpoint=False, partial_state=False,
            )

        try:
            result = _fold_beam_impl(
                session, surface, angle, axis, direction, restore_axis, replace_solve
            )
            return result
        except _FoldUnverified as exc:
            # The falsifier rejected the achieved fold (or restore / downstream frame /
            # reach) -> atomic rollback. The family is the exception's own override when
            # set (fold_downstream_miss / fold_downstream_frame_unverified),
            # else the base fold_unverified.
            return _rollback_fold(
                system, checkpoint_path, pre_snapshot,
                family=exc.family or _FOLD_UNVERIFIED,
                reason=str(exc), extra=exc.extra,
            )
        except (ToolParamError, SurfaceWriteError) as exc:
            # A delegated-tool refusal / a structured write failure mid-author ->
            # atomic rollback (no half-fold left).
            return _rollback_fold(
                system, checkpoint_path, pre_snapshot, family=_FOLD_WRITE,
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — a generic engine throw -> rollback (L26)
            return _rollback_fold(
                system, checkpoint_path, pre_snapshot, family=_FOLD_WRITE,
                reason=f"unexpected engine fault folding the beam ({exc!r})",
            )
    finally:
        _reap_fold_checkpoint(checkpoint_path, glob, os, _unlink_quiet)


class _FoldUnverified(Exception):
    """The achieved fold deviation (or restore orientation / downstream frame) did not
    match the request.

    Carries an ``extra`` dict (``requested``/``achieved``/...) so the rollback
    envelope discloses the measured-vs-requested numbers honestly, plus an optional
    ``family`` override (``fold_downstream_miss`` /
    ``fold_downstream_frame_unverified`` route through the SAME rollback path as the
    base ``fold_unverified`` but disclose their own family).
    """

    def __init__(self, message, extra=None, family=None):
        super().__init__(message)
        self.extra = extra or {}
        self.family = family


def _fold_beam_impl(session, surface, angle, axis, direction, restore_axis,
                    replace_solve=False):
    """Insert + author + falsify (raises into the checkpointed caller on any fault).

    ─── THE SURFACE-ALLOCATION INDEX MAP (nit 4a) ────────────────────────────────
    Let ``s = surface`` be the requested fold surface and ``N`` the pre-insert count.
    InsertNewSurfaceAt(at) shifts every index >= at UP by one (probe P5). We insert
    the entry CB BEFORE ``s`` so the CB takes the ``s`` slot and the original ``s``
    surface (and all downstream) shift up:

      restore_axis = False  (3 inserts — fold-and-stay):
        insert at s        -> entry CB slot       = s
        insert at s+1      -> mirror slot          = s+1
        insert at s+2      -> CORRECTION CB slot    = s+2  (NEW — rotates the downstream
                                                            local frame onto the reflected
                                                            ray axis so locally-authored
                                                            downstream optics land on the
                                                            beam, not the φ/2 bisector)
        (the original surface ``s`` is now at s+3; IMAGE moved from N-1 to N+2)
        index_shift = {from: s, delta: +3}

      restore_axis = True   (3 inserts):
        insert at s        -> entry CB slot   = s
        insert at s+1      -> mirror slot      = s+1
        insert at s+2      -> return CB slot   = s+2
        (the original surface ``s`` is now at s+3; IMAGE moved from N-1 to N+2)
        index_shift = {from: s, delta: +3}

    The POST-INSERT numbers fed to the delegated tools are therefore CONSTANTS off
    ``s``: add_coordinate_break(surface=s); set_mirror(surface=s+1); and the 3rd CB
    (restore_axis -> add_return_cb(entry_surface=s, return_surface=s+2); fold-and-stay
    -> the correction CB at s+2). The add_return_cb pickup ``.Surface`` is BY-NUMBER
    and is wired from the post-insert entry number ``s`` in this SAME transaction (so
    the renumber + by-number-pickup desync hazard, P5, is owned here — no upstream
    insert happens after).
    ──────────────────────────────────────────────────────────────────────────────
    """
    s = surface
    # BOTH branches insert THREE surfaces. restore_axis=True inserts the
    # entry CB + mirror + RETURN CB (unchanged). restore_axis=False (fold-
    # and-stay) inserts the entry CB + mirror + CORRECTION CB (NEW) so the downstream
    # local frame follows the reflected ray (the §0 fix). index_shift.delta = 3 for both.
    n_inserts = 3

    # ---- Step 1-3: allocate the inserted surfaces (validate-before-insert per nit
    # 7a is already done in _validate_fold_params; each insert_surface ALSO re-guards
    # bounds before InsertNewSurfaceAt, so an out-of-range insert never reaches the
    # engine). Insert front-to-back at s, s+1[, s+2].
    # The composer's carrier for each delegate's MEASURED post-retype diff. It stays
    # EMPTY on the ordinary fold (a freshly inserted surface carries no non-default
    # solve, measured), so it costs the common case zero envelope keys.
    solve_loss = []
    inserted = []
    for offset in range(n_inserts):
        at = s + offset
        ins = _ls.insert_surface(session, {"at": at})
        # insert_surface returns {at, count} on success; it RAISES SurfaceWriteError
        # if the count did not grow (propagated to the checkpointed caller -> rollback).
        inserted.append(at)

    entry_surface = s
    mirror_surface = s + 1
    # The 3rd inserted slot at s+2 is the RETURN CB (restore_axis) OR the CORRECTION CB
    # (fold-and-stay) — same slot, authored differently + falsified differently.
    return_surface = s + 2 if restore_axis else None
    correction_cb_surface = s + 2 if not restore_axis else None

    # ---- Step 4: author the entry CB tilt = direction · (φ/2) on the probed axis.
    # deviation = 2·tilt, so tilt = φ/2; the axis picks tilt_x (y-z fold) or tilt_y
    # (x-z fold); ``direction`` picks the sign (the requested bend direction). The
    # signed-plane convention (+tilt_x -> -y, +tilt_y -> +x) is the probe map; the
    # tool surfaces the achieved SIGNED deviation, the user picks direction.
    tilt_param = _AXIS_TO_TILT_PARAM[axis]
    # SEED at the single-fold law tilt = φ/2 (deviation = 2·tilt holds when the beam
    # enters the CB along its local +z axis — the on-axis special case). On a COMPOUND
    # fold (a periscope/Z-fold OR an orthogonal dogleg) the beam enters the second CB's
    # frame already rotated by the upstream fold, so the seed tilt UNDER-deviates; the
    # measure-and-solve loop below Newton-adjusts the tilt to hit φ exactly (probe q2/q4).
    tilt_value = float(direction) * (angle / 2.0)
    # The unsigned MAGNITUDE oracle (acos) verifies deviation == φ; the SIGNED oracle
    # below verifies the in-plane bend went the RIGHT way (direction). The tool surfaces
    # both: ``achieved_deviation`` (magnitude) + ``signed_deviation`` (signed).
    # Author the entry CB EXACTLY ONCE and check ITS never-raise envelope; a refusal
    # converts to a structured raise so the checkpointed caller rolls back.
    cb_res = _cbs.add_coordinate_break(
        session, {"surface": entry_surface, tilt_param: tilt_value, "order": 0,
                  "replace_solve": replace_solve}
    )
    _raise_on_delegate_fail(cb_res, "add_coordinate_break (entry CB)")
    _collect_solve_loss(solve_loss, cb_res, "entry_cb")

    # ---- Step 5: set the mirror (honest primitive; signed thickness kept).
    mir_res = set_mirror(session, {"surface": mirror_surface})
    _raise_on_delegate_fail(mir_res, "set_mirror (fold mirror)")

    # ---- Step 5b: MEASURE-AND-SOLVE the entry CB tilt (the entry-frame-aware fix). ---
    # The seed tilt φ/2 is only correct for a +z-incident beam; on a compound fold the
    # achieved signed deviation is ``sign_axis·2·(tilt + θ_in)`` (θ_in = the incoming
    # local incidence carried in by the upstream fold), so the seed under-deviates. We
    # MEASURE the achieved signed deviation via the EXISTING oracle and Newton-adjust the
    # tilt cell by the residual / the analytic slope (sign_axis·2, machine-exact linear
    # per probe q2) until it converges, then the EXISTING falsify gate below confirms.
    # An on-axis single fold (θ_in=0) is already converged at the seed -> 0 extra iters.
    tilt_value, solve_iters = _solve_entry_tilt(
        session, entry_surface, mirror_surface, axis, direction, angle, tilt_param,
        tilt_value,
    )

    # ---- Step 6 (restore_axis only): the return CB inverse (negate tilt + flip
    # Order) restores the downstream ORIENTATION to identity. add_return_cb wires the
    # scale −1 pickups by NUMBER off the post-insert entry surface ``s`` IN THIS
    # transaction (no later upstream insert -> no desync, P5).
    if restore_axis:
        ret_res = _cbs.add_return_cb(
            session,
            {"entry_surface": entry_surface, "return_surface": return_surface,
             "replace_solve": replace_solve},
        )
        _raise_on_delegate_fail(ret_res, "add_return_cb (return CB)")
        _collect_solve_loss(solve_loss, ret_res, "return_cb")

    # ---- FALSIFY the achieved global chief-ray deviation across the mirror (L28). --
    # The MAGNITUDE gate (the unsigned acos deviation == φ).
    achieved = _measure_fold_deviation(session, entry_surface, mirror_surface)
    residual = abs(achieved - angle)
    if residual > _FOLD_TOL_DEG:
        raise _FoldUnverified(
            f"fold authoring did not achieve the requested deviation: requested "
            f"φ={angle} deg, achieved {achieved} deg (residual {residual} > "
            f"{_FOLD_TOL_DEG}); the geometry does not show the fold — rolling back "
            "rather than claiming an unverified fold",
            extra={"requested": angle, "achieved": _safe(achieved)},
        )

    # ---- The INDEPENDENT DIRECTION gate (fixes the circularity issue): a
    # fold of the correct MAGNITUDE but the WRONG signed direction (the whole job of the
    # ``direction`` param) is NOT caught by the unsigned acos above. This gate reads the
    # RAW outgoing-vs-incoming chief-ray cosine CHANGE in the fold plane DIRECTLY (NOT
    # through ``_measure_signed_fold_deviation``) and asserts its SIGN matches the
    # requested direction per the probed convention — so the gate stays an INDEPENDENT
    # regression net even though the SOLVE now drives the unsigned deviation (a
    # systematically-wrong signed oracle can no longer mask a wrong direction, because
    # this gate never consults it). axis 'x': +tilt_x (direction +1) bends the in-plane
    # y-component toward -y (Δm < 0); axis 'y': +tilt_y bends toward +x (Δl > 0).
    direction_ok, in_plane_delta, expected_dir_sign = _check_independent_direction(
        session, entry_surface, mirror_surface, axis, direction, angle, achieved
    )

    # ---- restore_axis: ALSO assert the downstream orientation restored to identity.
    restored_residual = None
    if restore_axis:
        restored_residual = _measure_downstream_identity_residual(
            session, return_surface
        )
        if restored_residual > _RESTORE_TOL_DEG:
            raise _FoldUnverified(
                f"restore_axis fold achieved the bend (φ={achieved}) but the downstream "
                f"orientation did NOT restore to identity (rotation residual "
                f"{restored_residual} > {_RESTORE_TOL_DEG}); the return CB did not "
                "re-collimate the frame — rolling back",
                extra={
                    "requested": angle, "achieved": _safe(achieved),
                    "restore_residual": _safe(restored_residual),
                },
            )

    # ---- fold-and-stay: the BOUNDED DIRECT-RESIDUAL frame correction +
    # the rays-reach span commit gate. The fold deviates the RAY by φ but rotates the
    # downstream LOCAL frame by only φ/2, so a locally-authored downstream optic lands on
    # the φ/2 bisector axis NOT the reflected ray (the §0 bug). The correction CB at
    # mirror+1 (= correction_cb_surface) rotates the downstream local frame the remaining
    # residual r = ∠(local +z, reflected ray) onto the reflected axis. The cheap frame-axis
    # gate runs HERE (after magnitude/direction, BEFORE the expensive batch-trace reach
    # gate, per §A.5). A degraded read / a non-converging residual raises _FoldUnverified
    # -> rollback (fail closed).
    frame_axis_residual = None
    frame_corrections = None
    downstream_reaches = None
    reach_span = None
    if not restore_axis:
        frame_axis_residual, frame_corrections = _correct_downstream_frame(
            session, mirror_surface, correction_cb_surface, axis, direction,
            replace_solve, solve_loss
        )
        # ---- The expensive rays-reach span commit gate (LAST — §A.5 ordering). The
        # span is [mirror, IMAGE]; an in-span optical surface the corrected fold does not
        # reach (a §0-style mis-place, or a frame corruption via frame_required) flips
        # reaches=False -> convert the dict verdict to a RAISE so the checkpointed caller
        # ROLLS BACK (never let reaches=False fall through to ok:true).
        lde = session.system.LDE
        image_index = int(lde.NumberOfSurfaces) - 1
        reach_span = [mirror_surface, image_index]
        verdict = _beam_reaches_span(
            session.system, mirror_surface, image_index, frame_required=True
        )
        downstream_reaches = bool(verdict.get("reaches"))
        if not downstream_reaches:
            raise _FoldUnverified(
                f"fold-and-stay achieved the bend (φ={achieved}) and corrected the "
                f"downstream frame (r={frame_axis_residual}) but the rays do NOT reach a "
                f"downstream optic in span {reach_span} — the locally-authored downstream "
                "optics land off the reflected beam; rolling back rather than shipping a "
                "fold-and-stay the rays miss",
                extra={
                    "requested": angle, "achieved": _safe(achieved),
                    "first_miss": verdict.get("first_miss"),
                    "reach_span": reach_span,
                    "frame_axis_residual_deg": _safe(frame_axis_residual),
                },
                family=_FOLD_DOWNSTREAM_MISS,
            )

    # The SIGNED in-plane deviation is surfaced for REPORTING ONLY (the direction PROOF is
    # the independent raw-cosine gate above, never this value). It is the signed in-plane
    # angle the user can read against the documented convention; well-defined for a same-
    # plane fold and the single on-axis fold (it wraps for a skew compound — see the
    # independent gate's ``in_plane_delta`` for the load-bearing direction read).
    signed_achieved = _measure_signed_fold_deviation(
        session, entry_surface, mirror_surface, axis
    )

    index_shift = {"from": s, "delta": n_inserts}
    out = {
        "ok": True,
        "surface": s,
        "angle": angle,
        # ``achieved_deviation`` is the UNSIGNED magnitude (acos) the solve drove to φ;
        # ``signed_deviation`` is the SIGNED in-plane deviation (reporting only). The
        # direction PROOF is ``direction_ok`` / ``in_plane_delta`` (raw cosines, the
        # INDEPENDENT gate) — never the signed oracle (the circularity fix).
        "achieved_deviation": _safe(achieved),
        "signed_deviation": _safe(signed_achieved),
        # The INDEPENDENT direction proof (Part B): the raw in-plane cosine CHANGE and the
        # expected sign the gate verified, so the direction verdict is auditable WITHOUT
        # the signed oracle.
        "direction_ok": direction_ok,
        "in_plane_delta": _safe(in_plane_delta),
        "expected_direction_sign": expected_dir_sign,
        "axis": axis,
        "direction": direction,
        "restore_axis": restore_axis,
        "inserted_surfaces": inserted,
        "index_shift": index_shift,
        "mirror_surface": mirror_surface,
        "entry_cb_surface": entry_surface,
        "tilt_param": tilt_param,
        # The FINAL solved entry-CB tilt (not the φ/2 seed) — the measure-and-solve loop
        # adjusted it to hit φ on a compound fold; 0 extra iters on an on-axis fold.
        "tilt_value": _safe(tilt_value),
        "solve_iterations": solve_iters,
    }
    if restore_axis:
        out["return_cb_surface"] = return_surface
        out["restore_residual"] = _safe(restored_residual)
    else:
        # fold-and-stay additive keys (the §7 envelope): the correction CB's
        # post-insert slot, the final frame-axis residual (the falsifier value), the
        # number of correction steps, and the rays-reach verdict + span.
        out["post_mirror_cb_surface"] = correction_cb_surface
        out["frame_axis_residual_deg"] = _safe(frame_axis_residual)
        out["frame_corrections"] = frame_corrections
        out["downstream_reaches"] = downstream_reaches
        out["reach_span"] = reach_span
    # ADDITIVE, and ABSENT when nothing was at risk (never an empty list). The ordinary
    # fold onto freshly inserted surfaces produces no entry at all, so every shipped
    # assertion over this envelope's key set is unchanged; the key appears exactly when a
    # caller opted a solve-bearing retype in and there is something MEASURED to report.
    if solve_loss:
        out["delegate_solve_loss"] = solve_loss
    return out


def _solve_entry_tilt(session, entry_surface, mirror_surface, axis, direction, angle,
                      tilt_param, seed_tilt):
    """MEASURE-AND-SOLVE the entry CB tilt so the achieved fold reaches φ (compound fix).

    Solves on the UNSIGNED TOTAL deviation (the 3-D angle between the incoming and
    outgoing global chief-ray directions across the mirror — ``_measure_fold_deviation``,
    the acos oracle), NOT the signed in-plane deviation. The seed ``φ/2`` is exact ONLY
    for a beam entering the CB along its local +z axis; on a compound fold (a periscope/
    Z-fold OR an orthogonal dogleg) the beam enters the second CB rotated by the upstream
    fold and the seed under-deviates (probe q2/q3). The total deviation == φ for ANY fold
    plane (probe ``entry_frame_fold``), so it is the robust quantity — the SIGNED in-plane
    deviation WRAPS / sign-flips for a skew entry frame (probe q3: signed_xz jumps
    −165.998 → +27.236 → −140.768 across tilt_y 10→30), which is why the signed
    solve diverged on the orthogonal dogleg.

    WHY THIS ALSO BREAKS THE CIRCULARITY: the earlier solve drove the
    tilt against ``_measure_signed_fold_deviation`` — the SAME oracle the final SIGNED gate
    used — so a systematically-wrong signed oracle was self-consistent (the gate was no
    longer an independent regression net). This solve consumes ONLY the unsigned acos
    oracle; the independent direction gate (``_check_independent_direction``) reads the raw
    cosines, so neither the solve nor the magnitude gate depends on the signed oracle.

    THE BRANCH PROBLEM (L26 self-adversary): for a SAME-plane refold the unsigned total
    deviation is V-shaped in tilt — it falls to zero where the fold cancels the entry
    incidence, then rises again — so the requested φ is reached at TWO tilts straddling
    the V minimum (probe q2: φ=60 at tilt 15 AND 75; the −y-bending branch the
    ``direction`` wants is 75). A plain secant from the φ/2 seed (on the descending arm)
    walks to the WRONG (15) branch. So we MARCH the tilt OUTWARD (increasing |tilt|, the
    seed's sign — the branch ``direction`` selects, since the seed sign IS the requested
    bend sign) until the deviation brackets φ on the rising arm, THEN secant inside the
    bracket. An on-axis fold (θ_in=0) is already at φ at the seed -> 0 iterations.

    The CB is ALREADY authored at ``seed_tilt`` (by the caller). NON-convergence within
    ``_FOLD_SOLVE_MAX_ITERS`` raises ``_FoldUnverified`` -> the checkpointed caller ROLLS
    BACK (we never ship a fold that did not reach φ). A degraded mid-solve measurement (the
    acos oracle raises ``_FoldUnverified``) propagates -> rollback (fail closed, never a
    fabricated tilt). Returns ``(final_tilt, iterations)`` (iterations beyond the seed).
    """
    tilt = float(seed_tilt)
    # The seed measurement (the CB is already authored at seed_tilt by the caller). A
    # degraded read here raises _FoldUnverified -> rollback (the oracle fails closed).
    achieved = _measure_fold_deviation(session, entry_surface, mirror_surface)
    # The φ/2 seed is the exact answer ONLY for an ON-AXIS fold (the entry beam along the
    # local +z axis, θ_in=0); accept it with 0 iterations ONLY then (backward-compat with
    # the single on-axis fold). For a COMPOUND fold the φ/2 seed can COINCIDENTALLY satisfy
    # the magnitude at the WRONG (inner, opposite-sense) branch of the V (probe q2: φ=45,
    # seed 22.5 already reads D=45 at the wrong-direction branch), so we must NOT shortcut —
    # we march to the OUTWARD (requested-direction) branch below. On-axis is detected by the
    # incoming chief ray ≈ +z (the upstream frame is unrotated). A degraded incoming read
    # raises -> rollback (fail closed); never silently treats a compound as on-axis.
    if abs(achieved - angle) <= _FOLD_SOLVE_TOL_DEG and _entry_is_on_axis(
        session, entry_surface
    ):
        return tilt, 0

    iterations = 0
    # The march OUTWARD direction = the sign of the seed (= the requested bend sign). A
    # zero seed (degenerate; the caller seeds direction·φ/2 with φ>0 so this never fires)
    # marches positive. The branch ``direction`` selects is the OUTWARD (larger-|tilt|) arm
    # of the V — beyond the deviation minimum (where the fold cancels the entry incidence)
    # the in-plane bend reverses sense, so the rising arm carries the requested direction.
    march_sign = 1.0 if tilt >= 0.0 else -1.0
    march_step = _FOLD_SOLVE_MARCH_DEG
    prev_tilt = tilt
    # Seed the "previous" deviation to +inf so the rising-arm test (achieved >= prev) is
    # FALSE on entry — the march always takes at least one real step and never spuriously
    # treats the seed as already on the rising arm (which would skip the V traversal and
    # land on the wrong, descending-arm branch for an over-deviating same-plane seed).
    prev_achieved = float("inf")

    # ---- Phase 1: MARCH outward until the unsigned deviation brackets φ ON THE RISING ARM
    # of the V — i.e. we are past the deviation minimum (current D > previous D, the arm is
    # rising in |tilt|) AND current D >= φ, so the bracket [prev, current] straddles the
    # OUTWARD-arm crossing (the branch ``direction`` selects). The seed may OVER- or UNDER-
    # deviate and may sit on either arm: marching outward (geometric ×2 growth -> O(log)
    # steps) first traverses any descending stretch to the minimum, then climbs the rising
    # arm to bracket φ there — so a SAME-plane compound lands on the requested-direction
    # branch regardless of φ (probe q2: seed 15 over-deviates to D=60, the φ=30 right-branch
    # is at tilt 60 PAST the minimum at 45 — not the wrong-sense tilt-30 crossing). Each step
    # re-writes the tilt cell (read-back proven) and re-measures; a degraded read raises ->
    # rollback. Bounded by the cap (a frame that never brackets fails closed).
    while not (achieved >= angle - _FOLD_SOLVE_TOL_DEG and achieved >= prev_achieved):
        if iterations >= _FOLD_SOLVE_MAX_ITERS:
            raise _FoldUnverified(
                _solve_nonconverge_msg(angle, achieved, iterations, "march"),
                extra=_solve_extra(angle, achieved, iterations),
            )
        new_tilt = tilt + march_sign * march_step
        if not math.isfinite(new_tilt):
            raise _FoldUnverified(
                f"the entry-frame fold solve produced a non-finite tilt ({new_tilt}) "
                "while bracketing — refusing rather than authoring a degenerate "
                "coordinate break — rolling back",
                extra=_solve_extra(angle, achieved, iterations),
            )
        prev_tilt, prev_achieved = tilt, achieved
        _rewrite_entry_tilt(session, entry_surface, tilt_param, new_tilt)
        tilt = new_tilt
        iterations += 1
        # Geometric growth (×2) reaches the bracket in O(log) steps, but CAP the step so a
        # leap cannot vault past the deviation PEAK (D maxes at 180 deg then falls again —
        # the triangle-wave overshoot that lands the bracket on the wrong, falling arm). The
        # cap keeps each step on one arm so the rising-arm bracket stays valid; an extreme
        # secondary fold that still cannot bracket within the cap fails closed (never silently
        # wrong).
        march_step = min(march_step * 2.0, _FOLD_SOLVE_MARCH_STEP_MAX)
        achieved = _measure_fold_deviation(session, entry_surface, mirror_surface)

    # ---- Phase 2: secant inside the bracket [prev_tilt, tilt] (the rising arm). The
    # slope is ESTIMATED from the bracket evals (not assumed) so a skew frame's unknown
    # slope is handled; a near-zero estimated slope (flat region / the V minimum) fails
    # closed rather than diverging.
    while abs(achieved - angle) > _FOLD_SOLVE_TOL_DEG:
        if iterations >= _FOLD_SOLVE_MAX_ITERS:
            raise _FoldUnverified(
                _solve_nonconverge_msg(angle, achieved, iterations, "secant"),
                extra=_solve_extra(angle, achieved, iterations),
            )
        slope = (achieved - prev_achieved) / (tilt - prev_tilt) if tilt != prev_tilt \
            else 0.0
        if abs(slope) < _FOLD_SOLVE_MIN_SLOPE:
            raise _FoldUnverified(
                f"the entry-frame fold solve hit a near-zero estimated slope ({slope}); "
                "the secant step is singular (a flat/grazing region or the deviation "
                "minimum) — rolling back rather than diverging",
                extra=_solve_extra(angle, achieved, iterations),
            )
        new_tilt = tilt + (angle - achieved) / slope
        if not math.isfinite(new_tilt):
            raise _FoldUnverified(
                f"the entry-frame fold solve produced a non-finite tilt ({new_tilt}); "
                "refusing rather than authoring a degenerate coordinate break — "
                "rolling back",
                extra=_solve_extra(angle, achieved, iterations),
            )
        prev_tilt, prev_achieved = tilt, achieved
        # Re-write the entry CB tilt cell (the cell path — the typed setter is a no-op,
        # P1) with read-back proof; a write fault raises CBCellError -> rollback.
        _rewrite_entry_tilt(session, entry_surface, tilt_param, new_tilt)
        tilt = new_tilt
        iterations += 1
        achieved = _measure_fold_deviation(session, entry_surface, mirror_surface)

    return tilt, iterations


def _entry_is_on_axis(session, entry_surface):
    """True iff the entry chief ray is ≈ +z (no upstream fold; the φ/2 seed is exact).

    Reads the raw incoming chief-ray direction at the entry CB and tests it is the +z axis
    within ``_FOLD_SKEW_ENTRY_TOL_DEG`` (the n-component ≈ 1, l/m ≈ 0). A degraded read
    raises ``_FoldUnverified`` (the oracle fails closed) — we NEVER silently treat an
    unreadable / compound frame as on-axis (which would wrongly accept the φ/2 seed at a
    coincidental wrong-branch magnitude).
    """
    l, m, n = _read_direction_cosines(session.system, entry_surface)
    off_axis = math.sqrt(l * l + m * m)
    return n > 0.0 and math.degrees(math.asin(min(1.0, off_axis))) <= \
        _FOLD_SKEW_ENTRY_TOL_DEG


def _solve_nonconverge_msg(angle, achieved, iterations, phase):
    return (
        f"the entry-frame fold solve did not converge within {_FOLD_SOLVE_MAX_ITERS} "
        f"iterations ({phase} phase): last achieved total deviation {achieved} deg vs "
        f"requested φ={angle} deg (residual {abs(achieved - angle)} > "
        f"{_FOLD_SOLVE_TOL_DEG}); the entry frame is too skew for the bounded solve — "
        "rolling back rather than shipping a fold that did not reach φ"
    )


def _solve_extra(angle, achieved, iterations):
    return {
        "requested": angle,
        "achieved": _safe(achieved),
        "solve_iterations": iterations,
    }


# --------------------------------------------------------------------------- #
# The bounded direct-residual downstream-frame correction.
# --------------------------------------------------------------------------- #
def _correct_downstream_frame(session, mirror_surface, correction_cb_surface, axis,
                              direction, replace_solve=False, solve_loss=None):
    """Rotate the downstream LOCAL frame onto the reflected ray (the §0 fix). Raises.

    The fold deviates the chief RAY by φ but rotates the downstream LOCAL coordinate
    frame by only φ/2, so a locally-authored downstream optic lands on the φ/2 bisector
    axis, NOT the reflected ray (the §0 bug, probe Q1). This authors the correction CB at
    ``correction_cb_surface`` (= ``mirror_surface + 1``) to rotate the downstream frame
    the remaining residual ``r = ∠(downstream local +z, reflected chief ray)`` onto the
    reflected axis.

    The residual is measured from TWO INDEPENDENT engine subsystems (no circularity, the
    lesson): ``downstream_local_plus_z`` = the 3rd COLUMN of the
    ``read_global_matrix`` rotation block (the surface-frame matrix subsystem) at the
    first downstream Standard surface AFTER the correction CB; ``reflected_chief_ray_dir``
    = the chief-ray global direction cosines (RAGA/RAGB/RAGC) at that SAME surface (the
    ray-trace subsystem).

    The correction is monotone-LINEAR with slope 1.0 (probe A2 live-pinned): the one-shot
    seed tilt = ``direction · r0`` zeroes ``r`` in ONE step. A bounded Newton step
    (``Δtilt = -r/slope``) is the defense-in-depth backstop. It is BIDIRECTIONAL (H-1): the
    step SIGN is derived from the OBSERVED pre/post-seed residual delta (the seed reduced r
    -> keep the seed's sense; the seed INCREASED r, an overshoot/wrong-sense -> REVERSE), so
    it recovers an overshoot as well as an undershoot rather than looping the wrong way. CAP
    = 2 corrections; still ``> tol`` ->
    ``_FoldUnverified(family=fold_downstream_frame_unverified)`` -> rollback. A degraded
    ``read_global_matrix`` / direction-cosine read FAILS CLOSED (raises -> rollback; never a
    fabricated ``r``).

    Returns ``(frame_axis_residual_deg, frame_corrections)`` (the final residual + the
    number of correction steps applied).
    """
    # The first downstream Standard surface AFTER the correction CB. The residual is read
    # there (probe: downstream_surf = correction_cb + 1). A pure CB does NOT bend the
    # global RAY direction (A3), so the reflected ray dir at this surface is the post-fold
    # direction; the local +z is the cumulative-frame +z including the correction CB.
    downstream_surface = correction_cb_surface + 1
    tilt_param = _AXIS_TO_TILT_PARAM[axis]

    # ---- Measure r0 (the uncorrected divergence) BEFORE authoring any correction tilt.
    # The correction CB is already inserted (slot allocated) but carries no tilt yet, so
    # the downstream frame still diverges by φ/2 — r0 is that divergence (on-axis: 45°).
    r0 = _measure_frame_axis_residual(session, downstream_surface)

    # ---- One-shot seed: author the correction CB tilt = direction · r0 (same sense as
    # the fold, probe A2 reducing-sign = +direction). Delegate to add_coordinate_break
    # (its never-raise envelope -> a structured raise on refusal -> rollback).
    seed_tilt = float(direction) * r0
    cb_res = _cbs.add_coordinate_break(
        session,
        {"surface": correction_cb_surface, tilt_param: seed_tilt, "order": 0,
         "replace_solve": replace_solve},
    )
    _raise_on_delegate_fail(cb_res, "add_coordinate_break (correction CB)")
    _collect_solve_loss(solve_loss, cb_res, "correction_cb")
    corrections = 1

    # ---- Falsify: re-measure r. If converged, DONE. Else ONE bounded Newton step.
    #
    # The Newton step is BIDIRECTIONAL (H-1): it must recover an OVERSHOOT, not only an
    # undershoot. The residual r is UNSIGNED (an acos angle >= 0), so a raw +direction step
    # only ever reduces an undershoot and would push an overshoot FURTHER wrong. We instead
    # derive the step SIGN from the OBSERVED residual delta across the seed: r0 (pre-seed) ->
    # r (post-seed). If the seed reduced the residual (r < r0) the +direction sense is the
    # reducing sense; if the seed INCREASED it (r > r0, an overshoot / wrong-sense seed) the
    # reducing sense is the OPPOSITE. ``step_sense`` carries that observed sign; the step
    # magnitude is the monotone-linear Newton step ``r / slope`` (probe A2: |slope| == 1.0).
    # On the happy path (probe A2: one-shot convergence) this branch is never entered.
    #
    # L26 self-adversary (what does the SIGNED step now PERMIT?): the signed step can, in a
    # pathological non-slope-1 regime, OSCILLATE between two residuals (overshoot -> reverse ->
    # overshoot the other way) instead of converging. That is BOUNDED: the 2-step cap
    # (_FRAME_CORRECTION_MAX_STEPS) caps it at one signed step, after which an unconverged r
    # FAILS CLOSED -> _FoldUnverified(fold_downstream_frame_unverified) -> atomic rollback. The
    # signed step never authors a non-finite tilt (the isfinite guard below) and never trusts a
    # write without read-back (_rewrite_cb_tilt). So the worst case is the SAME fail-closed
    # rollback as the old code on a true overshoot — never a silently-wrong shipped frame.
    r = _measure_frame_axis_residual(session, downstream_surface)
    cur_tilt = seed_tilt
    r_prev = r0          # the residual BEFORE the most-recent tilt change (the seed)
    last_step = seed_tilt - 0.0   # the most-recent tilt delta (the seed, from 0)
    while r > _FRAME_AXIS_TOL_DEG and corrections < _FRAME_CORRECTION_MAX_STEPS:
        # Derive the reducing sense from the OBSERVED pre/post-step residual delta rather
        # than hard-wiring +direction. If the last tilt step REDUCED r, keep stepping the
        # same way as that step; if it INCREASED r (overshoot / wrong sense), reverse.
        # ``slope_signed = +1`` means "more tilt in last_step's direction reduces r";
        # ``-1`` means "reverse last_step's direction reduces r". The signed Newton step is
        # ``Δtilt = -(r) * slope_signed * sign(last_step) / |slope|`` — i.e. move toward the
        # residual minimum regardless of which side of it the seed landed on.
        if r > r_prev:
            # the last step moved AWAY from the optimum -> reverse the step sense
            step_sense = -math.copysign(1.0, last_step)
        else:
            # the last step moved TOWARD the optimum (undershoot) -> keep the step sense
            step_sense = math.copysign(1.0, last_step) if last_step != 0.0 \
                else float(direction)
        step = step_sense * (r / _FRAME_CORRECTION_SLOPE)
        new_tilt = cur_tilt + step
        if not math.isfinite(new_tilt):
            raise _FoldUnverified(
                f"the downstream-frame correction produced a non-finite tilt "
                f"({new_tilt}); refusing rather than authoring a degenerate correction "
                "CB — rolling back",
                extra={"frame_axis_residual_deg": _safe(r),
                       "requested": 0.0, "achieved": _safe(r)},
                family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
            )
        _rewrite_cb_tilt(session, correction_cb_surface, tilt_param, new_tilt)
        last_step = step           # remember this step's tilt delta + sign
        cur_tilt = new_tilt
        r_prev = r                 # the residual BEFORE this step (for the next sign check)
        corrections += 1
        r = _measure_frame_axis_residual(session, downstream_surface)

    if r > _FRAME_AXIS_TOL_DEG:
        raise _FoldUnverified(
            f"the downstream-frame correction did not bring the frame-axis residual "
            f"within tolerance (r={r} > {_FRAME_AXIS_TOL_DEG}) after "
            f"{corrections} correction(s); the downstream local frame does NOT follow "
            "the reflected ray — rolling back rather than shipping a frame-uncorrected "
            "fold-and-stay",
            extra={
                "frame_axis_residual_deg": _safe(r),
                "requested": 0.0, "achieved": _safe(r),
                "frame_corrections": corrections,
            },
            family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
        )
    return r, corrections


def _measure_frame_axis_residual(session, downstream_surface):
    """Measure r = ∠(downstream local +z, reflected chief ray) at ``downstream_surface``.

    TWO INDEPENDENT subsystems (no circularity): the local +z is the 3rd COLUMN of the
    ``read_global_matrix`` rotation block ``(R[2], R[5], R[8])`` (the surface-frame matrix
    subsystem); the reflected chief ray is ``_read_direction_cosines`` (RAGA/RAGB/RAGC,
    the ray-trace subsystem). FAILS CLOSED: a ``read_global_matrix`` / cosine read THROW,
    a non-finite / sentinel element, or a near-zero norm raises ``_FoldUnverified`` (the
    residual is unverifiable — never a fabricated r).
    """
    system = session.system
    lde = system.LDE
    try:
        r_flat, _x, _y, _z = _cb.read_global_matrix(system, lde, downstream_surface)
    except Exception as exc:  # noqa: BLE001 — M-1: ANY frame-read throw (CBCellError or a
        # rarer escape from the wrapper machinery itself) routes to the FRAME family for
        # label fidelity (the spec wants frame-correction faults under the frame family),
        # never to the generic fold_write path. Fail-closed -> rollback (never a fabricated r).
        raise _FoldUnverified(
            f"could not read the downstream global frame at surface "
            f"{downstream_surface} ({exc!r}) to verify the frame correction; the residual "
            "is unverifiable — rolling back",
            family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
        ) from exc
    # The LOCAL +z axis in the global frame is the 3rd COLUMN of the row-major R block:
    # (R[2], R[5], R[8]) (read_global_matrix doc: LOCAL +z in global = 3rd column).
    plus_z = _normalize_vec3(
        (r_flat[2], r_flat[5], r_flat[8]), downstream_surface, "downstream local +z"
    )
    # The reflected chief ray (the INDEPENDENT subsystem). A degraded read raises a BASE
    # _FoldUnverified inside _read_direction_cosines; re-route it to the frame-unverified
    # family (this IS the frame-correction's own fail-closed path, not the entry-solve's).
    try:
        reflected = _read_direction_cosines(system, downstream_surface)
    except _FoldUnverified as exc:
        raise _FoldUnverified(
            f"could not read the reflected chief-ray direction at surface "
            f"{downstream_surface} ({exc}); the frame correction residual is unverifiable "
            "— rolling back",
            family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
        ) from exc
    return _angle_between(plus_z, reflected)


def _normalize_vec3(vec, surface, what):
    """Validate + normalize a 3-vector; FAIL CLOSED on a degraded read (raises)."""
    for comp in vec:
        if (not isinstance(comp, (int, float)) or isinstance(comp, bool)
                or not math.isfinite(comp)
                or abs(comp) >= _COSINE_SENTINEL_MAGNITUDE):
            raise _FoldUnverified(
                f"the {what} at surface {surface} read a degraded value {vec!r} "
                "(non-finite or sentinel-magnitude); the frame correction residual is "
                "unverifiable — rolling back",
                family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
            )
    norm = math.sqrt(sum(c * c for c in vec))
    if norm < 1e-9:
        raise _FoldUnverified(
            f"the {what} at surface {surface} has a near-zero norm {norm} (a collapsed "
            "frame); the frame correction residual is unverifiable — rolling back",
            family=_FOLD_DOWNSTREAM_FRAME_UNVERIFIED,
        )
    return tuple(c / norm for c in vec)


def _rewrite_cb_tilt(session, cb_surface, tilt_param, tilt_value):
    """Re-write a CB's tilt cell to ``tilt_value`` (cell path, read-back proven).

    Delegates to ``_cb.write_cb_cell`` (the DataType-keyed, read-back-firewalled cell
    writer) on the CB surface row — the ``_rewrite_entry_tilt`` idiom reused for the
    correction CB's bounded Newton re-author. A write/read-back fault raises ``CBCellError``
    (a ``SurfaceWriteError`` subclass) -> the checkpointed caller routes to atomic rollback.
    """
    lde = session.system.LDE
    row = lde.GetSurfaceAt(cb_surface)
    _cb.write_cb_cell(session.system, row, tilt_param, float(tilt_value))


def _rewrite_entry_tilt(session, entry_surface, tilt_param, tilt_value):
    """Re-write the entry CB's tilt cell to ``tilt_value`` (cell path, read-back proven).

    Delegates to ``_cb.write_cb_cell`` (the DataType-keyed, read-back-firewalled cell
    writer) on the entry CB surface row. A write/read-back fault raises ``CBCellError``
    (a ``SurfaceWriteError`` subclass) which the checkpointed caller routes to an atomic
    rollback. NEVER trusts the write without the cell's own read-back proof.
    """
    lde = session.system.LDE
    row = lde.GetSurfaceAt(entry_surface)
    _cb.write_cb_cell(session.system, row, tilt_param, float(tilt_value))


def _collect_solve_loss(sink, envelope, where):
    """Carry a delegate's MEASURED solve-loss diff up to the composer's envelope.

    Item 3. The CB doors
    report ``replaced_solves`` / ``preserved_solves`` — read from the row AFTER the retype,
    so a MEASUREMENT rather than a prediction — and the composition boundary was DROPPING
    it. A caller who opts a fold in to a destructive retype was therefore told nothing
    about what it destroyed, which is worse than the refusal it replaced.

    NEVER raises and is never a gate: it decorates. ``sink`` is ``None`` on any path that
    does not accumulate, and a delegate that reports nothing contributes nothing — so the
    ordinary fold onto a freshly inserted surface adds ZERO keys, which is the behaviour
    every existing assertion pins.
    """
    if sink is None or not isinstance(envelope, dict):
        return
    try:
        entry = {k: envelope[k] for k in
                 ("replaced_solves", "preserved_solves", "solves_unreadable_before")
                 if envelope.get(k)}
        if entry:
            entry["at"] = where
            sink.append(entry)
    except Exception:  # noqa: BLE001 — a decoration NEVER displaces the fold
        pass


def _raise_on_delegate_fail(envelope, what):
    """A delegated tool returns its OWN never-raise envelope; convert an ok:false to a
    structured raise so the checkpointed caller rolls back (no half-fold left).

    A delegated refusal is the FIRST committed sub-step's failure — but because the
    whole transaction sits inside the SaveAs/LoadFile checkpoint, the rollback undoes
    every earlier insert/author atomically (nit 4b). We carry the delegate's family +
    error into the raised message so the rollback envelope is diagnostic.
    """
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        fam = envelope.get("error_family") if isinstance(envelope, dict) else None
        err = envelope.get("error") if isinstance(envelope, dict) else repr(envelope)
        raise SurfaceWriteError(
            f"{what} failed during the fold (delegate family={fam!r}: {err}); the "
            "partial fold will be rolled back",
            field="fold_delegate", intended=what, actual=fam, surface=None,
        )


# --------------------------------------------------------------------------- #
# The fold-deviation falsifier (the load-bearing oracle).
# --------------------------------------------------------------------------- #
def _measure_fold_deviation(session, entry_surface, mirror_surface):
    """Measure the global chief-ray deviation across the mirror (Q-A). Degraded->raise.

    Reads the INCOMING chief-ray global direction cosines (RAGA/RAGB/RAGC at the entry
    CB surface) and the OUTGOING cosines (at the surface AFTER the mirror), then
    returns the angle between them in degrees (the achieved fold deviation). The
    chief ray is Hx=Hy=0, Px=Py=0 (the 9-arg GetOperandValue signature _layout_rays
    already uses).

    FAILS CLOSED on degraded data (this was a recurring failure class):
    a read THROW, a None/short read, a non-finite cosine, a sentinel-magnitude cosine
    (|value| >= 1e9), or a near-zero direction-vector norm -> raise (caught by the
    checkpointed caller -> rollback). NEVER returns a fabricated deviation.
    """
    system = session.system
    # The post-mirror surface (the frame the mirror redirects). With the mirror at
    # mirror_surface, the outgoing chief direction is read at mirror_surface (the ray
    # leaving the mirror) — but the global DIRECTION cosines at a surface report the
    # ray direction INTO that surface; the redirected direction is read at the surface
    # AFTER the mirror. We read incoming at the entry CB and outgoing at mirror+1.
    out_surface = mirror_surface + 1

    incoming = _read_direction_cosines(system, entry_surface)
    outgoing = _read_direction_cosines(system, out_surface)

    return _angle_between(incoming, outgoing)


def _measure_signed_fold_deviation(session, entry_surface, mirror_surface, axis):
    """Measure the SIGNED in-plane chief-ray deviation across the mirror.

    Unlike ``_measure_fold_deviation`` (the unsigned acos magnitude), this returns the
    SIGNED angle (deg) by which the chief ray turned IN THE FOLD PLANE, so a fold of the
    correct magnitude but the WRONG signed direction is distinguishable. Uses the SAME
    fail-closed direction-cosine reads (a degraded read raises ``_FoldUnverified`` ->
    rollback; NEVER fabricates a signed value).

    The plane + orientation are keyed on ``axis`` so the result matches the documented
    convention (signed_yz = -2·tilt_x for axis 'x'; signed_xz = +2·tilt_y for axis 'y'):

    - axis 'x' (y-z plane, components m=cosines[1], n=cosines[2]):
      ``signed = atan2(in_m·out_n − in_n·out_m, in·out)``.
    - axis 'y' (x-z plane, components l=cosines[0], n=cosines[2]):
      ``signed = atan2(in_n·out_l − in_l·out_n, in·out)``.
    """
    system = session.system
    out_surface = mirror_surface + 1
    incoming = _read_direction_cosines(system, entry_surface)
    outgoing = _read_direction_cosines(system, out_surface)
    return _signed_angle_in_plane(incoming, outgoing, axis)


def _signed_angle_in_plane(u, v, axis):
    """Signed angle (deg) from unit vector ``u`` to ``v`` in the ``axis`` fold plane.

    The 2-D signed angle ``atan2(cross, dot)`` over the two plane components; the cross
    orientation is fixed PER AXIS so the MEASURED signed deviation matches the LIVE engine
    recorded convention (NOT inverted to agree with
    any expected value — re-derived from the capture):

    - axis 'x' (y-z plane, components (m, n) = (u[1], u[2])): the capture shows a
      ``+tilt_x`` fold (incoming (0,0,1) -> outgoing (0, -0.7071, +0.7071)) has a signed
      y-z deviation of **-45 deg** (``deviation_signed_yz_deg`` / ``pos_tilt_x_bends_toward
      = -y``). The cross is oriented ``ub·va - ua·vb`` so this -y bend reads NEGATIVE,
      matching the capture (a naive ``ua·vb - ub·va`` would read +45 and FALSE-REJECT the
      geometrically-correct default fold — the earlier defect).
    - axis 'y' (x-z plane, components (l, n) = (u[0], u[2])): the capture shows a
      ``+tilt_y`` fold (outgoing (+0.7071, 0, +0.7071)) has a signed x-z deviation of
      **+45 deg** (``pos_tilt_y_bends_toward = +x``). The cross ``ub·va - ua·vb`` reads
      +45 for this +x bend, matching the capture.
    """
    if axis == "x":
        # y-z plane: components (m, n) = (u[1], u[2]). Capture: +tilt_x -> -y -> signed
        # NEGATIVE, so orient the cross ``ub·va - ua·vb`` (a -y bend reads negative).
        ua, ub, va, vb = u[1], u[2], v[1], v[2]
        cross = ub * va - ua * vb
    else:  # axis == "y": x-z plane, components (l, n) = (u[0], u[2]).
        ul, un, vl, vn = u[0], u[2], v[0], v[2]
        ua, ub, va, vb = ul, un, vl, vn
        # Capture: +tilt_y -> +x -> signed POSITIVE; the cross ``ub·va - ua·vb`` reads +45.
        cross = ub * va - ua * vb
    dot = ua * va + ub * vb
    return math.degrees(math.atan2(cross, dot))


def _early_skew_refusal(session, surface, axis):
    """Part C — refuse EARLY (pre-insert) if the entry beam is not in the chosen fold plane.

    Reads the chief-ray global direction at the requested fold ``surface`` BEFORE any
    insert and measures the component OUT of the requested fold plane (axis 'x' -> y-z
    plane -> the x-component l; axis 'y' -> x-z plane -> the y-component m). An entry skew
    (asin|out-of-plane component|) beyond ``_FOLD_SKEW_ENTRY_TOL_DEG`` -> a structured
    ``fold_skew_entry`` refusal (NO mutation; the magnitude would solve but the in-plane
    direction is unverifiable, so we never ship it). Returns the refusal envelope, or
    ``None`` to proceed.

    Never raises: a degraded/unreadable entry direction returns ``None`` (we cannot PROVE
    skew, so we fall through to the checkpointed body whose falsifier fails closed) rather
    than refusing on an unreadable read.
    """
    try:
        incoming = _read_direction_cosines(session.system, surface)
    except Exception:  # noqa: BLE001 — an unreadable entry direction -> cannot prove skew
        return None
    # The out-of-fold-plane component: x (l) for an axis-'x' (y-z) fold; y (m) for axis 'y'.
    out_of_plane = incoming[0] if axis == "x" else incoming[1]
    out_of_plane = max(-1.0, min(1.0, out_of_plane))
    skew_deg = math.degrees(math.asin(abs(out_of_plane)))
    if skew_deg <= _FOLD_SKEW_ENTRY_TOL_DEG:
        return None
    plane = "y-z (meridional)" if axis == "x" else "x-z (sagittal)"
    return error_envelope(
        "fold_beam", _FOLD_SKEW_ENTRY,
        f"fold_beam supports folds whose entry beam lies in the chosen fold plane; the "
        f"entry beam at surface {surface} is SKEW to the {plane} fold plane by "
        f"{skew_deg:.4f} deg (out-of-plane direction component {out_of_plane:.6f}). The "
        f"fold would reach the requested magnitude but its in-plane direction is "
        f"unverifiable for this skew (orthogonal) entry frame — fold in the matching plane "
        f"(axis {'y' if axis == 'x' else 'x'}), or restore the axis upstream first. No "
        "surfaces were inserted.",
        skew_deg=_safe(skew_deg), axis=axis,
    )


def _check_independent_direction(session, entry_surface, mirror_surface, axis,
                                 direction, angle, achieved):
    """The INDEPENDENT direction gate (Part B): verify the fold bent the REQUESTED way.

    Reads the RAW incoming and outgoing chief-ray global direction cosines DIRECTLY (the
    same fail-closed ``_read_direction_cosines``) and inspects the SIGN of the in-plane
    ROTATION SENSE — a raw 3-D cross-product component computed INLINE, NOT the signed-
    deviation oracle (``_measure_signed_fold_deviation`` / ``_signed_angle_in_plane``).
    This is the durable fix for the circularity issue: the solve drives the
    unsigned deviation and THIS gate is a separate raw-cosine computation, so a
    systematically-wrong signed oracle can no longer be self-consistent with both the solve
    and the gate (a globally-flipped ``_signed_angle_in_plane`` does not touch this check).

    The rotation SENSE = the fold-plane-normal component of ``incoming × outgoing`` (the
    sin-weighted turn direction; it distinguishes the two magnitude-equal branches a same-
    plane refold has — probe q2: φ=60 at tilt 15 AND 75, opposite turn senses — which the
    unsigned acos gate alone cannot):
    - axis 'x' (y-z fold plane, normal +x): sense = (incoming × outgoing)_x = m_in·n_out −
      n_in·m_out. +tilt_x (direction +1) turns the beam the +x-normal way (sense > 0).
    - axis 'y' (x-z fold plane, normal +y): sense = (incoming × outgoing)_y = n_in·l_out −
      l_in·n_out. +tilt_y (direction +1) turns it the +y-normal way (sense > 0).
    So the expected sense sign == ``direction`` for BOTH axes (live-pinned single-fold +
    same-plane). NOTE this is derived independently — it is NOT ``_AXIS_SIGNED_SIGN`` × the
    signed oracle.

    A sense magnitude below ``_FOLD_DIR_MIN_DELTA`` (the in-plane turn is degenerate — the
    entry-skew guard SHOULD have refused such a frame early, this is the defense-in-depth
    backstop) is UNVERIFIABLE -> raise ``_FoldUnverified`` (fail closed, never a guessed
    direction). A sense whose sign DISAGREES with ``direction`` -> raise (the beam bent the
    WRONG way). Returns ``(True, sense, expected_sign)`` only when the direction is proven.
    """
    system = session.system
    out_surface = mirror_surface + 1
    incoming = _read_direction_cosines(system, entry_surface)
    outgoing = _read_direction_cosines(system, out_surface)
    l_i, m_i, n_i = incoming
    l_o, m_o, n_o = outgoing
    # The fold-plane-normal component of incoming × outgoing (raw cosines; NOT the signed
    # oracle). axis 'x' -> the x-normal (y-z plane) sense; axis 'y' -> the y-normal (x-z).
    if axis == "x":
        sense = m_i * n_o - n_i * m_o
    else:
        sense = n_i * l_o - l_i * n_o
    expected_sign = int(math.copysign(1, direction))

    # A ~180-deg fold REVERSES the beam (incoming ≈ −outgoing): the rotation sense vanishes
    # by GEOMETRY (a half-turn has no in-plane direction — every plane reverses the beam
    # identically), so the direction param is not applicable. This is DISTINCT from a skew-
    # entry degeneracy (caught early by ``_early_skew_refusal``); the magnitude proof (==φ,
    # already passed) is the complete proof for a reversal. Disclose direction_ok=None.
    if abs(achieved - 180.0) <= _FOLD_TOL_DEG:
        return None, sense, expected_sign

    if abs(sense) < _FOLD_DIR_MIN_DELTA:
        raise _FoldUnverified(
            f"the fold's in-plane direction is UNVERIFIABLE: the in-plane rotation sense is "
            f"degenerate ({sense}, < {_FOLD_DIR_MIN_DELTA}); the entry frame is too skew to "
            "read the bend direction from the raw cosines — rolling back rather than claiming "
            "an unverified direction (an orthogonal entry should have been refused early)",
            extra={
                "requested": angle, "in_plane_delta": _safe(sense),
                "expected_direction_sign": expected_sign,
            },
        )
    measured_sign = 1 if sense > 0.0 else -1
    if measured_sign != expected_sign:
        raise _FoldUnverified(
            f"fold authoring achieved the requested MAGNITUDE (φ={angle}) but the beam bent "
            f"the WRONG way: the in-plane rotation sense is {sense} (sign {measured_sign}), "
            f"expected sign {expected_sign} for direction {direction} (independent raw-cosine "
            "cross-product, NOT the signed oracle) — rolling back rather than claiming a fold "
            "in the wrong direction",
            extra={
                "requested": angle, "in_plane_delta": _safe(sense),
                "expected_direction_sign": expected_sign,
            },
        )
    return True, sense, expected_sign


def _read_direction_cosines(system, surface):
    """Read the chief-ray global direction cosines (l, m, n) at ``surface``. Raises.

    Uses RAGA/RAGB/RAGC (the global direction-cosine analogue of RAGX/RAGY/RAGZ;
    same 9-arg signature, Q-A confirmed present). FAILS CLOSED: a read THROW, a
    non-finite / sentinel-magnitude component, or a near-zero norm raises
    ``_FoldUnverified`` (the deviation is unverifiable — never a fabricated pass).
    """
    mfe = system.MFE
    enum_type = _merit_operand_enum(system)
    try:
        raga = getattr(enum_type, "RAGA")
        ragb = getattr(enum_type, "RAGB")
        ragc = getattr(enum_type, "RAGC")
    except Exception as exc:  # noqa: BLE001 — the direction-cosine operands are missing
        raise _FoldUnverified(
            f"could not resolve the global direction-cosine operands RAGA/RAGB/RAGC "
            f"({exc!r}); the fold deviation is unverifiable — rolling back"
        ) from exc

    try:
        # chief ray: Hx=Hy=0, Px=Py=0; wave 1; ex=ey=0 (the 9-arg signature).
        l = mfe.GetOperandValue(raga, surface, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        m = mfe.GetOperandValue(ragb, surface, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        nval = mfe.GetOperandValue(ragc, surface, 1, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    except Exception as exc:  # noqa: BLE001 — a direction-cosine read THROW -> unverifiable
        raise _FoldUnverified(
            f"could not read the chief-ray global direction cosines at surface "
            f"{surface} ({exc!r}); the fold deviation is unverifiable — rolling back"
        ) from exc

    vec = (l, m, nval)
    for comp in vec:
        if (not isinstance(comp, (int, float)) or isinstance(comp, bool)
                or not math.isfinite(comp)
                or abs(comp) >= _COSINE_SENTINEL_MAGNITUDE):
            raise _FoldUnverified(
                f"the chief-ray global direction cosine at surface {surface} read a "
                f"degraded value {vec!r} (non-finite or sentinel-magnitude); the fold "
                "deviation is unverifiable — rolling back"
            )
    norm = math.sqrt(l * l + m * m + nval * nval)
    if norm < 1e-9:
        raise _FoldUnverified(
            f"the chief-ray global direction vector at surface {surface} has a near-"
            f"zero norm {norm} (a vignetted/collapsed ray); the fold deviation is "
            "unverifiable — rolling back"
        )
    return (l / norm, m / norm, nval / norm)


def _angle_between(u, v):
    """Angle (deg) between two UNIT direction vectors via a clamped dot product."""
    dot = u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    # Clamp for float noise so acos never NaNs at |dot| slightly > 1.
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _measure_downstream_identity_residual(session, return_surface):
    """Max-abs residual of the post-return rotation block vs identity (restore_axis).

    The return CB restores the downstream frame ORIENTATION to identity (Q-A). Reads
    the global rotation block of the surface AFTER the return CB and returns the
    max-abs element-wise residual against the 3x3 identity. A degraded read raises
    (via ``read_global_matrix``'s own success/shape guards) -> rolled back. NEVER
    fabricates a clean residual.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    post = return_surface + 1
    if post > n - 1:
        raise _FoldUnverified(
            f"the return CB at surface {return_surface} has no downstream surface to "
            f"verify the restored orientation (post={post} > IMAGE {n - 1}); rolling "
            "back rather than claiming an unverified restore"
        )
    try:
        measured_R, _x, _y, _z = _cb.read_global_matrix(system, lde, post)
    except _cb.CBCellError as exc:
        raise _FoldUnverified(
            f"could not read the post-return global frame to verify the restored "
            f"orientation ({exc!r}); rolling back"
        ) from exc
    identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    measured = list(measured_R)
    if len(measured) != 9:
        raise _FoldUnverified(
            "the post-return rotation block was not the 9-element flat block; the "
            "restore is unverifiable — rolling back"
        )
    worst = 0.0
    for mr, ie in zip(measured, identity):
        if not isinstance(mr, (int, float)) or isinstance(mr, bool) or not math.isfinite(mr):
            raise _FoldUnverified(
                "the post-return rotation block carried a non-finite element; the "
                "restore is unverifiable — rolling back"
            )
        worst = max(worst, abs(mr - ie))
    return worst


def _merit_operand_enum(system):
    """Resolve the live ``MeritOperandType`` enum (SHARED fake-injection seam)."""
    from .analysis_operand import _merit_operand_enum as _moe
    return _moe(system)


# --------------------------------------------------------------------------- #
# Rollback + reap (the apply_lens_spec precedent).
# --------------------------------------------------------------------------- #
def _fold_pre_snapshot(system):
    """A cheap pre-mutation snapshot for the post-restore verify (never raises).

    Captures (a) ``NumberOfSurfaces`` and (b) a stable downstream global rotation block
    + vertex (the LAST surface's frame) — enough that a LoadFile that loaded-but-didn't-
    restore (the inserted CB/mirror still committed -> a different surface count and/or a
    shifted downstream frame) is CAUGHT. Returns a dict, or ``None`` if the snapshot is
    unreadable (degrade: the post-restore verify then cannot prove the restore, so it
    treats an unverifiable snapshot conservatively — see ``_post_restore_fold_problems``).
    """
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable count -> no snapshot (degrade)
        return None
    # ``frame_captured`` records WHETHER the downstream frame portion landed. A count-only
    # (frame-unreadable) snapshot must NOT be treated as a fully-verifiable rollback proof
    # (a fail-open that was flagged): without the frame the post-restore verify can
    # only prove the count, NOT that the geometry restored. The flag lets
    # ``_post_restore_fold_problems`` fail CLOSED on the frame (partial_state) rather than
    # silently skip it.
    snap = {"n": n, "frame_captured": False}
    # A stable downstream frame read (the last surface). THROW-guarded -> omitted; the
    # count alone still catches a usurped-image / extra-surface non-restore.
    try:
        measured_R, x, y, z = _cb.read_global_matrix(system, lde, n - 1)
        snap["image_R"] = list(measured_R)
        snap["image_vertex"] = [x, y, z]
        snap["frame_captured"] = True
    except Exception:  # noqa: BLE001 — a frame read fault -> count-only snapshot
        pass
    return snap


def _post_restore_fold_problems(system, pre_snapshot):
    """Read the system AFTER LoadFile + compare to the pre-fold snapshot.

    A read-vs-read of the SAME system (the ``_post_restore_mismatches`` precedent in
    ``lens_spec``): the restore is faithful iff the post-LoadFile count + downstream
    frame match ``pre_snapshot``. Returns a list of mismatch strings (empty == faithful
    restore). NEVER raises — an unreadable post-restore state is itself a mismatch (we
    cannot prove the restore landed, so we must NOT claim ``rolled_back:true``).
    """
    if not isinstance(pre_snapshot, dict):
        # The pre-snapshot was unreadable -> we cannot prove the restore is faithful.
        return ["the pre-fold snapshot was unreadable; the rollback cannot be verified"]
    problems = []
    try:
        lde = system.LDE
        n_now = int(lde.NumberOfSurfaces)
    except Exception as exc:  # noqa: BLE001 — an unreadable count post-restore -> mismatch
        return [f"could not read the surface count after the rollback ({exc!r})"]
    if n_now != pre_snapshot.get("n"):
        problems.append(
            f"surface count after rollback is {n_now}, expected {pre_snapshot.get('n')} "
            "(the LoadFile did not restore the pre-fold surface count — a half-fold may "
            "still be committed)"
        )
    # MED (partial-snapshot fail-CLOSED): if the pre-snapshot could NOT capture the
    # downstream frame (a count-only snapshot), we cannot prove the geometry restored — a
    # LoadFile that restored the correct surface COUNT but a wrong downstream frame would
    # otherwise slip through with NO frame read-vs-read. Route to a conservative
    # partial_state (the same fail-closed posture the fully-absent snapshot takes), never a
    # silently-skipped frame verify.
    if not pre_snapshot.get("frame_captured"):
        problems.append(
            "the pre-fold snapshot could not capture the downstream global frame (count-"
            "only); the rollback's geometry restoration cannot be verified — disclosing a "
            "PARTIAL state rather than claiming a frame-verified clean rollback"
        )
        return problems
    if "image_R" in pre_snapshot:
        try:
            measured_R, x, y, z = _cb.read_global_matrix(system, lde, n_now - 1)
        except Exception as exc:  # noqa: BLE001 — an unreadable frame post-restore -> mismatch
            problems.append(
                f"could not read the downstream global frame after the rollback "
                f"({exc!r}); the restore cannot be verified"
            )
            return problems
        if not _frames_match(list(measured_R), pre_snapshot["image_R"]):
            problems.append(
                "the downstream global rotation block after rollback does not match the "
                "pre-fold frame (the LoadFile loaded but did not restore the geometry)"
            )
        if not _frames_match([x, y, z], pre_snapshot.get("image_vertex", [])):
            problems.append(
                "the downstream global vertex after rollback does not match the pre-fold "
                "frame (the LoadFile loaded but did not restore the geometry)"
            )
    return problems


def _frames_match(a, b, tol=1e-9):
    """Element-wise compare two flat numeric lists within ``tol`` (rollback verify)."""
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if (not isinstance(x, (int, float)) or isinstance(x, bool)
                or not isinstance(y, (int, float)) or isinstance(y, bool)
                or not math.isfinite(x) or not math.isfinite(y)
                or abs(x - y) > tol):
            return False
    return True


def _rollback_fold(system, checkpoint_path, pre_snapshot, *, family, reason, extra=None):
    """Restore from the checkpoint + POST-RESTORE read-back verify (never raises).

    A ``LoadFile`` THROW (the restore itself failed) -> ``partial_state:true`` + "the
    system is in an UNKNOWN state — reload your design .zmx". A LoadFile that returns
    cleanly but loaded-but-DIDN'T-restore (the inserted CB/mirror still committed
    while the .zmx claims a clean rollback) is caught by the post-restore read-vs-read
    against ``pre_snapshot`` -> ``rolled_back:false, partial_state:true`` (the
    ``lens_spec._post_restore_mismatches`` precedent). Only a VERIFIED-faithful restore
    -> ``rolled_back:true``. The envelope carries the requested-vs-achieved numbers when
    the falsifier supplied them.
    """
    fields = {
        "checkpoint": True,
        "rolled_back": False,
        "partial_state": False,
    }
    if extra:
        fields.update(extra)
    try:
        system.LoadFile(checkpoint_path, False)
    except Exception as exc:  # noqa: BLE001 — a rollback LoadFile throw -> partial state
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "fold_beam", family,
            f"fold_beam failed ({reason}); the ROLLBACK restore itself threw ({exc!r}) "
            "— the system is in an UNKNOWN state. Reload your design .zmx to recover.",
            **fields,
        )

    # POST-RESTORE verify: did the LoadFile actually bring the system back? A
    # clean LoadFile that did not restore would otherwise report a FALSE clean rollback
    # while a half-fold (orphaned CB/mirror) is still committed.
    restore_problems = _post_restore_fold_problems(system, pre_snapshot)
    if restore_problems:
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "fold_beam", family,
            f"fold_beam failed ({reason}); the checkpoint LoadFile returned cleanly but "
            f"the post-restore read-back does NOT match the pre-fold snapshot "
            f"({restore_problems}) — the rollback did not faithfully restore. The system "
            "may be in a PARTIAL state; reload your design .zmx to recover.",
            **fields,
        )

    fields.update({"rolled_back": True, "partial_state": False})
    return error_envelope(
        "fold_beam", family,
        f"fold_beam failed ({reason}); the system was ROLLED BACK to its pre-fold "
        "state via the temp checkpoint.",
        **fields,
    )


def _reap_fold_checkpoint(checkpoint_path, glob, os, _unlink_quiet):
    """Reap the temp ``.zmx`` placeholder AND the engine's ``.ZDA`` companion (#59).

    The live engine's ``SaveAs`` writes its native ``.ZDA`` binary (same stem), so
    unlinking only the ``.zmx`` leaks the ``.ZDA`` on every call. Glob the unique
    mkstemp token stem + remove everything matching (scoped to this token — never a
    foreign file). NEVER raises.
    """
    if not checkpoint_path:
        return
    _unlink_quiet(checkpoint_path)
    try:
        directory = os.path.dirname(checkpoint_path)
        base = os.path.basename(checkpoint_path)
        stem, _ext = os.path.splitext(base)
        for path in glob.glob(os.path.join(directory, stem + "*")):
            _unlink_quiet(path)
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel)."""
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_MIRROR_SPEC = ToolSpec(
    name="set_mirror",
    handler=set_mirror,
    required_params=("surface",),
    param_types={"surface": "number"},
    description=(
        "Make a surface a mirror by writing Material='MIRROR' (read-back proven). "
        "Honest primitive: you own the post-mirror thickness sign — the engine keeps "
        "whatever sign you wrote (a real reflective leg uses a NEGATIVE post-mirror "
        "thickness so the beam travels back), and this tool does ZERO sign bookkeeping. "
        "Refuses the object and image surfaces. fold_beam composes this into a full "
        "beam fold. See fold_beam, set_surface."
    ),
)

FOLD_BEAM_SPEC = ToolSpec(
    name="fold_beam",
    handler=fold_beam,
    required_params=("surface", "angle"),
    param_types={
        "surface": "number",
        "angle": "number",
        "axis": "string",
        "direction": "number",
        "restore_axis": "boolean",
        "replace_solve": "boolean",
    },
    description=(
        "Fold the beam by angle φ at a surface: inserts an entry coordinate break "
        "tilted φ/2 (a plane mirror deviates the beam by 2× its tilt) + a MIRROR, "
        "MEASURE-AND-SOLVES the tilt so a COMPOUND same-plane fold (a periscope/Z-fold, "
        "where the beam enters the second mirror already rotated within the SAME plane) "
        "still reaches φ exactly, then FALSIFIES the achieved bend with TWO independent "
        "proofs before claiming success: the UNSIGNED global chief-ray deviation == φ, AND "
        "an INDEPENDENT raw-cosine direction check that the beam bent the requested way "
        "(a fold of the right magnitude but the wrong direction, or one the geometry does "
        "not show, is rolled back, never reported). axis='x' folds in the meridional y-z "
        "plane, 'y' in the sagittal x-z plane; direction (+1/-1) picks which way to bend. "
        "Set restore_axis=True for the tilt-and-return case (adds a return CB that "
        "re-collimates the downstream frame to the input axis, the beam position "
        "displaced). Atomic: any fault rolls the whole insert+author back. Gotcha: an "
        "ORTHOGONAL dogleg (e.g. a tilt_x fold then a tilt_y fold) carries the entry beam "
        "OUT of the second fold's plane; its in-plane direction is unverifiable, so it is "
        "REFUSED early (fold_skew_entry) — fold in the plane the entry beam lies in, or "
        "restore_axis upstream first. Gotcha: inserting surfaces RENUMBERS everything from "
        "the fold surface up — see index_shift in the result. Gotcha: if a surface the "
        "fold retypes carries an authored solve, the coordinate-break door REFUSES "
        "(error_family cb_solve_loss) because a retype can DISCARD that relationship and "
        "nothing can put it back; pass replace_solve=true to proceed deliberately, and the "
        "result then carries delegate_solve_loss naming which solves were lost and which "
        "survived, MEASURED after the retype. See set_mirror, "
        "add_coordinate_break, describe_surfaces, render_layout."
    ),
)

SET_DIFFRACTION_GRATING_SPEC = ToolSpec(
    name="set_diffraction_grating",
    handler=set_diffraction_grating,
    required_params=("surface", "lines_per_micron", "order", "reflective"),
    param_types={
        "surface": "number",
        "lines_per_micron": "number",
        "order": "number",
        "reflective": "boolean",
    },
    description=(
        "Make a surface a diffraction grating: writes the Lines/µm (grating frequency ν) "
        "and Diffract Order (m) cells (read-back proven), and verifies the surface really "
        "retyped to a grating. Diffraction follows the grating equation "
        "sin θ_out = sin θ_in + m·λ·ν: order 0 is specular (no deviation), +m bends the "
        "beam one way and -m the other, and |deviation| grows with |m| and with ν. order "
        "must be an integer-valued number (1.0 is accepted, 1.5 is refused — a fractional "
        "order is non-physical); lines_per_micron must be > 0. reflective is REQUIRED (not "
        "optional): reflective=True is a reflective grating (a mirror substrate — the beam "
        "reflects AND diffracts; this also sets Material='MIRROR' via the mirror logic, "
        "read-back proven — you then own the negative post-grating thickness for the "
        "reflected leg, as with set_mirror), reflective=False is transmissive (the beam "
        "passes through AND diffracts). Their deviation differs fundamentally, so the tool "
        "will not guess the type — if the spec doesn't state reflective vs transmissive, "
        "ask the user. Refuses the object and image surfaces. See set_mirror, set_surface."
    ),
)

TOOL_SPECS = (SET_MIRROR_SPEC, FOLD_BEAM_SPEC, SET_DIFFRACTION_GRATING_SPEC)
