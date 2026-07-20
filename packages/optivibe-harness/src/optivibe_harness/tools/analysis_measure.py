"""tools/analysis_measure.py — first-class measurement tools.

Four dispatchable measurement tools that read named optical quantities through the
probe-grounded named-slot operand reader + the side-effect-free best-focus scan,
so the agent gets the headline acceptance numbers (focal length, Strehl, RMS
wavefront, axial color) WITHOUT hand-driving operand rows or a thickness solve:

- ``get_first_order``     (locked D8) — EFFL/WFNO/PIMH/EXPP/ENPP/TOTR + BFL from
  the back-airgap thickness. EXCLUDES EFLX/EFLY (the 1e10 rot-sym sentinel).
  A suspicious reading is dropped from the headline + named in ``flags``.
- ``analyze_strehl``      (locked D4/A) — ``STRH`` per wave, reported at BOTH the
  current image plane AND best focus (the dual-focus default). Best focus = a
  per-wave back-airgap scan (poly = QuickFocus), always restored.
- ``analyze_wavefront``   (locked D5/D6/B) — ``RWCE`` (RMS-to-centroid, "the RMS")
  + ``RWRE`` (RMS-to-chief), per wave, at both planes. ``samp`` density guard
  (>= 1; default 6; a value < 1 -> ``measurement_param``, never clamped). PV is
  unavailable (no PVCE/PVRE operand) — reported as such, never fabricated.
- ``analyze_axial_color`` (locked D7/C) — ``AXCL`` scalar (F-C focus shift in mm)
  with the F/C wavelength indices RESOLVED by matching the 0.4861/0.6563 µm values
  in ``SystemData.Wavelengths`` (NOT hard-coded — AXCL sign-flips on swap). The
  121-pt ``FocalShiftDiagram`` curve returns only on ``full=True``; when present,
  AXCL<->curve agreement is asserted (else ``axial_color_inconsistent``).

Every tool returns the uniform ``{ok, tool, ...}`` envelope, NEVER raises past the
handler (locked D10): an expected failure is an ``error_envelope`` family
(``measurement_param``, ``analysis_empty``, ``best_focus_unavailable``,
``axial_color_inconsistent``). Every operand scalar carries a per-reading
``suspicious`` bool; a per-block ``flags`` list aggregates them (one
``if result["flags"]`` check for the grader). Every linear quantity carries an
explicit ``units``.

Covered by live ZOS-API integration (reload the doublet, grade all acceptance from
tool outputs) and unit tests (fixture-seeded slot semantics + per-wave best-focus
scan).
"""
import functools
import math

from .._io import safe_float
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _asphere_cells as _asph
from . import _collimation as _col
from . import _config_common as _cfg
from . import _layout_geometry as _geom
from . import _measurement_common as _mc
from . import _optimize_common as _oc
from . import _structural_common as _sc
from . import analysis_mtf as _amtf

# (A3) The dominant-gap fraction of total_track that, paired with an
# inert + collimated gap, flags a suspicious orphaned ghost dead-space. Probe: ghost
# = 0.905, healthy max = 0.16 (regimes ~60x apart).
_GHOST_FRACTION = 0.6

# (geometry-readouts gap #2) The folded-system honesty flag for get_first_order:
# back_focal_length stays the raw back-airgap thickness, but on a folded system
# that is NOT the behind-primary clearance — name the check_clearance field to use.
_FOLDED_BFL_FLAG = (
    "back_focal_length is the raw back-airgap thickness (NOT the behind-primary "
    "clearance on a folded system) — use check_clearance.global_bfd.behind_first_optic"
)

# (afocal/collimation §3.1) The ONE loud flag the analyzers append when the shared
# detector recognizes a collimated / afocal output (image at infinity). The
# image-plane Strehl/RMS-wavefront is measured against an ABSENT plane and is NOT
# meaningful (the headline is nulled for the two scan doors; kept-with-flag for the
# lighter first-order door). Grade collimation with verify_collimation.
_COLLIMATED_FLAG_MSG = (
    "collimated_output: this system has a collimated / afocal output (image at "
    "infinity); the image-plane Strehl/RMS-wavefront is measured against an absent "
    "plane and is NOT meaningful (the headline is nulled). Grade collimation with "
    "verify_collimation."
)


# (§2) The far/virtual-ENPP "ray aiming recommended" heuristic. The entrance
# pupil is materially displaced from the front vertex when |ENPP| exceeds a fraction of
# |EFFL| (a DISPLACED — internal/rear — stop, NOT a long focal length; probe Q3: a
# front-stop telephoto reads ENPP=0.0). This is a disclosed HEURISTIC HINT, not a hard
# guard — the make-it-bite (chief mis-launch) is the real proof.
_ENPP_DISPLACED_FRACTION = 0.1

# Below this |EFFL| the system is treated as afocal/degenerate (the verify_collimation
# regime) and the displacement ratio is indeterminate (a tiny EFFL is not a real lens).
_EFFL_AFOCAL_EPS = 1e-9

# The 1e10 rot-sym / overflow sentinel: a FINITE value that is NOT a real ENPP
# reading. get_first_order pre-drops it from the headline, but the helper guards it itself
# so it honors its docstring + is safe to reuse standalone (the latent-
# fabrication guard — |ENPP|>=this is indeterminate, never a fabricated recommendation).
_ENPP_SENTINEL = 1e10

# (S7 #17) The degenerate-zoom-config thresholds. A +-+ mid-zoom system can pass through
# an INTERNAL afocal/variator crossing — a config whose first-order headline is meaningless
# even though the OUTPUT still images (so detect_collimated_output does NOT fire — probe-
# confirmed). The degeneracy signal lives in the EFL SIGN-FLIP (the cross-config ±infinity
# crossing) and the ABSOLUTE pathological f/# / ENPP sentinel, NOT in any cross-config
# magnitude RATIO. The flag scans the RAW `readings` (NOT the dropped headline) so a config
# whose first-order read tripped the suspicious-sentinel drop STILL flags.
#
# WHY NO MEDIAN-RATIO OUTLIER: a `|X| >= 8x median`
# rule on a small sample with a low median CANNOT distinguish a clustered-low LEGIT extreme
# zoom from a near-afocal blow-up. The WFNO ratio false-flagged a legit non-uniform slow
# zoom ({4,4,64}); replacing it with an EFL-magnitude ratio merely MOVED the false-positive
# (a ~10x zoom {18,19,180} clusters short configs low -> median 19 -> 180 >= 8*19=152 ->
# the legit 180mm tele false-flags). The median-ratio is REDUNDANT for real degenerates and
# contributes ONLY false-positives, because:
#   - a true CROSS-config near-afocal crossing SIGN-FLIPS the EFL (±infinity) -> caught by
#     the sign-flip arm; and
#   - a true SAME-SIGN near-afocal (EFL -> infinity, no sign-flip) drives WFNO ~ EFL/EPD up
#     too, so |WFNO| >= 100 catches it.
# So the predicate is sign-flip OR |WFNO| >= 100 (abs) OR |ENPP| >= 1e10 (sentinel) OR a
# suspicious WFNO/EFFL sentinel-drop — NO magnitude ratio anywhere.
_DEGENERATE_WFNO_ABS = 100.0     # absolute degeneracy ceiling — a real lens never runs
                                 # >= f/100; HIGH to avoid false-flagging a slow (f/45) config
# reuse: _ENPP_SENTINEL (1e10), _EFFL_AFOCAL_EPS (1e-9) — already defined.

# The ONE flag appended (to the existing flags channel) when ray aiming is recommended.
_ENPP_DISPLACED_FLAG_MSG = (
    "entrance_pupil_displaced: the entrance pupil is materially displaced from the "
    "front vertex (|ENPP| > 0.1*|EFFL|) -> a far/virtual pupil; consider "
    "set_ray_aiming(real) so the wide-field off-axis chief rays aim through the true "
    "stop (a displaced stop, NOT long EFL, is the trigger)"
)


def _finite_num(value):
    """True iff ``value`` is a finite real number (rejects bool / non-number / inf / nan)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _ray_aiming_recommendation(effl, enpp, *, collimated=False):
    """Compute the far/virtual-ENPP ``ray_aiming_recommended`` flag (§2).

    Returns ``(recommended: bool, note: str|None)``. NON-fabricating + GUARDED: a
    collimated/afocal output, a non-finite / ~0 EFFL, or a non-finite / sentinel ENPP
    yields ``(False, "<indeterminate ...>")`` (never a fabricated recommendation on a
    degraded read). Otherwise ``recommended = |ENPP| > _ENPP_DISPLACED_FRACTION*|EFFL|``.
    """
    if collimated:
        return False, (
            "ray_aiming_recommended indeterminate: collimated/afocal output "
            "(ENPP/EFFL are referenced to an absent image plane)"
        )
    if not _finite_num(effl) or abs(effl) <= _EFFL_AFOCAL_EPS:
        return False, (
            "ray_aiming_recommended indeterminate: EFFL is non-finite/~0 (afocal); "
            "cannot judge entrance-pupil displacement"
        )
    if not _finite_num(enpp):
        return False, (
            "ray_aiming_recommended indeterminate: entrance-pupil position (ENPP) is "
            "non-finite/unreadable"
        )
    if abs(enpp) >= _ENPP_SENTINEL:
        return False, (
            "ray_aiming_recommended indeterminate: entrance-pupil position (ENPP) reads "
            "the 1e10 sentinel/overflow (not a real position)"
        )
    recommended = abs(enpp) > _ENPP_DISPLACED_FRACTION * abs(effl)
    return recommended, None


def _degenerate_zoom_signals(readings):
    """(degenerate: bool, reasons:[str]) for ONE config's RAW readings list (S7 #17).

    Reads the RAW ``readings`` (NOT the dropped headline) so a config so degenerate its
    WFNO/EFFL tripped the suspicious-sentinel drop STILL flags. Intra-config signals
    only (suspicious WFNO/EFFL | |WFNO| >= abs ceiling | ENPP sentinel). The cross-config
    EFL sign-flip runs in the config='all' post-pass (NO magnitude ratio).

    NOTE: a suspicious-sentinel reading's ``value`` may be the STRING sentinel (a
    ``safe_float`` non-finite marker), so the ``suspicious`` flag is the reliable signal
    for the dropped case; ``_finite_num`` guards the numeric reads.
    """
    reasons = []
    by_code = {r["operand"]: r for r in readings if isinstance(r, dict) and "operand" in r}
    for code in ("WFNO", "EFFL"):
        r = by_code.get(code)
        if isinstance(r, dict) and r.get("suspicious"):
            reasons.append(
                f"{code} suspicious (sentinel/overflow — near an afocal crossing)"
            )
    wfno = (by_code.get("WFNO") or {}).get("value")
    if _finite_num(wfno) and abs(wfno) >= _DEGENERATE_WFNO_ABS:
        reasons.append(f"WFNO blow-up (|{wfno}| >= {_DEGENERATE_WFNO_ABS})")
    enpp = (by_code.get("ENPP") or {}).get("value")
    if _finite_num(enpp) and abs(enpp) >= _ENPP_SENTINEL:
        reasons.append(f"ENPP at the {_ENPP_SENTINEL:g} sentinel")
    return (len(reasons) > 0), reasons


def _flag_cross_config_degeneracy(result):
    """On a config='all' result, add the cross-config degenerate signals (S7 #17). NEVER raises.

    The cross-config degeneracy signal lives in the EFL SIGN-FLIP (the ±infinity crossing
    near an internal afocal point), NOT in any cross-config magnitude RATIO. A median-ratio
    outlier (the dropped WFNO ratio AND its EFL-magnitude replacement) cannot tell a
    clustered-low legit extreme zoom from a near-afocal blow-up on a small sample, and is
    redundant: a true SAME-SIGN near-afocal trips |WFNO| >= 100 (intra-config) and a true
    CROSS-config crossing sign-flips the EFL (here). So this post-pass contributes ONLY the
    sign-flip arm:

    - EFL SIGN-FLIP: scan ``per_config[k]["config_headline"]`` for comparable finite
      non-sentinel ``|EFL| >= eps`` values of BOTH signs -> degenerate_zoom_efl_sign_flip:true.
    - degenerate_configs: the union of (per-config degenerate_zoom_config True — the
      intra-config |WFNO|>=100 / ENPP-sentinel / suspicious signals) and the sign-flip
      participants. A flagged config is forced to degenerate_zoom_config:True in its
      per_config entry (consistency).

    A single-config / current / int result (no per_config) is untouched.
    """
    if not isinstance(result, dict) or result.get("config_evaluated") != _cfg._ALL:
        return
    per = result.get("per_config")
    if not isinstance(per, list):
        return

    degenerate = set()

    # The comparable EFL per config (config_headline = the EFL, verified). Exclude
    # sentinel / |EFL| < eps / non-finite — these must not pollute the sign-flip.
    efl_by_idx = {}
    for entry in per:
        if not isinstance(entry, dict):
            continue
        efl = entry.get("config_headline")
        if (
            _finite_num(efl)
            and abs(efl) >= _EFFL_AFOCAL_EPS
            and abs(efl) < _ENPP_SENTINEL
        ):
            efl_by_idx[entry.get("config")] = efl

    # Sign-flip over the comparable EFL — the ONLY cross-config signal (NO median ratio).
    signs = {1 if v > 0 else -1 for v in efl_by_idx.values()}
    sign_flip = len(signs) > 1
    if sign_flip:
        degenerate.update(efl_by_idx.keys())

    # The configs flagged by a CROSS-config signal (sign-flip) — captured
    # BEFORE the union so a per-config flags note can name the cross-config reason (a config
    # flagged ONLY cross-config otherwise has no per-config flags entry saying WHY; the
    # reason would live only in the top-level warning).
    cross_flagged = set(degenerate)

    # Union the per-config intra-signals (a config already flagged degenerate_zoom_config).
    for entry in per:
        if isinstance(entry, dict) and entry.get("degenerate_zoom_config"):
            degenerate.add(entry.get("config"))

    # Consistency: any config in degenerate_configs MUST carry degenerate_zoom_config:True.
    # If it was flagged ONLY by a cross-config signal (not already intra-flagged), name the
    # cross-config reason in its per-config flags so an agent reading that entry alone sees why.
    for entry in per:
        if not isinstance(entry, dict) or entry.get("config") not in degenerate:
            continue
        already_intra = bool(entry.get("degenerate_zoom_config"))
        entry["degenerate_zoom_config"] = True
        if not already_intra and entry.get("config") in cross_flagged:
            entry_flags = entry.setdefault("flags", [])
            if isinstance(entry_flags, list):
                entry_flags.append(
                    "degenerate_zoom_config: flagged by the CROSS-config EFL sign-flip "
                    "(the EFL changes sign across configs — a near-afocal ±infinity "
                    "crossing); this config's first-order headline is meaningless. "
                    "See the top-level degenerate_zoom warning."
                )

    result["degenerate_zoom_efl_sign_flip"] = bool(sign_flip)
    result["degenerate_configs"] = sorted(
        c for c in degenerate if c is not None
    )
    if result["degenerate_configs"] or sign_flip:
        warnings = result.setdefault("warnings", [])
        if isinstance(warnings, list):
            warnings.append(
                "degenerate_zoom_config: one or more configs are near a +-+ mid-zoom "
                "afocal/variator crossing (EFL sign-flip across configs and/or a "
                "pathological |WFNO| >= 100 / ENPP sentinel) — the first-order headline "
                "is meaningless there; the system still images (NOT a collimated output, "
                "so verify_collimation/detect_collimated_output do NOT fire). Move the "
                "zoom group off the crossing."
            )


def _is_collimated_output(system):
    """True iff the SHARED detector recognizes a collimated/afocal output (§3.4).

    Thin guarded never-raise wrapper DELEGATING to the ONE shared
    ``_collimation.detect_collimated_output`` (the SAME detector ``verify_collimation``
    consumes — never a second copy, the L26 anti-drift rule). FAIL-OPEN: any throw ->
    ``False`` (the detector itself fails open, but defense in depth here too — a
    detector fault degrades to the pre-existing behavior, NEVER blocks a valid
    analysis). Returns ``bool``.
    """
    try:
        return bool(_col.detect_collimated_output(system)["collimated"])
    except Exception:  # noqa: BLE001 — a detector fault -> no quarantine (fail-open)
        return False

# (D10, fix 1) The single measurement family for an UNEXPECTED engine throw that
# escapes a handler's engine-touching body (a disconnected .MFE, a plain .NET/
# RuntimeError raised mid-read). The four spec families are measurement_param /
# analysis_empty / best_focus_unavailable / axial_color_inconsistent; an
# unexpected engine fault degrades to ``analysis_empty`` (no scalar could be
# read), keeping the envelope inside the locked family set rather than letting a
# raw throw reach the agent.
_ENGINE_ERROR_FAMILY = "analysis_empty"


def _never_raise(tool_name):
    """Wrap a measurement handler so it NEVER raises past its boundary (D10, fix 1).

    A ``ToolParamError`` (a bad-typed/out-of-range measurement param) keeps its
    intended ``measurement_param`` family (fix 3) — NOT the generic ``tool_param``
    family dispatch would otherwise assign, and never mislabeled as an engine
    fault. ANY other ``Exception`` (pythonnet maps every .NET throw onto a Python
    ``Exception`` subclass — ``errors.py`` L212/L225) is netted to a typed
    ``analysis_empty`` envelope so a disconnected engine mid-read becomes
    ``{ok:false}`` rather than a crash. ``BaseException`` (KeyboardInterrupt /
    SystemExit) is deliberately NOT caught — those propagate.
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params):
            try:
                return handler(session, params)
            except ToolParamError as exc:
                return _ac.error_envelope(tool_name, "measurement_param", str(exc))
            except Exception as exc:  # noqa: BLE001 — D10: net any engine throw
                return _ac.error_envelope(
                    tool_name, _ENGINE_ERROR_FAMILY,
                    f"{tool_name} hit an unexpected engine error: {exc}",
                )
        return _wrapped
    return _decorate

# Default ring/sample density (locked D5): the probe's stable wavefront density.
_DEFAULT_SAMP = 6
# The F and C reference wavelengths (µm) the axial-color tool resolves by VALUE
# against SystemData.Wavelengths (locked D7 — NOT a hard-coded file index; AXCL
# sign-flips on swap). The standard hydrogen F / C lines.
_WAVE_F_UM = 0.486133
_WAVE_C_UM = 0.656273
# Tolerance for matching a system wavelength to F/C by value (µm). The file values
# are exact to ~1e-6; a 1e-3 µm window tolerates a rounded entry without aliasing
# d (0.5876) onto F/C.
_WAVE_MATCH_TOL_UM = 1e-3
# AXCL<->curve agreement tolerance (mm) for the full-curve consistency assert (D7).
_AXCL_CURVE_TOL_MM = 1e-3


# --------------------------------------------------------------------------- #
# Slot maps (locked D2 — operand-defined positional slots, probe-grounded).
# --------------------------------------------------------------------------- #
def _slots(**named):
    """Build a ``{position: (Header, value)}`` slot dict from keyword positions.

    Each kwarg is ``HEADER=(<position>, <value>)``. Returns the position-keyed dict
    ``read_operand_slots`` consumes (so a value can never land in the wrong arg).
    """
    return {pos: (header, value) for header, (pos, value) in named.items()}


def _strh_slots(wave):
    # STRH: {2:"Samp", 3:"Wave", 4:"Field"} — NO surf slot (locked D2). Samp/Field
    # default 0 (engine default sampling, on-axis field 1 via the default).
    return _slots(Wave=(3, int(wave)))


def _rwre_slots(samp, wave):
    # RWRE: {2:"Samp", 3:"Wave", 4:"Hx", 5:"Hy"} (locked D2).
    return _slots(Samp=(2, int(samp)), Wave=(3, int(wave)))


def _rwce_slots(ring, wave):
    # RWCE: {2:"Ring", 3:"Wave", 4:"Hx", 5:"Hy"} — a2 Header "Ring", same density
    # role as RWRE's Samp (locked D2). The position-based read uses only the value
    # at slot 2; the "Ring" label is cosmetic self-documentation (fix 6).
    return _slots(Ring=(2, int(ring)), Wave=(3, int(wave)))


def _axcl_slots(wave1, wave2):
    # AXCL: {2:"Wave1", 3:"Wave2"} (locked D2).
    return _slots(Wave1=(2, int(wave1)), Wave2=(3, int(wave2)))


def _wave_slot(wave):
    # EFFL/WFNO/PIMH/EXPP/ENPP/TOTR: {3:"Wave"} (slot-2 unused -> 0) (locked D2).
    return _slots(Wave=(3, int(wave)))


# --------------------------------------------------------------------------- #
# (S4 analysis-coverage) DIMX / RELI slot maps + shared aggregation helpers.
# --------------------------------------------------------------------------- #
def _dimx_slots(field, wave):
    # DIMX: {2:"Field", 3:"Wave", 4:"Absolute"} — ALL Integer (S4 probe). Field=0 is
    # the engine max-over-fields headline; Field=k is per-field k. Absolute is left at 0
    # (signed %); the grader takes abs() for the acceptance magnitude.
    return _slots(Field=(2, int(field)), Wave=(3, int(wave)))


def _reli_slots(samp, wave, field):
    # RELI: {2:"Samp", 3:"Wave", 4:"Field", 5:"Pol"} (S4 probe). FIELD IS SLOT 4 (Wave is
    # slot 3) — a Field at slot 3 silently reads the on-axis fraction for every field (the
    # mock-divergence the fakes MUST redden). Samp is TOLERANT of 0 (NOT the silent-0 trap).
    return _slots(Samp=(2, int(samp)), Wave=(3, int(wave)), Field=(4, int(field)))


# --------------------------------------------------------------------------- #
# (G2 lateral color) REAY chief-ray + native LACL slot maps + the disclose note.
# --------------------------------------------------------------------------- #
def _reay_chief_slots(surf, wave, hy):
    # REAY: {2:Surf(int), 3:Wave(int), 4:Hx, 5:Hy, 6:Px, 7:Py} (probe). Chief ray:
    # Px=Py=0. Meridional: Hx=0. Every load-bearing slot is set EXPLICITLY (do not rely
    # on the reader padding 6/7) so the chief-ray contract is unambiguous.
    return _slots(Surf=(2, int(surf)), Wave=(3, int(wave)),
                  Hx=(4, 0.0), Hy=(5, float(hy)),
                  Px=(6, 0.0), Py=(7, 0.0))


def _lacl_slots():
    # LACL: {2:Minw(int), 3:Maxw(int)}; Minw=Maxw=0 => the defined min/max waves.
    return _slots(Minw=(2, 0), Maxw=(3, 0))


_LATERAL_COLOR_LACL_NOTE = (
    "native Zemax LACL uses a paraxial/whole-system reference convention and "
    "legitimately DIFFERS from the real chief-ray per-field headline (here ~3.5x); "
    "it is a cross-reference, NOT asserted to agree with max_lateral_color_um."
)


def _field_count(system):
    """Read ``SystemData.Fields.NumberOfFields`` (THROW-guarded -> 0)."""
    try:
        return int(system.SystemData.Fields.NumberOfFields)
    except Exception:  # noqa: BLE001 — an unreadable field set degrades to 0
        return 0


def _resolve_wave(system, params):
    """Resolve the OPTIONAL ``wave`` param (default 1) -> a positive int 1..NumberOfWavelengths.

    A bool / negative / 0 / non-integral / out-of-range -> ToolParamError (-> measurement_param
    via _never_raise). An integral float (2.0) is accepted (JSON round-trip).
    Validated ONCE outside the sweep (the wavelength set is config-independent).
    """
    if "wave" not in params:
        return 1
    w = params["wave"]
    if isinstance(w, bool):
        raise ToolParamError(f"wave must be an integer >= 1, not a bool ({w!r})")
    if isinstance(w, float):
        if not math.isfinite(w) or w != int(w):
            raise ToolParamError(f"wave must be an integer wavelength index, got {w!r}")
        w = int(w)
    if not isinstance(w, int):
        raise ToolParamError(
            f"wave must be an integer wavelength index, got {type(w).__name__} {w!r}"
        )
    nwave = _wave_count(system)
    if not (1 <= w <= max(nwave, 1)):
        raise ToolParamError(
            f"wave {w} out of range; valid 1..{nwave} (NumberOfWavelengths={nwave})"
        )
    return w


def _aggregate_clean(per_field, *, op):
    """Aggregate (op=max for distortion, op=min for RI) over the CLEAN per-field values.

    Excludes any reading whose ``suspicious`` is True or whose value is not a finite
    number (never max([])/min([]) over a sentinel). Returns ``(headline_or_None, all_suspicious)``:
    ``all_suspicious`` is True iff >=1 field was read but every clean filter dropped them.
    """
    clean = [e["value"] for e in per_field
             if not e["suspicious"] and _finite_num(e["value"])]
    if not clean:
        return None, bool(per_field)   # all-suspicious iff there WAS at least one field
    return op(clean), False


def _valid_reli_samp(n):
    """RELI Samp guard: a NON-NEGATIVE integer (0 = engine default; RELI is 0-tolerant).

    DISTINCT from valid_density (which rejects 0 — the RWCE silent-0 trap). RELI with
    Samp=0 returns a real ~unity-falloff value (probe), so 0 is VALID here. Rejects bool /
    negative / non-integral float / non-number. Accepts an integral float (20.0 -> 20).
    """
    if isinstance(n, bool):
        return False
    if isinstance(n, int):
        return n >= 0
    if isinstance(n, float):
        return math.isfinite(n) and n == int(n) and int(n) >= 0
    return False


# --------------------------------------------------------------------------- #
# (S2b PROFILE) BFSD best-fit-sphere departure — the asphere manufacturability
# readout. The BFSD operand reads SIX best-fit-sphere quantities (min-volume
# criterion) selected by the ``Data`` code over the radial fit zone [MinR, MaxR].
# --------------------------------------------------------------------------- #
# probe-FROZEN: Data code -> (envelope_key, units,
# role). ONLY codes 0..5 by PROVEN meaning. Codes >= 6 are undocumented (return
# finite garbage 0.00109/0.000788) and are NEVER read. A future probe that proves
# >= 6 adds a ROW here; there is no other path to a Data code. The handler iterates
# this tuple ONLY (never ``range(7)``, never a composed int).
_BFSD_DATA = (
    (0, "best_fit_curvature", "1/lens_units", "context"),
    (1, "best_fit_radius",    "lens_units",   "headline"),   # the optician's base sphere
    (2, "vertex_offset",      "lens_units",   "context"),
    (3, "max_departure",      "lens_units",   "headline"),   # depth — fabrication method
    (4, "volume_of_material", "lens_units^3", "headline"),   # material-removal effort
    (5, "slope_difference",   "lens_units/lens_units", "headline"),  # testability (null/CGH)
)


def _bfsd_slots(surf, code, minr, maxr):
    # BFSD: {2:"Surf", 3:"Data", 4:"MinR", 5:"MaxR"} (locked S2b §1.3). Surf/Data are
    # Integer slots; MinR/MaxR are the Double fit-zone radii. The named-slot dict means
    # a value can NEVER land in the wrong positional arg (a swapped MinR/MaxR / a code
    # routed into slot 4 reddens against the zone-responsive COMPUTING fake).
    return _slots(Surf=(2, int(surf)), Data=(3, int(code)),
                  MinR=(4, float(minr)), MaxR=(5, float(maxr)))


# --------------------------------------------------------------------------- #
# Param helpers.
# --------------------------------------------------------------------------- #
def _bool_param(params, key, default):
    """Pull an optional bool param; reject a non-bool (loud, never coerced)."""
    if key not in params:
        return default
    value = params[key]
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{key!r} must be a boolean, got {type(value).__name__} {value!r}"
        )
    return value


def _wave_count(system):
    """Read ``SystemData.Wavelengths.NumberOfWavelengths`` (guarded -> 0)."""
    try:
        return int(system.SystemData.Wavelengths.NumberOfWavelengths)
    except Exception:  # noqa: BLE001 — an unreadable wave set degrades to 0
        return 0


def _dedupe_warnings(warnings):
    """Order-preserving de-dup of restore ``mutation_warning`` strings (fix 5).

    ``with_best_focus`` is called once per wave; a system-wide restore fault (e.g.
    a thickness-write throw) yields the SAME warning on every wave, so a naive
    join re-bundles the degrade reason N times (a 3-wave failure -> ~970 chars).
    Collapse to each DISTINCT warning once, first-seen order preserved. ASCII
    punctuation only (no em-dash).
    """
    seen = set()
    out = []
    for w in warnings:
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _reading(code, raw, suspicious, *, units, **extra):
    """Build a uniform per-reading dict with the suspicious flag + units."""
    entry = {
        "operand": code,
        "value": safe_float(raw),
        "units": units,
        "suspicious": bool(suspicious),
    }
    entry.update(extra)
    return entry


# --------------------------------------------------------------------------- #
# (D8) get_first_order.
# --------------------------------------------------------------------------- #
# (operand, units) for the headline first-order reads. EFLX/EFLY are EXCLUDED
# (1e10 sentinel for rot-sym systems).
_FIRST_ORDER = (
    ("EFFL", "mm", "effective_focal_length"),
    ("WFNO", "dimensionless", "working_f_number"),
    ("PIMH", "mm", "paraxial_image_height"),
    ("EXPP", "mm", "exit_pupil_position"),
    ("ENPP", "mm", "entrance_pupil_position"),
    ("TOTR", "mm", "total_track"),
)


def _system_is_folded(system):
    """True iff ANY surface is a coordinate-break OR a mirror (geometry-readouts §1).

    A thin guarded never-raise wrapper that DELEGATES the actual detection to the ONE
    shared predicate ``_layout_geometry.system_is_folded`` (which scans ALL surfaces
    incl. 0 and n-1 against the raw Type/Material) — the SAME predicate
    ``check_clearance`` uses, so the fold signal can never drift between the two
    readout tools. READ-GUARDED here too (defense in depth): any throw degrades to
    ``False`` (no flag rather than a crash — the flag is honesty-only, not
    load-bearing).
    """
    try:
        return _geom.system_is_folded(system)
    except Exception:  # noqa: BLE001 — an unreadable LDE/count -> no fold flag
        return False


@_never_raise("get_first_order")
def get_first_order(session, params):
    """Read the first-order quantities via the named-slot reader (locked D8).

    EFFL/WFNO/PIMH/EXPP/ENPP/TOTR (wave 1) + BFL from the back-airgap thickness
    (labeled ``source:"back_airgap_thickness"``). EFLX/EFLY are EXCLUDED (the 1e10
    rot-sym sentinel). A reading that trips ``suspicious_sentinel`` is
    dropped from the ``headline`` map (kept in ``readings`` with its flag) and named
    in ``flags``. Never raises past the handler.

    ``config`` (None|int|"all") selects which configuration to read:
    None=current (byte-identical), an int reads at that config, ``"all"`` sweeps every
    config into a ``per_config`` vector with a coverage reconcile + ``config_differs``
    (the per-config EFL headline). A bad ``config`` -> ``measurement_param``.
    """
    config = params.get("config") if isinstance(params, dict) else None

    def _grade(sess):
        system = sess.system
        wave = 1
        readings = []
        headline = {}
        flags = []
        for code, units, name in _FIRST_ORDER:
            try:
                raw, suspicious = _mc.read_operand_slots(system, code, _wave_slot(wave))
            except ToolParamError as exc:
                # An unresolvable operand is an EXPECTED class -> flag, keep going.
                flags.append(f"{code}: {exc}")
                continue
            entry = _reading(code, raw, suspicious, units=units, name=name)
            readings.append(entry)
            if suspicious:
                flags.append(f"{code} suspicious (sentinel/overflow {entry['value']!r})")
            else:
                headline[name] = entry["value"]

        # BFL from the back-airgap thickness (locked D8 — a clean BFL operand does
        # not exist on this enum; the probe confirmed BFL is the back-airgap thickness).
        surface = _mc.resolve_back_airgap(system)
        bfl = None
        if surface is not None:
            t = _mc._read_thickness(system, surface)
            if t is not None and math.isfinite(t):
                bfl = {
                    "value": safe_float(t),
                    "units": "mm",
                    "source": "back_airgap_thickness",
                    "surface": surface,
                }
                headline["back_focal_length"] = bfl["value"]
        if bfl is None:
            # (D10 flags discipline, fix 2) Do NOT silently omit BFL: name the drop so
            # the grader's single ``if result["flags"]`` check sees it. The back-airgap
            # was unresolvable (< 3 surfaces / LDE read throw) or its thickness was
            # unreadable/non-finite.
            flags.append(
                "back_focal_length unavailable "
                "(back-airgap surface unresolved or its thickness non-finite/unreadable)"
            )

        # (geometry-readouts gap #2) On a FOLDED system the raw back-airgap thickness
        # is NOT the behind-primary clearance — name the check_clearance field to use.
        # The unfolded behaviour is UNCHANGED (no flag, BFL == back-airgap thickness).
        folded = _system_is_folded(system)
        if folded:
            flags.append(_FOLDED_BFL_FLAG)

        # (A3) The ghost-gap advisory: ONE interior air gap that is
        # > _GHOST_FRACTION of total_track AND inert (air<->air, powerless) AND
        # collimated (moving it repositions nothing downstream) is almost certainly an
        # orphaned dead-space DOF, not a physical gap. READ-ONLY (no Variable
        # requirement — this is a report). The whole pass is guarded (a ghost-scan
        # hiccup must never lose the first-order readout); RANG is read for ONLY the
        # single dominant gap.
        suspicious_ghost_gap = None
        try:
            n = int(system.LDE.NumberOfSurfaces)
            total = headline.get("total_track")
            gaps = []                       # (surface, |thickness|) for interior AIR gaps
            for i in range(1, n - 1):       # interior surfaces 1..n-2
                row = system.LDE.GetSurfaceAt(i)
                if not _sc._material_is_air(row):
                    continue
                t = _mc._read_thickness(system, i)
                if t is not None and math.isfinite(t):
                    gaps.append((i, abs(t)))
            if gaps:
                if total is None or not math.isfinite(total) or total <= 0:
                    total = sum(g for _s, g in gaps) or None
                if total:
                    K, g_max = max(gaps, key=lambda kt: kt[1])
                    if (g_max / total) > _GHOST_FRACTION \
                            and _oc._surface_is_inert(system.LDE, K) is True \
                            and _mc._gap_is_collimated(system, K) is True:
                        pct = round(100.0 * g_max / total, 1)
                        flags.append(
                            f"suspicious_ghost_gap: surface {K} thickness={g_max:g} mm "
                            f"is {pct}% of total_track ({total:g} mm), air-both-sides + "
                            "collimated dummy — likely an orphaned dead-space DOF, not a "
                            f"physical gap; freeze/reseat surface {K} (clear_variable / "
                            "set_surface)"
                        )
                        suspicious_ghost_gap = {
                            "surface": K, "thickness": g_max,
                            "totr_fraction": round(g_max / total, 4),
                        }
        except Exception:  # noqa: BLE001 — honesty-only, never crash get_first_order
            suspicious_ghost_gap = None

        # (afocal/collimation §3.2) The LIGHTER door: EFFL is a DEFINED paraxial
        # quantity (finite, not catastrophic), so it is KEPT (over-firing a defined
        # quantity to null is wrong) but FLAGGED + a note that EFFL/BFL are referenced
        # to an absent image plane. One detector call near the existing folded read.
        collimated = _is_collimated_output(system)
        if collimated:
            flags.append(_COLLIMATED_FLAG_MSG)
            flags.append(
                "EFFL/back_focal_length are referenced to an absent image plane "
                "(collimated/afocal output); grade collimation with verify_collimation"
            )

        # (§2) The far/virtual-ENPP flag: ray aiming is RECOMMENDED when the
        # entrance pupil is materially displaced from the front vertex (a DISPLACED stop,
        # NOT long EFL). NON-fabricating: a collimated/afocal/degraded read -> False + a
        # note (no recommendation). When True, append to the EXISTING flags channel.
        ray_aiming_recommended, ra_note = _ray_aiming_recommendation(
            headline.get("effective_focal_length"),
            headline.get("entrance_pupil_position"),
            collimated=collimated,
        )
        if ra_note:
            flags.append(ra_note)
        if ray_aiming_recommended:
            flags.append(_ENPP_DISPLACED_FLAG_MSG)

        # (S7 #17) The per-config degenerate-zoom flag — INTRA-config signals only
        # (suspicious WFNO/EFFL | |WFNO|>=100 | ENPP sentinel), read from the RAW
        # `readings` (NOT the dropped headline) so a config whose WFNO tripped the
        # sentinel-drop STILL flags. The cross-config EFL sign-flip runs in the
        # config='all' post-pass (_flag_cross_config_degeneracy); a single-config read
        # carries this per-config bool with a NOTE that the sign-flip needs config='all'.
        degenerate_zoom_config, _degen_reasons = _degenerate_zoom_signals(readings)
        if degenerate_zoom_config:
            flags.append(
                "degenerate_zoom_config: this config is near a +-+ mid-zoom afocal/"
                "variator crossing (the first-order headline is meaningless here; the "
                "system still images but is near an internal afocal condition — NOT a "
                "collimated output, so verify_collimation/detect_collimated_output do "
                "NOT fire). Move the zoom group off the crossing. ["
                + "; ".join(_degen_reasons) + "]"
            )
        flags.append(
            "degenerate_zoom note: the cross-config EFL sign-flip signal needs "
            "config='all' (a single-config read sees only the intra-config WFNO/ENPP "
            "signals)"
        )

        return {
            "ok": True,
            "tool": "get_first_order",
            "wave": wave,
            "folded": folded,
            "collimated_output": collimated,
            # (§2) The far/virtual-ENPP heuristic hint (displaced stop -> ray aiming).
            "ray_aiming_recommended": ray_aiming_recommended,
            # (S7 #17) The per-config degenerate-zoom bool (intra-config signals).
            "degenerate_zoom_config": degenerate_zoom_config,
            # (A3) The ghost-gap advisory ({surface,thickness,
            # totr_fraction} when flagged, else null).
            "suspicious_ghost_gap": suspicious_ghost_gap,
            # The per-config divergence signal (D3): EFL is the first-order headline.
            "config_headline": headline.get("effective_focal_length"),
            "headline": headline,
            "readings": readings,
            "back_focal_length": bfl,
            "flags": flags,
        }

    result = _cfg.evaluate_over_configs(session, config, _grade)
    # (S7 #17) Additive cross-config post-pass — only acts on a config='all' result
    # (a single-config / current / int result is untouched). NEVER raises.
    _flag_cross_config_degeneracy(result)
    return result


# --------------------------------------------------------------------------- #
# (A) analyze_strehl — dual-focus STRH per wave.
# --------------------------------------------------------------------------- #
@_never_raise("analyze_strehl")
def analyze_strehl(session, params):
    """Strehl per wave at the current image plane AND best focus (locked D4/A).

    Reports BOTH ``at_image_plane`` and ``at_best_focus`` by default; ``best_focus``
    (default True) may be False for a pure read-only current-plane call (no
    mutation). The best-focus block runs a per-wave back-airgap scan maximizing
    STRH (= minimizing RWCE) and ALWAYS restores; a restore that does not verify
    raises a loud ``mutation_warning``. Never raises past the handler.

    ``config`` (None|int|"all") selects the configuration: None=current
    (byte-identical), an int reads at that config, ``"all"`` sweeps every config into a
    ``per_config`` vector (the wave-1 image-plane STRH is the headline). The per-config
    ``with_best_focus`` scan nests INSIDE the driver's ``with_configuration`` wrap
    (config OUTER, focus INNER — Q11). A bad ``config`` -> ``measurement_param``.
    """
    config = params.get("config") if isinstance(params, dict) else None
    do_best = _bool_param(params, "best_focus", True)

    def _grade(sess):
        system = sess.system
        nwave = _wave_count(system)
        if nwave <= 0:
            return _ac.error_envelope(
                "analyze_strehl", "analysis_empty",
                "SystemData reports 0 wavelengths; no Strehl to read",
            )
        waves = list(range(1, nwave + 1))
        flags = []

        # Current image plane (read-only, no mutation).
        at_image = []
        for w in waves:
            raw, suspicious = _mc.read_operand_slots(system, "STRH", _strh_slots(w))
            entry = _reading("STRH", raw, suspicious, units="dimensionless", wave=w)
            at_image.append(entry)
            if suspicious:
                flags.append(f"STRH(image,wave={w}) suspicious")

        # The per-config headline (D3): the wave-1 image-plane STRH.
        headline_value = at_image[0]["value"] if at_image else None
        result = {
            "ok": True,
            "tool": "analyze_strehl",
            "waves": waves,
            "config_headline": headline_value,
            "at_image_plane": at_image,
            "at_best_focus": None,
            "best_focus_requested": do_best,
            "flags": flags,
        }

        # (afocal/collimation §3.1) QUARANTINE the catastrophic headline on a
        # collimated/afocal output: the image-plane STRH (0.0173) is plausible-WRONG.
        # Null the headline (key STAYS present so no consumer KeyErrors), expose the
        # raw labeled, SKIP the best-focus scan ENTIRELY (a quarantined door never
        # mutates the back-airgap — quarantine REMOVES a mutation), and flag loud.
        if _is_collimated_output(system):
            result["config_headline"] = None
            result["collimated_output"] = True
            result["verdict"] = "metric_not_applicable"
            result["raw_image_plane_reading"] = {
                "STRH": at_image,
                "meaningless_for_collimated_output": True,
            }
            result["at_best_focus"] = {
                "available": False, "reason": "collimated_output",
            }
            flags.append(_COLLIMATED_FLAG_MSG)
            return result

        if not do_best:
            return result

        at_best = []
        restore_flags = []
        for w in waves:
            # Each wave is scanned to its OWN best plane (per-wave best focus, D3) and
            # restored before the next wave (the context manager restores in finally).
            # This with_best_focus is the INNER manager (Q11) — the driver's
            # with_configuration wraps the WHOLE grade OUTER.
            with _mc.with_best_focus(
                system, wave=w, code="RWCE",
                slots_by_name=_rwce_slots(_DEFAULT_SAMP, w),
            ) as focus:
                if focus["available"]:
                    raw, suspicious = _mc.read_operand_slots(
                        system, "STRH", _strh_slots(w)
                    )
                    entry = _reading(
                        "STRH", raw, suspicious, units="dimensionless", wave=w,
                        plane_thickness=focus["plane_thickness"],
                    )
                    at_best.append(entry)
                    if suspicious:
                        flags.append(f"STRH(best,wave={w}) suspicious")
                else:
                    at_best.append({
                        "operand": "STRH", "wave": w, "value": None,
                        "units": "dimensionless", "suspicious": True,
                        "reason": focus["reason"],
                    })
                    flags.append(f"STRH(best,wave={w}): {focus['reason']}")
            if not focus["restore_verified"]:
                restore_flags.append(focus["mutation_warning"])
        result["at_best_focus"] = at_best
        if restore_flags:
            restore_flags = _dedupe_warnings(restore_flags)
            flags.extend(restore_flags)
            result["mutation_warning"] = "; ".join(restore_flags)
        return result

    return _cfg.evaluate_over_configs(session, config, _grade)


# --------------------------------------------------------------------------- #
# (B) analyze_wavefront — RWCE/RWRE per wave, dual-focus, density guard.
# --------------------------------------------------------------------------- #
@_never_raise("analyze_wavefront")
def analyze_wavefront(session, params):
    """RMS wavefront (RWCE centroid + RWRE chief) per wave, dual-focus (locked D5/D6/B).

    ``samp`` (the Gaussian-quadrature ring density, default 6) is guarded by the
    SHARED ``valid_density`` predicate: a value < 1 (incl. 0 — the silent 0.0 trap)
    -> ``measurement_param``, NEVER clamped. RWCE = RMS-to-centroid ("the RMS",
    piston+tilt removed), RWRE = RMS-to-chief (piston only); both reported + labeled
    in waves. PV is unavailable (no PVCE/PVRE operand) — reported as such, never
    fabricated from RMS. Never raises past the handler.

    ``config`` (None|int|"all") selects the configuration: None=current
    (byte-identical), an int reads at that config, ``"all"`` sweeps every config into a
    ``per_config`` vector (the on-axis wave-1 image-plane RWCE is the headline). The
    per-config ``with_best_focus`` scan nests INSIDE the driver's ``with_configuration``
    wrap (config OUTER, focus INNER — Q11). A bad ``config``/``samp`` ->
    ``measurement_param``.
    """
    config = params.get("config") if isinstance(params, dict) else None
    do_best = _bool_param(params, "best_focus", True)

    # Density guard (locked D5): the SAME valid_density predicate the executor uses.
    # Validated ONCE here (outside the sweep) so a bad samp is a single param error,
    # not N per-config errors. A bad samp raises ToolParamError -> measurement_param.
    samp = params.get("samp", _DEFAULT_SAMP)
    if not _mc.valid_density(samp):
        return _ac.error_envelope(
            "analyze_wavefront", "measurement_param",
            f"samp must be an integer >= 1 (the Gaussian-quadrature ring density; "
            f"samp < 1 returns a silent 0.0), got {samp!r}",
            samp=samp,
        )
    samp = _mc.density_as_int(samp)

    def _grade(sess):
        system = sess.system
        nwave = _wave_count(system)
        if nwave <= 0:
            return _ac.error_envelope(
                "analyze_wavefront", "analysis_empty",
                "SystemData reports 0 wavelengths; no wavefront to read",
            )
        waves = list(range(1, nwave + 1))
        flags = []

        def _pair_at_current(w):
            out = {}
            for code, slots in (
                ("RWCE", _rwce_slots(samp, w)),
                ("RWRE", _rwre_slots(samp, w)),
            ):
                raw, suspicious = _mc.read_operand_slots(system, code, slots)
                out[code] = _reading(
                    code, raw, suspicious, units="waves", wave=w, samp=samp
                )
                if suspicious:
                    flags.append(f"{code}(image,wave={w}) suspicious")
            return out

        at_image = [{"wave": w, **_pair_at_current(w)} for w in waves]

        # The per-config headline (D3): the on-axis wave-1 image-plane RWCE value.
        headline_value = None
        if at_image and isinstance(at_image[0].get("RWCE"), dict):
            headline_value = at_image[0]["RWCE"].get("value")

        result = {
            "ok": True,
            "tool": "analyze_wavefront",
            "waves": waves,
            "samp": samp,
            "config_headline": headline_value,
            "at_image_plane": at_image,
            "at_best_focus": None,
            "best_focus_requested": do_best,
            "pv": {"unavailable": "from Wavefront-Map/Zernike analysis text only"},
            "flags": flags,
        }

        # (afocal/collimation §3.1) QUARANTINE the catastrophic headline on a
        # collimated/afocal output: the image-plane RWCE (973.85 waves) / RWRE are
        # plausible-WRONG against an absent plane. Null the headline (key STAYS
        # present), expose the raw labeled, SKIP the best-focus scan ENTIRELY (no
        # mutation on a meaningless plane), and flag loud.
        if _is_collimated_output(system):
            result["config_headline"] = None
            result["collimated_output"] = True
            result["verdict"] = "metric_not_applicable"
            result["raw_image_plane_reading"] = {
                "wavefront": at_image,
                "meaningless_for_collimated_output": True,
            }
            result["at_best_focus"] = {
                "available": False, "reason": "collimated_output",
            }
            flags.append(_COLLIMATED_FLAG_MSG)
            return result

        if not do_best:
            return result

        at_best = []
        restore_flags = []
        for w in waves:
            with _mc.with_best_focus(
                system, wave=w, code="RWCE", slots_by_name=_rwce_slots(samp, w),
            ) as focus:
                entry = {"wave": w, "plane_thickness": focus["plane_thickness"]}
                if focus["available"]:
                    for code, slots in (
                        ("RWCE", _rwce_slots(samp, w)),
                        ("RWRE", _rwre_slots(samp, w)),
                    ):
                        raw, suspicious = _mc.read_operand_slots(system, code, slots)
                        entry[code] = _reading(
                            code, raw, suspicious, units="waves", wave=w, samp=samp
                        )
                        if suspicious:
                            flags.append(f"{code}(best,wave={w}) suspicious")
                else:
                    entry["reason"] = focus["reason"]
                    flags.append(f"wavefront(best,wave={w}): {focus['reason']}")
                at_best.append(entry)
            if not focus["restore_verified"]:
                restore_flags.append(focus["mutation_warning"])
        result["at_best_focus"] = at_best
        if restore_flags:
            restore_flags = _dedupe_warnings(restore_flags)
            flags.extend(restore_flags)
            result["mutation_warning"] = "; ".join(restore_flags)
        return result

    return _cfg.evaluate_over_configs(session, config, _grade)


# --------------------------------------------------------------------------- #
# (C) analyze_axial_color — AXCL scalar + (full) FocalShiftDiagram curve.
# --------------------------------------------------------------------------- #
def _resolve_fc_waves(system):
    """Resolve the F and C wavelength INDICES by matching µm values (locked D7).

    Reads each ``SystemData.Wavelengths.GetWavelength(i).Wavelength`` (µm) and
    matches the closest to 0.4861 (F) / 0.6563 (C) within ``_WAVE_MATCH_TOL_UM``.
    NOT hard-coded by file order (the file order is d,C,F and AXCL sign-flips on
    swap). Returns ``(f_index, c_index)`` or ``(None, None)`` if either is absent
    (the caller flags it).
    """
    nwave = _wave_count(system)
    if nwave <= 0:
        return None, None
    f_idx = c_idx = None
    f_best = c_best = _WAVE_MATCH_TOL_UM
    for i in range(1, nwave + 1):
        try:
            um = float(system.SystemData.Wavelengths.GetWavelength(i).Wavelength)
        except Exception:  # noqa: BLE001 — an unreadable wave is skipped, not fatal
            continue
        df = abs(um - _WAVE_F_UM)
        dc = abs(um - _WAVE_C_UM)
        if df <= f_best:
            f_best, f_idx = df, i
        if dc <= c_best:
            c_best, c_idx = dc, i
    return f_idx, c_idx


def _focal_shift_curve(system):
    """Read the FocalShiftDiagram curve (X=µm, Y=mm) via GetDataSeries(0) (locked D7).

    Opens ``AnalysisIDM.FocalShiftDiagram`` through ``New_Analysis`` + the L22
    ``_run_analysis`` reaper, reads series 0's X (1-D µm) / Y (2-D [n,1] mm) arrays,
    and returns ``(points, shift_at)`` where ``points`` is ``[[um, mm], ...]`` and
    ``shift_at(um)`` linearly interpolates the shift (mm) at a wavelength. Returns
    ``(None, None)`` on any empty/odd result (the caller degrades — the scalar AXCL
    is still returned). NEVER raises past here.
    """
    try:
        idm = _amtf._analysis_idm(system, "FocalShiftDiagram")
        analysis = system.Analyses.New_Analysis(idm)
    except Exception:  # noqa: BLE001 — unavailable analysis -> degrade (no curve)
        return None, None
    try:
        with _ac._run_analysis(analysis) as results:
            if results is None:
                return None, None
            n_series = int(getattr(results, "NumberOfDataSeries", 0) or 0)
            if n_series <= 0:
                return None, None
            ds = results.GetDataSeries(0)
            x_arr = ds.XData.Data
            y_arr = ds.YData.Data
            xs = _ac._marshal_array(x_arr)        # 1-D [n] µm
            ys = _ac._marshal_array(y_arr)        # 2-D [n, 1] mm
    except Exception:  # noqa: BLE001 — empty/odd curve -> degrade (no curve)
        return None, None
    # Flatten the [n, 1] Y into a shift list; pair with X.
    points = []
    for i in range(min(len(xs), len(ys))):
        yi = ys[i]
        shift = yi[0] if isinstance(yi, (list, tuple)) and yi else yi
        points.append([safe_float(xs[i]), safe_float(shift)])
    if not points:
        return None, None

    def shift_at(um):
        # Linear interpolation on the (monotone-in-wavelength) X grid.
        pts = points
        if um <= pts[0][0]:
            return pts[0][1]
        if um >= pts[-1][0]:
            return pts[-1][1]
        for j in range(1, len(pts)):
            x0, y0 = pts[j - 1]
            x1, y1 = pts[j]
            if x0 <= um <= x1 and x1 != x0:
                frac = (um - x0) / (x1 - x0)
                return y0 + frac * (y1 - y0)
        return pts[-1][1]

    return points, shift_at


def _single_config_selector(session, params, tool_name):
    """Resolve a SINGLE-config selector (None|int) — ``"all"`` is REJECTED (D1).

    The single-config-only tools (``analyze_axial_color``, ``get_mtf``, ``get_spot``)
    offer ``config=None|int`` but NOT the ``"all"`` sweep (axial color is a per-wave
    F-C shift; MTF/spot are the heavy-analysis class). Thin wrapper over the shared
    ``_config_common.resolve_single_config_selector`` (one contract) — extracts
    ``config`` from ``params`` and delegates with ``session.system``. Returns the int config
    to read at (the active config when ``None``), or RAISES ``ToolParamError`` (the tool's
    never-raise wrapper nets it to the tool's param family).
    """
    config = params.get("config") if isinstance(params, dict) else None
    return _cfg.resolve_single_config_selector(session.system, config, tool_name)


@_never_raise("analyze_axial_color")
def analyze_axial_color(session, params):
    """Axial (longitudinal) color: AXCL F-C focus shift (mm) + optional curve (D7/C).

    Headline ``f_minus_c_shift_mm`` = ``AXCL(wave1=F, wave2=C)`` with F/C resolved by
    matching 0.4861/0.6563 µm in ``SystemData.Wavelengths`` (NOT a hard-coded index;
    AXCL sign-flips on swap). ``full=True`` returns the 121-pt FocalShiftDiagram
    curve and asserts AXCL<->curve agreement (else ``axial_color_inconsistent``).
    ``secondary_spectrum_mm`` is a derived field. Never raises past the handler.

    ``config`` (None|int) selects the configuration — SINGLE-config only,
    ``config="all"`` is REFUSED (axial color is a per-wave F-C shift, not an obvious
    per-config headline; loop ``set_current_configuration`` for a per-all-config read).
    """
    cfg_idx = _single_config_selector(session, params, "analyze_axial_color")
    if cfg_idx is None:
        result = _analyze_axial_color_at(session, params)
        if isinstance(result, dict):
            result.setdefault(
                "config_evaluated", _cfg.safe_current_configuration(session.system)
            )
        return result
    with _cfg.with_configuration(session.system, cfg_idx) as ctx:
        result = _analyze_axial_color_at(session, params)
    if isinstance(result, dict):
        result.setdefault("config_evaluated", cfg_idx)
        if not ctx["restore_verified"]:
            result["mutation_warning"] = ctx["mutation_warning"]
        if not ctx["switched"] and ctx["mutation_warning"]:
            result.setdefault("config_switch_warning", ctx["mutation_warning"])
    return result


def _analyze_axial_color_at(session, params):
    """The pure per-config axial-color body (read at the ACTIVE config). See the wrapper."""
    system = session.system
    full = _bool_param(params, "full", False)
    flags = []

    f_idx, c_idx = _resolve_fc_waves(system)
    if f_idx is None or c_idx is None:
        return _ac.error_envelope(
            "analyze_axial_color", "analysis_empty",
            "could not resolve the F (0.4861 µm) and/or C (0.6563 µm) wavelengths in "
            "SystemData.Wavelengths by value; axial color needs both",
            f_index=f_idx, c_index=c_idx,
        )

    raw, suspicious = _mc.read_operand_slots(
        system, "AXCL", _axcl_slots(f_idx, c_idx)
    )
    if suspicious:
        flags.append("AXCL suspicious (sentinel/overflow)")

    result = {
        "ok": True,
        "tool": "analyze_axial_color",
        "f_minus_c_shift_mm": safe_float(raw),
        "units": "mm",
        "wave_slots_used": {"wave1_F": f_idx, "wave2_C": c_idx},
        "suspicious": bool(suspicious),
        "secondary_spectrum_mm": None,
        "curve": None,
        "flags": flags,
    }

    if full:
        points, shift_at = _focal_shift_curve(system)
        if points is None:
            flags.append("FocalShiftDiagram curve unavailable (returning the scalar only)")
        else:
            result["curve"] = {
                "x_units": "um", "y_units": "mm", "points": points,
            }
            # AXCL<->curve agreement: |shift(F) - shift(C)| should match |AXCL|.
            try:
                curve_fc = shift_at(_WAVE_F_UM) - shift_at(_WAVE_C_UM)
            except Exception:  # noqa: BLE001 — interp failure -> skip the assert
                curve_fc = None
            if curve_fc is not None and isinstance(raw, (int, float)):
                if not math.isclose(
                    abs(curve_fc), abs(float(raw)), rel_tol=1e-2,
                    abs_tol=_AXCL_CURVE_TOL_MM,
                ):
                    return _ac.error_envelope(
                        "analyze_axial_color", "axial_color_inconsistent",
                        f"AXCL scalar ({safe_float(raw)} mm) disagrees with the "
                        f"FocalShiftDiagram F-C span ({safe_float(curve_fc)} mm) "
                        "beyond tolerance",
                        f_minus_c_shift_mm=safe_float(raw),
                        curve_f_minus_c_mm=safe_float(curve_fc),
                        wave_slots_used={"wave1_F": f_idx, "wave2_C": c_idx},
                    )
            # Secondary spectrum: the curve's max |shift| over the band (the residual
            # span d sits near the crossing of) — derived, labeled.
            try:
                result["secondary_spectrum_mm"] = safe_float(
                    max(abs(p[1]) for p in points)
                )
            except Exception:  # noqa: BLE001 — derived field is best-effort
                result["secondary_spectrum_mm"] = None

    return result


# --------------------------------------------------------------------------- #
# (S2b PROFILE) analyze_aspheric_profile — BFSD best-fit-sphere departure.
# --------------------------------------------------------------------------- #
def _coerce_profile_surface(value):
    """Coerce the ``surface`` param to an int (S2b §1.1): reject bool / non-integral.

    A surface NUMBER index (NOT an int CELL — the L30 ``is_integral_int`` constraint
    binds only the TOL term cells, §2.3). Accepts an exact int OR an integral float
    (``3.0`` -> 3). A bool / non-integral float / non-number -> ``ToolParamError``
    (netted to ``measurement_param`` by ``_never_raise``).
    """
    if isinstance(value, bool):
        raise ToolParamError(f"surface must be an integer, not a bool ({value!r})")
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(f"surface must be an integer, got non-integral {value!r}")
    if isinstance(value, int):
        return value
    raise ToolParamError(
        f"surface must be an integer, got {type(value).__name__} {value!r}"
    )


def _read_semidiameter(row):
    """Read ``row.SemiDiameter`` as a positive finite float; ``None`` on a bad read.

    READ-GUARDED (S2b §1.6): a throw / non-finite / ``<= 0`` -> ``None`` so the caller
    falls back to the engine 0/0 auto-full-aperture (probe §A.3) and flags the source.
    """
    try:
        semi = float(row.SemiDiameter)
    except Exception:  # noqa: BLE001 — an unreadable semi -> engine auto-full-aperture
        return None
    if not math.isfinite(semi) or semi <= 0.0:
        return None
    return semi


def _resolve_fit_zone(params, row):
    """Resolve + echo the [min_radius, max_radius] fit zone (S2b §1.6).

    Default ``min_radius=0.0``; ``max_radius = row.SemiDiameter`` (read-guarded). A
    user-supplied pair is coerced to finite non-bool numbers ``>= 0`` and REQUIRES
    ``min_radius < max_radius`` (a malformed zone -> ``ToolParamError`` ->
    ``measurement_param``, reject loud never clamp). Returns the ``fit_zone`` dict
    ``{min_radius, max_radius, units, source}`` (ALWAYS echoed — interpretability).
    A bad semi falls back to the engine 0/0 full aperture with the source flagged.
    """
    have_min = "min_radius" in params
    have_max = "max_radius" in params
    if have_min or have_max:
        r0 = params.get("min_radius", 0.0)
        r1 = params.get("max_radius", None)
        for label, v in (("min_radius", r0), ("max_radius", r1)):
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ToolParamError(
                    f"{label} must be a non-negative number, got "
                    f"{type(v).__name__} {v!r}"
                )
            if not math.isfinite(v) or v < 0.0:
                raise ToolParamError(
                    f"{label} must be a finite number >= 0, got {v!r}"
                )
        # Resolve a final (min, max, source) FIRST, then run ONE well-formedness gate
        # below (fix): an early-return from the SemiDiameter fallback used to
        # SKIP the min<max guard, shipping an inverted [MinR>0, 0.0] zone with ok:true.
        if r1 is None:
            # min_radius given alone: use the SemiDiameter (or engine auto) for max.
            semi = _read_semidiameter(row)
            if semi is None:
                # The engine 0/0 auto-full-aperture (MaxR=0.0) cannot represent a
                # valid zone alongside a POSITIVE user min_radius -> REJECT loud
                # (naming the degraded SemiDiameter), never an inverted zone.
                if float(r0) > 0.0:
                    raise ToolParamError(
                        f"min_radius ({r0!r}) was given but max_radius was omitted "
                        "and the surface SemiDiameter is unreadable/non-positive, so "
                        "the fit zone cannot be resolved (the engine auto-full-"
                        "aperture max of 0.0 cannot pair with a positive min_radius); "
                        "supply an explicit max_radius"
                    )
                # min_radius == 0 -> the benign engine 0/0 auto-full-aperture.
                r1 = 0.0
                source = "engine_auto_full_aperture"
            else:
                r1 = semi
                source = "user"
        else:
            source = "user"
        # ONE well-formedness gate for EVERY user-zone path: a malformed
        # [MinR >= MaxR] zone is rejected loud (mirror valid_density: reject, never
        # clamp). The benign engine auto-full-aperture [0, 0] path is exempt (MaxR=0
        # is the auto sentinel, NOT an inverted zone) — it only fires when r0 == 0.
        if source != "engine_auto_full_aperture" and not (float(r0) < float(r1)):
            raise ToolParamError(
                f"min_radius ({r0!r}) must be < max_radius ({r1!r}); a malformed fit "
                "zone is rejected (never clamped)"
            )
        return {"min_radius": float(r0), "max_radius": float(r1),
                "units": "lens_units", "source": source}

    # Default: [0, SemiDiameter]; a bad semi -> engine 0/0 auto-full-aperture.
    semi = _read_semidiameter(row)
    if semi is None:
        return {"min_radius": 0.0, "max_radius": 0.0,
                "units": "lens_units", "source": "engine_auto_full_aperture"}
    return {"min_radius": 0.0, "max_radius": float(semi),
            "units": "lens_units", "source": "default_semidiameter"}


_RMS_DEPARTURE_NOTE = (
    "BFSD has no native RMS-departure quantity; max departure (depth) is the native "
    "form-deviation number. An RMS from a DSAG grid sampling is deferred until the "
    "sampling is probe-proven faithful (DISCLOSE-not-fabricate)."
)


@_never_raise("analyze_aspheric_profile")
def analyze_aspheric_profile(session, params):
    """Even-asphere MANUFACTURABILITY profile via the best-fit-sphere operand (S2b §1).

    Reads BFSD (the minimum-volume best-fit-sphere) on ONE EvenAspheric surface over
    the fit zone [min_radius, max_radius] (default [0, SemiDiameter], echoed): max
    departure (depth — the fabrication-method number), best-fit base-sphere radius
    (the optician's sphere), max slope difference (the interferometric-null/CGH
    testability number), volume of removed material, plus best-fit curvature + vertex
    offset as context.

    The load-bearing firewall (§1.4): BFSD is read ONLY after ``is_even_asphere(row)
    == True`` — a READABLE non-asphere (e.g. Standard) returns finite, plausible-WRONG
    numbers, so it is refused (``measurement_param``); a ``row.Type`` throw nets to
    ``analysis_empty`` (fail-CLOSED, BFSD never read). RMS departure is NOT a native
    BFSD quantity (``rms_departure_available:false``; not fabricated). Never raises.
    """
    if "surface" not in params:
        return _ac.error_envelope(
            "analyze_aspheric_profile", "measurement_param",
            "the 'surface' param (the EvenAspheric surface number) is required",
        )
    surface = _coerce_profile_surface(params["surface"])

    system = session.system
    # Surface-bounds check BEFORE GetSurfaceAt (fix): an out-of-range surface
    # is a PARAM error (-> measurement_param), consistent with the other measurement
    # tools (the _require_geometry_index idiom), NOT an engine fault (analysis_empty
    # via a GetSurfaceAt throw). 1 <= surface <= N-1 (refuse OBJECT 0; an asphere is
    # never the object surface). This is a PARAM gate; the is_even_asphere Type-throw
    # gate below stays the fail-CLOSED engine-fault path (in-range surface, bad Type).
    n = int(system.LDE.NumberOfSurfaces)
    if not (1 <= surface <= n - 1):
        raise ToolParamError(
            f"surface {surface} out of range for an aspheric-profile read; valid "
            f"1..{n - 1} (OBJECT 0 and beyond IMAGE are refused; N={n})"
        )
    # First engine-touch: resolve the row + the POSITIVE asphere gate (§1.4). A Type
    # throw raises AsphereWriteError -> netted to analysis_empty by _never_raise
    # (fail-CLOSED — BFSD is NEVER read on an unclassifiable surface).
    row = system.LDE.GetSurfaceAt(surface)
    if not _asph.is_even_asphere(row):
        try:
            type_name = str(row.Type)
        except Exception:  # noqa: BLE001 — already-positive gate path won't hit this
            type_name = "?"
        return _ac.error_envelope(
            "analyze_aspheric_profile", "measurement_param",
            f"surface {surface} is not an even asphere (Type={type_name!r}); BFSD "
            "best-fit-sphere departure is only meaningful for an aspheric surface",
            surface=surface, surface_type=type_name,
        )

    # The fit zone (resolve + echo; a malformed user zone -> measurement_param).
    fit_zone = _resolve_fit_zone(params, row)
    r0 = fit_zone["min_radius"]
    r1 = fit_zone["max_radius"]

    readings = {}
    headline = {}
    flags = []
    if fit_zone["source"] == "engine_auto_full_aperture":
        flags.append(
            "fit zone defaulted to the engine auto-full-aperture (the surface "
            "SemiDiameter was unreadable/non-positive)"
        )
    for code, key, units, role in _BFSD_DATA:
        raw, suspicious = _mc.read_operand_slots(
            system, "BFSD", _bfsd_slots(surface, code, r0, r1)
        )
        readings[key] = _reading("BFSD", raw, suspicious, units=units, data=code)
        if suspicious:
            flags.append(
                f"BFSD Data {code} ({key}) suspicious "
                f"(sentinel/overflow {readings[key]['value']!r}) — dropped from headline"
            )
        elif role == "headline":
            headline[key] = readings[key]["value"]

    return {
        "ok": True,
        "tool": "analyze_aspheric_profile",
        "surface": surface,
        "surface_type": "EvenAspheric",
        "fit_zone": fit_zone,
        "readings": readings,
        "headline": headline,
        "manufacturing_hint": {
            "fabrication_method_number": "max_departure",
            "testability_number": "slope_difference",
            "base_sphere": "best_fit_radius",
        },
        "rms_departure": None,
        "rms_departure_available": False,
        "rms_departure_note": _RMS_DEPARTURE_NOTE,
        "slope_available": True,
        "flags": flags,
    }


# --------------------------------------------------------------------------- #
# (S4 analysis-coverage) analyze_distortion — DIMX per field, MAX headline.
# --------------------------------------------------------------------------- #
# tolerance for the DIMX(Field=0) engine-max vs per-field-sweep-max cross-check.
_DIMX_FIELD0_ABS_TOL = 1e-3
_DIMX_FIELD0_REL_TOL = 1e-2


@_never_raise("analyze_distortion")
def analyze_distortion(session, params):
    """Percent distortion per field (DIMX), MAX-over-fields headline (the acceptance %).

    Sweeps fields 1..N at the headline wave reading DIMX(Field=k) (% distortion). The
    headline ``max_distortion_percent`` is the MAX over the CLEAN per-field |%| (a
    suspicious field is excluded + named in flags); ``engine_field0_percent`` is the
    engine's own DIMX(Field=0) max as a cross-check (a disagreement -> a flag, not an
    error). Grades ``distortion < X%`` directly (compare |max_distortion_percent|). Cheap
    operand reads -> config=None|int|"all" via evaluate_over_configs. Never raises.
    """
    config = params.get("config") if isinstance(params, dict) else None
    # wave is config-independent — resolve ONCE outside the sweep (a bad wave is ONE error).
    wave = _resolve_wave(session.system, params)

    def _grade(sess):
        system = sess.system
        nfield = _field_count(system)
        if nfield <= 0:
            return _ac.error_envelope(
                "analyze_distortion", "analysis_empty",
                "SystemData reports 0 fields; no distortion to read",
            )
        flags = []
        per_field = []
        for f in range(1, nfield + 1):
            raw, suspicious = _mc.read_operand_slots(system, "DIMX", _dimx_slots(f, wave))
            # Acceptance is on magnitude; store abs() so the max is the worst |%|.
            absval = abs(raw) if _finite_num(raw) else raw
            entry = _reading("DIMX", absval, suspicious, units="percent", field=f)
            per_field.append(entry)
            if suspicious:
                flags.append(f"DIMX(field={f}) suspicious (sentinel/overflow) — excluded from max")

        max_pct, all_susp = _aggregate_clean(per_field, op=max)
        if all_susp:
            flags.append("all field distortion readings were suspicious; no max headline")

        # Cross-check: the engine's own DIMX(Field=0) max-over-fields.
        raw0, susp0 = _mc.read_operand_slots(system, "DIMX", _dimx_slots(0, wave))
        field0 = abs(raw0) if _finite_num(raw0) else raw0
        engine_field0 = None if susp0 else safe_float(field0)
        if susp0:
            flags.append("DIMX(field=0, engine max) suspicious — cross-check skipped")
        elif max_pct is not None and _finite_num(field0):
            if not math.isclose(max_pct, float(field0),
                                rel_tol=_DIMX_FIELD0_REL_TOL, abs_tol=_DIMX_FIELD0_ABS_TOL):
                flags.append(
                    f"distortion_field0_disagreement: per-field sweep max ({max_pct}) "
                    f"disagrees with the engine DIMX(Field=0) ({safe_float(field0)})"
                )

        return {
            "ok": True,
            "tool": "analyze_distortion",
            "wave": wave,
            "n_fields": nfield,
            "max_distortion_percent": max_pct,        # the authoritative sweep max (|%|)
            "engine_field0_percent": engine_field0,   # the cross-check canary
            "config_headline": max_pct,               # D3 per-config divergence scalar
            "per_field": per_field,
            "headline": {"max_distortion_percent": max_pct},
            "flags": flags,
        }

    return _cfg.evaluate_over_configs(session, config, _grade)


# --------------------------------------------------------------------------- #
# (S4 analysis-coverage) analyze_relative_illumination — RELI per field, MIN headline.
# --------------------------------------------------------------------------- #
@_never_raise("analyze_relative_illumination")
def analyze_relative_illumination(session, params):
    """Relative illumination per field (RELI, 0-1 fraction), MIN-over-fields headline.

    RELI(Field=k) reads field k's relative illumination (1.0 on-axis, falling off-axis via
    cos^4 / vignetting). The acceptance is ``RI >= 50%``, so the worst (MIN) field governs ->
    headline = the min CLEAN per-field fraction (a suspicious field excluded + flagged).
    samp (default 0 = engine default) is OPTIONAL (RELI is 0-tolerant — NOT the silent-0
    trap). Cheap -> config=None|int|"all" via evaluate_over_configs. Never raises.
    """
    config = params.get("config") if isinstance(params, dict) else None
    wave = _resolve_wave(session.system, params)

    # samp guard (config-independent) — validate ONCE outside the sweep. Default 0 = engine
    # default; RELI is 0-tolerant so 0 is VALID (NOT valid_density which rejects 0).
    samp = params.get("samp", 0)
    if not _valid_reli_samp(samp):
        return _ac.error_envelope(
            "analyze_relative_illumination", "measurement_param",
            f"samp must be an integer >= 0 (the RELI pupil-integration density; 0 = engine "
            f"default), got {samp!r}",
            samp=samp,
        )
    samp = int(samp)

    def _grade(sess):
        system = sess.system
        nfield = _field_count(system)
        if nfield <= 0:
            return _ac.error_envelope(
                "analyze_relative_illumination", "analysis_empty",
                "SystemData reports 0 fields; no relative illumination to read",
            )
        flags = []
        per_field = []
        for f in range(1, nfield + 1):
            raw, suspicious = _mc.read_operand_slots(
                system, "RELI", _reli_slots(samp, wave, f)
            )
            entry = _reading("RELI", raw, suspicious, units="fraction", field=f, samp=samp)
            per_field.append(entry)
            if suspicious:
                flags.append(f"RELI(field={f}) suspicious (sentinel/overflow) — excluded from min")

        min_ri, all_susp = _aggregate_clean(per_field, op=min)
        if all_susp:
            flags.append("all field relative-illumination readings were suspicious; no min headline")

        return {
            "ok": True,
            "tool": "analyze_relative_illumination",
            "wave": wave,
            "samp": samp,
            "n_fields": nfield,
            "min_relative_illumination": min_ri,
            "config_headline": min_ri,                # D3 per-config divergence scalar
            "per_field": per_field,
            "headline": {"min_relative_illumination": min_ri},
            "flags": flags,
        }

    return _cfg.evaluate_over_configs(session, config, _grade)


# --------------------------------------------------------------------------- #
# (G2 iterative-0706) analyze_lateral_color — real chief-ray REAY(F)-REAY(C) per field.
# --------------------------------------------------------------------------- #
@_never_raise("analyze_lateral_color")
def analyze_lateral_color(session, params):
    """Lateral (transverse) color: real chief-ray REAY(F)-REAY(C) per field, in um.

    Headline max_lateral_color_um = the MAX over the CLEAN per-field |F-C| (the worst
    field governs a <= X um acceptance). The image surface is resolved INTERNALLY so
    the reading never depends on a surf default. F/C are resolved by their 0.4861/0.6563
    um values (not file order; the sign is physical). Also reports the native Zemax LACL
    as a LABELED cross-reference (a different paraxial/whole-system convention that
    legitimately differs ~3.5x — disclosed, NOT asserted to agree). config
    (None|int|'all') selects the configuration. Never raises.
    """
    config = params.get("config") if isinstance(params, dict) else None

    def _grade(sess):
        system = sess.system
        f_idx, c_idx = _resolve_fc_waves(system)
        if f_idx is None or c_idx is None:
            return _ac.error_envelope(
                "analyze_lateral_color", "analysis_empty",
                "could not resolve F (0.4861um) and/or C (0.6563um) in SystemData "
                "(wavelengths by value); lateral color needs both",
                f_index=f_idx, c_index=c_idx)

        image = _mc.image_surface_index(system)
        if image is None:
            return _ac.error_envelope(
                "analyze_lateral_color", "analysis_empty",
                "could not resolve the image surface (need >= 2 surfaces)")

        # 0 fields -> analysis_empty (§2.6). NOTE: read_field_hy is non-empty by
        # contract (a 0-field/read-throw system degrades to one on-axis field), so the
        # explicit field-count gate is what makes the stated 0-fields refusal real; the
        # `not fields` check below is a belt-and-suspenders backstop.
        if _field_count(system) <= 0:
            return _ac.error_envelope(
                "analyze_lateral_color", "analysis_empty",
                "SystemData reports 0 fields; no lateral color to read")

        fields = _mc.read_field_hy(system)           # [(index, y, hy)], never raises
        if not fields:
            return _ac.error_envelope(
                "analyze_lateral_color", "analysis_empty",
                "SystemData reports 0 fields; no lateral color to read")

        flags = []
        per_field = []
        for k, (_idx, _y, hy) in enumerate(fields, start=1):
            yF, sF = _mc.read_operand_slots(system, "REAY", _reay_chief_slots(image, f_idx, hy))
            yC, sC = _mc.read_operand_slots(system, "REAY", _reay_chief_slots(image, c_idx, hy))
            fF, fC = _finite_num(yF), _finite_num(yC)
            if fF and fC and not (sF or sC):
                lc_um = 1000.0 * (yF - yC)
                per_field.append(_reading(
                    "REAY", abs(lc_um), False, units="um",
                    field=k, signed_um=safe_float(lc_um), hy=safe_float(hy)))
            else:
                per_field.append(_reading(
                    "REAY", None, True, units="um",
                    field=k, signed_um=None, hy=safe_float(hy)))
                flags.append(f"lateral_color(field={k}) unreadable/suspicious - excluded from max")

        max_um, all_susp = _aggregate_clean(per_field, op=max)
        if all_susp:
            flags.append("all field lateral-color readings were suspicious; no max headline")

        # Native LACL: labeled cross-reference, DISCLOSE-not-assert (probe: 4.52 vs 1.27).
        lacl_raw, lacl_susp = _mc.read_operand_slots(system, "LACL", _lacl_slots())
        if _finite_num(lacl_raw) and not lacl_susp:
            native_lacl_um = safe_float(1000.0 * lacl_raw)
        else:
            native_lacl_um = None
            flags.append("native LACL reading suspicious/non-finite; reported as null "
                         "(headline is unaffected)")

        return {
            "ok": True,
            "tool": "analyze_lateral_color",
            "wave_slots_used": {"wave_F": f_idx, "wave_C": c_idx},
            "image_surface": image,
            "max_lateral_color_um": max_um,      # worst-field |F-C| um, the acceptance number
            "config_headline": max_um,           # per-config divergence scalar
            "per_field": per_field,              # each: value=|um|, signed_um, hy, field, units, suspicious
            "native_lacl_um": native_lacl_um,
            "native_lacl_note": _LATERAL_COLOR_LACL_NOTE,
            "units": "um",
            "headline": {"max_lateral_color_um": max_um},
            "flags": flags,
        }

    return _cfg.evaluate_over_configs(session, config, _grade)


# --------------------------------------------------------------------------- #
# ToolSpecs (locked D11 — param_types + agent-facing descriptions).
# --------------------------------------------------------------------------- #
GET_FIRST_ORDER_SPEC = ToolSpec(
    name="get_first_order",
    handler=get_first_order,
    required_params=(),
    # config is a UNION None|int|"all"; advertised as "number" (the dominant int type
    # — the MCP reparse shim preserves the "all" string via raw-fallback).
    param_types={"config": "number"},
    description=(
        "Read the first-order optical layout: effective focal length (EFFL, mm), "
        "working f/# (WFNO), paraxial image height, exit/entrance pupil position, "
        "total track, and back focal length (from the back-airgap thickness). "
        "Excludes EFLX/EFLY (they return a 1e10 sentinel for rotationally-symmetric "
        "systems). A suspicious reading is dropped from the headline and named in flags. "
        "config (None|int|'all') selects the multi-config configuration: None=current, "
        "int=that config, 'all'=sweep every config (per_config + config_differs)."
    ),
)

ANALYZE_STREHL_SPEC = ToolSpec(
    name="analyze_strehl",
    handler=analyze_strehl,
    required_params=(),
    param_types={"best_focus": "boolean", "config": "number"},
    description=(
        "Measure the Strehl ratio (STRH) per wavelength, reported at BOTH the current "
        "image plane AND per-wave best focus (the headline diffraction-limited number "
        "is the best-focus value). best_focus (default true) runs a back-airgap focus "
        "scan that is always restored; set false for a read-only current-plane call. "
        "Gotcha: the loaded-plane Strehl is lower than the best-focus Strehl — do not "
        "conflate them."
    ),
)

ANALYZE_WAVEFRONT_SPEC = ToolSpec(
    name="analyze_wavefront",
    handler=analyze_wavefront,
    required_params=(),
    param_types={"samp": "number", "best_focus": "boolean", "config": "number"},
    description=(
        "Measure RMS wavefront error per wavelength in waves: RWCE (RMS-to-centroid, "
        "'the RMS', piston+tilt removed) and RWRE (RMS-to-chief, piston only), at both "
        "the image plane and best focus. samp is the Gaussian-quadrature ring density "
        "(default 6); samp < 1 is REJECTED (it returns a silent 0.0), never clamped. "
        "PV is unavailable (no PV operand) — read it from a Wavefront-Map/Zernike "
        "analysis instead."
    ),
)

ANALYZE_AXIAL_COLOR_SPEC = ToolSpec(
    name="analyze_axial_color",
    handler=analyze_axial_color,
    required_params=(),
    # config is SINGLE-config only (None|int); 'all' is refused. Advertised "number".
    param_types={"full": "boolean", "config": "number"},
    description=(
        "Measure axial (longitudinal) color: the F-C focus shift in mm (AXCL), "
        "referenced to the primary (d) focus, with the F/C wavelengths resolved by "
        "their 0.4861/0.6563 µm values (not by file order — AXCL sign-flips on swap). "
        "full=true also returns the FocalShiftDiagram curve and the derived secondary "
        "spectrum, and asserts the scalar agrees with the curve."
    ),
)

ANALYZE_ASPHERIC_PROFILE_SPEC = ToolSpec(
    name="analyze_aspheric_profile",
    handler=analyze_aspheric_profile,
    required_params=("surface",),
    param_types={
        "surface": "number",       # the EvenAspheric surface (integral float OK: 3.0->3)
        "min_radius": "number",    # optional fit-zone inner radius (default 0.0)
        "max_radius": "number",    # optional fit-zone outer radius (default SemiDiameter)
    },
    description=(
        "Measure an even-asphere's MANUFACTURABILITY profile via the best-fit-sphere "
        "(minimum-volume) operand BFSD on ONE EvenAspheric surface: max departure "
        "(depth, the fabrication-method number — small -> polish a sphere + correct; "
        "large -> diamond-turn / aspheric-polish); best-fit base-sphere radius (the "
        "optician's sphere); max slope difference (the interferometric-null / CGH "
        "testability number); and volume of removed material. Reports best-fit "
        "curvature + vertex offset as context. The fit zone is [min_radius, max_radius] "
        "(default [0, SemiDiameter]) and is echoed. RMS departure is NOT a native BFSD "
        "quantity (rms_departure_available:false; not fabricated). Refuses a "
        "non-EvenAspheric surface (measurement_param). See set_asphere, check_clearance."
    ),
)

ANALYZE_DISTORTION_SPEC = ToolSpec(
    name="analyze_distortion",
    handler=analyze_distortion,
    required_params=(),
    param_types={"wave": "number", "config": "number"},
    description=(
        "Measure percent distortion (DIMX) per field at the design wavelength, with the "
        "MAX-over-fields % as the acceptance headline (max_distortion_percent — grade "
        "against e.g. < 0.1 %, compare its magnitude). Returns the per-field % vector, "
        "the worst-field max, and the engine's own field-0 max as a cross-check. A "
        "suspicious reading is excluded from the max and named in flags. wave (default 1) "
        "selects the wavelength; config (None|int|'all') selects the multi-config "
        "configuration (per_config + config_differs)."
    ),
)

ANALYZE_RELATIVE_ILLUMINATION_SPEC = ToolSpec(
    name="analyze_relative_illumination",
    handler=analyze_relative_illumination,
    required_params=(),
    param_types={"samp": "number", "wave": "number", "config": "number"},
    description=(
        "Measure relative illumination (RELI, a 0-1 fraction) per field at the design "
        "wavelength, with the MIN-over-fields (the dimmest corner) as the acceptance "
        "headline (min_relative_illumination — grade against e.g. RI >= 50 %). Returns the "
        "per-field falloff vector + the worst-field min. samp is the pupil-integration "
        "density (default 0 = engine default; 0 is a real value here, NOT the silent-0 trap). "
        "A suspicious reading is excluded from the min and named in flags. config "
        "(None|int|'all') sweeps configurations."
    ),
)

ANALYZE_LATERAL_COLOR_SPEC = ToolSpec(
    name="analyze_lateral_color",
    handler=analyze_lateral_color,
    required_params=(),
    param_types={"config": "number"},
    description=(
        "Measure LATERAL (transverse) color: the real chief-ray image-height "
        "difference REAY(F)-REAY(C) per field, in micrometers, with the MAX-over-"
        "fields |F-C| as the acceptance headline (max_lateral_color_um - grade "
        "against e.g. <= 4 um). Returns the per-field SIGNED F-C vector and the "
        "worst-field max; F/C are resolved by their 0.4861/0.6563 um values (not "
        "file order). The image surface is resolved internally. Also reports the "
        "native Zemax LACL as a labeled cross-reference (a different paraxial "
        "convention that reads ~3.5x larger - disclosed, NOT asserted to agree). "
        "config (None|int|'all') selects the configuration. See analyze_axial_color."
    ),
)

TOOL_SPECS = (
    GET_FIRST_ORDER_SPEC,
    ANALYZE_STREHL_SPEC,
    ANALYZE_WAVEFRONT_SPEC,
    ANALYZE_AXIAL_COLOR_SPEC,
    ANALYZE_ASPHERIC_PROFILE_SPEC,
    ANALYZE_DISTORTION_SPEC,                 # NEW (S4)
    ANALYZE_RELATIVE_ILLUMINATION_SPEC,      # NEW (S4)
    ANALYZE_LATERAL_COLOR_SPEC,              # NEW (G2 iterative-0706)
)
