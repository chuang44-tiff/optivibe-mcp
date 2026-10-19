"""tools/analysis_field_curv.py -- ``render_field_curvature_distortion``: field curvature / F-Tan(theta)
distortion, drawn AND returned.

The data are the native window's TYPED per-wave series
(``New_FieldCurvatureAndDistortion`` -> ``GetDataSeries(i)`` -> ``XData`` / ``YData``, five labelled columns);
NOTHING is written to the design. S/T and the real height are cross-checked against FCGS/FCGT/REAY at every
unvignetted field. A missing point is JSON ``null`` in the envelope and ``math.nan`` inside the
drawn array, never 0.0. Configuration identity is the fan's ``_config_window`` (imported, unchanged).
Raises ``ToolParamError`` pre-engine and re-raises a transport loss; every other fault is a returned refusal.
"""
import math
import os
import re

from .._io import safe_exc, safe_repr
from ..errors import AnalysisResultError, ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _plot_style as ps
from ._field_curv_plot import build_fcd_figure, fcd_model
from ._measurement_common import image_surface_index, read_operand_slots, suspicious_sentinel
from ._optimize_common import _base_token
from ._plot_publish import publish, write_data_sidecar
from ._solve_trace import _primary_wave
from .analysis_mtf_field import (FIELD_TYPES, _Refuse, _confirm_engine_alive, _field_type_token,
                                 _read_fields_strict, _read_wavelengths, _refuse_non_png_extension,
                                 _resolve_png_path, _transport_loss)
from .analysis_ray_fan import _attach_window_disclosure, _config_window, _resolve_config

TOOL = "render_field_curvature_distortion"
STEM = "field_curvature"
UNPROVEN = "field_geometry_unproven"
MALFORMED = "analysis_malformed"
CLOSE_FAILED = "analysis_window_close_failed"
LABELS = ["Tan Shift", "Sag Shift", "Real Height", "Ref. Height", "Distortion"]
#: (setting, enum type name | None for a bool, member name) -- the four load-bearing writes
WRITES = (("IgnoreVignette", None, True), ("Distortion", "Distortions", "F_TanTheta"),
          ("DisplayAs", "DisplayAsTypes", "Percent"), ("ScanType", "FieldScanDirections", "Plus_Y"))
SERIES = ("tan_shift", "sag_shift", "real_height", "ref_height", "distortion")
TOL_BIND_UM = 1e-6 + 1e-12
TOL_EXACT, TOL_INTERP = 1e-6, 1e-4
_DESC_RE = re.compile(r"Data for wavelength\s*:\s*([0-9]+(?:\.[0-9]+)?)")


def _binds(printed, um):
    """The ONE bind window: a window description printing ``printed`` um attributes to a table wavelength ``um``.
    Used by the series bind check AND the ambiguity guard, so the two can never drift."""
    return abs(printed - um) <= TOL_BIND_UM

def _int_text(v):
    """An exact int as text without the 4300-digit conversion limit: absurd magnitudes render as a bit length."""
    return str(v) if v.bit_length() <= 64 else "a %d-bit integer" % v.bit_length()


def _validate_waves(waves):
    """The shape of ``waves`` (pre-engine): ``None`` (every wave) or a non-empty ``list`` of distinct EXACT built-in
    ints >= 1. A bool, any int / list subclass, a float, a string, a duplicate is refused ``ToolParamError``. The
    upper bound needs the table: ``_select_waves``. Nothing here formats a caller object beyond its type name."""
    if waves is None:
        return None
    if type(waves) is not list:
        raise ToolParamError("waves must be an array of wavelength numbers (1-based table indices), got %s"
                             % type(waves).__name__)
    if not waves:
        raise ToolParamError("waves must be a non-empty array of wavelength numbers; omit it to draw every wave")
    seen = set()
    for i, v in enumerate(waves):
        if type(v) is not int:
            raise ToolParamError("waves[%d] must be an exact integer wavelength number, got %s"
                                 % (i, type(v).__name__))
        if v < 1:
            raise ToolParamError("waves[%d] = %s: wavelength numbers start at 1" % (i, _int_text(v)))
        if v in seen:
            raise ToolParamError("waves[%d] repeats wavelength number %s" % (i, _int_text(v)))
        seen.add(v)
    return list(waves)


def _select_waves(geo, requested):
    """Positions (in TABLE order) of the drawn waves. A number outside the table is a ``tool_param`` refusal raised
    on the module's own ``_Refuse`` path, so the config-window restore disclosure and the module flags ride it."""
    table = [w["index"] for w in geo["waves"]]
    if requested is None:
        return list(range(len(table)))
    bad = [v for v in requested if v not in table]
    if bad:
        raise _Refuse("tool_param", "waves: wavelength number %s is not in the design's wavelength table "
                      "(valid 1..%d)" % (_int_text(bad[0]), len(table)))
    return [i for i, n in enumerate(table) if n in requested]


DESCRIPTION = (
    "FIELD CURVATURE and F-Tan(theta) DISTORTION of the active design, drawn as a PNG AND returned as numbers. "
    "Two panels over the field: sagittal (dotted) / tangential (solid) focus shift per wavelength in the lens "
    "unit, and distortion in percent. Read from the native analysis window's typed series (101 samples) and "
    "cross-checked against the FCGS/FCGT/REAY operands at every unvignetted field. Params: config (optional "
    "integer k to evaluate configuration k, restored afterwards; 'all' is refused), waves (optional array of "
    "wavelength NUMBERS, 1-based table indices: only those waves are drawn AND returned and the scale is fitted "
    "to them; default every wave; the title values stay primary-wave and the primary is always traced even when "
    "not drawn; waves_selected discloses the selection), path (a .png file, or a "
    "folder to mint into; default <workspace_root>/evaluation). Returns the full S/T/real/reference-height/"
    "distortion tables (null = a point the engine did not give, NEVER 0; null_runs says why), the field axis "
    "(degrees, or image height when the design defines it so; the 89-degree cap of a fisheye and the "
    "RealImageHeight conversion are declared), the distortion focal length (angle axes) or reference slope "
    "(height axes), both scales, title values and every drawn string. Gotcha: on a zero field range the "
    "distortion panel is empty by design and the titles are null (on-axis shifts are given). Pure read: "
    "nothing is written to the design. PNG minted in <workspace_root>/evaluation/field_curvature_NNNN.png "
    "(no overwrite). Also writes the full returned envelope, unchanged, as strict JSON to <png stem>.data.json beside the PNG: its path is data_path (null plus a data_sidecar_failed:<reason> flag if that write failed; a refusal writes none). Not the reviewable figure: save_candidate's png_sha256 PNG is."
)


# --------------------------------------------------------------------------- #
# Geometry (NOT the fan's: the fan's pupil-remap refusal on VCX/VCY >= 1 does not apply here)
# --------------------------------------------------------------------------- #
def _geometry(system):
    try:
        field_type = _field_type_token(system)
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse("field_read_failed", f"the field type could not be read: {safe_exc(exc, repr_form=True)}") from None
    if field_type not in FIELD_TYPES:
        raise _Refuse("field_unsupported", f"field type {field_type!r} not in {list(FIELD_TYPES)}")
    fields = _read_fields_strict(system)
    primary, pfault = _primary_wave(system)
    image_surf = image_surface_index(system)
    if primary is None or image_surf is None:
        _confirm_engine_alive(system)
        raise _Refuse(UNPROVEN, f"primary wavelength: {pfault}; image surface: {image_surf}")
    waves, wfault = _read_wavelengths(system)
    if waves is None:
        raise _Refuse("wavelengths_unread", str(wfault))
    geo_flags = []
    for w in waves:
        if not ps._finite(w["um"]):
            raise _Refuse("wavelengths_unread", "wavelength %d value is not finite (%r)" % (w["index"], w["um"]))
        if not ps._finite(w["weight"]):
            w["weight"] = None
            geo_flags.append("wavelength_weight_non_finite:%d" % w["index"])
    token, label, factor = _ac._lens_unit_and_um_factor(system)
    if factor is None:
        _confirm_engine_alive(system)
        raise _Refuse(_ac.ANALYSIS_UNITS_UNCONVERTIBLE_FAMILY,
                      f"the lens unit reads {token!r}: no proven factor to micrometres, so no number is served")
    ambiguous = [(a["index"], b["index"]) for a in waves for b in waves
                 if a is not b and _binds(float("%.6f" % b["um"]), a["um"])]
    if ambiguous:
        i, j = ambiguous[0]
        raise _Refuse(MALFORMED, f"wavelengths {i} and {j} cannot be told apart at 6 decimals within the 1e-6 "
                      "bind window; the series cannot be attributed")
    return {"field_type": field_type, "fields": fields, "primary": primary, "image_surf": image_surf,
            "waves": waves, "unit_token": token, "unit_label": label, "um": factor, "flags": geo_flags}


# --------------------------------------------------------------------------- #
# The native window
# --------------------------------------------------------------------------- #
def _num(v):
    """A finite float, or ``None`` (the ``safe_float`` string sentinels and any non-number are ``null``)."""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None


def _write_settings(typed):
    """Write the four values; return nothing. A member that cannot be resolved is 'the write did not take'."""
    for name, enum_name, member in WRITES:
        try:
            value = member if enum_name is None else getattr(type(getattr(typed, name)), member)
            setattr(typed, name, value)
        except Exception as exc:  # noqa: BLE001
            t = _transport_loss(exc)
            if t is not None:
                raise t
            raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY, f"setting {name} could not be written as "
                          f"{member!r}: {safe_exc(exc, repr_form=True)}") from None


def _read_back(typed):
    out = {}
    for name, enum_name, member in WRITES:
        try:
            got = getattr(typed, name)
            out[name] = got if enum_name is None else _base_token(got)
        except Exception as exc:  # noqa: BLE001
            t = _transport_loss(exc)
            if t is not None:
                raise t
            out[name] = None
        want = member
        if not (out[name] == want and type(out[name]) is type(want)):
            raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY, f"post-run {name} reads {safe_repr(out[name])}, "
                          f"expected {want!r} (it was written before the run): no series accepted")
    return out


def _read_series(results, waves):
    """The five columns of every series, bound by index and confirmed by um; common X grid and label."""
    n = int(results.NumberOfDataSeries)
    if n != len(waves):
        raise _Refuse(MALFORMED, f"{n} data series for {len(waves)} wavelengths")
    out, x0, label0 = [], None, None
    for i in range(n):
        ds = results.GetDataSeries(i)
        w = waves[i]
        try:
            labels = list(ds.SeriesLabels)
            xlabel = _base_token(ds.XLabel).strip()
            desc = _base_token(ds.Description)
            ydata = ds.YData.Data
            cols, rows = int(ydata.GetLength(1)), int(ydata.GetLength(0))
            x = _ac._marshal_array(ds.XData.Data)
        except AnalysisResultError as exc:
            raise _Refuse(MALFORMED, f"series {i}: {safe_exc(exc)}") from None
        if labels != LABELS or cols != len(LABELS):
            raise _Refuse(MALFORMED, f"series {i}: SeriesLabels {labels!r} with {cols} columns, expected {LABELS!r}: "
                          "the column order cannot be proven")
        if rows != len(x):
            raise _Refuse(MALFORMED, f"series {i}: {rows} data rows for {len(x)} field samples")
        xs = [_num(v) for v in x]
        if not xs:
            raise _Refuse(MALFORMED, f"series {i}: no field samples")
        if any(v is None for v in xs) or (not all(v == 0.0 for v in xs) and
                                          any(b <= a for a, b in zip(xs, xs[1:]))):
            raise _Refuse(MALFORMED, f"series {i}: X is not finite and strictly increasing (or all zero)")
        m = _DESC_RE.search(desc)
        if not m or not _binds(float(m.group(1)), w["um"]):
            raise _Refuse(MALFORMED, f"series {i} reads {desc!r}, but wavelength {w['index']} is "
                          f"{w['um']!r} um: the series cannot be attributed")
        if x0 is None:
            x0, label0 = xs, xlabel
        elif xs != x0 or xlabel != label0:
            raise _Refuse(MALFORMED, f"series {i}: its X grid or label differs from series 0")
        grid = _ac._marshal_2d(ydata)
        out.append(dict(index=w["index"], um=w["um"],
                        **{s: [_num(r[c]) for r in grid] for c, s in enumerate(SERIES)}))
    return {"x": x0, "xlabel": label0, "series": out}


def _run_fcd_window(system, waves):
    """Own window; settings written + read back AFTER the run; ``Close()`` attempted on every exit.
    A close failure that is not a transport loss is the flag ``analysis_window_close_failed``: appended
    to the refusal in flight, or returned on success. Closure is never claimed as proven."""
    try:
        analysis = system.Analyses.New_FieldCurvatureAndDistortion()
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse(MALFORMED, f"the field curvature window could not be opened: {safe_exc(exc, repr_form=True)}") from None
    pending, acq = None, None
    try:
        typed = getattr(analysis.GetSettings(), "__implementation__", None)
        if typed is None:
            raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY, "the typed settings view is unavailable")
        _write_settings(typed)
        analysis.ApplyAndWaitForCompletion()
        read_back = _read_back(typed)
        results = analysis.GetResults()
        if results is None:
            raise _Refuse("analysis_empty", "the field curvature window returned no results object")
        acq = _read_series(results, waves)
        acq["settings_read_back"] = read_back
    except _Refuse as r:
        pending = r
    except Exception as exc:  # noqa: BLE001
        pending = _transport_loss(exc) or _Refuse(MALFORMED, "the field curvature engine call raised "
                                                  f"{safe_exc(exc, repr_form=True)}")
    closed, flags = True, []
    try:
        analysis.Close()
    except Exception as exc:  # noqa: BLE001
        closed = False
        if _transport_loss(exc) is None:
            flags.append(CLOSE_FAILED + ": " + safe_exc(exc, repr_form=True))
    if pending is not None:
        if isinstance(pending, _Refuse) and flags:
            pending.extra["flags"] = list(pending.extra.get("flags") or []) + flags
        raise pending
    acq["settings_read_back"]["window_closed"] = closed
    acq["flags"] = flags
    return acq


# --------------------------------------------------------------------------- #
# Derived quantities; every value is rounded ONCE here
# --------------------------------------------------------------------------- #
def _axis(acq, geo):
    kind = {"Y Angle (deg)": "angle", "Y Height": "image_height"}.get(acq["xlabel"])
    if kind is None:
        raise _Refuse(MALFORMED, f"the field axis label {acq['xlabel']!r} is neither 'Y Angle (deg)' nor 'Y Height'")
    expected = {"Angle": "angle", "RealImageHeight": "angle", "ParaxialImageHeight": "image_height"}.get(
        geo["field_type"])
    if expected is not None and expected != kind:
        raise _Refuse(MALFORMED, f"a {geo['field_type']} design reads a {kind} axis ({acq['xlabel']!r}): "
                      "the axis kind disagrees with the field type")
    x = acq["x"]
    ymax = max(abs(f["y"]) for f in geo["fields"])
    zero = all(v == 0.0 for v in x)
    converted = "RealImageHeight" if geo["field_type"] == "RealImageHeight" and kind == "angle" else None
    capped = x[-1] if geo["field_type"] == "Angle" and kind == "angle" and not zero and x[-1] < 0.999 * ymax else None
    return {"quantity": kind, "unit": "deg" if kind == "angle" else geo["unit_label"], "max": x[-1],
            "samples": list(x), "zero_range": zero, "native_converted_from": converted,
            "capped_at_deg": capped, "ymax": ymax}


def _reasons(raw):
    return ["native_non_finite" if v is None else None for v in raw]


def _derive_wave(s, axis):
    """Canonical (rounded-once) tables and null reasons of one wave."""
    x, zero = axis["samples"], axis["zero_range"]
    t = {k: ps.round_table(s[k], ps.ROUND_LENS) for k in ("tan_shift", "sag_shift", "real_height", "ref_height")}
    reasons = {k: _reasons(s[k]) for k in t}
    dist, dreason = [], []
    for j, xj in enumerate(x):
        d, r, h = s["distortion"][j], s["real_height"][j], s["ref_height"][j]
        if zero:
            dist.append(None), dreason.append("zero_field_range")
        elif d is None or r is None or h is None:
            dist.append(None), dreason.append("native_non_finite")
        elif h == 0.0 and xj > 0:
            dist.append(None), dreason.append("distortion_reference_unavailable")
        else:
            dist.append(ps.round_value(d, ps.ROUND_PCT)), dreason.append(None)
    t["distortion"], reasons["distortion"] = dist, dreason
    return t, reasons


def _runs(wave, series, reasons):
    out, i = [], 0
    while i < len(reasons):
        if reasons[i] is None:
            i += 1
            continue
        j = i
        while j + 1 < len(reasons) and reasons[j + 1] == reasons[i]:
            j += 1
        out.append({"wave": wave, "series": series, "j_from": i, "j_to": j, "reason": reasons[i]})
        i = j + 1
    return out


def _finite_or_none(value):
    """``value`` unchanged when finite, else ``None`` (a quotient that overflowed is a missing value, never
    an ``inf`` in the envelope). The value is NOT rounded: the envelope stays byte-identical for finite data."""
    return value if isinstance(value, float) and math.isfinite(value) else None


def _focal(tables, axis, unit_token):
    """``(distortion_focal_length, reference_slope)`` blocks of one wave."""
    x, ref = axis["samples"], tables["ref_height"]
    fd = {"unit": unit_token, "value": None, "basis": None}
    sl = {"unit": "dimensionless", "value": None, "basis": None}
    if axis["zero_range"]:
        fd["basis"] = sl["basis"] = "zero_field_range"
        return fd, sl
    last = next((j for j in range(len(x) - 1, -1, -1) if x[j] > 0 and ps._finite(ref[j])), None)
    if axis["quantity"] == "angle":
        sl["basis"] = "not applicable: angle axis"
        fd["basis"] = "ref_height / tan(theta), native reference"
        if last is not None and math.tan(math.radians(x[last])) not in (0.0,) \
                and math.isfinite(math.tan(math.radians(x[last]))):
            fd["value"] = _finite_or_none(ref[last] / math.tan(math.radians(x[last])))
    else:
        fd["basis"] = ("native's height-field focal-length convention is unmeasured "
                       "(cfg3 header 90.33708 != slope*EFFL 90.310)")
        sl["basis"] = "ref_height / X at the last positive sample, native reference"
        if last is not None:
            sl["value"] = _finite_or_none(ref[last] / x[last])
    return fd, sl


# --------------------------------------------------------------------------- #
# The independent cross-check
# --------------------------------------------------------------------------- #
def _locate_field(f, axis, real):
    """``(x_f, None)`` | ``(None, ("skip", reason))`` | ``(None, ("out", native_max))``."""
    x = axis["samples"]
    if axis["zero_range"]:
        return x[0], None
    if axis["native_converted_from"]:
        if any(v is None for v in real) or any(b <= a for a, b in zip(real, real[1:])):
            return None, ("skip", "real_height_not_monotone")
        tol = TOL_EXACT * max(1.0, abs(real[-1]))
        if f["y"] > real[-1] + tol:
            return None, ("out", real[-1])
        for j, r in enumerate(real):
            if abs(r - f["y"]) <= tol:
                return x[j], None
            if r > f["y"]:
                if j == 0:
                    return None, ("skip", "real_height_does_not_cover_field")
                return x[j - 1] + (x[j] - x[j - 1]) * (f["y"] - real[j - 1]) / (r - real[j - 1]), None
    if f["y"] > x[-1] + 1e-9 * max(1.0, abs(x[-1])):
        return None, ("out", x[-1])
    return f["y"], None


def _at(x, col, x_f):
    """``(value, tolerance)`` of ``col`` at ``x_f``: an exact index, or a linear interpolation."""
    for j, xj in enumerate(x):
        if abs(x_f - xj) <= 1e-9 * max(1.0, abs(x[-1])):
            return (col[j], TOL_EXACT) if col[j] is not None else (None, TOL_EXACT)
    j = next((i for i in range(len(x) - 1) if x[i] < x_f < x[i + 1]), None)
    if j is None or col[j] is None or col[j + 1] is None:
        return None, TOL_INTERP
    t = (x_f - x[j]) / (x[j + 1] - x[j])
    return col[j] + (col[j + 1] - col[j]) * t, _interp_tol(col, j, t)


def _triple_d2(col, a):
    """``|second difference|`` of the triple ``a, a+1, a+2`` when it is a trustworthy smooth stretch, else ``None``
    (a missing / non-finite / overflowing cell, or one that is not locally smooth: ``|d2| > 4 min|d1|``, the MIN
    because a spike at an end cell inflates BOTH first differences)."""
    cells = col[a:a + 3]
    if len(cells) < 3 or any(v is None for v in cells):
        return None
    d2 = abs(cells[0] - 2 * cells[1] + cells[2])
    d1 = min(abs(cells[1] - cells[0]), abs(cells[2] - cells[1]))
    return d2 if math.isfinite(d2) and d2 <= 4 * d1 else None


def _interp_tol(col, j, t):
    """``TOL_INTERP`` plus an ESTIMATE of the linear-interpolation truncation error ``t(1-t)/2 * |second difference|``
    in the interval ``j``. The estimate takes the MAX over BOTH bracketing triples (backward ``j-1, j, j+1`` and
    forward ``j, j+1, j+2``; only the one that exists at either end): on an increasing-curvature column the backward
    triple under-reads and the forward one over-reads. Never tighter than ``TOL_INTERP``,
    and ALL-OR-NOTHING: if ANY existing triple is missing a cell, non-finite, or not locally smooth (a wild
    neighbour) the whole estimate falls back to the floor, so a bad cell on either side cannot loosen the bound."""
    starts = ([j - 1] if j >= 1 else []) + ([j] if j + 2 < len(col) else [])
    d2s = [_triple_d2(col, a) for a in starts]
    if not d2s or any(d is None for d in d2s):
        return TOL_INTERP
    tol = TOL_INTERP + 0.5 * t * (1 - t) * max(d2s)
    return tol if math.isfinite(tol) else TOL_INTERP


def _operand(system, code, slots):
    try:
        raw, bad = read_operand_slots(system, code, slots)
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse(MALFORMED, f"the cross-check operand {code} could not be read: {safe_exc(exc, repr_form=True)}") from None
    return None if bad else float(raw)


def _cross_check(system, geo, axis, primary_tables):
    x, p = axis["samples"], geo["primary"]
    cc = {"rows_checked": 0, "max_abs_diff_shift_lens_units": None, "max_abs_diff_real_height_lens_units": None,
          "skipped_fields": [], "out_of_domain_fields": []}
    for f in geo["fields"]:
        if f["vdx"] or f["vdy"] or f["vcx"] or f["vcy"]:
            cc["skipped_fields"].append({"index": f["index"], "reason": "vignetted"})
            continue
        if geo["field_type"] == "ObjectHeight":
            cc["skipped_fields"].append({"index": f["index"], "reason": "object_height_locate_unmeasured"})
            continue
        x_f, odd = _locate_field(f, axis, primary_tables["real_height"])
        if odd and odd[0] == "skip":
            cc["skipped_fields"].append({"index": f["index"], "reason": odd[1]})
            continue
        if odd:
            cc["out_of_domain_fields"].append({"index": f["index"], "value": f["y"], "native_max": odd[1]})
            continue
        hy = f["hy"]
        fcg = {3: ("Wave", p), 4: ("Hx", 0.0), 5: ("Hy", hy)}
        t_op, s_op = _operand(system, "FCGT", fcg), _operand(system, "FCGS", fcg)
        y_op = _operand(system, "REAY", {2: ("Surf", geo["image_surf"]), 3: ("Wave", p), 4: ("Hx", 0.0),
                                         5: ("Hy", hy), 6: ("Px", 0.0), 7: ("Py", 0.0)})
        if None in (t_op, s_op, y_op):
            cc["skipped_fields"].append({"index": f["index"], "reason": "operand_sentinel"})
            continue
        real_t = f["y"] if axis["native_converted_from"] else None
        pairs = [("shift", t_op, _at(x, primary_tables["tan_shift"], x_f)),
                 ("shift", s_op, _at(x, primary_tables["sag_shift"], x_f)),
                 ("height", y_op, (real_t, TOL_EXACT) if real_t is not None
                  else _at(x, primary_tables["real_height"], x_f))]
        for kind, op, (val, tol) in pairs:
            if val is None:
                raise _Refuse(MALFORMED, f"field {f['index']}: the window has no finite value at its position "
                              f"{x_f!r}: the series cannot be tied to the live design")
            d = abs(val - op)
            if d > tol:
                raise _Refuse(MALFORMED, f"the window's series disagree with the operand read at field "
                              f"{f['index']} ({kind}: window {val!r}, operand {op!r}, tolerance {tol})")
            key = "max_abs_diff_%s_lens_units" % ("shift" if kind == "shift" else "real_height")
            cc[key] = d if cc[key] is None else max(cc[key], d)
        cc["rows_checked"] += 1
    return cc


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _footer(geo, axis, slope_note, primary_drawn=True):
    lines = ["vignetting factors ignored (native IgnoreVignette, read back)"]
    if axis["capped_at_deg"] is not None:
        lines.append("native F-Tan(θ) table ends at %.1f°; the field extends to %.1f° — S/T and distortion "
                     "above the cap are not available" % (axis["capped_at_deg"], axis["ymax"]))
    if axis["native_converted_from"]:
        lines.append("field axis converted to degrees by the native analysis (fields are defined as real image "
                     "height; the Real Height column is returned in %s)" % geo["unit_label"])
    if axis["zero_range"]:
        lines.append("zero field range: every sample is at 0; distortion is not computed, the titles are null")
    if slope_note is not None:
        lines.append("Reference slope = %.6g (paraxial image height basis)" % slope_note)
    if not primary_drawn:
        lines.append("title values are primary-wave (wave %d, not drawn)" % geo["primary"])
    return lines


def _module_flags(geo, acq, rfault):
    """The flags THIS module owns (geometry, window close, unresolved root): ONE expression for the success path and
    for every refusal raised after ``_geometry`` returned. ``None`` = that stage never completed."""
    return (geo["flags"] if geo else []) + (acq["flags"] if acq else []) + (
        ["workspace_root_unresolved"] if rfault else [])


def _render(session, system, k, path, waves=None):
    target, minted, fault, root, rfault = _resolve_png_path(session, path)
    if fault:
        raise _Refuse("workspace_unlistable", fault)
    geo = acq = None
    try:
        with _config_window(system, k) as win:
            ident = win["ident"]
            geo = _geometry(system)
            sel = _select_waves(geo, waves)
            acq = _run_fcd_window(system, geo["waves"])
            axis = _axis(acq, geo)
            derived = [_derive_wave(s, axis) for s in acq["series"]]
            p_pos = next((i for i, w in enumerate(geo["waves"]) if w["index"] == geo["primary"]), None)
            if p_pos is None:
                raise _Refuse(MALFORMED, "the primary wavelength %r is not among the system's wavelengths"
                              % (geo["primary"],))
            cross = _cross_check(system, geo, axis, derived[p_pos][0])
    except _Refuse as r:
        own = _module_flags(geo, acq, rfault)
        if own:
            r.extra["flags"] = list(r.extra.get("flags") or []) + own
        raise
    try:
        flags = list(win["flags"]) + _module_flags(geo, acq, rfault)
        tables = [t for t, _r in derived]                # EVERY wave: the primary's title values read from here
        drawn = [tables[i] for i in sel]                 # the selection, in table order: scale / curves / returned data
        ft = ps.max_abs(*[[tb[k_] for k_ in ("tan_shift", "sag_shift")] for tb in drawn])
        if ft is None:
            raise _Refuse("analysis_empty", "no finite sagittal or tangential focus shift in any "
                          + ("wavelength" if waves is None else "selected wavelength"), flags=flags)
        try:
            curvature = ps.curvature_scale(ft, geo["unit_token"], geo["unit_label"])
        except ValueError as exc:
            raise _Refuse(MALFORMED, "the field-curvature scale could not be chosen: %s" % exc,
                          flags=flags) from None
        dmax = ps.max_abs(*[[tb["distortion"]] for tb in drawn])
        if axis["zero_range"]:
            dscale, dnote = ps.empty_scale("distortion", "zero_field_range"), "not computed: zero_field_range"
        elif dmax is None:
            dscale, dnote = ps.empty_scale("distortion", "native_non_finite"), "not computed: native_non_finite"
        else:
            try:
                dscale = ps.distortion_scale(dmax)
            except ValueError as exc:
                raise _Refuse(MALFORMED, "the distortion scale could not be chosen: %s" % exc,
                              flags=flags) from None
            dnote = ("F-Tan(theta), native reference" if axis["quantity"] == "angle"
                     else "linear in paraxial image height, native reference")
        scales = {"curvature": curvature, "distortion_pct": dscale}
        flags += list(curvature["flags"]) + (["zero_field_range"] if axis["zero_range"] else []) + (
            ["object_height_axis_unmeasured"] if geo["field_type"] == "ObjectHeight" else []) + (
            ["cross_check_unmeasured"] if not cross["rows_checked"] else [])
        waves_env, runs, model_waves, cycled = {}, [], [], False
        prim_fd, prim_sl = _focal(tables[p_pos], axis, geo["unit_token"])   # primary-wave, drawn or not
        for pos, (w, s, (tb, rs)) in enumerate(zip(geo["waves"], acq["series"], derived)):
            if pos not in sel:
                continue
            fd, sl = _focal(tb, axis, geo["unit_token"])
            lens = {"unit": geo["unit_token"]}
            waves_env["%d" % w["index"]] = {
                **{n: dict(lens, values=tb[n]) for n in ("tan_shift", "sag_shift", "real_height", "ref_height")},
                "distortion": {"unit": "%", "values": tb["distortion"]},
                "distortion_focal_length": fd, "reference_slope": sl}
            for name in SERIES:
                runs += _runs(w["index"], name, rs[name])
            cycled = cycled or ps.wave_colour(w["index"])[2]
            model_waves.append({"index": w["index"], "um": w["um"], "tan_shift": tb["tan_shift"],
                                "sag_shift": tb["sag_shift"], "distortion": tb["distortion"]})
        if cycled:
            flags.append("wave_colour_cycled")
        ptb = tables[p_pos]
        endpoints = lambda col: None if col[0] is None or col[-1] is None or axis["zero_range"] else \
            ps.round_value(col[0] - col[-1], ps.ROUND_LENS)  # noqa: E731
        pdist = [v for v in ptb["distortion"] if v is not None]
        title_values = {"field_curvature_sag": endpoints(ptb["sag_shift"]),
                        "field_curvature_tan": endpoints(ptb["tan_shift"]), "unit": geo["unit_token"],
                        "basis": "primary wave; shift(first sample) - shift(last sample)",
                        "max_distortion_pct": max(abs(v) for v in pdist) if pdist else None,
                        "max_distortion_basis": "primary wave, over the sample list"}
        if axis["zero_range"]:
            title_values["on_axis_shifts"] = {str(w["index"]): {"sag": tb["sag_shift"][0], "tan": tb["tan_shift"][0]}
                                              for w, tb in zip((geo["waves"][i] for i in sel), drawn)}
        k_ev, n_ev = ident["config_evaluated"], ident["n_configs"]
        config_line = ("Configuration %d of %d" % (k_ev, n_ev)) if k_ev is not None and n_ev is not None \
            else "Configuration: unread"
        slope = prim_sl["value"] if prim_fd["value"] is None else None
        model = fcd_model({k_: axis[k_] for k_ in ("quantity", "unit", "samples", "max", "zero_range")},
                          model_waves, scales, title_values, config_line,
                          _footer(geo, axis, slope, primary_drawn=p_pos in sel),
                          geo["unit_token"], geo["unit_label"])
        final, sha, overwrote, pfault, cleanup = None, None, None, None, []
        try:
            final, sha, overwrote, pfault, cleanup = publish(build_fcd_figure, model, target, minted, STEM)
        except Exception as exc:  # noqa: BLE001
            pfault = ("render_failed", safe_exc(exc, repr_form=True))
        if pfault:
            raise _Refuse(pfault[0], pfault[1], flags=flags, partial={"waves": waves_env},
                          **({"cleanup_failures": cleanup} if cleanup else {}),
                          **({"published_path": pfault[2], "overwrote": overwrote} if len(pfault) > 2 and pfault[2] else {}))
        wave_env = [{"index": w["index"], "um": w["um"], "weight": w["weight"], "primary": w["primary"],
                     "colour": ps.wave_colour(w["index"])[0]} for w in (geo["waves"][i] for i in sel)]
        env = {"ok": True, "tool": TOOL, "path": final, "png_sha256": sha, "png_minted": minted,
               "workspace_root": root, "output_dir": target if minted else os.path.dirname(final),
               "config_evaluated": k_ev, "n_configs": n_ev, "config_entry": win["config_entry"],
               "lens_unit": geo["unit_token"], "lens_unit_label": geo["unit_label"],
               "um_per_lens_unit": geo["um"], "field_type": geo["field_type"],
               "primary_wave": {"index": geo["primary"], "um": next(
                   (w["um"] for w in geo["waves"] if w["index"] == geo["primary"]), None)},
               "wavelengths": wave_env,
               "waves_selected": {"requested": None if waves is None else list(waves),
                                  "drawn": [geo["waves"][i]["index"] for i in sel],
                                  "primary": geo["primary"], "primary_drawn": p_pos in sel},
               "field_axis": {"quantity": axis["quantity"], "unit": axis["unit"], "max": axis["max"],
                              "samples": axis["samples"], "native_converted_from": axis["native_converted_from"],
                              "capped_at_deg": axis["capped_at_deg"]},
               "waves": waves_env, "scale": scales, "title_values": title_values, "distortion_basis": dnote,
               "cross_check": cross, "settings_read_back": acq["settings_read_back"], "null_runs": runs,
               "rounding": {"shift_lens_units": 1e-8, "height_lens_units": 1e-8, "distortion_pct": 1e-6},
               "title_block": model["title_lines"], "footer_lines": model["footer_lines"], "flags": flags}
        if win["mutation_warning"]:
            env["mutation_warning"] = win["mutation_warning"]
        if not minted:
            env["overwrote"] = overwrote
        if cleanup:
            env["cleanup_failures"] = cleanup
        write_data_sidecar(env, minted)  # LAST: the sidecar is this exact dict (data_path included)
        return env
    except _Refuse as r:
        _attach_window_disclosure(r, win)
        raise


def render_field_curvature_distortion(session, params):
    """The handler. Raises only ``ToolParamError`` (and a transport loss, unchanged)."""
    path = params.get("path")
    if path is not None:
        if not isinstance(path, str) or not str.strip(path) or "\x00" in path:
            raise ToolParamError(f"path must be a non-empty string without NUL, got {safe_repr(path)}")
        _refuse_non_png_extension(path)
    waves = _validate_waves(params.get("waves"))     # shape only; the upper bound needs the table (_select_waves)
    system = session.system
    try:
        k = _resolve_config(system, params.get("config"), tool=TOOL)
        return _render(session, system, k, path, waves)
    except _Refuse as r:
        return _ac.error_envelope(TOOL, r.family, str(r), **r.extra)


RENDER_FIELD_CURVATURE_DISTORTION_SPEC = ToolSpec(
    name=TOOL,
    handler=render_field_curvature_distortion,
    required_params=(),
    param_types={"config": "number", "path": "string", "waves": "array"},
    description=DESCRIPTION,
)

TOOL_SPECS = (RENDER_FIELD_CURVATURE_DISTORTION_SPEC,)
