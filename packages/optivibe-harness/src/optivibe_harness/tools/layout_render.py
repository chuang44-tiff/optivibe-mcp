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
from dataclasses import dataclass as _dataclass
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
# The two-slot stagger above cannot separate three labels at ANY threshold, so
# labels whose RENDERED text boxes still overlap step OUTWARD in tiers on their
# own side: tier k sits k label-heights beyond tier 0 and its leader lengthens to
# match. The gap is the clear space BETWEEN two stacked labels; the cap bounds how
# tall a stack may grow (a taller stack pushes the axes limits out and shrinks the
# optics in frame), so a collision deeper than the cap keeps a residual overlap
# rather than crushing the drawing. XPAD is the minimum clear space in x: two
# labels that merely TOUCH read as one number (`20` beside `22` renders the string
# `2022`), so touching counts as colliding.
#
# XPAD IS JUSTIFIED BY CORPUS MEASUREMENT AND IS NOT PINNED BY AN OFFLINE TEST.
# Setting it to 0 leaves the whole unit suite GREEN, and deliberately so: after
# tiering, no two labels on the offline fixtures share a tier at all, so a
# same-row clear GAP is unobservable there and a fixture built only to exercise
# a 2 pt pad would be test surface for its own sake. The evidence is the corpus:
# at 2.0 pt the reference designs hold 0 overlapping pairs and 2 pairs within
# 2 pt; at 1.0 pt, 0 overlapping and 7 within, at IDENTICAL frame area (the
# subject-area cost is caused by the tiering, not by the pad). Re-measure before
# changing it; do not infer from a green suite that it does nothing.
_STAMP_TIER_GAP_PT = 2.0
_STAMP_TIER_XPAD_PT = 2.0
_STAMP_TIER_MAX = 6
# Tiering and framing feed back into each other (a taller stack -> wider
# limits -> fewer pixels per data unit -> a taller stack still needed), so the
# pass runs to a fixed point. Bounded: a non-converging figure ends up with the
# last assignment, never a hang.
#
# THE PASS COUNT AND THE X-PAD ARE CORPUS-JUSTIFIED **AND OFFLINE-REDDENABLE**
# — an earlier revision of this comment claimed they were not, and that was an
# OVERCLAIM: they were untested, not untestable. What the SHIPPED fixtures could
# not do, a fixture at corpus DENSITY can, because density is what makes the
# feedback loop bite (a taller stack widens the limits, which under
# equal-aspect-by-box shrinks pixels-per-data-unit, which needs a taller stack
# still) and the sparse fixtures' limits barely move. Measured on
# `_dense_stack_rows` (test D-1): baseline 0 overlapping pairs / 0 within 2 pt;
# `_STAMP_TIER_PASSES = 1` -> 3 and 5; `_STAMP_TIER_XPAD_PT = 0` -> 0 and 8;
# `_STAMP_TIER_GAP_PT = 0` -> 0 and 1. At corpus scale 1 pass clears every
# OVERLAP but leaves 7 pairs within 2 pt where 3 passes leave 2.
#
# NOT PRESENTLY COVERED (stated as coverage, not as impossibility): the post-tier
# re-frame call inside `_draw` — removing it stays green because
# `_expand_limits_to_drawn_text` catches the residue — and the number+word UNION
# in `_tier_colliding_stamps`, which still reads 0/0 on the dense fixture. The
# union's PREDICATE is pinned directly by test U-3, which is the part a unit test
# can reach; nobody has yet built a fixture that discriminates its EFFECT.
_STAMP_TIER_PASSES = 3
# STOP must read as ATTACHED to its own number, so its gap to that number is
# DELIBERATELY TIGHTER than the gap between two stacked labels (0.5 pt vs the
# 2.0 pt tier gap). The rider also OCCUPIES the tier immediately outboard of its
# number — without that reservation a neighbour bumped one tier up lands exactly
# on the word, because the word is sitting in that tier's band.
_STOP_RIDER_GAP_PT = 0.5
#: boxstyle pad (font-size units) for the white box behind a word-carrying block.
#: Sized for LEGIBILITY, not for tidiness: the box exists because the IMAGE
#: number is drawn ON the image-plane line (a vertical stroke at the same z, by
#: construction — the number labels that surface) inside the converging ray fan,
#: and a digit read through two crossing strokes is a digit a vision model reads
#: wrong. A pad that merely hugs the glyphs leaves the line cutting through them
#: and fixes nothing; this clears the line on both sides at 7-8 pt. It is NOT
#: free — an opaque box hides whatever it covers, and the rays are the figure's
#: payload, so it is deliberately the smallest pad that clears the stroke.
_RIDER_BOX_PAD = 0.3
#: Clear space (pt) between an unmeasured surface's vertex number and the word
#: block below it, and the increment by which that block steps further out when
#: it lands on the payload. DISPLAY-SPACE quantities — points and measured text
#: heights only — so nothing here derives from an aperture (a fabricated
#: height may not position an artist).
_VERTEX_WORD_GAP_PT = 2.0
#: How many steps the block may take. Bounded so an unclearable figure yields a
#: slightly-worse label, never a hang. Measured on a degraded-stop doublet,
#: step 4 is clear of both the payload and every label's ink.
_VERTEX_WORD_MAX_STEPS = 5
#: gids for the two boxed text families (see `_place_disclosures._box`).
_DISCLOSURE_GID = "disclosure:box"

# The word STOP RIDES its own red surface-number stamp — one short arm further
# out along the SAME local normal — instead of being placed independently at the
# bottom of the frame. The red number already has a leader pointing at the stop
# surface; that leader is the connection STOP was missing. Measured on the zoom,
# the free-standing text sat ~46 data units from the surface it named, on the
# opposite side of the axis, hard against the S-SCOPE footer. Riding the stamp
# means it moves through the tiering pass with its number, so it can never
# collide with the footer, strand itself at the frame edge, or need collision
# logic of its own. This fraction is the PRE-TIERING arm; once the tiering pass
# measures a real label height it hands back the exact one-label-height step.
_STOP_RIDER_ARM_FRAC = 0.14

# The default ray-trace budget (seconds) for the explicit render_layout ray overlay
# (render-obscured-slow A3). A healthy full trace is < 0.05 s; the reported stall was
# 233 s, so 12 s never truncates a healthy system but bounds a contention-stuck trace.
_DEFAULT_RAY_BUDGET_S = 12.0

# =========================================================================== #
# The style layer — a look BORROWED from
# optical drawing practice, in the "ISO 10110-inspired" sense and no stronger.
# Nothing here is a conformance claim; see the served description.
#
# EXACTLY TWO line weights, and no third. Every stroke this module draws picks
# one of the two constants below. The 2:1 ratio is OUR simplification of the
# 1.89:1 the standard's own artwork measures, stated as ours.
# =========================================================================== #
_WIDE_LINE_PT = 1.0        # outline weight
_NARROW_LINE_PT = 0.5      # everything else — axis, interfaces, leaders, rays

# Long-dash double-dot: the `05.1` optical-axis line TYPE in intent. The dash /
# gap / dot ELEMENT LENGTHS are OPTIVIBE-OWNED — the dash-element geometry annex
# (ISO 128-2 Annex A) is unread, so no length here is quoted from any source.
_AXIS_DASHES = (0, (12, 3, 1, 3, 1, 3))
# The NOT-MEASURED body stroke pattern — also OptiVibe's own (nothing sourced
# tells us how to mark an unmeasured body without hatch, and hatch is excluded
# whole-drawing).
_NOT_MEASURED_DASHES = (0, (4, 3))
_NOT_MEASURED_GREY = "0.45"

# Legend swatch width. LEGEND CHROME, deliberately outside the two-weight
# claim (which scopes to the drawing's own Line2D and patch families). No
# DRAWN artist uses it -- the rays stay narrow; only the legend key is made
# readable. Measured: at the narrow weight the middle (orange) swatch is
# present and the right colour but so low-contrast on the legend panel that a
# reader cannot map a ray colour to its field, i.e. the legend stops doing
# its one job.
_LEGEND_SWATCH_PT = 1.5

_DISCLOSURE_FONT_PT = 6.5
_SCOPE_FONT_PT = 6.0
#: gid for the S-SCOPE footer. It is a FIGURE-level caption, NOT a member of the
#: top-left disclosure stack, and it carries its own gid so the stack — which
#: identifies its members by `_DISCLOSURE_GID` precisely so nothing else can be
#: absorbed into it — can never count it.
_SCOPE_GID = "disclosure:footer"
#: Clear space (pt) between the axes' own bottom furniture (the x tick labels
#: and the x-label) and the caption seated below it.
_FOOTER_GAP_PT = 6.0
#: Deterministic figure-coordinate seat for the footer, used when the canvas
#: cannot be measured. Bottom-right of the figure, below every axes.
_FOOTER_FALLBACK_XY = (0.995, 0.005)
_DISCLOSURE_GAP_PT = 4.0
_DISCLOSURE_X = 0.02          # axes coords — the stack's left anchor
_DISCLOSURE_TOP = 0.98        # axes coords — the stack's top anchor
_DISCLOSURE_FLOOR = 0.02      # axes coords — below this a box has left the canvas
#: boxstyle pad, in units of the font size. ONE definition: the placement
#: maths and the drawn rectangle must not be able to disagree about the size.
_DISCLOSURE_BOX_PAD = 0.35


# --------------------------------------------------------------------------- #
# Locked figure strings — VERBATIM. These are contract, not suggestion:
# they are asserted character for character. Do not paraphrase.
# --------------------------------------------------------------------------- #
_S_FOLD = (
    "FOLDED CROSS-SECTION — global y-z projection; elements and rays share one "
    "global frame."
)
_S_FOLD_CLR = "CLEARANCE NOT MEASURED — folded_gaps_not_audited."
_S_OOP = (
    "OUT-OF-PLANE GEOMETRY — this 2-D global y-z projection discards x; apparent "
    "overlaps and clearances are NOT MEASURED."
)
_S_PROJ_UNK = (
    "PROJECTION STATUS NOT MEASURED — out-of-plane coordinate-break terms could "
    "not be read."
)
_S_PROJ_TYPE = (
    "PROJECTION STATUS NOT MEASURED — S{k} surface type is outside the projection "
    "check's domain."
)
_S_CFG = "CONFIGURATION {k} OF {N} — one active configuration shown."
_S_CFG_UNK = (
    "CONFIGURATION NOT MEASURED — active index or configuration count could not "
    "be read."
)
_S_AP = "S{k} APERTURE NOT MEASURED — gap_edge_not_measurable."
_S_AP_INT = (
    "S{k} INTERNAL INTERFACE NOT DRAWN — aperture not measured "
    "(gap_edge_not_measurable)."
)
_S_OVERFLOW = "{n} MORE DISCLOSURES NOT SHOWN — full list in the result."
_S_SAG = (
    "S{k} PROFILE NOT MEASURED — sag inputs unreadable (radius/conic/coefficients)."
)
_S_SCOPE = (
    "projection check: prescription-level only (CB terms + surface types); "
    "ray-plane departure not checked."
)
_S_GRP_PLACEHOLDER = (
    "S{a}-S{b} APERTURES NOT MEASURED — gap_edge_not_measurable; body drawn at "
    "placeholder height, not a measurement."
)
_S_GRP_OPEN = (
    "ELEMENT OUTLINE NOT CLOSED — gap_edge_not_measurable: S{k} aperture not "
    "measured."
)

#: The shipped coverage-reason token every aperture-provenance disclosure quotes.
_REASON_APERTURE = "gap_edge_not_measurable"
_REASON_FOLDED = "folded_gaps_not_audited"
_REASON_GRIN = "grin_not_audited"


@_dataclass(frozen=True)
class ConfigIdentity:
    """The READ-BACK multi-configuration identity — disclosure only.

    ``active`` is the configuration the figure was drawn at; ``count`` is how many
    exist. Either may be ``None`` — that is "could not be read", and it NEVER
    defaults to 1. This replaces nothing: no drawing decision depends on it.
    """

    active: "int | None"
    count: "int | None"


def _read_config_identity(system):
    """Guarded read of the MCE current-configuration / configuration-count pair.

    Never raises; never refuses; **never defaults a failed read to 1** — an
    unreadable identity is reported as unreadable and disclosed on the figure.
    """
    try:
        mce = system.MCE
    except Exception:  # noqa: BLE001 — no editor / a wedged read is "not measured"
        return ConfigIdentity(active=None, count=None)
    count = None
    active = None
    try:
        count = int(mce.NumberOfConfigurations)
    except Exception:  # noqa: BLE001
        count = None
    try:
        active = int(mce.CurrentConfiguration)
    except Exception:  # noqa: BLE001
        active = None
    return ConfigIdentity(active=active, count=count)


class _ProjectionReader:
    """Adapter handing ``read_projection_state`` BOTH seams it needs.

    ``_layout_geometry.read_projection_state`` reads surface rows off its first
    argument (``GetSurfaceAt``) AND passes that same argument to
    ``_cb_cells.read_cb_cell``, which resolves the ``SurfaceColumn`` enum off it.
    The live LDE satisfies the first and the live enum namespace satisfies the
    second by import, but a session that INJECTS its enum types carries them on
    the SYSTEM, not the editor — so handing the bare editor over makes every
    coordinate-break cell read fail and the projection state reads "not measured"
    on a system whose terms are perfectly readable.

    This adapter forwards ``GetSurfaceAt`` to the editor and every other attribute
    (notably ``_enum_types``) to the system, so the predicate is exercised for
    real. Read-only; it exposes no mutator.
    """

    def __init__(self, lde, system):
        self._lde = lde
        self._system = system

    def GetSurfaceAt(self, i):
        return self._lde.GetSurfaceAt(i)

    def __getattr__(self, name):
        return getattr(self._system, name)


def _optical_indices(rows, n):
    """The drawable optical surface indices — object, image and scaffold excluded.

    ONE definition consumed by both draw paths and by the disclosure-eligibility
    set, so the figure and the result dict cannot disagree about the OPTICAL-LOOP
    surfaces — the ones both sides derive from THIS list.

    **The claim stops there, deliberately.** This is NOT the whole set of drawn
    surfaces: `_glass_groups` takes the optical set and never reads it (see the
    note on that function), so a GROUP BODY CAP can be drawn from a surface this
    list excludes. That second source is reconciled downstream —
    `_aperture_disclosure_indices` unions in `drawn_surfaces` and the sag scan
    follows the same union — and stating "can never disagree" without naming the
    cap source invites someone to delete exactly the union that makes it true.
    """
    return [
        i
        for i in range(n)
        if i != 0 and i != n - 1 and not _is_suppressed_scaffold(rows, i, n)
    ]


def _aperture_disclosure_indices(optical_indices, stop_index, n, drawn_surfaces=()):
    """THE aperture-disclosure eligibility set.

    ``optical_indices ∪ drawn_surfaces ∪ {stop_index} ∪ {n − 1}``, minus surface 0.

    ``drawn_surfaces`` is the load-bearing addition, and it is still ONE rule, not
    a second one: a surface is eligible iff its aperture can POSITION DRAWN
    GEOMETRY. ``optical_indices`` was assumed to cover that and does not —
    ``_glass_groups`` takes the optical set and never reads it, so a surface
    suppressed from the STAMPS is still taken as a cemented group's cap and
    drawn. On a plano singlet (and every window, cover glass and field flattener)
    the flat back IS the element's back face: the figure would draw it, disclose
    it by name when its aperture was unreadable, and the result dict would
    meanwhile report that every aperture was measured. An agent reads the result
    dict. Eligibility therefore follows what the drawing actually consumed.

    - The OBJECT is NEVER eligible: its semi routinely reads ``inf`` on an
      infinite conjugate and it positions no drawn outline, tick or leader, so
      disclosing it would put a false alarm on every healthy figure.
    - The IMAGE surface IS eligible — the zero-semi image defect lives there and
      ``optical_indices`` excludes it.
    - Suppressed scaffold is excluded through ``optical_indices``; a
      ``normalize_stop`` dummy stop is suppression-exempt so it stays eligible.

    ONE derived set feeds the result list, the ``?`` marks and the figure strings.
    No second membership rule may exist.
    """
    eligible = {int(i) for i in optical_indices}
    eligible.update(int(i) for i in drawn_surfaces)
    if stop_index is not None:
        try:
            eligible.add(int(stop_index))
        except (TypeError, ValueError):
            pass
    if n >= 1:
        eligible.add(int(n) - 1)
    eligible.discard(0)
    return tuple(sorted(eligible))


def _sag_inputs_unreadable(row):
    """True iff this row's sag INPUTS are unreadable.

    A ``nan`` radius, a non-finite conic, or any non-finite polynomial
    coefficient. **``inf`` is a VALID plane and is NOT flagged** — it is the
    encoding of "flat", not of "unreadable", and treating it as a fault would
    disclose three of every nine ordinary surfaces.

    Disclosure ONLY: the profile is still drawn through the legacy fallback this
    cycle. Never raises.
    """
    try:
        r = float(row.get("radius", float("nan")))
    except (TypeError, ValueError):
        return True
    if math.isnan(r):
        return True
    try:
        k = float(row.get("conic", 0.0))
    except (TypeError, ValueError):
        return True
    if not math.isfinite(k):
        return True
    coeffs = row.get("aspheric_coefficients")
    if isinstance(coeffs, (list, tuple)):
        for c in coeffs:
            try:
                cf = float(c)
            except (TypeError, ValueError):
                return True
            if not math.isfinite(cf):
                return True
    return False


def _prepare_draw_geometry(rows, n):
    """Resolve the aperture RECORDS, the group rims and the ONE draw-height map.

    The single preparation both draw paths consume. ``draw_heights`` is the only
    height the drawing code may read: it covers every surface, clamps a
    non-measured member to its group's rim, and is computed once.
    """
    apertures = _geom.resolve_aperture_records([r["semi_diameter"] for r in rows])
    groups = _glass_groups(rows, n, _optical_indices(rows, n))
    rims, draw_heights = _geom.resolve_group_rims(apertures, groups)
    return apertures, rims, draw_heights


def _plot_h_max(draw_heights, rims, drawn_surfaces):
    """The half-height the PLOT LIMITS scale from — placeholders INCLUDED.

    Deliberately placeholder-inclusive: a non-measured height is sanctioned as
    "scaling/compatibility data", and framing is exactly that. A placeholder
    body is DRAWN, so the window has to contain it. Never use this for anything
    that becomes an artist coordinate — that is ``_plot_geom_height``.

    Restricted to DRAWN surfaces, which the spec's own
    ``max(draw_heights.values())`` is not. Provenance is not the only axis here:
    a finite-conjugate OBJECT carries a real 60 mm aperture, is never drawn, and
    framing to it left a ±9 mm lens occupying 15 % of its own figure. "Include
    placeholders" is about PROVENANCE; it was never a licence to frame to
    something the figure does not contain.
    """
    drawn = {int(i) for i in drawn_surfaces}
    candidates = [1.0]
    candidates.extend(float(v) for k, v in draw_heights.items()
                      if int(k) in drawn)
    candidates.extend(float(r.rim_height) for r in rims)
    return max(candidates)


def _plot_geom_height(apertures, drawn_surfaces):
    """The half-height DRAWN GEOMETRY scales from — MEASURED apertures only.

    The uniform leader length and the stop tick's half-length are both derived
    from a scale, and both end up as artist coordinates: a fabricated height
    is forbidden from reaching either. One scalar was serving both jobs
    with opposite provenance requirements, so a fabricated aperture moved every
    label anchor and every tick, and a finite-conjugate OBJECT — never drawn —
    set the whole drawing's scale (60 mm object, a ±9 mm lens).

    Two SEPARATELY NAMED scalars rather than one corrected one: a second scalar
    of the same shape invites the next call site to grab the wrong one, and the
    name is what makes that visible in review.

    Two filters, not one. MEASURED closes the fabrication half. **DRAWN closes
    the other half, and it is not optional:** a finite-conjugate OBJECT carries
    a perfectly real, perfectly MEASURED 60 mm aperture and is never drawn, so
    a measured-only scalar still lets it set the scale for a +/-9 mm lens —
    measured, with no monkeypatching, leaders 42 long on a 9 lens. Provenance
    alone is not enough; the question is what the drawing contains.

    Provenance already rides every record, so this is a read, not a
    recomputation. Falls back to 1.0 when nothing drawn was measured — the
    all-zero system, whose stamps anchor at the vertex and draw no leader.
    """
    drawn = {int(i) for i in drawn_surfaces}
    return max(
        (float(a.height) for a in apertures
         if a.measured and int(a.surface) in drawn),
        default=1.0,
    )


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


def _edge_sag(np, rows, i, half_height):
    """The LOCAL sag at surface ``i``'s own aperture edge — ``(upper, lower)``.

    Read from the LAST / FIRST VALID sample of the surface's own profile, so a cap
    whose aperture-edge samples masked out anchors at the last point that was
    actually computed rather than at a NaN. A fully masked profile yields ``0.0``.
    """
    y = np.linspace(-float(half_height), float(half_height), _N_SAMPLES)
    z, valid = _geom.sag_profile(
        rows[i]["radius"], rows[i]["conic"], y,
        coeffs=rows[i].get("aspheric_coefficients"),
        norm_radius=rows[i].get("asphere_norm_radius"),
        power=rows[i].get("asphere_power"),
    )
    idx = [k for k in range(len(y)) if bool(valid[k])]
    if not idx:
        return 0.0, 0.0
    return float(z[idx[-1]]), float(z[idx[0]])


def _emit_profile(ax, np, rows, i, half_height, to_plot, *, width, color,
                  family="profile"):
    """Draw ONE surface's own sag profile in plot space; return its points.

    Every per-surface artist carries ``gid = "s{i}:{family}"`` so a test can ask
    WHICH surface owns an artist instead of guessing from coordinates — a
    coordinate can legitimately coincide with a fabricated value, so numeric
    equality is not provenance.
    """
    y = np.linspace(-float(half_height), float(half_height), _N_SAMPLES)
    z, valid = _geom.sag_profile(
        rows[i]["radius"], rows[i]["conic"], y,
        coeffs=rows[i].get("aspheric_coefficients"),
        norm_radius=rows[i].get("asphere_norm_radius"),
        power=rows[i].get("asphere_power"),
    )
    pts = []
    for k in range(len(y)):
        if not bool(valid[k]):
            continue
        p = to_plot(i, float(y[k]), float(z[k]))
        if p is None:
            return []
        pts.append((float(p[0]), float(p[1])))
    if len(pts) < 2:
        return []
    line, = ax.plot(
        [p[0] for p in pts], [p[1] for p in pts],
        color=color, linewidth=width, zorder=3,
    )
    line.set_gid(f"s{i}:{family}")
    return pts


def _leader_dot(ax, surface, point, color):
    """The DOT terminator of an inside-the-part leader (an internal interface).

    Drawn as a scatter mark, not a one-point line: a degenerate ``Line2D`` is
    exactly what the no-single-point-artist guard exists to forbid, and it would
    also be indistinguishable from a collapsed polyline.
    """
    dot = ax.scatter(
        [point[0]], [point[1]], s=6.0, c=[color], marker="o",
        linewidths=0.0, zorder=6,
    )
    dot.set_gid(f"s{surface}:leader_dot")
    return dot


def _rider_word(surface, stop_index, n, *, at_vertex=False):
    """``(word, colour)`` for the surface whose number carries a word, else None.

    The STOP word is RED because red is the stop's semantic in this figure.
    IMAGE is GREY, matching its OWN number — deliberately NOT red: a second
    red word would say the image plane is a second stop.

    ``at_vertex`` is the vertex case: an UNMEASURED surface anchors ON THE AXIS,
    which on a converging system is the single busiest region of the figure.
    There STOP still ships — it is the ONLY thing identifying which surface is
    the stop once its aperture is unknown, and a shipped guard pins that — but
    IMAGE does NOT: the image plane already draws its own full-height line AND
    its number, so the word adds no identification, while its opaque box was
    measured covering 499 ray samples and 8315 element-outline samples on the
    zoom (whose image surface reads unmeasured). Redundant word, real cost.
    """
    if stop_index is not None and surface == stop_index:
        return ("STOP", "red")
    if not at_vertex and n is not None and surface == n - 1:
        return ("IMAGE", "0.4")
    return None


def _word_rider(ax, to_plot, surface, side, arm, sag, word, color):
    """Place a word one arm beyond its own surface-number stamp.

    A PLAIN ``ax.text`` in data coordinates, not an annotation: it carries no
    leader of its own (the number's leader is the pointer) and it must be visible
    to `_expand_limits_to_drawn_text`, which only walks artists drawn in
    ``ax.transData``. ``va`` faces AWAY from the body so the glyphs grow outward
    rather than back over the number.

    The OPAQUE WHITE BOX is the point of the bbox: rays and element outlines cross
    these words (the Cooke's image number sits inside the ray fan), and a word
    read through a ray is a word a vision model reads wrong. BORDERLESS — a
    visible rectangle would add furniture the figure does not need — but the
    patch still carries the NARROW weight, because the two-weight claim
    enumerates every text bbox patch and a borderless patch left at matplotlib's
    default width would read as a third weight to that check even though nothing
    is stroked.
    """
    pos = to_plot(surface, side * arm, sag)
    if pos is None:
        return None
    artist = ax.text(
        pos[0], pos[1], word, ha="center", va="bottom" if side > 0 else "top",
        fontsize=7, color=color, zorder=6,
        bbox={"boxstyle": f"square,pad={_RIDER_BOX_PAD}", "facecolor": "white",
              "edgecolor": "none", "linewidth": _NARROW_LINE_PT},
    )
    artist.set_gid(f"s{surface}:word")
    return artist


def _drawn_text_extent(artist, renderer):
    """The extent of what is actually DRAWN for a text artist.

    A bbox-carrying text draws its PATCH, which is larger than the glyphs by the
    boxstyle pad — so `Text.get_window_extent` (the glyph box) is the wrong ruler
    the moment a white box lands under a word. This is the same instrument error
    as `Annotation.get_window_extent` unioning in the arrow (see the warning on
    the test-side counter): measure the box that is drawn, not a box that is not.

    **REQUIRES A DRAWN CANVAS.** `Text.draw` is what positions the bbox patch, so
    on a never-drawn artist this returns a meaningless unit box — measured,
    bounds (-0.35, -0.35, 1.7, 1.7) pre-draw against (97.4, 296.6, 178.8, 18.4)
    post-draw for the same disclosure box. Every caller draws first; the one
    place that CANNOT (`_place_disclosures`, which seats each box relative to the
    previous one before any draw) says so and reconstructs the pad instead.
    """
    from matplotlib.text import Text as _Text
    patch = artist.get_bbox_patch()
    if patch is not None:
        try:
            return patch.get_window_extent(renderer)
        except Exception:  # noqa: BLE001 — fall back to the glyph box
            pass
    return _Text.get_window_extent(artist, renderer)


def _payload_segments(ax, renderer):
    """The figure's PAYLOAD in display coordinates: rays and element outlines.

    The rays are kept deliberately — ray bending is the visual for angle of
    incidence and spherical management — so an OPAQUE label patch laid over them
    destroys the thing the figure exists to show. The outlines are the other half
    of that payload. Returned as display-space segments so a candidate label seat
    can be graded without re-drawing the canvas for every candidate.
    """
    out = []
    for line in ax.get_lines():
        gid = line.get_gid() or ""
        if not (gid.startswith("ray")
                or gid.endswith((":profile", ":interface", ":body_stroke"))):
            continue
        try:
            xs, ys = line.get_xdata(), line.get_ydata()
            if len(xs) < 2:
                continue
            pts = ax.transData.transform(list(zip(xs, ys)))
        except Exception:  # noqa: BLE001 — an unmeasurable line contributes nothing
            continue
        for i in range(len(pts) - 1):
            out.append((tuple(pts[i]), tuple(pts[i + 1])))
    return out


def _segment_hits_rect(p0, p1, rect):
    """Liang-Barsky: does the segment ``p0``->``p1`` intersect ``rect``?

    A SEGMENT test, not a sampled one: sampling a polyline every N points can
    step straight over a thin label box and report clear, which is the same
    "the instrument cannot see the case that breaks it" failure the counter
    this feeds was built to end.
    """
    (x0, y0), (x1, y1) = p0, p1
    dx, dy = x1 - x0, y1 - y0
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, x0 - rect.x0), (dx, rect.x1 - x0),
                 (-dy, y0 - rect.y0), (dy, rect.y1 - y0)):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return False
            t0 = max(t0, t)
        else:
            if t < t0:
                return False
            t1 = min(t1, t)
    return t0 <= t1


def _rect_shifted(bb, dy):
    """``bb`` translated by ``dy`` display pixels in y. Cheap and allocation-free
    enough to grade a whole candidate ladder without re-drawing the canvas —
    a text patch translates RIGIDLY, so the shifted box is exact, not an
    estimate."""
    return _XYRect(bb.x0, bb.y0 + dy, bb.x1, bb.y1 + dy)


class _XYRect:
    """A display-space rectangle. Deliberately minimal — it exists so a candidate
    seat can be graded without mutating a live artist."""

    __slots__ = ("x0", "y0", "x1", "y1")

    def __init__(self, x0, y0, x1, y1):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1

    def overlaps(self, other):
        return not (self.x1 <= other.x0 or self.x0 >= other.x1
                    or self.y1 <= other.y0 or self.y0 >= other.y1)


def _clear_vertex_words(fig, ax, blocks):
    """Hold an unmeasured surface's WORD clear of its own number and the payload.

    An unmeasured surface's `k?` stamp is anchored AT THE VERTEX — on the axis,
    which on a converging system is the busiest region of the figure — and the
    word rode that same point. TWO harms, both measured on a reachable
    degraded-stop doublet:

    * the word's OPAQUE white patch WHITENED 83 pixels of number ink — its own
      `1?` by 15.0 x 3.5 px and the object `0` by 8.0 x 9.4 px. A figure that
      hides the surface identifier it is standing next to has destroyed the one
      thing the label exists to supply.
    * the patch cut ALL THREE chief rays (6 segments) and the cemented body
      outline (13 segments). The rays are the payload.

    The corpus counter read 0 for both because NO reference design has an
    unmeasured stop, and because it only ever scored boxes against rays and
    outlines — never against another label's INK. Its domain was too small in
    two directions at once.

    THE FIX, and why it fabricates nothing. Number and word are ONE BLOCK
    separated by a fixed DISPLAY-SPACE gap, and the block is stepped outward one
    measured text height at a time until the word's DRAWN patch hits neither the
    payload nor any label's ink. Every quantity here is POINTS or a RENDERED TEXT
    HEIGHT — no aperture value enters, so the prohibition (a fabricated height
    may not position an artist) is untouched: the word makes no claim about where
    this surface's edge is, and it never did. The number itself does not move; it
    stays at the vertex where the vertex anchor puts it.

    Stepping is graded WITHOUT re-drawing: a text patch translates rigidly, so a
    candidate seat is the measured box shifted in display space. Bounded at
    `_VERTEX_WORD_MAX_STEPS`; if no candidate is clear the least-occluding one is
    kept (deterministic, first-best-wins), because a slightly-worse label is
    better than the vertex. Measured on the doublet, step 4 of 5 is clear on both
    counts. Once the block has moved more than one step a NARROW leader is drawn
    from the number to the word so the association survives the distance — a
    stroke overlays, it does not HIDE, which is the whole distinction this
    function turns on.

    Never raises: an unmeasurable canvas leaves every label exactly where the
    shipped placement put it. Returns the number of words moved.
    """
    if not blocks:
        return 0
    from matplotlib.text import Text as _Text
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    except Exception:  # noqa: BLE001 — no renderer -> the shipped placement stands
        return 0
    try:
        payload = _payload_segments(ax, renderer)
    except Exception:  # noqa: BLE001 — an unreadable payload grades as empty
        payload = []
    # every OTHER label's glyph box: the block must not be pushed onto a
    # neighbour's ink either, which is the same harm one surface over.
    ink = []
    for artist in list(ax.texts):
        gid = artist.get_gid() or ""
        if not gid.endswith((":stamp", ":leader", ":word")):
            continue
        try:
            ink.append((artist, _Text.get_window_extent(artist, renderer)))
        except Exception:  # noqa: BLE001
            continue
    try:
        (_x0, py0), (_x1, py1) = ax.transData.transform([[0.0, 0.0], [0.0, 1.0]])
        px_per_unit = abs(py1 - py0)
    except Exception:  # noqa: BLE001 — an untransformable axes leaves labels alone
        return 0
    if not (math.isfinite(px_per_unit) and px_per_unit > 0.0):
        return 0
    px_per_pt = float(fig.dpi) / 72.0
    moved = 0
    for block in blocks:
        word = block.get("word")
        stamp = block.get("stamp")
        if word is None:
            continue
        try:
            wb = _drawn_text_extent(word, renderer)
        except Exception:  # noqa: BLE001 — an unmeasurable word is left alone
            continue
        if not (math.isfinite(wb.height) and wb.height > 0.0):
            continue
        step_px = wb.height + _VERTEX_WORD_GAP_PT * px_per_pt
        others = [bb for art, bb in ink if art is not word]
        best = None
        for k in range(1, _VERTEX_WORD_MAX_STEPS + 1):
            cand = _rect_shifted(wb, -k * step_px)   # outward, the `va="top"` sense
            n_pay = sum(1 for seg in payload
                        if _segment_hits_rect(seg[0], seg[1], cand))
            n_ink = sum(1 for bb in others if cand.overlaps(bb))
            if n_pay == 0 and n_ink == 0:
                best = (0, 0, k)
                break
            if best is None or (n_pay + n_ink) < (best[0] + best[1]):
                best = (n_pay, n_ink, k)
        if best is None:
            continue
        k = best[2]
        pos = word.get_position()
        word.set_position((pos[0], pos[1] - (k * step_px) / px_per_unit))
        moved += 1
        if k > 1 and stamp is not None:
            _vertex_word_leader(ax, block, stamp, word)
    return moved


def _vertex_word_leader(ax, block, stamp, word):
    """A NARROW leader from an unmeasured surface's number to its moved word.

    Drawn only when the block travelled more than one step, so a word sitting
    right under its number gains no furniture it does not need. A 0.5 pt stroke
    OVERLAYS what it crosses — it does not hide it — which is exactly the
    property the opaque patch lacked and the reason a leader is an acceptable
    price for the distance while a box is not.
    """
    try:
        sz, sy = stamp.get_position()
        wz, wy = word.get_position()
        line, = ax.plot([sz, wz], [sy, wy], color=word.get_color(),
                        linewidth=_NARROW_LINE_PT, zorder=5)
        line.set_gid(f"s{block.get('surface')}:word_leader")
    except Exception:  # noqa: BLE001 — a leader must never sink a draw
        return None
    return line


def _stamp_lane_blocked(lanes, tier, bb, pad):
    """True iff ``bb``'s x-interval comes within ``pad`` of something on this tier.

    ``pad`` matters: two labels that merely TOUCH do not overlap by pixel, but they
    read as one number — the zoom's surfaces 20 and 22 rendered as the string
    `2022`, which is exactly the misreading this whole change exists to stop.
    """
    if tier >= len(lanes):
        return False
    return any(not (bb.x1 + pad <= lo or bb.x0 - pad >= hi)
               for (lo, hi) in lanes[tier])


class _XSpan:
    """The x-interval of a drawn block. Deliberately has NO y.

    A word-carrying block is two artists at DIFFERENT heights, so the union's
    y-extent is meaningless — omitting it makes reading one a TypeError rather
    than a plausible wrong number.
    """

    __slots__ = ("x0", "x1")

    def __init__(self, x0, x1):
        self.x0 = x0
        self.x1 = x1


def _union_x(bb, other):
    """``bb`` widened to cover ``other`` in x. ``other`` may be None."""
    if other is None:
        return bb
    return _XSpan(min(bb.x0, other.x0), max(bb.x1, other.x1))


def _tier_colliding_stamps(fig, ax, placements):
    """Give overprinting surface-number labels DIFFERENT HEIGHTS on their side.

    The shipped stagger has exactly TWO slots -- a label goes to the top edge, or
    (on a z-collision) to the bottom. Measured on the reference corpus that leaves
    12 overprinting stamp pairs, and raising the collision threshold makes it
    monotonically WORSE (0.04 -> 12 pairs, 0.08 -> 21, 0.15 -> 22): a bigger
    threshold only moves more labels into the ONE bottom slot. Two slots also
    cannot separate a THREE-way collision, which 10 of those 12 pairs are.

    So the fix is more TIERS, not a bigger threshold. Labels whose RENDERED text
    boxes overlap in x step to tier 0, 1, 2 ... on the SAME side, each tier one
    label-height further out, and the leader lengthens to match: the arrowhead
    stays on the outline at the anchor z, so a longer leader still points
    unambiguously at its own surface.

    The tier is a LOCAL-frame arm length, handed back through the SAME ``to_plot``
    closure the geometry used, so on a fold the taller anchor rotates with
    the body instead of sliding up a global y that no longer means anything.
    Assignment itself is the shipped stagger's own plot-space collision logic,
    only measured instead of estimated.

    Greedy lowest-free-tier over x-sorted intervals is an OPTIMAL colouring for
    interval overlaps, so a k-way collision costs exactly k tiers and no more.

    ``placements`` rows carry ``anno`` / ``surface`` / ``side`` / ``base`` (the
    tier-0 arm, a positive magnitude) / ``sag`` / ``to_plot`` / ``tier``.
    Returns the number of labels whose tier CHANGED, so a caller can re-run after
    the limits move and stop when the assignment is stable. Never raises: an
    unmeasurable canvas leaves every label exactly where the shipped code put it.
    """
    if len(placements) < 2:
        return 0
    from matplotlib.text import Text as _Text
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
    except Exception:  # noqa: BLE001 — no renderer -> the shipped placement stands
        return 0
    measured = []
    for p in placements:
        rider_bb = None
        try:
            # The DRAWN extent, not the glyph box: these two artists carry white
            # boxes, and the box is what occupies the canvas.
            bb = _drawn_text_extent(p["anno"], renderer)
            rider = p.get("rider")
            if rider is not None:
                rider_bb = _drawn_text_extent(rider, renderer)
        except Exception:  # noqa: BLE001 — an unmeasurable label is left alone
            continue
        if math.isfinite(bb.x0) and math.isfinite(bb.x1) and bb.x1 > bb.x0:
            measured.append((p, bb, rider_bb))
    if len(measured) < 2:
        return 0
    # One tier = one label height + a fixed gap, converted display -> data through
    # the axes' own transform. Under equal aspect the y scale equals the x scale,
    # so this is the same distance the overlap was measured in.
    try:
        (_zx0, py0), (_zx1, py1) = ax.transData.transform([[0.0, 0.0], [0.0, 1.0]])
    except Exception:  # noqa: BLE001 — an untransformable axes leaves labels alone
        return 0
    px_per_unit = abs(py1 - py0)
    if not (math.isfinite(px_per_unit) and px_per_unit > 0.0):
        return 0
    px_per_pt = float(fig.dpi) / 72.0
    x_pad = _STAMP_TIER_XPAD_PT * px_per_pt
    text_px = max(r[1].height for r in measured)
    step = (text_px + _STAMP_TIER_GAP_PT * px_per_pt) / px_per_unit
    # TIGHTER than a tier by construction (0.5 pt vs 2.0 pt of clear space), so
    # STOP reads as belonging to the number it rides and not to a neighbour.
    rider_step = (text_px + _STOP_RIDER_GAP_PT * px_per_pt) / px_per_unit
    if not (math.isfinite(step) and step > 0.0):
        return 0

    changed = 0
    for side in (1, -1):
        lanes = []
        rows = [r for r in measured if r[0]["side"] == side]
        if not rows:
            continue
        rows.sort(key=lambda r: (r[1].x0, r[0]["surface"]))
        # Tier 0 keeps the per-surface anchor (each label sits at its OWN
        # drawn edge). Tier 1+ is a LADDER anchored on the side's TALLEST tier-0
        # arm, so one tier really is one clear label-height above EVERY tier-0
        # label. Stepping from each label's own arm does not: on the zoom, s23's
        # own edge sits 0.58 mm inside s21's, so its "one step up" cleared by
        # only 2.21 of the 2.79 needed and the pair still overprinted.
        ladder = max(r[0]["base"] for r in rows)
        for p, bb, rider_bb in rows:
            # A word-carrying label is ONE BLOCK: its occupancy is the UNION of
            # the number's box and the word's, because the word is the wider of
            # the two and a neighbour must clear the WHOLE block. The mechanism:
            # a neighbour's own tier-0 height differs from the block's (tier 0
            # anchors each label at its OWN drawn edge), so a neighbour that
            # merely clears the narrow NUMBER can still sit in the band the WORD
            # occupies one step out.
            #
            # CORPUS-JUSTIFIED, AND **NOT PRESENTLY COVERED** OFFLINE — stated as
            # coverage, not as impossibility. (The tier PASS COUNT and the X-PAD
            # once carried the same "not reddenable" claim and it was FALSE: the
            # dense fixture in test D-1 discriminates both. Nobody has yet built
            # one that discriminates THIS.) Reverting it leaves the whole unit
            # suite GREEN, the dense D-1 fixture included: on a small fixture a
            # bumped neighbour lands on the tier-1 LADDER, far above the word, so
            # the two never meet.
            # It only bites at corpus density — measured with this line reverted
            # and nothing else changed, one design's STOP block overlaps s6 and the
            # eyepiece's IMAGE block overlaps s7 (2 overlaps, 2 touches; 0 with
            # it). The cost is real and also corpus-only: eyepiece 44.85 -> 39.36
            # and another 44.80 -> 42.27 percent of frame. `test_u3` pins the
            # union PREDICATE directly, which is the part a unit test can reach.
            block = _union_x(bb, rider_bb)
            tier = 0
            while (tier < _STAMP_TIER_MAX - 1
                   and _stamp_lane_blocked(lanes, tier, block, x_pad)):
                tier += 1
            reserve = tier + 1 if rider_bb is not None else tier
            while len(lanes) <= reserve:
                lanes.append([])
            lanes[tier].append((block.x0, block.x1))
            if reserve != tier:
                # The word physically OCCUPIES the next tier's band on this side,
                # so that band is spoken for too — a neighbour bumped up one tier
                # would otherwise be placed straight onto the word.
                lanes[reserve].append((block.x0, block.x1))
            arm = p["base"] if tier == 0 else ladder + tier * step
            pos = p["to_plot"](p["surface"], p["side"] * arm, p["sag"])
            if pos is None:
                continue
            was = p["anno"].get_position()
            # The STEP is re-derived every pass, not just the tier: growing the
            # stack grows the limits, which under equal-aspect-by-box shrinks
            # pixels-per-data-unit, so an unchanged tier can still need a taller
            # arm to clear by the same number of PIXELS. Skipping on `tier ==
            # p["tier"]` alone left a measured residual overlap on the zoom.
            if not (tier == p["tier"] and abs(pos[1] - was[1]) <= 0.02 * step):
                p["anno"].set_position(pos)
                p["tier"] = tier
                changed += 1
            rider = p.get("rider")
            if rider is not None:
                # STOP follows its number OUTBOARD on the number's own side,
                # through whatever tier the pass settled it in — top lane, above;
                # bottom lane, below. Positioned here rather than at creation
                # because the lane and tier are only final once this pass has
                # run, and the offset is a MEASURED text height (the creation-time
                # fraction is only a stand-in until the canvas can be measured).
                r_pos = p["to_plot"](
                    p["surface"], p["side"] * (arm + rider_step), p["sag"])
                if r_pos is not None:
                    rider.set_position(r_pos)
    return changed


def _hide_axes_chrome(ax):
    """Hide the axes chrome -- spines and tick MARKS (the legend frame is off
    at creation).

    The two-weight claim is scoped to the DRAWING's own line and patch
    families; axes chrome sits outside it, and the standing instruction is to
    check whether the shipped figure exposes chrome and hide it if it does. It
    does: four spines at 0.8 pt and twenty visible tick marks at 0.8 pt -- a
    third weight on the canvas beside a drawing that claims two.

    The numeric tick LABELS stay. They are text, carry no line weight, and are
    the only scale cue a reader (or a vision model) gets; dropping them would
    trade a real capability for a claim that never covered them.
    """
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.tick_params(axis="both", which="both", length=0.0, width=0.0)
    return ax


def _draw_group_bodies(ax, np, rows, groups, rims, apertures, draw_heights, to_plot):
    """Draw ONE transparent fill + ONE closed stroke per group that has a body.

    Both consume ``GroupSection.polygon`` — there is no second copy of the
    geometry and no path-local closure, so the stroke can only ever render the
    fill's own boundary. The transparent fill keeps exactly one inspectable body
    artist per group.

    A ``placeholder``-basis body (no member's aperture was ever read) is the ONLY
    closed body that does not stroke wide continuous black: it renders narrow,
    dashed and grey, so a reader can tell at a glance that its height is not a
    measurement.
    """
    state = {
        "grouped": set(), "drawn_caps": set(), "placeholder_members": set(),
        "open_groups": [], "placeholder_groups": [], "rim_truncated": [],
        "rim_by_surface": {}, "points": [], "sections": [], "cap_surfaces": set(),
    }
    for group, rim in zip(groups, rims):
        for g in group:
            state["grouped"].add(int(g))
        # The OUTER caps this builder will consume as the body's boundary —
        # recorded whatever the outcome, because an unmeasured cap is exactly
        # the case that must reach the result dict.
        if group:
            state["cap_surfaces"].add(int(group[0]))
            state["cap_surfaces"].add(int(group[-1]))
        build = _geom.build_group_section(
            np, rows, group, apertures, draw_heights, rim, to_plot,
            samples=_N_SAMPLES,
        )
        state["rim_truncated"].extend(int(s) for s in build.rim_truncated)
        if build.section is None:
            if build.open_surfaces:
                state["open_groups"].append(
                    tuple(int(s) for s in build.open_surfaces))
            continue
        poly = list(build.section.polygon)
        zs = [float(p[0]) for p in poly]
        ys = [float(p[1]) for p in poly]
        # The body artists carry a gid too. Without one an ownership oracle is
        # BLIND to the closed body — it can prove no PER-SURFACE artist exists
        # for an unmeasured surface while the body silently drew it.
        gid = f"group{int(group[0])}_{int(group[-1])}"
        body_fill, = ax.fill(zs, ys, facecolor="none", edgecolor="none", zorder=1)
        body_fill.set_gid(f"{gid}:body_fill")
        if rim.basis == "placeholder":
            stroke, = ax.plot(zs + [zs[0]], ys + [ys[0]], color=_NOT_MEASURED_GREY,
                              linewidth=_NARROW_LINE_PT,
                              linestyle=_NOT_MEASURED_DASHES, zorder=3)
            state["placeholder_groups"].append(tuple(int(s) for s in group))
            state["placeholder_members"].update(int(s) for s in group)
        else:
            stroke, = ax.plot(zs + [zs[0]], ys + [ys[0]], color="black",
                              linewidth=_WIDE_LINE_PT, zorder=3)
        stroke.set_gid(f"{gid}:body_stroke")
        state["drawn_caps"].add(int(group[0]))
        state["drawn_caps"].add(int(group[-1]))
        for s in group:
            state["rim_by_surface"][int(s)] = rim
        state["points"].extend(zip(zs, ys))
        state["sections"].append(build.section)
    state["rim_truncated"] = sorted(set(state["rim_truncated"]))
    return state


def _projection_disclosures(projection, rows):
    """The projection-status strings, reason-specific.

    ``S-PROJ-UNK`` is EXCLUSIVELY the coordinate-break-cell-read cause;
    ``S-PROJ-TYPE`` is the surface-type-outside-the-domain cause. Emitting one
    for the other's cause would be a false diagnostic.
    """
    if projection is None:
        return []
    if projection.out_of_plane is True:
        return [_S_OOP]
    if projection.out_of_plane is not None:
        return []
    cb_cause = False
    type_cause = []
    for s in projection.unreadable_surfaces:
        try:
            type_name = str(rows[s]["type_name"])
        except Exception:  # noqa: BLE001 — an unreadable Type is a type-domain cause
            type_name = ""
        if _geom._is_coordinate_break(type_name):
            cb_cause = True
        else:
            type_cause.append(int(s))
    lines = []
    if cb_cause:
        lines.append(_S_PROJ_UNK)
    lines.extend(_S_PROJ_TYPE.format(k=s) for s in type_cause)
    return lines


def _figure_disclosure_strings(
    *, rows, folded, projection, config_identity, open_groups,
    placeholder_groups, aperture_not_measured, interfaces_omitted,
    profile_not_measured,
):
    """Assemble the figure strings in the fixed precedence order."""
    lines = list(_projection_disclosures(projection, rows))
    if folded:
        lines.append(_S_FOLD)
        lines.append(_S_FOLD_CLR)
    if config_identity is not None:
        if config_identity.count is None or config_identity.active is None:
            lines.append(_S_CFG_UNK)
        elif config_identity.count > 1:
            lines.append(_S_CFG.format(k=config_identity.active,
                                       N=config_identity.count))
    placeholder_members = set()
    for group in placeholder_groups:
        placeholder_members.update(group)
        lines.append(_S_GRP_PLACEHOLDER.format(a=group[0], b=group[-1]))
    open_caps = set()
    for caps in open_groups:
        for k in caps:
            open_caps.add(int(k))
            lines.append(_S_GRP_OPEN.format(k=k))
    for k in aperture_not_measured:
        if k in placeholder_members or k in open_caps:
            continue          # already disclosed by its group's own string
        if k in interfaces_omitted:
            lines.append(_S_AP_INT.format(k=k))
        else:
            lines.append(_S_AP.format(k=k))
    lines.extend(_S_SAG.format(k=k) for k in profile_not_measured)
    return lines


def _place_disclosures(fig, ax, strings):
    """Stack the disclosure boxes top-left, non-overlapping and contained.

    Each box is placed below the previous box's MEASURED bounding box plus a fixed
    gap — never at a hardcoded shared coordinate. The policy is TOTAL: boxes are
    placed in precedence order until the next one would leave the canvas, at which
    point the last drawn slot becomes the overflow line and every remaining box is
    reported in the result instead. Never a refusal, never a silent drop.

    Returns ``(drawn_strings, n_truncated)``.
    """
    if not strings:
        return [], 0
    try:
        renderer = fig.canvas.get_renderer()
    except Exception:  # noqa: BLE001 — no renderer -> the deterministic step below
        renderer = None
    inv = ax.transAxes.inverted()

    def _box(text, y_top):
        artist = ax.text(
            _DISCLOSURE_X, y_top, text, transform=ax.transAxes,
            ha="left", va="top", fontsize=_DISCLOSURE_FONT_PT, color="black",
            zorder=7,
            bbox={"boxstyle": f"square,pad={_DISCLOSURE_BOX_PAD}",
                  "facecolor": "white",
                  "edgecolor": "black", "linewidth": _NARROW_LINE_PT},
        )
        # The disclosure stack is now no longer the ONLY boxed text family — the
        # STOP/IMAGE blocks carry white boxes too. Identify by GID, never by
        # "has a bbox patch": that test was correct only while this was the sole
        # boxed family, and it silently absorbed the new one.
        artist.set_gid(_DISCLOSURE_GID)
        return artist

    # The BOX is the patch, not the glyphs. Text.get_window_extent returns the
    # TEXT extent, so stacking on it leaves the drawn rectangles overlapping
    # by the boxstyle padding -- measured at ~4.5 px, i.e. a non-empty
    # pairwise intersection over artists the contract says must not overlap.
    # pad + the border STROKE, which straddles the patch path (half a
    # linewidth outside each of the two adjacent boxes). Measured: pad alone
    # left the drawn rectangles overlapping by 0.71 px, down from 4.50 px on
    # the glyph extent; the stroke allowance closes it.
    #
    # WHY THIS RECONSTRUCTS THE PAD INSTEAD OF READING `_drawn_text_extent`
    # (an external audit asked for the direct read, and it is NOT AVAILABLE
    # HERE): a Text's bbox patch is positioned by `Text.draw`, so before the
    # first draw it reports a meaningless unit box. Measured on a freshly
    # created disclosure box, `get_bbox_patch().get_window_extent()` returns
    # bounds (-0.35, -0.35, 1.7, 1.7) pre-draw against (97.4, 296.6, 178.8,
    # 18.4) post-draw. Placement is inherently pre-draw and sequential -- each
    # box is seated below the previous one's measured bottom -- so reading the
    # patch here would need a full canvas draw PER BOX. The reconstruction is
    # therefore the only measurement available at this point, and it is not
    # trusted: tests I-3/I-4 assert the no-intersection contract POST-DRAW on
    # the real patches, which is what catches any drift in this arithmetic.
    pad_px = ((_DISCLOSURE_BOX_PAD * _DISCLOSURE_FONT_PT + _NARROW_LINE_PT)
              * (float(fig.dpi) / 72.0))

    def _bottom_axes(artist):
        if renderer is None:
            return None
        try:
            bb = artist.get_window_extent(renderer=renderer)
            bottom_px = float(min(bb.y0, bb.y1)) - pad_px
            pts = inv.transform([[bb.x0, bottom_px], [bb.x1, bottom_px]])
        except Exception:  # noqa: BLE001 -- an unmeasurable extent -> fixed step
            return None
        return float(min(pts[0][1], pts[1][1]))

    try:
        px_h = float(fig.get_figheight()) * float(fig.dpi)
    except Exception:  # noqa: BLE001
        px_h = 600.0
    gap_axes = _DISCLOSURE_GAP_PT / max(px_h, 1.0)
    fallback_step = 0.075

    placed = []
    y_cursor = _DISCLOSURE_TOP
    overflowed = False
    for text in strings:
        artist = _box(text, y_cursor)
        bottom = _bottom_axes(artist)
        if bottom is None:
            bottom = y_cursor - fallback_step
        if placed and bottom < _DISCLOSURE_FLOOR:
            artist.remove()
            overflowed = True
            break
        placed.append((artist, text, y_cursor))
        y_cursor = bottom - gap_axes
    if not overflowed:
        return [t for (_a, t, _y) in placed], 0
    hidden = len(strings) - (len(placed) - 1)
    artist, _text, slot = placed.pop()
    artist.remove()
    line = _S_OVERFLOW.format(n=hidden)
    placed.append((_box(line, slot), line, slot))
    return [t for (_a, t, _y) in placed], hidden


def _expand_limits_to_drawn_text(fig, ax):
    """Grow the limits until every DATA-space text box is inside the axes.

    The limits are computed from label ANCHOR points, but a label is a box: with
    `va="bottom"` the glyphs rise above their anchor, so an anchor inside the
    frame can still leave the text clipped. Measured on a long-back-focus
    doublet: two stamps sat outside a window that contained both their anchors.

    Reads the RENDERED extents rather than estimating a font height, and only
    ever grows. Disclosure boxes are in AXES coordinates and are deliberately
    skipped — they scale with the frame and cannot be chased by expanding it.
    Never raises; an unmeasurable extent simply contributes nothing.
    """
    try:
        renderer = fig.canvas.get_renderer()
    except Exception:  # noqa: BLE001 — no renderer -> the computed limits stand
        return
    inv = ax.transData.inverted()
    x_lo, x_hi = ax.get_xlim()
    y_lo, y_hi = ax.get_ylim()
    grew = False
    for artist in list(ax.texts):
        if artist.get_transform() is not ax.transData:
            continue
        try:
            # The DRAWN extent, not the glyph box. A rider carries an OPAQUE
            # white patch that is LARGER than its glyphs by the boxstyle pad, and
            # the patch is what occupies the canvas — measured on a short
            # measured singlet, the IMAGE glyphs ended 1.50 px INSIDE the axes
            # while the patch they sit in ended 2.00 px OUTSIDE it. Expanding on
            # the glyph box therefore satisfies the containment contract while
            # visible ink is out of frame; only `bbox_inches="tight"` was saving
            # it, and that is the saver's accident, not this function's promise.
            bb = _drawn_text_extent(artist, renderer)
            (dx0, dy0), (dx1, dy1) = inv.transform(
                [[bb.x0, bb.y0], [bb.x1, bb.y1]])
        except Exception:  # noqa: BLE001 — an unmeasurable text contributes nothing
            continue
        for dx, dy in ((dx0, dy0), (dx1, dy1)):
            if not (math.isfinite(dx) and math.isfinite(dy)):
                continue
            if dx < x_lo:
                x_lo, grew = dx, True
            if dx > x_hi:
                x_hi, grew = dx, True
            if dy < y_lo:
                y_lo, grew = dy, True
            if dy > y_hi:
                y_hi, grew = dy, True
    if not grew:
        return False
    x_pad = 0.01 * (x_hi - x_lo)
    y_pad = 0.01 * (y_hi - y_lo)
    ax.set_xlim(x_lo - x_pad, x_hi + x_pad)
    ax.set_ylim(y_lo - y_pad, y_hi + y_pad)
    return True


def _seat_scope_footer(fig, ax, artist):
    """Seat the footer just below the axes' OWN bottom furniture.

    The anchor is the axes box UNIONED with its x tick labels and x-label — the
    depth of that furniture is not a constant (it moves with the tick-label
    height, and with how far equal-aspect-by-box shrank the axes inside its
    rectangle), so a hardcoded figure-coordinate seat would land ON the scale
    cues on some designs and metres below them on others.

    The union is assembled EXPLICITLY rather than from ``ax.get_tightbbox``,
    which also folds in the ray legend — and the legend is anchored OUTSIDE the
    axes to the right, so its x-extent would drag a right-aligned caption out
    from under the plot and into the legend column.

    The footer is a FIGURE child, so it contributes nothing to the extents read
    here: this is a single measurement, not a fixed point. Never raises — an
    unmeasurable canvas leaves the deterministic seat the caller already made.
    """
    try:
        renderer = fig.canvas.get_renderer()
        axes_box = ax.get_window_extent(renderer)
    except Exception:  # noqa: BLE001 — the deterministic placement stands
        return False
    bottom_px = float(axes_box.y0)
    right_px = float(axes_box.x1)
    below = [ax.xaxis.label] + list(ax.get_xticklabels())
    for furniture in below:
        try:
            if not furniture.get_text():
                continue
            bb = furniture.get_window_extent(renderer)
        except Exception:  # noqa: BLE001 — an unmeasurable label contributes nothing
            continue
        if math.isfinite(bb.y0):
            bottom_px = min(bottom_px, float(bb.y0))
    gap_px = _FOOTER_GAP_PT * (float(fig.dpi) / 72.0)
    try:
        x_fig, y_fig = fig.transFigure.inverted().transform(
            (right_px, bottom_px - gap_px))
    except Exception:  # noqa: BLE001 — untransformable -> the fallback seat stands
        return False
    if not (math.isfinite(x_fig) and math.isfinite(y_fig)):
        return False
    artist.set_position((float(x_fig), float(y_fig)))
    artist.set_va("top")
    artist.set_ha("right")
    return True


def _draw_scope_footer(fig, ax):
    """The standing footer stating what the projection check covered.

    It is on EVERY figure so the ABSENCE of a projection box can never be read as
    proof of planarity, and it is EXEMPT from the disclosure-stack overflow rule.

    It is a FIGURE-level caption placed BELOW the axes, not an axes-coordinate
    text inside them. It describes the figure; it is not data, and it never
    competed for canvas on merit. Inside the axes it owned a band of the bottom
    that ordinary bottom-lane surface stamps also reach — measured on the
    reference corpus, 8 stamps on 5 of the 8 designs OVERPRINTED it (11 within
    2 pt), the worst being one design with 4. The only in-axes cure is to reserve
    that band for every bottom label, which was measured to cost ~10 % of the
    frame on seven of the eight designs. Moving the caption out removes the
    collision CLASS instead of negotiating with it, exactly as the ray legend
    was moved out via `bbox_to_anchor` earlier in this cycle (6 -> 0).

    `savefig(bbox_inches="tight")` grows the WRITTEN canvas to include a
    figure-level artist, so the axes keep their full size and nothing is
    cropped away.

    It carries its OWN gid, never `_DISCLOSURE_GID`: the top-left stack
    identifies its members by gid precisely so no other text can be absorbed
    into it, and a caption counted as a stack member would inflate the overflow
    bookkeeping the same way "any text with a bbox patch" once did.
    """
    artist = fig.text(
        _FOOTER_FALLBACK_XY[0], _FOOTER_FALLBACK_XY[1], _S_SCOPE,
        ha="right", va="bottom", fontsize=_SCOPE_FONT_PT, color="0.4", zorder=6,
    )
    artist.set_gid(_SCOPE_GID)
    _seat_scope_footer(fig, ax, artist)
    return _S_SCOPE


def _draw(
    plt, np, rows, n, title, stop_index, folded, apertures, rims, draw_heights,
    degraded, ray_data, draw_rays, projection, config_identity,
):
    """Draw the UNFOLDED figure and return it. Caller owns closing it in a finally.

    Coordinate-break and flat powerless air dummy/spacer surfaces are suppressed
    (scaffolding, not drawn); real optics (glass, mirrors, curved lens-backs), the
    stop, and the image are stamped with their true Zemax numbers.

    ``apertures`` carries each surface's reading WITH its provenance and
    ``draw_heights`` is the ONE height map the drawing code may consume; there is
    no bare heights list in this body, and ``all_zero`` is derived where needed.

    Returns ``(fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures)``
    where the trailing list is EXACTLY the disclosure strings placed on the figure.
    """
    fig, ax = plt.subplots(figsize=(13, 5), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    z_vertex = _geom.vertex_z([r["thickness"] for r in rows])
    optical_indices = _optical_indices(rows, n)
    surface_labels = []

    def to_plot(surface, y, sag):
        """Local ``(y, sag)`` -> plot ``(z, y)`` for the unfolded path."""
        return (z_vertex[surface] + sag, y)

    # h_max frames the figure; h_geom scales what is DRAWN. Do not swap them.
    h_max = _plot_h_max(draw_heights, rims, optical_indices)
    h_geom = _plot_geom_height(apertures, optical_indices)
    arrow_len = h_geom * _STAMP_ARROW_LEN_FRAC

    groups = _glass_groups(rows, n, optical_indices)
    body = _draw_group_bodies(
        ax, np, rows, groups, rims, apertures, draw_heights, to_plot,
    )

    callouts = []          # (surface_index, color)
    interfaces_omitted = []
    for i in optical_indices:
        record = apertures[i] if i < len(apertures) else None
        measured = bool(record is not None and record.measured)
        internal = (
            i in body["grouped"] and i != 0 and _geom.is_cemented_interface(rows, i)
        )
        if i in body["drawn_caps"] or i in body["placeholder_members"]:
            pass  # the group's ONE closed stroke already carries this surface
        elif internal:
            if measured:
                _emit_profile(
                    ax, np, rows, i, draw_heights[i], to_plot,
                    width=_NARROW_LINE_PT, color="black", family="interface",
                )
            else:
                # An interface whose aperture was never read is OMITTED, never
                # drawn at a fabricated height; the body still closes from its
                # measured outer caps and the omission is disclosed.
                interfaces_omitted.append(i)
        elif measured:
            _emit_profile(
                ax, np, rows, i, draw_heights[i], to_plot,
                width=_WIDE_LINE_PT, color="black", family="profile",
            )
        is_stop = (stop_index is not None and i == stop_index)
        callouts.append((i, "red" if is_stop else "black"))
        surface_labels.append(i)

    # --- optical axis + image plane (L5) ---------------------------------- #
    axis_line = ax.axhline(0.0, color="black", linewidth=_NARROW_LINE_PT,
                           linestyle=_AXIS_DASHES, zorder=0)
    axis_line.set_gid("axis:optical")
    image_z = z_vertex[n - 1]
    image_line = ax.axvline(image_z, color="black", linewidth=_NARROW_LINE_PT,
                            zorder=2)
    image_line.set_gid(f"s{n - 1}:image_plane")
    callouts.append((n - 1, "0.4"))

    # --- stop ticks — drawn ONLY from a MEASURED stop aperture -------------- #
    stop_label = None
    stop_record = (
        apertures[stop_index]
        if (stop_index is not None and 0 <= stop_index < len(apertures))
        else None
    )
    stop_measured = bool(stop_record is not None and stop_record.measured)
    if stop_index is not None and stop_index in optical_indices:
        stop_label = stop_index
        if stop_measured:
            zs = z_vertex[stop_index]
            stop_semi = float(stop_record.height)
            tick_half = max(0.06 * h_geom, 0.04 * stop_semi)
            for y_edge in (stop_semi, -stop_semi):
                tick, = ax.plot(
                    [zs, zs], [y_edge - tick_half, y_edge + tick_half],
                    color="#FF8C00", linewidth=_NARROW_LINE_PT,
                    solid_capstyle="butt", zorder=5,
                )
                tick.set_gid(f"s{stop_index}:tick")

    # --- surface-number stamps + leaders ----------------------------------- #
    first_optical_z = z_vertex[1] if n > 1 else z_vertex[0]
    z_span = image_z - first_optical_z
    if not math.isfinite(z_span) or z_span <= 0:
        z_span = 1.0
    collide_thresh = _STAMP_COLLIDE_FRAC * z_span

    def _order_key(c):
        idx = c[0]
        is_stop = stop_index is not None and idx == stop_index
        return (z_vertex[idx], 1 if is_stop else 0, idx)

    placed_top_z = []
    deepest_bottom_y = None
    highest_top_y = None
    placements = []
    vertex_blocks = []
    for idx, color in sorted(callouts, key=_order_key):
        record = apertures[idx] if idx < len(apertures) else None
        if record is None or not record.measured:
            # An unmeasured surface anchors its `?` stamp at the VERTEX and gets
            # no aperture-edge leader: there is no edge here we may draw at.
            vz, vy = to_plot(idx, 0.0, 0.0)
            stamp = ax.text(vz, vy, f"{idx}?", ha="center", va="bottom",
                            fontsize=8, color=color, zorder=6)
            stamp.set_gid(f"s{idx}:stamp")
            word = _rider_word(idx, stop_index, n, at_vertex=True)
            if word is not None:
                # THE VERTEX OVERRIDES THE TIER for the NUMBER: an unmeasured surface
                # anchors at the VERTEX, and dragging it out to an aperture edge
                # would place it at a height nobody measured. The WORD is seeded
                # here and then stepped OUT in display space by
                # `_clear_vertex_words` — a points-and-text-heights offset that
                # claims no aperture, so the fabricated-height prohibition is
                # untouched while the opaque patch stops sitting on its own
                # number and on the ray convergence.
                _vw = ax.text(vz, vy, word[0], ha="center", va="top",
                              fontsize=7, color=word[1], zorder=6,
                              bbox={"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                                    "facecolor": "white", "edgecolor": "none",
                                    "linewidth": _NARROW_LINE_PT})
                _vw.set_gid(f"s{idx}:word")
                vertex_blocks.append(
                    {"surface": idx, "stamp": stamp, "word": _vw})
            continue
        rim = body["rim_by_surface"].get(idx)
        drawn_edge = (
            float(rim.rim_height)
            if rim is not None and rim.basis == "measured"
            else float(draw_heights[idx])
        )
        sag_hi, sag_lo = _edge_sag(np, rows, idx, float(draw_heights[idx]))
        # An INTERNAL interface's leader terminates in a DOT at its own-semi curve
        # end, inside the body; an outer contour's leader terminates in an ARROWHEAD
        # touching the outline at the anchor z. The interface leader therefore
        # crosses the outline, which is ordinary practice for a leader pointing at
        # something inside a part.
        interior = (
            idx in body["grouped"] and idx != 0
            and _geom.is_cemented_interface(rows, idx)
            and idx not in body["drawn_caps"]
        )
        tip_edge = float(draw_heights[idx]) if interior else drawn_edge
        zc = z_vertex[idx]
        collides = any(abs(zc - pz) < collide_thresh for pz in placed_top_z)
        arm = drawn_edge + arrow_len
        if collides:
            side, sag_edge = -1, sag_lo
            tip = to_plot(idx, -tip_edge, sag_lo)
            label = to_plot(idx, -arm, sag_lo)
            deepest_bottom_y = (
                label[1] if deepest_bottom_y is None
                else min(deepest_bottom_y, label[1])
            )
            va = "top"
        else:
            side, sag_edge = 1, sag_hi
            tip = to_plot(idx, tip_edge, sag_hi)
            label = to_plot(idx, arm, sag_hi)
            placed_top_z.append(zc)
            highest_top_y = (
                label[1] if highest_top_y is None
                else max(highest_top_y, label[1])
            )
            va = "bottom"
        anno = ax.annotate(
            str(idx), xy=tip, xytext=label, ha="center", va=va, fontsize=8,
            color=color, zorder=6,
            arrowprops={"arrowstyle": "-" if interior else "->",
                        "lw": _NARROW_LINE_PT,
                        "color": color, "shrinkA": 1.0, "shrinkB": 1.0},
        )
        anno.set_gid(f"s{idx}:leader")
        placements.append({
            "anno": anno, "surface": idx, "side": side, "base": arm,
            "sag": sag_edge, "to_plot": to_plot, "tier": 0, "rider": None,
        })
        word = _rider_word(idx, stop_index, n)
        if word is not None:
            # The NUMBER gets the white box too, not just the word: the user's
            # report was that the IMAGE NUMBER is what is unreadable — it is
            # drawn ON its own surface's plane line, inside the ray fan.
            anno.set_bbox({"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                           "facecolor": "white", "edgecolor": "none",
                           "linewidth": _NARROW_LINE_PT})
            placements[-1]["rider"] = _word_rider(
                ax, to_plot, idx, side, arm + _STOP_RIDER_ARM_FRAC * h_geom,
                sag_edge, word[0], word[1],
            )
        if interior:
            _leader_dot(ax, idx, tip, color)

    # The "STOP" word is NOT placed here any more. It used to be computed
    # independently at the bottom of the frame (below the stop ticks, below the
    # deepest label), which put it ~46 data units from the surface it names on a
    # zoom — on the far side of the axis, with nothing connecting the two — and
    # hard against the S-SCOPE footer. It now RIDES its own red stamp (see
    # `_stop_rider`), which is the artist that already carries a leader to the
    # stop surface.

    # Object (0) reference tick at the figure margin (schematic).
    object_stamp = ax.text(z_vertex[0], 0.0, "0", ha="right", va="center",
                           fontsize=7, color="0.4", zorder=5)
    object_stamp.set_gid("s0:stamp")

    # --- per-field chief + marginal rays (L6) ----------------------------- #
    n_rays_drawn = _draw_rays(ax, plt, ray_data, draw_rays)

    ax.set_title(title)
    ax.set_xlabel("z (optical axis)")
    ax.set_ylabel("y")
    # Equal aspect: curvature and angles read TRUE. This must not be traded away.
    # EQUAL DATA ASPECT -- kept, and that is the clause that matters (true to
    # scale). What changed is matplotlib's MECHANISM for honouring it.
    # adjustable="datalim" satisfies the aspect by REWRITING the data limits,
    # which silently DISCARDS the limits computed immediately below: measured
    # on a real high-NA objective this code requested y +/-11.69 and the
    # figure rendered +/-7.19, clipping 22 of 24 stamps off the canvas; on a
    # long-back-focus doublet it inflated y to +/-44 for an 11 mm lens,
    # leaving ~1% of the frame occupied. adjustable="box" honours the SAME
    # aspect by sizing the AXES BOX, so the limits below govern and the
    # drawing fills its frame. A DELIBERATE DEVIATION from the earlier spec,
    # which named adjustable="datalim", ruled after that measurement. The
    # equal-aspect PROPERTY (not the string) is asserted by a permanent test
    # so a later polish cannot trade it away.
    ax.set_aspect("equal", adjustable="box")
    _hide_axes_chrome(ax)
    z_lo = min(z_vertex[1:]) if n > 1 else 0.0
    z_hi = image_z
    z_pad = 0.05 * (z_hi - z_lo) if z_hi > z_lo else 1.0
    ax.set_xlim(z_lo - z_pad, z_hi + z_pad)

    def _apply_y_limits():
        top_stack = highest_top_y if highest_top_y is not None else (
            h_max + arrow_len)
        y_top = top_stack + 0.18 * h_max
        y_bot = -1.55 * h_max
        if deepest_bottom_y is not None:
            y_bot = min(y_bot, deepest_bottom_y - 0.25 * h_max)
        # There is NO footer-band reservation here any more. It existed to keep
        # the STOP word out of an axes-coordinate footer that shared the bottom
        # of the frame; the footer is now a figure-level caption BELOW the axes,
        # so nothing inside the frame can reach it and reserving a band would
        # buy nothing at a real cost. The rider's ink is still framed — it is
        # folded into `deepest_bottom_y` above, so STOP cannot leave the canvas.
        ax.set_ylim(y_bot, y_top)

    _apply_y_limits()
    # Tier the overprinting labels, then re-frame. Two passes, because tiering
    # grows the stack, which grows the limits, which under equal-aspect-by-box
    # rescales the very pixels the overlap was measured in. The loop stops as
    # soon as the assignment is stable and is bounded either way.
    for _pass in range(_STAMP_TIER_PASSES):
        # The pass is run for its POSITIONING, not only for its verdict: it
        # settles every rider onto the measured step even when it moves no stamp
        # at all. Breaking on a 0-change FIRST pass therefore skipped the
        # re-frame entirely, and the rider's ink was never folded into the
        # bottom extreme on a figure with no stamp collisions — which is most
        # of them.
        _changed = _tier_colliding_stamps(fig, ax, placements)
        for p in placements:
            ys = [p["anno"].get_position()[1]]
            rider = p.get("rider")
            if rider is not None:
                # STOP is OUTBOARD of its number, so it — not the number — is
                # the extreme of that side's stack. Framing on the number alone
                # would make the one word that extends the stack the one thing
                # that escapes the frame.
                #
                # And the ANCHOR is not the extreme either: `va` faces outward,
                # so the glyphs run one text height PAST it. The offset that
                # placed the rider is exactly one text height, so reflecting the
                # anchor through the number gives the ink edge without needing a
                # renderer here.
                r_y = rider.get_position()[1]
                r_ink = r_y + (r_y - ys[0])
                ys.append(r_ink)
            for label_y in ys:
                if p["side"] > 0:
                    highest_top_y = (
                        label_y if highest_top_y is None
                        else max(highest_top_y, label_y))
                else:
                    deepest_bottom_y = (
                        label_y if deepest_bottom_y is None
                        else min(deepest_bottom_y, label_y))
        _apply_y_limits()
        if not _changed:
            break

    # AFTER the rays and the tier passes: the vertex block is graded against the
    # payload it must clear, so the payload has to be on the canvas first.
    _clear_vertex_words(fig, ax, vertex_blocks)

    figure_disclosures = _finish_disclosures(
        fig, ax, rows=rows, folded=folded, projection=projection,
        config_identity=config_identity, body=body,
        optical_indices=optical_indices, stop_index=stop_index, n=n,
        apertures=apertures, interfaces_omitted=interfaces_omitted,
    )
    return fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures


def _legend_handles(legend):
    """The legend's drawn handles, however this matplotlib spells them.

    There is no minimum matplotlib pin on this package, and the accessor was
    renamed (`legendHandles` -> `legend_handles`) without either name being
    guaranteed present. Both are tried, then the legend's own lines; an
    unrecognised legend yields nothing rather than raising, because a swatch
    tweak must never be able to sink a draw.
    """
    for name in ("legend_handles", "legendHandles"):
        handles = getattr(legend, name, None)
        if handles:
            return list(handles)
    try:
        return list(legend.get_lines())
    except Exception:  # noqa: BLE001 — an unrecognised legend contributes nothing
        return []


def _draw_rays(ax, plt, ray_data, draw_rays):
    """Draw the per-field chief + upper/lower marginal rays. Returns the count.

    The ray SET is untouched by the restyle — one chief and two marginals per
    field at the primary wavelength, one colour per field. Only the WIDTH changed:
    chief and marginal now share the narrow weight, because only two weights
    exist. Rays separate by colour, never by a third weight.
    """
    n_rays_drawn = 0
    if not (draw_rays and ray_data and ray_data.get("fields")):
        return 0
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
            legend = f"field Y={fy:g}" if first else "_nolegend_"
            first = False
            ray_line, = ax.plot(
                [p[0] for p in poly], [p[1] for p in poly], color=color,
                linewidth=_NARROW_LINE_PT, zorder=4, label=legend,
            )
            ray_line.set_gid(f"ray{fi}:{label}")
            n_rays_drawn += 1
    if n_rays_drawn > 0:
        # frameon=False: the legend FRAME is one of the three chrome families
        # the standing instruction says to hide.
        #
        # The legend sits OUTSIDE the axes, not in a corner of it. Every corner is
        # already spoken for: upper-right is where the tallest stamp tiers land,
        # upper-left is the disclosure stack, and the bottom lanes carry the
        # deepest surface stamps. (The S-SCOPE footer used to hold the
        # lower-right too; it has since moved outside the axes for the same
        # reason this legend did.)
        # Measured, an in-axes legend overprinted a surface number on 2 of 8
        # reference designs (the zoom's `22`, the Cooke's `8` — the Cooke's
        # predates tiering). Placing it outside removes the class rather than
        # relocating the collision, and `savefig(bbox_inches="tight")` grows the
        # canvas to include it, so the AXES keep their full size.
        legend = ax.legend(loc="upper left", bbox_to_anchor=(1.005, 1.0),
                           fontsize=7, framealpha=0.8, frameon=False)
        for handle in _legend_handles(legend):
            try:
                handle.set_linewidth(_LEGEND_SWATCH_PT)
            except Exception:  # noqa: BLE001 — never sinks a draw
                pass
    return n_rays_drawn


def _finish_disclosures(fig, ax, *, rows, folded, projection, config_identity,
                        body, optical_indices, stop_index, n, apertures,
                        interfaces_omitted):
    """Assemble, place and return the figure's disclosure strings + the footer."""
    # Before anything is placed in AXES coordinates, make sure the DATA-space
    # limits actually contain the text already drawn in them.
    #
    # This is a FIXED POINT, not one adjustment: text is a fixed number of
    # PIXELS, and under equal-aspect-by-box widening the data limits shrinks the
    # axes box, which makes the same glyphs occupy a larger fraction of it. One
    # pass on a long-back-focus doublet left two stamps still 1.2 px outside.
    # The loop is bounded and each pass only ever grows, so it terminates
    # whether or not it converges; a residual overflow is a smaller figure, not
    # a wrong one.
    for attempt in range(5):
        try:
            # Lay the box out FIRST. Before any draw the axes box is still the
            # default rectangle, so every extent measured against it is a
            # measurement of a layout the figure will not have — and the loop
            # would find nothing to fix, which is exactly what it did.
            fig.canvas.draw()
        except Exception:  # noqa: BLE001 — an un-drawable canvas stops the loop
            break
        if not _expand_limits_to_drawn_text(fig, ax):
            break
    caps = tuple(sorted(body.get("cap_surfaces") or ()))
    eligible = _aperture_disclosure_indices(
        optical_indices, stop_index, n, drawn_surfaces=caps)
    aperture_not_measured = [
        i for i in eligible
        if i < len(apertures) and not apertures[i].measured
    ]
    # The sag scan follows the same rule: a surface the figure DRAWS is a
    # surface whose sag inputs must be disclosed. `_is_suppressed_scaffold`
    # treats a `nan` radius as a flat powerless dummy (it tests
    # `not isfinite`), so a nan-radius cap left the optical set, was drawn flat
    # anyway, and never reached this list.
    scanned = sorted({int(i) for i in optical_indices} | set(caps))
    profile_not_measured = [
        i for i in scanned if 0 <= i < len(rows) and _sag_inputs_unreadable(rows[i])
    ]
    strings = _figure_disclosure_strings(
        rows=rows, folded=folded, projection=projection,
        config_identity=config_identity, open_groups=body["open_groups"],
        placeholder_groups=body["placeholder_groups"],
        aperture_not_measured=aperture_not_measured,
        interfaces_omitted=interfaces_omitted,
        profile_not_measured=profile_not_measured,
    )
    drawn, truncated = _place_disclosures(fig, ax, strings)
    drawn = list(drawn)
    # The scope footer is EXEMPT from the overflow rule: it is drawn
    # outside the stack, after the truncation decision, because it states
    # what the ABSENCE of a box means. Dropping it is the one truncation
    # that would make the figure less HONEST rather than merely less
    # complete.
    drawn.append(_draw_scope_footer(fig, ax))
    # The per-surface lists the result dict carries are computed HERE, beside the
    # strings that were drawn from them, so the figure and the result can never
    # disagree about what was disclosed. They ride back on the figure because the
    # draw signatures are contract; the lists are always COMPLETE even when the
    # canvas could not fit every box.
    fig._optivibe_layout_meta = {
        "aperture_not_measured": list(aperture_not_measured),
        "profile_not_measured": list(profile_not_measured),
        "rim_truncated": list(body["rim_truncated"]),
        "interfaces_omitted": list(interfaces_omitted),
        "disclosures_truncated": int(truncated),
        "figure_disclosures": list(drawn),
    }
    return drawn


def _draw_folded_global(
    plt, np, rows, n, title, stop_index, apertures, rims, draw_heights,
    degraded, global_frames, ray_data, draw_rays, projection, config_identity,
):
    """Draw a FOLDED system in the GLOBAL frame.

    Element vertices come from ``GetGlobalMatrix(i)[10:13]`` (the SAME global frame
    the RAGY/RAGZ rays use — coherent past the fold), and each outline point is the
    local ``(y, sag)`` transformed via ``global = vertex + R_rowmajor · local``.
    The flat rim extension is built in LOCAL space BEFORE that transform, so behind
    a fold it appears tilted in plot coordinates — which is correct, and which a
    global-frame extension would silently get wrong while passing every count test.

    A surface whose global frame is degraded is SUPPRESSED (no outline drawn at a
    bogus axis position), scoped to that surface.

    **Folded-frame caveat:** the stop ticks and the image-plane line below are
    drawn as unrotated global proxies. That is a PRE-EXISTING limitation this cycle
    does not fix and does not claim to have fixed; nothing here asserts those two
    pieces of furniture are coordinate-correct behind the fold.

    Returns ``(fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures)``.
    """
    fig, ax = plt.subplots(figsize=(13, 5), dpi=120)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")

    optical_indices = _optical_indices(rows, n)
    surface_labels = []
    # The folded path frames itself from the points it actually committed
    # (see the limits block), so the placeholder-inclusive h_max has no job
    # here and is not computed. h_geom scales what is DRAWN.
    h_geom = _plot_geom_height(apertures, optical_indices)
    arrow_len = h_geom * _STAMP_ARROW_LEN_FRAC

    def _frame(i):
        frame = global_frames[i] if i < len(global_frames) else None
        if frame is None or not frame.get("ok"):
            return None
        return frame

    def to_plot(surface, y, sag):
        """Local ``(y, sag)`` -> global plot ``(gz, gy)``; ``None`` when degraded."""
        frame = _frame(surface)
        if frame is None:
            return None
        _gx, gy, gz = _geom.sag_to_global(frame["R"], frame["vertex"], y, sag)
        return (gz, gy)

    all_gz = []
    all_gy = []

    groups = _glass_groups(rows, n, optical_indices)
    body = _draw_group_bodies(
        ax, np, rows, groups, rims, apertures, draw_heights, to_plot,
    )
    for gz, gy in body["points"]:
        all_gz.append(gz)
        all_gy.append(gy)

    callouts = []          # (surface_index, color)
    interfaces_omitted = []
    for i in optical_indices:
        frame = _frame(i)
        if frame is None:
            # Without a global vertex there is no honest place to draw or stamp.
            continue
        record = apertures[i] if i < len(apertures) else None
        measured = bool(record is not None and record.measured)
        internal = (
            i in body["grouped"] and i != 0 and _geom.is_cemented_interface(rows, i)
        )
        pts = []
        if i in body["drawn_caps"] or i in body["placeholder_members"]:
            pass
        elif internal:
            if measured:
                pts = _emit_profile(
                    ax, np, rows, i, draw_heights[i], to_plot,
                    width=_NARROW_LINE_PT, color="black", family="interface",
                )
            else:
                interfaces_omitted.append(i)
        elif measured:
            pts = _emit_profile(
                ax, np, rows, i, draw_heights[i], to_plot,
                width=_WIDE_LINE_PT, color="black", family="profile",
            )
        for pz, py in pts:
            all_gz.append(pz)
            all_gy.append(py)
        is_stop = (stop_index is not None and i == stop_index)
        callouts.append((i, "red" if is_stop else "black"))
        surface_labels.append(i)

    # --- optical axis + image plane (global frame) ------------------------- #
    axis_line = ax.axhline(0.0, color="black", linewidth=_NARROW_LINE_PT,
                           linestyle=_AXIS_DASHES, zorder=0)
    axis_line.set_gid("axis:optical")
    image_frame = _frame(n - 1)
    if image_frame is not None:
        image_line = ax.axvline(image_frame["vertex"][2], color="black",
                                linewidth=_NARROW_LINE_PT, zorder=2)
        image_line.set_gid(f"s{n - 1}:image_plane")
        callouts.append((n - 1, "0.4"))

    # --- stop ticks — drawn ONLY from a MEASURED stop aperture -------------- #
    stop_label = None
    stop_record = (
        apertures[stop_index]
        if (stop_index is not None and 0 <= stop_index < len(apertures))
        else None
    )
    stop_measured = bool(stop_record is not None and stop_record.measured)
    stop_frame = _frame(stop_index) if stop_index is not None else None
    if (stop_index is not None and stop_index in optical_indices
            and stop_frame is not None):
        stop_label = stop_index
        if stop_measured:
            zs = stop_frame["vertex"][2]
            vy_stop = stop_frame["vertex"][1]
            stop_semi = float(stop_record.height)
            tick_half = max(0.06 * h_geom, 0.04 * stop_semi)
            for y_edge in (vy_stop + stop_semi, vy_stop - stop_semi):
                tick, = ax.plot(
                    [zs, zs], [y_edge - tick_half, y_edge + tick_half],
                    color="#FF8C00", linewidth=_NARROW_LINE_PT,
                    solid_capstyle="butt", zorder=5,
                )
                tick.set_gid(f"s{stop_index}:tick")

    # --- surface-number stamps + leaders (anchored through the SAME closure) - #
    placements = []
    vertex_blocks = []
    for idx, color in callouts:
        record = apertures[idx] if idx < len(apertures) else None
        if record is None or not record.measured:
            vertex = to_plot(idx, 0.0, 0.0)
            if vertex is None:
                continue
            stamp = ax.text(vertex[0], vertex[1], f"{idx}?", ha="center",
                            va="bottom", fontsize=8, color=color, zorder=6)
            stamp.set_gid(f"s{idx}:stamp")
            all_gz.append(vertex[0])
            all_gy.append(vertex[1])
            # The word rides the MEASURED leader branch below, so an unmeasured
            # stop used to fall out here having stamped `k?` and nothing else —
            # the stop identified only by an unknown-aperture mark. The aperture
            # is unknown; WHICH surface is the stop is not, and the marker must
            # survive. (This is marker PRESENCE. The folded frame these anchors
            # sit in is the pre-existing ticketed defect and is not claimed
            # correct here.)
            word = _rider_word(idx, stop_index, n, at_vertex=True)
            if word is not None:
                _vw = ax.text(vertex[0], vertex[1], word[0], ha="center",
                              va="top", fontsize=7, color=word[1], zorder=6,
                              bbox={"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                                    "facecolor": "white", "edgecolor": "none",
                                    "linewidth": _NARROW_LINE_PT})
                _vw.set_gid(f"s{idx}:word")
                vertex_blocks.append(
                    {"surface": idx, "stamp": stamp, "word": _vw})
            continue
        rim = body["rim_by_surface"].get(idx)
        drawn_edge = (
            float(rim.rim_height)
            if rim is not None and rim.basis == "measured"
            else float(draw_heights[idx])
        )
        sag_hi, sag_lo = _edge_sag(np, rows, idx, float(draw_heights[idx]))
        interior = (
            idx in body["grouped"] and idx != 0
            and _geom.is_cemented_interface(rows, idx)
            and idx not in body["drawn_caps"]
        )
        arm = drawn_edge + arrow_len
        tip = to_plot(idx, float(draw_heights[idx]) if interior else drawn_edge,
                      sag_hi)
        label = to_plot(idx, arm, sag_hi)
        if tip is None or label is None:
            continue
        anno = ax.annotate(
            str(idx), xy=tip, xytext=label, ha="center", va="bottom",
            fontsize=8, color=color, zorder=6,
            arrowprops={"arrowstyle": "-" if interior else "->",
                        "lw": _NARROW_LINE_PT,
                        "color": color, "shrinkA": 1.0, "shrinkB": 1.0},
        )
        anno.set_gid(f"s{idx}:leader")
        placements.append({
            "anno": anno, "surface": idx, "side": 1, "base": arm,
            "sag": sag_hi, "to_plot": to_plot, "tier": 0, "rider": None,
        })
        if interior:
            _leader_dot(ax, idx, tip, color)
        # The limits are computed from the points the figure ACTUALLY
        # committed, the label anchor included. The previous form added a
        # heuristic multiple of h_max as headroom via max(y_hi, y_hi + k) --
        # unconditionally the second term, therefore not a maximum over
        # anything, and unable to respond to where a label really landed.
        all_gz.append(label[0])
        all_gy.append(label[1])
        word = _rider_word(idx, stop_index, n)
        if word is not None:
            # The word rides its number on the SAME side, through the SAME
            # `to_plot` closure — so behind a fold it rotates with the body
            # exactly as its number does. STOP used to be mirrored to the far
            # side of the axis (`-(drawn_edge + arrow_len)`), which put the word
            # and the number it belongs to on opposite sides of the drawing.
            anno.set_bbox({"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                           "facecolor": "white", "edgecolor": "none",
                           "linewidth": _NARROW_LINE_PT})
            rider = _word_rider(
                ax, to_plot, idx, 1, arm + _STOP_RIDER_ARM_FRAC * h_geom,
                sag_hi, word[0], word[1],
            )
            if rider is not None:
                placements[-1]["rider"] = rider
                r_pos = rider.get_position()
                all_gz.append(r_pos[0])
                all_gy.append(r_pos[1])

    # --- per-field chief + marginal rays (UN-SUPPRESSED for folds, nit 5a) -- #
    n_rays_drawn = _draw_rays(ax, plt, ray_data, draw_rays)
    if n_rays_drawn and ray_data:
        for field in ray_data.get("fields", []):
            for label in ("chief", "upper_marginal", "lower_marginal"):
                for p in field.get("rays", {}).get(label) or []:
                    all_gz.append(p[0])
                    all_gy.append(p[1])

    ax.set_title(title)
    ax.set_xlabel("z (global optical axis)")
    ax.set_ylabel("y (global)")
    # Equal DATA aspect, honoured by sizing the axes box (see _draw).
    ax.set_aspect("equal", adjustable="box")
    _hide_axes_chrome(ax)
    def _apply_limits():
        if all_gz and all_gy:
            z_lo, z_hi = min(all_gz), max(all_gz)
            y_lo, y_hi = min(all_gy), max(all_gy)
            z_pad = 0.08 * (z_hi - z_lo) if z_hi > z_lo else 1.0
            y_pad = 0.18 * (y_hi - y_lo) if y_hi > y_lo else 1.0
            ax.set_xlim(z_lo - z_pad, z_hi + z_pad)
            ax.set_ylim(y_lo - y_pad, y_hi + y_pad)
        else:
            ax.set_xlim(-1.0, 1.0)
            ax.set_ylim(-1.0, 1.0)

    _apply_limits()
    # Tier the overprinting labels, then re-frame from the points the figure
    # ACTUALLY committed — a tiered label anchor is one of them. Bounded, and it
    # stops as soon as the assignment is stable (see `_tier_colliding_stamps`).
    for _pass in range(_STAMP_TIER_PASSES):
        # Run for the POSITIONING, not only the verdict (see `_draw`): the pass
        # settles every rider even when it moves no stamp.
        _changed = _tier_colliding_stamps(fig, ax, placements)
        for p in placements:
            lz, ly = p["anno"].get_position()
            all_gz.append(lz)
            all_gy.append(ly)
            rider = p.get("rider")
            if rider is not None:
                # STOP is outboard of its number and therefore the extreme of
                # the stack — frame on it, not on the number it rides.
                rz, ry = rider.get_position()
                all_gz.append(rz)
                all_gy.append(ry + (ry - ly))
        _apply_limits()
        if not _changed:
            break

    # Same as the unfolded path: grade the vertex block against a canvas that
    # already carries the rays and outlines it has to clear.
    _clear_vertex_words(fig, ax, vertex_blocks)

    figure_disclosures = _finish_disclosures(
        fig, ax, rows=rows, folded=True, projection=projection,
        config_identity=config_identity, body=body,
        optical_indices=optical_indices, stop_index=stop_index, n=n,
        apertures=apertures, interfaces_omitted=interfaces_omitted,
    )
    return fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures


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

    ``config_title`` (int|None) records that a config was selected by the caller. The
    TITLE suffix no longer derives from it: the suffix reports the READ-BACK identity
    (``[config k of N]`` whenever the read-back count ``N > 1``, whether or not a
    config was requested), because a figure must state what it SHOWS, not what was
    asked for. The argument contract is unchanged.
    """
    title = params.get("title")
    if not isinstance(title, str) or title == "":
        title = _DEFAULT_TITLE
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

        # Aperture RECORDS (reading + provenance), the per-group rim heights and
        # the ONE draw-height map. `all_zero` is DERIVED from the records — there
        # is no second source of truth about what was measured.
        apertures, rims, draw_heights = _prepare_draw_geometry(rows, n)
        all_zero_semi = all(not a.measured for a in apertures)

        # The prescription-level projection check (coordinate-break terms + surface
        # types). Tri-state: unknown NEVER collapses to False.
        try:
            projection = _geom.read_projection_state(
                _ProjectionReader(lde, session.system), n, rows
            )
        except BaseException:  # noqa: BLE001 — a predicate fault is "not measured"
            projection = _geom.ProjectionState(
                out_of_plane=None, out_of_plane_surfaces=(),
                unreadable_surfaces=tuple(range(n)),
            )

        # The multi-configuration identity, read back and labelled (never defaulted).
        config_identity = _read_config_identity(session.system)
        if config_identity.count is not None and config_identity.count > 1 \
                and config_identity.active is not None:
            title = (
                f"{title} [config {config_identity.active} of "
                f"{config_identity.count}]"
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
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw_folded_global(
                plt, np, rows, n, title, stop_index, apertures, rims,
                draw_heights, degraded, global_frames, ray_data,
                effective_draw_rays, projection, config_identity,
            )
        else:
            # The all-refractive UNFOLDED path.
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw(
                plt, np, rows, n, title, stop_index, folded, apertures, rims,
                draw_heights, degraded, ray_data, effective_draw_rays,
                projection, config_identity,
            )
        _layout_meta = dict(getattr(fig, "_optivibe_layout_meta", {}) or {})

        # Atomic durability gate (§3.8): UNIQUE temp in the target dir -> _is_png ->
        # replace. A unique temp (tempfile.mkstemp) avoids the race where two
        # concurrent renders to the same path collide on a fixed "<name>.png.tmp" and
        # one returns ok:true for the other's figure (FIX 6).
        directory = os.path.dirname(attempted) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=directory)
        os.close(fd)  # mkstemp opens the file; savefig reopens by path.
        # bbox_inches="tight" crops the whitespace the box mechanism leaves
        # around a tall-or-wide axes. Measured against every consumer first:
        # nothing downstream reads pixel dimensions (the workspace gate is
        # magic-bytes + byte count, the montage imshow-s whatever it is
        # handed, no test asserts a size) -- and the catalog plotter already
        # ships this exact call.
        fig.savefig(tmp, dpi=120, format="png", bbox_inches="tight")
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
        rim_truncated = list(_layout_meta.get("rim_truncated", []))
        if rim_truncated:
            named = ", ".join(f"S{k}" for k in rim_truncated)
            notes.append(
                f"rim truncated at last valid sample on {named} "
                "(sag invalid at aperture edge)"
            )
        if all_zero_semi:
            notes.append("all semi-diameters read 0/non-finite; used h=1.0 fallback")
        if degraded:
            notes.append(f"surfaces {degraded} were unreadable and degraded")
        flags = list(ray_flags)  # additive machine-readable channel (rays + geometry)

        # GRIN (§6.2) disclosure: a GRIN surface's INTERNAL index profile is not
        # drawn — the drawn geometry is the Standard sphere/conic base (the index
        # profile does NOT perturb the sag, residual 0.0, so the figure is faithful for the
        # base geometry). NO ``_modelled``/``_approximate`` split (index profile ⊥ sag). A
        # degraded/unreadable row routes to the EXISTING degraded channel — never claimed
        # GRIN. Additive key + one flag, emitted ONLY when non-empty (a non-GRIN system is
        # byte-for-byte unchanged). Fail-safe: a resolver throw -> no GRIN disclosure.
        grin_index_profile_not_drawn = []
        try:
            from . import _grin_cells as _grin
            for i in range(n):
                if rows[i].get("unreadable", False):
                    continue  # degraded -> the existing degraded channel, never claimed GRIN
                if _grin.grin_type_of_name(str(rows[i].get("type_name", ""))) is not None:
                    grin_index_profile_not_drawn.append(i)
        except Exception:  # noqa: BLE001 — a GRIN resolver hiccup -> no GRIN disclosure
            grin_index_profile_not_drawn = []
        if grin_index_profile_not_drawn:
            flags.append(
                "GRIN: internal index profile not drawn (surfaces "
                f"{grin_index_profile_not_drawn}); the drawn/audited geometry is the "
                "Standard sphere/conic base — bulk-index manufacturability is not audited"
            )

        # A folded figure where TRULY NOTHING drew is a blank (neutral-axis)
        # render — flag it so the ok:true result is honest about the empty figure. The
        # Residual: the flag must NOT over-fire when ONLY the image-plane line
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

        # Shipped coverage tokens ONLY — nothing invented, and no clean/pass token.
        coverage_reasons = []
        if _layout_meta.get("aperture_not_measured"):
            coverage_reasons.append(_REASON_APERTURE)
        if folded:
            coverage_reasons.append(_REASON_FOLDED)
        if grin_index_profile_not_drawn:
            coverage_reasons.append(_REASON_GRIN)

        _result = {
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
            # --- aperture / projection / configuration provenance (additive) --- #
            # Always present, may be []; NEVER truncated by what fitted on the
            # canvas. The absence of an entry is not labelled as anything: this
            # renderer audits nothing and emits no clean/pass token.
            "aperture_not_measured": list(
                _layout_meta.get("aperture_not_measured", [])),
            "profile_not_measured": list(
                _layout_meta.get("profile_not_measured", [])),
            "coverage_reasons": coverage_reasons,
            "rim_truncated": rim_truncated,
            "out_of_plane": projection.out_of_plane,
            "out_of_plane_surfaces": list(projection.out_of_plane_surfaces),
            "projection_unreadable_surfaces": list(
                projection.unreadable_surfaces),
            "out_of_plane_scope": "cb_terms_and_surface_types",
            "configuration_count": config_identity.count,
            "figure_disclosures": list(figure_disclosures),
            "disclosures_truncated": int(
                _layout_meta.get("disclosures_truncated", 0)),
        }
        # GRIN (§6.2): the additive index-not-drawn surface list — emitted ONLY when
        # non-empty (a non-GRIN system stays byte-for-byte unchanged).
        if grin_index_profile_not_drawn:
            _result["grin_index_profile_not_drawn"] = grin_index_profile_not_drawn
        return _result
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
        "Draw a real meridional (y-z) optical layout PNG for the user, in an "
        "ISO 10110-inspired visual style (a LOOK borrowed from optical drawing "
        "practice — this is NOT a standards drawing and nothing here is a "
        "conformance claim): equal-aspect (true curvature), cement-aware closed "
        "element outlines with a FLAT rim, two line weights, a patterned optical "
        "axis + image plane, a STOP marker, OUR stamped surface numbers (how the "
        "user points at a surface), and — unless draw_rays=False — the chief + "
        "upper/lower marginal ray of each field, retained on purpose because ray "
        "bending is the strongest cue that a system is sane (one color per field). "
        "2-D meridional cross-section only — there is no other view. A group's rim "
        "height is the maximum MEASURED clear semi-diameter over the group: an "
        "approximation forced by the absence of any part-boundary data, and it is "
        "not a part dimension. An aperture that could not be read is DISCLOSED "
        "(drawn as unknown, never as a number nobody measured), as is a projection "
        "the check cannot vouch for; one active configuration is drawn and labelled. "
        "Returns the saved PNG path plus png_valid/n_fields/n_rays_drawn/"
        "aperture_not_measured/profile_not_measured/out_of_plane/figure_disclosures/"
        "cb_suppressed/scaffold_suppressed/flags; inspect result.ok. Folded systems "
        "(coordinate break / mirror) are drawn in the GLOBAL frame, coherent with "
        "the rays — so the rays ARE drawn for a fold; coordinate-break and flat "
        "powerless air dummy/spacer surfaces are suppressed (scaffolding, not "
        "drawn); real optics (glass, mirrors, curved lens-backs), the stop, and the "
        "image are stamped with their true Zemax numbers. Gotcha: this is a "
        "self-drawn headless figure (native export writes text, not an image) — for "
        "native fidelity, open the saved .zmx. See describe_surfaces, fold_beam."
    ),
)

TOOL_SPECS = (RENDER_LAYOUT_SPEC,)
