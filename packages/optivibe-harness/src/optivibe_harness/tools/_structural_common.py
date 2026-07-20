"""tools/_structural_common.py — shared STRUCTURAL helpers.

NOT dispatchable (no ``TOOL_SPECS``). The probe-grounded structural primitives the
``normalize_stop`` tool and the future element-splitting tool both
reuse live here in exactly one place:

- ``_classify_stop(lde)`` — the two-``Material``-read + ``IsStop`` detection rule
  (§2, probe (a)): a stop is free-standing iff its OWN material is air AND
  its predecessor's material is air (empty string ``""`` IS air). Returns
  ``(stop_idx, classification)`` where classification is ``"no_stop"`` /
  ``"free_airspace"`` / ``"on_glass_vertex"``.
- ``_select_host_gap(lde, stop_idx)`` — the on-glass-vertex host-air-gap selector
  (§3.2 R1/R2): image-side preferred, object-side fallback; returns the
  insert position + the gap-host surface + the finite gap thickness, or a
  fail-closed reason for the ``normalize_no_airspace`` envelope.
- ``_add_bound_operand(mfe, system, operand_token, surface, target)`` — the FIRST
  cell-aware Surf-celled operand author (§5.1): MNEA/MNCA/MXEA/MXCA are
  ``Surf1 -> Surf2`` RANGE operands (LIVE finding) — writes BOTH the Surf1
  cell ``GetCellAt(2).IntegerValue`` AND the Surf2 cell ``GetCellAt(3).IntegerValue``
  to the SAME single gap ``surface`` (a degenerate one-surface range Surf1==Surf2)
  so the operand actually evaluates the gap; the typed ``op.Surf1``/``op.Surf2``
  properties are SILENT no-ops (probe Q6), then read-back-proves ALL THREE cells
  (Surf1, Surf2, Target — P8/P9).

Import discipline (§7): this module is imported BY ``_optimize_common`` (one
direction). It NEVER imports back into ``optimize_run`` / ``optimize_merit``; the
live-enum resolver is imported LAZILY inside ``_add_bound_operand`` to avoid any
import cycle.

Live ZOS-API integration: exercised by the live tests; unit-tested here
against the FakeLDE/FakeMFE doubles (no backend).
"""
import math

from ..errors import SurfaceWriteError
from . import _lens_common as _lc

# Empty-string material IS air (probe (a)). A glass surface carries a non-empty
# catalog name. ``str(...)`` normalizes the .NET ``System.String`` proxy first.
_AIR_MATERIAL = ""

# The classification the read-failure path resolves to. NEVER fabricated
# as "not air" (that silently manufactures a glass vertex + would authorize a
# mutation of a valid system on a transient .NET hiccup). "indeterminate" is
# REFUSE-to-optimize and NEVER auto-mutates (see ``_classify_stop`` / the guard).
_INDETERMINATE = "indeterminate"


def _material_is_air(row):
    """True if ``row``'s Material reads as air (empty string ``""``, probe (a)).

    Invariant: a Material-read FAILURE must NEVER be silently treated as glass
    (returning ``False`` here would (a) manufacture a vertex-stop classification on
    a genuinely free stop and (b) — under ``auto_normalize`` — authorize a
    structural mutation of an ALREADY-VALID system from a transient API hiccup).
    So a read failure PROPAGATES as a ``SurfaceWriteError`` (the read-back-as-proof
    firewall shape): the caller (``_classify_stop``) resolves it to the explicit
    ``"indeterminate"`` classification, which the optimize guard treats as
    REFUSE-to-optimize and which NEVER drives a mutation. It is never swallowed.
    """
    try:
        return str(row.Material) == _AIR_MATERIAL
    except Exception as exc:  # noqa: BLE001 — propagate, never fabricate "not air"
        raise SurfaceWriteError(
            f"could not read surface Material ({exc!r}); stop classification is "
            "indeterminate — refusing rather than guessing",
            field="material",
            intended=None,
            actual=None,
            surface=None,
        )


def _classify_stop(lde):
    """Classify the sole stop as free-standing vs on a glass vertex (§2).

    The decision is two ``Material`` reads (the stop + its predecessor) plus
    ``IsStop`` (probe (a), load-bearing): a stop is FREE-STANDING iff its OWN
    material is air AND the preceding surface's material is air (air on BOTH
    sides). Empty string ``""`` is air. Otherwise the stop is ON / adjacent to a
    glass vertex.

    Scans the INTERIOR surfaces ``1..N-1`` for ``IsStop`` (OBJECT 0 never carries a
    stop; the single-stop convention keeps exactly one stop). Returns:

    - ``(None, "no_stop")``        — EVERY ``IsStop`` read SUCCEEDED and was False
      (a genuine afocal/stopless system — no read failure on the scan).
    - ``(idx, "free_airspace")``   — the stop is free-standing (air on both sides).
    - ``(idx, "on_glass_vertex")`` — the stop is on / adjacent to a glass vertex.
    - ``(idx, "indeterminate")``   — a Material read FAILED: the classification
      cannot be determined. NEVER fabricated as a vertex (which would authorize a
      spurious mutation); the guard treats it as REFUSE-to-optimize and
      ``normalize_stop`` fails-closed — neither auto-mutates on it.
    - ``(None, "indeterminate")``  — an ``IsStop`` read FAILED on the scan: a read
      failure must NOT silently downgrade a stopped system to
      ``no_stop`` (which is reserved for a scan where every read SUCCEEDED and was
      False). Resolved to REFUSE-to-optimize, consistent with the Material firewall.

    L26 firewall: EVERY LDE read on the classification path —
    ``NumberOfSurfaces`` and the two post-scan row fetches ``GetSurfaceAt(...)`` — is
    guarded the SAME way as the per-surface ``IsStop`` and ``Material`` reads. A raw
    .NET read failure on any of them resolves to the RETURNED ``"indeterminate"``
    classification (REFUSE, never auto-mutate). ``_classify_stop`` NEVER raises out
    of this firewall surface: a read failure is CAUGHT here and turned into the
    indeterminate tuple the guard / handler already treat as fail-closed — it NEVER
    escapes as an opaque dispatch ``internal`` family. No read on this path is
    unfirewalled.
    """
    try:
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — L26: a NumberOfSurfaces read failure is
        # indeterminate, NOT an internal crash. We cannot scan for the stop, so the
        # classification is indeterminate (REFUSE, never auto-mutate) — the same
        # firewall the IsStop/Material reads use, RETURNED (not raised) so the guard /
        # handler treat it as fail-closed rather than letting it escape as internal.
        return (None, _INDETERMINATE)
    stops = []
    isstop_read_failed = False
    for i in range(1, n):
        try:
            if bool(lde.GetSurfaceAt(i).IsStop):
                stops.append(i)
        except Exception:  # noqa: BLE001 — a read FAILURE is indeterminate, NOT no_stop
            # Fail-closed invariant: an IsStop read FAILURE must NOT silently
            # downgrade a stopped system to "no_stop" (which would let the optimize
            # guard pass an unread system, or normalize report normalize_no_stop on a
            # transient hiccup). Record the failure and resolve to "indeterminate"
            # (REFUSE, never auto-mutate) — distinct from "read succeeded and IsStop is
            # False everywhere", which is a genuine stopless/afocal system (no_stop).
            isstop_read_failed = True
    if not stops:
        # Distinguish a genuine stopless system (every IsStop read SUCCEEDED and was
        # False) from a transient read failure on the scan. Only the former is no_stop;
        # the latter is indeterminate (the same firewall the Material read uses).
        if isstop_read_failed:
            return (None, _INDETERMINATE)
        return (None, "no_stop")
    stop_idx = stops[0]  # the single-stop convention keeps exactly one stop

    # L26: the two post-scan row fetches are LDE reads on the
    # classification path too — guard them the SAME way as the Material reads below.
    # A raw GetSurfaceAt failure here would otherwise escape as dispatch "internal"
    # rather than the structured indeterminate/surface_write firewall.
    try:
        stop_row = lde.GetSurfaceAt(stop_idx)
        prev_row = lde.GetSurfaceAt(stop_idx - 1)
    except Exception:  # noqa: BLE001 — a row-fetch failure is indeterminate, never internal
        return (stop_idx, _INDETERMINATE)
    # A Material-read failure must NOT be silently treated as glass. Resolve it
    # to the explicit "indeterminate" classification (REFUSE, never auto-mutate)
    # instead of fabricating a vertex stop from a transient API hiccup.
    try:
        stop_mat_air = _material_is_air(stop_row)
        prev_mat_air = _material_is_air(prev_row)
    except SurfaceWriteError:
        return (stop_idx, _INDETERMINATE)
    free_standing = stop_mat_air and prev_mat_air  # probe (a)
    classification = "free_airspace" if free_standing else "on_glass_vertex"
    return (stop_idx, classification)


def _is_finite_thickness(value):
    """True if ``value`` is a finite (splittable) air-gap thickness.

    A non-finite gap (an ``inf`` OBJECT/back-focal gap) cannot be split in half —
    the host-gap selector fail-closes on it (§3.2 R2).
    """
    try:
        return isinstance(value, (int, float)) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _select_host_gap(lde, stop_idx):
    """Choose the host air gap for the on-glass-vertex insert+split (§3.2).

    Returns one of:

    - ``{"ok": True, "host_gap_surface": h, "insert_at": a, "thickness": t,
       "side": "image"|"object"}`` — a finite air gap to split.
    - ``{"ok": False, "reason": <str>}`` — fail-closed; the caller raises the
      ``normalize_no_airspace`` envelope with this reason.

    Rule (R1, image-side preferred — the canonical iris-after-element layout the
    probe built):

    - IMAGE-side candidate is air iff ``stop.Material == ""`` (the space LEAVING
      the stop). The host gap is the stop surface's OWN thickness; the dummy
      inserts at ``stop_idx + 1`` (between the stop and its successor).
    - else OBJECT-side candidate is air iff ``prev.Material == ""``. The host gap
      is the predecessor's thickness; the dummy inserts at ``stop_idx`` (between
      the predecessor and the stop).
    - NEITHER air -> ``normalize_no_airspace`` (cemented interior vertex -> the
      element-splitting tool).

    R2: the chosen host gap thickness ``t`` must be FINITE (an ``inf`` host cannot
    be halved). The required ``insert_at`` is pre-checked against the engine crash bound
    ``1..N-1`` (``_require_insert_at`` would raise on the engine; we fail-closed
    BEFORE ever reaching it).
    """
    # Firewall: the count read + the two row fetches are raw LDE
    # reads on the COMMITTED vertex-refactor decision path. The sibling
    # ``_classify_stop`` already guards its ``NumberOfSurfaces`` + ``GetSurfaceAt``
    # reads (the L26 fix); ``_select_host_gap`` is the one place that firewall was not
    # yet mirrored. A raw THROW here would otherwise escape ``normalize_stop`` as
    # dispatch ``internal``. Resolve a read THROW to the SAME fail-closed
    # ``{"ok": False, "reason": ...}`` the caller already turns into the
    # ``normalize_no_airspace`` envelope (consistent with the existing unreadable-
    # thickness fail-close below) — never an ``internal`` escape.
    try:
        n = int(lde.NumberOfSurfaces)
        stop_row = lde.GetSurfaceAt(stop_idx)
        prev_row = lde.GetSurfaceAt(stop_idx - 1)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> fail-closed, never internal
        return {
            "ok": False,
            "reason": (
                f"could not read the LDE while selecting the host air gap for the "
                f"stop on surface {stop_idx} ({exc!r}); refusing rather than guessing "
                "an air gap to split"
            ),
        }

    # ``_material_is_air`` RAISES ``SurfaceWriteError`` on a Material-read
    # THROW. Here on the HOST-SELECTOR path that would bubble out as a structured
    # ``surface_write`` envelope — but ``_select_host_gap``'s DOCUMENTED contract is to
    # fail-closed as ``{"ok": False, "reason": ...}`` (which the caller turns into the
    # ``normalize_no_airspace`` envelope), consistent with the unreadable-count and
    # unreadable-thickness fail-closes above/below. Catch the read THROW and return the
    # fail-closed dict so the selector's shape contract holds.
    try:
        image_side_air = _material_is_air(stop_row)
        object_side_air = _material_is_air(prev_row)
    except SurfaceWriteError as exc:
        return {
            "ok": False,
            "reason": (
                f"could not read the adjacent surface Material while selecting the host "
                f"air gap for the stop on surface {stop_idx} ({exc!r}); refusing rather "
                "than guessing an air gap to split"
            ),
        }

    if image_side_air:
        host_gap_surface = stop_idx          # the stop's own thickness leaves the stop
        insert_at = stop_idx + 1             # insert AFTER the stop (before successor)
        side = "image"
    elif object_side_air:
        host_gap_surface = stop_idx - 1      # the predecessor's thickness leads in
        insert_at = stop_idx                 # insert BEFORE the stop
        side = "object"
    else:
        return {
            "ok": False,
            "reason": (
                "stop is on a cemented interior vertex (glass on both sides); "
                "no reachable air gap to split — split the element first"
            ),
        }

    # R2: the OBJECT thickness (surface 0) is typically inf and is never a host —
    # an object-side host that resolves to surface 0 is refused outright.
    if host_gap_surface == 0:
        return {
            "ok": False,
            # D4 (Bug 4): a FRONT-VERTEX stop (the stop's only adjacent air is the
            # OBJECT/inf gap) is the one refusal the dedicated front-dummy reseat
            # handles. Flag it ADDITIVELY (the selector stays pure for the shelved
            # element-splitting tool — ``ok:false`` + the existing ``reason`` are unchanged);
            # ``_normalize_vertex`` reads ``front_vertex`` to route to the dedicated
            # ``_normalize_front_vertex`` instead of the no-airspace fail-close.
            "front_vertex": True,
            "reason": (
                "the only adjacent air is the OBJECT thickness; cannot split an "
                "object/infinite air gap"
            ),
        }

    # R2: the host gap thickness must be finite (an inf back-focal/object gap
    # cannot be halved).
    try:
        thickness = float(lde.GetSurfaceAt(host_gap_surface).Thickness)
    except Exception:  # noqa: BLE001 — an unreadable thickness is unsplittable
        return {
            "ok": False,
            "reason": f"host gap thickness on surface {host_gap_surface} is unreadable",
        }
    if not _is_finite_thickness(thickness):
        return {
            "ok": False,
            "reason": (
                f"host air gap on surface {host_gap_surface} is infinite "
                "(thickness=inf); cannot split an infinite air gap"
            ),
        }

    # Pre-check the engine crash bound 1..N-1 (never reach the crashing engine call).
    if not (1 <= insert_at <= n - 1):
        return {
            "ok": False,
            "reason": (
                f"the required insert position {insert_at} falls outside the safe "
                f"range 1..{n - 1} (would crash the engine)"
            ),
        }

    return {
        "ok": True,
        "host_gap_surface": host_gap_surface,
        "insert_at": insert_at,
        "thickness": thickness,
        "side": side,
    }


def _add_bound_operand(mfe, system, operand_token, surface, target):
    """Add ONE cell-aware Surf-celled boundary operand, read-back-proven (§5.1).

    The FIRST Surf-celled operand author in the codebase (``add_operand`` writes
    ``op.Target`` DIRECT with no ``GetCellAt`` — it cannot author a Surf cell). The
    Surf cells are written via ``op.GetCellAt(2).IntegerValue`` (Surf1) and
    ``op.GetCellAt(3).IntegerValue`` (Surf2) (probe Q6: the typed ``op.Surf1`` /
    ``op.Surf2`` properties are SILENT no-ops — they set WITHOUT error but the value
    stays 0).

    LIVE finding: MNEA/MNCA (and the
    MXEA/MXCA siblings) are ``Surf1 -> Surf2`` RANGE operands, NOT single-surface.
    Writing ONLY ``GetCellAt(2)`` (Surf1) and leaving ``GetCellAt(3)`` (Surf2) at its
    default 0 makes the operand evaluate an EMPTY range: it reports ``Value == Target``
    with contribution 0 and does NOT constrain the gap (live: the freed gap collapsed
    to 0 DESPITE the bound — the bound was INERT). For a single-gap air-thickness
    bound we author a DEGENERATE one-surface range by writing BOTH cells to the SAME
    gap ``surface`` (``Surf1 == Surf2 == surface``), so the range spans exactly that
    one gap and the operand actually bites. All THREE writes (Surf1, Surf2, Target)
    are read back + verified through the ``_lens_common`` firewall (P8/P9): a write
    that did not stick raises ``SurfaceWriteError``.

    The live-enum resolver is imported LAZILY here (§7 import-cycle note) so
    this module never imports back into ``optimize_merit`` / ``optimize_run``.

    Returns the operand number (``int(op.OperandNumber)``).
    """
    # Lazy import to keep _structural_common free of any cycle back into the
    # optimize tools (§7).
    from . import _optimize_common as _oc
    from ..enums import _resolve_enum

    enum_type = _oc._merit_operand_enum(system)        # reuse the live-enum resolver
    member = _resolve_enum(enum_type, operand_token)    # validate vs MeritOperandType

    # EVERY engine interaction in this authoring
    # SEQUENCE is a WRITE-time mutator — ``mfe.AddOperand()`` (a raw MFE call),
    # ``op.ChangeType(member)`` (the THROW path — DISTINCT from the False RETURN handled
    # below), the two ``GetCellAt(...).IntegerValue = surface`` cell writes, and the
    # ``op.Target`` / ``op.Weight`` setters. A .NET THROW on ANY of these has no local
    # catch and escapes ``normalize_stop`` as dispatch ``internal``. Wrap the WHOLE
    # mutator sequence in a guarded block that re-raises a STRUCTURED ``SurfaceWriteError``
    # on any throw. The ``ChangeType`` FALSE return (the engine rejecting the type
    # WITHOUT throwing) stays a DISTINCT structured raise inside the block; and the
    # read-back ``_verify_or_raise`` value-MISMATCH checks below remain DISTINCT too
    # (a write-THROW, a ChangeType-False, and a value-MISMATCH are ALL surface_write but
    # via different mechanisms — every one now covered).
    try:
        op = mfe.AddOperand()
        if not bool(op.ChangeType(member)):
            raise _lc.SurfaceWriteError(
                f"engine rejected operand type {operand_token!r} for the airgap bound",
                field="operand_type",
                intended=operand_token,
                actual=None,
                surface=surface,
            )
        # Surf cells via GetCellAt — NOT op.Surf1/op.Surf2 (silent no-ops, probe Q6).
        # LIVE finding: MNEA/MNCA are Surf1->Surf2 RANGE operands; leaving
        # Surf2 (col 3) at its default 0 makes an EMPTY range that does NOT constrain the
        # gap (the bound was INERT on the live engine). Author a DEGENERATE one-surface
        # range Surf1==Surf2==surface so the operand evaluates exactly this one gap.
        op.GetCellAt(2).IntegerValue = surface      # Surf1 (start of the range)
        op.GetCellAt(3).IntegerValue = surface      # Surf2 (end == start: a single gap)
        op.Target = target          # positive Min (must be >0 or the bound is inert)
        op.Weight = 1.0
    except _lc.SurfaceWriteError:
        # The ChangeType-False structured raise above is already the right envelope —
        # re-raise it verbatim (do NOT re-wrap it as a generic write throw).
        raise
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_write, never internal
        raise _lc.SurfaceWriteError(
            f"could not author the {operand_token} airgap bound on surface {surface} "
            f"({exc!r}); a write in the operand-authoring sequence (AddOperand / "
            "ChangeType / Surf cell / Target / Weight) was rejected by the engine — "
            "refusing rather than shipping a possibly-inert bound",
            field="bound_author_write",
            intended=target,
            actual=None,
            surface=surface,
        ) from exc

    # read-back-as-proof (P8/P9): BOTH Surf cells stuck + the Target stuck. A silent
    # no-op on EITHER cell (probe Q6) — including a Surf2 left at 0 that would re-open
    # the empty-range INERT bug — raises SurfaceWriteError rather than shipping an
    # inert bound.
    #
    # Firewall: the READ-BACK reads themselves
    # (``GetCellAt(2)/(3).IntegerValue``, ``op.Target``, ``op.OperandNumber``) are raw
    # .NET reads on the WRITE path. ``_verify_or_raise`` only converts a value MISMATCH
    # to ``SurfaceWriteError``; a read THROW would NOT be a ``SurfaceWriteError`` and,
    # with no local catch, would escape dispatch as ``internal``. Mirror the
    # state-verify firewall: read each value INTO a local inside a try/except that
    # re-raises a STRUCTURED ``SurfaceWriteError`` on a throw, THEN feed the local to
    # ``_verify_or_raise`` (which still flags a genuine mismatch unchanged).
    try:
        surf1_actual = int(op.GetCellAt(2).IntegerValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise _lc.SurfaceWriteError(
            f"could not read back the Surf1 cell of the {operand_token} bound on "
            f"surface {surface} ({exc!r}); the bound write is unverifiable — refusing "
            "rather than shipping a possibly-inert bound",
            field="bound_surf1_cell",
            intended=surface,
            actual=None,
            surface=surface,
        ) from exc
    _lc._verify_or_raise("bound_surf1_cell", surface, surf1_actual, surface=surface)

    try:
        surf2_actual = int(op.GetCellAt(3).IntegerValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise _lc.SurfaceWriteError(
            f"could not read back the Surf2 cell of the {operand_token} bound on "
            f"surface {surface} ({exc!r}); the bound write is unverifiable — refusing "
            "rather than shipping a possibly-inert bound",
            field="bound_surf2_cell",
            intended=surface,
            actual=None,
            surface=surface,
        ) from exc
    _lc._verify_or_raise("bound_surf2_cell", surface, surf2_actual, surface=surface)

    try:
        target_actual = float(op.Target)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise _lc.SurfaceWriteError(
            f"could not read back the Target of the {operand_token} bound on surface "
            f"{surface} ({exc!r}); the bound write is unverifiable — refusing rather "
            "than shipping a possibly-inert bound",
            field="bound_target",
            intended=target,
            actual=None,
            surface=surface,
        ) from exc
    _lc._verify_or_raise("bound_target", target, target_actual, surface=surface)

    try:
        return int(op.OperandNumber)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise _lc.SurfaceWriteError(
            f"could not read back the OperandNumber of the {operand_token} bound on "
            f"surface {surface} ({exc!r}); the bound write is unverifiable",
            field="bound_operand_number",
            intended=None,
            actual=None,
            surface=surface,
        ) from exc


__all__ = [
    "_classify_stop",
    "_select_host_gap",
    "_add_bound_operand",
    "_INDETERMINATE",
]
