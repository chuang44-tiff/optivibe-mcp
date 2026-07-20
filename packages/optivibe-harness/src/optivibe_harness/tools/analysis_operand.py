"""tools/analysis_operand.py — get_operand: direct stateless scalar via the MFE.

``get_operand`` reads a single scalar merit-function operand value DIRECTLY via
``system.MFE.GetOperandValue`` — the 9-arg call is a stateless evaluation.
It is now SLOT-AWARE: the 9-arg
``GetOperandValue(type, p2, p3, ..., p9)`` positional args have OPERAND-SPECIFIC
meanings, so a caller param (``ring``/``samp``/``surf``/``wave``/``hx``…) is placed
into the slot named by the operand's OWN live Header map, NOT a fixed positional
guess. The map is read GENERICALLY via a temp row (``AddOperand`` ->
``ChangeType(member)`` -> ``_merit_cells.read_param_map(op)`` returns the ORDERED
``{Header: {kind}}`` = positional slots 2,3,4,…; -> ``RemoveOperandAt`` in a
``finally`` so the MFE stays byte-identical). Examples (probe-captured):
``RWCE``: slot2 = Ring; ``RWRE``: slot2 = Samp; ``REAY``/``RAGY``: slot2 = Surf;
``EFFL``: Wave only.

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
        if isinstance(value, float) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"{key!r} must be an integer, got {type(value).__name__} {value!r}"
        )
    return int(value)


def _read_slot_headers(system, member):
    """Read the operand's ORDERED slot->Header map via an atomic temp row.

    ``op = mfe.AddOperand()`` -> ``op.ChangeType(member)`` -> the ordered
    ``_merit_cells.read_param_map(op)`` (``{Header: {col, kind, value}}`` for the
    non-blank cols 2..9, in column order = the positional slots) -> ALWAYS
    ``RemoveOperandAt`` in a ``finally`` (net-zero MFE mutation — the add_operand
    orphan-removal discipline). The temp row's operand number is captured
    IMMEDIATELY after the add so the right row is reaped even if a later read
    throws.

    Returns ``(headers, error)``: ``headers`` is the ordered map (or ``None`` on a
    failure) and ``error`` is a structured ``error_envelope`` (or ``None`` on
    success). NEVER raises — a ChangeType==False / read throw is caught and turned
    into a structured envelope so dispatch sees a clean ``ok:false``.
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
            headers = _mc.read_param_map(op)
        except SurfaceWriteError as exc:
            return None, _ac.error_envelope(
                "get_operand", "operand_param",
                f"could not read the operand's parameter-slot layout ({exc!r})",
            )
        return headers, None
    finally:
        _remove_temp_row(mfe, op, operand_number)


def _remove_temp_row(mfe, op, operand_number):
    """Best-effort removal of the temp row (net-zero MFE mutation). NEVER raises.

    Mirrors ``optimize_merit._remove_orphan``: remove the row the slot-map read
    added so the MFE is byte-identical afterward. The removal is itself GUARDED —
    a ``RemoveOperandAt`` throw during cleanup must not mask the caller's outcome
    nor escape as a dispatch ``internal``. ``operand_number`` is the index captured
    right after the add; if it was not captured, fall back to ``op.OperandNumber``
    (itself guarded).
    """
    try:
        number = operand_number if operand_number is not None else int(op.OperandNumber)
        mfe.RemoveOperandAt(number)
    except Exception:  # noqa: BLE001 — best-effort cleanup; never raises
        pass


def _build_slot_args(headers, supplied):
    """Map ``supplied`` caller params into the operand's ordered positional slots.

    ``headers`` is ``read_param_map``'s ordered ``{Header: {col, kind, value}}``.
    ``supplied`` is ``{param_name: value}`` (already typed) the caller passed.

    Returns ``(slot_args, applied_slots, slot_header_list, density_unset)``:

    - ``slot_args`` — the positional args (col order), each coerced int (int-kind
      slot) or float (double-kind), padded with 0 to the 8 trailing slots (a2..a9
      of the 9-arg call). A slot with no matching supplied param defaults to 0.
    - ``applied_slots`` — ``{Header: value}`` actually placed (the agent-facing map).
    - ``slot_header_list`` — the operand's slot Header list (col order).
    - ``density_unset`` — the Header name of a Ring/Samp slot left at 0 (the
      silent-0 flag trigger), or ``None``.

    The caller params are keyed BY HEADER (case-insensitive): a Header ``"Ring"``
    takes the ``ring`` param, ``"Surf"`` the ``surf`` param, etc. (``_PARAM_BY_HEADER``).
    """
    # Header (lower) -> the supplied caller value, if any.
    by_header_lower = {}
    for param_name, header_name in _PARAM_BY_HEADER.items():
        if param_name in supplied:
            by_header_lower[header_name.lower()] = supplied[param_name]

    slot_args = []
    applied_slots = {}
    slot_header_list = []
    density_unset = None

    for header, meta in headers.items():
        header_str = str(header)
        slot_header_list.append(header_str)
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
        slot_args.append(value)

    # Pad the trailing positional args to the 8 slots (a2..a9) the 9-arg call needs.
    while len(slot_args) < 8:
        slot_args.append(0)
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
    (``operand_param``); an unreadable parameter-slot layout (``operand_param``). A
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

    slot_args, applied_slots, slot_header_list, density_unset = _build_slot_args(
        headers, supplied
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
        "For an image-space intercept operand (REAX/REAY/REAZ) an omitted surf now "
        "defaults to the IMAGE surface (the image height ~= PIMH); other Surf-slot ray "
        "operands (RAG*/RAID/RANG) default surf to 0 (the OBJECT) with a disclosure "
        "flag — pass surf= to read a specific surface. The surface actually used is "
        "echoed as surf_defaulted. "
        "config (None|int|'all') reads the operand at one/every configuration."
    ),
)

TOOL_SPECS = (GET_OPERAND_SPEC,)
