"""tools/_beam_reach.py — the rays-reach-the-optics SUBSTRATE.

NOT dispatchable (no ``TOOL_SPECS``). The data + predicates behind ``verify_beam_path``
(``tools/beam_verify.py``) and the commit gate ``beam_reaches_span`` that the
Sprints-2/3 composers import as their rollback oracle. Pure data; NO matplotlib
import; unit-testable against fakes.

Spine = author-local / falsify-global. We never trust a clean call: we trace the
chief + marginal rays against the INDEPENDENT global frame and assert each ray
actually reaches each optic.

Two complementary signals:

1. **Survival (PRIMARY, decisive).** Per surface ``k`` a batch-ray-trace
   (``Tools.OpenBatchRayTrace`` -> ``CreateNormUnpol(1, RaysType.Real, toSurface=k)``
   -> ``AddRay`` -> ``RunAndWaitForCompletion`` -> ``ReadNextResult`` ->
   ``(success, _, errorCode, vignetteCode, ...)``). ``errorCode != 0`` (and, iff
   ``vignette_is_failure``, ``vignetteCode != 0``) marks the ray NOT surviving at
   ``k``. The batch tool is ``Close()``d in a ``finally`` EVERY iteration (L22). This
   alone governs ``all_rays_survive``.
2. **Geometric miss (COMPLEMENTARY, softer).** At each SUCCESSFULLY-reached surface
   (``errorCode == 0`` up to and including ``k`` — caveat: ``RAGX/Y/Z`` read
   ``(0,0,0)`` at/after a FAILED surface, so the position there is meaningless and
   must NOT be compared), ``miss = || ray_global_xyz - vertex_global_xyz ||``
   (``vertex = _cb_cells.read_global_matrix(...)[1:4]``). Flagged iff the surface is
   optical (``Standard``/MIRROR, NOT a coordinate-break) AND its clear aperture
   ``row.SemiDiameter`` is finite & > 0 (skip CB rows = 0 and OBJECT/IMAGE = inf) AND
   ``miss > SemiDiameter * (1 + margin) + floor``. (``SDIA`` is NOT in the live
   ``MeritOperandType`` enum — ``row.SemiDiameter`` is the source.)

A geometric miss does NOT by itself flip ``all_rays_survive`` (the heuristic is
margin/floor-tuned and must never falsely declare a healthy system broken — the
engine ``errorCode`` is the ground truth; the miss is the early warning that
precedes the hard failure). The Sprints-2/3 ``beam_reaches_span`` gate DOES count a
geometric miss (a composer must roll back a §0-style misplacement).

The enum seam is the shared ``analysis_operand._merit_operand_enum`` (for RAGX/Y/Z)
and the ``analysis_raytrace`` ``_rays_type_enum`` / ``_opd_mode_member`` seams (for
the batch trace) — the SAME fake-injection seams the production tools use.
"""
import math

from . import _cb_cells
from ..enums import _resolve_enum
from .analysis_operand import _merit_operand_enum
from .analysis_raytrace import _opd_mode_member, _rays_type_enum

# The three meridional pupil rays per field (label, Py). Px = 0 (meridional plane).
# The ``_layout_rays._PUPIL_RAYS`` convention: chief Py=0, upper +1, lower -1.
_PUPIL_RAYS = (("chief", 0.0), ("upper_marginal", 1.0), ("lower_marginal", -1.0))

# A successfully-reached surface's global position whose magnitude reaches this is a
# sentinel/collapse (the OBJECT-at-infinity -1e10, or the (0,0,0)-after-failure
# read on a degenerate row) — never compared as a real position.
_POSITION_MAGNITUDE = 1e9


def _read_fields(system):
    """Read ``[(field_index, field_y, Hy)]`` from ``SystemData.Fields``. Never raises.

    ``Hy = Y / max|Y|`` (angle/object-height normalization, the ``_layout_rays``
    convention) with the division guard ``max|Y| == 0 -> Hy = 0`` (a single on-axis
    field, or all-zero fields). Fields are 1-based via ``GetField(i)``; ``Hx`` is
    always 0 (meridional). A read failure returns a single on-axis field so the trace
    still runs.
    """
    try:
        fields = system.SystemData.Fields
        n_fields = int(fields.NumberOfFields)
        raw_y = [float(fields.GetField(i).Y) for i in range(1, n_fields + 1)]
    except BaseException:  # noqa: BLE001 — a field read failure degrades to on-axis
        return [(0, 0.0, 0.0)]
    if not raw_y:
        return [(0, 0.0, 0.0)]
    max_abs = max((abs(y) for y in raw_y), default=0.0)
    out = []
    for fi, fy in enumerate(raw_y):
        hy = 0.0 if max_abs == 0.0 else fy / max_abs
        out.append((fi, fy, hy))
    return out


def _finite_num(x):
    """Return ``float(x)`` iff ``x`` is a finite, non-bool number, else ``None``."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    xf = float(x)
    return xf if math.isfinite(xf) else None


def _position_ok(xyz):
    """True iff ``xyz`` is three finite numbers all below the sentinel magnitude.

    The (0,0,0)-after-failure read passes finiteness, so the CALLER only
    consults a position at a surface the ray successfully reached (errorCode 0). This
    guard additionally rejects the -1e10 OBJECT sentinel / any collapsed magnitude so
    a degenerate read never produces a fabricated miss.
    """
    if xyz is None or len(xyz) != 3:
        return False
    for c in xyz:
        if c is None or abs(c) >= _POSITION_MAGNITUDE:
            return False
    return True


def _read_ray_global(mfe, ragx, ragy, ragz, k, wave, hy, py):
    """Read the global ``(x, y, z)`` of one ray at surface ``k`` (9-arg). ``None`` on fail.

    ``GetOperandValue(member, k, wave, Hx=0, hy, Px=0, py, ex=0, ey=0)`` — the same
    9-arg signature ``_layout_rays`` + ``diagnose_fold`` use. Any throw or non-finite
    component yields ``None`` (the caller treats a missing position as "no geometric
    check possible at this surface", never a fabricated miss).
    """
    try:
        x = _finite_num(mfe.GetOperandValue(ragx, k, wave, 0.0, hy, 0.0, py, 0.0, 0.0))
        y = _finite_num(mfe.GetOperandValue(ragy, k, wave, 0.0, hy, 0.0, py, 0.0, 0.0))
        z = _finite_num(mfe.GetOperandValue(ragz, k, wave, 0.0, hy, 0.0, py, 0.0, 0.0))
    except BaseException:  # noqa: BLE001 — a bad read => no position, never raise
        return None
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


def _trace_one_surface(system, rays_real, opd_none, k, hx, hy, py):
    """Batch-trace ONE ray to surface ``k`` -> ``(errorCode, vignetteCode)`` or ``None``.

    The diagnose_fold idiom: open the batch tool, create a single Real ray to surface
    ``k``, add it, run, read the first result, and ALWAYS ``Close()`` in a ``finally``
    (L22). Returns ``(errorCode, vignetteCode)``; ``None`` on a trace failure (treated
    by the caller as a hard failure at ``k`` — a ray we cannot even trace did not
    survive).

    ``rays_real`` MUST be the resolved ``RaysType.Real`` **member** (NOT the enum
    class) — the live .NET ``CreateNormUnpol`` rejects the enum type and throws (the
    bug the live gate caught). The caller resolves the member once via ``_resolve_enum``
    (see ``evaluate_beam_path``) and threads it here.
    """
    try:
        rt = system.Tools.OpenBatchRayTrace()
    except BaseException:  # noqa: BLE001 — an open throw => "cannot trace" at k
        return None
    if rt is None:
        return None
    try:
        nun = rt.CreateNormUnpol(1, rays_real, k)
        nun.ClearData()
        nun.AddRay(1, hx, hy, 0.0, py, opd_none)
        rt.RunAndWaitForCompletion()
        nun.StartReadingResults()
        tup = nun.ReadNextResult()
        err = int(tup[2])
        vig = int(tup[3])
        return (err, vig)
    except BaseException:  # noqa: BLE001 — a trace throw => "cannot trace" at k
        return None
    finally:
        try:
            rt.Close()
        except BaseException:  # noqa: BLE001 — Close must never mask the result (L22)
            pass


def _surface_facts(lde, k):
    """Read ``(type_name, is_cb, semi_diameter)`` for surface ``k``. Never raises.

    ``is_cb`` is the ``_cb_cells.is_coordinate_break`` predicate (a CB is skipped from
    the geometric check). ``semi_diameter`` is ``row.SemiDiameter`` as a finite float,
    else ``None`` (inf / read failure => skip). A facts read failure returns
    ``(None, False, None)`` so the geometric check is simply skipped at ``k`` (the
    survival signal is unaffected).
    """
    try:
        row = lde.GetSurfaceAt(k)
    except BaseException:  # noqa: BLE001
        return (None, False, None)
    try:
        type_name = str(row.Type)
    except BaseException:  # noqa: BLE001
        type_name = None
    try:
        is_cb = bool(_cb_cells.is_coordinate_break(row))
    except BaseException:  # noqa: BLE001 — an unreadable type => not a confirmed CB
        is_cb = False
    try:
        sd = _finite_num(row.SemiDiameter)
    except BaseException:  # noqa: BLE001
        sd = None
    return (type_name, is_cb, sd)


def _vertex_global(system, lde, k):
    """The trusted global vertex ``(X, Y, Z)`` of surface ``k`` (or ``None``).

    ``_cb_cells.read_global_matrix(system, lde, k)`` returns
    ``(R_flat9, X, Y, Z)`` (slots ``[1:4]`` are the vertex). A read failure
    (unverifiable frame) yields ``None`` => no geometric check at ``k``.
    """
    try:
        _r, x, y, z = _cb_cells.read_global_matrix(system, lde, k)
    except BaseException:  # noqa: BLE001 — an unverifiable frame => skip the geo check
        return None
    if not _position_ok((x, y, z)):
        return None
    return (x, y, z)


def _is_optical(type_name, is_cb):
    """True iff surface ``k`` is an optical surface for the geometric check.

    Optical = NOT a coordinate-break (a CB is a frame operator, sd == 0) AND the row
    type is **Standard** (the supported scope). A MIRROR-bearing surface has type
    Standard + Material "MIRROR", so the ``"STANDARD"`` substring already covers the
    honest mirror case (matching how ``_layout_geometry`` classifies a mirror — type
    Standard, mirror is a Material fact). A Paraxial / DiffractionGrating /
    CoordinateBreak / any other special-type surface is NOT optical here, so a
    non-Standard special surface can never fabricate a geometric miss the commit gate
    counts. An unreadable ``type_name`` (None) is NOT confidently optical -> excluded
    (fail-closed: never fabricate a miss on a surface we could not classify).

    SCOPE NOTE (BY DESIGN — §3 Standard-only): the geometric/frame half
    is Standard(+MIRROR)-only on purpose. A downstream powered SPECIAL surface (e.g. a
    ``DiffractionGrating`` authored by ``set_diffraction_grating``, or a Paraxial) is NOT
    counted here, so its reach is covered by the PRIMARY batch-trace ``errorCode`` survival
    signal (a ray that genuinely cannot reach it HARD-fails), NOT by this softer geometric
    half. Broadening this predicate is a SHARED (``verify_beam_path`` +
    ``place_element``) change requiring a cross-tool regression sweep
    (trigger = a fold-onto-grating design); deliberately NOT widened here.
    """
    if is_cb:
        return False
    if not isinstance(type_name, str):
        return False
    return "STANDARD" in type_name.upper()


def _geo_miss(ray_xyz, vertex_xyz, semi_diameter, margin, floor):
    """The geometric-miss distance + flag for one (reached) surface.

    Returns ``(miss_distance, is_miss)``. ``miss = || ray - vertex ||`` (Euclidean).
    ``is_miss`` iff ``miss > semi_diameter * (1 + margin) + floor``. A healthy edge
    marginal sits AT ``semi_diameter``, so ``margin=0`` + a small ``floor`` (~1 mm)
    does not false-flag it (proven by the GREEN doublet's marginals all passing).
    """
    miss = math.sqrt(
        sum((r - v) ** 2 for r, v in zip(ray_xyz, vertex_xyz))
    )
    threshold = semi_diameter * (1.0 + margin) + floor
    return miss, (miss > threshold)


def _coerce_numeric(name, value, default):
    """Coerce an optional numeric param; reject bool / non-number / non-finite.

    Used for ``wave`` / ``margin`` / ``floor`` (the never-raise substrate keeps the
    caller's contract: a bad value falls back rather than raising — but the CALLER
    (``beam_verify``) validates strictly first; here we are defensive). ``wave`` is
    additionally floored to a positive int. Returns the coerced value.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    fv = float(value)
    if not math.isfinite(fv):
        return default
    return fv


def evaluate_beam_path(system, *, wave=1, margin=0.0, floor=1.0,
                       vignette_is_failure=False):
    """Trace the chief + marginal rays through the whole system; report reach. Never raises.

    Per FIELD (``SystemData.Fields``, 1-based) and per meridional pupil ray
    (chief/upper/lower) across surfaces ``k = 1 .. N-1``: the batch-trace survival
    signal (PRIMARY) + the geometric-miss signal (COMPLEMENTARY, only at reached
    surfaces). ``all_rays_survive`` is governed by ``errorCode`` (and vignette
    iff ``vignette_is_failure``) ONLY.

    Returns the dict documented in the spec
    (``ok``/``all_rays_survive``/``n_surfaces``/``first_failure``/``per_surface``/
    ``geometric_misses``/``flags``). A TOTAL failure returns
    ``{ok:false, error_family:"beam_path_unavailable", error, all_rays_survive:false}``.
    """
    flags = []
    try:
        # Coerce the knobs defensively (the caller validated strictly; belt-and-braces).
        margin = _coerce_numeric("margin", margin, 0.0)
        floor = _coerce_numeric("floor", floor, 1.0)
        wave_f = _coerce_numeric("wave", wave, 1.0)
        wave_i = int(wave_f) if wave_f >= 1 else 1
        vig_fail = bool(vignette_is_failure)

        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
        if n < 2:
            return {
                "ok": False,
                "error_family": "beam_path_unavailable",
                "error": f"system has {n} surface(s); need at least an object + image",
                "all_rays_survive": False,
            }

        mfe = system.MFE
        enum_type = _merit_operand_enum(system)
        ragx = getattr(enum_type, "RAGX")
        ragy = getattr(enum_type, "RAGY")
        ragz = getattr(enum_type, "RAGZ")
        # Resolve the RaysType.Real MEMBER ONCE (mirror analysis_raytrace: the live
        # .NET CreateNormUnpol REJECTS the enum CLASS and throws — the bug the live
        # gate caught. _rays_type_enum returns the enum TYPE; _resolve_enum selects the
        # .Real member). A resolution failure routes to the never-raise
        # beam_path_unavailable marker below (the outer except), never an uncaught throw.
        rays_real = _resolve_enum(_rays_type_enum(system), "Real")
        opd_none = _opd_mode_member(system, "None")

        # Multi-wave note (primary/selected wavelength only).
        try:
            n_waves = int(system.SystemData.Wavelengths.NumberOfWavelengths)
            if n_waves > 1:
                flags.append("single wavelength evaluated")
        except BaseException:  # noqa: BLE001 — a wave-count read failure is non-fatal
            pass

        fields = _read_fields(system)

        # Per-surface worst event (across all field/ray), for the per_surface table.
        per_surface_worst = {}  # k -> worst-event dict
        geometric_misses = []
        all_events = []  # every event (hard-fail OR geo-miss) for first_failure ranking
        all_survive = True
        # Cache per-surface facts (type/is_cb/sd) — they do not vary by field/ray.
        facts_cache = {}
        vertex_cache = {}

        for field_index, field_y, hy in fields:
            for ray_label, py in _PUPIL_RAYS:
                ray_failed = False  # once a ray hard-fails, later surfaces read (0,0,0)
                for k in range(1, n):
                    if k not in facts_cache:
                        facts_cache[k] = _surface_facts(lde, k)
                    type_name, is_cb, sd = facts_cache[k]

                    traced = _trace_one_surface(
                        system, rays_real, opd_none, k, 0.0, hy, py
                    )
                    if traced is None:
                        # Cannot trace to k => a hard failure (errorCode unknown).
                        err, vig = (None, None)
                        hard_fail = True
                    else:
                        err, vig = traced
                        hard_fail = (err != 0) or (vig_fail and vig != 0)

                    if hard_fail:
                        all_survive = False
                        kind = (
                            "vignette"
                            if (traced is not None and err == 0 and vig != 0)
                            else "ray_error"
                        )
                        event = {
                            "surface": k,
                            "field_index": field_index,
                            "field_y": field_y,
                            "ray": ray_label,
                            "kind": kind,
                            "error_code": err,
                            "vignette_code": vig,
                            "ray_global_xyz": None,
                            "vertex_global_xyz": None,
                            "miss_distance": None,
                        }
                        all_events.append(event)
                        _record_worst(per_surface_worst, k, type_name, is_cb, sd, event)
                        # At/after a failure the position is (0,0,0) — STOP the
                        # geometric check for this ray (do not double-count a fabricated
                        # miss at the failed surface).
                        ray_failed = True
                        break

                    # Survived at k. The geometric (reaches-but-misses) check runs ONLY
                    # on a reached optical surface with a finite, positive aperture.
                    if _is_optical(type_name, is_cb) and sd is not None and sd > 0.0:
                        ray_xyz = _read_ray_global(
                            mfe, ragx, ragy, ragz, k, wave_i, hy, py
                        )
                        if k not in vertex_cache:
                            vertex_cache[k] = _vertex_global(system, lde, k)
                        vertex_xyz = vertex_cache[k]
                        if _position_ok(ray_xyz) and vertex_xyz is not None:
                            miss, is_miss = _geo_miss(
                                ray_xyz, vertex_xyz, sd, margin, floor
                            )
                            if is_miss:
                                gm = {
                                    "surface": k,
                                    "field_index": field_index,
                                    "ray": ray_label,
                                    "miss_distance": miss,
                                    "ray_global_xyz": list(ray_xyz),
                                    "vertex_global_xyz": list(vertex_xyz),
                                }
                                geometric_misses.append(gm)
                                event = {
                                    "surface": k,
                                    "field_index": field_index,
                                    "field_y": field_y,
                                    "ray": ray_label,
                                    "kind": "geometric_miss",
                                    "error_code": err,
                                    "vignette_code": vig,
                                    "ray_global_xyz": list(ray_xyz),
                                    "vertex_global_xyz": list(vertex_xyz),
                                    "miss_distance": miss,
                                }
                                all_events.append(event)
                                _record_worst(
                                    per_surface_worst, k, type_name, is_cb, sd, event
                                )
                if ray_failed:
                    flags.append(
                        f"field {field_index + 1} (Y={field_y:g}) {ray_label}: "
                        f"a ray hard-failed and was not traced past surface {k}"
                    )

        # Record every OPTICAL surface (Standard/MIRROR, positive
        # aperture) whose global frame is UNREADABLE (``_vertex_global`` -> None). This
        # is computed WITHOUT touching ``all_rays_survive`` or the read-only verdict —
        # it is an ADVISORY list the COMMIT GATE ``beam_reaches_span(frame_required=True)``
        # consumes to fail CLOSED on a §0-style frame corruption. ``verify_beam_path`` /
        # ``evaluate_beam_path`` keep the softer fail-open geometric half (they REPORT;
        # the commit gate REFUSES). A surface already known optical-with-aperture but
        # whose ``vertex_cache`` read was None is the unreadable case.
        unreadable_optical_frames = []
        for k in range(1, n):
            if k not in facts_cache:
                facts_cache[k] = _surface_facts(lde, k)
            type_name, is_cb, sd = facts_cache[k]
            if _is_optical(type_name, is_cb) and sd is not None and sd > 0.0:
                if k not in vertex_cache:
                    vertex_cache[k] = _vertex_global(system, lde, k)
                if vertex_cache[k] is None:
                    unreadable_optical_frames.append(k)

        first_failure = _first_failure(all_events)
        per_surface = _per_surface_table(per_surface_worst)

        return {
            "ok": True,
            "all_rays_survive": all_survive,
            "n_surfaces": n,
            "first_failure": first_failure,
            "per_surface": per_surface,
            "geometric_misses": geometric_misses,
            "unreadable_optical_frames": unreadable_optical_frames,
            "flags": flags,
        }
    except BaseException as exc:  # noqa: BLE001 — total failure => error marker dict
        return {
            "ok": False,
            "error_family": "beam_path_unavailable",
            "error": f"{type(exc).__name__}: {exc}",
            "all_rays_survive": False,
        }


def _record_worst(per_surface_worst, k, type_name, is_cb, sd, event):
    """Keep the WORST event at surface ``k`` (a hard fail outranks a geometric miss)."""
    rank = 2 if event["kind"] in ("ray_error", "vignette") else 1
    cur = per_surface_worst.get(k)
    if cur is None or rank > cur["_rank"]:
        per_surface_worst[k] = {
            "_rank": rank,
            "surface": k,
            "type_name": type_name,
            "is_coordinate_break": bool(is_cb),
            "semi_diameter": sd,
            "worst": {
                "kind": event["kind"],
                "ray": event["ray"],
                "field_index": event["field_index"],
                "error_code": event["error_code"],
                "vignette_code": event["vignette_code"],
                "miss_distance": event["miss_distance"],
            },
        }


def _per_surface_table(per_surface_worst):
    """Sort the per-surface worst-event records by surface, dropping the rank helper."""
    out = []
    for k in sorted(per_surface_worst):
        rec = dict(per_surface_worst[k])
        rec.pop("_rank", None)
        out.append(rec)
    return out


def _first_failure(all_events):
    """The earliest event: a hard fail outranks a geometric miss, then by surface/field/ray.

    Ordered by (NOT-hard-fail first? no — hard fail PREFERRED), surface, field_index,
    ray order. So the decisive surf-8 ``ray_error`` is reported over an earlier surf-7
    ``geometric_miss`` only when they tie; per the spec a hard failure is PREFERRED, so
    a hard fail anywhere ranks before any geometric miss.
    """
    if not all_events:
        return None
    ray_order = {"chief": 0, "upper_marginal": 1, "lower_marginal": 2}

    def _key(ev):
        is_hard = 0 if ev["kind"] in ("ray_error", "vignette") else 1
        return (
            is_hard,  # hard failures first
            ev["surface"],
            ev["field_index"],
            ray_order.get(ev["ray"], 9),
        )

    best = min(all_events, key=_key)
    return {
        "surface": best["surface"],
        "field_index": best["field_index"],
        "field_y": best["field_y"],
        "ray": best["ray"],
        "kind": best["kind"],
        "error_code": best["error_code"],
        "vignette_code": best["vignette_code"],
        "ray_global_xyz": best["ray_global_xyz"],
        "vertex_global_xyz": best["vertex_global_xyz"],
        "miss_distance": best["miss_distance"],
    }


def beam_reaches_span(system, first_surface, last_surface, *, wave=1, margin=0.0,
                      floor=1.0, vignette_is_failure=False, frame_required=False):
    """The Sprints-2/3 COMMIT GATE: do the rays reach surfaces ``first..last``?

    A thin scope over ``evaluate_beam_path`` restricted to surfaces
    ``first_surface .. last_surface`` (inclusive). Returns
    ``{reaches:bool, first_miss:{...}|None}`` where ``reaches`` is False if ANY
    chief/marginal ray HARD-FAILS (errorCode/vignette) **OR** GEOMETRICALLY MISSES an
    optical surface in the span — here a geometric miss DOES count (a composer must
    roll back a §0-style misplacement even before the hard failure). KEEP THIS
    SIGNATURE STABLE (Sprints 2-3 import it as their rollback oracle — the
    ``frame_required`` kwarg is ADDITIVE, default-off, so existing callers are
    unaffected).

    Never raises: an evaluation failure returns ``{reaches:false, first_miss:{...}}``
    with the error marker, so a composer fails CLOSED (an unverifiable span never
    certifies as reached).

    With ``frame_required=True`` an
    UNREADABLE global frame on a positive-aperture optical surface IN SPAN (the
    ``unreadable_optical_frames`` list ``evaluate_beam_path`` now returns) counts as a
    span MISS (``first_miss.kind="frame_unreadable"``) — a §0-style frame corruption
    can no longer slip ``reaches:True`` through the geo half's fail-OPEN. The default
    (``frame_required=False``) keeps the softer behaviour, so ``verify_beam_path`` /
    ``evaluate_beam_path`` (which never pass the flag) keep their read-only fail-open
    geometric half (they REPORT the list, the commit gate REFUSES on it). Probe Q4:
    every healthy folded system reads all optical frames, so this never false-rejects.
    """
    result = evaluate_beam_path(
        system, wave=wave, margin=margin, floor=floor,
        vignette_is_failure=vignette_is_failure,
    )
    if not result.get("ok"):
        return {
            "reaches": False,
            "first_miss": {
                "surface": None,
                "kind": "beam_path_unavailable",
                "error": result.get("error"),
            },
        }

    # Normalize the span bounds defensively (a bad bound never raises).
    try:
        lo = int(first_surface)
        hi = int(last_surface)
    except BaseException:  # noqa: BLE001 — a non-int bound => fail closed
        return {
            "reaches": False,
            "first_miss": {
                "surface": None,
                "kind": "beam_path_unavailable",
                "error": f"invalid span bounds {first_surface!r}..{last_surface!r}",
            },
        }
    if lo > hi:
        lo, hi = hi, lo

    # Collect EVERY in-span event (hard fail OR geometric miss) and rank it. We rebuild
    # the events from per_surface + geometric_misses: a hard fail shows in per_surface
    # (worst kind ray_error/vignette); a geometric miss shows in geometric_misses.
    in_span_events = []
    for rec in result.get("per_surface", []):
        s = rec.get("surface")
        if s is None or not (lo <= s <= hi):
            continue
        worst = rec.get("worst", {})
        if worst.get("kind") in ("ray_error", "vignette"):
            in_span_events.append({
                "surface": s,
                "field_index": worst.get("field_index"),
                "field_y": None,
                "ray": worst.get("ray"),
                "kind": worst.get("kind"),
                "error_code": worst.get("error_code"),
                "vignette_code": worst.get("vignette_code"),
                "ray_global_xyz": None,
                "vertex_global_xyz": None,
                "miss_distance": worst.get("miss_distance"),
            })
    for gm in result.get("geometric_misses", []):
        s = gm.get("surface")
        if s is None or not (lo <= s <= hi):
            continue
        in_span_events.append({
            "surface": s,
            "field_index": gm.get("field_index"),
            "field_y": None,
            "ray": gm.get("ray"),
            "kind": "geometric_miss",
            "error_code": None,
            "vignette_code": None,
            "ray_global_xyz": gm.get("ray_global_xyz"),
            "vertex_global_xyz": gm.get("vertex_global_xyz"),
            "miss_distance": gm.get("miss_distance"),
        })

    # Commit-gate-only: with frame_required, an UNREADABLE optical-surface frame
    # IN SPAN is a span MISS (fail CLOSED). Consumed ONLY here; ``evaluate_beam_path`` /
    # ``verify_beam_path`` ignore the list for their verdict (the confinement).
    if frame_required:
        for s in result.get("unreadable_optical_frames", []):
            if s is None or not (lo <= s <= hi):
                continue
            in_span_events.append({
                "surface": s,
                "field_index": None,
                "field_y": None,
                "ray": None,
                "kind": "frame_unreadable",
                "error_code": None,
                "vignette_code": None,
                "ray_global_xyz": None,
                "vertex_global_xyz": None,
                "miss_distance": None,
            })

    if not in_span_events:
        return {"reaches": True, "first_miss": None}
    return {"reaches": False, "first_miss": _first_failure(in_span_events)}


__all__ = ["evaluate_beam_path", "beam_reaches_span"]
