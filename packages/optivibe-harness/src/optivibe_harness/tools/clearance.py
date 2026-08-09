"""tools/clearance.py — the post-optimize manufacturability / detector-clearance audit.

ONE dispatchable READ-ONLY tool ``check_clearance`` (geometry-readouts cycle).
Fixes the iterative Cooke gap #1 (the DETECT-side net for a negative/thin
center or EDGE thickness) + Cassegrain gap #2 (a folded system's raw back-airgap
``back_focal_length`` is NOT the behind-primary clearance — report the GLOBAL BFD).

The audit is geometry from READ-BACK only (LDE radius/conic/thickness/semi-diameter
+ ``GetGlobalMatrix``); it NEVER mutates the LDE and NEVER raises past its boundary.
It REUSES ``_layout_geometry`` (the sag profile, the global-frame read, the fold
predicates, the role classifier) + ``_clearance_common`` (the edge-thickness math)
so the geometry reads can never drift from the renderer.

Structure:

1. **Fold detection** — ``folded = True`` if ANY surface (ALL indices, incl. 0 and
   n-1) is a coordinate-break OR a mirror, via the ONE shared predicate
   ``_layout_geometry.lde_is_folded`` (NOT the role classifier — which stamps
   object/image at the ends and would miss a CB at surface 0 / a mirror at the image
   surface). The SAME predicate ``get_first_order`` uses, so the two readout tools'
   fold decision can never drift. Drives the per-gap audit's applicability.
2. **Per-gap thickness audit (UNFOLDED only)** — for each real gap ``i -> i+1``
   (skip the object gap; skip + flag a gap with no finite-positive aperture):
   ``kind`` glass/air; ``center_thickness`` = LDE thickness_i; ``edge_thickness`` via
   the probe formula; a ``violation`` when center OR edge ``< threshold`` (min_glass
   for glass, min_air for air) or NEGATIVE. The back-airgap (last optic -> image) is
   audited as an air gap. A read-only audit REPORTS (``ok:true``), never refuses.
3. **Folded systems** — emit NO per-gap thickness VIOLATIONS (the -525.7 fold trap);
   report per-gap ``center_thickness`` as INFORMATIONAL ``folded_gaps`` + a ``note``.
4. **Global BFD (BOTH)** — ``image_global_z`` (slot [12]); ``behind_first_optic`` /
   ``behind_last_optic`` (image_global_z - first/last OPTICAL surface global z); the
   first/last optical surface numbers. A degraded global frame -> that field ``None``
   + a flag (never a bogus number).

Envelope: ``{ok, folded, gaps, violations, folded_gaps, global_bfd, flags}``.
``min_air``/``min_glass`` validated (finite >= 0; nan/inf/negative/bool ->
``clearance_param``); a total geometry-read failure -> ``clearance_unavailable``.

Live ZOS-API integration: exercised by the geometry-readouts live test (the Cooke
BEST 0-violations + ~1.12 flint edge; the Cassegrain folded behind_first_optic
~160); unit-tested against the ``_describe_render_fakes`` LDE/GetGlobalMatrix doubles.
"""
import math

from ..errors import ToolParamError
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _clearance_common as _cl
from . import _config_common as _cfg
from . import _layout_geometry as _geom
from ._analysis_common import error_envelope

_CL_FAMILY = "clearance_unavailable"   # a total geometry-read failure
_CL_PARAM = "clearance_param"          # a bad min_air/min_glass value

_DEFAULT_MIN_AIR = 0.5     # matches build_merit's MNCA air floor
_DEFAULT_MIN_GLASS = 1.0   # matches build_merit's MNCG glass floor


def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _finite_nonneg(value, label, default):
    """Validate a clearance threshold: a FINITE number ``>= 0`` (locked §5).

    Rejects ``bool`` (an int subclass — a client miswrite), a non-number, inf/-inf/
    nan, and a negative value -> ``ToolParamError`` (the caller envelopes it as
    ``clearance_param``). A missing key uses ``default``. Returns the float.

    THE INVARIANT THIS FUNCTION ESTABLISHES: together with
    ``resolve_floors``, **the ONLY exception class that can leave the resolver is
    ``ToolParamError``**. That single-class guarantee is what licenses
    ``workspace._effective_floors``' single-class ``except`` — and ``save_candidate``,
    which documents "NEVER raises", has NO outer net, so a second escaping class there
    is a broken contract, not a cosmetic nit.

    The coercion is guarded because the type gate ADMITS values ``float()`` cannot
    represent: a huge Python ``int`` (measured: ``10**400``) passes
    ``isinstance(value, (int, float))`` and then ``float(value)`` raises
    ``OverflowError``, which is NOT a ``ToolParamError``. Fixed HERE, at the root, and
    not at the two call sites — two patches for one root is how this project has
    repeatedly bred siblings.
    """
    if value is None:
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number >= 0, got {type(value).__name__} "
            f"{value!r}"
        )
    try:
        coerced = float(value)
    except Exception:
        # ROUND 3 — the CONVERGED finding (an external review + an internal one
        # which DEMONSTRATED it). This was ``except (OverflowError, ValueError)``,
        # which is NOT exhaustive over what ``float()`` can raise: the type gate above
        # admits ``int``/``float`` SUBCLASSES, and a subclass whose ``__float__``
        # misbehaves raises something else entirely. Both measured, by fuzzing the real
        # tool:
        #
        #     save_candidate min_air=<int subclass, __float__ -> str>  -> TypeError
        #     save_candidate min_air=<float subclass, __float__ raises> -> ZeroDivisionError
        #
        # Each escaped this resolver, escaped ``_effective_floors``' single-class
        # ``except``, and escaped ``save_candidate`` — which has NO outer net precisely
        # BECAUSE of the single-class guarantee documented above. So the guarantee was
        # false, and the docstring's categorical claim was an OVERCLAIM of exactly the
        # kind this cycle exists to close.
        #
        # Broadened rather than narrowing the claim: NOTHING else is inside this ``try``,
        # and every possible failure of ``float(value)`` means the same thing — the
        # caller named a threshold this module cannot apply — which is already the
        # ``ToolParamError`` answer. So widening makes the stated invariant TRUE instead
        # of documenting a hole. (Not reachable across the MCP boundary, where JSON
        # yields plain ints/floats; reachable in-process, and the claim was categorical.)
        raise ToolParamError(
            f"{label} must be a finite number >= 0 (this value cannot be "
            f"represented as a float), got {type(value).__name__}"
        )
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be finite >= 0 (inf/-inf/nan are not a clearance "
            f"threshold), got {value!r}"
        )
    if coerced < 0.0:
        raise ToolParamError(f"{label} must be >= 0, got {coerced}")
    return coerced


def resolve_floors(params):
    """The ``(min_air, min_glass)`` this module will apply. RAISES ``ToolParamError``.

    AND NOTHING ELSE. ``ToolParamError`` is the ONLY exception class that can leave
    this function — the guarantee ``workspace._effective_floors``' single-class
    ``except`` rests on, and therefore the guarantee that ``save_candidate`` (no outer
    net, documented "NEVER raises") keeps its contract. Test A5 asserts the CLASS over a
    pathological corpus, not a message.

    THE single resolver. ``check_clearance`` calls it, and
    ``workspace._effective_floors`` calls it so the audit RECORD a save writes and the
    guard a promote applies come from ONE acceptance set. Sharing the constants and the
    validation PRIMITIVE is NOT sharing a resolver — the defaulting, the param lookup
    and the tuple construction must live here too, or the producer and the guard have
    two acceptance sets that can diverge.

    ``_finite_nonneg`` always returns a ``float`` (``float(default)`` / ``float(value)``)
    and already rejects ``bool``, so the returned pair is ``(float, float)`` BY
    CONSTRUCTION — which is what lets the record's floors be validated as exact floats
    (``True == 1.0`` and ``False == 0.0`` hold in Python, so
    an int/bool record would otherwise satisfy an "exact" floor comparison).
    """
    params = _require_dict(params)
    min_air = _finite_nonneg(params.get("min_air"), "min_air", _DEFAULT_MIN_AIR)
    min_glass = _finite_nonneg(
        params.get("min_glass"), "min_glass", _DEFAULT_MIN_GLASS
    )
    return (min_air, min_glass)


# --------------------------------------------------------------------------- #
# Geometry read (guarded per-surface, never raises).
# --------------------------------------------------------------------------- #
def _read_rows(lde, n, system=None):
    """Read every surface's geometry + role (guarded). Returns a list of dicts.

    Each entry is the ``read_geometry_row`` shape (raw float radius/thickness/conic/
    semi_diameter + type_name/material/is_stop + ``aspheric_coefficients``) PLUS the
    shared ``role``. A surface whose read THROWS degrades to a sentinel dict marked
    ``ok:False`` (its gap is skipped + flagged) so one wedged surface never sinks the
    whole audit.

    Asphere S2 SAGMATH: ``system`` is threaded into ``read_geometry_row`` so an
    EvenAspheric row carries its 8 even-asphere coefficients (the edge audit models the
    FULL sag). A non-asphere row carries ``aspheric_coefficients = None``.
    """
    rows = []
    for i in range(n):
        try:
            row = _geom.read_geometry_row(lde, i, system=system)
            row["ok"] = True
            row["role"] = _geom.classify_role(
                i, n, row["type_name"], row["material"], row["is_stop"]
            )
            # S6 Delta-2: the SemiDiameter cell solve-type (Fixed=frozen / Automatic / Variable /
            # None=degraded). A guarded PURE read (fail-OPEN -> None), via the ONE shared
            # primitive both freeze + clearance consume (L30). A frozen (Fixed) SemiDiameter's
            # edge clearance is the FROZEN max-over-configs aperture, not the true per-config
            # auto — flagged below (it ANNOTATES, never changes the violation verdict).
            row["semi_solve"] = _cl.semi_solve_type_name(system, lde, i)
        except Exception:  # noqa: BLE001 — one wedged surface degrades; scan continues
            row = {
                "ok": False, "radius": float("nan"), "thickness": float("nan"),
                "conic": float("nan"), "semi_diameter": float("nan"),
                "type_name": "", "material": "", "is_stop": False, "role": "air",
                "aspheric_coefficients": None, "asphere_norm_radius": None,
                "asphere_power": None, "semi_solve": None,
            }
        rows.append(row)
    return rows


def _gap_kind(row_i):
    """``"glass"`` if surface ``i``'s material is a real glass else ``"air"`` (locked §2).

    A real glass = material NOT in {air "", CB "-", MIRROR}. A mirror/CB/air gap is
    an AIR clearance (it carries no glass thickness floor).

    An AUTHORABLE GRIN primitive (Gradient2/Gradient3) is a SOLID element
    whose FOLLOWING gap IS its gradient-medium body (the sequential representation: a
    surface's Thickness is the medium to the next surface: the GRIN at surf 2's
    body is gap 2->3). Its Material reads air-like "" -> classify glass so its edge/center
    is audited at min_glass. Keyed on the AUTHORABLE resolver (the probe-proven set), NOT
    the family recognizer -> a loaded NON-authorable member (Gradium/GridGradient/
    Gradient6...) is NOT classified here (un-probed representation) -> disclosed not-audited
    at the result level (§2.3.4). No try/except-to-material fall-through: a (near-impossible)
    resolver throw over a stored string must NOT silently classify a GRIN as air.
    ``_gap_kind`` only ever sees OK rows with a real ``type_name`` string (a degraded row
    is ``ok:False`` and is ``continue``'d upstream in ``_audit_gaps``), so this cannot throw.
    """
    from . import _grin_cells as _grin
    if _grin.grin_type_of_name(str(row_i.get("type_name", ""))) is not None:
        return "glass"
    mat = str(row_i.get("material", ""))
    if _geom._is_air_material(mat) or _geom._is_mirror(mat):
        return "air"
    return "glass"


# --------------------------------------------------------------------------- #
# Per-gap audit (UNFOLDED).
# --------------------------------------------------------------------------- #
def _audit_gaps(rows, n, min_air, min_glass):
    """Per-gap center + edge audit for an UNFOLDED system (locked §2).

    Walks each gap ``i -> i+1`` for ``i`` in ``1 .. n-2`` (skip the object gap at
    ``i=0``; the last real gap ``n-2 -> n-1`` IS the back-airgap, audited as air).
    Returns ``(gaps, violations, flags)``.
    """
    gaps = []
    violations = []
    flags = []
    for i in range(1, n - 1):
        ri = rows[i]
        rip1 = rows[i + 1]
        if not ri.get("ok", True):
            flags.append(f"gap {i}->{i + 1} skipped (surface {i} geometry unreadable)")
            continue
        if not rip1.get("ok", True):
            flags.append(
                f"gap {i}->{i + 1} skipped (surface {i + 1} geometry unreadable)"
            )
            continue
        kind = _gap_kind(ri)
        threshold = min_glass if kind == "glass" else min_air
        center = ri["thickness"]

        h = _cl.controlling_height(ri["semi_diameter"], rip1["semi_diameter"])
        edge_approximate = False
        approx_surfaces = []
        if h is None:
            # No finite-positive aperture on either bounding surface -> no edge to
            # measure. Report the center (still auditable) + flag the missing edge.
            edge = None
            flags.append(
                f"gap {i}->{i + 1} edge skipped (no finite-positive semi-diameter on "
                f"surface {i} or {i + 1}); center audited only"
            )
        else:
            # Asphere S2 SAGMATH: feed each bounding surface's even-asphere coefficients
            # into the edge math so the edge is modelled from the FULL sag (sphere +
            # conic base + polynomial), not the base only. A non-asphere row carries
            # ``aspheric_coefficients = None`` -> the conic-only byte-identical edge.
            edge = _cl.edge_thickness(
                center, ri["radius"], ri["conic"], rip1["radius"], rip1["conic"], h,
                coeffs_i=ri.get("aspheric_coefficients"),
                coeffs_ip1=rip1.get("aspheric_coefficients"),
                norm_i=ri.get("asphere_norm_radius"),
                power_i=ri.get("asphere_power"),
                norm_ip1=rip1.get("asphere_norm_radius"),
                power_ip1=rip1.get("asphere_power"),
            )
            # (FIX 2) Conic sphere-fallback honesty: if EITHER bounding surface used
            # a NON-ZERO conic whose sag radical went negative at the controlling
            # aperture h, sag_profile fell back to the paraxial sphere term — finite
            # but an APPROXIMATION that can UNDER-estimate a steep-conic sag (the real
            # edge may be THINNER than computed). Disclose it; the violation logic is
            # UNCHANGED (still flag on the computed edge).
            if _cl.conic_sphere_fallback_fired(ri["radius"], ri["conic"], h):
                approx_surfaces.append(i)
            if _cl.conic_sphere_fallback_fired(rip1["radius"], rip1["conic"], h):
                approx_surfaces.append(i + 1)
            if approx_surfaces:
                edge_approximate = True
                surf_list = ", ".join(f"S{s}" for s in approx_surfaces)
                flags.append(
                    f"edge thickness for gap S{i}->S{i + 1} is APPROXIMATE (conic sag "
                    f"beyond its radical at the controlling aperture on {surf_list} — "
                    "sphere fallback; the true edge may be thinner)"
                )

        # The worst (min) of the finite center/edge values is the offending number.
        candidates = [v for v in (center, edge) if v is not None and math.isfinite(v)]
        worst = min(candidates) if candidates else None
        is_violation = worst is not None and worst < threshold

        # S6 Delta-2: a per-gap FROZEN-SemiDiameter flag (Fixed-solve ONLY — a Variable/
        # Automatic aperture is the REAL clear aperture, not a frozen value). It ANNOTATES
        # (never changes the violation verdict): the frozen aperture is the LARGER max-over-
        # configs one, so a thin edge there is genuinely thin; the flag just discloses the
        # number is the frozen aperture, not the true per-config auto.
        frozen_i = (ri.get("semi_solve") == "Fixed")
        frozen_ip1 = (rip1.get("semi_solve") == "Fixed")
        frozen_semi = bool(frozen_i or frozen_ip1)
        frozen_surfaces = [s for s, f in ((i, frozen_i), (i + 1, frozen_ip1)) if f]

        gap = {
            "surface": i,
            "next_surface": i + 1,
            "kind": kind,
            "center_thickness": _safe(center),
            "edge_thickness": _safe(edge) if edge is not None else None,
            "edge_height": _safe(h) if h is not None else None,
            "edge_approximate": bool(edge_approximate),
            "threshold": threshold,
            "violation": bool(is_violation),
            "is_back_airgap": (i == n - 2),
            "frozen_semi": frozen_semi,
            "frozen_surfaces": frozen_surfaces,
        }
        gaps.append(gap)
        if frozen_semi:
            flags.append(
                f"gap S{i}->S{i + 1} edge is at a FROZEN (Fixed-solve) SemiDiameter "
                f"(surface(s) {frozen_surfaces}); the edge clearance is the FROZEN "
                "max-over-configs aperture, NOT the true per-config auto aperture — re-float "
                "with freeze_semidiameters(mode='auto') for the true clearance"
            )
        if is_violation:
            violations.append({
                "surface": i,
                "next_surface": i + 1,
                "kind": kind,
                "center_thickness": _safe(center),
                "edge_thickness": _safe(edge) if edge is not None else None,
                "worst": _safe(worst),
                "threshold": threshold,
                "is_back_airgap": (i == n - 2),
            })
    return gaps, violations, flags


def _folded_gaps(rows, n):
    """INFORMATIONAL per-gap center thickness for a FOLDED system (locked §3).

    NO violation flag (the raw LDE thickness is the fold direction, not a clearance
    — the -525.7 trap). Returns the ``folded_gaps`` list.
    """
    out = []
    for i in range(1, n - 1):
        ri = rows[i]
        if not ri.get("ok", True):
            continue
        out.append({
            "surface": i,
            "next_surface": i + 1,
            "kind": _gap_kind(ri),
            "center_thickness": _safe(ri["thickness"]),
            "role": ri.get("role"),
        })
    return out


# --------------------------------------------------------------------------- #
# Global BFD (BOTH folded + unfolded).
# --------------------------------------------------------------------------- #
def _optical_surfaces(rows, n):
    """The first/last OPTICAL (powered) surface numbers (locked §4).

    Optical = role in {glass, mirror} (NOT object/image/CB/flat-air-dummy/stop). A
    grating reads "glass" (catalog material) or "mirror" (reflective). Returns
    ``(first, last)`` surface numbers, or ``(None, None)`` if none exist (a degenerate
    all-flat-air / all-CB system).
    """
    optical = [
        i for i in range(n)
        if rows[i].get("ok", True) and _cl.is_optical_surface(rows[i])
    ]
    if not optical:
        return None, None
    return optical[0], optical[-1]


def _global_z(frames, i):
    """The global vertex Z (slot [12]) of surface ``i`` from a read frames list.

    A degraded / out-of-range frame -> ``None`` (the caller flags it, never a bogus
    number). ``read_global_frames`` already returns ``vertex=(x,y,z)`` per surface.
    """
    if i is None or not (0 <= i < len(frames)):
        return None
    fr = frames[i]
    if not fr.get("ok"):
        return None
    vertex = fr.get("vertex")
    if not vertex or len(vertex) != 3:
        return None
    z = vertex[2]
    if not (isinstance(z, (int, float)) and math.isfinite(z)):
        return None
    return float(z)


def _global_bfd(lde, rows, n):
    """The ``global_bfd`` block from ``GetGlobalMatrix`` (locked §4). Never raises.

    ``image_global_z`` (slot [12] of the image surface); ``behind_first_optic`` /
    ``behind_last_optic`` = image_global_z - first/last optical-surface global z. A
    degraded/unreadable frame -> that field ``None`` + a flag (never a bogus number).
    """
    flags = []
    frames = _geom.read_global_frames(lde, n)
    image_idx = n - 1
    image_z = _global_z(frames, image_idx)
    if image_z is None:
        flags.append(
            "global_bfd: the image surface global frame is degraded/unreadable; "
            "BFD distances unavailable"
        )

    first_opt, last_opt = _optical_surfaces(rows, n)
    first_z = _global_z(frames, first_opt)
    last_z = _global_z(frames, last_opt)

    if first_opt is None:
        flags.append(
            "global_bfd: no optical (powered) surface found; behind_first_optic/"
            "behind_last_optic unavailable"
        )
    else:
        if first_z is None:
            flags.append(
                f"global_bfd: the first optical surface ({first_opt}) global frame is "
                "degraded; behind_first_optic unavailable"
            )
        if last_z is None:
            flags.append(
                f"global_bfd: the last optical surface ({last_opt}) global frame is "
                "degraded; behind_last_optic unavailable"
            )

    behind_first = (
        image_z - first_z if (image_z is not None and first_z is not None) else None
    )
    behind_last = (
        image_z - last_z if (image_z is not None and last_z is not None) else None
    )

    return {
        "image_global_z": _safe(image_z) if image_z is not None else None,
        "behind_first_optic": _safe(behind_first) if behind_first is not None else None,
        "behind_last_optic": _safe(behind_last) if behind_last is not None else None,
        "first_optic_surface": first_opt,
        "last_optic_surface": last_opt,
        "image_surface": image_idx,
        "units": "mm",
    }, flags


def _min_gap_clearance(gaps):
    """The smallest finite clearance over a gap list (the per-config headline, D3).

    Reads each gap's ``center_thickness`` + (when present) ``edge_thickness``; returns
    the min over the FINITE numeric values (a ``safe_float`` string sentinel is skipped),
    or ``None`` when there is nothing finite to compare. The per-config headline that
    makes the ``"all"`` sweep's ``config_differs`` exact for a zoom whose gaps move.
    """
    candidates = []
    for gap in gaps or []:
        if not isinstance(gap, dict):
            continue
        for key in ("center_thickness", "edge_thickness"):
            v = gap.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and math.isfinite(v):
                candidates.append(float(v))
    return min(candidates) if candidates else None


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float

    try:
        return safe_float(value)
    except Exception:  # noqa: BLE001 — a non-float passes through verbatim
        return value


# =========================================================================== #
# check_clearance
# =========================================================================== #
def check_clearance(session, params):
    """Audit per-gap center/edge clearance + the global back-focal distance.

    Params: ``min_air`` (default 0.5) / ``min_glass`` (default 1.0) — the clearance
    floors (match build_merit). READ-ONLY (no LDE mutation). For an UNFOLDED system,
    per-gap center + edge thickness with violations (the back-airgap audited as air);
    for a FOLDED system, informational ``folded_gaps`` + a note (the raw LDE thickness
    is the fold, not a clearance). ALWAYS a ``global_bfd`` block (behind the first /
    last optical surface). NEVER raises past the boundary.

    ``config`` (None|int|"all") selects the configuration: None=current,
    int=that config, ``"all"`` sweeps every config (cheap geometry) into a ``per_config``
    vector + coverage reconcile + ``config_differs`` (the min edge clearance headline). A
    bad ``config`` -> ``clearance_param``.
    """
    params = _require_dict(params)
    try:
        # ONE resolver, shared with workspace._effective_floors.
        # Do NOT re-inline the _finite_nonneg calls here — two copies is two acceptance
        # sets, and the guard would then be able to disagree with the producer.
        min_air, min_glass = resolve_floors(params)
    except ToolParamError as exc:
        return error_envelope("check_clearance", _CL_PARAM, str(exc))

    config = params.get("config")

    def _grade(sess):
        return _impl(sess, min_air, min_glass)

    try:
        return _cfg.evaluate_over_configs(session, config, _grade)
    except ToolParamError as exc:  # a bad config / param raised deeper -> clearance_param
        return error_envelope("check_clearance", _CL_PARAM, str(exc))
    except Exception as exc:  # noqa: BLE001 — a total geometry-read failure -> envelope
        return error_envelope(
            "check_clearance", _CL_FAMILY,
            f"could not read the system geometry for a clearance audit ({exc!r})",
        )


def _impl(session, min_air, min_glass):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    rows = _read_rows(lde, n, system=system)
    # ONE shared fold predicate (geometry-readouts §1): scan ALL surfaces (incl. 0
    # and n-1) against the raw Type/Material. The role classifier stamps object/image
    # at the ends BEFORE the CB/mirror checks, so a CB at surface 0 / a MIRROR at the
    # image surface would read UNFOLDED — use the shared predicate so this can never
    # drift from get_first_order's fold decision.
    folded = _geom.lde_is_folded(lde, n)

    flags = []
    gaps = []
    violations = []
    folded_gaps = None
    note = None

    # A system needs OBJECT + >=1 optical + IMAGE (>=3 surfaces) for any gap to audit.
    if n < 3:
        flags.append(
            f"system has {n} surface(s); no optical gap to audit (need OBJECT + an "
            "optic + IMAGE)"
        )

    if folded:
        folded_gaps = _folded_gaps(rows, n)
        note = (
            "system is folded (a coordinate-break or mirror is present); the per-gap "
            "edge/clearance VIOLATION audit is unfolded-only (the raw LDE thickness is "
            "the fold direction, not a clearance) — use global_bfd.behind_first_optic "
            "for the detector clearance. Global-frame per-gap clearance for a fold is "
            "future work."
        )
    elif n >= 3:
        gaps, violations, gap_flags = _audit_gaps(rows, n, min_air, min_glass)
        flags.extend(gap_flags)

    bfd, bfd_flags = _global_bfd(lde, rows, n)
    flags.extend(bfd_flags)

    # SAGMATH sag disclosure (REAL geometry — replaces the S1 flag-only
    # ``asphere_sag_ignored``): the per-gap edge audit above now models the FULL sag
    # (sphere + conic base + the polynomial Σα₂ₙr²ⁿ term) for an EvenAspheric surface whose
    # coefficients were read (``aspheric_coefficients`` populated). So a steep asphere that
    # thins the edge below ``min_glass`` is a REAL ``violation:true`` (the detect-side
    # payoff), NOT just a warning.
    #
    # The disclosure splits the EvenAspheric surfaces by whether their sag was MODELLED:
    #   - MODELLED (coefficients read OK) -> ``asphere_sag_modelled`` (informational; the
    #     edge IS the full asphere sag, no caveat needed).
    #   - UNREADABLE (a degraded/throwing coefficient cell, or a wedged row that could be an
    #     asphere) -> FAIL-CLOSED: flag it approximate. The edge was computed from the
    #     sphere+conic base only because the polynomial term could not be read — a thin-edge
    #     violation may be MISSED. NEVER silently treated as a faithful asphere edge.
    asphere_sag_modelled = []
    asphere_sag_approximate = []
    for i in range(n):
        if not rows[i].get("ok", True):
            # The geometry read threw — we could not characterize this surface's Type. It
            # MIGHT be an asphere whose polynomial sag the audit could not model; disclose
            # conservatively (fail-closed).
            asphere_sag_approximate.append(i)
            continue
        if _asph.asphere_type_of_name(str(rows[i].get("type_name", ""))) is None:
            continue  # not a Tier-1 asphere
        # A Tier-1 asphere: MODELLED iff its coefficients read back (a real list); else
        # fail-closed approximate (a degraded/unreadable coefficient cell).
        coeffs = rows[i].get("aspheric_coefficients")
        if isinstance(coeffs, (list, tuple)) and not rows[i].get(
            "coefficients_unreadable", False
        ):
            asphere_sag_modelled.append(i)
        else:
            asphere_sag_approximate.append(i)

    for s in asphere_sag_modelled:
        flags.append(
            f"surface {s} is an even asphere; its edge is modelled from the FULL sag "
            "(sphere + conic + polynomial term)"
        )
    for s in asphere_sag_approximate:
        if not rows[s].get("ok", True):
            flags.append(
                f"surface {s} could not be characterized (geometry read failed); it MAY "
                "be an even asphere whose polynomial sag the edge audit could not model — "
                "a thin-edge violation may be MISSED; verify in OpticStudio"
            )
        else:
            flags.append(
                f"surface {s} is an even asphere but its coefficients could not be read; "
                "the edge was computed from the sphere+conic base ONLY (the polynomial "
                "term is unavailable) — a thin-edge violation may be MISSED; verify in "
                "OpticStudio"
            )

    # (clearance-provenance, DQ-6) SAG-MODEL APPLICABILITY disclosure. The per-gap
    # edge audit computes every sag from the radius/conic/polynomial model; for a surface
    # whose TYPE that model was never written for, the bounding gaps were computed from
    # the base model instead of the real geometry. the probe measured this failing in
    # BOTH directions on a Tilted surface (a real 0.926 mm violation erased to 2.600 AND a
    # false 0.300 mm one invented), so the disclosure is CONSUMED by the save/promote
    # classifier, not merely emitted. A degraded row is SKIPPED here — it is already in
    # ``asphere_sag_approximate`` (the shipped channel); never double-reported.
    sag_model_unfaithful = []
    for i in range(n):
        if not rows[i].get("ok", True):
            continue        # already in asphere_sag_approximate — the shipped channel
        if _geom.sag_model_is_faithful(rows[i].get("type_name")) is not True:
            sag_model_unfaithful.append(i)
    # The claim is CONDITIONAL ("where it bounds"). This scan runs
    # over ALL surfaces (``range(n)``) while ``_audit_gaps`` walks only ``1 .. n-2``, so a
    # named surface may bound NO audited gap — reproduced on surface 0 ``Tilted`` on a
    # design with gaps 1->2 and 2->3 only, and the old unconditional "the gaps bounding it
    # were computed" asserted a computation that never occurred. Scoping the EMISSION
    # would empty this list on a fold (a fold never calls ``_audit_gaps``, so ``gaps ==
    # []``) and destroy the paired assertion, so scoping is deferred with its
    # measurement attached.
    # This string and ``workspace._COVERAGE_TYPE`` are TWO COPIES OF ONE CLAIM — a
    # hazard — and both were edited together; a test parametrised over both sites pins it.
    for s in sag_model_unfaithful:
        flags.append(
            f"surface {s} is a {rows[s].get('type_name')}; its geometry is NOT one the "
            "radius/conic/polynomial sag model this audit uses was written for, so where "
            "it bounds an audited gap that gap's edge was computed from that base model "
            "instead of its real geometry — a violation may be MISSED and a false one may "
            "be REPORTED (measured for a Tilted surface: a real 0.926 mm violation erased, "
            "a false 0.300 mm one invented). Verify in OpticStudio."
        )

    # GRIN disclosure: a GRIN surface's INTERNAL index profile is not
    # drawn/audited — the audited geometry here is the Standard sphere/conic base (live-
    # probed: the index profile does NOT perturb the sag, residual 0.0, so the clearance edge audit
    # is CORRECT on the base geometry; the flag is informational). NO ``_modelled``/
    # ``_approximate`` split (index profile ⊥ sag). A degraded/unreadable row routes to the
    # EXISTING degraded channel above — never claimed GRIN. Additive key + one flag, emitted
    # ONLY when non-empty (a non-GRIN system is byte-for-byte unchanged). Fail-safe: a
    # resolver throw -> no GRIN disclosure (never crashes the read-only audit).
    grin_index_profile_not_drawn = []
    try:
        from . import _grin_cells as _grin
        for i in range(n):
            if not rows[i].get("ok", True):
                continue  # degraded -> the existing degraded channel, never claimed GRIN
            if _grin.grin_type_of_name(str(rows[i].get("type_name", ""))) is not None:
                grin_index_profile_not_drawn.append(i)
    except Exception:  # noqa: BLE001 — a GRIN resolver hiccup -> no GRIN disclosure, never raise
        grin_index_profile_not_drawn = []
    if grin_index_profile_not_drawn:
        # This flag makes NO edge/center-audited claim — that claim is
        # keyed on the FAMILY/type surface list, which would falsely assert "audited" for a
        # primitive whose gap was SKIPPED (it lands in grin_not_audited, reason gap_unaudited).
        # The per-surface audited/not-audited truth is the GAP-DERIVED structured evidence
        # (grin_geometric_audit.audited vs grin_not_audited); this flag only discloses the
        # index profile (true for every recognized primitive regardless of gap coverage).
        flags.append(
            "GRIN: internal index profile not drawn (surfaces "
            f"{grin_index_profile_not_drawn}); bulk-index manufacturability is not audited. "
            "See grin_geometric_audit for the surfaces whose edge/center clearance WAS audited "
            "at min_glass (solid-medium policy) and grin_not_audited for any that were not"
        )

    # Positive evidence + unconditional not-audited disclosure, derived from the ACTUAL
    # evaluated gap records, NOT the
    # family-recognized surface list: a classified primitive whose gap was SKIPPED (its next
    # surface unreadable -> not in ``gaps``) is placed in ``grin_not_audited`` (never counted
    # as "audited as solid"). A recognized FAMILY member that is NOT an authorable primitive
    # (a loaded Gradium/GridGradient/…) is UNCONDITIONALLY disclosed not-audited (its
    # representation is un-probed — never classified glass, §2.3.4). Both keys emitted ONLY
    # when non-empty (a non-GRIN system is byte-for-byte unchanged). Fail-safe: the assembly
    # never breaks the read-only audit.
    grin_audited = []
    grin_not_audited = []
    try:
        from . import _grin_cells as _grin
        gap_by_surface = {g["surface"]: g for g in gaps}
        for i in range(n):
            r = rows[i]
            if not r.get("ok", True):
                continue                                   # degraded -> existing degraded channel
            tn = str(r.get("type_name", ""))
            is_prim = _grin.grin_type_of_name(tn) is not None
            is_fam = _grin.grin_family_type_of_name(tn) is not None
            if is_prim:
                g = gap_by_surface.get(i)
                if (g is not None and g.get("kind") == "glass"
                        and g.get("threshold") == min_glass):
                    grin_audited.append({
                        "surface": i, "next_surface": g["next_surface"], "kind": "glass",
                        "threshold": min_glass, "center_thickness": g["center_thickness"],
                        "edge_thickness": g["edge_thickness"], "violation": g["violation"],
                    })
                else:  # a classified primitive whose gap was SKIPPED/unmatched -> NOT audited
                    grin_not_audited.append(
                        {"surface": i, "type": tn, "reason": "gap_unaudited"})
            elif is_fam:  # family member, NOT authorable -> un-probed -> UNCONDITIONAL not-audited
                grin_not_audited.append(
                    {"surface": i, "type": tn, "reason": "grin_family_non_authorable"})
    except Exception:  # noqa: BLE001 — evidence assembly never breaks the read-only audit
        grin_audited, grin_not_audited = [], []

    if grin_audited:
        flags.append(
            f"GRIN: element(s) at surface(s) {[e['surface'] for e in grin_audited]} are a "
            f"SOLID medium, audited (edge AND center) at min_glass ({min_glass}) under the "
            "solid-medium geometric policy (tunable via min_glass; NOT a catalog-glass "
            "assertion). Their internal index profile is not drawn."
        )
    if grin_not_audited:
        flags.append(
            f"GRIN: surface(s) {[e['surface'] for e in grin_not_audited]} are a recognized "
            "GRIN family member NOT covered by the solid-medium geometric audit "
            "(loaded/non-authorable representation, un-probed, OR its gap was skipped) — NOT "
            "audited for edge/center manufacturability. Treat their edge and center "
            "clearance as UNKNOWN and check them in OpticStudio."
        )

    # (MCE, D3) The per-config divergence headline: the SMALLEST finite
    # gap clearance (min over each gap's center+edge). A zoom's gaps move per config,
    # so this differs across configs on a real sweep (a repeated value across configs
    # is the silent-wrong switch signature the driver's config_differs surfaces).
    config_headline = _min_gap_clearance(gaps if not folded else folded_gaps)

    # S6 Delta-2: the union of all Fixed-SemiDiameter (frozen) surface numbers (additive, the
    # global counterpart to the per-gap frozen flag). Populated on a FOLDED system too (the
    # semi_solve is read per-row regardless; a fold has no per-gap edge to flag). Empty for an
    # all-auto/all-variable (un-frozen) system. A Fixed solve is GLOBAL (one solve, not per-
    # config), so every config reads the same set — no config-sweep reconcile concern.
    frozen_semi_surfaces = sorted({
        i for i in range(n) if rows[i].get("semi_solve") == "Fixed"
    })

    result = {
        "ok": True,
        "tool": "check_clearance",
        "folded": bool(folded),
        "min_air": min_air,
        "min_glass": min_glass,
        "config_headline": config_headline,
        "gaps": gaps,
        "violations": violations,
        "folded_gaps": folded_gaps,
        "frozen_semi_surfaces": frozen_semi_surfaces,
        "global_bfd": bfd,
        # (additive, non-breaking): the EvenAspheric surface numbers whose
        # edge was modelled from the FULL asphere sag (the S1 ``asphere_sag_ignored`` flag
        # is REPLACED by real geometry). Empty for an all-spherical system.
        "asphere_sag_modelled": asphere_sag_modelled,
        # Fail-closed disclosure: EvenAspheric surfaces whose coefficients could NOT be read
        # (a degraded/unreadable row) — their edge is conic-only-approximate, never silently
        # presented as faithful. Empty when every asphere modelled cleanly.
        "asphere_sag_approximate": asphere_sag_approximate,
        "flags": flags,
    }
    # (clearance-provenance, DQ-6): the additive sag-model-applicability list — emitted
    # ONLY when non-empty (the shipped ``grin_not_audited`` precedent), so a
    # Standard/asphere system's envelope stays byte-identical.
    if sag_model_unfaithful:
        result["sag_model_unfaithful"] = sag_model_unfaithful
    # GRIN: the additive index-not-audited surface list — emitted ONLY when
    # non-empty (a non-GRIN system stays byte-for-byte unchanged).
    if grin_index_profile_not_drawn:
        result["grin_index_profile_not_drawn"] = grin_index_profile_not_drawn
    # The additive positive-audit + unconditional not-audited keys, emitted
    # ONLY when non-empty (a non-GRIN system stays byte-for-byte unchanged). ``audited`` maps
    # one-to-one to a real evaluated kind:"glass" min_glass gap (audited-and-passed
    # violation:false vs audited-and-violated violation:true vs never-examined absent).
    if grin_audited:
        result["grin_geometric_audit"] = {
            "basis": "grin_surface_type",   # NOT an AGF/catalog glass assertion
            "threshold": min_glass,          # the applied solid-medium floor (tunable)
            "audited": grin_audited,         # one entry per REAL evaluated kind:"glass" min_glass gap
        }
    if grin_not_audited:
        result["grin_not_audited"] = grin_not_audited
    if note is not None:
        result["note"] = note
    return result


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
CHECK_CLEARANCE_SPEC = ToolSpec(
    name="check_clearance",
    handler=check_clearance,
    required_params=(),
    param_types={
        "min_air": "number",
        "min_glass": "number",
        # config is a UNION None|int|"all"; advertised "number" (the dominant int type
        # — the MCP reparse shim preserves the "all" string via raw-fallback).
        "config": "number",
    },
    description=(
        "Audit a design's manufacturability and detector clearance (read-only, never "
        "mutates). For an UNFOLDED system: per-gap CENTER and EDGE thickness with "
        "violations flagged when a gap falls below min_air (default 0.5) for an air "
        "gap or min_glass (default 1.0) for a glass element (a NEGATIVE edge means a "
        "steep surface crosses its neighbour) — the back-airgap is audited as air. For "
        "a FOLDED system (any coordinate-break or mirror): the per-gap thickness audit "
        "is suppressed (a folded LDE thickness is the fold direction, NOT a clearance) "
        "and informational folded_gaps + a note are returned instead. ALWAYS returns a "
        "global_bfd block (the image plane's global distance behind the first and last "
        "optical surface) — for a folded/Cassegrain system this behind_first_optic is "
        "the true behind-primary clearance, NOT the misleading raw back-airgap "
        "thickness that get_first_order.back_focal_length reports. "
        "Run after optimize to catch a thin/negative gap a merit floor missed. An authored "
        "GRIN element (Gradient2/Gradient3) is audited as a solid (glass) element at "
        "min_glass; see grin_geometric_audit. A non-authorable GRIN family member is listed "
        "under grin_not_audited. See get_first_order, describe_surfaces."
    ),
)

TOOL_SPECS = (CHECK_CLEARANCE_SPEC,)
