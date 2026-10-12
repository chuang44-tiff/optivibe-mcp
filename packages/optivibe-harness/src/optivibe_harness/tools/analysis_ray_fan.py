"""tools/analysis_ray_fan.py — ``render_ray_fan``: the transverse ray fan, drawn AND returned.

Every defined field, every (selected) wavelength, 41 pupil samples per fan, traced through ONE
``trace_rays`` batch per field and bound by ``rayNumber`` (never position). The aberration is
measured against the PRIMARY-wave Py=0 chief of THAT field (never ``_chief_pupil_coords``, never the
own-wave or wave-1 chief). A missing sample is JSON ``null`` in the envelope and ``math.nan`` inside the
drawn array — never 0.0. The tool returns every number it plotted. The DESIGN is read-only; the one
engine write is the optional configuration switch of ``config=k``, whose identity this module owns
(``_config_window``: a strict entry read, an in-window identity check, our own post-exit restore
check). Raises ``ToolParamError`` pre-engine, re-raises a transport loss, and lets the strict config reads' own
``config_unverified`` escape from ``with_configuration.__enter__`` (before any switch); every other fault is a
returned refusal envelope. The optional ``waves`` selects the drawn wavelengths; the primary chief is always traced.
"""
import math
import os
from contextlib import contextmanager

from .._io import safe_exc, safe_repr
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _mce_cells
from . import _plot_style as ps
from . import analysis_raytrace
from ._config_common import _coerce_config_int, resolve_single_config_selector, with_configuration
from ._measurement_common import image_surface_index, suspicious_sentinel
from ._plot_publish import publish, write_data_sidecar
from ._ray_fan_plot import build_fan_figure, fan_model
from ._solve_trace import _primary_wave
from .analysis_mtf_field import (FIELD_TYPES, _Refuse, _confirm_engine_alive, _config_identity,
                                 _field_type_token, _read_fields_strict, _read_wavelengths,
                                 _refuse_non_png_extension, _resolve_png_path, _transport_loss)

TOOL = "render_ray_fan"
STEM = "ray_fan"
N = ps.NUM_PUPIL_SAMPLES
UNPROVEN = "field_geometry_unproven"
CHIEF_FAILED = "chief_ray_failed"
TOOL_PARAM = "tool_param"
ACTIVE_NOT_RESTORED = "active_config_not_restored"

DESCRIPTION = (
    "Transverse RAY FAN of the active design, drawn as a PNG AND returned as numbers. Every defined field x "
    "every wavelength (or the selected waves), tangential (ey vs Py) and sagittal (ex vs Px), 41 pupil samples, image surface, "
    "aberration in um against the PRIMARY-wave chief of each field. Params: config (optional integer k to "
    "evaluate configuration k, restored afterwards; 'all' is refused), waves (optional array of wavelength "
    "NUMBERS, 1-based table indices: only those waves are drawn AND returned and the scale is fitted to them; "
    "default every wave; the primary-wave chief is always traced even when the primary is not drawn; "
    "waves_selected discloses the selection), path (a .png file, or a folder to mint "
    "into; default <workspace_root>/evaluation). Returns the full ey/ex tables (null = a ray that did not "
    "trace, NEVER 0; null_runs says why), x axes (shortened for a vignetted pupil), the scale actually drawn, "
    "wavelength colours and every title/footer string. Gotcha: a drawn sample next to a blocked run is listed "
    "in edge_samples because the native plot may omit it. Pure read; the fan scale is 1-2-5 of 1.1x the data "
    "maximum. PNG minted in <workspace_root>/evaluation/ray_fan_NNNN.png (no overwrite). "
    "Also writes the full returned envelope, unchanged, as strict JSON to <png stem>.data.json beside the PNG: its path is data_path (null plus a data_sidecar_failed:<reason> flag if that write failed; a refusal writes none). Not the reviewable figure: save_candidate's png_sha256 PNG is."
)


# --------------------------------------------------------------------------- #
# Configuration identity — imported unchanged by analysis_field_curv.
# --------------------------------------------------------------------------- #
def _entry_read(system):
    """The STRICT entry identity ``(entry, n)`` — a read fault refuses BEFORE any switch."""
    try:
        return _mce_cells.current_configuration(system), _mce_cells.number_of_configurations(system)
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY, "the active configuration could not be read; "
                      f"nothing was switched ({safe_exc(exc, repr_form=True)})") from None


def _post_exit_restore(system, entry, win):
    """OUR check against the strictly-read ``entry``: one re-set on a mismatch, re-read, then a flag."""
    def read():
        try:
            return _mce_cells.current_configuration(system)
        except Exception as exc:  # noqa: BLE001
            t = _transport_loss(exc)
            if t is not None:
                raise t
            return None
    now = read()
    if now == entry:
        return
    try:  # orchestrator lock amendment: the helper's restore may not take (it restores to the PROVEN entry now)
        system.MCE.SetCurrentConfiguration(entry)
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
    again = read()
    if again != entry:
        win["flags"].append(ACTIVE_NOT_RESTORED)
        win["mutation_warning"] = (f"the active configuration was {entry} on entry; it reads {again!r} "
                                   "after the call (a re-set was attempted once) — select it again with "
                                   "set_current_configuration")


def _attach_window_disclosure(refusal, win):
    """The ONE attach site for the window's disclosures on a refusal: ``win["flags"]`` merged by MEMBERSHIP
    (a refusal raised after the window already carries a snapshot of them) and ``mutation_warning`` copied
    only when truthy (the shape the success envelope uses). A refusal that moved the user's configuration
    and does not say so is forbidden."""
    have = list(refusal.extra.get("flags") or [])
    merged = have + [f for f in win["flags"] if f not in have]
    if merged:
        refusal.extra["flags"] = merged
    if win["mutation_warning"]:
        refusal.extra["mutation_warning"] = win["mutation_warning"]


@contextmanager
def _config_window(system, k):
    """Yield ``{"ident", "config_entry", "flags", "mutation_warning"}`` with the design evaluated at
    configuration ``k`` (``None``: the active one — the helper is NOT entered, nothing is switched).

    A refusal raised inside carries the window's flags and ``mutation_warning`` (``_attach_window_disclosure``).
    The post-exit restore check runs on EVERY exit, refusals included."""
    win = {"ident": None, "config_entry": None, "flags": [], "mutation_warning": None}
    if k is None:
        ident = _config_identity(system)
        win["flags"] += ident.pop("flags")
        win["ident"] = ident
        try:
            yield win
        except _Refuse as r:
            _attach_window_disclosure(r, win)
            raise
        return
    entry, _n = _entry_read(system)
    win["config_entry"] = entry
    state, refusal = None, None
    try:
        with with_configuration(system, k) as state:
            ident = _config_identity(system)
            ident.pop("flags", None)
            if ident["config_evaluated"] != k:
                raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY,
                              f"configuration {k} was requested, the entry configuration is {entry} and "
                              f"the evaluated configuration reads {ident['config_evaluated']!r}; nothing "
                              "was drawn")
            win["ident"] = ident
            yield win
    except _Refuse as r:
        refusal = r
        raise
    finally:
        if state is not None:  # the helper's own disclosure: extra words, never the proof
            if not state.get("restore_verified", True):
                win["flags"].append("config_helper_restore_unverified")
            if state.get("mutation_warning"):
                win["flags"].append("config_helper_warning")
        _post_exit_restore(system, entry, win)
        if refusal is not None:
            _attach_window_disclosure(refusal, win)


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #
def _geometry(system):
    """Fields, type, primary wave, image surface, wavelengths, unit factor — or a ``_Refuse``."""
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
    for f in fields:
        if f["vcy"] >= 1 or f["vcx"] >= 1:
            raise _Refuse(UNPROVEN, f"field {f['index']} has VCX {f['vcx']} / VCY {f['vcy']} >= 1: "
                          "the pupil remap is undefined")
    waves, wfault = _read_wavelengths(system)
    if waves is None:
        raise _Refuse("wavelengths_unread", str(wfault))
    geo_flags = []
    for w in waves:  # no nan anywhere in the envelope: the value binds colour/legend/primary, the weight is metadata
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
    return {"field_type": field_type, "fields": fields, "primary": primary, "image_surf": image_surf,
            "waves": waves, "unit_token": token, "unit_label": label, "um": factor, "flags": geo_flags}


# --------------------------------------------------------------------------- #
# Rays
# --------------------------------------------------------------------------- #
def _build_rays(hy, wave_indices, primary):
    """``1 + 2*41*W`` rays: ray 1 = the PRIMARY-wave Py=0 chief; then per wave 41 tangential (Px=0,
    Py=p_k) and 41 sagittal (Px=p_k, Py=0). ``p_k`` is the exact grid, never accumulated."""
    grid = ps.pupil_grid()
    rays = [{"wave": primary, "Hx": 0.0, "Hy": hy, "Px": 0.0, "Py": 0.0}]
    for w in wave_indices:
        rays += [{"wave": w, "Hx": 0.0, "Hy": hy, "Px": 0.0, "Py": p} for p in grid]
        rays += [{"wave": w, "Hx": 0.0, "Hy": hy, "Px": p, "Py": 0.0} for p in grid]
    return rays


def _int_text(v):
    """An exact int as text without the 4300-digit conversion limit: absurd magnitudes render as a bit length."""
    return str(v) if v.bit_length() <= 64 else "a %d-bit integer" % v.bit_length()


def _validate_waves(waves):
    """The shape of ``waves`` (pre-engine): ``None`` (every wave) or a non-empty ``list`` of distinct EXACT built-in
    ints >= 1. A bool, any int / list subclass, a float, a string, a duplicate is a ``ToolParamError``. The upper
    bound needs the table: ``_select_waves``. Nothing here formats a caller object beyond its type name."""
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
    """The drawn waves, in TABLE order. A number outside the table is a ``tool_param`` refusal raised on the
    module's own ``_Refuse`` path (inside the config window), so the restore disclosure rides it."""
    table = [w["index"] for w in geo["waves"]]
    if requested is None:
        return list(geo["waves"])
    bad = [v for v in requested if v not in table]
    if bad:
        raise _Refuse(TOOL_PARAM, "waves: wavelength number %s is not in the design's wavelength table "
                      "(valid 1..%d)" % (_int_text(bad[0]), len(table)))
    return [w for w in geo["waves"] if w["index"] in requested]


def _trace_bound(session, rays, image_surf):
    """ONE batch; every ray BOUND BY ``rayNumber`` (never position). ``short_read`` refuses."""
    n = len(rays)
    try:
        env = analysis_raytrace.trace_rays(session, {"rays": rays, "to_surface": image_surf})
    except Exception as exc:  # noqa: BLE001 — a ToolParamError here is not about THIS call's params
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse(UNPROVEN, f"ray trace could not run: {safe_exc(exc, repr_form=True)}") from None
    if not env.get("ok"):
        if env.get("error_family") in ("batch_unavailable", "analysis_malformed"):
            raise _Refuse(env["error_family"], env.get("error", ""))
        raise _Refuse(UNPROVEN, f"ray trace returned {env.get('returned')}/requested {n}: {env.get('error')}")
    got = env["rays"]
    nums = [r.get("rayNumber") for r in got]
    bad = sorted({repr(x) for x in nums if isinstance(x, bool) or not isinstance(x, int)
                  or not 1 <= x <= n or nums.count(x) > 1})
    missing = sorted(set(range(1, n + 1)) - set(nums))
    if env.get("short_read") or len(nums) != n or bad or missing:
        raise _Refuse(UNPROVEN, f"rays returned {len(nums)}/requested {n} (short_read "
                      f"{env.get('short_read')!r}); rayNumbers offending {bad}, missing {missing[:10]}: a "
                      "positional binding is refused", ray_identity={
                          "requested": n, "returned": len(nums), "bound_by": "rayNumber"})
    return {r["rayNumber"]: r for r in got}


def _int_code(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _reference_ok(ray):
    """``(usable, status, vignette_surface)``: ``errorCode == 0`` AND X, Y finite. A non-zero
    ``vignetteCode`` is DISCLOSED (``vignetted``), never used to null the field."""
    ec = ray.get("errorCode")
    if not _int_code(ec):
        return False, "trace_unavailable", None
    if ec != 0:
        return False, "ray_error:%d" % ec, None
    if suspicious_sentinel(ray.get("X")) or suspicious_sentinel(ray.get("Y")):
        return False, "non_finite", None
    vig = ray.get("vignetteCode")
    surf = vig if _int_code(vig) and vig != 0 else None
    return True, ("vignetted" if surf else "ok"), surf


def _sample_value(ray, ref, um):
    """``(ey_um, ex_um, reason)``: ``errorCode`` first, then ``vignetteCode`` (the code IS the blocking
    surface), then finiteness."""
    ec = ray.get("errorCode")
    if not _int_code(ec):
        return None, None, "trace_unavailable"
    if ec != 0:
        return None, None, "ray_error:%d" % ec
    vig = ray.get("vignetteCode")
    if not _int_code(vig):
        return None, None, "trace_unavailable"
    if vig != 0:
        return None, None, "vignetted:surface %d" % vig
    if suspicious_sentinel(ray.get("X")) or suspicious_sentinel(ray.get("Y")):
        return None, None, "non_finite"
    ey, ex = (ray["Y"] - ref["Y"]) * um, (ray["X"] - ref["X"]) * um
    if not (math.isfinite(ey) and math.isfinite(ex)):
        return None, None, "non_finite"
    return ey, ex, None


def _chief_reason(ray, status):
    ec = ray.get("errorCode")
    return ("chief_ray_failed:%d" % ec) if status.startswith("ray_error") else status


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #
def _runs(reasons, field, wave, fan, grid):
    """Run-length ``null_runs`` of one 41-long reason list (``None`` = a drawn sample)."""
    out, i = [], 0
    while i < len(reasons):
        if reasons[i] is None:
            i += 1
            continue
        j = i
        while j + 1 < len(reasons) and reasons[j + 1] == reasons[i]:
            j += 1
        out.append({"field": field, "wave": wave, "fan": fan, "p_from": grid[i], "p_to": grid[j],
                    "reason": reasons[i]})
        i = j + 1
    return out


def _border_samples(values, reasons, field, wave, fan, grid):
    """Drawn samples adjacent to a BLOCKED (``vignetted:*``) run, not at +-1: CANDIDATES the native
    plot may omit."""
    out = []
    blocked = [r is not None and r.startswith("vignetted:") for r in reasons]
    for k, v in enumerate(values):
        if v is None or k in (0, N - 1):
            continue
        if (k > 0 and blocked[k - 1]) or (k < N - 1 and blocked[k + 1]):
            out.append({"field": field, "wave": wave, "fan": fan, "p": grid[k], "value_um": v})
    return out


def _assemble(geo, traced):
    """The canonical per-field records (values rounded ONCE), null runs, edge samples."""
    grid = ps.pupil_grid()
    fields_out, runs, edges, usable_any, chief_reasons = [], [], [], False, {}
    wave_idx = [w["index"] for w in geo["waves"]]
    for f in geo["fields"]:
        by_num = traced[f["index"]]
        ref = by_num[1]
        ok, status, vsurf = _reference_ok(ref)
        usable_any = usable_any or ok
        if not ok:
            chief_reasons[f["index"]] = _chief_reason(ref, status)
        ey_by, ex_by = {}, {}
        for wpos, w in enumerate(wave_idx):
            base = 2 + wpos * 2 * N
            tan_v, tan_r, sag_v, sag_r = [], [], [], []
            for k in range(N):
                for off, vals, reas, which in ((base + k, tan_v, tan_r, 0), (base + N + k, sag_v, sag_r, 1)):
                    if not ok:
                        vals.append(None)
                        reas.append(chief_reasons[f["index"]])
                        continue
                    ey, ex, why = _sample_value(by_num[off], ref, geo["um"])
                    vals.append(ps.round_value((ey, ex)[which], ps.ROUND_UM))
                    reas.append(why)
            ey_by[w], ex_by[w] = tan_v, sag_v
            for fan, vals, reas in (("tan", tan_v, tan_r), ("sag", sag_v, sag_r)):
                runs += _runs(reas, f["index"], w, fan, grid)
                edges += _border_samples(vals, reas, f["index"], w, fan, grid)
        fields_out.append({"f": f, "chief_ok": ok, "chief_status": status, "vsurf": vsurf,
                           "ey": ey_by, "ex": ex_by})
    return fields_out, runs, edges, usable_any, chief_reasons


def _data_max(fields_out):
    best, at = None, None
    for rec in fields_out:
        for fan, tab in (("tan", rec["ey"]), ("sag", rec["ex"])):
            for w, vals in tab.items():
                for v in vals:
                    if v is not None and (best is None or abs(v) > best):
                        best, at = abs(v), {"field": rec["f"]["index"], "wave": w, "fan": fan}
    return best, at


def _footer(runs, edges, fields_out, geo, chief_reasons, remap_unmeasured, n_clipped=0):
    lines = []
    if runs:
        reasons = {}
        for r in runs:
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        lines.append("not drawn (null, never 0): %d run(s): %s" % (
            len(runs), ", ".join("%s x%d" % kv for kv in sorted(reasons.items()))))
    for rec in fields_out:
        if rec["chief_ok"] and rec["vsurf"]:
            lines.append("field %d: chief ray vignetted at surface %d (drawn as the reference)" % (
                rec["f"]["index"], rec["vsurf"]))
    for i, why in sorted(chief_reasons.items()):
        lines.append("field %d: chief ray unusable (%s)" % (i, why))
    lines.append("ey, ex converted from %s to um (x%g)" % (geo["unit_label"], geo["um"]))
    if geo["primary"] not in [w["index"] for w in geo["waves"]]:
        lines.append("aberrations are measured against the primary-wave chief ray (wave %d, not drawn)"
                     % geo["primary"])
    if edges:
        lines.append("%d drawn sample(s) border a blocked run; the native plot may omit one of them." % len(edges))
    if remap_unmeasured:
        lines.append("sagittal pupil remap with VCX/VDX != 0 is inferred by symmetry (unmeasured)")
    if n_clipped:
        lines.append("%d sample(s) fall outside the ±1 pupil axis (vignetting factors); the values are in "
                     "the envelope." % n_clipped)
    return lines


def _field_env(rec, geo, label, max_field):
    f, grid = rec["f"], ps.pupil_grid()
    tan_x = ps.round_table([f["vdy"] + p * (1 - f["vcy"]) for p in grid], ps.ROUND_LENS)
    sag_x = ps.round_table([f["vdx"] + p * (1 - f["vcx"]) for p in grid], ps.ROUND_LENS)
    return {"index": f["index"], "value": f["y"],
            "value_unit": "deg" if geo["field_type"] == "Angle" else geo["unit_label"], "label": label,
            "hy": f["hy"], "vdx": f["vdx"], "vdy": f["vdy"], "vcx": f["vcx"], "vcy": f["vcy"],
            "chief": {"status": rec["chief_status"], "vignette_surface": rec["vsurf"]},
            "tan": {"x": {"unit": "real pupil L = VDY + p(1-VCY), axis +-1", "values": tan_x},
                    "ey": {"unit": "um", "by_wave": {str(w): v for w, v in rec["ey"].items()}}},
            "sag": {"x": {"unit": "real pupil L = VDX + p(1-VCX), axis +-1", "values": sag_x},
                    "ex": {"unit": "um", "by_wave": {str(w): v for w, v in rec["ex"].items()}}}}, tan_x, sag_x


def _render(session, system, k, path, requested=None):
    target, minted, fault, root, rfault = _resolve_png_path(session, path)
    if fault:
        raise _Refuse("workspace_unlistable", fault)
    geo = None
    try:
        with _config_window(system, k) as win:
            ident, flags = win["ident"], list(win["flags"])
            geo = _geometry(system)
            geo["all_waves"] = geo["waves"]  # the primary's um is read from the whole table, drawn or not
            geo["waves"] = _select_waves(geo, requested)
            grid = ps.pupil_grid()
            wave_idx = [w["index"] for w in geo["waves"]]
            traced = {}
            for f in geo["fields"]:
                traced[f["index"]] = _trace_bound(session, _build_rays(f["hy"], wave_idx, geo["primary"]),
                                                  geo["image_surf"])
            fields_out, runs, edges, usable_any, chief_reasons = _assemble(geo, traced)
    except _Refuse as r:
        # A refusal raised after ``_geometry`` returned
        # carries the geometry's flags (and the unresolved-root flag), merged by MEMBERSHIP -- the FCD pattern.
        have = list(r.extra.get("flags") or [])
        own = (geo["flags"] if geo else []) + (["workspace_root_unresolved"] if rfault else [])
        r.extra["flags"] = have + [f for i, f in enumerate(own) if f not in have and f not in own[:i]]
        if not r.extra["flags"]:
            del r.extra["flags"]
        raise
    try:
        flags = list(win["flags"]) + geo["flags"] + (["workspace_root_unresolved"] if rfault else [])
        if not usable_any:
            raise _Refuse(CHIEF_FAILED, "no field has a usable reference (chief) ray: " + "; ".join(
                "field %d: %s" % kv for kv in sorted(chief_reasons.items())), flags=flags)
        data_max, data_at = _data_max(fields_out)
        if data_max is None:
            raise _Refuse("analysis_empty", "no finite aberration value in any fan", flags=flags)
        scale = ps.fan_scale(data_max, data_at)
        max_field = max(abs(f["y"]) for f in geo["fields"])
        env_fields, model_fields, label_flags, clip_flags, n_clipped = [], [], [], [], 0
        for rec in fields_out:
            label, lf = ps.field_label(geo["field_type"], rec["f"]["y"], max_field, geo["unit_label"])
            label_flags += [x for x in lf if x not in label_flags]
            fe, tan_x, sag_x = _field_env(rec, geo, label, max_field)
            for fan, xs in (("tan", tan_x), ("sag", sag_x)):  # ONE scan of the canonical (rounded) axis values
                out = sum(1 for x in xs if abs(x) > 1.0)
                if out:
                    n_clipped += out
                    clip_flags.append("pupil_axis_clipped:%d:%s" % (rec["f"]["index"], fan))
            env_fields.append(fe)
            model_fields.append({"index": rec["f"]["index"], "label": label, "tan_x": tan_x, "sag_x": sag_x,
                                 "ey": rec["ey"], "ex": rec["ex"],
                                 "annotation": ("field %d: chief ray unusable (%s)" % (
                                     rec["f"]["index"], chief_reasons[rec["f"]["index"]])
                                     if not rec["chief_ok"] else None),
                                 # The gid carries the reason
                                 # TOKEN (the FCD idiom), never the displayed sentence.
                                 "annotation_token": (chief_reasons[rec["f"]["index"]]
                                                      if not rec["chief_ok"] else None)})
        waves_env, cycled = [], False
        for w in geo["waves"]:
            name, hexc, cyc = ps.wave_colour(w["index"])
            cycled = cycled or cyc
            waves_env.append({"index": w["index"], "um": w["um"], "weight": w["weight"],
                              "primary": w["primary"], "colour": name, "_hex": hexc})
        remap = any(f["vcx"] != 0 or f["vdx"] != 0 for f in geo["fields"])
        flags += label_flags + clip_flags + (["wave_colour_cycled"] if cycled else []) + (
            ["sagittal_vignetting_remap_unmeasured"] if remap else []) + [
            "chief_ray_failed:%d" % i for i in sorted(chief_reasons)]
        k_ev, n_ev = ident["config_evaluated"], ident["n_configs"]
        config_line = ("Configuration %d of %d" % (k_ev, n_ev)) if k_ev is not None and n_ev is not None \
            else "Configuration: unread"
        footer = _footer(runs, edges, fields_out, geo, chief_reasons, remap, n_clipped)
        model = fan_model(model_fields, [{"index": w["index"], "um": w["um"], "colour": w["_hex"]}
                                         for w in waves_env], scale, config_line, footer)
        final, sha, overwrote, pfault, cleanup = None, None, None, None, []
        try:
            final, sha, overwrote, pfault, cleanup = publish(build_fan_figure, model, target, minted, STEM)
        except Exception as exc:  # noqa: BLE001
            pfault = ("render_failed", safe_exc(exc, repr_form=True))
        if pfault:
            raise _Refuse(pfault[0], pfault[1], flags=flags, partial={"fields": env_fields},
                          **({"cleanup_failures": cleanup} if cleanup else {}),
                          **({"published_path": pfault[2], "overwrote": overwrote} if len(pfault) > 2 and pfault[2] else {}))
        n_total = sum(len(v) for rec in fields_out for tab in (rec["ey"], rec["ex"]) for v in tab.values())
        total = len(geo["fields"]) * (1 + 2 * N * len(wave_idx))
        env = {"ok": True, "tool": TOOL, "path": final, "png_sha256": sha, "png_minted": minted,
               "workspace_root": root, "output_dir": target if minted else os.path.dirname(final),
               "config_evaluated": k_ev, "n_configs": n_ev, "config_entry": win["config_entry"],
               "lens_unit": geo["unit_token"], "lens_unit_label": geo["unit_label"],
               "um_per_lens_unit": geo["um"], "field_type": geo["field_type"],
               "primary_wave": {"index": geo["primary"], "um": next(
                   (w["um"] for w in geo["all_waves"] if w["index"] == geo["primary"]), None)},
               "waves_selected": {"requested": None if requested is None else list(requested),
                                  "drawn": list(wave_idx), "primary": geo["primary"],
                                  "primary_drawn": geo["primary"] in wave_idx},
               "wavelengths": [{kk: vv for kk, vv in w.items() if kk != "_hex"} for w in waves_env],
               "pupil_p": {"unit": "normalized pupil (traced)", "values": grid},
               "scale": scale, "fields": env_fields, "null_runs": runs, "edge_samples": edges,
               "rounding": {"aberration_um": 1e-4}, "n_values": n_total,
               "ray_identity": {"requested": total, "returned": total, "bound_by": "rayNumber"},
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


def _config_in_range(k, n):
    """True iff ``k`` is a 1-based index of an ``n``-config system (exact built-in ints only; ``n >= 1``)."""
    return type(k) is int and type(n) is int and n >= 1 and 1 <= k <= n


def _resolve_config(system, config, tool=TOOL):
    """``config`` -> ``None`` (the active configuration) or an exact int ``k`` proven in range.

    ``None``, ``"all"`` and every other string go to the shared selector (shape only, no verdict on the
    count). A bool / non-integral / non-numeric value is refused by ``_coerce_config_int`` BEFORE any engine
    touch. A numeric ``k`` is range-checked HERE against ONE strict count read (``_config_in_range(k, n)``):
    the selector's throw-guarded substitute of ``1`` for an unreadable count is never the basis of a refusal,
    a transport loss is re-raised, and the count is never resolved by two paths."""
    if config is None or isinstance(config, str):
        return resolve_single_config_selector(system, config, tool)
    cfg = _coerce_config_int(config)
    try:
        n = _mce_cells.number_of_configurations(system)
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse(_ac.SETTINGS_UNVERIFIED_FAMILY, "the configuration count could not be read; nothing "
                      f"was switched ({safe_exc(exc, repr_form=True)})") from None
    if not _config_in_range(cfg, n):
        raise ToolParamError(f"config {cfg} out of range; valid 1..{n} (NumberOfConfigurations={n})")
    return cfg


def render_ray_fan(session, params):
    """The handler. Raises ``ToolParamError``, a transport loss, and the strict config reads' own
    ``config_unverified`` (from ``with_configuration.__enter__``, before any switch); every other fault is a
    returned refusal envelope."""
    path = params.get("path")
    if path is not None:
        if not isinstance(path, str) or not str.strip(path) or "\x00" in path:
            raise ToolParamError(f"path must be a non-empty string without NUL, got {safe_repr(path)}")
        _refuse_non_png_extension(path)
    requested = _validate_waves(params.get("waves"))  # shape only, pre-engine; the bound needs the table
    system = session.system
    try:
        k = _resolve_config(system, params.get("config"))
        return _render(session, system, k, path, requested)
    except _Refuse as r:
        return _ac.error_envelope(TOOL, r.family, str(r), **r.extra)


RENDER_RAY_FAN_SPEC = ToolSpec(
    name=TOOL,
    handler=render_ray_fan,
    required_params=(),
    param_types={"config": "number", "path": "string", "waves": "array"},
    description=DESCRIPTION,
)

TOOL_SPECS = (RENDER_RAY_FAN_SPEC,)
