"""tools/_zoom_array.py — the ARRAY (per-config element placement) substrate (MCE S2 §5).

NOT dispatchable (no ``TOOL_SPECS``). The array/place mode physics: relocate the SAME
element span (``first..last``) to a per-config 3-D position via the ELEMENT-COORDINATE
family — ``CADX``/``CADY`` (decenter X/Y, the operands the probe proved BITE, §1) +
the preceding airgap ``THIC`` for Z — NEVER the CB family (``CBDX``…, which stores the
cell but NOTHING bites, the worst silent-wrong). NO solve (positions are authored
directly); the FALSIFY-global step is the whole correctness story.

THE LOAD-BEARING ORACLE (§5.2, OPEN-1 — REFINED by the probe `probe_mce_s2d.py`):
the placement is falsified by the chief-ray IMAGE-PLANE landing ``REAX``/``REAY`` per
config — the REFERENCE-INDEPENDENT real-ray oracle — NOT ``RAGX``/``RAGY`` (global) and
NEVER ``GetGlobalMatrix``. The probe live-falsified that BOTH ``RAGX`` global AND
``GetGlobalMatrix`` are REFERENCE-SURFACE-DEPENDENT: a CADX decenter on a DOWNSTREAM
element (surface 3, not the global reference surface 1) leaves ``RAGX``/``GetGlobalMatrix``
at 0 at EVERY surface — yet ``REAX`` at the IMAGE moves by the authored decenter at every
surface downstream of the decentered element (the channel's actual sensor landing, the
user's multi-sensor deliverable). So the robust, reference-INDEPENDENT array falsifier is
the IMAGE-PLANE landing (``REAX`` for a decenter_x, ``REAY`` for a decenter_y); ``RAGX``
global is a SECONDARY corroborator ONLY (disclosed, explicitly NOT the gate — it is
reference-surface-dependent).

MAGNITUDE CAVEAT (probe): the image-plane shift is NOT 1:1 with the decenter — it is
scaled by the element's power/position (a surface-3 decenter of 4 -> image shift -4; a
surface-1 decenter of 4 -> image shift only -0.93 at the image). So the falsifier checks
the per-config image landings DIFFER MONOTONICALLY with the authored decenter deltas
ABOVE A FLOOR (the landing spread tracks the decenter spread, sign-consistent, magnitude
> a small absolute floor) — NOT that the image shift EQUALS the decenter. A zero/near-zero
spread across configs is the ``array_no_bite`` failure.

Every cell write is DELEGATED to the S1 ``set_config_operand`` / ``set_config_value``
handlers (INVARIANT-1 read-back-proven there, D6); this module owns NO cell-write code.
"""
import math

from ._beam_reach import beam_reaches_span
from ._measurement_common import read_operand_slots
from ._zoom_solve import _ZoomUnverified

# The array families (the failure-envelope ``error_family`` values raised via
# ``_ZoomUnverified`` so they route the SAME rollback path).
_ARRAY_UNREACHED = "array_unreached"
_ARRAY_NO_BITE = "array_no_bite"

# The minimum image-landing DIFFERS the bite gate needs: a config's REAX/REAY image
# landing must differ from config 1's by at least this (lens units) for the placement to
# count as biting. A placement that does not move the chief ray's image landing (the
# CBDX-inert / blind-config / dropped-write class) reads the same REAX every config and
# FAILS. Set above pure round-off so a degenerate (all-zero-decenter) array reddens while
# a real placement (the probe's -0.93/-4 image shifts) passes comfortably.
_ARRAY_BITE_MIN_DELTA = 0.05

# A global ray coordinate whose magnitude reaches this is a sentinel / collapse (the
# OBJECT-at-infinity sentinel, or the (0,0,0)-after-failure read) — never differenced.
_RAY_SENTINEL_MAGNITUDE = 1e9


def z_to_thic(system, gap_surface, z_abs):
    """Map a requested ABSOLUTE Z to the preceding-gap THIC value (the §5.1 contract).

    The agent supplies an absolute Z position; the tool owns the nominal-gap arithmetic
    so the agent never hand-computes a cell. v1 semantics: the requested Z IS the
    preceding gap thickness (the element sits ``z`` after the gap's start) — a direct
    THIC value. (A future absolute-track variant would offset against the cumulative
    track; the locked S2 scope is the direct preceding-gap THIC, D5/§5.1.) Returns the
    float THIC to author. Raises ``_ZoomUnverified`` on a non-finite request.
    """
    if not isinstance(z_abs, (int, float)) or isinstance(z_abs, bool) \
            or not math.isfinite(float(z_abs)):
        raise _ZoomUnverified(
            f"the requested array Z {z_abs!r} is non-finite; refusing rather than "
            "authoring a degenerate gap",
            family="zoom_param",
        )
    return float(z_abs)


def read_ray_global_x(system, surface, *, wave=1):
    """Read the chief-ray GLOBAL X (``RAGX``) at ``surface`` for the ACTIVE config.

    A SECONDARY corroborator ONLY (§5.2, probe `probe_mce_s2d.py`): the real-ray global X
    is REFERENCE-SURFACE-DEPENDENT (it reads 0 for a decenter NOT on/near the global
    reference surface, the same fragility as ``GetGlobalMatrix``) — it is NOT the array
    bite gate. The PRIMARY, reference-INDEPENDENT oracle is the IMAGE-plane landing
    (``read_image_landing_x``/``_y``). Reads via the named-slot firewall ({Surf, Wave,
    Hy, Py} — chief ray Hy=0, Py=0). Returns the float X, or ``None`` on a degraded /
    sentinel read (the caller treats None as "no real-ray reading at this config", never
    a fabricated bite).
    """
    return _read_real_ray(system, "RAGX", surface, wave)


def _image_surface_index(system):
    """The 0-based IMAGE surface index = ``NumberOfSurfaces - 1`` (read LIVE).

    Read live each call so the readers are robust to surface inserts/removes between
    configs. A throw -> ``None`` (the caller treats a None image index as a degraded
    read and yields ``None``, never a fabricated landing). REAX/REAY REQUIRE an explicit
    Surf slot — they do NOT default to the image (probe `probe_mce_s2d.py`: a Surf-absent
    read lands at the object/surface-0 where the chief X/Y is ~0 every config, a false
    ``array_no_bite``); the image landing must be read at this explicit index.
    """
    try:
        return int(system.LDE.NumberOfSurfaces) - 1
    except Exception:  # noqa: BLE001 — an unreadable count -> degraded read
        return None


def read_image_landing_x(system, *, wave=1):
    """Read the chief-ray IMAGE-plane landing X (``REAX``) for the ACTIVE config.

    The PRIMARY array bite oracle for a decenter_x + the ``per_config_image_shift``
    disclosure source (§5.2/§5.3). The reference-INDEPENDENT real-ray reading: it moves by
    the authored decenter at the image regardless of WHERE the decentered element sits in
    the stack (unlike ``RAGX``/``GetGlobalMatrix``, which read 0 for a downstream decenter,
    probe `probe_mce_s2d.py`). Reads the chief ray (Hy=0, Py=0) at the IMAGE surface
    (Surf = ``NumberOfSurfaces - 1``, read LIVE — REAX REQUIRES an explicit surface; with
    the Surf slot absent the engine reads at surface 0/object where the chief X is ~0 every
    config, a false ``array_no_bite``, probe `probe_mce_s2d.py`). Returns the float X, or
    ``None`` on a degraded / sentinel read.
    """
    image = _image_surface_index(system)
    if image is None:
        return None
    return _read_real_ray(system, "REAX", image, wave)


def read_image_landing_y(system, *, wave=1):
    """Read the chief-ray IMAGE-plane landing Y (``REAY``) for the ACTIVE config.

    The PRIMARY array bite oracle for a decenter_y (the reference-INDEPENDENT image-plane
    landing on the Y axis), the ``REAX`` sibling. Reads the chief ray (Hy=0, Py=0) at the
    IMAGE surface (Surf = ``NumberOfSurfaces - 1``, read LIVE — REAY REQUIRES an explicit
    surface, same as REAX). Returns the float Y, or ``None`` on a degraded / sentinel read.

    NOTE: this reads the CHIEF ray (Py=0), distinct from ``_zoom_solve.read_marginal_for_config``
    which reads the MARGINAL ray (Py=1) for the focus-defocus metric.
    """
    image = _image_surface_index(system)
    if image is None:
        return None
    return _read_real_ray(system, "REAY", image, wave)


def _read_real_ray(system, code, surface, wave):
    """Read RAGX/REAX/REAY via the named-slot firewall; None on a degraded read.

    ``RAGX`` is surface-keyed ({Surf}); ``REAX``/``REAY`` ALSO require an explicit Surf
    slot (the IMAGE surface — they do NOT default to the image, probe `probe_mce_s2d.py`).
    The chief ray is Hy=0, Py=0 (the meridional chief). A throw / non-finite / sentinel
    read yields None (never a fabricated reading).
    """
    slots = {3: ("Wave", int(wave)), 5: ("Hy", 0.0), 7: ("Py", 0.0)}
    if surface is not None:
        slots[2] = ("Surf", int(surface))
    try:
        raw, _suspicious = read_operand_slots(system, code, slots)
    except Exception:  # noqa: BLE001 — a read THROW -> no reading, never fabricate
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    val = float(raw)
    if not math.isfinite(val) or abs(val) >= _RAY_SENTINEL_MAGNITUDE:
        return None
    return val


def array_placement_bites(per_config_landing, authored_decenters=None):
    """The DIFFERS-check: do the per-config IMAGE landings track the decenters (§5.2)?

    The PRIMARY, reference-INDEPENDENT array oracle (probe `probe_mce_s2d.py`):
    ``per_config_landing`` is the list of per-config chief-ray IMAGE-PLANE landings
    (``REAX`` for a decenter_x, ``REAY`` for a decenter_y), ONE per config in order — NOT
    ``RAGX`` global (which is reference-surface-dependent and reads 0 for a downstream
    decenter, the mock-divergence the live gate caught). A placement BITES iff the
    per-config landing SPREAD exceeds ``_ARRAY_BITE_MIN_DELTA`` AND (when the authored
    decenter deltas are supplied) the landing differences are SIGN-CONSISTENT with the
    decenter differences (the image shift tracks the decenter MONOTONICALLY — the
    magnitude is NOT 1:1, it is scaled by the element power/position, so we check sign +
    above-floor spread, never landing == decenter).

    A no-op placement (the CBDX-inert / blind-config / dropped-write class) reads the SAME
    landing every config and FAILS (spread below floor). A placement whose image shift
    moves OPPOSITE the decenter ordering (a sign-flipped / wrong-config author) FAILS the
    monotone check.

    ``authored_decenters`` (optional) is the per-config authored decenter list (the same
    axis as ``per_config_landing``), ONE per config in order, for the sign-consistency
    check. ``None`` (or all-equal decenters) skips the monotone check and falls back to the
    spread-above-floor check alone.

    Returns ``(bites: bool, max_delta: float|None, reason: str|None)`` where ``max_delta``
    is the max |landing - config-1 landing| (the disclosed lateral image shift). A reading
    list that is all-None / too short is treated as NOT biting (fail-closed — the caller
    rolls back rather than claiming an unverifiable placement).
    """
    vals = list(per_config_landing or [])
    if not vals or vals[0] is None:
        return False, None, (
            "the config-1 chief-ray IMAGE landing is unavailable; the placement cannot be "
            "falsified — refusing rather than claiming an unverified array placement"
        )
    if len(vals) < 2:
        # A single config cannot DIFFER from itself; a 1-config array is meaningless
        # (the caller already requires >=2 configs, but fail-closed here too).
        return False, None, (
            "an array placement needs >= 2 configurations to differ; only one readable "
            "config-1 IMAGE landing was available"
        )
    base = vals[0]
    max_delta = 0.0
    have_any = False
    for v in vals[1:]:
        if v is None:
            continue
        have_any = True
        max_delta = max(max_delta, abs(v - base))
    if not have_any:
        return False, None, (
            "no config beyond config 1 had a readable chief-ray IMAGE landing; the "
            "placement cannot be falsified — refusing"
        )
    if max_delta < _ARRAY_BITE_MIN_DELTA:
        return False, max_delta, (
            f"the placement is INERT: the chief-ray IMAGE landing did not move across "
            f"configs (max landing spread {max_delta} < {_ARRAY_BITE_MIN_DELTA}); the "
            "per-config decenter did not bite at the image (the CBDX-inert / blind-config "
            "/ dropped-write class) — refusing rather than claiming an unverified array "
            "placement"
        )

    # The MONOTONE-above-floor check (probe magnitude caveat): when the authored decenters
    # are supplied AND themselves vary, the image landing differences must be SIGN-CONSISTENT
    # with the decenter differences (the image shift tracks the decenter monotonically — a
    # wrong-config / sign-flipped author moves the image the WRONG way). The magnitude is
    # NOT checked (it is power/position-scaled, never 1:1).
    monotone_ok, monotone_reason = _landings_track_decenters(vals, authored_decenters)
    if not monotone_ok:
        return False, max_delta, monotone_reason
    return True, max_delta, None


def _landings_track_decenters(landings, authored_decenters):
    """Are the per-config image-landing diffs CONSISTENTLY proportional to the decenter diffs?

    The probe magnitude caveat: the image shift is power/position-scaled (NOT 1:1 with the
    decenter) AND the SENSE is fixed by the optics (a +decenter shifts the chief ray to a
    landing of the SAME sign for every config — e.g. REAX = -scale*decenter, so a positive
    decenter always lands negative). So we do NOT require the landing-shift sign to MATCH
    the decenter-shift sign; we require the RELATIONSHIP to be CONSISTENT across configs —
    every meaningful per-config (landing change / decenter change) ratio shares the SAME
    sign. A wrong-config / sign-flipped author makes ONE config's image move the opposite
    way relative to the others (an inconsistent sense) and FAILS; a config whose image did
    NOT move while its decenter DID also FAILS (a dropped/inert write). Returns
    ``(ok, reason|None)``.

    ``authored_decenters=None`` or all-equal decenters -> the monotone check is SKIPPED
    (the spread-above-floor check alone is decisive there) -> ``(True, None)``.
    """
    if not authored_decenters or len(authored_decenters) != len(landings):
        return True, None
    decs = [d if isinstance(d, (int, float)) and not isinstance(d, bool) else None
            for d in authored_decenters]
    if decs[0] is None or landings[0] is None:
        return True, None
    dec_base = float(decs[0])
    land_base = float(landings[0])
    # If the decenters do not vary, there is no ordering to track (skip — spread decides).
    varying = [d for d in decs[1:] if d is not None
               and abs(float(d) - dec_base) >= _ARRAY_BITE_MIN_DELTA]
    if not varying:
        return True, None
    sense = 0  # the established sign of (dland / ddec); 0 until the first meaningful config
    for k in range(1, len(landings)):
        lk = landings[k]
        dk = decs[k]
        if lk is None or dk is None:
            continue
        ddec = float(dk) - dec_base
        if abs(ddec) < _ARRAY_BITE_MIN_DELTA:
            continue  # this config's decenter ~ config 1's; no ordering to check here
        dland = float(lk) - land_base
        if abs(dland) < _ARRAY_BITE_MIN_DELTA:
            return False, (
                f"the array placement is INERT at config {k + 1}: its decenter changed "
                f"({ddec:+g}) but its image landing did NOT move ({dland:+g}) — a dropped "
                "/ no-op config write. Refusing rather than claiming a placement the image "
                "landing does not corroborate."
            )
        this_sense = 1 if (dland / ddec) > 0.0 else -1
        if sense == 0:
            sense = this_sense
        elif this_sense != sense:
            return False, (
                f"the array placement is NOT MONOTONE: config {k + 1}'s image landing "
                f"shift ({dland:+g}) is INCONSISTENT in sense with the other configs "
                f"relative to its decenter shift ({ddec:+g}) — a wrong-config / "
                "sign-flipped / swapped author. Refusing rather than claiming a placement "
                "the image landing does not corroborate."
            )
    return True, None


def reach_span_all_configs(system, first_surface):
    """The reach commit gate: do the rays reach the placed span on the ACTIVE config?

    Delegates to ``_beam_reach.beam_reaches_span`` over ``first-1 .. IMAGE`` (the
    ``place_element`` §0 precedent). The caller switches the active config FIRST and calls
    this per config. Returns the ``beam_reaches_span`` verdict dict
    (``{reaches, first_miss}``); a non-reaching verdict -> the caller raises
    ``_ZoomUnverified(array_unreached)`` + rollback. NEVER raises.
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
        image_index = n - 1
        start = max(0, first_surface - 1)
        return beam_reaches_span(system, start, image_index)
    except Exception as exc:  # noqa: BLE001 — fail closed: an unverifiable span never reaches
        return {
            "reaches": False,
            "first_miss": {"surface": None, "kind": "beam_path_unavailable",
                           "error": f"{type(exc).__name__}: {exc}"},
        }


__all__ = [
    "z_to_thic",
    "read_ray_global_x",
    "read_image_landing_x",
    "read_image_landing_y",
    "array_placement_bites",
    "reach_span_all_configs",
    "_ARRAY_UNREACHED",
    "_ARRAY_NO_BITE",
]
