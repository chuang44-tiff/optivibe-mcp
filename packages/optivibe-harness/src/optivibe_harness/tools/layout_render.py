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
import re
import tempfile
from dataclasses import dataclass as _dataclass
from dataclasses import replace as _dc_replace
from time import perf_counter

from .. import artifact_naming as _naming
from ..artifact_sink import _ILLEGAL_CHARS as _ILLEGAL_STEM_CHARS
from ..artifact_sink import _safe_name
from ..errors import ToolParamError
from . import _workspace_paths as _wsp  # S-1 — cycle-safe (never imports layout_render)
from ..server import ToolSpec
from ..server import _wedge_recorded as _slot_wedged
from . import _asphere_cells as _asph
from . import _config_common as _cfg
from . import _layout_geometry as _geom
from . import _layout_rays as _rays
from . import _layout_native as _native
from . import _layout_register as _lr
from . import _ray_coverage as _rc
from ._image_gate import _is_png, _png_ihdr

_DEFAULT_TITLE = "Layout"
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
# union's PREDICATE is pinned directly by
# `test_u3_a_block_is_one_occupancy_unit_for_collision`, which is the part a unit
# test can reach; nobody has yet built a fixture that discriminates its EFFECT.
# That regression lives in the development suite and is
# not shipped with this package.
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
#: Clear space between two stacked disclosure boxes. THE NAME OVERSTATES WHAT IS
#: DELIVERED, and the number is left alone deliberately.
#:
#: The use site divides this constant by the FIGURE height in pixels and uses the
#: result as an AXES fraction, so the gap actually laid down is
#: ``4 * axes_height_px / figure_height_px`` DEVICE PIXELS — not 4 points, and not
#: 4 pixels either. It carries neither the pt -> px factor (``dpi / 72``) nor the
#: axes-to-figure ratio, so the delivered spacing moves with BOTH the dpi and the
#: axes' share of the canvas.
#:
#: Correcting the arithmetic would move every box in every figure, so this release
#: corrects the CLAIM and defers the delivery. Compare ``_FOOTER_GAP_PT``, which
#: applies ``dpi / 72`` and works in pixel space throughout, and therefore does
#: deliver points.
_DISCLOSURE_GAP_PT = 4.0
#: dogfood F-4: the legend seat search steps down in this many points per candidate.
_LEGEND_STEP_PT = 2.0
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
#: The MEASURED/EXTENDED sibling of ``_S_AP_INT``. ONE
#: aggregated string per figure naming every affected surface, the effective
#: token and the envelope key that carries the band widths.
#:
#: It asserts that the band is a DRAWING CONVENTION and not a measurement, and it
#: deliberately does NOT claim to be the figure's only synthetic band — other
#: conventions widen other ink and this string does not speak for them.
#:
#: **It says NOTHING about what any vendor's software draws for an internal
#: cemented interface. That is UNMEASURED**, and no wording here,
#: in the code around it, or in any test name may imply otherwise.
_S_AP_INT_EXT = (
    "S{ks} INTERNAL INTERFACE{s} DRAWN BEYOND MEASURED APERTURE — drawing "
    "convention (element_outline={token}), not a measurement; band widths in "
    "interfaces_extended."
)
#: RESERVED for the OUTER-CAP extension class.
#: Deliberately DEFINED-AND-UNUSED: the cap class is not disclosed yet,
#: and naming the constant here is what stops the next author reaching for
#: ``_S_AP_INT_EXT`` — which is scoped to cemented JOINS — to cover caps too.
_S_AP_EXT = (
    "S{ks} ELEMENT OUTLINE{s} DRAWN BEYOND MEASURED APERTURE — drawing "
    "convention (element_outline={token}), not a measurement; band widths in "
    "caps_extended."
)
_S_OVERFLOW = "{n} MORE DISCLOSURES NOT SHOWN — full list in the result."
# native-layout-retool: the shared far-object rule's figure strings, placed as
# FIGURE-level captions under the scope footer (`_draw_far_captions`).
_S_FAR = "S{k} {ROLE} NOT DRAWN: {gap:g} mm away ({ratio:.0f}x lens length)."
_S_FAR_UNCAPTIONED = "Far-object exclusion applied; caption unavailable."
_S_SPECK = ("Lens fills {fill:.0%} of the frame and no end qualifies for exclusion "
            "(speck_no_cut).")
_S_SAG = (
    "S{k} PROFILE NOT MEASURED — sag inputs unreadable (radius/conic/coefficients)."
)
_S_SAG_PARAXIAL = (
    "S{k} PROFILE NOT MEASURED — a paraxial surface: an ideal element with no sag to "
    "draw."
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
#: The THIRD member of the ``_S_GRP_*`` family: a cemented group that declined to
#: split under ``per_element`` and therefore drew the GROUPED geometry — one body
#: at the group rim — while the envelope echoes ``per_element`` (external
#: audit).
#:
#: **Why a new string rather than an existing channel.** The adjacent fact is
#: ``_S_GRP_PLACEHOLDER``, which says a body's height is fabricated; this says
#: something else — the body's HEIGHT is measured, its PARTITION is not the one
#: the caller asked for. An all-placeholder group is the case those two coincide
#: on, and there the shipped placeholder string already carries it, so this string
#: is NOT emitted for it (that no-op is sanctioned, and the shipped string is its
#: disclosure). What was missing is the PARTLY-measured group: the same rule grants
#: the no-op to an all-placeholder group, while the exclusion is a condition on
#: each PAIR, so a group with one all-placeholder pair beside measured members
#: also declines — and until this string existed it drew ``grouped`` ink under a
#: ``per_element`` label with nothing on the figure saying so.
#:
#: The fallback itself is CORRECT and is not widened: splitting such a group would
#: take a pairwise rim from the max of two fabricated heights, which is the
#: prohibition the exclusion exists to honour. The defect was the silence.
#:
#: It says NOTHING about what any vendor's software draws for a cemented group
#: — only which of OUR two conventions produced OUR ink.
#: **LENGTH IS A CONTRACT HERE, not a style preference.** The placer requires every box
#: to lie fully inside the canvas, and the disclosure stack starts at an axes
#: fraction — so on a FOLDED figure, whose axes are narrow, a long sentence runs
#: off the right edge. Measured: a first draft naming the blocking pair and
#: quoting three clauses was 1921 px wide against a 1560 px canvas and reddened
#: `test_i3_disclosure_boxes_never_overlap_and_stay_on_canvas`. WHICH pair
#: blocked the partition therefore rides `groups_not_split`, not the figure —
#: the same division of labour `_S_AP_INT_EXT` has with `interfaces_extended`.
_S_GRP_NOT_SPLIT = (
    "S{a}-S{b} DRAWN AS ONE BODY — element_outline={token} not applied here "
    "(gap_edge_not_measurable); see groups_not_split."
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


_SCRATCH_LAYOUT_RE = re.compile(r"^layout_(\d+)\.png$")


def _scratch_layout_index(name):
    """The index a minted scratch layout name carries, else ``None``."""
    match = _SCRATCH_LAYOUT_RE.match(name)
    return int(match.group(1)) if match else None


def _resolve_path(session, path, exact_path=None):
    """Resolve the output PNG path. Returns ``(path, minted, error)``.

    Three cases:

    * ``exact_path`` — an INTERNAL caller (``save_candidate``) that has already
      composed the name through the naming authority. The stem is validated for
      filesystem legality and length and used UNCHANGED: re-sanitising a name that
      is already canonical is a second resolution of a resolved value, and at a long
      label it TRUNCATES the picture's stem away from its ``.zmx``.
    * a user-supplied ``path`` — today's sanitisation, unchanged. The caller asked
      for that file, so overwrite semantics are unchanged too.
    * nothing — a SCRATCH figure. It is minted as
      ``<root>/candidates/scratch/layout_<NNNN>.png`` from the directory LISTING.
      **If the listing FAILS the tool REFUSES and writes nothing**: a refusal cannot
      clobber anything, so "an existing figure is never destroyed" holds with no
      reservation, no descriptor and no cleanup semantics. Falling back to index 1
      on an unreadable directory is exactly how the ninth render overwrites the
      first. The bare ``layout.png``-in-CWD branch survives only when NO root
      resolves at all.
    """
    if exact_path:
        stem = os.path.splitext(os.path.basename(exact_path))[0]
        if (not stem or len(stem) > 255
                or any(ch in stem for ch in _ILLEGAL_STEM_CHARS)
                or any(ord(ch) < 32 for ch in stem)):
            return None, False, (
                f"the composed figure stem is not a legal filename: {stem!r}")
        return exact_path, False, None
    if path:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, ext = os.path.splitext(base)
        if not ext:
            ext = ".png"
        safe_stem = _safe_name(stem)
        resolved = (os.path.join(directory, f"{safe_stem}{ext}")
                    if directory else f"{safe_stem}{ext}")
        return resolved, False, None
    # A resolution FAULT is not permission to use a fixed name. The bare
    # ``layout.png``-in-CWD branch survives ONLY where no root is cleanly configured
    # (the documented last resort); where the resolver RAISED, the root is UNKNOWN and
    # a fixed name would silently overwrite the previous figure -- so this refuses,
    # through the same channel an unlistable directory already uses.
    scratch, scratch_fault = _wsp.scratch_dir_state(session)
    if scratch_fault is not None:
        return None, False, scratch_fault
    if not scratch:
        return "layout.png", False, None
    index = _naming.next_index_in_dir(scratch, _scratch_layout_index)
    if index is None:
        return None, False, (
            f"the scratch figure directory exists but could not be listed, so a "
            f"free name cannot be minted and an existing figure could be "
            f"overwritten: {scratch}. Pass an explicit path, or fix the "
            f"directory's permissions.")
    return os.path.join(scratch, f"layout_{index:04d}.png"), True, None


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


#: The ``element_outline`` convention vocabulary. These two
#: literals are the ONLY accepted values -- there is NO case or alias
#: normalisation, deliberately: a silent fallback on a convention selector is a
#: silent-wrong, so an unrecognised value is REFUSED rather than coerced.
_ELEMENT_OUTLINE_VALUES = ("grouped", "per_element")
#: The DEFAULT convention. Was ``"grouped"``; switched to ``"per_element"`` by
#: owner ruling. The
#: ruling's recorded ground is that ``grouped`` draws a vertical at a group rim no
#: ground surface reaches, and that ``per_element`` fabricates LESS aperture. It is
#: NOT an edge-thickness claim: no rim in this figure is a manufacturing edge
#: thickness, and `check_clearance` is unaffected by this constant.
_ELEMENT_OUTLINE_DEFAULT = "per_element"


def _resolve_element_outline(params):
    """Return the effective ``element_outline`` token, or ``None`` if unrecognised.

    An ABSENT key takes the default. Every other value must be one of the two
    literals EXACTLY, so ``"Grouped"``, ``"per element"``, ``"grouped "``, ``3``
    and an explicit ``None`` all return ``None`` and the caller refuses.
    ``True`` is rejected by the ``isinstance(value, str)`` test, which ``bool``
    does not satisfy.

    NEVER raises: a non-dict ``params`` is normalised by the caller first.
    """
    if "element_outline" not in params:
        return _ELEMENT_OUTLINE_DEFAULT
    value = params.get("element_outline")
    if isinstance(value, str) and value in _ELEMENT_OUTLINE_VALUES:
        return value
    return None


#: The ``renderer`` vocabulary (native-layout-retool). Exact tokens,
#: no aliasing, no case-folding -- the element_outline rule.
_RENDERER_VALUES = ("native", "self", "native_3d", "native_shaded")
#: A7: THE switch point -- the ONLY place in src/ that names the default renderer,
#: read in exactly one place (`_resolve_renderer`). The served description never says
#: which token is the default, so flipping this constant is CODE-ONLY for release
#: purposes: it needs NO hand-declared description change.
_DEFAULT_RENDERER = "native"
#: The tokens this build can draw. Every other member of `_RENDERER_VALUES` is
#: refused "not available in this build" (the fixed refusal order, third rule).
#:: every token is built -- the native cross-section AND the two 3-D views.
_RENDERERS_BUILT = ("self", "native", "native_3d", "native_shaded")
#: (export_3d): the native 3-D tokens -> the `_layout_native.export_3d` kind.
#: Only ever EXPLICIT (the default never selects a 3-D view), so every failure token
#: maps to a refusal through `_NATIVE_EXPLICIT_FAMILY`, never to a fallback.
_RENDERERS_3D = {"native_3d": "viewer", "native_shaded": "shaded"}
_S_3D = "UNSTAMPED 3-D VIEW - OpticStudio {kind}; no surface numbers are drawn."
_S_3D_KIND = {"viewer": "3-D viewer export", "shaded": "shaded model export"}
_S_3D_COLOUR_NULL = "3-D ray-colour consistency check not available ({reason})"
#: Why a DEFAULT render fell back from its first choice. Frozen; order = the
#: check order. Declared here so a test can discover it from the module. Since
#: the default is `native`: a default render that cannot run natively falls back to
#: `self` with one of these (an EXPLICIT native is refused instead; an explicit
#: `self` never falls back).
_RENDERER_FALLBACK_TOKENS = (
    "non_axial", "configuration_unreadable", "grin_surface", "extent_unreadable",
    "registration_unmodelled", "native_unavailable", "native_settings_unverified",
    "native_export_failed", "native_not_png", "registration_unverified",
)
#: What an EXPLICIT `renderer="native"` returns for each token -- a refusal family,
#: or `None` for the two registration tokens, which ship the native picture UNSTAMPED.
_NATIVE_EXPLICIT_FAMILY = {
    "non_axial": "render_unavailable",
    "configuration_unreadable": "render_unavailable",
    "grin_surface": None,              # explicit native proceeds + a flag
    "extent_unreadable": "render_unavailable",
    "registration_unmodelled": None,   # unstamped native
    "native_unavailable": "render_unavailable",
    "native_settings_unverified": "render_failed",
    "native_export_failed": "render_failed",
    "native_not_png": "render_gate_failed",
    "registration_unverified": None,   # unstamped native
}
#: The SELF-only envelope keys, removed from a native envelope (the vendor drew
#: the ink these describe, so under native they would describe nothing).
_NATIVE_INK_KEYS = ("element_outline", "asphere_sag_modelled", "asphere_sag_approximate",
                    "rim_truncated", "n_rays_drawn", "effective_draw_rays", "n_fields")
# --- the native overlay canvas -- all [unmeasured choice] unless tagged ---- #
_STAMP_ARM_PX = 36        # clears the 5 % margin at H>=400
_GUTTER_STAMP_PX = 170    # arm 36 + _STAMP_TIER_MAX x ~16 px + a rider box
_GUTTER_LINE_PX = 14      # disclosure strip: 14 px per line + 6
_GUTTER_CAPTION_PX = 22   # title strip
_GUTTER_MAX_LINES = 8     # disclosure strip cap (the self render caps by axes fit)
_MARKER_INSET_PX = 10     # far-end marker row: this far above the bottom gutter's edge
_OVERLAY_DPI = 100        # figsize = px / dpi at dpi -> integer output pixels
#: The `registration` keys `_register_and_verify` measures (plus the
#: curve-interior check). One tuple: the envelope copy reads this list.
_REGISTRATION_INFO_KEYS = (
    "bbox_sides_graded", "bbox_residual_px", "tip_points_checked", "tip_residual_px",
    "ambiguous_surfaces", "subpixel_surfaces", "profile_points_checked",
    "profile_residual_px", "profile_unverified_surfaces", "profile_unchecked_surfaces",
    "profile_not_applicable", "profile_unmeasurable_surfaces",
)
_S_WITHHELD = ("STAMPS WITHHELD - registration {status}; no surface numbers are drawn "
               "(see result.registration).")
_S_FALLBACK = ("Drawn by OptiVibe's own renderer: the native export could not be used "
               "({token}).")
#: R-6 (MEASURED, captures/probe_native_layout_slot.json): 6 of 6 raising export runs
#: left the engine's single tool slot unusable -- OpenCrossSectionExport,
#: OpenLocalOptimization and OpenBatchRayTrace all returned None -- and only a NEW
#: session recovered it. The FACT (this run raised) is stated; that THIS session's slot
#: is wedged is the measured consequence, phrased as "likely".
#: Served only when the latch is ON RECORD -- chosen by
#: re-reading the dispatcher's own arbiter (``_slot_latched``), never the verdict.
_S_SLOT_RAISED = (
    "the export run RAISED; a raising export was measured (6 of 6) to wedge the "
    "engine's single tool slot, so this session's tool slot is now WEDGED: the session "
    "refuses every slot tool (the native export, optimize, batch ray traces) and "
    "saving or loading a design, which were measured to block a wedged engine -- "
    "restart the MCP process (restart the session or reconnect the optivibe server), "
    "then load_design the last design saved before the wedge")
#: The raise is NOT on record (the observation was missing, failed, disagreed, or the
#: latch write failed), so the session will NOT refuse the save / load tools that
#: block a wedged engine. Self-contained: it points at no other flag.
_S_SLOT_RAISED_UNLATCHED = (
    "the export run RAISED; a raising export was measured (6 of 6) to wedge the "
    "engine's single tool slot, but this session has no record of that latch, so it "
    "will NOT refuse the slot, save and load tools "
    "that were measured to block a wedged engine -- do not save or load from this "
    "session: restart the MCP process (restart the session or reconnect the optivibe "
    "server), then load_design the last design saved before the wedge")
#: The degrader rule: on a session whose tool-slot latch is set, the
#: native export AND the ray read are NOT attempted ([measured]: every later open
#: returned None). Replaces 's `_S_SLOT_PRIOR_RAISE` (the advisory latch is gone).
_S_SLOT_LATCHED = (
    "not attempted: this session's tool slot is latched wedged (a native export was "
    "observed to wedge it); the native export and the ray trace would open nothing "
    "until a new engine session -- restart the MCP process to recover")
#: The wedge SIGNATURE without a raise, latched.
_S_SLOT_SIGNATURE = (
    "native export: the tool's Close() returned False and it still reported running "
    "(the measured wedge signature); this session's tool slot is now latched wedged")
#: The ABSENT/UNREADABLE band: NO latch, one flag.
_S_SLOT_UNCONFIRMED = (
    "native export: the tool's Close() did not confirm the slot was released "
    "(Close() {close!r}, IsRunning {running!r}); the slot may be unusable")
_S_SLOT_NOT_RECORDED = (
    "tool-slot observation not recorded (session has no observe_tools_slot)")
_S_SLOT_OBSERVE_FAILED = "tool-slot observation failed ({exc}); not recorded"
#: The ONE figure line for a sampled-ray failure (one line whatever the
#: field count). Conditional wording -- the drawing MAY omit or cut them short.
_S_RAYS_MISSING = (
    "SAMPLED RAYS FAILED for field(s) {fields}: {n_failed} of {n_sampled} sampled rays "
    "did not reach the image; the drawing may omit or cut them short. See "
    "ray_coverage.")


class _RenderParams(dict):
    """The normalised params COPY, plus the one internal fact that is not a served
    parameter: ``renderer_strict`` (was the renderer given explicitly)."""

    renderer_strict = False


def _resolve_renderer(params):
    """-> ``(effective_token, strict)`` or ``None`` (refuse). Never raises.

    Key absent / ``None``: the default, UNLESS ``element_outline`` was given, which is
    a self-only convention and so selects ``self`` (strict). Otherwise the value must
    be an exact member of ``_RENDERER_VALUES``.
    """
    value = params.get("renderer")
    if value is None:
        if "element_outline" in params:
            return "self", True
        return _DEFAULT_RENDERER, False
    if isinstance(value, str) and value in _RENDERER_VALUES:
        return value, True
    return None


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


def _edge_sag_checked(np, rows, i, half_height):
    """``_edge_sag``'s sampling, EXPOSING validity: ``(sag_hi, sag_lo, valid, h_used)``.

    ``valid`` iff the samples at ``+half_height`` AND ``-half_height`` are themselves
    valid (``h_used == half_height``); otherwise the fallback ``_edge_sag`` would have
    returned, with ``valid=False`` and the height it came from (``0.0`` for a fully
    masked profile). ``_edge_sag`` itself is untouched, so the self path is unchanged.
    """
    h = float(half_height)
    y = np.linspace(-h, h, _N_SAMPLES)
    z, valid = _geom.sag_profile(
        rows[i]["radius"], rows[i]["conic"], y,
        coeffs=rows[i].get("aspheric_coefficients"),
        norm_radius=rows[i].get("asphere_norm_radius"),
        power=rows[i].get("asphere_power"),
    )
    idx = [k for k in range(len(y)) if bool(valid[k])]
    if not idx:
        return 0.0, 0.0, False, 0.0
    hi, lo = idx[-1], idx[0]
    ends_valid = hi == len(y) - 1 and lo == 0
    h_used = h if ends_valid else float(min(abs(y[hi]), abs(y[lo])))
    return float(z[hi]), float(z[lo]), bool(ends_valid), h_used


def _emit_profile(ax, np, rows, i, half_height, to_plot, *, width, color,
                  family="profile", rim_height=None, extensions=None,
                  truncations=None):
    """Draw ONE surface's own sag profile in plot space; return its points.

    Every per-surface artist carries ``gid = "s{i}:{family}"`` so a test can ask
    WHICH surface owns an artist instead of guessing from coordinates — a
    coordinate can legitimately coincide with a fabricated value, so numeric
    equality is not provenance.

    ``rim_height`` extends this profile FLAT from its own
    aperture edge out to the group rim. **It is NOT a bigger ``half_height``, and
    the difference is the whole point.** ``half_height`` is the span this function
    SAMPLES THE CURVE over, so raising it sweeps the curved surface outward and
    draws a swooping arc through the band the caps close flat across — it looks
    almost right, it reintroduces the sloped edge the flat-rim rule was built to remove, and
    no count-, gid- or ``draw_heights``-based test in this repo can see it. The
    extension is therefore DELEGATED to ``_geom.extend_profile_to_rim``, which
    appends a vertex at constant local sag.

    **Built in LOCAL space, BEFORE ``to_plot``** — load-bearing on the folded
    path, where the extension is not axis-aligned in plot coordinates and a
    plot-space extension would be a horizontal rim in the wrong frame.

    ``extensions``, when given, is the caller's accumulator: on an extension this
    records ``{surface: (own_semi, drawn_to)}`` in LOCAL radial mm, taken from the
    ``extend_profile_to_rim`` OUTPUT. It is written ONLY after the artist is
    actually committed, so a suppressed profile (a degraded ``to_plot``, an empty
    or single-point mask) records nothing — ink only.

    ``own_semi`` is the surface's MEASURED semi (``half_height``, the one shared
    ``draw_heights`` map), never the largest surviving SAMPLE.
    The two differ exactly when the sag mask truncates inside the aperture, and
    that is the case where the distinction matters: the record's whole job is to
    say how much of the ink was measured and how much was not.

    ``truncations``, when given, is the caller's set of surfaces whose profile
    stopped SHORT of their own measured aperture because the sag samples masked
    out. That band is drawn flat and was never sampled, so it is disclosed — on
    the shipped ``rim_truncated`` channel, the same concept
    ``build_group_section`` already reports for a truncated CAP. Written under
    the same ink-only rule as ``extensions``.
    """
    y = np.linspace(-float(half_height), float(half_height), _N_SAMPLES)
    z, valid = _geom.sag_profile(
        rows[i]["radius"], rows[i]["conic"], y,
        coeffs=rows[i].get("aspheric_coefficients"),
        norm_radius=rows[i].get("asphere_norm_radius"),
        power=rows[i].get("asphere_power"),
    )
    # Mask to the VALID samples in LOCAL space first. The pre-extension sequence
    # is element-for-element what the shipped loop emitted, so `rim_height=None`
    # leaves this function's output byte-identical.
    ys_local = []
    sags_local = []
    for k in range(len(y)):
        if not bool(valid[k]):
            continue
        ys_local.append(float(y[k]))
        sags_local.append(float(z[k]))

    record = None
    truncated = False
    if rim_height is not None and ys_local:
        # This is THE SURFACE'S ONE MEASURED SEMI, read from the
        # shared `draw_heights` map this call is already sampling over — NOT
        # `max(|ys_local|)`, which is merely the largest SURVIVING sample. The
        # two coincide on a sphere and diverge when the conic radical masks out
        # INSIDE the aperture, and publishing the sample artefact put a
        # sampling-grid number in the one field of the record that must be a
        # measurement, underneath a string asserting exactly that.
        own_semi = float(half_height)
        # Ink drawn flat from the last valid sample out to the measured edge is
        # a band NOBODY SAMPLED. It is not the synthetic band — that is the part
        # past `own_semi`, carried by `interfaces_extended` — so it rides the
        # shipped truncation channel instead of inventing a second vocabulary.
        if max(abs(v) for v in ys_local) < own_semi:
            truncated = True
        y_ext, sag_ext = _geom.extend_profile_to_rim(
            ys_local, sags_local, rim_height)
        # The DOCUMENTED identity return (the SAME objects) is the "no extension
        # needed" signal, and it is the signal the disclosure reads. Comparing
        # VALUES here would call an equal-semi cap "extended".
        if y_ext is not ys_local or sag_ext is not sags_local:
            ys_local = [float(v) for v in y_ext]
            sags_local = [float(v) for v in sag_ext]
            drawn_to = max(abs(v) for v in ys_local)
            # A record ADMITS ink drawn past the MEASUREMENT, so it is published
            # only when there is such ink. The identity return alone no longer
            # decides that: a truncating mask ends the profile short of its own
            # aperture, so a join already AT its group rim still takes an
            # extension vertex while nothing is drawn past its measured semi.
            # Recording that would put "DRAWN BEYOND MEASURED APERTURE" — and a
            # zero-width band — on a surface that was not drawn beyond it.
            if drawn_to > own_semi:
                record = (float(own_semi), float(drawn_to))

    pts = []
    for yy, ss in zip(ys_local, sags_local):
        p = to_plot(i, yy, ss)
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
    if extensions is not None and record is not None:
        _record_extension(extensions, i, record[0], record[1])
    if truncations is not None and truncated:
        # Same ink-only rule as the extension record: past every early return,
        # after the artist is committed.
        truncations.add(int(i))
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


def _surface_drawn_points(ax, body, surface):
    """Every point the figure COMMITTED for ``surface``, in PLOT space.

    Read from the INK, never re-derived from the planner: the per-surface artist
    when one exists (``s{i}:interface`` / ``s{i}:profile``), else the closed body
    polygon of the group that draws this surface as one of its caps. A surface
    the figure never stroked yields ``()`` and the caller leaves the placement
    alone — there is no honest place to move a mark toward ink that does not
    exist.
    """
    gids = (f"s{surface}:interface", f"s{surface}:profile")
    for line in ax.get_lines():
        if (line.get_gid() or "") in gids:
            return [(float(x), float(y))
                    for x, y in zip(line.get_xdata(), line.get_ydata())]
    for section in body.get("sections") or ():
        if int(surface) in tuple(int(s) for s in section.surfaces):
            return [(float(p[0]), float(p[1])) for p in section.polygon]
    return []


def _snap_to_drawn(point, pts):
    """``point`` moved to the NEAREST committed vertex (the placement falsifier).

    The dot is placed at the surface's own measured semi paired with the sag of
    its LAST VALID SAMPLE. On a fully-sampled surface those are the SAME point,
    so this returns it unchanged at distance 0 and every such figure stays
    byte-identical. They diverge exactly when the sag mask truncates INSIDE the
    aperture: the curve stops short, and the naive point then lies on no drawn
    artist at all — under ``per_element`` it floats in empty space.

    The falsifier rules that the PLACEMENT moves to the nearest drawn
    vertex. The MEANING is unchanged: the dot still marks where the measured
    aperture ends. The leader's tip moves WITH the dot, because the dot IS that
    leader's terminator — leaving the arrow at the floating point would detach
    the line from the mark it ends in.
    """
    if point is None or not pts:
        return point
    px, py = float(point[0]), float(point[1])
    return min(pts, key=lambda q: (q[0] - px) ** 2 + (q[1] - py) ** 2)


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


def _tier_colliding_stamps(fig, ax, placements, *, unit_px=None):
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

    ``unit_px`` (native overlay only): display pixels per unit of the ``to_plot``
    ARGUMENTS. On the self figure the data units ARE those units, so the scale is
    read off ``ax.transData`` (``None``, the default -- unchanged). On the native
    overlay the axes' data space is PIXELS while ``to_plot`` still takes local
    millimetres, so the caller passes the registration's px/mm; reading it off
    ``transData`` there would add a pixel step to a millimetre arm.
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
    if unit_px is not None:
        px_per_unit = float(unit_px)
        data_px_per_unit = px_per_unit     # data units are pixels on the overlay
    else:
        try:
            (_zx0, py0), (_zx1, py1) = ax.transData.transform(
                [[0.0, 0.0], [0.0, 1.0]])
        except Exception:  # noqa: BLE001 — an untransformable axes leaves labels alone
            return 0
        px_per_unit = abs(py1 - py0)
        data_px_per_unit = 1.0
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
            # and another 44.80 -> 42.27 percent of frame.
            # `test_u3_a_block_is_one_occupancy_unit_for_collision` pins the union
            # PREDICATE directly, which is the part a unit test can reach. That
            # regression lives in the development suite and is
            # not shipped with this package.
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
            if not (tier == p["tier"]
                    and abs(pos[1] - was[1]) <= 0.02 * step * data_px_per_unit):
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


def _rim_supersedes(rim, prior):
    """Should ``rim`` replace ``prior`` as a surface's recorded rim?

    🔴 **THE ONE RULE-BEARING LINE OF THIS CONVENTION.** Under ``per_element`` a
    SHARED cemented surface belongs to BOTH adjacent elements, so the writer walks
    over it TWICE. The shipped writer was a plain assignment — LAST-WRITE-WINS —
    and last-write DIVERGES from the correct union ``max(rim(E_left),
    rim(E_right))`` on **10 of the 15 shared interfaces in the measured corpus**.
    The 5 where they coincide are exactly the REVERSED-ordering groups, one of
    which is the group the original demo showcased: a fixture built on it passes
    while the rim is wrong nearly everywhere else.

    The rule is ``resolve_group_rims`` rule 1 carried to the writer:

    1. nothing recorded yet -> take it;
    2. a MEASURED rim always displaces a PLACEHOLDER one — a fabricated height may
       neither raise a rim nor hold one against a measurement;
    3. a PLACEHOLDER rim never displaces a MEASURED one;
    4. between two rims of the SAME basis, the greater ``rim_height`` wins.

    Under ``grouped`` each surface is written exactly once, so this returns True on
    the first write and the comparison arms are never reached — which is what makes
    this rule behaviour-neutral for the default convention.

    The union is computed HERE and NOWHERE ELSE: there is no second map.
    """
    if prior is None:
        return True
    rim_measured = (rim.basis == "measured")
    prior_measured = (prior.basis == "measured")
    if rim_measured != prior_measured:
        return rim_measured
    return float(rim.rim_height) > float(prior.rim_height)


def _element_partition_selection(groups, apertures):
    """The ``per_element`` partition and ITS rims.

    ``resolve_group_rims(apertures, element_partition(groups))``, applied group by
    group so that a group which must NOT be split keeps its shipped single body.
    Returns ``(groups, rims, not_split)``, where ``not_split`` is one
    ``(members, blocking_pairs)`` entry per group that DECLINED to split while
    carrying at least one measured member.

    **``not_split`` IS THE DISCLOSURE FEED, and it is
    the whole of the fix.** A declining group draws the GROUPED geometry — one body
    at the group rim — while the envelope still echoes ``per_element``. The
    geometry is right and is deliberately left alone; what was wrong is that
    nothing said so. Every such group now reaches ``_S_GRP_NOT_SPLIT`` on the
    figure and ``groups_not_split`` in the envelope.

    **A group with NO measured member is NOT reported here.** It is a sanctioned
    no-op and its body is already disclosed by the shipped
    ``_S_GRP_PLACEHOLDER``, which says the height is fabricated; a second string
    would disclose one body twice and say nothing the first does not. The class
    that WAS undisclosed is the PARTLY-measured group — matrix row 8 speaks only of
    the all-placeholder case, while the exclusion below is a condition on each
    PAIR, so a measured-basis group holding one all-placeholder pair declines too.

    A 2-surface group (a singlet, or a doublet's own halves) is the partition
    IDENTITY, not a decline: it is already drawn per-element, so it is not
    reported either.

    **THE ONE EXCLUSION, and it is a designed one.** The behaviour rules make
    an all-placeholder group a no-op under BOTH conventions, and the stated reason
    is a prohibition — "no pairwise rims from placeholder heights". Stated
    precisely, that is a condition on each PAIR, not on the group: a pair with no
    measured member would take its rim from ``resolve_group_rims`` rule 2, the max
    of two FABRICATED heights, and then draw a body at it. So a group is
    partitioned only when EVERY consecutive pair carries at least one measured
    member; otherwise it is left whole. An all-placeholder group satisfies that
    test nowhere and so falls out as the no-op the matrix requires.

    **``draw_heights`` is deliberately NOT re-derived here.**
    ``resolve_group_rims`` returns a height map as well; it
    is DISCARDED. The one map the drawing code reads stays the caller's, computed
    once in ``_prepare_draw_geometry``. Only the rims change.
    """
    out_groups = []
    not_split = []

    def _has_measured(indices):
        return any(0 <= j < len(apertures) and apertures[j].measured
                   for j in indices)

    for group in groups:
        members = [int(g) for g in group]
        pairs = _geom.element_partition([members])
        blocking = [pair for pair in pairs if not _has_measured(pair)]
        splits = len(pairs) > 1 and not blocking
        if splits:
            out_groups.extend(pairs)
        else:
            out_groups.append(members)
            # The decline is reported ONLY when this group has a measured member
            # and had more than one element to lose: an all-placeholder group is
            # matrix row 8's no-op (disclosed by `_S_GRP_PLACEHOLDER`) and a
            # 2-surface group is the partition identity.
            if len(pairs) > 1 and _has_measured(members):
                not_split.append((tuple(members),
                                  tuple(tuple(int(j) for j in p)
                                        for p in blocking)))
    new_rims, _discarded_heights = _geom.resolve_group_rims(apertures, out_groups)
    return out_groups, new_rims, tuple(not_split)


def _draw_group_bodies(ax, np, rows, groups, rims, apertures, draw_heights, to_plot,
                       *, outline=_ELEMENT_OUTLINE_DEFAULT):
    """Draw ONE transparent fill + ONE closed stroke per BODY that has one.

    Under ``grouped`` a body is a cemented GROUP; under ``per_element`` it is a
    single ELEMENT, and a group of ``m`` surfaces yields ``m-1`` of them.

    Both consume ``GroupSection.polygon`` — there is no second copy of the
    geometry and no path-local closure, so the stroke can only ever render the
    fill's own boundary. The transparent fill keeps exactly one inspectable body
    artist per group.

    A ``placeholder``-basis body (no member's aperture was ever read) is the ONLY
    closed body that does not stroke wide continuous black: it renders narrow,
    dashed and grey, so a reader can tell at a glance that its height is not a
    measurement.
    """
    not_split = ()
    if outline == "per_element":
        groups, rims, not_split = _element_partition_selection(groups, apertures)
    state = {
        "grouped": set(), "drawn_caps": set(), "placeholder_members": set(),
        "open_groups": [], "placeholder_groups": [], "rim_truncated": [],
        "rim_by_surface": {}, "points": [], "sections": [], "cap_surfaces": set(),
        "extensions": {},
        # The groups that drew GROUPED ink under a `per_element` request.
        # Empty under `grouped`, where the partition is
        # never reached and there is nothing to decline — so the default
        # convention's published envelope is unchanged by this channel.
        "grouped_fallback": list(not_split),
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
            k = int(s)
            # NOT a plain assignment. A shared surface is written once per
            # element that contains it, and the union is what the leader arm and
            # the stamp must read. See `_rim_supersedes`.
            if _rim_supersedes(rim, state["rim_by_surface"].get(k)):
                state["rim_by_surface"][k] = rim
        if outline == "per_element":
            # Feed (ii) of the extension accumulator. It sits
            # AFTER the `section is None` continue above and in the same block as
            # the rim writes, so admission (beta) — INK ONLY — is STRUCTURAL
            # rather than a second test: a suppressed body cannot reach here.
            #
            # `grouped` is deliberately excluded, and the exclusion is a scope
            # statement rather than an oversight. There the shared join is drawn
            # by `_emit_profile`, which already records it; the only caps this
            # would add are a group's OUTER caps, which are the axis-(g) class
            # the owner ruled OUT of NARROW disclosure. Feeding them would change
            # the DEFAULT convention's published envelope, which this change
            # must not do.
            for j, own_h, rim_h in build.cap_extensions:
                _record_extension(state["extensions"], j, own_h, rim_h)
        state["points"].extend(zip(zs, ys))
        state["sections"].append(build.section)
    state["rim_truncated"] = sorted(set(state["rim_truncated"]))
    return state


def _is_cemented_join(rows, body, i):
    """Is surface ``i`` a CEMENTED JOIN? A prescription fact about the surface.

    Split out of the shipped ``interior`` predicate, which conflated
    two different questions: *what kind of surface is this* and *who draws it*.
    Cemented-ness is a property of the prescription — the convention changes the
    OUTLINE, never what the surface IS — so the figure's only cue for a cemented
    join (its leader DOT and flat ``"-"`` arrowstyle) is keyed on THIS, alone.

    Deliberately does NOT ask whether a body already drew the surface; that is
    ``_drawn_by_a_body``'s question and merging the two is what would drop the dot
    the moment a convention drew the join as a cap.
    """
    try:
        k = int(i)
    except (TypeError, ValueError):
        return False
    return (k in body["grouped"] and k != 0
            and _geom.is_cemented_interface(rows, k))


def _drawn_by_a_body(body, i):
    """Is surface ``i`` already carried by a group's ONE closed stroke?

    The other half of the shipped ``interior`` split: membership of
    ``drawn_caps ∪ placeholder_members``. This decides EMISSION (drawing it again
    would double-stroke it); it decides nothing about leader STYLE.
    """
    try:
        k = int(i)
    except (TypeError, ValueError):
        return False
    return k in body["drawn_caps"] or k in body["placeholder_members"]


def _interface_rim_height(body, outline, i):
    """The rim height surface ``i``'s profile extends to, or ``None``.

    ONE height source, read through the map that already exists
    (``rim_by_surface``) — the same expression the leader's ``drawn_edge`` reads,
    never a second parallel map. ``None`` means "do not extend", and it is the
    answer for every case that must not extend on THIS path: a non-``grouped``
    convention, a surface no body covers, and a PLACEHOLDER-basis group.

    **Why ``per_element`` answers ``None`` — the REASON changed at, the
    behaviour did not.** It is no longer "that convention is inert". Under
    ``per_element`` a cemented join IS a body cap, so it is ``_drawn_by_a_body``
    and the emission loop never reaches this function for it at all; the join is
    extended inside ``build_group_section`` and reported on
    ``GroupSectionBuild.cap_extensions``. What the ``None`` still buys is the case
    where that body was SUPPRESSED — there the per-surface fallback does draw the
    join, and it must draw it at its own semi rather than at a rim no body
    committed.

    The placeholder exclusion is admission condition (α) and it is load-bearing:
    an all-placeholder group still reaches the cap loop, so without it a
    FABRICATED height would be published as a measured ``own_semi`` underneath a
    string asserting a measured aperture. A placeholder group's honest disclosure
    is the shipped ``_S_GRP_PLACEHOLDER``.

    Condition (β), INK ONLY, is satisfied structurally rather than by a second
    test: ``rim_by_surface`` is written only AFTER ``_draw_group_bodies`` clears
    its ``section is None`` continue, so a suppressed body has no entry here and
    this returns ``None``.
    """
    if outline != "grouped":
        return None
    rim = body["rim_by_surface"].get(i)
    if rim is None or rim.basis != "measured":
        return None
    return float(rim.rim_height)


def _record_extension(extensions, surface, own_semi, drawn_to):
    """Accumulate ONE extension event, keyed by SURFACE.

    Never appends, never last-write-wins, never first-write-wins: on a second
    event for the same surface the record with the GREATER ``drawn_to`` is kept.
    ``own_semi`` is the surface's one measured semi and so cannot legitimately
    differ between events; a mismatch is a defect and raises rather than
    silently publishing one of two disagreeing numbers. The draw path's own
    ``except BaseException`` converts that into ``ok:false`` / ``render_failed``,
    so no figure and no ``interfaces_extended`` are published.

    Under ``grouped`` each draw path visits a surface at most once, so the
    ``prior is not None`` arm below — the coalescing rule AND its raise — runs
    only under ``per_element``, where a shared cemented interface is a cap of two
    elements and so produces two events for one surface.
    """
    k = int(surface)
    prior = extensions.get(k)
    if prior is None:
        extensions[k] = (float(own_semi), float(drawn_to))
        return
    if float(prior[0]) != float(own_semi):
        raise AssertionError(
            f"surface {k} reported two different own_semi values "
            f"({prior[0]} then {own_semi}) — a surface has ONE measured semi, "
            "so this is a defect, not a coalescing case"
        )
    if float(drawn_to) > float(prior[1]):
        extensions[k] = (float(own_semi), float(drawn_to))


def _extension_records(extensions):
    """The surface-sorted ``interfaces_extended`` entries. ONE per surface.

    ``synthetic_mm`` is computed from the KEPT pair at full precision — never
    carried alongside it, which is how the three numbers could drift apart.
    """
    out = []
    for k in sorted(extensions):
        own_semi, drawn_to = extensions[k]
        out.append({
            "surface": int(k),
            "own_semi": float(own_semi),
            "drawn_to": float(drawn_to),
            "synthetic_mm": float(drawn_to) - float(own_semi),
        })
    return out


def _narrow_extension_records(extensions, rows, body):
    """``interfaces_extended`` under the NARROW disclosure policy.

    The owner ruled NARROW first-hand: the published band covers a
    surface that is a **cemented JOIN** drawn past its own measured semi, and
    nothing else. Under ``per_element`` the accumulator legitimately also holds a
    group's OUTER caps — element ``(a,b)``'s shorter cap is drawn flat out to that
    element's rim by exactly the same mechanism, on exactly the same ink — so the
    filter is what keeps them out of the envelope.

    **Filtered HERE, at envelope assembly, never at accumulation.** The reason is not
    tidiness: a cap event and a join event can land
    on the SAME surface, and the coalescing rule has to see BOTH to keep the
    greater ``drawn_to``. Filtering early would drop an event that should have won.

    A no-op under ``grouped``, where only cemented joins are ever recorded.

    ``_S_AP_EXT`` is the constant RESERVED for the outer-cap class should the
    switch ever be turned on; owns it and nothing here publishes it.
    """
    return [e for e in _extension_records(extensions)
            if _is_cemented_join(rows, body, e["surface"])]


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


def _extension_disclosure_string(interfaces_extended, element_outline):
    """THE ``_S_AP_INT_EXT`` line, or ``None`` when nothing was extended.

    ONE producer with two readers — ``_figure_disclosure_strings`` (which places
    it) and ``_finish_disclosures`` (which protects it). A second hand-built copy
    in the protector is exactly how the protected set and the placed string would
    drift apart, at which point the protection would silently guard a string that
    is not on the figure.
    """
    if not interfaces_extended:
        return None
    ks = sorted(int(e["surface"]) for e in interfaces_extended)
    return _S_AP_INT_EXT.format(
        ks=", S".join(str(k) for k in ks),
        s="" if len(ks) == 1 else "S",
        token=element_outline,
    )


def _not_split_records(grouped_fallback):
    """The ``groups_not_split`` envelope entries — ONE per DECLINING group.

    The never-truncated half of the disclosure: ``_S_GRP_NOT_SPLIT`` is a
    figure box and the canvas can run out of room, while this list is always
    COMPLETE — the same division of labour ``interfaces_extended`` has with its
    own string.

    ``unmeasured_pairs`` is the REASON, measured rather than asserted: it names
    the consecutive pairs that carry no measured aperture, which is exactly why
    the group could not be partitioned without taking a rim from fabricated
    heights. A reader can check it against ``aperture_not_measured``.
    """
    out = []
    for members, blocking in grouped_fallback or ():
        out.append({
            "surfaces": [int(s) for s in members],
            "unmeasured_pairs": [[int(j) for j in pair] for pair in blocking],
        })
    return out


def _not_split_disclosure_strings(grouped_fallback, element_outline):
    """``(first_surface, line)`` per declining group — ONE ``_S_GRP_NOT_SPLIT``.

    The first surface is returned BESIDE the text rather than parsed back out of
    it: the disclosure stack orders by it, and recovering it from the formatted
    sentence would make the placement depend on the wording.
    """
    lines = []
    for members, _blocking in grouped_fallback or ():
        # `_blocking` is deliberately NOT formatted into the sentence — see the
        # constant's own note on containment. It reaches the caller through
        # `groups_not_split`, which no canvas can truncate.
        lines.append((int(members[0]), _S_GRP_NOT_SPLIT.format(
            a=int(members[0]), b=int(members[-1]), token=element_outline,
        )))
    return lines


def _figure_disclosure_strings(
    *, rows, folded, projection, config_identity, open_groups,
    placeholder_groups, aperture_not_measured, interfaces_omitted,
    profile_not_measured, interfaces_extended=(), grouped_fallback=(),
    element_outline=_ELEMENT_OUTLINE_DEFAULT,
):
    """Assemble the figure strings in the fixed precedence order.

    ``_S_AP_INT_EXT`` is returned FIRST. The
    shipped placer seats the first box UNCONDITIONALLY — ``if placed and bottom <
    floor`` cannot fire on an empty ``placed`` — so "never skipped" costs nothing
    here. Ordering ALONE is not sufficient, though: it only moves WHICH string is
    vulnerable to the overflow pop, which is why ``_place_disclosures`` also takes
    a ``protected`` set.
    """
    lines = []
    extension_line = _extension_disclosure_string(
        interfaces_extended, element_outline)
    if extension_line is not None:
        lines.append(extension_line)
    lines.extend(_projection_disclosures(projection, rows))
    if folded:
        lines.append(_S_FOLD)
        lines.append(_S_FOLD_CLR)
    if config_identity is not None:
        if config_identity.count is None or config_identity.active is None:
            lines.append(_S_CFG_UNK)
        elif config_identity.count > 1:
            lines.append(_S_CFG.format(k=config_identity.active,
                                       N=config_identity.count))
    # --- the GROUP-level strings ------------------------------------------------ #
    # Three families now share this slot, so they are emitted in ASCENDING
    # FIRST-SURFACE order rather than family by family. The ordering rule has
    # always SAID "ascending first-surface order"; production satisfied it only by
    # accident of statement order, and adding a third family is exactly what would
    # have turned that accident into a contract violation the `_precedence_rank`
    # oracle can see. Sorting is stable, so two strings about the same first
    # surface keep their family order.
    slot4 = list(_not_split_disclosure_strings(grouped_fallback, element_outline))
    placeholder_members = set()
    for group in placeholder_groups:
        placeholder_members.update(group)
        slot4.append((int(group[0]),
                      _S_GRP_PLACEHOLDER.format(a=group[0], b=group[-1])))
    open_caps = set()
    for caps in open_groups:
        for k in caps:
            kk = int(k)
            # Under `per_element` an unmeasured
            # SHARED interface opens BOTH adjacent elements, so the same surface
            # arrives here twice. One surface, one unread aperture, one sentence
            # — printing it twice would read as two separate problems.
            if kk in open_caps:
                continue
            open_caps.add(kk)
            slot4.append((kk, _S_GRP_OPEN.format(k=k)))
    lines.extend(text for _first, text in sorted(slot4, key=lambda kv: kv[0]))
    for k in aperture_not_measured:
        if k in placeholder_members or k in open_caps:
            continue          # already disclosed by its group's own string
        if k in interfaces_omitted:
            lines.append(_S_AP_INT.format(k=k))
        else:
            lines.append(_S_AP.format(k=k))
    # dogfood F-7: a Paraxial surface has no sag at all (an ideal thin element), so its
    # non-finite conic is not an UNREADABLE input -- it says what it is instead.
    lines.extend((_S_SAG_PARAXIAL if 0 <= k < len(rows)
                  and _type_name(rows, k).startswith("Paraxial") else _S_SAG).format(k=k)
                 for k in profile_not_measured)
    return lines


def _place_disclosures(fig, ax, strings, protected=()):
    """Stack the disclosure boxes top-left, non-overlapping and contained.

    Each box is placed below the previous box's MEASURED bounding box plus a fixed
    gap — never at a hardcoded shared coordinate. The policy is TOTAL: boxes are
    placed in precedence order until the next one would leave the canvas, at which
    point the last drawn slot becomes the overflow line and every remaining box is
    reported in the result instead. Never a refusal, never a silent drop.

    ``protected`` names strings the overflow pop
    may not evict. The shipped pop takes the LAST PLACED box, so a mandatory
    string placed first is still evicted the moment the box AFTER it overflows —
    being first makes it the last placed at exactly that boundary. The pop
    therefore selects the last placed box NOT in this set.

    **Degenerate case, stated rather than left to be discovered:** if the overflow
    occurs while only protected boxes are placed, NOTHING is popped, the overflow
    line is not drawn (there is no slot for it), and ``n_truncated`` still counts
    every hidden string — so the envelope stays complete even though the canvas
    cannot say so.

    Returns ``(drawn_strings, n_truncated)``.
    """
    if not strings:
        return [], 0
    protected_set = set(protected)
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
    # trusted: `test_i3_disclosure_boxes_never_overlap_and_stay_on_canvas`
    # asserts the no-intersection contract POST-DRAW on the real patches. That
    # regression lives in the development suite and is
    # not shipped with this package.
    #
    # WHAT THAT DOES NOT COVER, stated because the earlier wording claimed it did:
    # the post-draw check is NON-OVERLAP AND ON-CANVAS ONLY. It says nothing about
    # the SIZE of the gap, so this arithmetic can drift by a pixel or two in either
    # direction and stay green. It catches a REGRESSION TO OVERLAP, not drift.
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
    # The constant divided by the FIGURE height in pixels, then used below as an
    # AXES fraction. Two conversions are missing: ``dpi / 72`` (pt -> px) and the
    # axes-to-figure height ratio. The gap this delivers is therefore
    # ``4 * axes_height_px / figure_height_px`` device pixels, not the 4 points the
    # constant's name promises (see its definition). Left as-is on purpose: this
    # arithmetic IS the figure's current geometry and changing it would move every
    # box in every figure.
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
    victim = None
    for pos in range(len(placed) - 1, -1, -1):
        if placed[pos][1] not in protected_set:
            victim = pos
            break
    if victim is None:
        # Only protected boxes were placed. Nothing may be evicted, so no slot
        # exists for the overflow line — but every withheld string is still
        # counted, and the result's per-surface lists remain COMPLETE.
        return [t for (_a, t, _y) in placed], len(strings) - len(placed)
    hidden = len(strings) - (len(placed) - 1)
    artist, _text, slot = placed.pop(victim)
    artist.remove()
    line = _S_OVERFLOW.format(n=hidden)
    placed.insert(victim, (_box(line, slot), line, slot))
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


_FAR_CAPTION_GID = "disclosure:far_caption"


def _draw_far_captions(fig, strings):
    """Stack the far-object lines as FIGURE-level captions directly under the footer.

    Each line is an Annotation anchored to the bottom-right of the line above it (the
    footer first), so the stack follows wherever the footer was seated and can never
    enter the axes, where the stamps are, or the legend column. Returns the strings
    placed; never raises (a line that cannot be anchored is placed at the footer's
    fallback corner instead, never dropped).
    """
    placed = []
    if not strings:
        return placed
    from matplotlib.text import Annotation
    anchor = next((t for t in fig.texts if t.get_gid() == _SCOPE_GID), None)
    for text in strings:
        try:
            if anchor is None:
                raise LookupError("no footer to anchor to")
            art = Annotation(text, xy=(1.0, 0.0), xycoords=anchor,
                             xytext=(0.0, -2.0), textcoords="offset points",
                             ha="right", va="top", fontsize=_DISCLOSURE_FONT_PT,
                             color="black", annotation_clip=False)
            fig.add_artist(art)
        except Exception:  # noqa: BLE001 -- the deterministic corner stands
            art = fig.text(_FOOTER_FALLBACK_XY[0], _FOOTER_FALLBACK_XY[1], text,
                           ha="right", va="bottom", fontsize=_DISCLOSURE_FONT_PT,
                           color="black")
        art.set_gid(_FAR_CAPTION_GID)
        anchor = art
        placed.append(text)
    return placed


def _draw(
    plt, np, rows, n, title, stop_index, folded, apertures, rims, draw_heights,
    degraded, ray_data, draw_rays, projection, config_identity,
    *, outline=_ELEMENT_OUTLINE_DEFAULT, exclusion=None, extra_strings=(),
):
    """Draw the UNFOLDED figure and return it. Caller owns closing it in a finally.

    ``exclusion`` is the shared far-object decision
    (``_layout_register.FarDecision``). ``None`` -- the default -- draws exactly as
    before. ``cut_object`` drops the object ``0`` stamp (the text that dragged the
    x-limits back out to z=0) and marks the left edge instead; ``cut_image`` ends the
    frame at the last lens surface, drops the image plane line and its stamp, and marks
    the right edge. ``extra_strings`` (the far-object lines) are forwarded to
    ``_finish_disclosures``, which places them as captions under the scope footer.

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
        outline=outline,
    )

    callouts = []          # (surface_index, color)
    interfaces_omitted = []
    # The EMITTED extensions, keyed by surface. Written by `_emit_profile` from
    # the `extend_profile_to_rim` OUTPUT in LOCAL space — never from the planner
    # (`draw_heights` / `rims` / `_prepare_draw_geometry`), which would make the
    # plan and the figure wrong TOGETHER and agreeing, and never from an artist,
    # which carries plot-frame ordinates.
    #
    # ONE accumulator, and it is SEEDED BY THE BODY PASS. Under `per_element` the
    # shared join is a body CAP, so `_emit_profile` never sees it and every event
    # arrives from `GroupSectionBuild.cap_extensions`; under `grouped` the body
    # pass contributes nothing and `_emit_profile` fills it. Taking the body's
    # own dict rather than starting a second one is what keeps the coalescing
    # rule operating over a SINGLE map — two maps merged later would be the
    # last-write defect again, one layer up.
    extensions = body["extensions"]
    # Surfaces stroked flat from their LAST VALID SAMPLE to their own measured
    # edge, because the sag mask truncated inside the aperture. A separate fact
    # from the band above, on a separate (and already shipped) channel.
    truncations = set()
    for i in optical_indices:
        record = apertures[i] if i < len(apertures) else None
        measured = bool(record is not None and record.measured)
        if _drawn_by_a_body(body, i):
            pass  # the group's ONE closed stroke already carries this surface
        elif _is_cemented_join(rows, body, i):
            if measured:
                _emit_profile(
                    ax, np, rows, i, draw_heights[i], to_plot,
                    width=_NARROW_LINE_PT, color="black", family="interface",
                    rim_height=_interface_rim_height(body, outline, i),
                    extensions=extensions, truncations=truncations,
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

    # An interface whose own sag samples masked out was stroked flat from its
    # last valid sample to its measured edge. That band was never sampled, so it
    # joins the SHIPPED truncation channel — the same fact `build_group_section`
    # already reports for a cap. It is NOT the synthetic band, which stays in
    # `interfaces_extended`.
    if truncations:
        body["rim_truncated"] = sorted(set(body["rim_truncated"]) | truncations)

    # --- optical axis + image plane (L5) ---------------------------------- #
    axis_line = ax.axhline(0.0, color="black", linewidth=_NARROW_LINE_PT,
                           linestyle=_AXIS_DASHES, zorder=0)
    axis_line.set_gid("axis:optical")
    image_z = z_vertex[n - 1]
    cut_object = bool(exclusion is not None and exclusion.cut_object)
    cut_image = bool(exclusion is not None and exclusion.cut_image)
    if cut_image:
        # The frame ends at the last lens surface (vertex + its outward edge sag), so
        # the far image plane neither sets the x-limits nor inflates `z_span` (which
        # otherwise drops every stamp to the bottom lane). No image line, no image
        # stamp: a marker at the right edge names it instead.
        _last = n - 2
        _hi, _lo = _edge_sag(np, rows, _last, float(draw_heights[_last]))
        image_z = z_vertex[_last] + max(0.0, _hi, _lo)
    else:
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
        #
        # Keyed on the PRESCRIPTION alone. The DOT is not a boundary
        # claim: it marks where the MEASURED aperture ends and the synthetic band
        # begins, so it stays at `draw_heights[idx]` even when the stroke now runs
        # past it to the rim — which is precisely when a reader most needs to be
        # told where the measurement stopped.
        interior = _is_cemented_join(rows, body, idx)
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
        if interior:
            # The placement falsifier: the dot must LIE in the drawn point set; on
            # a truncating conic the own-semi/last-valid-sample pair is on no
            # artist at all, so the placement — never the meaning — moves to the
            # nearest committed vertex. A fully-sampled surface snaps to itself
            # at distance 0, which is what keeps every other figure unchanged.
            tip = _snap_to_drawn(tip, _surface_drawn_points(ax, body, idx))
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

    # Object (0) reference tick at the figure margin (schematic). Under an object cut
    # it is NOT drawn: as a data-space text it would drag the x-limits back out to
    # z=0 through `_expand_limits_to_drawn_text` and defeat the exclusion.
    if not cut_object:
        object_stamp = ax.text(z_vertex[0], 0.0, "0", ha="right", va="center",
                               fontsize=7, color="0.4", zorder=5)
        object_stamp.set_gid("s0:stamp")
    _far_markers(ax, exclusion, n)

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
        interfaces_extended=_narrow_extension_records(extensions, rows, body),
        element_outline=outline, extra_strings=extra_strings,
    )
    return fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures


def _register_rays_to_drawing(ray_data, rows, lde):
    """Shift the global-frame ray polylines into the unfolded drawing's z frame.

    On an unfolded (axial, mirror-free) system global z and `vertex_z` differ by one
    constant, measured at the first surface whose global frame reads: the offset is
    `vertex_z[k] - global_z[k]`. No readable frame -> the rays are returned as read
    (the ray reader already truncated every ray at an unreadable frame). Never raises.
    """
    try:
        z_vertex = _geom.vertex_z([r["thickness"] for r in rows])
        offset = None
        for k in range(1, len(rows)):
            frame = _geom._read_one_global_frame(lde, k)
            if frame.get("ok") and math.isfinite(z_vertex[k]):
                offset = z_vertex[k] - float(frame["vertex"][2])
                break
        if offset is None or offset == 0.0 or not math.isfinite(offset):
            return ray_data
        shifted = dict(ray_data)
        shifted["fields"] = [
            dict(field, rays={
                label: [(z + offset, y) for (z, y) in poly]
                for label, poly in (field.get("rays") or {}).items()})
            for field in ray_data.get("fields", [])
        ]
        return shifted
    except Exception:  # noqa: BLE001 -- registration never sinks a render
        return ray_data


def _far_markers(ax, exclusion, n):
    """The frame-edge markers for a far end the shared rule left out.

    ``"← 0 OBJECT {gap:g} mm"`` just outside the LEFT edge and ``"IMAGE {n-1} →
    {gap:g} mm"`` just outside the RIGHT edge, on the optical axis, in the rider-word
    box style, so a reader (and the vision reviewer) still sees the image NUMBER. x is
    in AXES coordinates and y in data coordinates: the marker sits at the frame edge
    whatever the limits are, and -- not being a data-space text -- it never drags the
    limits back out to the excluded end.
    """
    if exclusion is None:
        return
    from matplotlib.transforms import blended_transform_factory
    trans = blended_transform_factory(ax.transAxes, ax.transData)
    box = {"boxstyle": f"square,pad={_RIDER_BOX_PAD}", "facecolor": "white",
           "edgecolor": "none", "linewidth": _NARROW_LINE_PT}
    for entry in exclusion.excluded:
        if entry["role"] == "object":
            text, x, ha = f"← 0 OBJECT {entry['gap_mm']:g} mm", 0.0, "right"
        else:
            text, x, ha = f"IMAGE {n - 1} → {entry['gap_mm']:g} mm", 1.0, "left"
        marker = ax.text(x, 0.0, text, transform=trans, ha=ha, va="center",
                         fontsize=7, color="0.4", zorder=6, clip_on=False, bbox=box)
        marker.set_gid(f"s{entry['surface']}:marker")


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
    n_proxies = 0
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
        if first:
            # No polyline of this field had 2 points, so nothing above
            # labelled it and the picture would read as one field fewer. A
            # legend-only proxy (no data, not counted in n_rays_drawn) names the
            # field and says what the PICTURE shows -- not a coverage claim;
            # ray_coverage stays the coverage authority.
            proxy, = ax.plot([], [], color=color, linewidth=_NARROW_LINE_PT,
                             label=f"field Y={fy:g} (no ray drawn)")
            proxy.set_gid(f"ray{fi}:legend_only")
            n_proxies += 1
    if n_rays_drawn > 0 or n_proxies > 0:
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


def _legend_protected_extents(fig, ax, renderer):
    """The DRAWN extents a re-seated legend may not cover: every axes text (the
    disclosure boxes, surface stamps, STOP/IMAGE riders, markers -- patch, not glyph,
    via ``_drawn_text_extent``), every figure text (the scope footer, far captions, the
    fallback line), the title and the axis labels. An unmeasurable or empty text
    contributes nothing."""
    out = []
    texts = list(ax.texts) + list(fig.texts) + [ax.title, ax.xaxis.label, ax.yaxis.label]
    for t in texts:
        try:
            if not t.get_visible() or not t.get_text():
                continue
            bb = _drawn_text_extent(t, renderer)
        except Exception:  # noqa: BLE001 -- an unmeasurable text contributes nothing
            continue
        if all(math.isfinite(v) for v in (bb.x0, bb.y0, bb.x1, bb.y1)):
            out.append(bb)
    return out


def _seat_legend_clear_of_disclosures(fig, ax):
    """Seat the ray legend where it is INSIDE the figure and covers nothing protected.

    The legend is anchored just outside the axes' right edge, at the top. A disclosure
    box is anchored at the axes' LEFT edge and is as wide as its text, so on a narrow
    (equal-aspect, tall-lens) frame a box runs past the right edge and was drawn OVER
    the legend's first rows (dogfood F-4: the on-axis field drawn but read as absent).

    BOUNDED, DETERMINISTIC SEARCH (an audit finding -- the first cut
    anchored at an unbounded y below the boxes, could leave the canvas, and checked the
    disclosure boxes only). The legend's measured box is translated over candidate
    seats; a seat is valid iff the translated box lies inside ``fig.bbox`` AND overlaps
    no protected extent (``_legend_protected_extents``). Columns, in order: the
    original x, then just right of the rightmost protected extent. In each column the
    seat steps DOWN from the original top in ``_LEGEND_STEP_PT`` steps to the figure
    floor. First valid seat wins. When none is valid the legend is NOT moved off its
    original seat; that outcome is recorded on ``fig._optivibe_legend_seat``.

    Returns the outcome token: ``"original"`` | ``"moved"`` | ``"no_clear_seat"`` |
    ``"no_legend"`` | ``"unmeasured"``. Never raises.
    """
    legend = ax.get_legend()
    if legend is None:
        return "no_legend"
    outcome = "unmeasured"
    try:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        protected = _legend_protected_extents(fig, ax, renderer)
        lb = legend.get_window_extent(renderer)
        fb = fig.bbox
        anchor = legend.get_bbox_to_anchor()
        ax0, ay0 = float(anchor.x0), float(anchor.y1)

        def _clear(dx, dy):
            x0, x1 = lb.x0 + dx, lb.x1 + dx
            y0, y1 = lb.y0 + dy, lb.y1 + dy
            if x0 < fb.x0 or x1 > fb.x1 or y0 < fb.y0 or y1 > fb.y1:
                return False
            return not any(x0 < p.x1 and p.x0 < x1 and y0 < p.y1 and p.y0 < y1
                           for p in protected)

        if _clear(0.0, 0.0):
            outcome = "original"
        else:
            step = _LEGEND_STEP_PT * float(fig.dpi) / 72.0
            gap = _DISCLOSURE_GAP_PT * float(fig.dpi) / 72.0
            right = max([p.x1 for p in protected if p.x1 > lb.x0] or [lb.x0 - gap])
            seat = None
            for dx in (0.0, right + gap - lb.x0):
                dy = 0.0
                while lb.y0 + dy >= fb.y0 and seat is None:
                    if _clear(dx, dy):
                        seat = (dx, dy)
                    dy -= step
                if seat is not None:
                    break
            if seat is None:
                outcome = "no_clear_seat"
            else:
                # Anchored in AXES coordinates, never display pixels: the tight-bbox
                # save re-lays the canvas out and a display anchor would not follow it.
                xa, ya = ax.transAxes.inverted().transform(
                    (ax0 + seat[0], ay0 + seat[1]))
                if not (math.isfinite(xa) and math.isfinite(ya)):
                    raise ValueError("legend seat not representable in axes coords")
                legend.set_bbox_to_anchor((float(xa), float(ya)), transform=ax.transAxes)
                outcome = "moved"
    except Exception:  # noqa: BLE001 -- a legend seat must never sink a draw
        outcome = "unmeasured"
    try:
        fig._optivibe_legend_seat = outcome
    except Exception:  # noqa: BLE001
        pass
    return outcome


def _finish_disclosures(fig, ax, *, rows, folded, projection, config_identity,
                        body, optical_indices, stop_index, n, apertures,
                        interfaces_omitted, interfaces_extended=(),
                        element_outline=_ELEMENT_OUTLINE_DEFAULT, extra_strings=()):
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
        interfaces_extended=interfaces_extended,
        # Read off the body state rather than taken as a parameter: the decline
        # is decided inside `_draw_group_bodies`, and both draw paths already
        # hand this function that state, so there is no second route by which a
        # figure could be drawn with a declining group and reach a different
        # answer here.
        grouped_fallback=body.get("grouped_fallback") or (),
        element_outline=element_outline,
    )
    # The protected set comes from the SAME producer that built the string, so
    # the box being guarded and the box on the figure cannot be different text.
    _extension_line = _extension_disclosure_string(
        interfaces_extended, element_outline)
    protected = () if _extension_line is None else (_extension_line,)
    drawn, truncated = _place_disclosures(fig, ax, strings, protected=protected)
    drawn = list(drawn)
    # The scope footer is EXEMPT from the overflow rule: it is drawn
    # outside the stack, after the truncation decision, because it states
    # what the ABSENCE of a box means. Dropping it is the one truncation
    # that would make the figure less HONEST rather than merely less
    # complete.
    drawn.append(_draw_scope_footer(fig, ax))
    # The far-object lines (`_S_FAR` / `_S_SPECK`) are
    # FIGURE-level captions stacked under the scope footer, not boxes in the top-left
    # axes stack: measured on the D3 self render, a boxed `_S_FAR` covered the stamps
    # of surfaces 1 and 2 and a legend entry. Outside the axes it cannot cover a stamp
    # or the legend -- the footer's own precedent.
    drawn.extend(_draw_far_captions(fig, extra_strings))
    # native-layout-retool dogfood F-4: a disclosure box is as wide as its TEXT, so on a
    # narrow (height-limited) frame it runs past the axes' right edge into the legend
    # column and covered the first legend row -- a field drawn but missing from the
    # legend. Seated LAST, so every box, stamp, footer and caption is already placed.
    _seat_legend_clear_of_disclosures(fig, ax)
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
        # COMPLETE regardless of what fitted on the canvas. The figure string is
        # placed FIRST and PROTECTED from the overflow pop, so it is no longer a
        # string the canvas can take away; the band widths ride this channel
        # either way, so no reader of the envelope depends on what fitted.
        "interfaces_extended": [dict(e) for e in interfaces_extended],
        # COMPLETE for the same reason and by the same rule as the band list: the
        # figure string can be crowded off the canvas, this cannot (external
        # audit).
        "groups_not_split": _not_split_records(body.get("grouped_fallback")),
        "disclosures_truncated": int(truncated),
        "figure_disclosures": list(drawn),
    }
    return drawn


def _draw_folded_global(
    plt, np, rows, n, title, stop_index, apertures, rims, draw_heights,
    degraded, global_frames, ray_data, draw_rays, projection, config_identity,
    *, outline=_ELEMENT_OUTLINE_DEFAULT, extra_strings=(),
):
    """Draw a FOLDED system in the GLOBAL frame.

    ``extra_strings`` is forwarded to
    ``_finish_disclosures``: it is the ONLY route by which a default render that fell
    back from ``native`` on a fold says so on the figure (``_S_FALLBACK``). The default
    ``()`` keeps every direct caller unchanged; the far-object rule is still computed
    and disclosed, not applied, on this path.

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
        outline=outline,
    )
    for gz, gy in body["points"]:
        all_gz.append(gz)
        all_gy.append(gy)

    callouts = []          # (surface_index, color)
    interfaces_omitted = []
    # See the unfolded path's note. On a fold this matters MORE, not less: the
    # extension is built before `to_plot`, so the recorded reach is a LOCAL
    # radial millimetre value, while the artist's own ordinates are rotated.
    # Seeded by the body pass — see the unfolded path's note on why it is ONE map.
    extensions = body["extensions"]
    # See the unfolded path's note.
    truncations = set()
    for i in optical_indices:
        frame = _frame(i)
        if frame is None:
            # Without a global vertex there is no honest place to draw or stamp.
            continue
        record = apertures[i] if i < len(apertures) else None
        measured = bool(record is not None and record.measured)
        pts = []
        if _drawn_by_a_body(body, i):
            pass
        elif _is_cemented_join(rows, body, i):
            if measured:
                pts = _emit_profile(
                    ax, np, rows, i, draw_heights[i], to_plot,
                    width=_NARROW_LINE_PT, color="black", family="interface",
                    rim_height=_interface_rim_height(body, outline, i),
                    extensions=extensions, truncations=truncations,
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

    # See the unfolded path's note: a mask-truncated interface joins the shipped
    # truncation channel, and is NOT the synthetic band.
    if truncations:
        body["rim_truncated"] = sorted(set(body["rim_truncated"]) | truncations)

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
        # Keyed on the PRESCRIPTION alone — see the unfolded site's note. Sites 3/4
        # are NOT twins of 1/2 and this one is edited on its own evidence.
        interior = _is_cemented_join(rows, body, idx)
        arm = drawn_edge + arrow_len
        tip = to_plot(idx, float(draw_heights[idx]) if interior else drawn_edge,
                      sag_hi)
        label = to_plot(idx, arm, sag_hi)
        if tip is None or label is None:
            continue
        if interior:
            # See the unfolded site: the same falsifier, applied here on this
            # path's own evidence. The snap runs in PLOT space, which on a fold
            # is the ROTATED frame the artist itself committed — so it follows
            # the ink rather than an axis-aligned guess about where it went.
            tip = _snap_to_drawn(tip, _surface_drawn_points(ax, body, idx))
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
        interfaces_extended=_narrow_extension_records(extensions, rows, body),
        element_outline=outline, extra_strings=extra_strings,
    )
    return fig, surface_labels, stop_label, n_rays_drawn, figure_disclosures


# =========================================================================== #
# native-layout-retool -- the NATIVE cross-section
#
# OpticStudio draws the lens; OptiVibe registers the raster to the prescription
# (model B), VERIFIES that registration against the vendor's own ink, and only
# then stamps OUR surface numbers over it. There is no refit: a frame that does not
# verify either falls back to the self drawing (default) or ships UNSTAMPED
# (explicit native).
# =========================================================================== #
class _NativeOutcome:
    """What one native attempt produced. Exactly one of three shapes:

    * ``token`` set -- the attempt could not produce a usable native picture; the caller
      falls back (default) or refuses (explicit, via ``_NATIVE_EXPLICIT_FAMILY``);
    * ``refusal`` set -- a configuration restore that did not verify: the WHOLE
      render is refused, default or explicit;
    * neither -- ``rgb`` / ``reg`` / ``plan`` are the registered raster, and
      ``stamps_withheld`` names the reason when an explicit native ships unstamped.
    """

    def __init__(self):
        self.token = None
        self.detail = None
        self.refusal = None
        self.rgb = None
        self.reg = None
        self.extent = None
        self.start = None
        self.end = None
        self.plan = []
        self.flags = []
        self.tmp_path = None
        self.registration = None
        self.stamps_withheld = None
        #: the far decision whose Start/End this export CONSUMED (None = the exporter's
        #: own default range). The envelope's `far_object_excluded` must describe the
        #: SAME object (R-5); a disagreement is flagged, never silent.
        self.range_decision = None
        #: The SAMPLED-ray coverage of a successful export (None until
        #: the PNG gate passed), and its per-field flags (used only when native ships).
        self.ray_coverage = None
        self.coverage_flags = []
        #:: the `view_3d` envelope dict of an accepted 3-D export (None otherwise).
        self.view_3d = None

    def fail(self, token, detail):
        self.token = token
        self.detail = detail
        return self


def _type_name(rows, i):
    """The row's ``type_name`` when it is a real ``str``, else ``""`` -- never a
    ``str()`` of a caller-controlled value (the base-slot normalization rule)."""
    value = rows[i].get("type_name", "")
    return value if type(value) is str else ""


def _native_refusal_message(outcome, renderer="native"):
    """The explicit-native refusal text for ``outcome.token`` (never raises)."""
    token = outcome.token
    detail = outcome.detail or ""
    if token == "non_axial":
        return ("renderer 'native' cannot draw this system: it is non-axial (a "
                "coordinate break, even an all-zero one, a Tilted surface or a "
                "non-axial GRIN); use renderer='self' or a 3-D view ('native_3d' / "
                f"'native_shaded'). (non_axial: {detail})")
    return f"renderer {renderer!r} could not draw this figure ({token}): {detail}"


def _native_to_plot(rows, frames, reg):
    """``to_plot(surface, y, sag)`` in the overlay's PIXEL data space: local mm ->
    the SAME frame ``surface_points`` used -> model B. Registration applied once."""
    z_vertex = _geom.vertex_z([r["thickness"] for r in rows])

    def to_plot(surface, y, sag):
        if frames is None:
            z, yy = z_vertex[surface] + sag, y
        else:
            frame = frames[surface] if surface < len(frames) else None
            if not (isinstance(frame, dict) and frame.get("ok")):
                return None
            _gx, yy, z = _geom.sag_to_global(frame["R"], frame["vertex"], y, sag)
        return reg.to_px(z, yy)

    return to_plot


def _sag_samples(np, rows, i, ys):
    """The surface's sag at every height in ``ys`` -> ``(sags, valid)``: the sampler
    ``_layout_register.profile_polyline`` consumes (``_edge_sag``'s own sag model)."""
    z, valid = _geom.sag_profile(
        rows[i]["radius"], rows[i]["conic"], np.asarray(ys, dtype=float),
        coeffs=rows[i].get("aspheric_coefficients"),
        norm_radius=rows[i].get("asphere_norm_radius"),
        power=rows[i].get("asphere_power"),
    )
    return [float(v) for v in z], [bool(v) for v in valid]


def _plan_native_stamps(np, *, rows, n, points, reg, extent, apertures, rims,
                        draw_heights, stop_index, start, end):
    """The stamp loop's SIDE SELECTION, run before anything is drawn.

    ``_draw``'s callout set restricted to the drawn range, the same z-order and the
    same top/bottom rule, with the collide threshold taken as ``_STAMP_COLLIDE_FRAC``
    of the drawn span IN PIXELS. Each entry names the terminus its leader will touch:
    the surface's OWN-semi curve end on the chosen side -- never our group rim, which
    comes from OUR body pass and may touch no vendor ink.
    """
    optical = _optical_indices(rows, n)
    callouts = [i for i in optical if start <= i <= end]
    if end == n - 1:
        callouts.append(n - 1)
    grouped = {int(s) for g in _glass_groups(rows, n, optical) for s in g}
    body = {"grouped": grouped}
    rim_of = {}
    for rim in rims:
        if rim.basis == "measured":
            for s in rim.surfaces:
                rim_of[int(s)] = float(rim.rim_height)
    x_lo = reg.to_px(extent.zmin, 0.0)[0]
    x_hi = reg.to_px(extent.zmax, 0.0)[0]
    collide_px = _STAMP_COLLIDE_FRAC * max(1.0, x_hi - x_lo)
    vertex_px = {i: reg.to_px(points[i].z, points[i].y) for i in callouts}

    def _order_key(i):
        is_stop = stop_index is not None and i == stop_index
        return (vertex_px[i][0], 1 if is_stop else 0, i)

    plan = []
    placed_top = []
    for idx in sorted(callouts, key=_order_key):
        p = points[idx]
        record = apertures[idx] if idx < len(apertures) else None
        measured = bool(record is not None and record.measured)
        h = float(draw_heights[idx])
        if stop_index is not None and idx == stop_index:
            color = "red"
        elif idx == n - 1:
            color = "0.4"
        else:
            color = "black"
        vx = vertex_px[idx][0]
        collides = any(abs(vx - q) < collide_px for q in placed_top)
        side = -1 if collides else 1
        if not collides:
            placed_top.append(vx)
        tz, ty = (p.z_lo, p.y_lo) if side < 0 else (p.z_hi, p.y_hi)
        sag_hi, sag_lo, ok, _h_used = _edge_sag_checked(np, rows, idx, h)
        subpixel = bool(h * reg.s < _lr.MIN_VISIBLE_PX)
        plan.append({
            "surface": idx, "color": color, "side": side,
            "terminus": reg.to_px(tz, ty), "vertex": vertex_px[idx],
            "sag": ((sag_lo if side < 0 else sag_hi) if ok else 0.0),
            "edge_mm": rim_of.get(idx, h),
            "interior": _is_cemented_join(rows, body, idx),
            "subpixel": subpixel, "measured": measured,
            "leader_bearing": bool(measured and not subpixel),
            "ambiguous": False, "separation_px": None,
            "profile_applicable": _profile_applicable(rows, n, idx, grouped),
            "profile_demoted": False,
        })
    return plan


def _profile_applicable(rows, n, i, grouped):
    """True iff the vendor draws surface ``i`` as a PROFILE the interior samples can
    land on (R-1): a glass-group member (glass on either side), a mirror, a paraxial
    line or the image line. A FLAT AIR surface outside every glass group -- the air
    stop, drawn as two ticks at +/-semi with nothing between them (measured: DGauss s6,
    no dark ink at any interior height) -- is NOT: it keeps the terminus check alone
    and is listed in ``registration.profile_not_applicable``. This is
    ``_is_suppressed_scaffold``'s flat-powerless-air arm WITHOUT its stop exemption."""
    if i == n - 1 or i in grouped:
        return True
    r = rows[i]
    if _geom._is_mirror(r.get("material", "")) or _type_name(rows, i).startswith(
            "Paraxial"):
        return True
    return not (_geom._is_air_material(r.get("material", ""))
                and not math.isfinite(r.get("radius", float("nan"))))


def _register_and_verify(np, dark, reg, extent, points, plan, *, rows, frames,
                         draw_heights, start, end):
    """Steps 5-6: the dark-bbox z sides, then the ink at the CHOSEN termini.

    Mutates ``plan`` (``ambiguous`` / ``separation_px``) and returns ``(verified,
    info)``. What a pass DECIDES, exactly: every leader-bearing surface that keeps its
    leader has dark ink within ``REG_TOL_PX`` of its predicted terminus, and no OTHER
    stamped surface's PREDICTED curve passes within ``MIN_TERMINUS_SEPARATION_PX`` of
    it. It does NOT decide that the ink belongs to that surface rather than to
    unstamped geometry near the terminus: ownership rests on the prescription model.
    """
    info = {"bbox_sides_graded": [], "bbox_residual_px": None,
            "tip_points_checked": 0, "tip_residual_px": None,
            "ambiguous_surfaces": [], "subpixel_surfaces": [],
            "profile_points_checked": 0, "profile_residual_px": None,
            "profile_unverified_surfaces": [], "profile_unchecked_surfaces": [],
            "profile_not_applicable": [], "profile_unmeasurable_surfaces": [],
            "reasons": []}
    # --- step 5: the dark bbox z sides (top/bottom are never graded) ------------- #
    bb = _lr.dark_bbox(dark)
    in_range = [p for p in points[start:end + 1]
                if all(math.isfinite(v) for v in (p.z, p.z_hi, p.z_lo))]
    residuals = []
    if bb is None:
        info["reasons"].append("no dark ink in the export")
    else:
        for side, target, got in (("left", extent.zmin, bb[0]),
                                  ("right", extent.zmax, bb[2])):
            tol = 1e-9 * max(1.0, abs(target))
            owners = [p.surface for p in in_range
                      if min(abs(p.z - target), abs(p.z_hi - target),
                             abs(p.z_lo - target)) <= tol]
            if not any(float(draw_heights[k]) * reg.s >= _lr.MIN_VISIBLE_PX
                       for k in owners):
                continue
            info["bbox_sides_graded"].append(side)
            residuals.append(abs(reg.to_px(target, 0.0)[0] - got))
        if not info["bbox_sides_graded"]:
            info["reasons"].append("no bbox side could be graded")
    bbox_ok = bool(info["bbox_sides_graded"]) and all(
        r <= _lr.REG_TOL_PX for r in residuals)
    info["bbox_residual_px"] = round(max(residuals), 3) if residuals else None
    if residuals and not bbox_ok:
        info["reasons"].append(f"bbox residual {max(residuals):.2f} px")
    # --- step 6(a): distinctness against the other stamped surfaces' curves ------ #
    polys = {}
    for e in plan:
        k = e["surface"]
        poly = _lr.profile_polyline(
            rows, k, draw_heights[k], frames=frames, px_per_mm=reg.s,
            sag_fn=lambda _r, _i, _ys: _sag_samples(np, _r, _i, _ys))
        if not poly:
            p = points[k]
            poly = [(p.z_lo, p.y_lo), (p.z, p.y), (p.z_hi, p.y_hi)]
        polys[k] = [reg.to_px(z, y) for z, y in poly]
    info["subpixel_surfaces"] = [e["surface"] for e in plan if e["subpixel"]]
    leader_bearing = [e for e in plan if e["leader_bearing"]]
    for e in leader_bearing:
        d = min((_lr.point_polyline_distance(e["terminus"], polys[o["surface"]])
                 for o in plan if o["surface"] != e["surface"]), default=math.inf)
        e["separation_px"] = d
        if d < _lr.MIN_TERMINUS_SEPARATION_PX:
            e["ambiguous"] = True
            info["ambiguous_surfaces"].append(
                {"surface": e["surface"], "separation_px": round(d, 3)})
    distinct = [e for e in leader_bearing if not e["ambiguous"]]
    # --- step 6(b): tip on ink, at the CHOSEN side's terminus -------------------- #
    res = _lr.ink_residuals(dark, [e["terminus"] for e in distinct])
    info["tip_points_checked"] = len(distinct)
    found = [r for r in res if r is not None]
    info["tip_residual_px"] = round(max(found), 3) if found else None
    misses = [e["surface"] for e, r in zip(distinct, res) if r is None]
    # --- step 6(c), R-1/R-2: the curve INTERIOR on the chosen side --------------- #
    # The terminus sits where two vendor strokes meet (the face's own flat rim
    # extension and the group rim line), so a face displaced ALONG that line keeps ink
    # under it. The interior samples (PROFILE_FRACS of the own semi, chosen side) lie
    # on the face's OWN curve, which no rim stroke shares. Per-SAMPLE distinctness: a
    # sample within MIN_TERMINUS_SEPARATION_PX of another stamped surface's predicted
    # curve is SKIPPED (that ink could be the neighbour's); ANY distinct sample off ink
    # DEMOTES that surface to the unverified `k?` vertex form -- the stamp is the
    # thing that could not be bound, not the frame. The terminus check above stays
    # load-bearing: a terminus miss still fails the whole frame.
    hits = []
    for e in distinct:
        k = e["surface"]
        if not e["profile_applicable"]:
            info["profile_not_applicable"].append(k)
            continue
        h = float(draw_heights[k])
        fracs = [e["side"] * f for f in _lr.PROFILE_FRACS]
        pts = _lr.curve_points(
            rows, k, [f * h for f in fracs], frames=frames,
            sag_fn=lambda _r, _i, _ys: _sag_samples(np, _r, _i, _ys))
        # Round 3: a sample the sampler could not COMPUTE (``None`` from
        # ``curve_points`` -- an invalid/non-finite sag, a frame not ok, a raise) has
        # established NOTHING, unlike the proximity skip below (which HAS established
        # the ink could be a neighbour's). It demotes the surface like an off-ink
        # sample: `k?`, no leader, against the ceiling and the >= 2 rule.
        if len(pts) != len(fracs):
            pts = list(pts) + [None] * (len(fracs) - len(pts))
        failed = [f for f, pm in zip(fracs, pts) if pm is None]
        if failed:
            e["profile_demoted"] = True
            info["profile_unmeasurable_surfaces"].append(
                {"surface": k, "height_fracs": failed})
            continue
        checked = []
        for f, pm in zip(fracs, pts):
            q = reg.to_px(*pm)
            near = min((_lr.point_polyline_distance(q, polys[o["surface"]])
                        for o in plan if o["surface"] != k), default=math.inf)
            if near >= _lr.MIN_TERMINUS_SEPARATION_PX:
                checked.append((f, q))
        if not checked:
            info["profile_unchecked_surfaces"].append(k)
            continue
        pres = _lr.ink_residuals(dark, [q for _f, q in checked])
        info["profile_points_checked"] += len(checked)
        for (f, _q), r in zip(checked, pres):
            if r is not None and r <= _lr.PROFILE_TOL_PX:
                hits.append(r)
            else:
                e["profile_demoted"] = True
                info["profile_unverified_surfaces"].append(
                    {"surface": k, "height_frac": f,
                     "residual_px": None if r is None else round(r, 3)})
    info["profile_residual_px"] = round(max(hits), 3) if hits else None
    if info["profile_unmeasurable_surfaces"]:
        info["reasons"].append(
            "profile samples could not be computed for surfaces "
            f"{[u['surface'] for u in info['profile_unmeasurable_surfaces']]}")
    remaining = [e for e in distinct if not e["profile_demoted"]]
    n_demoted = len(distinct) - len(remaining)
    tips_ok = (len(remaining) >= 2 and not misses
               and all(r <= _lr.REG_TOL_PX for r in found))
    if len(remaining) < 2:
        info["reasons"].append(f"only {len(remaining)} distinct, profile-verified "
                               "termini")
    if misses:
        info["reasons"].append(f"no ink at the terminus of surfaces {misses}")
    elif found and max(found) > _lr.REG_TOL_PX:
        info["reasons"].append(f"tip residual {max(found):.2f} px")
    n_amb = len(leader_bearing) - len(distinct)
    ceiling_ok = n_amb + n_demoted <= _lr.AMBIGUITY_CEILING * len(leader_bearing)
    if not ceiling_ok:
        info["reasons"].append(
            f"{n_amb + n_demoted} of {len(leader_bearing)} leader-bearing surfaces "
            "ambiguous or profile-unverified")
    return bool(bbox_ok and tips_ok and ceiling_ok), info


def _native_attempt(session, *, rows, n, apertures, rims, draw_heights, folded,
                    global_frames, points, decision, draw_rays, config_identity,
                    directory, stop_index, strict, tmp_sink):
    """Export, gate, register and verify one native cross-section. Never raises
    for an engine fault -- every one becomes a token.

    ``folded`` is the role classification (mirror => folded): an axial mirror system
    is NOT non-axial, and its extent comes from the GLOBAL frames. The checks run in
    ``_RENDERER_FALLBACK_TOKENS`` order. A registration failure is a token only for the
    DEFAULT; an explicit native ships the raster unstamped instead.
    """
    import numpy as np
    out = _NativeOutcome()
    # The degrader: a LATCHED session attempts no export (zero opens).
    if _slot_latched(session):
        out.ray_coverage = _rc.unavailable(_rc.REASON_WEDGED)
        return out.fail("native_unavailable", _S_SLOT_LATCHED)
    system = session.system
    frames = global_frames if folded else None
    # non_axial -- decided BEFORE any tool is opened (Q4, 7/7) ------------------ #
    if _native.read_axiality(system) is True:
        return out.fail("non_axial", "system.IsNonAxial reads True")
    if config_identity.count is None:
        return out.fail("configuration_unreadable",
                        "the configuration count could not be read")
    # the drawn range: the shared far rule over the exporter's own default --------- #
    if decision is not None and decision.reason != "extent_unreadable":
        start, end = int(decision.start), int(decision.end)
        out.range_decision = decision
    else:
        start, end = _lr.native_default_range(rows, n)
    grin = [i for i in range(start, end + 1)
            if _type_name(rows, i).startswith("Gradient")]
    if grin:
        if not strict:
            return out.fail("grin_surface",
                            f"GRIN surfaces {grin} in range; the native outline of a "
                            "GRIN surface is unverified")
        out.flags.append(f"native outline unverified on GRIN surfaces {grin}")
    if points is None:
        return out.fail("extent_unreadable",
                        "the drawn extent cannot be predicted from the prescription")
    t0 = float(rows[0].get("thickness", float("nan"))) if n else float("nan")
    # A coordinate break is scaffolding the exporter never draws (and on this path
    # it can only be reached with axiality unreadable, where the export's own refusal
    # message is the backstop), so its unmeasured semi says nothing about the frame.
    unmeasured = [i for i in range(start, end + 1)
                  if i < len(apertures) and not apertures[i].measured
                  and not (i == 0 and not math.isfinite(t0))
                  and not _geom._is_coordinate_break(rows[i].get("type_name", ""))]
    if unmeasured:
        return out.fail("extent_unreadable",
                        f"surfaces {unmeasured} have no measured semi-diameter, so the "
                        "exported frame cannot be predicted")
    extent = _lr.drawn_extent(points, start, end)
    if extent is None:
        return out.fail("extent_unreadable", "no finite drawn extent in range")
    W, H = _lr.choose_canvas(extent)
    try:
        reg = _lr.fit(extent, W, H)
    except ValueError as exc:
        return out.fail("extent_unreadable", str(exc))
    paraxial = [i for i in range(start, end + 1)
                if _type_name(rows, i).startswith("Paraxial")]
    unmodelled = bool(reg.height_limited and paraxial)
    if unmodelled and not strict:
        return out.fail("registration_unmodelled",
                        f"height-limited fit with paraxial surfaces {paraxial} "
                        "(their arrowhead extent is unmodelled)")
    # export --------------------------------------------------------------------- #
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="native_", dir=directory)
    # Registered with the CALLER at once, so its `finally` removes the raster on every
    # exit -- including an exception raised anywhere below.
    tmp_sink.append(tmp)
    os.close(fd)
    out.tmp_path = tmp
    res = _native.export_cross_section(
        system, tmp, W=W, H=H, start=start, end=end,
        n_rays=_native.DEFAULT_N_RAYS if draw_rays else 0,
        config_count=config_identity.count, n_surfaces=n)
    # The OBSERVATION and the latch come FIRST, before any gate
    # and before any coverage open. The export token is returned unchanged whatever
    # the observation says (the verdict is the observation, never the write).
    verdict, slot_flags = _observe_slot(session, res)
    out.flags.extend(slot_flags)
    detail = res.detail
    if res.run_raised:
        # The RECORDED latch decides the text (the gate reads the same arbiter)
        raised = _S_SLOT_RAISED if _slot_latched(session) else _S_SLOT_RAISED_UNLATCHED
        detail = f"{detail}; {raised}"
        out.flags.append(f"native export: {raised}")
    if res.close_failed:
        out.flags.append("the native export tool's Close() raised; the export result "
                         "stands")
    if res.restore_verified and res.configuration_moved_to is not None:
        # A move the restore undid is DISCLOSED, never silent. One flag, not
        # `mutation_warning` -- that key belongs to the `with_configuration` wrapper and
        # a second writer would be a second path onto it.
        out.flags.append(
            "the native export moved the active configuration to "
            f"{res.configuration_moved_to}; restored to {res.configuration_written} "
            "and verified")
    if not res.restore_verified:
        out.refusal = _fail(
            "render_failed",
            "configuration restore did NOT verify after the native export (intended "
            f"{res.configuration_written}, re-read {res.configuration_after}); the "
            "active configuration may be wrong")
        return out
    if not res.ok:
        return out.fail(res.token, detail)
    ihdr = _png_ihdr(tmp)
    if not _is_png(tmp) or ihdr != (W, H):
        return out.fail("native_not_png",
                        f"the export is not a {W}x{H} PNG (IHDR {ihdr})")
    rgb = _lr.load_rgb_u8(tmp)
    if rgb is None or tuple(rgb.shape[:2]) != (H, W):
        return out.fail("native_not_png", "the exported PNG could not be decoded")
    try:
        dark = _lr.classify_pixels(rgb)[1]
    except ValueError as exc:
        return out.fail("native_not_png", str(exc))
    out.rgb, out.reg, out.extent, out.start, out.end = rgb, reg, extent, start, end
    # Coverage AFTER the PNG gate, BEFORE registration, and ONLY if the
    # observation did not latch -- an Arm-2 export ships its PNG but has just wedged
    # the slot, so a coverage open here would run on the wedged slot (#6).
    if verdict == "wedged" or _slot_latched(session):
        out.ray_coverage = _rc.unavailable(_rc.REASON_WEDGED)
    else:
        out.ray_coverage, out.coverage_flags = _rc.native_coverage(
            system, res, draw_rays=draw_rays,
            deadline=perf_counter() + _ray_budget_s())
    registration = {
        "model": "prescription_fit_v1", "width": W, "height": H,
        "px_per_mm": reg.s, "start_surface": start, "end_surface": end,
        "height_limited": bool(reg.height_limited), "bbox_sides_graded": [],
        "bbox_residual_px": None, "tip_points_checked": 0, "tip_residual_px": None,
        "ambiguous_surfaces": [], "subpixel_surfaces": [],
        "profile_points_checked": 0, "profile_residual_px": None,
        "profile_unverified_surfaces": [], "profile_unchecked_surfaces": [],
        "profile_not_applicable": [], "profile_unmeasurable_surfaces": [],
        "status": "withheld",
    }
    out.registration = registration
    if unmodelled:                      # explicit only: unstamped, no check ran
        out.stamps_withheld = "registration_unmodelled"
        out.flags.append("native registration unmodelled (height-limited fit with "
                         f"paraxial surfaces {paraxial}); stamps withheld")
        return out
    plan = _plan_native_stamps(
        np, rows=rows, n=n, points=points, reg=reg, extent=extent,
        apertures=apertures, rims=rims, draw_heights=draw_heights,
        stop_index=stop_index, start=start, end=end)
    verified, info = _register_and_verify(
        np, dark, reg, extent, points, plan, rows=rows, frames=frames,
        draw_heights=draw_heights, start=start, end=end)
    for key in _REGISTRATION_INFO_KEYS:
        registration[key] = info[key]
    out.plan = plan
    if verified:
        registration["status"] = "verified"
        return out
    detail = "; ".join(info["reasons"]) or "registration check failed"
    if not strict:
        return out.fail("registration_unverified", detail)
    out.stamps_withheld = "registration_unverified"
    out.flags.append(f"native registration unverified ({detail}); stamps withheld")
    return out


def _slot_latched(session):
    """INC-2b: is a WEDGED tool-slot observation on record for ``session``? The
    dispatcher's own arbiter (``server._wedge_recorded``): ABSENT -> False, UNREADABLE
    -> True (skip the export -- the fail-closed direction for a degrader)."""
    return bool(_slot_wedged(session))


def _observe_slot(session, res):
    """Hand one export's slot evidence to the session's SOLE latch writer.
    ``-> (verdict, flags)``; never raises an ordinary ``Exception``. A double without
    ``observe_tools_slot`` -> ``"unknown"`` + a flag; a PRESENT method that raises ->
    ``"unknown"`` + a flag. ``res`` (the export's own token) is never changed here."""
    if not getattr(res, "opened", False):
        return "not_opened", []                # no tool, no Close(), nothing observed
    try:
        observe = getattr(session, "observe_tools_slot")
    except AttributeError:
        return "unknown", [_S_SLOT_NOT_RECORDED]
    try:
        verdict = observe(run_raised=res.run_raised, close_returned=res.close_returned,
                          is_running_after_close=res.is_running_after_close)
    except Exception as exc:  # noqa: BLE001 -- a failed observation is not a latch
        return "unknown", [_S_SLOT_OBSERVE_FAILED.format(exc=type(exc).__name__)]
    flags = []
    if verdict == "wedged" and res.run_raised is not True:
        flags.append(_S_SLOT_SIGNATURE)
    elif verdict != "wedged" and res.close_returned is not True:
        flags.append(_S_SLOT_UNCONFIRMED.format(close=res.close_returned,
                                                running=res.is_running_after_close))
    return verdict, flags


# =========================================================================== #
# native-layout-retool -- the native 3-D views (export_3d)
#
# OpticStudio's 3-D viewer / shaded-model exporters, default camera, UNSTAMPED: there
# is no 3-D registration model, so no surface number is drawn on them. One
# configuration each (the strict current read, proven before AND after), rays
# optional, and the far-object rule APPLIED through Start/End -- on a folded system
# too (an owner ruling), with the fold-safe trailing end.
# =========================================================================== #
def _decision_3d(system, rows, n, raw_decision, rule_fault):
    """``(decision | None, flags)``: the far rule a 3-D view applies (step 7).

    An AXIAL system (``IsNonAxial`` reads False) takes the SAME prescription decision the
    cross-section takes (``raw_decision``), so the cut happens exactly when the
    cross-section rule would cut; anything else (non-axial, or axiality unreadable)
    takes ``far_object_decision_folded`` -- the gap-ratio arm alone, basis labelled.
    A cut image ends at ``last_drawn_surface_before_image`` on BOTH, never a coordinate
    break (a known trap). ``None`` = nothing applied, and a flag says why."""
    flags = []
    if _native.read_axiality(system) is False:
        if rule_fault is not None:
            flags.append(f"far-object rule failed ({type(rule_fault).__name__}): "
                         "nothing was excluded")
            return None, flags
        if raw_decision.reason == "extent_unreadable":
            flags.append("far-object rule not evaluated (extent_unreadable): the drawn "
                         "extent could not be predicted from the prescription; nothing "
                         "was excluded")
            return None, flags
        decision = raw_decision
        if decision.cut_image:
            last = _lr.last_drawn_surface_before_image(
                rows, n, scaffold=_is_suppressed_scaffold)
            if last is not None and last != decision.end:
                decision = _dc_replace(decision, end=last)
        return decision, flags
    try:
        decision = _lr.far_object_decision_folded(rows, n,
                                                  scaffold=_is_suppressed_scaffold)
    except Exception as exc:  # noqa: BLE001 -- the figure never depends on the rule
        flags.append(f"far-object rule failed ({type(exc).__name__}): nothing was "
                     "excluded")
        return None, flags
    if decision.reason == "zero_optics_length":
        flags.append("far-object rule not evaluated (zero_optics_length): no positive "
                     "sequential path length between the first drawn optical surface "
                     "and the fold-safe end; nothing was excluded")
        return None, flags
    return decision, flags


def _native_3d_attempt(session, *, renderer, rows, n, decision, draw_rays,
                       config_identity, directory, tmp_sink):
    """Export and gate one native 3-D view (``export_3d``). Never raises for an
    engine fault -- every one becomes a token, which the caller REFUSES (a 3-D token is
    always explicit).

    Slot obligations: a LATCHED session attempts no export (zero
    ``Tools.Open*``); every export that opened a tool hands its slot evidence to
    ``_observe_slot`` FIRST, exactly as the cross-section does."""
    out = _NativeOutcome()
    kind = _RENDERERS_3D[renderer]
    if _slot_latched(session):
        return out.fail("native_unavailable", _S_SLOT_LATCHED)
    system = session.system
    if config_identity.count is None:
        return out.fail("configuration_unreadable",
                        "the configuration count could not be read")
    if decision is not None:
        start, end = int(decision.start), int(decision.end)
    else:
        start, end = _lr.native_default_range(rows, n)
    out.range_decision = decision
    n_rays = _native.DEFAULT_N_RAYS_3D[kind] if draw_rays else 0
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".png", prefix="native3d_", dir=directory)
    tmp_sink.append(tmp)
    os.close(fd)
    out.tmp_path = tmp
    res = _native.export_3d(system, kind, tmp, config_count=config_identity.count,
                            start=start, end=end, n_rays=n_rays, n_surfaces=n)
    verdict, slot_flags = _observe_slot(session, res)
    out.flags.extend(slot_flags)
    detail = res.detail
    if res.run_raised:
        raised = _S_SLOT_RAISED if _slot_latched(session) else _S_SLOT_RAISED_UNLATCHED
        detail = f"{detail}; {raised}"
        out.flags.append(f"native export: {raised}")
    if res.close_failed:
        out.flags.append("the native export tool's Close() raised; the export result "
                         "stands")
    k = res.configuration_drawn
    if res.opened and not res.identity_verified:
        # Step 5: the ONE identity proof failed -- the whole render is REFUSED.
        out.refusal = _fail(
            "render_failed",
            "configuration identity did NOT verify after the 3-D export (configuration "
            f"{k} before, re-read {res.configuration_after} after one restore); the "
            "active configuration may be wrong")
        return out
    if res.configuration_moved_to is not None:
        out.flags.append(
            "the 3-D export moved the active configuration to "
            f"{res.configuration_moved_to}; restored to {k} and verified")
    if not res.ok:
        return out.fail(res.token, detail)
    W, H = _native.EXPORT_3D_SIZE
    ihdr = _png_ihdr(tmp)
    if not _is_png(tmp) or ihdr != (W, H):
        return out.fail("native_not_png",
                        f"the 3-D export is not a {W}x{H} PNG (IHDR {ihdr})")
    rgb = _lr.load_rgb_u8(tmp)
    if rgb is None or tuple(rgb.shape[:2]) != (H, W):
        return out.fail("native_not_png", "the exported 3-D PNG could not be decoded")
    # Step 6: the NAMED WEAKER consistency check -- never the identity proof.
    consistent, reason = None, None
    if kind != "viewer":
        reason = "the shaded exporter colours rays by field"
    elif n_rays == 0:
        reason = "rays off"
    elif config_identity.count > len(_lr.CONFIG_RAY_PALETTE):
        reason = (f"{config_identity.count} configurations; the ray palette is "
                  f"measured for {len(_lr.CONFIG_RAY_PALETTE)} only")
    else:
        try:
            counts = _lr.ray_colour_counts(rgb)
        except ValueError as exc:
            return out.fail("native_not_png", str(exc))
        consistent, reason = _lr.ray_colour_verdict(counts, k)
        if consistent is False:
            return out.fail(
                "native_export_failed",
                f"ray colours inconsistent with configuration {k} (blue "
                f"{counts['blue']} / green {counts['green']} / red {counts['red']} px; "
                f"configuration {k} is {_lr.CONFIG_RAY_PALETTE[k]})")
    if consistent is None:
        out.flags.append(_S_3D_COLOUR_NULL.format(reason=reason))
    out.rgb, out.start, out.end = rgb, start, end
    out.view_3d = {
        "exporter": kind, "configuration_drawn": k,
        "configuration_identity": "current_readback",
        "ray_colour_consistent": consistent, "start_surface": start,
        "end_surface": end, "n_rays": n_rays,
    }
    out.stamps_withheld = "view_3d"
    return out


def _draw_native_3d(plt, np, outcome, *, rows, n, title, stop_index, apertures,
                    extra_strings=()):
    """The 3-D composite: the exported raster 1:1 (``figimage``), the disclosure
    strip on top (``_S_3D`` first) and the title strip below. No axes, no stamps, no
    leaders. Returns ``_draw``'s 5-tuple (no labels, no stop label, 0 rays drawn)."""
    rgb = outcome.rgb
    H, W = int(rgb.shape[0]), int(rgb.shape[1])
    kind = (outcome.view_3d or {}).get("exporter", "viewer")
    optical = _optical_indices(rows, n)
    eligible = _aperture_disclosure_indices(optical, stop_index, n)
    aperture_not_measured = [i for i in eligible
                             if i < len(apertures) and not apertures[i].measured]
    profile_not_measured = [i for i in optical if _sag_inputs_unreadable(rows[i])]
    strings = [_S_3D.format(kind=_S_3D_KIND.get(kind, kind))] + list(extra_strings)
    truncated = 0
    if len(strings) > _GUTTER_MAX_LINES:
        truncated = len(strings) - (_GUTTER_MAX_LINES - 1)
        strings = strings[:_GUTTER_MAX_LINES - 1] + [_S_OVERFLOW.format(n=truncated)]
    strip = _GUTTER_LINE_PX * len(strings) + 6
    C = _GUTTER_CAPTION_PX
    Htot = strip + H + C
    fig = plt.figure(figsize=((W + 1e-6) / _OVERLAY_DPI, (Htot + 1e-6) / _OVERLAY_DPI),
                     dpi=_OVERLAY_DPI)
    fig.patch.set_facecolor("white")
    fig.figimage(rgb, xo=0, yo=C, origin="upper", zorder=-10)
    placed = _native_captions(fig, title=title, strings=strings, width=W,
                              height_total=Htot)
    fig._optivibe_save_kwargs = {"dpi": _OVERLAY_DPI}
    fig._optivibe_layout_meta = {
        "aperture_not_measured": list(aperture_not_measured),
        "profile_not_measured": list(profile_not_measured),
        "rim_truncated": [],
        "interfaces_omitted": [],
        "interfaces_extended": [],
        "groups_not_split": [],
        "disclosures_truncated": int(truncated),
        "figure_disclosures": list(placed),
    }
    return fig, [], None, 0, list(placed)


def _native_captions(fig, *, title, strings, width, height_total):
    """Place the disclosure strip (top) and the title strip (bottom) in FIGURE pixels.

    Returns the strings placed, in order. The strip holds at most
    ``_GUTTER_MAX_LINES`` lines (the caller already folded any excess into the
    ``_S_OVERFLOW`` line). Nothing here enters the raster.
    """
    placed = []
    for i, text in enumerate(strings):
        y_px = height_total - 4 - i * _GUTTER_LINE_PX
        art = fig.text(6.0 / width, y_px / height_total, text, ha="left", va="top",
                       fontsize=_DISCLOSURE_FONT_PT, color="black")
        art.set_gid("disclosure:native_strip")
        placed.append(text)
    cap = fig.text(0.5, (_GUTTER_CAPTION_PX / 2.0) / height_total, title,
                   ha="center", va="center", fontsize=9, color="black")
    cap.set_gid("disclosure:native_title")
    return placed


def _native_markers(ax, reg, exclusion, n):
    """The far-end markers, in the BOTTOM stamp gutter -- OUTSIDE the raster, never on
    vendor ink (a ~110 px marker in the ~80 px 5 % margin covered surface 1's
    vertex on the axis row, and only leaders and white-boxed NUMBERS on the
    vendor's pixels). Left / right frame edge, the rider-word box style, so the image
    NUMBER stays readable when the image plane is left out. ONE placement rule, no
    runtime relocation: the no-overlap guarantee is a test, not a second path."""
    if exclusion is None:
        return
    box = {"boxstyle": f"square,pad={_RIDER_BOX_PAD}", "facecolor": "white",
           "edgecolor": "none", "linewidth": _NARROW_LINE_PT}
    # data y grows DOWN the raster (limits are pixel edges): H - 0.5 is the raster's
    # bottom edge, so this row sits in the gutter just above the caption strip.
    y = reg.H - 0.5 + _GUTTER_STAMP_PX - _MARKER_INSET_PX
    for entry in exclusion.excluded:
        if entry["role"] == "object":
            text, x, ha = f"← 0 OBJECT {entry['gap_mm']:g} mm", 3.0, "left"
        else:
            text, x, ha = (f"IMAGE {n - 1} → {entry['gap_mm']:g} mm",
                           reg.W - 4.0, "right")
        marker = ax.text(x, y, text, ha=ha, va="center", fontsize=7,
                         color="0.4", zorder=6, clip_on=False, bbox=box)
        marker.set_gid(f"s{entry['surface']}:marker")


def _draw_native_overlay(plt, np, outcome, *, rows, n, title, stop_index, apertures,
                         folded, global_frames, projection, config_identity, decision,
                         extra_strings=()):
    """Composite the native raster 1:1 with OUR stamps. Returns ``_draw``'s 5-tuple.

    The raster is placed with ``figimage`` (no resampling path exists) and an axes
    covering EXACTLY the raster's pixels carries the stamps, its data space being the
    raster's pixel space (``Registration.data_limits``): ``to_plot`` returns PIXELS and
    registration is applied exactly once. The figure is saved at its own dpi with no
    tight crop (a crop would move the registration). An unstamped native (explicit,
    registration withheld) draws no leaders and says so in the strip.
    """
    rgb, reg = outcome.rgb, outcome.reg
    H, W = int(rgb.shape[0]), int(rgb.shape[1])
    stamps = outcome.stamps_withheld is None
    optical = _optical_indices(rows, n)
    # The strip's strings: the shared vocabulary. The fold strings describe OUR
    # folded drawing and never apply to vendor ink, hence folded=False.
    eligible = _aperture_disclosure_indices(optical, stop_index, n)
    aperture_not_measured = [i for i in eligible
                             if i < len(apertures) and not apertures[i].measured]
    profile_not_measured = [i for i in optical if _sag_inputs_unreadable(rows[i])]
    strings = _figure_disclosure_strings(
        rows=rows, folded=False, projection=projection,
        config_identity=config_identity, open_groups=(), placeholder_groups=(),
        aperture_not_measured=aperture_not_measured, interfaces_omitted=(),
        profile_not_measured=profile_not_measured)
    if not stamps:
        strings.insert(0, _S_WITHHELD.format(status=outcome.stamps_withheld))
    strings.extend(extra_strings)
    truncated = 0
    if len(strings) > _GUTTER_MAX_LINES:
        truncated = len(strings) - (_GUTTER_MAX_LINES - 1)
        strings = strings[:_GUTTER_MAX_LINES - 1] + [_S_OVERFLOW.format(n=truncated)]
    strip = _GUTTER_LINE_PX * len(strings) + 6
    P, C = _GUTTER_STAMP_PX, _GUTTER_CAPTION_PX
    Htot = strip + P + H + P + C
    # +1e-6 px: matplotlib TRUNCATES figsize * dpi to the canvas size, and e.g.
    # 1624 / 100 * 100 is 1623.9999999999998 -- one pixel row short of the raster.
    fig = plt.figure(figsize=((W + 1e-6) / _OVERLAY_DPI, (Htot + 1e-6) / _OVERLAY_DPI),
                     dpi=_OVERLAY_DPI)
    fig.patch.set_facecolor("white")
    # Below every axes artist: a figure image is drawn after the axes at equal zorder
    # and would otherwise cover the stamps.
    fig.figimage(rgb, xo=0, yo=C + P, origin="upper", zorder=-10)
    ax = fig.add_axes([0.0, (C + P) / Htot, 1.0, H / Htot])
    lims = reg.data_limits()
    ax.set_xlim(lims[0], lims[1])
    ax.set_ylim(lims[2], lims[3])
    ax.set_aspect("auto")
    ax.set_axis_off()
    frames = global_frames if folded else None
    to_plot = _native_to_plot(rows, frames, reg)

    surface_labels = []
    stop_label = None
    placements = []
    if stamps:
        for e in outcome.plan:
            idx = e["surface"]
            color = e["color"]
            if idx != n - 1:
                surface_labels.append(idx)
            if stop_index is not None and idx == stop_index:
                stop_label = idx
            no_leader = bool(e["ambiguous"] or e["subpixel"] or not e["measured"]
                             or e.get("profile_demoted"))
            word = _rider_word(idx, stop_index, n, at_vertex=no_leader)
            if no_leader:
                # The terminus could not be attributed (ambiguous), is below a pixel
                # (subpixel), has no measured edge, or its predicted profile was not
                # found on ink (profile_demoted): the number sits at the PREDICTED
                # VERTEX with no leader, UNVERIFIED -- so it always reads `k?` (R-3: a
                # subpixel stamp is never ink-checked either, and must not look it).
                vx, vy = e["vertex"]
                text = f"{idx}?"
                stamp = ax.text(vx, vy, text, ha="center", va="bottom", fontsize=8,
                                color=color, zorder=6, clip_on=False)
                stamp.set_gid(f"s{idx}:stamp")
                if word is not None:
                    w_art = ax.annotate(
                        word[0], xy=(vx, vy), xytext=(0.0, -4.0),
                        textcoords="offset points", ha="center", va="top",
                        fontsize=7, color=word[1], zorder=6, annotation_clip=False,
                        bbox={"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                              "facecolor": "white", "edgecolor": "none",
                              "linewidth": _NARROW_LINE_PT})
                    w_art.set_gid(f"s{idx}:word")
                continue
            side = e["side"]
            # The fixed 36-px arm expressed in LOCAL mm for `to_plot` (two coordinate
            # systems: the helpers take mm, the axes' data units are pixels).
            arm = float(e["edge_mm"]) + _STAMP_ARM_PX / reg.s
            label = to_plot(idx, side * arm, e["sag"])
            if label is None:
                continue
            tip = e["terminus"]
            anno = ax.annotate(
                str(idx), xy=tip, xytext=label, ha="center",
                va="bottom" if side > 0 else "top", fontsize=8, color=color,
                zorder=6, annotation_clip=False,
                arrowprops={"arrowstyle": "-" if e["interior"] else "->",
                            "lw": _NARROW_LINE_PT, "color": color,
                            "shrinkA": 1.0, "shrinkB": 1.0},
            )
            anno.set_clip_on(False)
            anno.set_gid(f"s{idx}:leader")
            placements.append({"anno": anno, "surface": idx, "side": side,
                               "base": arm, "sag": e["sag"], "to_plot": to_plot,
                               "tier": 0, "rider": None})
            if word is not None:
                anno.set_bbox({"boxstyle": f"square,pad={_RIDER_BOX_PAD}",
                               "facecolor": "white", "edgecolor": "none",
                               "linewidth": _NARROW_LINE_PT})
                rider = _word_rider(ax, to_plot, idx, side, arm + 12.0 / reg.s,
                                    e["sag"], word[0], word[1])
                if rider is not None:
                    rider.set_clip_on(False)
                    placements[-1]["rider"] = rider
            if e["interior"]:
                _leader_dot(ax, idx, tip, color).set_clip_on(False)
        if outcome.start == 0:
            ox, oy = to_plot(0, 0.0, 0.0) or (None, None)
            if ox is not None:
                obj = ax.text(ox, oy, "0", ha="right", va="center", fontsize=7,
                              color="0.4", zorder=5, clip_on=False)
                obj.set_gid("s0:stamp")
        # Tier to a fixed point with NO re-framing: the limits ARE the raster.
        for _pass in range(_STAMP_TIER_PASSES):
            if not _tier_colliding_stamps(fig, ax, placements, unit_px=reg.s):
                break
    _native_markers(ax, reg, decision, n)
    placed = _native_captions(fig, title=title, strings=strings, width=W,
                              height_total=Htot)
    fig._optivibe_save_kwargs = {"dpi": _OVERLAY_DPI}
    fig._optivibe_layout_meta = {
        "aperture_not_measured": list(aperture_not_measured),
        "profile_not_measured": list(profile_not_measured),
        "rim_truncated": [],
        "interfaces_omitted": [],
        "interfaces_extended": [],
        "groups_not_split": [],
        "disclosures_truncated": int(truncated),
        "figure_disclosures": list(placed),
    }
    # dogfood F-6: the stamps are PLACED in plan order (tiering), not surface order;
    # the envelope lists the same set ascending, as the self renderer does.
    return fig, sorted(surface_labels), stop_label, 0, list(placed)


def render_layout(session, params, *, exact_path=None):
    """Render a meridional layout PNG from surface geometry. NEVER raises.

    Returns ``{ok:true, path, size_bytes, surface_labels, stop_label, folded,
    note, cb_suppressed, scaffold_suppressed, n_surfaces}`` on success, or an ``ok:false``
    envelope (``render_unavailable``/``render_gate_failed``/``render_failed``). The figure
    is always closed; the write is atomic (temp -> ``_is_png`` gate -> ``os.replace``).

    ``n_surfaces`` is the authoritative ``lde.NumberOfSurfaces`` READ AT
    RENDER TIME from the state this figure was drawn from — every row, including object,
    image and every suppressed CB / flat-air dummy; the image plane is ``n_surfaces - 1``.
    It is NOT ``len(surface_labels)`` (those are only the surfaces DRAWN) and must never
    be derived from them. It is present on EVERY ``ok:true`` envelope and ABSENT from
    every ``ok:false`` one, so ``"n_surfaces" in env`` means exactly "a successful render
    by a producer at or after this change, value a builtin ``int`` >= 3". A count that
    cannot be read refuses as ``render_unavailable`` ("surface count unreadable: ...")
    before anything is drawn or written.

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
    # element_outline: validated HERE, before ANY engine touch or
    # file write -- the config resolve immediately below reads `session.system`,
    # so a refusal living in `_render_layout_at` would already have touched the
    # engine. No PNG can exist for a refused call.
    _outline = _resolve_element_outline(params)
    if _outline is None:
        return _fail(
            "render_failed",
            "element_outline must be one of ['grouped', 'per_element']; got "
            f"{params.get('element_outline')!r}",
        )
    # Write the EFFECTIVE token back onto a COPY of params, so everything
    # downstream (the drawing decision AND the echo) reads one already-validated
    # value instead of re-deriving it from the raw input. The copy keeps this
    # normalisation out of the caller's dict.
    # renderer (native-layout-retool): resolved HERE for the same reason -- every
    # refusal lands before the config resolve below touches `session.system`. The
    # order is FIXED so a message is deterministic: unknown token -> element_outline
    # with a non-self renderer -> a token this build cannot draw.
    _renderer = _resolve_renderer(params)
    if _renderer is None:
        return _fail(
            "render_failed",
            f"renderer must be one of {list(_RENDERER_VALUES)}; got "
            f"{params.get('renderer')!r}",
        )
    if _renderer[0] != "self" and "element_outline" in params:
        return _fail(
            "render_failed",
            "element_outline applies to renderer='self' only; pass renderer='self' "
            "or omit element_outline",
        )
    if _renderer[0] not in _RENDERERS_BUILT:
        return _fail(
            "render_failed",
            f"renderer {_renderer[0]!r} is not available in this build",
        )
    params = _RenderParams(params)
    params["element_outline"] = _outline
    # The effective token rides the params COPY; the `strict` flag rides the copy as
    # an ATTRIBUTE, not a key -- it is not a served parameter, and the body's signature
    # is pinned: an EXPLICIT renderer that cannot run is REFUSED, an omitted one (the
    # default) may fall back.
    params["renderer"] = _renderer[0]
    params.renderer_strict = bool(_renderer[1])
    # resolve the OPTIONAL single-config selector; stamp the title.
    try:
        cfg_idx = _resolve_render_config(session.system, params.get("config"))
    except ToolParamError as exc:
        return _fail("render_failed", str(exc))
    except Exception as exc:  # noqa: BLE001 — a config read fault -> render_failed
        return _fail("render_failed", f"could not resolve config: {exc!r}")

    if cfg_idx is None:
        result = _render_layout_at(session, params, exact_path=exact_path)
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
            result = _render_layout_at(session, params, exact_path=exact_path)
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


def _render_layout_at(session, params, *, exact_path=None):
    """The pure per-config render body (draws at the ACTIVE config). See ``render_layout``.

    Takes NO config argument. The title suffix reports the READ-BACK identity
    (``[config k of N]`` whenever the read-back count ``N > 1``, whether or not a
    config was requested), because a figure must state what it SHOWS, not what was
    asked for — so the caller's selection is not an input to this body at all.
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
    native_tmps = []
    minted = False
    try:
        # WHERE THE FIGURE GOES IS DECIDED FIRST, BEFORE ANY ENGINE READ. A minted
        # name whose directory cannot be LISTED cannot be proven free, so the call
        # REFUSES and writes nothing — and it refuses before it has cost a geometry
        # read, a batch trace or a matplotlib import. A refusal cannot clobber an
        # existing figure, which is why there is no reservation and no cleanup path.
        attempted, minted, mint_err = _resolve_path(
            session, params.get("path"), exact_path=exact_path)
        if mint_err is not None:
            return _fail("workspace_unlistable", mint_err)

        # Resolve geometry FIRST so an import failure does not depend on the LDE.
        try:
            plt, np = _import_mpl()
        except BaseException as exc:  # noqa: BLE001 — broken mpl -> render_unavailable
            return _fail(
                "render_unavailable",
                f"matplotlib/numpy unavailable: {type(exc).__name__}: {exc}",
            )

        # The authoritative surface count is read HERE, ONCE, from the
        # state about to be drawn, and emitted as `n_surfaces` in the success envelope
        # (the ONE place the success envelope is BUILT — note `render_layout` then
        # RETURNS it through two branches, and the config-wrapped one mutates the
        # dict afterwards, so "built once" is not "cannot be removed downstream";
        # that second exit is covered by its own tests). A count that cannot be read refuses
        # BEFORE any geometry is read or any byte is written, so no PNG can exist
        # without the measurement that qualifies it — the pairing a driver was
        # previously asked to make in prose, and made once in three renders.
        #
        # `except Exception`, NOT BaseException: a deliberate abort must still reach
        # the outer handler and stay `render_failed` (pinned by
        #: the deliberate-abort projection test),
        # which is the same reason the projection read below is narrow.
        try:
            lde = session.system.LDE
            n = int(lde.NumberOfSurfaces)
        except Exception as exc:  # noqa: BLE001 — unreadable count -> refuse, never draw
            return _fail(
                "render_unavailable",
                f"surface count unreadable: {type(exc).__name__}: {exc}",
            )

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
        # An ORDINARY fault in the projection predicate is "not measured", never a
        # failed render: every surface goes into ``unreadable_surfaces`` and the
        # figure discloses that the check could not decide. A deliberate abort is
        # NOT an ordinary fault, so this catches ``Exception`` — a
        # ``KeyboardInterrupt`` or ``SystemExit`` raised inside the reader LEAVES
        # THIS HANDLER instead of being silently recorded as an unmeasurable
        # prescription.
        #
        # IT DOES NOT LEAVE ``render_layout``, and the difference is the whole
        # honest scope of this change: the outermost handler of this function is
        # still ``except BaseException``, so an abort that gets past here is turned
        # into a ``render_failed`` envelope one frame out rather than propagating to
        # the caller. What is bought here is that the abort no longer shows up as a
        # SUCCESSFUL render whose projection reads unmeasurable.
        #
        # ``exc`` is BOUND AND DELIBERATELY NOT CONSUMED, stated so it does not
        # read as an oversight: ``ProjectionState`` carries no reason field and
        # both disclosure strings are constants, so there is nowhere honest to put
        # the diagnosis, and inventing a place would change what the figure says.
        except Exception as exc:  # noqa: F841 — see the note above
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

        # --- the shared far-object rule (native-layout-retool, owner ruling) ------ #
        # Computed from the prescription alone, BEFORE the ray read. APPLIED on the
        # unfolded self path and on the native cross-section; on the folded self path
        # it is computed and DISCLOSED but NOT applied (an owner ruling -- the folded
        # self frame includes every ray point by design), ticketed.
        renderer_req = params.get("renderer", "self")
        strict = bool(getattr(params, "renderer_strict", False))
        renderer_used = "self"
        fallback_token = None
        native = None
        native_flags = []
        # The rule is NEW code run on every render, so a fault in it must never sink
        # the figure: it is caught HERE, nothing is cut, and one flag says so. An
        # abort (BaseException) is not caught and reaches the outer handler.
        points = None
        raw_decision = None
        rule_fault = None
        try:
            points = _lr.surface_points(
                rows, n, draw_heights, frames=global_frames if folded else None,
                sag_fn=lambda _rows, _i, _h: _edge_sag_checked(np, _rows, _i, _h))
            raw_decision = _lr.far_object_decision(rows, n, points)
        except Exception as exc:  # noqa: BLE001 -- the figure never depends on the rule
            points, raw_decision, rule_fault = None, None, exc

        # --- the native cross-section: first choice when requested -------
        if renderer_req == "native":
            native = _native_attempt(
                session, rows=rows, n=n, apertures=apertures, rims=rims,
                draw_heights=draw_heights, folded=folded, global_frames=global_frames,
                points=points, decision=raw_decision, draw_rays=draw_rays,
                config_identity=config_identity,
                directory=os.path.dirname(attempted) or ".", stop_index=stop_index,
                strict=strict, tmp_sink=native_tmps)
            if native.refusal is not None:
                return native.refusal
            if native.token is not None:
                family = _NATIVE_EXPLICIT_FAMILY.get(native.token, "render_failed")
                if strict:
                    return _fail(family or "render_failed",
                                 _native_refusal_message(native))
                fallback_token = native.token
                native_flags.append(
                    f"native fallback ({native.token}): {native.detail}")
            else:
                renderer_used = "native"
            native_flags.extend(native.flags)
        # --- the native 3-D views: ALWAYS explicit, so never a fallback ------
        rule_flags_3d = []
        if renderer_req in _RENDERERS_3D:
            decision_3d, rule_flags_3d = _decision_3d(
                session.system, rows, n, raw_decision, rule_fault)
            native = _native_3d_attempt(
                session, renderer=renderer_req, rows=rows, n=n, decision=decision_3d,
                draw_rays=draw_rays, config_identity=config_identity,
                directory=os.path.dirname(attempted) or ".", tmp_sink=native_tmps)
            if native.refusal is not None:
                return native.refusal
            if native.token is not None:
                family = _NATIVE_EXPLICIT_FAMILY.get(native.token, "render_failed")
                return _fail(family or "render_failed",
                             _native_refusal_message(native, renderer_req))
            renderer_used = renderer_req
            native_flags.extend(native.flags)

        far_flags = []
        far_strings = []
        # R-5: WHETHER the rule applies is decided in a PURE branch, outside any `try`,
        # so a fault in the caption formatting below can drop the caption but never the
        # RECORD of a cut the picture already carries (the export and the self frame
        # consume the same `raw_decision`).
        if renderer_used in _RENDERERS_3D:
            # the decision the 3-D export CONSUMED (its Start/End): one object, one range
            decision = native.range_decision
            far_flags.extend(rule_flags_3d)
        elif rule_fault is not None:
            decision = None
            far_flags.append(
                f"far-object rule failed ({type(rule_fault).__name__}): nothing was "
                "excluded")
        elif raw_decision.reason == "extent_unreadable":
            # checked FIRST, on BOTH paths ("no cut + flag")
            decision = None
            far_flags.append(
                "far-object rule not evaluated (extent_unreadable): the drawn "
                "extent could not be predicted from the prescription; nothing "
                "was excluded")
        elif folded and renderer_used == "self":
            decision = None
            # dogfood F-3 (spec Task B step 2): a folded SELF render never applies the
            # rule, so it says so on EVERY such render, not only when the rule fired --
            # an untriggered rule is still a rule this path did not apply.
            if raw_decision.triggered:
                far_flags.append(
                    f"far-object rule computed (fill {raw_decision.fill:.3f}) but "
                    "not applied: folded system — "
                    "")
            else:
                _fill = raw_decision.fill
                _fill_txt = (f"fill {_fill:.3f}" if isinstance(_fill, (int, float))
                             and math.isfinite(_fill) else "fill unread")
                far_flags.append(
                    f"far-object rule computed ({_fill_txt}, not triggered) but "
                    "not applied: folded system — "
                    "")
        else:
            decision = raw_decision
        if decision is not None:
            try:
                for entry in decision.excluded:
                    far_strings.append(_S_FAR.format(
                        k=entry["surface"], ROLE=entry["role"].upper(),
                        gap=entry["gap_mm"], ratio=entry["gap_over_length"]))
                # the fill is a 2-D frame prediction; a 3-D view has none
                if (decision.reason == "speck_no_cut"
                        and renderer_used not in _RENDERERS_3D):
                    far_strings.append(_S_SPECK.format(fill=decision.fill))
            except Exception as exc:  # noqa: BLE001 -- a caption fault drops the caption only
                far_strings = [_S_FAR_UNCAPTIONED] if decision.excluded else []
                far_flags.append(
                    f"far-object caption unavailable ({type(exc).__name__}); the "
                    "exclusion itself stands (see far_object_excluded)")
        # One decision object, one range: the native export's own Start/End must be
        # what the envelope describes. A disagreement is flagged, never silent.
        if renderer_used == "native" and native is not None:
            used = native.range_decision
            if (used is None) != (decision is None) or (
                    used is not None and decision is not None
                    and (used.start, used.end) != (decision.start, decision.end)):
                far_flags.append(
                    "far-object record and the native export's range disagree "
                    f"(export {getattr(used, 'start', None)}..{getattr(used, 'end', None)}"
                    f", record {getattr(decision, 'start', None)}.."
                    f"{getattr(decision, 'end', None)})")

        ray_data = None
        ray_flags = []
        # Rays draw whenever requested AND not blocked by the unfolded axial-degrade
        # case. Folded systems NO LONGER suppress rays (nit 5a).
        suppress_axial = bool(axial_degraded) and not folded
        effective_draw_rays = (bool(draw_rays) and not suppress_axial
                               and renderer_used == "self")
        # The degrader rule: a latched session does NOT attempt the ray
        # read -- every batch open would return None and each polyline would truncate
        # at k=1 with a wrong "ray was truncated" diagnosis.
        rays_latched = False
        if effective_draw_rays and _slot_latched(session):
            effective_draw_rays = False
            rays_latched = True
            ray_flags = [f"rays {_S_SLOT_LATCHED}"]
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
        elif draw_rays and suppress_axial and renderer_used == "self":
            ray_flags = [
                "rays suppressed: degraded axial geometry (surfaces "
                f"{axial_degraded} have non-finite thickness; vertex registration "
                "unreliable)"
            ]

        # The ONE place the two frames meet (unfolded path only). The rays are in the
        # engine's GLOBAL frame, whose origin is the global coordinate reference
        # surface (surface 1 by default); the unfolded drawing places surfaces by
        # `vertex_z`, whose origin is the OBJECT surface. They coincide only when the
        # object is at infinity AND the reference is surface 1 -- a finite object at
        # 1000 mm drew every ray 1000 mm upstream of the lens.
        if not folded and ray_data and ray_data.get("fields"):
            ray_data = _register_rays_to_drawing(ray_data, rows, lde)

        # The convention token, read ONCE from the params the caller normalised.
        # `render_layout` has already VALIDATED it and written the effective value
        # back, so there is no re-derivation here and no branch that could coerce
        # an unrecognised value to a default.
        outline = params.get("element_outline", _ELEMENT_OUTLINE_DEFAULT)
        # A default render that fell back from native says so ON the figure too.
        fallback_strings = (
            (_S_FALLBACK.format(token=fallback_token),) if fallback_token else ())
        # The SAMPLED-ray coverage of whichever renderer drew. Never a
        # refusal, never a fallback token; one flag per affected field (self: only an
        # empty field -- a cut is already flagged by the reader) and ONE figure line.
        if renderer_used in _RENDERERS_3D:
            # POPPED under the 3-D tokens (a 3-D depiction of a failing
            # ray is unmeasured) -- no key, no flag, no figure line.
            ray_coverage = None
            coverage_flags = []
        elif renderer_used == "native":
            ray_coverage = native.ray_coverage or _rc.unavailable(
                _rc.REASON_TRACE_UNAVAILABLE)
            coverage_flags = list(native.coverage_flags) + _rc.coverage_flags(
                ray_coverage)
        else:
            ray_coverage = _rc.self_coverage(
                ray_data, draw_rays=draw_rays, latched=rays_latched,
                suppressed=bool(draw_rays) and suppress_axial)
            coverage_flags = ([] if ray_coverage.get("reason") in (
                _rc.REASON_WEDGED, _rc.REASON_SUPPRESSED)
                else _rc.coverage_flags(ray_coverage, only_none=True))
        _missing = (None if ray_coverage is None
                    else _rc.missing_summary(ray_coverage))
        missing_strings = (() if _missing is None else (_S_RAYS_MISSING.format(
            fields=", ".join(str(f) for f in _missing[0]), n_failed=_missing[1],
            n_sampled=_missing[2]),))
        if renderer_used in _RENDERERS_3D:
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw_native_3d(
                plt, np, native, rows=rows, n=n, title=title, stop_index=stop_index,
                apertures=apertures, extra_strings=tuple(far_strings),
            )
        elif renderer_used == "native":
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw_native_overlay(
                plt, np, native, rows=rows, n=n, title=title, stop_index=stop_index,
                apertures=apertures, folded=folded, global_frames=global_frames,
                projection=projection, config_identity=config_identity,
                decision=decision,
                extra_strings=tuple(far_strings) + missing_strings,
            )
        elif folded:
            # The global-frame coherent folded figure.
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw_folded_global(
                plt, np, rows, n, title, stop_index, apertures, rims,
                draw_heights, degraded, global_frames, ray_data,
                effective_draw_rays, projection, config_identity,
                outline=outline, extra_strings=fallback_strings + missing_strings,
            )
        else:
            # The all-refractive UNFOLDED path.
            (fig, surface_labels, stop_label, n_rays_drawn,
             figure_disclosures) = _draw(
                plt, np, rows, n, title, stop_index, folded, apertures, rims,
                draw_heights, degraded, ray_data, effective_draw_rays,
                projection, config_identity, outline=outline,
                exclusion=decision,
                extra_strings=(fallback_strings + tuple(far_strings)
                               + missing_strings),
            )
        _layout_meta = dict(getattr(fig, "_optivibe_layout_meta", {}) or {})
        _legend_seat = getattr(fig, "_optivibe_legend_seat", None)

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
        # The native overlay carries its own save arguments (its own dpi, NO tight
        # crop: a crop would shift the registration); the self figures keep theirs.
        fig.savefig(tmp, format="png", **getattr(
            fig, "_optivibe_save_kwargs", {"dpi": 120, "bbox_inches": "tight"}))
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
        if asphere_sag_modelled and renderer_used == "self":
            notes.append(
                f"surfaces {asphere_sag_modelled} are even aspheres drawn from the FULL "
                "sag (sphere + conic + polynomial term) — the drawn profile is faithful"
            )
        if asphere_sag_approximate and renderer_used == "self":
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
        if folded and renderer_used == "self":
            # the folded figure is now drawn COHERENTLY in the global
            # frame (elements + rays share one frame), so the note is honest about that
            # — no longer the "axial layout past the fold is schematic" caveat. A
            # surface whose global frame was degraded is surfaced via `degraded`.
            notes.append(
                "Folded system drawn in the global frame (element vertices from "
                "GetGlobalMatrix coherent with the RAGY/RAGZ rays). For the native 3-D "
                "view call render_layout with renderer=\"native_3d\" (or "
                "\"native_shaded\")."
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
                f"profile truncated at last valid sample on {named} "
                "(sag invalid at the aperture edge; a group cap or an "
                "internal interface)"
            )
        if all_zero_semi:
            notes.append("all semi-diameters read 0/non-finite; used h=1.0 fallback")
        if degraded:
            notes.append(f"surfaces {degraded} were unreadable and degraded")
        flags = list(ray_flags)  # additive machine-readable channel (rays + geometry)
        flags.extend(native_flags)
        flags.extend(far_flags)
        flags.extend(coverage_flags)
        if _legend_seat == "no_clear_seat":
            # dogfood F-4: never silent -- the bounded seat search found no seat inside the
            # figure clear of the disclosures/stamps/captions, so the legend kept its seat.
            flags.append("ray legend could not be seated clear of the figure's "
                         "disclosures and stamps; it may overlap them")

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
        if renderer_used in _RENDERERS_3D:
            # POPPED under the 3-D tokens -- the key, its flag and _REASON_GRIN (how
            # a 3-D view depicts a GRIN is unmeasured, so nothing is claimed about it).
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
        if (folded and renderer_used == "self" and not surface_labels
                and n_rays_drawn == 0 and not image_frame_ok):
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
        if folded and renderer_used == "self":
            # the token describes OUR folded global-frame drawing, never vendor ink
            coverage_reasons.append(_REASON_FOLDED)
        if grin_index_profile_not_drawn:
            coverage_reasons.append(_REASON_GRIN)

        _result = {
            "ok": True,
            "path": attempted,
            # A6: TRUE when this tool chose the name itself. A minted figure is
            # SCRATCH — the REVIEWABLE figure is the one save_candidate(render=true)
            # writes beside its .zmx.
            "minted": minted,
            "workspace_root": _wsp.workspace_root(session),
            "size_bytes": size,
            "surface_labels": surface_labels,
            "stop_label": stop_label,
            "folded": folded,
            "note": note,
            # element_outline: an IDENTITY key, not a finding key
            # -- echoed UNCONDITIONALLY on every ok:true envelope, equal to the
            # EFFECTIVE token, in the same family as `draw_rays` /
            # `config_evaluated` / `n_surfaces`. An absent key would make
            # "grouped" and "producer predates the convention" indistinguishable.
            # Read from the ALREADY-VALIDATED params, never re-derived here. The
            # old form called the resolver a second time and `or`-coerced its
            # `None` to "grouped", so an unrecognised value would have been
            # echoed as a legal one -- contradicting this module's own
            # no-normalisation rule. One validation, one value, one echo.
            "element_outline": outline,
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
            # The LDE row count read at the TOP of this call —
            # the SAME `n` this figure was drawn from, never a re-read (a re-read here
            # would recreate 's temporal proxy inside one tool and would pass
            # every other test, because the two readings agree on healthy systems).
            # NEVER derived from surface_labels: those are the surfaces DRAWN, and the
            # gap is not a constant — live 9/7, 4/2, 14/12, 25/23 (gap 2, nothing
            # suppressed) but 12/5 (gap 7) on cb_folded_rows. Counts EVERY row,
            # including object, image and every suppressed CB / flat-air dummy;
            # the image plane is `n_surfaces - 1`.
            # Present on this envelope (the only ok:true exit) and ABSENT on every
            # ok:false envelope, so `"n_surfaces" in env` means exactly: ok:true from
            # a producer at or after this change, value a builtin int >= 3.
            "n_surfaces": n,
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
            # native-layout-retool: WHO drew these bytes, whether the default fell
            # back from its first choice and why (null = it did not), and which far
            # end the shared rule left out of the frame ([] = nothing), each entry
            # carrying its own numbers so a wrong cut is auditable.
            "renderer": renderer_used,
            "renderer_fallback": fallback_token,
            "far_object_excluded": (
                [dict(e) for e in decision.excluded] if decision is not None else []),
            # WHICH of the rays OptiVibe SAMPLED could not reach the image
            # (`basis` names the sample; native: exporter correspondence UNVERIFIED).
            "ray_coverage": ray_coverage,
        }
        # native: the SELF-only ink keys describe nothing on vendor ink -> removed;
        # `registration` rides EVERY native cross-section envelope (verified and
        # withheld alike); `stamps_withheld` names why an explicit native is unstamped.
        if renderer_used != "self":
            for _key in _NATIVE_INK_KEYS:
                _result.pop(_key, None)
            if native is not None and native.registration is not None:
                _result["registration"] = dict(native.registration)
            if native is not None and native.stamps_withheld is not None:
                _result["stamps_withheld"] = native.stamps_withheld
        # The 3-D counterpart of `registration`, on EVERY 3-D envelope; the
        # sampled-ray coverage is POPPED there.
        if renderer_used in _RENDERERS_3D:
            _result["view_3d"] = dict(native.view_3d)
            _result.pop("ray_coverage", None)
        # GRIN (§6.2): the additive index-not-drawn surface list — emitted ONLY when
        # non-empty (a non-GRIN system stays byte-for-byte unchanged).
        if grin_index_profile_not_drawn:
            _result["grin_index_profile_not_drawn"] = grin_index_profile_not_drawn
        # The synthetic bands this figure DREW. Emitted only
        # when non-empty (the GRIN precedent), so a design with no extended
        # interface keeps a byte-identical envelope. ALWAYS COMPLETE: it carries
        # every affected surface regardless of what fitted on the canvas. It no
        # longer carries it "even when the figure string was truncated away" —
        # that string is placed FIRST and PROTECTED from the overflow pop (see
        # the producer's own note), so the canvas cannot take it away at all.
        _interfaces_extended = list(_layout_meta.get("interfaces_extended", []))
        if _interfaces_extended:
            _result["interfaces_extended"] = _interfaces_extended
        # The groups that drew GROUPED ink under a `per_element` request.
        # Same emission rule as the band list — present
        # only when non-empty, so every figure whose convention DID apply keeps a
        # byte-identical envelope, and `grouped` renders never carry it at all.
        # Without it the envelope echoed `per_element` over grouped ink with no
        # channel a caller could read the exception from.
        _groups_not_split = list(_layout_meta.get("groups_not_split", []))
        if _groups_not_split:
            _result["groups_not_split"] = _groups_not_split
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
        # Clean a leftover temp on any unexpected exit path -- and the native
        # export's raster, which is never the delivered file.
        for _left in [tmp] + native_tmps:
            if _left is None:
                continue
            try:
                if os.path.exists(_left):
                    os.remove(_left)
            except OSError:
                pass


#: The served description's HARD CAP (native-layout-retool): 5,416 chars MEASURED
#: (`len(RENDER_LAYOUT_SPEC.description)`) + 1,000 [unmeasured
#: choice: room for the four call-changing native facts an agent must read at
#: tool-selection time]. Pinned by a test so a later edit cannot exceed it silently;
#: slimming is.
_DESCRIPTION_CHAR_CAP = 6_416


RENDER_LAYOUT_SPEC = ToolSpec(
    name="render_layout",
    handler=render_layout,
    required_params=(),
    param_types={"title": "string", "path": "string", "draw_rays": "boolean",
                 "config": "number",
                 # element-edge convention selector; the closed
                 # vocabulary lives in the description prose, NOT in a served
                 # enum -- see the description and `_resolve_element_outline`.
                 "element_outline": "string",
                 # who draws the picture (native-layout-retool); the same
                 # prose-not-enum rule -- see `_resolve_renderer`.
                 "renderer": "string"},
    description=(
        "With no path the figure goes to candidates/scratch/ as a numbered scratch "
        "file (minted:true) and never overwrites an earlier one; if that directory "
        "cannot be listed the call REFUSES (workspace_unlistable) and writes nothing, "
        "because a name it cannot prove is free could destroy an existing figure. A "
        "REVIEWABLE figure is the one save_candidate(render=true) writes beside its "
        ".zmx. "
        "renderer picks who draws the picture: 'native' (OpticStudio's own "
        "cross-section, axial systems only; it cannot draw any coordinate break, even "
        "all-zero) or 'self' (OptiVibe's drawing, below), or the UNSTAMPED "
        "'native_3d' / 'native_shaded' views (one configuration each, rays optional, "
        "the same far-object exclusion, folds included). Omit it for the default; "
        "every successful result names the renderer that drew the bytes (renderer) "
        "and, if the default could not use its first choice, why (renderer_fallback). "
        "An explicit renderer that cannot run is refused, never swapped. Both "
        "cross-sections stamp "
        "OUR surface numbers; native stamps are registered to the prescription and "
        "ink-verified (registration), and an explicit native whose check failed is "
        "UNSTAMPED (stamps_withheld). A very distant object or image plane is left "
        "out by BOTH and named in far_object_excluded, with a marker at the frame "
        "edge; for self, on a folded system that rule is computed and disclosed in "
        "flags but not applied. "
        "The self renderer draws a real meridional (y-z) optical layout PNG for the "
        "user, in an "
        "ISO 10110-inspired visual style (a LOOK borrowed from optical drawing "
        "practice — this is NOT a standards drawing and nothing here is a "
        "conformance claim): equal-aspect (true curvature), cement-aware closed "
        "element outlines with a FLAT rim, two line weights, a patterned optical "
        "axis + image plane, a STOP marker, OUR stamped surface numbers. "
        "element_outline (self renderer only; giving it "
        "selects self) selects the element-outline convention and takes exactly one of two values: 'per_element' (the "
        "default) or 'grouped'; any other value — including a differently-cased or "
        "space-separated spelling — is REFUSED before anything is drawn or "
        "written, and the effective value is echoed back as element_outline on "
        "every successful result. Under 'per_element' each ELEMENT of a cemented "
        "run is closed as its own body out to its own rim, and the shared cemented "
        "surface is a cap of the two bodies that meet there. Under 'grouped' the "
        "whole cemented run is closed as ONE body out to the group's rim, so a "
        "cemented internal interface is drawn flat out to that rim. Both are "
        "drawing conventions over incomplete part-boundary data and neither is a "
        "measurement of a part boundary. Under EITHER, a surface shorter than the "
        "rim of the body that carries it is drawn flat past its own measured clear "
        "semi-diameter, and that band is SYNTHETIC — a drawing convention, not a "
        "measurement — and is disclosed rather than drawn silently: the figure "
        "carries one aggregated line naming every affected cemented join, and "
        "interfaces_extended carries a per-surface own_semi/drawn_to/synthetic_mm "
        "band width for the cemented joins among them (present only when some "
        "interface was extended, and always complete even when the figure's own "
        "line did not fit on the canvas). Under 'per_element' a cemented run is "
        "split into its elements only where every consecutive pair has at least "
        "one MEASURED aperture; a run where some pair has none is drawn as ONE "
        "body instead, because a rim for that pair could only come from heights "
        "nobody measured — and that exception is never silent: the figure says so, "
        "and groups_not_split lists those runs with the pairs that blocked them "
        "(present only when it happened). And — unless draw_rays=False — the chief + "
        "upper/lower marginal ray of each field, retained on purpose because ray "
        "bending is the strongest cue that a system is sane (one color per field). "
        "A body's rim "
        "height — the body being a cemented GROUP under 'grouped' and a single "
        "ELEMENT under 'per_element' — is the maximum MEASURED clear semi-diameter "
        "over that body: an approximation forced by the absence of any "
        "part-boundary data, and it is not a part dimension. An aperture that "
        "could not be read is DISCLOSED "
        "(drawn as unknown, never as a number nobody measured), as is a projection "
        "the check cannot vouch for; one active configuration is drawn and labelled. "
        "Returns the saved PNG path plus png_valid/n_fields/n_rays_drawn/n_surfaces/"
        "aperture_not_measured/profile_not_measured/out_of_plane/figure_disclosures/"
        "cb_suppressed/scaffold_suppressed/renderer/renderer_fallback/"
        "far_object_excluded/ray_coverage/flags; inspect result.ok. n_surfaces is the "
        "system's surface count measured at render time from the state drawn — every "
        "row including object, image and suppressed scaffolding, so the image plane is "
        "n_surfaces-1; it is NOT len(surface_labels) and must not be derived from them. "
        "It is present on every ok:true result and absent when ok is false. Folded systems "
        "(coordinate break / mirror) are drawn by the self renderer in the GLOBAL "
        "frame, coherent with "
        "the rays — so the rays ARE drawn for a fold; coordinate-break and flat "
        "powerless air dummy/spacer surfaces are suppressed (scaffolding, not "
        "drawn); real optics (glass, mirrors, curved lens-backs), the stop, and the "
        "image are stamped with their true Zemax numbers. Gotcha: the surface-number "
        "stamps are OptiVibe's on every cross-section renderer; a bare vendor image "
        "numbers nothing, and under 'self' the figure is self-drawn. The saved .zmx "
        "is the full-fidelity record. This PNG is a SCRATCH drawing for your "
        "own eyes; it is NOT the reviewable figure — a finding about it cannot be "
        "recorded (record_findings refuses it as finding_figure_unbound). To have a "
        "figure reviewed, call save_candidate first and review ITS paired PNG, which is "
        "the one png_sha256 binds. "
        "ray_coverage: fields whose sampled rays fail. "
        "TALKING ABOUT A SURFACE WITH THE USER: this figure stamps OUR surface numbers "
        "on it, so the user points at a stamped number to talk about a surface -- keep "
        "that convention in mind for the rest of the exchange, including turns where no "
        "tool is open. Pair it with describe_surfaces, which gives the surface-number -> "
        "role ground-truth table, so number talk is unambiguous. "
        "BEFORE PRESENTING A MULTI-CONFIG DESIGN: call freeze_semidiameters (so each "
        "element draws at ONE size across configs -- a physically-correct layout) and "
        "verify_zoom (which flags a gap declared to zoom that is CONSTANT across "
        "configs). "
        "See fold_beam."
    ),
)

TOOL_SPECS = (RENDER_LAYOUT_SPEC,)
