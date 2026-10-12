"""Measure a lens file the way the demo pages do, on a fresh load.

    python demos/measure_design.py path/to/lens.zmx [more.zmx ...] [--json out.json]

It needs the optivibe-mcp packages installed and a ZOS-API licence. It opens ONE OpticStudio
session, works on a temporary COPY of each file (the original is never written), and closes the
session when it is done. Run it while no other OpticStudio automation session is open.

What it reads, per file (for every configuration when the file has several):

  focal length, f-number, total track        get_first_order, and the TOTR operand
  working distance, on axis                  the thickness of the last air gap
  working distance, at the lens rim          the ETVA operand on the last air gap
  polychromatic Strehl, per field            the STRH operand, Wave 0, at the current image plane
  RMS wavefront error, per field             the RWCE operand, Ring 6, Wave 0 (waves)
  RMS spot radius, per field and wavelength  the RSCE operand, Ring 6 (micrometres)
  RMS spot radius, polychromatic             RSCE, Wave 0, at field heights 0 / 0.5 / 0.7 / 0.8 / 1
  field curvature                            the FCGS and FCGT operands at 11 field heights and
                                             every wavelength; the figure is the largest shift
                                             from the on-axis sagittal focus, per wavelength
  distortion                                 analyze_distortion (percent, worst field)
  lateral colour                             analyze_lateral_color (micrometres, worst field)
  clearance                                  check_clearance, glass edges >= 1.0 mm, air >= 0.5 mm

Every optical figure comes from an OptiVibe tool call or an operand read through one. The script
reads four things straight from the loaded system: the field heights, the wavelengths, the number
of configurations, and the thickness of the last air gap. A reading that could not be made, or
that came back as a sentinel, is reported as null, never as 0.
"""
import json
import math
import os
import shutil
import sys
import tempfile
import time

from optivibe_harness import process_reaper
from optivibe_harness.server import Dispatcher
from optivibe_harness.session import ZemaxSession

FIELD_HEIGHTS = [i / 10.0 for i in range(11)]
SPOT_HEIGHTS = [0.0, 0.5, 0.7, 0.8, 1.0]
MIN_AIR, MIN_GLASS = 0.5, 1.0


def _number(v):
    """A finite reading, or None. OpticStudio's could-not-compute sentinels are 1e10 and 9e9."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) and abs(v) < 1e9 else None


def _result(env):
    """The tool's own result, or None when either the dispatch or the tool reported failure."""
    inner = env.get("result")
    if not env.get("ok") or not isinstance(inner, dict) or inner.get("ok") is False:
        return None
    return inner


def _operand(d, **params):
    r = _result(d.dispatch("get_operand", params))
    if r is None or r.get("suspicious_sentinel") or r.get("suspicious"):
        return None
    return _number(r.get("value"))


def _poly_strehl(d, n_fields, config=None):
    """STRH with Wave 0 (polychromatic) for each field, read back from a scratch merit function.

    On a multi-configuration file each operand is bracketed to ``config``; without that the merit
    function does not follow the current configuration."""
    d.dispatch("clear_merit", {})
    for f in range(1, n_fields + 1):
        args = {"operand": "STRH", "params": {"Field": f, "Wave": 0}, "target": 0, "weight": 0}
        if config is not None:
            args["config"] = config
        d.dispatch("add_operand", args)
    dump = _result(d.dispatch("dump_merit_function", {})) or {}
    vals = [_number(o.get("value")) for o in dump.get("operands", []) if o.get("type") == "STRH"]
    d.dispatch("clear_merit", {})
    return vals if len(vals) == n_fields else [None] * n_fields


def _field_curvature(d, n_waves):
    per_wave = {}
    for w in range(1, n_waves + 1):
        sag = [_operand(d, operand="FCGS", wave=w, hx=0.0, hy=h) for h in FIELD_HEIGHTS]
        tan = [_operand(d, operand="FCGT", wave=w, hx=0.0, hy=h) for h in FIELD_HEIGHTS]
        vals = sag + tan
        # One failed read makes the wavelength's figure unknown: a maximum over the readings
        # that happened to succeed would understate it.
        per_wave[w] = (None if any(v is None for v in vals)
                       else max(abs(v - sag[0]) for v in vals) * 1000.0)
    worst = None if any(v is None for v in per_wave.values()) else max(per_wave.values())
    return {"per_wavelength_um": per_wave, "worst_wavelength_um": worst}


def _measure_current_config(d, session, config=None):
    system = session.system
    fields = system.SystemData.Fields
    waves = system.SystemData.Wavelengths
    n_fields, n_waves = int(fields.NumberOfFields), int(waves.NumberOfWavelengths)
    ys = [float(fields.GetField(i).Y) for i in range(1, n_fields + 1)]
    y_max = max(abs(y) for y in ys) or 1.0
    n_surf = int(system.LDE.NumberOfSurfaces)
    last_gap = n_surf - 2

    rec = {"fields_y": ys,
           "wavelengths_um": [float(waves.GetWavelength(i).Wavelength) for i in range(1, n_waves + 1)]}
    first = _result(d.dispatch("get_first_order", {})) or {}
    rec["first_order"] = first.get("headline")
    rec["total_track_mm"] = _operand(d, operand="TOTR")
    rec["working_distance_axis_mm"] = _number(system.LDE.GetSurfaceAt(last_gap).Thickness)
    rec["working_distance_rim_mm"] = _operand(d, operand="ETVA", surf=last_gap)
    rec["poly_strehl"] = _poly_strehl(d, n_fields, config)
    rec["rms_wavefront_waves"] = [_operand(d, operand="RWCE", ring=6, wave=0, hx=0.0, hy=y / y_max)
                                  for y in ys]
    rec["rms_spot_um"] = [[(lambda v: None if v is None else v * 1000.0)(
        _operand(d, operand="RSCE", ring=6, wave=w, hx=0.0, hy=y / y_max))
        for w in range(1, n_waves + 1)] for y in ys]
    rec["rms_spot_poly_um"] = {str(h): (lambda v: None if v is None else v * 1000.0)(
        _operand(d, operand="RSCE", ring=6, wave=0, hx=0.0, hy=h)) for h in SPOT_HEIGHTS}
    rec["field_curvature"] = _field_curvature(d, n_waves)
    dist = _result(d.dispatch("analyze_distortion", {})) or {}
    rec["max_distortion_percent"] = _number(dist.get("max_distortion_percent"))
    lat = _result(d.dispatch("analyze_lateral_color", {})) or {}
    rec["max_lateral_color_um"] = _number(lat.get("max_lateral_color_um"))
    clear = _result(d.dispatch("check_clearance", {"min_air": MIN_AIR, "min_glass": MIN_GLASS}))
    rec["clearance_violations"] = None if clear is None else clear.get("violations")
    return rec


def measure(d, session, path, work):
    copy = os.path.join(work, os.path.basename(path))
    shutil.copy2(path, copy)
    loaded = _result(d.dispatch("load_design", {"path": copy}))
    if loaded is None:
        return {"error": "load_design refused the file"}
    n_cfg = int(session.system.MCE.NumberOfConfigurations)
    rec = {"file": os.path.basename(path), "configurations": n_cfg, "per_configuration": {}}
    for cfg in range(1, n_cfg + 1):
        if n_cfg > 1 and _result(d.dispatch("set_current_configuration", {"config": cfg})) is None:
            rec["per_configuration"][cfg] = {"error": "could not switch to this configuration"}
            continue
        rec["per_configuration"][cfg] = _measure_current_config(d, session, cfg if n_cfg > 1 else None)
    return rec


def _fmt(v, nd=3):
    if v is None:
        return "n/a"
    if isinstance(v, (list, tuple)):
        return " / ".join(_fmt(x, nd) for x in v)
    return ("%." + str(nd) + "f") % v


def report(rec):
    print("\n== %s" % rec.get("file"))
    if "error" in rec:
        print("   " + rec["error"])
        return
    for cfg, r in rec["per_configuration"].items():
        if rec["configurations"] > 1:
            print("-- configuration %s" % cfg)
        if "error" in r:
            print("   " + r["error"])
            continue
        fo = r.get("first_order") or {}
        print("   focal length (mm)             %s" % _fmt(fo.get("effective_focal_length")
                                                         if "effective_focal_length" in fo else fo.get("EFFL")))
        print("   first-order headline          %s" % json.dumps(fo, default=str))
        print("   total track (mm)              %s" % _fmt(r["total_track_mm"]))
        print("   working distance axis / rim   %s / %s" % (_fmt(r["working_distance_axis_mm"]),
                                                            _fmt(r["working_distance_rim_mm"])))
        print("   polychromatic Strehl, fields  %s" % _fmt(r["poly_strehl"]))
        print("   RMS wavefront (waves)         %s" % _fmt(r["rms_wavefront_waves"]))
        print("   RMS spot (um), field x wave   %s" % "; ".join(_fmt(row, 2) for row in r["rms_spot_um"]))
        print("   RMS spot, polychromatic (um)  %s   at field heights %s"
              % (_fmt(list(r["rms_spot_poly_um"].values()), 2), " / ".join(r["rms_spot_poly_um"])))
        print("   field curvature, worst (um)   %s" % _fmt(r["field_curvature"]["worst_wavelength_um"], 2))
        print("   max distortion (%%)            %s" % _fmt(r["max_distortion_percent"], 2))
        print("   max lateral colour (um)       %s" % _fmt(r["max_lateral_color_um"], 2))
        viol = r["clearance_violations"]
        print("   clearance violations          %s" % ("n/a" if viol is None else len(viol)))


def main(argv):
    args = [a for a in argv if a != "--json"]
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        if i + 1 >= len(argv):
            print("--json needs a file name")
            return 64
        out_json = argv[i + 1]
        args.remove(out_json)
    if not args:
        print(__doc__)
        return 64
    missing = [p for p in args if not os.path.isfile(p)]
    if missing:
        print("not a file: %s" % ", ".join(missing))
        return 64

    before = set(process_reaper.snapshot_engine_pids())
    work = tempfile.mkdtemp(prefix="optivibe_measure_")
    session = ZemaxSession()
    session.workspace_root = work
    results = []
    try:
        session.open()
        d = Dispatcher(session)
        for path in args:
            rec = measure(d, session, os.path.abspath(path), work)
            results.append(rec)
            report(rec)
    finally:
        session.close()
        time.sleep(2)
        left = sorted(set(process_reaper.snapshot_engine_pids()) - before)
        shutil.rmtree(work, ignore_errors=True)
    print("\nOpticStudio sessions left running by this script: %d" % len(left))
    if out_json:
        with open(out_json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1, default=str)
        print("wrote %s" % out_json)
    return 0 if not left else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
