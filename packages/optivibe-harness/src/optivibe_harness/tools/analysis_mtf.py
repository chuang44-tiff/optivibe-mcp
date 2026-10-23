"""tools/analysis_mtf.py — get_mtf: FFT MTF typed-array extraction.

``get_mtf`` runs the FFT MTF analysis (``Analyses.New_FftMtf()`` ->
``ApplyAndWaitForCompletion`` -> ``GetResults()``) and marshals each data series'
X (spatial-frequency grid, ``System.Double[]`` rank 1, length 300 in the baseline)
and Y (modulation, ``System.Double[,]`` rank 2, shape [300, 2]) arrays to JSON
lists. Per the locked column ordering: col0 = Tangential, col1 = Sagittal.
Every cell passes through ``safe_float``.

Completion canary (§d): ``results.NumberOfDataSeries > 0``; else the
structured ``analysis_empty`` envelope.

This module also hosts ``_analysis_idm`` — the shared resolver for an
``AnalysisIDM`` member off the live ``ZOSAPI.Analysis`` namespace — reused by
``analysis_spot`` and ``analysis_graphic``.

Live ZOS-API integration: exercised by the live test (4 series, Y[3]~0.99888
within 2%); unit-tested against a fixture-seeded fake MTF result.
"""
import math
import re

from .._io import safe_exc, safe_float
from ..errors import (AnalysisResultError, SessionClosedError, ToolParamError,
                      map_dotnet_exception)
from ..server import ToolSpec, _wedge_recorded
from . import _analysis_common as _ac
from . import _collimation as _col
from . import _config_common as _cfg
from ._optimize_common import _base_token  # the ONE base-slot token read

#: Series 0 of an FFT MTF run is the diffraction limit ONLY while the engine's saved Show
#: Diffraction Limit setting is on; with it off the first series is field 1. The engine's own
#: Description is the proof (``render_mtf_vs_field`` binds on it too and imports this name).
_DIFF_RE = re.compile(r"^Field:\s*Diffraction limit$", re.IGNORECASE)
_DL_HIDDEN_NOTE = (
    "the diffraction-limit curve is not in this run (the saved MTF settings hide it): "
    "series 0 is the first field, not the diffraction limit"
)
_DL_NOT_ON = "; get_mtf could not turn the diffraction limit on for this run"
_DL_UNREAD_NOTE = (
    "series 0 could not be identified as the diffraction limit (its description was not "
    "readable), so it is not named; series 0 may be the first field"
)

# (S7 #16) Auto-extend the FFT-MTF grid to cover requested at_frequencies. The default
# grid caps at 30.0 cyc/mm (probe-frozen), so an at_frequencies element above it silently
# returns null+note. When max_frequency is ABSENT, auto-raise the ceiling to cover the
# request (round up with a small pad so the target sits strictly INSIDE the grid, not at
# the excluded edge). An explicit max_frequency is HONORED — never overridden.
_MTF_DEFAULT_GRID_MAX = 30.0     # probe: the engine's default FFT-MTF grid cap (cyc/mm)
_MTF_AUTO_EXTEND_PAD = 1.1       # round needed UP so the target sits strictly INSIDE the
                                 # grid (not at the excluded grid edge); ceil() after
_MTF_AUTO_EXTEND_MAX = 2000.0    # SOFT cap (cyc/mm): beyond it, fall through to the honest
                                 # null+note, never auto-author a garbage grid


def _auto_extended_ceiling(frequencies):
    """The FFT-MTF grid ceiling that covers ``frequencies``, or ``None`` when the default grid does.

    ONE rule for every tool that asks for the engine's MTF at chosen frequencies: only a request
    above the default cap (and within the soft cap) raises the ceiling, rounded up with a pad so the
    target sits strictly inside the grid. The caller decides whether an explicit ceiling overrides it."""
    needed = max(frequencies)
    if _MTF_DEFAULT_GRID_MAX < needed <= _MTF_AUTO_EXTEND_MAX:
        return float(max(_MTF_DEFAULT_GRID_MAX, math.ceil(needed * _MTF_AUTO_EXTEND_PAD)))
    return None


def _series_xy(ds, index, pair_note=""):
    """One data series -> ``(frequency, tangential, sagittal, mismatch)`` (marshalled, split, paired).

    X and Y both go through the marshalling helper that owns the None/empty canary, and the split
    owns the 2-column check: either raises ``AnalysisResultError`` (the caller maps its family).
    ``mismatch`` is ``None``, or the text of the frequency-count != modulation-row-count fault: a
    misaligned pair must never go out as a number. ``pair_note`` is appended to that text."""
    freq = _ac._marshal_array(ds.XData.Data)
    tangential, sagittal = _ac._split_mtf_columns(_ac._marshal_array(ds.YData.Data))
    mismatch = None
    if len(freq) != len(tangential):
        mismatch = (f"series {index}: frequency length {len(freq)} != modulation row "
                    f"count {len(tangential)}{pair_note}")
    return freq, tangential, sagittal, mismatch


def _resolve_single_config(system, params, tool_name):
    """Resolve a SINGLE-config selector (None|int) — ``config="all"`` is REFUSED (D1).

    MTF/spot are the heavy-analysis class (the render-obscured-slow stall on the single
    serialized seat), so they offer ``config=None|int`` but NOT the ``"all"`` sweep. Thin
    wrapper over the shared ``_config_common.resolve_single_config_selector`` (one
    contract) — extracts ``config`` from ``params`` and delegates. Returns the int config to
    read at (``None`` -> the active config, no switch), or RAISES ``ToolParamError``.
    """
    config = params.get("config") if isinstance(params, dict) else None
    return _cfg.resolve_single_config_selector(system, config, tool_name)


def _validate_at_frequencies(value):
    """Validate the optional ``at_frequencies`` param -> a list of finite floats >= 0.

    Mirrors how ``series`` is validated (a strict pre-flight raise -> the dispatch
    envelope catches it). Requires a NON-EMPTY list/tuple of real numbers (an int
    or float, NOT a bool) that are each FINITE and ``>= 0`` (a negative frequency /
    nan / inf / string / bool element is rejected). Returns the list of floats.
    """
    if not isinstance(value, (list, tuple)) or isinstance(value, (str, bytes)):
        raise ToolParamError(
            f"at_frequencies must be a non-empty list of numbers >= 0, got "
            f"{type(value).__name__} {value!r}"
        )
    if len(value) == 0:
        raise ToolParamError("at_frequencies must be a non-empty list of numbers >= 0")
    out = []
    for f in value:
        if isinstance(f, bool) or not isinstance(f, (int, float)):
            raise ToolParamError(
                f"at_frequencies elements must be numbers, got "
                f"{type(f).__name__} {f!r}"
            )
        try:
            ff = float(f)
        except OverflowError:  # (0.1.13 E5): a JSON int too big for a float
            raise ToolParamError(
                f"at_frequencies elements must be finite numbers representable as a float, got an "
                f"integer of {f.bit_length()} bits"
            ) from None
        if not math.isfinite(ff):
            raise ToolParamError(
                f"at_frequencies elements must be finite numbers, got {f!r}"
            )
        if ff < 0:
            raise ToolParamError(
                f"at_frequencies elements must be >= 0, got {f!r}"
            )
        out.append(ff)
    return out


def _interp_at(freq_grid, y_values, target):
    """Linearly interpolate ``y_values`` at ``target`` over the monotonic ``freq_grid``.

    ``freq_grid`` is the spatial-frequency X grid (monotonic increasing, the FFT
    MTF grid); ``y_values`` is the matching modulation column (same length). Rules:

    - an EXACT grid hit returns the exact grid value;
    - a ``target`` strictly between two grid points is LINEAR-interpolated between
      the bracketing pair;
    - ``target`` below the grid min (usually 0) interpolates from the first point
      (the grid starts at 0 in practice, so this is normally an exact hit);
    - ``target`` ABOVE the grid max returns ``(None, "beyond MTF grid max …")`` (MTF
      past the cutoff is ~0 but we do NOT fabricate it).

    Returns ``(value, note)`` — ``note`` is ``None`` unless the point is beyond the
    grid max. A non-finite marshalled grid/Y cell (a string sentinel from
    ``safe_float``) is skipped for interpolation math by treating it as unusable ->
    the bracketing pair is used as-is (the sentinel surfaces in the full-grid mode;
    in summary mode an interpolation that would need a sentinel cell returns None).
    """
    n = len(freq_grid)
    if n == 0:
        return None, "empty MTF grid"
    fmax = freq_grid[-1]
    fmin = freq_grid[0]
    # Beyond the grid max -> null + note (MTF past cutoff ~ 0; do not fabricate).
    if isinstance(fmax, (int, float)) and target > fmax:
        return None, f"beyond MTF grid max {fmax}"
    # At/below the grid min -> the first grid value (the grid starts at 0).
    if isinstance(fmin, (int, float)) and target <= fmin:
        return _as_finite(y_values[0]), None
    # Find the bracketing pair [i-1, i] with freq_grid[i-1] <= target <= freq_grid[i].
    for i in range(1, n):
        lo = freq_grid[i - 1]
        hi = freq_grid[i]
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            continue
        if lo <= target <= hi:
            ylo = _as_finite(y_values[i - 1])
            yhi = _as_finite(y_values[i])
            if ylo is None or yhi is None:
                return None, "non-finite modulation at the interpolation point"
            if hi == lo:  # a duplicated grid point -> exact value
                return ylo, None
            frac = (target - lo) / (hi - lo)
            return ylo + frac * (yhi - ylo), None
    # Should not reach here for an in-range monotonic grid; be defensive.
    return None, f"frequency {target} not bracketed by the MTF grid"


def _as_finite(value):
    """Return ``value`` as a float if it is a finite number, else ``None``.

    A marshalled MTF cell is either a finite float or a ``safe_float`` STRING
    sentinel ("inf"/"-inf"/"nan"); a sentinel is not interpolatable, so it maps to
    ``None`` (the caller emits a null point rather than fabricating a number).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _analysis_idm(system, member_name):
    """Resolve an ``AnalysisIDM`` enum member off the live ``ZOSAPI.Analysis``.

    A fake system injects an ``_analysis_idm`` mapping (``{name: member}``) so unit
    tests resolve without the backend; otherwise the live ``ZOSAPI.Analysis``
    namespace's ``AnalysisIDM`` enum is imported and the member resolved by name.
    """
    injected = getattr(system, "_analysis_idm", None)
    if injected is not None and member_name in injected:
        return injected[member_name]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Analysis as _an  # type: ignore

        return getattr(_an.AnalysisIDM, member_name)
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve AnalysisIDM.{member_name} from ZOSAPI.Analysis: {exc}"
        )


#: Served on a latched tool slot: the collimated-output check traces rays through the slot, so it is
#: skipped there and the caller is told the flag's absence proves nothing.
_COLLIMATED_CHECK_SKIPPED_MSG = (
    "collimated-output check not run: the engine's tool slot is busy, so whether this output is "
    "collimated (afocal) is unknown and the absence of a collimated_output flag does not rule it out."
)


def _read_engine_messages(results):
    """``(messages, unread)``: the engine's own analysis messages, read as text (empty list when none).

    A read fault is disclosed (``unread=True``, empty list), never fabricated; a transport loss
    is re-raised unchanged."""
    try:
        return [_base_token(results.GetMessageAt(i).Text)
                for i in range(int(results.NumberOfMessages))], False
    except Exception as exc:  # noqa: BLE001 -- disclosed as engine_messages_unread
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        return [], True


def get_mtf(session, params):
    """Run FFT MTF and return per-series frequency/tangential/sagittal lists.

    ``series`` (int | None, default None=all) restricts the output to one series
    (field); ``max_frequency`` (number, optional) sets the FFT spatial-frequency
    grid ceiling via ``IAS_FftMtf.MaximumFrequency`` (default: the engine default,
    probe-measured at 30 cyc/mm -- ``_MTF_DEFAULT_GRID_MAX``). A zero ``NumberOfDataSeries`` returns the
    ``analysis_empty`` envelope (§d).

    SUMMARY MODE (``at_frequencies``, a list of finite numbers >= 0): instead of
    the full ~300-point grid (the overflow), each series returns ONLY
    the interpolated tangential + sagittal at each requested frequency
    (``{index, label, at:[{frequency, tangential, sagittal, [note]}]}``). An exact
    grid hit is exact; a frequency beyond the grid max is ``null`` + a note. With NO
    ``at_frequencies`` the full-grid behaviour is unchanged.

    ``config`` (None|int) selects the configuration — SINGLE-config only,
    ``config="all"`` is REFUSED (MTF is the heavy-analysis class). Reads inside a
    ``with_configuration`` wrap that ALWAYS restores the active config.
    """
    cfg_idx = _resolve_single_config(session.system, params, "get_mtf")
    if cfg_idx is None:
        result = _get_mtf_at(session, params)
        if isinstance(result, dict):
            result.setdefault(
                "config_evaluated", _cfg.safe_current_configuration(session.system)
            )
        return result
    with _cfg.with_configuration(session.system, cfg_idx) as ctx:
        result = _get_mtf_at(session, params)
    if isinstance(result, dict):
        result.setdefault("config_evaluated", cfg_idx)
        if not ctx["restore_verified"]:
            result["mutation_warning"] = ctx["mutation_warning"]
        if not ctx["switched"] and ctx["mutation_warning"]:
            result.setdefault("config_switch_warning", ctx["mutation_warning"])
    return result


def _show_diffraction_limit(analysis):
    """Turn the diffraction-limit curve ON for THIS run and read it back; True only if it reads
    back ``True``. The concrete settings view (``__implementation__``, as ``render_mtf_vs_field``
    uses) is required: a bare ``IAS_`` base has no such property and would take a phantom attribute.
    Never saved to disk (no ``Save()``): a saved setting stays as the user left it. A transport
    loss propagates; any other fault is "not turned on", and the series-0 Description decides."""
    try:
        typed = getattr(analysis.GetSettings(), "__implementation__", None)
        typed.ShowDiffractionLimit = True
        return typed.ShowDiffractionLimit is True
    except Exception as exc:  # noqa: BLE001 -- the Description binding below is the proof either way
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        return False


def _diffraction_limit_binding(results, turned_on=True):
    """``(series, shown, note)``: ``(0, True, None)`` when series 0's engine Description says it is
    the diffraction limit; ``(None, False, note)`` when it was READ and says otherwise (the saved
    settings hide the limit); ``(None, None, note)`` when it could not be read (layout unknown).
    ``shown`` is the explicit marker consumers use: null is "unread", never "hidden". A transport
    loss still propagates. ``turned_on`` False (the write or its read-back failed) adds a short
    sentence to the note of a run whose series 0 is not proven the limit. The series count against the lens's field count is NOT used to
    prove the layout of an unread run."""
    try:
        raw = results.GetDataSeries(0).Description
        if not isinstance(raw, str):  # None / a non-string is "unread", never "read and not the limit"
            return None, None, _DL_UNREAD_NOTE + ("" if turned_on else _DL_NOT_ON)
        desc = _base_token(raw)
    except Exception as exc:  # noqa: BLE001 -- an unreadable label is "not proven"; transport loss re-raises
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        return None, None, _DL_UNREAD_NOTE + ("" if turned_on else _DL_NOT_ON)
    if isinstance(desc, str) and _DIFF_RE.match(desc):
        return 0, True, None
    return None, False, _DL_HIDDEN_NOTE + ("" if turned_on else _DL_NOT_ON)


def _get_mtf_at(session, params):
    """The pure per-config FFT-MTF body (read at the ACTIVE config). See ``get_mtf``."""
    system = session.system

    series_sel = params.get("series")
    if series_sel is not None:
        if isinstance(series_sel, bool) or not isinstance(series_sel, int):
            raise ToolParamError(
                f"series must be an integer index, got {series_sel!r}"
            )
        series_sel = int(series_sel)

    at_frequencies = None
    if params.get("at_frequencies") is not None:
        at_frequencies = _validate_at_frequencies(params["at_frequencies"])

    max_frequency = params.get("max_frequency")
    if max_frequency is not None:
        if isinstance(max_frequency, bool) or not isinstance(
            max_frequency, (int, float)
        ):
            raise ToolParamError(
                f"max_frequency must be a finite positive number, got "
                f"{type(max_frequency).__name__} {max_frequency!r}"
            )
        max_frequency = float(max_frequency)
        if not math.isfinite(max_frequency) or max_frequency <= 0:
            raise ToolParamError(
                f"max_frequency must be a finite positive number (> 0), got "
                f"{max_frequency!r}"
            )

    # (S7 #16) Auto-extend the grid for a requested at_frequency above the default cap.
    # Fires ONLY when max_frequency is ABSENT (an explicit ceiling is the caller's stated
    # intent — never overridden; a beyond-explicit element stays the existing null+note).
    # A request beyond the SOFT cap falls through to the honest "beyond MTF grid max"
    # null+note (NOT a refusal, NOT a garbage grid). _validate_at_frequencies already
    # rejected a non-finite element pre-mutation.
    max_frequency_auto_extended = False
    if at_frequencies and max_frequency is None:
        extended = _auto_extended_ceiling(at_frequencies)
        if extended is not None:
            max_frequency = extended
            max_frequency_auto_extended = True

    try:
        analysis = system.Analyses.New_FftMtf()
    except Exception as exc:  # noqa: BLE001 -- a window that will not open is a family, not `internal`
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        return _ac.error_envelope(
            "get_mtf", "analysis_malformed",
            "the FFT MTF window could not be opened: " + safe_exc(exc, repr_form=True),
        )
    try:
        dl_turned_on = _show_diffraction_limit(analysis)
    except BaseException:  # a transport loss here would otherwise skip the window's Close()
        try:
            analysis.Close()
        except Exception:  # noqa: BLE001 -- teardown must never mask the real error
            pass
        raise
    max_frequency_applied = None
    if max_frequency is not None:
        max_frequency_applied = False
        try:
            settings = analysis.GetSettings()
            # GetSettings() returns the bare IAS_ base, which has NO MaximumFrequency
            # (analyses.md §6 — the documented "bare IAS_ base" hazard). A plain
            # `settings.MaximumFrequency = X` sets a phantom Python attr on the COM
            # wrapper and the grid never moves (live-falsified). The concrete
            # AS_FftMtf view (which HAS the property) is reached via pythonnet's
            # __implementation__; the getattr fallback keeps unit fakes working.
            typed = getattr(settings, "__implementation__", settings)
            typed.MaximumFrequency = max_frequency
            # Read-back-as-proof: the typed view actually accepted the ceiling.
            if math.isclose(
                float(typed.MaximumFrequency), max_frequency,
                rel_tol=1e-9, abs_tol=1e-9,
            ):
                max_frequency_applied = True
        except Exception:  # noqa: BLE001 — degrade to engine default
            pass
    with _ac._run_analysis(analysis) as results:
        n = int(results.NumberOfDataSeries)
        # The engine says WHY a result is empty or inaccurate; read it before any early return.
        messages, messages_unread = _read_engine_messages(results)
        # Completion canary (§d): zero series is a void result.
        if n <= 0:
            extra = {"messages": messages}
            if messages_unread:
                extra["engine_messages_unread"] = True
            return _ac.error_envelope(
                "get_mtf", "analysis_empty",
                f"FFT MTF produced {n} data series"
                + (f"; engine messages: {messages}" if messages else ""),
                **extra,
            )

        dl_series, dl_shown, dl_note = _diffraction_limit_binding(results, dl_turned_on)

        if series_sel is not None:
            if not (0 <= series_sel < n):
                raise ToolParamError(
                    f"series {series_sel} out of range; valid 0..{n - 1}"
                )
            indices = [series_sel]
        else:
            indices = list(range(n))

        series_out = []
        for i in indices:
            ds = results.GetDataSeries(i)
            label = str(ds.SeriesLabels[0])
            # Per-series completion canary (§d): NumberOfDataSeries>0 is NOT
            # proof of data — a stale/void completion can carry an empty per-series
            # payload. Route BOTH X and Y through the marshalling helper that owns
            # the None/empty `analysis_empty` canary (never marshal *.Data directly
            # and bypass the guard), then assert the frequency<->modulation length
            # pairing (X.Length == Y rows) so a misaligned result surfaces as a
            # structured error, never `ok:true` with mismatched/empty arrays.
            try:
                # The split must sit INSIDE this try (NEW-1): a non-2-col Y raises
                # AnalysisResultError(family="analysis_malformed"); outside it, that
                # exception would escape past the tool boundary instead of returning
                # the locked {ok:false} envelope.
                freq, tangential, sagittal, mismatch = _series_xy(
                    ds, i, " (X/Y arrays misaligned)")
            except AnalysisResultError as exc:
                return _ac.error_envelope(
                    "get_mtf", exc.family, str(exc), series_index=i,
                )
            if mismatch is not None:
                # Frequency count != modulation row count: frequency[i] no longer
                # pairs with tangential[i]. A wrong-number-as-success the canary
                # must intercept (BUG2/BUG3).
                return _ac.error_envelope(
                    "get_mtf", "analysis_malformed", mismatch, series_index=i,
                )
            if at_frequencies is not None:
                # SUMMARY MODE: interpolate T/S at each requested frequency and OMIT
                # the full freq/tangential/sagittal arrays (the ~300-point overflow).
                at = []
                for target in at_frequencies:
                    t_val, t_note = _interp_at(freq, tangential, target)
                    s_val, s_note = _interp_at(freq, sagittal, target)
                    point = {
                        "frequency": target,
                        "tangential": safe_float(t_val) if t_val is not None else None,
                        "sagittal": safe_float(s_val) if s_val is not None else None,
                    }
                    note = t_note or s_note
                    if note is not None:
                        point["note"] = note
                    at.append(point)
                series_out.append({"index": i, "label": label, "at": at})
            else:
                series_out.append(
                    {
                        "index": i,
                        "label": label,
                        "frequency": freq,
                        "tangential": tangential,
                        "sagittal": sagittal,
                    }
                )

    # The lens unit is still read through the unit helper, for its transport-loss re-raise only: the
    # spatial-frequency unit is NOT derived from it -- see ``x_units`` below.
    _ac._lens_units_string(system)
    grid_max = None
    if series_out and "frequency" in series_out[0]:
        freq_data = series_out[0].get("frequency", [])
        if freq_data:
            last = freq_data[-1]
            if isinstance(last, (int, float)) and math.isfinite(last):
                grid_max = last
    result = {
        "ok": True,
        "number_of_data_series": n,
        # series 0, proved by its engine Description (whichever ``series`` was asked for); null when
        # it is not the diffraction limit or could not be read, with ``diffraction_limit_note``
        "diffraction_limit_series": dl_series,
        # true: series 0 proved the limit; false: read and it is not (limit hidden); null: unread
        "diffraction_limit_shown": dl_shown,
        "x_label": "Spatial Frequency",
        # the FFT MTF abscissa is cycles/mm for EVERY lens unit, measured mm/cm/in/m
        "x_units": "cycles/mm",
        "y_units": "modulation (dimensionless 0..1)",
        "series": series_out,
        # the engine's own words (e.g. 'Sampling too low, data inaccurate!'), always present
        "messages": messages,
    }
    if dl_note is not None:
        result["diffraction_limit_note"] = dl_note
    if dl_shown is not True:
        # Not proven shown: a consumer that maps series by position (idx0 = the limit) would be
        # wrong, and ``validity_warning`` is the key every internal reader already screens.
        result["validity_warning"] = dl_note
    if messages_unread:
        result["engine_messages_unread"] = True
    if grid_max is not None:
        result["grid_max"] = grid_max
    # Active disclosure: when a max_frequency was requested, say
    # whether the engine actually accepted it — a silent degrade is now explicit,
    # not inferred from grid_max alone.
    if max_frequency_applied is not None:
        result["max_frequency_applied"] = max_frequency_applied
    # (S7 #16) Disclose the auto-extension ONLY when it fired (default-grid result key
    # set stays byte-identical). max_frequency_auto_extended:true + max_frequency_applied:
    # false is the honest "tried to extend, engine didn't accept it" disclosure.
    if max_frequency_auto_extended:
        result["max_frequency_auto_extended"] = True
    # A collimated (afocal) output: flag it and redirect to the angular tools. The detector
    # traces rays through the tool slot, so it is skipped (and said so) while that slot is
    # latched; the MTF answer itself is never refused or nulled for this. Adds keys ONLY when
    # it fires, so a normal imager's key set is unchanged.
    if _wedge_recorded(session):
        result["flags"] = [_COLLIMATED_CHECK_SKIPPED_MSG]
    else:
        collimated_flags = _col.collimated_flags(system)
        if collimated_flags:
            result["collimated_output"] = True
            result["flags"] = collimated_flags
    return result


GET_MTF_SPEC = ToolSpec(
    name="get_mtf",
    handler=get_mtf,
    required_params=(),
    param_types={
        "series": "integer",
        "max_frequency": "number",
        "at_frequencies": "array",
        # config is SINGLE-config only (None|int); 'all' is refused. Advertised "number".
        "config": "number",
    },
    description=(
        "Measure image contrast vs spatial frequency (FFT MTF). Pass "
        "max_frequency to set the spatial-frequency grid ceiling (default: "
        "engine default); grid_max in the result reports the actual extent. By "
        "DEFAULT returns the full per-series grid plus tangential and sagittal "
        "modulation (col0=tangential, col1=sagittal) — can be ~300 points per "
        "series (large). Pass at_frequencies=[f1,f2,...] for SUMMARY mode: each "
        "series returns only the interpolated tangential+sagittal at those "
        "frequencies (compact; null+note past the grid max). The series map: idx0 = "
        "the diffraction-limit series, idx1..N = the fields in order: get_mtf turns the "
        "diffraction-limit curve on for the run. diffraction_limit_shown true: idx0 is the limit; "
        "false: idx0 is field 1; null: unknown, do not map series by position (validity_warning "
        "says so); on-axis T == S. "
        "OBSCURED PUPILS: on a system with a central obscuration (a Cassegrain "
        "secondary shadow) author a CircularObscuration on the obstructing surface "
        "with set_surface_aperture FIRST, so this number is computed on the TRUE "
        "obscured pupil, not the full circle (a central CircularObscuration "
        "min_radius=0, max_radius=R plus an outer CircularAperture forms the "
        "annulus). On a collimated (afocal) output the result carries collimated_output + a flag. "
        "messages holds the engine's own messages verbatim (e.g. 'Sampling too low, data inaccurate!')."
    ),
)

TOOL_SPECS = (GET_MTF_SPEC,)
