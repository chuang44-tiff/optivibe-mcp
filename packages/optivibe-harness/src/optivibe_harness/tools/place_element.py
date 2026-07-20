"""tools/place_element.py — the perturb-and-return tilted/decentered-element composer.

ONE NEW first-class dispatchable composer:

- ``place_element`` — tilt/decenter an EXISTING element span (first..last) about its
  own frame and restore the downstream axis with a CO-LOCATED return CB — THE §0
  element-half bug fix. It WRAPS the engine's native ``RunTool_TiltDecenterElements``
  (Decision = WRAP: the native generator authors HONEST cell.
  DataType-keyed CB Par cells + a co-located negated/Order-flipped return CB + a
  trailing Dummy and renumbers predictably), owns the surface renumber (no raw indices
  handed across the inserts), and FALSIFIES the composition against the INDEPENDENT
  global frame (the ``_cb_cells`` compose-oracle on the return CB + the
  downstream-R==parent-OBJECT-R block compare) + the rays-reach oracle
  (``_beam_reach.beam_reaches_span``), rolling back on any miss.

CORRECTIONS (re-probe ``probe_place_element_colocation.py``, 2026-06-21): the
native tool ALWAYS co-locates the return CB with the ENTRY CB (it inserts an extra
Standard "back-up spacer" at ``return_cb − 1`` whose thickness = ``−Σ(wrapped axial
thicknesses)`` that walks the vertex back to the entry origin). There is NO co-location
gap to enforce — the original ``gap=0`` check read that spacer's ``−Σ`` thickness and
was structurally impossible to satisfy (it rolled back every correct wrap). So the
composer NEVER rolls back on co-location and NEVER touches the spacer (zeroing it would
shift the image plane by ``Σ`` — a back-focus error). ``colocated`` is a vertex-match
DISCLOSURE (entry-CB vs return-CB global vertex), not a gate. The wrap's entry/return CB
are identified by SET-DIFF (the CBs in ``post_set − pre_set``, renumber-adjusted), NOT
first/last-in-table — a pre-existing upstream/downstream CB would otherwise be
mis-picked; a wrap window already containing a CB, a
set-diff not yielding exactly 2 new CBs, OR an UNREADABLE CB-candidate row FAIL CLOSED
(rollback). The last OPTICAL wrapped surface is ``return_cb − 2`` (the spacer at
``return_cb − 1`` is skipped). The trailing Dummy disclosure is gated on a Standard
surface NEWLY inserted PAST the return CB (the set-diff role), NOT a ``"dummy"`` comment.

Spine = author-local / falsify-global (L28). ``place_element`` never hand-computes a
global vertex; it delegates the coordinate authoring to the native generator, then
PROVES the result against the independently-measured global frame. A clean native call
is NOT proof — the falsifier decides commit-vs-rollback.

Q-OPEN-2 = PARTIAL (the load-bearing design finding, probe-proven): the native return
CB is the COMPLETE inverse for the ORIENTATION (tilt) — the downstream rotation block is
byte-identical to the parent OBJECT block (residual 0.0; return-CB compose-oracle
4.26e-13) — but NOT for the DECENTER position: a decentered element's downstream axis
ends up parallel-but-laterally-shifted by the full decenter. So ``place_element`` proves
ORIENTATION-restored (the decisive commit invariant) and DISCLOSES the residual lateral
decenter honestly (``downstream_lateral_shift``), NEVER silently implying a
position-restore (the ``add_return_cb`` "author the transform, not an on-axis guarantee"
precedent). No compensating shift is built here (the honest disclosure IS the
correct boundary).

GetGlobalMatrix anchoring (the load-bearing gotcha): GetGlobalMatrix anchors the ENTRY
CB as the global origin, so the parent frame is the OBJECT (surf 0) rotation block, NOT
raw identity. The restoration proof is ``downstream_R == OBJECT_R``, never
``downstream_R == identity`` (a false ``sin(tilt)`` fail). The PRIMARY proof is the
``_cb_cells.relative_rotation_residual`` compose-oracle on the RETURN CB (it removes the
upstream frame — the decisive surface), with the ``downstream_R == OBJECT_R``
block compare as the corroborating check.

``tool.Order`` on ``ITiltDecenterElements`` is the ``TiltDecenterOrderType`` ENUM
(``Decenter_Tilt`` = 0, ``Tilt_Decenter`` = 1), NOT an int — pythonnet 3.0 REFUSES an
implicit int->Enum, so a bare ``tool.Order = 0`` SILENTLY no-ops; the member is resolved
first (the probe ``_resolve_order_member`` idiom). This is LOAD-BEARING — a bare int
drives the tool at its default order.

Live ZOS-API integration: exercised by a live test; unit-tested against
fake doubles (which COMPUTE the resulting global frame from
first principles, so a placement bug / a rollback bug cannot green-pass).
"""
import math

from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _beam_reach
from . import _cb_cells as _cb
from . import _lens_common as _lc
from ._analysis_common import error_envelope

# Family tokens (the failure envelope ``error_family`` values).
_ELEMENT_PARAM = "element_param"                  # a bad param value (pre-mutation)
_ELEMENT_FRAME_UNRESTORED = "element_frame_unrestored"  # orientation falsifier reject
_ELEMENT_UNREACHED = "element_placement_unreached"      # rays-reach oracle reject
_ELEMENT_WRITE = "element_write"                  # an authoring / checkpoint fault

# The orientation-restored gate: reuse the CB primitive's machine-epsilon gate (the
# observed return-CB compose residual was 4.26e-13; downstream-vs-OBJECT 0.0). 1e-9 is
# nine-plus orders of magnitude above the floor while far below any genuine mis-author.
_ORIENT_TOL_DEG = _cb.GLOBAL_FRAME_TOL_DEG

# The co-location DISCLOSURE tolerance (lens units): the native return CB is structurally
# co-located with the ENTRY CB (their global AXIAL Z coincide via the −Σ back-up spacer).
# This is a DISCLOSURE only (``colocated``), NEVER a rollback gate — the re-probe proved
# the orientation/reach falsifiers are the real proofs and they pass on the native wrap.
# A GENEROUS floor (~0.1 mm) absorbs the native co-location residual (entry z=0 vs return
# z≈0.009 mm observed) while staying well below any genuine non-co-location — the old tight
# 1e-3 read False on the real residual.
_COLOCATION_VERTEX_TOL = 0.1

# A global vertex coordinate whose magnitude reaches this is a sentinel/collapse — the
# OBJECT-at-infinity object-distance marker (~1e8 / the −1e10 sentinel) or a degenerate
# read — never a real position to difference (the ``_beam_reach._position_ok`` precedent).
_POSITION_SENTINEL_MAGNITUDE = 1e9


# --------------------------------------------------------------------------- #
# Shared validation.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}``."""
    return params if isinstance(params, dict) else {}


def _finite_number(value, label, default=None):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float.

    ``default`` is used when the key was absent (``value is None``) — the caller passes
    the param's ``.get(key)`` so an OMITTED optional param defaults to 0.0; a PRESENT
    bad value (a string / bool / NaN / inf) is rejected LOUD.
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number, got {type(value).__name__} {value!r}"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be a finite number (inf/-inf/nan are non-physical), got "
            f"{value!r}"
        )
    return coerced


def _require_order(value, label="order"):
    """Require an Order flag in {0, 1}, coercing an INTEGRAL float (the number contract).

    ``order`` is advertised as ``param_types["order"] == "number"`` so the MCP/JSON
    round-trip delivers an integral float (``1`` -> ``1.0``); per the number contract
    an integral ``0.0``/``1.0`` is accepted and coerced. ``1.5`` (non-integral), ``"1"``
    (a string), ``True`` (a bool), NaN/inf, and an order outside {0, 1} are rejected LOUD.
    The CB Par6 Order cell is Integer (the native tool authors the entry CB with this
    Order and the return CB with the flipped Order).
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"{label} must be an integer 0 or 1, not a bool ({value!r})"
        )
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            coerced = int(value)
        else:
            raise ToolParamError(
                f"{label} must be an integer 0 or 1, got non-integral float {value!r}"
            )
    else:
        raise ToolParamError(
            f"{label} must be an integer 0 or 1, got {type(value).__name__} {value!r}"
        )
    if coerced not in (0, 1):
        raise ToolParamError(
            f"{label} must be 0 (decenter-then-tilt) or 1 (tilt-then-decenter), got "
            f"{coerced}"
        )
    return coerced


def _require_bool(value, label):
    """Require a real bool (reject 0/1 ints + 'true'/'false' strings)."""
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{label} must be a bool (true/false), got {type(value).__name__} {value!r}"
        )
    return value


# =========================================================================== #
# place_element — the dispatchable composer.
# =========================================================================== #
def place_element(session, params):
    """Tilt/decenter an existing element span and restore the downstream axis (perturb-and-return).

    Params: ``first``/``last`` (REQUIRED interior surfaces of the EXISTING element span
    to wrap, ``1 <= first <= last <= N-1``), ``tilt_x``/``tilt_y``/``tilt_z`` (deg, def
    0.0), ``decenter_x``/``decenter_y`` (lens units, def 0.0), ``order`` (0/1, def 0,
    integral-float coerced), ``draw`` (bool, def False), ``design_name`` (str).

    Validate-before-mutate firewall (1<=first<=last<=N-1, refuse OBJECT, require a
    downstream IMAGE). Then ALL inside ONE SaveAs/LoadFile atomic checkpoint: capture the
    pre-wrap CB surface set, WRAP the native ``RunTool_TiltDecenterElements`` (entry CB
    before first, co-located return CB after last, +1 renumber, ``Order`` resolved as the
    ENUM MEMBER — a bare int silently no-ops), identify the wrap's entry/return CB by
    SET-DIFF (post − pre; exactly 2 new CBs or FAIL CLOSED), then FALSIFY: the
    orientation is restored (the return-CB compose-oracle + downstream-R==OBJECT-block,
    fails CLOSED on an unreadable frame) AND the rays reach the span. Disclose the
    residual ``downstream_lateral_shift`` (never claim a position-restore) + ``colocated``
    (entry-vs-return vertex match, disclosure only). On ANY falsifier miss / fault ->
    atomic LoadFile rollback. NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        first, last, tilt_x, tilt_y, tilt_z, decenter_x, decenter_y, order, draw, \
            design_name = _validate_place_params(session, params)
    except ToolParamError as exc:
        return error_envelope("place_element", _ELEMENT_PARAM, str(exc))
    except Exception as exc:  # noqa: BLE001 — a pre-mutation read fault -> element_param
        return error_envelope(
            "place_element", _ELEMENT_PARAM,
            f"could not validate the place_element request ({exc!r})",
        )

    # The whole wrap + falsify runs inside ONE SaveAs/LoadFile checkpoint (the fold_beam
    # precedent): the native tool's renumber is irrelevant once the outer LoadFile rolls
    # back, and the orientation/reach falsifiers decide commit-vs-rollback.
    return _place_element_checkpointed(
        session, first, last, tilt_x, tilt_y, tilt_z, decenter_x, decenter_y, order,
        draw, design_name,
    )


def _validate_place_params(session, params):
    """Validate every place_element param BEFORE any mutation (the insert firewall).

    The surface-bounds FIREWALL runs HERE, before any native wrap — an out-of-range wrap
    could renumber unexpectedly, so the bound can NEVER be an exception handler (the
    insert_surface precedent). ``first``/``last`` must be interior surfaces with an IMAGE
    surface remaining downstream so the post-wrap frame has a surface to measure (the
    fold_beam ``surface >= n-1`` precedent).
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)

    first = _lc._require_int_index(params, "first")
    last = _lc._require_int_index(params, "last")

    # The geometry firewall: refuse OBJECT(0); require both endpoints interior. The
    # native wrap inserts the entry CB BEFORE ``first`` and the return CB (+ Dummy)
    # AFTER ``last``; an IMAGE surface must remain downstream of the wrap so the
    # post-wrap frame has a surface to measure (so last <= N-2 — a wrap ENDING on the
    # IMAGE surface leaves no downstream optic).
    if first < 1:
        raise ToolParamError(
            f"first must be an interior surface (>= 1), got {first} (the object surface "
            "0 cannot be wrapped)"
        )
    if last < first:
        raise ToolParamError(
            f"last ({last}) must be >= first ({first}); the element span runs first..last"
        )
    if last >= n - 1:
        raise ToolParamError(
            f"last ({last}) is out of range (1..{n - 2}); the wrap needs a downstream "
            f"IMAGE surface (N={n}, IMAGE={n - 1}) for the return CB + the post-wrap "
            "frame to measure — wrap an interior element span, not one ending on the "
            "image plane"
        )

    tilt_x = _finite_number(params.get("tilt_x"), "tilt_x", default=0.0)
    tilt_y = _finite_number(params.get("tilt_y"), "tilt_y", default=0.0)
    tilt_z = _finite_number(params.get("tilt_z"), "tilt_z", default=0.0)
    decenter_x = _finite_number(params.get("decenter_x"), "decenter_x", default=0.0)
    decenter_y = _finite_number(params.get("decenter_y"), "decenter_y", default=0.0)
    order = _require_order(params.get("order", 0))
    draw = _require_bool(params.get("draw", False), "draw")
    design_name = params.get("design_name")
    if design_name is not None and not isinstance(design_name, str):
        raise ToolParamError(
            f"design_name must be a string, got {type(design_name).__name__} "
            f"{design_name!r}"
        )

    return (first, last, tilt_x, tilt_y, tilt_z, decenter_x, decenter_y, order, draw,
            design_name)


# --------------------------------------------------------------------------- #
# The atomic checkpointed body.
# --------------------------------------------------------------------------- #
class _PlaceUnverified(Exception):
    """The placement falsifier (orientation OR reach) rejected the wrap.

    Carries an ``error_family`` (``element_frame_unrestored`` / ``element_placement_
    unreached``) + an ``extra`` dict so the rollback envelope discloses the measured-vs-
    requested numbers honestly.
    """

    def __init__(self, message, *, family, extra=None):
        super().__init__(message)
        self.error_family = family
        self.extra = extra or {}


def _place_element_checkpointed(session, first, last, tilt_x, tilt_y, tilt_z,
                                decenter_x, decenter_y, order, draw, design_name):
    """Run the wrap + falsify inside ONE SaveAs/LoadFile checkpoint. Never raises.

    On ANY fault inside the boundary — a native-tool throw, an authoring fault, OR the
    orientation/reach falsifier rejecting the placement — the system is restored from the
    temp ``.zmx`` checkpoint (so a half-wrap is NEVER left committed). The temp + its
    native ``.ZDA`` companion are reaped on every path (the apply_lens_spec #59
    precedent); the LoadFile path is forward-slashed (#73).
    """
    import glob
    import os
    import tempfile

    from .optimize_merit_io import _unlink_quiet

    system = session.system

    checkpoint_path = None
    try:
        try:
            fd, checkpoint_path = tempfile.mkstemp(
                suffix=".zmx", prefix="optivibe_place_ckpt_"
            )
            os.close(fd)
            # Capture a cheap pre-mutation snapshot (the fold_beam BUG-3 pattern) so the
            # rollback can POST-RESTORE read-vs-read verify the LoadFile actually restored
            # the pre-place form. Taken BEFORE SaveAs so it reflects the exact pre-place
            # state the .zmx records.
            pre_snapshot = _place_pre_snapshot(system)
            system.SaveAs(_fwd(checkpoint_path))
        except Exception as exc:  # noqa: BLE001 — a checkpoint SaveAs throw -> fail-closed
            _reap_place_checkpoint(checkpoint_path, glob, os, _unlink_quiet)
            checkpoint_path = None
            return error_envelope(
                "place_element", _ELEMENT_WRITE,
                f"could not checkpoint the system before placing the element ({exc!r}); "
                "the system was NOT mutated — nothing was applied",
                rolled_back=False, checkpoint=False, partial_state=False,
            )

        try:
            result = _place_element_impl(
                session, first, last, tilt_x, tilt_y, tilt_z, decenter_x, decenter_y,
                order, draw, design_name,
            )
            return result
        except _PlaceUnverified as exc:
            # The orientation / reach falsifier rejected the placement -> atomic rollback.
            return _rollback_place(
                system, checkpoint_path, pre_snapshot, family=exc.error_family,
                reason=str(exc), extra=exc.extra,
            )
        except (ToolParamError, SurfaceWriteError) as exc:
            # A structured authoring failure mid-wrap -> atomic rollback (no half-wrap).
            return _rollback_place(
                system, checkpoint_path, pre_snapshot, family=_ELEMENT_WRITE,
                reason=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 — a generic engine throw -> rollback (L26)
            return _rollback_place(
                system, checkpoint_path, pre_snapshot, family=_ELEMENT_WRITE,
                reason=f"unexpected engine fault placing the element ({exc!r})",
            )
    finally:
        _reap_place_checkpoint(checkpoint_path, glob, os, _unlink_quiet)


def _place_element_impl(session, first, last, tilt_x, tilt_y, tilt_z, decenter_x,
                        decenter_y, order, draw, design_name):
    """WRAP the native generator + read by role + falsify (raises into the checkpoint).

    ─── THE SURFACE-ALLOCATION INDEX MAP (probe EXP3 + the co-location re-probe) ──────
    The native ``RunTool_TiltDecenterElements`` inserts the entry CB IMMEDIATELY before
    FirstSurface, the return CB IMMEDIATELY after LastSurface, AND a Standard "back-up
    spacer" at ``return_cb − 1`` (thickness ``−Σ(wrapped axial thk)`` — it co-locates the
    return CB with the entry CB); a multi-surface wrap ALSO migrates the trailing airgap
    to a "Dummy" surface just past the return CB. Every surface from FirstSurface onward
    shifts +1 (the single inserted entry CB upstream). We identify the wrap's CB PAIR by
    SET-DIFF (the CBs in ``post_set − pre_set``, renumber-adjusted) — NOT first/last in
    the table, which a pre-existing fold CB would corrupt. The last OPTICAL wrapped
    surface is ``return_cb − 2`` (skip the −Σ spacer). Disclose ``index_shift = {from:
    first, delta: +1}``.
    ──────────────────────────────────────────────────────────────────────────────
    """
    system = session.system
    lde = system.LDE

    # ---- Capture the PRE-WRAP CB surface set (the SET-DIFF baseline). A wrap window that
    # ALREADY contains a CB, or an UNREADABLE CB-candidate row
    # in the window, FAILS CLOSED (an unverifiable CB row must never be silently treated as
    # "not a CB"). ----
    n_pre = int(lde.NumberOfSurfaces)
    pre_cb_set = _find_cb_surfaces(lde, n_pre)
    # Refuse a CB already inside the wrap window (first..last): the native wrap re-tilts an
    # existing CB-bearing element and the set-diff pair semantics break.
    window_cbs = [s for s in pre_cb_set if first <= s <= last]
    if window_cbs:
        raise _PlaceUnverified(
            f"the wrap window (surfaces {first}..{last}) already contains coordinate-break "
            f"surface(s) {window_cbs}; place_element wraps a plain element span — a CB "
            "already inside the span makes the entry/return identification ambiguous. "
            "Re-author the existing CB or wrap a CB-free span. Rolling back rather than "
            "claiming an unverified placement.",
            family=_ELEMENT_FRAME_UNRESTORED,
            extra={"window_cbs": list(window_cbs)},
        )

    # ---- WRAP the native TiltDecenterElements generator. ----
    _run_tilt_decenter(
        system, lde, first, last, tilt_x, tilt_y, tilt_z, decenter_x, decenter_y, order,
    )

    # ---- Identify the wrap's CB PAIR by SET-DIFF (post − pre, renumber-adjusted). ----
    n_post = int(lde.NumberOfSurfaces)
    post_cb_set = _find_cb_surfaces(lde, n_post)
    new_cbs = _wrap_cb_pair(pre_cb_set, post_cb_set, first, n_post - n_pre)
    if len(new_cbs) != 2:
        raise _PlaceUnverified(
            f"the native tilt/decenter wrap did not introduce exactly TWO new "
            f"coordinate-break surfaces (set-diff post {sorted(post_cb_set)} − pre "
            f"{sorted(pre_cb_set)} = {sorted(new_cbs)}); the native generator did not "
            "author the expected entry+return sandwich — rolling back rather than claiming "
            "an unverified placement",
            family=_ELEMENT_WRITE,
            extra={"new_cb_surfaces": sorted(new_cbs), "pre_cb_surfaces": sorted(pre_cb_set)},
        )
    entry_cb_surface, return_cb_surface = sorted(new_cbs)
    dummy_surface = _find_dummy_surface(lde, n_post, return_cb_surface, last > first)
    # The element span post-wrap = the OPTICAL surfaces strictly between the entry CB and
    # the −Σ back-up spacer (which sits at return_cb − 1). So the optical span is
    # ``entry_cb+1 .. return_cb−2`` (EXCLUDING the spacer); the last OPTICAL wrapped
    # surface is ``return_cb − 2``.
    last_optical = return_cb_surface - 2
    element_span_post = list(range(entry_cb_surface + 1, last_optical + 1))

    # ---- Co-location DISCLOSURE (entry-vs-return axial Z match), NOT a gate. ----
    # The native tool ALWAYS co-locates the return CB with the entry CB via the −Σ spacer;
    # there is no gap to enforce (the re-probe proved the orientation/reach falsifiers are
    # the real proofs). ``colocated`` is disclosed from the axial-Z coincidence within a
    # GENEROUS tol, with the axial residual NUMBER disclosed alongside; never rolled back
    # on, and a non-finite / sentinel-magnitude Z read discloses ``colocated`` UNKNOWN
    # (None) rather than leaking the sentinel.
    colocated, colocated_axial_residual = _vertex_colocated(
        system, lde, entry_cb_surface, return_cb_surface,
    )

    # ---- FALSIFY (the load-bearing proof, L28). ----
    # (decisive) orientation restored — the RETURN CB's rotation contribution via the
    # compose-oracle (upstream = the surface before the return CB) matches its authored
    # -tilt product within the gate, AND the downstream rotation block equals the PARENT
    # frame the element lives in (the surface JUST BEFORE the entry CB) within the same
    # gate. The parent frame is ``entry_cb − 1`` (== the OBJECT block when no upstream CB
    # exists, but the FOLDED parent when a pre-existing upstream fold does — comparing to
    # surf 0 would FALSE-FAIL a correctly-restored element placed downstream of a fold).
    # NEVER raw identity (the GetGlobalMatrix anchoring gotcha). Fails CLOSED: an unreadable
    # frame raises (read_global_matrix) -> element_frame_unrestored (the close-out).
    orientation_residual = _verify_orientation_restored(
        system, lde, n_post, entry_cb_surface, return_cb_surface,
    )

    # (reach) the rays reach the optics — beam_reaches_span over the entry CB .. the last
    # downstream surface (the IMAGE). A geometric miss / hard failure in the span ->
    # element_placement_unreached (the §0 dropped-co-location fingerprint).
    last_downstream = n_post - 1
    span = [entry_cb_surface, last_downstream]
    reach = _beam_reach.beam_reaches_span(
        system, entry_cb_surface, last_downstream
    )
    if not (isinstance(reach, dict) and reach.get("reaches") is True):
        first_miss = reach.get("first_miss") if isinstance(reach, dict) else None
        raise _PlaceUnverified(
            f"the placed element's beam does NOT reach the downstream optics: "
            f"beam_reaches_span({entry_cb_surface}..{last_downstream}) reports "
            f"reaches=False (first_miss={first_miss!r}); the placement steers the beam "
            "off an optic (the §0 dropped-co-location class) — rolling back rather than "
            "claiming an unverified placement",
            family=_ELEMENT_UNREACHED,
            extra={"first_miss": first_miss},
        )

    # ---- DISCLOSE the residual lateral decenter (never claim a position-restore). ----
    # Q-OPEN-2 (live-probed): the native return restores the TILT exactly but leaves the
    # DECENTER as a parallel lateral translation — so the residual lateral shift IS the
    # requested decenter (pure-tilt -> 0; decenter 2.0/1.5 -> 2.5). It is computed
    # DETERMINISTICALLY from the VALIDATED request, NEVER from a downstream/IMAGE global
    # vertex: a vertex read conflates this clean residual with (a) the infinite-conjugate
    # OBJECT object-distance sentinel and (b) the tilt-over-thickness SHEAR propagated to
    # the far IMAGE (a pure-tilt read 0.209 mm = tan(1deg)*track, not the intended ~0). The
    # actual geometry is verified by the load-bearing orientation + reach proofs above, not
    # by this disclosure hint. This is DISCLOSED honestly, never silently implied as
    # restored.
    downstream_lateral_shift = math.hypot(decenter_x, decenter_y)

    result = {
        "ok": True,
        "first": first,
        "last": last,
        "tilt_x": _safe(tilt_x),
        "tilt_y": _safe(tilt_y),
        "tilt_z": _safe(tilt_z),
        "decenter_x": _safe(decenter_x),
        "decenter_y": _safe(decenter_y),
        "order": order,
        "entry_cb_surface": entry_cb_surface,
        "return_cb_surface": return_cb_surface,
        "dummy_surface": dummy_surface,
        "element_span_post": element_span_post,
        "index_shift": {"from": first, "delta": 1},
        "orientation_restored": True,
        "orientation_residual_deg": _safe(orientation_residual),
        "colocated": colocated,
        "colocated_axial_residual": _safe(colocated_axial_residual),
        "downstream_lateral_shift": _safe(downstream_lateral_shift),
        "rays_reach": True,
        "span": span,
        "restores_on_axis_only_if_colocated": True,
        "warning": (
            "the downstream ORIENTATION is restored parallel to the parent axis, but a "
            "DECENTERED element's beam returns LATERALLY SHIFTED by the decenter "
            f"(downstream_lateral_shift={_safe(downstream_lateral_shift)!r}) — the "
            "position is NOT restored (the native return CB inverts the tilt, not the "
            "decenter). The native tool always co-locates the return CB with the entry CB "
            "(via a −Σ back-up spacer), so the orientation inverse is exact. The "
            "entry/return CBs reference surfaces by NUMBER; a later "
            "upstream insert_surface renumbers and desyncs them — re-author after any "
            "upstream insert."
        ),
    }

    # ---- (optional) best-effort figure (NON-fatal). ----
    figure_path = None
    figure_error = None
    if draw:
        figure_path, figure_error = _emit_figure(session, design_name)
    result["figure_path"] = figure_path
    result["figure_error"] = figure_error
    return result


# --------------------------------------------------------------------------- #
# The native TiltDecenterElements drive (the probe idiom; enum-injection seam).
# --------------------------------------------------------------------------- #
def _run_tilt_decenter(system, lde, first, last, tilt_x, tilt_y, tilt_z, decenter_x,
                       decenter_y, order):
    """GetTool_TiltDecenterElements -> set props (Order = ENUM MEMBER) -> RunTool.

    The Order property is the ``TiltDecenterOrderType`` ENUM (pythonnet 3.0 refuses an
    int->Enum implicit convert), so a bare ``tool.Order = 0`` SILENTLY no-ops; we resolve
    the member whose underlying value == the requested order (the probe idiom) and set
    THAT. This is LOAD-BEARING — a bare int drives the tool at its default order. A native
    drive throw -> ``SurfaceWriteError`` (caught by the checkpointed caller -> rollback).
    The tool is ``Close()``/``Dispose()``d after the run.
    """
    try:
        tool = lde.GetTool_TiltDecenterElements()
    except Exception as exc:  # noqa: BLE001 — a get-tool THROW -> element_write
        raise SurfaceWriteError(
            f"could not open the native TiltDecenterElements tool ({exc!r}); refusing "
            "rather than shipping an unverified placement",
            field="tilt_decenter_tool", intended=None, actual=None, surface=None,
        ) from exc
    if tool is None:
        raise SurfaceWriteError(
            "the native TiltDecenterElements tool opened as None; refusing rather than "
            "shipping an unverified placement",
            field="tilt_decenter_tool", intended=None, actual=None, surface=None,
        )
    try:
        # Resolve the Order ENUM MEMBER (the load-bearing seam: a bare int silently
        # no-ops). The fake injects ``_resolve_tilt_decenter_order`` via the enum seam.
        order_member = _resolve_order_member(system, tool, order)
        try:
            tool.FirstSurface = first
            tool.LastSurface = last
            tool.TiltX = float(tilt_x)
            tool.TiltY = float(tilt_y)
            tool.TiltZ = float(tilt_z)
            tool.DecenterX = float(decenter_x)
            tool.DecenterY = float(decenter_y)
            tool.GlobalCoordinates = False  # local element tilt (cells read back regardless)
            tool.HideTrailingDummySurface = False  # we read the post-table by role
            tool.Order = order_member  # the resolved ENUM member, NEVER a bare int
        except Exception as exc:  # noqa: BLE001 — a property-set THROW -> element_write
            raise SurfaceWriteError(
                f"could not set the native TiltDecenterElements properties ({exc!r}); "
                "refusing rather than shipping an unverified placement",
                field="tilt_decenter_props", intended=None, actual=None, surface=None,
            ) from exc
        try:
            lde.RunTool_TiltDecenterElements(tool)
        except Exception as exc:  # noqa: BLE001 — a run THROW -> element_write
            raise SurfaceWriteError(
                f"the native RunTool_TiltDecenterElements threw ({exc!r}); refusing "
                "rather than shipping an unverified placement",
                field="tilt_decenter_run", intended=None, actual=None, surface=None,
            ) from exc
    finally:
        # Close/Dispose the tool (the native tool is a single-slot resource — the L22
        # reap analogue). Never mask the result.
        for meth in ("Close", "Dispose"):
            try:
                getattr(tool, meth)()
                break
            except Exception:  # noqa: BLE001 — teardown must never mask the outcome
                pass


def _resolve_order_member(system, tool, order):
    """Resolve the ``TiltDecenterOrderType`` ENUM member for ``order`` (the load-bearing seam).

    Mirrors the probe ``_resolve_order_member``: read the live ``tool.Order`` property to
    learn the enum TYPE, enumerate its members, and pick the member whose underlying
    integer value == ``order``. A fake injects ``system._enum_types["TiltDecenterOrderType"]``
    (a FakeEnum) so the unit tests exercise the SAME member-resolution seam (and the fake's
    ``tool.Order`` setter REJECTS the enum CLASS, requiring the resolved member — the
    lesson). A resolution failure -> ``SurfaceWriteError`` (element_write).
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "TiltDecenterOrderType" in injected:
        from ..enums import _resolve_enum
        enum_type = injected["TiltDecenterOrderType"]
        # The member NAME per the live enum: Decenter_Tilt=0, Tilt_Decenter=1.
        name = "Decenter_Tilt" if order == 0 else "Tilt_Decenter"
        try:
            return _resolve_enum(enum_type, name)
        except Exception as exc:  # noqa: BLE001 — a fake-enum resolution miss -> element_write
            raise SurfaceWriteError(
                f"could not resolve TiltDecenterOrderType.{name} for order {order} "
                f"({exc!r}); refusing rather than driving the native tool at a wrong "
                "(default) order",
                field="tilt_decenter_order", intended=name, actual=None, surface=None,
            ) from exc
    # Live path: reflect the property's enum TYPE + pick the member by underlying value.
    try:  # pragma: no cover - live backend path
        import System  # type: ignore

        cur = tool.Order
        enum_type = cur.GetType()
        names = list(System.Enum.GetNames(enum_type))
        values = list(System.Enum.GetValues(enum_type))
        for nm, val in zip(names, values):
            if int(val) == int(order):
                return val
        raise SurfaceWriteError(
            f"no TiltDecenterOrderType member has underlying value {order}; refusing "
            "rather than driving the native tool at a wrong (default) order",
            field="tilt_decenter_order", intended=order, actual=None, surface=None,
        )
    except SurfaceWriteError:
        raise
    except Exception as exc:  # noqa: BLE001 — a reflection THROW -> element_write
        raise SurfaceWriteError(
            f"could not resolve the TiltDecenterOrderType member for order {order} "
            f"({exc!r}); a bare int silently no-ops the native tool — refusing rather "
            "than driving it at a wrong (default) order",
            field="tilt_decenter_order", intended=order, actual=None, surface=None,
        ) from exc


# --------------------------------------------------------------------------- #
# Post-table role readers.
# --------------------------------------------------------------------------- #
def _find_cb_surfaces(lde, n):
    """The set of surface numbers of every coordinate-break row in 0..n-1 (by role).

    Fails CLOSED on an UNREADABLE row: if
    ``is_coordinate_break`` RAISES on any row, the CB set is unverifiable — we raise
    ``_PlaceUnverified(element_frame_unrestored)`` rather than silently dropping a
    CB-candidate row (which would corrupt the set-diff and mis-locate the pair). A row
    that reads cleanly and is simply not a CB is excluded normally.
    """
    out = set()
    for i in range(n):
        try:
            row = lde.GetSurfaceAt(i)
            is_cb = _cb.is_coordinate_break(row)
        except Exception as exc:  # noqa: BLE001 — an unreadable row -> fail CLOSED
            raise _PlaceUnverified(
                f"could not classify surface {i} as a coordinate break ({exc!r}) while "
                "identifying the wrap's CB pair; an unverifiable CB-candidate row must NOT "
                "be silently treated as 'not a CB' — rolling back rather than risking a "
                "mis-located entry/return CB",
                family=_ELEMENT_FRAME_UNRESTORED,
                extra={"unreadable_surface": i},
            ) from exc
        if is_cb:
            out.add(i)
    return out


def _wrap_cb_pair(pre_cb_set, post_cb_set, first, insert_delta):
    """The SET-DIFF of the wrap's NEW CBs (post − pre), renumber-adjusted, as a set.

    The native wrap inserts the entry CB AT ``first`` and the spacer/return CB/[Dummy]
    after the span — ALL at index >= ``first``. So EVERY pre-existing surface at index
    >= ``first`` shifts by ``insert_delta`` (= ``n_post − n_pre``, the total inserted
    count); a pre-existing CB strictly UPSTREAM of ``first`` does not shift. We renumber
    the pre-set accordingly THEN diff, leaving exactly the two newly-authored CBs (the
    wrap's entry + return) — robust to a pre-existing fold CB UPSTREAM or DOWNSTREAM of the
    span, unlike a naive first/last-in-table read (which
    picks the pre-existing fold) or a flat +1 shift (which mis-renumbers a downstream fold,
    since a downstream CB shifts by the FULL insert count, not +1).
    """
    shifted_pre = {s + insert_delta if s >= first else s for s in pre_cb_set}
    return post_cb_set - shifted_pre


def _find_dummy_surface(lde, n_post, return_cb_surface, multi):
    """The trailing native Dummy surface number just past the return CB, or None — BY ROLE.

    The BUG-3 fix: gate the disclosure on the wrap's KNOWN multiplicity + the post-table
    ROLE of the candidate surface, NOT a ``"dummy"`` comment substring (a USER surface
    commented "dummy" must not false-positive). A MULTI-surface wrap (``last > first``)
    migrates the trailing airgap to a native Dummy at ``return_cb + 1``; a SINGLE-surface
    wrap (``last == first``) inserts NO Dummy (the surface after the return CB is then the
    USER's downstream surface, which we must NOT disclose).

    For a multi-surface wrap, ``return_cb + 1`` is the Dummy iff it is an in-range Standard
    (non-CB) surface short of the IMAGE (a downstream fold CB sitting at ``return_cb + 1``
    is NOT a Dummy). This is comment-free and matches both the relocated-trailing-airgap
    live insert and the fake's appended Dummy.
    """
    if not multi:
        return None
    cand = return_cb_surface + 1
    if cand > n_post - 2:  # must be a real interior carrier, never the IMAGE (n_post-1)
        return None
    try:
        row = lde.GetSurfaceAt(cand)
        if _cb.is_coordinate_break(row):
            return None  # a downstream fold CB sits here, not the native Dummy
    except Exception:  # noqa: BLE001 — an unreadable candidate -> no Dummy disclosed
        return None
    return cand


def _vertex_colocated(system, lde, entry_cb_surface, return_cb_surface):
    """The (colocated, axial_residual) co-location DISCLOSURE — return-CB vs entry-CB Z.

    Returns ``(bool, residual)`` where ``bool`` is ``True`` iff the return CB shares the
    entry CB's AXIAL (Z) position within a GENEROUS tolerance (~0.1 mm — the native always
    co-locates via the −Σ back-up spacer, leaving a ~0.009 mm residual; the old tight tol
    read False on the real residual) and ``residual`` is the axial Z residual NUMBER
    (disclosed alongside the bool).

    The native tool walks the return CB back onto the entry-CB origin via the −Σ back-up
    spacer; co-location is therefore an AXIAL property — the return CB's global Z equals
    the entry CB's. We compare ONLY Z (not the full vertex): GetGlobalMatrix anchors the
    ENTRY CB at the (0,0,0) origin, so the entry-CB frame IS the parent-axis reference
    (NOT surface 0 — on an infinite-conjugate system the OBJECT vertex is the
    object-distance sentinel, never the origin). A DECENTERED element legitimately differs
    from the entry CB in (x, y) by the decenter — that lateral offset is the disclosed
    ``downstream_lateral_shift``, NOT a co-location failure; only the axial Z must match.

    SENTINEL-GUARDED: a non-finite OR a sentinel-magnitude (``>= 1e9``) Z read -> the
    co-location is UNKNOWN: returns ``(None, None)`` (never leak the ~1.7e8 object-distance
    sentinel as a fabricated residual). NEVER a rollback gate (the re-probe finding) — a
    read fault / a mismatch is disclosed honestly, never raises.
    """
    try:
        _e_R, _ex, _ey, ez = _cb.read_global_matrix(system, lde, entry_cb_surface)
        _r_R, _rx, _ry, rz = _cb.read_global_matrix(system, lde, return_cb_surface)
    except Exception:  # noqa: BLE001 — an unreadable vertex -> unknown, never raise
        return None, None
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               and abs(v) < _POSITION_SENTINEL_MAGNITUDE for v in (ez, rz)):
        # A non-finite / sentinel-magnitude read -> unknown; never leak the sentinel.
        return None, None
    residual = abs(ez - rz)
    return (residual <= _COLOCATION_VERTEX_TOL), residual


def _verify_orientation_restored(system, lde, n, entry_cb_surface, return_cb_surface):
    """The DECISIVE orientation-restored falsifier (raises _PlaceUnverified on a miss).

    Two corroborating proofs (both fail CLOSED on an unreadable frame):

    (1) the RETURN CB's rotation contribution via ``_cb_cells.relative_rotation_residual``
        (upstream = the surface BEFORE the return CB) matches its authored ``-tilt``
        product within the gate. The return CB's authored tilt cells negate the entry
        tilts (the native generator's inverse), so the relative residual is ~0.
    (2) the downstream rotation block (the surface AFTER the return CB) equals the PARENT
        frame the element lives in — the surface JUST BEFORE the entry CB (``entry_cb −
        1``) — within the same gate. This is the OBJECT block when no upstream CB exists,
        but the FOLDED parent when a pre-existing upstream fold does (a correctly-restored
        element placed DOWNSTREAM of a fold is parallel to the FOLDED axis, NOT the OBJECT
        axis — comparing to surf 0 would FALSE-FAIL it). NEVER ``== identity`` (the
        GetGlobalMatrix anchoring gotcha).

    Returns the max of the two residuals (for disclosure). A mismatch / an unreadable
    frame -> ``_PlaceUnverified(family=element_frame_unrestored)`` (the close-out:
    this falsifier fails CLOSED, so an unreadable optical frame can never silently certify
    a bad placement).
    """
    # (1) the return-CB compose-oracle (the decisive surface).
    try:
        ret_row = lde.GetSurfaceAt(return_cb_surface)
        ret_tilt_x = _cb.read_cb_cell(system, ret_row, "tilt_x")
        ret_tilt_y = _cb.read_cb_cell(system, ret_row, "tilt_y")
        ret_tilt_z = _cb.read_cb_cell(system, ret_row, "tilt_z")
        ret_order = _cb.read_cb_cell(system, ret_row, "order")
        measured_R, _x, _y, _z = _cb.read_global_matrix(system, lde, return_cb_surface)
        upstream_R, _ux, _uy, _uz = _cb.read_global_matrix(
            system, lde, return_cb_surface - 1
        )
    except Exception as exc:  # noqa: BLE001 — an unreadable return frame -> fail closed
        raise _PlaceUnverified(
            f"could not read the return-CB global frame / cells to verify the restored "
            f"orientation ({exc!r}); the frame is unverifiable — rolling back rather than "
            "claiming an unverified placement",
            family=_ELEMENT_FRAME_UNRESTORED,
        ) from exc
    return_residual = _cb.relative_rotation_residual(
        upstream_R, measured_R, ret_tilt_x, ret_tilt_y, ret_tilt_z, int(ret_order)
    )
    if not (isinstance(return_residual, (int, float))
            and math.isfinite(return_residual)
            and return_residual <= _ORIENT_TOL_DEG):
        raise _PlaceUnverified(
            f"the return CB's rotation contribution does not match its authored -tilt "
            f"product (relative residual {return_residual} > {_ORIENT_TOL_DEG}); the "
            "co-located return did not invert the element tilt — rolling back",
            family=_ELEMENT_FRAME_UNRESTORED,
            extra={"return_cb_relative_residual": _safe(return_residual)},
        )

    # (2) the downstream rotation block == the PARENT frame (the surface just BEFORE the
    # entry CB). The downstream surface is the one AFTER the return CB; if the return CB is
    # the last interior surface (return_cb == N-2), the downstream is the IMAGE (N-1). The
    # parent frame is ``entry_cb − 1`` (== the OBJECT block when no upstream fold exists).
    downstream = return_cb_surface + 1
    if downstream > n - 1:
        downstream = n - 1
    parent = entry_cb_surface - 1
    if parent < 0:
        parent = 0
    try:
        ds_R, _dx, _dy, _dz = _cb.read_global_matrix(system, lde, downstream)
        parent_R, _ox, _oy, _oz = _cb.read_global_matrix(system, lde, parent)
    except Exception as exc:  # noqa: BLE001 — an unreadable downstream frame -> fail closed
        raise _PlaceUnverified(
            f"could not read the downstream / parent global frame to verify the restored "
            f"orientation ({exc!r}); the frame is unverifiable — rolling back",
            family=_ELEMENT_FRAME_UNRESTORED,
        ) from exc
    block_residual = _block_residual(ds_R, parent_R)
    if block_residual is None or not math.isfinite(block_residual) \
            or block_residual > _ORIENT_TOL_DEG:
        raise _PlaceUnverified(
            f"the downstream rotation block does NOT match the parent frame (surface "
            f"{parent}, just before the entry CB) (max element-wise residual "
            f"{block_residual} > {_ORIENT_TOL_DEG}); the downstream axis is not parallel "
            "to the parent axis — the orientation is NOT restored; rolling back rather "
            "than claiming an unverified placement",
            family=_ELEMENT_FRAME_UNRESTORED,
            extra={"downstream_vs_parent_residual": _safe(block_residual)},
        )

    return max(return_residual, block_residual)


def _block_residual(a, b):
    """Max-abs element-wise residual between two flat 9-lists (or None if either bad).

    Used to compare the downstream rotation block to the OBJECT block. A non-9 / non-
    finite element yields None (a degenerate frame is never a pass).
    """
    al = list(a) if a is not None else []
    bl = list(b) if b is not None else []
    if len(al) != 9 or len(bl) != 9:
        return None
    worst = 0.0
    for x, y in zip(al, bl):
        if (not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
                or not isinstance(y, (int, float)) or isinstance(y, bool)
                or not math.isfinite(y)):
            return None
        worst = max(worst, abs(x - y))
    return worst


# --------------------------------------------------------------------------- #
# Best-effort figure (NON-fatal).
# --------------------------------------------------------------------------- #
def _emit_figure(session, design_name):
    """Best-effort fold-coherent layout figure (rays ON). Returns ``(path, error)``.

    Delegates to ``render_layout`` with ``draw_rays=True`` (the verify_beam_path /
    beam_verify precedent — folded systems are drawn in the global frame, coherent with
    the rays). A figure failure is NON-fatal: returns ``(None, "<msg>")``; the verdict is
    unaffected. NEVER raises.
    """
    try:
        from . import layout_render as _layout_render
        render_params = {"draw_rays": True}
        if isinstance(design_name, str) and design_name:
            render_params["title"] = design_name
        result = _layout_render.render_layout(session, render_params)
        if isinstance(result, dict) and result.get("ok"):
            return result.get("path"), None
        err = (
            result.get("error")
            if isinstance(result, dict)
            else "render_layout returned a non-dict"
        )
        return None, f"figure not rendered: {err}"
    except BaseException as exc:  # noqa: BLE001 — a figure throw is NON-fatal
        return None, f"figure render failed: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# Rollback + reap (the fold_beam / apply_lens_spec precedent).
# --------------------------------------------------------------------------- #
def _place_pre_snapshot(system):
    """A cheap pre-mutation snapshot for the post-restore verify (never raises).

    Captures (a) ``NumberOfSurfaces`` and (b) a stable downstream global rotation block +
    vertex (the LAST surface's frame) — enough that a LoadFile that loaded-but-didn't-
    restore (the inserted CBs/dummy still committed -> a different surface count and/or a
    shifted downstream frame) is CAUGHT. Returns a dict, or None if the snapshot is
    unreadable (degrade — the post-restore verify then treats it conservatively).
    """
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable count -> no snapshot (degrade)
        return None
    snap = {"n": n, "frame_captured": False}
    try:
        measured_R, x, y, z = _cb.read_global_matrix(system, lde, n - 1)
        snap["image_R"] = list(measured_R)
        snap["image_vertex"] = [x, y, z]
        snap["frame_captured"] = True
    except Exception:  # noqa: BLE001 — a frame read fault -> count-only snapshot
        pass
    return snap


def _post_restore_place_problems(system, pre_snapshot):
    """Read the system AFTER LoadFile + compare to the pre-place snapshot. Never raises.

    A read-vs-read of the SAME system: the restore is faithful iff the post-LoadFile count
    + downstream frame match ``pre_snapshot``. Returns a list of mismatch strings (empty
    == faithful restore). An unreadable post-restore state is itself a mismatch (we cannot
    prove the restore landed, so we must NOT claim ``rolled_back:true``).
    """
    if not isinstance(pre_snapshot, dict):
        return ["the pre-place snapshot was unreadable; the rollback cannot be verified"]
    problems = []
    try:
        lde = system.LDE
        n_now = int(lde.NumberOfSurfaces)
    except Exception as exc:  # noqa: BLE001 — an unreadable count post-restore -> mismatch
        return [f"could not read the surface count after the rollback ({exc!r})"]
    if n_now != pre_snapshot.get("n"):
        problems.append(
            f"surface count after rollback is {n_now}, expected {pre_snapshot.get('n')} "
            "(the LoadFile did not restore the pre-place surface count — a half-wrap may "
            "still be committed)"
        )
    if not pre_snapshot.get("frame_captured"):
        problems.append(
            "the pre-place snapshot could not capture the downstream global frame "
            "(count-only); the rollback's geometry restoration cannot be verified — "
            "disclosing a PARTIAL state rather than claiming a frame-verified clean "
            "rollback"
        )
        return problems
    if "image_R" in pre_snapshot:
        try:
            measured_R, x, y, z = _cb.read_global_matrix(system, lde, n_now - 1)
        except Exception as exc:  # noqa: BLE001 — an unreadable frame post-restore -> mismatch
            problems.append(
                f"could not read the downstream global frame after the rollback "
                f"({exc!r}); the restore cannot be verified"
            )
            return problems
        if not _frames_match(list(measured_R), pre_snapshot["image_R"]):
            problems.append(
                "the downstream global rotation block after rollback does not match the "
                "pre-place frame (the LoadFile loaded but did not restore the geometry)"
            )
        if not _frames_match([x, y, z], pre_snapshot.get("image_vertex", [])):
            problems.append(
                "the downstream global vertex after rollback does not match the "
                "pre-place frame (the LoadFile loaded but did not restore the geometry)"
            )
    return problems


def _frames_match(a, b, tol=1e-9):
    """Element-wise compare two flat numeric lists within ``tol`` (post-restore verify)."""
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if (not isinstance(x, (int, float)) or isinstance(x, bool)
                or not isinstance(y, (int, float)) or isinstance(y, bool)
                or not math.isfinite(x) or not math.isfinite(y)
                or abs(x - y) > tol):
            return False
    return True


def _rollback_place(system, checkpoint_path, pre_snapshot, *, family, reason,
                    extra=None):
    """Restore from the checkpoint + POST-RESTORE read-back verify (never raises).

    A ``LoadFile`` THROW (the restore itself failed) -> ``partial_state:true`` + "the
    system is in an UNKNOWN state — reload your design .zmx". A LoadFile that returns
    cleanly but loaded-but-DIDN'T-restore is caught by the post-restore read-vs-read
    against ``pre_snapshot`` -> ``rolled_back:false, partial_state:true``. Only a VERIFIED-
    faithful restore -> ``rolled_back:true``. The envelope carries the requested-vs-
    achieved numbers + the ``first_miss`` when the falsifier supplied them.
    """
    fields = {
        "checkpoint": True,
        "rolled_back": False,
        "partial_state": False,
        "first_miss": None,
    }
    if extra:
        fields.update(extra)
    try:
        system.LoadFile(_fwd(checkpoint_path), False)
    except Exception as exc:  # noqa: BLE001 — a rollback LoadFile throw -> partial state
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "place_element", family,
            f"place_element failed ({reason}); the ROLLBACK restore itself threw "
            f"({exc!r}) — the system is in an UNKNOWN state. Reload your design .zmx to "
            "recover.",
            **fields,
        )

    restore_problems = _post_restore_place_problems(system, pre_snapshot)
    if restore_problems:
        fields.update({"rolled_back": False, "partial_state": True})
        return error_envelope(
            "place_element", family,
            f"place_element failed ({reason}); the checkpoint LoadFile returned cleanly "
            f"but the post-restore read-back does NOT match the pre-place snapshot "
            f"({restore_problems}) — the rollback did not faithfully restore. The system "
            "may be in a PARTIAL state; reload your design .zmx to recover.",
            **fields,
        )

    fields.update({"rolled_back": True, "partial_state": False})
    return error_envelope(
        "place_element", family,
        f"place_element failed ({reason}); the system was ROLLED BACK to its pre-place "
        "state via the temp checkpoint.",
        **fields,
    )


def _reap_place_checkpoint(checkpoint_path, glob, os, _unlink_quiet):
    """Reap the temp ``.zmx`` placeholder AND the engine's ``.ZDA`` companion (#59).

    The live engine's ``SaveAs`` writes its native ``.ZDA`` binary (same stem), so
    unlinking only the ``.zmx`` leaks the ``.ZDA`` on every call. Glob the unique mkstemp
    token stem + remove everything matching (scoped to this token). NEVER raises.
    """
    if not checkpoint_path:
        return
    _unlink_quiet(checkpoint_path)
    try:
        directory = os.path.dirname(checkpoint_path)
        base = os.path.basename(checkpoint_path)
        stem, _ext = os.path.splitext(base)
        for path in glob.glob(os.path.join(directory, stem + "*")):
            _unlink_quiet(path)
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


def _fwd(path):
    """Forward-slash a path for SaveAs/LoadFile (#73 — a backslash silently mis-writes)."""
    return path.replace("\\", "/") if isinstance(path, str) else path


def _safe(value):
    """JSON-safe float (NaN/inf -> string sentinel)."""
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
PLACE_ELEMENT_SPEC = ToolSpec(
    name="place_element",
    handler=place_element,
    required_params=("first", "last"),
    param_types={
        "first": "number",
        "last": "number",
        "tilt_x": "number",
        "tilt_y": "number",
        "tilt_z": "number",
        "decenter_x": "number",
        "decenter_y": "number",
        "order": "number",
        "draw": "boolean",
        "design_name": "string",
    },
    description=(
        "Tilt/decenter an existing element span (first..last) about its own frame and "
        "restore the downstream axis with a co-located return — the perturb-and-return "
        "composer. Authors an entry coordinate break, the wrapped element, and a "
        "co-located return CB atomically (wrapping the engine's native tilt/decenter "
        "generator), owning the surface renumber. Params: first, last, tilt_x/y/z, "
        "decenter_x/y, order, draw. Proves the downstream orientation is restored "
        "parallel to the parent AND the rays reach every optic; rolls back if not. "
        "Gotcha: a DECENTERED element's beam returns parallel but LATERALLY SHIFTED by "
        "the decenter (orientation restored, position not — disclosed as "
        "downstream_lateral_shift); the inverse is exact only for a co-located (gap=0) "
        "return, which this tool owns. See add_coordinate_break, add_return_cb, "
        "verify_beam_path, fold_beam."
    ),
)

TOOL_SPECS = (PLACE_ELEMENT_SPEC,)
