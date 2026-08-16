"""tools/grin_surface.py — the GRIN (Gradient2 / Gradient3) authoring primitives (GRIN §1/§2).

TWO dispatchable surface-level authoring tools (NOT a LensSpec extension — the flat
SurfaceSpec schema cannot carry the Par coefficient cells; ``lens_spec`` REFUSES a
Gradient2 round-trip fail-closed rather than flattening it, the CB/grating/asphere
precedent):

- ``set_grin`` — ChangeType a surface to a ``Gradient2`` (pure even-power radial) or a
  ``Gradient3`` (radial + axial).
  **CELL CONVENTION (live-falsified against ``EFFL == R/(n-1)``):** a Gradient2
  Par polynomial is the index **SQUARED** — ``n(r)² = n0 + Nr2·r² + … + Nr12·r¹²`` — exactly
  as Zemax's own "Gradient 2" surface documents it, so ``n0`` is the base index SQUARED
  (``n0 = 2.25`` builds a medium of index 1.5). A ``Gradient3`` is the plain index:
  ``n(r,z) = n0 + Nr2·r² + Nr4·r⁴ + Nr6·r⁶ + Nz1·z + Nz2·z² + Nz3·z³``. r and z in mm,
  absolute coefficients. The tool takes the CELL value as the engine defines it and does NOT
  convert (``analyze_grin_profile`` reports the resulting PHYSICAL index). ``n0`` is REQUIRED
  on the authoring path; ``coefficients`` is a MAP keyed by the exact Header tokens
  {Nr2,Nr4,Nr6,Nr8,Nr10,Nr12}. The call states the COMPLETE profile: every OMITTED
  radial term is authored to a read-back-proven ``0.0`` (a re-author NEVER inherits a stale
  term — the dogfooded curved-stop-reset silent-wrong closed for ``apply_lens_spec``). The
  ``Delta T`` trace step is written to ``1.0`` internally (disclosed as ``grin_step_size``,
  never a user param). ``radius``/``conic`` are OPTIONAL don't-touch base-shape params
  (NOT part of the complete state). ``surface_type='Standard'`` reverts (the engine
  clears the coefficients), reusing the shared ``_revert_to_standard_proven``.

- ``set_grin_variable`` — make a GRIN coefficient (n0 or Nr2..Nr12) an optimizer Variable,
  proved by the optimizer's own ``opt.Variables`` count (a GRIN coefficient cell is a
  GENUINE Double DOF). REFUSES a non-GRIN surface (author with ``set_grin`` first), the
  ``Delta T`` trace-step cell (a numerical-accuracy knob, not a design DOF), and any
  unknown/Integer cell.

Every handler returns the uniform never-raise envelope and NEVER raises past its boundary
(the never-raise firewall): an EXPECTED failure (bad param / wrong surface type / a read-back
mismatch) is a structured ``{ok:false}`` dict; an unexpected engine throw is caught broad
and resolved to the ``grin_write`` family. The family contract is a QUARTET:
``grin_param`` (a bad param, from ``ToolParamError``), ``surface_grin`` (a ChangeType / cell
/ read-back failure, from ``GrinWriteError``), ``grin_write`` (a raw engine throw -> fail
closed), and ``surface_write`` (the shared ``_revert_to_standard_proven``'s BASE
``SurfaceWriteError`` on the revert path ONLY). EVERY envelope — success AND refusal —
carries ``grin_wavelength_blind:true``: the cell coefficients are
monochromatic.

Live ZOS-API integration: exercised by a live integration test; unit-tested against the
fixture-style fake row/cell doubles.
"""
import math

from ..errors import SurfaceWriteError, ToolParamError, GrinWriteError
from ..server import ToolSpec
from . import _grin_cells as _grin
from . import _lens_common as _lc
from ._analysis_common import error_envelope

# Family tokens (the quartet's pre-mutation + fail-closed families; ``surface_grin`` is
# carried by ``GrinWriteError.error_family``, ``surface_write`` by the shared revert primitive).
_GRIN_PARAM = "grin_param"      # a bad param value family (ToolParamError)
_GRIN_WRITE = "grin_write"      # a raw engine throw fallback family (fail closed)

# Refuse-tier (§2.1): a RECOGNIZED-but-unauthorable GRIN family member routes to
# ``surface_grin`` STATING WHY it cannot be authored; a genuine typo stays ``grin_param``.
# Each reason is the real engine-level obstacle, not a scheduling note: these members do
# not take their index profile from the Par cells this primitive writes.
_GRIN_DEFERRAL_REASON = {
    "Gradium": ("its axial index profile is supplied by a vendor glass-catalog entry, "
                "not by the Par coefficient cells this primitive writes"),
    "GridGradient": ("its index field is supplied by an external sampled grid file, "
                     "not by the Par coefficient cells this primitive writes"),
}  # every OTHER recognized-but-unauthorable member -> the generic unverified-layout reason
_GRIN_TYPE_EXPANSION_REASON = (
    "its Par-cell layout has not been verified against the live engine, so authoring it "
    "would mean guessing which cell is which")


# --------------------------------------------------------------------------- #
# Shared helpers.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise: a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _safe(value):
    """JSON-safe float (NaN/inf -> a string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float
    return safe_float(value)


def _finite_number(value, label):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float."""
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


def _surface_hint(params):
    """A best-effort ``surface`` int for a refusal envelope (never raises)."""
    if not isinstance(params, dict):
        return None
    v = params.get("surface")
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v == int(v):
        return int(v)
    return None


def _exc_surface(exc, params):
    """The refusal envelope's ``surface``: the exception's, else the requested one.

    Substrate ``GrinWriteError``s commonly carry ``surface=None`` (the cell-layer helpers do
    not know the surface index); fall back to ``_surface_hint(params)`` so the refusal names
    the surface the caller asked about rather than dropping the key. Interior GRIN surfaces
    are ``>= 1`` so ``surface`` is never a falsy-0 (an explicit ``is not None`` check anyway).
    """
    s = getattr(exc, "surface", None)
    return s if s is not None else _surface_hint(params)


def _grin_error(tool, family, message, *, surface=None, field=None, partial_state=False):
    """Build a GRIN ``{ok:false}`` envelope with the MANDATORY ``grin_wavelength_blind``.

    Every refusal envelope carries ``grin_wavelength_blind:true`` (a fixed part of the contract —
    the key rides EVERY envelope, success AND refusal); ``surface``/``field`` are best-effort
    diagnostics; ``partial_state`` is stamped iff the engine state is not proven unchanged.
    """
    extra = {"grin_wavelength_blind": True}
    if surface is not None:
        extra["surface"] = surface
    if field is not None:
        extra["field"] = field
    if partial_state:
        extra["partial_state"] = True
    return error_envelope(tool, family, message, **extra)


def _require_interior_index(surface, n):
    """GRIN interior firewall: ``1 <= surface <= N-2`` — GRIN-worded, NOT stop-worded.

    A Gradient2 defines the medium FOLLOWING its row, so OBJECT(0) and IMAGE(N-1) — which
    have no following medium — are REFUSED (an image-surface GRIN is an inert author + a
    zero-gradient DOF, the class on the authoring side). This deliberately TIGHTENS the
    shipped ``_require_geometry_index`` (which allows IMAGE).
    """
    if not (1 <= surface <= n - 2):
        raise ToolParamError(
            f"surface {surface} is not an interior GRIN surface; a GRIN medium is defined "
            f"FOLLOWING its surface, so valid GRIN surfaces are 1..{n - 2} (OBJECT 0 and "
            f"IMAGE {n - 1} have no following medium — an image-surface GRIN is inert; N={n})"
        )
    return surface


# =========================================================================== #
# §1. set_grin — author a GRIN surface (n0 + the complete per-type coefficient state).
# =========================================================================== #
def set_grin(session, params):
    """ChangeType a surface to a GRIN type + author its complete coefficient state (§1).

    Params: ``surface`` (int, REQUIRED, interior 1..N-2), ``surface_type`` (optional,
    {"Gradient2"[default], "Gradient3", "Standard"=revert}), ``n0`` (number, REQUIRED on
    the authoring path — the base term of the type's index polynomial: on a Gradient2 the
    polynomial is n SQUARED, so n0 is the base index SQUARED (n0=2.25 builds a medium of
    physical index 1.5); on a Gradient3 the polynomial is the index itself, so n0 IS the
    base index), ``coefficients`` (optional map keyed by the type's coefficient tokens —
    OMITTED terms authored to 0.0, the COMPLETE profile), ``radius``/``conic`` (optional
    don't-touch base shape).

    Spine (read-back-as-proof): validate everything pre-mutation -> compute the complete Par
    state (Delta T=1.0, n0, and the resolved type's coefficient tokens supplied-or-0.0) ->
    STORE the original Type token BEFORE ChangeType -> ChangeType to the resolved
    GRIN type (mark ``attempted`` at invoke)
    -> RE-FETCH + prove the type by exact-token match BEFORE any cell write -> write
    radius/conic + Par1..Par8 (each read-back-proven via the ZERO-BOUNDARY oracle) -> assemble
    the envelope ENTIRELY from read-backs. Never raises past the boundary.

    Returns ``{ok, surface, type, n0, coefficients, coefficient_orders, radius, conic,
    grin_step_size, grin_wavelength_blind, was_grin}``.
    """
    params = _require_dict(params)
    # NON-ATOMIC with HONEST DISCLOSURE (makes recovery trivial): ``mutated``
    # is the ``attempted`` flag — flipped True at the ChangeType INVOKE; a later cell write
    # failure => a half-authored surface => the {ok:false} envelope stamps ``partial_state``.
    # A read-back that POSITIVELY proves the pre-state persisted (a proven ChangeType no-op)
    # CLEARS it (a proven no-op must not report partial).
    state = {"mutated": False}
    try:
        # The unknown-param firewall runs BEFORE the author/revert dispatch, so
        # the REVERT path (surface_type="Standard") also refuses ``delta_t`` / a typo'd key
        # LOUD — the same silent-wrong the authoring path never ships. (Idempotent: the
        # revert arm's own explicit n0/coefficients/radius/conic refusal still fires first for
        # those known keys; only genuinely-unknown keys are newly refused on the revert path.)
        _reject_unknown_grin_params(params)
        if params.get("surface_type") == "Standard":
            return _revert_grin(session, params, state)
        return _set_grin_impl(session, params, state)
    except ToolParamError as exc:
        # A param-class failure normally validates BEFORE any mutation. Defensive backstop:
        # honor ``state["mutated"]`` so a ToolParamError that somehow escapes
        # post-ChangeType is never misreported as a clean (non-partial) refusal over a
        # mutated GRIN surface.
        return _grin_error("set_grin", _GRIN_PARAM, str(exc),
                           surface=_surface_hint(params), partial_state=state["mutated"])
    except SurfaceWriteError as exc:
        # GrinWriteError -> "surface_grin"; the shared revert primitive's BASE
        # SurfaceWriteError -> "surface_write" (the quartet).
        return _grin_error(
            "set_grin", getattr(exc, "error_family", "surface_write"), str(exc),
            surface=_exc_surface(exc, params), field=getattr(exc, "field", None),
            partial_state=state["mutated"],
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> grin_write
        return _grin_error(
            "set_grin", _GRIN_WRITE,
            f"unexpected engine fault authoring the GRIN surface ({exc!r}); refusing "
            "rather than shipping an unverified surface",
            surface=_surface_hint(params), partial_state=state["mutated"],
        )


def _resolve_grin_surface_type(params):
    """Resolve + validate the optional ``surface_type`` (default "Gradient2", §1.1).

    The valid set is {"Gradient2","Gradient3","Standard"} (case-EXACT full token);
    "Standard" is short-circuited to the revert arm in the handler BEFORE this is reached,
    so here only "Gradient2"/"Gradient3"/absent is authorable. ANY other value ->
    ``ToolParamError`` LOUD, ZERO mutation.
    """
    raw = params.get("surface_type")
    if raw is None:
        raw = "Gradient2"
    if not isinstance(raw, str):
        raise ToolParamError(
            "surface_type must be a string in ['Gradient2', 'Gradient3', 'Standard'], got "
            f"{type(raw).__name__} {raw!r}"
        )
    if raw in ("Gradient2", "Gradient3", "Standard"):
        return raw
    # Refuse tier: a RECOGNIZED GRIN family member that is NOT authorable
    # -> surface_grin, naming the FROZEN token + its deferral ticket, ZERO mutation.
    # Keyed on the 12-member FAMILY recognizer (exact token, NEVER substring). Pure
    # string resolver -> no engine access; fires PRE-ChangeType (this runs before the
    # GetSurfaceTypeSettings/ChangeType in _set_grin_impl). GrinWriteError.error_family is
    # "surface_grin"; the shipped ``except SurfaceWriteError`` handler maps it -> no handler
    # change.
    fam = _grin.grin_family_type_of_name(raw)
    if fam is not None:
        reason = _GRIN_DEFERRAL_REASON.get(fam, _GRIN_TYPE_EXPANSION_REASON)
        raise GrinWriteError(
            f"surface_type {fam!r} is a recognized GRIN family member that is NOT "
            f"authorable here (this primitive authors 'Gradient2'/'Gradient3' only): "
            f"{reason}. Refusing before any engine mutation.",
            field="surface_type", intended=fam, actual=None, surface=_surface_hint(params),
        )
    # A pure typo / non-GRIN token stays a grin_param param-domain refusal (unchanged).
    raise ToolParamError(
        f"surface_type {raw!r} is not valid; the GRIN primitive authors 'Gradient2' "
        "(radial, default) or 'Gradient3' (radial+axial), and reverts with 'Standard' "
        "(case-EXACT; the other GRIN members are deferred)"
    )


def _require_n0(params):
    """Require ``n0`` on the authoring path -> a finite float (§1.1)."""
    if "n0" not in params or params.get("n0") is None:
        raise ToolParamError(
            "n0 is required to author a GRIN surface (the base term of the index polynomial: "
            "on a Gradient2 the polynomial is n^2 so n0 is the base index SQUARED, e.g. "
            "n0=2.25 for index 1.5; on a Gradient3 n0 is the base index itself); it "
            "is its OWN param, not a coefficients-map key"
        )
    return _finite_number(params["n0"], "n0")


def _validate_coefficients_map(coefficients, valid_tokens):
    """Validate the optional ``coefficients`` MAP -> {token: finite float} (§1.1).

    Keys validated against THIS type's coefficient token set (``info.coeff_map_tokens()`` —
    radial for Gradient2, radial∪axial for Gradient3; case-EXACT): an unknown / wrong-family
    / case-miss key (``"Nr3"``, ``"nr2"``, ``"Delta T"``, ``"n0"`` — n0 is its OWN param;
    ``"Nz1"`` on a Gradient2 — INCLUDING an explicit ``0.0`` value, presence not value
    triggers) -> ``grin_param`` PRE-mutation. Each value finite (bool refused);
    ``0.0`` valid.
    """
    if coefficients is None:
        return {}
    if not isinstance(coefficients, dict):
        raise ToolParamError(
            f"coefficients must be a map keyed by {list(valid_tokens)}, got "
            f"{type(coefficients).__name__} {coefficients!r}"
        )
    valid = set(valid_tokens)
    out = {}
    for key, value in coefficients.items():
        if key not in valid:
            raise ToolParamError(
                f"unknown coefficient key {key!r}; valid keys (case-EXACT): "
                f"{list(valid_tokens)} (n0 is its OWN param, not a map key; the Delta-T "
                "trace step is internal; axial Nz* keys are Gradient3-only)"
            )
        out[key] = _finite_number(value, f"coefficients[{key!r}]")
    return out


def _coerce_radius(value):
    """Coerce the optional ``radius``: finite-nonzero | native ±inf | "inf"/"-inf".

    ``"nan"`` / float-nan / bool -> ``grin_param`` (a planar/curved radius must be finite or
    ±inf). Routed through the shipped ``_coerce_inf`` channel (bool + non-sentinel-string
    already rejected there), then nan is rejected explicitly.

    SPEC-NOTE (live-divergence fix): a literal ``radius == 0.0`` is REFUSED
    pre-mutation. The live engine NORMALIZES a zero radius to inf (probe_grin_flat_radius:
    a raw ``row.Radius = 0.0`` reads back inf — the engine treats a 0 radius as a flat/planar
    surface), so a ``0.0`` write silently no-ops and the read-back-proof would refuse
    ``surface_grin`` AFTER the ChangeType (a confusing partial_state). Refusing the degenerate
    0.0 up front (ZERO engine mutation) is the honest anti-silent-wrong choice and names the
    supported flat-face author (``radius=inf`` OR omit radius — a fresh surface is planar).
    Any NONZERO finite radius is a real curvature and passes.
    """
    from .lens_spec import _coerce_inf
    coerced = _coerce_inf(value, label="radius")
    if isinstance(coerced, float) and math.isnan(coerced):
        raise ToolParamError(
            "radius may not be nan (a planar/curved radius must be a finite number or "
            "±inf); 'nan'/nan is rejected"
        )
    if coerced == 0.0:  # covers 0.0, -0.0, int 0 -> all the degenerate zero radius
        raise ToolParamError(
            "radius 0.0 is degenerate — the engine normalizes a zero radius to inf (a "
            "flat/planar surface), so a literal 0.0 write silently no-ops. Author a flat "
            "GRIN face with radius=inf (native inf or the string 'inf'), or OMIT radius "
            "entirely (a surface with no curvature is already planar). A nonzero finite "
            "radius is a real curvature."
        )
    return coerced


def _coerce_conic(value):
    """Coerce the optional ``conic``: STRICTLY finite (native/string inf, nan, bool
    all -> ``grin_param``)."""
    return _finite_number(value, "conic")


_KNOWN_SET_GRIN_PARAMS = frozenset(
    {"surface", "surface_type", "n0", "coefficients", "radius", "conic"})


def _reject_unknown_grin_params(params):
    """Refuse any unknown top-level param LOUD, pre-mutation (§1.2 step 1).

    The Delta-T trace step is INTERNAL — silently ignoring a ``delta_t`` param (or
    any typo'd key) would let a caller believe they tuned something they did not, the
    silent-wrong ``set_grin`` must never ship.
    """
    unknown = sorted(k for k in params if k not in _KNOWN_SET_GRIN_PARAMS)
    if unknown:
        raise ToolParamError(
            f"unknown param(s) {unknown!r} for set_grin; valid keys: "
            f"{sorted(_KNOWN_SET_GRIN_PARAMS)}. The Delta-T trace step is internal "
            "and cannot be set."
        )


def _set_grin_impl(session, params, state):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    # --- Validate every param BEFORE any engine mutation. ---
    # (the unknown-param firewall now runs in ``set_grin`` before dispatch, covering
    # BOTH the authoring and revert arms — so it is NOT repeated here.)
    surface = _lc._require_int_index(params, "surface")
    _require_interior_index(surface, n)  # interior firewall (1..N-2)

    surface_type = _resolve_grin_surface_type(params)  # Gradient2/Gradient3 (Standard short-circuits)
    info = _grin.GRIN_TYPE_INFO[surface_type]
    target = info.type_token

    n0_value = _require_n0(params)
    coeff_map = _validate_coefficients_map(
        params.get("coefficients"), info.coeff_map_tokens())

    touch_radius = "radius" in params and params.get("radius") is not None
    touch_conic = "conic" in params and params.get("conic") is not None
    radius_value = _coerce_radius(params["radius"]) if touch_radius else None
    conic_value = _coerce_conic(params["conic"]) if touch_conic else None

    # --- The COMPLETE intended Par state: Delta T=1.0, n0, each coefficient
    # (radial AND, for Gradient3, axial) supplied-or-0.0. ---
    intended = {"Delta T": info.default_delta_t, "n0": n0_value}
    for token in info.coeff_map_tokens():
        intended[token] = coeff_map.get(token, 0.0)

    # --- 3a: read + STORE the exact original Type token BEFORE any mutation. ---
    row = lde.GetSurfaceAt(surface)
    try:
        original_type = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — an unreadable pre-state -> refuse PRE-mutation
        raise GrinWriteError(
            f"could not read surface {surface} Type before the GRIN author ({exc!r}); the "
            "no-op carve-out and was_grin both depend on it — refusing rather than proceeding",
            field="surface_type", intended=target, actual=None, surface=surface,
        ) from exc
    was_grin = _grin.grin_type_of_name(original_type) is not None

    # --- 3b: ChangeType -> target (mark ``attempted`` at the ChangeType INVOKE). ---
    member = _grin._surface_type_grin(system, info.member)  # getattr
    row = lde.GetSurfaceAt(surface)
    try:
        settings = row.GetSurfaceTypeSettings(member)
    except Exception as exc:  # noqa: BLE001 — a settings throw leaves state clean (not mutated)
        raise GrinWriteError(
            f"could not get the {target} surface-type settings for surface {surface} "
            f"({exc!r}); refusing rather than authoring on an un-retyped surface",
            field="changetype", intended=target, actual=None, surface=surface,
        ) from exc
    # flip ``attempted`` IMMEDIATELY before ChangeType — a post-invoke throw/no-op
    # then reasons about ``partial_state`` from the read-back (never a stale "clean refusal").
    state["mutated"] = True
    try:
        row.ChangeType(settings)
    except Exception as exc:  # noqa: BLE001 — a ChangeType throw -> state unknown -> partial
        raise GrinWriteError(
            f"could not ChangeType surface {surface} to {target} ({exc!r}); refusing "
            "rather than authoring on an un-retyped surface",
            field="changetype", intended=target, actual=None, surface=surface,
        ) from exc

    # --- Step 4: RE-FETCH + prove the target type BEFORE any cell write. ---
    row = lde.GetSurfaceAt(surface)
    try:
        after_type = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a read-back THROW -> state unknown -> partial
        raise GrinWriteError(
            f"could not read back surface {surface} Type after the {target} ChangeType "
            f"({exc!r}); the retype is unverifiable — refusing rather than guessing",
            field="surface_type", intended=target, actual=None, surface=surface,
        ) from exc
    if _grin.grin_type_of_name(after_type) != target:
        if after_type == original_type:
            # Carve-out: a POSITIVELY-proven no-op (Type unchanged) -> NOT partial.
            state["mutated"] = False
            raise GrinWriteError(
                f"surface {surface} is still {original_type!r} after ChangeType — the "
                "retype silently no-opped; refusing rather than writing coefficient cells "
                "to the wrong surface type",
                field="surface_type", intended=target, actual=after_type,
                surface=surface,
            )
        # A THIRD type (≠ original, ≠ target) -> the engine mutated -> partial_state.
        raise GrinWriteError(
            f"surface {surface} retyped to {after_type!r} (neither the original "
            f"{original_type!r} nor {target}) after ChangeType — refusing rather than "
            "writing coefficient cells to the wrong surface type",
            field="surface_type", intended=target, actual=after_type, surface=surface,
        )

    # --- Step 5: radius/conic via the Standard row properties, read-back-proven. ---
    if touch_radius:
        _write_standard_scalar(row, "Radius", radius_value, surface)
    if touch_conic:
        _write_standard_scalar(row, "Conic", conic_value, surface)

    # --- Step 6: Par1..Par8 in the RESOLVED type's table order, read-back-proven. ---
    for (token, _par, _h, _k, _role, _pw) in info.params:
        _grin.write_grin_cell(system, row, token, intended[token], info)

    # --- Step 7: assemble the envelope ENTIRELY from read-backs (never echo). ---
    row = lde.GetSurfaceAt(surface)
    n0_back = _grin.read_grin_cell(system, row, "n0", info)
    delta_t_back = _grin.read_grin_cell(system, row, "Delta T", info)
    coeff_back = {t: _grin.read_grin_cell(system, row, t, info)
                  for t in info.coeff_map_tokens()}
    radius_back = _read_standard_scalar(row, "Radius", surface)
    conic_back = _read_standard_scalar(row, "Conic", surface)

    return {
        "ok": True,
        "surface": surface,
        "type": target,
        "n0": _safe(n0_back),
        "coefficients": {t: _safe(coeff_back[t]) for t in info.coeff_map_tokens()},
        "coefficient_orders": _coefficient_orders(info, coeff_back),
        "radius": _safe(radius_back),
        "conic": _safe(conic_back),
        "grin_step_size": _safe(delta_t_back),
        "grin_wavelength_blind": True,
        "was_grin": was_grin,
    }


def _coefficient_orders(info, coeff_back):
    """The additive per-coefficient order/term labels list (§1.3 — ``_term_label``).

    Iterates the resolved type's coefficient cells (radial ∪ axial, minus n0); the r/z axis
    label comes from ``_term_label`` (token-prefix derived), NEVER a hardcoded radial-only
    format string (which would mislabel a Gradient3 Nz cell — Nz2.power == Nr2.power).
    """
    out = []
    for (token, par, _h, _k, _role, power) in info.coeff_cells():
        out.append({
            "coefficient": token,
            "par": par,
            "term": _grin._term_label(token, power),
            "value": _safe(coeff_back[token]),
        })
    return out


def _write_standard_scalar(row, attr, value, surface):
    """Write ``row.<attr>`` (Radius/Conic) with a read-back proof (clone the asphere writer)."""
    try:
        setattr(row, attr, value)
    except Exception as exc:  # noqa: BLE001 — a property write THROW -> surface_grin
        raise GrinWriteError(
            f"could not write {attr}={value!r} to GRIN surface {surface} ({exc!r}); the "
            "engine rejected the write — refusing rather than shipping an unverified surface",
            field=attr.lower(), intended=value, actual=None, surface=surface,
        ) from exc
    actual = _read_standard_scalar(row, attr, surface)
    if not _lc._readback_ok(value, actual):
        raise GrinWriteError(
            f"GRIN surface {surface} {attr} did not read back: wrote {value!r}, read "
            f"{actual!r} — the write silently no-opped; refusing rather than claiming an "
            "unverified surface",
            field=attr.lower(), intended=value, actual=actual, surface=surface,
        )


def _read_standard_scalar(row, attr, surface):
    """Read ``row.<attr>`` (Radius/Conic) -> float, THROW-guarded -> surface_grin."""
    try:
        return float(getattr(row, attr))
    except Exception as exc:  # noqa: BLE001 — a property read THROW -> surface_grin
        raise GrinWriteError(
            f"could not read {attr} of GRIN surface {surface} ({exc!r}); refusing rather "
            "than guessing the value",
            field=attr.lower(), intended=None, actual=None, surface=surface,
        ) from exc


# =========================================================================== #
# §1.4 — the Standard-revert arm (reuse the shared _revert_to_standard_proven).
# =========================================================================== #
def _revert_grin(session, params, state):
    """``set_grin(surface_type="Standard")`` arm: revert a surface to Standard (§1.4).

    REFUSES ``n0``/``coefficients``/``radius``/``conic`` LOUD (``grin_param``, ZERO mutation
    — a Standard revert is granular, not a re-author). Captures the original Type token FIRST
    (the step-3a discipline applies to the revert path too), then REUSES the shared
    ``asphere_surface._revert_to_standard_proven`` with the backward-compatible ``attempt``
    out-param. On any raise it maps ``partial_state`` from ``changetype_invoked`` +
    the exception's ``actual`` vs the stored original token; the failure family is
    ``surface_write`` (the BASE ``SurfaceWriteError`` the shared primitive raises — the
    quartet's revert arm). NO-OP ONLY when the stored original type is EXACTLY
    "Standard" (was_grin:false). Any NON-Standard type genuinely reverts to Standard via the
    shared proven primitive — a GRIN family member (was_grin:true) AND any other special type
    (an EvenAspheric, a loaded non-authorable GRIN member; was_grin:false but still retyped),
    NOT falsely reported as Standard while the row keeps its prior type (the fix — the
    pre-fix ``if not was_grin`` skipped ChangeType for every non-Gradient2 original yet claimed
    Standard).

    Returns ``{ok, surface, type:"Standard", was_grin, grin_wavelength_blind}``.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    _require_interior_index(surface, n)  # interior firewall (GRIN-worded)

    # Refuse re-author params LOUD, ZERO mutation (a Standard revert takes no
    # n0/coefficients/radius/conic — author geometry via set_surface / a fresh set_grin).
    for refused in ("n0", "coefficients", "radius", "conic"):
        if refused in params and params.get(refused) is not None:
            raise ToolParamError(
                f"set_grin(surface_type='Standard') reverts a surface to Standard; it does "
                f"NOT take {refused!r} (it is a granular revert, not a re-author). Author "
                "geometry via set_surface / a fresh set_grin after the revert."
            )

    # Discipline: capture the original Type FIRST (drives was_grin + the no-op map).
    row = lde.GetSurfaceAt(surface)
    try:
        original_type = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — unreadable pre-state -> refuse PRE-mutation
        raise GrinWriteError(
            f"could not read surface {surface} Type before the Standard revert ({exc!r}); "
            "the revert is unverifiable — refusing rather than proceeding",
            field="surface_type", intended="Standard", actual=None, surface=surface,
        ) from exc
    was_grin = _grin.grin_type_of_name(original_type) is not None
    # No-op ONLY when the STORED original token is EXACTLY "Standard". A surface that
    # is NOT an authorable GRIN (an EvenAspheric, a loaded non-authorable GRIN family member,
    # any other special type) must GENUINELY revert to Standard via the shared proven
    # primitive — NOT falsely report type:"Standard" while the row stays its prior type (the
    # silent-wrong the pre-fix ``if not was_grin`` shipped: it skipped ChangeType for every
    # non-Gradient2 original yet claimed Standard). ``was_grin`` still reflects whether the
    # ORIGINAL was an authorable GRIN (False for an EvenAspheric revert).
    if original_type == "Standard":
        return {"ok": True, "surface": surface, "type": "Standard",
                "was_grin": False, "grin_wavelength_blind": True}

    attempt = {"changetype_invoked": False}
    from . import asphere_surface as _asph
    try:
        _asph._revert_to_standard_proven(system, surface, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 — map partial_state for EVERY exception
        # partial_state map: the engine state is unknown/mutated iff ChangeType was
        # invoked AND the read-back is NOT a positively-proven no-op (``actual`` != the stored
        # original token). This covers a ``SurfaceWriteError`` (settings/changetype/read-back)
        # AND a RAW throw — notably a post-ChangeType row RE-FETCH throw inside the shared
        # primitive, which is OUTSIDE that primitive's own ``SurfaceWriteError`` wrapper and
        # carries no ``actual`` (-> None != original -> partial when invoked). A settings throw
        # (never invoked) OR a proven no-op (``actual`` == original) -> clean refusal, no
        # partial. Re-raised so the outer handler resolves the family (``surface_write`` for a
        # SurfaceWriteError; ``grin_write`` for a raw throw).
        if attempt.get("changetype_invoked") and getattr(exc, "actual", None) != original_type:
            state["mutated"] = True
        raise

    return {"ok": True, "surface": surface, "type": "Standard",
            "was_grin": was_grin, "grin_wavelength_blind": True}


# =========================================================================== #
# §2. set_grin_variable — make a GRIN coefficient cell an optimizer DOF.
# =========================================================================== #
def set_grin_variable(session, params):
    """Make a GRIN coefficient cell an optimizer Variable, opt.Variables-proven (§2).

    Params: ``surface`` (int, REQUIRED, interior 1..N-2), ``coefficient`` (string, REQUIRED —
    the exact Header token — per type: n0/Nr2/Nr4/Nr6/Nr8/Nr10/Nr12 on a Gradient2,
    n0/Nr2/Nr4/Nr6/Nz1/Nz2/Nz3 on a Gradient3).

    REFUSES a non-GRIN surface (``surface_grin`` — author with ``set_grin`` first), the
    ``Delta T`` trace-step cell (``grin_param``, naming it the numerical trace step),
    and any unknown/Integer/String-drift cell. Mechanism: resolve the token -> its Par cell ->
    ``_expect_grin_layout`` (Header+Double; a drift REFUSES, never "not found") -> the
    optimizer-backed idempotent / fresh-set branch. NEVER raises past the boundary.

    Fresh-set success: ``{ok, surface, coefficient, par, term, is_variable:true,
    dof_proven:true, proof_basis:"count_increment", was_variable:false, variables_before,
    variables_after, grin_wavelength_blind}``. No success carries ``dof_proven:false``
    (authority unreadable = refusal — honored on BOTH branches).
    """
    params = _require_dict(params)
    # ``mutated`` = the ``attempted`` flag: flipped True IMMEDIATELY before
    # MakeSolveVariable; a failure at-or-after MakeSolveVariable stamps ``partial_state``.
    state = {"mutated": False}
    try:
        # The prior-solve DISCLOSE stamp, at the PUBLIC entry, so no
        # success return inside the impl can be added later and quietly miss it.
        # Function-local import, the house style already used in this module for
        # ``_optimize_common`` (it imports these tool modules back).
        from . import _optimize_common as _oc_stamp
        return _oc_stamp._stamp_prior_solve_unchecked(
            _set_grin_variable_impl(session, params, state), "GRIN coefficient (Par)")
    except ToolParamError as exc:
        # Backstop: honor ``state["mutated"]`` (a post-MakeSolveVariable ToolParamError
        # must never read as a clean, non-partial refusal). All param validation here is
        # pre-mutation, so ``state["mutated"]`` is False on every current path.
        return _grin_error("set_grin_variable", _GRIN_PARAM, str(exc),
                           surface=_surface_hint(params), partial_state=state["mutated"])
    except SurfaceWriteError as exc:
        return _grin_error(
            "set_grin_variable", getattr(exc, "error_family", "surface_write"), str(exc),
            surface=_exc_surface(exc, params), field=getattr(exc, "field", None),
            partial_state=state["mutated"],
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> grin_write
        return _grin_error(
            "set_grin_variable", _GRIN_WRITE,
            f"unexpected engine fault setting the GRIN variable ({exc!r}); refusing rather "
            "than shipping an unverified DOF",
            surface=_surface_hint(params), partial_state=state["mutated"],
        )


def _require_coefficient_format(value):
    """Type-INDEPENDENT format check on ``coefficient`` (§2.1 step 1).

    Checks ONLY the every-type invariants — the string-ness and the ``"Delta T"`` refusal
    (Delta T is Par1 in BOTH tables, a numerical-accuracy knob never a DOF). NO
    membership check here — per-type eligibility runs AFTER the surface type is resolved (so
    ``"Nz1"`` on a Gradient3 is NOT false-refused, and ``"Nz1"`` on a Gradient2 gets a clean
    ``grin_param`` naming Gradient2's set). Returns the token.
    """
    if not isinstance(value, str):
        raise ToolParamError(
            "coefficient must be a string (the exact Header token, e.g. n0/Nr2/…/Nz1), got "
            f"{type(value).__name__} {value!r}"
        )
    if value == "Delta T":
        raise ToolParamError(
            "coefficient 'Delta T' is the numerical trace step-size (a converged accuracy "
            "knob), NOT a design DOF; it cannot be made an optimizer variable"
        )
    return value


def _set_grin_variable_impl(session, params, state):
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    surface = _lc._require_int_index(params, "surface")
    _require_interior_index(surface, n)

    # 1. Type-INDEPENDENT format check (string + Delta T refusal); NO membership yet.
    coefficient = _require_coefficient_format(params.get("coefficient"))

    # 2. Resolve the LIVE surface type BEFORE the eligibility check.
    row = lde.GetSurfaceAt(surface)
    grin_key = _grin.grin_type_of(row)
    if grin_key is None:
        # §2.1 loaded-Gradium sidecar: distinguish a genuinely non-GRIN surface from
        # a RECOGNIZED-but-unauthorable GRIN family member (a loaded Gradium/GridGradient/…)
        # so the refusal names it + its deferral ticket (the refuse-tier's sibling on the
        # variable path). A Type read here is best-effort (grin_type_of already proved the
        # Type is readable — it returned None, not a throw — but re-guard anyway).
        try:
            fam = _grin.grin_family_type_of_name(str(row.Type))
        except Exception:  # noqa: BLE001 — a Type read hiccup -> fall back to the plain message
            fam = None
        if fam is not None:
            reason = _GRIN_DEFERRAL_REASON.get(fam, _GRIN_TYPE_EXPANSION_REASON)
            raise GrinWriteError(
                f"surface {surface} is a {fam!r} surface — a recognized GRIN family member "
                f"that is NOT authorable here, so its coefficient cells cannot be made "
                f"variables (this primitive authors 'Gradient2'/'Gradient3' only): "
                f"{reason}.",
                field="surface_type", intended=fam, actual=None, surface=surface,
            )
        raise GrinWriteError(
            f"surface {surface} is not a GRIN surface; cannot set a GRIN coefficient "
            "variable on it (author it with set_grin first)",
            field="surface_type", intended="grin", actual=None, surface=surface,
        )
    info = _grin.GRIN_TYPE_INFO[grin_key]

    # 3. Per-type eligibility — the message lists THIS type's tokens (fixing the hardcoded
    # Gradient2 list; ``"Nz1"`` on a Gradient2 -> a clean grin_param, never a KeyError escape).
    if not info.is_variable_eligible(coefficient):
        raise ToolParamError(
            f"coefficient {coefficient!r} is not a variable-eligible {grin_key} coefficient; "
            f"valid (case-EXACT): {list(info.coeff_tokens())}"
        )

    # 4. Resolve the cell + its axis-derived term label.
    token_row = info.token_row(coefficient)  # (token, par, header, kind, role, power)
    par = token_row[1]
    term = _grin._term_label(coefficient, token_row[5])

    # 5. Layout-verify (Header + Double) so a drift REFUSES (never make the WRONG cell a var).
    cell = _grin._grin_cell(system, row, par)
    _grin._expect_grin_layout(cell, coefficient, info)

    variable_member = _variable_member(system)

    # --- The already-Variable (idempotent) branch — OPTIMIZER-BACKED. ---
    if _solve_type_name(cell) == str(variable_member):
        now = _open_count_close_variables(system)
        if not isinstance(now, int):
            raise GrinWriteError(
                f"surface {surface} coefficient {coefficient!r} already reads back Variable "
                "but the pre-existing DOF cannot be certified — the optimizer authority "
                "(opt.Variables) is unreadable; refusing rather than fabricating dof_proven",
                field="grin_variable", intended="Variable", actual=None, surface=surface,
            )
        if now < 1:
            raise GrinWriteError(
                f"surface {surface} coefficient {coefficient!r} reads back Variable but the "
                f"optimizer counts {now} DOFs — a flagged-but-uncounted phantom; refusing "
                "rather than claiming a DOF the optimizer does not see",
                field="grin_variable", intended="Variable", actual=now, surface=surface,
            )
        return {
            "ok": True, "surface": surface, "coefficient": coefficient, "par": par,
            "term": term, "is_variable": True, "dof_proven": True,
            "proof_basis": "count_consistent", "was_variable": True,
            "variables_now": now, "variables_before": None, "variables_after": None,
            "grin_wavelength_blind": True,
        }

    # --- The fresh-set branch: MakeSolveVariable proven by the opt.Variables +1 increment. ---
    before = _open_count_close_variables(system)
    # Mark ``attempted`` IMMEDIATELY before MakeSolveVariable (a mutates-then-throws
    # engine then stamps partial_state; a mark-after-return impl lies "clean refusal").
    state["mutated"] = True
    try:
        cell.MakeSolveVariable()
    except Exception as exc:  # noqa: BLE001 — a solve THROW -> surface_grin (partial_state)
        raise GrinWriteError(
            f"could not make GRIN coefficient {coefficient!r} (the {term} term) a variable "
            f"on surface {surface} ({exc!r}); the engine rejected the solve",
            field="grin_variable", intended="Variable", actual=None, surface=surface,
        ) from exc

    # Re-fetch (§2) + prove the solve read-back == Variable AND opt.Variables == before+1.
    row = lde.GetSurfaceAt(surface)
    cell = _grin._grin_cell(system, row, par)
    _grin._expect_grin_layout(cell, coefficient, info)
    solve_name = _solve_type_name(cell)
    if solve_name != str(variable_member):
        raise GrinWriteError(
            f"GRIN variable solve on {coefficient!r} (surface {surface}) did not take "
            f"effect: solve Type reads {solve_name!r} (silent no-op); refusing rather than "
            "claiming a DOF that does not exist",
            field="grin_variable", intended="Variable", actual=solve_name, surface=surface,
        )
    after = _open_count_close_variables(system)
    if not (isinstance(before, int) and isinstance(after, int)):
        # Authority unreadable = refusal (C's rule — no fabricated dof_proven:false success;
        # the asphere warn-and-succeed arm is NOT cloned, §2).
        raise GrinWriteError(
            f"the optimizer DOF count could not be confirmed to have incremented for "
            f"{coefficient!r} (surface {surface}): before={before!r}, after={after!r}; the "
            "cell reads back Variable but the opt.Variables authority is unreadable — "
            "refusing rather than claiming an unconfirmed DOF",
            field="grin_variable_dof", intended="Variable", actual=None, surface=surface,
        )
    if after != before + 1:
        # The cell reads Variable but the optimizer count did NOT increment by one -> phantom.
        raise GrinWriteError(
            f"GRIN variable on {coefficient!r} (surface {surface}) reads back Variable but "
            f"the optimizer DOF count did not increment ({before} -> {after}); the solve is "
            "not a real optimizer variable — refusing rather than claiming a phantom DOF",
            field="grin_variable_dof", intended=before + 1, actual=after, surface=surface,
        )

    return {
        "ok": True, "surface": surface, "coefficient": coefficient, "par": par,
        "term": term, "is_variable": True, "dof_proven": True,
        "proof_basis": "count_increment", "was_variable": False,
        "variables_before": before, "variables_after": after,
        "grin_wavelength_blind": True,
    }


# --------------------------------------------------------------------------- #
# Optimizer-proof helpers (cloned from asphere_surface / cb_surface).
# --------------------------------------------------------------------------- #
def _variable_member(system):
    """The live ``SolveType.Variable`` member (reused from the optimize tier)."""
    from . import _optimize_common as _oc
    return _oc._solve_type_variable_enum(system)


def _solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the read-back truth source).

    THROW-guarded -> ``GrinWriteError`` (a read THROW on the proof leaves the DOF
    unverifiable — refuse rather than guess it took).
    """
    try:
        return str(cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> surface_grin
        raise GrinWriteError(
            f"could not read back the solve type of a GRIN cell ({exc!r}); the variable is "
            "unverifiable — refusing rather than guessing it took",
            field="grin_variable", intended="Variable", actual=None, surface=None,
        ) from exc


def _open_count_close_variables(system):
    """Open the optimizer, read ``opt.Variables`` (the DOF count), close it.

    Cloned verbatim from ``asphere_surface._open_count_close_variables``: a cell that merely
    reads back Variable is NOT proof it is a real optimizer DOF — ``ILocalOptimization.Variables``
    is the authority. Opens ONCE, reads, ``Close()`` in finally (the single-seat reap).
    Returns the int count, or ``None`` if the optimizer is unavailable / the count is
    unreadable (a non-fatal degradation the caller treats as "could not confirm", never a crash).
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
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass


# =========================================================================== #
# ToolSpec registration (GRIN §1/§2).
# =========================================================================== #
SET_GRIN_SPEC = ToolSpec(
    name="set_grin",
    handler=set_grin,
    required_params=("surface",),
    param_types={
        "surface": "number",
        "surface_type": "string",          # {"Gradient2"[default], "Gradient3", "Standard"}
        "n0": "number",                    # REQUIRED by the handler when authoring
        "coefficients": "object",          # MAP keyed by exact Header token (per type)
        "radius": "number",
        "conic": "number",
    },
    description=(
        "Make a surface a GRIN medium. surface_type='Gradient2' (default) authors the "
        "radial profile n(r)^2 = n0+Nr2*r^2+...+Nr12*r^12 — the Gradient2 polynomial is the "
        "index SQUARED (Zemax's own Gradient 2 definition), so n0 is the base index SQUARED: "
        "pass n0=2.25 to build a medium of physical index 1.5. surface_type='Gradient3' "
        "authors radial+axial n(r,z)=n0+Nr2*r^2+Nr4*r^4+Nr6*r^6+Nz1*z+Nz2*z^2+Nz3*z^3, where "
        "n0 IS the index (not squared). r and z in mm, absolute coefficients; z measured from "
        "the surface's FRONT face. Values are taken as the engine's CELL values and are NOT "
        "converted — read the resulting PHYSICAL index with analyze_grin_profile. n0 required; "
        "coefficients is a map keyed by the exact tokens (Nr2..Nr12 for Gradient2; "
        "Nr2/Nr4/Nr6/Nz1/Nz2/Nz3 for Gradient3) — OMITTED terms are authored to 0.0 (the "
        "call states the COMPLETE profile; a re-author never inherits a stale term). "
        "radius/conic optionally set the base surface shape (omitted = untouched). Proves "
        "by reading back the type and every cell. surface_type='Standard' reverts "
        "(coefficients cleared by the engine). Gotcha: the coefficients are "
        "WAVELENGTH-BLIND (same index at every wave — grin_wavelength_blind disclosed; "
        "chromatic grades are not modelled). NOTE: a FLAT axial (Gradient3) window has no "
        "paraxial power — verify it via absolute OPL, or via EFFL/RWCE on a CURVED element; "
        "do NOT expect a flat window to refocus. The Delta-T trace step is internal "
        "(disclosed as grin_step_size). check_clearance / build_merit now audit an "
        "AUTHORED GRIN element's edge/center at the glass floors (solid-medium policy); a "
        "loaded non-authorable GRIN family member is disclosed not-audited. See "
        "set_grin_variable, describe_surfaces."
    ),
)

SET_GRIN_VARIABLE_SPEC = ToolSpec(
    name="set_grin_variable",
    handler=set_grin_variable,
    required_params=("surface", "coefficient"),
    param_types={"surface": "number", "coefficient": "string"},
    description=(
        "Make a GRIN coefficient an optimizer Variable (coefficient is the exact Header "
        "token; per-type: n0/Nr2/Nr4/Nr6/Nr8/Nr10/Nr12 on a Gradient2 surface, or "
        "n0/Nr2/Nr4/Nr6/Nz1/Nz2/Nz3 on a Gradient3 surface), proved by the optimizer's own "
        "DOF count incrementing. Refuses a non-GRIN surface (author with set_grin "
        "first), the Delta-T trace-step cell (a numerical-accuracy knob, not a design "
        "DOF), and any unknown/wrong-family/Integer cell. See set_grin, set_variable."
    ),
)

TOOL_SPECS = (SET_GRIN_SPEC, SET_GRIN_VARIABLE_SPEC)
