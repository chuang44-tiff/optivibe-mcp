"""enums.py — live ZOS-API enum resolution.

The LIVE .NET enum is the runtime source of truth. ``_resolve_enum`` does a
guarded ``getattr`` against the enum type the caller passes in (the real
``ZOSAPI.SystemData.FieldType`` / ``WavelengthPreset`` / ``ZemaxApertureType``
at runtime), so a member name that does not exist on the live enum raises
``ToolParamError`` — it can NEVER silently resolve to a stale/wrong member.

There is intentionally NO frozen runtime member set: a hardcoded set the resolver
checked against would be a second source of truth that drifts on an OpticStudio
version bump. Instead the curated member lists below are
TEST-ONLY drift insurance — the ``test_enum_no_drift`` integration test asserts
``set(expected) <= set(Enum.GetNames(enum))`` against the LIVE enum.

Live ZOS-API integration: ``_resolve_enum`` resolves against the live .NET enum;
the drift lists are asserted ⊆ the live ``Enum.GetNames`` by the live test.
"""
from .errors import ToolParamError

# --------------------------------------------------------------------------- #
# Curated documentation member lists — TEST-ONLY drift insurance.
# The runtime resolver does NOT check against these; the live enum is the truth.
# --------------------------------------------------------------------------- #
FIELD_TYPE_MEMBERS = (
    "Angle",
    "ObjectHeight",
    "ParaxialImageHeight",
    "RealImageHeight",
    "TheodoliteAngle",
)

# 28 members live; these are the minimum the drift test asserts ⊆ live GetNames
# (the full set is discovered live, not pinned here).
WAVELENGTH_PRESET_MEMBERS = (
    "FdC_Visible",
    "d_0p587",
    "F_0p486",
    "C_0p656",
    "e_0p54607",
)

# NOTE: the aperture enum is ``ZemaxApertureType`` (NOT "ApertureType").
ZEMAX_APERTURE_TYPE_MEMBERS = (
    "EntrancePupilDiameter",
    "ImageSpaceFNum",
    "ObjectSpaceNA",
    "FloatByStopSize",
    "ParaxialWorkingFNum",
    "ObjectConeAngle",
)

# A small subset of the live ``MeritOperandType`` enum (438 members live).
# TEST-ONLY drift insurance — the runtime ``get_operand`` tool resolves a
# member name against the LIVE enum via ``_resolve_enum`` (the live enum is the
# source of truth); the live drift test asserts this subset ⊆ live GetNames.
MERIT_OPERAND_TYPE_MEMBERS = (
    "EFFL",
    "REAX",
    "REAY",
    "DIST",
    "RSCE",
    "RSCH",
    "TRAR",
    "AMAG",
    "WFNO",
)


def _enum_names(enum_type):
    """Best-effort list of an enum's member names for an error message.

    Tries the live .NET reflection path ``System.Enum.GetNames(enum_type)`` first
    (the authoritative discovery surface), then falls back to ``dir()`` filtered
    to public names. NEVER raises — a discovery failure must not mask the
    underlying ``ToolParamError``.
    """
    # Live .NET path: System.Enum.GetNames(enum_type).
    try:  # pragma: no cover - exercised only against the live backend
        import System  # type: ignore

        names = list(System.Enum.GetNames(enum_type))
        if names:
            return names
    except Exception:  # noqa: BLE001 — fall back to dir() introspection
        pass
    try:
        return [n for n in dir(enum_type) if not n.startswith("_")]
    except Exception:  # noqa: BLE001 — last resort: nothing to list
        return []


def _resolve_enum(enum_type, name):
    """Resolve member ``name`` on the LIVE ``enum_type`` via guarded ``getattr``.

    The live enum is the runtime source of truth. ``getattr`` against
    the real .NET enum returns the typed member directly; an unknown member name
    raises ``ToolParamError`` listing the valid members (discovered live), so a
    typo/unknown member is rejected up front — never passed through.

    ``name`` MUST be a non-empty string; anything else is a client param bug.
    """
    if not isinstance(name, str) or name == "":
        raise ToolParamError(
            f"enum member name must be a non-empty string, got {name!r}"
        )
    try:
        return getattr(enum_type, name)
    except AttributeError:
        valid = _enum_names(enum_type)
        raise ToolParamError(
            f"unknown {getattr(enum_type, '__name__', enum_type)!r} member "
            f"{name!r}; valid: {valid}"
        )
