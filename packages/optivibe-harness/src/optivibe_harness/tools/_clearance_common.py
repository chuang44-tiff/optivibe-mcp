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
    sag_i = _sag_at(R_i, conic_i, h, coeffs=coeffs_i, norm_radius=norm_i, power=power_i)
    sag_ip1 = _sag_at(
        R_ip1, conic_ip1, h, coeffs=coeffs_ip1, norm_radius=norm_ip1, power=power_ip1
    )
    return float(thickness_i) + sag_ip1 - sag_i


def controlling_height(semi_i, semi_ip1):
    """The controlling clear-aperture half-height ``h = min`` of the two semis.

    ``h`` is the SMALLER of the two surfaces' finite-positive semi-diameters (the
    common clear aperture the edge is measured at). If NEITHER surface has a
    finite-positive semi-diameter, returns ``None`` (the caller skips + flags the
    gap — there is no aperture to measure an edge at). If only ONE is finite-
    positive, use it (the smaller of {that, +inf} = that). NEVER raises.
    """
    finite = [s for s in (semi_i, semi_ip1) if isinstance(s, (int, float))
              and math.isfinite(s) and s > 0.0]
    if not finite:
        return None
    return min(finite)


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
