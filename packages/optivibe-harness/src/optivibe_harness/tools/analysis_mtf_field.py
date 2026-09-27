"""tools/analysis_mtf_field.py — ``render_mtf_vs_field``: MTF against REAL image height.

ONE FFT-MTF run at the design's own fields; each series bound to its field by the engine's
``Description`` (list order is only the PROPOSAL, content is the proof; identical prints are
tie-broken by list order and FLAGGED); each field placed on X by a batch-traced GEOMETRIC chief
ray (``Py = -VDY/(1-VCY)``) bound by ``rayNumber`` and gated on ``errorCode``; the PNG published
temp -> ``_is_png`` -> exclusive create (minted) or ``os.replace`` (explicit ``path``).
The DESIGN is read-only; the only engine writes are this call's own analysis-window settings,
disclosed in ``settings_written`` (persistence UNMEASURED). The first refusing step
names the family; later steps only disclose. Raises ``ToolParamError`` (step 1) and re-raises a
TRANSPORT-LOSS exception — the transport LINK is raised (``_transport_loss``) — every other engine
fault is netted per step to that step's family; a harness bug reaches dispatch as
``internal`` — by design.

R-A″ — transport-loss precedence, scoped. A transport-loss exception that REACHES this module's
handlers — directly, or as the explicit ``__cause__`` of a wrapper raised by a shared helper — is
re-raised unchanged (the transport link itself) and dispatch serves ``session_closed``; the gate
latches ``engine_channel_dead`` on the NEXT call. Three fenced shared helpers this tool is required
to call DESTROY the exception before a handler can see it (``_solve_trace._primary_wave``,
``_measurement_common.image_surface_index``, ``_analysis_common._units_member``); on THEIR failure
paths, and only there, the tool performs one guarded liveness read
(``SystemData.Fields.NumberOfFields`` — a member step 5 reads in every call; at the lens-unit site the
probe runs BEFORE step 5, at the geometry site after it) [P2R5-1] before choosing a design
family — a transport-typed raise is re-raised, any other outcome leaves the documented family. This
rests on the premise that the same handle raises again on the next read in the same call
(measured for flavour B, inferred for flavour A). One helper-failure path (Angle + unresolved lens
unit) still succeeds, so it reads ``NumberOfFields`` twice. The tool does
not read ``IsAlive``, does not call ``observe_channel()`` and never latches. A shared helper that
swallows a transport loss at any OTHER site degrades the fault to that site's design family on THIS
call; the gate refuses the dead channel on the NEXT call. Those helpers are ticketed harness-wide.
The tool claims exactly this and no more.
"""
import hashlib, math, os, re, shutil, sys  # noqa: E401

from .._io import safe_exc, safe_repr
from ..artifact_naming import next_index_in_dir
from ..artifact_sink import _safe_name
from ..errors import (AnalysisResultError, SessionClosedError, ToolParamError,
                      map_dotnet_exception)
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _mce_cells
from . import analysis_raytrace
from ._image_gate import _is_png
from ._measurement_common import image_surface_index, suspicious_sentinel
from ._optimize_common import _base_token  # the ONE base-slot token read [R-B]
from ._mtf_field_plot import (ReferenceInvalid, exclusive_temp, plot_model, reference_digest, render_png,
                              validate_reference)
from ._solve_trace import _primary_wave
from .workspace import _resolve_root  # import != edit; the root, never a fixed name
from .analysis_mtf import (_MTF_AUTO_EXTEND_MAX, _MTF_AUTO_EXTEND_PAD, _MTF_DEFAULT_GRID_MAX,
                           _as_finite, _interp_at, _validate_at_frequencies)

TOOL = "render_mtf_vs_field"
#: FROZEN, exact-token compared [P-3][P-4] — ``"Angle" in "TheodoliteAngle"`` is True.
FIELD_TYPES = ("Angle", "ObjectHeight", "ParaxialImageHeight", "RealImageHeight")
SAMPLE_SIZES, DEFAULT_FREQUENCIES, MAX_FREQUENCIES, MAX_MINT_ATTEMPTS = (64, 128, 256, 512), \
    (10.0, 20.0, 40.0), 6, 3
_DIFF_RE = re.compile(r"^Field:\s*Diffraction limit$", re.IGNORECASE)
_DESC_RE = re.compile(r"^Field:\s*([-+]?\d+(?:\.\d+)?)\s(mm|\(deg\))$")
_MINT_RE = re.compile(r"^mtf_vs_field_(\d{4,9})\.png$", re.IGNORECASE)  # the formatter's set
_NONFINITE = "non-finite modulation at the interpolation point"
_RMTREE_HOOK = "onexc" if sys.version_info >= (3, 12) else "onerror"
RAY_BASIS = "Hx=0, Px=0, Py=-VDY/(1-VCY) (0 where VDY=0); per-field py in fields[].py_traced"
HEIGHT_NOTE = ("batch trace gated on errorCode==0; the MTF is of the vignetted pupil, the "
               "height is the geometric chief ray")
WAVELENGTH_BASIS = ("the FFT MTF is the engine's polychromatic result over the system "
                    "wavelengths and weights; the image height is traced at the primary")
CHIEF_REMEDY = ("reduce the field / enable ray aiming / fix the aperture; a failed chief ray "
                "makes the FFT MTF return no series for ANY field, so no chart can be drawn")
SAMPLING_NOTE = "the engine warned 'Sampling too low' at this sampling: "
NEXT_SAMPLE_SIZE = {64: 256, 128: 256, 256: 512}  # the note names the NEXT size up [P2R6-OWN-1]
DEFAULT_SAMPLE_SIZE = 256  # [P2R6-OWN-1] owner ruling: the default is WRITTEN (S_256x256), never the engine's
OBJECT_HEIGHT_HINT = ("; the field type is ObjectHeight, and on an infinite-conjugate object "
                      "the FFT MTF returns 0 series with no message")


def _transport_loss(exc):
    """The transport-loss LINK in ``exc``'s EXPLICIT ``__cause__`` chain, or ``None``.

    (1) cause-only: ``__context__`` is NEVER followed — an implicit context acquires unrelated
    exceptions, so a plain error raised while a transport loss was being handled would be renamed;
    (2) each link is classified by the ONE shared mapper dispatch uses
    (``isinstance(map_dotnet_exception(link), SessionClosedError)``, type-keyed, never a message);
    (3) the guards raise the LINK, never a wrapper, so dispatch serves ``session_closed``; (4) a
    visited set breaks a cycle and at most 8 links are read (a deeper transport is left to the step
    family — the documented bound); (5) a wrapper whose chain holds no transport is untouched;
    (6) the ``__cause__`` read itself is guarded — a raising attribute ends the walk (round 5)."""
    seen, link = set(), exc
    for _ in range(8):
        if link is None or id(link) in seen:
            return None
        if isinstance(map_dotnet_exception(link), SessionClosedError):
            return link
        seen.add(id(link))
        try:  # a hostile / raising `__cause__` ENDS the walk: "no transport link" [round 5]
            link = getattr(link, "__cause__", None)
        except Exception:  # noqa: BLE001 — the step family then applies (fail closed for the claim)
            return None
    return None


def _confirm_engine_alive(system):
    """HELPER-FAILURE PATHS ONLY (not "refusal paths"): after a fenced helper swallowed a
    failure, ONE guarded read of ``Fields.NumberOfFields``; a transport-typed raise is re-raised,
    anything else falls through (this read never creates a refusal). One such path — an Angle design
    whose lens unit is unresolved — still SUCCEEDS (a flagged chart), so a success on that path reads
    ``NumberOfFields`` TWICE; the ordinary resolved-unit happy path reads it once. Never reads
    ``IsAlive``, never calls ``observe_channel()``, never latches. Premise: the same handle that raised
    a transport loss raises again on the next read in the same call (measured for flavour B,
    inferred for flavour A)."""
    try:
        int(system.SystemData.Fields.NumberOfFields)
    except Exception as exc:  # noqa: BLE001 — only a transport loss may escape
        t = _transport_loss(exc)
        if t is not None:
            raise t


class _Refuse(Exception):
    """An expected refusal: ``family`` + message + envelope disclosures."""

    def __init__(self, family, message="", **extra):
        super().__init__(message)
        self.family, self.extra = family, extra


def _validate_frequencies(value):
    """``_validate_at_frequencies`` THEN each ``> 0``, at most six, unique (cycles/lens unit)."""
    if value is None:
        return list(DEFAULT_FREQUENCIES)
    try:  # TOTAL: a 400-digit int / hostile subclass is a refusal, never `internal`
        out = _validate_at_frequencies(value)
    except Exception as exc:  # noqa: BLE001 — caller input; no legitimate value raises here
        raise ToolParamError(f"frequencies: {safe_exc(exc)}; got {safe_repr(value)}") from None
    if min(out) <= 0 or len(out) > MAX_FREQUENCIES or len(set(out)) != len(out):
        raise ToolParamError(f"frequencies must be 1..{MAX_FREQUENCIES} UNIQUE values > 0, "
                             f"got {safe_repr(value)}")
    return out


def _validate_sample_size(value):
    """``None`` (the caller passed nothing: ``_run_fftmtf`` WRITES ``DEFAULT_SAMPLE_SIZE`` and
    discloses ``default_applied`` [P2R6-OWN-1]) or one of 64/128/256/512; bool / non-integral -> refuse."""
    if value is None:
        return None
    try:  # a hostile __eq__ / __int__ is a refusal, never `internal`
        ok = not isinstance(value, bool) and isinstance(value, (int, float)) \
            and value in SAMPLE_SIZES
        out = int(value) if ok else None
    except Exception:  # noqa: BLE001
        ok = False
    if not ok:
        raise ToolParamError(f"sample_size must be one of {list(SAMPLE_SIZES)}, "
                             f"got {safe_repr(value)}")
    return out


def _field_type_token(system):
    return _base_token(system.SystemData.Fields.GetFieldType())


def _read_fields_strict(system):
    """``[{index, x, y, weight, hy, vdx, vdy, vcx, vcy}]``; ANY throw -> ``field_read_failed``."""
    try:
        fl, out = system.SystemData.Fields, []
        for i in range(1, int(fl.NumberOfFields) + 1):
            f = fl.GetField(i)
            row = {k.lower(): float(getattr(f, k))
                   for k in ("X", "Y", "Weight", "VDX", "VDY", "VCX", "VCY")}
            if not all(math.isfinite(v) for v in row.values()):
                raise ValueError(f"field {i} has a non-finite value {row}")
            out.append(dict(row, index=i))
    except Exception as exc:  # noqa: BLE001 — never a guessed field; a refusal names it
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse("field_read_failed", f"the field set could not be read: {safe_exc(exc, repr_form=True)}") from None
    bad_x, bad_y = [f["index"] for f in out if f["x"] != 0], [f["index"] for f in out if f["y"] < 0]
    if not out or bad_x or bad_y:
        raise _Refuse("field_unsupported", f"supported: >= 1 field, X == 0, Y >= 0; fields "
                      f"with X != 0: {bad_x}; with Y < 0: {bad_y}; n_fields {len(out)}")
    ymax = max(abs(f["y"]) for f in out)
    for f in out:
        f["hy"] = f["y"] / ymax if ymax else 0.0
    return out


def _config_identity(system):
    """The RAISING ``_mce_cells`` readers; a raise -> ``None`` + a flag. Never ``safe_*``."""
    try:
        return {"config_evaluated": _mce_cells.current_configuration(system),
                "n_configs": _mce_cells.number_of_configurations(system), "flags": []}
    except Exception as exc:  # noqa: BLE001 — disclose, never refuse, never default to 1
        t = _transport_loss(exc)
        if t is not None:
            raise t
        return {"config_evaluated": None, "n_configs": None,
                "flags": ["config_identity_unreadable"]}


def _chief_pupil_coords(field):
    """``(0.0, -VDY/(1-VCY))`` (``0.0`` exactly when VDY == 0); ``None`` when VCY >= 1 [P-1]."""
    if field["vcy"] >= 1:
        return None
    return 0.0, (0.0 if field["vdy"] == 0 else -field["vdy"] / (1.0 - field["vcy"]))


def _read_wavelengths(system):
    """``([{index, um, weight, primary}], None)`` or ``(None, fault)`` — a disclosure only."""
    try:
        w = system.SystemData.Wavelengths
        return [{"index": i, "um": float(g.Wavelength), "weight": float(g.Weight),
                 "primary": bool(g.IsPrimary)} for i, g in
                ((i, w.GetWavelength(i)) for i in range(1, int(w.NumberOfWavelengths) + 1))], None
    except Exception as exc:  # noqa: BLE001 — [CR-Q1-9] disclosed, never a refusal
        t = _transport_loss(exc)
        if t is not None:
            raise t
        return None, f"wavelengths could not be read: {safe_exc(exc, repr_form=True)}"


def _trace_chief_heights(session, fields, primary, image_surf):
    """ONE ``trace_rays`` call; every ray BOUND BY ``rayNumber`` (never position) [SA-2]."""
    n = len(fields)
    rays = [{"wave": primary, "Hx": 0.0, "Hy": f["hy"], "Px": 0.0, "Py": f["py_traced"]}
            for f in fields]
    try:  # a ToolParamError from trace_rays is NOT about THIS call's params
        env = analysis_raytrace.trace_rays(session, {"rays": rays, "to_surface": image_surf})
    except Exception as exc:  # noqa: BLE001 — the trace could not run: a geometry refusal
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse("field_geometry_unproven", "chief-ray trace could not run: "
                      f"{safe_exc(exc, repr_form=True)}") from None
    if not env.get("ok"):
        if env.get("error_family") in ("batch_unavailable", "analysis_malformed"):
            raise _Refuse(env["error_family"], env.get("error", ""))
        raise _Refuse("field_geometry_unproven", f"chief-ray trace returned "
                      f"{env.get('returned')}/requested {n}: {env.get('error')}")
    nums = [r.get("rayNumber") for r in env["rays"]]
    bad = sorted({repr(x) for x in nums if isinstance(x, bool) or not isinstance(x, int)
                  or not 1 <= x <= n or nums.count(x) > 1})
    missing = sorted(set(range(1, n + 1)) - set(nums))
    if len(nums) != n or bad or missing:
        raise _Refuse("field_geometry_unproven", f"chief rays returned {len(nums)}/requested "
                      f"{n}; rayNumbers {nums}: offending {bad}, missing {missing}; a "
                      "positional binding is refused", ray_identity={
                          "returned": len(nums), "requested": n, "ray_numbers": nums})
    by_num, per_field, failed, unplaced = {r["rayNumber"]: r for r in env["rays"]}, [], [], []
    for f in fields:
        r = by_num[f["index"]]
        err, vig, y = r.get("errorCode"), r.get("vignetteCode"), r.get("Y")
        if isinstance(err, bool) or not isinstance(err, int):  # [A5] err FIRST
            status = "trace_unavailable"
        elif err != 0:  # a known error is a failure regardless of an unread vignette code
            status = f"ray_error:{err}"
            failed.append(f["index"])
        elif isinstance(vig, bool) or not isinstance(vig, int):  # placed needs a proven marker
            status = "trace_unavailable"
        else:
            status = "non_finite" if suspicious_sentinel(y) else "ok"
        if status in ("trace_unavailable", "non_finite"):
            unplaced.append(f["index"])
        per_field.append({"image_height": y if status == "ok" else None, "status": status,
                          "vignette_surface": vig if status == "ok" else 0,
                          "error_code": err, "y_raw": y})
    return {"per_field": per_field, "failed": failed, "unplaced": unplaced,
            "ray_identity": {"returned": n, "requested": n, "bound_by": "rayNumber"}}


def _sample_sizes_enum(system):
    try:  # the fakes inject `_sample_sizes`; a live system has no such attribute (R-G tripwire:
        return system._sample_sizes  # no attribute-lookup builtin aliasing `system` here)
    except AttributeError:
        import ZOSAPI.Analysis as _an  # type: ignore  # pragma: no cover - live backend path
        return _an.SampleSizes


def _sub_number(setting, getter):
    return int(getattr(getattr(setting, "__implementation__", setting), getter)())


def _unverified(disc, harvest, setting, message):
    """A setting that did not take: refuse on the normal path; on the chief-failed HARVEST
    path it is a disclosure and the run continues (one window, [A6])."""
    if harvest:
        disc["flags"].append({"harvest_setting_unverified": message})
        return
    raise _Refuse("sampling_not_applied" if setting in ("SampleSize", "sample_size") else
                  "analysis_settings_unverified", message, **disc)


def _engine_fault(disc, harvest, exc, lead):
    """An engine raise -> ``analysis_malformed``; on the harvest path also a ``harvest_run_failed``
    flag, so the chief-failed envelope says WHY no engine text was harvested."""
    text = safe_exc(exc, repr_form=True)
    if harvest:
        disc["flags"].append({"harvest_run_failed": text})
    return _Refuse("analysis_malformed", lead + text, **disc)


def _write_setting(typed, name, value, same, disc, harvest=False, note=""):
    """Write on the TYPED view and read it back: a write that did not take refuses [SA3-3]."""
    disc["settings_written"].append(name)
    try:  # the comparator runs INSIDE the guard: a raising one is "did not take"
        setattr(typed, name, value)
        got = getattr(typed, name)
        ok = bool(same(got))
    except Exception as exc:  # noqa: BLE001 — an unreadable read-back is not a proof
        t = _transport_loss(exc)
        if t is not None:
            raise t
        got, ok = f"<read-back raised (or its comparison did): {safe_exc(exc, repr_form=True)}>", False
    if not ok:
        _unverified(disc, harvest, name, f"pre-run: {name} was written {value!s}{note} but reads "
                    f"back {safe_repr(got)} (the write did not take)")


def _post_run_settings(typed):
    """The POST-run values of the seven settings (``None`` where a read throws)."""
    post = {}
    for key, read in (("show_diffraction_limit", lambda: bool(typed.ShowDiffractionLimit)),
                      ("type", lambda: _base_token(typed.Type)),
                      ("field", lambda: _sub_number(typed.Field, "GetFieldNumber")),
                      ("wavelength", lambda: _sub_number(typed.Wavelength, "GetWavelengthNumber")),
                      ("surface", lambda: _sub_number(typed.Surface, "GetSurfaceNumber")),
                      ("sample_size", lambda: _base_token(typed.SampleSize)),
                      ("maximum_frequency", lambda: float(typed.MaximumFrequency))):
        try:
            post[key] = read()
        except Exception as exc:  # noqa: BLE001 — an unread setting cannot pass an assertion
            t = _transport_loss(exc)
            if t is not None:
                raise t
            post[key] = None
    return post


def _series_at(results, i, frequencies, disc):
    """One series: marshal + the X-grid precondition [SA-5][SA3-1] + ``_interp_at``."""
    ds = results.GetDataSeries(i)
    try:  # a RETURNED None/odd string is a CONTENT fault for the binder; a THROW is a read fault
        desc = _base_token(ds.Description)
    except Exception as exc:  # noqa: BLE001 — a read fault -> ``analysis_malformed`` naming the series
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _Refuse("analysis_malformed", f"series {i}: Description could not be read: "
                      f"{safe_exc(exc, repr_form=True)}", series_index=i, **disc) from None
    try:
        x = _ac._marshal_array(ds.XData.Data)
        t, s = _ac._split_mtf_columns(_ac._marshal_array(ds.YData.Data))
    except AnalysisResultError as exc:  # relabel: only analysis_empty / analysis_malformed pass
        family = exc.family if exc.family in ("analysis_empty", "analysis_malformed") \
            else "analysis_malformed"
        raise _Refuse(family, f"series {i}: {safe_exc(exc)}", series_index=i, **disc) from None
    if len(x) != len(t):
        raise _Refuse("analysis_malformed", f"series {i}: frequency length {len(x)} != "
                      f"modulation row count {len(t)}", series_index=i, **disc)
    bad = next((j for j, v in enumerate(x) if _as_finite(v) is None or (j and v <= x[j - 1])), None)
    if bad is not None:
        raise _Refuse("analysis_malformed", f"series {i}: X grid is not finite and STRICTLY "
                      f"increasing at index {bad}", series_index=i, **disc)
    if x[0] > min(frequencies):
        raise _Refuse("analysis_malformed", f"series {i}: x[0]={x[0]} does not cover requested "
                      f"frequency {min(frequencies)}", series_index=i, **disc)
    at = []
    for f in frequencies:
        (tv, tn), (sv, sn) = _interp_at(x, t, f), _interp_at(x, s, f)
        at.append({"frequency": f, "t": tv, "s": sv, "t_note": tn, "s_note": sn})
    return {"index": i, "description": desc, "at": at, "x0": x[0], "x1": x[-1]}


def _run_fftmtf(system, frequencies, sample_size, *, harvest=False):
    """Own ``New_FftMtf``; settings written pre-run and ASSERTED post-run [P-2][SA-3].

    ``harvest=True`` (the chief-failed path only): a setting that did not take is FLAGGED and
    the ONE window still runs so the engine's messages are harvested [A6]. An engine raise
    anywhere in the window -> ``analysis_malformed`` with the window closed."""
    needed = max(frequencies)
    extend = _MTF_DEFAULT_GRID_MAX < needed <= _MTF_AUTO_EXTEND_MAX
    maxf = float(max(_MTF_DEFAULT_GRID_MAX, math.ceil(needed * _MTF_AUTO_EXTEND_PAD))) \
        if extend else None
    requested = sample_size or DEFAULT_SAMPLE_SIZE  # ALWAYS written [P2R6-OWN-1]
    token, dflt = f"S_{requested}x{requested}", "" if sample_size else " (the default 256)"
    disc = {"messages": [], "settings_written": [], "settings_read_back": None, "flags": [],
            "sampling": {"requested": requested, "default_applied": not sample_size,
                         "read_back": "read-back raised"},  # replaced by the post-run token
            "max_frequency_auto_extended": extend, "max_frequency_applied": None}
    try:  # outside the window's try/finally: nothing was opened, nothing to close
        analysis = system.Analyses.New_FftMtf()
    except Exception as exc:  # noqa: BLE001
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _engine_fault(disc, harvest, exc,
                            "the FFT MTF window could not be opened: ") from None
    try:
        typed = getattr(analysis.GetSettings(), "__implementation__", None)
        _write_setting(typed, "ShowDiffractionLimit", True, lambda g: g is True, disc, harvest)
        _write_setting(typed, "SampleSize", getattr(_sample_sizes_enum(system), token),
                       lambda g: _base_token(g) == token, disc, harvest, dflt)
        if extend:
            _write_setting(typed, "MaximumFrequency", maxf, lambda g: isinstance(g, float)
                           and math.isclose(g, maxf, rel_tol=1e-9, abs_tol=1e-9), disc, harvest)
            disc["max_frequency_applied"] = True
        analysis.ApplyAndWaitForCompletion()
        results = analysis.GetResults()
        if results is None:  # the silent-void canary's documented mapping
            raise _Refuse("analysis_empty", "the FFT MTF returned no results object", **disc)
        try:
            disc["messages"] = [_base_token(results.GetMessageAt(i).Text)
                                for i in range(int(results.NumberOfMessages))]
        except Exception as exc:  # noqa: BLE001 — disclosed, never fabricated
            t = _transport_loss(exc)
            if t is not None:
                raise t
            disc["flags"].append("engine_messages_unread")
        post = disc["settings_read_back"] = _post_run_settings(typed)
        # "unread" is RETIRED [P2R6-OWN-1]: a raising read-back leaves "read-back raised" and REFUSES
        disc["sampling"]["read_back"] = post["sample_size"] or "read-back raised"
        expected = dict({"show_diffraction_limit": True, "type": "Modulation", "field": 0,
                         "wavelength": 0, "surface": 0, "sample_size": token},
                        **({"maximum_frequency": maxf} if extend else {}))
        for key, want in expected.items():
            if not (post[key] == want and type(post[key]) is type(want)):
                got = "the read-back raised" if post[key] is None else f"reads {safe_repr(post[key])}"
                _unverified(disc, harvest, key, f"post-run {key} {got}, expected {want!r}"
                            f"{dflt if key == 'sample_size' else ''}: no series accepted")
        n = int(results.NumberOfDataSeries)
        if n <= 0:
            raise _Refuse("analysis_empty", f"FFT MTF produced {n} data series; engine "
                          f"messages: {disc['messages']}", **disc)
        series = [_series_at(results, i, frequencies, disc) for i in range(n)]
    except _Refuse:  # a specific refusal keeps its specific message
        raise
    except Exception as exc:  # noqa: BLE001 — the window still closes (finally)
        t = _transport_loss(exc)
        if t is not None:
            raise t
        raise _engine_fault(disc, harvest, exc, "the FFT MTF engine call raised ") from None
    finally:
        try:
            analysis.Close()
        except Exception:  # noqa: BLE001 — window teardown never masks the outcome
            # teardown: a transport throw here is swallowed by design (R-A′); the next
            # dispatch's pre-flight latches it
            pass
    return dict(disc, ok=True, series=series, grid_max=series[0]["x1"], grid_min=series[0]["x0"])


def _bind_series(series, fields, lens_unit, field_type):
    """Bind by CONTENT; raises ``_Refuse("series_identity_unproven")``."""
    descs = [s["description"] for s in series]

    def refuse(why):
        raise _Refuse("series_identity_unproven", f"{why}; Descriptions: {descs}")
    if len(series) != len(fields) + 1:
        refuse(f"{len(series)} series for {len(fields)} fields (expected N+1)")
    if not (isinstance(descs[0], str) and _DIFF_RE.match(descs[0])):
        refuse(f"series 0 is not the diffraction limit: {descs[0]!r}")
    unit = "(deg)" if field_type == "Angle" else lens_unit
    bound, groups = [], {}
    for k, f in enumerate(fields, 1):
        d = descs[k]
        m = _DESC_RE.match(d) if isinstance(d, str) and "diffraction" not in d.lower() else None
        # The printed unit is part of the content proof, and that wins over the fallback:
        # only an Angle design keeps the no-reference fallback when the lens unit is unread.
        if m is not None and m.group(2) != unit and field_type != "Angle" \
                and lens_unit in ("unknown", None, ""):
            refuse(f"series {k} Description {d!r} prints unit {m.group(2)!r} but the design's "
                   "lens unit could not be read (Units.LensUnits → 'unknown'); the unit "
                   "proof cannot be made")
        if m is None or m.group(2) != unit:
            refuse(f"series {k} Description {d!r} is not 'Field: <value> {unit}'")
        dec = len(m.group(1).partition(".")[2])
        if abs(float(m.group(1)) - f["y"]) > 0.5 * 10 ** -dec + 1e-9:
            refuse(f"series {k} Description {d!r} does not print field {k}'s Y {f['y']!r}")
        groups.setdefault(d, []).append(f)
        bound.append({"field": f, "series_index": k, "description": d, "at": series[k]["at"]})
    flags = [{("duplicate_fields_bound_by_list_order" if len({f["y"] for f in g}) == 1 else
               "near_duplicate_fields_bound_by_list_order"): [f["index"] for f in g]}
             for g in groups.values() if len(g) > 1]
    for b in bound:
        g = groups[b["description"]]
        b["duplicate_group"] = [f["index"] for f in g] if len(g) > 1 else None
    axis = [b["field"]["index"] for b in bound if b["field"]["hy"] == 0 and any(
        a["t"] is not None and a["s"] is not None and abs(a["t"] - a["s"]) > 1e-6 for a in b["at"])]
    diffraction = [{"frequency": a["frequency"], "modulation": a["t"],
                    "series_description": descs[0]} for a in series[0]["at"]]
    return diffraction, bound, flags + ([{"axis_t_ne_s": axis}] if axis else [])


def _assemble(bound, heights, frequencies, diffraction):
    """Points / missing / unplaced; a value outside [0, 1] is KEPT and flagged, never clipped."""
    points, missing, oor = [], [], []
    for b, h in zip(bound, heights["per_field"]):
        idx, at = b["field"]["index"], []
        for a in b["at"]:
            row = {"frequency": a["frequency"]}
            for orient, key in (("tangential", "t"), ("sagittal", "s")):
                v = row[orient] = a[key]
                if v is None:
                    reason = a[key + "_note"] or _NONFINITE
                    row[orient + "_status"] = ("beyond_grid_max" if reason.startswith("beyond")
                                               else "non_finite")
                    missing.append({"field_index": idx, "frequency": a["frequency"],
                                    "orientation": orient, "reason": reason})
                    continue
                row[orient + "_status"] = "ok"
                if not -1e-6 <= v <= 1 + 1e-6:
                    oor.append({"field_index": idx, "frequency": a["frequency"],
                                "orientation": orient, "value": v})
            at.append(row)
        points.append({"field_index": idx, "field_value": b["field"]["y"],
                       "image_height": h["image_height"], "series_index": b["series_index"],
                       "series_description": b["description"],
                       "duplicate_group": b["duplicate_group"], "at": at})
    beyond = [d["frequency"] for d in diffraction
              if d["modulation"] is not None and d["modulation"] <= 1e-6]
    return (points, missing, list(heights["unplaced"]),
            [{"modulation_out_of_range": oor}] if oor else [], beyond)


def _height_flags(fields, per_field, field_type):
    placed = [(f, h) for f, h in zip(fields, per_field) if h["status"] == "ok"]
    by_hy = [f["index"] for f, _ in sorted(placed, key=lambda p: p[0]["hy"])]
    by_h = [f["index"] for f, _ in sorted(placed, key=lambda p: p[1]["image_height"])]
    flags = [{name: payload} for name, payload in (
        ("chief_ray_vignetted", [{"field_index": f["index"], "surface": h["vignette_surface"]}
                                 for f, h in placed if h["vignette_surface"]]),
        ("declared_height_mismatch", [f["index"] for f, h in placed if field_type ==
                                      "RealImageHeight" and abs(h["image_height"] - f["y"])
                                      > 1e-4 * max(1.0, abs(f["y"]))]),
        ("vdx_nonzero_px_zero", [f["index"] for f in fields if f["vdx"] != 0])) if payload]
    return flags + (["height_order_differs_from_field_order"] if by_hy != by_h else [])


def _mtf_index(name):
    """Reads EXACTLY the names the formatter writes for 1 <= k < 10**9 (``%04d``: 4..9 digits),
    case-insensitive [P2R2-7]; any other digit run is a foreign name (``None``)."""
    m = _MINT_RE.match(name)
    return int(m.group(1)) if m else None


def _refuse_non_png_extension(path):
    """[P2R3-OWN-2] a last component with a NON-.png extension is a typo, not a folder (kept isolated:
    the owner may still move this one clause)."""
    ext = os.path.splitext(os.path.basename(path))[1]
    if ext and str.lower(ext) != ".png":
        raise ToolParamError(f"path {safe_repr(path)} ends in {ext!r}: end with .png for a file, or "
                             "with a path separator for a dotted folder name")


def _root_state(session):
    """``(root | None, fault | None)``: ABSENT (clean ``None``) and UNREADABLE (a fault naming the
    resolver's exception) are two answers, never one value."""
    try:
        return _resolve_root(session)[0] or None, None
    except Exception as exc:  # noqa: BLE001 — unreadable root: a refusal, never a fixed name
        t = _transport_loss(exc)
        if t is not None:
            raise t
        return None, safe_exc(exc, repr_form=True)


def _output_dir_state(session):
    """[P2R3-OWN-1] the LOCAL replacement for ``scratch_dir_state``: ``<root>/evaluation``."""
    root, fault = _root_state(session)
    return (os.path.join(root, "evaluation") if root else None), (fault and (
        f"the workspace root could not be RESOLVED ({fault}); pass an explicit path"))


def _resolve_png_path(session, path):
    """``(target, minted, fault, root, root_fault)`` — step 3, pre-engine [P2R3-OWN-2]. An explicit
    ``.png`` path is the FILE (stem sanitised, extension case kept); any other ``path`` is a FOLDER to
    mint into; absent -> ``<root>/evaluation``. A RELATIVE path (file or folder) resolves against the
    workspace root (owner ruling), never the process CWD — no root, no relative path. R-F accepted
    shapes: RELATIVE (no drive, not rooted) or ABSOLUTE (drive/UNC prefix AND rooted); a
    drive-relative ``Q:fig.png`` and (on nt) a rooted-without-drive ``\\fig.png`` resolve against
    a CURRENT directory, so they refuse ``tool_param`` — never ``abspath``. A minted directory is
    proven a directory, created and listed HERE (the NAME is chosen at publication [SA2-1]);
    ``output_dir`` is the directory without a trailing separator (``dirname(join(target, "x"))``:
    keeps a drive root's separator; ``normpath`` would collapse the literal ``..``)."""
    if path is not None:
        drive, rest = os.path.splitdrive(path)
        rooted = rest[:1] in (os.sep, os.altsep or os.sep)  # from `rest`, not isabs [P2R3-3]
        if (drive and not rooted) or (not drive and rooted and os.name == "nt"):
            unc = drive.startswith(("\\\\", "//"))  # a UNC share root has no current directory
            shape = "a bare UNC share root" if unc else "drive-relative" if drive else "rooted without a drive"
            raise ToolParamError(f"path {safe_repr(path)} is {shape}: " + (
                f"name the share root with a trailing separator ({drive}/) or a file under it "
                f"({drive}/fig.png)" if unc else "it resolves against a current directory; use an "
                "absolute path such as Q:\\dir\\fig.png, or a path relative to the workspace root"))
    root, rfault = _root_state(session)
    if path is not None and not os.path.isabs(path):  # [P2R3-OWN-12] never a CWD fallback
        if rfault or not root:
            return None, None, (f"a relative path needs the workspace root, which could not be "
                                f"resolved: {rfault}; pass an absolute path" if rfault else
                                "no workspace root; pass an absolute path"), root, rfault
        path = os.path.join(root, path)
    if path is not None and str.lower(path).endswith(".png"):
        head, base = os.path.split(path)
        stem, ext = os.path.splitext(base)
        return os.path.join(head, _safe_name(stem) + ext), False, None, root, rfault
    target = path
    if target is None:
        target, fault = _output_dir_state(session)
        if fault or not target:
            return None, True, fault or "no workspace root; pass an explicit path", root, rfault
    target = os.path.dirname(os.path.join(target, "x"))  # strip a trailing sep (see docstring)
    try:
        if os.path.exists(target) and not os.path.isdir(target):
            return None, True, (f"{target!r} exists and is not a directory; pass a .png file path "
                                "or a different folder"), root, rfault
        os.makedirs(target, exist_ok=True)
        os.listdir(target)
    except OSError as exc:  # two bare-Name handlers, never a tuple type (R-G shape pin)
        return None, True, (f"the output directory {target!r} is not listable: "
                            f"{safe_exc(exc)}"), root, rfault
    except ValueError as exc:  # an embedded NUL in the root [V13]
        return None, True, (f"the output directory {target!r} is not listable: "
                            f"{safe_exc(exc)}"), root, rfault
    return target, True, None, root, rfault


def _publish_exclusive(tmp_png, out_dir):
    """Copy an ``_is_png``-gated temp into a freshly MINTED name via ``O_CREAT | O_EXCL``.

    A successful open is the OWNERSHIP TOKEN: nothing here unlinks a path this call did not
    create. ``FileExistsError`` -> foreign, never touched, re-mint (<= 3). Release order: the
    DESCRIPTOR (``finally``), then the PATHNAME. ``fault`` = ``(family, message)``, always the
    ORIGINAL fault; cleanup diagnostics travel only in ``cleanup_failures`` [SA2-1][SA3-2].
    Returns ``(final, sha256, fault, cleanup)``: the digest is read HERE, after the re-gate and
    while the file is still this call's own, so a failed read unlinks an OWNED file [A2].
    """
    with open(tmp_png, "rb") as fh:
        data = fh.read()
    for _ in range(MAX_MINT_ATTEMPTS):
        index = next_index_in_dir(out_dir, _mtf_index)
        if index is None:
            return None, None, ("workspace_unlistable", f"{out_dir!r} could not be listed"), []
        if index >= 10 ** 9:  # the honest edge of a finite parser width [P2R2-7]
            return None, None, ("workspace_unlistable", "the minted-index space is exhausted (a "
                                "foreign mtf_vs_field_<9 digits>.png sits in the output "
                                "directory); pass an explicit path"), []
        final = os.path.join(out_dir, "mtf_vs_field_%04d.png" % index)
        try:
            fd = os.open(final, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except FileExistsError:
            continue
        except OSError as exc:
            return None, None, ("render_failed", f"exclusive open of {final!r} failed: "
                                f"{safe_exc(exc)}"), []
        fault, close_fault, cleanup = None, None, []
        try:
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                try:
                    os.close(fd)
                except Exception as exc:  # noqa: BLE001 — broad by design; recorded, never replaces
                    cleanup.append("fd:" + final)
                    close_fault = f"close of {final!r} failed: {safe_exc(exc, repr_form=True)}"
        except Exception as exc:  # noqa: BLE001 — broad by design: an OWNED file is unlinked
            fault = f"copy into {final!r} failed: {safe_exc(exc, repr_form=True)}"
        fault = fault or close_fault  # the ORIGINAL fault wins by construction
        if fault is None:
            try:  # re-gate + digest while the file is still this call's own [A2][#5]
                if not _is_png(final):
                    fault = f"the published {final!r} failed the PNG magic-byte re-gate"
                else:
                    with open(final, "rb") as fh:
                        return final, hashlib.sha256(fh.read()).hexdigest(), None, cleanup
            except Exception as exc:  # noqa: BLE001 — broad by design, no engine here
                fault = f"re-gate/digest of {final!r} failed: {safe_exc(exc, repr_form=True)}"
        try:
            os.unlink(final)
        except Exception:  # noqa: BLE001 — broad by design: the owned final is NAMED, never lost
            cleanup.append(final)
        return None, None, ("render_failed", fault), cleanup
    return None, None, ("workspace_unlistable", f"{MAX_MINT_ATTEMPTS} consecutive name "
                        "collisions in the output directory; nothing was published"), []


def _publish(model, target, minted):
    """``(final, sha256, overwrote, fault, cleanup_failures)``: render, gate, publish."""
    if not minted:  # [R-D] the caller's pathname: disclosed, never unlinked
        overwrote = os.path.exists(target)
        final, sha, fault, published = render_png(model, target)
        return final, sha, overwrote, (("render_failed", fault, published) if fault else None), []
    try:  # BOUNDED (round 5): an ACL-denied folder returns this fault, never a TMP_MAX retry loop
        tmp_dir = exclusive_temp(target, mkdir=True)
    except OSError as exc:  # nothing was created, so nothing is owned; the fault RETURNS, never raises
        return None, None, None, ("render_failed", f"no private render directory: {safe_exc(exc)}"), []
    cleanup = []
    try:  # (no .png name in out_dir) the private dir is an OWNED resource: disclosed [A3]
        tmp = os.path.join(tmp_dir, "render.png")
        fault = render_png(model, tmp)[2]  # the 4th element: the private temp, reaped below
        if fault or not _is_png(tmp):
            return None, None, None, ("render_failed", fault or "temp failed the PNG gate"), \
                cleanup
        final, sha, fault, owned = _publish_exclusive(tmp, target)
        cleanup += owned
        return final, sha, None, fault, cleanup
    finally:
        failed = []  # appended after `return`: the returned tuple holds THIS list object
        shutil.rmtree(tmp_dir, **{_RMTREE_HOOK: lambda *_: failed.append(1)})
        if failed:
            cleanup.append(tmp_dir)


def render_mtf_vs_field(session, params):
    """The handler (the steps, in order). Raises only ``ToolParamError``."""
    frequencies = _validate_frequencies(params.get("frequencies"))
    sample_size = _validate_sample_size(params.get("sample_size"))
    path = params.get("path")
    if path is not None:  # [P2R3-OWN-2] a .png FILE, or a FOLDER to mint into
        if not isinstance(path, str) or not str.strip(path) or "\x00" in path:
            raise ToolParamError(f"path must be a non-empty string without NUL, got {safe_repr(path)}")
        _refuse_non_png_extension(path)
    try:
        return _render(session, frequencies, sample_size, path, params.get("reference"))
    except _Refuse as r:
        return _ac.error_envelope(TOOL, r.family, str(r), **r.extra)


def _geometry(session, system):
    """Steps 5-7: fields, type, primary, image surface, formula ray, the one trace."""
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
        _confirm_engine_alive(system)  # a fenced helper swallowed a failure
        raise _Refuse("field_geometry_unproven", f"primary wavelength: {pfault}; image "
                      f"surface: {image_surf}")
    for f in fields:
        coords = _chief_pupil_coords(f)
        if coords is None:
            raise _Refuse("field_geometry_unproven", f"field {f['index']} has VCY {f['vcy']} "
                          ">= 1: the chief-ray formula is undefined")
        f["py_traced"] = coords[1]
    waves, wfault = _read_wavelengths(system)
    return field_type, fields, primary, image_surf, waves, wfault, _trace_chief_heights(
        session, fields, primary, image_surf)


def _render(session, frequencies, sample_size, path, reference):
    system = session.system
    try:  # the fenced helper's str(member) can raise on a hostile/dead token [#6]
        lens_unit = _ac._lens_units_string(system)
    except Exception as exc:  # noqa: BLE001 — the documented unresolved-unit path
        t = _transport_loss(exc)
        if t is not None:
            raise t
        lens_unit = "unknown"
    if lens_unit == "unknown":  # the fenced helper may have swallowed a dead engine
        _confirm_engine_alive(system)
    frequency_units, ref, ref_digest = f"cycles/{lens_unit}", None, None
    if reference is not None:
        try:
            ref = validate_reference(reference, frequencies=frequencies, lens_unit=lens_unit,
                                     frequency_units=frequency_units)
        except ReferenceInvalid as exc:
            raise _Refuse("reference_invalid", str(exc)) from None
        ref_digest = reference_digest(ref)  # before the engine AND any file [A2]
    target, minted, fault, root, rfault = _resolve_png_path(session, path)
    if fault:
        raise _Refuse("workspace_unlistable", fault)
    ident = _config_identity(system)
    flags = ident.pop("flags") + (["lens_unit_unresolved"] if lens_unit == "unknown" else []) + (
        ["workspace_root_unresolved"] if rfault else [])  # an absolute path published regardless
    field_type, fields, primary, image_surf, waves, wfault, heights = _geometry(session, system)
    if heights["failed"]:  # [P-5][CR-Q1-8]: harvest messages; the FFT's own verdict discloses
        try:  # ONE window; its outcome — success, refusal OR throw — is a DISCLOSURE
            disc = _run_fftmtf(system, frequencies, sample_size, harvest=True)
        except _Refuse as harvest:
            disc = harvest.extra
        except Exception as exc:  # noqa: BLE001 — the harvest could not run; say why
            t = _transport_loss(exc)
            if t is not None:
                raise t  # [R-A′] transport loss outranks chief_ray_failed
            disc = {"messages": [], "flags": [{"harvest_run_failed": safe_exc(exc)}]}
        raise _Refuse("chief_ray_failed", f"the chief ray of field(s) {heights['failed']} could "
                      f"not be traced: {CHIEF_REMEDY}", remedy=CHIEF_REMEDY, failed_fields=[
                          {"field_index": f["index"], "field_value": f["y"],
                           "error_code": h["error_code"], "surface_reached_y": h["y_raw"]}
                          for f, h in zip(fields, heights["per_field"])
                          if f["index"] in heights["failed"]],
                      **{k: disc.get(k) for k in ("messages", "sampling", "settings_read_back",
                                                  "settings_written", "flags")})
    try:
        fft = _run_fftmtf(system, frequencies, sample_size)
    except _Refuse as r:
        if r.family == "analysis_empty" and field_type == "ObjectHeight":
            raise _Refuse(r.family, str(r) + OBJECT_HEIGHT_HINT, **r.extra) from None
        raise
    diffraction, bound, bind_flags = _bind_series(fft["series"], fields, lens_unit, field_type)
    points, missing, unplaced, oor_flags, beyond_cutoff = _assemble(
        bound, heights, frequencies, diffraction)
    sampling = dict(fft["sampling"])
    if any("Sampling too low" in m for m in fft["messages"]):
        flags.append("engine_sampling_warning")
        nxt = NEXT_SAMPLE_SIZE.get(sampling["requested"])
        sampling["note"] = SAMPLING_NOTE + (f"pass sample_size={nxt}" if nxt else
                                            "no larger sampling is available")
    flags += (["wavelengths_unread"] if wfault else []) + fft["flags"] + bind_flags + oor_flags \
        + _height_flags(fields, heights["per_field"], field_type)
    env_fields = [{"index": f["index"], "value": f["y"], "weight": f["weight"], "hy": f["hy"],
                   "py_traced": f["py_traced"], "image_height": h["image_height"],
                   "image_height_status": h["status"], "vignette_surface": h["vignette_surface"],
                   "vignetting_factors_present": any(f[k] != 0 for k in ("vdx", "vdy", "vcx", "vcy"))}
                  for f, h in zip(fields, heights["per_field"])]
    curve_note = (f"Only the design's {len(fields)} defined fields are computed (filled "
                  "markers); straight segments between them are NOT computed.")
    model = plot_model(points, env_fields, frequencies, ref, lens_unit, dict(
        ident, frequency_units=frequency_units, curve_note=curve_note, missing=missing,
        messages=fft["messages"], sampling=sampling, modulation_out_of_range=(
            oor_flags[0]["modulation_out_of_range"] if oor_flags else [])))
    try:  # a publish fault of ANY type is render_failed, disclosed by type
        final, sha, overwrote, fault, cleanup = _publish(model, target, minted)
    except Exception as exc:  # noqa: BLE001
        final, sha, overwrote, fault, cleanup = None, None, None, (
            "render_failed", safe_exc(exc, repr_form=True)), []
    if fault:
        raise _Refuse(fault[0], fault[1], partial={"points": points},
                      **({"cleanup_failures": cleanup} if cleanup else {}),
                      **({"published_path": fault[2], "overwrote": overwrote}
                         if len(fault) > 2 and fault[2] else {}))
    # after the publish: dict assembly ONLY — nothing here can raise [A2]
    env = {"ok": True, "tool": TOOL, "path": final, "png_sha256": sha, "png_minted": minted,
           "workspace_root": root, "output_dir": target if minted else os.path.dirname(final),
           **ident, "frequencies": frequencies, "frequency_units": frequency_units,
           "x_quantity": "real_image_height", "x_units": lens_unit, "y_units": "modulation 0..1",
           "field_type": field_type, "fields": env_fields, "points": points,
           "diffraction_limit": diffraction, "missing": missing, "unplaced_points": unplaced,
           "beyond_cutoff": beyond_cutoff, "sampling": sampling, "wavelengths": waves,
           **{k: fft[k] for k in ("grid_max", "grid_min", "max_frequency_auto_extended",
                                  "max_frequency_applied", "settings_read_back",
                                  "settings_written", "messages")},
           "ray_identity": heights["ray_identity"], "wavelength_basis": WAVELENGTH_BASIS,
           "image_height_basis": {"ray": RAY_BASIS, "wave_index": primary, "wave_um": next(
               (w["um"] for w in waves or () if w["primary"]), None), "surface": image_surf,
               "note": HEIGHT_NOTE},
           "curve_note": curve_note, "n_field_points": len(points) - len(unplaced),
           "flags": flags}
    if not minted:
        env["overwrote"] = overwrote
    if cleanup:  # an owned cleanup failed; the success is still a success [A3]
        env["cleanup_failures"] = cleanup
    if ref is not None:
        placed = [p["image_height"] for p in points if p["image_height"] is not None]
        env["reference"] = {
            "source": ref["source"], "kind": ref["kind"], "aperture": ref["aperture"],
            "digest": ref_digest, "comparison_computed": False,
            "extent": [min(c["points"][0][0] for c in ref["curves"]),
                       max(c["points"][-1][0] for c in ref["curves"])],
            "measured_x_max": max(placed) if placed else None,
            "curves": [{"frequency": c["frequency"], "orientation": c["orientation"],
                        "n_points": len(c["points"])} for c in ref["curves"]]}
        env["beyond_reference_extent"] = model["beyond_reference_extent"]
    return env


RENDER_MTF_VS_FIELD_SPEC = ToolSpec(
    name=TOOL,
    handler=render_mtf_vs_field,
    required_params=(),
    param_types={"frequencies": "array", "sample_size": "integer", "path": "string",
                 "reference": "object"},
    description=(
        "MTF vs REAL image height (geometric chief ray, lens units); T solid, S dashed. Only "
        "defined fields computed, not segments between. Bound by content (ties: "
        "list order, flagged); diffraction limit apart. Missing never drawn as 0. "
        "Untraceable chief ray REFUSES (chief_ray_failed). Engine messages verbatim ('Sampling "
        "too low': pass sample_size). Reference: context, no comparison. ACTIVE config named "
        "(set_current_configuration switches). PNG minted in <workspace_root>/evaluation/ "
        "(no overwrite); path *.png = file, else a folder. Not the reviewable figure: "
        "save_candidate's png_sha256 PNG is."),
)

TOOL_SPECS = (RENDER_MTF_VS_FIELD_SPEC,)
