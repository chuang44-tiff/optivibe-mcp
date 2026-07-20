"""tools/lens_vignetting.py — the vignetting-factor authoring tool (§1).

ONE dispatchable tool ``set_vignetting`` over ``system.SystemData.Fields`` — a
``set_field`` / ``set_aperture`` sibling. Vignetting factors (VDX/VDY decenter,
VCX/VCY compression) reduce the launched pupil per field so a wide-field fast system's
corner rays trace (the dogfood 9e9 "could-not-compute" merit becomes computable after
the factors are set AND the GQ merit is rebuilt — probe Q2).

Three modes (``mode`` REQUIRED — no silent default; the modes have different effects,
validated PRE-mutation):

- ``from_rays`` — ``Fields.SetVignetting()`` sets EVERY field's factors from its current
  marginal rays (the AUTO dogfood path). Then read back ALL fields' VDX/VDY/VCX/VCY.
- ``set`` — manual per-field write. REQUIRES ``field`` (1-based) + at least one of
  ``vdx``/``vdy``/``vcx``/``vcy``. The COMPLETE client-side firewall (the engine accepts
  garbage silently — the #108 surface-aperture precedent) runs PRE-mutation: ``field`` ∈
  ``[1, NumberOfFields]``; ``vdx``/``vdy`` finite (any sign — a decenter); ``vcx``/``vcy``
  finite AND ∈ ``[0, 1)`` (compression; 1.0 = fully vignetted, ≥1 and <0 rejected). Each
  written ``IField.VDx`` is read-back-proven (a silent no-op -> ``vignetting`` family).
- ``clear`` — ``Fields.ClearVignetting()``; read back -> assert all factors 0.0.

``merit_rebuild_required`` is ALWAYS True in the envelope: the GQ operands bake the pupil
coordinates at BUILD time, so a bare ``CalculateMeritFunction()`` on the OLD operands
still reads 9e9 after factors change — the merit MUST be rebuilt (re-run build_merit).
The tool CANNOT own build_merit (separability); it DISCLOSES, the agent rebuilds. The
``optimize_merit_uncomputable`` preflight is the existing BACKSTOP if the
agent forgets (both share ``_merit_is_uncomputable``, cannot disagree).

Family: the STRING token ``"vignetting"`` via ``error_envelope`` (NO new error class —
reuse ``SurfaceWriteError`` / ``ToolParamError``, the mce_config/aperture precedent). The
handler NEVER raises past its boundary (the L26 firewall).

Live ZOS-API integration: exercised by the live test (THE make-it-bite —
a 9e9 GQ merit becomes computable after from_rays + rebuild; the corner ray flips
vignetteCode 1->0); unit-tested against non-hollow vignette fake
doubles whose ``SetVignetting`` COMPUTES factors from modeled marginal rays.
"""
import math
from time import perf_counter

from .._io import safe_float
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _config_common as _cc
from . import _mce_cells as _mc
from ._analysis_common import error_envelope
# Shared, session-free MCE helpers (L30 — do NOT re-implement the token/param readers or
# the row author; the reference direction is one-way, so this import is cycle-free per the
# spec §Axis-6).
from .mce_config import _operand_code, _read_param_safe, set_config_operand
from ._spot_validity import (
    evaluate_spot_validity,
    read_field_hys,
    validity_budget_s,
)


_VIG_FAMILY = "vignetting"   # the read-back / engine-throw refusal family
_VIG_PARAM = "tool_param"    # a bad param value (ToolParamError)

_MODES = ("from_rays", "set", "clear")
_FACTOR_KEYS = ("vdx", "vdy", "vcx", "vcy")

# S7 (GAP 6): the per-config field-vignetting operand tokens the factor keys author
# into per-config MCE cells (config='all'|<n> from_rays sweep). vdx->FVDX, ... The
# authoring/dedup key is (operand_code in this set) AND (Param1 == field - 1).
_FACTOR_TOKEN = {"vdx": "FVDX", "vdy": "FVDY", "vcx": "FVCX", "vcy": "FVCY"}
_FV_TOKENS = frozenset(_FACTOR_TOKEN.values())

# S7 module constants (§2 / §Axis-4 — NO env var).
_FV_AUTHOR_TOL = 1e-9        # non-zero footprint gate (Axis 2): |value| > this is authored
_TAPER_STEP = 0.05           # symmetric VCX/VCY compression step (Axis 4)
_VC_CAP = 0.95               # the compression cap, strictly < 1.0 (Axis 4)
_TAPER_MAX_PASSES = 16       # a HARD iteration cap independent of the factor cap (Axis 4)


def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _finite(value, label):
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


def _finite_compression(value, label):
    """A compression factor (VCX/VCY): finite AND in ``[0, 1)`` (1.0 = fully vignetted)."""
    coerced = _finite(value, label)
    if coerced < 0.0 or coerced >= 1.0:
        raise ToolParamError(
            f"{label} must be in [0, 1) (a compression factor; 0 = no compression, "
            f"1.0 = fully vignetted — >=1 and <0 are non-physical), got {coerced}"
        )
    return coerced


def _require_field_index(value, n_fields):
    """Require an exact field index (coercing an integral float) in ``[1, n_fields]``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"field must be an integer field index, got {type(value).__name__} {value!r}"
        )
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            raise ToolParamError(
                f"field must be an integer field index, got non-integral {value!r}"
            )
    field = int(value)
    if not (1 <= field <= n_fields):
        raise ToolParamError(
            f"field {field} out of range; valid 1..{n_fields} "
            f"(NumberOfFields={n_fields})"
        )
    return field


def _vig_write_error(message, *, field=None, intended=None, actual=None):
    """Build a ``SurfaceWriteError`` carrying the DISTINCT ``vignetting`` family (D10)."""
    exc = SurfaceWriteError(message, field=field, intended=intended, actual=actual,
                            surface=None)
    exc.error_family = _VIG_FAMILY
    return exc


def _readback_ok(intended, actual):
    """Tight read-back equality for a vignetting factor (double)."""
    return (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and math.isfinite(actual)
        and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=1e-12)
    )


def _read_all_factors(fields_mgr, n_fields):
    """Read back EVERY field's VDX/VDY/VCX/VCY (the read-back-as-proof for from_rays/clear)."""
    out = []
    for i in range(1, n_fields + 1):
        f = fields_mgr.GetField(i)
        out.append({
            "field": i,
            "vdx": safe_float(f.VDX),
            "vdy": safe_float(f.VDY),
            "vcx": safe_float(f.VCX),
            "vcy": safe_float(f.VCY),
        })
    return out


# =========================================================================== #
# set_vignetting
# =========================================================================== #
def set_vignetting(session, params):
    """Set / compute / clear per-field vignetting factors with read-back-as-proof.

    Params: ``mode`` (str, REQUIRED — one of from_rays/set/clear). For ``set``: ``field``
    (int, 1-based) + at least one of ``vdx``/``vdy`` (decenter, any sign) /
    ``vcx``/``vcy`` (compression, [0, 1)).

    Authors via ``Fields.SetVignetting()`` / ``ClearVignetting()`` / per-field
    ``IField.VDx``, proves the change by reading the factors back, and ALWAYS discloses
    ``merit_rebuild_required:true`` (the GQ merit must be rebuilt after factors change —
    the tool does not own build_merit). NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_vignetting", _VIG_PARAM, str(exc))
    except SurfaceWriteError as exc:  # incl. the vignetting read-back failure
        return error_envelope(
            "set_vignetting", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> vignetting (L26)
        return error_envelope(
            "set_vignetting", _VIG_FAMILY,
            f"unexpected engine fault setting the vignetting factors ({exc!r}); refusing "
            "rather than shipping unverified factors",
        )


def _impl(session, params):
    system = session.system
    fields_mgr = system.SystemData.Fields

    # mode firewall (no silent default) — PRE-mutation.
    mode = params.get("mode")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ToolParamError(
            f"mode is required and must be one of {list(_MODES)}, got {mode!r}"
        )

    # S7 (GAP 6): the per-config selector. config is None -> the byte-identical legacy
    # path below (the T-BC anchor — NO sweep, NO MCE, NO validator, NO new keys). An
    # EXPLICIT selector (int | "all") routes to the per-config from_rays sweep + the
    # off-meridian validator. config is ORTHOGONAL to mode and valid ONLY with
    # mode='from_rays' (Axis 5) — set/clear + config is REFUSED loud PRE-mutation (ZERO
    # engine touch: this raise is BEFORE any SetVignetting/AddOperand/write_config_cell).
    config = params.get("config")
    if config is not None:
        taper = params.get("taper", False)
        if not isinstance(taper, bool):
            raise ToolParamError(
                f"taper must be a bool (the opt-in auto-compression flag), got "
                f"{type(taper).__name__} {taper!r}"
            )
        if mode != "from_rays":
            raise ToolParamError(
                f"config is only valid with mode='from_rays' (per-config vignetting is fit "
                f"from each config's marginal rays); mode={mode!r} + config={config!r} is "
                "refused with zero mutation. For a per-config MANUAL write use "
                "set_config_value(FVDX/FVDY/FVCX/FVCY); a per-config clear is ambiguous."
            )
        return _from_rays_over_configs(session, params, config, taper)

    n_fields = int(fields_mgr.NumberOfFields)

    if mode == "from_rays":
        # Compute every field's factors from its current marginal rays.
        fields_mgr.SetVignetting()
        factors = _read_all_factors(fields_mgr, n_fields)
        # Read-back-as-proof: the engine-COMPUTED factors must be FINITE
        # before we claim success — a non-finite factor is a degraded/failed compute, so
        # fail LOUD rather than ship a meaningless factor as ok. NO [0, 1) bound here:
        # SetVignetting may legitimately compute heavy vignetting (vcy near 1) for a wide
        # field — the compression range is a MANUAL-input firewall (the `set` path) only.
        for row in factors:
            for key in _FACTOR_KEYS:
                val = row[key]
                if not (isinstance(val, (int, float)) and not isinstance(val, bool)
                        and math.isfinite(val)):
                    raise _vig_write_error(
                        f"SetVignetting produced a non-finite factor {key.upper()}={val!r} "
                        f"on field {row['field']} — a degraded compute; refusing rather "
                        "than shipping unverified factors",
                        field=f"field_{row['field']}_{key}", intended=None, actual=val,
                    )

    elif mode == "clear":
        fields_mgr.ClearVignetting()
        factors = _read_all_factors(fields_mgr, n_fields)
        # Read-back-as-proof: ClearVignetting must zero EVERY factor (a silent no-op caught).
        for row in factors:
            for key in _FACTOR_KEYS:
                val = row[key]
                if not (isinstance(val, (int, float)) and not isinstance(val, bool)
                        and val == 0.0):
                    raise _vig_write_error(
                        f"ClearVignetting did not zero field {row['field']} {key.upper()} "
                        f"(reads {val!r}); the clear silently no-opped — refusing rather "
                        "than claiming unverified factors",
                        field=f"field_{row['field']}_{key}", intended=0.0, actual=val,
                    )

    else:  # mode == "set"
        # The COMPLETE per-field firewall (zero engine touch on a bad value) — PRE-mutation.
        if "field" not in params:
            raise ToolParamError("mode='set' requires a field (1-based field index)")
        field = _require_field_index(params["field"], n_fields)

        supplied = {k: params[k] for k in _FACTOR_KEYS if k in params}
        if not supplied:
            raise ToolParamError(
                "mode='set' requires at least one of vdx/vdy (decenter) or vcx/vcy "
                "(compression)"
            )
        # Validate every supplied factor BEFORE any write.
        intended = {}
        for key, raw in supplied.items():
            if key in ("vdx", "vdy"):
                intended[key] = _finite(raw, key)          # any sign (decenter)
            else:
                intended[key] = _finite_compression(raw, key)  # [0, 1)

        # Write the validated cells, then read-back-prove each one (a silent no-op caught).
        f = fields_mgr.GetField(field)
        for key, value in intended.items():
            setattr(f, key.upper(), value)
        # Re-fetch a fresh handle and read back each written cell.
        f2 = fields_mgr.GetField(field)
        for key, value in intended.items():
            actual = float(getattr(f2, key.upper()))
            if not _readback_ok(value, actual):
                raise _vig_write_error(
                    f"field {field} {key.upper()} write did not read back: wrote "
                    f"{value!r}, read {actual!r} — the engine silently rejected the write "
                    "(a no-op), refusing rather than shipping unverified factors",
                    field=f"field_{field}_{key}", intended=value, actual=actual,
                )
        factors = _read_all_factors(fields_mgr, n_fields)

    return {
        "ok": True,
        "mode": mode,
        "factors": factors,
        # ALWAYS true: setting factors changes the pupil sampling, so the GQ merit must be
        # REBUILT (the operands bake the pupil coords at build time — a bare recompute on
        # the old operands still reads 9e9). The tool discloses; the agent rebuilds.
        "merit_rebuild_required": True,
    }


# =========================================================================== #
# S7 (GAP 6) — the per-config from_rays sweep + off-meridian validator + taper.
# =========================================================================== #
def _from_rays_over_configs(session, params, config, taper):
    """Per-config from_rays FV authoring + the off-meridian validator (config=int|'all').

    Routes through ``evaluate_over_configs`` (-> ``resolve_config_selector``: a bad config
    -> ``ToolParamError`` -> the outer ``_VIG_PARAM``; ``config=int`` visits one config,
    ``config='all'`` sweeps every config, both through the SAME grader). Aggregates the
    per-config grader dicts into the S7 envelope (§5): ``all_corners_clean`` /
    ``residual_clips`` (the anti-silent-wrong) / ``validation_available`` /
    ``worst_traced_fraction`` / ``fv_rows_authored`` / ``taper`` /
    ``merit_rebuild_required:True`` (KEPT — separability; the validator READS trace
    validity, it does NOT author a merit or optimize). An AUTHORING throw on the
    single-config path escapes to the outer ``_VIG_FAMILY`` refusal (fail-closed).
    """
    system = session.system
    n_configs = _cc.safe_number_of_configurations(system)

    def grader(sess):
        sys_ = sess.system
        k = _cc.safe_current_configuration(sys_)
        return _grade_one_config(sess, sys_, k, n_configs, taper)

    swept = _cc.evaluate_over_configs(session, config, grader)

    # Normalize the sweep result to a list of per-config grader entries. mode 'all'
    # returns the sweep shape (per_config); mode 'single' returns the grader dict itself.
    switch_warning = None
    if isinstance(swept, dict) and "per_config" in swept:
        entries = swept["per_config"]
        config_evaluated = swept.get("config_evaluated")
        coverage = swept.get("coverage")
        config_differs = swept.get("config_differs")
        base_warning = swept.get("warning")
    else:
        cfg = swept.get("config_evaluated") if isinstance(swept, dict) else None
        entry = dict(swept) if isinstance(swept, dict) else {"ok": False}
        entry.setdefault("config", cfg)
        entries = [entry]
        config_evaluated = cfg
        coverage = None
        config_differs = None
        base_warning = swept.get("warning") if isinstance(swept, dict) else None
        # FIX 1 (the convergent anti-silent-wrong): the single-visit
        # (config=int) path of evaluate_over_configs sets config_switch_warning /
        # mutation_warning when a SILENT SetCurrentConfiguration no-op leaves the active
        # config at the wrong index (the grade authored+validated the WRONG config while
        # config_evaluated claims the requested one). The 'all' path discloses the
        # equivalent failure via coverage.missing; the single-visit path must surface it
        # too, else the agent cannot tell the requested config was never reached — the
        # exact silent-wrong this guard fights. Fold it into the envelope warning.
        if isinstance(swept, dict):
            switch_warning = (
                swept.get("config_switch_warning") or swept.get("mutation_warning")
            )

    per_config = []
    residual_clips = []
    fv_rows_authored = []
    fv_cells_skipped = []
    duplicate_fv_rows = []
    all_fracs = []
    any_unavailable = False
    single_config = False
    taper_passes = {}
    taper_restored = []
    restore_failed_any = False

    for entry in entries:
        cfg = entry.get("config")
        if not entry.get("ok"):
            per_config.append({"config": cfg, "ok": False,
                               "error": entry.get("error")})
            any_unavailable = True
            continue
        validation = entry.get("validation") or {"available": False, "fields": []}
        if not validation.get("available"):
            any_unavailable = True
        single_config = single_config or bool(entry.get("single_config"))
        for f in validation.get("fields", []):
            tf = f.get("traced_fraction")
            if f.get("available") and isinstance(tf, (int, float)) \
                    and not isinstance(tf, bool):
                all_fracs.append(tf)
            elif not f.get("available"):
                any_unavailable = True
        residual_clips.extend(_residuals_for_config(cfg, entry))
        fv_rows_authored.extend(entry.get("fv_rows") or [])
        fv_cells_skipped.extend(entry.get("fv_cells_skipped") or [])
        duplicate_fv_rows.extend(entry.get("duplicate_fv_rows") or [])
        tblock = entry.get("taper") or {}
        if tblock.get("applied"):
            for fld, p in (tblock.get("passes") or {}).items():
                taper_passes[f"cfg{cfg}_field{fld}"] = p
            for fld in tblock.get("restored_baseline") or []:
                taper_restored.append({"config": cfg, "field": fld})
            if tblock.get("restore_failed"):
                restore_failed_any = True
        per_config.append({
            "config": cfg,
            "factors": entry.get("factors"),
            "fv_rows": entry.get("fv_rows"),
            "validation": validation,
            "taper": tblock,
        })

    # The load-bearing anti-silent-wrong headline. A validator fault on ANY visited config
    # -> INDETERMINATE (None), never a false "clean" (§5). Else clean iff NO residual clip.
    all_corners_clean = None if any_unavailable else (not residual_clips)
    worst_traced_fraction = min(all_fracs) if all_fracs else None
    validation_available = not any_unavailable

    if taper:
        # converged ONLY when EVERY clipped field reached 9/9 (no residual clip remains),
        # the validator ran for every visited config, and no restore silently failed.
        converged = (not residual_clips) and (not any_unavailable) \
            and (not restore_failed_any)
        taper_block = {
            "applied": True,
            "converged": converged,
            "passes": taper_passes,
            "restored_baseline": taper_restored,
        }
    else:
        taper_block = {"applied": False}

    env = {
        "ok": True,
        "mode": "from_rays",
        "config_evaluated": config_evaluated,
        "n_configs": n_configs,
        "single_config": single_config,
        "per_config": per_config,
        "all_corners_clean": all_corners_clean,
        "residual_clips": residual_clips,
        "validation_available": validation_available,
        "worst_traced_fraction": worst_traced_fraction,
        "fv_rows_authored": fv_rows_authored,
        "fv_cells_skipped": fv_cells_skipped,
        "duplicate_fv_rows": duplicate_fv_rows,
        "taper": taper_block,
        # Separability, KEPT: setting factors changes the pupil sampling, so the GQ merit
        # must be REBUILT — the tool discloses, the agent rebuilds (build_merit again).
        "merit_rebuild_required": True,
    }
    if coverage is not None:
        env["coverage"] = coverage
    if config_differs is not None:
        env["config_differs"] = config_differs
    warnings = []
    if base_warning:
        warnings.append(base_warning)
    if switch_warning:
        # FIX 1: surface the config=int silent-switch no-op (the requested config was not
        # reached — the grade ran at the active config instead).
        warnings.append(switch_warning)
    if restore_failed_any:
        warnings.append(
            "taper_restore_failed: a cap-fail field's baseline restore did not read back; "
            "the residual clip is retained and its FV cell may be left over-compressed"
        )
    if warnings:
        env["warning"] = "; ".join(warnings)
    return env


def _grade_one_config(session, system, k, n_configs, taper):
    """Grade ONE active config: per-config from_rays -> author FV rows -> validate -> taper.

    Runs with config ``k`` active (the surrounding ``with_configuration`` switched it).
    Returns a dict the sweep aggregates: ``config_headline`` (the worst traced_fraction,
    for the D3 differs signal), ``factors``, ``fv_rows``, ``fv_cells_skipped``,
    ``duplicate_fv_rows``, ``validation``, ``taper``, ``single_config``,
    ``_residual_reasons``. An AUTHORING throw (``write_config_cell``/``AddOperand``) is NOT
    caught here — it propagates fail-closed (the single-config path -> ``_VIG_FAMILY``; the
    'all' path -> that config's ``ok:false`` via ``evaluate_over_configs._grade_safe``). The
    VALIDATOR never raises (its own never-raise contract).
    """
    fields_mgr = system.SystemData.Fields
    n_fields = int(fields_mgr.NumberOfFields)

    # 1. per-config from_rays: compute every field's factors from its marginal rays.
    fields_mgr.SetVignetting()
    factors = _read_all_factors(fields_mgr, n_fields)

    fv_rows = []
    fv_cells_skipped = []
    duplicate_fv_rows = []
    single_config = n_configs <= 1

    # 2. author the NON-ZERO factors into config k's per-config FV MCE cells (multi-config
    # only — a single-config system's global IField factors suffice, no MCE rows, Axis 2).
    if not single_config:
        for row in factors:
            fr, sk, du = _author_config_factors(session, system, k, row, row["field"])
            fv_rows.extend(fr)
            fv_cells_skipped.extend(sk)
            duplicate_fv_rows.extend(du)

    # 3. read the per-field Hy at the ACTIVE config (INSIDE the grader — Hy may be MCE-
    # driven XFIE/YFIE; do NOT hoist outside the sweep) then 4. validate off-meridian.
    field_hys = read_field_hys(system)
    validation = _validate_off_meridian(system, field_hys)

    taper_block = {"applied": False}
    residual_reasons = {}
    residual_final_factors = {}   # FIX 2: field -> {vcx, vcy} ACTUAL cell on restore_failed
    if taper:
        taper_block = {
            "applied": True, "passes": {}, "restored_baseline": [],
            "restore_failed": [], "skipped": [],
        }
        clipped = [
            f["field"] for f in validation["fields"]
            if f.get("available")
            and isinstance(f.get("traced_fraction"), (int, float))
            and not isinstance(f.get("traced_fraction"), bool)
            and f["traced_fraction"] < 1.0
        ]
        for field in clipped:
            tr = _taper_config(session, system, k, field)
            taper_block["passes"][field] = tr["passes"]
            if tr["skipped"]:
                taper_block["skipped"].append(field)
                residual_reasons[field] = "slaved_factor_cannot_taper"
            elif tr["converged"]:
                pass  # the tapered VCX/VCY are KEPT (that IS the fix)
            else:
                if tr["restore_failed"]:
                    taper_block["restore_failed"].append(field)
                    residual_reasons[field] = "taper_restore_failed"
                    # FIX 2: the cell is left over-compressed (~cap) — carry the ACTUAL
                    # read-back so the residual disclosure is honest, not the baseline.
                    if tr.get("final_vc"):
                        residual_final_factors[field] = tr["final_vc"]
                else:
                    taper_block["restored_baseline"].append(field)
                    residual_reasons[field] = "vignetting_cap_reached"
        # Re-validate after tapering for the FINAL per-config validation block + residuals.
        field_hys = read_field_hys(system)
        validation = _validate_off_meridian(system, field_hys)

    fracs = [
        f["traced_fraction"] for f in validation["fields"]
        if isinstance(f.get("traced_fraction"), (int, float))
        and not isinstance(f.get("traced_fraction"), bool)
    ]
    headline = min(fracs) if fracs else None

    return {
        "ok": True,
        "config_headline": headline,
        "factors": factors,
        "fv_rows": fv_rows,
        "fv_cells_skipped": fv_cells_skipped,
        "duplicate_fv_rows": duplicate_fv_rows,
        "validation": validation,
        "taper": taper_block,
        "single_config": single_config,
        "_residual_reasons": residual_reasons,
        "_residual_final_factors": residual_final_factors,
    }


def _author_config_factors(session, system, k, factors_row, field):
    """Author the NON-ZERO FV factors of one field into config ``k`` (dedup + solve-skip).

    Returns ``(fv_rows, fv_cells_skipped, duplicate_fv_rows)``. For each factor with
    ``abs(value) > _FV_AUTHOR_TOL`` (Axis 2), find-or-author the ``(token, field)`` FV row
    (Axis 6 dedup) then WRITE config ``k``'s cell read-back-proven — UNLESS the cell carries
    a non-Fixed solve (Axis 6-iii solve-skip: NEVER clobber a deliberate Variable/Pickup).
    """
    fv_rows = []
    skipped = []
    dups = []
    for key in _FACTOR_KEYS:
        value = factors_row.get(key)
        if not (isinstance(value, (int, float)) and not isinstance(value, bool)):
            continue
        if abs(float(value)) <= _FV_AUTHOR_TOL:
            continue  # the non-zero footprint gate (a clean control authors nothing)
        token = _FACTOR_TOKEN[key]
        op, row, dup_rows = _find_or_author_fv_row(session, system, token, field)
        if dup_rows:
            # Write-first + DISCLOSE, never auto-delete (adjudicated by-design).
            # Auto-remove/merge is deferred.
            dups.append({"token": token, "field": field, "rows": [row] + dup_rows})
        cell = _mc.operand_cell(system, op, k)
        solve = _mc.solve_type_name(cell)
        if solve != "Fixed":
            skipped.append({"config": k, "field": field, "token": token,
                            "reason": "variable_or_pickup_solve", "solve": solve})
            continue
        try:
            written = _mc.write_config_cell(system, op, k, "Double", float(value))
        except SurfaceWriteError as exc:  # a read-back no-op -> _VIG_FAMILY (fail-closed)
            raise _vig_write_error(
                f"per-config field-vignetting write for {token}(field={field}) config {k} "
                f"did not read back ({exc}); refusing rather than shipping an unverified "
                "per-config vignetting factor",
                field=f"{token}_field_{field}",
            ) from exc
        fv_rows.append({"config": k, "field": field, "token": token,
                        "row": row, "written": written})
    return fv_rows, skipped, dups


def _find_or_author_fv_row(session, system, token, field):
    """Find the existing FV ``(token, field)`` row, else author it ONCE (Axis 6).

    Enumerate ``range(1, MCE.NumberOfOperands+1)``; a row matches iff ``_operand_code(op)
    == token`` (in the FV set) AND ``_read_param_safe(op, 1) == field - 1`` (both readers
    THROW-guarded -> None, so an unreadable row never spuriously matches). FOUND ->
    ``(op, row, duplicate_rows)`` (the FIRST match; extra matches are DISCLOSED, never
    auto-deleted). NOT found -> author via the shipped ``set_config_operand(operand=token,
    field=field)`` (``field -> Param1=field-1``, ``selects_field``-gated, transactional).
    An author refusal -> ``_VIG_FAMILY`` (fail-closed).
    """
    matches = []
    n = int(system.MCE.NumberOfOperands)
    for row in range(1, n + 1):
        op = system.MCE.GetOperandAt(row)
        if _operand_code(op) == token and _read_param_safe(op, 1) == field - 1:
            matches.append((row, op))
    if matches:
        first_row, first_op = matches[0]
        return first_op, first_row, [r for r, _ in matches[1:]]
    res = set_config_operand(session, {"operand": token, "field": field})
    if not isinstance(res, dict) or not res.get("ok"):
        raise _vig_write_error(
            f"could not author the per-config field-vignetting row {token}(field={field}): "
            f"{res.get('error') if isinstance(res, dict) else res!r} — refusing rather than "
            "shipping an unverified per-config vignetting factor",
            field=f"{token}_field_{field}",
        )
    row = res["row"]
    op = system.MCE.GetOperandAt(row)
    return op, row, []


def _validate_off_meridian(system, field_hys):
    """Off-meridian validator (PURE REUSE of ``evaluate_spot_validity``). Never raises.

    Returns ``{"available": bool, "fields": [{field, traced_fraction, valid, available,
    reason}, ...]}``. A field is a residual clip iff ``traced_fraction < 1.0``; a ``None``
    tally -> that field ``available:False`` (validity-indeterminate). ``field_hys is None``
    (a read failure) -> the whole config is indeterminate. READS trace validity only —
    authors no merit, does not optimize.
    """
    if not field_hys:
        return {"available": False, "fields": []}
    ev = evaluate_spot_validity(
        system, field_hys, deadline=perf_counter() + validity_budget_s()
    )
    fields = []
    for i, tally in enumerate(ev.get("fields", []), start=1):
        if not isinstance(tally, dict):
            fields.append({"field": i, "traced_fraction": None, "valid": None,
                           "available": False, "reason": "validity_indeterminate"})
            continue
        tf = tally.get("traced_fraction")
        valid = (isinstance(tf, (int, float)) and not isinstance(tf, bool) and tf >= 1.0)
        fields.append({
            "field": i,
            "traced_fraction": tf,
            "valid": valid,
            "available": True,
            "reason": None if valid else "partial_vignette",
        })
    return {"available": bool(ev.get("available")), "fields": fields}


def _field_fraction(system, field):
    """Re-validate + return ONE field's traced_fraction (or None). Never raises."""
    field_hys = read_field_hys(system)
    val = _validate_off_meridian(system, field_hys)
    for f in val["fields"]:
        if f["field"] == field:
            return f.get("traced_fraction")
    return None


def _read_cell_safe(system, op, k):
    """Read one per-config Double cell back (or None on any throw). Never raises."""
    try:
        return float(_mc.read_config_cell(system, op, k, "Double"))
    except Exception:  # noqa: BLE001 — an unreadable cell -> None (honest "unknown")
        return None


def _taper_config(session, system, k, field):
    """Symmetric VCX+VCY monotone compression for ONE clipped ``(config, field)`` (Axis 1/4).

    Direction-agnostic (the validator returns only aggregate ``n/9``, so a symmetric tighten
    is the only robust choice). Monotone non-decreasing in ``[vc0, _VC_CAP)``; first-clear
    stop; bounded by ``_TAPER_MAX_PASSES``. Solve-skip (Axis 6-iii): a non-Fixed FVCX/FVCY
    cell is NEVER tightened (residual ``slaved_factor_cannot_taper``). A field the cap CANNOT
    clear is RESTORED to its from_rays baseline (a per-cell snapshot, restored read-back-
    proven; a restore write that throws -> ``restore_failed``, residual retained, never
    raises). A converged field KEEPS its tapered VCX/VCY (that IS the fix).
    """
    # FIX 3 (DISCLOSURE GAP, tracked): a field whose from_rays VCX/VCY was ~0
    # (below _FV_AUTHOR_TOL, so _author_config_factors authored no FVCX/FVCY) gets its
    # FVCX/FVCY row CREATED here on-demand by the taper — that taper-authored row is NOT
    # merged into the envelope's fv_rows_authored (which lists only non-zero-from_rays rows).
    # Left as-is: threading + de-duping the taper's rows against _author_config_factors' rows
    # materially complicates the taper for a LOW nicety.
    op_x, row_x, _dx = _find_or_author_fv_row(session, system, "FVCX", field)
    op_y, row_y, _dy = _find_or_author_fv_row(session, system, "FVCY", field)
    cell_x = _mc.operand_cell(system, op_x, k)
    cell_y = _mc.operand_cell(system, op_y, k)
    if _mc.solve_type_name(cell_x) != "Fixed" or _mc.solve_type_name(cell_y) != "Fixed":
        return {"field": field, "converged": False, "passes": 0, "skipped": True,
                "restored": False, "restore_failed": False}

    base_x = float(_mc.read_config_cell(system, op_x, k, "Double"))
    base_y = float(_mc.read_config_cell(system, op_y, k, "Double"))
    vc = max(base_x, base_y, 0.0)
    passes = 0
    converged = False
    while passes < _TAPER_MAX_PASSES:
        frac = _field_fraction(system, field)
        if isinstance(frac, (int, float)) and not isinstance(frac, bool) and frac >= 1.0:
            converged = True
            break
        vc_next = vc + _TAPER_STEP
        if vc_next >= _VC_CAP:
            break  # the compression cap (first-clear stop is bounded by one STEP)
        _mc.write_config_cell(system, op_x, k, "Double", vc_next)
        _mc.write_config_cell(system, op_y, k, "Double", vc_next)
        vc = vc_next
        passes += 1

    if converged:
        return {"field": field, "converged": True, "passes": passes, "skipped": False,
                "restored": False, "restore_failed": False}

    # Cap-fail: RESTORE the from_rays baseline (light for the corner still dark).
    restore_failed = False
    try:
        _mc.write_config_cell(system, op_x, k, "Double", base_x)
        _mc.write_config_cell(system, op_y, k, "Double", base_y)
    except Exception:  # noqa: BLE001 — a restore write throw -> disclose, never raise
        restore_failed = True
    # FIX 2 (honest disclosure): a restore that THREW leaves the FV cell
    # OVER-COMPRESSED (~cap), NOT at the from_rays baseline — so read the ACTUAL final cell
    # value back so the residual's final_vcx/final_vcy report the light truly thrown away
    # (sourcing the baseline _factor_for would UNDERSTATE it, claiming ~0.04 while the cell
    # is ~cap). On the restored path the baseline IS what the cell holds, so no override.
    final_vc = None
    if restore_failed:
        final_vc = {"vcx": _read_cell_safe(system, op_x, k),
                    "vcy": _read_cell_safe(system, op_y, k)}
    return {"field": field, "converged": False, "passes": passes, "skipped": False,
            "restored": not restore_failed, "restore_failed": restore_failed,
            "final_vc": final_vc}


def _residuals_for_config(cfg, entry):
    """Build the ``residual_clips`` entries for one config from its FINAL validation (§5).

    A field is a residual clip iff its FINAL ``traced_fraction < 1.0``. The reason is the
    taper outcome (cap/slaved/restore_failed) when taper ran, else ``partial_vignette``; the
    ``recovery_hint`` NAMES ``taper=true`` when taper was NOT applied (the S5 recovery-naming
    precedent). ``final_vcx``/``final_vcy`` are the field's baseline compression factors (the
    restored / from_rays values).
    """
    validation = entry.get("validation") or {}
    reasons = entry.get("_residual_reasons") or {}
    # FIX 2: the ACTUAL read-back FV cell for a restore_failed field (~cap), keyed by field.
    final_factors = entry.get("_residual_final_factors") or {}
    taper_applied = bool((entry.get("taper") or {}).get("applied"))
    out = []
    for f in validation.get("fields", []):
        tf = f.get("traced_fraction")
        if not (f.get("available") and isinstance(tf, (int, float))
                and not isinstance(tf, bool) and tf < 1.0):
            continue
        field = f["field"]
        reason = reasons.get(field) or "partial_vignette"
        # FIX 2: on the taper_restore_failed path the from_rays baseline (_factor_for)
        # UNDERSTATES the light thrown away — report the ACTUAL over-compressed cell value
        # read back at restore time (None = unreadable, honest). Every other path (converged
        # / restored / no-taper) correctly holds the baseline, so keep _factor_for there.
        if field in final_factors:
            final_vcx = final_factors[field].get("vcx")
            final_vcy = final_factors[field].get("vcy")
        else:
            final_vcx = _factor_for(entry, field, "vcx")
            final_vcy = _factor_for(entry, field, "vcy")
        out.append({
            "config": cfg,
            "field": field,
            "traced_fraction": tf,
            "final_vcx": final_vcx,
            "final_vcy": final_vcy,
            "reason": reason,
            "recovery_hint": _recovery_hint(reason, taper_applied),
        })
    return out


def _factor_for(entry, field, key):
    """The field's from_rays/baseline factor value (or None) from the grader's factors."""
    for row in entry.get("factors") or []:
        if row.get("field") == field:
            return row.get(key)
    return None


def _recovery_hint(reason, taper_applied):
    """The residual-clip recovery hint — NAMES the taper when it was not applied (S5)."""
    if not taper_applied:
        return (
            "pass taper=true to attempt automatic vignetting compression, or reshape the "
            "aperture (set_surface_aperture) / accept the corner vignette."
        )
    if reason == "slaved_factor_cannot_taper":
        return (
            "the FVCX/FVCY factor carries a variable/pickup solve and cannot be tapered; "
            "clear the solve or reshape the aperture (set_surface_aperture)."
        )
    if reason == "taper_restore_failed":
        return (
            "automatic compression could not clear the corner AND the baseline restore did "
            "not read back; reshape the aperture (set_surface_aperture) and re-check."
        )
    return (
        "automatic compression hit its cap without clearing the corner (light was thrown "
        "away for a corner still dark); reshape the aperture (set_surface_aperture) or "
        "accept the corner vignette."
    )


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
SET_VIGNETTING_SPEC = ToolSpec(
    name="set_vignetting",
    handler=set_vignetting,
    required_params=("mode",),
    param_types={
        "mode": "string",
        "field": "number",
        "vdx": "number",
        "vdy": "number",
        "vcx": "number",
        "vcy": "number",
        "config": "number",
        "taper": "boolean",
    },
    description=(
        "Set per-field vignetting factors (VDX/VDY decenter, VCX/VCY compression) that "
        "reduce the launched pupil so a wide-field fast system's corner rays trace. "
        "mode=from_rays computes every field's factors from its current marginal rays "
        "(the auto path); mode=set writes one field's factors manually (field is 1-based; "
        "vdx/vdy any sign, vcx/vcy in [0,1)); mode=clear zeroes all factors. Proves the "
        "change by reading the factors back. PER-CONFIG: config='all'|<n> (mode=from_rays "
        "only) authors FV rows per config from each config's marginal rays and validates "
        "the OFF-MERIDIAN pupil (a skew/diagonal corner from_rays' meridional fit misses); "
        "residual_clips discloses any corner still dark. Pass taper=true to attempt "
        "automatic vignetting compression (opt-in — it throws away light and cannot verify "
        "relative illumination). config omitted = the legacy global path (byte-identical). "
        "Gotcha: after changing factors you MUST REBUILD the merit (build_merit again) — "
        "the GQ operands bake the pupil coords at build time, so a bare recompute still "
        "reads the 9e9 could-not-compute sentinel; the envelope always returns "
        "merit_rebuild_required:true. A replace-all set_field ZEROES vignetting, so author "
        "factors AFTER it. See set_field, build_merit, set_config_operand."
    ),
)

TOOL_SPECS = (SET_VIGNETTING_SPEC,)
