"""tools/analysis_spot.py — get_spot: standard-spot RMS/GEO with trace validity.

``get_spot`` runs the StandardSpot analysis
(``Analyses.New_Analysis(AnalysisIDM.StandardSpot)`` -> wait -> ``GetResults()``
-> ``.SpotData``) and reads the per-FIELD RMS/GEO spot radii via the 1-based
``GetRMSSpotSizeFor(field, wave)`` / ``GetGeoSpotSizeFor(field, wave)``. Every
scalar passes through ``safe_float``.

S2 (#7 / #14) — the validity + per-wave rewire:

- **#14 COLLAPSE (probe-falsified):** ``GetRMSSpotSizeFor(field, wave)`` IGNORES
  the ``wave`` arg — it returns the SAME polychromatic (all-wave RSS) value for
  every wave. The old per-(field,wave) matrix was FABRICATED duplication. The
  default output now COLLAPSES the wave dimension -> ONE polychromatic RMS/GEO per
  FIELD (``rms_reference:"polychromatic"``).

- **#14 TRUE per-wave via RSCE (micro-probe CONFIRMED):** ``per_wave:true`` reads
  the ``RSCE(Ring, Wave, Hx, Hy)`` merit operand (the live micro-probe proved RSCE
  HONORS its ``Wave`` slot: 3 distinct on-axis values across waves, unlike the
  inert ``GetRMSSpotSizeFor``) via the stateless named-slot 9-arg
  ``read_operand_slots`` path. RSCE is RMS-only -> per-wave entries OMIT ``geo``
  (``geo_reference:"unavailable_per_wave"``).

- **#7 ALWAYS validity cross-check (the silent-wrong killer):** the spot result has
  NO native validity member (probe headline #1). EVERY call runs ONE full-pupil
  batch ray-trace cross-check (``_spot_validity``): a field with a blocked (M1),
  partially-vignetted (M3), or beyond-capture (M2) pupil reads ``valid:false`` +
  ``rms:null`` + a ``reason`` + ``traced_fraction``, with the RAW reading KEPT in
  ``rms_raw``/``geo_raw`` — NEVER a misleading ``rms:0.0``/huge headline. A genuine
  perfect spot (``~2.83e-13``, all rays traced) reads ``valid:true`` (no
  over-refusal — ``valid`` is never derived from a small RMS, only from
  ``traced_fraction`` + the ``_SPOT_HUGE`` gate). A cross-check failure / budget
  overrun leaves the field ``valid:null`` with the RMS PRESERVED — a validity-check
  failure NEVER turns a working ``get_spot`` into an error (``ok:true`` always; no
  new error family).

Completion canary (UNCHANGED): ``SpotData is not None`` AND ``NumberOfFields > 0``
AND ``NumberOfWavelengths > 0``; else ``analysis_empty``.

Live ZOS-API integration: ``test_spot_validity_live.py`` (the make-it-bite per
mode); unit-tested against the fixture-seeded fake SpotData + an independent
``grid_failure_mode`` batch double.
"""
import math

from .._io import safe_float
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _config_common as _cfg
from . import _measurement_common as _mc
from . import _spot_validity as _sv
from . import analysis_mtf as _amtf

_KINDS = ("rms", "geo", "both")

# The RSCE per-wave named-slot map (§2, probe-confirmed live: Ring@2, Wave@3,
# Hx@4, Hy@5 — IDENTICAL to the RWCE slot layout). The named-slot path means Wave
# lands at slot 3 (NOT slot 2 — a positional guess would mis-route).
_RSCE_RING_DEFAULT = 3  # the locked default ring density (valid_density >= 1).


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


def _int_or_none(params, key):
    """Pull an optional 1-based int param (default None = all); reject bool."""
    if key not in params or params[key] is None:
        return None
    value = params[key]
    if isinstance(value, bool) or not isinstance(value, int):
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

    # ----- the ALWAYS validity cross-check (§1; runs OUTSIDE the spot analysis) ----
    validity = _run_validity(system, fields, n_fields)

    if per_wave:
        spots = _build_per_wave_spots(
            system, fields, n_waves, wave_sel, kind, validity
        )
        rms_reference = "per_wave"
    else:
        spots = _build_collapsed_spots(fields, kind, poly_rms, poly_geo, validity)
        rms_reference = "polychromatic"

    out = {
        "ok": True,
        "units": _ac._spot_units_string(system),
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
    if validity.get("warning"):
        out["validity_warning"] = validity["warning"]
    if wave_arg_polychromatic:
        out["wave_arg_polychromatic"] = True
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
    ``raw_value_by_key`` maps each to its RAW engine reading. On an INVALID field
    the headline ``rms``/``geo`` are NULLED and the raw kept in ``rms_raw``/``geo_raw``;
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


def _build_per_wave_spots(system, fields, n_waves, wave_sel, kind, validity):
    """Per-(field, wave) entries via RSCE (TRUE per-wave). RSCE is RMS-only (no geo).

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
                raw_by_key["rms"] = _read_rsce(system, _RSCE_RING_DEFAULT, w, hy)
            _apply_verdict(entry, verdict, rms_keys, raw_by_key)
            spots.append(entry)
    return spots


def _read_rsce(system, ring, wave, hy):
    """Read one RSCE per-wave RMS via the stateless 9-arg named-slot path. Never raises.

    Uses the SAME ``read_operand_slots`` firewall the measurement tools use (the
    value lands in its OWN slot — Wave@3, not @2). A ``valid_density`` guard on the
    ring keeps the silent-0 trap out (Ring < 1 -> the ~0 garbage reading). A read
    failure / non-number -> ``safe_float`` of the raw (the validity verdict already
    governs whether the value is the headline).
    """
    if not _mc.valid_density(ring):
        # Should never happen (the default is 3) — a defensive guard, fail to a None.
        return None
    try:
        raw, _suspicious = _mc.read_operand_slots(
            system, "RSCE", _rsce_slots(ring, wave, hy)
        )
        return safe_float(raw)
    except BaseException:  # noqa: BLE001 — an RSCE read failure -> None (graded data)
        return None


GET_SPOT_SPEC = ToolSpec(
    name="get_spot",
    handler=get_spot,
    required_params=(),
    param_types={"field": "number", "wave": "number", "kind": "string",
                 "config": "number", "per_wave": "boolean"},
    description=(
        "Measure spot-diagram blur. Returns per-FIELD RMS and GEO spot radii "
        "(1-based), POLYCHROMATIC by default (all-wave RSS — the wave arg does NOT "
        "select a wavelength on a StandardSpot). Each field is graded for trace "
        "validity via a full-pupil cross-check: a totally-blocked, partially-"
        "vignetted, or beyond-capture field reads valid:false/rms:null + a reason + "
        "traced_fraction, NEVER a misleading rms:0.0/huge value (the raw reading is "
        "kept in rms_raw). Set per_wave:true for a TRUE per-wavelength RMS (RSCE "
        "operand; RMS-only, no geo). Units: um for a mm lens. For chromatic detail "
        "also see analyze_axial_color / analyze_wavefront."
    ),
)

TOOL_SPECS = (GET_SPOT_SPEC,)
