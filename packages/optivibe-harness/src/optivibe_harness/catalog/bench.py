"""catalog/bench.py — the control layer.

The folder loop + the per-design decision tree (load -> classify -> normalize ->
profile) + the 3-tier ``row_status`` predicate + the template/basis resolution + the
CSV/manifest writers. Imports ``metrics`` (the shared firewall) + ``registry`` (the
metric adapters). Adds NO MCP tool — the skill drives ``bench_folder``
in-process.

Normalization order (D6, fixed): ``load_design -> scale_lens(to_efl) -> set_aperture
(only if f/# pinned) -> set_field -> set_wavelength -> profile``.

``row_status`` lives here as the ONE locus; every dispatch site consumes
``metrics.reading_ok`` — there is NO second, competing ``_both_ok`` (D11).
"""
import csv
import glob
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from typing import Optional

from ..tools._layout_geometry import normalize_material
from . import metrics as M
from . import registry
# The RANKER'S OWN cell disposition, imported rather than re-declared so the two can
# never drift apart: the quarantine's job is to leave nothing the ranker reads as a
# number, so the predicate that decides "a number" must be the ranker's, not a second
# local copy that can drift.
# ``plot`` is import-safe here — it imports no catalog module and defers matplotlib.
from .plot import classify_cell as _classify_cell


# --------------------------------------------------------------------------- #
# Constants.
# --------------------------------------------------------------------------- #
_DEFAULT_METRICS = ("first_order", "rms_spot", "mtf", "strehl", "clearance")
_DEFAULT_PLOTS = ("bar_kpi", "montage")
_DEFAULT_MTF_FREQ = 50.0          # [Assumption] — the skill confirms this with the user.
_DEFAULT_SAMP = 6                 # analyze_wavefront silent-0 guard (probe-established).
_DEFAULT_THETA = 5.0             # fallback half-field angle when no census/template FOV.
_SENTINEL_MAG = 1e10             # afocal EFL sentinel (mirrors _measurement_common).

# The scale-outcome tier is decided by a DIRECT INVARIANT, fail-closed-by-default:
# a scale is NON-fatal ONLY if it is a PROVEN SUCCESS or the ONE benign afocal token
# (``scale_efl_undefined`` — geometry intact, measured at NATIVE scale, an honest partial).
# EVERYTHING else is fatal (the basis is UNPROVABLE -> a wrongly-scaled or unproven
# measurement is worse than a missing one -> failed). This is an ALLOW-LIST of the non-fatal
# cases — a NEW/unknown/None/empty/missing refusal family DEFAULTS to fatal (there is NO
# denylist of fatal families to fall OPEN on: FIX closing the malformed-scale-envelope class).
_SCALE_EFL_UNDEFINED = "scale_efl_undefined"   # the ONE non-fatal refusal token.
_SCALE_TIER_OK = "ok"          # proven success (or no scale attempted) — the row may read ok.
_SCALE_TIER_PARTIAL = "partial"    # scale_efl_undefined — afocal, measured native.
_SCALE_TIER_FATAL = "failed"       # EVERYTHING else — the row reads failed.


def _scale_tier(scale_result):
    """The DIRECT scale-outcome invariant over the scale PAYLOAD — the dict
    ``_scale_payload`` normalizes EVERY scale env (inner refusal / dispatch fault / non-dict
    result) into. Returns one of ``_SCALE_TIER_{OK,PARTIAL,FATAL}``. The ONE locus of the
    scale-tier decision: ``row_status`` (the tier stamp) AND ``_bench_one`` (the
    norm-note) consume THIS verdict, so they can never drift.

    Fail-closed-by-default — NON-fatal is an ALLOW-LIST, EVERYTHING else is FATAL:
    - ``scale_result is None`` (no scale attempted / absent) -> OK.
    - a PROVEN SUCCESS (``ok is True`` AND (``scaled_ok is True`` OR ``already_at_target``))
      -> OK. A bare ``ok:True`` that did NOT prove it scaled is NOT trusted -> FATAL.
    - a ``scale_efl_undefined`` refusal (afocal, measured native) -> PARTIAL.
    - ANY other refusal (``ok`` not True) with ANY family — ``None`` / ``""`` / a missing
      key / an unknown or known refusal token — -> FATAL. By CONSTRUCTION a malformed/None
      family is failed, never silently non-fatal (no "is this in the fatal set?" question)."""
    if scale_result is None:
        return _SCALE_TIER_OK                       # no scale attempted -> non-fatal.
    if not isinstance(scale_result, dict):
        return _SCALE_TIER_FATAL                     # a non-dict payload is unprovable.
    if scale_result.get("ok") is True:
        if scale_result.get("scaled_ok") is True or scale_result.get("already_at_target"):
            return _SCALE_TIER_OK                    # proven scale / basis already met.
        return _SCALE_TIER_FATAL                     # ok:True but unproven scale -> fail-closed.
    if scale_result.get("error_family") == _SCALE_EFL_UNDEFINED:
        return _SCALE_TIER_PARTIAL                   # the ONE benign refusal (afocal).
    return _SCALE_TIER_FATAL                         # ANY other refusal (incl. None/"" family).


def _scale_note_family(scale_result):
    """A well-formed family token for the ``scale:<fam>`` norm-note / ledger on a FATAL scale
    — NEVER a bare ``None``. Reads the refusal's ``error_family``; a fatal refusal that named
    no family (the None/empty/missing-key axis) -> the honest fallback ``scale_refused`` (the
    ``dispatch_error`` precedent), so the note is never ``scale:None``."""
    if isinstance(scale_result, dict):
        fam = scale_result.get("error_family")
        if fam:
            return fam
    return "scale_refused"

# The vignetting BASIS of the field leg. The replace-all
# ``set_field`` ZEROES every field's VDX/VDY/VCX/VCY, so a design carrying authored
# vignetting was measured over a full pupil it does not pass. The basis field set is
# installed IN PLACE instead (vignetting preserved BY CONSTRUCTION) and what happened is
# DISCLOSED on every row, in BOTH artifacts.
_VIG_NATIVE_PRESERVED = "native_preserved"
_VIG_RESET = "reset"
_VIG_UNKNOWN = "unknown"
_VIG_BASES = (_VIG_NATIVE_PRESERVED, _VIG_RESET, _VIG_UNKNOWN)   # the CLOSED domain.
# The note per non-preserved token. ONE mapping: the field leg stamps it, and a
# POST-field-leg damage demotion re-reads it for the FINAL token, so the two can
# never disagree about what a ``reset`` row's reason says.
_VIG_NOTES = {_VIG_RESET: ("vignetting_reset",), _VIG_UNKNOWN: ("field_unset",)}
# The CLASS guard for the dropped-disclosure family found during hardening.
# ``_bench_one`` may dispatch these tools DIRECTLY because they are READ-ONLY — they
# cannot move the operating point, so there is no damage disclosure to consume. EVERY
# mutating leg must go through ``_mutating_leg``.
_BENCH_ONE_READ_ONLY_DISPATCH = ("load_design", "get_first_order")

# ...and the guard is MODULE-WIDE, not ``_bench_one``-wide. (A later hardening sweep of
# the earlier fix found: scoping the guard to ``_bench_one`` would leave a leg added
# inside ANY helper free to recreate the defect a fourth time — and a helper is exactly
# where the second instance, the verification edit, arrived.) Every ``dispatcher.dispatch``
# call site in this file, keyed by its owning function, with the tools it may dispatch.
# Adding a dispatch ANYWHERE in this module turns the AST guard RED until the author says
# which column it belongs in:
#
#   read-only     — cannot move the operating point; there is nothing to consume.
#   the field leg — ``set_field`` / ``get_system_info``, whose disclosure IS consumed, by
#                   the ``_vig_token`` + forced-token contract. This is the ONE
#                   place a mutating dispatch may sit outside the door, because it is the
#                   mechanism the door generalises.
#   the door      — ``_mutating_leg``; its tool is the caller's argument (``None`` here).
#
# ``registry.profile``'s metric dispatches are out of scope BY CONSTRUCTION: they
# run after the token is settled, and are read-only graders, not normalization legs.
#
# ROUND 4 — the census is a MULTISET, not a set. Declaring a set let a SECOND dispatch of
# an ALREADY-DECLARED tool, inside an ALREADY-DECLARED function, deduplicate away
# invisibly: a second replace-all ``set_field`` added to ``_install_basis_fields`` left the
# census EXACTLY unchanged and shipped ``native_preserved`` over zeroed factors. Each tool
# is now listed ONCE PER CALL SITE (note the two ``set_field`` sites below: the in-place
# edit and the C-5 heal), so a third turns the guard RED.
_DISPATCH_CENSUS = {
    "_census": ("load_design", "get_first_order"),       # read-only (the pre-basis pass)
    "_describe": ("describe_surfaces",),                 # read-only
    "_folded_detail": ("check_clearance",),              # read-only
    "_native_field_count": ("get_system_info",),         # read-only (the C-2 count gate)
    "_verify_final_state": ("set_field",),               # the field leg (C-4/C-4b)
    "_install_basis_fields": ("set_field", "set_field"),  # the field leg: edit + C-5 heal
    "_mutating_leg": (None,),                            # THE DOOR (tool = an argument)
    "_bench_one": _BENCH_ONE_READ_ONLY_DISPATCH,         # read-only ONLY
}
# ...and the DOOR's own call sites are censused (round 4). A ``.dispatch``-only census is
# blind to exactly the thing fix created: every mutating leg now enters through
# ``_mutating_leg``, so a NEW leg — the very event the guard exists to catch — was
# invisible. Declared function -> tools, one entry per call site. Two further rules are
# enforced structurally by the guard rather than written here, because a comment is not a
# fix: EVERY door call site must sit in the SAME function as the ``_demote_basis`` damage
# sink, and must sit BEFORE it. A leg appended after the sink has its disclosure read into
# a list that was already consumed — the damage is real, the row still ships ``ok``.
_MUTATING_LEG_CENSUS = {
    "_bench_one": ("scale_lens", "set_aperture", "set_wavelength"),
}
_FIELD_MATCH_RTOL = 1e-9          # C-4 field-set comparison.
_FIELD_MATCH_ATOL = 1e-12
_BASIS_FIELD_TYPE = "Angle"       # C-13 (written) / C-4b (verified live).
# C-9a — the quarantine is specified as what SURVIVES (a FAIL-CLOSED ALLOW-LIST), so a CSV
# column added in a FUTURE cycle is emptied BY DEFAULT and re-opening it to the ranker takes
# a reviewable keep-list edit (the `_scale_tier` denylist-fails-open lesson, applied here).
_QUARANTINE_KEEP = ("design_name", "status", "reason", "source_file", "source_kind",
                    "folded", "scaled_ok", "vignetting_basis", "notes")

# The FIXED identity/normalization/status CSV columns, in order.
_FIXED_COLUMNS = (
    "design_name", "status", "reason", "source_file", "source_kind", "folded",
    "target_efl_mm", "native_efl_mm", "scale_factor", "norm_efl_mm", "scaled_ok",
    "vignetting_basis",                      # the field-basis operating-point token.
    "notes",
)


# --------------------------------------------------------------------------- #
# Basis (the resolved target the loop normalizes to).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Basis:
    efl: Optional[float]                 # None = all-afocal folder (native scale)
    fov_fields: tuple                    # tuple of [x, y, weight]
    wavelengths: Optional[tuple]         # None = keep the design band
    fnum: Optional[float]                # None = report native (no set_aperture)
    mtf_freq: float
    samp: int
    efl_basis: str                       # "given"|"median"|"native_all_afocal"
    fov_basis: str                       # "given"|"median_max_angle"|"default"
    fnum_basis: str                      # "native"|"pinned"
    wavelength_band: str                 # "given"|"native"


def _num(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_pos(value):
    return _num(value) and math.isfinite(value) and value > 0.0


# --------------------------------------------------------------------------- #
# row_status — the 3-tier predicate (the ONE locus).
# --------------------------------------------------------------------------- #
def _norm_leg_ok(env):
    """Acceptance for a NORMALIZATION side-effect write (set_aperture/set_field/
    set_wavelength). LIVE-PROVEN against the real engine: these SUCCESS payloads do NOT
    carry an inner ``ok`` (only the 12 ok-bearing metric tools + load/scale/describe do) —
    so ``reading_ok``'s strict ``is True`` gate would false-reject a good write. Acceptance
    here: the dispatch succeeded AND the inner result is not an EXPLICIT failure (a set_*
    refusal is an ``error_envelope`` carrying ``ok:False``). This is NOT the killed
    ``_both_ok`` duplicate of ``reading_ok`` (D11): it is a DIFFERENT contract for a
    DIFFERENT envelope shape — a missing ``ok`` is SUCCESS for a side-effect writer but
    (per D11) a REJECT for an ok-bearing metric tool. Consumed ONLY by the three set_*
    legs; every other dispatch site keeps the strict ``reading_ok``. NEVER raises.

    FAIL-CLOSED on a non-dict/None inner result: an ``ok:True`` + ``result:None``
    shape is NOT a proven write — reject it, matching ``reading_ok``'s non-dict guard
    (metrics.py), so there is no laxity drift between the two acceptance predicates. The
    live set_* tools never emit this shape (they return a dict on success / raise on
    failure), so this is defensive."""
    if not isinstance(env, dict) or env.get("ok") is not True:
        return False
    res = env.get("result")
    if not isinstance(res, dict):
        return False   # a non-dict/None result is not a proven side-effect write.
    return res.get("ok") is not False


def _scale_family_of(scale_result):
    """The refusal family of a scale_lens payload (a success dict -> None)."""
    if isinstance(scale_result, dict) and scale_result.get("ok") is not True:
        return scale_result.get("error_family")
    return None


def row_status(normalized_ok, scale_result, metric_statuses):
    """Return ``'ok'|'partial'|'failed'``. The ONLY place the tier decision is made.

    - failed  : the scale is FATAL (``_scale_tier`` — any refusal that is not the benign
      ``scale_efl_undefined``, incl. a None/empty/unknown family, a non-dict/None result, a
      dispatch fault, or an unproven ``ok:True``) OR every metric is ``row_failed`` (a
      load/first-order death).
    - partial : NOT ``normalized_ok`` OR any metric status == ``tool_error`` (D7).
    - ok      : otherwise (the six soft-null tokens NEVER demote a normalized row).
    """
    if _scale_tier(scale_result) == _SCALE_TIER_FATAL:
        return "failed"
    if metric_statuses and all(s == M.STATUS_ROW_FAILED for s in metric_statuses):
        return "failed"
    if not normalized_ok:
        return "partial"
    if any(s == M.STATUS_TOOL_ERROR for s in metric_statuses):
        return "partial"
    return "ok"


# --------------------------------------------------------------------------- #
# Template normalization + basis resolution.
# --------------------------------------------------------------------------- #
def _normalize_template(template):
    """A non-dict / empty template -> the lean-core default (never crash)."""
    if not isinstance(template, dict):
        template = {}
    tb = template.get("target_basis")
    if not isinstance(tb, dict):
        tb = {}
    metrics_sel = template.get("metrics")
    if not isinstance(metrics_sel, list) or not metrics_sel:
        metrics_sel = list(_DEFAULT_METRICS)
    return {
        "target_basis": tb,
        "metrics": metrics_sel,
        "plots": template.get("plots") if isinstance(template.get("plots"), list)
        else list(_DEFAULT_PLOTS),
        "ranking": template.get("ranking"),
    }


def _angle_fields(theta):
    theta = float(theta)
    return ([0.0, 0.0, 1.0], [0.0, 0.707 * theta, 1.0], [0.0, theta, 1.0])


def _census(dispatcher, files):
    """PASS 1 (D9): read-only native EFL + half-field census (no target EFL given).

    Loads each design, reads ``get_first_order``; collects finite non-afocal EFLs and a
    per-design half-field angle estimate (``atan(|PIMH|/|EFL|)``). Afocals are EXCLUDED
    from the medians but STILL benched in pass 2. NEVER raises.
    """
    efls = []
    thetas = []
    for path in files:
        env = dispatcher.dispatch("load_design", {"path": path})
        ok, _hint, _reason = M.reading_ok(env)
        if not ok:
            continue
        fo = dispatcher.dispatch("get_first_order", {})
        ok, _hint, _reason = M.reading_ok(fo)
        if not ok:
            continue
        r = fo["result"]
        if r.get("collimated_output"):
            continue
        hd = r.get("headline") or {}
        efl = hd.get("effective_focal_length")
        if not (_num(efl) and math.isfinite(efl) and abs(efl) < _SENTINEL_MAG):
            continue
        efls.append(float(efl))
        pimh = hd.get("paraxial_image_height")
        if _num(pimh) and efl != 0:
            thetas.append(math.degrees(math.atan2(abs(pimh), abs(efl))))
    return efls, thetas


def _resolve_basis(dispatcher, files, template):
    """Resolve the target Basis. One pass when a target EFL is given; two passes
    (a read-only census) when it is not (D9)."""
    tb = template["target_basis"]
    warnings = []

    given_efl = tb.get("efl")
    given_efl = float(given_efl) if _finite_pos(given_efl) else None
    if tb.get("efl") is not None and given_efl is None:
        warnings.append(f"ignored non-positive target_basis.efl {tb.get('efl')!r}")

    census_thetas = []
    if given_efl is not None:
        efl = given_efl
        efl_basis = "given"
    else:
        efls, census_thetas = _census(dispatcher, files)
        if efls:
            efl = statistics.median(efls)
            efl_basis = "median"
            if efls and min(efls) > 0 and max(efls) / min(efls) > 100:
                warnings.append(
                    "max_efl/min_efl > 100: the folder is not one family")
        else:
            efl = None
            efl_basis = "native_all_afocal"

    # FOV.
    fov = tb.get("fov")
    if isinstance(fov, list) and fov:
        fov_fields = tuple(fov)
        fov_basis = "given"
    elif _finite_pos(fov):
        fov_fields = _angle_fields(fov)
        fov_basis = "given"
    elif census_thetas:
        fov_fields = _angle_fields(statistics.median(census_thetas))
        fov_basis = "median_max_angle"
    else:
        fov_fields = _angle_fields(_DEFAULT_THETA)
        fov_basis = "default"

    # Wavelengths / f/# / MTF frequency.
    wl = tb.get("wavelengths")
    wavelengths = tuple(wl) if isinstance(wl, list) and wl else None
    wavelength_band = "given" if wavelengths is not None else "native"

    fnum = tb.get("fnum")
    fnum = float(fnum) if _finite_pos(fnum) else None
    fnum_basis = "pinned" if fnum is not None else "native"

    mtf_freq = tb.get("mtf_frequency")
    mtf_freq = float(mtf_freq) if _finite_pos(mtf_freq) else _DEFAULT_MTF_FREQ

    basis = Basis(
        efl=efl, fov_fields=fov_fields, wavelengths=wavelengths, fnum=fnum,
        mtf_freq=mtf_freq, samp=_DEFAULT_SAMP, efl_basis=efl_basis,
        fov_basis=fov_basis, fnum_basis=fnum_basis, wavelength_band=wavelength_band)
    return basis, warnings


# --------------------------------------------------------------------------- #
# describe_surfaces-derived: asphere surface + n_elements (D2).
# --------------------------------------------------------------------------- #
def _describe(dispatcher):
    """One describe_surfaces read -> the result dict, or None on a fault."""
    env = dispatcher.dispatch("describe_surfaces", {})
    ok, _hint, _reason = M.reading_ok(env)
    if not ok:
        return None
    return env["result"]


def _asphere_surface(desc):
    """First surface index with ``type_name == "EvenAspheric"``, or None."""
    if not isinstance(desc, dict):
        return None
    for s in desc.get("surfaces") or []:
        if isinstance(s, dict) and s.get("type_name") == "EvenAspheric":
            return s.get("surface")
    return None


def _n_elements(desc):
    """Distinct-material-transition count (D2). None on a fault (never guessed)."""
    if not isinstance(desc, dict):
        return None
    surfaces = desc.get("surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        return None
    n = len(surfaces)
    count = 0
    prev_mat = None
    for i, s in enumerate(surfaces):
        if not isinstance(s, dict):
            return None
        if i == 0 or i == n - 1:   # exclude object + image
            prev_mat = normalize_material(str(s.get("material", "")))
            continue
        mat = normalize_material(str(s.get("material", "")))
        is_glass = mat not in ("AIR", "MIRROR")
        if is_glass and mat != prev_mat:
            count += 1
        prev_mat = mat
    return count


# --------------------------------------------------------------------------- #
# scale_lens seam helpers.
# --------------------------------------------------------------------------- #
# The synthetic FATAL family both scale-leg readers stamp when an ``ok:True`` dispatch
# carries a non-dict/None inner result (FIX-3, defensive): an unprovable scale is failed,
# never silently partial. Both readers use the SAME token so ``_scale_family`` (norm_notes)
# and ``_scale_family_of`` (row_status, reading the synthetic payload) AGREE (no drift).
_SCALE_RESULT_UNREADABLE = "scale_readback_failed"


def _scale_payload(scale_env):
    """The inner scale_lens dict (success payload OR the error envelope) from a dispatch.

    FAIL-CLOSED on BOTH unprovable-basis axes so ``row_status`` (which reads this payload
    via ``_scale_family_of``) tiers an unproven scale ``failed``, never silently ``partial``:

    - FIX-3 axis: an ``ok:True`` dispatch with a non-dict/None result -> a SYNTHETIC
      ``scale_readback_failed`` refusal dict.
    - FIX-A axis: an OUTER ``env.ok=False`` DISPATCH fault (the dispatcher wrapped an engine
      throw) -> a SYNTHETIC refusal dict carrying the SAME family ``_scale_family`` returns
      (the envelope's ``error_family`` or ``dispatch_error``), so both scale readers AGREE on
      this axis (no row_status-vs-norm_notes drift) and the fatal predicate fires. The ONLY
      non-fatal scale refusal — afocal ``scale_efl_undefined`` — arrives as an INNER refusal
      (``env.ok=True``), never a dispatch fault, so failing this axis closed cannot demote a
      good afocal. The live scale_lens tool never emits either synthetic shape; both are
      defensive."""
    if not isinstance(scale_env, dict):
        return None
    if scale_env.get("ok") is not True:
        # Dispatch fault (env.ok=False) — basis UNPROVEN -> a fatal refusal dict.
        fam = scale_env.get("error_family") or "dispatch_error"
        return {"ok": False, "error_family": fam,
                "error": scale_env.get("error") or "scale dispatch failed"}
    res = scale_env.get("result")
    if not isinstance(res, dict):
        return {"ok": False, "error_family": _SCALE_RESULT_UNREADABLE,
                "error": "scale returned a non-dict result"}
    return res


def _scale_family(scale_env):
    """The scale refusal family (a dispatch fail OR an inner refusal); None on success.

    FAIL-CLOSED (FIX-3): an ``ok:True`` + non-dict result is an unprovable scale ->
    ``_SCALE_RESULT_UNREADABLE`` (matching ``_scale_payload`` so both agree), never None."""
    if not isinstance(scale_env, dict):
        return None
    if scale_env.get("ok") is not True:
        return scale_env.get("error_family") or "dispatch_error"
    res = scale_env.get("result")
    if not isinstance(res, dict):
        return _SCALE_RESULT_UNREADABLE
    if res.get("ok") is not True:
        return res.get("error_family")
    return None


def _folded_detail(dispatcher):
    """behind_first_optic from a check_clearance read (folded designs only). None on fault."""
    env = dispatcher.dispatch("check_clearance", {})
    ok, _hint, _reason = M.reading_ok(env)
    if not ok:
        return None
    gb = env["result"].get("global_bfd")
    if isinstance(gb, dict):
        v = gb.get("behind_first_optic")
        if _num(v):
            return float(v)
    return None


# --------------------------------------------------------------------------- #
# The field leg — install the basis field set WITHOUT destroying the operating point.
# --------------------------------------------------------------------------- #
def _leg_payload(env):
    """The accepted inner ``result`` of a normalization write, else None. Acceptance is
    DELEGATED to ``_norm_leg_ok`` VERBATIM — ONE acceptance set for the reader and the
    gate it guards; ``_norm_leg_ok`` stays BYTE-UNCHANGED. Never raises."""
    return env.get("result") if _norm_leg_ok(env) else None


def _native_field_count(dispatcher):
    """The CURRENT field count via the READ-ONLY ``get_system_info`` (S12) — the C-2 gate
    preceding every write. ``int >= 1`` or None, gated by ``_norm_leg_ok`` (no inner ``ok``:
    the D11 boundary). FAIL-CLOSED — unreadable / non-numeric / bool / non-finite /
    non-integral / ``< 1`` -> None -> the caller heals. NEVER guessed. Never raises."""
    try:
        res = _leg_payload(dispatcher.dispatch("get_system_info", {}))
        n = res.get("field_count") if isinstance(res, dict) else None
        if not _num(n) or not math.isfinite(n) or n != int(n) or int(n) < 1:
            return None
        return int(n)
    except Exception:  # noqa: BLE001 — a dispatch/engine fault routes to the heal.
        return None


def _discloses_reset(payload):
    """The DAMAGE half of C-3, as ONE named predicate — ``vignetting_reset`` is
    present and is anything OTHER than exactly ``False``.

    Extracted into its own function because this rule now has THREE consumers, and the
    class of defect this cycle keeps re-finding is precisely "one leg reads the
    disclosure, the next one does not". C-3's wording verbatim: absent-or-exactly-``False`` is no
    disclosure; ``True`` / ``1`` / ``"false"`` / a dict — any other value — is read as
    evidence of DAMAGE (a key a tool writes ONLY when it destroyed the operating point is
    not something to interpret charitably).

    ABSENT is NOT safety, only SILENCE: a leg that says nothing tells us nothing. The
    POSITIVE half (``vignetting_preserved is True``) is what proves preservation, and only
    ``set_field``'s in-place edit emits it — so the two halves stay separate and
    ``_echo_undamaged`` is their conjunction. Never raises."""
    if not isinstance(payload, dict):
        return False
    return payload.get("vignetting_reset", False) is not False


def _echo_undamaged(payload):
    """The STRICT per-echo rule (C-3) applied to ONE accepted in-place payload:
    ``vignetting_preserved is True`` (STRICT identity — ``"True"`` / ``1`` / a missing
    key do NOT qualify) AND ``vignetting_reset`` ABSENT-or-exactly-``False`` (anything
    else — truthy, or merely present-and-odd — is read as DAMAGE, via the shared
    ``_discloses_reset``).

    ONE predicate, consumed by ``_vig_token`` (the N basis writes) AND by the primary
    path's VERIFICATION-edit check: a second copy of this rule is exactly the drift
    this cycle exists to prevent. Live-faithful — ``lens_system.set_field`` stamps
    ``vignetting_preserved: True`` on EVERY in-place edit (type-bearing or type-omitted)
    and ``vignetting_reset`` ONLY on the replace-all. Never raises."""
    return (isinstance(payload, dict)
            and payload.get("vignetting_preserved") is True
            and not _discloses_reset(payload))


def _mutating_leg(dispatcher, tool, args, damage):
    """THE ONE DOOR for a MUTATING normalization dispatch.

    Dispatches ``tool``, and — win or lose — reads the inner payload for an
    OPERATING-POINT damage disclosure via the shared ``_discloses_reset``, appending
    ``tool`` to the ``damage`` list when one is found. Returns the raw dispatch env
    (``None`` on a dispatch fault) so each caller keeps its OWN acceptance rule
    (``_norm_leg_ok`` for the ``set_*`` writers, ``_scale_tier`` for ``scale_lens``): this
    door owns the DISCLOSURE, never the acceptance.

    WHY A DOOR AND NOT ANOTHER PER-SITE PATCH. The same defect —
    *"the call succeeded, so the row is fine"* — has now been found at THREE separate
    legs: the replace-all ``set_field``, the type-omitted verification edit,
    and ``set_wavelength``. Each was fixed where it was found, and
    each fix left the NEXT leg free to recreate it, because consuming the disclosure was
    a per-site DISCIPLINE. It is now a per-site IMPOSSIBILITY: every mutating leg goes
    through this function, and a dedicated regression test
    fails if a future cycle adds a direct ``dispatcher.dispatch`` for anything outside
    ``_BENCH_ONE_READ_ONLY_DISPATCH``.

    The disclosure is read on ACCEPTED **and** REJECTED payloads: a leg that refused but
    still says it zeroed the factors has told us the operating point is gone, and
    believing the refusal instead of the disclosure is the same charitable reading C-3
    forbids.

    DELIBERATELY NOT ``try``-WRAPPED. Every call site here dispatched RAW before this
    door existed, so a dispatch throw propagated to ``bench_folder``'s own handler.
    Swallowing it to ``None`` would hand ``_scale_payload(None)`` -> ``_scale_tier(None)``
    -> ``_SCALE_TIER_OK``, i.e. a faulted scale silently tiering as "no scale attempted" —
    a fail-OPEN sibling manufactured by an earlier fix for a fail-open bug. The door adds a
    READ; it changes no control flow."""
    env = dispatcher.dispatch(tool, args)
    if isinstance(env, dict) and _discloses_reset(env.get("result")):
        damage.append(tool)
    return env


def _demote_basis(token, damaged):
    """Apply a POST-field-leg damage disclosure to a settled basis token (C-3/C-8).

    MONOTONE DOWNGRADE on the ladder ``native_preserved > reset > unknown`` — a later leg
    can only ever make the disclosure WORSE, never better, so an ``unknown`` (C-6, the
    quarantine trigger) can never be laundered into ``reset`` by a leg that happens to
    echo something.

    ``native_preserved -> reset`` is the honest token, not ``unknown``: the basis IS
    installed (the field leg proved the field SET and the live field TYPE, C-4/C-4b) and
    the vignetting factors ARE gone (the leg said so) — which is exactly what ``reset``
    means (C-5). ``unknown`` asserts the stronger, FALSE claim that the basis could not be
    established, and would fire the C-9 quarantine, suppressing a measurement that is
    valid at a disclosed operating point. Both tokens are in the C-8 closed domain; this
    picks the one that is TRUE. Pure; never raises."""
    if not damaged or token != _VIG_NATIVE_PRESERVED:
        return token
    return _VIG_RESET


def _vig_token(primary_echoes):
    """The token derived from the TOOL'S OWN ECHOES (C-3), never the branch taken — a
    path-derived token would keep reading ``native_preserved`` if ``set_field`` ever
    changed; an echo-derived one degrades instead. Fed the accepted PRIMARY in-place
    payloads ONLY (C-3a: the verification edit is itself an in-place edit and echoes
    ``vignetting_preserved: True`` even AFTER a replace-all zeroed every factor; a heal
    never calls this). Proves iff non-empty AND EVERY echo is ``_echo_undamaged``.
    Never raises."""
    proven = bool(primary_echoes) and all(_echo_undamaged(e) for e in primary_echoes)
    return _VIG_NATIVE_PRESERVED if proven else _VIG_RESET


def _basis_field_count(basis):
    """``len(basis.fov_fields)`` or ``-1`` when it is not sized. The ONE reader of that
    length: both ``_verify_final_state`` and ``_install_basis_fields`` took it
    RAW, so a ``Basis`` whose ``fov_fields`` is not a sized object (``None``) raised
    ``TypeError`` straight through two docstrings that promise "Never raises". ``-1`` can
    never equal a native count (which is ``>= 1`` by construction) and is ``< 1``, so both
    callers route to the fail-closed arm they already have. Never raises."""
    try:
        return len(basis.fov_fields)
    except Exception:  # noqa: BLE001 — a malformed Basis is an unprovable basis, not a crash.
        return -1


def _fields_match_basis(payload, basis):
    """C-4: the verification echo's ``fields`` (S11) vs ``basis.fov_fields`` — element-wise
    on x/y/weight, with an EXACT length match. The ONLY check that the N per-field edits
    COMPOSED into the intended basis (each tool-side read-back verifies its OWN field
    only). Missing / non-list / short / ragged / non-numeric -> False. Never raises."""
    got = payload.get("fields") if isinstance(payload, dict) else None
    # A sibling of the ``_install_basis_fields`` fix, found on the same hardening sweep:
    # this ``len(basis.fov_fields)`` sat outside the try as well, so the SAME
    # unsized-Basis input raised straight through the SAME "Never raises" claim. Closed
    # at the shared reader, not per-site.
    n_want = _basis_field_count(basis)
    if not isinstance(got, list) or n_want < 0 or len(got) != n_want:
        return False
    try:
        for entry, triple in zip(got, basis.fov_fields):
            if not isinstance(entry, dict) or len(triple) < 3:
                return False
            for key, ref in zip(("x", "y", "weight"), triple):
                val = entry.get(key)
                if not (_num(val) and _num(ref)) or not math.isclose(
                        float(val), float(ref),
                        rel_tol=_FIELD_MATCH_RTOL, abs_tol=_FIELD_MATCH_ATOL):
                    return False
    except Exception:  # noqa: BLE001 — any malformed shape is a fail-closed mismatch.
        return False
    return True


def _field_type_is_basis(payload):
    """C-4b: the verification echo's LIVE ``field_type`` (S13) vs ``_BASIS_FIELD_TYPE`` —
    a non-empty ``str`` whose LAST ``.``-separated token, stripped and casefolded, EQUALS
    ``"angle"``. EXACT FULL TOKEN: a substring test would admit ``TheodoliteAngle`` (the
    ``Gradient1 ⊂ Gradient10`` class). Anything else -> False -> heal. Never raises."""
    raw = payload.get("field_type") if isinstance(payload, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return False
    return raw.strip().rsplit(".", 1)[-1].strip().casefold() == \
        _BASIS_FIELD_TYPE.casefold()


def _verify_final_state(dispatcher, basis):
    """The VERIFICATION EDIT (C-4 + C-4b) — the single proof of the FINAL field state.

    ONE in-place ``set_field(index=N, x=, y=, weight=)`` carrying field N's OWN basis
    triple (IDEMPOTENT — the tool read-back-verifies it) that DELIBERATELY OMITS
    ``field_type``: the ONLY shape under which ``set_field`` returns a LIVE
    ``str(fields.GetFieldType())`` rather than a request echo (S13, C-13 second half) —
    supplying it here would DISABLE the only live type readback the tool offers. Returns
    ``(ok, payload|None)``, ok iff accepted AND C-4 AND C-4b. Runs on the primary path AND
    again after an accepted heal (whose own type echo is request-derived, so it proves
    nothing). Never raises."""
    n = _basis_field_count(basis)
    if n < 1:
        return (False, None)
    try:
        x, y, w = (float(v) for v in tuple(basis.fov_fields[n - 1])[:3])
        payload = _leg_payload(dispatcher.dispatch(
            "set_field", {"index": n, "x": x, "y": y, "weight": w}))
    except Exception:  # noqa: BLE001 — an unusable basis triple / engine fault.
        return (False, None)
    if payload is None:
        return (False, None)
    return (_fields_match_basis(payload, basis)
            and _field_type_is_basis(payload), payload)


def _install_basis_fields(dispatcher, basis):
    """Install ``basis.fov_fields``; report what happened to the per-field vignetting.
    Returns ``{"basis": <token>, "native_count": int|None, "notes": [str]}``.

    PRIMARY (C-1/C-2/C-13) — the native count equals ``len(basis.fov_fields)`` and is
    ``>= 1``: write each field IN PLACE (``index=k`` + x/y/weight), which touches only
    those cells (no AddField/DeleteFieldAt) so every field's VDX/VDY/VCX/VCY survives BY
    CONSTRUCTION. ``field_type`` rides every BASIS-WRITING edit because an omitted one
    SKIPS ``SetFieldType`` entirely (S10) — a non-Angle design would keep its type while
    basis ANGLES were written into its cells. The index mapping is the IDENTITY, the only
    well-defined one (R3). Then ONE ``_verify_final_state``; ``native_preserved`` iff the
    echoes prove (C-3) AND that verification passes.

    HEAL (C-5) — counts differ / count unreadable / ANY edit refused / token unproven /
    verification failed: ONE replace-all, which rebuilds the whole set (so it also heals a
    HALF-written one) and GUARANTEES the basis is installed at the cost of
    ZEROING vignetting -> ``reset``, disclosed. Then one more ``_verify_final_state``.
    UNPROVEN (C-6) — that heal refused/faulted, or its verification edit refused, or the
    post-heal C-4/C-4b failed -> ``unknown`` (the C-9 quarantine trigger).

    C-3a: the heal path NEVER calls ``_vig_token``; its token is FORCED. Never raises."""
    native = _native_field_count(dispatcher)
    token = None

    if native is not None and native == _basis_field_count(basis):
        echoes = []
        for idx, triple in enumerate(basis.fov_fields, start=1):
            try:
                x, y, w = (float(v) for v in tuple(triple)[:3])
                payload = _leg_payload(dispatcher.dispatch("set_field", {
                    "index": idx, "x": x, "y": y, "weight": w,
                    "field_type": _BASIS_FIELD_TYPE}))
            except Exception:  # noqa: BLE001 — a fault part-way in heals (found on sweep).
                payload = None
            if payload is None:
                echoes = None        # refused/faulted -> the token can never prove.
                break
            echoes.append(payload)
        if echoes is not None and _vig_token(echoes) == _VIG_NATIVE_PRESERVED:
            verified, verify_payload = _verify_final_state(dispatcher, basis)
            # A later hardening round found this sibling — the verification edit ITSELF created it.
            # ``_verify_final_state`` proves the field SET and the live field TYPE; it
            # does NOT look at its own vignetting disclosure, and ``_vig_token`` is fed
            # only the EARLIER basis writes.  So a verification edit that zeroed every
            # factor while echoing ``vignetting_reset: True`` shipped ``native_preserved``
            # + ``status: ok`` over the ticket's own collapsed corner number — the
            # dropped-disclosure defect, recreated one layer up by its own fix.  The SAME
            # strict no-damage rule the N basis writes are held to now governs it.
            #
            # PRIMARY PATH ONLY.  On the heal path the token is FORCED (C-3a) to
            # ``reset``/``unknown``, which is strictly safer than any echo could make it;
            # reading a damage token there could only turn an honest ``reset`` into
            # ``unknown``, changing C-5 for no gain.
            if verified and _echo_undamaged(verify_payload):
                token = _VIG_NATIVE_PRESERVED

    if token is None:
        try:
            healed = _leg_payload(dispatcher.dispatch("set_field", {
                "field_type": _BASIS_FIELD_TYPE,
                "fields": [list(f) for f in basis.fov_fields]})) is not None
        except Exception:  # noqa: BLE001 — an engine fault leaves the basis unproven.
            healed = False
        # C-3a: FORCED, never echo-derived — the verification edit proves the field SET
        # and TYPE, never the vignetting (a replace-all has already zeroed it).
        token = ((_VIG_RESET if _verify_final_state(dispatcher, basis)[0]
                  else _VIG_UNKNOWN) if healed else _VIG_UNKNOWN)

    return {"basis": token, "native_count": native,
            "notes": list(_VIG_NOTES.get(token, ()))}


# --------------------------------------------------------------------------- #
# Row + manifest assembly.
# --------------------------------------------------------------------------- #
def _design_name(path):
    return os.path.splitext(os.path.basename(path))[0]


def _selected_columns(template):
    """The ordered metric column names for the selected metrics (unknown key -> the raw
    key as a single column). Dedups against the fixed block."""
    cols = []
    seen = set(_FIXED_COLUMNS)
    for key in template["metrics"]:
        canon = registry.resolve_key(key)
        if canon is None:
            name = str(key)
            if name not in seen:
                cols.append(name)
                seen.add(name)
            continue
        for spec in registry.REGISTRY[canon].columns:
            if spec.name not in seen:
                cols.append(spec.name)
                seen.add(spec.name)
    return cols


def _failed_row(path, reason, template, *, source_file="", folded=False,
                native_efl=None, target_efl=None):
    """A ``failed`` design's flat CSV row — every metric cell EMPTY."""
    row = {c: None for c in _FIXED_COLUMNS}
    row["design_name"] = _design_name(path)
    row["status"] = "failed"
    row["reason"] = reason
    row["source_file"] = source_file or path
    row["source_kind"] = "zmx"
    row["folded"] = bool(folded)
    row["target_efl_mm"] = target_efl
    row["native_efl_mm"] = native_efl
    row["notes"] = ""
    for c in _selected_columns(template):
        row[c] = None
    row["n_elements"] = None
    return row


def _quarantine_cells(template, reason):
    """C-9(1): stamp EVERY selected metric column ``(None, tool_error, reason)`` WITHOUT
    any dispatch — an unproven field basis means no measurement is taken at all. DELEGATES
    to ``registry.stamp_selected`` so the cell dict is built by the SAME ``registry._cell``
    constructor ``profile`` uses. Pure; never raises."""
    return registry.stamp_selected(template["metrics"], M.STATUS_TOOL_ERROR, reason)


def _renders_finite(value):
    """True when ``value`` would reach the ranker, through the CSV, as a FINITE NUMBER.

    The predicate is the RANKER'S OWN ``classify_cell`` applied to the WRITER'S OWN
    ``_fmt`` render — the actual two-step a cell takes from this row to a ranking axis —
    so "finite" has exactly ONE definition in the codebase. Fail-CLOSED: an
    unclassifiable value is treated as a vote and emptied. Never raises."""
    try:
        return _classify_cell(_fmt(value))[0] == "finite"
    except Exception:  # noqa: BLE001 — unclassifiable -> assume it votes -> empty it.
        return True


def _quarantine_row(row):
    """C-9(2)/C-9a: empty EVERY rank-addressable cell of an assembled row. TWO locks,
    both fail-closed, because the keep-list alone was not one:

    1. **the ALLOW-LIST** — every key NOT in ``_QUARANTINE_KEEP`` is set to ``None``, so a
       CSV column added in a future cycle is emptied BY DEFAULT and re-opening it to the
       ranker takes a reviewable keep-list edit.
    2. **the INVARIANT** (round 2) — a KEPT cell that would still RENDER as a
       finite number is emptied too. The keep-list is a list of columns *assumed* to
       render non-numeric, and that assumption is FALSE for ordinary inputs: a bare
       patent-number file stem (``4182550.zmx`` — this bench's own staging convention)
       renders ``design_name`` as ``4182550``, and a truthy-non-bool ``scaled_ok`` echo
       renders ``1``. Either one let an UNMEASURED row score ``0.5`` and rank FIRST under
       ``ranking.priorities=["design_name"]``, breaching C-9.3/I11's "under ANY
       ``ranking.priorities``" verbatim.

    Lock 2 enforces the PROPERTY ("nothing finite survives quarantine") rather than
    auditing the NAMES, so it closes ``design_name``, ``scaled_ok`` and every
    FUTURE keep-list member at once, without a per-name argument about how each renders.

    This removes the row's VOTE, not its RECORD: every emptied cell has a manifest twin
    left FULLY populated (S17) — including ``design_name``, which the manifest block
    always carries — and the CSV row keeps ``source_file`` (the path, a non-numeric
    string), ``status``, ``reason`` and ``vignetting_basis``. Mutates and returns ``row``;
    pure otherwise; never raises."""
    for key in list(row):
        if key not in _QUARANTINE_KEEP or _renders_finite(row[key]):
            row[key] = None
    return row


def _failed_manifest(path, stage, family, message, *, source_file="", folded=False):
    return {
        "design_name": _design_name(path), "status": "failed", "reason": message,
        "source_file": source_file or path, "source_kind": "zmx", "folded": bool(folded),
        "heavily_rescaled": False,
        # C-16: the two vignetting-basis-disclosure keys ride EVERY design block, a failed
        # one included (the same empty values its CSV row carries), so the manifest writer
        # needs no "except sometimes".
        "normalization": {"vignetting_basis": "", "native_field_count": None},
        "metrics": {}, "folded_detail": {},
        "_ledger": {"design_name": _design_name(path), "stage": stage,
                    "error_family": family, "message": message},
    }


def _norm_block(native_efl, scale_result, normalized_ok, norm_notes,
                vignetting_basis=None, native_field_count=None):
    """The manifest normalization trace (verbatim scale_lens keys + status).

    ``vignetting_basis`` / ``native_field_count`` are the vignetting-basis disclosure —
    the token, and the count that decided which field-write path ran. Both default, so any
    direct caller keeps working. A QUARANTINED design's block stays FULLY POPULATED: the
    C-9 CSV emptying is a ranking-surface action, not a disclosure one."""
    block = {
        "native_efl_mm": native_efl, "applied_factor": None, "norm_efl_mm": None,
        "scaled_ok": None, "scale_status": None, "scaled_config_only": None,
        "normalized_ok": normalized_ok, "norm_notes": list(norm_notes),
        "vignetting_basis": vignetting_basis, "native_field_count": native_field_count,
        "trace": {},
    }
    if isinstance(scale_result, dict):
        if scale_result.get("ok") is True:
            block["applied_factor"] = scale_result.get("applied_factor")
            block["norm_efl_mm"] = scale_result.get("efl_after")
            # (round 2), the SOURCE half: ``scaled_ok`` was copied from the tool
            # payload VERBATIM — unlike ``folded``, which ``_bench_one`` ``bool(...)``-
            # coerces — so a truthy-NON-bool echo (``1``, a documented reachable
            # ``scale_lens`` shape) rendered the CSV cell ``1`` instead of ``true``, i.e.
            # a FINITE rank-addressable number in a column meant to be a boolean. Coerce
            # to a real bool, but keep ``None`` (= "the tool did not report it") distinct
            # from ``False`` (= "it reported a failure") — collapsing them would destroy
            # disclosure to fix a rendering bug.
            # MED (round 3), the SECOND half of the same line: ``bool(...)`` fixed the
            # RENDERING (``1`` -> a finite CSV number) but LAUNDERED the VALUE. The
            # decision-maker, ``_scale_tier``, requires ``scaled_ok is True`` — so
            # ``scaled_ok=1`` and ``scaled_ok="false"`` are both graded FATAL while these
            # artifacts announced ``scaled_ok: True`` / ``scale_status: "ok"``. The row
            # could not vote, but every human-readable trace of it contradicted the
            # verdict. Grade the disclosure with the SAME predicate the tier uses,
            # so the artifacts and the decision cannot disagree; ``None`` (= "the tool did
            # not report it") stays DISTINCT from ``False`` (= "it did not prove a scale").
            raw_scaled_ok = scale_result.get("scaled_ok")
            block["scaled_ok"] = (None if raw_scaled_ok is None
                                  else raw_scaled_ok is True)
            # ...and the status token follows the same proof: an ``ok:True`` payload that
            # proved NEITHER ``scaled_ok`` NOR ``already_at_target`` is the fail-closed
            # ``_SCALE_TIER_FATAL`` arm, and must not report itself as "ok".
            block["scale_status"] = (
                "already_at_target" if scale_result.get("already_at_target")
                else ("ok" if _scale_tier(scale_result) == _SCALE_TIER_OK
                      else "unproven"))
            block["scaled_config_only"] = scale_result.get("scaled_config_only")
            block["trace"] = {
                "efl_before": scale_result.get("efl_before"),
                "efl_after": scale_result.get("efl_after"),
                "totr_before": scale_result.get("totr_before"),
                "totr_after": scale_result.get("totr_after"),
                "wfno_before": scale_result.get("wfno_before"),
                "wfno_after": scale_result.get("wfno_after"),
            }
        else:
            block["scale_status"] = scale_result.get("error_family")
            block["scaled_ok"] = False
    return block


def _bench_one(dispatcher, path, basis, template):
    """The per-design decision tree (load -> classify -> normalize -> profile). Returns
    ``(row, manifest_block)``. A load/first-order death is a ``failed`` row (all metric
    cells empty); a wrongly-scaled measurement is ``failed`` (via ``row_status``)."""
    name = _design_name(path)

    # ---- Stage 1: LOAD ----
    env = dispatcher.dispatch("load_design", {"path": path})
    ok, _hint, reason = M.reading_ok(env)
    if not ok:
        return (_failed_row(path, f"load: {reason}", template,
                            target_efl=basis.efl),
                _failed_manifest(path, "load", reason, f"load: {reason}"))
    load = env["result"]
    source_file = load.get("system_file") or path
    n_configs = load.get("number_of_configurations") or 1
    active_config = load.get("active_configuration") or 1

    # ---- Stage 2: CLASSIFY ----
    fo_env = dispatcher.dispatch("get_first_order", {})
    ok, _hint, reason = M.reading_ok(fo_env)
    flags = set()
    if n_configs > 1:
        flags.add("multi_config")
    if not ok:
        flags.add("first_order_unreadable")
        return (_failed_row(path, f"first_order: {reason}", template,
                            source_file=source_file, target_efl=basis.efl),
                _failed_manifest(path, "classify", reason,
                                 f"first_order: {reason}", source_file=source_file))
    fo = fo_env["result"]
    folded = bool(fo.get("folded"))
    if folded:
        flags.add("folded")
    native_efl = (fo.get("headline") or {}).get("effective_focal_length")
    if bool(fo.get("collimated_output")):
        flags.add("afocal")
        flags.add("collimated_output")
    if native_efl is None:
        flags.add("afocal")
        flags.add("efl_sentinel")

    # ---- Stage 3: NORMALIZE (order per D6) ----
    scale_env = None
    scale_result = None
    normalized_ok = True
    norm_notes = []
    # The CLASS sink: every MUTATING leg below routes through ``_mutating_leg``,
    # which drops the tool's name here when its payload discloses that it destroyed the
    # operating point. Consumed ONCE, after the last leg, so no leg's disclosure can be
    # stranded by the one that follows it.
    vig_damage = []

    if "efl_sentinel" in flags:
        normalized_ok = False
        norm_notes.append("afocal_native_scale")
    elif basis.efl is not None:
        scale_env = _mutating_leg(
            dispatcher, "scale_lens", {"mode": "to_efl", "value": basis.efl}, vig_damage)
        scale_result = _scale_payload(scale_env)
        # The norm-note consumes the SAME `_scale_tier` verdict row_status stamps (no drift).
        tier = _scale_tier(scale_result)
        if tier == _SCALE_TIER_PARTIAL:
            # Afocal — geometry intact, measured native (partial, NOT failed).
            normalized_ok = False
            norm_notes.append("afocal_native_scale")
        elif tier == _SCALE_TIER_FATAL:
            # EVERY fatal scale (incl. a None/empty-family refusal) -> failed via row_status.
            normalized_ok = False
            norm_notes.append(f"scale:{_scale_note_family(scale_result)}")

    if "multi_config" in flags:
        normalized_ok = False
        norm_notes.append("multiconfig_active_config_only")
    if "collimated_output" in flags and "efl_sentinel" not in flags:
        normalized_ok = False
        if "collimated_output" not in norm_notes:
            norm_notes.append("collimated_output")

    # set_aperture (only if f/# pinned) -> set_field -> set_wavelength (each non-fatal).
    # These side-effect writers do NOT emit an inner ``ok`` -> use _norm_leg_ok, NOT the
    # strict metric-tool reading_ok (confirmed live against the real engine).
    if basis.fnum is not None:
        e = _mutating_leg(dispatcher, "set_aperture",
                          {"aperture_type": "ImageSpaceFNum", "value": basis.fnum},
                          vig_damage)
        if not _norm_leg_ok(e):
            normalized_ok = False
            norm_notes.append("fnum_unset")
    # Install the basis fields IN PLACE (vignetting preserved) and disclose
    # the operating point; a heal/unproven basis demotes the row (C-7/C-10).
    field_out = _install_basis_fields(dispatcher, basis)
    vig_basis = field_out["basis"]
    if vig_basis != _VIG_NATIVE_PRESERVED:
        normalized_ok = False
        norm_notes.extend(field_out["notes"])
    if basis.wavelengths is not None:
        e = _mutating_leg(dispatcher, "set_wavelength",
                          {"wavelengths": [list(w) for w in basis.wavelengths]},
                          vig_damage)
        if not _norm_leg_ok(e):
            normalized_ok = False
            norm_notes.append("wavelength_unset")

    # ---- The CLASS consumption point (round 3, HIGH) ----
    # Stage 3 is over; NO further leg can move the operating point, so this is the one
    # place the accumulated damage is applied — after the last leg, before any profiling.
    #
    # The defect this closes: ``vig_basis`` was frozen the instant the FIELD leg returned,
    # while ``set_wavelength`` (and, before it, ``scale_lens``) still ran and could still
    # zero every factor. A dispatcher that did exactly that — and SAID so with
    # ``vignetting_reset: True`` — shipped ``vignetting_basis="native_preserved"``,
    # ``status="ok"``, an EMPTY reason, and the ticket's own collapsed corner MTF. Three
    # legs, one defect; the door above makes the fourth leg impossible rather than
    # merely reviewed.
    demoted = _demote_basis(vig_basis, vig_damage)
    if demoted != vig_basis:
        vig_basis = demoted
        # C-7/C-10 in full: the token alone is not the disclosure — the row must also stop
        # reading ``ok`` and must carry a non-empty reason naming what happened.
        normalized_ok = False
        for note in _VIG_NOTES.get(demoted, ()):
            if note not in norm_notes:
                norm_notes.append(note)

    # ---- Stage 4: PROFILE + assemble ----
    desc = _describe(dispatcher)
    asphere_surface = _asphere_surface(desc)
    n_elem = _n_elements(desc)
    config_sweep = {}   # D4/D5 manifest disclosure sink (FIX-4); profile fills it.
    ctx = {"flags": sorted(flags), "n_configs": n_configs,
           "active_config": active_config, "afocal": "afocal" in flags,
           "opts": {"asphere_surface": asphere_surface},
           "config_sweep": config_sweep}
    if vig_basis == _VIG_UNKNOWN:
        # C-9: the field basis could not be established -> DO NOT MEASURE. Every metric
        # column is stamped without a dispatch; the row cannot vote (quarantined below).
        cells = _quarantine_cells(template, "field_basis_unproven")
    else:
        cells = registry.profile(dispatcher, template["metrics"], basis, ctx)

    metric_statuses = [c["status"] for c in cells.values()]
    status = row_status(normalized_ok, scale_result, metric_statuses)
    is_failed = status == "failed"
    # C-8/C-14 (MED, round 2) — a row can reach Stage 4 and STILL be graded ``failed`` by
    # ``row_status`` (a FATAL scale, or every metric dying). Its field leg ran and produced
    # a live token, but the row was never validly benched: C-8 says a ``failed`` row's cell
    # is ``""`` in BOTH artifacts, and C-14 says only benched rows feed the run tally. The
    # unconditional stamp gave a fatally-mis-scaled design a ``native_preserved`` cell,
    # counted it, and — with one genuinely benched sibling — fired the mixed-convention
    # WARNING over a single real convention (noise in an honesty channel). Normalise the
    # token HERE, before both the manifest block and the CSV row read it, so the two
    # artifacts cannot disagree and ``_build_manifest`` (which tallies from the block) can
    # never see it. ``native_field_count`` is NOT emptied — it is manifest-only, not
    # rank-addressable, and it is the honest record of what the field leg read.
    disclosed_basis = "" if is_failed else vig_basis

    folded_detail = {}
    if folded:
        bfd = _folded_detail(dispatcher)
        folded_detail["bfd_behind_primary_mm"] = bfd

    norm_block = _norm_block(native_efl, scale_result, normalized_ok, norm_notes,
                             vignetting_basis=disclosed_basis,
                             native_field_count=field_out["native_count"])
    heavily = False
    af = norm_block.get("applied_factor")
    if _num(af) and af > 0 and (af > 10 or af < 0.1):
        heavily = True

    # Assemble the flat CSV row.
    row = {c: None for c in _FIXED_COLUMNS}
    row["design_name"] = name
    row["status"] = status
    row["reason"] = "" if status == "ok" else ("; ".join(norm_notes) or status)
    row["source_file"] = source_file
    row["source_kind"] = "zmx"
    row["folded"] = folded
    row["target_efl_mm"] = basis.efl
    row["native_efl_mm"] = native_efl if _num(native_efl) else None
    row["scale_factor"] = norm_block.get("applied_factor")
    row["norm_efl_mm"] = norm_block.get("norm_efl_mm")
    row["scaled_ok"] = norm_block.get("scaled_ok")
    row["vignetting_basis"] = disclosed_basis
    row["notes"] = "; ".join(_cell_notes(cells))

    for colname in _selected_columns(template):
        cell = cells.get(colname)
        row[colname] = None if (is_failed or cell is None) else cell["value"]
    row["n_elements"] = None if is_failed else n_elem

    manifest_block = {
        "design_name": name, "status": status,
        "reason": row["reason"], "source_file": source_file, "source_kind": "zmx",
        "folded": folded, "heavily_rescaled": heavily,
        "normalization": norm_block,
        "metrics": {k: dict(v) for k, v in cells.items()},
        "folded_detail": folded_detail,
        "n_elements": n_elem,
    }
    if n_configs > 1:
        # D4/D5 disclosure (FIX-4): WHICH config the single-config (spot/MTF) metrics
        # measured, and the per-metric config="all" coverage summary of the swept metrics.
        manifest_block["measured_config"] = active_config
        manifest_block["config_sweep"] = config_sweep
    if is_failed:
        # A fatal scale names an HONEST family (never a bare None on a None/empty-family
        # refusal); a metric-death failure (scale non-fatal) keeps the plain family reader.
        fam = (_scale_note_family(scale_result)
               if _scale_tier(scale_result) == _SCALE_TIER_FATAL
               else _scale_family_of(scale_result))
        manifest_block["_ledger"] = {
            "design_name": name, "stage": "scale",
            "error_family": fam, "message": row["reason"]}
    if vig_basis == _VIG_UNKNOWN:
        # C-9(2)/C-9a — the LAST step, so no later assignment can re-expose a cell.
        _quarantine_row(row)
    return row, manifest_block


def _cell_notes(cells):
    """The ``;``-joined per-cell soft-null flags for the CSV ``notes`` column."""
    notes = []
    for name, cell in cells.items():
        if cell.get("status") not in (M.STATUS_OK,) and cell.get("reason"):
            notes.append(f"{name}:{cell['reason']}")
    return notes


# --------------------------------------------------------------------------- #
# Writers.
# --------------------------------------------------------------------------- #
def _count_columns():
    """The set of ``kind=='count'`` metric column names (rendered as INT strings, FIX-2)."""
    names = set()
    for adapter in registry.REGISTRY.values():
        for spec in adapter.columns:
            if spec.kind == "count":
                names.add(spec.name)
    return names


def _fmt(value, is_count=False):
    """CSV cell formatting: None -> '' (the empty cell); bool -> 'true'/'false';
    a number/string passes through (0/0.0 -> the literal). Null is NEVER 0.

    A ``count`` column (FIX-2) renders a finite number as an INT string (``3.0`` -> ``'3'``,
    the coerce_metric float never surfaces as ``'3.0'``); null still -> the empty cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if is_count and isinstance(value, (int, float)) and math.isfinite(value):
        return str(int(value))
    return value


def _write_csv(out_dir, rows, template):
    header = list(_FIXED_COLUMNS) + _selected_columns(template) + ["n_elements"]
    count_cols = _count_columns()
    path = os.path.join(out_dir, "catalog_metrics.csv")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for row in rows:
            writer.writerow([_fmt(row.get(col), col in count_cols) for col in header])
    return path


def _build_manifest(basis, template, manifest_blocks, warnings, counts):
    designs = []
    ledger = []
    for block in manifest_blocks:
        block = dict(block)
        led = block.pop("_ledger", None)
        if led is not None:
            ledger.append(led)
        designs.append(block)
    # C-14 — a DENSE tally over the CLOSED `_VIG_BASES` domain (every token is a key,
    # zero-counts included, so a reader learns the domain). Only BENCHED blocks
    # contribute; a failed row's "" is not a token. A run mixing conventions WARNS once.
    tally = {token: 0 for token in _VIG_BASES}
    for block in designs:
        norm = block.get("normalization")
        seen = norm.get("vignetting_basis") if isinstance(norm, dict) else None
        if isinstance(seen, str) and seen in tally:
            tally[seen] += 1
    warnings = list(warnings)
    if len([t for t, c in tally.items() if c > 0]) > 1:
        detail = ", ".join(f"{t}: {tally[t]}" for t in _VIG_BASES if tally[t] > 0)
        warnings.append(
            f"vignetting basis differs across designs ({detail}) — off-axis metrics are "
            "not strictly comparable; see the per-row vignetting_basis column")
    return {
        "run": {
            "target_basis": {
                "efl_mm": basis.efl, "efl_basis": basis.efl_basis,
                "fov_fields": [list(f) for f in basis.fov_fields],
                "fov_basis": basis.fov_basis,
                "fnum": basis.fnum, "fnum_basis": basis.fnum_basis,
                "wavelengths_um": ([list(w) for w in basis.wavelengths]
                                   if basis.wavelengths is not None else None),
                "wavelength_band": basis.wavelength_band,
                "mtf_frequency_cyc_mm": basis.mtf_freq, "samp": basis.samp,
            },
            "template": {"metrics": template["metrics"], "plots": template["plots"],
                         "ranking": template["ranking"]},
            "warnings": warnings,
            "vignetting_bases": tally,
            "n_designs": counts["n_designs"], "n_ok": counts["n_ok"],
            "n_partial": counts["n_partial"], "n_failed": counts["n_failed"],
        },
        "designs": designs,
        "ledger": ledger,
    }


def _write_manifest(out_dir, manifest):
    path = os.path.join(out_dir, "manifest.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=1, default=str)
    return path


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def bench_folder(dispatcher, staging_dir, template, out_dir):
    """Bench every ``.zmx`` in ``staging_dir`` to a common basis; write
    ``catalog_metrics.csv`` + ``manifest.json`` into ``out_dir``.

    TWO passes when no target EFL is given (a read-only median census, D9); one pass when
    a target EFL is given. Each design is benched inside a defense-in-depth ``try`` (a
    bench-side assembly bug becomes THAT design's ``failed`` row; the dispatcher never
    raises). Returns ``{ok, csv_path, manifest_path, n_designs, n_ok, n_partial,
    n_failed, rows, manifest}``.
    """
    template = _normalize_template(template)
    os.makedirs(out_dir, exist_ok=True)   # a missing dir is otherwise a silent no-op.
    files = sorted(glob.glob(os.path.join(staging_dir, "*.zmx")))

    basis, warnings = _resolve_basis(dispatcher, files, template)

    rows = []
    manifest_blocks = []
    for path in files:
        try:
            row, block = _bench_one(dispatcher, path, basis, template)
        except Exception as exc:  # noqa: BLE001 — per-design defense-in-depth
            row = _failed_row(path, f"bench: {exc!r}", template, target_efl=basis.efl)
            block = _failed_manifest(path, "bench", "internal", f"bench: {exc!r}")
        rows.append(row)
        manifest_blocks.append(block)

    n_ok = sum(1 for r in rows if r["status"] == "ok")
    n_partial = sum(1 for r in rows if r["status"] == "partial")
    n_failed = sum(1 for r in rows if r["status"] == "failed")
    counts = {"n_designs": len(rows), "n_ok": n_ok, "n_partial": n_partial,
              "n_failed": n_failed}

    csv_path = _write_csv(out_dir, rows, template)
    manifest = _build_manifest(basis, template, manifest_blocks, warnings, counts)
    manifest_path = _write_manifest(out_dir, manifest)

    return {
        "ok": True, "csv_path": csv_path, "manifest_path": manifest_path,
        "n_designs": len(rows), "n_ok": n_ok, "n_partial": n_partial,
        "n_failed": n_failed, "rows": rows, "manifest": manifest,
    }


__all__ = ["Basis", "row_status", "bench_folder", "_bench_one", "_resolve_basis"]
