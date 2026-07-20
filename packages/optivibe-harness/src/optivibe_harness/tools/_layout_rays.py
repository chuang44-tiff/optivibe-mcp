"""tools/_layout_rays.py — stateless per-field chief/marginal ray reader (NOT dispatchable).

The data feed for ``render_layout``'s ray overlay (L2). For every
field (from ``SystemData.Fields``) it traces, per pupil ray (chief ``Py=0``, upper
marginal ``Py=+1``, lower marginal ``Py=-1``, ``Px=0`` — meridional), the global
``(z, y)`` of the ray at every surface ``k = 1 .. N-1`` and returns the per-field
polylines the renderer overlays.

render-obscured-slow (FIX 2) rewrite — the BATCH-TRACE reader (was: RAGY/RAGZ via
``GetOperandValue`` per surface). For each surface ``k`` ONE ``OpenBatchRayTrace``
traces ALL ``3 * n_fields`` meridional rays to ``toSurface=k`` and reads each ray's
``(errorCode, vignetteCode, X, Y, Z)``. The batch ``(X, Y, Z)`` is the ray
intersection in surface ``k``'s LOCAL frame, so each point is transformed to the
GLOBAL frame via ``point_to_global(R_k, vertex_k, X, Y, Z)`` from
``read_global_frames`` — the same frame the drawn element vertices use.

THE BUG FIX (A1, the gap-#6 subject): a ray's polyline TRUNCATES at surface ``k``
when ``errorCode != 0`` OR ``vignetteCode != 0``. A vignetted ray (a central
obscuration / annular pupil flips ``vignetteCode`` 0->1 with ``errorCode`` staying
0) is physically BLOCKED — the old RAGY/RAGZ reader's finite gate missed it (a
blocked chief ray collapses to a finite ``0.0``), drawing a spurious leg to the
global origin on an obscured/reflective system. We do NOT reuse
``_beam_reach``'s ``vignette_is_failure=False``: the layout answers "where does the
drawn ray physically go", and a vignette stops it. Once a ray truncates at ``k`` it
does NOT resume at ``k+1`` (it died there). A surface whose global frame is
un-``ok`` (un-transformable) likewise truncates the polyline.

Time-bound (A3): an optional ``deadline`` (a ``perf_counter()`` value) is checked
ONLY between batch-tool opens (between surfaces ``k``). On overrun the reader STOPS
issuing further opens, appends a disclosure flag naming the budget, and returns the
geometry + whatever polylines were collected. HONEST caveat (stated in the flag): a
single in-flight batch open cannot be interrupted — the deadline bounds the COUNT of
stuck opens to one, not one open's duration (the hang-watchdog reality).

Robustness: the whole reader is wrapped and NEVER raises — a per-ray trace failure
truncates+flags that ray; a total failure returns
``{"fields": [], "flags": ["ray trace unavailable: ..."]}`` so the figure still
draws geometry. A multi-wavelength system appends a "primary wavelength only" flag.
A 2nd ``OpenBatchRayTrace`` returning ``None`` at any ``k`` is treated as "cannot
trace further" (every ray truncates there) — never a crash.

Output shape (UNCHANGED — ``layout_render._draw*`` reads ``p[0]=z``, ``p[1]=y``):
``{"fields": [{field_index, field_y, hy, rays: {chief, upper_marginal,
lower_marginal: [(z,y), ...]}}], "flags": [...]}``.

NO matplotlib import — pure data, unit-testable against fakes. The batch-trace
enum seams are the SHARED ``analysis_raytrace._rays_type_enum`` /
``_opd_mode_member`` + ``enums._resolve_enum`` (the SAME fake-injection seams the
production batch tools use); the global frames come from
``_layout_geometry.read_global_frames`` / ``point_to_global``.
"""
import math
from time import perf_counter

from ..enums import _resolve_enum
from . import _layout_geometry as _geom
from .analysis_raytrace import _opd_mode_member, _rays_type_enum

# The three meridional pupil rays per field (label, Py). Px = 0 (meridional plane).
_PUPIL_RAYS = (("chief", 0.0), ("upper_marginal", 1.0), ("lower_marginal", -1.0))


def _finite_num(x):
    """Return ``float(x)`` iff ``x`` is a finite, non-bool number, else ``None``."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    xf = float(x)
    return xf if math.isfinite(xf) else None


def _read_fields(system):
    """Read (Hy list, raw-Y list, flags) from ``SystemData.Fields``. Never raises.

    Returns ``([Hy...], [rawY...], flags)``. ``Hy = Y / max|Y|`` with the division
    guard ``max|Y| == 0 -> Hy = 0`` (a single on-axis field, or all-zero fields).
    Fields are 1-based via ``GetField(i)``; ``Hx`` is always 0 (meridional).

    # see _measurement_common.read_field_hy (canonical); this returns the layout shape
    # (hy, raw_y, flags) + carries the render/batch-trace caller blast radius, so it is
    # intentionally NOT consolidated (future consolidation ticket).
    """
    flags = []
    fields = system.SystemData.Fields
    n_fields = int(fields.NumberOfFields)
    raw_y = []
    for i in range(1, n_fields + 1):
        raw_y.append(float(fields.GetField(i).Y))
    max_abs = max((abs(y) for y in raw_y), default=0.0)
    if max_abs == 0.0:
        # Division guard: every field normalizes to Hy = 0 (on-axis only).
        hy = [0.0 for _ in raw_y]
    else:
        hy = [y / max_abs for y in raw_y]
    return hy, raw_y, flags


def _trace_surface_all_rays(system, rays_real, opd_none, k, ray_specs):
    """Batch-trace EVERY ray to surface ``k`` in ONE open. Never raises.

    ``ray_specs`` is an ordered list of ``(wave, hx, hy, px, py)`` 5-tuples — the
    full ray set. ONE ``OpenBatchRayTrace`` adds every ray (``AddRay(wave, hx, hy,
    px, py, opd_none)``), runs, and reads back the results. (S2: widened from the
    legacy ``(hy, py)`` 2-tuple — which hardcoded ``AddRay(1, 0.0, hy, 0.0, py,
    opd_none)`` — to full ``(wave, hx, hy, px, py)`` specs so a 2-D pupil grid AND a
    per-wave geometric check can vary ``Px``/``Hx``/``wave``; the lone layout caller
    emits ``(1, 0.0, hy, 0.0, py)`` for byte-identical behavior.) The read HONORS the success
    terminator + the arity guard (the authoritative ``analysis_raytrace`` contract):
    ``tup[0]`` (success) is the STREAM TERMINATOR (``False`` ends reading, NOT per-ray
    data) and the fixed read is EXACTLY a 15-tuple. Results bind by READ POSITION to
    ``ray_specs``. Returns a list ``[(errorCode, vignetteCode, X, Y, Z) | None]``
    aligned with ``ray_specs``; a per-ray read that yields a non-finite point is
    ``None`` for that ray (the caller truncates it). An EARLY terminator or a
    malformed-arity / non-15 row STOPS the stream — the un-returned rays stay ``None``
    (truncated, never a raw ``IndexError``, never a silently-dropped field). This is
    what stops a ``success=False`` ``(0,0,0)`` end-of-stream row being drawn as a leg
    to the global origin. The WHOLE surface returns ``None`` if the tool cannot open (a
    2nd open returns ``None``) or the trace itself throws — "cannot trace to ``k``" ->
    the caller truncates EVERY ray's polyline there.

    ``rays_real`` MUST be the resolved ``RaysType.Real`` MEMBER (NOT the enum class) —
    the live .NET ``CreateNormUnpol`` rejects the class and throws (gotcha #92/L35).
    The batch tool is ``Close()``d in a ``finally`` EVERY call (L22).
    """
    try:
        rt = system.Tools.OpenBatchRayTrace()
    except BaseException:  # noqa: BLE001 — an open throw => "cannot trace" at k
        return None
    if rt is None:
        return None
    try:
        nrays = len(ray_specs)
        nun = rt.CreateNormUnpol(nrays, rays_real, k)
        nun.ClearData()
        for wave, hx, hy, px, py in ray_specs:
            nun.AddRay(wave, hx, hy, px, py, opd_none)
        rt.RunAndWaitForCompletion()
        nun.StartReadingResults()
        out = [None] * nrays
        idx = 0
        while idx < nrays:
            try:
                tup = nun.ReadNextResult()
            except BaseException:  # noqa: BLE001 — a read throw => remaining rays None
                break
            # Arity guard: the read is EXACTLY a 15-tuple (analysis_raytrace
            # _RAY_FIELDS). A drift in EITHER direction stops the stream — remaining
            # rays stay None (truncated), never a raw IndexError, never a
            # silently-dropped field.
            if not isinstance(tup, (tuple, list)) or len(tup) != 15:
                break
            # tup[0] (success) is the STREAM TERMINATOR (False ends reading), NOT
            # per-ray data. An early terminator => the un-returned rays stay None (the
            # caller truncates them) — this stops a success=False (0,0,0) row being
            # drawn as a leg to the global origin.
            if not tup[0]:
                break
            try:
                err = int(tup[2])
                vig = int(tup[3])
                x = _finite_num(tup[4])
                y = _finite_num(tup[5])
                z = _finite_num(tup[6])
            except BaseException:  # noqa: BLE001 — a bad field read => this ray is dead
                out[idx] = None
                idx += 1
                continue
            out[idx] = None if (x is None or y is None or z is None) else (err, vig, x, y, z)
            idx += 1
        return out
    except BaseException:  # noqa: BLE001 — a trace throw => "cannot trace" at k
        return None
    finally:
        try:
            rt.Close()
        except BaseException:  # noqa: BLE001 — Close must never mask the result (L22)
            pass


def read_field_rays(system, wave=1, deadline=None):
    """Read per-field chief + upper/lower marginal ray polylines (global frame).

    Returns ``{"fields": [...], "flags": [...]}``. Each field entry is
    ``{"field_index": int, "field_y": float, "hy": float, "rays": {<label>:
    [(z,y), ...]}}`` with labels ``chief``/``upper_marginal``/``lower_marginal``
    (exactly 3 rays/field, primary wave only). ``deadline`` is an optional
    ``perf_counter()`` value (default ``None`` = no budget): it is checked ONLY
    between batch-tool opens (between surfaces ``k``); on overrun the reader STOPS,
    flags, and returns the polylines collected so far. The whole reader NEVER raises:

    - A per-ray truncation (``errorCode``/``vignetteCode != 0``, an un-``ok`` global
      frame, OR a trace failure) trims that polyline at the last good surface and the
      field is flagged (still returned, just shorter).
    - A multi-wavelength system appends ``"primary wavelength only"``.
    - A TOTAL failure returns ``{"fields": [], "flags": ["ray trace unavailable:
      ..."]}`` so the caller still draws the geometry.
    """
    flags = []
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)

        # Resolve the batch-trace enum members ONCE (the .Real MEMBER, not the class —
        # gotcha #92; OPDMode.None via getattr). A resolution failure routes to the
        # never-raise total-failure marker below (the outer except).
        rays_real = _resolve_enum(_rays_type_enum(system), "Real")
        opd_none = _opd_mode_member(system, "None")

        # The per-surface GLOBAL frames (vertex + row-major rotation). The batch X/Y/Z
        # are LOCAL; point_to_global maps each into the frame the drawn vertices use.
        global_frames = _geom.read_global_frames(lde, n)

        # Multi-wave flag (primary wavelength only in v1).
        try:
            n_waves = int(system.SystemData.Wavelengths.NumberOfWavelengths)
            if n_waves > 1:
                flags.append("primary wavelength only")
        except BaseException:  # noqa: BLE001 — a wave-count read failure is non-fatal
            pass

        hy_list, raw_y, field_flags = _read_fields(system)
        flags.extend(field_flags)
        n_fields = len(hy_list)

        # The ordered ray set: 3 rays/field. The result for surface k aligns to this
        # exact order (field-major, then chief/upper/lower).
        ray_specs = []  # (field_index, label, hy, py)
        for fi, hy in enumerate(hy_list):
            for label, py in _PUPIL_RAYS:
                ray_specs.append((fi, label, hy, py))
        # S2 5-tuple widening: the layout path is meridional primary-wave only —
        # emit (wave=1, Hx=0.0, hy, Px=0.0, py) so behavior is byte-identical to the
        # legacy (hy, py) -> AddRay(1, 0.0, hy, 0.0, py) call (T19).
        trace_specs = [(1, 0.0, hy, 0.0, py) for (_fi, _lbl, hy, py) in ray_specs]

        # Per-ray polyline + truncation flag, keyed by ray-spec index. Once a ray
        # truncates (errorCode/vignette/frame), it is DONE — no resume at k+1.
        polylines = [[] for _ in ray_specs]
        ray_done = [False for _ in ray_specs]

        budget_exhausted = False
        # k = 1 .. N-1 (skip object k=0 — its frame is the at-infinity sentinel;
        # the drawn polyline starts at k=1 (the stop) and includes the image N-1).
        for k in range(1, n):
            # A3: check the deadline ONLY between batch opens. On overrun, STOP issuing
            # further opens (a single in-flight open cannot be interrupted).
            if deadline is not None and perf_counter() >= deadline:
                budget_exhausted = True
                break
            if all(ray_done):
                # Every ray already truncated — no point opening the batch tool again.
                break

            frame = global_frames[k] if k < len(global_frames) else {"ok": False}
            frame_ok = bool(frame.get("ok"))

            results = _trace_surface_all_rays(
                system, rays_real, opd_none, k, trace_specs
            )
            for ri, _spec in enumerate(ray_specs):
                if ray_done[ri]:
                    continue
                res = None if results is None else results[ri]
                if res is None:
                    # Cannot trace this ray to k (or the whole surface failed): truncate.
                    ray_done[ri] = True
                    continue
                err, vig, lx, ly, lz = res
                if err != 0 or vig != 0:
                    # A1: a vignetted (obscuration-blocked) OR errored ray is physically
                    # stopped at k — truncate at the last good surface (the gap-#6 fix).
                    ray_done[ri] = True
                    continue
                if not frame_ok:
                    # An un-transformable global frame: never plot a raw-local point at a
                    # bogus global place — truncate here.
                    ray_done[ri] = True
                    continue
                gx, gy, gz = _geom.point_to_global(
                    frame["R"], frame["vertex"], lx, ly, lz
                )
                if not (math.isfinite(gz) and math.isfinite(gy)):
                    ray_done[ri] = True
                    continue
                # Emit (z, y) so the draw code reads p[0]=z, p[1]=y (UNCHANGED).
                polylines[ri].append((float(gz), float(gy)))

        # Assemble per-field output + flags.
        out_fields = []
        for fi in range(n_fields):
            rays = {}
            field_failed = False
            for ri, (spec_fi, label, _hy, _py) in enumerate(ray_specs):
                if spec_fi != fi:
                    continue
                rays[label] = polylines[ri]
                if ray_done[ri]:
                    field_failed = True
            if field_failed:
                # User-facing field number is 1-based to match ZOS GetField;
                # `field_index` below stays 0-based for internal indexing.
                flags.append(
                    f"field {fi + 1} (Y={raw_y[fi]:g}): a ray was truncated "
                    "(vignette/error/frame/read failure) — drawn to the last valid surface"
                )
            out_fields.append(
                {
                    "field_index": fi,
                    "field_y": raw_y[fi],
                    "hy": hy_list[fi],
                    "rays": rays,
                }
            )

        if budget_exhausted:
            flags.append(
                "ray budget exhausted (OPTIVIBE_RAY_BUDGET_S): the trace was stopped "
                "between surfaces and only partial ray polylines were drawn. NOTE: this "
                "bounds the COUNT of stuck engine traces to one, not the duration of a "
                "single in-flight trace (a single batch open cannot be interrupted)."
            )

        return {"fields": out_fields, "flags": flags}
    except BaseException as exc:  # noqa: BLE001 — total failure -> geometry-only
        return {
            "fields": [],
            "flags": [f"ray trace unavailable: {type(exc).__name__}: {exc}"],
        }
