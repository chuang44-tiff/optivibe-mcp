"""tools/layout_render.py — render_layout: headless meridional cross-section PNG.

Draws a y-z meridional schematic from surface geometry (§3): the sag
profile per surface, cumulative UNFOLDED vertex placement, light-grey glass bodies,
heavy mirror lines, OUR surface-number stamps (the whole point — read a number to
point at a surface), a STOP glyph (two short orange vertical aperture-edge ticks at
the stop's clear-aperture edges), an honest folded-system banner (NO bent-leg
reconstruction), a PNG-magic durability gate via the shared ``_is_png`` oracle, and
a temp -> gate -> ``os.replace`` ATOMIC write.

Import discipline (§3.1): matplotlib/numpy are imported LAZILY inside the handler
with ``matplotlib.use("Agg", force=True)`` BEFORE pyplot — a broken plotting
install must NOT break ``load_manifest()`` at import time and disable every tool.
The figure is ALWAYS closed (``plt.close(fig)`` in ``finally``) — matplotlib leaks
figures globally otherwise.

NEVER raises (§0.8): the whole body is wrapped; failures map to
``render_unavailable`` (no mpl / nothing to draw), ``render_gate_failed`` (a file
was written but failed the magic-byte gate), or ``render_failed`` (any other draw
error). The classifier + geometry read are SHARED with ``describe_surfaces`` via
``_layout_geometry`` (one classifier, one read path).

Live ZOS-API integration: exercised by the mandatory live test; unit-tested here
against FakeLDE/FakeRow doubles + a real Agg render asserting PNG magic bytes.
"""
import math
import os
import tempfile
from time import perf_counter

from ..artifact_sink import _safe_name
from ..errors import ToolParamError
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _config_common as _cfg
from . import _layout_geometry as _geom
from . import _layout_rays as _rays
from ._image_gate import _is_png

_DEFAULT_TITLE = "Layout"
_FOLDED_BANNER = (
    "Folded system (coordinate break / mirror) — axial layout past the fold is "
    "schematic; open the .zmx for the native 3D layout."
)
_N_SAMPLES = 81  # linspace(-h, +h, 81) per §3.2
# UNIFORM-ARROW-LENGTH, EDGE-ANCHORED callout scheme (Loop-4): every surface number is
# a thin ARROW (a leader with an arrowhead) pointing to ITS surface vertex at ITS OWN
# aperture edge. The arrow LENGTH is a single constant (`_STAMP_ARROW_LEN = a fraction
# of h_max`) so every leader is the SAME length, but the label is anchored to EACH
# surface's OWN edge height: a TOP label sits at `heights[idx] + arrow_len`, a BOTTOM
# label at `-(heights[idx] + arrow_len)`. For a system whose elements have DIFFERENT
# diameters the labels now FOLLOW the edges (a bigger element's label sits higher) with
# equal-length arrows — instead of a flat baseline that ignores the per-element diameter.
# The collision split is kept (a z-colliding number drops to the BOTTOM with an up-arrow;
# the non-colliding neighbour stays on TOP with a down-arrow).
_STAMP_ARROW_LEN_FRAC = 0.7  # uniform arrow length = h_max * this (clears the body+rays)
# A label whose surface z is within this fraction of the z-span of an ALREADY-PLACED
# top label's surface z is treated as COLLIDING and dropped to the BOTTOM baseline.
# Tuned to reliably catch the near-coincident case (Δz≈0, the stop on the lens vertex)
# while leaving comfortably-separated numbers on the top baseline.
_STAMP_COLLIDE_FRAC = 0.04

# The default ray-trace budget (seconds) for the explicit render_layout ray overlay
# (render-obscured-slow A3). A healthy full trace is < 0.05 s; the reported stall was
# 233 s, so 12 s never truncates a healthy system but bounds a contention-stuck trace.
_DEFAULT_RAY_BUDGET_S = 12.0


def _ray_budget_s():
    """Read ``OPTIVIBE_RAY_BUDGET_S`` (default 12.0) with the hang-watchdog clamp (A3).

    POSITIVE-FINITE clamp (mirrors ``__main__._env_float``): a non-positive value
    (0 / negative) would make the deadline already-elapsed and truncate EVERY healthy
    trace; a non-finite value (nan / inf) would defeat the budget (``perf_counter() >=
    nan`` is False -> unbounded). Any of those falls back to the default. Kept LOCAL
    (no re-plumb of ``__main__``); only the budget value crosses, not the env layer.
    """
    raw = os.environ.get("OPTIVIBE_RAY_BUDGET_S")
    if raw is None:
        return _DEFAULT_RAY_BUDGET_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_RAY_BUDGET_S
    if not math.isfinite(value) or value <= 0:
        return _DEFAULT_RAY_BUDGET_S
    return value


def _is_suppressed_scaffold(rows, i, n):
    """True iff surface ``i`` is non-optical SCAFFOLDING that must NOT be drawn/stamped.

    Suppressed iff the surface is a coordinate break OR a **flat, powerless air dummy**:
    an air material (``_is_air_material``) AND a non-finite (∞) radius (no optical power)
    AND it is NOT the stop (``rows[i]["is_stop"]``) AND NOT the image (``i == n - 1``) AND
    NOT the object (``i == 0``). Everything else is KEPT — glass surfaces, mirrors, CURVED
    (finite-radius) air surfaces (real lens back-faces, e.g. surf 2 R=-200 / surf 9 R=-120),
    the stop (even a flat-air ``normalize_stop`` dummy — the ``is_stop`` exemption keeps it),
    and the image.

    Rationale: a flat powerless air surface carries no optics — it is fold/spacer scaffolding
    (a CB-coincident surface, a flat air spacer). A curved air surface is a real lens back-face
    the user points at to edit, so it stays. For an all-refractive layout (every surface curved)
    NOTHING is suppressed -> no regression. The CB check is SUBSUMED here, so this predicate
    fully replaces the prior CB-only exclusion while preserving CB suppression.
    """
    r = rows[i]
    # An UNREADABLE/degraded row is NEVER confidently scaffolding:
    # `_read_all_geometry` fills a wedged surface with the
    # placeholder shape (`material="", radius=NaN`), which `_is_air_material("")` +
    # `not math.isfinite(NaN)` would BOTH read as a "flat powerless air dummy" — so a
    # transiently-unreadable REAL surface (glass / curved-air-back / CB) would be
    # silently dropped AND falsely listed in `scaffold_suppressed`. Fail OPEN: keep it
    # in the figure attempt + stamp it, and let the existing `degraded` channel own the
    # disclosure. This short-circuit MUST run FIRST (before the CB and flat-air checks).
    if r.get("unreadable"):
        return False
    # `.get` with safe defaults (LOW): a row missing a key degrades to "not scaffolding
    # / keep" rather than raising internally and aborting the WHOLE figure (the outer
    # try/except keeps dispatch safe, but it would sink the figure). Real rows always
    # carry these keys, so this is belt-and-braces.
    if _geom._is_coordinate_break(r.get("type_name", "")):
        return True
    if i == 0 or i == n - 1:
        return False
    if r.get("is_stop", False):
        return False
    return _geom._is_air_material(r.get("material", "")) and not math.isfinite(
        r.get("radius", float("nan"))
    )


def _import_mpl():
    """Lazy, guarded matplotlib/numpy import (§3.1). Agg BEFORE pyplot.

    A failure here is turned into ``render_unavailable`` by the caller — the
    manifest must still load even if matplotlib is broken.
    """
    import matplotlib
    matplotlib.use("Agg", force=True)  # headless, BEFORE pyplot
    import matplotlib.pyplot as plt
    import numpy as np
    return plt, np


def _resolve_path(session, path):
    """Resolve the output PNG path: caller-supplied (sanitized stem) or minted.

    Mirrors ``capture_graphic._resolve_path``: a supplied ``path`` keeps its dir +
    extension but the STEM is run through ``_safe_name`` (blocks the ADS ``:``
    trap). A null ``path`` mints ``layout.png`` under the sink run-dir if wired,
    else the current dir.
    """
    if path:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        safe_stem = _safe_name(stem)
        return os.path.join(directory, f"{safe_stem}{ext}") if directory else f"{safe_stem}{ext}"
    sink = getattr(session, "artifact_sink", None)
    run_dir = getattr(sink, "run_dir", None) if sink is not None else None
    if run_dir:
        return os.path.join(run_dir, "layout.png")
    return "layout.png"


def _fail(error_family, error, path=None):
    """Build a render failure envelope (never raises)."""
    return {
        "ok": False,
        "error_family": error_family,
        "error": error,
        "path": path,
        "size_bytes": 0,
        "surface_labels": [],
    }


def _read_all_geometry(lde, n, system=None):
    """Read every surface's raw-float geometry + facts (§0.1, §3.2). NEVER raises.

    One wedged surface degrades to a placeholder marked ``unreadable`` (the render
    continues, noted) rather than sinking the whole figure.

    Asphere S2 SAGMATH: ``system`` is threaded into ``read_geometry_row`` so an
    EvenAspheric row carries its 8 even-asphere coefficients (the drawn curve is the
    TRUE polynomial profile). A non-asphere row carries ``aspheric_coefficients = None``
    (the conic-only byte-identical path).
    """
    rows = []
    degraded = []
    for i in range(n):
        try:
            g = _geom.read_geometry_row(lde, i, system=system)
            g["unreadable"] = False
        except BaseException as exc:  # noqa: BLE001 — one bad surface degrades, render continues
            g = {
                "radius": float("nan"),
                "thickness": float("nan"),
                "conic": float("nan"),
                "semi_diameter": float("nan"),
                "type_name": "",
                "material": "",
                "is_stop": False,
                "aspheric_coefficients": None,
                "asphere_norm_radius": None,
                "asphere_power": None,
                "unreadable": True,
            }
            degraded.append(i)
        rows.append(g)
    return rows, degraded


def _glass_groups(rows, n, optical_indices):
    """Walk consecutive glass surfaces into cemented GROUPS.

    Returns a list of groups; each group is a list of consecutive surface indices
    that draw as ONE connected body. A singlet is its own one-element group. A
    surface ``i+1`` joins the running group iff ``is_cemented_interface(rows, i+1)``
    (both bounding media real glass) — so the doublet (surf 2/3/4)
    is ONE group while air-separated elements are separate groups.

    A glass element spans surface ``i`` (front cap) to ``i+1`` (back cap); the group
    therefore extends one surface PAST the last glass surface to close the body.
    """
    groups = []
    i = 0
    optical_set = set(optical_indices)
    while i < n:
        r = rows[i]
        is_glass = (
            not _geom._is_mirror(r["material"])
            and not _geom._is_coordinate_break(r["type_name"])
            and not _geom._is_air_material(r["material"])
        )
        if not is_glass:
            i += 1
            continue
        # Start a group at the first glass surface; absorb cemented continuations.
        group = [i]
        j = i + 1
        while j < n and rows[j] is not None and _geom.is_cemented_interface(rows, j):
            group.append(j)
            j += 1
        # The back cap is the surface AFTER the last glass surface in the group.
        back = j
        if back < n:
            group.append(back)
        groups.append([g for g in group])
        i = j  # the next element starts at the back cap (which may be air or glass)
    return groups


def _draw(
    plt, np, rows, n, title, stop_index, folded, heights, all_zero_semi,
    degraded, ray_data, draw_rays,
):
    """Draw the figure and return it. Caller owns closing it in a finally.

    Coordinate-break and flat powerless air dummy/spacer surfaces are suppressed
    (scaffolding, not drawn); real optics (glass, mirrors, curved lens-backs), the stop,
    and the image are stamped with their true Zemax numbers.

    Returns ``(fig, surface_labels, stop_label, n_rays_drawn)``. ``ray_data`` is the
    ``read_field_rays`` result (or ``None`` when ``draw_rays`` is False / suppressed);
    ``n_rays_drawn`` counts the ray polylines actually plotted.
    """
    fig, ax = plt.subplots(figsize=(13, 5), dpi=120)

    z_vertex = _geom.vertex_z([r["thickness"] for r in rows])

    # Optical surfaces = everything except the object (0), image (N-1), AND any
    # SCAFFOLDING surface: a coordinate-break frame operator OR a flat, powerless air
    # dummy/spacer (an air surface with ∞ radius carries no optics — fold scaffolding /
    # a spacer, not a surface the beam lands on). The stop is EXEMPT (a flat-air
    # normalize_stop dummy is still drawn + stamped). Curved (finite-radius) air surfaces
    # — real lens back-faces — are KEPT. An all-refractive system has every surface
    # curved, so NOTHING is suppressed and this list is IDENTICAL to the old one (no
    # regression). _is_suppressed_scaffold SUBSUMES the prior CB-only check.
    optical_indices = [
        i
        for i in range(n)
        if i != 0 and i != n - 1
        and not _is_suppressed_scaffold(rows, i, n)
    ]
    surface_labels = []

    h_max = max(heights) if heights else 1.0

    # STACKED + ARROW callouts (R5): collect (surface_index, color) here as the
    # per-surface loop runs; after the loop they are stamped onto staggered heights
    # with a thin arrow pointing from each number down to its surface vertex. This
    # replaces the old single-baseline stamp + the special-case stop dashed-leader —
    # the stagger + arrows handle crowding uniformly for ALL surfaces.
    callouts = []  # list of (surface_index, color)

    # --- cement-aware closed element outlines (L4) ------------------------- #
    grouped = set()  # surfaces drawn as part of a glass group (skip the per-pair fill)
    for group in _glass_groups(rows, n, optical_indices):
        # Outer outline: front cap forward edge + back cap reversed edge, closed at
        # each surface's own clear-aperture height (a slanted edge when the front/back
        # apertures differ is correct, L4). Internal cement interfaces draw as thin
        # lines (no air-edge gap).
        front = group[0]
        back = group[-1]
        h_front = heights[front]
        h_back = heights[back]
        y_front = np.linspace(-h_front, h_front, _N_SAMPLES)
        z_front, vf = _geom.sag_profile(
            rows[front]["radius"], rows[front]["conic"], y_front,
            coeffs=rows[front].get("aspheric_coefficients"),
            norm_radius=rows[front].get("asphere_norm_radius"),
            power=rows[front].get("asphere_power"),
        )
        z_front_abs = z_vertex[front] + z_front
        y_back = np.linspace(-h_back, h_back, _N_SAMPLES)
        z_back, vb = _geom.sag_profile(
            rows[back]["radius"], rows[back]["conic"], y_back,
            coeffs=rows[back].get("aspheric_coefficients"),
            norm_radius=rows[back].get("asphere_norm_radius"),
            power=rows[back].get("asphere_power"),
        )
        z_back_abs = z_vertex[back] + z_back

        # Closed polygon (forward front edge, then back edge reversed). The straight
        # outer edges (top + bottom) are implicit in the fill close.
        poly_z = list(z_front_abs[vf]) + list(z_back_abs[::-1])
        poly_y = list(y_front[vf]) + list(y_back[::-1])
        ax.fill(poly_z, poly_y, color="0.75", alpha=0.35, zorder=1, linewidth=0)
        # Outer top/bottom edges closing the body at the clear aperture.
        ax.plot(
            [z_front_abs[-1], z_back_abs[-1]], [y_front[-1], y_back[-1]],
            color="black", linewidth=1.0, zorder=3,
        )
        ax.plot(
            [z_front_abs[0], z_back_abs[0]], [y_front[0], y_back[0]],
            color="black", linewidth=1.0, zorder=3,
        )
        # Every surface in the group is drawn here (its sag line below) — mark them
        # so the per-surface loop does not re-draw a stray per-pair fill.
        for g in group:
            grouped.add(g)

    for i in optical_indices:
        r = rows[i]
        h = heights[i]
        R = r["radius"]
        k = r["conic"]
        material = r["material"]
        is_mirror = _geom._is_mirror(material)

        y = np.linspace(-h, h, _N_SAMPLES)
        z, valid = _geom.sag_profile(
            R, k, y, coeffs=r.get("aspheric_coefficients"),
            norm_radius=r.get("asphere_norm_radius"),
            power=r.get("asphere_power"),
        )
        z_abs = z_vertex[i] + z

        # Surface profile line: heavier black for a mirror, solid black otherwise.
        # Cement interfaces (internal to a glass group) draw thinner.
        is_internal_cement = (
            i in grouped and i != 0 and _geom.is_cemented_interface(rows, i)
        )
        if is_mirror:
            line_w = 2.4
        elif is_internal_cement:
            line_w = 0.8
        else:
            line_w = 1.3
        ax.plot(z_abs[valid], y[valid], color="black", linewidth=line_w, zorder=3)

        # Register a stacked-arrow callout for this surface (R5). The stop surface (1)
        # is distinguished in RED; every other optical surface in black. The arrow
        # target (vertex z, aperture-edge y) and the staggered label height are
        # computed uniformly below — no special single-baseline / leader-only-for-stop
        # case any more.
        is_stop = (stop_index is not None and i == stop_index)
        callouts.append((i, "red" if is_stop else "black"))
        surface_labels.append(i)

    # --- optical axis + image plane (L5) ---------------------------------- #
    ax.axhline(0.0, color="0.6", linewidth=0.6, zorder=0)
    image_z = z_vertex[n - 1]
    ax.axvline(image_z, color="0.5", linestyle="-", linewidth=0.8, zorder=2)
    # The image plane (N-1) joins the stacked-arrow callouts in grey.
    callouts.append((n - 1, "0.4"))

    # STOP glyph (user feedback R4): the classic aperture-stop caret — two short
    # VERTICAL orange tick marks straddling the stop's clear-aperture edges (+/- the
    # stop's own semi-diameter), one at the top edge and one at the bottom, drawn at
    # the stop surface's z. Each tick is a short vertical segment perpendicular to the
    # optical axis, matching the user's reference image (no "STOP" text — the glyph +
    # the red "1" surface number identify the stop). The stop's red "1" surface number
    # is emitted as one of the stacked-arrow callouts below (no special-case leader).
    stop_label = None
    if stop_index is not None and stop_index in optical_indices:
        stop_label = stop_index
        zs = z_vertex[stop_index]
        stop_semi = heights[stop_index]  # the stop's own clear-aperture half-height
        # Vertical tick half-length: a few data-units, scaled to the system so it
        # reads at any size. The tick straddles the aperture edge (edge +/- this).
        tick_half = max(0.06 * h_max, 0.04 * stop_semi)
        for y_edge in (stop_semi, -stop_semi):
            ax.plot(
                [zs, zs],
                [y_edge - tick_half, y_edge + tick_half],
                color="#FF8C00",
                linewidth=2.2,
                solid_capstyle="butt",
                zorder=5,
            )
        # Loop-2 FIX 2: in ADDITION to the red "1" stacked number callout, label the
        # stop "STOP" (user: "stop should label as stop"). The RED "STOP" text is
        # emitted AFTER the surface-number callouts below — so it can be stacked BELOW
        # any bottom-placed numbers at the stop's z (the stop "1" itself drops to the
        # bottom when it shares the lens vertex), keeping "1" and "STOP" from colliding.

    # --- UNIFORM-ARROW-LENGTH, EDGE-ANCHORED ARROW surface-number callouts (Loop-4) -- #
    # Every NON-colliding surface number is placed at `heights[idx] + arrow_len` (its OWN
    # top aperture edge plus the single uniform arrow length) with a LONG arrow (annotate
    # "->" leader) pointing DOWN to the surface vertex at the top of its aperture. Because
    # the offset is added to EACH surface's own edge, a bigger-diameter element's label
    # sits higher — the labels FOLLOW the edges (varied-diameter systems), but every arrow
    # is the SAME length. y-limits are raised below from the MAX label y. The stop stays RED.
    #
    # PUT-AT-BOTTOM collision handling (kept from Loop-2): walking the callouts in
    # z-order, we track the z of EACH already-placed TOP label. If the current surface's
    # z is within `collide_thresh` (a fraction of the z-span) of ANY already-placed top
    # label, it would overlap that neighbour's down-arrow (which runs from the
    # neighbour's top label all the way down to the shared z). So the colliding number
    # is placed at `-(heights[idx] + arrow_len)` BELOW the lower aperture edge with a LONG
    # arrow (same uniform length) pointing UP to its vertex.
    # e.g. the doublet's stop "1" (same z as lens "2") drops to the bottom with an
    # up-arrow; "2" stays alone on top — no overlap.
    arrow_len = h_max * _STAMP_ARROW_LEN_FRAC
    # z-span = image_z - first optical vertex (the drawn optical extent). A degenerate
    # (zero/non-finite) span falls back so the threshold stays a positive number.
    first_optical_z = z_vertex[1] if n > 1 else z_vertex[0]
    z_span = image_z - first_optical_z
    if not math.isfinite(z_span) or z_span <= 0:
        z_span = 1.0
    collide_thresh = _STAMP_COLLIDE_FRAC * z_span

    # Order the callouts by surface z (then by surface index, stable) so the proximity
    # walk is genuinely left-to-right regardless of append order. TIE-BREAK at equal z:
    # the STOP sorts LAST so a NON-stop neighbour claims the TOP slot first and the STOP
    # is the one detected as colliding -> dropped to the bottom (the user's intent: the
    # zero-thickness stop sharing the lens vertex is the number that goes to the bottom).
    def _order_key(c):
        idx = c[0]
        is_stop = stop_index is not None and idx == stop_index
        return (z_vertex[idx], 1 if is_stop else 0, idx)

    ordered = sorted(callouts, key=_order_key)
    placed_top_z = []   # z of every label already placed on the TOP baseline
    deepest_bottom_y = None  # deepest bottom-label y (for stacking STOP below it)
    highest_top_y = None  # highest top-label y (for the y-limit recompute, Loop-4)
    for idx, color in ordered:
        zc = z_vertex[idx]
        y_edge = heights[idx] if idx < len(heights) else h_max
        # Collision = within the threshold of ANY label already on the TOP baseline. Such
        # a number would overlap that neighbour's down-arrow, so it goes to the BOTTOM.
        collides = any(abs(zc - pz) < collide_thresh for pz in placed_top_z)
        if collides:
            # BOTTOM placement: anchored to THIS surface's own edge (-(edge + arrow_len)),
            # so the leader is the uniform arrow length below the lower aperture edge and
            # follows the per-element diameter. The arrow points UP to the vertex (at the
            # BOTTOM of the surface's aperture so the leader does not cross the system).
            label_y = -(y_edge + arrow_len)
            deepest_bottom_y = (
                label_y if deepest_bottom_y is None else min(deepest_bottom_y, label_y)
            )
            ax.annotate(
                str(idx),
                xy=(zc, -y_edge),
                xytext=(zc, label_y),
                ha="center",
                va="top",
                fontsize=8,
                color=color,
                zorder=6,
                arrowprops={
                    "arrowstyle": "->",
                    "lw": 0.6,
                    "color": color,
                    "shrinkA": 1.0,
                    "shrinkB": 1.0,
                },
            )
            continue
        # TOP placement (non-colliding): anchored to THIS surface's own edge
        # (edge + arrow_len), so the leader is the uniform arrow length above the top
        # aperture edge and follows the per-element diameter. Record this z as placed-on-top.
        label_y = y_edge + arrow_len
        placed_top_z.append(zc)
        highest_top_y = (
            label_y if highest_top_y is None else max(highest_top_y, label_y)
        )
        # Arrow tip sits at the surface vertex, at the top of its aperture.
        ax.annotate(
            str(idx),
            xy=(zc, y_edge),
            xytext=(zc, label_y),
            ha="center",
            va="bottom",
            fontsize=8,
            color=color,
            zorder=6,
            arrowprops={
                "arrowstyle": "->",
                "lw": 0.6,
                "color": color,
                "shrinkA": 1.0,
                "shrinkB": 1.0,
            },
        )

    # --- deferred "STOP" text (Loop-2 FIX 2, placed AFTER the callouts) ---- #
    # Now that the bottom-label stagger is known, drop the RED "STOP" text BELOW the
    # deepest bottom-placed number at the stop's z (the stop "1" itself lands on the
    # bottom when it shares the lens vertex) — so "1" and "STOP" stack, never collide.
    # If nothing went to the bottom, fall back to just under the lower aperture tick.
    stop_text_y = None
    if stop_index is not None and stop_index in optical_indices:
        zs = z_vertex[stop_index]
        stop_semi = heights[stop_index]
        tick_half = max(0.06 * h_max, 0.04 * stop_semi)
        below_ticks = -stop_semi - tick_half - 0.18 * h_max
        if deepest_bottom_y is not None:
            # Below the deepest bottom number (which grows downward, va="top") by a
            # text-height gap so the "1" digit and "STOP" word never overlap.
            stop_text_y = min(below_ticks, deepest_bottom_y - 0.22 * h_max)
        else:
            stop_text_y = below_ticks
        ax.text(
            zs, stop_text_y, "STOP",
            ha="center", va="top", fontsize=7, color="red", zorder=6,
        )

    # Object (0) reference tick at the figure margin (schematic).
    ax.text(
        z_vertex[0], 0.0, "0", ha="right", va="center", fontsize=7,
        color="0.4", zorder=5,
    )

    # --- per-field chief + marginal rays (L6) ----------------------------- #
    n_rays_drawn = 0
    if draw_rays and ray_data and ray_data.get("fields"):
        try:
            cmap = plt.get_cmap("tab10")
        except Exception:  # noqa: BLE001 — colormap lookup must never sink the draw
            cmap = None
        fields = ray_data["fields"]
        for fi, field in enumerate(fields):
            color = cmap(fi % 10) if cmap is not None else "C{}".format(fi % 10)
            fy = field.get("field_y", 0.0)
            rays = field.get("rays", {})
            first = True
            for label in ("chief", "upper_marginal", "lower_marginal"):
                poly = rays.get(label) or []
                if len(poly) < 2:
                    continue
                zs_ray = [p[0] for p in poly]
                ys_ray = [p[1] for p in poly]
                # Legend attaches to the FIRST SURVIVING ray of the field (chief
                # if present, else a marginal) — on an obscured pupil the chief
                # truncates to nothing, so a chief-only label leaves the field
                # legend-less (empty-legend warning). Width still tracks ray type.
                lw = 1.0 if label == "chief" else 0.8
                legend = f"field Y={fy:g}" if first else "_nolegend_"
                first = False
                ax.plot(
                    zs_ray, ys_ray, color=color, linewidth=lw, zorder=4,
                    label=legend,
                )
                n_rays_drawn += 1
        if n_rays_drawn > 0:
            ax.legend(loc="upper right", fontsize=7, framealpha=0.8)

    # Folded banner (§3.6): an honest caveat — NO bent-leg reconstruction.
    if folded:
        ax.text(
            0.5, 0.97, _FOLDED_BANNER,
            transform=ax.transAxes, ha="center", va="top", fontsize=8,
            color="red", wrap=True,
            bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.8},
            zorder=6,
        )

    ax.set_title(title)
    ax.set_xlabel("z (optical axis)")
    ax.set_ylabel("y")
    # Equal aspect (the headline fix, L3): curvature/angles read TRUE.
    ax.set_aspect("equal", adjustable="datalim")
    # Explicit limits: x from the first vertex to the image (~5% pad), y +/-1.15*h_max.
    z_lo = min(z_vertex[1:]) if n > 1 else 0.0
    z_hi = image_z
    z_pad = 0.05 * (z_hi - z_lo) if z_hi > z_lo else 1.0
    ax.set_xlim(z_lo - z_pad, z_hi + z_pad)
    # y-limits (Loop-4 EDGE-ANCHORED): the top must clear the HIGHEST top-placed
    # surface-number label (the MAX over surfaces of heights[idx] + arrow_len — a bigger
    # element's edge-anchored label sits higher), plus a little headroom for the glyph.
    # Fall back to the largest-possible top label (h_max + arrow_len) when no top label
    # was placed (e.g. every callout collided to the bottom).
    arrow_len = h_max * _STAMP_ARROW_LEN_FRAC
    top_stack_y = highest_top_y if highest_top_y is not None else (h_max + arrow_len)
    y_top = top_stack_y + 0.18 * h_max
    # The bottom must clear: the lower aperture-edge tick, every BOTTOM-placed surface
    # number (down to deepest_bottom_y), AND the "STOP" text under them (stop_text_y,
    # growing downward). Take the deepest of those plus generous headroom so nothing is
    # clipped. Defaults preserve the prior -1.55*h_max floor when nothing went below.
    y_bot = -1.55 * h_max
    if deepest_bottom_y is not None:
        y_bot = min(y_bot, deepest_bottom_y - 0.25 * h_max)
    if stop_text_y is not None:
        y_bot = min(y_bot, stop_text_y - 0.25 * h_max)
    ax.set_ylim(y_bot, y_top)

    return fig, surface_labels, stop_label, n_rays_drawn


def _draw_folded_global(
    plt, np, rows, n, title, stop_index, heights, all_zero_semi,
    degraded, global_frames, ray_data, draw_rays,
):
    """Draw a FOLDED system in the GLOBAL frame.

    Element vertices come from ``GetGlobalMatrix(i)[10:13]`` (the SAME global frame the
    RAGY/RAGZ rays use — coherent past the fold), and each curved-mirror/lens outline
    is the local sag profile transformed via ``global = vertex + R_rowmajor · local``
    (R applied DIRECTLY, NOT transposed — 74× live-falsified). A surface whose global
    frame is degraded (``global_frames[i]["ok"]`` False — unreadable matrix / non-
    finite vertex) is SUPPRESSED (no outline drawn at a bogus axis position), scoped to
    that surface. Rays are UN-SUPPRESSED for folded systems (nit 5a): the elements now
    live in the same global frame as the RAGY/RAGZ rays, which is the whole point.

    Coordinate-break AND flat powerless air dummy/spacer surfaces are SUPPRESSED from the
    figure (scaffolding, not drawn): a CB is a frame OPERATOR (it rotates/decenters the
    local coordinate frame) and a flat-∞-radius air surface carries no optics (a fold
    spacer / a CB-coincident dummy) — neither is a surface the beam lands on, and their
    zero semi-diameter falls back to a max-height flat line that dominated the figure.
    Suppression is geometrically safe (probe [Discovered]): each real surface is placed
    from its OWN independent ``GetGlobalMatrix(i)`` vertex, so dropping a scaffold
    outline + stamp moves no real surface. Real optics (glass, mirrors, curved lens-backs),
    the stop, and the image keep their TRUE Zemax number; only scaffold indices are ABSENT
    from the stamps. Mirrors and CURVED (finite-radius) air lens-backs are KEPT — real optics.

    Returns ``(fig, surface_labels, stop_label, n_rays_drawn)``.
    """
    fig, ax = plt.subplots(figsize=(13, 5), dpi=120)

    # Exclude object (0), image (n-1), AND scaffolding: coordinate-break frame surfaces
    # AND flat powerless air dummies/spacers (probe: neither is a surface the beam lands
    # on). Mirrors + CURVED air lens-backs + the stop stay. This drops scaffold from the
    # draw loop, the callouts (built inside it), AND the stop/image logic that references
    # optical_indices.
    optical_indices = [
        i
        for i in range(n)
        if i != 0 and i != n - 1
        and not _is_suppressed_scaffold(rows, i, n)
    ]
    surface_labels = []
    h_max = max(heights) if heights else 1.0

    # Per-surface global (gy, gz) sag profiles, suppressing degraded frames.
    def _profile(i):
        """Return ``(gz, gy, valid_mask, vertex_gz, vertex_gy)`` for surface ``i`` in
        the global frame, or ``None`` if the frame is degraded (suppress)."""
        frame = global_frames[i] if i < len(global_frames) else None
        if frame is None or not frame.get("ok"):
            return None
        R = frame["R"]
        vertex = frame["vertex"]
        h = heights[i]
        y = np.linspace(-h, h, _N_SAMPLES)
        z_local, valid = _geom.sag_profile(
            rows[i]["radius"], rows[i]["conic"], y,
            coeffs=rows[i].get("aspheric_coefficients"),
            norm_radius=rows[i].get("asphere_norm_radius"),
            power=rows[i].get("asphere_power"),
        )
        # local = (0, y, sag); global = vertex + R_rowmajor · local (Q-B).
        gy, gz = _geom.sag_to_global_arrays(R, vertex, y, z_local)
        return gz, gy, valid, vertex[2], vertex[1]

    # Collect drawn-point extents for the axis limits (global frame).
    all_gz = []
    all_gy = []

    callouts = []  # (surface_index, color, vertex_gz, vertex_gy)

    # --- cement-aware closed element outlines (L4), in the global frame ------- #
    grouped = set()
    for group in _glass_groups(rows, n, optical_indices):
        front = group[0]
        back = group[-1]
        pf = _profile(front)
        pb = _profile(back)
        if pf is None or pb is None:
            # A degraded frame on either cap -> skip the group body (suppress), but the
            # per-surface loop below still draws each readable surface's own sag line.
            continue
        gz_f, gy_f, vf, _vzf, _vyf = pf
        gz_b, gy_b, vb, _vzb, _vyb = pb
        # Mask the BACK cap with its OWN validity (vb) the same way the front
        # uses vf. The aperture-edge samples of a steep cap are masked (sag radical < 0),
        # so the raw [0]/[-1] endpoints are NaN — appending the unmasked reversed back
        # cap drew a malformed/NaN body. Build the closed polygon from the VALID samples
        # of each cap (front forward + back reversed) and derive the cap-join lines from
        # the first/last VALID samples, not the raw endpoints.
        gz_fv = list(gz_f[vf])
        gy_fv = list(gy_f[vf])
        gz_bv = list(gz_b[vb])
        gy_bv = list(gy_b[vb])
        if gz_fv and gz_bv:
            poly_z = gz_fv + gz_bv[::-1]
            poly_y = gy_fv + gy_bv[::-1]
            ax.fill(poly_z, poly_y, color="0.75", alpha=0.35, zorder=1, linewidth=0)
            # Cap-join lines: connect matching aperture edges from the VALID extremes.
            ax.plot([gz_fv[-1], gz_bv[-1]], [gy_fv[-1], gy_bv[-1]],
                    color="black", linewidth=1.0, zorder=3)
            ax.plot([gz_fv[0], gz_bv[0]], [gy_fv[0], gy_bv[0]],
                    color="black", linewidth=1.0, zorder=3)
        for g in group:
            grouped.add(g)

    for i in optical_indices:
        prof = _profile(i)
        if prof is None:
            # Suppressed degraded surface — still register a callout at NO position?
            # No: without a global vertex there is no honest place to stamp it. Skip.
            continue
        gz, gy, valid, vz, vy = prof
        material = rows[i]["material"]
        is_mirror = _geom._is_mirror(material)
        is_internal_cement = (
            i in grouped and i != 0 and _geom.is_cemented_interface(rows, i)
        )
        if is_mirror:
            line_w = 2.4
        elif is_internal_cement:
            line_w = 0.8
        else:
            line_w = 1.3
        ax.plot(gz[valid], gy[valid], color="black", linewidth=line_w, zorder=3)
        all_gz.extend([z for z, v in zip(gz, valid) if v])
        all_gy.extend([y for y, v in zip(gy, valid) if v])
        is_stop = (stop_index is not None and i == stop_index)
        callouts.append((i, "red" if is_stop else "black", vz, vy))
        surface_labels.append(i)

    # --- optical axis + image plane (global frame) ------------------------- #
    ax.axhline(0.0, color="0.6", linewidth=0.6, zorder=0)
    image_frame = global_frames[n - 1] if n - 1 < len(global_frames) else None
    image_z = None
    if image_frame is not None and image_frame.get("ok"):
        image_z = image_frame["vertex"][2]
        ax.axvline(image_z, color="0.5", linestyle="-", linewidth=0.8, zorder=2)
        callouts.append((n - 1, "0.4", image_z, image_frame["vertex"][1]))

    # STOP glyph: vertical aperture-edge ticks at the stop's global vertex.
    stop_label = None
    stop_frame = (
        global_frames[stop_index]
        if (stop_index is not None and stop_index < len(global_frames))
        else None
    )
    if (stop_index is not None and stop_index in optical_indices
            and stop_frame is not None and stop_frame.get("ok")):
        stop_label = stop_index
        zs = stop_frame["vertex"][2]
        vy_stop = stop_frame["vertex"][1]
        stop_semi = heights[stop_index]
        tick_half = max(0.06 * h_max, 0.04 * stop_semi)
        for y_edge in (vy_stop + stop_semi, vy_stop - stop_semi):
            ax.plot([zs, zs], [y_edge - tick_half, y_edge + tick_half],
                    color="#FF8C00", linewidth=2.2, solid_capstyle="butt", zorder=5)

    # --- surface-number callouts (anchored at the global vertex) ----------- #
    arrow_len = h_max * _STAMP_ARROW_LEN_FRAC
    for idx, color, vz, vy in callouts:
        y_edge = heights[idx] if idx < len(heights) else h_max
        label_y = vy + y_edge + arrow_len
        ax.annotate(
            str(idx), xy=(vz, vy + y_edge), xytext=(vz, label_y),
            ha="center", va="bottom", fontsize=8, color=color, zorder=6,
            arrowprops={"arrowstyle": "->", "lw": 0.6, "color": color,
                        "shrinkA": 1.0, "shrinkB": 1.0},
        )
        if color == "red":
            ax.text(vz, vy - y_edge - arrow_len, "STOP", ha="center", va="top",
                    fontsize=7, color="red", zorder=6)

    # --- per-field chief + marginal rays (UN-SUPPRESSED for folds, nit 5a) -- #
    n_rays_drawn = 0
    if draw_rays and ray_data and ray_data.get("fields"):
        try:
            cmap = plt.get_cmap("tab10")
        except Exception:  # noqa: BLE001 — colormap lookup must never sink the draw
            cmap = None
        for fi, field in enumerate(ray_data["fields"]):
            color = cmap(fi % 10) if cmap is not None else "C{}".format(fi % 10)
            fy = field.get("field_y", 0.0)
            rays = field.get("rays", {})
            first = True
            for label in ("chief", "upper_marginal", "lower_marginal"):
                poly = rays.get(label) or []
                if len(poly) < 2:
                    continue
                zs_ray = [p[0] for p in poly]
                ys_ray = [p[1] for p in poly]
                # Legend attaches to the FIRST SURVIVING ray of the field (chief
                # if present, else a marginal) — on an obscured pupil the chief
                # truncates to nothing, so a chief-only label leaves the field
                # legend-less (empty-legend warning). Width still tracks ray type.
                lw = 1.0 if label == "chief" else 0.8
                legend = f"field Y={fy:g}" if first else "_nolegend_"
                first = False
                ax.plot(zs_ray, ys_ray, color=color, linewidth=lw, zorder=4,
                        label=legend)
                all_gz.extend(zs_ray)
                all_gy.extend(ys_ray)
                n_rays_drawn += 1
        if n_rays_drawn > 0:
            ax.legend(loc="upper right", fontsize=7, framealpha=0.8)

    # Folded banner — now an HONEST coherent global-frame figure (no "schematic" lie).
    ax.text(
        0.5, 0.97,
        "Folded system drawn in the GLOBAL frame (element vertices + rays share one "
        "coordinate frame). Open the .zmx for the native 3D layout.",
        transform=ax.transAxes, ha="center", va="top", fontsize=8, color="darkgreen",
        wrap=True, bbox={"boxstyle": "round", "facecolor": "honeydew", "alpha": 0.85},
        zorder=6,
    )

    ax.set_title(title)
    ax.set_xlabel("z (global optical axis)")
    ax.set_ylabel("y (global)")
    ax.set_aspect("equal", adjustable="datalim")
    # Limits from the actually-drawn global extents (with a pad). A degenerate/empty
    # extent falls back so the figure still renders.
    if all_gz and all_gy:
        z_lo, z_hi = min(all_gz), max(all_gz)
        y_lo, y_hi = min(all_gy), max(all_gy)
        z_pad = 0.08 * (z_hi - z_lo) if z_hi > z_lo else 1.0
        y_pad = 0.18 * (y_hi - y_lo) if y_hi > y_lo else 1.0
        # Headroom for the top label stack.
        y_hi = max(y_hi, y_hi + h_max * (_STAMP_ARROW_LEN_FRAC + 0.3))
        ax.set_xlim(z_lo - z_pad, z_hi + z_pad)
        ax.set_ylim(y_lo - y_pad - h_max, y_hi + y_pad)
    else:
        # Every drawable global frame was suppressed and no ray was drawn (a fully
        # degraded folded system). Set NEUTRAL limits so the figure still renders (never a
        # default auto-axis blank with a degenerate extent); the caller appends a
        # "no drawable folded geometry" flag from the empty surface_labels + 0 rays so the
        # ok:true result is honest about the blank figure (no overpromised fallback).
        ax.set_xlim(-1.0, 1.0)
        ax.set_ylim(-1.0, 1.0)

    return fig, surface_labels, stop_label, n_rays_drawn


def render_layout(session, params):
    """Render a meridional layout PNG from surface geometry. NEVER raises.

    Returns ``{ok:true, path, size_bytes, surface_labels, stop_label, folded,
    note, cb_suppressed, scaffold_suppressed}`` on success, or an ``ok:false`` envelope
    (``render_unavailable``/``render_gate_failed``/``render_failed``). The figure
    is always closed; the write is atomic (temp -> ``_is_png`` gate -> ``os.replace``).

    Coordinate-break AND flat powerless air dummy/spacer surfaces are SUPPRESSED
    (scaffolding — frame operators / spacers, not surfaces the beam lands on) — only real
    optics (glass, mirrors, curved lens-backs), the stop, and the image are stamped. The
    suppressed CB indices are disclosed in ``cb_suppressed`` (back-compat); the FULL
    suppressed set (CBs + flat-air dummies) is ``scaffold_suppressed``; both are noted.

    ``config`` (int) draws the figure at that multi-config
    configuration (inside a ``with_configuration`` wrap that ALWAYS restores), the title
    stamps ``[config k]``, and ``config_evaluated`` is echoed. ``"all"`` is NOT offered
    (no per-config figure set — the render-obscured-slow lock); a bad ``config`` ->
    ``render_failed``.
    """
    if not isinstance(params, dict):  # never-raise: None OR a truthy non-dict (§0.8, L26)
        params = {}
    # resolve the OPTIONAL single-config selector; stamp the title.
    try:
        cfg_idx = _resolve_render_config(session.system, params.get("config"))
    except ToolParamError as exc:
        return _fail("render_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — a config read fault -> render_failed
        return _fail("render_failed", f"could not resolve config: {exc!r}")

    if cfg_idx is None:
        result = _render_layout_at(session, params)
        if isinstance(result, dict) and result.get("ok"):
            try:
                result.setdefault(
                    "config_evaluated", _cfg.safe_current_configuration(session.system)
                )
            except Exception:  # noqa: BLE001 — disclosure only, never fail the render
                pass
        return result

    # Draw at config k inside a with_configuration wrap (ALWAYS restores the active
    # config). The title stamps [config k] so the figure is unambiguous.
    try:
        with _cfg.with_configuration(session.system, cfg_idx) as ctx:
            result = _render_layout_at(session, params, config_title=cfg_idx)
        if isinstance(result, dict) and result.get("ok"):
            result.setdefault("config_evaluated", cfg_idx)
            if not ctx["restore_verified"]:
                result["mutation_warning"] = ctx["mutation_warning"]
            if not ctx["switched"] and ctx["mutation_warning"]:
                result.setdefault("config_switch_warning", ctx["mutation_warning"])
        return result
    except Exception as exc:  # noqa: BLE001 — render never raises into dispatch
        return _fail("render_failed", f"{type(exc).__name__}: {exc}")


def _resolve_render_config(system, config):
    """Resolve a SINGLE-config selector (None|int) for render — NO ``"all"`` (D2/§2.7).

    Thin wrapper over the shared ``_config_common.resolve_single_config_selector`` (one
    contract). Returns the int config to draw at (``None`` -> the active config, no
    switch), or RAISES ``ToolParamError`` on ``"all"`` / a bad value (no per-config figure
    SET — the render-obscured-slow lock).
    """
    return _cfg.resolve_single_config_selector(system, config, "render_layout")


def _render_layout_at(session, params, config_title=None):
    """The pure per-config render body (draws at the ACTIVE config). See ``render_layout``.

    ``config_title`` (int|None) stamps a ``[config k]`` suffix on the figure title when a
    config was selected (so the figure is unambiguous about which config it shows).
    """
    title = params.get("title")
    if not isinstance(title, str) or title == "":
        title = _DEFAULT_TITLE
    if config_title is not None:
        title = f"{title} [config {config_title}]"
    # draw_rays default True (L9); a non-bool falls back to the default.
    draw_rays = params.get("draw_rays", True)
    if not isinstance(draw_rays, bool):
        draw_rays = True

    attempted = None
    fig = None
    plt = None
    tmp = None
    try:
        # Resolve geometry FIRST so an import failure does not depend on the LDE.
        try:
            plt, np = _import_mpl()
        except BaseException as exc:  # noqa: BLE001 — broken mpl -> render_unavailable
            return _fail(
                "render_unavailable",
                f"matplotlib/numpy unavailable: {type(exc).__name__}: {exc}",
            )

        lde = session.system.LDE
        n = int(lde.NumberOfSurfaces)

        # Need at least one OPTICAL surface to draw (not object/image only).
        optical_count = max(0, n - 2)
        if optical_count < 1:
            return _fail(
                "render_unavailable",
                f"no drawable surfaces (system has {n} surface(s); needs >= 1 "
                "optical surface between object and image)",
            )

        rows, degraded = _read_all_geometry(lde, n, system=session.system)

        # Classify roles via the SHARED classifier -> fold detection.
        folded = False
        stop_index = None
        for i, r in enumerate(rows):
            role = _geom.classify_role(
                i, n, r["type_name"], r["material"], r["is_stop"]
            )
            if role in ("coordinate-break", "mirror"):
                folded = True
            if stop_index is None and r["is_stop"]:
                stop_index = i

        # Aperture heights with the max-semi / 1.0 fallback (§3.2).
        heights, all_zero_semi = _geom.resolve_aperture_heights(
            [r["semi_diameter"] for r in rows]
        )

        attempted = _resolve_path(session, params.get("path"))

        # --- read the per-field rays (never raises; degrades to geometry-only). --
        # a FOLDED system now draws its ELEMENTS in the GLOBAL
        # frame (GetGlobalMatrix vertices) — the SAME frame the RAGY/RAGZ rays use — so
        # the rays are UN-SUPPRESSED for folds (the coherence is the whole point). The
        # only remaining ray-suppress is the AXIAL-DEGRADE case (H-1) for the UNFOLDED
        # path: a degraded row with a non-finite thickness collapses `vertex_z` and
        # shifts every downstream surface, but the rays read the TRUE cumulative RAGZ ->
        # the overlay drifts off the (unfolded) elements. The folded path draws in the
        # global frame and reads each surface's degrade independently (a per-surface
        # global-frame suppress), so the axial-degrade ray suppress does NOT apply there.
        axial_degraded = [
            i for i in degraded
            if not math.isfinite(rows[i].get("thickness", float("nan")))
        ]

        # For a folded system, read the per-surface GLOBAL frames (the honest
        # fold-coordinate channel — GetGlobalMatrix[10:13] + the row-major rotation).
        global_frames = None
        if folded:
            global_frames = _geom.read_global_frames(lde, n)

        ray_data = None
        ray_flags = []
        # Rays draw whenever requested AND not blocked by the unfolded axial-degrade
        # case. Folded systems NO LONGER suppress rays (nit 5a).
        suppress_axial = bool(axial_degraded) and not folded
        effective_draw_rays = bool(draw_rays) and not suppress_axial
        if effective_draw_rays:
            try:
                # A3: time-bound the ray loop — a perf_counter deadline checked BETWEEN
                # batch opens (the env budget is clamped positive-finite). A healthy
                # trace is < 0.05 s, so the budget never truncates a healthy system; it
                # bounds a contention-stuck trace to one stuck open.
                deadline = perf_counter() + _ray_budget_s()
                ray_data = _rays.read_field_rays(
                    session.system, wave=1, deadline=deadline
                )
            except BaseException as exc:  # noqa: BLE001 — belt-and-braces; reader is wrapped
                ray_data = {
                    "fields": [],
                    "flags": [f"ray trace unavailable: {type(exc).__name__}: {exc}"],
                }
            ray_flags = list(ray_data.get("flags", []))
        elif draw_rays and suppress_axial:
            ray_flags = [
                "rays suppressed: degraded axial geometry (surfaces "
                f"{axial_degraded} have non-finite thickness; vertex registration "
                "unreliable)"
            ]

        if folded:
            # The global-frame coherent folded figure.
            fig, surface_labels, stop_label, n_rays_drawn = _draw_folded_global(
                plt, np, rows, n, title, stop_index, heights,
                all_zero_semi, degraded, global_frames, ray_data,
                effective_draw_rays,
            )
        else:
            # The all-refractive UNFOLDED path — UNCHANGED (no regression).
            fig, surface_labels, stop_label, n_rays_drawn = _draw(
                plt, np, rows, n, title, stop_index, folded, heights,
                all_zero_semi, degraded, ray_data, effective_draw_rays,
            )

        # Atomic durability gate (§3.8): UNIQUE temp in the target dir -> _is_png ->
        # replace. A unique temp (tempfile.mkstemp) avoids the race where two
        # concurrent renders to the same path collide on a fixed "<name>.png.tmp" and
        # one returns ok:true for the other's figure (FIX 6).
        directory = os.path.dirname(attempted) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=directory)
        os.close(fd)  # mkstemp opens the file; savefig reopens by path.
        fig.savefig(tmp, dpi=120, format="png")
        # Close the figure NOW (before the gate / replace) — leak-safe even on the
        # gate-fail return path. (Also closed in finally as a backstop.) A teardown
        # failure must NEVER mask the gate result, so the inline close is guarded;
        # if it raises, fig stays non-None and the finally backstop retries it.
        try:
            plt.close(fig)
            fig = None
        except Exception:  # noqa: BLE001 — teardown must never mask the gate result
            pass

        if not _is_png(tmp):
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            tmp = None
            return _fail(
                "render_gate_failed",
                "the written figure failed the PNG-magic gate (not a real PNG)",
                path=attempted,
            )

        os.replace(tmp, attempted)
        tmp = None
        size = os.path.getsize(attempted)

        # Compose the caveat note (folded / degenerate). The ray flags live ONLY in
        # the additive `flags` list (NOT `note`) so `note` stays back-compatible — a
        # geometry-only system without a wired MFE never pollutes the human note.
        # Scaffold suppression (drawing concern only): coordinate-break frame surfaces
        # AND flat powerless air dummies/spacers are NOT drawn/stamped (neither is a
        # surface the beam lands on). Computed from the rows (via the SAME predicate the
        # draw paths use) so it is honest regardless of which path drew. Both are additive,
        # non-breaking disclosures: `cb_suppressed` (CB indices only, back-compat) and
        # `scaffold_suppressed` (ALL suppressed indices = CBs + flat-air dummies).
        cb_suppressed = [
            i for i in range(n) if _geom._is_coordinate_break(rows[i]["type_name"])
        ]
        scaffold_suppressed = [
            i
            for i in range(n)
            if i != 0 and i != n - 1 and _is_suppressed_scaffold(rows, i, n)
        ]
        # SAGMATH sag disclosure (REAL geometry — replaces the S1
        # flag-only ``asphere_sag_approximate``): the drawn curve for an EvenAspheric
        # surface is now the TRUE polynomial profile (sphere + conic base + Σα₂ₙy²ⁿ) when
        # its coefficients were read. So the figure IS faithful for a modelled asphere.
        #
        # The disclosure splits the EvenAspheric surfaces by whether their sag was MODELLED:
        #   - MODELLED (coefficients read OK) -> ``asphere_sag_modelled`` (the drawn profile
        #     is the full asphere shape, no caveat).
        #   - UNREADABLE (a degraded/throwing coefficient cell, or a wedged row that could be
        #     an asphere) -> FAIL-CLOSED ``asphere_sag_approximate``: the curve fell back to
        #     the sphere+conic base only because the polynomial term could not be read; the
        #     drawn profile is approximate, NEVER silently presented as faithful.
        asphere_sag_modelled = []
        asphere_sag_approximate = []
        for i in range(n):
            if rows[i].get("unreadable", False):
                # The geometry read threw — we could not characterize this surface's Type.
                # It MIGHT be an asphere drawn from the base only; disclose conservatively.
                asphere_sag_approximate.append(i)
                continue
            if _asph.asphere_type_of_name(str(rows[i].get("type_name", ""))) is None:
                continue  # not a Tier-1 asphere
            coeffs = rows[i].get("aspheric_coefficients")
            if isinstance(coeffs, (list, tuple)) and not rows[i].get(
                "coefficients_unreadable", False
            ):
                asphere_sag_modelled.append(i)
            else:
                asphere_sag_approximate.append(i)

        notes = []
        # The full scaffold-suppression disclosure (CBs + flat powerless air dummies),
        # emitted on EITHER path (folded or unfolded) whenever anything was dropped. An
        # all-refractive system suppresses nothing -> no note (no regression).
        if scaffold_suppressed:
            notes.append(
                f"non-optical surfaces {scaffold_suppressed} suppressed from the figure "
                "(coordinate breaks + flat powerless air dummies — frame operators / "
                "spacers, not surfaces the beam lands on)"
            )
        if asphere_sag_modelled:
            notes.append(
                f"surfaces {asphere_sag_modelled} are even aspheres drawn from the FULL "
                "sag (sphere + conic + polynomial term) — the drawn profile is faithful"
            )
        if asphere_sag_approximate:
            # FAIL-CLOSED honesty: every entry here is a surface whose even-asphere
            # polynomial term could NOT be read (a degraded/throwing coefficient cell, OR a
            # wedged row that could be an asphere). The drawn curve fell back to the
            # sphere+conic base only — disclose it, never present it as faithful. Split the
            # wording the same way clearance.py does: a READABLE EvenAspheric whose coeffs
            # were unreadable gets the definite note; an UNREADABLE/degraded row gets a
            # HEDGE ("could not be characterized … MAY be an even asphere").
            readable_asph = [
                i for i in asphere_sag_approximate
                if not rows[i].get("unreadable", False)
            ]
            degraded_asph = [
                i for i in asphere_sag_approximate
                if rows[i].get("unreadable", False)
            ]
            if readable_asph:
                notes.append(
                    f"surfaces {readable_asph} are even aspheres but their coefficients "
                    "could not be read — drawn from the sphere+conic base ONLY; the drawn "
                    "profile is approximate — open the .zmx for the true asphere shape"
                )
            if degraded_asph:
                notes.append(
                    f"surfaces {degraded_asph} could not be characterized (geometry "
                    "read failed) and MAY be even aspheres; if so the drawn profile is "
                    "approximate (sphere+conic base only) — open the .zmx to verify"
                )
        if folded:
            # the folded figure is now drawn COHERENTLY in the global
            # frame (elements + rays share one frame), so the note is honest about that
            # — no longer the "axial layout past the fold is schematic" caveat. A
            # surface whose global frame was degraded is surfaced via `degraded`.
            notes.append(
                "Folded system drawn in the global frame (element vertices from "
                "GetGlobalMatrix coherent with the RAGY/RAGZ rays). Open the .zmx for "
                "the native 3D layout."
            )
            if global_frames is not None:
                # Scan through n-1 (INCLUSIVE) so a degraded IMAGE-plane frame
                # (whose plane line/callout was suppressed) is disclosed too — the prior
                # range(1, n-1) stopped at n-2 and left a suppressed image plane silent.
                suppressed = [
                    i for i in range(1, n)
                    if i < len(global_frames) and not global_frames[i].get("ok")
                ]
                if suppressed:
                    notes.append(
                        f"surfaces {suppressed} had an unreadable/degraded global frame "
                        "and were suppressed from the figure"
                    )
        if all_zero_semi:
            notes.append("all semi-diameters read 0/non-finite; used h=1.0 fallback")
        if degraded:
            notes.append(f"surfaces {degraded} were unreadable and degraded")
        flags = list(ray_flags)  # additive machine-readable channel (rays + geometry)

        # A folded figure where TRULY NOTHING drew is a blank (neutral-axis)
        # render — flag it so the ok:true result is honest about the empty figure.
        # The flag must NOT over-fire when ONLY the image-plane line
        # drew (it is drawn independently from `_draw_folded_global` and does NOT append
        # to `surface_labels`). So in addition to "no optical surfaces + no rays", require
        # the IMAGE frame to ALSO be degraded — if the image plane line drew, the figure is
        # not blank. (A folded system with every OPTICAL surface suppressed but a readable
        # IMAGE frame still draws the optical-axis + image-plane line, so it is not "nothing
        # drawable".)
        image_frame_ok = (
            global_frames is not None
            and (n - 1) < len(global_frames)
            and bool(global_frames[n - 1].get("ok"))
        )
        if folded and not surface_labels and n_rays_drawn == 0 and not image_frame_ok:
            flags.append(
                "no drawable folded geometry after global-frame suppression — the figure "
                "is blank (every surface frame was degraded and no ray was drawn)"
            )

        n_fields = len(ray_data.get("fields", [])) if ray_data else 0

        # Color-recycle flag (L6): tab10 only has 10 distinct colors, so when
        # n_fields > 10 the per-field color recycles via `fi % 10` and two fields
        # share a color. Warn the user only when rays were actually drawn.
        if n_rays_drawn > 0 and n_fields > 10:
            flags.append(
                f"color recycle: {n_fields} fields exceed the 10-color palette "
                "(fi % 10) — two fields now share a color"
            )

        note = " | ".join(notes) if notes else None

        return {
            "ok": True,
            "path": attempted,
            "size_bytes": size,
            "surface_labels": surface_labels,
            "stop_label": stop_label,
            "folded": folded,
            "note": note,
            # Additive (non-breaking): the coordinate-break surface indices suppressed
            # from the figure (frame operators, not drawn). Empty for a CB-free system.
            # Kept for back-compat — a SUBSET of scaffold_suppressed (CBs only).
            "cb_suppressed": cb_suppressed,
            # Additive (non-breaking): the FULL suppressed set = CBs + flat powerless air
            # dummies/spacers (the indices absent from the figure stamps). Empty for an
            # all-refractive system (every surface curved -> nothing suppressed).
            "scaffold_suppressed": scaffold_suppressed,
            # (additive, non-breaking): the EvenAspheric surface numbers
            # whose drawn curve is the FULL sag (sphere + conic + polynomial term) — the
            # figure IS a faithful asphere profile for these (the S1 disclosure REPLACED by
            # real geometry). Empty for an all-spherical system.
            "asphere_sag_modelled": asphere_sag_modelled,
            # Fail-closed disclosure: EvenAspheric surfaces whose coefficients could NOT be
            # read (a degraded/unreadable row) — drawn from the sphere+conic base only, so
            # the curve is approximate, NEVER silently presented as faithful. Empty when
            # every asphere modelled cleanly / an all-spherical system.
            "asphere_sag_approximate": asphere_sag_approximate,
            # --- additive (non-breaking) keys for the ray overlay (L10) ---
            "png_valid": _is_png(attempted),
            "draw_rays": bool(draw_rays),
            # effective_draw_rays = whether rays were ACTUALLY drawn (True only when
            # not suppressed AND at least one ray polyline plotted); draw_rays stays
            # the REQUESTED echo (H-2).
            "effective_draw_rays": bool(effective_draw_rays and n_rays_drawn > 0),
            "n_fields": n_fields,
            "n_rays_drawn": n_rays_drawn,
            "flags": flags,
        }
    except BaseException as exc:  # noqa: BLE001 — render never raises into dispatch
        return _fail(
            "render_failed",
            f"{type(exc).__name__}: {exc}",
            path=attempted,
        )
    finally:
        # Always close the figure (matplotlib leaks figures globally otherwise).
        if fig is not None and plt is not None:
            try:
                plt.close(fig)
            except Exception:  # noqa: BLE001 — teardown must never raise
                pass
        # Clean a leftover temp on any unexpected exit path.
        if tmp is not None:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass


RENDER_LAYOUT_SPEC = ToolSpec(
    name="render_layout",
    handler=render_layout,
    required_params=(),
    param_types={"title": "string", "path": "string", "draw_rays": "boolean",
                 "config": "number"},
    description=(
        "Draw a real meridional (y-z) optical layout PNG for the user: equal-aspect "
        "(true curvature), cement-aware closed element outlines, the optical axis + "
        "image plane, a STOP marker, OUR stamped surface numbers (how the user points "
        "at a surface), and — unless draw_rays=False — the chief + upper/lower marginal "
        "ray of each field (one color per field). Returns the saved PNG path plus "
        "png_valid/n_fields/n_rays_drawn/cb_suppressed/scaffold_suppressed/flags; inspect "
        "result.ok. Folded systems (coordinate break / mirror) are drawn in the GLOBAL "
        "frame, coherent with the rays — so the rays ARE drawn for a fold; coordinate-break "
        "and flat powerless air dummy/spacer surfaces are suppressed (scaffolding, not "
        "drawn); real optics (glass, mirrors, curved lens-backs), the stop, and the image "
        "are stamped with their true Zemax numbers. Gotcha: this is a self-drawn headless "
        "figure (native export writes text, not an image) — for native fidelity, open "
        "the saved .zmx. See describe_surfaces, fold_beam."
    ),
)

TOOL_SPECS = (RENDER_LAYOUT_SPEC,)
