"""tools/_layout_geometry.py — shared geometry helpers (NOT dispatchable).

The SINGLE shared seam between ``describe_surfaces`` (``lens_describe``) and
``render_layout`` (``layout_render``): one role classifier, one raw-float geometry
read, the per-surface sag profile, and the cumulative vertex placement. There is
exactly ONE classifier so the JSON table and the drawn figure can never disagree
(§0.1, §2 role rules, §3.2, §3.3).

Grounding facts honored here (§0):
- §0.1: geometry MATH reads the RAW ``float(row.Radius/.Thickness/.SemiDiameter)``,
  NOT the ``safe_float`` string sentinel. Planar = ``not math.isfinite(R)`` on the
  raw float (``safe_float`` would hand back the STRING ``"inf"``).
- §0.2: CB detection is a SUBSTRING match on ``str(row.Type).upper()``
  (``"COORD"`` / ``"BREAK"``) — the probe-proven, version-robust signal.
- §0.3: mirror detection is ``str(row.Material).strip().upper() == "MIRROR"``.

The role classifier is FIRST-MATCH-WINS in the fixed order — the CB check runs
BEFORE the material check because a coordinate break's material reads ``"-"``,
which a naive air/glass walk misclassifies as air (the ``"-"`` trap, probe
Headline 1). That ordering is a load-bearing contract (guard
``test_l25_cb_before_material``).

NOT dispatchable: no ``TOOL_SPEC``/``TOOL_SPECS``; the server never registers it.
"""
import math


# --------------------------------------------------------------------------- #
# Fact predicates (§0.2 / §0.3) — string signals, NOT enum comparisons.
# --------------------------------------------------------------------------- #
def _is_coordinate_break(type_name: str) -> bool:
    """True if ``type_name`` names a coordinate break (substring rule, §0.2)."""
    upper = type_name.upper()
    return "COORD" in upper or "BREAK" in upper


def _is_mirror(material: str) -> bool:
    """True if ``material`` is the engine's own MIRROR readout (§0.3)."""
    return material.strip().upper() == "MIRROR"


def _is_air_material(material: str) -> bool:
    """True if the material reads as air: ``""`` or the CB placeholder ``"-"``."""
    stripped = material.strip()
    return stripped == "" or stripped == "-"


# --------------------------------------------------------------------------- #
# Sag-model applicability (clearance-provenance, DQ-6).
# --------------------------------------------------------------------------- #
def _sag_faithful_types():
    """The types the radius/conic/polynomial sag model was WRITTEN for — DERIVED.

    NOT a hand-copied literal (one source of truth). Verified read-path:
      - ``Standard``: the base sphere/conic the model IS.
      - ``_asph.ASPHERE_TYPE_NAMES`` — the SAME registry ``_read_rows`` keys on to populate
        ``aspheric_coefficients``/``asphere_norm_radius``/``asphere_power``
        (_layout_geometry.py:219-260) and that ``_audit_gaps`` feeds into
        ``edge_thickness`` (clearance.py:203-211). Registering a type and executing it are
        the same act, so the set cannot drift from the executor by construction.
      - ``_grin.GRIN_TYPE_INFO`` — the authorable GRIN primitives, whose SAG is the standard
        sphere/conic base (GRIN: the index profile does not perturb the sag,
        residual 0.0). The 12-member ``GRIN_FAMILY_TYPE_TOKENS`` recognition set is
        deliberately NOT used: a loaded Gradium/GridGradient is un-probed.
    """
    from . import _asphere_cells as _asph
    from . import _grin_cells as _grin
    return frozenset({"Standard"}) | frozenset(_asph.ASPHERE_TYPE_NAMES) | frozenset(
        _grin.GRIN_TYPE_INFO)


SAG_FAITHFUL_TYPES = _sag_faithful_types()


def sag_model_is_faithful(type_name):
    """Tri-state: True iff the radius/conic/polynomial sag model was WRITTEN FOR this
    surface type; False for a readable type OUTSIDE the set; None when the type is
    unreadable.

    POSITIVE membership, resolved through the EXECUTOR'S OWN acceptance predicate —
    ``_grin_cells._exact_token_match``, the shared resolver behind
    ``_asphere_cells.asphere_type_of_name`` and ``_grin_cells.grin_type_of_name``. EXACT
    full-token, NEVER a substring — the ``Gradient1`` subset-of ``Gradient10`` hazard
    (_grin_cells.py:227-232). Fail-CLOSED on an unreadable type (the lens_spec.py
    precedent) so a new engine surface type cannot silently rejoin the faithful set.
    NEVER raises.

    This used to be ``type_name.strip() in SAG_FAITHFUL_TYPES``
    — a guard RE-DERIVING an acceptance test its executor does not share. The SET could
    not drift from the executor by construction; the MATCHING RULE already had, in BOTH
    directions:

    - ``" EvenAspheric "`` -> guard said FAITHFUL (``.strip()`` hit) while
      ``asphere_type_of`` returned ``None``, so the coefficients were never read and the
      base conic model ran: asphere permission for a surface modelled as a bare conic,
      verdict ``clean``. FAIL-OPEN, and reproduced it.
    - ``"SurfaceType.EvenAspheric"`` -> guard said UNFAITHFUL (no ``.`` fallback) while
      the executor resolved it and DID read the coefficients: a noisy over-refusal.

    Consuming the resolver closes both at once, and it closes them for every token — not
    just the one an auditor happened to find. The free ``.strip()`` is GONE from the
    membership test; the emptiness test keeps it (an all-whitespace type is unreadable,
    which is the ``None`` arm, not the ``False`` arm — the shipped T28 contract).

    NEVER key on ``Radius == inf``: the probe caution 1 measured 3 of the 9 Cooke
    surfaces reading Radius = inf LEGITIMATELY (a plane). ``inf`` is the encoding of BOTH
    "inapplicable" and "flat"; only the TYPE discriminates.

    THE CLAIM IS APPLICABILITY, AND NOTHING MORE. ``True`` means "this type
    is one the sag executor has a code path for", NOT "the executor computes it correctly".
    A faithful type can still carry a degraded cell read (gotcha), and no measurement
    in this cycle validates the executor per-type; a follow-up would.
    """
    from ._grin_cells import _exact_token_match
    if not isinstance(type_name, str) or not type_name.strip():
        return None
    return _exact_token_match(type_name, SAG_FAITHFUL_TYPES) is not None


# --------------------------------------------------------------------------- #
# Role classifier (§2) — FIRST MATCH WINS, in EXACTLY this order.
# --------------------------------------------------------------------------- #
def lde_is_folded(lde, n) -> bool:
    """True iff ANY surface is a coordinate-break OR a mirror (the ONE fold predicate).

    The single shared "is this system folded" detector for the READOUT tools
    (``check_clearance`` + ``get_first_order``). Scans ALL indices ``0 .. n-1``
    INCLUDING the object (0) and image (n-1) surfaces against the RAW ``Type`` /
    ``Material`` strings — NOT the role classifier (which stamps ``object`` /
    ``image`` at the ends BEFORE the CB/mirror checks, so a CB at surface 0 or a
    MIRROR at the image surface would read UNFOLDED and the two readout tools could
    drift). A fold is a CORRECTNESS signal here, so it must see every surface.

    NEVER raises: an unreadable ``NumberOfSurfaces`` / a per-surface read throw
    treats THAT surface as not-a-fold-signal and continues (the fold flag is
    honesty-only, not load-bearing — a degraded read must not crash the readout).
    The rendering classifier (``layout_render``) is a SEPARATE concern and does NOT
    use this predicate.
    """
    try:
        count = int(n)
    except (TypeError, ValueError):
        return False
    for i in range(count):
        try:
            row = lde.GetSurfaceAt(i)
            if _is_coordinate_break(str(row.Type)):
                return True
            if _is_mirror(str(row.Material)):
                return True
        except Exception:  # noqa: BLE001 — one wedged surface is no fold signal; continue
            continue
    return False


def system_is_folded(system) -> bool:
    """Convenience wrapper: read ``system.LDE`` + count, then ``lde_is_folded``.

    NEVER raises (an unreadable LDE/count degrades to ``False``). Lets a caller that
    holds a ``system`` (``get_first_order``) share the EXACT predicate a caller that
    already holds the ``lde`` + ``n`` (``check_clearance``) uses, with no drift.
    """
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable LDE/count -> no fold flag
        return False
    return lde_is_folded(lde, n)


def classify_role(i: int, n: int, type_name: str, material: str, is_stop: bool) -> str:
    """Classify a surface's role from read facts. Pure function; FIRST MATCH WINS.

    Order (§2) — the CB check is BEFORE material (the ``"-"`` trap):
      1. ``i == 0``                                   -> ``"object"``
      2. ``i == n - 1``                               -> ``"image"``
      3. coordinate break (type substring)            -> ``"coordinate-break"``
      4. material == MIRROR                            -> ``"mirror"``
      5. air material AND is_stop                      -> ``"stop"``
      6. real glass (non-air, non-CB, non-mirror)      -> ``"glass"``
      7. else                                          -> ``"air"``
    """
    if i == 0:
        return "object"
    if i == n - 1:
        return "image"
    # CB BEFORE material (load-bearing): a CB's material reads "-" and would be
    # misclassified as air by a naive material walk.
    if _is_coordinate_break(type_name):
        return "coordinate-break"
    if _is_mirror(material):
        return "mirror"
    if _is_air_material(material):
        # Free-standing dummy AIR stop (stop-normalization convention).
        if is_stop:
            return "stop"
        return "air"
    # A real glass (non-empty, not "-", not "MIRROR"). A glass that is ALSO the
    # stop still classifies "glass"; is_stop stays True on its own field.
    return "glass"


def is_cemented_interface(rows, i: int) -> bool:
    """True iff the interface at surface ``i`` is a cemented (glass-to-glass) join.

    Cement rule: an interface at surface ``i`` is CEMENTED iff BOTH
    the medium BEFORE it (surface ``i-1`` material) AND the medium starting at it
    (surface ``i`` material) are real glass — material NOT in {``""``, ``"-"``,
    ``"MIRROR"``}. Reuses ``_is_air_material`` / ``_is_mirror`` so the predicate can
    never drift from the role classifier. Pure function; never raises (an
    out-of-range ``i`` or a missing material key returns False).

    ``rows`` is the list of geometry dicts (``read_geometry_row`` shape); ``i`` is a
    surface index. ``i <= 0`` is never cemented (no surface before it).
    """
    if i <= 0 or i >= len(rows):
        return False
    prev_mat = str(rows[i - 1].get("material", ""))
    this_mat = str(rows[i].get("material", ""))

    def _is_real_glass(m: str) -> bool:
        return not _is_air_material(m) and not _is_mirror(m)

    return _is_real_glass(prev_mat) and _is_real_glass(this_mat)


def normalize_material(material: str) -> str:
    """Map a raw material readout to the describe_surfaces JSON value.

    ``""`` -> ``"AIR"``; the CB placeholder ``"-"`` -> ``"AIR"``; ``"MIRROR"``
    kept; any real glass name kept verbatim (the §2 material rule).
    """
    stripped = material.strip()
    if stripped == "" or stripped == "-":
        return "AIR"
    return material


# --------------------------------------------------------------------------- #
# Raw-float geometry read (§0.1) — RAW floats for math, never the sentinel.
# --------------------------------------------------------------------------- #
def _raw_float(value):
    """``float(value)`` guarded; an unreadable / non-numeric value -> ``nan``.

    Used ONLY for the geometry MATH path (sag + placement). The describe table
    uses ``safe_float`` separately for the JSON sentinel string.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def read_geometry_row(lde, i, system=None):
    """Read ONE surface's geometry as raw floats + the fact strings (§0.1).

    Returns a dict with the RAW ``radius``/``thickness``/``conic``/``semi_diameter``
    floats (for the sag math), plus ``type_name``/``material``/``is_stop`` and the
    classifier-ready ``role`` (computed by the SHARED classifier). A per-surface
    read that throws is the CALLER's concern (describe wraps each row); this helper
    reads straight so the renderer can degrade a single wedged surface.

    Asphere SAGMATH: when ``system`` is supplied AND the row
    is EvenAspheric, the 8 even-asphere Par coefficients (α2..α16) are read via
    ``_asphere_cells.read_asphere_cell`` (the SAME substrate reader the write path
    uses; it needs ``(system, row)`` — the ONE plumbing seam the probe called out) and
    added as ``aspheric_coefficients``. A non-asphere row, or a call WITHOUT ``system``,
    carries ``aspheric_coefficients = None`` (a sphere/conic surface is byte-identical
    to before — the SAGMATH polynomial term is default-off). A coefficient read on a
    GENUINE asphere that THROWS is a degraded read: ``aspheric_coefficients`` carries
    ``None`` AND the row is marked ``coefficients_unreadable`` so the consuming readout
    can FAIL-CLOSED (flag approximate), NEVER silently compute conic-only as if exact.
    """
    row = lde.GetSurfaceAt(i)
    out = {
        "radius": _raw_float(row.Radius),
        "thickness": _raw_float(row.Thickness),
        "conic": _raw_float(row.Conic),
        "semi_diameter": _raw_float(row.SemiDiameter),
        "type_name": str(row.Type),
        "material": str(row.Material),
        "is_stop": bool(row.IsStop),
        # SAGMATH default: a non-asphere / no-system read carries None (the conic-only
        # byte-identical path). Populated below ONLY for a Tier-1 asphere row + a system.
        "aspheric_coefficients": None,
        # The per-type normalization radius (None for Odd/Even/non-
        # asphere) + the per-type physical-power callable (None -> the even-step default).
        "asphere_norm_radius": None,
        "asphere_power": None,
    }
    if system is not None:
        from . import _asphere_cells as _asph
        type_key = None
        try:
            type_key = _asph.asphere_type_of(row)
        except Exception:  # noqa: BLE001 — a Type-read throw on the asphere probe -> non-asphere
            type_key = None
        if type_key is not None:
            info = _asph.ASPHERE_TYPE_INFO[type_key]
            try:
                if info.gated:
                    # Read the live Max-Term gate, the norm radius, then that many cells.
                    max_terms = _asph.read_gate_cell(system, row, info)
                    norm_radius = _asph.read_computed_double_cell(
                        system, row, info.norm_par, _asph._NORM_HEADER
                    )
                    coeffs = [
                        _asph.read_computed_double_cell(
                            system, row, info.coeff_par(i), info.header(i)
                        )
                        for i in range(max_terms)
                    ]
                    out["aspheric_coefficients"] = coeffs
                    out["asphere_norm_radius"] = norm_radius
                else:
                    out["aspheric_coefficients"] = [
                        _asph.read_computed_double_cell(
                            system, row, info.coeff_par(i), info.header(i)
                        )
                        for i in range(info.max_terms)
                    ]
                # The per-type physical power (bound to this info; the renderer/clearance
                # thread it into sag_profile so the drawn/measured profile is exact).
                out["asphere_power"] = info.power
            except Exception:  # noqa: BLE001 — a drifted/throwing coeff/gate cell -> fail-closed
                # A GENUINE asphere whose coefficients could not be read: leave coeffs
                # None AND flag it, so the caller discloses approximate (never silent
                # conic-only on an asphere).
                out["aspheric_coefficients"] = None
                out["asphere_norm_radius"] = None
                out["asphere_power"] = None
                out["coefficients_unreadable"] = True
    return out


# --------------------------------------------------------------------------- #
# Sag profile (§3.2) + vertex placement (§3.3).
# --------------------------------------------------------------------------- #
def _poly_sag(coeffs, y, *, normalized_by=None, power=None):
    """The asphere polynomial term ``Σ coeffs[n] · p^power(n)`` (Asphere S2/S3 SAGMATH).

    ``coeffs`` is the ORDERED ``[c0, c1, …]`` (the read-back order, index n -> the
    ``power(n)``-th physical power). The DEFAULT kwargs reproduce the S2a EvenAspheric
    call BYTE-IDENTICAL: ``power=None`` -> the even-step ``2*(n+1)`` (coeffs[0]·y² +
    coeffs[1]·y⁴ + …), ``normalized_by=None`` -> absolute ``p = y`` (no normalization).
    Confirmed live (α4·y⁴ = 1e-6·10⁴ = 0.01, the 1.0202051443 == live SAGY total).

    ``power`` (a callable ``n -> exponent``) carries the per-type
    physical exponent (OddAsphere ``n+1``; ExtendedAsphere ``2*(n+1)``; ExtendedOdd
    ``n+1``); ``normalized_by`` (a positive float) divides ``p = y / normalized_by`` for
    the normalized Extended types. A non-positive / non-finite ``normalized_by`` falls
    back to absolute ``p = y`` (a defensive no-divide guard).

    A SHORT list contributes only its leading terms; a 0.0 / non-finite coefficient
    contributes 0.0 (a non-finite coeff would NaN-poison the draw — drop it rather than
    fabricate). Operates elementwise on a numpy array ``y``. Pure; never raises.
    """
    import numpy as np

    y = np.asarray(y, dtype=float)
    if power is None:
        power = lambda n: 2 * (n + 1)  # noqa: E731 — the S2a even-step default
    # Normalize p = y / normalized_by; guard a non-positive/non-finite divisor (no-divide).
    if (
        normalized_by is not None
        and isinstance(normalized_by, (int, float))
        and math.isfinite(float(normalized_by))
        and float(normalized_by) > 0.0
    ):
        p = y / float(normalized_by)
    else:
        p = y
    poly = np.zeros_like(y)
    for n, a in enumerate(coeffs):
        try:
            af = float(a)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(af) or af == 0.0:
            continue
        poly = poly + af * np.power(p, power(n))
    return poly


def sag_profile(R, k, y, coeffs=None, *, norm_radius=None, power=None):
    """Standard sag z(y) over a numpy array ``y`` (vectorized). NEVER NaN/raises.

    ``z(y) = y^2 / ( R * (1 + sqrt(1 - (1+k) * y^2 / R^2)) ) + Σ α₂ₙ·y^(2n)`` with
    these rules (§3.2 + Asphere S2 SAGMATH §3.1):
      - Planar / infinite radius (``not math.isfinite(R)``): ``z(y) = 0`` (flat
        line, conic base). Never divide.
      - Negative-radical guard: where ``1 - (1+k) * y^2 / R^2 < 0`` (a steep conic
        at full aperture), MASK those ``y`` out (NaN in the returned z, so the
        drawn path truncates there). If the WHOLE aperture is invalid, fall back to
        the sphere-only term ``y^2 / (2*R)``.
      - Never ``sqrt`` a negative; never emit a NaN where the radical was valid.

    ``coeffs`` (Asphere S2): the ORDERED even-asphere coefficients ``[α2, α4, …]``.
    **``coeffs=None`` (the default) is BYTE-IDENTICAL to the pre-S2a behaviour** — the
    polynomial branch is SKIPPED entirely, so every existing sphere/conic caller (render
    + clearance, all of which pass NO ``coeffs``) gets the EXACT old ``(z, valid)`` (the
    load-bearing minimalist invariant). When ``coeffs`` is supplied the polynomial term
    is ADDED on the valid mask (a masked / over-aperture point stays NaN — a truncated
    draw is correct).

    Returns ``(z, valid_mask)`` where ``valid_mask`` is True where ``z`` is drawn.
    """
    import numpy as np

    y = np.asarray(y, dtype=float)
    # Planar / infinite radius -> flat line at z = 0 (conic base).
    if not math.isfinite(R) or R == 0.0:
        z = np.zeros_like(y)
        if coeffs:
            z = z + _poly_sag(coeffs, y, normalized_by=norm_radius, power=power)
        return z, np.ones_like(y, dtype=bool)

    radical = 1.0 - (1.0 + k) * (y * y) / (R * R)
    valid = radical >= 0.0

    if not np.any(valid):
        # The whole aperture is invalid for the conic term -> sphere-only fallback.
        z = (y * y) / (2.0 * R)
        if coeffs:
            z = z + _poly_sag(coeffs, y, normalized_by=norm_radius, power=power)
        return z, np.ones_like(y, dtype=bool)

    # Compute z only where the radical is valid; mask the rest to NaN so the drawn
    # profile truncates (never sqrt a negative).
    z = np.full_like(y, np.nan)
    safe_radical = np.where(valid, radical, 0.0)
    denom = R * (1.0 + np.sqrt(safe_radical))
    # denom can be 0 only if R*(1+sqrt(...)) == 0; sqrt>=0 so 1+sqrt>=1, denom!=0.
    z_valid = (y * y) / denom
    z = np.where(valid, z_valid, np.nan)
    if coeffs:
        # Add the polynomial term only where the conic base is valid (the mask stays
        # NaN past the radical — a truncated draw is correct).
        z = np.where(
            valid, z + _poly_sag(coeffs, y, normalized_by=norm_radius, power=power),
            np.nan,
        )
    return z, valid


def vertex_z(thicknesses):
    """Cumulative UNFOLDED vertex z positions (§3.3).

    ``z_vertex[0] = 0``; ``z_vertex[i] = z_vertex[i-1] + thickness[i-1]``, using
    the RAW thickness float. A non-finite thickness (object at infinity reads
    ``inf``) is treated as ``0`` for placement (do not walk a surface to infinity).
    A negative mirror thickness walks z BACK as written (the correct unfolded
    picture — do NOT infer a fold from the sign).
    """
    z = [0.0]
    for t in thicknesses[:-1]:
        step = t if math.isfinite(t) else 0.0
        z.append(z[-1] + step)
    return z


def read_global_frames(lde, n):
    """Read each surface's GLOBAL vertex (x,y,z) + row-major rotation block.

    The honest fold-coordinate channel: ``GetGlobalMatrix(i)`` returns the
    13-tuple ``(success, R11..R33 ROW-MAJOR, X, Y, Z)``. Returns a per-surface list of
    dicts ``{"ok": bool, "vertex": (x,y,z), "R": [9 row-major]}``; a surface whose
    matrix read THROWS, reports ``success=False``, is a non-13 tuple, or carries a
    non-finite vertex/rotation entry degrades to ``{"ok": False}`` (the renderer
    SUPPRESSES that surface's global outline rather than drawing it at a bogus place —
    fail closed on degraded geometry, guarding a prior ray-drift bug). NEVER
    raises (one wedged surface degrades; the scan continues).
    """
    frames = []
    for i in range(n):
        frames.append(_read_one_global_frame(lde, i))
    return frames


def _read_one_global_frame(lde, i):
    """Read ONE surface's global frame; degrade to ``{"ok": False}`` on any fault."""
    try:
        ret = lde.GetGlobalMatrix(i)
        seq = list(ret)
    except BaseException:  # noqa: BLE001 — one bad surface degrades, never raises
        return {"ok": False, "vertex": None, "R": None}
    if len(seq) != 13:
        return {"ok": False, "vertex": None, "R": None}
    # The success flag (seq[0]) is decisive: a success=False matrix carries stale/
    # identity numbers that would CERTIFY a frame the engine could not compute.
    try:
        if not bool(seq[0]):
            return {"ok": False, "vertex": None, "R": None}
        R = [float(v) for v in seq[1:10]]
        vertex = (float(seq[10]), float(seq[11]), float(seq[12]))
    except BaseException:  # noqa: BLE001 — an unmarshallable frame degrades
        return {"ok": False, "vertex": None, "R": None}
    # A non-finite vertex/rotation entry is a degraded frame -> suppress (no axis-dive).
    if not all(math.isfinite(c) for c in vertex):
        return {"ok": False, "vertex": None, "R": None}
    if not all(math.isfinite(r) for r in R):
        return {"ok": False, "vertex": None, "R": None}
    return {"ok": True, "vertex": vertex, "R": R}


def sag_to_global(R, vertex, y, sag):
    """Map a local meridional sag point to the GLOBAL frame.

    ``local = (0, y, sag(y))``; ``global = vertex + R_rowmajor · local`` with ``R``
    the row-major GetGlobalMatrix rotation block applied DIRECTLY (NOT transposed —
    74× live-falsified). ``R`` is ``[R11 R12 R13 R21 R22 R23 R31 R32 R33]``;
    ``vertex`` is the global ``(x, y, z)``. Returns the global ``(gx, gy, gz)``.

    Pure function; the caller passes per-point ``y`` + ``sag`` scalars (numpy arrays
    map elementwise via ``sag_to_global_arrays``).
    """
    lx, ly, lz = 0.0, y, sag
    gx = vertex[0] + R[0] * lx + R[1] * ly + R[2] * lz
    gy = vertex[1] + R[3] * lx + R[4] * ly + R[5] * lz
    gz = vertex[2] + R[6] * lx + R[7] * ly + R[8] * lz
    return gx, gy, gz


def point_to_global(R, vertex, lx, ly, lz):
    """Map a FULL local ``(lx, ly, lz)`` point to the GLOBAL frame (A2, render-obscured).

    ``global = vertex + R_rowmajor · local`` with ``R`` the row-major GetGlobalMatrix
    rotation block applied DIRECTLY (NOT transposed — same convention as
    ``sag_to_global``, 74× live-falsified). ``R`` is
    ``[R11 R12 R13 R21 R22 R23 R31 R32 R33]``; ``vertex`` is the global ``(x, y, z)``.
    Returns the global ``(gx, gy, gz)``.

    Unlike ``sag_to_global`` (which hardcodes ``local = (0, y, sag)``) this carries a
    NON-ZERO local X — the batch ray trace returns a full ``(X, Y, Z)`` intersection
    in the surface's local frame, and a fold's R block mixes all three axes, so the
    local X cannot be dropped (raw batch Y diverges up to 87.7 on a fold; the
    transformed value matches RAGY/RAGZ to machine zero). Pure function.
    """
    gx = vertex[0] + R[0] * lx + R[1] * ly + R[2] * lz
    gy = vertex[1] + R[3] * lx + R[4] * ly + R[5] * lz
    gz = vertex[2] + R[6] * lx + R[7] * ly + R[8] * lz
    return gx, gy, gz


def sag_to_global_arrays(R, vertex, y_arr, sag_arr):
    """Vectorized ``sag_to_global`` over numpy ``y``/``sag`` arrays. Returns (gy, gz).

    Draws in the meridional (y-z) plane, so only the global y + z are returned (the
    global x stays ~0 for a meridional fold). ``local = (0, y, sag)``; each point is
    ``vertex + R_rowmajor · local`` (R applied DIRECTLY). The y-component uses rows
    R[3..5], the z-component rows R[6..8].
    """
    import numpy as np

    y = np.asarray(y_arr, dtype=float)
    sag = np.asarray(sag_arr, dtype=float)
    gy = vertex[1] + R[3] * 0.0 + R[4] * y + R[5] * sag
    gz = vertex[2] + R[6] * 0.0 + R[7] * y + R[8] * sag
    return gy, gz


def resolve_aperture_heights(semi_diameters):
    """Resolve a per-surface drawing half-height ``h`` from the semi-diameters (§3.2).

    A surface whose semi-diameter is ``0`` / non-finite / unreadable falls back to
    the MAX finite positive semi-diameter across all surfaces. If ALL surfaces read
    0/non-finite, every height is ``1.0`` and ``all_zero`` is True (the caller
    appends a note). Never returns a zero/invisible height.

    Returns ``(heights, all_zero)``.
    """
    finite_pos = [
        s for s in semi_diameters if math.isfinite(s) and s > 0.0
    ]
    if finite_pos:
        fallback = max(finite_pos)
        all_zero = False
    else:
        fallback = 1.0
        all_zero = True

    heights = []
    for s in semi_diameters:
        if math.isfinite(s) and s > 0.0:
            heights.append(s)
        else:
            heights.append(fallback)
    return heights, all_zero
