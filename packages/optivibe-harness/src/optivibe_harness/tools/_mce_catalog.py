"""tools/_mce_catalog.py — the frozen 117-member ``MultiConfigOperandType`` table.

NOT dispatchable (no ``TOOL_SPECS``). The SINGLE source of truth for the 117
``MultiConfigOperandType`` members: each operand's family, per-config CELL DataType
accessor, ``takes_surface`` / ``takes_param`` selector signature, tier classification,
and (for the non-authorable tiers) the labeled-gap ``reason``.

Mirrors ``_tolerance_catalog.py`` (the token->tier table derived 1:1 from a live
capture + the ``G-ENUM-COVER`` parity check). ONE dataclass, ONE table, ONE parity
check, NO per-type tool.

**Derived 1:1 from the live probe captures** (§2 — NOT hand-typed from
intuition):

- the live probe capture's ``members.<TOK>.DataType`` — the per-member per-config
  CELL DataType, **FROZEN 1:1**.
  The tally is the contract: 80 Double / 25 Integer / 12 String. Load-bearing case:
  ``CBOR`` is a **Double** cell (a logically-integer diffraction-grating order stored
  as Double — HZ-CBOR), recorded as the freeze says, NEVER the mnemonic-guessed Integer.
- the live probe capture's ``takes_surface`` — the curated ``takes_surface``
  signal (THIC/CRVT/SDIA/GLSS/CBDX/PRAM Param1 bite; TEMP/PRES/WAVE/APER/AFOC/TELE/MOFF/
  STPS Param1=0 system). ``takes_surface`` is **CURATED by family (§2.4), NOT read from
  the freeze** — the freeze captured ChangeType-seeded operands with selectors left at
  the default 0, so THIC reads Param1=0 in the freeze despite being surface-bearing.

The probe-grounded rules this module encodes:

- the per-config cell is ONE value cell via ``op.GetOperandCell(cfg)`` — there is NO
  ordered int-cell vector (unlike the TDE); the selectors are bare ``Param1/2/3``
  integer properties, captured here by the two booleans ``takes_surface``/``takes_param``;
- ``takes_param`` is read from the freeze WHERE non-zero (PAR1..8 carry ``Param2=1..8``)
  and curated otherwise (PRAM takes_param=True);
- the author boundary is the SINGLE catalog-driven gate ``is_authorable(meta) :=
  meta.tier in {"supported", "coord_break", "system"}`` — no per-type code.
"""
from dataclasses import dataclass
from typing import Dict, Optional


# --------------------------------------------------------------------------- #
# The frozen dataclass (§2.1 — FROZEN 7-field shape).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class MceOperandMeta:
    """The frozen metadata for one ``MultiConfigOperandType`` member (§2.1).

    ``value_datatype`` is the per-config CELL accessor discriminator (FROZEN 1:1 from
    the freeze, NOT the mnemonic — ``CBOR`` is Double). ``takes_surface`` is whether
    ``Param1`` is a surface number (CURATED by family, NOT the freeze defaults).
    ``takes_param`` is whether ``Param2`` is a parameter index (from the freeze where
    set; curated for PRAM). ``tier`` is the single author-boundary classification.
    ``reason`` is the labeled-gap message for a non-authorable tier (nsc/unsupported);
    ``None`` otherwise.
    """

    code: str               # the MultiConfigOperandType token (e.g. "THIC")
    family: str             # geometry / coord_break / element_coord / surface_param /
                            #   aperture / field / wavelength / system / nsc / control
    value_datatype: str     # "Double" | "Integer" | "String" — the per-config CELL accessor
    takes_surface: bool     # Param1 = a surface number? (CURATED by family)
    takes_param: bool       # Param2 = a parameter index?
    tier: str               # one of _TIERS (§2.2)
    reason: Optional[str]   # the labeled-gap message for a non-authorable tier; None otherwise


# --------------------------------------------------------------------------- #
# The tier set (frozen ``_TIERS``) — every one of the 117 lands in EXACTLY one
# (``validate_enum_parity`` asserts ``m.tier in _TIERS`` for all rows; no untiered
# blind spot). The author boundary keys on this set ONLY.
# --------------------------------------------------------------------------- #
_TIERS = frozenset({"supported", "coord_break", "system", "nsc", "unsupported"})

# The author boundary tiers (is_authorable). nsc/unsupported are fail-closed refusals.
_AUTHORABLE_TIERS = frozenset({"supported", "coord_break", "system"})


# --------------------------------------------------------------------------- #
# The labeled-gap reasons for the non-authorable tiers (§2.2).
# --------------------------------------------------------------------------- #
_NSC_GAP = (
    "an NSC / ghost-image multi-config operand — it requires a non-sequential "
    "surface; authoring it on a sequential system is refused fail-closed in S1 "
    "(the tolerance-v2 nsc_required precedent). Not authorable."
)
_UNSUPPORTED_GAP = (
    "not authorable in S1 — an un-probed / future member (the default fail-closed "
    "bucket) OR a member the build-opening micro-probe found throws on author. "
    "Refused rather than authored."
)


# --------------------------------------------------------------------------- #
# The frozen per-member DataType — 1:1 from the freeze (§2.3, the gap is CLOSED).
#
# These two sets are the freeze's 12 String + 25 Integer members, encoded VERBATIM
# from the live probe capture (§2.3 lists). Every OTHER member is
# Double (the remaining 80). ``CBOR`` is deliberately ABSENT from the Integer set —
# the freeze records it Double (HZ-CBOR), so it falls through to Double.
# --------------------------------------------------------------------------- #
_STRING_MEMBERS = frozenset({
    "COTN", "EDVA", "FCMM", "GLSS", "LTTL", "MCOM",
    "MOFF", "NCOM", "NCOT", "NGLS", "SWCN", "UDAF",
})

_INTEGER_MEMBERS = frozenset({
    "AFOC", "AICN", "APDT", "APTP", "CPCN", "CROR", "CRSR", "FLTP", "GCRS",
    "IGNM", "IGNR", "IGTO", "MCHI", "MTFU", "OPDR", "PRWV", "PSP1", "PUCN",
    "PXAR", "RAAM", "SATP", "SDRW", "SRTS", "STPS", "TELE",
})


def _datatype_for(code: str) -> str:
    """The frozen per-config cell DataType for ``code`` (String/Integer/Double).

    Keyed on the freeze's String (12) + Integer (25) sets; everything else is Double
    (the remaining 80). ``CBOR`` is NOT in the Integer set, so it resolves Double
    (the HZ-CBOR headline — a logically-integer order stored as a Double cell).
    """
    if code in _STRING_MEMBERS:
        return "String"
    if code in _INTEGER_MEMBERS:
        return "Integer"
    return "Double"


# --------------------------------------------------------------------------- #
# Family + takes_surface/takes_param + tier curation (§2.4 table).
#
# Each token is assigned to ONE family (which drives takes_surface/takes_param/tier).
# takes_surface is CURATED by family (NOT the freeze Param defaults — the freeze
# captured default-0 selectors on seeded operands). takes_param is read from the
# freeze where non-zero (PAR1..8 carry Param2=1..8) and curated for PRAM.
# --------------------------------------------------------------------------- #

# geometry (per-surface scalar geometry) — takes_surface=True, takes_param=False.
# Includes the lens-data per-surface values: thickness/curvature/semidiameter/conic/
# index/abbe/coating-comment/edge-value/glass/temperature-coeff. (TEMP/PRES are SYSTEM
# environment, not surface — they go in `system`.)
_GEOMETRY = frozenset({
    "THIC", "CRVT", "SDIA", "CONN", "COTN", "GLSS", "MABB", "MIND", "TCEX",
    "MCSD", "MDPG", "CHZN",
})

# coord_break — the CB / element-coord / surface-coord families (§2.2 list). All
# take a surface; authorable (cell verified, geometric effect deferred to S2).
_COORD_BREAK = frozenset({
    "CBDX", "CBDY", "CBTX", "CBTY", "CBTZ", "CBOR",
    "CADX", "CADY", "CATX", "CATY", "CATZ", "CAOR",
    "CSDX", "CSDY", "CSTX", "CSTY", "CSTZ", "CSP1", "CSP2",
    "CROR", "CRSR",
})

# surface_param (PRAM) — Param1=surface, Param2=which parameter (takes_param=True).
_SURFACE_PARAM_PRAM = frozenset({"PRAM"})

# surface_param (PARn) — Param2 pre-seeded 1..8 in the freeze; the index IS the
# operand. takes_surface=True (a surface's numbered parameter cell), takes_param=False
# (the parameter index is encoded in the operand code, not a Param2 the caller sets).
_SURFACE_PARAM_PARN = frozenset({
    "PAR1", "PAR2", "PAR3", "PAR4", "PAR5", "PAR6", "PAR7", "PAR8",
})

# aperture (per-surface aperture) — takes_surface=True, takes_param=False.
# APMN/APMX/APDX/APDY/APDT/APTP/PXAR/AICN are per-surface aperture controls.
_APERTURE_SURFACE = frozenset({
    "APMN", "APMX", "APDX", "APDY", "APDT", "APTP", "PXAR", "AICN", "APDF",
})

# aperture (system) — APER is the SYSTEM aperture value (Param1=0 in the freeze);
# the micro-probe (§11 item 1) confirms system-vs-surface; default conservative
# takes_surface=False, tier supported.
_APERTURE_SYSTEM = frozenset({"APER"})

# field — takes_surface=False, takes_param=False.
_FIELD = frozenset({
    "XFIE", "YFIE", "FTAN", "FVAN", "FVCX", "FVCY", "FVDX", "FVDY", "FLTP", "FLWT",
})

# The field-vignetting MCE operands (§2): FVDX/FVDY/FVCX/FVCY select the TARGET
# field via ``op.Param1`` (a 0-based field index, probe §3) — distinct from ``Param2``
# (PRAM's parameter index). ``set_config_operand`` exposes this via an optional ``field``
# selector ONLY for these operands; a non-field-selector operand handed ``field`` is
# refused. The FROZEN set is the single source of truth (the ``is_row_ref_header``
# module-predicate precedent — no dataclass change).
_FIELD_SELECTOR = frozenset({"FVDX", "FVDY", "FVCX", "FVCY"})

# wavelength — takes_surface=False, takes_param=False.
_WAVELENGTH = frozenset({"WAVE", "WLWT", "PRWV", "CWGT"})

# system / environment — takes_surface=False, takes_param=False (except IGNR/IGNM/IGTO
# which carry a surface in Param1, see _SYSTEM_SURFACE below). The big system bucket:
# environment, telecentric/afocal flags, hold/offset/stop/title/ray-aim/draw flags,
# pickup/solve config flags, multi-config-chief, etc.
_SYSTEM = frozenset({
    "TEMP", "PRES", "TELE", "AFOC", "MOFF", "STPS", "HOLD", "SATP", "LTTL",
    "MTFU", "SDRW", "SRTS", "RAAM", "GCRS", "OPDR", "MCHI", "MCOM", "PUCN",
    "CPCN", "FCMM", "EDVA", "SWCN", "UDAF", "FCMM", "PSP1", "PSP2", "PSP3",
})

# system operands that DO carry a surface in Param1 (object-ignore / ignore-to).
# IGNR reads Param1=1 in the freeze (object-ignore) — takes_surface=True.
_SYSTEM_SURFACE = frozenset({"IGNR", "IGNM", "IGTO"})

# nsc — the NSC / ghost-image families (NPOS/NPRO/NCOM/NCOT/NGLS/NPAR + GPxx + PSxx
# ghost). Require a non-sequential surface; authored fail-CLOSED in S1. takes_surface
# True (Param1 carries the NSC surface), takes_param where Param3 set.
_NSC = frozenset({
    "NPOS", "NPRO", "NCOM", "NCOT", "NGLS", "NPAR",
    "GPEX", "GPEY", "GPIU", "GPJX", "GPJY", "GPPX", "GPPY", "GQPO",
    "PSCX", "PSCY", "PSHX", "PSHY", "PSHZ",
})

# Per-surface tolerance-sag / parameter-solve operands that carry a surface but are
# system-level controls in the MCE. TSP1/2/3 + GPxx already placed; this catches the
# remaining stragglers from the 117 not yet assigned (assigned to `system` by
# fall-through below if not listed).
_SYSTEM_EXTRA = frozenset({"TSP1", "TSP2", "TSP3"})


def _family_for(code: str) -> str:
    """The curated family for ``code`` (drives takes_surface/takes_param/tier)."""
    if code in _GEOMETRY:
        return "geometry"
    if code in _COORD_BREAK:
        return "coord_break"
    if code in _SURFACE_PARAM_PRAM or code in _SURFACE_PARAM_PARN:
        return "surface_param"
    if code in _APERTURE_SURFACE:
        return "aperture"
    if code in _APERTURE_SYSTEM:
        return "aperture_system"
    if code in _FIELD:
        return "field"
    if code in _WAVELENGTH:
        return "wavelength"
    if code in _NSC:
        return "nsc"
    if code in _SYSTEM_SURFACE:
        return "system"
    # Everything else (the system / environment bucket + any straggler) -> system.
    return "system"


def _takes_surface_for(code: str, family: str) -> bool:
    """``takes_surface`` CURATED by family (§2.4 — NOT the freeze Param defaults)."""
    if family in ("geometry", "coord_break", "surface_param", "aperture", "nsc"):
        return True
    if code in _SYSTEM_SURFACE:  # IGNR/IGNM/IGTO carry a surface in Param1
        return True
    # aperture_system (APER) / field / wavelength / system -> no surface.
    return False


def _takes_param_for(code: str, family: str) -> bool:
    """``takes_param`` — read from the freeze where non-zero, curated for PRAM (§2.4)."""
    if code == "PRAM":
        return True
    # PAR1..8 carry Param2=1..8 in the freeze, but the index IS the operand code (the
    # caller does NOT set Param2 — the §2.4 note "the index IS the operand"), so the
    # caller-facing takes_param is False for PARn.
    return False


def _tier_for(family: str) -> str:
    """The author-boundary tier for a family (§2.2)."""
    if family == "coord_break":
        return "coord_break"
    if family == "nsc":
        return "nsc"
    # geometry / surface_param / aperture / aperture_system / field / wavelength /
    # system -> all authorable. supported (the falsifier-proven cell families) +
    # system (authorable, no S1 bite grader). We tier the system bucket as `system`
    # and everything else falsifier-provable as `supported`.
    if family == "system":
        return "system"
    return "supported"


def _reason_for(tier: str) -> Optional[str]:
    """The labeled-gap ``reason`` for a non-authorable tier; ``None`` otherwise."""
    if tier == "nsc":
        return _NSC_GAP
    if tier == "unsupported":
        return _UNSUPPORTED_GAP
    return None


# --------------------------------------------------------------------------- #
# THE TABLE — built from the freeze's 117 member tokens (the single enumeration
# source). Each row's DataType is frozen 1:1 from the freeze; family / takes_surface
# / takes_param / tier / reason are curated per the rules above.
# --------------------------------------------------------------------------- #
# The 117 tokens, enumerated VERBATIM from the freeze ``members`` keys (the build's
# single source of the membership universe). Listed in the freeze's sorted order.
_FREEZE_TOKENS = (
    "AFOC", "AICN", "APDF", "APDT", "APDX", "APDY", "APER", "APMN", "APMX",
    "APTP", "CADX", "CADY", "CAOR", "CATX", "CATY", "CATZ", "CBDX", "CBDY",
    "CBOR", "CBTX", "CBTY", "CBTZ", "CHZN", "CONN", "COTN", "CPCN", "CROR",
    "CRSR", "CRVT", "CSDX", "CSDY", "CSP1", "CSP2", "CSTX", "CSTY", "CSTZ",
    "CWGT", "EDVA", "FCMM", "FLTP", "FLWT", "FTAN", "FVAN", "FVCX", "FVCY",
    "FVDX", "FVDY", "GCRS", "GLSS", "GPEX", "GPEY", "GPIU", "GPJX", "GPJY",
    "GPPX", "GPPY", "GQPO", "HOLD", "IGNM", "IGNR", "IGTO", "LTTL", "MABB",
    "MCHI", "MCOM", "MCSD", "MDPG", "MIND", "MOFF", "MTFU", "NCOM", "NCOT",
    "NGLS", "NPAR", "NPOS", "NPRO", "OPDR", "PAR1", "PAR2", "PAR3", "PAR4",
    "PAR5", "PAR6", "PAR7", "PAR8", "PRAM", "PRES", "PRWV", "PSCX", "PSCY",
    "PSHX", "PSHY", "PSHZ", "PSP1", "PSP2", "PSP3", "PUCN", "PXAR", "RAAM",
    "SATP", "SDIA", "SDRW", "SRTS", "STPS", "SWCN", "TCEX", "TELE", "TEMP",
    "THIC", "TSP1", "TSP2", "TSP3", "UDAF", "WAVE", "WLWT", "XFIE", "YFIE",
)


def _build_meta(code: str) -> MceOperandMeta:
    """Assemble the frozen ``MceOperandMeta`` for one token from the curation rules."""
    family = _family_for(code)
    tier = _tier_for(family)
    return MceOperandMeta(
        code=code,
        family=family,
        value_datatype=_datatype_for(code),
        takes_surface=_takes_surface_for(code, family),
        takes_param=_takes_param_for(code, family),
        tier=tier,
        reason=_reason_for(tier),
    )


MCE_OPERAND_META: Dict[str, MceOperandMeta] = {
    code: _build_meta(code) for code in _FREEZE_TOKENS
}


# --------------------------------------------------------------------------- #
# Lookup helpers (§2.5).
# --------------------------------------------------------------------------- #
def meta_for(code) -> Optional[MceOperandMeta]:
    """The ``MceOperandMeta`` for ``code``, or ``None`` if not in the table.

    The runtime fail-closed lookup: an un-tabled / unknown token returns ``None`` ->
    the caller refuses (``mce_unsupported_operand``), never authors an unknown operand.
    """
    return MCE_OPERAND_META.get(code)


def is_authorable(meta: MceOperandMeta) -> bool:
    """The single catalog-driven author boundary: ``tier in {supported, coord_break, system}``.

    ``set_config_operand`` refuses ``tier in {nsc, unsupported}`` fail-closed (naming
    ``meta.reason``); everything else is authorable.
    """
    return meta.tier in _AUTHORABLE_TIERS


def authorable_tokens() -> frozenset:
    """The frozen set of authorable token codes (``is_authorable`` over the table)."""
    return frozenset(
        code for code, meta in MCE_OPERAND_META.items() if is_authorable(meta)
    )


def selects_field(code) -> bool:
    """True iff ``code`` is a field-vignetting operand whose ``Param1`` is a field index.

    The field-selector gate for ``set_config_operand``'s optional ``field`` param
    (§2): only FVDX/FVDY/FVCX/FVCY take a 0-based field index in ``Param1``. A non-member
    handed ``field`` is refused ``mce_param`` (disjoint) — ``field`` is NEVER conflated
    with ``param`` (which writes ``Param2``).
    """
    return code in _FIELD_SELECTOR


# --------------------------------------------------------------------------- #
# Enum parity (the tolerance-v2 ``G-ENUM-COVER``) — the covers-live-enum check,
# both directions + every row tiered (§2.5).
# --------------------------------------------------------------------------- #
def validate_enum_parity(live_member_names) -> dict:
    """Assert ``set(live) == set(MCE_OPERAND_META)`` BOTH directions; structured result.

    Used by BOTH the unit gate (against the captured 117 from the fixture) AND the live
    gate (against ``System.Enum.GetNames(MultiConfigOperandType)``). A version-bump
    member (enum-not-table) OR a removed/renamed member (table-not-enum) OR an untiered
    row -> ``ok: False`` LOUD with the offending names — a stale table fails both ways
    (the mock never closes the gate; the live reflection re-asserts the real membership,
    L24).

    Returns ``{ok, live_count, table_count, missing_from_table, missing_from_enum,
    untiered}``.
    """
    live = set(live_member_names)
    table = set(MCE_OPERAND_META)
    missing_from_table = sorted(live - table)   # enum has it, table doesn't (stale)
    missing_from_enum = sorted(table - live)    # table has it, enum doesn't (stale)
    # Defense in depth: every row is tiered into the known universe (no blind spot).
    untiered = sorted(
        c for c, m in MCE_OPERAND_META.items() if m.tier not in _TIERS
    )
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


__all__ = [
    "MceOperandMeta",
    "MCE_OPERAND_META",
    "meta_for",
    "is_authorable",
    "authorable_tokens",
    "selects_field",
    "validate_enum_parity",
    "_TIERS",
    "_AUTHORABLE_TIERS",
    "_FIELD_SELECTOR",
]
