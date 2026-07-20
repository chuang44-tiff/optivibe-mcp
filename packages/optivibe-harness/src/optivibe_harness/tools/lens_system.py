"""tools/lens_system.py — SystemData setters (field / wavelength / aperture).

Three dispatchable tools over ``system.SystemData``, each validating its enum
member against the LIVE .NET enum before writing, then read-back-
verifying:

- ``set_field``      — ``FieldType`` + the field set (replace all or set one).
- ``set_wavelength`` — a ``WavelengthPreset`` OR an explicit wavelength list
  (exactly one).
- ``set_aperture``   — ``ZemaxApertureType`` (NOT "ApertureType") + value.

The enum TYPE is resolved from the live ``ZOSAPI.SystemData`` namespace at call
time (the runtime source of truth); ``enums._resolve_enum`` then
guards the member-name ``getattr`` against it.

Live ZOS-API integration: exercised by the live test; unit-tested here against
a fake SystemData double whose enum types reproduce the live enum members.
"""
from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec


def _zosapi_enum(system, enum_name):
    """Resolve a live ZOSAPI.SystemData enum TYPE by name.

    The runtime source of truth for an enum's members is the live .NET enum. We
    resolve the TYPE object from the ``ZOSAPI.SystemData`` namespace (the module
    the probe captured the members under). A fake session injects a
    ``_enum_types`` mapping so unit tests resolve without the backend.

    Resolution order:
    1. ``session``/``system``-provided ``_enum_types`` mapping (test seam), if any;
    2. the live ``ZOSAPI.SystemData`` namespace import.
    """
    # Test seam: a fake system may expose ``_enum_types`` = {name: enum_type}.
    injected = getattr(system, "_enum_types", None)
    if injected is not None and enum_name in injected:
        return injected[enum_name]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.SystemData as _sd  # type: ignore

        return getattr(_sd, enum_name)
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve enum type {enum_name!r} from ZOSAPI.SystemData: {exc}"
        )


def _coerce_num(value, *, label):
    """Coerce a single number (reject bool / non-number) to float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a number, got {type(value).__name__} {value!r}"
        )
    return float(value)


def _coerce_triple(value, *, length, label):
    """Coerce a list/tuple of ``length`` numbers (reject bools)."""
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ToolParamError(
            f"{label} must be a list of {length} numbers, got {value!r}"
        )
    out = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ToolParamError(
                f"{label} entries must be numbers, got {type(item).__name__} {item!r}"
            )
        out.append(float(item))
    return out


def set_field(session, params):
    """Set the field type and (optionally) the field set, with read-back.

    ``field_type`` is resolved via the live ``FieldType`` enum. Provide EITHER
    ``fields`` (a list of ``[x, y, weight]`` triples that REPLACES the field set)
    OR a single-field edit identified by ``index`` (an in-place PARTIAL edit:
    supply AT LEAST ONE of ``x``/``y``/``weight``; only the supplied cells are
    written, the rest — and the field's vignetting factors — are left untouched)
    OR ``x``+``y``+``weight`` with NO ``index`` (append a new field). If neither
    ``fields`` nor any single-field key is given only the field type is set.

    ``field_type`` is REQUIRED for the replace-all path (a full re-spec declares
    the type) and for an append (a new field needs a type) but is OPTIONAL for an
    in-place single-field edit (``index`` given): a partial edit touches ONLY the
    requested field cell(s), so re-asserting the type is unnecessary and is SKIPPED
    when ``field_type`` is omitted (gap 9 — a weight-only edit no longer forces a
    replace-all, which would silently zero every field's vignetting).

    In every case the written x/y/weight of each affected field are read back and
    verified, so a silently-dropped or distorted field write raises
    ``SurfaceWriteError`` instead of returning ``ok``.
    """
    from . import _lens_common as _c

    system = session.system
    fields = system.SystemData.Fields

    new_fields = params.get("fields")
    single_keys = [k for k in ("index", "x", "y", "weight") if k in params]

    # ``fields`` (replace-all) and the single-field path are mutually exclusive.
    if new_fields is not None and single_keys:
        raise ToolParamError(
            "set_field accepts EITHER 'fields' (replace-all) OR a single "
            "x/y/weight (set one), not both"
        )

    # An in-place single-field PARTIAL edit (``index`` given) may OMIT ``field_type``
    # (gap 9): it touches only the supplied cell(s), so the type is not re-asserted.
    # Every other path (replace-all, append, type-only) keeps ``field_type`` required
    # and calls ``SetFieldType`` (benign per probe P3, but the contract is honest).
    #
    # HIGH (zero-mutation-on-refuse): RESOLVE the enum member here (validation-only —
    # ``_resolve_enum`` raises on a bad member WITHOUT mutating), but DEFER the actual
    # ``SetFieldType`` MUTATION. On the partial-edit path the mutation is held until the
    # partial edit itself (at-least-one-of-x/y/weight + index range) is known valid, so a
    # refused partial edit never silently flips the SYSTEM-WIDE field type. On the
    # replace-all/append/type-only paths the type is set immediately (unchanged behavior).
    is_partial_edit = "index" in params
    field_type_name = params.get("field_type")
    if is_partial_edit and field_type_name is None:
        member = None  # SetFieldType skipped on a partial in-place edit
    else:
        if field_type_name is None:
            raise ToolParamError(
                "set_field requires 'field_type' (only an in-place single-field "
                "edit identified by 'index' may omit it)"
            )
        enum_type = _zosapi_enum(system, "FieldType")
        member = _resolve_enum(enum_type, field_type_name)
        if not is_partial_edit:
            # Replace-all / append / type-only: set the type now (behavior unchanged).
            fields.SetFieldType(member)

    # ``intended`` maps the 1-based field index that must read back to its
    # intended (x, y, weight) triple after the write.
    intended = {}

    if new_fields is not None:
        # ---- Replace-all path: rebuild the field set from the triples. ----
        if not isinstance(new_fields, (list, tuple)) or not new_fields:
            raise ToolParamError(
                "fields must be a non-empty list of [x, y, weight] triples"
            )
        triples = [
            _coerce_triple(f, length=3, label="fields entry") for f in new_fields
        ]
        original_count = int(fields.NumberOfFields)
        for x, y, w in triples:
            fields.AddField(x, y, w)
        # Remove the pre-existing fields (indices 1..original_count) from the top
        # down so indices stay valid; the API is 1-based for field rows.
        for i in range(original_count, 0, -1):
            fields.DeleteFieldAt(i)
        intended_count = len(triples)
        for idx, (x, y, w) in enumerate(triples, start=1):
            intended[idx] = (x, y, w)
    elif "index" in params:
        # ---- Single-field PARTIAL in-place edit (gap 9): write only the ----
        # supplied cell(s); leave the rest — AND the field's vignetting — untouched.
        # No AddField/DeleteFieldAt, so vignetting is preserved BY CONSTRUCTION.
        supplied = [k for k in ("x", "y", "weight") if k in params]
        if not supplied:
            raise ToolParamError(
                "single-field set_field (with 'index') requires at least one of "
                "'x', 'y', 'weight' to edit; none were supplied"
            )
        current_count = int(fields.NumberOfFields)
        index = _c._require_int_index(params, "index")
        if not (1 <= index <= current_count):
            raise ToolParamError(
                f"field index {index} out of range; valid 1..{current_count}"
            )
        f = fields.GetField(index)
        # Coerce the supplied cells (nan/inf -> ToolParamError) BEFORE any mutation, so a
        # bad value refuses with the field type still unchanged too. Read the others back
        # so the read-back-verify intended-triple is the post-edit state of the field.
        x = _coerce_num(params["x"], label="x") if "x" in params else float(f.X)
        y = _coerce_num(params["y"], label="y") if "y" in params else float(f.Y)
        w = (
            _coerce_num(params["weight"], label="weight")
            if "weight" in params else float(f.Weight)
        )
        # The partial edit is now FULLY validated (≥1 supplied cell, in-range index, every
        # supplied value coerced) — only NOW apply the field type (if one was supplied),
        # so ANY refused partial edit above leaves the SYSTEM-WIDE field type UNCHANGED
        # (zero-mutation-on-refuse, HIGH + L26 sibling: nan/inf coercion too).
        if member is not None:
            fields.SetFieldType(member)
        if "x" in params:
            f.X = x
        if "y" in params:
            f.Y = y
        if "weight" in params:
            f.Weight = w
        edited_keys = supplied
        target_index = index
        intended_count = int(fields.NumberOfFields)
        intended[target_index] = (x, y, w)
    elif single_keys:
        # ---- Append a new field (no index): requires all of x/y/weight. ----
        for key in ("x", "y", "weight"):
            if key not in params:
                raise ToolParamError(
                    "appending a field (single-field set_field with no 'index') "
                    f"requires all of 'x', 'y', 'weight' (missing {key!r})"
                )
        x = _coerce_num(params["x"], label="x")
        y = _coerce_num(params["y"], label="y")
        w = _coerce_num(params["weight"], label="weight")
        fields.AddField(x, y, w)
        target_index = int(fields.NumberOfFields)
        intended_count = int(fields.NumberOfFields)
        intended[target_index] = (x, y, w)
    else:
        intended_count = int(fields.NumberOfFields)

    actual_count = int(fields.NumberOfFields)
    if actual_count != intended_count:
        raise SurfaceWriteError(
            f"set_field field count mismatch: intended={intended_count} "
            f"actual={actual_count}",
            field="field_count",
            intended=intended_count,
            actual=actual_count,
            surface=None,
        )

    # Read-back-verify the written x/y/weight of each affected field.
    out_fields = []
    for i in range(1, actual_count + 1):
        f = fields.GetField(i)
        ax, ay, aw = float(f.X), float(f.Y), float(f.Weight)
        if i in intended:
            ix, iy, iw = intended[i]
            _c._verify_or_raise(f"field_{i}_x", ix, ax, surface=None)
            _c._verify_or_raise(f"field_{i}_y", iy, ay, surface=None)
            _c._verify_or_raise(f"field_{i}_weight", iw, aw, surface=None)
        out_fields.append(
            {"x": safe_float(f.X), "y": safe_float(f.Y), "weight": safe_float(f.Weight)}
        )
    # Echo the field type. On a partial in-place edit that OMITTED ``field_type``
    # (member is None), report the LIVE type so the agent sees what the field set
    # actually is; if the live read is unavailable, echo None (the omitted value).
    out_field_type = field_type_name
    if is_partial_edit and member is None:
        try:  # pragma: no cover - depends on the live/fake GetFieldType
            out_field_type = str(fields.GetFieldType())
        except Exception:  # noqa: BLE001 — disclosure-only, never fail the edit
            out_field_type = None

    result = {
        "field_type": out_field_type,
        "field_count": actual_count,
        "fields": out_fields,
    }
    # §3 additive disclosure: the replace-all (AddField/DeleteFieldAt) path ZEROES
    # every field's vignetting factors (VDX/VDY/VCX/VCY) — probe Q4 — so the agent knows
    # to re-author vignetting (set_vignetting) AFTER a field-set replace.
    if new_fields is not None:
        result["vignetting_reset"] = True
    # Gap 9: a single-field PARTIAL in-place edit (``index`` given) PRESERVES every
    # field's vignetting BY CONSTRUCTION (no AddField/DeleteFieldAt). Stamp the inverse
    # disclosure so the agent SEES a weight-only edit kept the vignetting, plus which
    # field/cells were touched.
    if is_partial_edit:
        result["vignetting_preserved"] = True
        result["edited_field"] = target_index
        result["edited_keys"] = list(edited_keys)
    return result


def set_wavelength(session, params):
    """Set wavelengths via a preset OR an explicit list (exactly one).

    ``preset`` is resolved via the live ``WavelengthPreset`` enum then applied via
    ``SelectWavelengthPreset`` (returns ``bool`` -> ``False`` -> SurfaceWriteError).
    ``wavelengths`` is a list of ``[value_um, weight]`` pairs: the set is cleared
    and rebuilt via ``AddWavelength``. Both/neither -> ``ToolParamError``.
    """
    from . import _lens_common as _c

    system = session.system
    wavelengths = system.SystemData.Wavelengths
    preset_name = params.get("preset")
    explicit = params.get("wavelengths")

    has_preset = preset_name is not None
    has_list = explicit is not None
    if has_preset == has_list:
        raise ToolParamError(
            "set_wavelength requires EXACTLY ONE of 'preset' or 'wavelengths'"
        )

    # Maps a 1-based wavelength index to its intended (value, weight) for the
    # explicit-list read-back; empty for the preset path (which has no caller-
    # supplied values to compare against).
    intended = {}

    if has_preset:
        enum_type = _zosapi_enum(system, "WavelengthPreset")
        member = _resolve_enum(enum_type, preset_name)
        ok = wavelengths.SelectWavelengthPreset(member)
        if not bool(ok):
            raise SurfaceWriteError(
                f"SelectWavelengthPreset({preset_name!r}) returned False",
                field="preset",
                intended=preset_name,
                actual=False,
                surface=None,
            )
        # Read-back the resulting set: a preset must produce a non-empty, coherent
        # wavelength set (a True return with a no-op write must not pass).
        preset_count = int(wavelengths.NumberOfWavelengths)
        if preset_count < 1:
            raise SurfaceWriteError(
                f"SelectWavelengthPreset({preset_name!r}) returned True but the "
                f"resulting wavelength set is empty (count={preset_count})",
                field="preset",
                intended=preset_name,
                actual=preset_count,
                surface=None,
            )
        out_preset = preset_name
    else:
        pairs = [
            _coerce_triple(p, length=2, label="wavelengths entry") for p in explicit
        ]
        if not pairs:
            raise ToolParamError("wavelengths must be a non-empty list of [value, weight]")
        # Clear then rebuild: remove all but one, overwrite the first via add.
        for i in range(int(wavelengths.NumberOfWavelengths), 1, -1):
            wavelengths.RemoveWavelength(i)
        first_value, first_weight = pairs[0]
        w1 = wavelengths.GetWavelength(1)
        w1.Wavelength = first_value
        w1.Weight = first_weight
        for value, weight in pairs[1:]:
            wavelengths.AddWavelength(value, weight)
        intended_count = len(pairs)
        actual_count = int(wavelengths.NumberOfWavelengths)
        if actual_count != intended_count:
            raise SurfaceWriteError(
                f"set_wavelength count mismatch: intended={intended_count} "
                f"actual={actual_count}",
                field="wavelength_count",
                intended=intended_count,
                actual=actual_count,
                surface=None,
            )
        for idx, (value, weight) in enumerate(pairs, start=1):
            intended[idx] = (value, weight)
        out_preset = None

    count = int(wavelengths.NumberOfWavelengths)
    out = []
    for i in range(1, count + 1):
        w = wavelengths.GetWavelength(i)
        # Read-back-verify the written value/weight of each set wavelength.
        if i in intended:
            iv, iw = intended[i]
            _c._verify_or_raise(
                f"wavelength_{i}_value", iv, float(w.Wavelength), surface=None
            )
            _c._verify_or_raise(
                f"wavelength_{i}_weight", iw, float(w.Weight), surface=None
            )
        out.append({"value": safe_float(w.Wavelength), "weight": safe_float(w.Weight)})
    return {"preset": out_preset, "wavelength_count": count, "wavelengths": out}


def set_aperture(session, params):
    """Set the system aperture type + value with read-back.

    ``aperture_type`` is resolved via the live ``ZemaxApertureType`` enum (note:
    NOT "ApertureType"). Both the type and value are written and read back.
    """
    system = session.system
    aperture = system.SystemData.Aperture
    aperture_type_name = params.get("aperture_type")
    if "value" not in params:
        raise ToolParamError("set_aperture requires 'value'")
    value = params["value"]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"aperture value must be a number, got {type(value).__name__} {value!r}"
        )
    value = float(value)

    enum_type = _zosapi_enum(system, "ZemaxApertureType")
    member = _resolve_enum(enum_type, aperture_type_name)

    aperture.ApertureType = member
    aperture.ApertureValue = value

    # Read-back: the type str() is the member name; value compares numeric.
    actual_type = str(aperture.ApertureType)
    if actual_type != aperture_type_name:
        raise SurfaceWriteError(
            f"aperture type did not take effect: intended={aperture_type_name!r} "
            f"actual={actual_type!r}",
            field="aperture_type",
            intended=aperture_type_name,
            actual=actual_type,
            surface=None,
        )
    actual_value = safe_float(aperture.ApertureValue)
    from . import _lens_common as _c

    _c._verify_or_raise("aperture_value", value, actual_value, surface=None)
    return {"aperture_type": aperture_type_name, "value": actual_value}


SET_FIELD_SPEC = ToolSpec(
    name="set_field",
    handler=set_field,
    # ``field_type`` is CONDITIONALLY required (the replace-all / append / type-only
    # paths) — enforced in the handler, NOT via JSON-Schema — and OPTIONAL for an
    # in-place single-field edit identified by ``index`` (gap 9). So it is NOT in
    # the unconditional ``required_params``.
    required_params=(),
    param_types={
        "field_type": "string",
        "fields": "array",
        "x": "number",
        "y": "number",
        "weight": "number",
        "index": "number",
    },
    description=(
        "Set the field type (FieldType enum) and optionally replace the field "
        "set with [x, y, weight] triples; read-back verifies. To edit ONE field "
        "in place (e.g. a weight only), pass 'index' with any of x/y/weight — "
        "this PRESERVES the field's vignetting (a replace-all zeroes it). Resolve "
        "the field type (angle vs object/image height) from intent; a wrong member "
        "is loud-rejected. 'field_type' is required except for an in-place edit."
    ),
)

SET_WAVELENGTH_SPEC = ToolSpec(
    name="set_wavelength",
    handler=set_wavelength,
    required_params=(),
    param_types={
        "preset": "string",
        "wavelengths": "array",
    },
    description=(
        "Set wavelengths via a WavelengthPreset OR an explicit [value_um, weight] "
        "list (exactly one); read-back verifies the count. Resolve the preset from "
        "intent (its tokens are cryptic); a wrong preset is loud-rejected."
    ),
)

SET_APERTURE_SPEC = ToolSpec(
    name="set_aperture",
    handler=set_aperture,
    required_params=("aperture_type", "value"),
    param_types={
        "aperture_type": "string",
        "value": "number",
    },
    description=(
        "Set the system aperture (ZemaxApertureType enum, incl. ObjectSpaceNA) "
        "and value with read-back. Resolve the aperture type (e.g. f-number vs "
        "entrance-pupil-diameter vs NA) from intent; a wrong member is loud-rejected."
    ),
)

TOOL_SPECS = (SET_FIELD_SPEC, SET_WAVELENGTH_SPEC, SET_APERTURE_SPEC)
