"""tools/asphere_surface.py — the even-asphere authoring primitives.

TWO dispatchable surface-level authoring tools (NOT a LensSpec extension — the flat
SurfaceSpec schema cannot carry the 8 Par coefficient cells; the read->apply guard FLIP
in ``lens_spec`` REFUSES an asphere round-trip rather than flattening it, the CB/grating
precedent):

- ``set_asphere`` — ChangeType a surface to an ``EvenAspheric`` + write its Radius /
  Conic (the Standard columns) + up to 8 Double Par coefficient cells (α2..α16),
  every value read-back-proven. A SHORT coefficient list writes only the supplied leading
  terms (the rest untouched); writing ``0.0`` at an index IS "clear that coefficient" (a
  real read-back-proven write, no separate clear verb); a list longer than 8 is REFUSED
  LOUD (no Par9+ coefficient cell exists). A bare ``{surface}`` ChangeTypes the
  surface to EvenAspheric with ZERO coefficient writes (the ChangeType IS the minimum
  write — mirrors ``set_surface``'s "at least one write").

- ``set_asphere_variable`` — make a coefficient cell an optimizer Variable, proved via the
  optimizer's own ``opt.Variables`` increment (an asphere coefficient cell is a
  GENUINE Double DOF, unlike the CB Order Integer-phantom — so the ``cb_surface``
  opt-count proof path applies verbatim, with NO Integer-cell refusal to port). The agent
  passes the PHYSICAL even order (``term`` in {2,4,6,8,10,12,14,16}, e.g. ``term=4`` ->
  the 4th-order cell Par2).

Every handler returns the uniform never-raise envelope and NEVER raises past its boundary
(the L26 firewall): an EXPECTED failure (bad param / wrong surface type / a read-back
mismatch) is a structured ``{ok:false}`` dict; an unexpected engine throw is caught broad
and resolved to the ``asphere_write`` family. Handler-boundary families: ``asphere_param``
(a bad param value, from ``ToolParamError``), ``surface_asphere`` (a ChangeType / cell /
read-back failure, from ``AsphereWriteError``), ``asphere_write`` (a raw engine throw ->
fail closed).

The SAGY sag falsifier (the decisive L28 proof that a coefficient actually perturbs the
surface SHAPE) is a LIVE-GATE test-side oracle, NOT a per-call gate — it mutates
SemiDiameter, so it stays out of the production tool to keep it side-effect-free.

Live ZOS-API integration: exercised by a live authoring test; unit-tested against
fixture-style fake row/cell doubles.
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _asphere_cells as _ac
from . import _cb_cells as _cb
from . import _lens_common as _lc
from ._analysis_common import error_envelope

# Family tokens (the set_diffraction_grating shape).
_ASPHERE_PARAM = "asphere_param"      # a bad param value family
_ASPHERE_WRITE = "asphere_write"      # a raw engine throw fallback family (fail closed)


# --------------------------------------------------------------------------- #
# Shared validation.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _finite_number(value, label):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number, got {type(value).__name__} {value!r}"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be a finite number (inf/-inf/nan are non-physical), got "
            f"{value!r}"
        )
    return coerced


def _safe(value):
    """JSON-safe float (NaN/inf -> a string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# §1. set_asphere — author an EvenAspheric surface (Radius/Conic + 8 Par cells).
# =========================================================================== #
def set_asphere(session, params):
    """ChangeType a surface to EvenAspheric + author its Radius/Conic + coefficients (§1).

    Params: ``surface`` (int, REQUIRED), ``radius`` (number, optional — the Standard
    ``row.Radius``, default "don't touch"), ``conic`` (number, optional — the Standard
    ``row.Conic``, default "don't touch"), ``coefficients`` (array of 0..8 floats,
    optional — the ORDERED list ``[α2, α4, α6, α8, α10, α12, α14, α16]``; index i ->
    Par(i+1) -> the (2*(i+1))th-order term).

    A SHORT coefficient list writes only the supplied leading terms (the rest
    untouched). Writing ``0.0`` at an index IS "clear that coefficient" (a real
    read-back-proven write). A list length > 8 is REFUSED LOUD (no Par9+ cell exists).
    A bare ``{surface}`` ChangeTypes to EvenAspheric with zero coefficient
    writes (the ChangeType IS the minimum write).

    ``surface_type`` (OPTIONAL, default ``"EvenAspheric"``) selects
    the type — one of {EvenAspheric, OddAsphere, ExtendedAsphere, ExtendedOddAsphere}.
    Even/Odd use 8 absolute-r cells (Even = r^2..r^16, Odd = r^1..r^8). The Extended
    types are NORMALIZED on p=r/``norm_radius`` (norm_radius REQUIRED, >0) and Max-Term
    GATED — N coefficients materialize N cells (max 240); a gated type requires >=1
    coefficient. ``norm_radius`` is REFUSED for Even/Odd.

    Spine (read-back-as-proof): ChangeType -> the resolved type, RE-FETCH the row, prove
    the type by EXACT-token match BEFORE any cell write (a silent ChangeType no-op is
    caught here); for a gated type write norm -> gate (materializes the cells) -> each
    coefficient; each cell layout-verify -> write -> read-back; Radius/Conic via the
    Standard ``row.Radius``/``row.Conic`` with a read-back. Every returned value is READ
    BACK from the cell, NEVER echoed. Never raises past the boundary.

    Returns ``{ok, surface, type, radius, conic, normalized, norm_radius, max_terms,
    coefficients:[read-back floats], was_asphere}``.
    """
    params = _require_dict(params)
    # ATOMICITY: set_asphere is NON-ATOMIC with HONEST DISCLOSURE
    # (no SaveAs/LoadFile checkpoint — too heavy for a single-surface author; recovery
    # is trivial). ``state["mutated"]`` is flipped True right after the
    # ChangeType read-back-proof passes; if a LATER radius/conic or coefficient write then
    # fails, the surface is half-authored, so the {ok:false} envelope MUST stamp
    # ``partial_state:true`` so the agent knows the surface WAS mutated and must re-author
    # (a silent half-write is forbidden). A failure BEFORE the ChangeType proof (a bad
    # param, a ChangeType no-op) leaves the surface untouched -> no partial_state.
    state = {"mutated": False}
    try:
        # S1 GAP-6: the Standard-revert arm (the granular revert). Dispatched BEFORE
        # _set_asphere_impl, so _resolve_surface_type is NOT widened (Standard is not an
        # authorable asphere type — it short-circuits here). A revert that raises
        # ToolParamError / AsphereWriteError lands in the SAME never-raise envelope below.
        if params.get("surface_type") == "Standard":
            return _revert_to_standard(session, params)
        return _set_asphere_impl(session, params, state)
    except ToolParamError as exc:
        # A param-class failure always validates BEFORE any mutation -> never partial.
        return error_envelope("set_asphere", _ASPHERE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        extra = {
            "field": getattr(exc, "field", None),
            "surface": getattr(exc, "surface", None),
        }
        if state["mutated"]:
            extra["partial_state"] = True
        return error_envelope(
            "set_asphere", getattr(exc, "error_family", "surface_write"),
            str(exc), **extra,
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> asphere_write (L26)
        extra = {}
        if state["mutated"]:
            extra["partial_state"] = True
        return error_envelope(
            "set_asphere", _ASPHERE_WRITE,
            f"unexpected engine fault authoring the even asphere ({exc!r}); refusing "
            "rather than shipping an unverified surface",
            **extra,
        )


# =========================================================================== #
# §6 (S1 GAP-6) — revert a surface to Standard (the shared hard-reset primitive).
# =========================================================================== #
def _revert_to_standard_proven(system, surface, *, attempt=None):
    """ChangeType surface ``surface`` to ``Standard`` + read-back-prove Type==Standard.

    The S1 gap-6 PRIMITIVE (probe C idiom — the inverse of ``set_asphere``'s ChangeType).
    Resolves ``SurfaceType.Standard`` via ``_surface_type_member`` (getattr), authors
    the ChangeType, RE-FETCHES the row (never holds a proxy across ChangeType), and
    asserts ``str(row.Type) == "Standard"`` — a silent ChangeType no-op is caught here. Probe
    C proves the coefficient Par cells revert to the Standard ``"(unused)"`` layout for free
    (no separate Par-cell clear); the Type==Standard read-back is sufficient proof.

    BOTH ``set_asphere(surface_type="Standard")`` AND the ``apply_lens_spec`` hard-reset
    pre-pass call THIS (L30 — ONE revert path). Raises ``AsphereWriteError`` on a ChangeType
    throw OR a read-back that is not Standard (a silent no-op). Returns the re-fetched row.

    GRIN: the backward-compatible ``attempt=None`` out-param (a dict). When a
    dict is passed, ``attempt["changetype_invoked"]`` is set ``True`` IMMEDIATELY before
    ``row.ChangeType(settings)`` (after ``GetSurfaceTypeSettings`` — a settings throw leaves
    it ``False``, a clean, unmutated refusal). The ``set_grin`` revert arm reads this flag +
    the raised exception's ``actual`` to derive ``partial_state`` honestly. Asphere callers
    pass no ``attempt`` (keyword-only, default ``None``) and are byte-unchanged.
    """
    standard_member = _ac._surface_type_member(system, "Standard")
    row = system.LDE.GetSurfaceAt(surface)
    try:
        settings = row.GetSurfaceTypeSettings(standard_member)
        # Mark the invoke IMMEDIATELY before ChangeType (a settings throw above
        # leaves ``changetype_invoked`` False = a clean, never-mutated refusal).
        if attempt is not None:
            attempt["changetype_invoked"] = True
        row.ChangeType(settings)
    except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not ChangeType surface {surface} to Standard ({exc!r}); refusing "
            "rather than leaving the surface a non-Standard asphere",
            field="changetype", intended="Standard", actual=None, surface=surface,
        ) from exc
    # Read-back-as-proof: re-fetch the row and prove the revert took. A silent
    # ChangeType no-op leaves the prior asphere type whose Par cells are still coefficients.
    row = system.LDE.GetSurfaceAt(surface)
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not read back surface {surface} Type after the Standard revert "
            f"({exc!r}); the revert is unverifiable — refusing rather than guessing",
            field="surface_type", intended="Standard", actual=None, surface=surface,
        ) from exc
    if type_name != "Standard":
        raise SurfaceWriteError(
            f"surface {surface} is not Standard after ChangeType — the revert silently "
            f"no-opped (Type reads {type_name!r}); refusing rather than leaving a "
            "non-Standard asphere",
            field="surface_type", intended="Standard", actual=type_name, surface=surface,
        )
    return row


def _revert_to_standard(session, params):
    """``set_asphere(surface_type="Standard")`` arm: revert a surface to Standard (§6).

    Params: ``surface`` (int, REQUIRED). REFUSES ``coefficients`` / ``norm_radius`` /
    ``radius`` / ``conic`` LOUD (``asphere_param``, ZERO mutation — a Standard revert is a
    granular revert, not a re-author; geometry is written via ``set_surface``). The geometry
    firewall (``1 <= surface <= N-1``) refuses OBJECT(0). IDEMPOTENT: a surface that is
    already a non-asphere (``asphere_type_of`` is None) is a no-op (``was_asphere:false``, no
    ChangeType). Otherwise delegates to the shared ``_revert_to_standard_proven`` (the SAME
    path the apply hard-reset uses, L30). Returns
    ``{ok, surface, type:"Standard", was_asphere}``.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)

    # Refuse re-author params LOUD, ZERO mutation (a Standard revert is granular — it does
    # NOT take coefficients/norm/radius/conic; geometry is written via set_surface).
    for refused in ("coefficients", "norm_radius", "radius", "conic"):
        if refused in params and params.get(refused) is not None:
            raise ToolParamError(
                f"set_asphere(surface_type='Standard') reverts a surface to Standard; it "
                f"does NOT take {refused!r} (it is a granular revert, not a re-author). "
                "Write geometry via set_surface after the revert."
            )

    # Idempotent: a non-asphere surface stays Standard with NO ChangeType.
    was_asphere = _ac.asphere_type_of(lde.GetSurfaceAt(surface)) is not None
    if not was_asphere:
        return {"ok": True, "surface": surface, "type": "Standard",
                "was_asphere": False}

    _revert_to_standard_proven(system, surface)
    return {"ok": True, "surface": surface, "type": "Standard", "was_asphere": True}


def _resolve_surface_type(params):
    """Resolve + validate the optional ``surface_type`` (default ``"EvenAspheric"``, §3).

    AXIS 1: ``surface_type`` is OPTIONAL; absent ⇒ ``"EvenAspheric"`` (byte-identical
    back-compat). The valid set is the S3 allow-set (the 4 ``ASPHERE_TYPE_INFO`` keys);
    ANY other value (a 2-D/QType member name, a misspelling) -> ``ToolParamError`` LOUD,
    ZERO mutation, validated BEFORE the ChangeType. Returns the ``AsphereTypeInfo``.
    """
    raw = params.get("surface_type")
    if raw is None:
        raw = "EvenAspheric"
    if not isinstance(raw, str):
        raise ToolParamError(
            f"surface_type must be a string in {list(_ac.ASPHERE_TYPE_NAMES)}, got "
            f"{type(raw).__name__} {raw!r}"
        )
    if raw not in _ac.ASPHERE_TYPE_INFO:
        raise ToolParamError(
            f"surface_type {raw!r} is not an authorable asphere type; valid: "
            f"{list(_ac.ASPHERE_TYPE_NAMES)} (the Extended types are normalized + "
            "Max-Term gated; QType/freeform/2-D types are not authored by this tool)"
        )
    return _ac.ASPHERE_TYPE_INFO[raw]


def _set_asphere_impl(session, params, state=None):
    if state is None:
        state = {"mutated": False}
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    # --- Validate every param BEFORE any engine mutation. ---
    surface = _lc._require_int_index(params, "surface")
    # The geometry firewall (1 <= surface <= N-1): refuses OBJECT(0); allows N-1 (you
    # never aspherize the object surface).
    _lc._require_geometry_index(surface, n)

    # surface_type (default EvenAspheric) -> the ORDER MAP descriptor (AXIS 1).
    info = _resolve_surface_type(params)

    # Optional radius/conic: validate (a present-but-bad value is refused LOUD) and
    # record "touch?" so an absent value is left untouched (the set_surface idiom).
    touch_radius = "radius" in params and params.get("radius") is not None
    touch_conic = "conic" in params and params.get("conic") is not None
    radius_value = _finite_number(params["radius"], "radius") if touch_radius else None
    conic_value = _finite_number(params["conic"], "conic") if touch_conic else None

    # Optional coefficients: validate the list shape + each entry BEFORE any mutation
    # (per-type length bound; the gated types require >= 1).
    coeff_values = _validate_coefficients(params.get("coefficients"), info)

    # norm_radius: REQUIRED for the gated (Extended) types, REFUSED for Odd/Even (AXIS 2/4).
    norm_value = _validate_norm_radius(params, info, len(coeff_values))

    was_asphere = _ac.asphere_type_of(lde.GetSurfaceAt(surface)) == info.surface_type

    # --- ChangeType -> the resolved asphere type (the cb_surface / grating recipe). ---
    asphere_member = _ac._surface_type_member(system, info.member)
    row = lde.GetSurfaceAt(surface)
    try:
        settings = row.GetSurfaceTypeSettings(asphere_member)
        row.ChangeType(settings)
    except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not ChangeType surface {surface} to {info.surface_type} ({exc!r}); "
            "refusing rather than authoring on an un-retyped surface",
            field="changetype", intended=info.surface_type, actual=None, surface=surface,
        ) from exc
    # Read-back-as-proof: RE-FETCH the row (never hold a proxy across ChangeType) and
    # prove it is genuinely the resolved type — a ChangeType that silently
    # no-opped would leave the prior type whose Par cells are wrong, caught HERE before
    # any cell write (the spine step 1). EXACT full-token match (never naive ``in``).
    row = lde.GetSurfaceAt(surface)
    if _ac.asphere_type_of(row) != info.surface_type:
        raise SurfaceWriteError(
            f"surface {surface} is not a {info.surface_type} after ChangeType — the "
            "retype silently no-opped; refusing rather than writing coefficient cells "
            "to the wrong surface type",
            field="surface_type", intended=info.surface_type, actual=None,
            surface=surface,
        )

    # The ChangeType read-back-proof PASSED: the surface IS now mutated (retyped). From
    # here on, ANY failure (radius/conic, norm/gate, or a coefficient write) leaves a
    # HALF-AUTHORED surface -> the {ok:false} envelope must disclose ``partial_state:true``.
    # A ChangeType no-op above is caught BEFORE this flag flips.
    state["mutated"] = True

    # --- Radius / Conic via the Standard row properties, read-back-proven. ---
    # The conic BASE uses ABSOLUTE r for ALL four types.
    if touch_radius:
        _write_standard_scalar(row, "Radius", radius_value, surface)
    if touch_conic:
        _write_standard_scalar(row, "Conic", conic_value, surface)

    # --- Write the coefficient cells (gated vs non-gated), read-back-proven. ---
    if info.gated:
        # norm -> gate (materializes N cells) -> each coefficient (computed Header).
        _ac.write_gated_asphere(system, row, info, norm_value, coeff_values)
    else:
        # Direct Par1..Par8 cells (no gate, no norm) — the per-type computed Header.
        for i, value in enumerate(coeff_values):
            _ac.write_computed_double_cell(
                system, row, info.coeff_par(i), info.header(i), value
            )

    # --- Read EVERY field back from the surface for the envelope (never echo). ---
    radius_back = _read_standard_scalar(row, "Radius", surface)
    conic_back = _read_standard_scalar(row, "Conic", surface)
    if info.gated:
        norm_back = _ac.read_computed_double_cell(
            system, row, info.norm_par, _ac._NORM_HEADER
        )
        max_terms_back = _ac.read_gate_cell(system, row, info)
        coefficients_back = [
            _ac.read_computed_double_cell(system, row, info.coeff_par(i), info.header(i))
            for i in range(max_terms_back)
        ]
    else:
        norm_back = None
        max_terms_back = None
        coefficients_back = [
            _ac.read_computed_double_cell(system, row, info.coeff_par(i), info.header(i))
            for i in range(info.max_terms)
        ]

    return {
        "ok": True,
        "surface": surface,
        "type": info.surface_type,
        "radius": _safe(radius_back),
        "conic": _safe(conic_back),
        "normalized": bool(info.normalized),
        "norm_radius": _safe(norm_back) if norm_back is not None else None,
        "max_terms": max_terms_back,
        "coefficients": [_safe(c) for c in coefficients_back],
        # (S7 #13) Parallel ADDITIVE per-coefficient order/term labels (the float list
        # above stays byte-identical for the S2a/S3 carry path). Map-driven from the
        # per-type ORDER MAP so a heavy / normalized asphere never gets a wrong "r²" label.
        "coefficient_orders": _ac.coefficient_order_table(
            info, [_safe(c) for c in coefficients_back]
        ),
        # Honest: was the surface ALREADY this asphere type (an idempotent re-author) —
        # the caller never reads a clean ok:true as a NEW asphere.
        "was_asphere": was_asphere,
    }


def _validate_coefficients(coefficients, info=None):
    """Validate the optional ``coefficients`` list -> a list of finite floats (§3).

    - ``None`` / absent -> ``[]`` (a bare ChangeType; only legal for a non-gated type —
      the gated-requires->=1 rule is enforced separately so an EvenAspheric/OddAsphere
      bare ChangeType stays the S1 contract).
    - a non-list/tuple -> ``ToolParamError`` (the carrier is an ORDERED array).
    - length > the type's ``max_terms`` -> ``ToolParamError`` LOUD (Odd/Even cap at 8,
      no Par9+ cell exists; the gated types cap at 240 — over-240 is refused
      PRE-mutation, never clamp-and-succeed).
    - each entry must be a finite number (bool / non-number / inf / nan refused); a
      ``0.0`` is a VALID clear (kept as a real write).

    ``info`` is the resolved ``AsphereTypeInfo`` (the per-type length bound + order map);
    when ``None`` (the legacy LensSpec ``__post_init__`` call) the EvenAspheric 8-term
    cap is used (byte-identical to the S1 validator).
    """
    if info is None:
        info = _ac.ASPHERE_TYPE_INFO["EvenAspheric"]
    if coefficients is None:
        return []
    if not isinstance(coefficients, (list, tuple)) or isinstance(coefficients, str):
        raise ToolParamError(
            "coefficients must be an ordered array of floats, got "
            f"{type(coefficients).__name__} {coefficients!r}"
        )
    if len(coefficients) > info.max_terms:
        if info.gated:
            raise ToolParamError(
                f"coefficients has {len(coefficients)} entries but {info.surface_type} "
                f"caps at {info.max_terms} Max-Term coefficients (the engine clamps a "
                "larger gate to 240) — refusing rather than dropping the extra terms"
            )
        raise ToolParamError(
            f"coefficients has {len(coefficients)} entries but {info.surface_type} has "
            f"exactly {info.max_terms} coefficient cells (Par1..Par{info.max_terms}); "
            f"there is no Par{info.max_terms + 1}+ coefficient — refusing rather than "
            "dropping the extra terms"
        )
    values = []
    for i, value in enumerate(coefficients):
        coerced = _finite_number(
            value, f"coefficients[{i}] (the order-{info.power(i)} term)"
        )
        values.append(coerced)
    return values


def _validate_norm_radius(params, info, n_coeffs):
    """Validate ``norm_radius``: REQUIRED for gated, REFUSED for non-gated (AXES 2/4).

    For a GATED (Extended) type: ``norm_radius`` is REQUIRED (present + finite + > 0;
    bool / non-number / inf / nan / <= 0 all RAISE — the divide-by-zero trap), AND the
    coefficient list must be non-empty (a zero-coefficient gated author is a degenerate
    normalized sphere — refused LOUD). For a NON-GATED (Odd/Even) type: a present
    ``norm_radius`` is REFUSED (a wrong-type param). Returns the float (gated) or ``None``.
    """
    present = "norm_radius" in params and params.get("norm_radius") is not None
    if not info.gated:
        if present:
            raise ToolParamError(
                f"norm_radius is only valid for the normalized Extended asphere types; "
                f"{info.surface_type} uses absolute-r coefficients (no normalization) — "
                "refusing rather than silently dropping a wrong-type param"
            )
        return None
    # Gated: norm_radius REQUIRED + the coefficient list non-empty.
    if n_coeffs < 1:
        raise ToolParamError(
            f"{info.surface_type} is a Max-Term-gated asphere; it requires at least 1 "
            "coefficient (a zero-coefficient gated author is a degenerate normalized "
            "sphere) — supply a non-empty coefficients array"
        )
    if not present:
        raise ToolParamError(
            f"{info.surface_type} requires norm_radius (the p = r/norm_radius "
            "normalization radius, > 0); it is the gated types' normalization scale"
        )
    value = _finite_number(params["norm_radius"], "norm_radius")
    if value <= 0.0:
        raise ToolParamError(
            f"norm_radius must be > 0 (it normalizes p = r/norm_radius — a non-positive "
            f"value is a divide-by-zero), got {value!r}"
        )
    return value


def _write_standard_scalar(row, attr, value, surface):
    """Write ``row.<attr>`` (Radius/Conic) with a read-back proof."""
    try:
        setattr(row, attr, value)
    except Exception as exc:  # noqa: BLE001 — a property write THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not write {attr}={value!r} to even-asphere surface {surface} "
            f"({exc!r}); the engine rejected the write — refusing rather than shipping "
            "an unverified surface",
            field=attr.lower(), intended=value, actual=None, surface=surface,
        ) from exc
    actual = _read_standard_scalar(row, attr, surface)
    if not _lc._readback_ok(value, actual):
        raise SurfaceWriteError(
            f"even-asphere surface {surface} {attr} did not read back: wrote {value!r}, "
            f"read {actual!r} — the write silently no-opped; refusing rather than "
            "claiming an unverified surface",
            field=attr.lower(), intended=value, actual=actual, surface=surface,
        )


def _read_standard_scalar(row, attr, surface):
    """Read ``row.<attr>`` (Radius/Conic) -> float, THROW-guarded -> surface_asphere."""
    try:
        return float(getattr(row, attr))
    except Exception as exc:  # noqa: BLE001 — a property read THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not read {attr} of even-asphere surface {surface} ({exc!r}); "
            "refusing rather than guessing the value",
            field=attr.lower(), intended=None, actual=None, surface=surface,
        ) from exc


# =========================================================================== #
# §2. set_asphere_variable — make a coefficient cell an optimizer DOF.
# =========================================================================== #
def set_asphere_variable(session, params):
    """Make an even-asphere coefficient cell an optimizer Variable, opt.Variables-proven (§2).

    Params: ``surface`` (int, REQUIRED), ``term`` (number, REQUIRED — the PHYSICAL even
    order in {2,4,6,8,10,12,14,16}; e.g. ``term=4`` -> the 4th-order cell Par2, the
    order the agent reasons in).

    REFUSES a non-EvenAspheric surface LOUD (author it with ``set_asphere`` first) and a
    ``term`` outside {2..16 even} LOUD. Mechanism: ``cell.MakeSolveVariable()`` then the
    ``cb_surface`` opt-count proof copied verbatim — the optimizer's own ``opt.Variables``
    must increment 0->1 (an asphere coefficient cell is a GENUINE Double DOF,
    unlike the CB Order Integer phantom, so the increment proof applies and there is NO
    Integer-cell refusal to port). NEVER raises past the boundary.

    Returns ``{ok, surface, term, par:"Par2", is_variable:true, dof_proven:bool,
    variables_before, variables_after}`` (the ``cb_surface`` envelope shape verbatim).
    """
    params = _require_dict(params)
    try:
        # The prior-solve DISCLOSE stamp, at the PUBLIC entry, so no
        # success return inside the impl can be added later and quietly miss it.
        # Function-local import, the house style already used in this module for
        # ``_optimize_common`` (it imports these tool modules back).
        from . import _optimize_common as _oc_stamp
        return _oc_stamp._stamp_prior_solve_unchecked(
            _set_asphere_variable_impl(session, params), "asphere coefficient (Par)")
    except ToolParamError as exc:
        return error_envelope("set_asphere_variable", _ASPHERE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_asphere_variable", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> asphere_write (L26)
        return error_envelope(
            "set_asphere_variable", _ASPHERE_WRITE,
            f"unexpected engine fault setting the even-asphere variable ({exc!r}); "
            "refusing rather than shipping an unverified DOF",
        )


def _set_asphere_variable_impl(session, params):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    _lc._require_geometry_index(surface, n)

    # term is a NUMBER (integral float coerced); the PER-TYPE physical-order validation
    # happens after we resolve the LIVE surface type (a term legal for one type but not
    # the live type -> asphere_param).
    term = _require_term(params.get("term"))

    row = lde.GetSurfaceAt(surface)
    info = _ac.asphere_type_of(row)
    if info is None:
        raise SurfaceWriteError(
            f"surface {surface} is not an asphere; cannot set an asphere coefficient "
            "variable on it (author it with set_asphere first)",
            field="surface_type", intended="asphere", actual=None, surface=surface,
        )
    info = _ac.ASPHERE_TYPE_INFO[info]

    # Map the physical-order ``term`` to the materialized Par cell via the per-type ORDER
    # MAP + (for gated types) the LIVE Max-Term read-back. Refuses a term beyond the
    # authored Max-Term (that cell is "(unused)" String). Returns (par, header).
    par, header = _resolve_variable_cell(system, row, info, term, surface)

    cell = _ac._cell_by_col(system, row, par)
    # Layout-verify the cell (a drifted Header/kind RAISES) so we never make the WRONG
    # cell a variable.
    _ac._expect_computed_layout(cell, par, header, "double")

    # DOUBLE-VARY IDEMPOTENCY: detect "already Variable" via the
    # solve read-back BEFORE baselining the opt-count. Re-varying an already-variable term
    # is BENIGN — the second MakeSolveVariable does NOT increment opt.Variables (the var is
    # already counted), so baselining the count first and then demanding a +1 increment
    # would mis-flag a legitimate idempotent re-var as a "phantom DOF". An asphere
    # coefficient IS a genuine Double DOF, so there is NO real phantom case to refuse here
    # (unlike the CB Order Integer cell). Return the honest idempotent envelope.
    variable_member = _variable_member(system)
    if _solve_type_name(cell) == str(variable_member):
        return {
            "ok": True,
            "surface": surface,
            "term": term,
            "par": par,
            "is_variable": True,
            "dof_proven": True,
            "was_variable": True,
            "variables_before": None,
            "variables_after": None,
        }

    # Baseline the optimizer var count BEFORE the solve so the increment is the proof
    # (opt.Variables is the authority; the cell read-back alone is NOT proof a
    # cell is a real optimizer DOF). The opt-count helper is copied from cb_surface.
    before = _open_count_close_variables(system)

    try:
        cell.MakeSolveVariable()
    except Exception as exc:  # noqa: BLE001 — a solve THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not make asphere coefficient {par!r} (the order-{term} term) a "
            f"variable on surface {surface} ({exc!r}); the engine rejected the solve",
            field="asphere_variable", intended="Variable", actual=None, surface=surface,
        ) from exc

    # Read back the solve Type == Variable (the cell-level proof) ...
    variable_member = _variable_member(system)
    solve_name = _solve_type_name(cell)
    if solve_name != str(variable_member):
        raise SurfaceWriteError(
            f"asphere variable solve on {par!r} (surface {surface}) did not take "
            f"effect: solve Type reads {solve_name!r} (silent no-op); refusing rather "
            "than claiming a DOF that does not exist",
            field="asphere_variable", intended="Variable", actual=solve_name,
            surface=surface,
        )
    # ... AND confirm the optimizer's own DOF count incremented (a flag that
    # only reads back Variable is NOT a real DOF). dof_proven is POSITIVELY established
    # ONLY when BOTH counts are readable ints AND the increment is exactly +1.
    after = _open_count_close_variables(system)
    dof_proven = (
        isinstance(before, int) and isinstance(after, int) and after == before + 1
    )
    warning = None
    if isinstance(before, int) and isinstance(after, int) and not dof_proven:
        # The cell reads Variable but the optimizer count did NOT increment by one — the
        # solve is not a real optimizer variable; refuse a phantom DOF.
        raise SurfaceWriteError(
            f"asphere variable on {par!r} (surface {surface}) reads back "
            f"Variable but the optimizer DOF count did not increment ({before} -> "
            f"{after}); the solve is not a real optimizer variable — refusing rather "
            "than claiming a phantom DOF",
            field="asphere_variable_dof", intended=before + 1, actual=after,
            surface=surface,
        )
    if not dof_proven:
        # EITHER count is non-int (an asymmetric optimizer flake): the cell reads back
        # Variable but the opt.Variables increment proof was NOT established — warn and
        # surface dof_proven:false so a caller never reads a bare ok:true as a proven DOF.
        warning = (
            "the optimizer DOF count could not be confirmed to have incremented by one "
            f"(the optimizer may have been unavailable: before={before!r}, "
            f"after={after!r}); the cell reads back Variable but the opt.Variables "
            "increment proof was NOT established — treat this DOF as UNCONFIRMED"
        )

    result = {
        "ok": True,
        "surface": surface,
        "term": term,
        "par": par,
        "is_variable": True,
        "dof_proven": dof_proven,
        # Honest: the term was NOT already a Variable on entry (the idempotent re-var path
        # above returns was_variable:True and never reaches here).
        "was_variable": False,
        "variables_before": before if isinstance(before, int) else None,
        "variables_after": after if isinstance(after, int) else None,
    }
    # Symmetric conic+coeff degeneracy check (the K→asphere direction is in
    # set_variable; this is the asphere→K direction).
    from . import _optimize_common as _oc
    degen = _oc._check_conic_coeff_degeneracy(
        system, lde.GetSurfaceAt(surface), surface, variable_member,
    )
    if degen is not None:
        warning = f"{warning}; {degen}" if warning is not None else degen
    if warning is not None:
        result["warning"] = warning
    return result


def _require_term(value):
    """Require ``term`` to be a POSITIVE-integer PHYSICAL order (per-type, §3/c.5).

    Coerces an integral float (``4.0`` -> ``4``, the "number = handler
    accepts integral float" contract); rejects a bool, a non-integral float, a string,
    NaN/inf, and a non-positive value LOUD. The PER-TYPE order set (Even = {2,4..16},
    Odd = {1..8}, the gated types = their materialized powers) is enforced by
    ``_resolve_variable_cell`` against the LIVE surface type — a term legal for one type
    but not the live type is refused there (so a term=3 on an EvenAspheric is rejected
    by the order map, not here).
    """
    import math
    if isinstance(value, bool):
        raise ToolParamError(
            f"term must be a positive integer physical order, not a bool ({value!r})"
        )
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            coerced = int(value)
        else:
            raise ToolParamError(
                f"term must be a positive integer physical order, got non-integral "
                f"float {value!r}"
            )
    else:
        raise ToolParamError(
            f"term must be a positive integer physical order, got "
            f"{type(value).__name__} {value!r}"
        )
    if coerced < 1:
        raise ToolParamError(
            f"term must be a positive integer physical order (>= 1), got {coerced}"
        )
    return coerced


def _resolve_variable_cell(system, row, info, term, surface):
    """Map a physical-order ``term`` to its materialized Par cell (per-type, §3/c.5).

    Finds the 0-based coefficient index ``i`` whose physical power ``info.power(i) ==
    term`` (EvenAspheric: 2,4..16; OddAsphere: 1..8; ExtendedAsphere: even normalized
    powers; ExtendedOddAsphere: all normalized powers). For a GATED type the live
    Max-Term gate is read back and the term is REFUSED if its index >= Max-Term (that
    cell is an unmaterialized ``"(unused)"`` String). A term not in the type's power set
    -> ``asphere_param`` (a term legal for one type but not the live type). Returns
    ``(par, header)``.
    """
    # The legal index range: 0..(max_terms-1) for non-gated; 0..(live Max-Term - 1) for
    # gated (read back the authored gate).
    if info.gated:
        live_max = _ac.read_gate_cell(system, row, info)
    else:
        live_max = info.max_terms
    valid_orders = [info.power(i) for i in range(info.max_terms)]
    index = None
    for i in range(info.max_terms):
        if info.power(i) == term:
            index = i
            break
    if index is None:
        raise ToolParamError(
            f"term {term} is not a valid physical order for a {info.surface_type} "
            f"surface; valid orders: {valid_orders}"
        )
    if index >= live_max:
        raise ToolParamError(
            f"term {term} (coefficient index {index}) is beyond the authored Max-Term "
            f"({live_max}) on this {info.surface_type} surface — that cell is not "
            "materialized ('(unused)'); author more coefficients with set_asphere first"
        )
    return info.coeff_par(index), info.header(index)


def _variable_member(system):
    """The live ``SolveType.Variable`` member (reused from the optimize tier)."""
    from . import _optimize_common as _oc
    return _oc._solve_type_variable_enum(system)


def _solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the read-back truth source).

    THROW-guarded -> ``SurfaceWriteError`` (a read THROW on the proof leaves the DOF
    unverifiable — refuse rather than guess it took).
    """
    try:
        return str(cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> surface_asphere
        raise SurfaceWriteError(
            f"could not read back the solve type of an even-asphere cell ({exc!r}); "
            "the variable is unverifiable — refusing rather than guessing it took",
            field="asphere_variable", intended="Variable", actual=None, surface=None,
        ) from exc


def _open_count_close_variables(system):
    """Open the optimizer, read ``opt.Variables`` (the DOF count), close it (L22).

    Copied verbatim from ``cb_surface._open_count_close_variables``: a cell that merely
    reads back Variable is NOT proof it is a real optimizer DOF —
    ``ILocalOptimization.Variables`` (the only var-count member; ``NumberOfVariables``
    does NOT exist on this build) is the authority. Opens ONCE, reads, ``Close()`` in
    finally (the L22 single-seat reap). Returns the int count, or ``None`` if the
    optimizer is unavailable / the count is unreadable (a non-fatal degradation — the
    caller treats a None as "could not confirm the increment", never a crash).
    """
    opt = None
    try:
        opt = system.Tools.OpenLocalOptimization()
        if opt is None:
            return None
        try:
            return int(opt.Variables)
        except Exception:  # noqa: BLE001 — an unreadable count degrades to None
            return None
    except Exception:  # noqa: BLE001 — the optimizer being unavailable is non-fatal
        return None
    finally:
        if opt is not None:
            try:
                opt.Close()
            except Exception:  # noqa: BLE001 — teardown must never raise (L22)
                pass


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_ASPHERE_SPEC = ToolSpec(
    name="set_asphere",
    handler=set_asphere,
    required_params=("surface",),
    param_types={
        "surface": "number",
        "surface_type": "string",
        "radius": "number",
        "conic": "number",
        "norm_radius": "number",
        "coefficients": "array",
    },
    description=(
        "Make a surface a heavy asphere: pick surface_type (EvenAspheric [default] | "
        "OddAsphere | ExtendedAsphere | ExtendedOddAsphere) and set its radius/conic + "
        "an ordered coefficients array. Even/Odd use 8 absolute-r cells (Even = "
        "r^2..r^16, Odd = r^1..r^8); the Extended types are NORMALIZED on "
        "p=r/norm_radius (norm_radius REQUIRED, >0) and gated — give N coefficients and "
        "N cells materialize (max 240). Proves the change by reading back the surface "
        "type, the gate, the norm radius, and every coefficient cell — never echoing "
        "the request. Gotcha: norm_radius is required for the Extended types and "
        "refused for Even/Odd; a gated type needs at least 1 coefficient; the flat "
        "lens-spec round-trip carries these (this tool authors them). See "
        "set_asphere_variable, describe_surfaces."
    ),
)

SET_ASPHERE_VARIABLE_SPEC = ToolSpec(
    name="set_asphere_variable",
    handler=set_asphere_variable,
    required_params=("surface", "term"),
    param_types={"surface": "number", "term": "number"},
    description=(
        "Make an asphere coefficient an optimizer Variable (term is the physical order "
        "of the coefficient: EvenAspheric uses 2,4,..16; OddAsphere uses 1..8; the "
        "Extended types use their materialized normalized powers — e.g. term=4 varies "
        "the order-4 coefficient), proved by the optimizer's own DOF count "
        "incrementing. Refuses a surface that is not an asphere (author it with "
        "set_asphere first), a term not in the live type's order set, and a term beyond "
        "the authored Max-Term on a gated type. See set_asphere, set_variable."
    ),
)

TOOL_SPECS = (SET_ASPHERE_SPEC, SET_ASPHERE_VARIABLE_SPEC)
