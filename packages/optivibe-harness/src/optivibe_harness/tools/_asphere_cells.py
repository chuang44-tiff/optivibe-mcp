"""tools/_asphere_cells.py — the even-asphere cell substrate.

NOT dispatchable (no ``TOOL_SPECS``). The single place the asphere primitive reads
and writes an ``EvenAspheric`` surface's eight PARAMETER cells, each a Double (the
r²..r¹⁶ even-asphere coefficients α2..α16). Every cell is DataType-keyed on the live
``cell.DataType`` AND Header-verified against the expected ``"Nth Order Term"`` label
(a drifted layout RAISES rather than write the wrong cell), then read-back-proven with
a tight relative tolerance and a near-ULP absolute floor (load-bearing for the tiny
1e-9..1e-15 coefficients an asphere carries).

This MIRRORS ``_cb_cells.write_cb_cell`` / the ``reflective`` grating-cell writer (the
same ``cell.DataType`` + Header-drift firewall + read-back-as-proof), but is a SEPARATE
re-implementation: the EvenAspheric Par cells are a DIFFERENT cell table from the CB Par
cells (CB Par1 = "Decenter X", grating Par1 = "Lines/µm", asphere Par1 = "2nd Order
Term"), so we do NOT extend ``_cb_cells.CB_PARAMS``. We REUSE only the LDE-wide
``_cb._surface_column_enum`` (the ``SurfaceColumn.ParN`` resolver — not CB-specific; the
grating tool set that precedent).

Probe-grounded rules this module encodes (all discovered by live probing):

- the enum member is ``SurfaceType.EvenAspheric`` (value 23), NOT ``EvenAsphere`` —
  the spelling trap: ``getattr(SurfaceType, "EvenAsphere")`` raises. We
  ``getattr`` the correct spelling, never a literal ``Enum.Parse``;
- the eight coefficient cells are exactly ``Par1..Par8`` (Headers "2nd".."16th Order
  Term"), all **Double** — so there is NO Int/Double branch and NO integral-float
  trap (every accessor is ``DoubleValue``). ``Par9..Par20`` are String "(unused)"
  cells — reading ``DoubleValue`` on one RAISES, so the table caps at 8;
- the WRITE PATH IS THE CELL, never a typed coefficient property (no typed
  setter exists for an EvenAspheric coefficient — the cell is the only path); the
  read-back proves it took (a no-op / collapse-to-zero -> RAISE);
- ``is_even_asphere(row)`` keys on the ``"EvenAspheric"`` substring of
  ``str(row.Type)`` (the same idiom ``_cb.is_coordinate_break`` / the grating
  ``_is_diffraction_grating`` use).

Live ZOS-API integration: exercised by the live test; unit-tested against the
fixture-style fake row/cell doubles whose Par cells RAISE on the wrong accessor and
COMPUTE the read-back from what was written (so a wrong cell->coefficient mapping
reddens).
"""
import math

from ..errors import AsphereWriteError, ToolParamError
from . import _cb_cells as _cb

# Asphere §1: the surface-type substring that marks an EvenAspheric row. ``row.Type``
# reads ``EvenAspheric`` after ChangeType; the describe layer keys on this
# substring too.
_ASPHERE_TYPE_SUBSTRING = "EvenAspheric"

# Asphere §1: the ordered Par-cell map — name -> (SurfaceColumn member, expected
# Header, expected DataType). The SINGLE source of truth (the cell
# map). index i (0-based) -> Par(i+1) -> the (2*(i+1))th-order even-asphere term.
# Every cell is "double" — there is NO Integer coefficient cell (so no integral-float
# trap, unlike the CB Order cell). The ``SurfaceColumn`` member NAME is resolved
# against the live enum at call time (``_cb._surface_column_enum`` — the LDE-wide Par
# resolver, NOT CB-specific).
ASPHERE_PARAMS = (
    ("a2", "Par1", "2nd Order Term", "double"),
    ("a4", "Par2", "4th Order Term", "double"),
    ("a6", "Par3", "6th Order Term", "double"),
    ("a8", "Par4", "8th Order Term", "double"),
    ("a10", "Par5", "10th Order Term", "double"),
    ("a12", "Par6", "12th Order Term", "double"),
    ("a14", "Par7", "14th Order Term", "double"),
    ("a16", "Par8", "16th Order Term", "double"),
)

# Fast lookups derived from ASPHERE_PARAMS (single source of truth).
_PARAM_TO_COL = {name: col for name, col, _h, _k in ASPHERE_PARAMS}
_PARAM_TO_HEADER = {name: header for name, _col, header, _k in ASPHERE_PARAMS}
_PARAM_TO_EXPECTED_KIND = {name: kind for name, _col, _h, kind in ASPHERE_PARAMS}
_PARAM_NAMES = tuple(name for name, _c, _h, _k in ASPHERE_PARAMS)

# The number of even-asphere coefficient cells (exactly 8, no Par9+).
N_ASPHERE_TERMS = len(ASPHERE_PARAMS)

# index i (0-based, 0..7) -> the param token. ``coefficients[i]`` writes ``Par(i+1)``,
# the (2*(i+1))th-order term.
_INDEX_TO_PARAM = {i: name for i, (name, _c, _h, _k) in enumerate(ASPHERE_PARAMS)}
# index i -> the PHYSICAL even order it carries (2, 4, 6, .. 16).
_INDEX_TO_ORDER = {i: 2 * (i + 1) for i in range(N_ASPHERE_TERMS)}
# the PHYSICAL even order (2..16) -> its param token (the agent reasons in orders).
ORDER_TO_PARAM = {2 * (i + 1): name for i, (name, _c, _h, _k) in enumerate(ASPHERE_PARAMS)}

# The read-back proof's absolute floor (the ``_cb_cells._READBACK_ABS_TOL`` precedent
# — ≈ double-precision ULP near 1.0). The rel_tol governs normal magnitudes; the abs
# floor is load-bearing for the tiny coefficients (1e-9..1e-15) — a write that
# collapsed a >1e-15 value to 0.0 fails the proof LOUD.
_READBACK_ABS_TOL = 1e-15


# =========================================================================== #
# The per-type ORDER MAP — the SINGLE source of truth.
# =========================================================================== #
# The spec (§(c.2) / AXIS 3) makes ONE per-type descriptor the single source
# read by (1) the substrate write path (Par column + expected Header), (2) the
# sag-math (the physical exponent), and (3) the carry/verify (the read-back order). A
# mis-map cannot pass the read-back, cannot pass the Header-drift guard, and cannot
# pass the offline SAGY-computing fake (which computes from the SAME map). Grounded
# against the materialized-cell table.
#
# Each ``AsphereTypeInfo`` carries:
#   member       : the SurfaceType spelling (getattr resolution).
#   gated        : True for the Extended types (Par13 Max-Term gate + Par14 Norm Radius).
#   normalized   : True iff coefficients are on p = r / norm_radius (Extended types).
#   max_terms    : the legal coefficient count (8 fixed / 240 gated).
#   first_par    : the Par index of coefficient i=0 (1 for Odd/Even, 15 for the gated).
#   gate_par     : "Par13" (Integer Max-Term) for the gated types, else None.
#   norm_par     : "Par14" (Double Norm Radius) for the gated types, else None.
#   power(i)     : the physical exponent coefficients[i] carries.
#   header(i)    : the expected Header string for the i-th coefficient cell.

def ordinal(n):
    """Render an English ordinal: ``1->"1st"``, ``2->"2nd"``, ``3->"3rd"``, ``4->"4th"``…

    Used to GENERATE the Odd / Even Header strings (``"Nth Order Term"``). The 11..13
    teens take "th" (the standard rule); everything else keys on the last digit.
    """
    n = int(n)
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


class AsphereTypeInfo:
    """One asphere ``surface_type``'s descriptor (the ORDER MAP — the single source).

    ``power`` and ``header`` are callables of the 0-based coefficient index ``i``.
    """

    __slots__ = (
        "surface_type", "member", "gated", "normalized", "max_terms",
        "first_par", "gate_par", "norm_par", "_power", "_header",
    )

    def __init__(self, surface_type, *, member, gated, normalized, max_terms,
                 first_par, gate_par, norm_par, power, header):
        self.surface_type = surface_type
        self.member = member
        self.gated = gated
        self.normalized = normalized
        self.max_terms = max_terms
        self.first_par = first_par
        self.gate_par = gate_par
        self.norm_par = norm_par
        self._power = power
        self._header = header

    def power(self, i):
        """The physical exponent ``coefficients[i]`` carries."""
        return self._power(i)

    def header(self, i):
        """The expected Header string of the i-th coefficient cell."""
        return self._header(i)

    def coeff_par(self, i):
        """The ``"ParN"`` column the i-th coefficient lives in (first_par + i)."""
        return f"Par{self.first_par + i}"


# The EvenAspheric header function returns the EXACT existing literal Headers (reuse
# ``ASPHERE_PARAMS``) so the S1 drift guard is byte-identical (the AXIS 3 note).
def _even_header(i):
    return _PARAM_TO_HEADER[_INDEX_TO_PARAM[i]]


ASPHERE_TYPE_INFO = {
    "EvenAspheric": AsphereTypeInfo(
        "EvenAspheric", member="EvenAspheric", gated=False, normalized=False,
        max_terms=N_ASPHERE_TERMS, first_par=1, gate_par=None, norm_par=None,
        power=lambda i: 2 * (i + 1),
        header=_even_header,
    ),
    "OddAsphere": AsphereTypeInfo(
        "OddAsphere", member="OddAsphere", gated=False, normalized=False,
        max_terms=8, first_par=1, gate_par=None, norm_par=None,
        power=lambda i: i + 1,
        header=lambda i: f"{ordinal(i + 1)} Order Term",
    ),
    "ExtendedAsphere": AsphereTypeInfo(
        "ExtendedAsphere", member="ExtendedAsphere", gated=True, normalized=True,
        max_terms=240, first_par=15, gate_par="Par13", norm_par="Par14",
        power=lambda i: 2 * (i + 1),
        header=lambda i: f"Coeff. on p^{2 * (i + 1)}",
    ),
    "ExtendedOddAsphere": AsphereTypeInfo(
        "ExtendedOddAsphere", member="ExtendedOddAsphere", gated=True,
        normalized=True, max_terms=240, first_par=15, gate_par="Par13",
        norm_par="Par14",
        power=lambda i: i + 1,
        header=lambda i: f"Coeff. on p^{i + 1}",
    ),
}

# The full S3 allow-set (the surface_type validation universe).
ASPHERE_TYPE_NAMES = tuple(ASPHERE_TYPE_INFO)

# The gated Par13/Par14 Headers (the gate/norm cell drift verify).
_GATE_HEADER = "Maximum Term #"
_NORM_HEADER = "Norm Radius"

# The Max-Term hard ceiling (the engine clamps >240 silently).
_MAX_GATED_TERMS = 240


# --------------------------------------------------------------------------- #
# Generic SurfaceType member resolution (getattr, never Enum.Parse).
# --------------------------------------------------------------------------- #
def _surface_type_member(system, name):
    """Resolve the live ``SurfaceType.<name>`` member (the §3 generic authoring enum).

    The SPELLING TRAP: a misspelling (``EvenAsphere`` for ``EvenAspheric``)
    must REFUSE via getattr, never an engine crash. Injected via
    ``_enum_types["SurfaceType"]`` for unit tests; otherwise the live
    ``ZOSAPI.Editors.LDE.SurfaceType`` namespace. A resolution failure surfaces as a
    ``ToolParamError`` (a param-class problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceType" in injected:
        from ..enums import _resolve_enum
        return _resolve_enum(injected["SurfaceType"], name)
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return getattr(_lde.SurfaceType, name)
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            f"could not resolve SurfaceType.{name} from ZOSAPI.Editors.LDE: {exc}"
        )


def coefficient_order_table(info, coefficients):
    """[{index, order, term, normalized, header, value}] for a coefficient list (S7 #13).

    Per-type, map-driven from the ``AsphereTypeInfo`` ORDER MAP — NEVER a static
    "r²-first" label (which mis-labels a heavy / normalized asphere). ``order =
    info.power(i)`` (the physical exponent); ``term`` is ``"p^N"`` for a NORMALIZED
    (Extended) type else ``"r^N"`` (absolute Odd/Even); ``header`` is the engine's own
    label ``info.header(i)`` (the most faithful disambiguator: ``"Coeff. on p^4"`` vs
    ``"4th Order Term"``). Pure / no engine — index 0 = r² for EvenAspheric by
    construction. ``value`` MIRRORS ``coefficients[i]`` (the same read-back float).
    """
    var = "p" if info.normalized else "r"
    out = []
    for i, value in enumerate(coefficients):
        order = info.power(i)
        out.append({
            "index": i,
            "order": order,
            "term": f"{var}^{order}",
            "normalized": bool(info.normalized),
            "header": info.header(i),
            "value": value,
        })
    return out


def asphere_type_of_name(type_name):
    """Resolve a Type-NAME string to a Tier-1 asphere key, or ``None`` (no row needed).

    The string-only sibling of ``asphere_type_of`` for the render/clearance flag blocks
    (which carry a ``type_name`` string in their per-surface dicts, not a live row). EXACT
    full-token match, NEVER a naive ``in`` — so an ``"ExtendedOddAsphere"`` name resolves
    to ExtendedOddAsphere, not OddAsphere via a substring. Refuses on ambiguity (-> None).
    """
    if not isinstance(type_name, str):
        return None
    if type_name in ASPHERE_TYPE_INFO:
        return type_name
    matches = [
        key for key in sorted(ASPHERE_TYPE_INFO, key=len, reverse=True)
        if type_name.endswith("." + key) or type_name == key
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def asphere_type_of(row):
    """Resolve ``str(row.Type)`` to a Tier-1 asphere ``surface_type`` key, or ``None``.

    EXACT FULL-TOKEN match, NEVER a naive ``in`` (the ``"OddAsphere" in
    "ExtendedOddAsphere"`` trap, AXIS 3). ``str(row.Type)`` reads back the bare
    member name (``"EvenAspheric"``/``"OddAsphere"``/``"ExtendedAsphere"``/
    ``"ExtendedOddAsphere"``) on this build, so an EXACT equality against the
    ``ASPHERE_TYPE_INFO`` keys is correct and unambiguous. A live Type that is some
    DECORATED string (e.g. ``"SurfaceType.OddAsphere"``) is matched by a
    longest-token-first endswith fallback that REFUSES on ambiguity (two keys both
    matching). A Type read THROW -> ``AsphereWriteError`` (refuse rather than guess).

    Returns the canonical key string for a Tier-1 asphere, else ``None`` (a non-asphere
    or a non-Tier-1 special type).
    """
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read a surface Type ({exc!r}); cannot classify the asphere "
            "type — refusing rather than guessing",
            field="surface_type", intended=None, actual=None, surface=None,
        ) from exc
    # Exact full-token match first (the live bare-member-name case).
    if type_name in ASPHERE_TYPE_INFO:
        return type_name
    # Longest-token-first endswith fallback for a decorated string; refuse on ambiguity
    # (NEVER a naive ``in`` substring — that conflates OddAsphere with ExtendedOddAsphere).
    matches = [
        key for key in sorted(ASPHERE_TYPE_INFO, key=len, reverse=True)
        if type_name.endswith("." + key) or type_name == key
    ]
    if len(matches) == 1:
        return matches[0]
    return None


# --------------------------------------------------------------------------- #
# Enum resolution (live-enum-is-truth, mirroring _cb._surface_type_coordinate_break).
# --------------------------------------------------------------------------- #
def _surface_type_even_asphere(system):
    """Resolve the live ``SurfaceType.EvenAspheric`` member (the §1 authoring enum).

    The SPELLING TRAP: the member is ``EvenAspheric`` (value 23), NOT
    ``EvenAsphere`` — ``getattr(SurfaceType, "EvenAsphere")`` raises ``AttributeError``.
    We resolve the CORRECT spelling via ``getattr`` (never a literal ``Enum.Parse``).
    Injected via ``_enum_types["SurfaceType"]`` (a FakeEnum with an ``EvenAspheric``
    member) for unit tests; otherwise the live ``ZOSAPI.Editors.LDE.SurfaceType``
    namespace. A resolution failure surfaces as a ``ToolParamError`` (a param-class
    problem), never an internal crash.
    """
    injected = getattr(system, "_enum_types", None)
    if injected is not None and "SurfaceType" in injected:
        from ..enums import _resolve_enum
        return _resolve_enum(injected["SurfaceType"], "EvenAspheric")
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Editors.LDE as _lde  # type: ignore

        return _lde.SurfaceType.EvenAspheric
    except Exception as exc:  # noqa: BLE001 — surface as a param error, not internal
        raise ToolParamError(
            "could not resolve SurfaceType.EvenAspheric from "
            f"ZOSAPI.Editors.LDE: {exc}"
        )


# --------------------------------------------------------------------------- #
# The EvenAspheric predicate (the describe/read layer + the asphere tools consult it).
# --------------------------------------------------------------------------- #
def is_even_asphere(row) -> bool:
    """True iff ``row`` is an EvenAspheric surface (§1).

    Keys on the ``"EvenAspheric"`` substring of ``str(row.Type)`` (the SAME idiom
    ``_cb.is_coordinate_break`` / the grating ``_is_diffraction_grating`` use). A
    ``row.Type`` read THROW -> ``AsphereWriteError`` ("refuse rather than guess the
    type") so a caller that consults this never silently treats an unreadable row as
    non-asphere.
    """
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read a surface Type ({exc!r}); cannot classify it as an "
            "even asphere — refusing rather than guessing",
            field="surface_type", intended=None, actual=None, surface=None,
        ) from exc
    return _ASPHERE_TYPE_SUBSTRING in type_name


# --------------------------------------------------------------------------- #
# Cell access (DataType-keyed + Header-drift-guarded — mirrors _cb_cells / grating).
# --------------------------------------------------------------------------- #
def _asphere_cell(system, row, param):
    """Fetch ``row.GetSurfaceCell(SurfaceColumn.ParN)`` for ``param``, THROW-guarded.

    Resolves the ``ParN`` SurfaceColumn member off the live enum (REUSING the LDE-wide
    ``_cb._surface_column_enum`` — not CB-specific), fetches the cell, and returns it.
    A raw .NET THROW on the fetch (or an unknown ``param``) resolves to a structured
    ``AsphereWriteError`` ("refuse rather than guess"), never an opaque dispatch
    ``internal``.
    """
    if param not in _PARAM_TO_COL:
        raise AsphereWriteError(
            f"unknown even-asphere parameter {param!r}; valid: {list(_PARAM_NAMES)}",
            field="asphere_param", intended=param, actual=None, surface=None,
        )
    col_name = _PARAM_TO_COL[param]
    enum_type = _cb._surface_column_enum(system)
    try:
        from ..enums import _resolve_enum
        col_member = _resolve_enum(enum_type, col_name)
        return row.GetSurfaceCell(col_member)
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001 — a fetch THROW -> surface_asphere, never internal
        raise AsphereWriteError(
            f"could not fetch the {param!r} ({col_name}) cell of an even-asphere "
            f"surface ({exc!r}); the cell layout is unreadable — refusing rather than "
            "guessing",
            field="asphere_cell", intended=param, actual=None, surface=None,
        ) from exc


def _cell_kind(cell):
    """Classify a LIVE asphere cell -> ``"int"`` / ``"double"`` from ``cell.DataType``.

    The SAME Int/Double discriminator the merit/CB/grating layers key on (Integer ->
    ``"int"``, else ``"double"``). Every asphere coefficient cell is Double live, so
    the ``_expect_layout`` drift guard rejects an Integer cell — but the kind is still
    read off the LIVE DataType so a drifted engine is CAUGHT, not assumed. A DataType
    read THROW -> ``AsphereWriteError``.
    """
    try:
        return "int" if str(cell.DataType) == "Integer" else "double"
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read an even-asphere cell DataType ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing",
            field="asphere_cell_datatype", intended=None, actual=None, surface=None,
        ) from exc


def _expect_layout(cell, param):
    """Verify the live cell's Header + DataType match the ASPHERE_PARAMS expectation.

    A live Header that does not match the expected ``"Nth Order Term"`` -> RAISE (the
    layout drifted — refuse, do not read/write the WRONG cell). A live DataType that
    disagrees with the catalog kind (every asphere cell is "double") -> RAISE (the
    table drifted from the engine; refuse rather than the wrong accessor — the
    ``_cb_cells`` / grating drift guard). Returns the verified live kind.
    """
    expected_header = _PARAM_TO_HEADER[param]
    try:
        live_header = str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read an even-asphere cell Header ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing",
            field="asphere_cell_header", intended=None, actual=None, surface=None,
        ) from exc
    if live_header != expected_header:
        raise AsphereWriteError(
            f"even-asphere cell layout mismatch for {param!r}: expected Header "
            f"{expected_header!r} but the live cell Header is {live_header!r} — "
            "refusing rather than reading/writing the wrong cell",
            field="asphere_cell_layout", intended=expected_header, actual=live_header,
            surface=None,
        )
    expected_kind = _PARAM_TO_EXPECTED_KIND[param]
    live_kind = _cell_kind(cell)
    if live_kind != expected_kind:
        raise AsphereWriteError(
            f"even-asphere cell {live_header!r} ({param}) is a {live_kind} cell live "
            f"but the asphere catalog declared a {expected_kind} cell — the table "
            "drifted from the engine; refusing rather than the wrong accessor",
            field="asphere_cell_datatype", intended=expected_kind, actual=live_kind,
            surface=None,
        )
    return live_kind


def read_asphere_cell(system, row, param):
    """Type-aware READ of one even-asphere Par cell (Double, layout-verified).

    Every asphere coefficient cell is Double, so the value is read via ``DoubleValue``
    (after the layout verify confirms the live kind is "double" — a drifted Integer
    cell is rejected by ``_expect_layout``, never read with the wrong accessor). A read
    THROW -> ``AsphereWriteError`` ("refuse rather than guess"). Returns a ``float``.
    """
    cell = _asphere_cell(system, row, param)
    _expect_layout(cell, param)
    try:
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_asphere, never internal
        raise AsphereWriteError(
            f"could not read the value of even-asphere cell {param!r} ({exc!r}); the "
            "cell read is unverifiable — refusing rather than guessing",
            field="asphere_cell_value", intended=None, actual=None, surface=None,
        ) from exc


def _coerce_double(param, value):
    """Coerce ``value`` to a FINITE float for an asphere coefficient cell.

    Reject ``bool`` (an int subclass — a client miswrite) FIRST; reject a non-number;
    reject inf/-inf/nan (non-physical for a coefficient). Writing ``0.0`` is a VALID
    clear (a real read-back-proven write), so zero is accepted. Returns the float.
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"even-asphere coefficient {param!r} must not be a bool ({value!r}); a "
            "bool is an int subclass — a client miswrite"
        )
    if not isinstance(value, (int, float)):
        raise ToolParamError(
            f"even-asphere coefficient {param!r} is a numeric cell; got "
            f"{type(value).__name__} {value!r} (need a number)"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"even-asphere coefficient {param!r} is a numeric cell; got the non-finite "
            f"value {value!r} (inf/-inf/nan are non-physical and are rejected pre-write)"
        )
    return coerced


def _readback_ok(intended, actual):
    """Read-back equality for an asphere coefficient (tight rel tol + near-ULP abs floor).

    The abs floor (``_READBACK_ABS_TOL``) is load-bearing for the tiny coefficients an
    asphere carries (1e-9..1e-15): a write of a >1e-15 magnitude that the engine
    silently collapsed to 0.0 fails the proof. The rel_tol governs normal magnitudes.
    """
    return (
        actual is not None
        and isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and math.isfinite(actual)
        and math.isclose(actual, intended, rel_tol=1e-9, abs_tol=_READBACK_ABS_TOL)
    )


def write_asphere_cell(system, row, param, value):
    """Type-aware WRITE of one even-asphere Par cell, read-back-proven (the §1 firewall).

    Steps (mirroring ``_cb_cells.write_cb_cell`` / the grating writer):

    1. fetch the cell + verify Header + DataType match the ASPHERE_PARAMS expectation
       (``_expect_layout`` — a drifted Header/kind RAISES, never the wrong cell);
    2. coerce ``value`` to a finite float (bool / non-number / non-finite all RAISE
       pre-write; ``0.0`` is a valid clear);
    3. write ``DoubleValue`` (every asphere cell is Double); a write THROW -> RAISE;
    4. read it back and verify with the tight rel tol + near-ULP abs floor; a silent
       no-op / a collapse-to-zero (the typed-setter trap, or a rejected write) -> RAISE.
       NEVER trusts the write returned without the read-back.

    Returns the read-back value.
    """
    cell = _asphere_cell(system, row, param)
    _expect_layout(cell, param)
    coerced = _coerce_double(param, value)
    try:
        cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_asphere, never internal
        raise AsphereWriteError(
            f"could not write the value {coerced!r} to even-asphere cell {param!r} "
            f"({exc!r}); the engine rejected the write — refusing rather than shipping "
            "an unverified cell",
            field="asphere_cell_write", intended=coerced, actual=None, surface=None,
        ) from exc

    actual = read_asphere_cell(system, row, param)
    if not _readback_ok(coerced, actual):
        raise AsphereWriteError(
            f"even-asphere cell {param!r} write did not read back: wrote {coerced!r}, "
            f"read {actual!r} — the engine silently rejected the write (a no-op / the "
            "typed-setter trap), refusing rather than shipping an unverified cell",
            field="asphere_cell_value", intended=coerced, actual=actual, surface=None,
        )
    return actual


# =========================================================================== #
# Generic computed-(header, kind) cell access + the gated writer.
# =========================================================================== #
def _cell_by_col(system, row, col_name):
    """Fetch ``row.GetSurfaceCell(SurfaceColumn.<col_name>)``, THROW-guarded.

    The §3 generalization of ``_asphere_cell`` keyed on a literal ``"ParN"`` column
    (not an ``ASPHERE_PARAMS`` token), so the gated coefficient cells (Par15+, Par13/
    Par14) are reachable. A raw .NET THROW resolves to a structured ``AsphereWriteError``.
    """
    enum_type = _cb._surface_column_enum(system)
    try:
        from ..enums import _resolve_enum
        col_member = _resolve_enum(enum_type, col_name)
        return row.GetSurfaceCell(col_member)
    except ToolParamError:
        raise
    except Exception as exc:  # noqa: BLE001 — a fetch THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not fetch the {col_name} cell of an asphere surface ({exc!r}); the "
            "cell layout is unreadable — refusing rather than guessing",
            field="asphere_cell", intended=col_name, actual=None, surface=None,
        ) from exc


def _expect_computed_layout(cell, col_name, expected_header, expected_kind):
    """Verify ``cell``'s live Header + DataType match a COMPUTED ``(header, kind)`` (§3).

    The §3 generalization of ``_expect_layout``: the expected Header is COMPUTED from
    the resolved type's ORDER MAP (e.g. ``"Coeff. on p^4"``) rather than looked up in
    ``ASPHERE_PARAMS``. A drifted Header -> RAISE (never write the WRONG cell — the
    over-index ``"(unused)"`` String cell is caught here). A drifted DataType -> RAISE
    (the wrong accessor). Returns the verified live kind.
    """
    try:
        live_header = str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read an asphere cell Header ({exc!r}); the cell layout is "
            "unreadable — refusing rather than guessing",
            field="asphere_cell_header", intended=None, actual=None, surface=None,
        ) from exc
    if live_header != expected_header:
        raise AsphereWriteError(
            f"asphere cell layout mismatch for {col_name}: expected Header "
            f"{expected_header!r} but the live cell Header is {live_header!r} — refusing "
            "rather than reading/writing the wrong cell (an over-index hits a "
            "'(unused)' cell)",
            field="asphere_cell_layout", intended=expected_header, actual=live_header,
            surface=None,
        )
    live_kind = _cell_kind(cell)
    if live_kind != expected_kind:
        raise AsphereWriteError(
            f"asphere cell {live_header!r} ({col_name}) is a {live_kind} cell live but "
            f"the asphere catalog declared a {expected_kind} cell — the table drifted "
            "from the engine; refusing rather than the wrong accessor",
            field="asphere_cell_datatype", intended=expected_kind, actual=live_kind,
            surface=None,
        )
    return live_kind


def read_computed_double_cell(system, row, col_name, expected_header):
    """READ a Double cell at ``col_name`` with a COMPUTED Header verify (§3)."""
    cell = _cell_by_col(system, row, col_name)
    _expect_computed_layout(cell, col_name, expected_header, "double")
    try:
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read the value of asphere cell {col_name} ({exc!r}); the cell "
            "read is unverifiable — refusing rather than guessing",
            field="asphere_cell_value", intended=None, actual=None, surface=None,
        ) from exc


def write_computed_double_cell(system, row, col_name, expected_header, value):
    """WRITE a Double cell at ``col_name`` (computed Header verify), read-back-proven (§3)."""
    cell = _cell_by_col(system, row, col_name)
    _expect_computed_layout(cell, col_name, expected_header, "double")
    coerced = _coerce_double(col_name, value)
    try:
        cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not write the value {coerced!r} to asphere cell {col_name} "
            f"({exc!r}); the engine rejected the write — refusing rather than shipping "
            "an unverified cell",
            field="asphere_cell_write", intended=coerced, actual=None, surface=None,
        ) from exc
    actual = read_computed_double_cell(system, row, col_name, expected_header)
    if not _readback_ok(coerced, actual):
        raise AsphereWriteError(
            f"asphere cell {col_name} write did not read back: wrote {coerced!r}, read "
            f"{actual!r} — the engine silently rejected the write (a no-op / the "
            "typed-setter trap), refusing rather than shipping an unverified cell",
            field="asphere_cell_value", intended=coerced, actual=actual, surface=None,
        )
    return actual


def read_gate_cell(system, row, info):
    """READ the Integer Max-Term gate cell (Par13), DataType-discriminated (§3).

    The gate is INTEGER — reading it via ``DoubleValue`` RAISES (the mandatory
    discriminator). ``_expect_computed_layout`` verifies the Header is
    ``"Maximum Term #"`` AND the kind is "int" before ``IntegerValue``.
    """
    cell = _cell_by_col(system, row, info.gate_par)
    _expect_computed_layout(cell, info.gate_par, _GATE_HEADER, "int")
    try:
        return int(cell.IntegerValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not read the Max-Term gate cell {info.gate_par} ({exc!r}); refusing "
            "rather than guessing the term count",
            field="asphere_gate", intended=None, actual=None, surface=None,
        ) from exc


def write_gate_cell(system, row, info, n):
    """WRITE the Integer Max-Term gate = ``n``, read-back-proven (clamp-aware, §3).

    The engine CLAMPS >240 silently, so the read-back — NOT the requested
    value — is the proof: a read-back != ``n`` REFUSES (the silent-clamp/reject net).
    ``n`` is already bounded <= 240 by the caller. Materializes ``n`` coefficient cells.
    """
    cell = _cell_by_col(system, row, info.gate_par)
    _expect_computed_layout(cell, info.gate_par, _GATE_HEADER, "int")
    try:
        cell.IntegerValue = int(n)
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_asphere
        raise AsphereWriteError(
            f"could not write the Max-Term gate {n!r} to {info.gate_par} ({exc!r}); the "
            "engine rejected the write — refusing rather than shipping an unverified gate",
            field="asphere_gate", intended=n, actual=None, surface=None,
        ) from exc
    actual = read_gate_cell(system, row, info)
    if actual != int(n):
        raise AsphereWriteError(
            f"the Max-Term gate {info.gate_par} did not read back: wrote {n!r}, read "
            f"{actual!r} — the engine clamped or silently rejected the term count "
            "(>240 clamps to 240); refusing rather than writing a coefficient to a "
            "cell that did not materialize",
            field="asphere_gate", intended=n, actual=actual, surface=None,
        )
    return actual


def write_gated_asphere(system, row, info, norm_radius, coeff_values):
    """Author a GATED asphere (Extended / ExtendedOdd): norm -> gate -> coefficients (§3).

    The STRICT write-order:
      1. (the caller already ChangeType'd + re-fetched + proved the type — like S1.)
      2. write **Norm Radius** (Double, ``norm_par``), read-back-proven (>0 already
         validated pre-mutation by the tool); a read-back <= 0 / mismatch -> RAISE.
      3. write **Maximum Term #** (Integer, ``gate_par``) = ``len(coeff_values)`` via the
         Integer accessor, read it back (the engine clamps >240); a divergence -> RAISE.
         This MATERIALIZES the N coefficient cells.
      4. re-fetch nothing (the live engine materializes in place; ``row`` is still the
         live proxy), then write each coefficient via its now-materialized Par cell
         (Double, read-back-proven) with the COMPUTED expected Header.

    ``norm_radius`` is validated finite & >0 by the caller. Returns
    ``(norm_back, gate_back, [coeff_read_backs])`` — all READ BACK, never echoed.
    """
    # Step 2 — Norm Radius (Double) read-back-proven.
    norm_back = write_computed_double_cell(
        system, row, info.norm_par, _NORM_HEADER, norm_radius
    )
    if not (isinstance(norm_back, float) and math.isfinite(norm_back) and norm_back > 0.0):
        raise AsphereWriteError(
            f"the Norm Radius {info.norm_par} read back {norm_back!r} (not a finite "
            "positive value) after the write — refusing rather than shipping a "
            "divide-by-zero normalization",
            field="asphere_norm_radius", intended=norm_radius, actual=norm_back,
            surface=None,
        )
    # Step 3 — Max-Term gate (Integer), read-back == N (materializes the N cells).
    n = len(coeff_values)
    gate_back = write_gate_cell(system, row, info, n)
    # Step 4 — each coefficient via its now-materialized Par cell (Double).
    coeff_backs = []
    for i, value in enumerate(coeff_values):
        col = info.coeff_par(i)
        header = info.header(i)
        coeff_backs.append(
            write_computed_double_cell(system, row, col, header, value)
        )
    return norm_back, gate_back, coeff_backs


__all__ = [
    "ASPHERE_PARAMS",
    "N_ASPHERE_TERMS",
    "ORDER_TO_PARAM",
    "is_even_asphere",
    "_surface_type_even_asphere",
    "read_asphere_cell",
    "write_asphere_cell",
    "_asphere_cell",
    "_PARAM_NAMES",
    "_INDEX_TO_PARAM",
    "_INDEX_TO_ORDER",
    # Additional type-info exports.
    "ASPHERE_TYPE_INFO",
    "ASPHERE_TYPE_NAMES",
    "AsphereTypeInfo",
    "ordinal",
    "asphere_type_of",
    "asphere_type_of_name",
    "coefficient_order_table",
    "_surface_type_member",
    "_MAX_GATED_TERMS",
    "read_computed_double_cell",
    "write_computed_double_cell",
    "read_gate_cell",
    "write_gate_cell",
    "write_gated_asphere",
]
