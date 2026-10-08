"""tools/_layout_register.py — the PURE, prescription-side half of the native-layout retool.

Extent, fit, canvas choice and the shared far-object rule (native-layout-retool locked
spec). Everything here is computed from the PRESCRIPTION alone: no engine,
no matplotlib, no numpy at import time, and nothing imported from ``layout_render``
(the renderer imports this module, never the reverse).

The far-object rule is SHARED (owner ruling A6): whichever renderer draws the bytes,
the same ``far_object_decision`` says which far end is left out of the frame, and
every exclusion carries its own numbers so a wrong cut is auditable after the fact.

Constant tags: ``[measured: ...]`` = a probe number decides it; ``[unmeasured
choice]`` = a design choice with its reason, a candidate for the live gate or a ticket,
never a fact. The far-object basis is THIN (two synthetic specks of one Cooke) and is
ticketed.
"""
import math
from dataclasses import dataclass, field

from . import _layout_geometry as _geom

# --- registration model B, frozen on D1 before the held-out runs ---
FIT_MARGIN = 0.90       # [measured: Q2 rule, "~5 % margin on each side"]
FIT_A = 1.5             # [measured: Q2 model B, fitted on D1, held-out max 0.70 px]
FIT_DX = -1.0           # [measured: Q2 model B]
REG_TOL_PX = 2.0        # [measured basis: EDGE termini (own-semi curve ends, +y AND -y) sit
                        #  within 0.72 / 0.73 / 0.75 / 1.38 px of vendor ink on Cooke / DGauss /
                        # RC / Zoom (68 termini, offline over the reg2d captures);
                        #  2.0 = 1.45x the worst edge residual, below the probe's +/-3 bar]
INK_SEARCH_PX = 6       # [unmeasured choice: > REG_TOL_PX; a miss beyond it is "no ink"]
MIN_TERMINUS_SEPARATION_PX = 3 * REG_TOL_PX   # 6.0 — a terminus is DISTINCT iff every OTHER
                        # stamped surface's PREDICTED curve is >= this far away   [derived choice]
MIN_VISIBLE_PX = 2.0    # [measured: Q2 D2a "interior vertices unresolvable (< 2 px apart)"]
AMBIGUITY_CEILING = 0.5  # [unmeasured choice: more than half the leader-bearing surfaces
                         #  ambiguous -> the frame is not a stamped picture]
# --- curve-INTERIOR registration (round 2, R-1/R-2) ---
PROFILE_FRACS = (0.5, 0.8)   # [measured basis: the heights, as fractions of the surface's
                             #  OWN semi, sampled on the CHOSEN side. 0.5/0.8 over 4 designs
                             #  (Cooke/DGauss/RC/Zoom, 37 drawn-profile surfaces, rays on AND
                             #  off): worst residual 0.76 / 0.80 px.
                             #  The VERTEX is NOT sampled: it missed on the RC primary's
                             #  central hole (a measured false refuse) and adds nothing for a
                             #  cemented join, whose 0.5/0.8 samples lie on its own curve]
PROFILE_TOL_PX = REG_TOL_PX  # [measured basis: worst interior residual 0.80 px <= 1.4 px, so
                             #  the SAME 1.45x rule as REG_TOL_PX gives <= 2.0 -- shared]
# --- pixel classes (probe classify_pixels; Q5: 0 pixels outside these three on 108 PNGs) ---
BG_MIN = 245
DARK_MAX = 110
DARK_CHROMA_MAX = 30
COLOUR_CHROMA_MIN = 60   # [all four measured]
# --- canvas (Axis 16) ---
CANVAS_W = 1600                          # [unmeasured choice; inside the 600..2000 tested family]
CANVAS_H_MIN, CANVAS_H_MAX = 400, 1600   # [unmeasured choice; 400 is the smallest tested height]
CANVAS_H_SLACK = 1.25                    # [unmeasured choice; must exceed the paraxial-arrowhead ratio 3/46.4 = 6.5 %]
# --- far-object rule ---
FILL_TRIGGER = 0.08                      # [measured basis THIN: specks 0.026 / 0.011; smallest readable 0.12; marginal 0.15]
K_GAP = 40.0                             # [measured basis THIN: specks at 55.7 / 111.3; largest readable 32.9]
FAR_REFERENCE_CANVAS = (1200, 800)       # [measured: the canvas every Q3 fill was computed at]


@dataclass(frozen=True)
class Extent:
    """The drawn z/y extent of surfaces ``start..end`` (mm; global frame when folded)."""

    zmin: float
    zmax: float
    ymin: float
    ymax: float
    start: int
    end: int


@dataclass(frozen=True)
class Registration:
    """Model B: the lens-mm -> pixel map of a W x H export fitted to an extent."""

    W: int
    H: int
    s: float
    zc: float
    yc: float
    height_limited: bool

    def to_px(self, z, y):
        # [measured: Q2 model B, global-frame form]
        return (self.W / 2.0 + FIT_DX + self.s * (z - self.zc),
                self.H / 2.0 - self.s * (y - self.yc))

    def data_limits(self):
        """``(xlim_lo, xlim_hi, ylim_lo, ylim_hi)`` in PIXEL data units, pixel-EDGE aligned.

        The overlay axes' data space IS pixel space (y inverted so +y is up on the
        raster): display x in [0, W] <-> px in [-0.5, W - 0.5].
        """
        return (-0.5, self.W - 0.5, self.H - 0.5, -0.5)


@dataclass(frozen=True)
class SurfacePoint:
    """One surface's vertex and its two edge points at +/- its draw height (mm)."""

    surface: int
    z: float
    y: float
    z_hi: float
    y_hi: float
    z_lo: float
    y_lo: float


def _finite(*values):
    return all(isinstance(v, (int, float)) and math.isfinite(v) for v in values)


def surface_points(rows, n, draw_heights, *, frames=None, sag_fn):
    """Vertex + both edge points at +/-``draw_heights[i]`` for every surface ``0..n-1``.

    ``sag_fn(rows, i, h) -> (sag_hi, sag_lo, valid, h_used)`` (``layout_render.
    _edge_sag_checked``): a point is built ONLY from a VALID sample taken AT the
    requested height — never from ``_edge_sag``'s finite fallbacks. ``frames=None`` ->
    unfolded vertex z from ``_geom.vertex_z``; ``frames`` given -> GLOBAL (z, y) via
    ``_geom.sag_to_global`` for EVERY point. The object at infinity (non-finite
    thickness 0) gets a non-finite point, which ``drawn_extent`` skips.

    Returns ``None`` if any surface in ``1..n-2`` has a non-finite draw height, an
    invalid edge sag, or (folded) a frame that is not ok. The object and image degrade
    to a vertex-only point instead, because they carry no lens outline.
    """
    try:
        thicknesses = [float(r.get("thickness", float("nan"))) for r in rows[:n]]
        z_vertex = _geom.vertex_z(thicknesses)
    except Exception:  # noqa: BLE001 — an unreadable prescription predicts nothing
        return None
    points = []
    for i in range(n):
        interior = 0 < i < n - 1
        if i == 0 and not math.isfinite(thicknesses[0]):
            nan = float("nan")
            points.append(SurfacePoint(0, nan, nan, nan, nan, nan, nan))
            continue
        frame = None
        if frames is not None:
            frame = frames[i] if i < len(frames) else None
            if not (isinstance(frame, dict) and frame.get("ok")):
                if interior:
                    return None
                nan = float("nan")
                points.append(SurfacePoint(i, nan, nan, nan, nan, nan, nan))
                continue
        h = draw_heights.get(i) if isinstance(draw_heights, dict) else None
        sample = None
        if _finite(h):
            try:
                sample = sag_fn(rows, i, float(h))
            except Exception:  # noqa: BLE001 — an unsampleable edge is an invalid edge
                sample = None
        ok_edge = (sample is not None and bool(sample[2]) and _finite(sample[0], sample[1])
                   and sample[3] == float(h))
        if interior and not ok_edge:
            return None
        if ok_edge:
            locals_ = ((0.0, 0.0), (float(h), float(sample[0])),
                       (-float(h), float(sample[1])))
        else:
            locals_ = ((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))
        mapped = []
        for y, sag in locals_:
            if frame is None:
                mapped.append((z_vertex[i] + sag, y))
            else:
                _gx, gy, gz = _geom.sag_to_global(frame["R"], frame["vertex"], y, sag)
                mapped.append((gz, gy))
        (z0, y0), (zh, yh), (zl, yl) = mapped
        points.append(SurfacePoint(i, z0, y0, zh, yh, zl, yl))
    return points


def curve_points(rows, i, ys, *, frames=None, sag_fn):
    """Surface ``i``'s predicted point at each height in ``ys`` (mm): ``(z, y)`` (global
    when framed), or ``None`` where the sag is invalid, non-finite, or the frame is not
    ok. ONE mapping, shared by ``profile_polyline`` and the interior samples (R-1), so
    the distinctness curve and the checked points cannot disagree. Never raises."""
    try:
        ys = [float(y) for y in ys]
    except Exception:  # noqa: BLE001 — unreadable heights: nothing to predict
        return []
    try:
        sags, valid = sag_fn(rows, i, ys)
        sags, valid = list(sags), list(valid)
        if len(sags) != len(ys) or len(valid) != len(ys):
            return [None] * len(ys)       # never a zip-truncated, misaligned answer
        frame = None
        z0 = None
        if frames is not None:
            frame = frames[i] if i < len(frames) else None
            if not (isinstance(frame, dict) and frame.get("ok")):
                return [None] * len(ys)
        else:
            thicknesses = [float(r.get("thickness", float("nan"))) for r in rows]
            z0 = _geom.vertex_z(thicknesses)[i]
        out = []
        for y, sag, ok in zip(ys, sags, valid):
            if not ok or not _finite(float(sag)):
                out.append(None)
            elif frame is None:
                out.append((z0 + float(sag), y))
            else:
                _gx, gy, gz = _geom.sag_to_global(frame["R"], frame["vertex"], y,
                                                  float(sag))
                out.append((gz, gy))
        return out
    except Exception:  # noqa: BLE001 — an unsampleable curve predicts no ink
        return [None] * len(ys)


def profile_polyline(rows, i, draw_height, *, frames=None, sag_fn, px_per_mm):
    """Surface ``i``'s OWN predicted curve, ``[(z, y), ...]`` in mm (global when framed).

    The "other surfaces' predicted ink" of the distinctness check. ``sag_fn(rows,
    i, ys) -> (sags, valid)`` samples the surface's sag at every height in ``ys``; only
    VALID samples become points. Sample count ``max(33, ceil(2*h*px_per_mm) + 1)`` puts
    the samples <= 1 px apart IN HEIGHT [unmeasured choice] -- a sampling density, NOT
    a chord-error bound, and no such bound is claimed.

    ``None`` when the height is not a positive finite number, the frame is not ok, or no
    sample is valid. Never raises.
    """
    try:
        h = float(draw_height)
        s = float(px_per_mm)
        if not (math.isfinite(h) and h > 0.0 and math.isfinite(s) and s > 0.0):
            return None
        count = max(33, int(math.ceil(2.0 * h * s)) + 1)
        ys = [-h + 2.0 * h * k / (count - 1) for k in range(count)]
        out = [p for p in curve_points(rows, i, ys, frames=frames, sag_fn=sag_fn)
               if p is not None]
        return out or None
    except Exception:  # noqa: BLE001 — an unsampleable curve predicts no ink
        return None


def point_polyline_distance(point, poly):
    """Shortest distance from ``point`` to the polyline ``poly`` (same units). A
    one-point polyline is a point; an empty one is ``inf``."""
    px, py = float(point[0]), float(point[1])
    if not poly:
        return math.inf
    if len(poly) == 1:
        return math.hypot(poly[0][0] - px, poly[0][1] - py)
    best = math.inf
    for (x0, y0), (x1, y1) in zip(poly[:-1], poly[1:]):
        dx, dy = x1 - x0, y1 - y0
        seg = dx * dx + dy * dy
        t = 0.0 if seg <= 0.0 else max(0.0, min(1.0, ((px - x0) * dx + (py - y0) * dy) / seg))
        best = min(best, math.hypot(x0 + t * dx - px, y0 + t * dy - py))
    return best


def drawn_extent(points, start, end):
    """The z/y extent over surfaces ``start..end``; non-finite points are skipped."""
    if points is None:
        return None
    zs, ys = [], []
    for p in points[start:end + 1]:
        for z, y in ((p.z, p.y), (p.z_hi, p.y_hi), (p.z_lo, p.y_lo)):
            if _finite(z, y):
                zs.append(z)
                ys.append(y)
    if not zs:
        return None
    return Extent(min(zs), max(zs), min(ys), max(ys), int(start), int(end))


def native_default_range(rows, n):
    """The exporter's own default ``(StartSurface, EndSurface)``.   [measured: Q1 corpus, 78 samples]"""
    t0 = float(rows[0].get("thickness", float("nan"))) if n > 0 else float("nan")
    return (0 if math.isfinite(t0) else 1, n - 1)


def fit(extent, W, H):
    """Model B fit of ``extent`` into a ``W x H`` export.   [measured: Q2 model B]

    Raises ``ValueError`` on a degenerate extent (no span in either direction).
    """
    zr = extent.zmax - extent.zmin
    yr = extent.ymax - extent.ymin
    sz = (W - FIT_A) / zr if zr > 0 else math.inf
    sy = (H - FIT_A) / yr if yr > 0 else math.inf
    if not math.isfinite(min(sz, sy)):
        raise ValueError("degenerate extent: no span to fit")
    return Registration(
        W=int(W), H=int(H), s=FIT_MARGIN * min(sz, sy),
        zc=(extent.zmin + extent.zmax) / 2.0, yc=(extent.ymin + extent.ymax) / 2.0,
        height_limited=sy < sz,
    )


def choose_canvas(extent):
    """``(W, H)``: fixed width, height from the lens aspect with slack, clamped."""
    zr = extent.zmax - extent.zmin
    yr = extent.ymax - extent.ymin
    if not (zr > 0 and math.isfinite(yr)):
        return CANVAS_W, CANVAS_H_MIN
    H = round((CANVAS_W - FIT_A) * yr / zr * CANVAS_H_SLACK + FIT_A)
    return CANVAS_W, int(min(max(H, CANVAS_H_MIN), CANVAS_H_MAX))


def optics_fill(points, n, start, end, canvas=FAR_REFERENCE_CANVAS):
    """How much of the fit box the OPTICS (surfaces ``1..n-2``) occupy — the larger of
    the z- and y-fill — when ``start..end`` is fitted into ``canvas``.

    The box is ``FIT_MARGIN`` of each canvas dimension: the SAME arithmetic the measured
    fills (0.026 / 0.011 / 0.12 / 0.15) were computed with (probe ``lens_fill``), so
    ``FILL_TRIGGER`` is compared against the number it was measured as. ``None`` when
    the extent cannot be formed.
    """
    whole = drawn_extent(points, start, end)
    optics = drawn_extent(points, 1, n - 2)
    if whole is None or optics is None:
        return None
    W, H = canvas
    try:
        reg = fit(whole, W, H)
    except ValueError:
        return None
    fw = reg.s * (optics.zmax - optics.zmin) / (FIT_MARGIN * W)
    fh = reg.s * (optics.ymax - optics.ymin) / (FIT_MARGIN * H)
    return max(fw, fh)


@dataclass(frozen=True)
class FarDecision:
    """The shared far-object decision. ``excluded`` entries are the envelope's
    ``far_object_excluded``; ``start``/``end`` are the surfaces left in the frame."""

    fill: "float | None"
    L: "float | None"
    lead_gap: "float | None"
    trail_gap: "float | None"
    lead_ratio: "float | None"
    trail_ratio: "float | None"
    triggered: bool
    cut_object: bool
    cut_image: bool
    start: int
    end: int
    excluded: list = field(default_factory=list)
    reason: "str | None" = None


def far_object_decision(rows, n, points):
    """The AXIAL far-object rule (fill trigger + gap ratio), used by EVERY renderer.

    ``fill < FILL_TRIGGER`` is the trigger; the ratio only picks the end (the ratio
    alone mis-orders Relay vs MIL — measured). An infinite object is never cut.
    ``reason``: ``None`` | ``"speck_no_cut"`` (triggered, nothing qualifies) |
    ``"extent_unreadable"`` (no points, or a non-positive optics length).
    """
    start0, end0 = native_default_range(rows, n) if n > 0 else (1, n - 1)

    def _t(i):
        try:
            return float(rows[i].get("thickness", float("nan")))
        except Exception:  # noqa: BLE001
            return float("nan")

    t0 = _t(0) if n > 0 else float("nan")
    lead = t0 if math.isfinite(t0) else None
    trail_raw = _t(n - 2) if n >= 2 else float("nan")
    trail = trail_raw if math.isfinite(trail_raw) else None
    L = None
    if points is not None and n >= 3:
        z1, zl = points[1].z, points[n - 2].z
        if _finite(z1, zl):
            L = zl - z1

    def _none(reason):
        return FarDecision(fill=None, L=L, lead_gap=lead, trail_gap=trail,
                           lead_ratio=None, trail_ratio=None, triggered=False,
                           cut_object=False, cut_image=False, start=start0,
                           end=end0, excluded=[], reason=reason)

    if points is None or L is None or L <= 0:
        return _none("extent_unreadable")
    fill = optics_fill(points, n, start0, end0)
    if fill is None:
        return _none("extent_unreadable")
    lead_ratio = lead / L if lead is not None else None
    trail_ratio = trail / L if trail is not None else None
    triggered = fill < FILL_TRIGGER
    cut_object = bool(triggered and lead is not None and lead > K_GAP * L)
    cut_image = bool(triggered and trail is not None and trail > K_GAP * L)
    excluded = []
    if cut_object:
        excluded.append({"surface": 0, "role": "object", "gap_mm": lead,
                         "gap_over_length": lead_ratio, "fill": fill,
                         "basis": "fill_and_ratio"})
    if cut_image:
        excluded.append({"surface": n - 1, "role": "image", "gap_mm": trail,
                         "gap_over_length": trail_ratio, "fill": fill,
                         "basis": "fill_and_ratio"})
    reason = "speck_no_cut" if triggered and not (cut_object or cut_image) else None
    return FarDecision(
        fill=fill, L=L, lead_gap=lead, trail_gap=trail, lead_ratio=lead_ratio,
        trail_ratio=trail_ratio, triggered=triggered, cut_object=cut_object,
        cut_image=cut_image, start=1 if cut_object else start0,
        end=n - 2 if cut_image else end0, excluded=excluded, reason=reason,
    )


# --------------------------------------------------------------------------- #
# The FOLDED far-object rule (owner ruling): NATIVE 3-D views only.
#
# There is no 3-D registration model, so there is no fill to predict: the
# gap-ratio arm ALONE decides, with ``K_GAP`` borrowed from the axial rule. ``L`` is
# the SEQUENTIAL path length from the first drawn optical surface to the fold-safe
# end, never a z difference (a fold makes z meaningless). The scaffold predicate is
# INJECTED (``scaffold``: ``layout_render._is_suppressed_scaffold``) because this
# module imports nothing from the renderer -- one predicate, one owner, no copy.
# --------------------------------------------------------------------------- #
def is_drawn_optical(rows, i, n, *, scaffold):
    """THE named predicate for both cut ends: not a coordinate break, AND
    (a ``Paraxial*`` surface OR not ``scaffold(rows, i, n)``).

    ``scaffold`` is type-blind beyond its CB check -- a Paraxial row reads material
    ``""`` + radius ``inf`` and would be suppressed as flat powerless air, surviving only
    when it is the STOP -- so the explicit Paraxial arm makes the rule independent of
    where the stop sits. Never raises: a row it cannot read counts as NOT drawn."""
    try:
        r = rows[i]
        type_name = r.get("type_name", "")
        type_name = type_name if type(type_name) is str else ""
        if _geom._is_coordinate_break(type_name):
            return False
        return bool(type_name.startswith("Paraxial") or not scaffold(rows, i, n))
    except Exception:  # noqa: BLE001 -- an unreadable row is not a drawn optic
        return False


def last_drawn_surface_before_image(rows, n, *, scaffold):
    """The FOLD-SAFE trailing-cut end (owner ruling): the largest index
    ``1 <= i < n-1`` with ``is_drawn_optical`` True -- never a coordinate break, never a
    flat powerless air dummy, a Paraxial surface always counts. On an axial refractive
    system this is ``n-2``; on the tilted mirror it is the MIRROR (3), not the
    co-located CB (4). ``None`` when no surface qualifies.
    [unmeasured choice -- the probe calls the selection "inferred"]"""
    for i in range(n - 2, 0, -1):
        if is_drawn_optical(rows, i, n, scaffold=scaffold):
            return i
    return None


def path_length(rows, i, j):
    """``sum(|thickness_k|)`` for ``k in i..j-1`` along the sequential path (coordinate
    break thicknesses included). ``None`` if any term is non-finite or unreadable."""
    total = 0.0
    try:
        for k in range(int(i), int(j)):
            t = float(rows[k].get("thickness", float("nan")))
            if not math.isfinite(t):
                return None
            total += abs(t)
    except Exception:  # noqa: BLE001 -- an unreadable gap is no length
        return None
    return total


def far_object_decision_folded(rows, n, *, scaffold):
    """The FOLDED far-object rule -- the gap-ratio arm alone (no fill).

    ``L = path_length(first_optical, last_drawn)``; ``L`` None or <= 0 -> no cut,
    ``reason="zero_optics_length"``. ``lead = |t0|`` (finite object only), ``trail =
    path_length(last_drawn, n-1)``; an end is cut iff its gap exceeds ``K_GAP * L``.
    A cut image ends at ``last_drawn`` (never a CB). Every entry carries
    ``"basis": "ratio_only_folded"`` and ``"fill": None`` -- no fill exists on this
    basis, and none is invented. KNOWN MISS: the tilted mirror at trail
    2000 (ratio 20) is NOT cut."""
    start0, end0 = native_default_range(rows, n) if n > 0 else (1, n - 1)
    try:
        t0 = float(rows[0].get("thickness", float("nan"))) if n > 0 else float("nan")
    except Exception:  # noqa: BLE001
        t0 = float("nan")
    lead = abs(t0) if math.isfinite(t0) else None
    first = None
    for i in range(1, n - 1):
        if is_drawn_optical(rows, i, n, scaffold=scaffold):
            first = i
            break
    last = last_drawn_surface_before_image(rows, n, scaffold=scaffold)
    L = (path_length(rows, first, last)
         if first is not None and last is not None and last >= first else None)
    trail = path_length(rows, last, n - 1) if last is not None else None

    if L is None or L <= 0.0:
        return FarDecision(fill=None, L=L, lead_gap=lead, trail_gap=trail,
                           lead_ratio=None, trail_ratio=None, triggered=False,
                           cut_object=False, cut_image=False, start=start0, end=end0,
                           excluded=[], reason="zero_optics_length")
    lead_ratio = lead / L if lead is not None else None
    trail_ratio = trail / L if trail is not None else None
    cut_object = bool(lead is not None and lead > K_GAP * L)
    cut_image = bool(trail is not None and trail > K_GAP * L)
    excluded = []
    if cut_object:
        excluded.append({"surface": 0, "role": "object", "gap_mm": lead,
                         "gap_over_length": lead_ratio, "fill": None,
                         "basis": "ratio_only_folded"})
    if cut_image:
        excluded.append({"surface": n - 1, "role": "image", "gap_mm": trail,
                         "gap_over_length": trail_ratio, "fill": None,
                         "basis": "ratio_only_folded"})
    return FarDecision(
        fill=None, L=L, lead_gap=lead, trail_gap=trail, lead_ratio=lead_ratio,
        trail_ratio=trail_ratio, triggered=bool(cut_object or cut_image),
        cut_object=cut_object, cut_image=cut_image,
        start=1 if cut_object else start0, end=last if cut_image else end0,
        excluded=excluded, reason=None,
    )


# --------------------------------------------------------------------------- #
# Pixel space: the exported raster, read with the probe's OWN loader so the
# thresholds are applied to the SAME numbers they were measured on.
# --------------------------------------------------------------------------- #
def load_rgb_u8(path):
    """``PIL.Image.open(path).convert("RGB")`` -> uint8 ``(H, W, 3)``, else ``None``.

    NOT ``matplotlib.image.imread``: that returns a PNG as float 0-1, and the class
    thresholds below are 0-255 integers. Never raises.
    """
    try:
        import numpy as np
        from PIL import Image
        with Image.open(path) as im:
            arr = np.asarray(im.convert("RGB"), dtype=np.uint8)
    except Exception:  # noqa: BLE001 — an unreadable raster is no raster
        return None
    if arr.ndim != 3 or arr.shape[2] != 3 or arr.shape[0] < 1 or arr.shape[1] < 1:
        return None
    return arr


def classify_pixels(rgb):
    """``(bg_mask, dark_mask, colour_mask)`` with the probe thresholds.

    RAISES ``ValueError`` unless ``rgb`` is a uint8 ``(H, W, 3)`` array: a float raster
    would put every pixel under ``DARK_MAX`` and certify ink that is not there.
    """
    import numpy as np
    if not (isinstance(rgb, np.ndarray) and rgb.dtype == np.uint8 and rgb.ndim == 3
            and rgb.shape[2] == 3):
        raise ValueError("classify_pixels needs a uint8 (H, W, 3) raster")
    c = rgb.astype(np.int16)
    mx = c.max(axis=2)
    mn = c.min(axis=2)
    bg = mn >= BG_MIN
    dark = (mx <= DARK_MAX) & ((mx - mn) <= DARK_CHROMA_MAX)
    colour = (~bg) & ((mx - mn) > COLOUR_CHROMA_MIN)
    return bg, dark, colour


def dark_bbox(dark_mask):
    """``(x0, y0, x1, y1)`` pixel indices of the dark pixels (inclusive), else ``None``."""
    import numpy as np
    ys, xs = np.nonzero(dark_mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


def ink_residuals(dark_mask, points_px, radius=INK_SEARCH_PX):
    """Per point: the distance to the nearest DARK pixel centre within ``radius``,
    else ``None`` (no ink near enough to attribute). Pixel ``(i, j)`` is centred at
    ``(i, j)`` -- the coordinate ``Registration.to_px`` returns."""
    import numpy as np
    h, w = dark_mask.shape
    out = []
    for x, y in points_px:
        x, y = float(x), float(y)
        if not (math.isfinite(x) and math.isfinite(y)):
            out.append(None)
            continue
        x0 = max(0, int(math.floor(x - radius)))
        x1 = min(w - 1, int(math.ceil(x + radius)))
        y0 = max(0, int(math.floor(y - radius)))
        y1 = min(h - 1, int(math.ceil(y + radius)))
        if x0 > x1 or y0 > y1:
            out.append(None)
            continue
        jj, ii = np.nonzero(dark_mask[y0:y1 + 1, x0:x1 + 1])
        if len(ii) == 0:
            out.append(None)
            continue
        d = float(np.sqrt((ii + x0 - x) ** 2 + (jj + y0 - y) ** 2).min())
        out.append(d if d <= radius else None)
    return out


# --------------------------------------------------------------------------- #
# The 3-D viewer's per-configuration ray colours -- a named
# WEAKER consistency check, never the configuration-identity proof.
# --------------------------------------------------------------------------- #
#: The probe's own ray-pixel classes (a live probe's
#: ``content_stats``): one channel > 150, the other two < 90.   [measured]
RAY_COLOUR_HI = 150
RAY_COLOUR_LO = 90
#: The 3-D viewer's ``ColorRaysBy=Config`` palette, measured for THREE configurations
#: only (cur1 blue 12597 / cur2 green 12595 / cur3 red 12831 px).   [measured]
CONFIG_RAY_PALETTE = {1: "blue", 2: "green", 3: "red"}
#: The other two classes on a single-config viewer export stay <= 90 px (the axis
#: triad); the all-config overlay (5957 / 7479 / 10857) fails this by three orders of
#: magnitude.   [measured basis]
OTHER_CONFIG_COLOUR_MAX_PX = 100


def ray_colour_counts(rgb):
    """``{"blue": n, "green": n, "red": n}`` over the whole uint8 ``(H, W, 3)`` raster,
    with the probe's thresholds. RAISES ``ValueError`` on any other raster."""
    import numpy as np
    if not (isinstance(rgb, np.ndarray) and rgb.dtype == np.uint8 and rgb.ndim == 3
            and rgb.shape[2] == 3):
        raise ValueError("ray_colour_counts needs a uint8 (H, W, 3) raster")
    c = rgb.astype(np.int16)
    r, g, b = c[..., 0], c[..., 1], c[..., 2]
    hi, lo = RAY_COLOUR_HI, RAY_COLOUR_LO
    return {"blue": int(((b > hi) & (r < lo) & (g < lo)).sum()),
            "green": int(((g > hi) & (r < lo) & (b < lo)).sum()),
            "red": int(((r > hi) & (g < lo) & (b < lo)).sum())}


def ray_colour_verdict(counts, k):
    """``(consistent, reason)`` for configuration ``k`` from ``ray_colour_counts``.

    ``True`` iff ``k``'s palette class dominates AND each OTHER class is
    ``<= OTHER_CONFIG_COLOUR_MAX_PX``; ``False`` when ``k``'s class shows rays but the
    other bound fails (or another class dominates); ``None`` + a reason when the check
    cannot decide: ``k`` outside the measured palette, or ``k``'s own class is not above
    the triad bound (no visible rays to read -- an absence is never a verdict)."""
    want = CONFIG_RAY_PALETTE.get(k)
    if want is None:
        return None, f"configuration {k} is outside the measured 3-config palette"
    if counts[want] <= OTHER_CONFIG_COLOUR_MAX_PX:
        return None, (f"the configuration-{k} ray colour ({want}) shows "
                      f"{counts[want]} px, not above the axis-triad bound")
    others = [c for c in CONFIG_RAY_PALETTE.values() if c != want]
    dominant = max(counts, key=lambda c: counts[c])
    ok = dominant == want and all(counts[c] <= OTHER_CONFIG_COLOUR_MAX_PX
                                  for c in others)
    return bool(ok), None
