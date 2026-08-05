"""catalog/registry.py — the metric-adapter contract + the 12 concrete adapters.

Each registry tool is one frozen ``MetricAdapter`` (key path VERBATIM from the
adapter's frozen probe capture). ``profile`` owns the whole dispatch -> ``reading_ok`` ->
``extract`` -> coerce chain (D0); ``bench.py`` consumes the returned ``MetricCell``s and
NEVER re-reads ``env["result"]``. The clearance 3-state REUSES the shipped
``workspace._classify_clearance`` (do NOT re-implement it here); the numeric/label
coerce + the coverage sub-gate live in ``metrics.py`` as the single shared locus, so
the two can never drift apart.

``extract`` does the worst-field/worst-wave COLLAPSE (+ index) but NEVER coerces —
``profile`` routes each ``Reading`` through ``coerce_metric`` (numbers) or
``coerce_label`` (enum/index/string) per ``ColumnSpec.kind`` (D1). ``extract`` is total:
a missing key -> ``Reading(raw=None, note=...)``, never a KeyError.
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import metrics as M

# The clearance 3-state classifier — the shipped shape-aware verdict that is safe
# against the config="all" nested-result shape (it does not misread a per-config
# violation list as a top-level one). It returns
# ("clean"|"thin"|"indeterminate", summary) over the INNER check_clearance result
# (single-config OR the config="all" wrapper).
from ..tools.workspace import _classify_clearance


# --------------------------------------------------------------------------- #
# Frozen dataclasses.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ColumnSpec:
    name: str                        # CSV header, e.g. "rms_spot_um_worst"
    unit: str                        # "um"|"mm"|""|"pct"|"fraction"|"count"|"enum"
    kind: str                        # "number"|"enum"|"count"|"index"|"string"
    higher_is_better: Optional[bool] # ranker/plotter hint; None = not a KPI


@dataclass
class Reading:
    column: str
    raw: object = None               # float | str | None (pre-coerce)
    suspicious: bool = False
    valid: object = None             # tri-state (True|False|"indeterminate"|None; see metrics.py)
    applicable: bool = True
    field: Optional[int] = None      # collapsed worst-field/worst-wave index (manifest)
    note: Optional[str] = None


@dataclass(frozen=True)
class MetricAdapter:
    key: str
    tool_name: str
    columns: tuple
    params_from_basis: Callable
    extract: Callable
    config_all_capable: bool
    config_refusal: str              # "dispatch"|"result"|"ignore"|"n/a"
    config_scope: str                # "active" | "worst_over_configs"
    headline_kind: str
    single_config_note: Optional[str] = None


# MetricCell — what profile() hands bench.py (a plain dict, D0):
#   {"value": num|str|None, "status": <token>, "reason": str, "field": int|None,
#    "raw": <preserved raw or None>, "flags": [..]}
def _cell(value, status, *, reason="", field=None, raw=None, flags=None):
    return {"value": value, "status": status, "reason": reason or "",
            "field": field, "raw": raw, "flags": list(flags or [])}


# --------------------------------------------------------------------------- #
# Small extract helpers (pure; never raise).
# --------------------------------------------------------------------------- #
def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _get_headline(result, key):
    """A ``headline`` dict lookup -> value or None (a suspicious first-order key is
    DROPPED from ``headline``, so a missing key = the reading was suspicious/absent)."""
    hd = result.get("headline")
    if isinstance(hd, dict):
        return hd.get(key)
    return None


def _config_coverage_summary(result):
    """A compact config="all" coverage disclosure (D4/D5, FIX-4): visited/expected/missing
    /ok + config_differs — for the manifest ONLY (never a CSV cell). Total; never raises."""
    if not isinstance(result, dict):
        return {"visited": None, "expected": None, "missing": None, "ok": None,
                "config_differs": None}
    cov = result.get("coverage")
    cov = cov if isinstance(cov, dict) else {}
    return {"visited": cov.get("visited"), "expected": cov.get("expected"),
            "missing": cov.get("missing"), "ok": cov.get("ok"),
            "config_differs": result.get("config_differs")}


# =========================================================================== #
# The 12 adapters — params_from_basis + extract (key paths verbatim, capture-anchored).
# =========================================================================== #

# ---- first_order (get_first_order) -> fnum / total_track_mm / bfd_mm --------
# (native_efl_mm / norm_efl_mm live in bench.py's FIXED block — this adapter emits
#  the three non-EFL first-order KPIs.)
def _fo_params(basis, opts):
    return {}


def _fo_extract(result):
    out = []
    for col, hk in (("fnum", "working_f_number"),
                    ("total_track_mm", "total_track"),
                    ("bfd_mm", "back_focal_length")):
        val = _get_headline(result, hk)
        note = None if val is not None else "suspicious/absent"
        out.append(Reading(column=col, raw=val, note=note))
    return out


FIRST_ORDER = MetricAdapter(
    key="first_order", tool_name="get_first_order",
    columns=(ColumnSpec("fnum", "", "number", higher_is_better=False),
             ColumnSpec("total_track_mm", "mm", "number", higher_is_better=False),
             ColumnSpec("bfd_mm", "mm", "number", higher_is_better=None)),
    params_from_basis=_fo_params, extract=_fo_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="headline_dict")


# ---- rms_spot (get_spot) -> worst / worst_field / valid_fields -------------
def _spot_params(basis, opts):
    return {}


def _spot_extract(result):
    # get_spot's per-field tri-state (analysis_spot.py): ``valid is True`` = a proven
    # trace (rms real); ``valid is None`` = INDETERMINATE / budget-exhausted with the rms
    # PRESERVED (could-not-disprove); ``valid is False`` = a proven BAD trace (rms:null,
    # rms_raw preserved). BOTH proven AND indeterminate fields count toward the worst-field
    # collapse (a valid:None corner with a preserved 50 um must NOT be silently dropped
    # behind a valid:True 5 um — the understated-worst silent-wrong). Only valid:False is
    # EXCLUDED. A worst field that is indeterminate carries valid="indeterminate" ->
    # coerce reads ``unverified`` KEEPING the number; a proven worst carries valid=None -> ok.
    spots = result.get("spots")
    spots = spots if isinstance(spots, list) else []
    proven = [s for s in spots if isinstance(s, dict) and s.get("valid") is True]
    indet = [s for s in spots if isinstance(s, dict) and s.get("valid") is None]
    considered = proven + indet
    n_total = len(spots)
    n_valid = len(proven)   # "valid" count = PROVEN only (indeterminate is not proven).
    valid_fields = Reading(column="rms_spot_valid_fields",
                           raw=f"{n_valid}/{n_total}")
    if considered:
        worst = max(considered, key=lambda s: (s.get("rms")
                                               if _num(s.get("rms")) else float("-inf")))
        wf = worst.get("field")
        # A valid:None worst -> "indeterminate" (unverified, number KEPT); a valid:True
        # worst -> None (no validity concern -> ok).
        wvalid = M.VALID_INDETERMINATE if worst.get("valid") is None else None
        return [Reading(column="rms_spot_um_worst", raw=worst.get("rms"),
                        valid=wvalid, field=wf),
                Reading(column="rms_spot_worst_field", raw=wf),
                valid_fields]
    # No proven AND no indeterminate field -> every field is a proven bad trace.
    reasons = ";".join(str(s.get("reason")) for s in spots
                       if isinstance(s, dict) and s.get("valid") is not True)
    note = f"all fields invalid: {reasons}" if spots else "no spots"
    # Preserve the worst rms_raw for the manifest ("raw kept in rms_raw" even on a
    # failed trace); valid=False -> failed_trace (the cell value is None regardless
    # of the preserved raw).
    raws = [s.get("rms_raw") for s in spots
            if isinstance(s, dict) and _num(s.get("rms_raw"))]
    kept_raw = max(raws) if raws else None
    return [Reading(column="rms_spot_um_worst", raw=kept_raw, valid=False, note=note),
            Reading(column="rms_spot_worst_field", raw=None, note=note),
            valid_fields]


RMS_SPOT = MetricAdapter(
    key="rms_spot", tool_name="get_spot",
    columns=(ColumnSpec("rms_spot_um_worst", "um", "number", higher_is_better=False),
             ColumnSpec("rms_spot_worst_field", "", "index", higher_is_better=None),
             ColumnSpec("rms_spot_valid_fields", "", "string", higher_is_better=None)),
    params_from_basis=_spot_params, extract=_spot_extract,
    config_all_capable=False, config_refusal="dispatch", config_scope="active",
    headline_kind="list_collapse", single_config_note="single_config_only")


# ---- mtf (get_mtf) -> worst / worst_field ----------------------------------
def _mtf_params(basis, opts):
    return {"at_frequencies": [basis.mtf_freq]}


def _mtf_extract(result):
    series = result.get("series")
    series = series if isinstance(series, list) else []
    freq = None
    # The requested frequency = the single at[] frequency the summary carries.
    for s in series:
        ats = s.get("at") if isinstance(s, dict) else None
        if isinstance(ats, list) and ats and isinstance(ats[0], dict):
            freq = ats[0].get("frequency")
            break
    best = None
    best_idx = None
    for s in series:
        if not isinstance(s, dict):
            continue
        idx = s.get("index")
        # SERIES 0 IS THE DIFFRACTION LIMIT, NOT A FIELD -- get_mtf's own contract
        # ("idx0 = the diffraction-limit series, idx1..N = the fields in order").
        # It is the theoretical UPPER bound, so letting it into a worst-FIELD
        # collapse can only ever report a number no real field achieved.  Two ways
        # it bites, both silent: if every real field interpolates to null past the
        # grid while the always-computable limit survives, `mtf_worst` reports the
        # BEST PHYSICALLY POSSIBLE value with status ok -- a null resolving to the
        # BEST answer, the exact inversion the null-is-never-scored rule forbids;
        # and on a tie `mtf_worst_field` reports field 0, which does not exist.
        # A missing/non-integer index is skipped for the same reason: we cannot
        # prove it is NOT the limit, and this collapse must never score a series it
        # cannot identify.  If that leaves nothing, the null path below reports no
        # data -- the honest answer, and the one that keeps the row out of the rank.
        if not isinstance(idx, int) or isinstance(idx, bool) or idx <= 0:
            continue
        ats = s.get("at") if isinstance(s.get("at"), list) else []
        for entry in ats:
            if not isinstance(entry, dict):
                continue
            # NEVER key on series[].label (mislabeled live) — read both orientations.
            for okey in ("tangential", "sagittal"):
                mod = entry.get(okey)
                if _num(mod):
                    if best is None or mod < best:
                        best = mod
                        best_idx = idx
    if best is None:
        note = "frequency beyond MTF grid" if series else "no MTF series"
        return [Reading(column="mtf_worst", raw=None, note=note),
                Reading(column="mtf_worst_field", raw=None, note=note)]
    return [Reading(column="mtf_worst", raw=best, field=best_idx),
            Reading(column="mtf_worst_field", raw=best_idx)]


MTF = MetricAdapter(
    key="mtf", tool_name="get_mtf",
    columns=(ColumnSpec("mtf_worst", "fraction", "number", higher_is_better=True),
             ColumnSpec("mtf_worst_field", "", "index", higher_is_better=None)),
    params_from_basis=_mtf_params, extract=_mtf_extract,
    config_all_capable=False, config_refusal="dispatch", config_scope="active",
    headline_kind="list_collapse", single_config_note="single_config_only")


# ---- strehl (analyze_strehl) -> strehl_axis --------------------------------
def _strehl_params(basis, opts):
    return {}


def _strehl_extract(result):
    return [Reading(column="strehl_axis", raw=result.get("config_headline"))]


STREHL = MetricAdapter(
    key="strehl", tool_name="analyze_strehl",
    columns=(ColumnSpec("strehl_axis", "", "number", higher_is_better=True),),
    params_from_basis=_strehl_params, extract=_strehl_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="config_headline")


# ---- wavefront (analyze_wavefront) -> rms_wfe_worst_waves -------------------
def _wfe_params(basis, opts):
    return {"samp": basis.samp}   # samp>=1 MANDATORY, else the reading silently reads ~0


def _wfe_extract(result):
    return [Reading(column="rms_wfe_worst_waves", raw=result.get("config_headline"))]


WAVEFRONT = MetricAdapter(
    key="wavefront", tool_name="analyze_wavefront",
    columns=(ColumnSpec("rms_wfe_worst_waves", "waves", "number", higher_is_better=False),),
    params_from_basis=_wfe_params, extract=_wfe_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="config_headline")


# ---- distortion (analyze_distortion) -> distortion_max_pct -----------------
def _dist_params(basis, opts):
    return {}


def _dist_extract(result):
    return [Reading(column="distortion_max_pct",
                    raw=result.get("max_distortion_percent"))]


DISTORTION = MetricAdapter(
    key="distortion", tool_name="analyze_distortion",
    columns=(ColumnSpec("distortion_max_pct", "pct", "number", higher_is_better=False),),
    params_from_basis=_dist_params, extract=_dist_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="headline_dict")


# ---- rel_illum (analyze_relative_illumination) -> rel_illum_min ------------
def _ri_params(basis, opts):
    return {}


def _ri_extract(result):
    return [Reading(column="rel_illum_min",
                    raw=result.get("min_relative_illumination"))]


REL_ILLUM = MetricAdapter(
    key="rel_illum", tool_name="analyze_relative_illumination",
    columns=(ColumnSpec("rel_illum_min", "fraction", "number", higher_is_better=True),),
    params_from_basis=_ri_params, extract=_ri_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="headline_dict")


# ---- lateral_color (analyze_lateral_color) -> lateral_color_max_um ---------
def _lat_params(basis, opts):
    return {}


def _lat_extract(result):
    # NEVER read native_lacl_um (disclosed ~3.5x different convention).
    return [Reading(column="lateral_color_max_um",
                    raw=result.get("max_lateral_color_um"))]


LATERAL_COLOR = MetricAdapter(
    key="lateral_color", tool_name="analyze_lateral_color",
    columns=(ColumnSpec("lateral_color_max_um", "um", "number", higher_is_better=False),),
    params_from_basis=_lat_params, extract=_lat_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="headline_dict")


# ---- axial_color (analyze_axial_color) -> axial_color_fc_mm ----------------
def _ax_params(basis, opts):
    return {}


def _ax_extract(result):
    return [Reading(column="axial_color_fc_mm",
                    raw=result.get("f_minus_c_shift_mm"),
                    suspicious=bool(result.get("suspicious")))]


AXIAL_COLOR = MetricAdapter(
    key="axial_color", tool_name="analyze_axial_color",
    columns=(ColumnSpec("axial_color_fc_mm", "mm", "number", higher_is_better=False),),
    params_from_basis=_ax_params, extract=_ax_extract,
    config_all_capable=False, config_refusal="result", config_scope="active",
    headline_kind="flat_scalar")


# ---- aspheric_profile (analyze_aspheric_profile) -> departure/slope/bfs ----
def _asph_params(basis, opts):
    return {"surface": opts.get("asphere_surface")}


def _asph_susp(result, key):
    rd = result.get("readings")
    if isinstance(rd, dict) and isinstance(rd.get(key), dict):
        return bool(rd[key].get("suspicious"))
    return False


def _asph_extract(result):
    return [Reading(column="asphere_max_departure",
                    raw=_get_headline(result, "max_departure"),
                    suspicious=_asph_susp(result, "max_departure")),
            Reading(column="asphere_slope_diff",
                    raw=_get_headline(result, "slope_difference"),
                    suspicious=_asph_susp(result, "slope_difference")),
            Reading(column="asphere_bfs_radius",
                    raw=_get_headline(result, "best_fit_radius"),
                    suspicious=_asph_susp(result, "best_fit_radius"))]


ASPHERIC_PROFILE = MetricAdapter(
    key="aspheric_profile", tool_name="analyze_aspheric_profile",
    columns=(ColumnSpec("asphere_max_departure", "mm", "number", higher_is_better=False),
             ColumnSpec("asphere_slope_diff", "", "number", higher_is_better=False),
             ColumnSpec("asphere_bfs_radius", "mm", "number", higher_is_better=None)),
    params_from_basis=_asph_params, extract=_asph_extract,
    config_all_capable=False, config_refusal="ignore", config_scope="active",
    headline_kind="headline_dict")


# ---- clearance (check_clearance) -> status / n_violations / min_gap --------
def _clr_params(basis, opts):
    return {}


def _clr_min_gap(result):
    gaps = result.get("gaps")
    gaps = gaps if isinstance(gaps, list) else []
    best = None
    best_surf = None
    for g in gaps:
        if not isinstance(g, dict):
            continue
        for key in ("center_thickness", "edge_thickness"):
            v = g.get(key)
            if _num(v) and (best is None or v < best):
                best = v
                best_surf = g.get("surface")
    return best, best_surf


def _clr_n_violations(result):
    viol = result.get("violations")
    if viol is None:
        return None
    return len(viol) if isinstance(viol, list) else None


def _clr_extract(result):
    """Single-config readings: (per-config verdict, count, min-gap). ``profile``
    OWNS the worst_over_configs collapse + the shape-aware status (D4)."""
    verdict, _summary = _classify_clearance(result)
    status_label = "ok" if verdict == "clean" else verdict
    ngap = _clr_n_violations(result)
    mgap, surf = _clr_min_gap(result)
    return [Reading(column="clearance_status", raw=status_label),
            Reading(column="n_clearance_violations", raw=ngap),
            Reading(column="min_gap_mm", raw=mgap, field=surf)]


CLEARANCE = MetricAdapter(
    key="clearance", tool_name="check_clearance",
    columns=(ColumnSpec("clearance_status", "enum", "enum", higher_is_better=None),
             ColumnSpec("n_clearance_violations", "count", "count", higher_is_better=False),
             ColumnSpec("min_gap_mm", "mm", "number", higher_is_better=True)),
    params_from_basis=_clr_params, extract=_clr_extract,
    config_all_capable=True, config_refusal="n/a",
    config_scope="worst_over_configs", headline_kind="list_collapse")


# ---- collimation (verify_collimation) -> verdict / worst_residual ----------
def _coll_params(basis, opts):
    return {}


def _coll_extract(result):
    pf = result.get("per_field")
    wf = None
    if isinstance(pf, list) and pf:
        worst = max((p for p in pf if isinstance(p, dict)),
                    key=lambda p: (p.get("rms_angular_residual_mrad")
                                   if _num(p.get("rms_angular_residual_mrad"))
                                   else float("-inf")),
                    default=None)
        if isinstance(worst, dict):
            wf = worst.get("field_index")
    return [Reading(column="collimation_verdict", raw=result.get("verdict")),
            Reading(column="worst_residual_mrad",
                    raw=result.get("worst_field_residual_mrad"), field=wf)]


COLLIMATION = MetricAdapter(
    key="collimation", tool_name="verify_collimation",
    columns=(ColumnSpec("collimation_verdict", "enum", "enum", higher_is_better=None),
             ColumnSpec("worst_residual_mrad", "mrad", "number", higher_is_better=False)),
    params_from_basis=_coll_params, extract=_coll_extract,
    config_all_capable=True, config_refusal="n/a", config_scope="active",
    headline_kind="flat_scalar")


# --------------------------------------------------------------------------- #
# The registry + the D10 tool-name alias map.
# --------------------------------------------------------------------------- #
REGISTRY = {a.key: a for a in (
    FIRST_ORDER, RMS_SPOT, MTF, STREHL, WAVEFRONT, DISTORTION, REL_ILLUM,
    LATERAL_COLOR, AXIAL_COLOR, ASPHERIC_PROFILE, CLEARANCE, COLLIMATION,
)}

# 1:1 tool_name -> canonical key alias (D10): the template validator accepts either.
ALIASES = {a.tool_name: a.key for a in REGISTRY.values()}

# The set of enum allow-lists (label columns) — a foreign token reads EMPTY.
_ENUM_ALLOWED = {
    "clearance_status": frozenset({"ok", "thin", "indeterminate"}),
    "collimation_verdict": frozenset(
        {"collimated", "not_collimated", "collimation_indeterminate"}),
}

# Afocal-quarantine keys (D3): a null config_headline + an afocal signal -> not_applicable.
_AFOCAL_QUARANTINE = frozenset({"strehl", "wavefront"})


def resolve_key(key):
    """Canonical key for a template entry (accepts the tool_name alias, D10). None = unknown.

    R6 (the never-raise CLASS, fixed at the SOURCE): an UNHASHABLE metric key — a JSON
    array/object element in ``template['metrics']`` (e.g. ``[{}]`` / ``[[]]``), reachable
    via an ARBITRARY external template — makes ``key in REGISTRY`` (and ``ALIASES.get``)
    raise ``TypeError: unhashable type``. Treat it as UNKNOWN (``None``) rather than raise,
    so every caller's never-raise contract holds over ANY input without a per-caller guard:
    ``compute_ranking`` (via ``_default_axes``) stays honest to its "never raises" docstring,
    ``_unknown_metrics`` discloses it as unknown, and ``profile`` stamps a tool_error cell.
    ``isinstance(str)`` is too narrow (an ``int``/``bool`` key is hashable + legitimately
    resolvable-or-unknown); the guarded membership test is exact — hashable keys are
    byte-identical to the prior behavior, only an unhashable key changes (raise -> None)."""
    try:
        if key in REGISTRY:
            return key
        return ALIASES.get(key)
    except TypeError:
        return None


# --------------------------------------------------------------------------- #
# profile() — the D0 seam (dispatch -> reading_ok -> extract -> coerce).
# --------------------------------------------------------------------------- #
def _coerce_reading(adapter, reading, *, env_ok=True, result_ok=True, applicable=True,
                    flags=None):
    """Route ONE Reading through the numeric/label coercer per its ColumnSpec.kind."""
    spec = next((c for c in adapter.columns if c.name == reading.column), None)
    kind = spec.kind if spec is not None else "number"
    app = applicable and reading.applicable
    if kind == "number" or kind == "count":
        value, status = M.coerce_metric(
            reading.raw, env_ok=env_ok, result_ok=result_ok,
            suspicious=reading.suspicious, valid=reading.valid, applicable=app)
    else:  # enum | index | string
        allowed = _ENUM_ALLOWED.get(reading.column)
        value, status = M.coerce_label(
            reading.raw, env_ok=env_ok, result_ok=result_ok, applicable=app,
            allowed=allowed)
    return _cell(value, status, reason=reading.note or "", field=reading.field,
                 raw=reading.raw, flags=flags)


def _stamp_all(adapter, status, reason, *, applicable=None):
    """A rejected reading_ok stamps ``(None, status)`` on EVERY column (no extract)."""
    out = {}
    for spec in adapter.columns:
        out[spec.name] = _cell(None, status, reason=reason or "")
    return out


def stamp_selected(selected_keys, status, reason):
    """Stamp EVERY column of the selected metrics ``(None, status, reason)`` WITHOUT a
    dispatch — the bench's quarantine path, used where an unproven field basis means
    no measurement may be taken at all.

    Reuses ``_stamp_all``/``_cell`` so the cell shape has exactly ONE constructor,
    never a duplicated second one that could drift out of sync; an unknown key
    mirrors ``profile``'s unknown-key branch (a single column named for the raw key,
    matching ``_selected_columns``). Pure; never raises."""
    cells = {}
    for raw_key in selected_keys or ():
        key = resolve_key(raw_key)
        if key is None:
            cells[str(raw_key)] = _cell(None, status, reason=reason or "")
            continue
        cells.update(_stamp_all(REGISTRY[key], status, reason))
    return cells


def _afocal_detected(result, ctx):
    if ctx.get("afocal"):
        return True
    for f in (result.get("flags") or []):
        t = str(f).lower()
        if "afocal" in t or "collimat" in t:
            return True
    return False


def _apply_afocal_quarantine(adapter, cells, result, ctx):
    """D3: for strehl/wavefront, a NULL cell + an afocal signal -> not_applicable."""
    if adapter.key not in _AFOCAL_QUARANTINE:
        return
    if result.get("config_headline") is not None:
        return
    if not _afocal_detected(result, ctx):
        return
    for spec in adapter.columns:
        cell = cells.get(spec.name)
        if cell is not None and cell["status"] == M.STATUS_NULL:
            cell["value"] = None
            cell["status"] = M.STATUS_NOT_APPLICABLE
            cell["reason"] = "metric_not_applicable (afocal)"


def _active_config_entry(per_config, active_config):
    """Pick the ACTIVE config's per_config entry (D4). One entry -> that entry.
    No match -> None (fail-closed to unverified)."""
    if not per_config:
        return None
    if len(per_config) == 1:
        return per_config[0]
    for pc in per_config:
        if isinstance(pc, dict) and pc.get("config") == active_config:
            return pc
    return None


def _profile_single(adapter, result, ctx):
    """Extract + coerce one SINGLE-config result (scope 'active', non-multi path)."""
    flags = result.get("flags")
    cells = {}
    for reading in adapter.extract(result):
        cells[reading.column] = _coerce_reading(adapter, reading, flags=flags)
    _apply_afocal_quarantine(adapter, cells, result, ctx)
    return cells


def _profile_clearance(adapter, result, ctx):
    """The worst_over_configs clearance collapse (D4) — status via the shared
    shape-aware classifier, n_violations SUM, min_gap MIN, coverage fail-closed."""
    verdict, _summary = _classify_clearance(result)   # handles the nested wrapper shape too
    status_label = "ok" if verdict == "clean" else verdict
    cov_ok, per_config, cov_reason = M.config_sweep_ok(result)

    # Collapse n_violations (SUM) + min_gap (MIN) over configs.
    total_viol = 0
    any_viol_readable = False
    min_gap = None
    min_surf = None
    per_cfg_flags = result.get("flags")
    for pc in per_config:
        if not isinstance(pc, dict):
            continue
        nv = _clr_n_violations(pc)
        if nv is not None:
            total_viol += nv
            any_viol_readable = True
        mg, surf = _clr_min_gap(pc)
        if mg is not None and (min_gap is None or mg < min_gap):
            min_gap = mg
            min_surf = surf

    # clearance_status is a real label read fine (value = the verdict); it already
    # reads "indeterminate" when coverage is incomplete (the classifier is fail-closed).
    status_cell = _cell(status_label, M.STATUS_OK, field=None, raw=status_label,
                        flags=per_cfg_flags)

    nviol_raw = total_viol if any_viol_readable else None
    nviol_val, nviol_status = M.coerce_metric(nviol_raw)
    mgap_val, mgap_status = M.coerce_metric(min_gap)

    cells = {
        "clearance_status": status_cell,
        "n_clearance_violations": _cell(nviol_val, nviol_status, raw=nviol_raw,
                                        flags=per_cfg_flags),
        "min_gap_mm": _cell(mgap_val, mgap_status, field=min_surf, raw=min_gap,
                            flags=per_cfg_flags),
    }
    if not cov_ok:
        # D4: coverage incomplete -> numeric cells UNVERIFIED (value kept where present);
        # the clearance enum already reads "indeterminate" via the fail-closed classifier.
        for name in ("n_clearance_violations", "min_gap_mm"):
            c = cells[name]
            c["status"] = M.STATUS_UNVERIFIED
            c["reason"] = cov_reason or "coverage_incomplete"
    return cells


def profile(dispatcher, selected_keys, basis, ctx):
    """Dispatch + gate + extract + coerce the selected metrics (D0).

    Returns ``dict[column_name, MetricCell]``. ``ctx`` = ``{flags, n_configs,
    active_config, afocal, opts:{asphere_surface}}``. NEVER raises the harness envelope
    contract past its own guards (the dispatcher never raises; each metric read is gated
    by ``reading_ok``).
    """
    opts = ctx.get("opts") or {}
    n_configs = ctx.get("n_configs") or 1
    active_config = ctx.get("active_config")
    # D4/D5 manifest disclosure sink (FIX-4): per config-swept metric -> its coverage
    # summary. ``bench.py`` reads it into the per-design manifest block; a None sink
    # (a direct profile() caller) disables the recording.
    sweep_sink = ctx.get("config_sweep")
    cells = {}

    for raw_key in selected_keys:
        key = resolve_key(raw_key)
        if key is None:
            # Unknown metric key -> a tool_error cell (never a KeyError, D10).
            cells[str(raw_key)] = _cell(None, M.STATUS_TOOL_ERROR,
                                        reason=f"unknown_metric:{raw_key}")
            continue
        adapter = REGISTRY[key]

        # aspheric_profile with no resolved asphere surface -> stamp
        # not_applicable for its columns WITHOUT a dispatch.
        if key == "aspheric_profile" and opts.get("asphere_surface") is None:
            cells.update(_stamp_all(adapter, M.STATUS_NOT_APPLICABLE,
                                    "no aspheric surface"))
            continue

        params = adapter.params_from_basis(basis, opts)
        want_all = n_configs > 1 and adapter.config_all_capable
        if want_all:
            params = dict(params)
            params["config"] = "all"

        env = dispatcher.dispatch(adapter.tool_name, params)
        accepted, hint, reason = M.reading_ok(env)
        if not accepted:
            cells.update(_stamp_all(adapter, hint, reason))
            continue
        result = env["result"]

        # Record the config="all" coverage for the manifest (D4/D5, FIX-4) — every
        # metric that was actually config-swept (both worst_over_configs clearance and
        # the image-quality active-config metrics).
        if want_all and isinstance(sweep_sink, dict):
            sweep_sink[adapter.key] = _config_coverage_summary(result)

        if adapter.config_scope == "worst_over_configs":
            cells.update(_profile_clearance(adapter, result, ctx))
            continue

        if want_all:
            # Active-config cell for image-quality metrics (D4).
            cov_ok, per_config, cov_reason = M.config_sweep_ok(result)
            entry = _active_config_entry(per_config, active_config)
            if entry is None:
                cells.update(_stamp_all(adapter, M.STATUS_UNVERIFIED,
                                        "active_config_not_in_sweep"))
                continue
            single = _profile_single(adapter, entry, ctx)
            if not cov_ok:
                for name, c in single.items():
                    # Value kept where present; status -> unverified (D4).
                    c["status"] = M.STATUS_UNVERIFIED
                    c["reason"] = cov_reason or "coverage_incomplete"
            cells.update(single)
        else:
            cells.update(_profile_single(adapter, result, ctx))

    return cells


__all__ = [
    "ColumnSpec", "Reading", "MetricAdapter", "REGISTRY", "ALIASES", "resolve_key",
    "profile", "FIRST_ORDER", "RMS_SPOT", "MTF", "STREHL", "WAVEFRONT", "DISTORTION",
    "REL_ILLUM", "LATERAL_COLOR", "AXIAL_COLOR", "ASPHERIC_PROFILE", "CLEARANCE",
    "COLLIMATION", "stamp_selected",
]
