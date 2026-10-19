"""tools/_clearance_common.py — private substrate for ``check_clearance``.

NOT dispatchable (no ``TOOL_SPEC``). The probe-grounded geometry primitives the
``check_clearance`` handler reuses so the load-bearing math (the edge-thickness
formula + the optical-surface classifier + the global-BFD read) lives in exactly
one place. Mirrors ``_measurement_common`` / ``_layout_geometry`` (which it REUSES
for the sag profile, the global-frame read, and the fold predicates — never
re-implements geometry reads).

Probe facts honored here:
- §Probe: the edge thickness of the gap ``i -> i+1`` is
  ``edge = thickness_i + sag_{i+1}(h) - sag_i(h)`` evaluated at
  ``h = min(semi_i, semi_{i+1})`` (the controlling clear aperture, the smaller of
  the two finite-positive semi-diameters). ``sag`` via
  ``_layout_geometry.sag_profile(R, conic, [h])`` -> ``(z_arr, valid)``.
  That is now the rule for air gaps and for a glass gap missing a semi. An unfolded GLASS gap with both semis
  readable is read at each surface's OWN semi (``glass_edge_heights``).
- §Fold: a surface that is a coordinate-break OR a mirror folds the system; for a
  folded system the LDE ``thickness`` is the fold direction, NOT a clearance — so
  the per-gap thickness VIOLATION audit is UNFOLDED-ONLY (the -525.7 trap).
- §BFD: ``GetGlobalMatrix(i)`` slot [12] is the surface's global vertex Z; the BFD
  "behind the primary/last optic" is ``image_global_z - optic_global_z``.

Never raises; every read is guarded (a wedged surface degrades, the scan continues).
"""
import math

from ..enums import _resolve_enum
from . import _layout_geometry as _geom
from . import _solve_cells as _sc


def semi_solve_type_name(system, lde, i):
    """The SemiDiameter cell solve-type NAME of surface ``i`` (S6 §1.3, the L30 single locus).

    Returns one of ``'Fixed'`` / ``'Automatic'`` / ``'Variable'`` / … (the live solve member
    str), or ``None`` on a degraded read. PURE read; NEVER raises (fail-open) — a wedged cell /
    missing enum / throwing solve read degrades to ``None`` so the caller fails closed
    (``freeze``) or fails open (``check_clearance``) without crashing the surrounding scan.

    ``lde`` is passed explicitly because ``check_clearance`` holds ``lde`` (``system.LDE``) and
    a fake clearance system may not expose ``.LDE`` identically; ``freeze`` passes ``system.LDE``
    too. The SHARED solve-type read consumed by BOTH ``freeze_semi._unfreeze_impl`` (the Delta-1
    pre/post gate) and ``clearance._read_rows`` (the Delta-2 frozen flag) — ONE primitive so the
    two readouts can never drift. The ``SurfaceColumn`` enum is resolved via
    ``_solve_cells.surface_column_enum`` — the SAME fake-injectable resolver
    (``system._enum_types["SurfaceColumn"]``) ``freeze_semi`` delegates to, so the two callers
    cannot resolve the column differently either. The import is MODULE-level: the substrate
    imports no tool module (asserted by a test that the substrate imports neither
    ``freeze_semi`` nor ``lens_surface``),
    so there is no cycle to break. It binds the MODULE, never the function — a from-import
    would bind at import time and a test monkeypatching the substrate would stop being seen
    here, which is the same reason ``freeze_semi``'s own resolvers are call-through defs.
    """
    try:
        col = _resolve_enum(_sc.surface_column_enum(system), "SemiDiameter")
        cell = lde.GetSurfaceAt(i).GetSurfaceCell(col)
        return str(cell.GetSolveData().Type)
    except Exception:  # noqa: BLE001 — a degraded solve read -> None (fail-open / fail-closed)
        return None


def _sag_at(R, conic, h, coeffs=None, norm_radius=None, power=None):
    """Sag ``z(h)`` of a Standard/conic/asphere surface at half-height ``h`` (probe formula).

    Reuses ``_layout_geometry.sag_profile`` (the SAME vectorized sag the renderer
    draws) over a length-1 array; returns the scalar ``z_arr[0]``. A planar /
    infinite radius -> 0.0 (sag_profile's flat-line rule). A masked-out (invalid
    radical) point -> ``0.0`` (treat as no sag contribution rather than NaN-poison
    the edge math). NEVER raises.

    Asphere S2 SAGMATH: ``coeffs`` (the ordered ``[α2, α4, …]``) adds the polynomial
    term to the edge math; ``coeffs=None`` (the default) is BYTE-IDENTICAL to the
    pre-S2a conic-only edge (the load-bearing minimalist invariant — a sphere/conic
    surface's audited edge does not move). Asphere S3: ``norm_radius``/``power`` carry
    the normalized Extended-type p=r/norm_radius + the per-type physical power; both
    ``None`` -> the S2a even-absolute path byte-identical.
    """
    import numpy as np

    try:
        z_arr, valid = _geom.sag_profile(
            R, conic, np.array([float(h)]), coeffs=coeffs,
            norm_radius=norm_radius, power=power,
        )
        z = float(z_arr[0])
        if not math.isfinite(z):
            return 0.0
        return z
    except Exception:  # noqa: BLE001 — a degenerate sag read contributes no offset
        return 0.0


def conic_sphere_fallback_fired(R, conic, h):
    """True iff ``sag_profile`` would use its SPHERE fallback for a NON-ZERO conic at ``h``.

    ``sag_profile`` masks a point whose conic radical ``1 - (1+k)*(c*h)^2 < 0`` (a
    steep conic at the controlling aperture); when the WHOLE aperture is invalid it
    falls back to the paraxial sphere term ``h^2/(2R)`` — a FINITE but APPROXIMATE
    value that can UNDER-estimate a steep-conic sag (-> a thinner real edge than
    computed -> a missed violation on an aspheric refractive surface).

    This replicates ``sag_profile``'s EXACT radical condition for the single
    controlling-height point so the caller can DISCLOSE that the edge is approximate.
    Returns True ONLY for a surface with a NON-ZERO conic whose radical is negative at
    ``h`` AND a finite non-zero ``R`` (a planar/zero-radius surface and a SPHERICAL
    surface ``conic == 0`` NEVER trigger — a spherical over-aperture is a real
    geometric fact, not a conic approximation). Cheap + NEVER raises.
    """
    try:
        k = float(conic)
        if k == 0.0:
            return False
        Rf = float(R)
        if not math.isfinite(Rf) or Rf == 0.0:
            return False
        hf = float(h)
        if not math.isfinite(hf):
            return False
        # sag_profile's exact radical at this point (y == h).
        radical = 1.0 - (1.0 + k) * (hf * hf) / (Rf * Rf)
        return radical < 0.0
    except Exception:  # noqa: BLE001 — a degenerate read is no approximation signal
        return False


def edge_thickness(thickness_i, R_i, conic_i, R_ip1, conic_ip1, h,
                   coeffs_i=None, coeffs_ip1=None,
                   norm_i=None, power_i=None, norm_ip1=None, power_ip1=None):
    """Edge thickness of the gap ``i -> i+1`` at clear-aperture half-height ``h``.

    ``edge = thickness_i + sag_{i+1}(h) - sag_i(h)`` (the probe-validated formula).
    A convex back face (positive sag) on surface ``i`` EATS into the gap; a convex
    front face on ``i+1`` GIVES the gap room — exactly the sign convention the
    formula encodes. Returns a float; NEVER raises.

    Asphere S2 SAGMATH: ``coeffs_i`` / ``coeffs_ip1`` are the per-surface asphere
    coefficients; each feeds its surface's ``_sag_at`` so the edge is modelled from the
    FULL sag (sphere + conic base + polynomial), not the base only. Asphere S3:
    ``norm_i``/``power_i`` (and the ``_ip1`` pair) carry the per-surface normalized
    Extended-type p=r/norm + the physical power. All default ``None`` -> BYTE-IDENTICAL
    to the pre-S2a conic-only edge.
    """
    return edge_thickness_at(
        thickness_i, R_i, conic_i, R_ip1, conic_ip1, h, h,
        coeffs_i=coeffs_i, coeffs_ip1=coeffs_ip1,
        norm_i=norm_i, power_i=power_i, norm_ip1=norm_ip1, power_ip1=power_ip1,
    )


def edge_thickness_at(thickness_i, R_i, conic_i, R_ip1, conic_ip1, h_i, h_ip1,
                      coeffs_i=None, coeffs_ip1=None,
                      norm_i=None, power_i=None, norm_ip1=None, power_ip1=None):
    """``thickness_i + sag_{i+1}(h_ip1) - sag_i(h_i)``: each surface's sag at its OWN height.

    ``edge_thickness`` is this with ``h_i == h_ip1``. A GLASS gap passes each surface's own
    semi-diameter (``GLASS_EDGE_SOURCE``). Returns a float; NEVER raises.
    """
    sag_i = _sag_at(R_i, conic_i, h_i, coeffs=coeffs_i, norm_radius=norm_i, power=power_i)
    sag_ip1 = _sag_at(
        R_ip1, conic_ip1, h_ip1, coeffs=coeffs_ip1, norm_radius=norm_ip1, power=power_ip1
    )
    return float(thickness_i) + sag_ip1 - sag_i


#: ``edge_height_source`` of a GLASS gap whose edge is read at each surface's own semi.
GLASS_EDGE_SOURCE = "own_semi_each"


def glass_edge_heights(semi_i, semi_ip1):
    """``(semi_i, semi_ip1, rim)`` for a glass gap, or ``None`` unless BOTH are finite-positive.

    OpticStudio's edge thickness (manual §15.9 "Edge Thickness") takes each
    surface's sag at ITS OWN semi-diameter, and its mechanical model holds a surface flat
    beyond that (the MEMA section of the surface-data chapter), so at the element's rim
    ``rim = max(semi_i, semi_ip1)`` the gap is exactly that own-semi edge. The narrower
    face is never continued past its own aperture. With only one semi readable there is
    no own-semi edge: the caller keeps the single-semi reading and says so.
    """
    if not (_is_finite_positive(semi_i) and _is_finite_positive(semi_ip1)):
        return None
    return float(semi_i), float(semi_ip1), float(max(semi_i, semi_ip1))


#: Why an optic-rim reading of an end gap was not computed (frozen tokens).
RIM_END_NOT_FLAT = "end_surface_not_flat"
RIM_NO_OPTIC_SEMI = "optic_semi_diameter_unreadable"
RIM_SAG_NOT_EXACT = "optic_sag_not_exact_at_rim"
RIM_CENTER_UNREADABLE = "center_thickness_unreadable"
RIM_TYPE_NOT_MODELLED = "surface_type_not_modelled"


def _row_is_flat(row):
    """True iff the row's shape is a PLANE: an infinite (or zero) radius and no asphere
    terms. A conic on a plane is inert. Never raises; an unreadable value is not flat."""
    try:
        radius = float(row.get("radius"))
    except (TypeError, ValueError):
        return False
    if math.isfinite(radius) and radius != 0.0:
        return False
    if math.isnan(radius):
        return False
    coeffs = row.get("aspheric_coefficients")
    try:
        return not coeffs or all(float(c) == 0.0 for c in coeffs)
    except (TypeError, ValueError):
        return False


def optic_rim_edge(center, optic_row, end_row):
    """The END gap's edge read at the OPTIC's own rim against a FLAT end surface.

    ``check_clearance`` reads every gap's edge at the SMALLER of the two semi-diameters.
    For the gap that ends at the image that is the image circle, close to the axis, so
    the reading is nearly the vertex distance. This is the second reading: how far the
    optic's own clear rim sits from the end PLANE (a sample, cover glass or sensor is
    wider than the image circle). ``rim = center - sag_optic(optic semi-diameter)``.

    Returns ``{"status": "measured", "edge_thickness", "edge_height"}`` or
    ``{"status": "not_computed", "reason": <RIM_* token>}``. It is computed ONLY where it
    is exact: a flat end surface (a curved one would have to be continued past the
    aperture it is defined on), both surfaces of a type the sag model was written for,
    every asphere term read, a readable optic semi-diameter, and an optic sag whose
    radical is valid at the rim (past it the sag routine hands back a finite paraxial
    value marked valid, which would read as a real gap). Never raises.
    """
    import numpy as np

    def no(reason):
        return {"status": "not_computed", "reason": reason}

    try:
        if not _row_is_flat(end_row):
            return no(RIM_END_NOT_FLAT)
        h = optic_row.get("semi_diameter")
        if not _is_finite_positive(h):
            return no(RIM_NO_OPTIC_SEMI)
        if isinstance(center, bool) or not isinstance(center, (int, float)) \
                or not math.isfinite(center):
            return no(RIM_CENTER_UNREADABLE)
        # The sag math is exact only for the surface types it was written for, and only
        # with every asphere term read. Anything else is "not computed", never a number:
        # a base-only sag can read NEGATIVE on a healthy surface and refuse a keeper.
        for row in (optic_row, end_row):
            if _geom.sag_model_is_faithful(row.get("type_name")) is not True:
                return no(RIM_TYPE_NOT_MODELLED)
        coeffs = optic_row.get("aspheric_coefficients")
        if optic_row.get("coefficients_unreadable") or any(
                not math.isfinite(float(c)) for c in (coeffs or ())):
            return no(RIM_SAG_NOT_EXACT)
        # The sag routine falls back to a paraxial term when the whole aperture is past
        # the radical and still reports the point valid, so the radical is tested HERE.
        radius, conic = float(optic_row.get("radius")), float(optic_row.get("conic") or 0.0)
        if math.isnan(radius) or not math.isfinite(conic):
            return no(RIM_SAG_NOT_EXACT)
        if math.isfinite(radius) and radius != 0.0 \
                and 1.0 - (1.0 + conic) * (h * h) / (radius * radius) < 0.0:
            return no(RIM_SAG_NOT_EXACT)
        z_arr, valid = _geom.sag_profile(
            optic_row.get("radius"), optic_row.get("conic"), np.array([float(h)]),
            coeffs=optic_row.get("aspheric_coefficients"),
            norm_radius=optic_row.get("asphere_norm_radius"),
            power=optic_row.get("asphere_power"),
        )
        z = float(z_arr[0])
        if not bool(np.all(valid)) or not math.isfinite(z):
            return no(RIM_SAG_NOT_EXACT)
        return {"status": "measured", "edge_thickness": float(center) - z,
                "edge_height": float(h)}
    except Exception:  # noqa: BLE001 - an unreadable input is "not computed", said so
        return no(RIM_SAG_NOT_EXACT)


def _is_finite_positive(s):
    """The ONE finite-positive-semi predicate ``controlling_height`` and
    ``controlling_height_source`` both consult (so they can never disagree)."""
    return isinstance(s, (int, float)) and math.isfinite(s) and s > 0.0


def controlling_height(semi_i, semi_ip1):
    """The controlling clear-aperture half-height ``h = min`` of the two semis.

    ``h`` is the SMALLER of the two surfaces' finite-positive semi-diameters (the
    common clear aperture the edge is measured at). If NEITHER surface has a
    finite-positive semi-diameter, returns ``None`` (the caller skips + flags the
    gap — there is no aperture to measure an edge at). If only ONE is finite-
    positive, use it (the smaller of {that, +inf} = that). Never raises for an engine-read
    semi (a float); a Python ``int`` too large for a float raises ``OverflowError`` from the
    shared predicate (not reachable from the engine path).
    """
    finite = [s for s in (semi_i, semi_ip1) if _is_finite_positive(s)]
    if not finite:
        return None
    return min(finite)


def controlling_height_source(semi_i, semi_ip1):
    """WHERE ``controlling_height``'s ``h`` came from (edge-height provenance).

    ``"surface_i"`` / ``"surface_ip1"`` when BOTH semis are finite-positive and that
    surface's semi is the smaller (a TIE goes to ``"surface_i"``);
    ``"substituted_single_finite"`` when exactly ONE is finite-positive (the other was
    degraded and its semi was substituted away); ``None`` when neither is (``h`` is
    ``None``). Uses the same predicate as ``controlling_height``, so it raises exactly where that
    function does (only a float-overflowing ``int``, never an engine-read semi).
    """
    ok_i, ok_ip1 = _is_finite_positive(semi_i), _is_finite_positive(semi_ip1)
    if ok_i and ok_ip1:
        return "surface_i" if semi_i <= semi_ip1 else "surface_ip1"
    if ok_i or ok_ip1:
        return "substituted_single_finite"
    return None


def is_optical_surface(row):
    """True iff ``row`` is a powered/optical surface for the BFD anchor (§4).

    "Optical surface" = a Standard/MIRROR/grating surface that is NOT object/image/
    coordinate-break/FLAT-AIR-DUMMY. Concretely:
      - object / image / coordinate-break roles -> NOT optical (no power anchor).
      - a real glass face (role "glass") -> optical.
      - a MIRROR (role "mirror") -> optical.
      - an AIR/STOP surface that is CURVED (finite, non-zero radius) -> optical: this
        is the BACK FACE of a glass element (its material reads air because the gap
        AFTER it is air, but the surface itself is powered). On the Cooke this is the
        last optical surface, so ``behind_last_optic`` == the back-airgap thickness.
      - a FLAT air / dummy / stop surface (infinite or zero radius) -> NOT optical.

    ``row`` is a ``read_geometry_row``-shape dict (carries ``role`` + the raw
    ``radius`` float). NEVER raises.
    """
    role = row.get("role")
    if role in ("object", "image", "coordinate-break"):
        return False
    if role in ("glass", "mirror"):
        return True
    # An air / stop surface: optical ONLY if it carries power (a finite non-zero
    # radius — the curved back face of a glass element). A flat air dummy / stop
    # (infinite or zero radius) is NOT an optical anchor.
    R = row.get("radius")
    return isinstance(R, (int, float)) and math.isfinite(R) and R != 0.0
