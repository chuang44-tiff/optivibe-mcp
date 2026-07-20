"""tools/_tolerance_catalog.py — the frozen operand-vocabulary home.

NOT dispatchable (no ``TOOL_SPECS``). The SINGLE source of truth for the 62
``ToleranceOperandType`` members: each operand's per-cell Header+DataType signature,
its family / cell-layout / tier classification, ±delta channel, units, and (for the
non-supported tiers) the labeled gap reason + fast-follow ticket.

**Derived 1:1 from the live probe captures** (§2.2 — NOT hand-typed from
intuition):

- ``m1_classify.operand_table`` —
  the per-operand cell-Header signature (col -> Header + ``kind``) read live off
  ``op.GetCellAt(col).Header`` + ``cell.DataType``. ``int_cells`` below is the ordered
  tuple of the INTEGER cells (the surface / range / code / param cells — NOT the
  ``Min``/``Max``/``Nominal`` Double VALUE cells, which are authored separately by the
  Double channel). The ``G-CATALOG-CAPTURE`` test re-reads this fixture and asserts
  each row's ``int_cells`` matches the captured signature — so the table CANNOT
  silently diverge from the probe (L25).
- ``c2c3_family_classification`` — the per-token run verdict (bites / zero-change /
  cb_required / nsc_required / crash / control) that drives the ``tier`` column, and
  the PROBE-2 compensator-inert finding.

The probe-grounded rules this module encodes:

- **The col->meaning map is per-operand and read off the live ``Header``** (mirrors
  the merit ``_merit_cells`` cell.DataType discriminator discipline — the TDE and MFE
  editors DIFFER, so this is a SEPARATE re-implementation, not a cross-import).
  ``Surf`` is a single surface; ``Surf1``/``Surf2`` are a surface RANGE; ``RollSurf``
  is the roll-surface (Param3); ``Code``/``Par#`` select a DOF/parameter — the layout
  is the ground truth, never guessed from the mnemonic (TRAD carries a ``Code``
  int cell at col 3 the v1 author never wrote — recorded here so the author path does
  not assume a scalar op has only a ``Surf`` cell).
- **The validator's accept set == the author's supported set**, both computed from
  THIS table (G-VALIDATE-EQ-AUTHOR) — ``int_cell_writes`` (the author side) and the
  tier classification (the validate side) read the same rows.
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------- #
# The frozen dataclasses (§2.1 — FROZEN field shapes).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CellSpec:
    """One TDE cell's live signature: column + Header + role + read-back DataType.

    ``col`` is the 2..N TDE cell column; ``header`` is the live Header at that col
    (``"Surf"`` / ``"Surf1"`` / ``"Surf2"`` / ``"RollSurf"`` / ``"Code"`` / ``"Par#"``
    / ``"Layr"`` / …); ``role`` is the semantic slot (the entry-key the author reads
    from); ``datatype`` is the read-back accessor discriminator (``"Integer"`` /
    ``"Double"`` — D6, the same Int/Double rule as the merit layer).
    """

    col: int
    header: str
    role: str
    datatype: str


@dataclass(frozen=True)
class TolOperandMeta:
    """The frozen metadata for one ``ToleranceOperandType`` member (§2.1).

    ``int_cells`` is the ordered tuple of the INTEGER cells the author resolves the
    entry's surface/surface2/roll_surf/code/param into (drives ``int_cell_writes``);
    the Double ``Min``/``Max`` value cells are authored separately. ``tier`` is the
    D10 classification (the single ``_TIERS`` set). ``has_minmax`` is True iff a
    ±delta perturbation operand (a control / compensator op carries no perturbation).
    """

    code: str
    family: str
    cell_layout: str
    int_cells: Tuple[CellSpec, ...]
    has_minmax: bool
    tier: str
    units: Optional[str]
    precondition: Optional[str]
    reason: Optional[str]
    ticket: Optional[str]


# The D10 tier universe — every one of the 62 lands in EXACTLY one (G-ENUM-COVER
# asserts ``m.tier in _TIERS`` for all rows; no untiered blind spot).
_TIERS = frozenset(
    {
        "supported",
        "structural_zero",
        "cb_required",
        "nsc_required",
        "crash_class",
        "compensator",
        "control",
    }
)

# The role universe (CellSpec.role) — the entry keys the author resolves into.
_ROLES = frozenset(
    {"surface", "surface2", "roll_surf", "code", "param", "layer", "other",
     "max_term", "min_term",
     # (§2.8 / D5) the TMCO multi-config row/config selectors. Distinct
     # entry keys from the analysis ``config`` selector (no parser cross-wire); both
     # REQUIRED (an omitted one is a malformed entry, never a silent default-0).
     "mce_row", "mce_config"}
)

# Header -> role classification (read off the live Header — the per-operand layout is
# the ground truth). ``Surf`` (single) and ``Surf1`` (range first) both resolve the
# entry ``surface`` slot; ``Surf2`` -> ``surface2``; ``RollSurf`` -> ``roll_surf``;
# the parameter/DOF selectors -> ``code``/``param``; everything else (Layr, Units,
# Object, Row, Config#, Seed, Type, #Dev, File#, Max#, Min#, Adjust, Data, Param,
# Statistics, Pivot At, Wave…) is a structural ``other`` cell (authored only when the
# entry carries that role; for the supported families the author drives surface/range
# /code/param — the ``other`` cells stay at their default 0).
_ROLE_BY_HEADER = {
    "Surf": "surface",
    "Surf1": "surface",
    "Surf2": "surface2",
    "RollSurf": "roll_surf",
    "Code": "code",
    "Par#": "param",
    # (S2b TOL) TEZI/TEXI Zernike-term range cells. Promoting Max#/Min# from the
    # ``other`` default to real entry-key roles AUTO-drops them from
    # ``param_required_cells`` (the ``role == "other"`` filter) so a supplied
    # max_term/min_term AUTHORS through ``int_cell_writes``; an OMITTED pair stays
    # the labeled ``param_required`` gap (never a silent 0/0 author). Units/Statistics
    # stay ``other`` -> ISO ops stay refused LOUD (scope-limit, free-by-construction).
    "Max#": "max_term",
    "Min#": "min_term",
    # (§2.8 / D5) TMCO multi-config selectors: ``Row`` (the MCE operand
    # row) and ``Config#`` (the configuration) are promoted from the ``other`` default
    # to real entry-key roles so a supplied mce_row/mce_config AUTHORS through
    # ``int_cell_writes``; BOTH are REQUIRED (an omitted one is a LOUD malformed-entry
    # refusal, the silent-zero-cell discipline). Distinct keys from the analysis
    # ``config`` selector so the two surfaces never cross-wire.
    "Row": "mce_row",
    "Config#": "mce_config",
}


def _role_for(header: str) -> str:
    """The CellSpec role for a live int-cell Header (default ``"other"``)."""
    return _ROLE_BY_HEADER.get(header, "other")


def _cells(*specs: Tuple[int, str]) -> Tuple[CellSpec, ...]:
    """Build the ordered ``int_cells`` tuple from ``(col, header)`` pairs.

    Every int cell is ``datatype="Integer"`` (the captured signature: the
    surface/range/code/param cells are all Integer cells; the Double cells are
    ``Nominal``/``Min``/``Max`` and are NOT int cells). ``role`` is derived from the
    Header.
    """
    return tuple(
        CellSpec(col=col, header=header, role=_role_for(header), datatype="Integer")
        for (col, header) in specs
    )


# --------------------------------------------------------------------------- #
# THE TABLE — 62 rows, derived 1:1 from the captures (§2.2).
#
# int_cells = the NON-BLANK INTEGER cells from m1_classify.operand_table.<TOK>.cells
# (cols 2/3/4 whose kind=="int" and Header != ""); the Double Nominal/Min/Max cells
# are excluded (they are the value channel, authored separately). tier comes from
# c2c3_family_classification.<TOK>.verdict (+ the PROBE-2 crash/cb/nsc/comp/control
# splits). has_minmax is True for every ±delta perturbation op; False for the
# control/compensator ops (no perturbation row). units per family (PROBE §Units).
# --------------------------------------------------------------------------- #
_SUPPORTED = "fully supported — authors, runs, and bites the criterion on a system " \
    "with the relevant structure"
_STRUCTURAL = "supported — authors and runs, but has no degree of freedom on a " \
    "plain spherical uncoated single-config surface (a zero-change row, NOT a gap); " \
    "bites on a system with the relevant structure (a coating / non-zero parameter / " \
    "multi-config / extra-data)"
_CONTROL = "a control / utility operand — it sets run state, it is not a ±delta " \
    "perturbation; authored for .zmx fidelity, no perturbation channel"
_COMP = "a user compensator — INERT through the headless OpenTolerancing() path " \
    "(PROBE-2: the engine does paraxial back-focus compensation only; an authored " \
    "COMP/CPAR changes nothing). Authored for .zmx fidelity; compensator_participates " \
    "is always false headless — never claim a user compensator recovered performance"
_CB_GAP = "needs a coordinate-break surface; the engine refuses on a plain " \
    "sequential surface ('Surface N must be a Coordinate break'). Supported after " \
    "the reflective/coordinate-break cycle lands. Use TETX/TEDX (surface tilt/" \
    "decenter range) meanwhile."
_NSC_GAP = "requires a non-sequential surface; the engine emits a clean " \
    "'requires surface N to be a Non-Sequential surface!' error on a sequential surface."
_CRASH_GAP = "refused: authored on a sequential surface this HARD-CRASHES the " \
    "headless tolerancing run (IPC RemotingException, no clean error line) — never " \
    "run on a sequential system."


def _supported(code, family, layout, int_cells, units):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=True, tier="supported", units=units,
        precondition=None, reason=None, ticket=None,
    )


def _structural(code, family, layout, int_cells, units, precondition=None):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=True, tier="structural_zero", units=units,
        precondition=precondition, reason=_STRUCTURAL, ticket=None,
    )


def _control(code, family, layout, int_cells):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=False, tier="control", units=None,
        precondition=None, reason=_CONTROL, ticket=None,
    )


def _compensator(code, family, layout, int_cells):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=False, tier="compensator", units=None,
        precondition=None, reason=_COMP, ticket=None,
    )


def _cb(code, family, layout, int_cells, units):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=True, tier="cb_required", units=units,
        precondition="coordinate_break", reason=_CB_GAP, ticket="Area-1-CB",
    )


def _nsc(code, family, layout, int_cells, units):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=True, tier="nsc_required", units=units,
        precondition="non_sequential", reason=_NSC_GAP, ticket="Area-NSC",
    )


def _crash(code, family, layout, int_cells, units):
    return TolOperandMeta(
        code=code, family=family, cell_layout=layout, int_cells=int_cells,
        has_minmax=True, tier="crash_class", units=units,
        precondition="non_sequential", reason=_CRASH_GAP, ticket="Area-NSC",
    )


TOL_OPERAND_META: Dict[str, TolOperandMeta] = {
    # --- control / utility (D19): author the row, NO ±delta perturbation -------- #
    "TOFF": _control("TOFF", "control", "control_only", ()),
    "SAVE": _control("SAVE", "control", "control_only", _cells((2, "File#"))),
    "STAT": _control("STAT", "control", "control_only",
                     _cells((2, "Type"), (3, "#Dev"))),
    "TWAV": _control("TWAV", "control", "control_only", ()),
    "SEED": _control("SEED", "control", "control_only", _cells((2, "Seed"))),
    "COMM": _control("COMM", "control", "control_only", ()),
    "MPVT": _control("MPVT", "control", "control_only", _cells((2, "Pivot At"))),

    # --- scalar (radius / curvature / fringe / thickness / conic / index / abbe) #
    # TRAD: the Code int cell at col 3 is recorded (the v1 silent-wrong trap — the
    # author path must not assume a scalar op has only a Surf cell). lens_units (a
    # radius perturbation is in lens units).
    "TRAD": _supported("TRAD", "scalar", "surf_code",
                       _cells((2, "Surf"), (3, "Code")), "lens_units"),
    "TCUR": _supported("TCUR", "scalar", "single_surf",
                       _cells((2, "Surf")), "dimensionless"),
    "TFRN": _supported("TFRN", "scalar", "single_surf",
                       _cells((2, "Surf")), "fringes"),
    "TTHI": _supported("TTHI", "scalar", "single_surf",
                       _cells((2, "Surf"), (3, "Adjust")), "lens_units"),
    "TCON": _supported("TCON", "scalar", "single_surf",
                       _cells((2, "Surf")), "dimensionless"),
    "TIND": _supported("TIND", "scalar", "single_surf",
                       _cells((2, "Surf")), "dimensionless"),
    "TABB": _supported("TABB", "scalar", "single_surf",
                       _cells((2, "Surf")), "dimensionless"),

    # --- surface irregularity (single Surf) ------------------------------------ #
    "TIRR": _supported("TIRR", "irregularity", "single_surf",
                       _cells((2, "Surf")), "fringes"),
    "TIRX": _supported("TIRX", "irregularity", "single_surf",
                       _cells((2, "Surf")), "fringes"),
    "TIRY": _supported("TIRY", "irregularity", "single_surf",
                       _cells((2, "Surf")), "fringes"),

    # --- sag decenter / tilt (single Surf) ------------------------------------- #
    "TSDX": _supported("TSDX", "sag", "single_surf",
                       _cells((2, "Surf")), "lens_units"),
    "TSDY": _supported("TSDY", "sag", "single_surf",
                       _cells((2, "Surf")), "lens_units"),
    "TSDR": _supported("TSDR", "sag", "single_surf",
                       _cells((2, "Surf")), "lens_units"),
    "TSTX": _supported("TSTX", "sag", "single_surf",
                       _cells((2, "Surf")), "degrees"),
    "TSTY": _supported("TSTY", "sag", "single_surf",
                       _cells((2, "Surf")), "degrees"),
    # TSDI = sag-decenter, zero-change here (no DOF on a centered surface).
    "TSDI": _structural("TSDI", "sag", "single_surf",
                        _cells((2, "Surf")), "lens_units"),

    # --- surface tilt RANGE (Surf1, Surf2) ------------------------------------- #
    "TETX": _supported("TETX", "surface_tilt_decenter", "surf_range",
                       _cells((2, "Surf1"), (3, "Surf2")), "degrees"),
    "TETY": _supported("TETY", "surface_tilt_decenter", "surf_range",
                       _cells((2, "Surf1"), (3, "Surf2")), "degrees"),
    # TETZ = z-tilt of a centered element -> zero-change on this doublet.
    "TETZ": _structural("TETZ", "surface_tilt_decenter", "surf_range",
                        _cells((2, "Surf1"), (3, "Surf2")), "degrees"),

    # --- surface decenter RANGE (Surf1, Surf2) --------------------------------- #
    "TEDX": _supported("TEDX", "surface_tilt_decenter", "surf_range",
                       _cells((2, "Surf1"), (3, "Surf2")), "lens_units"),
    "TEDY": _supported("TEDY", "surface_tilt_decenter", "surf_range",
                       _cells((2, "Surf1"), (3, "Surf2")), "lens_units"),
    "TEDR": _supported("TEDR", "surface_tilt_decenter", "surf_range",
                       _cells((2, "Surf1"), (3, "Surf2")), "lens_units"),

    # --- tilt-about-axis RANGE (Surf1, Surf2, RollSurf) ------------------------ #
    "TARX": _supported("TARX", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "degrees"),
    "TARY": _supported("TARY", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "degrees"),
    "TARR": _supported("TARR", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "degrees"),

    # --- roll decenter RANGE (Surf1, Surf2, RollSurf) -------------------------- #
    "TRLX": _supported("TRLX", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "lens_units"),
    "TRLY": _supported("TRLY", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "lens_units"),
    "TRLR": _supported("TRLR", "roll", "surf_range_roll",
                       _cells((2, "Surf1"), (3, "Surf2"), (4, "RollSurf")), "lens_units"),

    # --- Zernike / extended irregularity (Surf, Max#, Min#) -------------------- #
    "TEXI": _supported("TEXI", "zernike", "surf_minmax_term",
                       _cells((2, "Surf"), (3, "Max#"), (4, "Min#")), "fringes"),
    "TEZI": _supported("TEZI", "zernike", "surf_minmax_term",
                       _cells((2, "Surf"), (3, "Max#"), (4, "Min#")), "fringes"),

    # --- ISO 10110 (Surf, Units[, Statistics]) --------------------------------- #
    "ISOA": _supported("ISOA", "iso", "iso",
                       _cells((2, "Surf"), (3, "Units")), "dimensionless"),
    "ISOB": _supported("ISOB", "iso", "iso",
                       _cells((2, "Surf"), (3, "Units"), (4, "Statistics")),
                       "dimensionless"),
    "ISOC": _supported("ISOC", "iso", "iso",
                       _cells((2, "Surf"), (3, "Units"), (4, "Statistics")),
                       "dimensionless"),
    "ISOD": _supported("ISOD", "iso", "iso",
                       _cells((2, "Surf"), (3, "Units"), (4, "Statistics")),
                       "dimensionless"),

    # --- coating (multiplier / index / extinction) -> structural_zero ---------- #
    "TCMU": _structural("TCMU", "coating", "surf_layr",
                        _cells((2, "Surf"), (3, "Layr")), "dimensionless"),
    "TCIO": _structural("TCIO", "coating", "surf_layr",
                        _cells((2, "Surf"), (3, "Layr")), "dimensionless"),
    "TCEO": _structural("TCEO", "coating", "surf_layr",
                        _cells((2, "Surf"), (3, "Layr")), "dimensionless"),

    # --- surface-parameter value / irregular -> structural_zero ---------------- #
    "TPAR": _structural("TPAR", "parameter", "surf_par",
                        _cells((2, "Surf"), (3, "Par#")), "dimensionless"),
    "TPAI": _structural("TPAI", "parameter", "surf_par",
                        _cells((2, "Surf"), (3, "Par#")), "dimensionless"),

    # --- multi-config value -> SUPPORTED (§2.8 / D5) --------------- #
    # TMCO authors a ±perturbation on a TARGETED MCE cell (Row+Config# select it; col 5
    # Nominal auto-resolves to that cell's value; col 6/7 Min/Max = the ±delta channel,
    # written via write_double_verified). Family ``multi_config``; units are
    # per-the-targeted-MCE-operand (a CRVT TMCO is dimensionless, a THIC TMCO is
    # lens_units — operand-agnostic, disclosed). The headless run analyzes only the
    # CURRENT config, so a TMCO whose Config# != current reads a silent zero (the
    # tmco_config_mismatch WARN). TEDV stays _control (ChangeType(TEDV) FAILS — a labeled
    # known_gap refusal).
    "TMCO": _supported("TMCO", "multi_config", "config_row",
                       _cells((2, "Row"), (3, "Config#")), "per_targeted_mce_operand"),
    "TEDV": _control("TEDV", "parameter", "surf_par",
                     _cells((2, "Surf"), (3, "Par#"))),

    # --- element pivot-tilt -> CB-required (single Surf) ----------------------- #
    "TUTX": _cb("TUTX", "element_pivot", "single_surf", _cells((2, "Surf")), "degrees"),
    "TUTY": _cb("TUTY", "element_pivot", "single_surf", _cells((2, "Surf")), "degrees"),
    "TUTZ": _cb("TUTZ", "element_pivot", "single_surf", _cells((2, "Surf")), "degrees"),

    # --- element decenter -> CB-required (single Surf) ------------------------- #
    "TUDX": _cb("TUDX", "element_decenter", "single_surf",
                _cells((2, "Surf")), "lens_units"),
    "TUDY": _cb("TUDY", "element_decenter", "single_surf",
                _cells((2, "Surf")), "lens_units"),

    # --- NSC position -> nsc_required (clean error) ---------------------------- #
    "TNPS": _nsc("TNPS", "nsc", "nps_object",
                 _cells((2, "Surf"), (3, "Object"), (4, "Data")), "lens_units"),

    # --- NSC param / max -> crash_class (HARD refusal, ALWAYS) ----------------- #
    "TNPA": _crash("TNPA", "nsc", "nps_object",
                   _cells((2, "Surf"), (3, "Object"), (4, "Param")), "lens_units"),
    "TNMA": _crash("TNMA", "nsc", "nps_object",
                   _cells((2, "Surf"), (3, "Object"), (4, "Data")), "lens_units"),

    # --- compensators -> compensator / control (inert headless, PROBE-2) ------- #
    "COMP": _compensator("COMP", "compensator", "surf_code",
                         _cells((2, "Surf"), (3, "Code"))),
    "CPAR": _compensator("CPAR", "compensator", "surf_par",
                         _cells((2, "Surf"), (3, "Par#"))),
    # CEDV CMCO CNPA CNPS = compensators-as-control (D19): author, no perturbation.
    "CEDV": _control("CEDV", "compensator", "surf_par",
                     _cells((2, "Surf"), (3, "Par#"))),
    "CMCO": _control("CMCO", "compensator", "config_row",
                     _cells((2, "Row"), (3, "Config#"))),
    "CNPA": _control("CNPA", "compensator", "object_par",
                     _cells((2, "Object"), (3, "Par#"), (4, "Surf"))),
    "CNPS": _control("CNPS", "compensator", "object_par",
                     _cells((2, "Object"), (3, "Code"), (4, "Surf"))),
}


# --------------------------------------------------------------------------- #
# The supported-token sets (G-VALIDATE-EQ-AUTHOR: validate-accept == author-supported,
# both computed from THIS table so they cannot diverge).
# --------------------------------------------------------------------------- #
# A tier whose entry IS authorable + runnable (a ±delta perturbation lands a row).
_AUTHORABLE_TIERS = frozenset({"supported", "structural_zero"})


def supported_tokens() -> frozenset:
    """The set of tokens the author can author + run as a perturbation (G-VALIDATE-EQ-AUTHOR).

    Computed FROM the table: a token whose tier is ``supported`` / ``structural_zero``
    AND which exposes a ±delta channel (``has_minmax``). This is BOTH the validator's
    accept set and the author's supported set — they read the SAME table, so a tier
    flip moves both together (the G-VALIDATE-EQ-AUTHOR mutate-fails test).
    """
    return frozenset(
        code
        for code, meta in TOL_OPERAND_META.items()
        if meta.tier in _AUTHORABLE_TIERS and meta.has_minmax
    )


def meta_for(code: str) -> Optional[TolOperandMeta]:
    """The ``TolOperandMeta`` for ``code``, or ``None`` if not in the table.

    The runtime fail-closed lookup (G-RUNTIME-UNKNOWN): an un-tabled token returns
    ``None`` -> the caller refuses (never authors an unknown operand).
    """
    return TOL_OPERAND_META.get(code)


# --------------------------------------------------------------------------- #
# Enum parity (G-ENUM-COVER) — the covers-live-enum check, both directions.
# --------------------------------------------------------------------------- #
def validate_enum_parity(live_member_names) -> dict:
    """Assert ``set(live) == set(TOL_OPERAND_META)`` BOTH directions; structured result.

    Used by BOTH the unit gate (against the captured 62 ``m1_classify.enum_members``)
    AND the live gate (against ``System.Enum.GetNames(ToleranceOperandType)``). A
    version-bump member (enum-not-table) OR a removed/renamed member (table-not-enum)
    -> ``ok: False`` with the offending names — a stale table fails LOUD, both ways
    (G-ENUM-COVER). The caller asserts ``result["ok"]`` (the mock never closes the
    gate; the live reflection re-asserts the real membership, L24).
    """
    live = set(live_member_names)
    table = set(TOL_OPERAND_META)
    missing_from_table = sorted(live - table)   # enum has it, table doesn't (stale)
    missing_from_enum = sorted(table - live)    # table has it, enum doesn't (stale)
    # Defense in depth: every row is tiered into the known universe (no blind spot).
    untiered = sorted(c for c, m in TOL_OPERAND_META.items() if m.tier not in _TIERS)
    ok = (
        not missing_from_table
        and not missing_from_enum
        and not untiered
    )
    return {
        "ok": ok,
        "live_count": len(live),
        "table_count": len(table),
        "missing_from_table": missing_from_table,
        "missing_from_enum": missing_from_enum,
        "untiered": untiered,
    }


# --------------------------------------------------------------------------- #
# The author side (G-VALIDATE-EQ-AUTHOR) — the ordered int-cell write plan.
# --------------------------------------------------------------------------- #
# Entry-key -> CellSpec.role mapping. The author resolves the entry's surface /
# surface2 / roll_surf / code / param into the operand's declared int cells; the same
# role names appear on both sides (the entry schema and the CellSpec.role) so the
# author and the validator read the SAME table.
_ROLE_ENTRY_KEY = {
    "surface": "surface",
    "surface2": "surface2",
    "roll_surf": "roll_surf",
    "code": "code",
    "param": "param",
    # (S2b TOL) the Zernike-term range entry keys -> the Max#/Min# Integer cells.
    # ``int_cell_writes`` reads ``entry.get("max_term")``/``("min_term")``; the
    # validator's required-both/malformed-range pre-mutation guard is the single
    # enforcement point (these roles are deliberately NOT in ``_REQUIRED_ROLES`` —
    # that would raise a redundant generic error at AUTHOR time, post-mutation).
    "max_term": "max_term",
    "min_term": "min_term",
    # (§2.8 / D5) the TMCO multi-config selectors -> the Row/Config# int
    # cells. ``int_cell_writes`` reads ``entry.get("mce_row")``/``("mce_config")``; both
    # are REQUIRED (below) so an omitted one is a LOUD CatalogResolveError (the
    # malformed-entry refusal), never a silent default-0 author.
    "mce_row": "mce_row",
    "mce_config": "mce_config",
}

# The roles that are STRUCTURALLY REQUIRED — a missing value is a malformed entry (a
# range op with no ``surface2`` errors "Illegal surface ranges." at run). The DOF
# selectors ``code`` / ``param`` are OPTIONAL and default to ``0`` (the captured
# default: TRAD's ``Code=0`` worked precisely because the engine defaults it; a COMP's
# ``Code`` / a CPAR's ``Par#`` is supplied by the caller when a specific DOF is meant).
# (§2.8 / D5) ``mce_row``/``mce_config`` are REQUIRED — a TMCO with no
# Row/Config# targets nothing (the silent-zero-cell discipline; omitting one is a LOUD
# malformed-entry refusal, the S2b Max#/Min# role-promotion precedent).
_REQUIRED_ROLES = frozenset(
    {"surface", "surface2", "roll_surf", "mce_row", "mce_config"}
)


class CatalogResolveError(ValueError):
    """An entry could not be resolved against an operand's declared int cells.

    A plain ``ValueError`` (a PRE-mutation input-class failure — the caller turns it
    into a ``tolerancing_param`` envelope WITHOUT touching the engine). Raised when a
    required role cell has no entry value (a range op with no ``surface2``), or an
    entry supplies a role the operand does not expose (``G-INPUT-EXTRACELL`` — a
    ``surface2`` on a single_surf op).
    """


def int_cell_writes(meta: TolOperandMeta, entry: dict) -> List[Tuple[CellSpec, int]]:
    """The ordered ``[(CellSpec, int_value)]`` the author path writes for ``entry``.

    Resolves the entry's ``surface``/``surface2``/``roll_surf``/``code``/``param`` into
    ``meta.int_cells`` (the SAME table the validator's accept set reads — the
    G-VALIDATE-EQ-AUTHOR contract). For each declared int cell:

    - a ``surface``/``surface2``/``roll_surf``/``code``/``param`` role reads the entry
      value at that key; a MISSING required value -> ``CatalogResolveError`` (a range
      op with no ``surface2``);
    - an ``other`` role (Layr / Units / Object / Row / Adjust / … and the ``code``
      cell on TRAD that the v1 author left at 0) defaults to the entry value if the
      caller supplied one at a matching key, else ``0`` (the captured default — TRAD's
      ``Code=0`` worked precisely because the engine defaults it).

    G-INPUT-EXTRACELL (closed at the consumer's validation boundary, not here): the
    consumer rejects an entry that supplies a role the operand does not declare BEFORE
    calling this. This function is the AUTHOR plan only — it never silently drops a
    declared cell.
    """
    writes: List[Tuple[CellSpec, int]] = []
    for spec in meta.int_cells:
        role = spec.role
        if role in _ROLE_ENTRY_KEY:
            key = _ROLE_ENTRY_KEY[role]
            supplied = entry.get(key)
            if supplied is None:
                if role in _REQUIRED_ROLES:
                    raise CatalogResolveError(
                        f"operand {meta.code} cell {spec.header!r} (col {spec.col}) "
                        f"needs the entry {key!r} value; none supplied"
                    )
                # An OPTIONAL DOF selector (code / param): default to 0 (the captured
                # default — TRAD's Code=0 worked because the engine defaults it).
                value = 0
            else:
                value = supplied
        else:
            # An ``other`` structural cell (Layr, Units, Adjust, Object, Row, …):
            # default to 0 (the captured default) unless the caller supplied a value
            # at a matching role key.
            value = entry.get(role, 0)
            if value is None:
                value = 0
        writes.append((spec, value))
    return writes


# The ``other`` int-cell Headers whose captured DEFAULT 0 silently encodes a DIFFERENT
# tolerance than any sane intent — the blind spots. These cells SELECT
# WHAT is perturbed (which Zernike-term range; which ISO unit/statistics convention), so a
# default-0 is not a benign "use the default" — it is a wrong/empty tolerance that runs,
# parses, and counts as ran while encoding nothing the caller meant:
#   - ``Max#`` / ``Min#`` (TEXI/TEZI): the Zernike-term index RANGE; default 0/0 perturbs
#     ZERO terms (a degenerate extended-irregularity tolerance).
#   - ``Units`` / ``Statistics`` (ISOA/ISOB/ISOC/ISOD): the ISO-10110 unit + statistics
#     selector; default 0 is a wrong convention, not "the intended one".
# We have NO probe-grounded entry key to drive them, so an operand exposing one is a
# labeled ``param_required`` gap (refused loudly), NEVER a silent zero-cell author. Other
# ``other`` cells (TTHI's ``Adjust``, TCMU's ``Layr``, …) DEFAULT 0 = the intended value
# the v1 author already relied on (probe-grounded benign) and are NOT gated.
# NOTE (S2b): ``Max#``/``Min#`` are now ROLE-PROMOTED to the max_term/min_term entry keys
# (driven through the term validator), so they no longer reach this param_required shelf
# (param_required_cells filters on role == "other"; they are no longer "other"). Their
# presence in this set is now DEAD but HARMLESS — left as-is to avoid any behavior risk.
# ``Units``/``Statistics`` stay "other" (NOT promoted) so ISO ops stay refused LOUD.
_PARAM_REQUIRED_HEADERS = frozenset({"Max#", "Min#", "Units", "Statistics"})


def param_required_cells(meta: TolOperandMeta) -> Tuple[str, ...]:
    """The undrivable parameter-selecting int-cell Headers an operand exposes.

    Returns the ordered tuple of ``_PARAM_REQUIRED_HEADERS`` cells the operand declares —
    cells whose default 0 SILENTLY encodes a DIFFERENT tolerance than intended (TEXI's
    ``Max#``/``Min#`` Zernike-term range -> zero terms; ISOB/C/D's ``Units``/``Statistics``
    selector -> wrong convention). A NON-EMPTY result means the operand is a labeled
    ``param_required`` gap — refused loudly, NEVER silently authored. The probe-grounded
    rule: drive a meaningful cell or refuse the operand loudly. Benign ``other`` cells
    (Adjust / Layr / …) whose captured default 0 IS the intended value are NOT gated.
    """
    return tuple(
        spec.header
        for spec in meta.int_cells
        if spec.role == "other" and spec.header in _PARAM_REQUIRED_HEADERS
    )


def declared_roles(meta: TolOperandMeta) -> frozenset:
    """The set of resolvable entry roles an operand exposes (for G-INPUT-EXTRACELL).

    The consumer rejects an entry that carries a role-bearing key (surface2 /
    roll_surf / code / param) the operand does NOT declare — read from THIS table so
    the reject set and the author set come from the same place.
    """
    return frozenset(
        spec.role for spec in meta.int_cells if spec.role in _ROLE_ENTRY_KEY
    )


__all__ = [
    "CellSpec",
    "TolOperandMeta",
    "TOL_OPERAND_META",
    "meta_for",
    "validate_enum_parity",
    "int_cell_writes",
    "supported_tokens",
    "declared_roles",
    "param_required_cells",
    "CatalogResolveError",
    "_TIERS",
    "_ROLES",
    "_AUTHORABLE_TIERS",
]
