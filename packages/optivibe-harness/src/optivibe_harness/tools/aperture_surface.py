"""tools/aperture_surface.py — the per-surface aperture/obstruction authoring tool.

ONE dispatchable tool ``set_surface_aperture`` over the ``_aperture_cells`` substrate
(the surface-aperture spec). The 11-type discriminator
(None/CircularAperture/CircularObscuration/Spider/Rectangular*/Elliptical*/User*/
FloatingAperture) authors a per-surface aperture through the typed ``_S_<TypeName>``
view (NEVER the bare settings object, the §1a silent-no-op trap) and PROVES the change
by reading the live ``CurrentType`` + every field back.

The structure:

1. validate-then-open: the COMPLETE per-type client-side firewall (the engine
   validates NONE of these — probe §2b) raises ``ToolParamError`` PRE-mutation, zero
   engine touch on a bad value. Universal gates (surface range, type-on-the-live-enum,
   no-silent-drop param-name gate, required-field gate) + the per-type field rules incl.
   the LOAD-BEARING cross-field ``max_radius > min_radius`` (a per-field loop misses it;
   the probe proved ``Min=8 > Max=2`` commits silently as an inverted/empty annulus).
2. single-commit atomic author: all field writes (type fields + decenter + uda_scale)
   go onto the ONE in-hand settings object via its ``_S_`` view BEFORE the single
   ``ChangeApertureTypeSettings`` commit (no checkpoint — if ChangeApertureTypeSettings
   throws the prior aperture is unchanged).
3. read-back-as-proof: type proof (``CurrentType == requested`` — a ChangeType no-op
   leaves the prior type) + every ``proof=="equal"`` field reads back == intended +
   User* ``ApertureFile != "None"``. A mismatch -> ``ApertureWriteError`` (the DISTINCT
   ``surface_aperture`` family). ``verified:true`` is set ONLY after every proof passes.

The handler NEVER raises past its boundary for expected failures (the L26 firewall):
a bad param -> ``tool_param`` envelope; a read-back/engine-throw failure ->
``surface_aperture`` envelope; mirrors ``cb_surface`` exactly.

Live ZOS-API integration: exercised by a live annular falsification test (a center ray
flips pass->BLOCKED after a CircularObscuration); unit-tested against fixture-seeded fake
settings/ApertureData doubles.
"""
import math

from ..errors import ApertureWriteError, SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _aperture_cells as _ap
from . import _lens_common as _lc
from ._analysis_common import error_envelope


_AP_FAMILY = "surface_aperture"   # the read-back / engine-throw refusal family
_AP_PARAM = "tool_param"          # a bad param value family (ToolParamError)


# --------------------------------------------------------------------------- #
# Shared validation helpers.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _finite(value, label, *, allow_zero=False, allow_negative=False):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float.

    ``allow_zero``/``allow_negative`` tune the sign gate AFTER the finite check.
    Default: strictly positive (``> 0``) — every aperture dimension but ``min_radius``
    (which is ``>= 0``) and the signed decenters (which allow both).
    """
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
    if not allow_negative:
        if allow_zero:
            if coerced < 0.0:
                raise ToolParamError(f"{label} must be >= 0, got {coerced}")
        elif coerced <= 0.0:
            raise ToolParamError(f"{label} must be > 0, got {coerced}")
    return coerced


def _coerce_arms(value):
    """Coerce ``num_arms`` -> an int: accept an integral float, reject the rest.

    reject ``bool`` FIRST (int-subclass miswrite); accept ``int``; accept an integral
    ``float`` (``3.0`` -> 3 — the MCP/JSON round-trip delivers a count as a float);
    reject a non-integral float (``2.5``), a non-number, a non-finite. The ``>= 1``
    floor is enforced by the caller's per-type gate.
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"num_arms must be an integer count, not a bool ({value!r}); a bool is an "
            "int subclass — a client miswrite"
        )
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"num_arms must be an integer count, got non-integral float {value!r}"
        )
    raise ToolParamError(
        f"num_arms must be an integer count, got {type(value).__name__} {value!r}"
    )


def _require_str(value, label):
    """Require a non-empty (non-whitespace) string -> the stripped-checked str."""
    if not isinstance(value, str) or value.strip() == "":
        raise ToolParamError(
            f"{label} must be a non-empty string, got {type(value).__name__} {value!r}"
        )
    return value


# --------------------------------------------------------------------------- #
# The validation firewall (§3): the COMPLETE per-type rule table,
# client-side, PRE-mutation, zero engine touch until every check passes.
#
# Returns the validated ``{friendly: value}`` dict (only the fields actually supplied/
# defaulted) to author. RAISES ``ToolParamError`` on ANY violation.
# --------------------------------------------------------------------------- #

# The per-type ALLOWED param set is computed from the substrate table (single source of
# truth): {surface, aperture_type} + the type's field-friendly-names + decenters.
def _allowed_params(token):
    allowed = {"surface", "aperture_type"}
    for friendly, _dotnet, _kind, _proof in _ap.aperture_fields(token):
        allowed.add(friendly)
    return allowed


def _validate(token, params):
    """Run the COMPLETE per-type firewall; return the ``{friendly: value}`` to author.

    The order: (a) no-silent-drop param-name gate (every supplied param is in
    the resolved type's allowed set); (b) required-field gate; (c) per-type numeric/
    cross-field rules. ALL pre-mutation, zero engine touch.
    """
    # (a) No-silent-drop param-name gate: every supplied aperture param ∈ allowed set.
    allowed = _allowed_params(token)
    # The full universe of friendly param names this tool knows (so an unknown key like
    # ``foo`` is also rejected, not just a wrong-type key like ``num_arms`` on Circular).
    known = {
        "surface", "aperture_type", "min_radius", "max_radius", "num_arms", "arm_width",
        "x_half_width", "y_half_width", "aperture_file", "uda_scale",
        "x_decenter", "y_decenter",
    }
    for key in params:
        if key not in known:
            # An unknown key is a client bug — refuse rather than silently ignore.
            raise ToolParamError(
                f"unknown parameter {key!r} for set_surface_aperture; valid params for "
                f"aperture_type={token!r}: {sorted(allowed)}"
            )
        if key in ("surface", "aperture_type"):
            continue
        if key not in allowed:
            raise ToolParamError(
                f"parameter {key!r} is not valid for aperture_type={token!r} "
                f"(allowed: {sorted(allowed - {'surface', 'aperture_type'})}); refusing "
                "rather than silently dropping it"
            )

    values = {}

    # (b)+(c) per-type required-field gate + numeric/cross-field rules.
    if token in ("CircularAperture", "CircularObscuration"):
        if "max_radius" not in params:
            raise ToolParamError(
                f"aperture_type={token!r} requires max_radius (the outer edge)"
            )
        min_r = _finite(params.get("min_radius", 0.0), "min_radius", allow_zero=True)
        max_r = _finite(params["max_radius"], "max_radius")  # > 0
        if max_r <= min_r:
            raise ToolParamError(
                f"max_radius ({max_r}) must be strictly greater than min_radius "
                f"({min_r}); a min >= max is an inverted/zero-width annulus (the engine "
                "accepts it silently — this is the load-bearing cross-field gate)"
            )
        values["min_radius"] = min_r
        values["max_radius"] = max_r

    elif token == "Spider":
        if "num_arms" not in params:
            raise ToolParamError("aperture_type='Spider' requires num_arms")
        if "arm_width" not in params:
            raise ToolParamError("aperture_type='Spider' requires arm_width")
        arms = _coerce_arms(params["num_arms"])
        if arms < 1:
            raise ToolParamError(f"num_arms must be >= 1, got {arms}")
        values["num_arms"] = arms
        values["arm_width"] = _finite(params["arm_width"], "arm_width")  # > 0

    elif token in (
        "RectangularAperture", "RectangularObscuration",
        "EllipticalAperture", "EllipticalObscuration",
    ):
        if "x_half_width" not in params:
            raise ToolParamError(f"aperture_type={token!r} requires x_half_width")
        if "y_half_width" not in params:
            raise ToolParamError(f"aperture_type={token!r} requires y_half_width")
        values["x_half_width"] = _finite(params["x_half_width"], "x_half_width")  # > 0
        values["y_half_width"] = _finite(params["y_half_width"], "y_half_width")  # > 0

    elif token in ("UserAperture", "UserObscuration"):
        if "aperture_file" not in params:
            raise ToolParamError(f"aperture_type={token!r} requires aperture_file")
        values["aperture_file"] = _require_str(params["aperture_file"], "aperture_file")
        values["uda_scale"] = _finite(params.get("uda_scale", 1.0), "uda_scale")  # > 0

    elif token in ("None", "FloatingAperture"):
        # No type-specific fields. The param-name gate (a) already refused any stray
        # field (incl. x_decenter on None/Floating — neither has_decenter, so the
        # decenter params are NOT in the allowed set).
        pass

    # decenter (every type with has_decenter — finite signed, default 0.0). The
    # param-name gate has already refused x_decenter/y_decenter on None/Floating, so
    # these branches only run for decenter-bearing types.
    if _ap.has_decenter(token):
        if "x_decenter" in params:
            values["x_decenter"] = _finite(
                params["x_decenter"], "x_decenter", allow_zero=True, allow_negative=True
            )
        if "y_decenter" in params:
            values["y_decenter"] = _finite(
                params["y_decenter"], "y_decenter", allow_zero=True, allow_negative=True
            )

    return values


# =========================================================================== #
# set_surface_aperture
# =========================================================================== #
def set_surface_aperture(session, params):
    """Set (or clear, via aperture_type='None') a per-surface aperture/obstruction.

    Params: ``surface`` (int, REQUIRED), ``aperture_type`` (str, REQUIRED — one of the
    11 SurfaceApertureTypes). Per-type optional params per the table
    (min_radius/max_radius; num_arms/arm_width; x_half_width/y_half_width;
    aperture_file/uda_scale; x_decenter/y_decenter).

    Validates the COMPLETE per-type firewall PRE-mutation (zero engine touch on a bad
    value), authors through the typed ``_S_<TypeName>`` view in a single atomic
    ``ChangeApertureTypeSettings`` commit, and PROVES the change by reading the live
    ``CurrentType`` + every field back. NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_surface_aperture", _AP_PARAM, str(exc))
    except SurfaceWriteError as exc:  # incl. ApertureWriteError -> "surface_aperture"
        return error_envelope(
            "set_surface_aperture", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> surface_aperture (L26)
        return error_envelope(
            "set_surface_aperture", _AP_FAMILY,
            f"unexpected engine fault setting the surface aperture ({exc!r}); refusing "
            "rather than shipping an unverified aperture",
        )


def _impl(session, params):
    system = session.system
    lde = system.LDE

    # Universal gate 1: surface index range (1..N-1; OBJECT 0 refused, IMAGE allowed).
    # Client-side, BEFORE any engine touch.
    surface = _lc._require_int_index(params, "surface")
    n = int(lde.NumberOfSurfaces)
    _lc._require_geometry_index(surface, n)

    # Universal gate 2: aperture_type resolves on the live enum (unknown -> loud). The
    # member is resolved here (engine-crash firewall: validate before any mutation) but
    # the friendly TOKEN drives the substrate table.
    token = params.get("aperture_type")
    if not isinstance(token, str) or token == "":
        raise ToolParamError(
            "aperture_type is required and must be a non-empty string (one of "
            f"{list(_ap.APERTURE_TYPE_NAMES)})"
        )
    member = _ap._aperture_member(system, token)  # ToolParamError on unknown
    # Belt-and-suspenders: the resolved token must be in our catalog (the live enum and
    # our table agree on the 11 members; a live enum carrying an extra member we do not
    # model is refused rather than authored half-known).
    if token not in _ap.APERTURE_TYPES:
        raise ToolParamError(
            f"aperture_type={token!r} resolved on the live enum but is not in the "
            f"OptiVibe aperture catalog ({list(_ap.APERTURE_TYPE_NAMES)}); refusing "
            "rather than authoring an unmodelled type"
        )

    # Universal gates 3+4 + per-type rules: the COMPLETE firewall (zero engine touch on
    # a bad value). Returns the validated {friendly: value} to author.
    values = _validate(token, params)

    # --- single-commit atomic author (no checkpoint) --------------------------- #
    row = lde.GetSurfaceAt(surface)
    try:
        ad = row.ApertureData
    except Exception as exc:  # noqa: BLE001 — an ApertureData read THROW -> surface_aperture
        raise ApertureWriteError(
            f"could not access ApertureData on surface {surface} ({exc!r}); refusing "
            "rather than guessing the aperture",
            field="aperture_data", intended=token, actual=None, surface=surface,
        ) from exc

    try:
        settings = ad.CreateApertureTypeSettings(member)
    except Exception as exc:  # noqa: BLE001 — a settings-create THROW -> surface_aperture
        raise ApertureWriteError(
            f"could not create the {token!r} aperture settings on surface {surface} "
            f"({exc!r}); refusing rather than shipping an unverified aperture",
            field="aperture_settings", intended=token, actual=None, surface=surface,
        ) from exc

    # Author every field through the typed _S_<TypeName> view ONLY (fail-closed resolver;
    # NEVER the bare settings object — the §1a silent-no-op trap). None/Floating author
    # no fields (the view exists but carries nothing writable). The decenters + uda_scale
    # ride the SAME settings object (single-commit atomicity).
    view = _ap._typed_view(settings, token, surface=surface)
    _ap.write_aperture_fields(view, token, values, surface=surface)

    # The single commit. If this throws, the prior aperture is unchanged (never committed).
    try:
        ad.ChangeApertureTypeSettings(settings)
    except Exception as exc:  # noqa: BLE001 — a commit THROW -> surface_aperture
        raise ApertureWriteError(
            f"ChangeApertureTypeSettings({token!r}) threw on surface {surface} "
            f"({exc!r}); the prior aperture is unchanged — refusing rather than "
            "claiming a write that did not commit",
            field="change_aperture", intended=token, actual=None, surface=surface,
        ) from exc

    # --- read-back-as-proof (read the LIVE committed settings) ----------------- #
    # 1. Type proof (all 11): a ChangeType no-op leaves the prior type — caught here.
    current = _ap.read_current_type(ad, surface=surface)
    if current != token:
        raise ApertureWriteError(
            f"surface {surface} aperture type reads back {current!r} after the change "
            f"(requested {token!r}) — the ChangeApertureTypeSettings silently no-opped; "
            "refusing rather than shipping an unverified aperture",
            field="aperture_type", intended=token, actual=current, surface=surface,
        )

    # 2.+3.+4. Field/decenter/User-file proof off the LIVE CurrentTypeSettings._S_<token>.
    # Returns the read-back echo (what actually stuck). A mismatch raises ApertureWriteError.
    echo = _ap.read_back_aperture(ad, token, values, surface=surface)
    # The read-back-raised ApertureWriteError now carries the surface index,
    # mirroring lens_surface.py's SurfaceWriteError(surface=...) — the structured error
    # carries the locus, not just the field/family.

    written = {k: _safe(v) for k, v in echo.items()}

    return {
        "ok": True,
        "surface": surface,
        "aperture_type": token,
        "written": written,
        "current_type": current,
        "verified": True,
    }


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel via the tier-wide safe_float).

    A string field (ApertureFile) passes through unchanged.
    """
    if isinstance(value, str):
        return value
    from .._io import safe_float

    try:
        return safe_float(value)
    except Exception:  # noqa: BLE001 — a non-float echo passes through verbatim
        return value


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_SURFACE_APERTURE_SPEC = ToolSpec(
    name="set_surface_aperture",
    handler=set_surface_aperture,
    required_params=("surface", "aperture_type"),
    param_types={
        "surface": "number",
        "aperture_type": "string",
        "min_radius": "number",
        "max_radius": "number",
        "num_arms": "number",   # "number" NOT "integer" — integral-float accepted
        "arm_width": "number",
        "x_half_width": "number",
        "y_half_width": "number",
        "aperture_file": "string",
        "uda_scale": "number",
        "x_decenter": "number",
        "y_decenter": "number",
    },
    description=(
        "Set a per-surface aperture or obstruction (clip/block rays at a surface). "
        "aperture_type is one of CircularAperture/CircularObscuration/Spider/"
        "RectangularAperture/RectangularObscuration/EllipticalAperture/"
        "EllipticalObscuration/UserAperture/UserObscuration/FloatingAperture, or None to "
        "clear. Circular* take min_radius/max_radius (a central obscuration is "
        "min_radius=0, max_radius=R — the annulus a Cassegrain secondary shadow needs); "
        "Spider takes num_arms/arm_width; Rectangular*/Elliptical* take "
        "x_half_width/y_half_width; User* take an existing .uda aperture_file. Writes the "
        "typed aperture fields and PROVES the change by reading CurrentType + every field "
        "back. Gotcha: the obstruction is REAL — it is needed for correct PSF/MTF/Strehl "
        "on an obscured/reflective annular pupil. The engine accepts insane values "
        "(min>max, negative radius, 0-arm spider) silently — this tool validates them "
        "client-side first; and the bare settings setter is a silent no-op (it writes the "
        "typed view). See describe_surfaces, analyze_strehl."
    ),
)

TOOL_SPECS = (SET_SURFACE_APERTURE_SPEC,)
