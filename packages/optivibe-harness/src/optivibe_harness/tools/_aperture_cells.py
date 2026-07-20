"""tools/_aperture_cells.py — the surface-aperture typed-view substrate.

NOT dispatchable (no ``TOOL_SPECS``). The single source of truth for the
``set_surface_aperture`` tool: the ``APERTURE_TYPES`` table (type -> {typed ``_S_``
view, type-specific fields, has_decenter}) + the FAIL-CLOSED ``_typed_view``
resolver + the read-back primitives.

This MIRRORS ``_cb_cells.py`` (the typed-view authoring + read-back-as-proof) but is
a SEPARATE re-implementation: an aperture is a per-surface ``ApertureData`` typed
SETTINGS object (``CreateApertureTypeSettings`` / ``ChangeApertureTypeSettings``),
NOT an LDE Par cell, so we do NOT cross-import ``_cb_cells``.

Probe-grounded rules this module encodes (all [Discovered-live-probed]):

- THE LOAD-BEARING TRAP (§1a): ``CreateApertureTypeSettings`` returns the BASE
  ``ISurfaceApertureType`` interface whose only members are ``Type``/``IsReadOnly``.
  A bare ``settings.MaximumRadius = 6.0`` does NOT raise but does NOT take (reads back
  the ``10000.0`` default). The supported path is the typed accessor
  ``settings._S_<TypeName>`` (``_S_CircularObscuration``, ``_S_Spider``, ...). The
  tool authors ONLY through that view and reads back through the LIVE
  ``ad.CurrentTypeSettings._S_<TypeName>`` — NEVER the bare settings object.
- ``_typed_view`` FAILS CLOSED: exactly ONE place resolves the view
  (``getattr(settings, "_S_" + member)``); an absent ``_S_`` attr (future engine
  drift) RAISES ``ApertureWriteError`` rather than falling through to the bare
  settings object (the fall-through re-introduces the §1a silent-no-op trap).
- the int/double/string discriminator is DECLARED in the table (the ``kind``), NOT
  read from a live ``cell.DataType`` — these are typed-VIEW properties, not LDE cells;
  every numeric field reads back as a Double via pythonnet (even ``NumberOfArms``
  reads ``3.0``, probe §2).
- field-name casing is ``UDASCale`` (capital S — probe §2), NOT ``UDAScale``.
- ALL types carry ``ApertureXDecenter``/``ApertureYDecenter`` EXCEPT ``None`` AND
  ``FloatingAperture``: the capture's ``per_type_field_map`` shows ``FloatingAperture``
  carries ONLY ``IsReadOnly``/``Type`` (no decenter fields), so ``has_decenter=False``
  for it (fail-closed per the spec's "pin from the capture" instruction).

Live ZOS-API integration: exercised by a live integration test; unit-tested
against the fixture-seeded fake settings/ApertureData doubles whose bare-object
writes are DROPPED (the §1a no-op) and whose ``_S_`` view writes stick.
"""
import math

from ..errors import ApertureWriteError, ToolParamError

# --------------------------------------------------------------------------- #
# The type -> {view, fields, has_decenter} table (one source of truth).
#
# field tuple: (friendly, dotnet, kind, proof)
#   kind  in {"double", "int", "string"}
#   proof in {"equal", "non_none"}   (non_none ONLY for ApertureFile)
# --------------------------------------------------------------------------- #
_CIRC = (
    ("min_radius", "MinimumRadius", "double", "equal"),
    ("max_radius", "MaximumRadius", "double", "equal"),
)
_SPIDER = (
    ("num_arms", "NumberOfArms", "int", "equal"),
    ("arm_width", "WidthOfArms", "double", "equal"),
)
_XY_HALF = (
    ("x_half_width", "XHalfWidth", "double", "equal"),
    ("y_half_width", "YHalfWidth", "double", "equal"),
)
_USER = (
    ("aperture_file", "ApertureFile", "string", "non_none"),
    ("uda_scale", "UDASCale", "double", "equal"),  # UDASCale: capital S (probe §2)
)

# The two decenter fields — appended at write/read-back time from this ONE constant
# for every ``has_decenter=True`` row (NOT duplicated per table row).
_DECENTER_FIELDS = (
    ("x_decenter", "ApertureXDecenter", "double", "equal"),
    ("y_decenter", "ApertureYDecenter", "double", "equal"),
)

APERTURE_TYPES = {
    # None / FloatingAperture are degenerate EMPTY-fields rows (no special branch).
    # The capture's per_type_field_map confirms BOTH carry ONLY IsReadOnly/Type — so
    # neither has decenter fields (fail-closed: only write fields the table declares).
    "None": {"view": "_S_None", "fields": (), "decenter": False},
    "CircularAperture": {"view": "_S_CircularAperture", "fields": _CIRC, "decenter": True},
    "CircularObscuration": {"view": "_S_CircularObscuration", "fields": _CIRC, "decenter": True},
    "Spider": {"view": "_S_Spider", "fields": _SPIDER, "decenter": True},
    "RectangularAperture": {"view": "_S_RectangularAperture", "fields": _XY_HALF, "decenter": True},
    "RectangularObscuration": {"view": "_S_RectangularObscuration", "fields": _XY_HALF, "decenter": True},
    "EllipticalAperture": {"view": "_S_EllipticalAperture", "fields": _XY_HALF, "decenter": True},
    "EllipticalObscuration": {"view": "_S_EllipticalObscuration", "fields": _XY_HALF, "decenter": True},
    "UserAperture": {"view": "_S_UserAperture", "fields": _USER, "decenter": True},
    "UserObscuration": {"view": "_S_UserObscuration", "fields": _USER, "decenter": True},
    # FloatingAperture: the capture shows NO decenter fields -> has_decenter=False
    # (pinned from per_type_field_map, fail-closed per the spec instruction).
    "FloatingAperture": {"view": "_S_FloatingAperture", "fields": (), "decenter": False},
}

# The ordered member tuple (the live enum is the runtime source of truth; this is the
# offline catalog the firewall lists in an "unknown type" refusal).
APERTURE_TYPE_NAMES = tuple(APERTURE_TYPES.keys())

# The read-back proof's absolute floor (the _cb_cells._READBACK_ABS_TOL shape): tight
# enough (≈ double-precision ULP near 1.0) to catch a >1e-15 value the engine collapsed
# to 0.0, while rel_tol=1e-9 governs normal magnitudes.
_READBACK_ABS_TOL = 1e-15


def aperture_fields(token):
    """The FULL ordered field list for ``token`` = type-specific fields + decenters.

    The type-specific fields from the table PLUS the two decenter fields when
    ``has_decenter`` (appended from the ONE ``_DECENTER_FIELDS`` constant). For
    ``None``/``FloatingAperture`` this is the empty tuple. Returns a tuple of
    ``(friendly, dotnet, kind, proof)`` tuples.
    """
    row = APERTURE_TYPES[token]
    fields = tuple(row["fields"])
    if row["decenter"]:
        fields = fields + _DECENTER_FIELDS
    return fields


def has_decenter(token):
    """True iff ``token`` carries ApertureXDecenter/YDecenter (every type but None/Floating)."""
    return bool(APERTURE_TYPES[token]["decenter"])


# --------------------------------------------------------------------------- #
# Enum resolution (live-enum-is-truth, same seam as _cb_cells / enums._resolve_enum).
# --------------------------------------------------------------------------- #
def _aperture_type_enum(system):
    """Resolve the live ``SurfaceApertureTypes`` enum TYPE (fake-injectable).

    A fake system injects ``_enum_types["SurfaceApertureTypes"]``; otherwise the live
    ``ZOSAPI.Editors.LDE.SurfaceApertureTypes`` namespace. A resolution failure
    surfaces as a ``ToolParamError`` (a param-class problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceApertureTypes" in injected:
        return injected["SurfaceApertureTypes"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return _lde.SurfaceApertureTypes
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SurfaceApertureTypes from ZOSAPI.Editors.LDE: {exc}"
        )


def _aperture_member(system, token):
    """Resolve ``token`` on the LIVE ``SurfaceApertureTypes`` enum (unknown -> loud).

    Uses the SAME ``_resolve_enum`` seam every tier uses — an unknown member is
    rejected LOUD listing the valid members, never silently mapped.
    """
    from ..enums import _resolve_enum

    return _resolve_enum(_aperture_type_enum(system), token)


# --------------------------------------------------------------------------- #
# The FAIL-CLOSED typed-view resolver (the ONE place; the §1a guard).
# --------------------------------------------------------------------------- #
def _typed_view(settings, token, surface=None):
    """Resolve the ``_S_<TypeName>`` typed view, FAILING CLOSED (the §1a trap guard).

    The EXACTLY-ONE place a view is resolved. Returns ``getattr(settings, "_S_" +
    view_member)`` for the catalog's view attr. An absent ``_S_`` attr (the getattr
    THROWS / returns nothing — future engine drift) RAISES ``ApertureWriteError``
    ("the typed view is absent — refusing rather than the bare-settings silent
    no-op"). It MUST NEVER fall through to the bare ``settings`` object: that
    fall-through re-introduces the §1a #76 silent-no-op trap (a bare ``MaximumRadius``
    write does not take, reads back the 10000.0 default, and would obscure-everything
    while passing every offline test). This single fail-closed resolver is what makes
    the no-op firewall complete.

    L26 self-check: there is NO ``except``-and-return-settings path here. The only
    return is the resolved ``_S_`` view; every failure RAISES.
    """
    if token not in APERTURE_TYPES:
        # Unreachable in production (the handler resolves the type against the live
        # enum first), but the substrate refuses an unknown token rather than guess.
        raise ApertureWriteError(
            f"unknown aperture type {token!r}; valid: {list(APERTURE_TYPE_NAMES)}",
            field="aperture_type", intended=token, actual=None, surface=surface,
        )
    view_attr = APERTURE_TYPES[token]["view"]
    try:
        view = getattr(settings, view_attr)
    except Exception as exc:  # noqa: BLE001 — the _S_ view is absent -> FAIL CLOSED
        raise ApertureWriteError(
            f"the typed aperture view {view_attr!r} is absent on the settings object "
            f"({exc!r}); refusing rather than falling through to the bare-settings "
            "silent no-op (the §1a trap)",
            field="typed_view", intended=view_attr, actual=None, surface=surface,
        ) from exc
    if view is None:
        raise ApertureWriteError(
            f"the typed aperture view {view_attr!r} resolved to None; refusing rather "
            "than falling through to the bare-settings silent no-op (the §1a trap)",
            field="typed_view", intended=view_attr, actual=None, surface=surface,
        )
    return view


# --------------------------------------------------------------------------- #
# Field write (through the VIEW only) + read-back.
# --------------------------------------------------------------------------- #
def write_aperture_fields(view, token, values, surface=None):
    """Write each declared field of ``token`` onto the typed ``view`` (THE write path).

    ``values`` is ``{friendly: value}`` carrying ALREADY-VALIDATED values (the handler
    ran the firewall first — this substrate trusts them but still writes the correct
    accessor per kind). Writes through the VIEW only (never the bare settings object).
    A field absent from ``values`` is skipped (an omitted optional). A write THROW ->
    ``ApertureWriteError``.

    int kind (NumberOfArms) -> write the Python int; double kind -> write the float;
    string kind (ApertureFile) -> write the str. The engine reads every numeric back as
    a Double (probe §2); the kind only decides the WRITE accessor + the read-back proof.
    """
    for friendly, dotnet, kind, _proof in aperture_fields(token):
        if friendly not in values:
            continue
        value = values[friendly]
        try:
            setattr(view, dotnet, value)
        except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_aperture
            raise ApertureWriteError(
                f"could not write aperture field {friendly!r} ({dotnet}) = {value!r} "
                f"on the typed view ({exc!r}); refusing rather than shipping an "
                "unverified aperture",
                field=friendly, intended=value, actual=None, surface=surface,
            ) from exc


def read_current_type(ad, surface=None):
    """``str(ad.CurrentType)`` — the live committed aperture type (read-back proof)."""
    try:
        return str(ad.CurrentType)
    except Exception as exc:  # noqa: BLE001 — a CurrentType read THROW -> surface_aperture
        raise ApertureWriteError(
            f"could not read the live ApertureData.CurrentType ({exc!r}); the aperture "
            "is unverifiable — refusing rather than guessing it took",
            field="current_type", intended=None, actual=None, surface=surface,
        ) from exc


def _read_field(view, dotnet, surface=None):
    """Read ``getattr(view, dotnet)``, THROW-guarded -> ``ApertureWriteError``."""
    try:
        return getattr(view, dotnet)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_aperture
        raise ApertureWriteError(
            f"could not read aperture field {dotnet!r} off the live typed view "
            f"({exc!r}); the aperture is unverifiable — refusing rather than guessing",
            field=dotnet, intended=None, actual=None, surface=surface,
        ) from exc


def _readback_equal(intended, actual, kind):
    """Read-back equality per kind (double tight tolerance, int exact via round)."""
    if kind == "int":
        if actual is None or isinstance(actual, bool):
            return False
        if not isinstance(actual, (int, float)) or not math.isfinite(actual):
            return False
        # Defense-in-depth: a hypothetical non-integral live read-back
        # (e.g. 2.6) must NOT round-to-3 false-pass. Require the live value be EXACTLY
        # integral (an exact integral double 3.0 still passes) AND match intended. The
        # tiny abs floor admits double-precision ULP noise while rejecting a true 2.6.
        if not math.isclose(actual, round(actual), rel_tol=0.0, abs_tol=_READBACK_ABS_TOL):
            return False
        return int(round(actual)) == int(intended)
    # double: math.isclose with the abs floor (catches a >1e-15 value collapsed to 0.0).
    if actual is None or isinstance(actual, bool):
        return False
    if not isinstance(actual, (int, float)) or not math.isfinite(actual):
        return False
    return math.isclose(actual, float(intended), rel_tol=1e-9, abs_tol=_READBACK_ABS_TOL)


def read_back_aperture(ad, token, intended, surface=None):
    """Read the LIVE ``ad.CurrentTypeSettings._S_<token>`` and verify every field.

    ``intended`` is ``{friendly: value}`` (what the handler asked for; an omitted
    optional is absent). The read-back proof:

    1. (the TYPE proof is the handler's job — ``read_current_type``; this reads fields.)
    2. for each ``proof=="equal"`` field present in ``intended``: re-read it off the LIVE
       view and assert ``_readback_equal`` (per kind). A mismatch (the §1a silent no-op
       reads back the 10000.0 default) -> ``ApertureWriteError`` naming field/intended/actual.
    3. for the ``proof=="non_none"`` field (ApertureFile) present in ``intended``: assert
       the live value ``!= "None"`` AND is non-blank (the engine silently drops a
       non-resolvable path to ``"None"``, probe §4). NOT ``readback == written`` (the
       engine may canonicalize the path).

    Returns ``{friendly: live_value}`` for every declared field of ``token`` (the
    ``written`` echo — what actually stuck), so the handler's success envelope echoes the
    read-back values, not the request.
    """
    # None / FloatingAperture have no fields -> nothing to read (type proof only).
    fields = aperture_fields(token)
    if not fields:
        return {}
    try:
        live_settings = ad.CurrentTypeSettings
    except Exception as exc:  # noqa: BLE001 — a settings read THROW -> surface_aperture
        raise ApertureWriteError(
            f"could not read the live ApertureData.CurrentTypeSettings ({exc!r}); the "
            "aperture is unverifiable — refusing rather than guessing it took",
            field="current_settings", intended=None, actual=None, surface=surface,
        )
    # Resolve the LIVE typed view (fail-closed — never the bare settings object).
    view = _typed_view(live_settings, token, surface=surface)

    echo = {}
    for friendly, dotnet, kind, proof in fields:
        live = _read_field(view, dotnet, surface=surface)
        echo[friendly] = live
        if friendly not in intended:
            # An omitted optional field — we did not write it, so we do not prove it
            # against a value we never sent (its live value is still echoed).
            continue
        want = intended[friendly]
        if proof == "non_none":
            live_str = "" if live is None else str(live)
            if live_str.strip() == "" or live_str.strip() == "None":
                raise ApertureWriteError(
                    f"aperture field {friendly!r} read back {live_str!r} after the "
                    f"change (the engine silently dropped the path it could not "
                    f"resolve, probe §4): the .uda file '{want}' was NOT accepted. "
                    "Ensure it exists (the engine searches relative to "
                    "<Documents>/Zemax/Objects/Apertures); refusing rather than "
                    "shipping an aperture whose file did not stick (ApertureFile == "
                    "\"None\").",
                    field=friendly, intended=want, actual=live_str, surface=surface,
                )
            continue
        # proof == "equal".
        if not _readback_equal(want, live, kind):
            raise ApertureWriteError(
                f"aperture field {friendly!r} did not read back: wrote {want!r}, the "
                f"live typed view reads {live!r} — the write silently no-opped (the "
                "§1a base-interface trap reads back the 10000.0 default); refusing "
                "rather than shipping an unverified aperture",
                field=friendly, intended=want, actual=live, surface=surface,
            )
    return echo


__all__ = [
    "APERTURE_TYPES",
    "APERTURE_TYPE_NAMES",
    "aperture_fields",
    "has_decenter",
    "_aperture_type_enum",
    "_aperture_member",
    "_typed_view",
    "write_aperture_fields",
    "read_current_type",
    "read_back_aperture",
]
