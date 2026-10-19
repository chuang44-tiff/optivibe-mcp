"""tools/analysis_operand.py — get_operand: direct stateless scalar via the MFE.

``get_operand`` reads a single scalar merit-function operand value DIRECTLY via
``system.MFE.GetOperandValue`` — the 9-arg call is a stateless evaluation.
It is now SLOT-AWARE: the 9-arg
``GetOperandValue(type, p2, p3, ..., p9)`` positional args have OPERAND-SPECIFIC
meanings, so a caller param (``ring``/``samp``/``surf``/``wave``/``hx``…) is placed
into the slot named by the operand's OWN live Header map, NOT a fixed positional
guess. The map is read GENERICALLY via a temp row (``AddOperand`` ->
``ChangeType(member)`` -> ``_merit_cells.read_param_map(op)`` returns
``{Header: {col, kind}}``, and each value is placed at the argument for its OWN ``col``
(never by header order, since a blank col 2 shifts every slot) -> ``RemoveOperandAt`` in a
``finally`` so the MFE stays byte-identical). Examples (probe-captured):
``RWCE``: slot2 = Ring; ``RWRE``: slot2 = Samp; ``REAY``/``RAGY``: slot2 = Surf;
``EFFL``: Wave at col 3 (col 2 blank); ``FCGS``/``FCGT``: Wave/Hx/Hy at cols 3/4/5.

The silent-0 trap this closes (the gap's whole point): a SAMPLED operand
(``RWCE``/``RWRE``) read with its Ring/Samp slot left at 0 returns ~0 (e.g. RWCE
slot2=0 -> 5.3e-29) — a wrong-but-plausible value. So a Ring/Samp slot left unset
fires a ``flags`` entry naming it; a supplied ``ring``/``samp`` is validated with
``_measurement_common.valid_density`` (int >= 1) and a bad density is rejected with
an ``operand_param`` envelope (never a silently-evaluated density-0 reading).

GOTCHA: the signature is **9 args** — the trailing
extended-aperture slots are MANDATORY (a 7-arg call raises ``TypeError``); unused
slots default to 0. The operand name is resolved against the LIVE
``MeritOperandType`` enum via ``enums._resolve_enum`` (the live enum is the source
of truth; an unknown member is rejected up front). The returned value is the RAW
MFE operand reading (``raw_operand_reading: true``); a reading that is non-finite
(inf/nan) OR whose magnitude is ``>= 1e10`` is flagged ``suspicious_sentinel``
(decided on the RAW reading before safe_float stringifies a non-finite value).

Live ZOS-API integration: exercised by the analysis-readouts live test (RWCE with
ring=6 -> a non-zero WFE matching analyze_wavefront(samp=6); RWCE without ring ->
the silent-0 flag fires); unit-tested here against the slot-aware fake MFE.
"""
import math

from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _config_common as _cfg
from . import _merit_cells as _mc

# Magnitude at/above which a returned operand value is flagged as a likely
# sentinel/overflow (locked Verdict 5). 1e10 is the empty-system operand sentinel
# an empty-system probe captured; a real focal length / aberration never reaches it.
_SUSPICIOUS_MAGNITUDE = 1e10

# The caller-param -> Header name each maps to (case-insensitive on the live
# Header). The slot map is read from the LIVE operand (NOT a per-operand table);
# this is only the param-name <-> Header-name correspondence so a value lands in
# the slot the operand's OWN Header map names. A Header with no matching supplied
# param defaults to 0 (the engine's unused-slot default).
_PARAM_BY_HEADER = {
    "ring": "Ring",
    "samp": "Samp",
    "surf": "Surf",
    "wave": "Wave",
    "hx": "Hx",
    "hy": "Hy",
    "px": "Px",
    "py": "Py",
    "ex": "Ex",
    "ey": "Ey",
}
# The density (sampling) Header names: a slot whose Header is one of these read at
# 0 makes a sampled operand silently read ~0 (the gap). Lower-cased for matching.
_DENSITY_HEADERS = frozenset({"ring", "samp"})

# A field/pupil coordinate of 0 asks for the on-axis / chief default, so on an operand
# with no such slot it cannot change the reading: it passes. Zero is NOT neutral for
# surf (0 is the OBJECT), wave, ring or samp, so those never take this exemption.
_ZERO_NEUTRAL_PARAMS = frozenset({"hx", "hy", "px", "py", "ex", "ey"})

# (G1) The slot-2 Header of the ray-operand family (REAX/REAY/REAZ, RAG*, RAID/RANG/…).
_SURF_HEADER = "surf"

# (G1) The INTERCEPT-COORDINATE subfamily: an omitted surf on these defaults to the
# IMAGE surface (an image-height read is the overwhelming use; Surf=0=OBJECT is never
# a useful intercept). CURATED BY OPERAND CODE, not by Header layout — REAX/REAY/REAZ
# share the IDENTICAL {Surf,Wave,Hx,Hy,Px,Py} layout with RAG* and the angle family
# (probe §G1), so a "has a Surf slot?" heuristic CANNOT distinguish them; the code is
# the only discriminator. Membership is opt-in (the _BFSD_DATA / asphere-order-map
# precedent): an unlisted intercept operand falls through to disclosed surf-0, never a
# silent wrong image read.
_INTERCEPT_COORD_OPERANDS = frozenset({"REAX", "REAY", "REAZ"})


def _merit_operand_enum(system):
    """Resolve the live ``MeritOperandType`` enum TYPE.

    The runtime source of truth is the live .NET enum on the
    ``ZOSAPI.Editors.MFE`` namespace. A fake system injects a ``_enum_types``
    mapping (``{"MeritOperandType": <fake enum>}``) so unit tests resolve without
    the backend; otherwise the live namespace is imported.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "MeritOperandType" in injected:
        return injected["MeritOperandType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.MFE as _mfe  # type: ignore

        return _mfe.MeritOperandType
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve MeritOperandType from ZOSAPI.Editors.MFE: {exc}"
        )


def _num(params, key):
    """Pull an optional numeric param (default 0); reject bool/non-number."""
    if key not in params:
        return 0.0
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{key!r} must be a number, got {type(value).__name__} {value!r}"
        )
    return float(value)


def _int_param(params, key):
    """Pull an optional integer param (default 0); reject bool/non-integer."""
    if key not in params:
        return 0
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        # Allow an integral float (a JSON round-trip can float an int).
        if isinstance(value, float) and math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"{key!r} must be an integer, got {type(value).__name__} {value!r}"
        )
    return int(value)


def _read_slot_headers(system, member):
    """Read the operand's ORDERED slot->Header map via an atomic temp row.

    ``op = mfe.AddOperand()`` -> ``op.ChangeType(member)`` -> the ordered
    ``_merit_cells.read_param_map(op)`` (``{Header: {col, kind, value}}`` for the
    non-blank cols 2..9; each entry's ``col`` IS its positional slot, and blank columns
    are skipped, so the map's ORDER is not the slot index) -> ALWAYS
    ``RemoveOperandAt`` in a ``finally`` (net-zero MFE mutation — the add_operand
    orphan-removal discipline). The temp row's operand number is captured
    IMMEDIATELY after the add so the right row is reaped even if a later read
    throws.

    Returns ``(headers, error)``: ``headers`` is the ordered map (or ``None`` on a
    failure) and ``error`` is a structured ``error_envelope`` (or ``None`` on
    success). NEVER raises an ``Exception`` — a ChangeType==False / any read throw is
    caught and turned into a structured envelope so dispatch sees a clean ``ok:false``.
    A temp row that could NOT be removed also returns an error envelope (carrying
    ``temp_row_not_removed``): the merit function was changed, so the read is refused.
    """
    mfe = system.MFE
    try:
        op = mfe.AddOperand()
    except Exception as exc:  # noqa: BLE001 — an AddOperand throw -> structured
        # The add is guarded like its sibling failure paths (ChangeType throw,
        # ChangeType==False, read_param_map throw): a degraded engine where the
        # temp-row add itself throws must surface the SAME structured operand_param
        # envelope, never escape into the dispatcher's generic ``internal`` family.
        # Nothing was added, so there is no temp row to reap.
        return None, _ac.error_envelope(
            "get_operand", "operand_param",
            f"could not add a temp row to read the operand's parameter slots "
            f"({exc!r})",
        )
    # Capture the row number BEFORE any later read can throw (so the reap targets
    # the right row even on a degraded engine).
    operand_number = None
    try:
        operand_number = int(op.OperandNumber)
    except Exception:  # noqa: BLE001 — fall back to op.OperandNumber at reap time
        operand_number = None
    try:
        headers, error = _typed_slot_map(op, member)
    finally:
        removed = _remove_temp_row(mfe, op, operand_number)
    if removed:
        return headers, error
    # The temp row is still in the merit function. That is a mutation the caller did not
    # ask for, so the read is REFUSED and the row is named, never returned as ok:true.
    detail = f" (the slot read had already failed: {error['error']})" if error else ""
    return None, _ac.error_envelope(
        "get_operand", "operand_param",
        f"the temporary merit-function row used to read the operand's parameter slots "
        f"could not be removed, so the merit function may now hold one extra row"
        f"{'' if operand_number is None else f' (row {operand_number})'}. Inspect it with "
        f"dump_merit_function and delete it with remove_operand before optimizing."
        f"{detail}",
        temp_row_not_removed=operand_number if operand_number is not None else "unknown",
    )


def _typed_slot_map(op, member):
    """``(headers, error)`` for a freshly added temp row. NEVER raises an ``Exception``."""
    try:
        changed = op.ChangeType(member)
    except Exception as exc:  # noqa: BLE001 — a ChangeType throw -> structured
        return None, _ac.error_envelope(
            "get_operand", "operand_param",
            f"could not type a temp row to the operand to read its parameter "
            f"slots ({exc!r})",
        )
    if changed is False:
        return None, _ac.error_envelope(
            "get_operand", "operand_param",
            "the engine refused to type a temp row to this operand (ChangeType "
            "returned False) — its parameter slots are unreadable",
        )
    try:
        return _mc.read_param_map(op), None
    except Exception as exc:  # noqa: BLE001 — ANY read fault -> structured, not only
        # SurfaceWriteError: a degraded read must not escape as a bare exception.
        return None, _ac.error_envelope(
            "get_operand", "operand_param",
            f"could not read the operand's parameter-slot layout ({exc!r})",
        )


def _remove_temp_row(mfe, op, operand_number):
    """Remove the temp row (net-zero MFE mutation). NEVER raises; returns whether it did.

    Mirrors ``optimize_merit._remove_orphan``: remove the row the slot-map read
    added so the MFE is byte-identical afterward. A ``RemoveOperandAt`` throw during
    cleanup must not escape as a dispatch ``internal`` -- and must not be HIDDEN either:
    ``False`` tells the caller the row may still be there. ``operand_number`` is the index
    captured right after the add; if it was not captured, fall back to
    ``op.OperandNumber`` (itself guarded).
    """
    try:
        number = operand_number if operand_number is not None else int(op.OperandNumber)
        mfe.RemoveOperandAt(number)
    except Exception:  # noqa: BLE001 — never raises; the caller discloses the failure
        return False
    return True


class _SlotColumnError(ValueError):
    """A slot header carried a ``col`` that is not an exact int in 2..9 (degraded read)."""


def _build_slot_args(headers, supplied):
    """Place ``supplied`` caller params into the 9-arg call BY THE OPERAND'S COLUMN.

    ``headers`` is ``read_param_map``'s ``{Header: {col, kind, value}}`` where ``col`` is
    the MFE cell column 2..9. ``supplied`` is ``{param_name: value}`` (already typed).

    Returns ``(slot_args, applied_slots, slot_header_list, density_unset)``:

    - ``slot_args`` -- the 8 positional args a2..a9 of the 9-arg ``GetOperandValue``,
      argument ``aN`` for column ``N``: the header at column ``c`` lands at index
      ``c - 2``. A value is the supplied param coerced int (int-kind slot) or float
      (double-kind), or ``0`` for a column with no supplied param or no header (a blank
      column such as EFFL's col 2, which the engine ignores).
    - ``applied_slots`` -- ``{Header: value}`` exactly as placed (the agent-facing map).
      There are no cells to read back here: ``GetOperandValue`` is a stateless call and
      the temp row was only used to read the slot map. The guarantee is therefore that
      the SAME column map that NAMES a slot also POSITIONS its argument.
    - ``slot_header_list`` -- the operand's slot Header list (col order).
    - ``density_unset`` -- the Header name of a Ring/Samp slot left at 0 (the
      silent-0 flag trigger), or ``None``.

    The caller params are keyed BY HEADER (case-insensitive): a Header ``"Ring"``
    takes the ``ring`` param, ``"Surf"`` the ``surf`` param, etc. (``_PARAM_BY_HEADER``).

    Placement is by COLUMN, never by header order: an operand whose first
    header is not at column 2 (FCGS/FCGT: Wave col 3, Hx col 4, Hy col 5; EFFL: Wave col
    3) used to have every argument shifted one slot left. Fails CLOSED with
    ``_SlotColumnError`` on a ``col`` that is not an exact int in 2..9 -- never a
    fall-back to order.
    """
    # Header (lower) -> the supplied caller value, if any.
    by_header_lower = {}
    for param_name, header_name in _PARAM_BY_HEADER.items():
        if param_name in supplied:
            by_header_lower[header_name.lower()] = supplied[param_name]

    slot_args = [0] * 8
    applied_slots = {}
    slot_header_list = []
    density_unset = None

    for header, meta in headers.items():
        header_str = str(header)
        slot_header_list.append(header_str)
        col = meta.get("col") if isinstance(meta, dict) else None
        if type(col) is not int or not 2 <= col <= 9:
            raise _SlotColumnError(
                f"slot header #{len(slot_header_list)} carries a column that is not an int "
                f"in 2..9 (a degraded header read)"
            )
        kind = meta.get("kind")
        hl = header_str.lower()
        if hl in by_header_lower:
            raw_value = by_header_lower[hl]
            if kind == "int":
                value = int(raw_value)
            else:
                value = float(raw_value)
            applied_slots[header_str] = value
        else:
            value = 0
            # A density (Ring/Samp) slot left at 0 is the silent-0 trap.
            if hl in _DENSITY_HEADERS:
                density_unset = header_str
        slot_args[col - 2] = value

    return slot_args, applied_slots, slot_header_list, density_unset


def get_operand(session, params):
    """Read one scalar operand SLOT-AWARELY via the 9-arg ``GetOperandValue``.

    Resolves ``operand`` against the live ``MeritOperandType`` enum, reads the
    operand's ordered slot->Header map via an atomic temp row, maps the caller's
    params (``ring``/``samp``/``surf``/``wave``/``hx``/``hy``/``px``/``py``/``ex``/
    ``ey``) into the slot the operand's OWN Header map names, then calls
    ``mfe.GetOperandValue(type, *slot_args)`` STATELESSLY (after the temp row is
    gone). The value goes through ``safe_float``; ``raw_operand_reading`` is always
    True and ``suspicious_sentinel`` is True when the RAW reading is non-finite or
    ``abs(raw) >= 1e10``.

    Returns a structured ``{ok:false}`` envelope (NEVER raised past the boundary)
    for: an unresolvable operand (``operand_unknown``); a bad density param
    (``operand_param``); an unreadable parameter-slot layout (``operand_param``); a
    caller parameter the operand has no slot for (``operand_param``, with
    ``unplaced_params`` + ``slot_headers`` -- a zero hx/hy/px/py/ex/ey is exempt). A
    sampled operand (RWCE/RWRE) whose Ring/Samp slot is left at 0 still returns the
    value but adds a ``flags`` entry naming the unset density slot (the silent-0
    disclosure — a density-0 sampled operand reads ~0, not a real value).

    (S4) config (None|int|"all") selects the multi-config configuration: None=current
    (byte-identical), int=that config, 'all'=sweep every config into a per_config vector
    (config_headline = the operand value, so config_differs reflects per-config variation).
    The operand resolution + slot-Header map + density guard are config-INDEPENDENT and run
    ONCE outside the sweep (the temp-row read is NOT repeated per config). A bad config ->
    operand_param.
    """
    system = session.system
    operand = params.get("operand")
    if not isinstance(operand, str) or operand == "":
        raise ToolParamError(
            f"operand must be a non-empty string, got {operand!r}"
        )

    config = params.get("config") if isinstance(params, dict) else None

    enum_type = _merit_operand_enum(system)
    try:
        member = _resolve_enum(enum_type, operand)
    except ToolParamError as exc:
        # An unknown operand is an EXPECTED failure class -> structured envelope
        # (do not raise into dispatch; pre-empt with a clean ok:false).
        return _ac.error_envelope(
            "get_operand", "operand_unknown", str(exc), operand=operand
        )

    # Read the per-param scalars (these RAISE ToolParamError on a bad type, matching
    # the existing dispatch-caught pattern). ring/samp validated as densities below.
    supplied = {}
    for key in ("surf", "wave"):
        if key in params:
            supplied[key] = _int_param(params, key)
    for key in ("hx", "hy", "px", "py", "ex", "ey"):
        if key in params:
            supplied[key] = _num(params, key)

    # ring / samp: validate as densities (int >= 1) — a bad density is a sampled
    # operand miswrite, rejected pre-evaluation with the operand_param envelope so a
    # density-0/sub-1 reading is NEVER reported as a real value. The density predicate
    # is the SINGLE shared one (_measurement_common.valid_density, L30); imported
    # function-locally to avoid the module-level import cycle (that module imports
    # _merit_operand_enum/_SUSPICIOUS_MAGNITUDE from here).
    from ._measurement_common import density_as_int, valid_density

    for key in ("ring", "samp"):
        if key in params:
            d = params[key]
            if not valid_density(d):
                return _ac.error_envelope(
                    "get_operand", "operand_param",
                    f"{key!r} must be a sampling density >= 1 (int), got {d!r}; a "
                    "density < 1 makes a sampled operand read ~0 — refusing rather "
                    "than evaluating a silent-0 reading",
                    operand=operand,
                )
            supplied[key] = density_as_int(d)

    # Read the operand's ordered slot->Header map (atomic temp row, net-zero).
    headers, error = _read_slot_headers(system, member)
    if error is not None:
        return error

    # What the CALLER supplied, captured before the tool injects its own surf default.
    caller_params = sorted(supplied)

    # G1: image-space ray-operand surf default (config-independent — resolved once).
    # Runs AFTER the slot-Header read + BEFORE _build_slot_args, injecting the resolved
    # surf into `supplied` so _build_slot_args places it into applied_slots naturally
    # (no _build_slot_args signature change). Surface count is config-independent -> once.
    surf_defaulted = None
    surf_flag = None
    if "surf" not in supplied:
        has_surf = any(str(h).lower() == _SURF_HEADER for h in headers)
        if has_surf:
            # Function-local import (mirrors the density_as_int/valid_density local
            # import — _measurement_common imports from this module at module level).
            from ._measurement_common import image_surface_index
            code = operand.strip().upper()
            if code in _INTERCEPT_COORD_OPERANDS:
                img = image_surface_index(system)
                if img is not None:
                    supplied["surf"] = img
                    surf_defaulted = img
                    surf_flag = (
                        f"surf not supplied for image-space intercept {operand}; "
                        f"defaulted to the IMAGE surface {img} (an intercept at the "
                        f"image is the image height, ~= PIMH). Pass surf= to read a "
                        f"specific surface."
                    )
                else:
                    supplied["surf"] = 0
                    surf_defaulted = 0
                    surf_flag = (
                        f"surf not supplied for {operand} AND the image surface could "
                        f"not be resolved; read at surf 0 (the OBJECT) which is almost "
                        f"certainly WRONG — pass surf= explicitly."
                    )
            else:
                supplied["surf"] = 0
                surf_defaulted = 0
                surf_flag = (
                    f"surf not supplied for {operand}; defaulted to surf 0 (the "
                    f"OBJECT). A per-surface global/angle ray operand (RAG*/RAID/"
                    f"RANG/…) needs an explicit surf= — surf 0 reads the object-plane "
                    f"value, a plausible-but-WRONG number. Pass surf= to read a "
                    f"specific surface."
                )

    try:
        slot_args, applied_slots, slot_header_list, density_unset = _build_slot_args(
            headers, supplied
        )
    except _SlotColumnError as exc:
        # A degraded slot-map read (a col that is not an int in 2..9): same family the
        # slot-header read serves (operand_param). The temp row is already reaped.
        return _ac.error_envelope(
            "get_operand", "operand_param",
            f"could not place the operand's parameter slots ({exc})",
            operand=operand,
        )

    # A caller parameter the operand has no slot for was never placed, so the call cannot
    # return what was asked for (STRH has no Hy slot: hy=1 used to read the ON-AXIS value
    # with ok:true). Refuse before any value is read. The temp row is already reaped.
    placed = {h.lower() for h in applied_slots}
    unplaced = [
        key for key in caller_params
        if _PARAM_BY_HEADER[key].lower() not in placed
        and not (key in _ZERO_NEUTRAL_PARAMS and supplied[key] == 0)
    ]
    if unplaced:
        real_slots = list(slot_header_list)   # blank columns are already excluded
        message = (
            f"operand {operand} has no parameter slot for {', '.join(unplaced)}, so the "
            f"call cannot return the value asked for; refusing rather than reading a "
            f"different quantity. Its parameter slots are: "
            f"{', '.join(real_slots) if real_slots else 'none'}."
        )
        if any(h.lower() == "field" for h in real_slots):
            message += (
                f" {operand} selects its field by the integer Field slot, which "
                f"get_operand cannot set."
            )
            if operand.strip().upper() == "STRH":
                message += " For a Strehl ratio at a field use analyze_strehl(field=N)."
        return _ac.error_envelope(
            "get_operand", "operand_param", message,
            operand=operand, unplaced_params=unplaced, slot_headers=real_slots,
        )

    flags = []
    if density_unset is not None:
        flags.append(
            f"operand {operand} has a {density_unset} density slot left at 0 — a "
            f"sampled operand reads ~0 unless the density is >= 1; pass "
            f"{density_unset.lower()}= to set it"
        )
    if surf_flag is not None:
        flags.append(surf_flag)

    def _grade(sess):
        mfe = sess.system.MFE
        raw = mfe.GetOperandValue(member, *slot_args)
        # Suspicion is decided on the RAW reading, BEFORE/independent of safe_float
        # (safe_float turns a non-finite reading into a STRING sentinel). Flag suspicious
        # when the raw value is non-finite (inf/nan) OR its magnitude reaches the sentinel
        # threshold. Guard the isinstance/math.isfinite calls so we never raise here.
        suspicious = False
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if not math.isfinite(raw) or abs(raw) >= _SUSPICIOUS_MAGNITUDE:
                suspicious = True
        value = safe_float(raw)
        result = {
            "ok": True,
            "operand": operand,
            "value": value,
            "units": "operand-defined",
            "raw_operand_reading": True,
            "suspicious_sentinel": bool(suspicious),
            "applied_slots": applied_slots,
            "slot_headers": slot_header_list,
            "config_headline": value,        # S4: the per-config divergence scalar
        }
        # (G1) Echo the surface actually used when a surf default fired (a
        # config-independent scalar closed over; each per-config copy carries it).
        if surf_defaulted is not None:
            result["surf_defaulted"] = surf_defaulted
        if flags:
            result["flags"] = list(flags)    # list() so each config entry gets its own copy
        return result

    try:
        return _cfg.evaluate_over_configs(session, config, _grade)
    except ToolParamError as exc:
        # A bad config selector (resolve_config_selector raises) is an EXPECTED param class
        # for get_operand (which pre-empts its families with envelopes, not @_never_raise).
        return _ac.error_envelope("get_operand", "operand_param", str(exc), operand=operand)


GET_OPERAND_SPEC = ToolSpec(
    name="get_operand",
    handler=get_operand,
    required_params=("operand",),
    param_types={
        "operand": "string",
        "ring": "number",
        "samp": "number",
        "surf": "number",
        "wave": "number",
        "hx": "number",
        "hy": "number",
        "px": "number",
        "py": "number",
        "ex": "number",
        "ey": "number",
        "config": "number",
    },
    description=(
        "Read one scalar optical quantity by operand code (e.g. EFFL) directly, "
        "without adding it to the merit function. Params are placed SLOT-AWARELY "
        "into the operand's own parameter slots (e.g. ring->Ring, samp->Samp, "
        "surf->Surf, wave->Wave). A SAMPLED operand (RWCE/RWRE) NEEDS its density "
        "set: pass ring= (RWCE) or samp= (RWRE) >= 1, else it reads ~0 and a flag "
        "is returned. Returns the raw operand reading with a suspicious_sentinel "
        "overflow flag plus applied_slots (the {Header:value} placed). "
        "A parameter the operand has NO slot for is REFUSED (the call cannot return "
        "what was asked): e.g. STRH has no hy slot -- it selects its field by a Field "
        "slot get_operand cannot set, so read a Strehl at a field with analyze_strehl. "
        "A zero hx/hy/px/py/ex/ey on an operand without that slot is accepted. "
        "For an image-space intercept operand (REAX/REAY/REAZ) an omitted surf now "
        "defaults to the IMAGE surface (the image height ~= PIMH); other Surf-slot ray "
        "operands (RAG*/RAID/RANG) default surf to 0 (the OBJECT) with a disclosure "
        "flag — pass surf= to read a specific surface. The surface actually used is "
        "echoed as surf_defaulted. "
        "config (None|int|'all') reads the operand at one/every configuration. "
        "BEFORE YOU TRUST AN MTF OPERAND READING: cross-check the operand reading "
        "against get_mtf at the same field+frequency -- two independent engine paths -- "
        "and only then treat it as the number."
    ),
)

TOOL_SPECS = (GET_OPERAND_SPEC,)
