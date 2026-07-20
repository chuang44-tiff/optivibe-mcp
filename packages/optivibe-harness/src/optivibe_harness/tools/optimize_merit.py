"""tools/optimize_merit.py — build_merit / add_operand / dump_merit_function.

Three dispatchable tools over the Merit Function Editor (MFE), all probe-grounded:

- ``build_merit``         — build the default RMS-spot merit via the SEQ wizard
  (``Apply()`` then ``OK()`` — NOT ``CommonSettings()``, which is a property that
  raises ``TypeError: 'IWizard' object is not callable``).
  Read back ``NumberOfOperands`` + ``CalculateMeritFunction``; a build that did NOT
  grow the operand count past the placeholder -> ``optimize_no_merit``.
- ``add_operand``         — ``op = mfe.AddOperand()`` -> ``op.ChangeType(<TYPE>)``
  -> set ``op.Target`` / ``op.Weight`` DIRECT (no cell access). The operand
  token is validated against the LIVE ``MeritOperandType`` enum BEFORE
  ``ChangeType``; an unknown token -> ``optimize_param``. Target/Weight are read
  back off ``op.Target`` / ``op.Weight`` through the ``_lens_common`` firewall.
- ``dump_merit_function`` — NON-MUTATING read of the full MFE: per-operand
  type/target/weight/value/contribution + the merit. Never mutates.

Live ZOS-API integration: exercised by the live closed-loop test; unit-tested
against the fixture-seeded fake MFE/wizard.
"""
import hashlib
import math
import os
import re
import tempfile

from .._io import safe_float
from ..enums import _resolve_enum
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _config_common as _ccfg
from . import _lens_common as _lc
from . import _merit_cells as _mc
from . import _optimize_common as _oc
from . import _structural_common as _sc


# MTF-aware merit: the MTF-family operand prefixes. An MTF-family merit
# operand (MTFT/MTFS/MTFA/MTFN/MTFX + the geometric GMT*, square-wave MSW*, Huygens
# MTH* siblings) selects the FIELD by an INTEGER ``Field`` index at slot 4 — NOT by
# Hx/Hy (the probe's load-bearing catch: a value at Hx/Hy silently reads the on-axis
# field). The predicate is a 3-char prefix match, NOT a per-code branch, so every
# member of the four families is covered without enumeration. Used ONLY to gate two
# DISCLOSURE behaviours on ``add_operand`` (the Hx/Hy-mis-field message enrichment +
# the Field-unset silent-on-axis WARN); it changes NO authoring path.
_MTF_FAMILY_PREFIXES = ("MTF", "GMT", "MSW", "MTH")

# The ``Field``-selector Header an MTF-family operand carries at slot 4 (probe).
_MTF_FIELD_HEADER = "Field"

# The field-selection param NAMES an agent might MIS-PASS on an MTF operand (the
# RWCE/REAY pupil-coordinate convention) — these are NOT how an MTF operand selects a
# field (it uses the integer ``Field`` index). A ParamCoercionError naming one of these
# on an MTF-family operand gets the enriched, MTF-aware message.
_MTF_FIELD_MISWRITE_NAMES = frozenset({"hx", "hy", "px", "py"})

# The exact shape of the ``apply_params`` unknown-name ParamCoercionError (``_merit_cells``):
# ``operand <TOKEN> has no parameter '<NAME>'; valid: [...]``. We anchor the mis-field match
# to the OFFENDING name only (the ``'<NAME>'`` after ``has no parameter``) — NOT the whole
# message, which also contains the operand's ``valid: [...]`` Header list. Anchoring closes a
# LATENT fragility: if a future MTF operand ever listed a pupil coordinate as a
# VALID header, a whole-message match would mis-enrich an unrelated bad-name error.
_MTF_UNKNOWN_PARAM_RE = re.compile(r"has no parameter '([^']+)'")


def _is_mtf_family(operand) -> bool:
    """True iff ``operand`` is an MTF-family merit operand (3-char prefix match).

    Pure string predicate over ``_MTF_FAMILY_PREFIXES`` (MTF/GMT/MSW/MTH). Covers
    MTFT/MTFS/MTFA/MTFN/MTFX + the GMT*/MSW*/MTH* siblings (the same ``Field``-slot
    field selection) without a per-code table. A non-MTF operand (EFFL/RWCE/CONF) ->
    False, so the guard is strictly MTF-scoped (the non-MTF authoring path stays
    byte-identical).
    """
    return str(operand).upper()[:3] in _MTF_FAMILY_PREFIXES


def _mtf_field_miswrite_name(operand, exc):
    """The mis-passed field-coordinate param name on an MTF operand, or ``None``.

    Returns the offending param NAME (``Hx``/``Hy``/``Px``/``Py``, original case from
    the params dict) when ``operand`` is MTF-family AND the ParamCoercionError ``exc``
    was raised for one of those names — so ``add_operand`` can ENRICH the
    ``merit_param`` message to steer the agent to the integer ``Field`` index. Returns
    ``None`` otherwise (a non-MTF operand, or a different param-class error) so the
    plain message is used.

    The match is ANCHORED to the OFFENDING name only: the ``'<NAME>'`` token after
    ``has no parameter`` in the ``apply_params`` ParamCoercionError (case-insensitive).
    It does NOT scan the whole message — which also carries the operand's
    ``valid: [...]`` Header list — so an unrelated bad name (``'Foo'``) is never
    mis-classified as a pupil-coordinate mis-field even if a future MTF operand listed
    a pupil coordinate as a valid header. If the message shape can't be
    parsed (any other ParamCoercionError form), we DON'T enrich — the plain message
    still helps (safe fallback).
    """
    if not _is_mtf_family(operand):
        return None
    match = _MTF_UNKNOWN_PARAM_RE.search(str(exc))
    if match is None:
        return None  # unparseable shape -> no enrichment (the plain message still helps)
    offending = match.group(1)
    if offending.lower() in _MTF_FIELD_MISWRITE_NAMES:
        return offending.capitalize()
    return None


def _read_mtf_field_cell(op):
    """Read back the live ``Field`` cell value of an MTF-family operand, or ``None``.

    Walks the operand's live param signature (``_mc.read_param_map`` — the SAME
    type-aware, THROW-guarded reader the authoring path uses) and returns the integer
    value of the ``Field`` slot. Returns ``None`` if the operand has no ``Field`` slot
    (a malformed/degraded layout) — the caller then simply omits the disclosure. NEVER
    raises: a read fault degrades to ``None`` (best-effort disclosure, not a failure of
    the author that already succeeded).
    """
    try:
        live = _mc.read_param_map(op)
    except Exception:  # noqa: BLE001 — a degraded read -> no disclosure (author succeeded)
        return None
    info = live.get(_MTF_FIELD_HEADER)
    if info is None:
        return None
    value = info.get("value")
    try:
        return int(value)
    except Exception:  # noqa: BLE001 — a non-int Field cell -> no disclosure
        return None


def _reap_leading_conf_to(mfe, count_before):
    """Reap LEADING ``CONF`` rows while ``NumberOfOperands > count_before`` (bounded).

    Extracted VERBATIM from ``_remove_orphan``'s auto-seed loop so ``_remove_orphan``
    and the ``_reap_config_bracket_to`` share ONE leading-reap body (§1.7,
    no-divergence). Reaps ONLY a LEADING ``CONF`` (the engine's auto-seeded current-config
    marker at row 1) — never real content — bounded by ``guard < 4`` + fully guarded. NEVER
    raises during cleanup. ``count_before is None`` is a no-op (no baseline captured).
    """
    if count_before is None:
        return
    guard = 0
    try:
        while int(mfe.NumberOfOperands) > count_before and guard < 4:
            guard += 1
            top = mfe.GetOperandAt(1)
            if str(top.TypeName) != "CONF":
                break  # a non-CONF leading row is real content — never reap it
            mfe.RemoveOperandAt(1)
    except Exception:  # noqa: BLE001 — best-effort baseline restore; never raise
        pass


def _reap_config_bracket_to(mfe, count_before):
    """Reap the ``config=`` CONF rows ABOVE ``count_before`` (the hand bracket + auto-seed).

    The ``config=`` reap (§1.7): a ``config=k`` author can introduce TWO CONF rows
    above the baseline — the hand-authored ``CONF k`` at the TOP of the stack (appended by
    ``_author_config_bracket``) AND the engine's LEADING auto-seed at row 1. This
    is invoked ONLY from the ``config=`` failure paths, where every row ABOVE ``count_before``
    is provably one WE added (the operand orphan, if any, is removed by number first), so
    reaping the TOP-of-stack ``CONF`` is safe (unlike the legacy ``_remove_orphan`` loop,
    whose leading-only firewall must NOT reap a real top-of-stack CONF). Removes the TOP
    ``CONF`` while the count is above ``count_before``, then delegates to the shared
    leading-reap for the auto-seed. Bounded + fully guarded — NEVER raises during cleanup.
    """
    if count_before is None:
        return
    guard = 0
    try:
        # Reap the hand-authored CONF at the TOP of the stack (and any sibling we appended)
        # while the count is above the baseline + the top row is a CONF.
        while int(mfe.NumberOfOperands) > count_before and guard < 4:
            guard += 1
            top_index = int(mfe.NumberOfOperands)
            if str(mfe.GetOperandAt(top_index).TypeName) != "CONF":
                break  # a non-CONF top row is the operand orphan (removed elsewhere) / content
            mfe.RemoveOperandAt(top_index)
    except Exception:  # noqa: BLE001 — best-effort; the leading reap below still runs
        pass
    # Then reap the engine's LEADING auto-seed CONF (row 1) to the exact baseline.
    _reap_leading_conf_to(mfe, count_before)


def _truncate_mfe_to(mfe, count_before):
    """Remove top MFE rows until ``NumberOfOperands == count_before`` (bounded, guarded).

    The ETGT per-row fail-safe: when ``AddOperand`` half-
    mutates (appends a row + increments the count) then throws WITHOUT returning the handle
    (``op is None``), the appended orphan cannot be reaped by number — truncate the count
    back to the captured baseline so no leaked row poisons the stored wizard boundary
    ``B``/``sig_hash`` (which is re-read + fingerprinted from the post-authoring MFE). Used
    ONLY from the single-config ETGT path (no CONF auto-seed), so every row above
    ``count_before`` is provably one WE just added. Bounded (``guard < 4``) + fully guarded —
    NEVER raises during cleanup. ``count_before is None`` (the ``NumberOfOperands`` read
    itself threw -> nothing was appended) is a no-op.
    """
    if count_before is None:
        return
    guard = 0
    try:
        while int(mfe.NumberOfOperands) > count_before and guard < 4:
            guard += 1
            mfe.RemoveOperandAt(int(mfe.NumberOfOperands))
    except Exception:  # noqa: BLE001 — best-effort baseline restore; never raise
        pass


def _remove_orphan(mfe, op, operand_number=None, *, count_before=None,
                   reap_bracket=False):
    """Best-effort removal of a half-authored ``add_operand`` row. NEVER raises.

    On an ``add_operand`` error path AFTER ``mfe.AddOperand()`` created the row, this
    removes the orphan so a failed add leaves the MFE UNCHANGED (transactional —
    mirrors the recipe layer's atomic discipline).

    Self-adversary: the orphan-removal is itself GUARDED. A ``RemoveOperandAt``
    THROW during cleanup must NOT mask the original error nor escape as dispatch
    ``internal`` (best-effort cleanup — the original error wins). ``operand_number``
    is the index captured BEFORE any later mutation (so we remove the RIGHT row); if it
    was not captured (the ChangeType==False path, where the row number is still read
    off ``op``), fall back to ``op.OperandNumber`` — itself guarded.

    LIVE bug 2 (live-probed): on a MULTI-config MFE the FIRST
    ``AddOperand`` auto-seeds a LEADING ``CONF`` bracket (the engine's current-config
    marker) at row 1 IN ADDITION to the row we asked for — a single ``AddOperand`` grows
    the count by TWO. Removing only our row then leaves the count ONE ABOVE the pre-add
    baseline (the live gate caught a refused CONF leaving ``NumberOfOperands`` at 2, not
    1). When ``count_before`` (captured BEFORE ``AddOperand``) is supplied and the count
    still exceeds it AFTER our row is removed, reap the engine's auto-seeded leading
    ``CONF`` too — bounded + guarded (``_reap_leading_conf_to``) — so a REFUSED
    ``add_operand`` restores the MFE EXACTLY. Only a LEADING ``CONF`` is reaped (never real
    content), and only while the count is above the captured baseline.

    §1.7: ``reap_bracket=True`` (the ``config=k`` failure paths ONLY) additionally reaps
    the hand-authored ``CONF k`` at the TOP of the stack via ``_reap_config_bracket_to`` —
    safe ONLY there because every row ABOVE the baseline is provably one WE added (the legacy
    ``reap_bracket=False`` default keeps the leading-only firewall byte-identical, so a real
    top-of-stack CONF is never reaped on the non-``config=`` path).
    """
    try:
        number = operand_number if operand_number is not None else int(op.OperandNumber)
        mfe.RemoveOperandAt(number)
    except Exception:  # noqa: BLE001 — best-effort cleanup; the original error wins
        # The primary removal itself faulted: do NOT attempt the auto-seed cleanup below
        # (reaping a leading CONF while our orphan row persists would CORRUPT the MFE).
        # The original error wins; never raise.
        return

    # Restore the pre-add baseline by reaping the auto-seed (+ the hand bracket on the
    # config= path) — type-gated + bounded + guarded.
    if reap_bracket:
        _reap_config_bracket_to(mfe, count_before)
    else:
        _reap_leading_conf_to(mfe, count_before)


def _require_config_number(system, value):
    """Validate ``config=k`` 1-based against ``1..NumberOfConfigurations`` (§1.5).

    Returns ``(cfg, None)`` on a valid number or ``(None, error_envelope)`` on a bad
    value. REUSES ``_mc.row_ref_int`` — the SAME integer-acceptance rule the ``Cfg#``
    cell WRITER uses (``validate_valueless_control``), so the ``config=`` gate CANNOT
    diverge from the writer: an exact ``int`` or an integral ``float`` (``2.0``)
    passes; a ``bool`` (the bool-is-int trap), a non-integral float (``2.5``), a string,
    ``nan``/``inf`` all reject. The ``1..n_configs`` range uses the THROW-guarded
    ``safe_number_of_configurations`` (-> 1 on a fresh / non-MCE / wedged system), so
    ``config=2`` on a 1-config system refuses honestly.
    """
    cfg = _mc.row_ref_int(value)
    if cfg is None:
        return None, _oc.error_envelope(
            "add_operand", "optimize_param",
            f"config must be an integer config number, got {value!r}",
        )
    n_configs = _ccfg.safe_number_of_configurations(system)
    if not (1 <= cfg <= n_configs):
        return None, _oc.error_envelope(
            "add_operand", "optimize_param",
            f"config {cfg} is out of range; the system has {n_configs} "
            f"configuration(s) (valid 1..{n_configs})",
        )
    return cfg, None


def _author_config_bracket(session, mfe, cfg, count_before):
    """Author one ``CONF k`` row via the shipped value-less-control ``add_operand`` path.

    Returns ``None`` on success or the structured error envelope on failure (§1.4).
    DELEGATES to ``add_operand`` itself with the CONF spec — this REUSES the
    value-less-control path VERBATIM (the ``Cfg#`` cell read-back proof, the
    ``validate_valueless_control`` range check, the auto-seed-aware reap), the smallest
    correct implementation (an inline AddOperand->ChangeType->Cfg# sequence would
    DUPLICATE that logic, a divergence risk). NO recursion hazard: the recursive
    call passes NO ``config=``, so it cannot re-enter the bracket path. On failure we
    reap once more to ``count_before`` defensively (the auto-seed may have grown the
    count) so the caller sees the EXACT pre-call baseline.
    """
    conf = add_operand(session, {"operand": "CONF", "params": {"Cfg#": cfg}})
    if not conf.get("ok", False):
        _reap_config_bracket_to(mfe, count_before)
        conf.setdefault("note", "config wrap: CONF author failed; no operand authored")
        return conf
    return None


def _bool_param(params, key, default):
    """Pull an optional bool param; reject a non-bool (a client miswrite)."""
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{key!r} must be a bool, got {type(value).__name__} {value!r}"
        )
    return value


def _num_param(params, key, default):
    """Pull an optional numeric param (default ``default``); reject bool/non-number.

    ``default`` of ``None`` means "leave the engine default" (the caller skips the
    write when this returns ``None``).
    """
    if key not in params:
        return default
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{key!r} must be a number, got {type(value).__name__} {value!r}"
        )
    return float(value)


def _floor_param(params, key, default):
    """Pull an optional thickness-FLOOR param; reject nan/inf/negative/bool/non-number.

    A floor is a finite number ``>= 0`` (a target-0 floor is the explicit opt-out, a
    negative one is meaningless). Reuses ``_num_param``'s bool/non-number rejection,
    then adds the finite + non-negative gate. Raises ``ToolParamError`` (the caller
    converts it to the ``optimize_param`` envelope — ``build_merit`` never raises into
    dispatch).
    """
    value = _num_param(params, key, default)
    if value is None:
        return None
    if not math.isfinite(value):
        raise ToolParamError(f"{key!r} must be a finite number, got {value!r}")
    if value < 0:
        raise ToolParamError(f"{key!r} must be >= 0, got {value!r}")
    return value


def _int_count_param(params, key):
    """Pull an optional integer COUNT param (rings/arms); reject bool/non-integral.

    Mirrors the ``get_mtf.series`` strict-integer precedent: a bool is rejected (the
    ``isinstance(int)`` trap), a non-integral float (``3.5``) is rejected, a string is
    rejected. Returns ``None`` when the param is absent (leave the wizard default).
    Raises ``ToolParamError`` (converted to ``optimize_param`` by the caller).
    """
    if key not in params:
        return None
    value = params[key]
    if isinstance(value, bool):
        raise ToolParamError(f"{key!r} must be an integer count, got {value!r}")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    raise ToolParamError(f"{key!r} must be an integer count, got {value!r}")


def _resolve_count_index(wizard, count, *, count_attr, getter_name, label):
    """Find the wizard INDEX whose enumerated count == ``count`` (rings/arms).

    Enumerates ``0..wizard.<count_attr>-1`` calling ``wizard.<getter_name>(i)`` (the
    live ``GetRingAt`` / ``GetArmAt``) and returns the matching index. Does NOT assume
    ``index == count-1`` (probe-proven today, but a future reorder/limit stays correct).
    An unavailable count raises ``ToolParamError`` naming the LIVE-READ valid set (never
    a silent snap, never a hardcoded list). Converted to ``optimize_param`` by the caller.
    """
    n = int(getattr(wizard, count_attr))
    valid = []
    getter = getattr(wizard, getter_name)
    for i in range(n):
        available = int(getter(i))
        valid.append(available)
        if available == count:
            return i
    raise ToolParamError(
        f"{label}={count} is not an available pupil-sampling count; "
        f"the wizard offers {sorted(valid)}"
    )


# The frozen criterion-token set. The SEQ wizard's image-quality
# criterion is TWO int properties (probe: NOT a ``Criterion`` member — that is the
# QuickFocus tool): ``Data`` (Spot Radius / Wavefront / ...) + ``Type`` (RMS / PTV).
# ``build_merit`` previously set NEITHER, so both inherited the wizard's PERSISTED
# last-used value — a non-deterministic "default" (the probe saw OPDX / TRCY / MECS
# across runs). ``criterion`` maps to ``Data`` (spot -> "Spot Radius", wavefront ->
# "Wavefront") + PINS ``Type`` to "RMS", closing the whole non-determinism class.
_CRITERION_TOKENS = ("spot", "wavefront")

# The target NAME the wizard's ``GetDataTypeAt`` enumeration must expose for each token
# (probe §Item 3: Data=Spot Radius -> TRAC family; Data=Wavefront -> OPDX family). The
# index is RESOLVED by enumeration (never hardcoded ``Data=1``), so a wizard reorder /
# limit stays correct + a PERMUTED-order fake can falsify a hardcode.
_CRITERION_DATA_NAME = {"spot": "Spot Radius", "wavefront": "Wavefront"}

# The ETGT true-edge restoring-floor constants (§4).
_ETGT_EDGE_SAFETY_MARGIN = 0.2   # relative; target = min_glass * (1 + margin). FORCE-CALIBRATION,
                                 # NOT safety-load-bearing (check_clearance is the authority). Covers the
                                 # observed +15.5% dangerous own-vs-min divergence with headroom; over-
                                 # constrains a -divergence gap modestly (bounded by the merit balance,
                                 # harmless: a thicker-than-needed edge is safe). Tunable.
_ETGT_EDGE_WEIGHT = 10.0         # heavier than the wizard's weight-1 MNCG/MNEG (drowned by the heavy
                                 # RMS-spot operands, gotcha #240) so the ETGT force actually bites in a
                                 # FULL merit. This IS the mechanism by which ETGT beats MNEG in practice
                                 # — the WEIGHT, not the operand type. A one-sided restoring force whose
                                 # violation contribution is 0 on a healthy edge, so weight-10 never
                                 # distorts a healthy design. Tunable.

# The multi-config disclosure clause (§7.2) — anti-silent-absence, NO authoring on a
# multi-config build (the per-config CONF-bracket auto-seed hazard is deferred).
_ETGT_MULTICONFIG_NOTE = (
    "ETGT true-edge glass floors are single-config-only in this release; this "
    "multi-config build's glass edges are floored by the per-config wizard MNEG "
    "(weight 1). Per-config ETGT is deferred in this release. "
    "check_clearance(config=\"all\") remains the edge authority."
)


def _criterion_param(params):
    """Pull + validate the optional ``criterion`` token (default ``"spot"``).

    Rejects any value outside the frozen ``{"spot", "wavefront"}`` set (a bad token,
    the wrong case ``"Spot"``, a non-string, ``""``, ``True``) -> ``ToolParamError``
    (the caller converts it to the ``optimize_param`` envelope). Validated in the
    ``build_merit`` param block BEFORE the wizard is touched, so a bad token mutates
    NOTHING (§3.1).
    """
    if "criterion" not in params:
        return "spot"
    value = params["criterion"]
    # ``value in tuple`` is exact + type-safe: ``True``/``5``/``""``/``"Spot"`` all miss.
    if not isinstance(value, str) or value not in _CRITERION_TOKENS:
        raise ToolParamError(
            f"criterion must be one of {list(_CRITERION_TOKENS)}, got {value!r}"
        )
    return value


def _resolve_data_index(wizard, target_name, *, count_attr="NumberOfDataTypes",
                        getter_name="GetDataTypeAt", label="criterion"):
    """Find the wizard INDEX whose enumerated NAME == ``target_name`` (Data/Type).

    The STRING-match sibling of ``_resolve_count_index`` (the ring/arm resolver): the
    criterion getters (``GetDataTypeAt`` / ``GetTypeAt``) return NAME strings (not
    ints), so this enumerates ``0..wizard.<count_attr>-1`` and returns the matching
    index. Does NOT hardcode ``Data=1`` (probe-proven today, but a wizard reorder /
    limit stays correct, and a PERMUTED-order fake can falsify a hardcode). An unmatched target raises ``ToolParamError`` naming the LIVE-read set
    (never a silent snap / hardcoded index). Converted to ``optimize_param`` by the caller.
    """
    n = int(getattr(wizard, count_attr))
    valid = []
    getter = getattr(wizard, getter_name)
    for i in range(n):
        name = str(getter(i))
        valid.append(name)
        if name == target_name:
            return i
    raise ToolParamError(
        f"{label} target {target_name!r} is not an available wizard {label}; "
        f"the wizard offers {valid}"
    )


def _glass_floor_warning(system):
    """WARN str-or-None: a glass surface carries an exposed sag/thickness DOF while
    the LIVE MFE lacks the matching positive-target floor (widened from
    the center-only ``_glass_center_floor_warning``). NEVER raises.

    Reads the LIVE MFE, NOT the ``glass`` flag — so it is correct on every path
    (``glass=True`` with a positive ``min_glass`` self-silences via the positive-target
    scan; ``glass=True, min_glass=0`` authors a Target-0 MNEG that does NOT count, so it
    warns). Returns a single combined ``glass_floor_warning`` string of
    up to two clauses:

    - EDGE clause: >=1 glass surface with a free curvature/radius/conic/asphere
      variable (the EDGE driver) AND no positive MNEG. A steep glass surface can cross
      its neighbour at the clear aperture (a thin/NEGATIVE edge) while the center holds.
    - CENTER clause: >=1 glass surface with a free center-thickness variable AND no
      positive MNCG. A center thickness can optimize NEGATIVE.

    Reuses the SHARED ``_variable_inventory`` enumerator (one walk, never a
    drifting copy) + ``_min_positive_target`` (the ONE operand-target reader). The
    glass-classify vs floor-presence direction-of-error is asymmetric (§1.3): a Material
    read hiccup errs toward NOT counting (require a confirmed non-air surface — the
    all-air negative control), while a floor-presence read errs toward WARN (``None`` on
    doubt = unfloored). Advisory only — any hiccup degrades to ``None`` (no warn), never
    breaks a successful build.
    """
    try:
        member = _oc._solve_type_variable_enum(system)
        lde = system.LDE
        mfe = system.MFE

        # (1) free-DOF scan on CONFIRMED-GLASS surfaces only (glass-classify errs toward
        #     NOT counting — the load-bearing all-air negative control).
        free_shape = 0  # radius / conic / asphere-coeff -> EDGE driver
        free_thick = 0  # thickness                       -> CENTER driver
        for it in _oc._variable_inventory(system, member):
            src, cell = it.get("source"), it.get("cell")
            surf = it.get("surface")
            if surf is None:
                continue
            try:
                row = lde.GetSurfaceAt(int(surf))
                if _sc._material_is_air(row):  # air surface -> skip
                    continue
                # A MIRROR / coordinate-break / powered-non-Standard surface
                # is NOT glass — reuse the sibling inert-DOF predicate rather than re-derive
                # a MIRROR test inline. `_material_is_air("MIRROR")` is False, so without this
                # a free mirror radius (glass=False, no MNEG) would false-fire the glass-EDGE
                # warning (MNEG is meaningless on a reflective surface). Only a positively-True
                # classification skips (None = unprovable Type -> treat as plain glass, the
                # material-confirmed direction); a predicate throw is caught by the except
                # below -> skip (fail-closed-to-not-count, consistent with the air-read skip).
                if _oc._row_is_mirror_or_cb(row) is True:
                    continue
            except Exception:  # noqa: BLE001 — cannot confirm glass -> skip
                continue
            if src == "lde" and cell == "thickness":
                free_thick += 1
            elif (src == "lde" and cell in ("radius", "conic")) or src == "asphere":
                free_shape += 1

        if not (free_shape or free_thick):
            return None  # nothing exposed -> silent

        # (2) LIVE positive-target floor presence (fail-closed -> None ⟺ unfloored).
        edge_floored = _oc._min_positive_target(mfe, "MNEG") is not None
        center_floored = _oc._min_positive_target(mfe, "MNCG") is not None

        # (3) compose — ONE key, edge clause first, center clause second.
        clauses = []
        if free_shape and not edge_floored:
            clauses.append(
                f"{free_shape} glass surface(s) carry a free curvature/radius/asphere "
                "variable and the merit has NO edge floor (MNEG) — a glass EDGE can "
                "optimize thin/NEGATIVE (a steep surface crossing its neighbour at the "
                "clear aperture) while the center holds and the image merit barely moves"
            )
        if free_thick and not center_floored:
            clauses.append(
                f"{free_thick} glass surface(s) carry a free center-thickness variable "
                "and the merit has NO center floor (MNCG) — a center can optimize NEGATIVE"
            )
        if clauses:
            return (
                "exposed glass DOF without a thickness floor: "
                + "; ".join(clauses)
                + ". Pass glass=true (with a positive min_glass) to author MNCG+MNEG, "
                "or run check_clearance after optimize."
            )
    except Exception:  # noqa: BLE001 — advisory; never break a successful build
        return None
    return None


def _is_glass_edge_surface(row):
    """True iff surface ``row`` bounds a glass EDGE we should floor (§5.1).

    Reuses the SHARED classifiers VERBATIM (never re-derive "is glass"):
    ``_sc._material_is_air`` (a confirmed AIR gap is MNEA's job, not a glass edge) and
    ``_oc._row_is_mirror_or_cb`` (a confirmed MIRROR/CB/powered-non-Standard surface has
    no glass edge). Everything else — confirmed glass OR unprovable-but-readable — is a
    glass-edge surface (the safe authoring direction: a one-sided ETGT floor is 0 on a
    healthy edge, so a spurious author on an ambiguous surface is inert). A Material read
    THROW propagates out of ``_material_is_air`` (it RAISES, never fabricates "not air");
    the caller catches it per-surface and SKIPS (a surface we cannot even read is not
    floored).
    """
    if _sc._material_is_air(row) is True:        # confirmed AIR gap -> MNEA's job, not glass
        return False
    if _oc._row_is_mirror_or_cb(row) is True:    # confirmed MIRROR/CB/powered-nonstd -> no glass edge
        return False
    return True                                  # confirmed glass OR unprovable -> author (inert if healthy)


def _author_etgt_edge_floors(system, mfe, min_glass):
    """Author one ``ETGT(Surf=n, Target=min_glass*(1+margin), Weight=_ETGT_EDGE_WEIGHT)``
    per glass-edge surface ``n`` (§5.2). Returns
    ``(authored:int, glass_surfaces:int, incomplete:list[int], fault:bool)``. NEVER raises.

    Per-row fail-safe: each row is authored in its own try; a ChangeType-False, a
    ``_merit_cells`` firewall raise (Surf/Target/Weight silent no-op), or a raw throw ->
    reap the half-authored orphan (``_remove_orphan``) + record the surface in
    ``incomplete`` + continue. A partial author yields fewer floors, never a poisoned
    boundary. A total failure (enum/LDE unresolvable, or every AddOperand throws) leaves
    ``authored == 0`` (the caller then emits NO key — the wizard MNEG still floors the
    build). Uses the EXACT shared machinery ``add_operand`` uses (``_resolve_enum`` +
    ``_mc.apply_params`` + the ``count_before=`` ``_remove_orphan``) — no new cell-write path.
    """
    authored = 0
    glass_surfaces = 0
    incomplete = []
    target = float(min_glass) * (1.0 + _ETGT_EDGE_SAFETY_MARGIN)

    # Resolve the ETGT enum member + the surface count ONCE. A failure here (enum
    # unresolvable — impossible on a live engine — or an unreadable LDE) is a total fault.
    try:
        member = _resolve_enum(_oc._merit_operand_enum(system), "ETGT")
        lde = system.LDE
        n_surfaces = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — enum/LDE unresolvable -> total fault (no rows, no raise)
        return (0, 0, [], True)

    # Iterate the INTERIOR surfaces 1..N-2 (object 0 + image excluded; n+1 must exist). A
    # cemented glass->glass interface surface is a glass-edge surface (its buried edge is
    # floored — buried-cement intent). One ETGT per glass-bounded gap.
    for n in range(1, n_surfaces - 1):
        try:
            row = lde.GetSurfaceAt(n)
            is_glass = _is_glass_edge_surface(row)
        except Exception:  # noqa: BLE001 — cannot classify (row/Material read throw)
            # A surface we cannot even read is NOT silently
            # dropped — record it in ``incomplete`` so the disclosure note fires (never
            # overstate coverage). It is NOT counted as a glass surface (we never proved it
            # glass) and no row was added, so nothing to reap.
            incomplete.append(n)
            continue
        if not is_glass:
            continue
        glass_surfaces += 1
        op = None
        count_before = None
        try:
            count_before = int(mfe.NumberOfOperands)  # read INSIDE the per-row guard
            op = mfe.AddOperand()
            if not bool(op.ChangeType(member)):
                _remove_orphan(mfe, op, count_before=count_before)
                incomplete.append(n)
                continue
            _mc.apply_params(op, {"Surf": n}, operand_token="ETGT")  # Integer Surf cell, read-back proven
            op.Target = target
            op.Weight = _ETGT_EDGE_WEIGHT
            # READ-BACK-AS-PROOF on Target/Weight: a silent no-op -> per-row hiccup.
            if not (
                math.isclose(float(op.Target), target, rel_tol=1e-9, abs_tol=1e-12)
                and math.isclose(
                    float(op.Weight), _ETGT_EDGE_WEIGHT, rel_tol=1e-9, abs_tol=1e-12
                )
            ):
                _remove_orphan(mfe, op, count_before=count_before)
                incomplete.append(n)
                continue
            authored += 1
        except Exception:  # noqa: BLE001 — per-row fail-safe: reap the orphan + record + continue
            try:
                if op is not None:
                    _remove_orphan(mfe, op, count_before=count_before)
                else:
                    # AddOperand may have half-mutated (appended a row) then thrown
                    # WITHOUT returning the handle -> reap COUNT-BASED to the captured baseline
                    # so no leaked row poisons B/sig_hash. ``count_before is None`` (the
                    # NumberOfOperands read itself threw -> nothing appended) is a no-op.
                    _truncate_mfe_to(mfe, count_before)
            except Exception:  # noqa: BLE001 — best-effort cleanup; never raise
                pass
            incomplete.append(n)
            continue

    fault = authored == 0
    return (authored, glass_surfaces, incomplete, fault)


def _etgt_authored_note(authored, target, min_glass, incomplete):
    """The §7.1 single-config authored-path note (LOAD-BEARING anti-silent-wrong copy).

    Names the OWN-semi convention, the ~46% BOTH-directions divergence, that a satisfied
    ETGT does NOT certify the manufacturable edge, and that ``check_clearance`` / the
    post-optimize ``thin_edge_warning`` is the AUTHORITY. A partial author (``incomplete``
    non-empty) appends the reaped-surfaces clause.
    """
    note = (
        f"Authored {authored} ETGT true-edge glass floor(s) at target {target:.3f} mm "
        f"(min_glass {min_glass} × {1.0 + _ETGT_EDGE_SAFETY_MARGIN:.2f}), weight "
        f"{_ETGT_EDGE_WEIGHT:g}, on the glass-edge surfaces. ETGT is an in-merit RESTORING "
        "FORCE evaluated at each surface's OWN semi-diameter — which diverges from "
        "check_clearance's common min(semi) audited edge by up to ~46% in BOTH directions "
        "(gotcha #240 class). A SATISFIED ETGT floor does NOT certify the manufacturable "
        "edge: on some geometries ETGT reads THICKER than the audited edge, so the audited "
        "edge can still be below min_glass. check_clearance (and the post-optimize "
        "thin_edge_warning on `optimize`) is the AUTHORITY for edge safety — run/read it "
        "after optimize. The safety margin biases the force to converge manufacturable but "
        "does NOT guarantee it."
    )
    if incomplete:
        note += (
            f"; {len(incomplete)} ETGT floor(s) could not be authored (engine no-op/throw) "
            "— fewer floors than glass surfaces; the wizard MNEG still floors those surfaces."
        )
    return note


def _etgt_edge_floor_key(glass, min_glass, n_configs, authored, glass_surfaces, incomplete):
    """The additive ``etgt_edge_floor`` disclosure dict, or ``None`` (§7). NEVER raises.

    - ``glass=False`` OR ``min_glass<=0`` -> ``None`` (gate off, byte-identical, §7.3).
    - ``n_configs>1`` -> the multi-config disclosure clause (§7.2 — authored:0, no rows).
    - single-config, ``authored>=1`` -> the authored/partial dict (§7.1).
    - single-config, ``authored==0`` (total author failure) -> ``None`` (§7.1 — wizard MNEG intact).
    """
    if not (glass and min_glass > 0):
        return None
    if n_configs > 1:
        return {
            "authored": 0,
            "scope": "single_config_only",
            "note": _ETGT_MULTICONFIG_NOTE,
        }
    if authored < 1:
        return None  # total author failure -> no key (silent-advisory; wizard MNEG floors)
    target = float(min_glass) * (1.0 + _ETGT_EDGE_SAFETY_MARGIN)
    return {
        "authored": authored,
        "glass_surfaces": glass_surfaces,
        "target": target,
        "min_glass": min_glass,
        "margin": _ETGT_EDGE_SAFETY_MARGIN,
        "weight": _ETGT_EDGE_WEIGHT,
        "convention": "own-semi",
        "incomplete": list(incomplete),
        "note": _etgt_authored_note(authored, target, min_glass, incomplete),
    }


# The per-config THIC-floor REWEIGHT constants (§2.1).
_MCE_THIC_FLOOR_WEIGHT = 100.0   # probe: HELD the MCE-overridden per-config THIC vs
                                 # the spot merit under DLS; weight-1 (the wizard's own)
                                 # was DROWNED to -50 (gotcha #240). ONE constant, ONE literal
                                 # (ETGT chose 10 for a glass EDGE; the THIC gap uses
                                 # the THIC-vs-spot-PROVEN value.)

# The wizard's per-config thickness-floor operand TypeNames the reweight strengthens. The AIR
# family (MNCA/MNEA) is authored on air surfaces, the GLASS family (MNCG/MNEG) on glass surfaces
# — reweight matches whichever the wizard placed on the THIC surface (material-BLIND, §1 axis 2).
_THIC_FLOOR_TYPES = ("MNCA", "MNEA", "MNCG", "MNEG")


def _reweight_per_config_thic_floors(system, mfe, weight):
    """Bump the wizard's own per-config THIC-surface floor rows to ``weight``.

    NEVER raises. Boundary-neutral (adds NO rows; the TypeName tuple is unchanged) — so the
    stored wizard boundary ``B``/``sig_hash`` are neutral and the reweight regenerates for
    free on a ``preserve_custom`` rebuild. Returns
    ``(n_reweighted:int, reweighted_surfaces:list[int], unfloored_thic_surfaces:list[int],
    reweight_failed_surfaces:list[int], fault:bool)``.

    1. THIC-surface set — walk ``system.MCE`` 1..NumberOfOperands, collect ``int(op.Param1)``
       for each ``str(op.TypeName)=="THIC"`` (the SAME read ``_scan_per_config_thin`` uses).
       Guarded per row.
    2. For each MFE row 1..NumberOfOperands keep iff ``str(op.TypeName) in _THIC_FLOOR_TYPES``
       AND its ``Surf1==Surf2 == k in THIC-surfaces`` (read Surf1/Surf2 via
       ``_mc.read_param_map(op)`` by Header — NEVER ``op.Value``, which reads 0.0 standalone
       even when correct, §3). Bump ``op.Weight = weight`` + read-back-prove
       ``math.isclose(float(op.Weight), weight)``. A per-row hiccup is SKIPPED (not counted).
    3. Per-surface BUCKETING (precedence success > failed > unfloored; a surface is in AT MOST
       one bucket):
       - ``reweighted`` = a THIC surface with >=1 matching floor row whose weight-write read
         back as ``weight`` (a successful strengthening).
       - ``reweight_failed`` (a convergent fix) = a THIC surface with a
         matching floor row but ZERO successful weight read-backs — a GENUINELY-FAILED
         strengthening (a silent ``op.Weight`` no-op / locked row). Made VISIBLE so an
         unstrengthened per-config gap is never invisible (weight-1 drowns to -50, gotcha #240).
       - ``unfloored`` = a THIC surface with NO matching floor row (a glass THIC under
         ``glass=False`` has no MNCG to strengthen -> greenfield authoring deferred, §9).
    4. A total MCE/MFE scan throw -> ``(0, [], [], [], True)``.
    """
    # (1) the per-config THIC surface set from the MCE (the _scan_per_config_thin read).
    try:
        mce = system.MCE
        n_mce = int(mce.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a wedged MCE -> total fault (no rows, no raise)
        return (0, [], [], [], True)
    thic_surfaces = set()
    for row in range(1, n_mce + 1):
        try:
            op = mce.GetOperandAt(row)
            if str(op.TypeName) != "THIC":
                continue
            thic_surfaces.add(int(op.Param1))
        except Exception:  # noqa: BLE001 — an unreadable THIC row contributes nothing
            continue
    if not thic_surfaces:
        # No per-config THIC surface -> nothing to strengthen (NOT a fault; key absent).
        return (0, [], [], [], False)

    # (2) walk the MFE, bumping the wizard's floor rows on those THIC surfaces.
    try:
        n_mfe = int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 — an unreadable MFE count -> total fault
        return (0, [], [], [], True)
    n_reweighted = 0
    bumped_surfaces = set()
    surfaces_with_floor = set()
    for i in range(1, n_mfe + 1):
        try:
            op = mfe.GetOperandAt(i)
            if str(op.TypeName) not in _THIC_FLOOR_TYPES:
                continue
            pmap = _mc.read_param_map(op)
            s1 = pmap.get("Surf1")
            s2 = pmap.get("Surf2")
            if s1 is None or s2 is None:
                continue
            k1 = int(s1["value"])
            k2 = int(s2["value"])
        except Exception:  # noqa: BLE001 — an unreadable floor row is skipped
            continue
        if k1 != k2 or k1 not in thic_surfaces:
            continue
        surfaces_with_floor.add(k1)  # a matching floor EXISTS on this THIC surface
        try:
            op.Weight = weight
            if not math.isclose(float(op.Weight), weight, rel_tol=1e-9, abs_tol=1e-12):
                continue  # a silent no-op read-back -> per-row hiccup, NOT counted
        except Exception:  # noqa: BLE001 — a per-row write hiccup is skipped (not counted)
            continue
        n_reweighted += 1
        bumped_surfaces.add(k1)

    reweighted_surfaces = sorted(bumped_surfaces)
    # A matching floor exists but NOT ONE of its weight-writes read back -> a FAILED
    # strengthening (the convergent MEDIUM silent-wrong). Precedence success > failed:
    # a surface with >=1 successful bump is `reweighted`, never `reweight_failed`.
    reweight_failed = sorted(surfaces_with_floor - bumped_surfaces)
    unfloored = sorted(thic_surfaces - surfaces_with_floor)
    return (n_reweighted, reweighted_surfaces, unfloored, reweight_failed, False)


def _per_config_thic_floor_key(weight, n_reweighted, reweighted_surfaces,
                               unfloored_surfaces, reweight_failed_surfaces, fault):
    """The additive ``per_config_thic_floor`` disclosure dict, or ``None`` (§5). NEVER raises.

    ``None`` when: ``fault``, OR (``n_reweighted==0`` AND no ``unfloored_surfaces`` AND no
    ``reweight_failed_surfaces``) — the gate ran but there was nothing to do (no THIC floor,
    no unfloored surface, no failed strengthening). Present with a non-empty
    ``unfloored_surfaces`` OR ``reweight_failed_surfaces`` even at ``n_reweighted==0`` so the
    disclosure surfaces a glass THIC the wizard did not floor (the ``glass=False`` case) OR a
    matching floor whose weight-write silently no-opped (the fix — an unstrengthened
    per-config gap is never invisible, §1 axis 0).
    """
    if fault:
        return None
    if n_reweighted == 0 and not unfloored_surfaces and not reweight_failed_surfaces:
        return None
    return {
        "weight": weight,
        "rows_reweighted": n_reweighted,
        "reweighted_surfaces": list(reweighted_surfaces),
        "unfloored_surfaces": list(unfloored_surfaces),
        "reweight_failed_surfaces": list(reweight_failed_surfaces),
    }


def build_merit(session, params):
    """Build the default RMS-spot merit via the SEQ wizard (Apply + OK).

    Drives ``wizard = mfe.SEQOptimizationWizard`` -> set the exposed flags
    (``IsGlassUsed`` / ``IsAirUsed`` / ``OverallWeight``), the thickness FLOORS, and
    the ring/arm pupil-sampling indices -> ``wizard.Apply()`` -> ``wizard.OK()`` (the
    drive sequence; ``CommonSettings()`` is NEVER called — it is a property, not
    a method). Reads back ``NumberOfOperands`` + ``CalculateMeritFunction``. A post-build
    operand count still ``<= 1`` (the placeholder did not grow) -> ``optimize_no_merit``.

    **Thickness floors (a DELIBERATE BREAKING default):** ``min_air`` (default
    **0.5**) -> ``AirMin``/``AirEdge``; ``min_glass`` (default **1.0**) ->
    ``GlassMin``/``GlassEdge``. The wizard then authors positive ``MNCA``/``MNEA`` /
    ``MNCG``/``MNEG`` bounds so an optimize cannot drive a center/edge thickness to 0 or
    negative. The old wizard-default (target-0) is restored by passing ``min_air=0`` /
    ``min_glass=0``. The 0.5 mm air / 1.0 mm glass defaults suit a ~10-500 mm system; a
    micro-optic should override. Optional ``max_air``/``max_glass`` set ``AirMax``/
    ``GlassMax`` ONLY when supplied (default leaves the wizard 1000.0).

    **Rings / arms:** ``rings``/``arms`` set the Gaussian-Quadrature pupil
    sampling density. The index is resolved by ENUMERATING ``GetRingAt``/``GetArmAt``
    (never assume ``index == count-1``); an unavailable count -> ``optimize_param``
    naming the live-valid set. Omitting them leaves the wizard default (byte-identical
    to before). They apply under Gaussian Quadrature (``PupilIntegrationMethod == 0``);
    under Rectangular Array the write is harmless (a ``note`` is returned, not a failure).

    All new params are validated (nan/inf/negative/bool/non-integral -> ``optimize_param``
    envelope); the applied floors + resolved ring/arm COUNTS are echoed in the ok envelope
    (read-back-as-proof, not "the call didn't raise").
    """
    system = session.system
    mfe = system.MFE

    # Validate ALL params BEFORE touching the wizard, converting a ToolParamError to the
    # structured ``optimize_param`` envelope (build_merit never raises into dispatch).
    try:
        glass = _bool_param(params, "glass", False)
        air = _bool_param(params, "air", True)
        overall_weight = _num_param(params, "overall_weight", None)
        min_air = _floor_param(params, "min_air", 0.5)
        min_glass = _floor_param(params, "min_glass", 1.0)
        max_air = _floor_param(params, "max_air", None)
        max_glass = _floor_param(params, "max_glass", None)
        rings = _int_count_param(params, "rings")
        arms = _int_count_param(params, "arms")
        span_configs = _bool_param(params, "span_configs", False)
        # Snapshot + preserve the hand-authored custom operand tail across a
        # wizard rebuild. Validated with the same bool idiom as glass/air/span_configs so
        # a non-bool refuses with optimize_param (mutating nothing).
        preserve_custom = _bool_param(params, "preserve_custom", False)
        # The criterion token is validated HERE (before the wizard is
        # created) so a bad token mutates NOTHING (§3.1); the Data/Type indices are
        # RESOLVED against the live wizard below.
        criterion = _criterion_param(params)
    except ToolParamError as exc:
        return _oc.error_envelope("build_merit", "optimize_param", str(exc))

    # preserve_custom (§3): when True, DELEGATE to the snapshot -> rebuild -> re-append
    # wrapper (which calls THIS body verbatim for the rebuild step). Dispatched AFTER all
    # param validation (a bad param still refuses with optimize_param above) and BEFORE
    # any wizard mutation, so the byte-compat body below is untouched when the flag is
    # absent/False (regression-guarded by test_flag_off_byte_identical).
    if preserve_custom:
        return _preserve_custom_rebuild(session, params)

    # The config count drives the span lever + INVARIANT-2 (THROW-guarded -> 1 on a
    # fresh / non-MCE system, MCE). Read BEFORE the wizard so a 1-config
    # build is byte-identical to today.
    n_configs = _ccfg.safe_number_of_configurations(system)

    wizard = mfe.SEQOptimizationWizard
    wizard.IsGlassUsed = glass
    wizard.IsAirUsed = air
    if overall_weight is not None:
        wizard.OverallWeight = overall_weight

    # Config-spanning (MCE): the ONE branch — span_configs=True sets
    # wizard.Configuration=0 (the ALL-CONFIGS spanning lever, probe Q9, live-proven) so the
    # wizard authors one CONF-bracketed operand block per config. False leaves the engine
    # default (current config) — byte-identical to today.
    if span_configs:
        wizard.Configuration = 0

    # Pin the criterion (Data) + Type DETERMINISTICALLY, ENUMERATED
    # (§3.1 write order — Type + Data BEFORE the floors + Apply). ``build_merit`` set
    # NEITHER before, inheriting the wizard's persisted last-used value (non-deterministic).
    # criterion='spot' -> Data=Spot Radius (TRAC family); 'wavefront' -> Data=Wavefront
    # (OPDX family). Type is PINNED to RMS (Reference is left at the Centroid default —
    # probe observed no drift). The index is resolved by name-enumeration, so an
    # unavailable name -> optimize_param naming the live set (never a hardcoded Data=1).
    try:
        wizard.Data = _resolve_data_index(wizard, _CRITERION_DATA_NAME[criterion])
        wizard.Type = _resolve_data_index(
            wizard, "RMS",
            count_attr="NumberOfTypes", getter_name="GetTypeAt", label="type",
        )
    except ToolParamError as exc:
        return _oc.error_envelope("build_merit", "optimize_param", str(exc))

    # Thickness floors: set BOTH the center (Min) and edge (Edge) bound for
    # each family. Set regardless of glass/air being used (harmless on the unused
    # family — only the USED family authors bound operands).
    wizard.AirMin = min_air
    wizard.AirEdge = min_air
    wizard.GlassMin = min_glass
    wizard.GlassEdge = min_glass
    if max_air is not None:
        wizard.AirMax = max_air
    if max_glass is not None:
        wizard.GlassMax = max_glass

    # Rings / arms: resolve the INDEX by enumeration, then write it. An
    # unavailable count -> optimize_param (the resolver raises ToolParamError).
    note = None
    try:
        if rings is not None:
            wizard.Ring = _resolve_count_index(
                wizard, rings,
                count_attr="NumberOfRings", getter_name="GetRingAt", label="rings",
            )
        if arms is not None:
            wizard.Arm = _resolve_count_index(
                wizard, arms,
                count_attr="NumberOfArms", getter_name="GetArmAt", label="arms",
            )
    except ToolParamError as exc:
        return _oc.error_envelope("build_merit", "optimize_param", str(exc))

    if (rings is not None or arms is not None) and int(
        getattr(wizard, "PupilIntegrationMethod", 0)
    ) != 0:
        note = (
            "rings/arms apply only under Gaussian Quadrature "
            "(PupilIntegrationMethod==0); the system is in Rectangular Array — the "
            "ring/arm write is inert"
        )

    # Apply() builds the operands; OK() commits + closes the wizard. NOT
    # CommonSettings() (a property — calling it raises TypeError).
    wizard.Apply()
    wizard.OK()

    number_of_operands = int(mfe.NumberOfOperands)
    merit = mfe.CalculateMeritFunction()
    if number_of_operands <= 1:
        return _oc.error_envelope(
            "build_merit",
            "optimize_no_merit",
            f"wizard produced no operands (NumberOfOperands={number_of_operands})",
            number_of_operands=number_of_operands,
            merit=safe_float(merit),
        )

    # Config-coverage read-back (MCE): scan the post-OK MFE's CONF rows'
    # Cfg# cells (factored into _merit_configs_covered so optimize/dry_run reuse it). On a
    # single-config (non-spanning) build NO CONF rows are authored -> covered == [].
    configs_covered = _oc._merit_configs_covered(system)
    all_configs = set(range(1, n_configs + 1))
    spans_all_configs = (
        (n_configs <= 1) or (set(configs_covered) == all_configs)
    )

    # INVARIANT-2 (the config-spanning path ONLY, §2.2): span_configs=True must cover EVERY
    # config or a merit_config_coverage REFUSAL — the merit was built but does not span as
    # claimed (a Configuration=0 that silently no-opped, or an enum drift). This is the ONE
    # new refusal; it closes the dominant HAZARD-BLIND (a false "all-config" claim).
    if span_configs and set(configs_covered) != all_configs:
        missing = sorted(all_configs - set(configs_covered))
        return _oc.error_envelope(
            "build_merit",
            "merit_config_coverage",
            f"span_configs=True but the merit covers configs {configs_covered} of "
            f"{n_configs} (missing {missing}); the wizard's Configuration=0 spanning "
            "lever did not author a CONF block for every config (a silent no-op or an "
            "enum drift) — refusing rather than shipping a merit that claims to span all "
            "configs but does not. The merit WAS built but spans only configs "
            f"{configs_covered} of {n_configs}; run clear_merit (or rebuild build_merit) "
            "before optimizing — the MFE holds a partial config-bracketed merit",
            number_of_operands=number_of_operands,
            merit=safe_float(merit),
            n_configs=n_configs,
            configs_covered=configs_covered,
            missing=missing,
            partial_state=True,
        )

    # Single-config WARN (AXIS-3 / INVARIANT-2 scope — NEVER a refusal): a
    # multi-config system handed a deliberately single-config merit gets a visible WARN.
    if not span_configs and n_configs > 1:
        current = _ccfg.safe_current_configuration(system)
        others = sorted(set(range(1, n_configs + 1)) - {current})
        warn_extra = (
            f"this merit controls only the current config (config {current}) of "
            f"{n_configs}; configs {others} are UNCONTROLLED — optimize will not improve "
            "them. Pass span_configs=true to span all configs."
        )
    else:
        warn_extra = None

    # Author one ETGT true-edge restoring floor per glass-edge surface
    # (single-config only, §5.3), BEFORE the ``result`` assembly + the boundary store so
    # ``B`` and ``sig_hash`` cover the ETGT rows (§6 REGENERATION). NEVER raises — a total
    # failure degrades to key-absent, a per-row hiccup reaps its orphan. RE-READ the count
    # + merit when ETGT touched the MFE so the stored boundary includes the ETGT rows.
    etgt_authored = etgt_glass = 0
    etgt_incomplete = []
    if glass and min_glass > 0 and n_configs == 1:
        # the helper's 4th return (``fault``) is unconsumed by design — authored==0
        # degrades to NO key on EITHER a structural fault OR an all-rows-failed pass
        # (enforced by a guard test),
        # so the distinguishing flag is dead here. Unpacked into a throwaway (no dead binding).
        etgt_authored, etgt_glass, etgt_incomplete, _etgt_fault = (
            _author_etgt_edge_floors(system, mfe, min_glass)
        )
        if etgt_authored or etgt_incomplete:
            # §5.3 RE-READ the count + merit so the RESULT ECHO reflects the ETGT rows.
            # THROW-GUARDED: a NumberOfOperands/
            # CalculateMeritFunction throw here must NEVER escape build_merit. On a throw the
            # ECHO degrades to the best-known (pre-ETGT) count/merit — the read genuinely
            # failed, so echoing the last good value is the honest fallback. This can NO
            # LONGER poison the stored boundary (sibling, CLOSED):
            # _store_wizard_boundary (below) RE-READS NumberOfOperands ITSELF, so B/sig_hash
            # always fingerprint the ACTUAL live rows (ETGT included) or the boundary is
            # INVALIDATED — a stale echo here can never mis-slice a later preserve_custom.
            try:
                number_of_operands = int(mfe.NumberOfOperands)   # ECHO: include ETGT if readable
                merit = mfe.CalculateMeritFunction()             # ECHO: honest full merit
            except Exception:  # noqa: BLE001 — §5.3 NEVER raises: keep the best-known (pre-ETGT) echo
                pass

    # REWEIGHT the wizard's own per-config THIC-surface floor rows to a HEAVY
    # weight so the per-config zoom gap is not driven negative by the image-quality operands
    # (probe, gotcha #240 — the wizard authors the floor at weight-1, DROWNED by the spot
    # merit). REWEIGHT (not greenfield author): boundary-neutral (adds no rows), material-blind
    # (strengthens whichever MNCA/MNEA/MNCG/MNEG the wizard placed on the THIC surface), and
    # regenerates for free on a preserve_custom rebuild. Gated on the span-configs multi-config
    # floored path AND NOT folded (the shared fold predicate — a heavy positive air floor on
    # a fold leg would FIGHT the designed-negative thickness). ETGT (n_configs==1) and reweight
    # (n_configs>1) are DISJOINT. NEVER raises -> key absent on a fault/fold.
    mce_thic_key = None
    if span_configs and n_configs > 1 and (min_air > 0 or min_glass > 0):
        folded = False
        try:
            from . import _layout_geometry as _geom
            folded = _geom.system_is_folded(system)
        except Exception:  # noqa: BLE001 — a fold-read hiccup -> treat as unfolded (author)
            folded = False
        if not folded:
            (_n_rw, _rw_surfs, _unfloored, _rw_failed,
             _rw_fault) = _reweight_per_config_thic_floors(
                system, mfe, _MCE_THIC_FLOOR_WEIGHT
            )
            mce_thic_key = _per_config_thic_floor_key(
                _MCE_THIC_FLOOR_WEIGHT, _n_rw, _rw_surfs, _unfloored, _rw_failed, _rw_fault
            )

    # Echo the APPLIED settings (read-back-as-proof). The resolved ring/arm COUNTS are
    # read back off the wizard index via GetRingAt/GetArmAt (proves the index landed).
    result = {
        "ok": True,
        "number_of_operands": number_of_operands,
        "merit": safe_float(merit),
        "glass": glass,
        "air": air,
        "min_air": min_air,
        "min_glass": min_glass,
        "rings": int(wizard.GetRingAt(int(wizard.Ring))),
        "arms": int(wizard.GetArmAt(int(wizard.Arm))),
        # Echo the applied criterion + the READ-BACK of the Data/Type
        # index (proves the write landed, mirrors the rings/arms GetRingAt read-back).
        "criterion": criterion,
        "criterion_data": str(wizard.GetDataTypeAt(int(wizard.Data))),
        "criterion_type": str(wizard.GetTypeAt(int(wizard.Type))),
        # MCE additive keys (byte-compatible add — a 1-config system echoes
        # span_configs:false, configs_covered:[], spans_all_configs:true).
        "span_configs": span_configs,
        "n_configs": n_configs,
        "configs_covered": configs_covered,
        "spans_all_configs": spans_all_configs,
        # §a disclosure (probe Q3a, live-proven): when span_configs=True the SEQ
        # wizard authors the MNCA/MNEA/MNCG/MNEG floor operands INSIDE every per-config CONF
        # bracket, so active floors (min_air>0 or min_glass>0) are PER-CONFIG — free from the
        # span lever, no hand-authoring. False for a single-config / floors-off build.
        "floors_per_config": bool(
            span_configs and (min_air > 0 or min_glass > 0)
        ),
    }
    if max_air is not None:
        result["max_air"] = max_air
    if max_glass is not None:
        result["max_glass"] = max_glass
    if note is not None and warn_extra is not None:
        result["note"] = f"{note}; {warn_extra}"
    elif note is not None:
        result["note"] = note
    elif warn_extra is not None:
        result["warning"] = warn_extra

    # The widened glass-floor advisory (non-blocking, additive key, never
    # flips ok). Called UNCONDITIONALLY — it reads the LIVE MFE, so the floored
    # glass=True path self-silences via the positive-target scan while an exposed glass
    # sag/thickness DOF with no matching positive floor (incl. glass=True, min_glass=0,
    # whose MNEG/MNCG author at Target 0) warns.
    floor_warning = _glass_floor_warning(system)
    if floor_warning:
        result["glass_floor_warning"] = floor_warning

    # The additive ETGT disclosure key (§7). Present on the single-config
    # authored path (>=1 ETGT authored) or the multi-config disclosure clause; ABSENT on
    # glass=False / min_glass=0 (gate off) and on a total author failure (authored==0, the
    # wizard MNEG still floors). Self-silences cleanly against ``glass_floor_warning`` (the
    # floored glass=true, min_glass>0 path silences the warning).
    etgt_key = _etgt_edge_floor_key(
        glass, min_glass, n_configs, etgt_authored, etgt_glass, etgt_incomplete
    )
    if etgt_key is not None:
        result["etgt_edge_floor"] = etgt_key

    # The additive per-config THIC-floor disclosure (§5). Present when the
    # reweight strengthened >=1 floor OR disclosed an unfloored THIC surface OR a FAILED
    # strengthening (the fix); ABSENT on the gate-off / single-config / folded /
    # total-fault paths. A non-empty unfloored/failed set also merges a non-blocking warning
    # (never a refusal, §2.1 scope).
    if mce_thic_key is not None:
        result["per_config_thic_floor"] = mce_thic_key
        unfloored = mce_thic_key.get("unfloored_surfaces")
        if unfloored:
            thic_warn = (
                f"per-config THIC surface(s) {unfloored} have no wizard floor to strengthen "
                "— pass glass=true (or min_glass>0) so a glass-gap THIC gets a per-config "
                "floor; otherwise that gap can still collapse negative"
            )
            existing = result.get("warning")
            result["warning"] = (
                f"{existing}; {thic_warn}" if existing else thic_warn
            )
        # The convergent fix: a matching floor that FAILED to strengthen (a
        # silent op.Weight no-op) is now VISIBLE — surface it so the agent knows the
        # per-config gap is UNPROTECTED (still weight-1, drowns to -50, gotcha #240).
        failed = mce_thic_key.get("reweight_failed_surfaces")
        if failed:
            failed_warn = (
                f"THIC surface(s) {failed}: the wizard floor could not be strengthened to "
                f"weight {int(_MCE_THIC_FLOOR_WEIGHT)} (the Weight write silently no-opped) "
                "— its per-config gap is UNPROTECTED and can still collapse negative"
            )
            existing = result.get("warning")
            result["warning"] = (
                f"{existing}; {failed_warn}" if existing else failed_warn
            )

    # preserve_custom (§2.2): fingerprint the wizard-ONLY block on the session so a
    # LATER build_merit(preserve_custom=True) can find + preserve the hand-authored
    # custom tail. ONE store site, at the wizard-only moment — the body NEVER re-appends
    # (the preserve wrapper does that AFTER this body returns, §3), so the MFE here is
    # ALWAYS wizard-only for both the normal build and the inner rebuild of a preserve
    # call. B is RE-READ inside _store_wizard_boundary off the LIVE MFE (NOT the
    # ``number_of_operands`` local, which a throwing §5.3 ETGT re-read may have left stale)
    # — so B/sig_hash always cover EXACTLY the live rows (ETGT included) or the boundary
    # is invalidated. Gated B>1 + fully THROW-guarded.
    _store_wizard_boundary(session, mfe, n_configs)
    return result


def _wizard_sig(mfe, count):
    """sha1 hex fingerprint of the ordered ``TypeName[1..count]`` tuple (§2.1).

    A STABLE hash (``hashlib.sha1``), NOT Python's per-process-salted ``hash()`` — so a
    boundary stored in one call is comparable in a later call. Raises on a TypeName read
    throw (the caller THROW-guards it: at store time a fingerprint failure leaves the
    prior boundary; at preserve time it refuses rather than mis-slicing).
    """
    joined = "\n".join(
        str(mfe.GetOperandAt(i).TypeName) for i in range(1, count + 1)
    )
    return hashlib.sha1(joined.encode()).hexdigest()


def _store_wizard_boundary(session, mfe, n_configs):
    """Persist ``{B, sig_hash, n_configs}`` on the session, fingerprinting the ACTUAL
    live MFE (§2.2). NEVER raises.

    ``B`` is RE-READ from ``mfe.NumberOfOperands`` HERE — never a caller-supplied local —
    so the stored boundary always covers EXACTLY the rows that currently exist, INCLUDING
    any ETGT edge-floor rows authored after the wizard build (§5.3). This closes the
    sibling: a caller whose own post-ETGT re-read threw would
    otherwise pass a STALE pre-ETGT count, storing ``B`` too LOW with a ``sig_hash`` over
    only the unchanged ``[1..pre-ETGT]`` prefix — a later ``preserve_custom`` would then
    PASS its staleness check (that prefix is unchanged) and MIS-SLICE the real
    wizard-authored ETGT rows into the "custom" tail, silently DUPLICATING the floors on
    the inner rebuild's re-append.

    Gated on the LIVE ``B > 1`` (a degenerate 1-row MFE has no meaningful wizard
    boundary). On ANY read failure — the ``NumberOfOperands`` read throws, ``_wizard_sig``
    throws, or a degenerate ``B <= 1`` — the boundary is INVALIDATED (deleted, NOT left
    stale), so a later ``preserve_custom`` hits the no-boundary REFUSE path rather than
    mis-slicing; never a boundary that is too low with a clean-hashing stale prefix.
    (Leaving a stale PRIOR boundary is unsafe: an earlier build's ``{B0,sig0}`` whose
    ``[1..B0]`` prefix still hashes clean against the new wizard would itself mis-slice —
    so a failure INVALIDATES, it does not fall back.) Uses the getattr/setattr
    optional-persistence idiom (``session.workspace_root`` at session.py) — no
    ``__init__`` change.
    """
    try:
        number_of_operands = int(mfe.NumberOfOperands)   # RE-READ off the LIVE MFE (never a stale local)
        if number_of_operands <= 1:
            _invalidate_wizard_boundary(session)         # degenerate live MFE -> no boundary
            return
        sig = _wizard_sig(mfe, number_of_operands)       # fingerprint [1..fresh B], ETGT included
    except Exception:  # noqa: BLE001 — a live block we cannot count/fingerprint -> INVALIDATE, never a stale boundary
        _invalidate_wizard_boundary(session)
        return
    session._merit_wizard_boundary = {
        "B": number_of_operands,
        "sig_hash": sig,
        "n_configs": n_configs,
    }


def _invalidate_wizard_boundary(session):
    """Drop any stored wizard boundary (§2.2, defensive). NEVER raises.

    A later ``preserve_custom`` then hits the no-boundary REFUSE path rather than trusting
    a stale/uncertain boundary that could mis-slice real wizard rows into the custom tail.
    """
    if hasattr(session, "_merit_wizard_boundary"):
        try:
            delattr(session, "_merit_wizard_boundary")
        except Exception:  # noqa: BLE001 — best-effort invalidation; never raise
            pass


def _stale_refuse(stored_B, n_live):
    """The §2.4 staleness REFUSE envelope (row count OR row-type layout diverged)."""
    return _oc.error_envelope(
        "build_merit", "merit_preserve_custom",
        "the wizard block changed since it was built (row count "
        f"{n_live} vs stored B {stored_B}, or the row-type layout differs) — the "
        "stored boundary can no longer safely separate wizard from custom rows. Re-run "
        "build_merit WITHOUT preserve_custom to rebuild from scratch then re-add your "
        "custom suite, or serialize_merit the whole merit first to keep everything.",
        stale_boundary=True, stored_B=stored_B, live_operands=n_live,
        preserve_custom=True,
    )


def _first_cross_boundary_ref(tail_entries, W):
    """The FIRST tail ref that targets a WIZARD-block operand, or ``None`` (§4).

    Pure recipe-space scan: a tail entry at offset ``o`` has absolute recipe index
    ``W + o``; any of its ``refs`` values ``< W`` points into the wizard block (which the
    rebuild destroys), so it cannot be rebased into the tail-local recipe. Returns
    ``(offending_recipe_row, header, target_index)`` on the first such ref. A ``refs``
    value ``>= W`` is intra-tail (rebased to ``target-W >= 0`` by ``_rebased_tail_recipe``
    — the proven DIFF->REAY case). Bools are excluded (an index is an exact int).
    """
    for offset, entry in enumerate(tail_entries):
        refs = entry.get("refs") or {}
        for header, target_idx in refs.items():
            if (isinstance(target_idx, int) and not isinstance(target_idx, bool)
                    and target_idx < W):
                return (W + offset, header, target_idx)
    return None


def _rebased_tail_recipe(full_recipe, tail_entries, W):
    """Copy the sliced tail, rebasing every intra-tail ref index by ``-W`` (§3.1 step 2d).

    Each tail entry is shallow-copied and its ``refs`` map (absolute recipe indices, all
    ``>= W`` after the cross-boundary scan) is rebased to tail-local 0-based indices, so
    ``apply_merit_recipe(mode='append')``'s Op#-remap (which builds ``index_to_live`` over
    ONLY the appended tail) wires them to the correct re-appended rows. Non-ref keys
    (``type``/``params``/``target``/``weight``/``value_at_capture``) ride through
    unchanged. Preserves the recipe ``schema``/``version`` so it re-applies cleanly.
    """
    operands = []
    for entry in tail_entries:
        new_entry = dict(entry)
        refs = entry.get("refs")
        if refs:
            new_entry["refs"] = {h: idx - W for h, idx in refs.items()}
        operands.append(new_entry)
    return {
        "schema": full_recipe.get("schema"),
        "version": full_recipe.get("version"),
        "operands": operands,
    }


def _enrich_first_use(build_result):
    """The first-use disclosure (§4): a normal build + preserve keys, K=0, NOT an error."""
    result = dict(build_result)
    result["preserve_custom"] = True
    result["preserved_custom_rows"] = 0
    result["custom_tail_reappended"] = False
    result["wizard_boundary_before"] = None
    result["wizard_boundary_after"] = int(build_result.get("number_of_operands"))
    result["preserve_note"] = (
        "no prior build boundary — nothing to preserve; behaving as a normal build"
    )
    return result


def _enrich_empty_tail(build_result, stored_B):
    """The empty-tail no-op disclosure (§4): boundary valid but no custom rows beyond it."""
    result = dict(build_result)
    result["preserve_custom"] = True
    result["preserved_custom_rows"] = 0
    result["custom_tail_reappended"] = False
    result["wizard_boundary_before"] = stored_B
    result["wizard_boundary_after"] = int(build_result.get("number_of_operands"))
    result["preserve_note"] = (
        "no custom rows beyond the wizard block — nothing to preserve; the wizard was "
        "rebuilt normally"
    )
    return result


def _enrich_success(build_result, apply_result, mfe, tail_recipe, stored_B,
                    wizard_boundary_after):
    """The success envelope (§6): the build ok payload + the additive preserve keys.

    ``number_of_operands`` is overwritten with the POST re-append total (wizard + custom)
    and ``merit`` is RE-READ post-append (§3.3, THROW-guarded) so the envelope reflects
    the FULL restored merit, not the wizard-only merit the body read.
    """
    result = dict(build_result)
    k = len(tail_recipe["operands"])
    total = apply_result.get("number_of_operands")
    if total is not None:
        result["number_of_operands"] = int(total)
    try:
        result["merit"] = safe_float(mfe.CalculateMeritFunction())
    except Exception:  # noqa: BLE001 — leave the wizard-only merit (non-load-bearing, §3.3)
        if apply_result.get("merit") is not None:
            result["merit"] = apply_result.get("merit")
    result["preserve_custom"] = True
    result["preserved_custom_rows"] = k
    result["custom_tail_reappended"] = True
    result["wizard_boundary_before"] = stored_B
    result["wizard_boundary_after"] = wizard_boundary_after
    result["preserve_note"] = (
        f"wizard rebuilt ({stored_B}->{wizard_boundary_after} ops); {k} custom row(s) "
        "re-appended (CONF brackets, Cfg# + intra-tail Op# refs intact)."
    )
    return result


def _restore_full_merit(mfe, checkpoint_path):
    """LoadMeritFunction(ckpt) restore of the ENTIRE pre-preserve merit. NEVER raises.

    Returns ``True`` on a clean restore, ``False`` if the LOAD itself throws (the
    caller surfaces the honest flag).
    """
    try:
        mfe.LoadMeritFunction(checkpoint_path)
        return True
    except Exception:  # noqa: BLE001 — a restore throw is surfaced as restored_full_merit:false
        return False


def _restore_boundary(session, boundary0):
    """Restore the pre-preserve stored boundary ``{B0,sig0}`` to the session (FIX 2).

    A SUCCESSFUL inner rebuild's store site (§2.2) overwrote
    ``session._merit_wizard_boundary`` to the NEW ``{B1,sig1}``. When recovery restores
    the old merit byte-faithfully, its boundary MUST be restored too — otherwise the NEXT
    preserve slices with the wrong ``B``, and worse, a smaller ``B1`` whose ``TypeName``
    tuple is a PREFIX of the old block PASSES the sig check and silently absorbs old wizard
    rows into the "custom" tail. ``boundary0`` is the ``{B0,sig0,n_configs}`` dict captured
    BEFORE the rebuild (never ``None`` on the recovery paths — a no-boundary preserve
    returns before the rebuild); the ``None`` branch is defense-in-depth (a first-use
    preserve had nothing to hold, so leave no stale ``{B1,sig1}``).
    """
    if boundary0 is None:
        if hasattr(session, "_merit_wizard_boundary"):
            delattr(session, "_merit_wizard_boundary")
    else:
        session._merit_wizard_boundary = boundary0


def _recover_preserve(mfe, checkpoint_path, tail_recipe, session, boundary0, reason):
    """§3.2 + FIX 1/2 recovery: ONE routine that restores the FULL pre-preserve merit AND
    the stored boundary ``{B0,sig0}``. NEVER raises.

    Shared by the append-fail path AND the FIX-1 rebuild/re-append THROW path (a raw .NET
    throw from the unguarded wizard ``Apply``/``OK``/``CalculateMeritFunction`` or the
    re-append). Steps: (a) ``LoadMeritFunction(ckpt)`` restores the entire pre-preserve
    merit (old wizard + tail) byte-faithfully; (b) restore ``{B0,sig0}`` (FIX 2 — the old
    merit is restored byte-faithfully, so its boundary must be too); (c) return
    ``rolled_back:true``; (d) if the restore ITSELF throws -> FAIL-CLOSED
    ``partial_state:true`` + a "reload your .zmx" message (never re-raise). ``reason`` is
    the human clause naming what failed (append error vs rebuild throw). Either way
    ``preserved_custom_recipe`` (the tail, held in-hand) is returned so the caller can
    re-apply it.
    """
    try:
        mfe.LoadMeritFunction(checkpoint_path)
    except Exception as exc:  # noqa: BLE001 — the restore itself threw -> partial state
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            f"the wizard was rebuilt but {reason}; ROLLBACK FAILED — the restore itself "
            f"threw ({exc!r}), so the MFE is in a PARTIAL state. Reload your design .zmx, "
            "then re-apply preserved_custom_recipe via apply_merit_recipe(mode='append').",
            rolled_back=False, partial_state=True,
            preserved_custom_recipe=tail_recipe, preserve_custom=True,
        )
    # The old merit is back byte-faithfully — restore its boundary too (FIX 2).
    _restore_boundary(session, boundary0)
    return _oc.error_envelope(
        "build_merit", "merit_preserve_custom",
        f"the wizard was rebuilt but {reason}; the merit was RESTORED to its pre-preserve "
        "state — your custom rows are intact and the rebuild did NOT take effect. The "
        "custom suite is also returned as preserved_custom_recipe.",
        rolled_back=True, restored_full_merit=True,
        preserved_custom_recipe=tail_recipe, preserve_custom=True,
    )


def _preserve_custom_rebuild(session, params):
    """Snapshot the custom tail, rebuild the wizard, re-append the tail — atomically (§3).

    The ``preserve_custom=True`` orchestrator. Flow (snapshot-FIRST,
    recover-on-any-post-rebuild-failure): staleness check (§2.4) -> serialize the FULL
    merit + derive the recipe-space wizard boundary ``W`` (§2.1) + slice the tail ->
    cross-boundary ref scan (§4) -> rebase intra-tail refs -> full-merit ``.MF``
    checkpoint -> DESTRUCTIVE wizard rebuild (the unmodified build body, which stores the
    NEW boundary) -> ``apply_merit_recipe(mode='append')`` the tail. On append-fail the
    OUTER full-merit checkpoint restores the ENTIRE pre-preserve merit (old wizard + tail)
    — apply's OWN inner rollback keeps the new wizard but LOSES the tail, so the outer
    checkpoint is load-bearing (§3.2). The handler NEVER raises.
    """
    # Lazy import: the recipe layer lives in optimize_merit_io, which does NOT import this
    # module — a top-level import would be acyclic, but the lazy form matches the
    # established _unlink_quiet usage in place_element/reflective/zoom_compose and is
    # cycle-proof regardless of future import-graph churn.
    from . import optimize_merit_io as _mrio

    system = session.system
    mfe = system.MFE

    stored = getattr(session, "_merit_wizard_boundary", None)

    # The live operand count is a raw .NET read -> a THROW refuses (nothing mutated, §2.4).
    try:
        n_live = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — a count read throw -> refuse, nothing mutated
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            f"could not read the live operand count before preserving ({exc!r}); "
            "refusing rather than mutating the merit.",
            preserve_custom=True,
        )

    # The inner rebuild runs the build body verbatim (preserve_custom stripped so it
    # cannot re-enter this wrapper; the body's :store site fingerprints the new wizard).
    inner_params = {k: v for k, v in params.items() if k != "preserve_custom"}

    # ---- §4 no-boundary handling: first-use (empty) vs non-empty (refuse). ----
    if stored is None:
        if n_live <= 1:
            build_result = build_merit(session, inner_params)
            if not build_result.get("ok", False):
                return build_result
            return _enrich_first_use(build_result)
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            "preserve_custom=True but there is no stored wizard-block boundary and the "
            f"merit is non-empty ({n_live} operands) — the custom rows cannot be "
            "identified without a boundary. Run build_merit once WITHOUT preserve_custom "
            "to store the boundary then preserve next time, or serialize_merit the whole "
            "merit to keep everything.",
            no_boundary=True, live_operands=n_live, preserve_custom=True,
        )

    stored_B = int(stored.get("B"))
    stored_sig = stored.get("sig_hash")

    # ---- Staleness check (§2.4): cheap count precheck, then the sha1 fingerprint. ----
    if n_live < stored_B:
        return _stale_refuse(stored_B, n_live)
    try:
        live_sig = _wizard_sig(mfe, stored_B)
    except Exception as exc:  # noqa: BLE001 — cannot fingerprint the live block -> refuse
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            f"could not fingerprint the live wizard block [1..{stored_B}] ({exc!r}); "
            "refusing rather than mis-slicing the merit.",
            stale_boundary=True, stored_B=stored_B, live_operands=n_live,
            preserve_custom=True,
        )
    if live_sig != stored_sig:
        return _stale_refuse(stored_B, n_live)

    # ---- SNAPSHOT (zero mutation): serialize the FULL merit + derive W + slice. ----
    full_env, live_to_index = _mrio._serialize_with_rowmap(session, {})
    if not full_env.get("ok", False):
        env = dict(full_env)
        env["preserve_aborted"] = True  # passthrough serialize family, nothing mutated
        return env

    # W = the recipe-space wizard boundary, DERIVED FRESH from the same serialize's
    # row-map (§2.1) — |{serialized live rows <= B}|. Never a stored value.
    W = sum(1 for row in live_to_index if row <= stored_B)
    all_operands = full_env["recipe"]["operands"]
    tail_entries = all_operands[W:]

    # ---- Empty tail (N == B): rebuild normally, no checkpoint / append (§4). ----
    if not tail_entries:
        build_result = build_merit(session, inner_params)
        if not build_result.get("ok", False):
            return build_result
        return _enrich_empty_tail(build_result, stored_B)

    # ---- Cross-boundary ref scan BEFORE the checkpoint (§4): MFE untouched on refuse. ----
    cross = _first_cross_boundary_ref(tail_entries, W)
    if cross is not None:
        offending_row, header, target_idx = cross
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            f"custom tail row (recipe index {offending_row}, type "
            f"{tail_entries[offending_row - W]['type']!r}) references a WIZARD-block "
            f"operand via {header!r} (recipe index {target_idx} < wizard boundary "
            f"W={W}); a cross-boundary reference cannot survive a wizard rebuild — "
            "refusing rather than shipping a dangling reference. Re-point it at a "
            "custom-tail row, or serialize_merit the whole merit to keep everything.",
            cross_boundary_ref=True, offending_row=offending_row,
            offending_ref_header=header, wizard_target_index=target_idx,
            preserve_custom=True,
        )

    # ---- Rebase intra-tail refs by -W (tail-local 0-based). ----
    tail_recipe = _rebased_tail_recipe(full_env["recipe"], tail_entries, W)

    # ---- FULL-MERIT CHECKPOINT (fail-closed, §2e): a SAVE throw refuses pre-rebuild. ----
    from . import _merit_io as _mio

    checkpoint_path = None
    try:
        fd, checkpoint_path = tempfile.mkstemp(
            suffix=".MF", prefix="optivibe_preserve_"
        )
        os.close(fd)
        mfe.SaveMeritFunction(checkpoint_path)
    except Exception as exc:  # noqa: BLE001 — checkpoint save throw -> fail-closed, nothing mutated
        _mrio._unlink_quiet(checkpoint_path)
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            f"could not checkpoint the full merit before rebuilding ({exc!r}); nothing "
            "was mutated — the merit is unchanged.",
            checkpoint=False, preserve_custom=True,
        )

    # ---- FIX 3 (durability, gotcha #58): VERIFY the checkpoint before trusting it. ----
    # SaveMeritFunction can SILENTLY no-op (a missing-dir / headless-text trap) leaving a
    # 0-byte or non-merit .MF — trusting it would mean the later restore LOSES the tail.
    # Read it back through the SAME magic-byte oracle load_merit uses (_is_merit_file:
    # exists + non-empty + UTF-16LE BOM + "VERS") BEFORE the destructive rebuild; a bad
    # checkpoint REFUSES with the merit INTACT (nothing destroyed yet, no rebuild, no
    # unlink surprise). Closes the corruption window at the unit level (the §8 live gate
    # is the full round-trip proof).
    if not _mio._is_merit_file(checkpoint_path):
        try:
            size = os.path.getsize(checkpoint_path) if os.path.isfile(checkpoint_path) else 0
        except OSError:
            size = 0
        _mrio._unlink_quiet(checkpoint_path)
        return _oc.error_envelope(
            "build_merit", "merit_preserve_custom",
            "the full-merit checkpoint did not verify after SaveMeritFunction "
            f"(size={size}, magic-byte gate failed); refusing to rebuild the wizard when "
            "the restore could not recover your custom tail — nothing was mutated, the "
            "merit is unchanged.",
            checkpoint_unverified=True, checkpoint=False, preserve_custom=True,
        )

    try:
        try:
            # ---- REBUILD (DESTRUCTIVE): the wizard REPLACES the whole MFE; body stores B1. ----
            build_result = build_merit(session, inner_params)
            if not build_result.get("ok", False):
                # A degenerate / refused rebuild (an ENVELOPE, not a throw): RESTORE the
                # full pre-preserve merit (never leave a partial wizard merit) + the stored
                # boundary + surface the rebuild error. The errored body never reached its
                # store site, so the boundary is still {B0,sig0} — the restore is a
                # belt-and-braces no-op here (FIX 2), load-bearing on the append-fail path.
                restored = _restore_full_merit(mfe, checkpoint_path)
                if restored:
                    _restore_boundary(session, stored)
                env = dict(build_result)
                env["restored_full_merit"] = restored
                env["preserve_custom"] = True
                return env

            wizard_boundary_after = int(build_result.get("number_of_operands"))

            # ---- RE-APPEND the custom tail (atomic — apply's own inner rollback + this). ----
            apply_result = _mrio.apply_merit_recipe(
                session, {"recipe": tail_recipe, "mode": "append", "atomic": True}
            )
            if apply_result.get("ok", False):
                return _enrich_success(
                    build_result, apply_result, mfe, tail_recipe,
                    stored_B, wizard_boundary_after,
                )
            # ---- RE-APPEND failed (an ENVELOPE): recover the pre-preserve merit (§3.2). ----
            return _recover_preserve(
                mfe, checkpoint_path, tail_recipe, session, stored,
                f"re-appending your custom suite failed ({apply_result.get('error')})",
            )
        except Exception as exc:  # noqa: BLE001 — FIX 1: ANY raw .NET throw in the rebuild/re-append
            # The destructive rebuild runs the build body's UNGUARDED wizard
            # Apply()/OK()/CalculateMeritFunction() (only ToolParamError is caught there),
            # and the re-append touches the engine too — a raw .NET throw would otherwise
            # escape this except-less try, run only the finally (unlink), and leave a bare
            # rebuilt wizard with the custom tail LOST while the handler RAISES. Catch
            # GENERIC Exception (raw .NET throws are not a structured subclass; NOT
            # BaseException) and run the SAME full-merit + boundary recovery as the
            # append-fail path — NEVER re-raise (§3.2 'the handler NEVER raises').
            return _recover_preserve(
                mfe, checkpoint_path, tail_recipe, session, stored,
                f"the wizard rebuild/re-append raised ({exc!r})",
            )
    finally:
        _mrio._unlink_quiet(checkpoint_path)


def add_operand(session, params):
    """Add one merit operand with a direct Target/Weight write + read-back proof.

    Validates the ``operand`` token against the LIVE ``MeritOperandType`` enum
    BEFORE ``ChangeType`` (an unknown token -> ``optimize_param`` envelope, never
    passed through). Then ``op = mfe.AddOperand()`` -> ``op.ChangeType(<member>)``
    (``False`` -> ``optimize_param`` envelope) -> set ``op.Target`` / ``op.Weight``
    DIRECT. Target/Weight are read back off the row and verified through
    the ``_lens_common`` firewall (a silent-no-op write -> ``SurfaceWriteError``).

    OPTIONAL ``params`` dict (additive, §3): omitting it is BYTE-IDENTICAL to
    the original behavior (callers + the optimize loop untouched). With it, the
    freshly-typed operand's parameter cells are set type-aware via ``_merit_cells``
    (``Surf``/``Wave``/``Hx``/... keyed off the LIVE ``cell.Header``, read-back
    proven). All param NAMES are validated against the operand's live signature
    BEFORE any cell write (an unknown name -> ``merit_param`` envelope, NO mutation);
    a wrong-type param value (incl. the bool-is-int trap) -> ``merit_param``; a
    silent cell no-op / layout mismatch -> ``surface_write`` (raised). The param
    write happens AFTER ``ChangeType`` and BEFORE the Target/Weight write.
    """
    system = session.system
    mfe = system.MFE

    operand = params.get("operand")
    if not isinstance(operand, str) or operand == "":
        raise ToolParamError(f"operand must be a non-empty string, got {operand!r}")

    target = _num_param(params, "target", 0.0)
    weight = _num_param(params, "weight", 1.0)
    cell_params = params.get("params")  # OPTIONAL — None = byte-identical old path

    # ---- NEW: validate config=k PRE-mutation (E0 — a bad config opens NOTHING).
    #      ``config`` absent (None) is BYTE-IDENTICAL to the old path. ----
    config = params.get("config")
    cfg = None
    if config is not None:
        cfg, cfg_err = _require_config_number(system, config)
        if cfg_err is not None:
            return cfg_err                              # zero mutation

    enum_type = _oc._merit_operand_enum(system)
    try:
        member = _resolve_enum(enum_type, operand)
    except ToolParamError as exc:
        # An unknown operand is an EXPECTED failure -> structured envelope
        # (§a: do not raise into dispatch; pre-empt with a clean ok:false).
        # NOTE: the enum check is BEFORE any AddOperand on BOTH paths, so an unknown
        # operand under config=k authors NO CONF (E2: zero mutation).
        return _oc.error_envelope(
            "add_operand", "optimize_param", str(exc), operand=operand
        )

    # LIVE bug 2: capture the operand count BEFORE AddOperand so a
    # failure-path cleanup can restore the EXACT pre-add baseline — on a multi-config MFE
    # the first AddOperand auto-seeds a leading CONF too (count grows by 2), and
    # ``_remove_orphan`` reaps that auto-seed back to ``count_before``.
    #
    # §1.2: this is the SINGLE ``count_before`` capture, moved EARLIER — BEFORE the
    # CONF author — so a stranded CONF + the auto-seed both reap to this ONE baseline on
    # any operand failure. When ``config`` is absent the capture lands in the SAME logical
    # spot as before (immediately before ``op = mfe.AddOperand()``), so the non-config=
    # path is byte-identical.
    count_before = int(mfe.NumberOfOperands)

    # §1.4: author the per-config CONF k row FIRST (only when config=k). On a
    # CONF author failure the bracket is already reaped to count_before by the helper.
    if cfg is not None:
        conf_err = _author_config_bracket(session, mfe, cfg, count_before)
        if conf_err is not None:
            return conf_err

    # Fix: capture the new row number IMMEDIATELY after AddOperand+ChangeType
    # and BEFORE any later mutation, so a failure-path cleanup removes the RIGHT row.
    # (OperandNumber is read here, before apply_params / Target / Weight touch the MFE,
    # so no subsequent mutation can shift the captured index.)
    #
    # §1.6: the operand author is GUARDED ONLY on the config=k path so a RAW .NET throw
    # on AddOperand/ChangeType there reaps the already-authored CONF to count_before and
    # returns ``surface_write``, never ``internal``. The non-config= path is byte-identical
    # (no broad guard added — a raw throw there escapes exactly as today, a pre-existing
    # condition out of scope).
    op = None
    try:
        op = mfe.AddOperand()
        changed = op.ChangeType(member)
    except Exception as exc:  # noqa: BLE001 — never-raise; reap the stranded CONF
        if cfg is not None:
            # A raw throw on AddOperand (op is None) leaves only the CONF (+ auto-seed);
            # a throw on ChangeType (op appended) ALSO leaves a fresh untyped operand
            # orphan at the TOP — remove it by number first (guarded), then reap the CONF
            # + auto-seed to the exact baseline (the operand orphan carries no real
            # content yet, so this restores the pre-call MFE).
            if op is not None:
                try:
                    mfe.RemoveOperandAt(int(op.OperandNumber))
                except Exception:  # noqa: BLE001 — best-effort; the CONF reap follows
                    pass
            _reap_config_bracket_to(mfe, count_before)
            return _oc.error_envelope(
                "add_operand", "surface_write",
                f"engine threw authoring operand {operand} under config={cfg} "
                f"({exc!r}); the per-config bracket was rolled back", operand=operand,
            )
        raise          # non-config= path: byte-identical to today (no broad guard added)
    if not bool(changed):
        # The row was already added + (attempted) typed; remove the orphan before
        # returning so a ChangeType==False leaves the MFE unchanged (transactional).
        _remove_orphan(mfe, op, count_before=count_before, reap_bracket=cfg is not None)
        return _oc.error_envelope(
            "add_operand",
            "optimize_param",
            f"engine rejected operand type {operand}",
            operand=operand,
        )

    operand_number = int(op.OperandNumber)

    # Math-scaffold light guard (§6): a live ``Op#`` ROW REFERENCE must
    # point at a row that can exist. ``add_operand`` authors with RAW live rows (NO
    # remap — remap is a recipe-only concern), so an ``Op#`` cell is just an int
    # cell ``apply_params`` writes; the ONE hardening is to refuse a PROVABLY dangling
    # live reference (a row ``< 0`` or ``> NumberOfOperands``). The engine TOLERATES
    # any row number and a dangling ref ships silently as 0.0 (the LIVE twin of the
    # silent-wrong hazard) — so reject it pre-Target/Weight + remove the orphan.
    # ``0`` is allowed (the unset sentinel). NO forward/self policy here: at live
    # authoring time the agent controls evaluation order (forward/self is a frozen-
    # recipe / Phase-C concern); we refuse ONLY the row-cannot-exist case.
    if cell_params is not None and isinstance(cell_params, dict):
        try:
            live_sig = _mc.read_param_map(op)
        except SurfaceWriteError:
            # A live-signature read firewall fault leaves the orphan -> reap + re-raise.
            _remove_orphan(mfe, op, operand_number, count_before=count_before, reap_bracket=cfg is not None)
            raise
        n_operands = int(mfe.NumberOfOperands)
        for header, value in cell_params.items():
            info = live_sig.get(header)
            if info is None or not _mc.is_row_ref_header(header, info["kind"]):
                continue  # a non-ref / unknown name is handled by apply_params below
            # Fix (§6): range-check the SAME effective integer the WRITER stores.
            # ``coerce_param_value`` accepts an integral float (``999.0`` -> ``int(999)``)
            # into an int cell, so a bare ``isinstance(value, int)`` skip let an integral
            # float bypass the range check and write a provably dangling raw row (the
            # live twin). ``_mc.row_ref_int`` returns the exact int for an int OR an
            # integral float (rejecting bool), and ``None`` for a non-integral float /
            # non-number — which is the param-class trap ``apply_params`` rejects below,
            # so we leave it to that rather than double-handling here.
            effective_row = _mc.row_ref_int(value)
            if effective_row is None:
                continue
            value = effective_row
            if value != 0 and (value < 0 or value > n_operands):
                _remove_orphan(mfe, op, operand_number, count_before=count_before, reap_bracket=cfg is not None)
                return _oc.error_envelope(
                    "add_operand",
                    "merit_param",
                    f"operand {operand} param {header!r} references row {value} but "
                    f"the merit has only {n_operands} operands; an out-of-range row "
                    "reference would read 0.0 silently",
                    operand=operand,
                )

    # A value-less CONTROL operand (CONF) reads a NON-FINITE
    # Target/Weight on a fresh author (the inf sentinel, probe PART A) — the numeric
    # read-back guard at the Target/Weight write below is SKIPPED and the proof is
    # REDIRECTED to its semantic cell (Cfg#). Validate the proof param PRE-mutation
    # (presence + 1..NumberOfConfigurations) and refuse LOUD on a missing/bad/out-of-range
    # config number (no silent hole — the redirected read-back IS the Cfg# cell). The
    # Cfg# CELL itself is authored + read-back-proven by apply_params below (it is just an
    # int cell in ``params``); this only adds the range firewall + the Target/Weight skip.
    is_valueless = _mc.is_valueless_control(operand)
    if is_valueless:
        n_configs = _ccfg.safe_number_of_configurations(system)
        verr = _mc.validate_valueless_control(operand, cell_params, n_configs=n_configs)
        if verr is not None:
            _remove_orphan(mfe, op, operand_number, count_before=count_before, reap_bracket=cfg is not None)
            return _oc.error_envelope(
                "add_operand", "merit_param", verr, operand=operand
            )

    # Transactional core: the params-apply + Target/Weight + read-back all run
    # AFTER the row was created + typed. On ANY failure path that returns/raises an
    # error, REMOVE the orphan row BEFORE returning/re-raising, so a failed add leaves
    # the MFE unchanged (mirrors the recipe layer's atomic discipline). DEFERRED:
    # ``apply_params`` only raises ``ParamCoercionError`` + ``SurfaceWriteError`` today
    # (verified against the apply_params contract); the ``except ParamCoercionError``
    # below is exhaustive for the current contract — not broadened this round.
    try:
        # NEW (§3): set the parameter cells type-aware, validated + read-back
        # proven. A bad param NAME or a wrong-type VALUE is a param-class failure
        # (``merit_param`` envelope, no further mutation); a silent cell no-op / layout
        # mismatch RAISES SurfaceWriteError/CellLayoutError -> ``surface_write``.
        written_params = {}
        if cell_params is not None:
            try:
                written_params = _mc.apply_params(
                    op, cell_params, operand_token=operand
                )
            except _mc.ParamCoercionError as exc:
                # Param-class reject AFTER the typed-row creation: remove the orphan
                # so the MFE is unchanged, THEN return the merit_param envelope.
                _remove_orphan(mfe, op, operand_number, count_before=count_before, reap_bracket=cfg is not None)
                # ENRICH the message when an MTF-family operand was handed
                # a pupil-coordinate field name (Hx/Hy/Px/Py). An MTF operand selects
                # the field by an INTEGER ``Field`` index (slot 4), NOT Hx/Hy — and an
                # Hx/Hy value would SILENTLY read the on-axis field. apply_params already
                # refused the unknown name (MTFT's live signature has no Hx/Hy); the
                # enrichment just steers the agent to the right param.
                miswrite = _mtf_field_miswrite_name(operand, exc)
                message = str(exc)
                if miswrite is not None:
                    message = (
                        f"{message}. Operand {operand} selects the FIELD by an integer "
                        "'Field' index (1-based), NOT by "
                        f"{miswrite} (a pupil coordinate) — pass "
                        "params={'Field':<n>,'Freq':<cyc/mm>}; an Hx/Hy value would "
                        "silently read the on-axis field"
                    )
                return _oc.error_envelope(
                    "add_operand", "merit_param", message, operand=operand
                )

        if is_valueless:
            # Value-less control operand: do NOT write/verify Target/Weight — a fresh CONF
            # reads inf, so the numeric guard would FALSE-REJECT. The proof is the Cfg#
            # cell, authored + read-back-proven by apply_params above (and Cfg# was
            # range-validated pre-mutation in 2.1). NO Target/Weight is authored.
            actual_target = None
            actual_weight = None
        else:
            op.Target = target
            op.Weight = weight

            # Read-back-as-proof off the DIRECT properties (the silent-no-op canary). A
            # mismatch / read THROW raises SurfaceWriteError.
            actual_target = float(op.Target)
            actual_weight = float(op.Weight)
            _lc._verify_or_raise("target", target, actual_target, surface=None)
            _lc._verify_or_raise("weight", weight, actual_weight, surface=None)
    except SurfaceWriteError:
        # A cell-write firewall failure (silent no-op / layout mismatch / Target/Weight
        # read-back mismatch) after the row was created: remove the orphan BEFORE
        # re-raising so dispatch's ``surface_write`` envelope leaves the MFE unchanged.
        _remove_orphan(mfe, op, operand_number, count_before=count_before, reap_bracket=cfg is not None)
        raise

    result = {
        "ok": True,
        "operand": operand,
        "operand_number": operand_number,
        "number_of_operands": int(mfe.NumberOfOperands),
    }
    # For an MTF-family operand, READ BACK the live ``Field`` cell and
    # disclose it (``mtf_field``) so the agent always SEES the field the operand reads.
    # A ``Field`` left at 0 reads the ON-AXIS field SILENTLY — a legitimate on-axis MTF
    # constraint, so this WARNS (a ``flags`` entry mirroring get_operand's density-unset
    # flag), it does NOT refuse (ok stays True). The read is best-effort (a degraded read
    # -> no disclosure, the author already succeeded).
    if _is_mtf_family(operand):
        mtf_field = _read_mtf_field_cell(op)
        if mtf_field is not None:
            result["mtf_field"] = mtf_field
            if mtf_field == 0:
                result.setdefault("flags", []).append(
                    f"operand {operand} authored with Field=0 (on-axis): an MTF operand "
                    "with Field unset reads the ON-AXIS field — pass "
                    "params={'Field':<1-based index>} to constrain a specific (corner) "
                    "field"
                )
    # §1.9: additive echo when a per-config CONF bracket was authored. Absent on
    # the non-config= path (byte-identical envelope).
    if cfg is not None:
        result["config"] = cfg
        result["config_bracket_authored"] = True
    if is_valueless:
        # A value-less control operand has NO numeric Target/Weight; disclose the
        # redirected proof instead of a misleading 0.0/1.0.
        result["control_operand"] = True
        result["target"] = None
        result["weight"] = None
    else:
        result["target"] = safe_float(actual_target)
        result["weight"] = safe_float(actual_weight)
    if cell_params is not None:
        result["params"] = written_params
    return result


def dump_merit_function(session, params):
    """NON-MUTATING read of the full MFE (every operand row + the merit).

    Iterates ``1..NumberOfOperands``, ``op = mfe.GetOperandAt(i)``, and reads
    ``Type`` (via ``TypeName``) / ``Target`` / ``Weight`` / ``Value`` /
    ``Contribution`` (all read-only; ``Value`` / ``Contribution`` / ``Target`` /
    ``Weight`` through ``safe_float``). A 0-operand MFE returns ``operands: []``.
    NEVER mutates the merit function.
    """
    system = session.system
    mfe = system.MFE
    number_of_operands = int(mfe.NumberOfOperands)

    operands = []
    for i in range(1, number_of_operands + 1):
        op = mfe.GetOperandAt(i)
        operands.append(
            {
                "number": i,
                "type": str(op.TypeName),
                "target": safe_float(op.Target),
                "weight": safe_float(op.Weight),
                "value": safe_float(op.Value),
                "contribution": safe_float(op.Contribution),
            }
        )

    return {
        "ok": True,
        "number_of_operands": number_of_operands,
        "merit": safe_float(mfe.CalculateMeritFunction()),
        "operands": operands,
    }


def _safe_read_float(op, attr):
    """Read ``op.<attr>`` as a float, or ``None`` on any throw (the pre-edit snapshot)."""
    try:
        return float(getattr(op, attr))
    except Exception:  # noqa: BLE001 — a degraded read -> no snapshot (best-effort restore)
        return None


def _restore_target_weight(op, old_target, old_weight):
    """Best-effort restore of Target/Weight after a partial edit (atomicity).

    Each write is guarded — restoring must NEVER mask the original SurfaceWriteError (the
    caller re-raises it). A ``None`` snapshot (the pre-edit read threw) is left untouched.
    """
    if old_target is not None:
        try:
            op.Target = old_target
        except Exception:  # noqa: BLE001 — restore is best-effort; never mask the real error
            pass
    if old_weight is not None:
        try:
            op.Weight = old_weight
        except Exception:  # noqa: BLE001
            pass


def _require_operand_number(params, n_operands):
    """Pull + validate the REQUIRED ``number`` (1-based operand row). Raises ToolParamError.

    Rejects bool / non-int / non-integral float / out-of-range. An integral float (a JSON
    round-trip can float an int) is coerced. The range is ``1..n_operands`` inclusive.
    """
    value = params.get("number")
    if isinstance(value, bool):
        raise ToolParamError(f"number must be an integer, not a bool ({value!r})")
    if isinstance(value, float):
        # Guard non-finite BEFORE int() — int(nan) raises ValueError, int(inf) raises
        # OverflowError (NEITHER a ToolParamError), which would ESCAPE the merit_param
        # catch into the opaque `internal` family. Reject as merit_param.
        if not math.isfinite(value):
            raise ToolParamError(f"number must be a finite integer, got {value!r}")
        if value == int(value):
            value = int(value)
        else:
            raise ToolParamError(f"number must be an integer, got non-integral {value!r}")
    if not isinstance(value, int):
        raise ToolParamError(
            f"number is required and must be an integer in 1..{n_operands}, "
            f"got {type(value).__name__} {value!r}"
        )
    if value < 1 or value > n_operands:
        raise ToolParamError(
            f"number {value} is out of range; the merit has {n_operands} operands "
            f"(valid 1..{n_operands})"
        )
    return value


def edit_operand(session, params):
    """Re-target an EXISTING merit operand IN PLACE (§c).

    Params: ``number`` (REQUIRED int, 1-based operand row) + ``target`` and/or ``weight``
    (at least one). Reads ``op = mfe.GetOperandAt(number)``, sets ONLY the provided
    ``op.Target`` / ``op.Weight``, and proves each through the ``_lens_common`` read-back
    firewall (a silent no-op -> ``SurfaceWriteError`` -> dispatch ``surface_write``).

    The point: re-targeting a per-config EFFL INSIDE a ``CONF`` block must PRESERVE
    the CONF-bracket context — a pure in-place ``GetOperandAt(n)`` property write does (probe
    Q3b: TypeName + operand count preserved), unlike a remove+re-add (which appends + breaks
    the bracket). An out-of-range ``number`` -> ``merit_param`` (zero mutation). A VALUE-LESS
    control operand (``CONF`` — ``_mc.is_valueless_control``) is REFUSED (``merit_param``): it
    has no numeric Target/Weight (a fresh CONF reads the inf sentinel), so an edit would
    false-reject on the read-back. A non-finite ``target``/``weight`` is rejected pre-write
    (``merit_param``) — an ``inf`` target reads back equal to itself and would slip the
    read-back firewall (a silent-nonsense write). Never raises past the boundary; param-class
    -> ``merit_param``; a read-back firewall failure -> ``surface_write`` (dispatch envelope).

    ATOMIC: a target+weight edit captures the OLD Target/Weight first and, on
    ANY read-back firewall failure (e.g. the second write does not take), RESTORES both before
    re-raising — so a partial two-write failure leaves the row UNCHANGED, never a half-applied
    edit. (Restore is best-effort; the read-back firewall raise is the authoritative signal.)
    """
    system = session.system
    mfe = system.MFE

    n_operands = int(mfe.NumberOfOperands)
    try:
        number = _require_operand_number(params, n_operands)
    except ToolParamError as exc:
        return _oc.error_envelope("edit_operand", "merit_param", str(exc))

    has_target = "target" in params
    has_weight = "weight" in params
    if not has_target and not has_weight:
        return _oc.error_envelope(
            "edit_operand", "merit_param",
            "nothing to edit: provide target and/or weight",
        )
    try:
        target = _num_param(params, "target", None) if has_target else None
        weight = _num_param(params, "weight", None) if has_weight else None
    except ToolParamError as exc:
        return _oc.error_envelope("edit_operand", "merit_param", str(exc))
    # Reject a non-finite target/weight pre-write: inf reads back == itself and would
    # slip the read-back firewall; nan would route to surface_write — neither is a real edit.
    for label, val, present in (("target", target, has_target),
                                ("weight", weight, has_weight)):
        if present and not math.isfinite(val):
            return _oc.error_envelope(
                "edit_operand", "merit_param",
                f"{label} must be a finite number, got {val!r}",
            )

    op = mfe.GetOperandAt(number)
    try:
        type_name = str(op.TypeName)
    except Exception:  # noqa: BLE001 — an unreadable type still allows the edit; label unknown
        type_name = "<unknown>"

    # A value-less control operand (CONF) has no numeric Target/Weight (inf sentinel) —
    # editing it would false-reject on the read-back. Refuse pre-mutation (zero mutation).
    if _mc.is_valueless_control(type_name):
        return _oc.error_envelope(
            "edit_operand", "merit_param",
            f"operand {number} is a value-less control operand ({type_name}); it has no "
            "numeric Target/Weight to edit (re-author it via add_operand if needed)",
        )

    # Capture the OLD Target/Weight so a partial two-write failure can restore the row to
    # its pre-edit state (atomicity). A read-throw degrades to None (best-effort restore).
    old_target = _safe_read_float(op, "Target")
    old_weight = _safe_read_float(op, "Weight")

    # In-place writes, each read-back proven. A mismatch RAISES SurfaceWriteError; on that
    # raise we RESTORE both cells (atomic) then re-raise -> dispatch surface_write, the MFE
    # row otherwise unchanged.
    try:
        if has_target:
            op.Target = target
            actual_target = float(op.Target)
            _lc._verify_or_raise("target", target, actual_target, surface=None)
        if has_weight:
            op.Weight = weight
            actual_weight = float(op.Weight)
            _lc._verify_or_raise("weight", weight, actual_weight, surface=None)
    except SurfaceWriteError:
        _restore_target_weight(op, old_target, old_weight)
        raise

    return {
        "ok": True,
        "number": number,
        "type": type_name,
        "target": safe_float(float(op.Target)),
        "weight": safe_float(float(op.Weight)),
    }


BUILD_MERIT_SPEC = ToolSpec(
    name="build_merit",
    handler=build_merit,
    required_params=(),
    param_types={
        "glass": "boolean",
        "air": "boolean",
        "overall_weight": "number",
        "min_air": "number",
        "min_glass": "number",
        "max_air": "number",
        "max_glass": "number",
        # rings/arms are integer COUNTS but follow the harness index/count convention
        # (``optimize.cycles``/``max_passes``): typed "number" so an integral float
        # (``6.0``) is accepted, NOT the strict-"integer" get_mtf.series exception.
        "rings": "number",
        "arms": "number",
        # MCE: span all configurations (the CONF-bracket spanning lever).
        "span_configs": "boolean",
        # The image-quality criterion ('spot' | 'wavefront').
        "criterion": "string",
        # Preserve the hand-authored custom operand tail across the rebuild.
        "preserve_custom": "boolean",
    },
    description=(
        "Build a default RMS merit function via the optimization wizard — the "
        "image-quality baseline you add constraints onto. criterion='spot' (default, "
        "RMS spot radius) or 'wavefront' (RMS wavefront error — use for a "
        "near-diffraction-limited design); the criterion is pinned deterministically. "
        "By DEFAULT authors positive thickness floors (min_air=0.5, min_glass=1.0, in "
        "lens units — assumes a mm-scale ~10-500mm system; override for micro-optics, or "
        "pass min_air=0/min_glass=0 to restore target-0 bounds) so optimization cannot "
        "drive a center/edge thickness negative. rings/arms set the Gaussian-Quadrature "
        "pupil sampling density (more = finer, slower). Returns the operand count + merit "
        "+ the applied floors + resolved ring/arm counts + the applied criterion. NOTE a "
        "rebuild REPLACES the whole MFE (hand-authored operands DELETED) — preserve them "
        "with serialize_merit -> apply_merit_recipe(mode='append'), OR pass "
        "preserve_custom=true to snapshot the custom tail, rebuild the wizard, and "
        "re-append the tail automatically (CONF brackets + intra-tail Op# refs intact); "
        "preserve_custom REFUSES (never mis-slices) a stale/hand-edited wizard block or a "
        "custom row referencing a wizard operand, and on any failure restores the FULL "
        "pre-preserve merit. "
        "If the built merit reads 9e9 (uncomputable — a wide-field/fast GQ corner ray fails "
        "to trace), do NOT hide it with coarser rings/arms (a denser GQ sample lands on the "
        "clipped corner and flips a near-vignetting design uncomputable): "
        "set_vignetting(mode='from_rays') per config FIRST, then rebuild here so the GQ "
        "operands launch the vignetted (traceable) pupil. See add_operand, "
        "add_math_constraint, normalize_stop."
    ),
)

ADD_OPERAND_SPEC = ToolSpec(
    name="add_operand",
    handler=add_operand,
    required_params=("operand",),
    param_types={
        "operand": "string",
        "target": "number",
        "weight": "number",
        "params": "object",
        "config": "number",
    },
    description=(
        "Add one merit-function operand by code (e.g. EFFL) with a Target/Weight, "
        "validated against the live operand-type enum and read-back proven. Takes a "
        "CODE, not a phrase. Pass config=k to author the operand inside a per-config "
        "CONF k bracket (it evaluates only in configuration k) — no hand-authoring of "
        "CONF rows. For an MTF-aware merit, author MTFT (tangential) / MTFS (sagittal) "
        "with params={'Field':<1-based index>,'Freq':<cyc/mm>} — the field is an "
        "integer Field index, NOT Hx/Hy (an omitted Field reads on-axis, which is "
        "flagged). See add_math_constraint, apply_merit_recipe, build_merit, get_mtf."
    ),
)

DUMP_MERIT_FUNCTION_SPEC = ToolSpec(
    name="dump_merit_function",
    handler=dump_merit_function,
    required_params=(),
    description=(
        "Read the full merit function (per-operand type/target/weight/value/"
        "contribution + the merit value). Read-only; never mutates."
    ),
)

EDIT_OPERAND_SPEC = ToolSpec(
    name="edit_operand",
    handler=edit_operand,
    required_params=("number",),
    param_types={
        "number": "number",
        "target": "number",
        "weight": "number",
    },
    description=(
        "Re-target an EXISTING merit operand IN PLACE by row number (target and/or "
        "weight), read-back proven. Use this to change a per-config EFFL inside a CONF "
        "block WITHOUT remove+re-add (which appends and breaks the CONF bracket). "
        "Refuses a value-less control operand (e.g. CONF, which has no numeric "
        "Target/Weight). Find the row number with dump_merit_function. See add_operand, "
        "build_merit."
    ),
)

TOOL_SPECS = (
    BUILD_MERIT_SPEC, ADD_OPERAND_SPEC, DUMP_MERIT_FUNCTION_SPEC, EDIT_OPERAND_SPEC,
)
