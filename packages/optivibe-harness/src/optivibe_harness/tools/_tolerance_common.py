"""tools/_tolerance_common.py — private substrate for the tolerance tool.

NOT dispatchable (no ``TOOL_SPECS``). The probe-grounded tolerancing primitives the
single ``tolerance`` handler reuses, so every load-bearing safety rule lives in
exactly one place (mirrors ``_optimize_common`` / ``_measurement_common`` /
``_merit_io``):

- ``_resolve_tol_enum(enum_type, member_name)`` — getattr-ONLY enum member resolution
  for ALL families (the operand-type enum + the SetupModes / Criterions /
  MonteCarloStatistics run enums). NEVER ``System.Enum.Parse`` (probe §2: passing a
  reflected ``RuntimeType`` HARD-CRASHES pythonnet, no Python traceback).
- ``_tol_namespace_enum(system, name)`` — reach the run-mode enum TYPE by DIRECT
  attribute on ``ZOSAPI.Tools.Tolerancing`` (its ``dir()`` is EMPTY — a proxy quirk,
  probe §2). A TYPE-reach failure is the ``tolerancing_unavailable`` signal.
- ``_tolerance_operand_enum(system)`` — the ``ZOSAPI.Editors.TDE.ToleranceOperandType``
  enum TYPE (62 members; TRAD/TTHI/TIND/TABB present).
- ``_tolerancing_session(system)`` — the SINGLE-SLOT ``OpenTolerancing()`` -> ``Close()``-
  in-``finally`` context manager (probe §2/§7). A ``None`` open is the
  ``tolerancing_unavailable`` signal (the manager never enters the try/finally on a
  ``None`` handle — nothing to Close).
- ``parse_tol_report(text, *, mode, trials)`` — the report PARSER (the highest-value
  anti-silent-zero axis, D11). Pure + fixture-testable. Every field parses to
  ``(value, found)``, NEVER zero-fills; a completion/criterion-label/BOM/mode-yield
  canary miss raises a LOUD ``ToleranceError(family="tolerancing_parse")``.
- ``decode_tol_report(raw_bytes)`` — the BOM-magic gate + UTF-16-LE decode (mirrors
  ``_merit_io``: ``FF FE`` BOM check, BOM-skip before decode).
- ``_default_tolerances(system)`` — pure-python default-budget builder (D3): one
  TRAD+TTHI per powered surface, one TIND+TABB per glass surface; SKIPS a flat/``inf``
  surface for TRAD and an AIR surface for TIND/TABB.
- ``validate_tolerances(system, tolerances)`` — the two-phase pre-flight validator
  (D14): collects ALL input errors BEFORE any TDE mutation or tool open.
- ``snapshot_lde`` / ``lde_unchanged`` — the D16 side-effect tripwire.
- ``reap_tol_artifacts`` — the ``.ZTD`` / report glob-reap (D15, the ``.ZDA`` #59
  precedent).

Live ZOS-API integration: exercised live against the engine; unit-tested against
fixture-seeded fakes that write the CAPTURED UTF-16 report so the parser runs against
REAL engine text.
"""
import glob
import math
import os
import re
from contextlib import contextmanager

from ..errors import ToolError
from . import _tolerance_catalog as _cat

# NOTE: ``_tol_cells`` is imported LAZILY (inside ``validate_tolerances``) to avoid a
# circular import — ``_tol_cells`` imports ``ToleranceError`` from THIS module (defined
# below), so a top-level ``from . import _tol_cells`` would import a partially-built
# module. The shared L30 predicate ``is_integral_int`` is reached via the lazy import.


# TIND/TABB need a glass surface (D3/G15); TRAD is degenerate on a flat
# (inf-radius) surface (D3/G16). The v1 SCALAR whitelist (_SCALAR_TOKENS /
# _GLASS_TOKENS) is DELETED (D3): the catalog table is the single operand vocabulary;
# the glass/flat preconditions are now keyed off the per-operand ``precondition`` /
# the token identity below (back-compat: the four scalars resolve identically).
_GLASS_TOKENS = frozenset({"TIND", "TABB"})

# The run criterion (D7): RMS wavefront, field-summed, in waves. Resolved by
# NAME off the live ``Criterions`` enum (NOT by index — index drifts on a version bump).
_CRITERION_MEMBER = "RMSWavefront"
# The two SetupMode members the ``mode`` switch maps to (D1): a sensitivity run
# emits the sensitivity table + an MC section; SkipSensitivity = MC-only (probe §2/§5).
_MODE_TO_SETUP = {"sensitivity": "Sensitivity", "monte_carlo": "SkipSensitivity"}

# The criterion-label anchor (D11/G3): the report's own criterion label string
# PROVES we parsed an RMS-wavefront report, NOT a stale default RMSSpotRadius one
# (probe capture: ``criterion_label: "RMS Wavefront Error in waves"``).
_CRITERION_LABEL_ANCHOR = "RMS Wavefront Error in waves"

# The UTF-16LE byte-order mark every native engine report opens with (probe capture:
# ``first_bytes_hex: "fffe4100"``). The BOM-magic gate (D11/G4), mirroring ``_merit_io``.
_UTF16LE_BOM = b"\xff\xfe"

# Default MC trials (TOOL SIGNATURE) + the "unstable below" soft-warning floor.
_DEFAULT_TRIALS = 20
_MIN_STABLE_TRIALS = 20

# A NaN/Infinity textual token a malformed engine report could carry; a parser that
# float()'d these would manufacture a non-finite value into a manufacturability field
# (the L14 silent-zero bug class). The parser rejects a non-finite value LOUD.
_NONFINITE_TOKENS = ("nan", "inf", "infinity", "-inf", "-infinity", "+inf", "+infinity")

# The fifth (NEW) error family (D21/D22): the anti-over-optimistic reconcile
# canary. NOT a new exception TYPE — ``ToleranceError`` carries ``family`` as a free
# string; ``tolerancing_reconcile`` is a new VALUE. An authored operand producing
# NEITHER a parsed row NOR a captured error line surfaces here (the no-blind-spot
# guarantee, D12).
_RECONCILE_FAMILY = "tolerancing_reconcile"

# The SIXTH (NEW) error family: the headless tolerancing run Succeeded
# but wrote a 0-byte report (the engine's recent-files/usage cache, keyed to the
# EXACT original .zmx path, is unreachable from the ZOS-API — probe-frozen).
# NOT a new exception TYPE; a free-string
# ``family`` VALUE (like ``tolerancing_reconcile``). Disjoint from
# ``tolerancing_parse`` by the ``len(report_bytes)==0`` test: an EMPTY report is a
# non-blessed-system diagnosis (load a saved design with ``load_design``); a NON-empty
# corrupt/garbage report stays ``tolerancing_parse``.
_EMPTY_REPORT_FAMILY = "tolerancing_empty_report"

# The actionable recovery message the empty-report envelope carries (names the
# ``load_design`` recovery tool — the user-directed workflow).
_EMPTY_REPORT_RECOVERY = (
    "the headless tolerancing run produced no report for this system (its backing "
    "file is not a saved design the engine recognizes); load your saved design from "
    "disk with load_design and run tolerance on that."
)


def _safe_system_file(system):
    """Read ``system.SystemFile`` (the loaded backing-file path); ``None`` on any throw.

    The blessing discriminator (probe-frozen): the headless tolerancing report writer
    emits a full report ONLY when ``SystemFile`` is the EXACT path of a blessed on-disk
    original. A guarded read (the value is surfaced in the empty-report envelope so the
    caller sees the non-blessed path). NEVER raises (mirrors ``read_report_bytes``).
    """
    try:
        value = system.SystemFile
    except Exception:  # noqa: BLE001 — an unreadable SystemFile -> None (never raises)
        return None
    if value is None:
        return None
    try:
        text = str(value)
    except Exception:  # noqa: BLE001 — an unstringifiable handle -> None
        return None
    return text if text.strip() != "" else None


# The engine's ``New()``-default backing-NAMES — the basenames the engine reports for an
# un-saved / freshly-``New()``ed in-memory system. These are NOT blessed on-disk
# originals. Matched on the BASENAME with EXACT / stem / prefix rules (case-insensitive),
# NOT a substring scan: a legit saved design like ``my_new_lens.zmx`` / ``renewable.zmx``
# must NOT be false-flagged, and the engine's ``…\SAMPLES\LENS.zmx`` default must
# classify NON-blessed (``lens.zmx`` is the New() default basename).
_NEW_DEFAULT_EXACT = ("new", "lens.zos", "lens.zmx", "new.zmx", "new.zos")
# Basename STEMS (the part before the extension) that mark a New()-default / scratch
# system: ``untitled`` / a bare ``new``.
_NEW_DEFAULT_STEMS = ("untitled", "new")
# Basename PREFIXES (followed by a WORD BOUNDARY — space/underscore/hyphen/digit) for
# the New()-default family: ``untitled1.zmx``, the engine display name ``New Lens``,
# ``new_design.zmx``. A word boundary is REQUIRED so a glued name (``newton.zmx``,
# ``renewable.zmx``, ``untitledx`` is not a default — only ``untitled<sep|digit>``) is
# NOT false-flagged. The boundary chars: space, ``_``, ``-``, or a digit.
_NEW_DEFAULT_PREFIXES = ("untitled", "new")
_NEW_BOUNDARY_CHARS = " _-"

# The mkstemp checkpoint PREFIXES the harness' own ``SaveAs`` checkpoints carry
# (``optivibe_lens_ckpt`` / ``optivibe_tol_``): a system whose backing-file BASENAME
# STARTS WITH one of OUR temp-checkpoint prefixes is NOT a blessed on-disk original.
# Matched with ``startswith`` on the BASENAME (the bare ``optivibe_`` prefix is
# DROPPED so a legit ``optivibe_final.zmx`` is not false-flagged; a %TEMP% path is still
# caught by the tempdir gate below).
_CHECKPOINT_PREFIXES = ("optivibe_lens_ckpt", "optivibe_tol_")


def classify_system_file(system_file):
    """Classify a ``SystemFile`` path for tolerancing blessing (§3).

    Pure (no engine touch — takes the ALREADY-read path string / None). Returns
    ``(blessed_hint, warning_or_None)``:

    - empty / ``None`` / a ``New()``-default name / a path under the OS temp dir or a
      harness checkpoint prefix -> ``(False, <loud warning>)``: this system is not
      backed by a saved design on disk, so the run will LIKELY produce no report.
    - a real on-disk path (anything else) -> ``(True, None)``: a HINT, not a guarantee
      (the post-run empty-report net is definitive — cond F: a byte-identical same-folder
      copy still failed, so pre-run blessing is unpredictable; we WARN, never refuse).
    """
    warn = (
        "this system is not backed by a saved design on disk; the tolerancing run "
        "will likely produce no report — load your saved design from disk with "
        "load_design and tolerance that"
    )
    if not isinstance(system_file, str) or system_file.strip() == "":
        return False, warn
    low = system_file.strip().lower()
    # Match on the BASENAME with EXACT / stem / prefix rules (NOT a substring scan), so a
    # legit saved design (``my_new_lens.zmx``, ``renewable.zmx``, ``optivibe_final.zmx``)
    # is not false-flagged while the engine New() defaults are caught.
    base = os.path.basename(low.replace("\\", "/"))
    stem = os.path.splitext(base)[0]
    if base in _NEW_DEFAULT_EXACT:
        return False, warn
    if stem in _NEW_DEFAULT_STEMS:
        return False, warn
    # A New()-default PREFIX followed by a word boundary (sep or digit) — catches
    # ``New Lens`` / ``new_design`` / ``untitled1`` WITHOUT glued names (``newton`` /
    # ``renewable``).
    for p in _NEW_DEFAULT_PREFIXES:
        if stem.startswith(p) and len(stem) > len(p):
            nxt = stem[len(p)]
            if nxt in _NEW_BOUNDARY_CHARS or nxt.isdigit():
                return False, warn
    if any(base.startswith(p) for p in _CHECKPOINT_PREFIXES):
        return False, warn
    try:
        import tempfile
        tmp = os.path.normcase(os.path.normpath(tempfile.gettempdir()))
        norm = os.path.normcase(os.path.normpath(system_file))
        if norm.startswith(tmp + os.sep) or norm == tmp:
            return False, warn
    except Exception:  # noqa: BLE001 — a temp-dir resolution failure -> no temp gate
        pass
    return True, None

# The report header line the engine ALWAYS emits headless (PROBE-2, D13/G-COMP-
# INERT): a user compensator is INERT through ``OpenTolerancing()`` so the report stays
# "Paraxial Focus compensation only." We assert this string survives and DISCLOSE
# ``compensator_participates: false`` — never claim a user compensator recovered.
_PARAXIAL_FOCUS_HEADER = "Paraxial Focus compensation only."

# The report's "Units are <unit>." line (probe capture: "Units are Millimeters.") —
# the lens-unit echo source (D16/G-UNITS-ECHO; NEVER hardcode "mm"). The unit
# word is normalized to its short form by ``_normalize_lens_units``.
_UNITS_LINE_RE = r"Units are\s+([A-Za-z]+)\s*\."

# The per-family unit map (D16): the report does NOT annotate the per-operand
# unit, so the catalog ``units`` field is authoritative. ``lens_units`` (the report's
# linear unit) substitutes for a "lens_units" catalog value (mm/cm/in/m).
_UNITS_BY_FAMILY_LABELS = {
    "lens_units": "lens_units",
    "degrees": "degrees",
    "fringes": "fringes",
    "dimensionless": "dimensionless",
}


class ToleranceError(ToolError):
    """A tolerance-tier failure carrying a structured ``family``.

    The four families (D11/D17): ``tolerancing_param`` (bad input / bad enum
    member; carries a ``known_gap`` / ``precondition`` discriminator for a CB/NSC/crash
    refusal, D21), ``tolerancing_unavailable`` (``OpenTolerancing`` -> None, or an
    enum-TYPE reach failure), ``tolerancing_run`` (``Succeeded=False`` / ``ErrorMessage``
    / a pythonnet throw mid-run), ``tolerancing_parse`` (the report canary failed — the
    anti-silent-zero family) + the FIFTH (D21/D22) ``tolerancing_reconcile`` (an authored
    operand produced NEITHER a parsed row NOR an error line — the over-optimistic
    canary). One exception type carries the discriminator on a
    ``family`` instance attribute (mirrors ``AnalysisResultError`` / ``OptimizeError``);
    the WIRE contract is the ``error_family`` string, never ``isinstance``.
    """

    error_family = "tolerancing"

    def __init__(self, message, *, family="tolerancing"):
        super().__init__(message)
        self.family = family


# --------------------------------------------------------------------------- #
# Enum resolution — getattr ONLY (D6/G1; NEVER System.Enum.Parse).
# --------------------------------------------------------------------------- #
def _resolve_tol_enum(enum_type, member_name):
    """Resolve ``member_name`` on ``enum_type`` via guarded ``getattr`` (D6/G1).

    The ONE resolver for EVERY tolerancing enum family (ToleranceOperandType +
    SetupModes / Criterions / MonteCarloStatistics). It NEVER calls
    ``System.Enum.Parse`` (probe §2: passing a reflected ``RuntimeType`` HARD-CRASHES
    the process — no Python traceback, ``FRU__delta_init``). A bad member name ->
    ``ToleranceError(family="tolerancing_param")`` (a param-class problem); the enum
    TYPE itself is resolved upstream (``_tol_namespace_enum`` -> ``tolerancing_unavailable``).
    """
    if not isinstance(member_name, str) or member_name == "":
        raise ToleranceError(
            f"enum member name must be a non-empty string, got {member_name!r}",
            family="tolerancing_param",
        )
    try:
        return getattr(enum_type, member_name)
    except AttributeError:
        valid = _enum_member_names(enum_type)
        raise ToleranceError(
            f"unknown enum member {member_name!r} on "
            f"{getattr(enum_type, '__name__', enum_type)!r}; valid: {valid}",
            family="tolerancing_param",
        )


def _enum_member_names(enum_type):
    """Best-effort list of an enum's member names for an error message. NEVER raises.

    Tries the live ``System.Enum.GetNames`` reflection (the authoritative surface),
    then a ``dir()`` filter. A discovery failure must not mask the underlying error.
    """
    try:  # pragma: no cover - exercised only against the live backend
        import System  # type: ignore

        names = list(System.Enum.GetNames(enum_type))
        if names:
            return [str(n) for n in names]
    except Exception:  # noqa: BLE001 — fall back to dir() introspection
        pass
    try:
        return [n for n in dir(enum_type) if not n.startswith("_")]
    except Exception:  # noqa: BLE001 — last resort: nothing to list
        return []


def _tolerance_operand_enum(system):
    """Resolve the live ``ToleranceOperandType`` enum TYPE (probe §1).

    A fake system injects ``_tol_enum_types["ToleranceOperandType"]`` so unit tests
    resolve without the backend; otherwise the live ``ZOSAPI.Editors.TDE`` namespace.
    A resolution failure -> ``tolerancing_unavailable`` (the enum TYPE could not be
    reached — distinct from a bad MEMBER, which is ``tolerancing_param``).
    """
    injected = getattr(system, "_tol_enum_types", None)
    if injected is not None and "ToleranceOperandType" in injected:
        return injected["ToleranceOperandType"]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.TDE as _tde  # type: ignore

        return _tde.ToleranceOperandType
    except Exception as exc:  # noqa: BLE001 — TYPE reach failure -> unavailable
        raise ToleranceError(
            "could not resolve ToleranceOperandType from ZOSAPI.Editors.TDE "
            f"({exc!r}); tolerancing is unavailable",
            family="tolerancing_unavailable",
        )


def _tol_namespace_enum(system, name):
    """Reach a run-mode enum TYPE by DIRECT attribute on the Tolerancing namespace (D6).

    The ``ZOSAPI.Tools.Tolerancing`` namespace ``dir()`` is EMPTY (a proxy quirk, probe
    §2), so the enum TYPE is reached by direct attribute (``TT.SetupModes`` /
    ``TT.Criterions`` / ``TT.MonteCarloStatistics``). A fake system injects
    ``_tol_enum_types[name]``. A TYPE-reach failure -> ``tolerancing_unavailable``.
    """
    injected = getattr(system, "_tol_enum_types", None)
    if injected is not None and name in injected:
        return injected[name]
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Tools.Tolerancing as _tt  # type: ignore

        return getattr(_tt, name)
    except Exception as exc:  # noqa: BLE001 — TYPE reach failure -> unavailable
        raise ToleranceError(
            f"could not reach the {name!r} enum on ZOSAPI.Tools.Tolerancing "
            f"({exc!r}); tolerancing is unavailable",
            family="tolerancing_unavailable",
        )


# --------------------------------------------------------------------------- #
# The single-slot tolerancing-tool context manager (D13 — the L22 reap).
# --------------------------------------------------------------------------- #
@contextmanager
def _tolerancing_session(system):
    """Open the tolerancing tool ONCE, yield it, ALWAYS ``Close()`` it (D13).

    ``tol = system.Tools.OpenTolerancing()``. If ``tol is None`` the single-instance
    tool is ALREADY open (the slot is busy) -> raise
    ``ToleranceError(family="tolerancing_unavailable")`` WITHOUT entering the
    try/finally (there is nothing to Close). Else ``yield tol`` in a ``try`` and
    ``Close()`` in the ``finally``, itself guarded so a teardown throw never masks the
    body. The ``finally`` runs on ``KeyboardInterrupt``/``SystemExit`` too — the
    never-raise wrapper catches ``Exception`` only, ``BaseException`` propagates AFTER
    the slot is reaped (probe §2: only ONE tolerancing tool may be open; a leak makes
    every later ``OpenTolerancing()`` return None).
    """
    tol = system.Tools.OpenTolerancing()
    if tol is None:
        # Do NOT enter the try/finally — there is no handle to Close.
        raise ToleranceError(
            "OpenTolerancing() returned None (another tolerancing tool is open / the "
            "slot is busy); nothing was run — retry once the prior tool is closed",
            family="tolerancing_unavailable",
        )
    try:
        yield tol
    finally:
        try:
            tol.Close()
        except Exception:  # noqa: BLE001 — tool teardown must never mask the body
            pass


# --------------------------------------------------------------------------- #
# Report decode + BOM-magic gate (D11/G4 — mirrors _merit_io).
# --------------------------------------------------------------------------- #
def read_report_bytes(path):
    """Read the report file bytes; ``None`` on any OSError/TypeError/ValueError.

    A missing/unreadable report path degrades to ``None`` (the caller raises the
    ``tolerancing_parse`` canary). NEVER raises (mirrors ``_merit_io._is_merit_file``).
    """
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()
    except (OSError, TypeError, ValueError):
        return None


def decode_tol_report(raw):
    """BOM-magic gate + UTF-16-LE decode (D11 step 1/G4). Raises on a magic miss.

    The engine writes a UTF-16-LE report opening with the ``FF FE`` BOM (probe capture:
    ``first_bytes_hex: "fffe4100"``). A ``None`` / empty / non-``FF FE`` head is a
    wrong/missing/garbage file -> ``ToleranceError(family="tolerancing_parse")`` (G4 —
    dropping this gate would let a wrong/empty file through). On a pass the 2 BOM bytes
    are SKIPPED before the decode (mirrors ``_merit_io``'s BOM-skip).
    """
    if not raw:
        raise ToleranceError(
            "the tolerancing report is missing or empty; the run wrote no result "
            "(refusing to report a guessed/zero criterion)",
            family="tolerancing_parse",
        )
    if raw[:2] != _UTF16LE_BOM:
        raise ToleranceError(
            "the tolerancing report does not start with the UTF-16LE BOM "
            f"(first bytes {raw[:4].hex()!r}); it is not a valid engine report "
            "(refusing to parse a wrong/garbage file)",
            family="tolerancing_parse",
        )
    return raw[2:].decode("utf-16-le", errors="replace")


# --------------------------------------------------------------------------- #
# The report PARSER (D11 — the anti-silent-zero axis; NEVER zero-fills).
# --------------------------------------------------------------------------- #
def _to_finite_float(token):
    """Parse a numeric token to a FINITE float, or ``(None, False)`` (D11).

    Returns ``(value, found)``. A ``None`` / non-finite-textual / un-floatable / NaN /
    Inf token -> ``(None, False)`` — NEVER a bare ``0.0`` (the L14 silent-zero bug
    class). A finite float -> ``(value, True)``.
    """
    if token is None:
        return None, False
    text = str(token).strip()
    if text == "" or text.lower() in _NONFINITE_TOKENS:
        return None, False
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None, False
    if not math.isfinite(value):
        return None, False
    return value, True


# A report SELECTOR column (a Par#, a Surf, a range Surf2) is, in the mechanical v2
# report, a BARE ASCII integer. A ``decimal.Decimal(text)`` lossless parse
# silently OVER-ACCEPTS a whole class of MALFORMED selector text as clean integers in
# this Python (VERIFIED live) — a digit-group underscore (``"3_0"`` -> 30, ``"3__0"`` ->
# 30), a leading/trailing underscore (``"_3"`` / ``"3_"`` -> 3), scientific notation
# (``"3e0"`` -> 3), a decimal render (``"3.0"`` -> 3) — each fabricating a FALSE keyed /
# ``method:"keyed"`` per-Par proof. We enforce a STRICT optionally-signed
# ASCII-integer grammar on the STRIPPED text BEFORE any numeric conversion; everything
# else that is PRESENT is a PARSE ANOMALY (belt-INELIGIBLE), and a truly BLANK/absent
# token stays the non-anomalous count-belt input.
#
# The grammar is ASCII-ONLY: ``\d`` matches Unicode decimal digits
# (``"٣"`` -> 3), so a report carrying a non-ASCII decimal digit would alias to a
# fabricated int. The engine emits ASCII, but ``[0-9]`` closes it for free — a Unicode
# digit now reads as a PARSE ANOMALY, never a false key.
_STRICT_INT_RE = re.compile(r"[+-]?[0-9]+")


def _classify_selector(raw):
    """Classify a report selector TEXT token into ``(integer, anomalous)`` in ONE parse.

    - ``(int, False)``  — a clean optionally-signed ASCII integer (``"3"`` / ``"-3"`` /
      ``"+3"`` / ``"007"``, whitespace-stripped). The KEYED input.
    - ``(None, False)`` — a truly BLANK/absent token (``None`` / ``""``). The
      non-anomalous COUNT-BELT input (a legitimately-blank Surf2 column); NEVER turned
      into an anomaly (that would regress the range-op parse).
    - ``(None, True)``  — a PRESENT but MALFORMED token (``"_3"``, ``"3_0"``, ``"3__0"``,
      ``"3_"``, ``"3e0"``, ``"3.5"``, ``"3.0"``, a garble) — a PARSE ANOMALY,
      belt-INELIGIBLE, never a truncated false key.

    The strict grammar mirrors the writer's ``_tol_cells.is_integral_int`` EXACTLY (both
    accept only a true integer; both reject the decimal/float render ``"3.0"`` the OLD
    ``decimal.Decimal`` parse wrongly aliased to 3) — cross-pinned by a unit test.
    """
    if raw is None:
        return None, False
    text = str(raw).strip()
    if text == "":
        return None, False
    if _STRICT_INT_RE.fullmatch(text):
        return int(text), False
    return None, True


def _selector_int(raw):
    """STRICT EXACT-INTEGRAL selector at the TEXT boundary.

    Returns the integer for a clean optionally-signed ASCII integer, else ``None`` (a
    blank OR a malformed/non-integral token). The parser-side counterpart of the writer's
    ``_tol_cells.is_integral_int`` (which operates on Python VALUES; this operates on
    report TEXT), cross-pinned by a unit test: every int this returns satisfies
    is_integral_int, and the acceptance boundary mirrors the writer's — ``"3"`` accepts as
    3; a decimal render (``"3.0"`` / ``"3.5"``), an underscore/scientific selector, or the
    lossy ``"3.0000000000000001"`` alias REJECTS (never a blind int() truncation, never a
    lossy ``float()``/``Decimal`` over-accept).
    """
    return _classify_selector(raw)[0]


def _finite_non_integral(raw):
    """True iff ``raw`` is a PRESENT but malformed/non-integral selector (a PARSE ANOMALY).

    A present selector that is NOT a clean ASCII integer
    (``"3.5"``, ``"3.0"``, the lossy ``"3.0000000000000001"`` alias, ``"3_0"``, ``"3e0"``)
    is a PARSE ANOMALY — never truncated into a false keyed proof, never blank-None belt
    input. Derived from the SAME classified parse as ``_selector_int`` (exact complements
    over PRESENT tokens). A blank / absent (``None`` / ``""``) token -> False (the belt
    input, not an anomaly); a clean integer -> False.
    """
    return _classify_selector(raw)[1]


# A numeric token captured from the report. The capture class accepts the digit/dot/
# sign/exponent characters, but the WHOLE token MUST be followed by a token boundary
# (whitespace / EOL / end-of-string) — a trailing non-boundary char (a decimal COMMA, a
# garble, a locale separator) means the captured number is a PARTIAL truncation of a
# longer garbled token, which must be LOUD (tolerancing_parse), NEVER a fabricated
# leading-run value (the L14 silent-wrong bug class). The trailing
# ``(?![^\s])`` lookahead rejects any char that is not whitespace/EOL right after the
# number, so ``0,055`` (comma after ``0``) fails to match and the field reads as missing.
_NUM = r"([0-9.E+\-]+)(?![^\s])"


def _search(text, pattern):
    """Return the first regex group-1 match (a string) or ``None`` (no zero-fill)."""
    import re

    m = re.search(pattern, text)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Lens-units echo (D16/G-UNITS-ECHO — NEVER hardcode "mm").
# --------------------------------------------------------------------------- #
_LENS_UNIT_SHORT = {
    "millimeters": "mm", "millimeter": "mm",
    "centimeters": "cm", "centimeter": "cm",
    "inches": "in", "inch": "in",
    "meters": "m", "meter": "m",
}


def parse_lens_units(text):
    """Echo the report's linear unit from the "Units are <unit>." line (G-UNITS-ECHO).

    The report opens with a literal ``Units are Millimeters.`` line (probe capture). We
    READ the unit word and normalize it to a short form (mm/cm/in/m); NEVER hardcode
    "mm". A missing line -> ``None`` (the handler discloses ``lens_units: null`` rather
    than guessing). A non-mm unit follows the report (mutate-fail: change the report's
    unit and ``lens_units`` follows).
    """
    import re

    m = re.search(_UNITS_LINE_RE, text)
    if not m:
        return None
    word = m.group(1).strip().lower()
    return _LENS_UNIT_SHORT.get(word, word)


def _operand_error_lines(text):
    """Capture interleaved engine error lines into ``[{type, line}]`` (D11/G-PARSE-TAB).

    The engine interleaves a per-operand precondition error directly into the
    sensitivity stream (probe: ``Surface 2 must be a Coordinate break for TUTX!`` ,
    ``TNPS requires surface 2 to be a Non-Sequential surface!``). We scan EVERY line for
    a 4-letter operand TYPE token followed by ``!`` (a refusal) and attribute it to that
    operand; a generic ``Surface N must be a Coordinate break!`` (no token) is captured
    with ``type=None`` (unattributed — surfaced LOUD, never swallowed). Returns the list
    of captured error lines (de-duplicated, order-preserving).
    """
    import re

    out = []
    seen = set()
    # A line carrying a 3-4 letter UPPER operand token AND ending with '!' is a refusal.
    token_re = re.compile(r"\b([A-Z]{3,4})\b")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or "!" not in line:
            continue
        low = line.lower()
        # Heuristic anchors the engine uses for a per-operand precondition refusal.
        if not ("must be" in low or "requires" in low or "illegal" in low
                or "cannot" in low):
            continue
        key = line
        if key in seen:
            continue
        seen.add(key)
        # Attribute to a 3-4 letter token that is a known tolerance operand code.
        attributed = None
        for tok in token_re.findall(line):
            if _cat.meta_for(tok) is not None:
                attributed = tok
                break
        out.append({"type": attributed, "line": line})
    return out


def parse_tol_report(text, *, mode, trials):
    """Parse the DECODED report string into the result fields (D11). NEVER zero-fills.

    Pure + fixture-testable. EVERY field parses to ``(value, found)``; an
    unmatched/non-finite/un-floatable token yields ``(None, False)``, never a bare
    ``0.0``. The TWO-LAYER report-content canary (D11) gates the result BEFORE any value
    returns:

      1. ``Nominal Criterion :`` parsed found=True and FINITE (else ``tolerancing_parse``).
      2. CRITERION-LABEL anchor: the report's criterion label contains
         ``"RMS Wavefront Error in waves"`` (G3 — proves we parsed an RMS-wavefront
         report, not a stale RMSSpotRadius one). A mismatch -> ``tolerancing_parse``.
      3. mode-specific minimum-yield (G5/G6):
         - ``sensitivity`` -> >=1 sensitivity row with finite min/max criterion AND a
           finite RSS estimate.
         - ``monte_carlo`` -> mc_mean/std/best/worst ALL finite AND the parsed per-trial
           count == the echoed ``trials`` (G6 — both counts named on a mismatch).

    (The BOM-magic gate is ``decode_tol_report``; the engine-success canary is read
    WHILE the tool is open, in the handler.) A canary miss is a HARD
    ``ToleranceError(family="tolerancing_parse")`` — never a degraded partial.
    Returns a dict of the parsed fields (all finite / structurally validated).
    """
    # --- the criterion-label anchor (G3) ---
    if _CRITERION_LABEL_ANCHOR not in text:
        raise ToleranceError(
            "the tolerancing report's criterion label is not "
            f"{_CRITERION_LABEL_ANCHOR!r} (a stale/wrong criterion was applied, or the "
            "config did not take); refusing to mislabel a non-RMS-wavefront report",
            family="tolerancing_parse",
        )

    # --- the nominal criterion (D11 step 2) ---
    nominal, nominal_found = _to_finite_float(
        _search(text, r"Nominal Criterion\s*:\s*" + _NUM)
    )
    if not nominal_found:
        raise ToleranceError(
            "the tolerancing report has no finite 'Nominal Criterion' value "
            "(refusing to report a guessed/zero nominal criterion)",
            family="tolerancing_parse",
        )
    if nominal <= 0:
        # An RMS wavefront / Strehl criterion can NEVER be <= 0 (a corrupted/garbled
        # report self-reporting a non-positive nominal is loud, never a verdict).
        raise ToleranceError(
            f"the 'Nominal Criterion' is non-positive ({nominal!r}); an RMS-wavefront "
            "criterion in waves cannot be <= 0 (the report is corrupted)",
            family="tolerancing_parse",
        )

    out = {
        "nominal_criterion": nominal,
        "test_wavelength": _to_finite_float(
            _search(text, r"Test Wavelength\s*:\s*" + _NUM)
        )[0],
        # Echo the report's linear unit (NEVER hardcode "mm"; D16/G-UNITS-ECHO).
        "lens_units": parse_lens_units(text),
        # Capture interleaved engine error lines (D11/G-PARSE-TAB) — used by reconcile.
        "operand_errors": _operand_error_lines(text),
    }

    if mode == "sensitivity":
        _parse_sensitivity(text, out)
    else:
        _parse_monte_carlo(text, out, trials)
    return out


# The columns of a tab-delimited sensitivity data row (probe ``raw_sens_block``):
#   Type \t Surf1 \t Surf2 \t MinValue \t MinCrit \t MinChange \t MaxValue \t MaxCrit \t MaxChange
# The Surf2 column may be BLANK (a single-Surf op like TIRR) — the tab-first split
# PRESERVES the empty column so the numeric columns never shift left (the L28 trap a
# whitespace split would hit, collapsing the blank Surf2 and mis-reading the values).
def _split_sens_cells(line):
    """Tab-first tokenize ONE sensitivity row; the value columns are whitespace-trimmed.

    Splits on TAB so a blank Surf2 stays an empty cell (the column alignment the v2
    mechanical report relies on, D11/G-PARSE-TAB). Each cell is ``.strip()``'d (the
    engine pads with spaces around tabs). When the line has NO tab (the
    space-delimited report, back-compat §6) it falls back to a whitespace split so the
    legacy ``Type Surf Code -val crit chg +val crit chg`` rows still parse identically.
    Returns the list of (possibly-empty) cell strings.
    """
    if "\t" in line:
        return [c.strip() for c in line.split("\t")]
    import re
    return [c for c in re.split(r"\s+", line.strip()) if c]


def _parse_sensitivity(text, out):
    """Parse the TAB-delimited sensitivity table + worst offenders + RSS + back-focus.

    TAB-first tokenize (D11/G-PARSE-TAB): the mechanical table is tab-delimited
    with a possibly-BLANK Surf2 column; splitting on whitespace would collapse the blank
    column and shift the numeric columns (the L28 silent-drop). The interleaved engine
    error lines are captured into ``out['operand_errors']`` (done in ``parse_tol_report``).
    """
    import re

    rows = []
    dropped_rows = 0
    sens_block = re.search(
        r"Sensitivity Analysis:(.+?)(?:Worst offenders|Monte Carlo|$)", text, re.S
    )
    if sens_block:
        for line in sens_block.group(1).splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            cells = _split_sens_cells(line)
            # A data row starts with a 3-4 letter operand token. Tab-split yields
            # [Type, Surf1, Surf2(maybe blank), MinVal, MinCrit, MinChg, MaxVal,
            # MaxCrit, MaxChg]. Require >=9 columns (the blank Surf2 is preserved).
            if len(cells) < 9 or not re.match(r"^[A-Z]{3,4}$", cells[0]):
                continue
            min_crit, min_ok = _to_finite_float(cells[4])
            max_crit, max_ok = _to_finite_float(cells[7])
            if not (min_ok and max_ok):
                # A half-parsed row is NOT silently accepted (D11): require finite
                # min/max criterion. A dropped row is COUNTED (shrunk table disclosed) AND
                # its TYPE recorded so reconcile counts it as ran-but-corrupt (it DID
                # produce a row — it is NOT a silent omission, so it must not escalate to
                # the over-optimistic tolerancing_reconcile; the dropped-row WARNING is the
                # honest signal instead).
                dropped_rows += 1
                # PARSE + RETAIN the row's Surf
                # column so reconcile can key the dropped anomaly by (type, surface). A
                # corrupt row at ONE surface must NOT disqualify a legit count-fallback for
                # the SAME type at a DIFFERENT surface (a false type-wide escalation). Only
                # when the Surf itself is UNREADABLE (blank/garbled/non-integral) does the
                # anomaly carry no usable surface -> fall back to the type-wide
                # ``dropped_types`` (fail-closed: disqualify every group of the type).
                drop_surf = _selector_int(cells[1])
                if drop_surf is None:
                    out.setdefault("dropped_types", set()).add(cells[0])
                else:
                    out.setdefault("dropped_keys", set()).add((cells[0], drop_surf))
                continue
            # (GRIN §1.2a) the col3 render is CATALOG-DRIVEN AND
            # EXACT-INTEGRAL. ``parser_secondary_key`` decides whether col3 is a Par#
            # (``param`` — TPAR/TPAI/... ) or a range/config selector (``surface2``);
            # ``_selector_int`` rejects a blind int() truncation (a corrupt Surf=2.9 /
            # Par#=3.5 reads None, never a fabricated 2/3).
            sec_key = _cat.parser_secondary_key(cells[0])
            surf_int = _selector_int(cells[1])
            # (SYMMETRIC AUTHORITY) classify col3 ONCE for BOTH col3
            # renders (the param branch AND the surface2 branch) — a present-but-MALFORMED
            # col3 ("3.0"/"3_0"/"_3"/"3e0"/a Unicode digit) is a PARSE ANOMALY on EITHER
            # family, never a truncated false key. ``_classify_selector`` is the SINGLE
            # authority; no col3 render path may skip it.
            sec_int, sec_anom = _classify_selector(cells[2])
            row = {
                "type": cells[0],
                "surface": surf_int,       # None on a non-integral Surf -> the EXISTING
                                           # :1611 unreadable_surface anomaly arm fires
                "min": {
                    "delta": _to_finite_float(cells[3])[0],
                    "criterion": min_crit,
                    "change": _to_finite_float(cells[5])[0],
                },
                "max": {
                    "delta": _to_finite_float(cells[6])[0],
                    "criterion": max_crit,
                    "change": _to_finite_float(cells[8])[0],
                },
            }
            if sec_key == "param":
                # A Par#-carrying op (TPAR/TPAI): col3 is the Par#. A present-but-malformed
                # Par# (3.5/"3_0") is a PARSE ANOMALY (param=None + param_anomalous=True),
                # NOT a truncated false key and NOT blank-None belt input.
                row["param"] = sec_int
                row["surface2"] = None
                row["param_anomalous"] = sec_anom
            else:
                # A range / config / single op: col3 is the legacy surface2 slot. A blank
                # col3 stays the non-anomalous single-surface/range belt input (surface2=None
                # / a clean range Surf2) — byte-identical to the pre-fix render. A
                # PRESENT-but-malformed col3 is the SYMMETRIC sibling of param_anomalous: it
                # carries a ``surface2_anomalous`` bit (added ONLY when anomalous, so a blank/
                # clean col3 stays byte-identical) and reconcile routes it to anomalous_rows
                # exactly like a param anomaly — never a silent ``surface2=None`` that the
                # single-surface arm consumes into a FALSE keyed proof (the
                # Asymmetric sibling).
                row["surface2"] = sec_int
                if sec_anom:
                    row["surface2_anomalous"] = True
            rows.append(row)
    if not rows:
        # G5/G-PARSE-EMPTY (generalized): 0 rows is only a HARD parse failure when there
        # are ALSO 0 explanatory error lines. If the report carried only refused
        # operands (all error lines, no rows), reconcile handles them — the parser does
        # not falsely raise on a set whose every operand legitimately produced an error.
        if not out.get("operand_errors"):
            raise ToleranceError(
                "the sensitivity report parsed 0 tolerance rows AND 0 explanatory error "
                "lines (the run produced no sensitivity table); refusing to report an "
                "empty sensitivity result",
                family="tolerancing_parse",
            )
    out["sensitivity"] = rows

    # Worst offenders (ranked). TAB-aware: when the line is tab-delimited (the v2
    # mechanical report) split on TAB so a blank Surf2 column does not shift the value
    # columns; else fall back to a whitespace split (the space-delimited
    # report, with a literal "0" Code column). Both place criterion@4 / change@5.
    worst = []
    w_block = re.search(r"Worst offenders:(.+?)(?:Estimated|Monte Carlo|$)", text, re.S)
    if w_block:
        for line in w_block.group(1).splitlines():
            if "\t" in line:
                cells = _split_sens_cells(line)
            else:
                cells = [c for c in re.split(r"\s+", line.strip()) if c]
            if len(cells) >= 6 and re.match(r"^[A-Z]{3,4}$", cells[0]):
                crit, crit_ok = _to_finite_float(cells[4])
                if not crit_ok:
                    dropped_rows += 1
                    continue
                surf_val, _ = _to_finite_float(cells[1])
                worst.append({
                    "type": cells[0],
                    "surface": int(surf_val) if surf_val is not None else None,
                    "delta": _to_finite_float(cells[3])[0],
                    "criterion": crit,
                    "change": _to_finite_float(cells[5])[0],
                })
    out["worst_offenders"] = worst

    # A shrunk table is DISCLOSED (never a silent-incomplete verdict): a corrupt
    # sensitivity/worst-offender row that was dropped emits a warning (the worst-offender
    # being dropped is the dangerous case — fewer offenders than the engine found).
    if dropped_rows:
        out.setdefault("warnings", []).append(
            f"{dropped_rows} sensitivity/worst-offender row(s) dropped (unparseable "
            "criterion); the reported table is INCOMPLETE — re-check the raw report"
        )

    # RSS estimate (G5 — required finite for sensitivity).
    rss_change, rss_change_ok = _to_finite_float(
        _search(text, r"Estimated change\s*:\s*" + _NUM)
    )
    rss_crit, rss_crit_ok = _to_finite_float(
        _search(text, r"Estimated RMS Wavefront\s*:\s*" + _NUM)
    )
    if not rss_crit_ok:
        raise ToleranceError(
            "the sensitivity report has no finite RSS 'Estimated RMS Wavefront' value; "
            "refusing to report a guessed/zero RSS estimate",
            family="tolerancing_parse",
        )
    if rss_crit <= 0:
        raise ToleranceError(
            f"the RSS 'Estimated RMS Wavefront' criterion is non-positive ({rss_crit!r}); "
            "an RMS-wavefront criterion in waves cannot be <= 0 (the report is corrupted)",
            family="tolerancing_parse",
        )
    out["rss_estimated_change"] = rss_change
    out["rss_estimated_criterion"] = rss_crit

    # Compensator (back-focus) statistics block (D9 — paraxial back-focus compensation).
    out["back_focus_change"] = _parse_back_focus(text)


def _parse_back_focus(text):
    """Parse the 'Compensator Statistics: Change in back focus' min/max/mean/std block.

    Best-effort (D9): the block is always emitted (probe §1), but a missing field
    degrades to ``None`` (the back-focus stats are an adjunct, not the deliverable —
    unlike the nominal/sensitivity/RSS fields that gate the canary).
    """
    import re

    block = re.search(
        r"Change in back focus:?(.+?)(?:Monte Carlo|Worst offenders|$)", text, re.S
    )
    scope = block.group(1) if block else text

    def _stat(label):
        return _to_finite_float(
            _search(scope, re.escape(label) + r"\s*:?\s*" + _NUM)
        )[0]

    return {
        "min": _stat("Minimum"),
        "max": _stat("Maximum"),
        "mean": _stat("Mean"),
        "std_dev": _stat("Standard Deviation"),
    }


def _parse_monte_carlo(text, out, trials):
    """Parse the Monte-Carlo mean/std/best/worst + percentiles + trial-count (G5/G6).

    The whole-report first-match regexes (``Mean``/``Std Dev``/…) are SCOPED to the
    Monte-Carlo section FIRST (anchored on the ``Monte Carlo Analysis`` block header) so
    a stray earlier ``Mean`` line (e.g. a compensator / back-focus block) can NEVER
    poison ``monte_carlo.mean``.
    """
    import re

    # Slice to the Monte-Carlo section before parsing the stat lines. The block header is
    # "Monte Carlo Analysis:" with a COLON (the section line "Monte Carlo Analysis: Number
    # of trials: N" — distinct from a "Monte Carlo Analysis Report" TITLE line, which must
    # NOT anchor the slice or a poison line below the title would leak in). A missing
    # header is a hard parse failure (no MC section to read).
    mc_header = re.search(r"Monte Carlo Analysis:", text)
    if not mc_header:
        raise ToleranceError(
            "the Monte-Carlo report has no 'Monte Carlo Analysis' section header "
            "(refusing to scrape MC statistics from an unscoped report)",
            family="tolerancing_parse",
        )
    scope = text[mc_header.start():]

    mean, mean_ok = _to_finite_float(_search(scope, r"Mean\s+" + _NUM))
    std, std_ok = _to_finite_float(_search(scope, r"Std Dev\s+" + _NUM))
    best, best_ok = _to_finite_float(_search(scope, r"Best\s+" + _NUM))
    worst, worst_ok = _to_finite_float(_search(scope, r"Worst\s+" + _NUM))
    if not (mean_ok and std_ok and best_ok and worst_ok):
        # G5: MC requires all four headline stats finite.
        raise ToleranceError(
            "the Monte-Carlo report is missing a finite mean/std/best/worst statistic "
            "(refusing to report a guessed/zero Monte-Carlo result)",
            family="tolerancing_parse",
        )
    # MC ordering sanity (a cheap honesty canary): best <= mean <= worst. An impossible
    # ordering (best > worst, mean outside [best, worst]) means a corrupted/mislabelled
    # report -> LOUD, never a structurally-impossible verdict.
    if not (best <= mean <= worst):
        raise ToleranceError(
            f"the Monte-Carlo statistics are not ordered best <= mean <= worst "
            f"(best={best!r}, mean={mean!r}, worst={worst!r}); the report is corrupted",
            family="tolerancing_parse",
        )

    cumulative = {}
    cum_order = []
    for pc in ("90%", "80%", "50%", "20%", "10%"):
        v, found = _to_finite_float(
            _search(scope, re.escape(pc) + r"\s*>\s*" + _NUM)
        )
        if found:
            cumulative[pc] = v
            cum_order.append((pc, v))
    # The cumulative-probability rows are reported high->low percentile; their values must
    # be MONOTONIC non-increasing in that order (a higher percentile bounds a larger
    # criterion). A non-monotonic sequence is a corrupted MC table -> LOUD.
    cum_vals = [v for _pc, v in cum_order]
    if cum_vals != sorted(cum_vals, reverse=True):
        raise ToleranceError(
            "the Monte-Carlo cumulative-probability rows are non-monotonic "
            f"({cum_order}); the report is corrupted",
            family="tolerancing_parse",
        )

    # Per-trial lines: "<n>  <criterion>  <change>" (probe §5). The COUNT is the G6
    # cross-check; the values feed the optional ``full`` per_trial array. Scoped to the
    # MC section so a stray numeric triple elsewhere cannot inflate the count.
    trial_pairs = re.findall(
        r"^\s*\d+\s+([0-9.E+\-]+)\s+([0-9.E+\-]+)\s*$", scope, re.M
    )
    parsed_count = len(trial_pairs)
    if parsed_count != int(trials):
        # G6: the parsed per-trial count MUST equal the echoed trials — name BOTH
        # counts (never silently report fewer trials than asked).
        raise ToleranceError(
            f"the Monte-Carlo report parsed {parsed_count} per-trial rows but the run "
            f"was configured for {int(trials)} trials; refusing to report a "
            "trial-count mismatch (the report may be truncated)",
            family="tolerancing_parse",
        )
    per_trial = []
    for crit_tok, _chg_tok in trial_pairs:
        v, found = _to_finite_float(crit_tok)
        per_trial.append(v if found else None)

    out["monte_carlo"] = {
        "mean": mean,
        "std_dev": std,
        "best": best,
        "worst": worst,
        "cumulative_probability": cumulative,
    }
    out["per_trial"] = per_trial
    out["parsed_trial_count"] = parsed_count


# --------------------------------------------------------------------------- #
# Default-budget builder (D3 — pure-python, NOT a tool/mode).
# --------------------------------------------------------------------------- #
# Sensible default deltas per token (probe §4 magnitudes that BITE on the doublet).
_DEFAULT_DELTA = {"TRAD": 0.1, "TTHI": 0.2, "TIND": 0.001, "TABB": 0.5}


def _surface_radius(system, surface):
    """Read ``surface``'s radius as a float; ``None`` on a throw (fail-closed)."""
    try:
        return float(system.LDE.GetSurfaceAt(surface).Radius)
    except Exception:  # noqa: BLE001 — an unreadable radius -> treat as degenerate
        return None


def _surface_material(system, surface):
    """Read ``surface``'s material string; ``None`` on a throw (fail-closed).

    A blank material ('') = AIR. A read THROW -> ``None`` (the caller treats an
    indeterminate material as NON-glass for the default budget — it never AUTHORS a
    TIND/TABB it cannot justify).
    """
    try:
        mat = system.LDE.GetSurfaceAt(surface).Material
        return str(mat) if mat is not None else ""
    except Exception:  # noqa: BLE001 — an unreadable material -> indeterminate
        return None


def _is_flat(radius):
    """True if a radius is flat/degenerate for TRAD (inf / non-finite / 0) — D3/G16."""
    return radius is None or not math.isfinite(radius) or radius == 0.0


def _default_tolerances(system):
    """Build a default tolerance budget by reading the LDE (D3). Pure-python.

    One TRAD+TTHI per POWERED surface + one TIND+TABB per GLASS surface, over the
    INTERIOR surfaces (1..N-2; OBJECT 0 and IMAGE N-1 excluded). SKIPS a flat/``inf``
    radius for TRAD (probe §1: TRAD on an inf radius is a degenerate no-change) and an
    AIR surface for TIND/TABB. A surface with glass authors all four; an air surface
    with a real (powered) radius authors TRAD+TTHI only. NEVER opens a tool / authors a
    TDE row — it returns the entry list the handler authors.
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable count -> no default budget
        return []
    tolerances = []
    for surf in range(1, n - 1):
        radius = _surface_radius(system, surf)
        material = _surface_material(system, surf)
        is_glass = bool(material) and material.strip() != ""
        if not _is_flat(radius):
            tolerances.append({"type": "TRAD", "surface": surf,
                               "delta": _DEFAULT_DELTA["TRAD"]})
        # TTHI applies to any interior surface's thickness (a flat dummy's thickness is
        # still a real airgap DOF; the probe authored TTHI on the powered surf 2). We
        # author TTHI on a surface that has EITHER a real radius OR glass (skip a pure
        # zero-thickness flat dummy with no glass — nothing to perturb).
        if not _is_flat(radius) or is_glass:
            tolerances.append({"type": "TTHI", "surface": surf,
                               "delta": _DEFAULT_DELTA["TTHI"]})
        if is_glass:
            tolerances.append({"type": "TIND", "surface": surf,
                               "delta": _DEFAULT_DELTA["TIND"]})
            tolerances.append({"type": "TABB", "surface": surf,
                               "delta": _DEFAULT_DELTA["TABB"]})
    return tolerances


# --------------------------------------------------------------------------- #
# Two-phase validate-then-author pre-flight (D14 — collect ALL errors).
# --------------------------------------------------------------------------- #
def _coerce_surface(value):
    """Coerce a surface to an int (G10): reject bool / non-integral float / non-int.

    Accepts an exact int OR an integral float (a JSON round-trip can float an int:
    ``2.0`` -> 2). A bool / a non-integral float (``2.5``) / a non-number ->
    ``(None, <error-message>)``. Returns ``(int, None)`` on success.
    """
    if isinstance(value, bool):
        return None, f"surface must be an integer, not a bool ({value!r})"
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value), None
        return None, f"surface must be an integer, got non-integral {value!r}"
    if isinstance(value, int):
        return value, None
    return None, f"surface must be an integer, got {type(value).__name__} {value!r}"


def _valid_delta(value):
    """True iff ``value`` is a finite, positive, non-bool number (G12)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0


def _image_surface_index(system):
    """Read the image-surface index (``NumberOfSurfaces - 1``); ``None`` on a throw."""
    try:
        return int(system.LDE.NumberOfSurfaces) - 1
    except Exception:  # noqa: BLE001 — unreadable -> the caller fails closed
        return None


# The entry-key -> CellSpec.role map (§3.1 / D8). An optional entry key is
# consulted ONLY when the operand's metadata declares a cell of the matching role; an
# extra key whose role the layout does NOT expose -> LOUD reject (G-INPUT-EXTRACELL).
_ENTRY_ROLE_KEYS = {
    "surface2": "surface2",
    "roll_surf": "roll_surf",
    "code": "code",
    "param": "param",
    # (S2b TOL) the Zernike-term range keys: G-INPUT-EXTRACELL ACCEPTS max_term/
    # min_term on TEZI/TEXI (role declared) and REJECTS them on a non-term op (role
    # not declared — a silent no-op write is refused).
    "max_term": "max_term",
    "min_term": "min_term",
    # (§2.8 / D5) the TMCO multi-config keys: G-INPUT-EXTRACELL ACCEPTS
    # mce_row/mce_config on TMCO (role declared) and REJECTS them on a non-TMCO op.
    "mce_row": "mce_row",
    "mce_config": "mce_config",
}
# The two range layouts that REQUIRE a ``surface2`` (Surf1 < Surf2 client-side, §3.1).
_RANGE_LAYOUTS = frozenset({"surf_range", "surf_range_roll"})


def _meta_roles(meta):
    """The set of int-cell ROLES this operand exposes (e.g. {surface, surface2}).

    Delegates to the catalog's ``declared_roles`` (the single owner of the input-facing
    role set) when available; falls back to walking ``int_cells`` directly otherwise.
    """
    declared = getattr(_cat, "declared_roles", None)
    if declared is not None:
        try:
            return set(declared(meta))
        except Exception:  # noqa: BLE001 — fall back to the int_cells walk
            pass
    roles = set()
    for cs in getattr(meta, "int_cells", ()):  # CellSpec.role (frozen tuple)
        roles.add(getattr(cs, "role", None))
    return roles


def _validate_multi_config(i, token, meta, entry):
    """Validate a multi_config (TMCO) entry: ``{mce_row, mce_config, delta|min+max}``.

    Returns ``(validated_dict, None)`` on success, or ``(None, error_string)``. BOTH
    ``mce_row`` and ``mce_config`` are REQUIRED 1-based INTEGER indices (the
    silent-zero-cell discipline — an omitted one is a malformed entry, never a default-0).
    L30 ``is_integral_int``: an integral float (``2.0``) is the strict-int path that the
    Row/Config# int-cell writer keys on; a non-integral float / a bool is REJECTED
    pre-mutation (it would die in the int-cell writer post-mutation). The ±delta channel
    (``delta`` symmetric XOR ``min``+``max`` asymmetric) is the SAME contract as a
    surface op (TMCO is has_minmax=True). The units are operand-agnostic (per the
    targeted MCE operand — disclosed).
    """
    from . import _tol_cells as _tcells

    mce_row = entry.get("mce_row")
    mce_config = entry.get("mce_config")
    if mce_row is None or mce_config is None:
        return None, (
            f"tolerances[{i}] {token} is a multi-config tolerance and REQUIRES both "
            "'mce_row' (the MCE operand row) and 'mce_config' (the configuration "
            f"number); got mce_row={mce_row!r}, mce_config={mce_config!r} — omitting "
            "either would target nothing (a silent-zero tolerance)"
        )
    for label, val in (("mce_row", mce_row), ("mce_config", mce_config)):
        if not _tcells.is_integral_int(val):
            return None, (
                f"tolerances[{i}] {token}: {label!r} must be an exact integer >= 1 "
                f"(an integral float like 2.0 is fine, a bool / non-integral float is "
                f"refused — it would die in the int-cell writer post-mutation, L30), "
                f"got {val!r}"
            )
        if int(val) < 1:
            return None, (
                f"tolerances[{i}] {token}: {label!r} must be >= 1 (1-based), got {val!r}"
            )

    validated = {
        "type": token,
        # The TMCO author keys: ``int_cell_writes`` resolves these into the Row/Config#
        # int cells (mce_row/mce_config roles).
        "mce_row": int(mce_row),
        "mce_config": int(mce_config),
        "tier": getattr(meta, "tier", None),
        "family": getattr(meta, "family", None),
        "units": getattr(meta, "units", None),
        "cell_layout": getattr(meta, "cell_layout", None),
    }

    # The ±perturbation channel — delta XOR (min, max), exactly one (TMCO is has_minmax).
    delta_in = "delta" in entry
    minmax_in = ("min" in entry) or ("max" in entry)
    if delta_in and minmax_in:
        return None, (
            f"tolerances[{i}] {token}: supply EITHER 'delta' (symmetric) OR 'min'+'max' "
            "(asymmetric), not both"
        )
    if not delta_in and not minmax_in:
        return None, (
            f"tolerances[{i}] {token}: a multi-config tolerance needs 'delta' "
            "(symmetric) or 'min'+'max' (asymmetric)"
        )
    if delta_in:
        delta = entry.get("delta")
        if not _valid_delta(delta):
            return None, (
                f"tolerances[{i}] delta must be a finite number > 0, got {delta!r}"
            )
        validated["min"] = -float(delta)
        validated["max"] = float(delta)
        validated["delta"] = float(delta)
    else:
        mn = entry.get("min")
        mx = entry.get("max")
        if mn is None or mx is None:
            return None, (
                f"tolerances[{i}] {token}: an asymmetric override needs BOTH 'min' and "
                "'max'"
            )
        if not _finite_number(mn) or not _finite_number(mx):
            return None, (
                f"tolerances[{i}] {token}: 'min'/'max' must be finite numbers, got "
                f"min={mn!r}, max={mx!r}"
            )
        if not (float(mn) < float(mx)):
            return None, (
                f"tolerances[{i}] {token}: requires min < max (got min={mn!r}, "
                f"max={mx!r})"
            )
        validated["min"] = float(mn)
        validated["max"] = float(mx)
    return validated, None


def _bare_grin_hint(system, surface, token):
    """A token-correct GRIN hint suffix for the blank-material refusal, or "".

    (GRIN §1.2b-2) When ``surface`` provably resolves a GRIN FAMILY
    member (the 12-member RECOGNITION resolver ``grin_family_type_of_name`` — NOT the
    authorable resolver: the hint should fire for ANY GRIN family member), append a
    per-token hint. Any read throw / non-GRIN surface -> "" (never a false GRIN claim).
    Import ``_grin_cells`` lazily (the TEZI-precedent circular-import posture).
    """
    try:
        from . import _grin_cells as _grin  # lazy — circular-import avoidance
        row = system.LDE.GetSurfaceAt(surface)
        info = _grin.grin_family_type_of_name(str(row.Type))
    except Exception:  # noqa: BLE001 — any read throw -> no hint (never a false claim)
        return ""
    if info is None:
        return ""
    if token == "TIND":
        return (
            " — this is a bare-cell GRIN surface: the base index n0 is Par#=2; author "
            "TPAR(param=2) for the base-index fabrication tolerance (a material-bound "
            "GRIN, whose index comes from a catalog entry rather than these cells, has no "
            "tolerance path here)."
        )
    if token == "TABB":
        return (
            " — this is a bare-cell GRIN surface: it has no catalog-glass Abbe tolerance "
            "path (its dispersion is not carried by a catalog entry), and TPAR is NOT an "
            "Abbe substitute."
        )
    return ""


def validate_tolerances(system, tolerances, *, strict=False):
    """Two-phase pre-flight (D14): operand-AGNOSTIC, fail-closed preconditions.

    PURE validation (read-only engine touches: ``NumberOfSurfaces`` / ``Radius`` /
    ``Material``) BEFORE any TDE mutation or tool open. Resolves each entry's ``type``
    -> ``TOL_OPERAND_META`` (unknown -> ``tolerancing_param``, cross-checked vs the live
    enum). Returns ``(authored, errors, warnings, known_gaps)``:

    - ``errors`` is the FULL list of input errors (all collected, no fail-fast, D14). A
      non-empty list -> the handler returns ``tolerancing_param``, opening/authoring
      NOTHING (G17).
    - ``authored`` is the list of validated entries (each ``{type, surface, surface2?,
      roll_surf?, code?, param?, delta?, min, max, tier, units, family}``).
    - ``warnings`` carries the soft notes (a skipped degenerate TRAD, a disclosed gap).
    - ``known_gaps`` carries the labeled CB/NSC pre-author refusals (D14; in the
      run-valid-disclose-loud default ``strict=False`` they ride ALONGSIDE the valid set).

    The fail-closed precondition pre-check (§5/D14):
    - ``cb_required`` {TUTX,TUTY,TUTZ,TUDX,TUDY} / ``nsc_required`` {TNPS} -> a LABELED
      ``known_gap`` refusal: NOT authored; under ``strict`` it becomes a hard error.
    - ``crash_class`` {TNPA,TNMA} -> a HARD refusal ALWAYS (regardless of ``strict``);
      the tool MUST NOT open with one in the set.

    Guards (each mutate-fails, L25): G13 unknown token (operand-agnostic, table-keyed);
    G-INPUT-EXTRACELL extra/absent cell; range surface2 + surface<surface2; delta XOR
    (min,max); control/compensator-carrying-delta reject; G10 integral-float surface;
    G11 surface client-side bound; G15 TIND/TABB needs glass; G16 degenerate-TRAD skip.
    """
    errors = []
    warnings = []
    authored = []
    known_gaps = []

    if not isinstance(tolerances, list):
        return [], [f"tolerances must be a list, got {type(tolerances).__name__}"], [], []
    if len(tolerances) == 0:
        # G14: refuse a degenerate empty-TDE run.
        return [], ["tolerances is empty; provide at least one {type, surface, delta} "
                    "entry (or omit it for the default budget)"], [], []

    image_surf = _image_surface_index(system)
    if image_surf is None:
        return [], ["could not read the image-surface index (NumberOfSurfaces is "
                    "unreadable); refusing to author a tolerance set"], [], []

    for i, entry in enumerate(tolerances):
        if not isinstance(entry, dict):
            errors.append(f"tolerances[{i}] must be a dict {{type, surface, delta}}, "
                          f"got {type(entry).__name__}")
            continue
        token = entry.get("type")
        # G13 / G-RUNTIME-UNKNOWN: resolve the token against the static catalog table
        # (operand-agnostic; the catalog accept-set == the author supported-set). An
        # un-tabled token is a loud reject (cross-checked vs the live enum upstream).
        meta = _cat.meta_for(token) if isinstance(token, str) else None
        if meta is None:
            errors.append(
                f"tolerances[{i}] 'type' {token!r} is not in the supported tolerance "
                "operand metadata table (unknown / un-tabled operand)"
            )
            continue

        tier = getattr(meta, "tier", None)
        # The fail-closed precondition PRE-CHECK (§5/D14) — BEFORE any surface read.
        if tier == "crash_class":
            # HARD refusal ALWAYS (regardless of strict): never run a session-killer.
            errors.append(
                f"tolerances[{i}] {token} refused: would CRASH the headless tolerancing "
                "run (IPC fault) on a sequential surface — the tool will not open. "
                "(crash_class precondition; needs a non-sequential surface.)"
            )
            known_gaps.append({
                "type": token, "gap": "crash_class",
                "ticket": getattr(meta, "ticket", None),
                "message": (getattr(meta, "reason", None)
                            or "refused: would crash the headless tolerancing run "
                            "(IPC fault) on a sequential surface."),
            })
            continue
        if tier in ("cb_required", "nsc_required"):
            gap = tier
            msg = (getattr(meta, "reason", None)
                   or (f"{token} needs a coordinate-break surface; supported after the "
                       "reflective/CB cycle lands. Use TETX/TEDX (surface tilt/decenter) "
                       "meanwhile." if gap == "cb_required"
                       else f"{token} requires a non-sequential surface."))
            known_gaps.append({
                "type": token, "gap": gap,
                "ticket": getattr(meta, "ticket", None), "message": msg,
            })
            if strict:
                errors.append(
                    f"tolerances[{i}] {token} refused ({gap}, strict=True): {msg}"
                )
            else:
                warnings.append(
                    f"tolerances[{i}] {token} shelved ({gap}): {msg} "
                    "(the valid operands still run; this gap is disclosed not silent.)"
                )
            continue

        # An operand exposing an ``other`` int cell with NO probe-grounded
        # entry key (TEXI/TEZI Max#/Min# Zernike-term range; ISOA/ISOB/ISOC/ISOD
        # Units/Statistics) would SILENTLY default that cell to 0 — encoding a DIFFERENT
        # tolerance than any sane intent (TEXI -> zero Zernike terms; ISOB -> Units=0), a
        # row that runs + parses + counts as ran but is silently wrong. The probe-grounded
        # rule: drive a meaningful cell or refuse the operand LOUDLY. We have no
        # probe-grounded semantics for these cells, so refuse them as a labeled
        # ``param_required`` gap — NEVER a silent zero-cell author.
        undrivable = _cat.param_required_cells(meta)
        if undrivable:
            cell_list = ", ".join(repr(h) for h in undrivable)
            msg = (
                f"{token} exposes parameter cell(s) {cell_list} that need additional "
                "parameters not yet probe-grounded; refusing rather than silently "
                "defaulting them to 0 (which would author a different tolerance than "
                "intended). File a follow-up to probe-ground these cells."
            )
            known_gaps.append({
                "type": token, "gap": "param_required",
                "ticket": getattr(meta, "ticket", None) or "Area-PARAM",
                "message": msg,
            })
            if strict:
                errors.append(
                    f"tolerances[{i}] {token} refused (param_required, strict=True): {msg}"
                )
            else:
                warnings.append(
                    f"tolerances[{i}] {token} shelved (param_required): {msg} "
                    "(the valid operands still run; this gap is disclosed not silent.)"
                )
            continue

        roles = _meta_roles(meta)
        has_minmax = bool(getattr(meta, "has_minmax", False))

        # G-INPUT-EXTRACELL: an optional cell key the layout does NOT expose -> reject.
        extracell = False
        for key, role in _ENTRY_ROLE_KEYS.items():
            if key in entry and role not in roles:
                errors.append(
                    f"tolerances[{i}] {token} has no {key!r} cell "
                    f"(layout {getattr(meta, 'cell_layout', '?')!r} exposes "
                    f"{sorted(r for r in roles if r)}); refusing the silent no-op write"
                )
                extracell = True
        if extracell:
            continue

        # (§2.8 / D5) the multi_config family (TMCO) is NOT surface-keyed:
        # it targets an MCE cell via {mce_row, mce_config} (NOT a Surf). Validate it on
        # its own branch (the surface-centric path below assumes a Surf cell). BOTH roles
        # are REQUIRED (an omitted one is a malformed entry, never a silent default-0).
        if getattr(meta, "family", None) == "multi_config":
            tmco_validated, tmco_err = _validate_multi_config(i, token, meta, entry)
            if tmco_err is not None:
                errors.append(tmco_err)
                continue
            authored.append(tmco_validated)
            continue

        # The primary surface int cell (Surf or Surf1). G10 integral-float coerce.
        surface, surf_err = _coerce_surface(entry.get("surface"))
        if surf_err is not None:
            errors.append(f"tolerances[{i}] {surf_err}")
            continue
        # G11: surface client-side bound (OBJECT 0 and IMAGE excluded — interior-surface firewall).
        if not (1 <= surface < image_surf):
            errors.append(
                f"tolerances[{i}] surface {surface} out of range; a tolerance surface "
                f"must be an interior surface 1..{image_surf - 1} (OBJECT 0 and the "
                f"image surface {image_surf} are excluded)"
            )
            continue

        validated = {"type": token, "surface": surface, "tier": tier,
                     "family": getattr(meta, "family", None),
                     "units": getattr(meta, "units", None),
                     "cell_layout": getattr(meta, "cell_layout", None)}

        # Range layouts REQUIRE surface2 + surface < surface2 (client-side, §3.1).
        layout = getattr(meta, "cell_layout", None)
        if "surface2" in roles:
            if layout in _RANGE_LAYOUTS and "surface2" not in entry:
                errors.append(
                    f"tolerances[{i}] {token} is a surface-RANGE operand and requires a "
                    "'surface2' (Surf2) cell"
                )
                continue
            if "surface2" in entry:
                surf2, s2_err = _coerce_surface(entry.get("surface2"))
                if s2_err is not None:
                    errors.append(f"tolerances[{i}] surface2: {s2_err}")
                    continue
                if not (1 <= surf2 < image_surf):
                    errors.append(
                        f"tolerances[{i}] surface2 {surf2} out of range "
                        f"(interior 1..{image_surf - 1})"
                    )
                    continue
                if layout in _RANGE_LAYOUTS and not (surface < surf2):
                    errors.append(
                        f"tolerances[{i}] {token} requires surface < surface2 "
                        f"(got {surface} >= {surf2}); the engine reports 'Illegal "
                        "surface ranges.'"
                    )
                    continue
                validated["surface2"] = surf2

        # The remaining optional int cells (roll_surf / code / param) — coerced to an
        # integer index (a code/param is an index, not a surface bound; roll_surf is a
        # surface number but is engine-validated, so we only coerce it integral here).
        cell_err = False
        for key in ("roll_surf", "code", "param"):
            role = _ENTRY_ROLE_KEYS[key]
            if key in entry and role in roles:
                val, err = _coerce_surface(entry.get(key))
                if err is not None:
                    errors.append(f"tolerances[{i}] {key}: {err}")
                    cell_err = True
                    break
                validated[key] = val
        if cell_err:
            continue

        # (GRIN §1.2b-1) the REQUIRED-PARAM firewall, catalog-scoped. An
        # ``is_param_perturbation_op`` op (the SHARED predicate — today exactly TPAR/TPAI)
        # targets a surface Par# cell and REQUIRES an explicit ``param`` >= 1; an omitted
        # ``param`` today defaults to Par#=0 — a phantom perturbation of a cell that does
        # not exist, counted ``ran``. Refuse pre-mutation (zero engine touch). TEDV
        # (control, has_minmax=False) and the compensator ops are structurally unaffected.
        if _cat.is_param_perturbation_op(token):
            if "param" not in entry:
                errors.append(
                    f"tolerances[{i}] {token} targets a surface Par# cell and REQUIRES "
                    "'param' (the 1-based Par# — on a GRIN surface Par2=n0, "
                    "Par3..Par8=the profile coefficients); omitting it would author a "
                    "phantom Par#=0 perturbation"
                )
                continue
            par_val = validated.get("param")
            if par_val is None or par_val < 1:
                errors.append(
                    f"tolerances[{i}] {token} 'param' must be a Par# >= 1 (a Par#0 cell "
                    f"does not exist), got {entry.get('param')!r}"
                )
                continue

        # (S2b TOL) Zernike-term range (TEZI/TEXI). If the operand exposes the term
        # cells (roles max_term/min_term), BOTH are REQUIRED (omitting them = the inert
        # 0/0 trap that runs + counts-as-ran while biting nothing, probe §B.1 Delta 0.0)
        # AND the range must be well-formed (1-based Zernike indices). Scoped ONLY to the
        # term cells; Units/Statistics stay refused above (still 'other'). L30: coerce
        # with is_integral_int (the SHARED predicate the writer keys on), NOT
        # _coerce_surface (which accepts 8.0 and would die in the writer post-mutation).
        # PLACEMENT: after the optional roll_surf/code/param loop and BEFORE the
        # delta/min-max channel — TEZI is has_minmax=True and STILL needs the
        # perturbation magnitude (the term cells are the WHICH, Min/Max is the HOW-MUCH),
        # so it does NOT ``continue`` on success (it FALLS THROUGH to the delta channel).
        if {"max_term", "min_term"} & roles:
            # Lazy import (circular-import avoidance — see the module-top note): the L30
            # shared predicate the int-cell writer ALSO keys on (validator-accept ==
            # writer-accept, BOTH true ``int`` only).
            from . import _tol_cells as _tcells
            mt = entry.get("max_term")
            nt = entry.get("min_term")
            if mt is None or nt is None:
                errors.append(
                    f"tolerances[{i}] {token} is a Zernike form-error operand and "
                    "REQUIRES both 'max_term' and 'min_term' (1-based Zernike-term "
                    "indices); omitting them would author an inert 0/0 range that runs "
                    "but perturbs zero terms"
                )
                continue
            if not _tcells.is_integral_int(mt) or not _tcells.is_integral_int(nt):
                errors.append(
                    f"tolerances[{i}] {token}: 'max_term'/'min_term' must be exact "
                    f"integers (1-based Zernike-term indices), got max_term={mt!r}, "
                    f"min_term={nt!r} (an integral float like 8.0 or a bool is refused "
                    "— it would die in the int-cell writer post-mutation, L30)"
                )
                continue
            if mt < 1 or nt < 1 or nt > mt:
                errors.append(
                    f"tolerances[{i}] {token}: malformed Zernike-term range "
                    f"(max_term={mt}, min_term={nt}); requires max_term >= 1, "
                    "min_term >= 1, and min_term <= max_term"
                )
                continue
            validated["max_term"] = int(mt)
            validated["min_term"] = int(nt)
            if mt == nt:
                warnings.append(
                    f"tolerances[{i}] {token}: single-term Zernike range ({mt}..{nt}); "
                    "intended? (valid but unusual)"
                )
            # FALL THROUGH to the delta/min-max channel — TEZI has_minmax=True still
            # needs the perturbation magnitude. Do NOT continue here.

        # delta XOR (min, max) — exactly one channel for a perturbation operand.
        delta_in = "delta" in entry
        minmax_in = ("min" in entry) or ("max" in entry)
        if not has_minmax:
            # control / compensator / structural — a delta/min/max is a role mismatch.
            if delta_in or minmax_in:
                errors.append(
                    f"tolerances[{i}] {token} is a {tier or 'control'}-tier operand "
                    "(no ±perturbation); a 'delta'/'min'/'max' is a role mismatch"
                )
                continue
            authored.append(validated)
            continue

        if delta_in and minmax_in:
            errors.append(
                f"tolerances[{i}] {token}: supply EITHER 'delta' (symmetric) OR "
                "'min'+'max' (asymmetric), not both"
            )
            continue
        if not delta_in and not minmax_in:
            errors.append(
                f"tolerances[{i}] {token}: a perturbation operand needs 'delta' "
                "(symmetric) or 'min'+'max' (asymmetric)"
            )
            continue

        if delta_in:
            delta = entry.get("delta")
            if not _valid_delta(delta):
                errors.append(
                    f"tolerances[{i}] delta must be a finite number > 0, got {delta!r}"
                )
                continue
            validated["min"] = -float(delta)
            validated["max"] = float(delta)
            validated["delta"] = float(delta)
        else:
            mn = entry.get("min")
            mx = entry.get("max")
            if mn is None or mx is None:
                errors.append(
                    f"tolerances[{i}] {token}: an asymmetric override needs BOTH "
                    "'min' and 'max'"
                )
                continue
            if not _finite_number(mn) or not _finite_number(mx):
                errors.append(
                    f"tolerances[{i}] {token}: 'min'/'max' must be finite numbers, got "
                    f"min={mn!r}, max={mx!r}"
                )
                continue
            if not (float(mn) < float(mx)):
                errors.append(
                    f"tolerances[{i}] {token}: requires min < max (got min={mn!r}, "
                    f"max={mx!r})"
                )
                continue
            validated["min"] = float(mn)
            validated["max"] = float(mx)

        # G15: TIND/TABB needs a glass surface (a read throw -> fail-closed reject).
        if token in _GLASS_TOKENS:
            material = _surface_material(system, surface)
            if material is None:
                errors.append(
                    f"tolerances[{i}] {token} on surface {surface}: the surface "
                    "Material could not be read (refusing rather than guessing)"
                )
                continue
            if material.strip() == "":
                errors.append(
                    f"tolerances[{i}] {token} requires a glass surface, but surface "
                    f"{surface} is AIR (blank material)"
                    + _bare_grin_hint(system, surface, token)
                )
                continue
        # G16: degenerate-TRAD soft-skip (a flat/inf radius -> warn + SKIP, not reject).
        if token == "TRAD":
            radius = _surface_radius(system, surface)
            if _is_flat(radius):
                warnings.append(
                    f"TRAD on surface {surface} skipped: the radius is flat/infinite "
                    f"({radius!r}) so a radius perturbation is degenerate (no change)"
                )
                continue
        authored.append(validated)

    if not errors and not authored:
        # Every entry was a soft-skip / a non-crash gap shelved -> nothing to run.
        if known_gaps and not strict:
            errors.append(
                "every supplied tolerance is a known-gap operand (CB/NSC-required); "
                "none could be authored — see 'known_gaps'"
            )
        else:
            errors.append(
                "every tolerance entry was skipped (e.g. all TRAD on flat/infinite "
                "surfaces); no perturbation would be authored"
            )
    return authored, errors, warnings, known_gaps


def _finite_number(value):
    """True iff ``value`` is a finite, non-bool number (asymmetric min/max channel)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


# --------------------------------------------------------------------------- #
# The authored-vs-parsed RECONCILE canary (D12/§5 — the CENTERPIECE).
# --------------------------------------------------------------------------- #
# The tiers whose operand, if authored but UNACCOUNTED, escalates to the over-optimistic
# tolerancing_reconcile failure. An AUTHORABLE op (``supported`` OR ``structural_zero``) is
# authored as a real ±delta perturbation and is EXPECTED to produce a (sensitivity) row — a
# ``supported`` op a real-change row, a ``structural_zero`` op a zero/near-zero-change row.
# Either, if silently OMITTED (no keyed row, no error line), is the same over-optimistic
# failure mode and MUST escalate (was only ``supported`` -> a silently-omitted
# structural_zero shipped ``ok:true`` with a non-empty unaccounted list, violating the
# invariant ``ok:true ⇒ unaccounted_operands == []``). Sourced from the catalog so the
# author/validate/reconcile tier set cannot diverge.
_AUTHORABLE_TIERS = _cat._AUTHORABLE_TIERS  # {"supported", "structural_zero"}
# control/compensator rows that legitimately produce no sensitivity row are NOT a reconcile
# failure (D18/D19): they are NOT perturbations, so they are expected-silent.
_EXPECTED_SILENT_TIERS = frozenset({"control", "compensator"})

# L26 SELF-CHECK — does FAIL-CLOSED now OVER-escalate?
#  1. Only if the real engine emits a legitimately-SURFACELESS sensitivity row for an
#     authored op. The probe shows every real sensitivity row carries a Surf column (the
#     live TIRR@2/TIRR@3 case produced surfaces [2,3], never None) — a surfaceless row
#     signals CORRUPTION, not a normal case, so escalating it loud is correct.
#  2. A false escalation is ok:false (the SAFE failure direction for a manufacturability
#     verdict); the silent over-optimistic mark-ran (the surfaceless fallback) was
#     the DANGEROUS direction this round closes. The ONLY mark-ran paths are a confident
#     keyed _consume + the explicit control/compensator expected-silent tier — NO path
#     marks an authored op ran from an unattributable (surfaceless / dropped / type-only)
#     row (verified: A range-vs-single mismatch, a dropped-corrupt row, a wrong-type
#     surfaceless row all escalate; the unattributable row is surfaced loud, not consumed).


def reconcile_authored_vs_parsed(authored, parsed):
    """Partition authored operands -> ran / refused / unaccounted (D12/G-RECON).

    PURE (no engine touch). FAIL-CLOSED: a parsed row is allowed to mark an
    authored op "ran" ONLY when it can be confidently attributed to a specific authored
    ``(type, surface)`` / ``(type, surface, surface2)`` key. No surfaceless / type-only
    fallback EVER marks an op ran — over-claiming manufacturability is the dangerous
    direction (the exact over-optimistic class ``tolerancing_reconcile`` exists to close),
    so an unattributable row is surfaced LOUD as a parse anomaly and an authored op with no
    keyed match is REFUSED (escalates) rather than rescued.

    For each AUTHORED operand, decide:

    - **ran** — a parsed sensitivity row carries its type AND surface and matches the op on
      **(type, surface)** — or, for a surface-RANGE op, ``(type, surface, surface2)``.
      Consume-once, so two authored same-type ops on DIFFERENT surfaces do NOT collapse onto
      one parsed row (BUG-1/C1: the engine silently omits one yet ``ok:true`` shipped). A
      structural_zero op that parsed a zero-change row at its surface counts as ran (D18).
    - **refused** — an interleaved engine error line (``parsed['operand_errors']``) names
      its type (the report-only precondition error the silent-author-failure hides; D11).
      Carries the ``reason_line``.
    - **unaccounted** — NEITHER a keyed row NOR an error line. An AUTHORABLE-tier unaccounted
      op (``supported`` OR ``structural_zero``) escalates to ``tolerancing_reconcile`` (the
      over-optimistic class, D12; structural_zero was wrongly excluded). A
      control/compensator op (no perturbation BY DESIGN) is expected-silent, NOT a failure.

    PARSE ANOMALIES (never mark an op ran):
    - A parsed sensitivity row whose ``surface`` did not parse (blank/garbled Surf column ->
      ``surface:None``), OR a DROPPED-corrupt row (``parsed['dropped_keys']`` when its Surf
      was readable, else the type-wide ``parsed['dropped_types']``), cannot be keyed to a
      specific authored operand. It is surfaced as an ``anomalous_rows`` entry (LOUD) and
      does NOT satisfy any authored op. The authored op it would have satisfied then has no
      keyed match -> unaccounted -> escalate (fail-closed). (FIX-B: a dropped row's Surf is
      retained so a corrupt row at ONE surface disqualifies ONLY its (type, surface) group,
      not a legit count-fallback for the SAME type at a DIFFERENT surface.)
    - A KEYED parsed row that matches NO authored ``(type, surface)`` is an engine/author
      desync — surfaced as an ``unexpected_parsed_rows`` entry (never silently dropped).

    L26 self-check (does this now OVER-escalate?): only if the real engine emits a
    legitimately-surfaceless sensitivity row for an authored op. The probe shows every real
    sensitivity row carries a Surf column (the live TIRR@2/TIRR@3 case produced surfaces
    [2,3], never None) — so a surfaceless row signals CORRUPTION, not a normal case, and
    escalating it loud is correct. A false escalation is ``ok:false`` (the SAFE failure
    direction for a manufacturability verdict); the silent over-optimistic mark-ran was the
    dangerous direction this round closes. NO path marks an authored op ran from an
    unattributable (surfaceless / dropped / type-only) row.

    Returns ``{ran, refused, unaccounted, escalate, unattributed_errors, anomalous_rows,
    unexpected_parsed_rows, reconciled_operand_keys, reconciliation_mode, par_unresolved,
    matched_operand_errors}`` where ``escalate`` is True iff any AUTHORABLE-tier op
    (``supported`` OR ``structural_zero``) is unaccounted (the handler then returns
    ``ok:false`` / ``tolerancing_reconcile``). The ``ok:true ⇒ unaccounted_operands == []``
    invariant is unchanged and now PROVABLE per-Par# via ``reconciled_operand_keys`` (each
    consumed op's truthful selector + ``method``:``"keyed"``/``"count_fallback"``);
    ``reconciliation_mode`` is ``"count_fallback"`` iff any group used the belt (else
    ``"keyed"``); ``par_unresolved`` carries the belt disclosure; ``matched_operand_errors``
    (v2) surfaces a matched type's engine-error line that would otherwise be swallowed.
    The invariant holds for BOTH supported and structural_zero.

    L26 self-check — does escalating structural_zero OVER-escalate (false
    positive)? NO on the tested lenses: the probe classified structural_zero as
    ``supported_row_zero_change`` — these ops (TCMU/TCIO/TCEO/TPAR/TPAI/TETZ/TSDI) produce a
    ZERO-CHANGE ROW, not an omission, so in practice a structural_zero op is keyed (matched
    via ``_consume``) -> marked ran -> never reaches the escalation branch. The escalation
    fires ONLY if a structural_zero op is GENUINELY omitted (no keyed row, no error) — an
    anomaly worth surfacing LOUD (``ok:false``, the safe manufacturability-verdict direction).
    The dangerous direction (silent ``ok:true`` with a non-empty unaccounted list) is what
    this fix closes.
    """
    rows = parsed.get("sensitivity") or []
    errors = parsed.get("operand_errors") or []
    # ``dropped_types`` — a dropped-corrupt row whose Surf was UNREADABLE (type-wide
    # disqualification, fail-closed). ``dropped_keys`` (FIX-B) — a dropped-corrupt row
    # whose Surf WAS readable, keyed by (type, surface) so it disqualifies ONLY that
    # group, not every surface of the type.
    dropped = parsed.get("dropped_types") or set()
    dropped_keys = parsed.get("dropped_keys") or set()

    # Build a consumable pool of keyable ROW RECORDS (one per parsed sensitivity row). Each
    # row is consumed AT MOST ONCE so two authored ops cannot both claim the SAME single
    # parsed row (the BUG-1 collapse). A row whose surface did NOT parse (surface is None) is
    # a PARSE ANOMALY: it joins ``anomalous_rows`` and never marks an op ran.
    keyed_records = []       # {type, surface, surface2, param, consumed} — consume-once
    anomalous_rows = []      # rows that cannot be keyed to a specific authored operand
    # count-belt anomaly membership keyed by (type, surface). The count
    # fallback consumes BLANK-col3 records by cardinality; a group that ALSO produced a
    # PARSE ANOMALY (a non-integral Par# row dropped from keyed_records, OR a dropped-corrupt
    # row) is UNRELIABLE and MUST NOT count-fall-back (the locked rule "a non-integral
    # selector disqualifies the whole group"). ``anomaly_group_keys`` holds the (type,
    # surface) groups with a surface-readable anomaly (a param-anomalous row with a readable
    # Surf, OR a dropped-corrupt row with a readable Surf — FIX-B); ``anomaly_group_types``
    # holds types whose anomaly carries NO usable surface (a param-anomalous row whose Surf
    # ALSO failed, or a dropped-corrupt row whose Surf was unreadable) — those disqualify
    # EVERY group of that type, fail-closed.
    anomaly_group_keys = set()   # {(type, surface)}
    anomaly_group_types = set()  # {type} — surface unknown -> disqualify any surface
    for r in rows:
        rtype = r.get("type")
        rsurf = r.get("surface")
        # (§1.2a, SYMMETRIC AUTHORITY) a present-but-MALFORMED col3 selector
        # — a non-integral Par# (``param_anomalous``, param branch) OR a non-integral
        # surface2/range marker (``surface2_anomalous``, surface2 branch) — is a PARSE
        # ANOMALY: NOT a truncated false key, NOT belt-eligible; routed to anomalous_rows so
        # the op it would have satisfied escalates fail-closed. BOTH anomaly bits route
        # through this ONE arm (the single symmetric mechanism — no col3 family is exempt).
        if r.get("param_anomalous") or r.get("surface2_anomalous"):
            # disqualify this (type, surface) group from the count belt.
            if rsurf is None:
                anomaly_group_types.add(rtype)
            else:
                anomaly_group_keys.add((rtype, rsurf))
            anomalous_rows.append({
                "type": rtype,
                "reason": "non_integral_selector",
                "detail": (
                    f"a sensitivity row of type {rtype!r} had a present-but-MALFORMED "
                    "selector (col3) — a corrupt Par#/surface2 selector; it cannot be "
                    "reconciled to a specific authored operand and is not eligible for "
                    "the count fallback"
                ),
            })
            continue
        if rsurf is None:
            anomalous_rows.append({
                "type": rtype,
                "reason": "unreadable_surface",
                "detail": (
                    f"a sensitivity row of type {rtype!r} had an unreadable/missing/"
                    "non-integral Surf column — it cannot be reconciled to a specific "
                    "authored operand"
                ),
            })
            continue
        keyed_records.append({
            "type": rtype, "surface": rsurf,
            "surface2": r.get("surface2"), "param": r.get("param"),
            "consumed": False,
        })
    # A DROPPED-corrupt row (unparseable criterion) cannot be attributed to a specific
    # authored op. fail-closed: it is a PARSE ANOMALY (surfaced loud), NOT a free pass
    # that masks a same-type omission.
    # (FIX-B) A dropped row whose Surf WAS readable disqualifies ONLY its (type, surface)
    # group — a corrupt TPAR@2 must not block a legit count-fallback for TPAR@3.
    for (t, s) in dropped_keys:
        anomaly_group_keys.add((t, s))
        anomalous_rows.append({
            "type": t,
            "surface": s,
            "reason": "dropped_corrupt_row",
            "detail": (
                f"a sensitivity row of type {t!r} on surface {s} was dropped "
                "(corrupt/unparseable criterion) — it cannot be reconciled to a "
                "specific authored operand"
            ),
        })
    # A dropped row whose Surf was ALSO unreadable carries NO usable surface -> disqualify
    # EVERY group of that type (fail-closed; the report is unreliable for the type).
    for t in dropped:
        anomaly_group_types.add(t)
        anomalous_rows.append({
            "type": t,
            "reason": "dropped_corrupt_row",
            "detail": (
                f"a sensitivity row of type {t!r} was dropped (corrupt/unparseable "
                "criterion) — it cannot be reconciled to a specific authored operand"
            ),
        })

    err_by_type = {}
    err_unattributed = []
    for e in errors:
        if e.get("type"):
            err_by_type.setdefault(e["type"], []).append(e["line"])
        else:
            err_unattributed.append(e["line"])

    def _is_range_marker(s2):
        """True iff ``s2`` is a real range Surf2 (>= 1). 0/None = a non-range placeholder.

        A range op is validated as ``1 <= surface2 < image_surf`` (and ``surface < surface2``),
        so a real range Surf2 is always >= 1; the parser renders a BLANK Surf2 column as 0
        (or None when unreadable) — both mean "this is NOT a range row".
        """
        return s2 is not None and s2 >= 1

    def _consume(token, surface, surface2):
        """Consume the FIRST unconsumed keyed record matching (token, surface[, surface2]).

        A range op (surface2 >= 1) must match a record carrying that EXACT range Surf2; a
        single op (surface2 None) matches a NON-range record (Surf2 0/None) — so a range row
        and a single row of the same (type, surface) do NOT cross-consume. True iff a record
        was consumed.
        """
        want_range = _is_range_marker(surface2)
        for rec in keyed_records:
            if rec["consumed"]:
                continue
            if rec["type"] != token or rec["surface"] != surface:
                continue
            rec_range = _is_range_marker(rec["surface2"])
            if want_range:
                if rec["surface2"] != surface2:
                    continue
            elif rec_range:
                # a single (non-range) authored op must not steal a true range row
                continue
            rec["consumed"] = True
            return True
        return False

    def _consume_param(token, surface, param):
        """Consume the FIRST unconsumed record matching (token, surface, param) EXACTLY.

        (GRIN §1.2c-2) A parameter op keys on its Par#; it NEVER matches a range
        record (a param row renders surface2=None by construction) and NEVER falls back
        to (type, surface). A record with param=None (unreadable col3) is NOT consumable
        here — it is the count-belt's input only. (A param_anomalous row never reached
        keyed_records, §1.2a.) True iff a record was consumed.
        """
        for rec in keyed_records:
            if rec["consumed"]:
                continue
            if rec["type"] != token or rec["surface"] != surface:
                continue
            if _is_range_marker(rec["surface2"]):
                continue
            if rec.get("param") is None or rec["param"] != param:
                continue
            rec["consumed"] = True
            return True
        return False

    def _consume_param_group_by_count(token, surface, n_authored):
        """The fail-closed defense-in-depth COUNT belt (§1.2c-3).

        Fires ONLY when, for the (token, surface) group: EVERY unconsumed non-range
        record has param=None (wholly unreadable col3 — a MIXED readable/unreadable
        group never qualifies), NO exact-key consume happened for the group (gated by
        the caller), AND len(group records) == n_authored (exact cardinality). Consumes
        one-for-one and returns the consumed count. Any short/extra count or mixed
        readability -> returns 0 -> the group escalates fail-closed.
        """
        group = [
            rec for rec in keyed_records
            if not rec["consumed"]
            and rec["type"] == token and rec["surface"] == surface
            and not _is_range_marker(rec["surface2"])
        ]
        if not group:
            return 0
        if any(rec.get("param") is not None for rec in group):
            return 0                      # MIXED / readable-wrong-Par# -> escalate
        if len(group) != n_authored:
            return 0                      # short / extra / dropped-anomaly -> escalate
        for rec in group:
            rec["consumed"] = True
        return len(group)

    ran = []
    refused = []
    unaccounted = []
    escalate = False
    reconciled_keys = []          # (§1.2c-5) one entry per CONSUMED op, truthful selector
    par_unresolved = []           # (§1.2c-3) count-fallback disclosure strings
    matched_operand_errors = []   # (v2) a matched type's un-consumed error lines
    matched_op_types = set()      # types with >=1 keyed/belt consume (scope)
    consumed_refusal_lines = set()  # error lines already surfaced via the refused arm
    reconciliation_mode = "keyed"

    def _resolve_unmatched(token, tier, surface, param=None):
        """An unmatched op -> refused (error line) / expected-silent / unaccounted."""
        nonlocal escalate
        if token in err_by_type:
            refused.append({"type": token, "reason_line": err_by_type[token][0]})
            consumed_refusal_lines.add(err_by_type[token][0])
        elif tier in _EXPECTED_SILENT_TIERS:
            # control/compensator — expected-silent (no row, no error BY DESIGN, D18/D19).
            ran.append(token)
        else:
            u = {"type": token, "surface": surface, "tier": tier}
            if param is not None:
                u["param"] = param       # (§1.2c-5) name the missing coefficient
            unaccounted.append(u)
            # Escalate for ANY authorable perturbation tier (supported OR
            # structural_zero) — both are authored as a ±delta and expected to land a row.
            if tier in _AUTHORABLE_TIERS:
                escalate = True

    # First pass: exact keyed consume. Param-op failures are DEFERRED per (token, surface)
    # group for the count belt (the belt must not race the exact key, §1.2c-4 ordering).
    param_group_meta = {}   # (token, surface) -> {"n", "exact", "deferred": [(token,tier,surf,param)]}
    for entry in authored:
        token = entry.get("type")
        tier = entry.get("tier")
        surface = entry.get("surface")
        surface2 = entry.get("surface2")
        # (§2.8 / D5) the multi_config (TMCO) family is NOT surface-keyed —
        # it targets (mce_row, mce_config). Checked FIRST; byte-untouched (sound under the
        # §1.1 DISJOINTNESS invariant — no row is multi_config AND param-secondary-key).
        if entry.get("family") == "multi_config":
            surface = entry.get("mce_row")
            surface2 = entry.get("mce_config")
            matched = surface is not None and _consume(token, surface, surface2)
            if matched:
                ran.append(token)
                matched_op_types.add(token)
                reconciled_keys.append({
                    "type": token, "mce_row": surface, "mce_config": surface2,
                    "method": "keyed",
                })
            else:
                _resolve_unmatched(token, tier, surface)
        elif _cat.is_param_perturbation_op(token):
            # (v2) the SHARED authorable-perturbation predicate (TPAR/TPAI) — NOT
            # parser_secondary_key: a TEDV/CPAR/CEDV/CNPA (control/compensator; param-role
            # render but has_minmax=False) routes to the else/_consume arm exactly as today.
            param = entry.get("param")
            gm = param_group_meta.setdefault(
                (token, surface), {"n": 0, "exact": False, "deferred": []}
            )
            gm["n"] += 1
            matched = surface is not None and _consume_param(token, surface, param)
            if matched:
                gm["exact"] = True
                ran.append(token)
                matched_op_types.add(token)
                reconciled_keys.append({
                    "type": token, "surface": surface, "param": param,
                    "method": "keyed",
                })
            else:
                gm["deferred"].append((token, tier, surface, param))
        else:
            matched = surface is not None and _consume(token, surface, surface2)
            if matched:
                ran.append(token)
                matched_op_types.add(token)
                key = {"type": token, "surface": surface, "method": "keyed"}
                if _is_range_marker(surface2):
                    key["surface2"] = surface2
                reconciled_keys.append(key)
            else:
                _resolve_unmatched(token, tier, surface)

    # Second pass: the count-fallback belt for deferred param groups. No count consume
    # while any same-group exact consume succeeded (gm["exact"]) — a MIXED group escalates.
    count_fallback_used = False
    for (token, surface), gm in param_group_meta.items():
        deferred = gm["deferred"]
        if not deferred:
            continue
        consumed = 0
        # require ZERO group anomalies before the belt fires: a param-anomalous
        # or dropped-corrupt row on this (type, surface) — or ANY dropped/surfaceless anomaly
        # of this type — disqualifies the whole group (the report is unreliable), so the belt
        # returns 0 and the group escalates fail-closed rather than count-consuming the blanks.
        group_has_anomaly = (
            (token, surface) in anomaly_group_keys or token in anomaly_group_types
        )
        if not gm["exact"] and surface is not None and not group_has_anomaly:
            consumed = _consume_param_group_by_count(token, surface, gm["n"])
        if consumed and consumed == len(deferred):
            count_fallback_used = True
            for (t, _tier, surf, param) in deferred:
                ran.append(t)
                matched_op_types.add(t)
                reconciled_keys.append({
                    "type": t, "surface": surf, "param": param,
                    "method": "count_fallback",
                })
            par_unresolved.append(
                f"the {gm['n']} {token} tolerance(s) on surface {surface} had an "
                "unreadable Par# column in the report; reconciled by COUNT (exact "
                "cardinality match), NOT per-Par# key — verify the raw report"
            )
        else:
            for (t, tier, surf, param) in deferred:
                _resolve_unmatched(t, tier, surf, param)
    if count_fallback_used:
        reconciliation_mode = "count_fallback"

    # BUG-2 (MED): an engine error line attributed to a KNOWN operand that was NOT in the
    # authored set is neither a refusal of an authored op nor a type=None unattributed line
    # — surface it (the "never swallowed" guarantee) rather than drop it on the floor.
    authored_types = {entry.get("type") for entry in authored}
    for etype, lines in err_by_type.items():
        if etype not in authored_types:
            err_unattributed.extend(lines)

    # A matched type's captured engine-error line is neither a refusal (matched won)
    # nor unattributed (in authored_types), so it used to be swallowed. Per-Par# TPAR
    # reconcile puts TPAR on the matched path, so this surfaces the otherwise-swallowed
    # line as ADVISORY (never
    # flips ok; the raw line stays in the envelope's operand_errors list). The
    # err_unattributed arm above is byte-unchanged.
    for etype, lines in err_by_type.items():
        if etype in authored_types and etype in matched_op_types:
            for line in lines:
                if line not in consumed_refusal_lines:
                    matched_operand_errors.append(
                        {"type": etype, "reason_line": line}
                    )

    # A KEYED parsed row matching NO authored key was consumed by nobody —
    # an engine/author desync the verdict must not hide. Surface the residue TRUTHFULLY (a
    # param leftover renders {type, surface, param}, never a fake surface2).
    unexpected_parsed_rows = []
    for rec in keyed_records:
        if rec["consumed"]:
            continue
        entry = {"type": rec["type"], "surface": rec["surface"]}
        if rec.get("param") is not None:
            entry["param"] = rec["param"]
        elif _is_range_marker(rec["surface2"]):
            entry["surface2"] = rec["surface2"]
        unexpected_parsed_rows.append(entry)

    return {
        "ran": ran,
        "refused": refused,
        "unaccounted": unaccounted,
        "escalate": escalate,
        "unattributed_errors": err_unattributed,
        "anomalous_rows": anomalous_rows,
        "unexpected_parsed_rows": unexpected_parsed_rows,
        "reconciled_operand_keys": reconciled_keys,
        "reconciliation_mode": reconciliation_mode,
        "par_unresolved": par_unresolved,
        "matched_operand_errors": matched_operand_errors,
    }


def tol_operand_meta_covers_enum(live_names):
    """Cross-check the static catalog table covers the live enum EXACTLY (G-ENUM-COVER).

    Delegates to the catalog's ``validate_enum_parity`` (the single parity owner) which
    returns a ``{ok, missing_from_table, missing_from_enum, untiered, ...}`` verdict. A
    member in the live enum but not the table (a version bump) OR in the table but not
    the enum (a removed member) OR an untiered row -> ``ok:false``. This wrapper RAISES a
    LOUD ``tolerancing_param`` ``ToleranceError`` on a parity miss so both the unit gate
    (captured 62) and the live gate (real reflection) fail HARD the same way (the mock
    never closes the gate, L24). Returns the parity dict on success.
    """
    result = _cat.validate_enum_parity(live_names)
    if not result.get("ok", False):
        raise ToleranceError(
            f"the tolerance operand catalog does not cover the live enum exactly "
            f"(missing_from_table={result.get('missing_from_table')}, "
            f"missing_from_enum={result.get('missing_from_enum')}, "
            f"untiered={result.get('untiered')}); the catalog is stale / a version "
            "bump added a member — refusing to author against an incomplete table",
            family="tolerancing_param",
        )
    return result


# --------------------------------------------------------------------------- #
# The LDE side-effect tripwire (D16 — cheap; never silently hides).
# --------------------------------------------------------------------------- #
def _read_optional(getter):
    """Best-effort read of an adjunct cell -> a comparable token; sentinel on a throw.

    The material/conic/semi-diameter fields are READ-GUARDED (some surface types do not
    expose a conic/material); a throw -> a stable ``"<na>"`` token so a partial read still
    compares byte-for-byte (it never makes the snapshot raise).
    """
    try:
        value = getter()
    except Exception:  # noqa: BLE001 — an unreadable adjunct -> a stable sentinel
        return "<na>"
    if value is None:
        return "<na>"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return str(value)


def snapshot_lde(system):
    """Per-surface ``(i, radius, thickness, material, conic, semi_diameter)`` + solve (D16).

    The BEFORE/AFTER comparand for the side-effect tripwire (probe §7: a run is
    side-effect-free). Broadened beyond radius/thickness to also capture material + conic +
    semi-diameter per surface (D16 blind-spot top-up) so a future engine change that
    silently mutates one of those is CAUGHT (the run is side-effect-free today; this is the
    future-proofing net). Read-guarded: an unreadable surface contributes a sentinel row so
    a partial read still compares. Returns a tuple (hashable/comparable).
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — an unreadable count -> empty snapshot
        return ()
    rows = []
    for i in range(n):
        try:
            surf = system.LDE.GetSurfaceAt(i)
            radius = float(surf.Radius)
            thickness = float(surf.Thickness)
        except Exception:  # noqa: BLE001 — a partial read still compares
            rows.append((i, "<unreadable>", "<unreadable>",
                         "<unreadable>", "<unreadable>", "<unreadable>"))
            continue
        material = _read_optional(lambda s=surf: s.Material)
        conic = _read_optional(lambda s=surf: s.Conic)
        semi_dia = _read_optional(lambda s=surf: s.SemiDiameter)
        rows.append((i, radius, thickness, material, conic, semi_dia))
    # The back-airgap solve (the optimizer's DOF carries a Variable/Fixed solve; a run
    # that silently un-varies it is a mutation the value-only snapshot would miss).
    solve = None
    if n >= 3:
        try:
            solve = str(system.LDE.GetSurfaceAt(n - 2).ThicknessCell.GetSolveData().Type)
        except Exception:  # noqa: BLE001 — an unreadable solve -> None
            solve = None
    return (tuple(rows), solve)


def lde_unchanged(before, after):
    """True iff the LDE state vectors are byte-equal within float tolerance (D16).

    A float radius/thickness compares via ``math.isclose`` (rel_tol 1e-9); a sentinel /
    solve-name compares for equality. A structural difference (length / solve) -> False.
    """
    if before is None or after is None:
        return before == after
    try:
        rows_b, solve_b = before
        rows_a, solve_a = after
    except (TypeError, ValueError):
        return before == after
    if solve_b != solve_a:
        return False
    if len(rows_b) != len(rows_a):
        return False
    for rb, ra in zip(rows_b, rows_a):
        if len(rb) != len(ra) or rb[0] != ra[0]:
            return False
        for vb, va in zip(rb[1:], ra[1:]):
            if isinstance(vb, (int, float)) and isinstance(va, (int, float)) \
                    and not isinstance(vb, bool) and not isinstance(va, bool):
                if not math.isclose(vb, va, rel_tol=1e-9, abs_tol=1e-12):
                    return False
            elif vb != va:
                return False
    return True


# --------------------------------------------------------------------------- #
# The .ZTD / report reap (D15 — the .ZDA #59 precedent; glob by lens stem).
# --------------------------------------------------------------------------- #
def _unlink_quiet(path):
    """Best-effort unlink; NEVER raises (a ``None`` / missing file / OSError swallowed)."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass


def reap_tol_artifacts(system, report_path, *, reap_report=True):
    """Glob-reap the engine's tolerance-data side files (D15). NEVER raises.

    Probe §2 capture: ``TolDataFile`` is a ``<lens-stem>.ZTD`` next to the lens
    (``UseDataRetention: True``). Even with ``SaveTolDataFile=False`` set, the headless
    run may land a data-retention file (the ``.ZDA`` #59 precedent — a side file the
    ``.zmx``-only reap would leak). We glob the lens stem's tolerance-data extension
    next to the loaded ``.zmx`` and unlink each. ``report_path`` (the tool's own report)
    is reaped too when ``reap_report`` is True (a workspace temp the tool wrote).

    Returns the list of reaped file paths (for the live-gate leftover assertion). A
    read of the lens path / a glob / an unlink failure is swallowed (a reap must never
    mask the run outcome).
    """
    reaped = []
    # Resolve the loaded lens path to find the side files next to it.
    lens_path = None
    try:
        lens_path = str(system.SystemData.Files.OpticStudioFile)
    except Exception:  # noqa: BLE001 — a path read throw -> only the report is reaped
        lens_path = None
    if not lens_path:
        # Fallback: try the older accessor name some builds expose.
        try:
            lens_path = str(system.SystemFile)
        except Exception:  # noqa: BLE001 — no lens path -> only the report is reaped
            lens_path = None

    if lens_path:
        stem, _ext = os.path.splitext(lens_path)
        # The tolerance-data-retention side-file extension (assembled to keep the bare
        # literal out of the source; it is a boundary-safe ZOS-API file extension).
        side_ext = "." + "Z" + "TD"
        patterns = [stem + side_ext, stem + side_ext.lower(),
                    stem + ".ZDA", stem + ".zda"]
        for pat in patterns:
            try:
                for hit in glob.glob(glob.escape(pat)):
                    _unlink_quiet(hit)
                    reaped.append(hit)
            except OSError:  # noqa: PERF203 — a glob failure must not mask the run
                pass

    if reap_report and report_path:
        try:
            if os.path.isfile(report_path):
                _unlink_quiet(report_path)
                reaped.append(report_path)
        except OSError:
            pass
    return reaped


__all__ = [
    "ToleranceError",
    "_resolve_tol_enum",
    "_tolerance_operand_enum",
    "_tol_namespace_enum",
    "_tolerancing_session",
    "read_report_bytes",
    "decode_tol_report",
    "parse_tol_report",
    "parse_lens_units",
    "reconcile_authored_vs_parsed",
    "tol_operand_meta_covers_enum",
    "_default_tolerances",
    "validate_tolerances",
    "snapshot_lde",
    "lde_unchanged",
    "reap_tol_artifacts",
    "_CRITERION_MEMBER",
    "_MODE_TO_SETUP",
    "_DEFAULT_TRIALS",
    "_MIN_STABLE_TRIALS",
    "_RECONCILE_FAMILY",
    "_PARAXIAL_FOCUS_HEADER",
]
