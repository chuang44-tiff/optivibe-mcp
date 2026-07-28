"""tools/_grin_index_common.py — the GRIN index-range readout + floor-acceptance substrate.

NOT dispatchable (no ``TOOL_SPECS``). The GRIN OPTIMIZATION layer (``build_merit``'s
index floor + ``optimize``'s post-run audit) OWNS this module; the GRIN analysis
grader (``analyze_grin_profile``) CONSUMES it. The ONE place the GRIN index
operands (``I#VA`` per-point physical index, ``DLTN`` axial Δn, ``I#GT``/``I#LT`` floor
targets) are read + the ONE place the floor-acceptance predicate + the CELL->index
conversion live, so the prevent side (``build_merit`` floor) and the detect side
(``optimize`` audit) read the SAME constants + the SAME acceptance set, so the two
sides cannot drift apart.

Imports: ``_grin_cells`` (GRIN identity — exact-full-token), ``_merit_cells``
(``read_param_map`` — the box reader's row-param reads), ``_measurement_common``
(``read_operand_slots`` — the named-slot 9-arg firewall + its suspicious-sentinel), and
``math``. Imports NOTHING from ``optimize_merit`` / ``optimize_run`` (no cycle).

Probe-grounded rules this module encodes (all [Discovered-live-probed]):

- **The index OPERANDS report the TRUE PHYSICAL INDEX for EVERY GRIN type.**
  Falsified against an INDEPENDENT physical oracle (a plano-convex flat-back element
  has ``EFFL == R/(n-1)`` EXACTLY): on a ``Gradient2`` with ``n0`` cell ``2.25`` the engine
  returns ``EFFL == 100.0`` -> physical ``n == 1.5``, and ``I1VA == INDX == 1.5``; on a
  ``Gradient3`` with ``n0`` cell ``1.5``, ``EFFL == 100.0`` -> ``n == 1.5`` and
  ``I1VA == 1.5``. So there is **NO per-type index REPORT space** and this module performs
  **NO conversion** on a reading — ``index_vector`` / ``min_index`` / ``dn`` are physical
  index, verbatim, for both types. (The superseded "index_report_space" keying
  squared a Gradient2 reading back into the CELL value and labelled it "index" — the
  silent-wrong closed.)
- **The per-type difference is in the CELLS, not the readings:** the
  ``Gradient2`` Par polynomial is ``n² = n0 + Nr2·r² + …`` (so the physical base index is
  ``sqrt(n0_cell)``), the ``Gradient3`` polynomial is ``n = n0 + Nr2·r² + … + Nz1·z + …``.
  That convention lives on ``info.cell_index_space`` (``"index_squared"`` | ``"index"``)
  and is consumed by exactly ONE production path — the ``build_merit`` index box, which
  must convert the n0 CELL to a physical index before centring a physical-index box
  (``cell_value_to_index``, the ONE ``sqrt(`` locus in the GRIN index stack). It is NEVER
  applied to a reading.
- ``GRMN`` / ``I#GT`` / ``I#LT`` read ``0.0`` (the satisfied-inequality display, like
  ``MNCA``) — NEVER read as a value (both types).
- **``DLTN`` is AXIAL-ONLY (both types):** it reads ``0.0`` on a radial Gradient2
  (a REAL 0, not a fault) and the signed axial index swing on a Gradient3. So the radial
  Δn readout is the ``I#VA`` 6-point spread; the axial Δn readout is ``abs(DLTN)``.
- Index-space ``sqrt(`` / ``**2`` / ``v*v`` appears ONLY inside ``cell_value_to_index``.
  No other module (``optimize_merit`` / ``optimize_run`` / the analysis envelope)
  converts: each leaves a value in the space it read it from.

Live ZOS-API integration: exercised by a live integration test; unit-tested against the
fixture-seeded computing fakes, which MUST NOT import this module — the independence
guard — so the fake's point/order map is an INDEPENDENT copy the live gate reddens on
divergence.
"""
import math

from . import _grin_cells as _grin
from . import _merit_cells as _mc
from ._measurement_common import read_operand_slots, suspicious_sentinel


# =========================================================================== #
# §2.1 Frozen constants — the ONE source of GRIN operand tokens AND the floor
# acceptance set (both the writer's read-back and the silencing coverage check read
# THESE constants — one constant, read by both sides).
# =========================================================================== #
_GRIN_FLOOR_TOKENS = ("I1GT", "I2GT", "I3GT", "I4GT", "I5GT", "I6GT")     # min floor (>= target)
_GRIN_CEILING_TOKENS = ("I1LT", "I2LT", "I3LT", "I4LT", "I5LT", "I6LT")   # max ceiling (<= target)
_GRIN_VALUE_TOKENS = ("I1VA", "I2VA", "I3VA", "I4VA", "I5VA", "I6VA")     # per-point physical index
_GRIN_DLTN = "DLTN"                                                        # axial Δn
_ALL_FLOOR_TOKENS = frozenset(_GRIN_FLOOR_TOKENS + _GRIN_CEILING_TOKENS)

# FAIL-CLOSED value-operand ALLOW-LIST (a deny-list would fail OPEN on an operand nobody
# classified). GRMN / I#GT / I#LT / LPTD are constraint/display operands (0.0
# satisfied-display) — NEVER read as an index value. The GRIN profile grader reads
# ``min_index`` through THIS allow-list.
_INDEX_VALUE_OPERANDS = frozenset({"INDX", "GRMX", *_GRIN_VALUE_TOKENS})

_GRIN_NONPHYSICAL_FLOOR = 1.0     # n < 1 nonphysical for a passive medium (hardcoded; not a param)
_GRIN_INDEX_WEIGHT = 1.0e8        # The writer AND the silencing coverage check read the
                                  # SAME constant. One-sided restoring force, live-proven.
                                  # Do NOT raise without a new captured probe
                                  # (conditioning).
_GRIN_BOX_AUDIT_TOL = 5e-3        # PHYSICAL-INDEX tolerance for the box audit. Absorbs the
                                  # DLS penalty-equilibrium excursion (a run parked 3e-4 OUTSIDE
                                  # the bound: 1.1177 vs target 1.1180 — both physical-index
                                  # readings) while catching the runaway (7.5e-2 outside) with a
                                  # >25× gap. Recalibratable constant. NOTE: it was calibrated on
                                  # I#VA readings, which are physical index — the value carries
                                  # over unchanged from the superseded "√-space" label.
_GRIN_SAMPLED_COVERAGE_NOTE = (   # The honesty sentence EVERY warning/disclosure carries
    "6-canonical-point SAMPLED check: n>=1 and index range are verified AT the sampled "
    "points only; an interior extremum between samples is UNAUDITED "
    "(this check is sampled, not analytic).")

# Token -> canonical point index (1..6). The point is baked into the TOKEN,
# NOT a slot — I2GT is point 2, I5VA is point 5.
_POINT_OF = {tok: i + 1 for i, tok in enumerate(_GRIN_VALUE_TOKENS)}
_POINT_OF.update({tok: i + 1 for i, tok in enumerate(_GRIN_FLOOR_TOKENS)})
_POINT_OF.update({tok: i + 1 for i, tok in enumerate(_GRIN_CEILING_TOKENS)})


# =========================================================================== #
# §2.2 The single index ``sqrt(`` locus (AST-pinned to exactly one site).
#
# There is NO conversion on a READING (the operands report the physical index for
# every type). The one conversion that survives is CELL -> physical index, needed only by
# the build_merit floor, which reads the ``n0`` Par cell (n² on a Gradient2). This is the
# ONLY place index-space ``sqrt(`` / squaring appears (an AST test reddens a stray square
# in optimize_merit / optimize_run / the analysis envelope).
# =========================================================================== #
def _valid_cell_index_space(space):
    """True iff ``space`` is a known GRIN CELL index space (fail-closed on anything else).

    ``"index_squared"`` (Gradient2 — its Par polynomial is n²) | ``"index"`` (Gradient3)."""
    return space in ("index_squared", "index")


def cell_value_to_index(cell_value, space):
    """A GRIN Par-CELL index value -> the PHYSICAL index. The ONE ``sqrt(`` locus.

    ``"index_squared"`` -> ``sqrt(cell_value)`` (Gradient2: the cell holds n²);
    ``"index"``         -> ``float(cell_value)`` (Gradient3: the cell holds n).

    Fail-CLOSED: an UNKNOWN space RAISES ``ValueError`` (never a silent identity-on-unknown),
    as does a negative ``"index_squared"`` cell (no physical index exists for it). Callers
    gate on ``_valid_cell_index_space`` first and treat a raise as "n0 unreadable".

    NEVER applied to an operand READING — the operands already report the physical index."""
    if not _valid_cell_index_space(space):
        raise ValueError(f"unknown GRIN cell index space: {space!r}")
    v = float(cell_value)
    if space == "index_squared":
        return math.sqrt(v)          # the ONLY index sqrt( in the GRIN stack
    return v


# =========================================================================== #
# §2.3 Reader API (discovery-fault channel / suspicious contract).
# =========================================================================== #
def _suspicious_reading(v, *, allow_negative=False):
    """The ONE suspicious-sentinel predicate — shared by ``read_index_vector`` AND
    ``read_dltn``.

    ``True`` when the reading is non-finite, a bool, a non-number, or in a known Zemax
    sentinel band (``abs >= 1e10`` — reuses ``_measurement_common.suspicious_sentinel``),
    OR (unless ``allow_negative``) negative.

    Resolved spec tension (documented): §2.3's predicate text lists ``negative -> True``,
    but ``read_dltn`` returns a SIGNED axial Δn (``Nz1·t`` may be negative for a
    down-gradient axial GRIN). A shared predicate that faulted every negative would make a
    valid negative axial swing invisible. ``allow_negative`` keeps it ONE shared function:
    ``read_index_vector`` (a physical-index reading is non-negative by construction) uses the
    default (negative -> suspicious); ``read_dltn`` passes ``allow_negative=True`` (a
    negative axial swing is real). NaN/inf/bool/sentinel fault in BOTH modes.
    """
    if suspicious_sentinel(v):
        return True
    # suspicious_sentinel already covered bool / non-number / non-finite / sentinel; only
    # the sign check remains (v is a finite real number here).
    if not allow_negative and v < 0:
        return True
    return False


def grin_surfaces(system):
    """(entries, faults) — the DISCOVERY-FAULT CHANNEL. NEVER raises.

    ``entries`` = ``[(surf, info, is_axial)]`` for every AUTHORABLE GRIN surface (interior
    1..N-2). Identity via the GRIN FAMILY-RECOGNITION resolver
    (``_grin_cells.grin_family_type_of_name`` — exact-full-token, never a substring):
    a member with a known cell map (``Gradient2`` / ``Gradient3`` in ``GRIN_TYPE_INFO``) ->
    an entry; ``is_axial`` = the type carries axial ``Nz`` cells (Gradient3 -> True).

    ``faults`` = ``[{"surface": int | None, "reason": str}]``:
      - a row recognized as a GRIN family member we CANNOT author/read (e.g. Gradient4 —
        unknown cell map) -> ``{"surface": s, "reason": "grin_unreadable"}`` (fail-closed:
        never audited with a wrong map, never silently dropped);
      - a row whose Type read throws BEFORE classification -> ``{"surface": s, "reason":
        "unclassified_row"}`` (fail-closed: an unreadable row MIGHT be GRIN);
      - a total walk throw -> ``([], [{"surface": None, "reason": "enumeration_failed"}])``.

    A GRIN-free healthy system is ``([], [])`` — DISTINGUISHABLE from a total fault:
    a fault is an ENTRY in ``faults``, NEVER a silent skip.
    """
    try:
        lde = system.LDE
        n = int(lde.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — a total walk throw -> the surface-None fault
        return [], [{"surface": None, "reason": "enumeration_failed"}]
    entries = []
    faults = []
    for i in range(1, n - 1):  # interior 1..N-2 (OBJECT + IMAGE excluded)
        try:
            row = lde.GetSurfaceAt(i)
            type_name = str(row.Type)
        except Exception:  # noqa: BLE001 — a Type read throw BEFORE classification (fail-closed)
            faults.append({"surface": i, "reason": "unclassified_row"})
            continue
        try:
            fam = _grin.grin_family_type_of_name(type_name)
        except Exception:  # noqa: BLE001 — a resolver throw -> fail-closed unclassified
            faults.append({"surface": i, "reason": "unclassified_row"})
            continue
        if fam is None:
            continue  # not a GRIN family member — the silent skip is CORRECT
        info = _grin.GRIN_TYPE_INFO.get(fam)
        if info is None:
            # recognized GRIN family, unknown cell map (Gradient4/5/...) -> cannot audit it.
            faults.append({"surface": i, "reason": "grin_unreadable"})
            continue
        is_axial = bool(getattr(info, "axial_tokens", ()))
        entries.append((i, info, is_axial))
    return entries, faults


def read_index_vector(system, surf, wave=1):
    """(vector, fault): the 6-point PHYSICAL index vector via I1VA..I6VA.

    Returns the operand reads VERBATIM — they ARE the physical index, for EVERY GRIN type
    (live-falsified against the ``EFFL == R/(n-1)`` oracle; see the module docstring). No
    conversion is applied here or by any consumer.

    Each point is read through ``read_operand_slots(system, token, {2: ("Surf", surf),
    3: ("Wave", wave)})`` (slot map). ALL-OR-NONE fail-closed: a ``_suspicious_reading``
    on ANY point, or a throw -> ``(None, True)`` (NEVER a partial vector, NEVER a fabricated
    0). NEVER raises.
    """
    try:
        vector = []
        for token in _GRIN_VALUE_TOKENS:
            raw, susp = read_operand_slots(
                system, token, {2: ("Surf", int(surf)), 3: ("Wave", int(wave))})
            if susp or _suspicious_reading(raw):
                return None, True
            vector.append(float(raw))
        return vector, False
    except Exception:  # noqa: BLE001 — any read throw -> fail-closed fault
        return None, True


def read_dltn(system, surf, wave=1):
    """(signed raw axial Δn via DLTN, fault). Raw index units; ``0.0`` on a radial
    Gradient2 is a REAL 0 (not a fault).

    CONTRACT: a ``_suspicious_reading`` value (NaN/inf/bool/sentinel) or a throw ->
    ``(None, True)`` — a faulted DLTN NEVER flows into the ``max()`` (it routes to the
    unread disclosure on an axial-capable surface, §4.3). ``allow_negative=True`` — a
    negative axial swing is real. NEVER raises.
    """
    try:
        raw, susp = read_operand_slots(
            system, _GRIN_DLTN, {2: ("Surf", int(surf)), 3: ("Wave", int(wave))})
        if susp or _suspicious_reading(raw, allow_negative=True):
            return None, True
        return float(raw), False
    except Exception:  # noqa: BLE001 — any read throw -> fail-closed fault
        return None, True


def index_summary(system, surf, info, is_axial, wave=1):
    """The CONSUMER-facing per-surface summary (the ONE shape the warnings + the
    Grader share). NEVER raises. See §2.3 for the field contract."""
    summary = {
        "surface": surf,
        "grin_type": getattr(info, "type_token", None) if info is not None else None,
        "wave": wave,
        "index_vector": None,           # the PHYSICAL index per point (the I#VA reads verbatim)
        "dn_sampled": None,
        "dn_axial": None,
        "dn": None,
        "dn_source": "unreadable",
        "min_index": None,
        "is_axial": bool(is_axial),
        "vector_fault": False,
        "dltn_fault": False,
        "fault": False,
    }
    index_vector, vector_fault = read_index_vector(system, surf, wave)
    dltn, dltn_fault = read_dltn(system, surf, wave)
    summary["vector_fault"] = bool(vector_fault)
    summary["dltn_fault"] = bool(dltn_fault)
    if vector_fault or index_vector is None:
        # the vector is the load-bearing read — a vector fault is THE fault (no fabricated 0).
        summary["fault"] = True
        return summary

    # The reads ARE the physical index for every type — NO conversion, no type branch.
    summary["index_vector"] = index_vector
    dn_sampled = max(index_vector) - min(index_vector)      # the JOINT per-point field spread
    summary["dn_sampled"] = dn_sampled
    summary["min_index"] = min(index_vector)

    dn_axial = None
    if dltn is not None and not dltn_fault:
        dn_axial = abs(dltn)
        summary["dn_axial"] = dn_axial                      # 0.0 on a radial Gradient2 is REAL

    # Selection rule (NO family branch on the DATA — the branch is on is_axial identity).
    if not is_axial:
        # On a radial surface DLTN is structurally 0 and non-load-bearing; the Δn
        # readout IS the I#VA spread (a DLTN-alone reader under-reports -> unit-tested).
        summary["dn"] = dn_sampled
        summary["dn_source"] = "I#VA-spread"
    else:
        if dn_axial is not None:
            summary["dn"] = max(dn_sampled, dn_axial)       # big Nr2 dominates (unit-tested)
            summary["dn_source"] = "max(I#VA-spread,DLTN)"
        else:
            # axial surface but the DLTN half is unreadable — report the sampled half
            # and FLAG the unread axial half (the audit fires grin_index_unread_warning).
            summary["dn"] = dn_sampled
            summary["dn_source"] = "I#VA-spread (axial DLTN unread)"
    return summary


# =========================================================================== #
# §2.4 The shared floor-acceptance predicate + box reader. ONE acceptance
# set, defined HERE, consumed by (a) the writer's read-back (build_merit §3.3), (b) the
# silencing coverage check (build_merit §3.5), and (c) the box audit's row eligibility
# (optimize §4.3).
# =========================================================================== #
def _row_floor_acceptance(row_view, surface, *, require_weight, expected_token=None):
    """A PARSED MFE row counts toward a surface's floor iff the acceptance set holds.

    ``row_view`` = ``{"token", "surf", "wave", "target", "weight"}`` (already parsed +
    guarded; a per-row read fault produces a view with ``None`` fields -> not accepted).
    Acceptance: TypeName in the 12 tokens; ``Surf == surface``; ``Wave == 1``;
    ``Target`` finite; for a GT token ``Target >= 1.0`` (a GT below the nonphysical floor
    is no floor); and, when ``require_weight``, ``Weight >= _GRIN_INDEX_WEIGHT`` (a
    weight-0/lightweight row is provably inert against the calibrated competitor and must
    NOT self-silence). NEVER raises (any read fault -> False; doubt = not accepted).

    ``expected_token`` (the writer's read-back proves IDENTITY, not just MEMBERSHIP):
    when supplied, the row's read-back TypeName must equal EXACTLY the intended token — so a
    silent ``ChangeType(I6LT)`` that reads back ``I5LT`` (in-set but the WRONG member) is
    REJECTED (the writer-vs-coverage divergence the read-back proof exists to catch).
    When ``None`` (the coverage / box-audit callers) the MEMBERSHIP behavior is preserved —
    those legitimately accept ANY of the 12 floor tokens."""
    try:
        token = row_view.get("token")
        if token not in _ALL_FLOOR_TOKENS:
            return False
        if expected_token is not None and token != expected_token:
            return False  # membership held but IDENTITY failed (a token misread)
        surf = row_view.get("surf")
        if surf is None or int(surf) != int(surface):
            return False
        wave = row_view.get("wave")
        if wave is None or int(wave) != 1:
            return False
        target = row_view.get("target")
        if not (isinstance(target, (int, float)) and not isinstance(target, bool)
                and math.isfinite(target)):
            return False
        if token in _GRIN_FLOOR_TOKENS and not (target >= _GRIN_NONPHYSICAL_FLOOR):
            return False  # a GT below the nonphysical index floor (1.0) is no floor
        if require_weight:
            weight = row_view.get("weight")
            if not (isinstance(weight, (int, float)) and not isinstance(weight, bool)
                    and math.isfinite(weight) and weight >= _GRIN_INDEX_WEIGHT):
                return False
        return True
    except Exception:  # noqa: BLE001 — doubt = not accepted
        return False


def _parse_floor_row(mfe, i):
    """Parse MFE row ``i`` -> ``(row_view | None, fault: bool)`` (the per-row fault
    channel; a silently-skipped bound must NOT leave ``fault=False``).

    - a ``GetOperandAt`` / ``TypeName`` read throw -> ``(None, True)`` (FAIL-CLOSED: an
      unreadable row MIGHT be a floor row — doubt = fault, the ``grin_surfaces``
      ``unclassified_row`` philosophy);
    - a readable TypeName that is NOT one of the 12 floor tokens -> ``(None, False)`` (a
      genuine non-floor row is a CLEAN skip, never a fault);
    - a recognized floor token whose param read throws -> ``(None, True)`` (a floor row we
      could not read is a fault, not a silent skip);
    - a recognized floor token whose LOAD-BEARING audit read is unreadable
      (``Target``/``Surf``/``Wave`` -> ``None``) -> ``(None, True)`` (the same fail-closed
      rule one step deeper: the writer ALWAYS authors a positive Target + a readable
      Surf/Wave, so a ``None`` here means the read failed. A ``None`` Target/Surf/Wave is
      SILENTLY dropped from the box audit (``audit_rows``) by ``_row_floor_acceptance`` —
      a below-floor point would then go UNAUDITED with ``fault=False`` — so a recognized
      floor row with any unreadable load-bearing audit cell MUST fault, never read back as a
      ``None`` bound. ``Weight`` is DELIBERATELY excluded: the box audit ignores it
      (``require_weight=False``) and a ``None`` weight only downgrades the silencing tier
      (fail-SAFE — more warnings), so faulting on it would needlessly SKIP a box-violation
      check the readable Target could still run.

    Surf/Wave via ``_merit_cells.read_param_map`` (the slot map: Surf col2, Wave col3)."""
    try:
        op = mfe.GetOperandAt(i)
        token = str(op.TypeName)
    except Exception:  # noqa: BLE001 — fail-closed (a row we cannot classify is a fault)
        return None, True
    if token not in _ALL_FLOOR_TOKENS:
        return None, False  # a genuine non-floor row -> a clean skip (never a fault)
    try:
        params = _mc.read_param_map(op)
    except Exception:  # noqa: BLE001 — a recognized floor row we could not parse -> fault
        return None, True
    surf = _param_int(params, "Surf")
    wave = _param_int(params, "Wave")
    target = _safe_target(op)
    weight = _safe_weight(op)
    # Related case: a recognized floor row whose LOAD-BEARING audit read (Target / Surf /
    # Wave) is unreadable is a FAULT, not a silent None-bound with fault=False (a None here
    # is silently dropped from audit_rows -> a below-floor point goes UNAUDITED). Weight is
    # excluded (see the docstring — audit ignores it; None weight is fail-safe for silencing).
    if target is None or surf is None or wave is None:
        return None, True
    return ({"token": token, "surf": surf, "wave": wave,
             "target": target, "weight": weight}, False)


def _param_int(params, header):
    """The integer value of ``params[header]`` (from read_param_map), or ``None``."""
    entry = params.get(header)
    if not isinstance(entry, dict):
        return None
    value = entry.get("value")
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except Exception:  # noqa: BLE001 — an uncoercible param -> None
        return None


def _safe_target(op):
    """``op.Target`` as a finite float, or ``None`` (guarded)."""
    try:
        t = float(op.Target)
        return t if math.isfinite(t) else None
    except Exception:  # noqa: BLE001
        return None


def _safe_weight(op):
    """``op.Weight`` as a finite float, or ``None`` (guarded)."""
    try:
        w = float(op.Weight)
        return w if math.isfinite(w) else None
    except Exception:  # noqa: BLE001
        return None


def read_authored_box(mfe, surface):
    """Read the authored ``I#GT`` / ``I#LT`` physical-index targets off the live MFE for ``surface``.

    -> ``{"coverage": "complete"|"partial"|"none",
          "box": {point: (gt, lt)} | None,                    # SILENCING tier
          "audit_rows": {point: (gt|None, lt|None)},          # AUDIT tier (weight-IGNORED)
          "fault": bool}``

    coverage (SILENCING tier, ``require_weight=True``): "complete" = all 6 GT AND all 6 LT
    accepted AND per-point ``gt <= lt`` (coherence — an incoherent / impossible
    pre-existing box NEVER reads complete). "partial" = some accepted. "none" = zero.

    audit_rows (AUDIT tier, ``require_weight=False``): the strictest authored bound per point
    regardless of weight — the box audit checks live values against ANY authored box (a
    weight-0 box still gets audited: more detection, never less — the asymmetry).

    Duplicate rows for one (token, surface): the STRICTEST bound wins (max GT, min LT). A
    scan fault (``NumberOfOperands`` throws) -> ``{"coverage": "none", ..., "fault": True}``
    (doubt = warn). NEVER raises.
    """
    empty = {"coverage": "none", "box": None, "audit_rows": {}, "fault": False}
    try:
        n = int(mfe.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a total scan fault -> fault True (doubt = warn)
        return {"coverage": "none", "box": None, "audit_rows": {}, "fault": True}

    sil_gt, sil_lt = {}, {}   # silencing tier: strictest accepted target per point
    aud_gt, aud_lt = {}, {}   # audit tier: strictest accepted target per point (weight-ignored)
    row_fault = False         # a per-row read fault (a silently-skipped floor row)
    for i in range(1, n + 1):
        row_view, rf = _parse_floor_row(mfe, i)
        if rf:
            row_fault = True   # doubt = the box could not be fully audited (warn)
        if row_view is None:
            continue
        token = row_view["token"]
        point = _POINT_OF.get(token)
        if point is None:
            continue
        is_gt = token in _GRIN_FLOOR_TOKENS
        target = row_view["target"]
        if _row_floor_acceptance(row_view, surface, require_weight=False):
            if is_gt:
                aud_gt[point] = target if point not in aud_gt else max(aud_gt[point], target)
            else:
                aud_lt[point] = target if point not in aud_lt else min(aud_lt[point], target)
        if _row_floor_acceptance(row_view, surface, require_weight=True):
            if is_gt:
                sil_gt[point] = target if point not in sil_gt else max(sil_gt[point], target)
            else:
                sil_lt[point] = target if point not in sil_lt else min(sil_lt[point], target)

    audit_rows = {}
    for p in range(1, 7):
        g, l = aud_gt.get(p), aud_lt.get(p)
        if g is not None or l is not None:
            audit_rows[p] = (g, l)

    complete = (
        all(p in sil_gt for p in range(1, 7))
        and all(p in sil_lt for p in range(1, 7))
        and all(sil_gt[p] <= sil_lt[p] for p in range(1, 7))   # coherence
    )
    if complete:
        return {
            "coverage": "complete",
            "box": {p: (sil_gt[p], sil_lt[p]) for p in range(1, 7)},
            "audit_rows": audit_rows,
            "fault": row_fault,   # a per-row read fault is surfaced even with clean rows
        }
    if sil_gt or sil_lt:
        return {"coverage": "partial", "box": None,
                "audit_rows": audit_rows, "fault": row_fault}
    result = dict(empty)
    result["audit_rows"] = audit_rows
    result["fault"] = row_fault
    return result


__all__ = [
    # constants
    "_GRIN_FLOOR_TOKENS",
    "_GRIN_CEILING_TOKENS",
    "_GRIN_VALUE_TOKENS",
    "_GRIN_DLTN",
    "_ALL_FLOOR_TOKENS",
    "_INDEX_VALUE_OPERANDS",
    "_GRIN_NONPHYSICAL_FLOOR",
    "_GRIN_INDEX_WEIGHT",
    "_GRIN_BOX_AUDIT_TOL",
    "_GRIN_SAMPLED_COVERAGE_NOTE",
    # the ONE cell->index conversion locus; NEVER applied to a reading
    "_valid_cell_index_space",
    "cell_value_to_index",
    # readers
    "_suspicious_reading",
    "grin_surfaces",
    "read_index_vector",
    "read_dltn",
    "index_summary",
    # acceptance / box
    "_row_floor_acceptance",
    "read_authored_box",
]
