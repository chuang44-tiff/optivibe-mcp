"""tools/analysis_spot.py — get_spot: standard-spot RMS/GEO with trace validity.

``get_spot`` runs the StandardSpot analysis
(``Analyses.New_Analysis(AnalysisIDM.StandardSpot)`` -> wait -> ``GetResults()``
-> ``.SpotData``) and reads the per-FIELD RMS/GEO spot radii via the 1-based
``GetRMSSpotSizeFor(field, wave)`` / ``GetGeoSpotSizeFor(field, wave)``. Every
scalar passes through ``safe_float``.

S2 (#7 / #14) — the validity + per-wave rewire:

- **#14 COLLAPSE (probe-falsified):** ``GetRMSSpotSizeFor(field, wave)`` IGNORES
  the ``wave`` arg — it returns the SAME polychromatic (all-wave pooled RMS) value for
  every wave. The old per-(field,wave) matrix was FABRICATED duplication. The
  default output now COLLAPSES the wave dimension -> ONE polychromatic RMS/GEO per
  FIELD (``rms_reference:"polychromatic"``).

- **#14 TRUE per-wave via RSCE (micro-probe CONFIRMED):** ``per_wave:true`` reads
  the ``RSCE(Ring, Wave, Hx, Hy)`` merit operand (the live micro-probe proved RSCE
  HONORS its ``Wave`` slot: 3 distinct on-axis values across waves, unlike the
  inert ``GetRMSSpotSizeFor``) via the stateless named-slot 9-arg
  ``read_operand_slots`` path. RSCE is RMS-only -> per-wave entries OMIT ``geo``
  (``geo_reference:"unavailable_per_wave"``).

  per_wave UNITS + RING. RSCE returns LENS UNITS (mm on a mm lens),
  not micrometres, so ``get_spot`` CONVERTS every per-wave value to um (``_LENS_UNIT_TO_UM``)
  before the beyond-capture check and before it is served, and reads RSCE at ring 5 (ring 3
  was up to 7.2 % off on a measured lens). The lens unit is read ONCE per call
  (``_resolve_spot_units``) and feeds BOTH the ``units`` label and the factor; a ``per_wave``
  call that serves an RMS on a lens unit it cannot convert is REFUSED as a whole
  (``analysis_units_unconvertible``), never served in a unit it is not in. ``rms_raw`` on this
  path is the pre-verdict RSCE reading CONVERTED to ``units`` (not the raw engine reading).
  An out-of-range ``field`` (or, with ``per_wave``, ``wave``) index is refused before any read
  (``ToolParamError``, as ``get_mtf`` and the measurement graders refuse theirs), and an RSCE
  read that returns no number (None, a bool, an int/float subclass, any non-sentinel str) makes that
  entry ``valid:false`` / ``per_wave_rsce_unreadable`` -- EXCEPT a field the cross-check already
  graded blocked / partial_vignette, whose verdict stands (its reason + tallies, ``rms`` null).
  A transport loss from the lens-unit getter or the RSCE read propagates (never served as
  an "unknown" unit or an unreadable value).

- **#7 ALWAYS validity cross-check (the silent-wrong killer):** the spot result has
  NO native validity member (probe headline #1). EVERY call runs ONE full-pupil
  batch ray-trace cross-check (``_spot_validity``): a field with a blocked (M1),
  partially-vignetted (M3), or beyond-capture (M2) pupil reads ``valid:false`` +
  ``rms:null`` + a ``reason`` + ``traced_fraction``, with the RAW reading KEPT in
  ``rms_raw``/``geo_raw`` (on the per_wave path ``rms_raw`` is the pre-verdict RSCE reading
  CONVERTED to ``units``, not the raw engine reading) — NEVER a misleading ``rms:0.0``/huge headline. A genuine
  perfect spot (``~2.83e-13``, all rays traced) reads ``valid:true`` (no
  over-refusal — ``valid`` is never derived from a small RMS, only from
  ``traced_fraction`` + the ``_SPOT_HUGE`` gate). A cross-check failure / budget
  overrun leaves the field ``valid:null`` with the RMS PRESERVED — a validity-check
  failure NEVER turns a working ``get_spot`` into an error (``ok:true`` always; no
  new error family).

— SAMPLING. ``get_spot`` no longer runs ``StandardSpot`` at the
settings it came with (Hexapolar, density 6: probe rounds 1+2 read its RMS 26-74 % above a
converged independent trace). It writes FOUR settings through the CONCRETE
``__implementation__`` view (the base ``IAS_`` view silently drops a write)
and reads them back on a FRESH view: Pattern=Square, RayDensity=20, ReferTo=ChiefRay,
IgnoreLateralColor=True (the last is written only when its .NET property is writable, and
is ALWAYS verified). Any mismatch or raise fails closed (``analysis_settings_unverified``,
the existing family) BEFORE the analysis is applied; the call never runs at unverified
settings. The one exception is a lost engine connection, which propagates as session-closed.
MEASURED scope only: the RMS comes from a fixed dense Square (density 20) pupil
sample, chief-ray referenced, ignoring lateral color; on the live-gated lenses it agrees
with the RSCH operand within 2 %. GEO uses the same sampling and was NOT measured against
an oracle. ``per_wave`` reads RSCE (not the StandardSpot sampling; see the f2 note above) but
still configures and verifies the sampling first, so it also fails closed. Wavelength-weight and lateral-color
equivalence with RSCH beyond the gated lenses is not claimed.

Completion canary (UNCHANGED): ``SpotData is not None`` AND ``NumberOfFields > 0``
AND ``NumberOfWavelengths > 0``; else ``analysis_empty``.
"""
import math

from .._io import safe_float
from ..errors import SessionClosedError, ToolParamError, map_dotnet_exception
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _collimation as _col
from . import _config_common as _cfg
from . import _measurement_common as _mc
from . import _spot_validity as _sv
from . import analysis_mtf as _amtf

_KINDS = ("rms", "geo", "both")

# The RSCE per-wave named-slot map (§2, probe-confirmed live: Ring@2, Wave@3,
# Hx@4, Hy@5 — IDENTICAL to the RWCE slot layout). The named-slot path means Wave
# lands at slot 3 (NOT slot 2 — a positional guess would mis-route).
# Ring 5 (was 3). Probe (3 lenses, 34 cells): ring 5 is the lowest ring
# within 2 % of an independent converged oracle at EVERY cell (worst 0.077 %); ring 3 was up to
# 7.23 % off (lens A) and 2.04 % (Cooke). Ring 5 costs ~1.1x ring 3. (valid_density >= 1.)
_RSCE_RING_DEFAULT = 5


def _rsce_slots(ring, wave, hy):
    """Build the RSCE ``{position: (Header, value)}`` slot dict (Ring@2/Wave@3/Hy@5)."""
    return {
        2: ("Ring", int(ring)),
        3: ("Wave", int(wave)),
        4: ("Hx", 0.0),
        5: ("Hy", float(hy)),
    }


def _new_standard_spot(system):
    """Open a StandardSpot analysis via ``New_Analysis(AnalysisIDM.StandardSpot)``."""
    idm = _amtf._analysis_idm(system, "StandardSpot")
    return system.Analyses.New_Analysis(idm)


# The sampling get_spot REQUIRES. ReferTo and IgnoreLateralColor equal the probed
# untouched values; they are written and verified anyway so a session that started elsewhere
# cannot silently hand back a different reference.
_SAMPLING_FAMILY = _ac.SETTINGS_UNVERIFIED_FAMILY  # ONE constant, shared with analysis_mtf_field
_SAMPLING_PATTERN = "Square"
_SAMPLING_RAY_DENSITY = 20
_SAMPLING_REFER_TO = "ChiefRay"


class _SamplingNotApplied(Exception):
    """The StandardSpot sampling could not be written AND read back (fail closed)."""


def _safe_text(exc):
    """``"Type: message"`` for an exception, never raising (a hostile ``__str__``)."""
    try:
        return f"{type(exc).__name__}: {exc}"
    except Exception:  # noqa: BLE001 — diagnostics must not mask the refusal
        return type(exc).__name__


def _checked_member(current, member):
    """``member`` was read as a LITERAL attribute off ``type(current)`` (the property's own enum
    type, never an int or ``Enum.Parse``); confirm it is an instance of that type. (Literal
    attribute access, not ``getattr(cls, name)``: a new dynamic-dispatch site would need a
    ``test_tools_slot_census`` C5 adjudication, and the lookup is identical.)"""
    if not isinstance(member, type(current)):
        raise _SamplingNotApplied(f"{member!r} is not a member of {type(current).__name__}")
    return member


def _concrete_settings(analysis):
    """The settings' CONCRETE view. NO fallback to the base ``IAS_`` view: a
    missing ``__implementation__`` raises, which fails closed."""
    return analysis.GetSettings().__implementation__


def _configure_spot_sampling(analysis):
    """Write the required sampling on ``analysis`` and return the READ-BACK values.

    Writes Pattern, RayDensity, ReferTo (and IgnoreLateralColor only when its .NET property
    reports ``CanWrite``) through the concrete view, then reads all four on a FRESH view and
    compares. Returns ``{"pattern", "ray_density", "refer_to", "ignore_lateral_color",
    "ignore_lateral_color_settable"}`` as read back. Raises ``_SamplingNotApplied`` on any
    mismatch or engine raise; a transport loss (``SessionClosedError`` after mapping) is
    re-raised unchanged for the dispatcher.
    """
    try:
        impl = _concrete_settings(analysis)
        pattern = _checked_member(impl.Pattern, type(impl.Pattern).Square)
        refer_to = _checked_member(impl.ReferTo, type(impl.ReferTo).ChiefRay)
        ilc_property = impl.GetType().GetProperty("IgnoreLateralColor")
        if ilc_property is None:
            raise _SamplingNotApplied("the IgnoreLateralColor property is not present")
        settable = bool(ilc_property.CanWrite)
        impl.Pattern = pattern
        impl.RayDensity = _SAMPLING_RAY_DENSITY
        impl.ReferTo = refer_to
        if settable:
            impl.IgnoreLateralColor = True
        fresh = _concrete_settings(analysis)  # a FRESH view, never the one written through
        got = (str(fresh.Pattern), fresh.RayDensity, str(fresh.ReferTo),
               fresh.IgnoreLateralColor)
    except _SamplingNotApplied:
        raise
    except Exception as exc:  # noqa: BLE001 — any engine raise fails closed
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        raise _SamplingNotApplied(_safe_text(exc)) from None
    pat, density, ref, ilc = got
    bad = []
    if pat != _SAMPLING_PATTERN:
        bad.append(f"Pattern read back {pat!r}, wanted {_SAMPLING_PATTERN!r}")
    if (isinstance(density, bool) or not isinstance(density, int)
            or density != _SAMPLING_RAY_DENSITY):
        bad.append(f"RayDensity read back {density!r}, wanted {_SAMPLING_RAY_DENSITY}")
    if ref != _SAMPLING_REFER_TO:
        bad.append(f"ReferTo read back {ref!r}, wanted {_SAMPLING_REFER_TO!r}")
    if ilc is not True:
        bad.append(f"IgnoreLateralColor read back {ilc!r}, wanted True"
                   + ("" if settable else " (the property is read-only)"))
    if bad:
        raise _SamplingNotApplied("; ".join(bad))
    return {
        "pattern": pat,
        "ray_density": density,
        "refer_to": ref,
        "ignore_lateral_color": ilc,
        "ignore_lateral_color_settable": settable,
    }


def _close_quietly(analysis):
    """Close an analysis window opened before ``_run_analysis`` took ownership of it."""
    try:
        analysis.Close()
    except Exception:  # noqa: BLE001 — window teardown must never mask the outcome
        pass


def _int_or_none(params, key):
    """Pull an optional 1-based int param (default None = all); reject bool."""
    if key not in params or params[key] is None:
        return None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and not math.isfinite(value):
            # f2 A2-4: nan/inf must be a tool_param refusal, not a bare ValueError/OverflowError.
            raise ToolParamError(f"{key!r} must be a finite 1-based integer, got a non-finite float")
        if isinstance(value, float) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"{key!r} must be a 1-based integer, got {type(value).__name__} {value!r}"
        )
    return int(value)


def _coerce_per_wave(params):
    """Pull the optional ``per_wave`` boolean (default False); reject a non-bool."""
    if "per_wave" not in params or params["per_wave"] is None:
        return False
    value = params["per_wave"]
    if not isinstance(value, bool):
        raise ToolParamError(
            f"per_wave must be a boolean, got {type(value).__name__} {value!r}"
        )
    return value


def get_spot(session, params):
    """Run StandardSpot, grade trace validity, and return per-field (or per-wave) blur.

    Default: per-FIELD POLYCHROMATIC RMS/GEO (the ``wave`` dimension is collapsed —
    ``GetRMSSpotSizeFor`` ignores it). ``per_wave:true`` returns TRUE per-wave RMS
    (RSCE operand). EVERY field is graded by a full-pupil cross-check; an invalid
    field reads ``rms:null`` + ``reason`` + ``traced_fraction`` (raw kept in
    ``rms_raw``).

    ``field`` / ``wave`` (1-based, default all) restrict; ``kind``
    (``"rms"``/``"geo"``/``"both"``, default ``"both"``). ``config`` (None|int)
    selects a SINGLE config (``config="all"`` REFUSED — spot is the heavy class),
    read inside a ``with_configuration`` wrap that ALWAYS restores.

    SAMPLING: the StandardSpot is configured to a fixed dense Square (density 20)
    pupil sample, chief-ray referenced, ignoring lateral color, and the four settings are
    read back before the analysis runs (a mismatch fails closed, ``analysis_settings_unverified``;
    a lost engine connection propagates as session-closed instead).
    On the live-gated lenses the RMS agrees with the RSCH operand within 2 %; the collapsed
    path echoes the read-back values as ``rms_sampling``. GEO uses the same sampling and is NOT
    oracle-validated. ``per_wave`` reads RSCE (at ring 5, converted from lens units to um)
    but is gated by the same check.
    """
    cfg_idx = _amtf._resolve_single_config(session.system, params, "get_spot")
    if cfg_idx is None:
        result = _get_spot_at(session, params)
        if isinstance(result, dict):
            result.setdefault(
                "config_evaluated", _cfg.safe_current_configuration(session.system)
            )
        return result
    with _cfg.with_configuration(session.system, cfg_idx) as ctx:
        result = _get_spot_at(session, params)
    if isinstance(result, dict):
        result.setdefault("config_evaluated", cfg_idx)
        if not ctx["restore_verified"]:
            result["mutation_warning"] = ctx["mutation_warning"]
        if not ctx["switched"] and ctx["mutation_warning"]:
            result.setdefault("config_switch_warning", ctx["mutation_warning"])
    return result


def _get_spot_at(session, params):
    """The pure per-config StandardSpot body (read at the ACTIVE config). See ``get_spot``."""
    system = session.system

    kind = params.get("kind", "both")
    if kind not in _KINDS:
        raise ToolParamError(f"kind must be one of {_KINDS}, got {kind!r}")
    field_sel = _int_or_none(params, "field")
    wave_sel = _int_or_none(params, "wave")
    per_wave = _coerce_per_wave(params)
    wave_arg_polychromatic = (wave_sel is not None) and not per_wave

    analysis = _new_standard_spot(system)
    # Configure + verify the sampling BEFORE the analysis runs, on BOTH paths
    # (the StandardSpot run is the field/wave-count canary for per_wave too). The window is
    # opened before ``_run_analysis`` owns it, so it is closed here on every early exit.
    try:
        sampling = _configure_spot_sampling(analysis)
    except _SamplingNotApplied as exc:
        _close_quietly(analysis)
        return _ac.error_envelope(
            "get_spot", _SAMPLING_FAMILY,
            f"StandardSpot sampling was not applied ({exc}); get_spot refuses to run at "
            "unverified settings",
        )
    except BaseException:
        _close_quietly(analysis)
        raise
    with _ac._run_analysis(analysis) as results:
        spot_data = results.SpotData
        # Completion canary (UNCHANGED): a null SpotData / empty matrix -> analysis_empty.
        if spot_data is None:
            return _ac.error_envelope(
                "get_spot", "analysis_empty", "StandardSpot produced no SpotData"
            )
        n_fields = int(spot_data.NumberOfFields)
        n_waves = int(spot_data.NumberOfWavelengths)
        if n_fields <= 0 or n_waves <= 0:
            return _ac.error_envelope(
                "get_spot", "analysis_empty",
                f"StandardSpot reported {n_fields} fields x {n_waves} wavelengths",
            )

        # Validate the index range BEFORE any read. field applies
        # on every path; wave only on per_wave (on the collapsed path it is inert).
        if field_sel is not None and not 1 <= field_sel <= n_fields:
            raise ToolParamError(
                f"field {field_sel} out of range; valid 1..{n_fields} "
                f"(NumberOfFields={n_fields})"
            )
        if per_wave and wave_sel is not None and not 1 <= wave_sel <= n_waves:
            raise ToolParamError(
                f"wave {wave_sel} out of range; valid 1..{n_waves} "
                f"(NumberOfWavelengths={n_waves})"
            )

        fields = [field_sel] if field_sel is not None else list(range(1, n_fields + 1))

        # Read the polychromatic per-field RMS/GEO (wave=1 — the value is the same
        # for every wave, so wave=1 is the canonical poly read for the M2 magnitude
        # gate + the default headline). Keyed by field.
        poly_rms = {}
        poly_geo = {}
        for f in fields:
            if kind in ("rms", "both"):
                poly_rms[f] = safe_float(spot_data.GetRMSSpotSizeFor(f, 1))
            if kind in ("geo", "both"):
                poly_geo[f] = safe_float(spot_data.GetGeoSpotSizeFor(f, 1))

    # Resolve the lens unit ONCE; the label and the per_wave
    # um factor come from this one token. A per_wave call that serves an RMS on a lens unit
    # it cannot convert is a WHOLE-CALL refusal, before any validity trace or RSCE read.
    units, um_factor = _ac._resolve_spot_units(system)
    if per_wave and kind in ("rms", "both") and um_factor is None:
        shown = str.__str__(units) if isinstance(units, str) else "unknown"
        return _ac.error_envelope(
            "get_spot", _ac.ANALYSIS_UNITS_UNCONVERTIBLE_FAMILY,
            "per_wave RSCE values are in lens units and the lens unit "
            f"{shown!r} has no proven conversion to um; get_spot refuses to serve "
            "them under a um label. The default (collapsed) call is unaffected.",
        )

    # ----- the ALWAYS validity cross-check (§1; runs OUTSIDE the spot analysis) ----
    validity = _run_validity(system, fields, n_fields)

    if per_wave:
        spots = _build_per_wave_spots(
            system, fields, n_waves, wave_sel, kind, validity, um_factor
        )
        rms_reference = "per_wave"
    else:
        spots = _build_collapsed_spots(fields, kind, poly_rms, poly_geo, validity)
        rms_reference = "polychromatic"

    out = {
        "ok": True,
        "units": units,
        "number_of_fields": n_fields,
        "number_of_wavelengths": n_waves,
        "rms_reference": rms_reference,
        "validity_checked": validity["checked"],
        "validity_wave": _sv._VALIDITY_WAVE,
        "validity_grid": _sv._SPOT_GRID_N,
        "spots": spots,
    }
    if per_wave:
        out["rms_source"] = "RSCE"
        if kind in ("geo", "both"):
            out["geo_reference"] = "unavailable_per_wave"
    else:
        out["rms_sampling"] = sampling
    if validity.get("warning"):
        out["validity_warning"] = validity["warning"]
    if wave_arg_polychromatic:
        out["wave_arg_polychromatic"] = True
    # A collimated (afocal) output: flag it and redirect to the angular tools. Adds keys
    # ONLY when the shared detector fires, so a normal imager's key set is unchanged.
    collimated_flags = _col.collimated_flags(system)
    if collimated_flags:
        out["collimated_output"] = True
        out["flags"] = collimated_flags
    return out


def _run_validity(system, fields, n_fields):
    """Run the one-batch cross-check; return a per-field verdict map keyed by field.

    Returns ``{"checked": bool, "warning": str|None, "by_field": {f: {"valid",
    "traced", "traced_fraction", "n_traced", "n_grid", "reason"}}}``. A field whose
    cross-check is unavailable gets ``valid:null`` + ``reason:"validity_indeterminate"``;
    a budget overrun gets ``reason:"validity_budget_exhausted"``. NEVER raises.
    """
    by_field = {}
    deadline = _sv.perf_counter() + _sv.validity_budget_s()
    field_hys = _sv.read_field_hys(system)
    if field_hys is None or len(field_hys) < n_fields:
        # Could not resolve fields for the cross-check -> every field indeterminate
        # (the RMS is preserved upstream — could-not-disprove). field_hys is None so
        # the per-wave path (which threads this SAME geometry, L30) fails closed.
        for f in fields:
            by_field[f] = _indeterminate_verdict()
        return {"checked": False, "warning": _INDETERMINATE_WARNING,
                "by_field": by_field, "field_hys": field_hys}

    ev = _sv.evaluate_spot_validity(system, field_hys, deadline=deadline)
    if ev.get("budget_exhausted"):
        for f in fields:
            by_field[f] = _budget_verdict()
        return {"checked": False, "warning": None, "by_field": by_field,
                "field_hys": field_hys}
    if not ev.get("available"):
        for f in fields:
            by_field[f] = _indeterminate_verdict()
        return {"checked": False, "warning": _INDETERMINATE_WARNING,
                "by_field": by_field, "field_hys": field_hys}

    any_indeterminate = False
    for f in fields:
        tally = ev["fields"][f - 1] if 1 <= f <= len(ev["fields"]) else None
        if tally is None:
            by_field[f] = _indeterminate_verdict()
            any_indeterminate = True
        else:
            by_field[f] = {
                "n_traced": tally["n_traced"],
                "n_grid": tally["n_grid"],
                "traced_fraction": tally["traced_fraction"],
                # valid/traced/reason are filled per entry (they depend on the RMS
                # magnitude for the M2 gate) — see _apply_verdict.
            }
    return {
        "checked": True,
        "warning": _INDETERMINATE_WARNING if any_indeterminate else None,
        "by_field": by_field,
        # Thread the SINGLE field_hys the verdict was computed from (L30, one-reader):
        # the per-wave RSCE read MUST use the SAME field geometry the verdict graded,
        # never an independent re-read that could transiently diverge to hy=0.0.
        "field_hys": field_hys,
    }


_INDETERMINATE_WARNING = (
    "the trace-validity cross-check was unavailable for one or more fields; their "
    "rms/geo are reported UNVERIFIED (read each entry's valid/reason)"
)


def _indeterminate_verdict():
    return {"reason": "validity_indeterminate", "n_traced": None, "n_grid": None,
            "traced_fraction": None}


def _budget_verdict():
    return {"reason": "validity_budget_exhausted", "n_traced": None, "n_grid": None,
            "traced_fraction": None}


def _apply_verdict(entry, verdict, rms_keys, raw_value_by_key):
    """Stamp the validity verdict onto a spot ``entry`` (§1.4 ladder).

    ``rms_keys`` is the subset of ``("rms", "geo")`` present on the entry;
    ``raw_value_by_key`` maps each to its pre-verdict reading: the RAW engine reading on the
    collapsed path, but on the per_wave path the RSCE reading CONVERTED to the envelope
    ``units`` (RSCE itself returns lens units). On an INVALID
    field the headline ``rms``/``geo`` are NULLED and that reading kept in ``rms_raw``/``geo_raw``;
    on a VALID field the raw value stays the headline (no raw sub-field). On an
    INDETERMINATE/BUDGET field the value is PRESERVED (could-not-disprove).
    """
    reason = verdict.get("reason")
    n_traced = verdict.get("n_traced")
    n_grid = verdict.get("n_grid")
    frac = verdict.get("traced_fraction")

    if reason in ("validity_indeterminate", "validity_budget_exhausted"):
        # Value PRESERVED, valid/traced null.
        for key in rms_keys:
            entry[key] = raw_value_by_key[key]
        entry["valid"] = None
        entry["traced"] = None
        entry["reason"] = reason
        return

    # A graded field: run the ladder. M2 (beyond_capture) needs the RMS magnitude;
    # use the RMS reading (or GEO if RMS absent) as the magnitude witness.
    magnitude = raw_value_by_key.get("rms")
    if magnitude is None:
        magnitude = raw_value_by_key.get("geo")
    valid, traced, ladder_reason = _sv._verdict_for_field(n_traced, n_grid, magnitude)

    entry["valid"] = valid
    entry["traced"] = traced
    entry["traced_fraction"] = frac
    entry["n_traced"] = n_traced
    entry["n_grid"] = n_grid
    entry["reason"] = ladder_reason
    if valid:
        for key in rms_keys:
            entry[key] = raw_value_by_key[key]
    else:
        # Invalid: NULL the headline, keep the raw.
        for key in rms_keys:
            entry[key] = None
            entry[f"{key}_raw"] = raw_value_by_key[key]


def _build_collapsed_spots(fields, kind, poly_rms, poly_geo, validity):
    """One entry per FIELD (the #14 collapse), each graded by the field cross-check."""
    rms_keys = []
    if kind in ("rms", "both"):
        rms_keys.append("rms")
    if kind in ("geo", "both"):
        rms_keys.append("geo")

    spots = []
    for f in fields:
        entry = {"field": int(f)}
        raw_by_key = {}
        if "rms" in rms_keys:
            raw_by_key["rms"] = poly_rms[f]
        if "geo" in rms_keys:
            raw_by_key["geo"] = poly_geo[f]
        _apply_verdict(entry, validity["by_field"][f], rms_keys, raw_by_key)
        spots.append(entry)
    return spots


_RSCE_VALUE = "value"
_RSCE_UNREADABLE = "unreadable"
_RSCE_SENTINELS = ("nan", "inf", "-inf")


def _rsce_classify(raw, um_factor):
    """The ONE exhaustive classifier of an RSCE reading.

    Returns ``(kind, value)``:
      - an EXACT finite ``int``/``float`` -> ``("value", safe_float(raw * um_factor))`` (the
        product goes back through ``safe_float`` so an overflow is the sentinel, never a bare
        ``inf``; an exact int too large for a float multiply maps to the sign-matched
        ``"inf"``/``"-inf"`` sentinel, which reads beyond_capture);
      - an exact non-finite ``float`` -> ``("value", safe_float(raw))`` (the string sentinel);
      - an exact ``str`` that is one of the ``safe_float`` sentinels -> ``("value", raw)``;
      - ANYTHING ELSE (bool, an int/float subclass incl. numpy.float64, any other str, None,
        any other object) -> ``("unreadable", None)``: it is never served and never reaches
        the verdict ladder or the value-PRESERVED (indeterminate/budget) branch.
    ``type(x) is`` (not ``isinstance``) throughout: a bool and a subclass are not numbers here.
    """
    t = type(raw)
    if t is int or (t is float and math.isfinite(raw)):
        try:
            return _RSCE_VALUE, safe_float(raw * um_factor)
        except OverflowError:
            return _RSCE_VALUE, ("inf" if raw > 0 else "-inf")
    if t is float:
        return _RSCE_VALUE, safe_float(raw)
    if t is str and raw in _RSCE_SENTINELS:
        return _RSCE_VALUE, raw
    return _RSCE_UNREADABLE, None


def _build_per_wave_spots(system, fields, n_waves, wave_sel, kind, validity, um_factor):
    """Per-(field, wave) entries via RSCE (TRUE per-wave). RSCE is RMS-only (no geo).

    ``um_factor`` is the lens-unit -> um factor resolved ONCE by the caller from the same
    token as the envelope ``units`` label (every served RMS passes through ``_rsce_classify``
    before the verdict, so the beyond-capture magnitude and ``rms_raw`` are in um).

    Every wave row of a field INHERITS the field-level verdict (the cross-check is a
    per-FIELD geometric verdict, wave-independent to first order — §2.3). A per-wave
    RSCE value is nulled under an invalid field (a chromatic blur is never reported
    from a blocked/clipped pupil). RSCE has no GEO -> per-wave entries OMIT ``geo``.
    """
    waves = [wave_sel] if wave_sel is not None else list(range(1, n_waves + 1))
    # Per-wave is RMS only (RSCE has no GEO operand here). The ``kind`` "geo"-only
    # request still grades validity but carries no value key; ``rms`` is the only
    # numeric. The envelope's geo_reference discloses the per-wave GEO gap.
    rms_keys = ["rms"] if kind in ("rms", "both") else []

    # ONE-READER (L30, fix): consume the SINGLE ``field_hys`` the verdict was
    # computed from in ``_run_validity`` — NEVER re-read the field geometry here. An
    # independent re-read could transiently FAIL and silently fall back to hy=0.0, so
    # an off-axis field would read the ON-AXIS RSCE blur and be stamped valid:true
    # with no warning (the fail-OPEN this fix closes). If the threaded geometry is
    # unavailable / too short for a field, that field FAILS CLOSED (its per-wave
    # entries read valid:false + reason, never the on-axis fallback).
    field_hys = validity.get("field_hys")

    spots = []
    for f in fields:
        verdict = validity["by_field"][f]
        hy = None
        if field_hys is not None and 1 <= f <= len(field_hys):
            hy = field_hys[f - 1]
        for w in waves:
            entry = {"field": int(f), "wave": int(w)}
            if hy is None:
                # FAIL-CLOSED: the verdict's field geometry is unavailable for this
                # field, so the per-wave RSCE read cannot use the SAME Hy the verdict
                # graded. Disclose + refuse rather than silently read hy=0.0 (the
                # on-axis blur), which would be the fail-OPEN silent-wrong.
                entry["valid"] = False
                entry["traced"] = None
                entry["reason"] = "per_wave_field_geometry_unavailable"
                if rms_keys:
                    entry["rms"] = None
                spots.append(entry)
                continue
            raw_by_key = {}
            if rms_keys:
                raw = _read_rsce(system, _RSCE_RING_DEFAULT, w, hy)
                kind_, value = _rsce_classify(raw, um_factor)
                if kind_ == _RSCE_UNREADABLE:
                    # An unreadable reading (None, bool, subclass, other
                    # str, ...) is never certified valid or preserved as a headline. A field
                    # the cross-check ALREADY graded invalid (blocked / partial_vignette)
                    # keeps that verdict (reason + tallies, rms null, same key set as a
                    # readable one); only a valid or indeterminate/budget field is
                    # reported per_wave_rsce_unreadable.
                    graded_invalid = (
                        verdict.get("reason") not in (
                            "validity_indeterminate", "validity_budget_exhausted")
                        and not _sv._verdict_for_field(
                            verdict.get("n_traced"), verdict.get("n_grid"), None)[0]
                    )
                    if graded_invalid:
                        _apply_verdict(entry, verdict, rms_keys, {"rms": None})
                    else:
                        entry["valid"] = False
                        entry["traced"] = None
                        entry["reason"] = "per_wave_rsce_unreadable"
                        entry["rms"] = None
                    spots.append(entry)
                    continue
                raw_by_key["rms"] = value
            _apply_verdict(entry, verdict, rms_keys, raw_by_key)
            spots.append(entry)
    return spots


def _read_rsce(system, ring, wave, hy):
    """Read one RSCE per-wave RMS via the stateless 9-arg named-slot path.

    Returns ``None`` on a read failure; a transport loss (a mapped ``SessionClosedError``) and
    a ``KeyboardInterrupt``/``SystemExit`` are NOT swallowed (f2 A2-3).

    Uses the SAME ``read_operand_slots`` firewall the measurement tools use (the
    value lands in its OWN slot — Wave@3, not @2). A ``valid_density`` guard on the
    ring keeps the silent-0 trap out (Ring < 1 -> the ~0 garbage reading). A read
    failure / non-number -> ``safe_float`` of the raw (the validity verdict already
    governs whether the value is the headline).
    """
    if not _mc.valid_density(ring):
        # Should never happen (the default is 5) — a defensive guard, fail to a None.
        return None
    try:
        raw, _suspicious = _mc.read_operand_slots(
            system, "RSCE", _rsce_slots(ring, wave, hy)
        )
        return safe_float(raw)
    except Exception as exc:  # noqa: BLE001 — an RSCE read failure -> None (graded data); a transport loss is NEVER swallowed
        if isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise
        return None


GET_SPOT_SPEC = ToolSpec(
    name="get_spot",
    handler=get_spot,
    required_params=(),
    param_types={"field": "number", "wave": "number", "kind": "string",
                 "config": "number", "per_wave": "boolean"},
    description=(
        "Measure spot-diagram blur. Returns per-FIELD RMS and GEO spot radii "
        "(1-based), POLYCHROMATIC by default (all-wave pooled RMS — the wave arg does NOT "
        "select a wavelength on a StandardSpot). Each field is graded for trace "
        "validity via a full-pupil cross-check: a totally-blocked, partially-"
        "vignetted, or beyond-capture field reads valid:false/rms:null + a reason + "
        "traced_fraction, NEVER a misleading rms:0.0/huge value (the pre-verdict reading, in "
        "the envelope units, is kept in rms_raw). The RMS is computed from a fixed dense Square (density 20) "
        "pupil sample, chief-ray referenced, ignoring lateral color (echoed as "
        "rms_sampling); on the live-gated lenses it agrees with the RSCH operand within "
        "2%. GEO uses the same sampling and is not oracle-validated. If those settings "
        "cannot be verified the call refuses (analysis_settings_unverified), except a lost engine connection, which propagates as session-closed. "
        "Set per_wave:true for a TRUE per-wavelength RMS (RSCE "
        "operand read at ring 5 and converted from lens units to um; RMS-only, no geo; "
        "unaffected by the sampling; a lens unit it cannot convert is refused as "
        "analysis_units_unconvertible). A field index (or, with per_wave, a wave index) "
        "outside the system's range is refused. Units: um for a mm lens. For chromatic detail "
        "also see analyze_axial_color / analyze_wavefront. On a collimated (afocal) output the "
        "result carries collimated_output + a flag. "
        "OBSCURED PUPILS: on a system with a central obscuration (a Cassegrain "
        "secondary shadow) author a CircularObscuration on the obstructing surface "
        "with set_surface_aperture FIRST, so this number is computed on the TRUE "
        "obscured pupil, not the full circle (a central CircularObscuration "
        "min_radius=0, max_radius=R plus an outer CircularAperture forms the "
        "annulus)."
    ),
)

TOOL_SPECS = (GET_SPOT_SPEC,)
