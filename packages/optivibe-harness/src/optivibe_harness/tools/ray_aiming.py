"""tools/ray_aiming.py — the ray-aiming mode authoring tool (§1).

ONE dispatchable tool ``set_ray_aiming`` over ``system.SystemData.RayAiming.RayAiming``
— a ``set_aperture`` / ``set_wavelength`` sibling. The mode is a ``RayAimingMethod``
enum member (``Off`` / ``Paraxial`` / ``Real``). ``real`` ray aiming iteratively aims
EVERY ray through the TRUE aperture stop, which a system with a far/virtual entrance
pupil (a DISPLACED — internal/rear — stop, NOT merely a long focal length) needs so the
wide-field off-axis chief launches at the correct pupil coordinate instead of failing to
trace (probe Q2: errorCode 2 -> 0, the chief re-aimed onto the front element).

CRASH-SAFETY (the load-bearing gotcha, probe Q1): assigning a NON-enum value to
``.RayAiming`` HARD-CRASHES the CLR UNCATCHABLY (the Tolerancing ``Enum.Parse`` crash
class). The tool therefore:

- VALIDATES ``mode`` against the FROZEN ``{off, paraxial, real}`` set BEFORE any engine
  touch (an unknown / missing / non-string / wrong-case mode -> ``ToolParamError`` family
  ``ray_aiming`` with the engine NEVER touched);
- resolves the member ONLY from the frozen ``{off->Off, paraxial->Paraxial, real->Real}``
  map via the getattr-safe ``_resolve_enum`` seam — caller input NEVER reaches the enum.

Read-back-as-proof (D-7): after the write + ``UpdateStatus()`` the mode is re-read and
asserted equal to the requested member; a silent no-op -> ``SurfaceWriteError`` family
``ray_aiming``. There is NO ``merit_rebuild_required`` — ray aiming is a TRACE-TIME
setting (the merit re-evaluates correctly on the next CalculateMeritFunction; nothing is
baked, unlike vignetting).

Family: the STRING token ``"ray_aiming"`` via ``error_envelope`` (NO new error class —
reuse ``SurfaceWriteError`` / ``ToolParamError``, the vignetting/aperture precedent). The
handler NEVER raises past its boundary; the uncatchable CLR crash is structurally
prevented by the pre-validation (the enum is never handed a bad value).

Live ZOS-API integration: exercised by the live test (THE make-it-bite — a
displaced-stop wide-field chief flips errorCode 2 -> 0 Off -> Real); unit-tested against
the non-hollow ray-aiming fake doubles whose ``.RayAiming`` property reads
back the stored member and RAISES on a non-member (the crash boundary modeled, L35).
"""
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from ._analysis_common import error_envelope
from .lens_system import _zosapi_enum


# The single refusal family (read-back / engine-throw / bad-param all surface here).
_RAY_AIMING_FAMILY = "ray_aiming"

# The FROZEN lowercase caller token -> the live ``RayAimingMethod`` member NAME. The
# member name is taken ONLY from this map (never from raw caller input) so a non-enum
# value can never reach the ``.RayAiming`` enum property (the uncatchable-CLR-crash
# firewall, probe Q1).
_MODE_TO_MEMBER = {"off": "Off", "paraxial": "Paraxial", "real": "Real"}
_MODES = tuple(_MODE_TO_MEMBER)  # ("off", "paraxial", "real")


def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _ray_aiming_write_error(message, *, intended=None, actual=None):
    """Build a ``SurfaceWriteError`` carrying the DISTINCT ``ray_aiming`` family."""
    exc = SurfaceWriteError(message, field="ray_aiming_mode", intended=intended,
                            actual=actual, surface=None)
    exc.error_family = _RAY_AIMING_FAMILY
    return exc


def set_ray_aiming(session, params):
    """Set the system ray-aiming mode (off/paraxial/real) with read-back-as-proof.

    Params: ``mode`` (str, REQUIRED — one of off/paraxial/real, lowercase). Resolves the
    ``RayAimingMethod`` member from the frozen map (getattr-only, the crash-safety
    firewall), writes ``system.SystemData.RayAiming.RayAiming``, calls ``UpdateStatus()``,
    then re-reads the mode and proves it took. NEVER raises past the boundary; NO
    ``merit_rebuild_required`` (ray aiming is trace-time, not baked).
    """
    params = _require_dict(params)
    try:
        return _impl(session, params)
    except ToolParamError as exc:
        # The crash-safety firewall surfaces as the ray_aiming family (NOT tool_param) so
        # the agent branches on "the ray-aiming write was refused".
        return error_envelope("set_ray_aiming", _RAY_AIMING_FAMILY, str(exc))
    except SurfaceWriteError as exc:  # incl. the read-back no-op failure
        return error_envelope(
            "set_ray_aiming", getattr(exc, "error_family", _RAY_AIMING_FAMILY),
            str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> ray_aiming (L26)
        return error_envelope(
            "set_ray_aiming", _RAY_AIMING_FAMILY,
            f"unexpected engine fault setting the ray-aiming mode ({exc!r}); refusing "
            "rather than claiming an unverified mode",
        )


def _impl(session, params):
    # --- The crash-safety firewall: validate mode PRE-engine (no engine touch yet). ---
    mode = params.get("mode")
    if not isinstance(mode, str) or mode not in _MODE_TO_MEMBER:
        raise ToolParamError(
            f"mode is required and must be one of {list(_MODES)} (lowercase tokens), "
            f"got {mode!r}"
        )
    # The member NAME comes ONLY from the frozen map — never from raw caller input.
    member_name = _MODE_TO_MEMBER[mode]

    system = session.system
    ray_aiming = system.SystemData.RayAiming

    # Resolve the live RayAimingMethod enum TYPE via the SystemData enum seam, then the
    # member via the getattr-safe resolver (both getattr-only against a KNOWN name).
    enum_type = _zosapi_enum(system, "RayAimingMethod")
    member = _resolve_enum(enum_type, member_name)

    # Write the mode + freshen (UpdateStatus is the safe convention; probe Q4: a
    # subsequent trace reflects the mode even without it, but a non-trace read needs it).
    ray_aiming.RayAiming = member
    system.UpdateStatus()

    # Read-back-as-proof (D-7): re-read the mode and prove the RESULTING STATE equals the
    # request. Compare the read-back against the RESOLVED member stringified the SAME way
    # (str(member)), so a different enum-stringification format cannot false-refuse a good
    # write. This proves the mode IS the requested value — the contract that
    # matters; a no-op that left a DIFFERENT mode is caught here. (If the system was ALREADY
    # in the requested mode, the state is correct regardless — nothing harmful to catch;
    # This is honest about it: state-proof, not setter-fired-proof.)
    actual = ray_aiming.RayAiming
    actual_name = str(actual)
    expected_name = str(member)
    if actual_name != expected_name:
        raise _ray_aiming_write_error(
            f"ray-aiming mode is not the requested value: intended {expected_name!r}, read "
            f"{actual_name!r} — the engine did not reach the requested mode; refusing "
            "rather than claiming an unverified mode",
            intended=expected_name, actual=actual_name,
        )

    return {
        "ok": True,
        "mode": mode,
        "ray_aiming_method": actual_name,
        # NO merit_rebuild_required: ray aiming is a TRACE-TIME setting (the merit
        # re-evaluates on the next CalculateMeritFunction; nothing is baked).
    }


SET_RAY_AIMING_SPEC = ToolSpec(
    name="set_ray_aiming",
    handler=set_ray_aiming,
    required_params=("mode",),
    param_types={"mode": "string"},
    description=(
        "Set the system ray-aiming mode (off/paraxial/real) over SystemData.RayAiming. "
        "Real ray aiming iteratively aims EVERY ray through the TRUE aperture stop - "
        "needed for a system with a far/virtual entrance pupil, i.e. a DISPLACED "
        "(internal/rear) stop, NOT merely a long focal length. mode is required (one of "
        "off/paraxial/real, lowercase). Writes the RayAimingMethod enum member and reads "
        "the mode back to prove it took. Gotcha: the trigger is a DISPLACED stop (large "
        "|ENPP|, surfaced by get_first_order.ray_aiming_recommended) - NOT long EFL; on "
        "such a wide-field system the off-axis chief mis-launches (fails to trace) "
        "without real ray aiming. Trace-time setting (no merit rebuild). See "
        "get_first_order, set_aperture."
    ),
)

TOOL_SPECS = (SET_RAY_AIMING_SPEC,)
