"""tools/_grin_cells.py — the GRIN (Gradient2 / Gradient3) cell substrate (GRIN §3).

NOT dispatchable (no ``TOOL_SPECS``). The single place the GRIN primitive reads and
writes a ``Gradient2`` surface's PARAMETER cells (Par1 ``Delta T`` .. Par8 ``Nr12``,
all Double — the numerical trace step + the base index ``n0`` + the six even-power
radial coefficients Nr2..Nr12). Every cell is Header-verified FIRST against the frozen
``GRIN_PARAMS`` label (a drifted layout RAISES rather than write the wrong cell), THEN
DataType-keyed on the live ``cell.DataType`` (Double), then read-back-proven through the
GRIN-OWN ZERO-BOUNDARY ``_readback_ok`` (a DELIBERATE deviation from the asphere/CB
abs-floor rule).

This MIRRORS ``_asphere_cells`` / ``_cb_cells`` (the same Header-drift + DataType
firewall + read-back-as-proof) but is a SEPARATE re-implementation: the GRIN Par cells
are a DIFFERENT cell table (CB Par1 = "Decenter X", grating Par1 = "Lines/µm", asphere
Par1 = "2nd Order Term", GRIN Par1 = "Delta T"), so we do NOT extend the CB/asphere
tables. We REUSE only the LDE-wide ``_cb._surface_column_enum`` (the ``SurfaceColumn.ParN``
resolver — not CB-specific) and ``_ac._surface_type_member`` (the getattr SurfaceType
resolver).

Probe-grounded rules this module encodes (``probe_grin_*_capture.json``, all
[Discovered-live-probed]):

- the enum member is ``SurfaceType.Gradient2`` — no spelling trap in this
  family; every member is resolved by ``getattr(SurfaceType, name)``, never a
  literal ``Enum.Parse``;
- the primitive's cells are exactly Par1..Par8 (Headers "Delta T", "n0", "Nr2".."Nr12"),
  all **Double** — so there is NO Int/Double branch (every accessor is
  ``DoubleValue``). Par9..Par20 are unused String cells — reading
  ``DoubleValue`` on one RAISES, so the table caps at 8 and Par9+ is NEVER
  addressed (``_UNUSED_FROM = 9``);
- classification is Header-FIRST: the live coefficient cells are exactly
  Par2..Par8 (+ Par1 Delta T); a Header verify RAISES before any numeric accessor, so a
  ``"(unused)"`` String cell is never emitted as ``0.0``;
- the coefficients are WAVELENGTH-BLIND and the index profile does NOT perturb
  the sag surface — those disclosures live in the tool/describe layers;
- exact-full-token resolvers: ``"Gradient2"`` is NOT a substring of ``"Gradient12"``
  but ``"Gradient1"`` IS ⊂ ``"Gradient10"``/``"Gradient12"``, so identity is
  gated by EXACT full-token match — NEVER ``startswith("Grad")`` / substring / casefold.

Live ZOS-API integration: exercised by a live integration test; unit-tested against the
fixture-style fake row/cell doubles, which MUST NOT import this module — so the fake's
Par map is an INDEPENDENT copy the live gate reddens on divergence.
"""
import math

from ..errors import GrinWriteError, ToolParamError
from . import _cb_cells as _cb
from . import _asphere_cells as _ac  # ONLY _surface_type_member


# =========================================================================== #
# §3.1 The PER-TYPE source of truth: the Gradient2 (radial) + Gradient3 (radial+axial)
# Par-cell tables (no module-global union; every cell op resolves off the resolved
# type's ``params``).
# =========================================================================== #
# (token, par, header, kind, role, power) — every per-type structure is a VIEW of this.
# ``role`` ∈ {"step" (Delta T — the numerical trace accuracy cell, NOT a coefficient),
# "coeff" (n0 + the radial/axial coefficients — variable-eligible)}. ``power`` is the
# coefficient's OWN exponent (radial r^n for Nr*, axial z^n for Nz* — the r-vs-z axis
# derives from the frozen Nz/Nr Header-token PREFIX, NOT from ``power``, since
# Nz2.power == Nr2.power == 2). Delta T has no power (None).
GRIN_PARAMS_GRADIENT2 = (
    ("Delta T", "Par1", "Delta T", "double", "step",  None),  # trace accuracy — NOT a coeff
    ("n0",      "Par2", "n0",      "double", "coeff", 0),
    ("Nr2",     "Par3", "Nr2",     "double", "coeff", 2),
    ("Nr4",     "Par4", "Nr4",     "double", "coeff", 4),
    ("Nr6",     "Par5", "Nr6",     "double", "coeff", 6),
    ("Nr8",     "Par6", "Nr8",     "double", "coeff", 8),
    ("Nr10",    "Par7", "Nr10",    "double", "coeff", 10),
    ("Nr12",    "Par8", "Nr12",    "double", "coeff", 12),
)
# The axial member Gradient3: radial-to-r⁶ + axial-to-z³, all
# Double, ungated. ``Nz1``↔z¹ (∫z dz = t²/2), ``Nz2``↔z² (∫z² dz = t³/3), ``Nz3``↔z³
# — the axial powers are probe-pinned.
GRIN_PARAMS_GRADIENT3 = (
    ("Delta T", "Par1", "Delta T", "double", "step",  None),
    ("n0",      "Par2", "n0",      "double", "coeff", 0),
    ("Nr2",     "Par3", "Nr2",     "double", "coeff", 2),
    ("Nr4",     "Par4", "Nr4",     "double", "coeff", 4),
    ("Nr6",     "Par5", "Nr6",     "double", "coeff", 6),
    ("Nz1",     "Par6", "Nz1",     "double", "coeff", 1),   # axial z^1
    ("Nz2",     "Par7", "Nz2",     "double", "coeff", 2),   # axial z^2
    ("Nz3",     "Par8", "Nz3",     "double", "coeff", 3),   # axial z^3
)
# TEST-COMPAT ALIAS ONLY — since the axial family landed there is no PRODUCTION reader
# left: every cell op threads the RESOLVED type's ``info`` instead. The unit suite pins
# both halves (the alias is identical to the Gradient2 table, and no production reader
# addresses it).
GRIN_PARAMS = GRIN_PARAMS_GRADIENT2
# Par9..Par20 = "Par N(unused)" String cells — NEVER fetched (the maps never address
# them; the Header verify RAISES before any numeric accessor if one ever were). Both
# types cap at Par8.
_UNUSED_FROM = 9


class GrinTypeInfo:
    """One GRIN ``surface_type``'s descriptor — the SINGLE per-type source of truth.

    The per-instance lookups (``_par_of`` / ``_header_of`` / ``_eligible``) are built from
    ``params`` in ``__init__`` — there is NO module-global union map; every substrate cell
    op resolves off the RESOLVED type's ``info``, so a future member that ADDS a
    token can never silently collide with another type's cell (a divergent shared token is
    an import crash via ``_validate_grin_type_info``, not a runtime silent-wrong).
    """

    __slots__ = (
        "surface_type", "member", "type_token", "gated", "params",
        "radial_tokens", "axial_tokens", "default_n0", "default_delta_t",
        "cell_index_space",
        "_par_of", "_header_of", "_eligible",
    )

    def __init__(self, surface_type, *, member, type_token, gated, params,
                 radial_tokens, default_n0, default_delta_t, axial_tokens=(),
                 cell_index_space=None):
        self.surface_type = surface_type
        self.member = member                 # the SurfaceType member NAME (getattr-resolved)
        self.type_token = type_token         # the exact ``str(row.Type)`` token
        self.gated = gated                   # Gradient2/3 are ungated (no Integer Order cell)
        self.params = params                 # the per-type ordered table
        self.radial_tokens = radial_tokens   # the radial coefficient tokens (minus n0)
        self.axial_tokens = axial_tokens     # the axial coefficient tokens (Gradient3 -> Nz*)
        # A CELL value, in this type's own cell space (see ``cell_index_space`` below), NOT
        # a physical index: the two types' defaults therefore differ NUMERICALLY while
        # denoting the SAME physical index 1.5 (Gradient2 cell 2.25 = 1.5², Gradient3 cell
        # 1.5). A single shared literal here would mean physical index 1.2247 on a
        # Gradient2 — exactly the convention trap this module exists to close.
        self.default_n0 = default_n0
        self.default_delta_t = default_delta_t
        # The CELL convention for this type's index polynomial (live-falsified
        # against the plano-convex ``EFFL == R/(n-1)`` oracle). This describes what the
        # Par CELLS hold, NOT what the index operands report (the operands report the TRUE
        # PHYSICAL INDEX for EVERY type — there is no per-type report space):
        # ``"index_squared"`` — the cell polynomial is n² (Gradient2: n² = n0 + Nr2·r² + …);
        # ``"index"``         — the cell polynomial is n  (Gradient3: n = n0 + Nr2·r² + … + Nz1·z + …).
        # ``None`` = UNKNOWN/future type -> the floor FAILS CLOSED (authors nothing), never a
        # silently-wrong-space index box. Keyed on TYPE, not on axial-ness.
        self.cell_index_space = cell_index_space
        # Per-instance lookups (NO module-global union; built from this type's params).
        self._par_of = {r[0]: r[1] for r in params}
        self._header_of = {r[0]: r[2] for r in params}
        self._eligible = frozenset(r[0] for r in params if r[4] == "coeff")

    def par_of(self, token):
        """The ``ParN`` for ``token`` in THIS type (raises ``KeyError`` on an unknown token)."""
        return self._par_of[token]

    def header_of(self, token):
        """The expected live Header for ``token`` in THIS type."""
        return self._header_of[token]

    def is_variable_eligible(self, token):
        """True iff ``token`` is a variable-eligible coefficient of THIS type (n0 + Nr*/Nz*).

        The ONE shared predicate, relocated per-type (the module-global is GONE): Delta T
        (role "step") and any Integer control cell are EXCLUDED.
        """
        return token in self._eligible

    def radial_cells(self):
        """The RADIAL coefficient rows (``Nr*``), ordered by ``params`` — NOT the axial Nz*.

        Contract matches the name (and ``radial_tokens``): for Gradient2 this is Nr2..Nr12
        (byte-identical to the prior behaviour); for Gradient3 it EXCLUDES the axial Nz*
        cells (use ``coeff_cells()`` / ``coeff_map_tokens()`` for the radial∪axial set).
        """
        return tuple(r for r in self.params
                     if r[4] == "coeff" and r[0] != "n0" and r[0].startswith("Nr"))

    def coeff_cells(self):
        """The coefficient rows minus n0 (radial ∪ axial), ordered by ``params``.

        For Gradient2 == ``radial_cells()``; for Gradient3 the radial (Nr2/Nr4/Nr6) AND
        axial (Nz1/Nz2/Nz3) rows — the ``coefficients``-map / ``coefficient_orders`` set.
        """
        return tuple(r for r in self.params if r[4] == "coeff" and r[0] != "n0")

    def coeff_map_tokens(self):
        """The ``coefficients``-map key set (radial ∪ axial minus n0), ordered.

        Gradient2 == ``radial_tokens`` (byte-identical); Gradient3 == (Nr2,Nr4,Nr6,Nz1,Nz2,Nz3).
        """
        return tuple(r[0] for r in self.coeff_cells())

    def coeff_tokens(self):
        """The variable-eligible tokens, ordered: ("n0", <radial…>, <axial…>)."""
        return tuple(r[0] for r in self.params if r[4] == "coeff")

    def token_row(self, token):
        """The ``params`` row for ``token`` in THIS type (raises ``KeyError`` on unknown)."""
        for r in self.params:
            if r[0] == token:
                return r
        raise KeyError(token)


# The frozen per-type descriptors (seeded 1:1 from findings): ungated, no norm
# radius, radius/conic in the STANDARD columns, material inert, wavelength-blind. Gradient2
# is the radial primitive; Gradient3 is the radial-to-r⁶ + axial-to-z³ member.
GRIN_TYPE_INFO = {
    "Gradient2": GrinTypeInfo(
        "Gradient2", member="Gradient2", type_token="Gradient2", gated=False,
        params=GRIN_PARAMS_GRADIENT2,
        radial_tokens=("Nr2", "Nr4", "Nr6", "Nr8", "Nr10", "Nr12"),
        default_n0=2.25, default_delta_t=1.0,   # CELL value: 2.25 = 1.5² -> physical index 1.5
        # The Gradient2 CELL polynomial is n SQUARED (n² = n0 + Nr2·r² + …),
        # so the physical base index is sqrt(the n0 cell).
        cell_index_space="index_squared",
    ),
    "Gradient3": GrinTypeInfo(
        "Gradient3", member="Gradient3", type_token="Gradient3", gated=False,
        params=GRIN_PARAMS_GRADIENT3,
        radial_tokens=("Nr2", "Nr4", "Nr6"),
        axial_tokens=("Nz1", "Nz2", "Nz3"),
        default_n0=1.5, default_delta_t=1.0,    # CELL value: the index itself -> physical index 1.5
        # The Gradient3 CELL polynomial is the INDEX itself (n = n0 + …).
        cell_index_space="index",
    ),
}

# The 12-member frozen GRIN FAMILY recognition set — every ``str(row.Type)``
# token a GRIN-family member reads back as. NOT the authorable set (only Gradient2 and
# Gradient3 are authorable); this gates CLASSIFICATION (the spare arm) + the enumerator's
# "not GRIN at all (skip) vs recognized-GRIN-we-don't-author (fault/spare)" split. The
# ``Gradient1`` ⊂ ``Gradient10``/``Gradient12`` substring hazard is LIVE here — so the
# resolver is EXACT-full-token, never a substring.
GRIN_FAMILY_TYPE_TOKENS = frozenset({
    "Gradient1", "Gradient2", "Gradient3", "Gradient4", "Gradient5", "Gradient6",
    "Gradient7", "Gradient9", "Gradient10", "Gradient12", "Gradium", "GridGradient",
})


# =========================================================================== #
# §3.2 Resolvers (exact-full-token; NEVER startswith("Grad") / substring / casefold).
# =========================================================================== #
def _exact_token_match(type_name, universe):
    """EXACT-full-token resolution of ``type_name`` against ``universe`` (a set/dict).

    Exact equality first (the live bare-member-name case), then a longest-token-first
    ``endswith("." + key)`` fallback for a DECORATED string ("SurfaceType.Gradient2"),
    REFUSING on ambiguity (two keys both matching) -> ``None``. NEVER a naive ``in``
    substring (the ``"Gradient1"`` ⊂ ``"Gradient10"``/``"Gradient12"`` trap) and NEVER a
    ``startswith("Grad")`` / casefold. Clone of ``asphere_type_of_name``.
    """
    if not isinstance(type_name, str):
        return None
    if type_name in universe:
        return type_name
    matches = [
        key for key in sorted(universe, key=len, reverse=True)
        if type_name.endswith("." + key) or type_name == key
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def grin_type_of_name(type_name):
    """Resolve a Type-NAME string to an AUTHORABLE GRIN key, or ``None`` (§3.2).

    Over the ``GRIN_TYPE_INFO`` keys — BOTH ``Gradient2`` (radial) AND ``Gradient3``
    (radial+axial), the tokens ``set_grin`` authors. This is the AUTHORING +
    describe/disclosure + CLASSIFY resolver: a loaded NON-authorable family member
    (``Gradium`` / ``GridGradient`` / ``Gradient1/4/5/6/7/9/10/12``) is ``None`` here (it
    collapses to "not an authorable primitive"); the enumerator / the spare arm / the
    Not-audited disclosure use the FAMILY resolver below to tell "not GRIN at all"
    from "recognized-but-not-authorable". The CLASSIFY keys on THIS resolver,
    never the family recognizer (only an authorable primitive is proven to have its
    following gap == its gradient-medium body).
    """
    return _exact_token_match(type_name, GRIN_TYPE_INFO)


def grin_type_of(row):
    """Resolve ``str(row.Type)`` to an authorable GRIN key, or ``None`` (the row sibling).

    A ``row.Type`` read THROW -> ``GrinWriteError`` (refuse rather than guess — clone
    ``asphere_type_of``).
    """
    try:
        type_name = str(row.Type)
    except Exception as exc:  # noqa: BLE001 — a Type read THROW -> surface_grin
        raise GrinWriteError(
            f"could not read a surface Type ({exc!r}); cannot classify the GRIN type — "
            "refusing rather than guessing",
            field="surface_type", intended=None, actual=None, surface=None,
        ) from exc
    return grin_type_of_name(type_name)


def grin_family_type_of_name(type_name):
    """RECOGNITION-ONLY resolver over the 12-member ``GRIN_FAMILY_TYPE_TOKENS`` (§3.2).

    Does NOT make a member authorable — it only answers "is this a GRIN family member?"
    (and which). TWO consumers: the classifier spare arm AND the §4.1
    enumerator's type gate (the "not-GRIN skip vs recognized-GRIN-we-don't-author
    fault/spare" discrimination the authorable resolver structurally cannot make). EXACT
    full-token (the ``Gradient1`` ⊂ ``Gradient10``/``Gradient12`` hazard is LIVE here).
    """
    return _exact_token_match(type_name, GRIN_FAMILY_TYPE_TOKENS)


# =========================================================================== #
# §3.2 (d) — the two LIVE-ROW tri-state classify/recognition predicates.
# The CLASSIFY keys on the AUTHORABLE resolver (row_is_grin_primitive); the not-audited
# DISCLOSURE set = a FAMILY member that is NOT an authorable primitive. NEVER raises.
# =========================================================================== #
def row_is_grin_primitive(row):
    """Tri-state: True iff ``row.Type`` is an AUTHORABLE GRIN primitive (Gradient2/
    Gradient3 — the tokens set_grin authors, probe-proven for the following-gap =
    gradient-medium-body association); False if a readable non-primitive Type; None if
    the Type read THROWS. NEVER raises. This is the CLASSIFY predicate."""
    try:
        raw_type = str(row.Type)
    except Exception:  # noqa: BLE001 — unreadable Type -> unprovable (fail-closed to None)
        return None
    try:
        return grin_type_of_name(raw_type) is not None
    except Exception:  # noqa: BLE001 — a resolver hiccup over a stored string -> unprovable
        return None


def row_is_grin_family(row):
    """Tri-state: True iff ``row.Type`` is any of the 12 GRIN family tokens; False if a
    readable non-GRIN Type; None if the Type read THROWS. NEVER raises. RECOGNITION only
    — used to detect the NOT-AUDITED disclosure set (a family member that is NOT an
    authorable primitive, i.e. row_is_grin_family True AND row_is_grin_primitive not
    True). This is NEVER the classify predicate."""
    try:
        raw_type = str(row.Type)
    except Exception:  # noqa: BLE001
        return None
    try:
        return grin_family_type_of_name(raw_type) is not None
    except Exception:  # noqa: BLE001
        return None


def _surface_type_grin(system, name):
    """Resolve the live ``SurfaceType.<name>`` member (reuse ``_ac._surface_type_member``).

    getattr-only (never ``Enum.Parse``); a resolution failure surfaces as a
    ``ToolParamError`` (a param-class problem), never an internal crash. Injected via
    ``_enum_types["SurfaceType"]`` for unit tests; otherwise the live namespace.
    """
    return _ac._surface_type_member(system, name)


# =========================================================================== #
# §3.3 Cell access (Header-FIRST) + the ZERO-BOUNDARY read-back.
# =========================================================================== #
def _grin_cell(system, row, par):
    """Fetch ``row.GetSurfaceCell(SurfaceColumn.<par>)``, THROW-guarded (clone ``_cell_by_col``).

    Resolves the ``ParN`` SurfaceColumn member off the live enum (REUSING the LDE-wide
    ``_cb._surface_column_enum`` — not CB-specific). A raw .NET THROW on the fetch resolves
    to a structured ``GrinWriteError`` ("refuse rather than guess"), never an opaque
    dispatch ``internal``.
    """
    try:
        # The SurfaceColumn enum DISCOVERY is INSIDE the guard too — the same fail-closed
        # rule as the member fetch below, and for the same reason (leaving it outside was
        # a sibling of exactly the hole this guard was added to close).
        # ``_cb._surface_column_enum`` raises ``ToolParamError`` on a resolution
        # failure; on the authoring path this runs AFTER the ChangeType, so an un-guarded
        # discovery throw would escape as ``grin_param`` over an already-mutated Gradient2
        # surface — the exact ``surface_grin`` (partial_state) invariant established
        # for the ``_resolve_enum`` fetch. Wrapping it here maps EVERY enum/layout failure
        # (discovery OR member-resolution OR the GetSurfaceCell fetch) to ``GrinWriteError``.
        enum_type = _cb._surface_column_enum(system)
        from ..enums import _resolve_enum
        col_member = _resolve_enum(enum_type, par)
        return row.GetSurfaceCell(col_member)
    except Exception as exc:  # noqa: BLE001 — a discover/resolve/fetch THROW -> surface_grin, never internal
        # A ``ParN`` SurfaceColumn-resolution failure (``_resolve_enum`` raises
        # ``ToolParamError``) is a surface-LAYOUT problem, NOT a user-param problem — surface
        # it as ``GrinWriteError`` so an authoring-path cell resolution that fails AFTER the
        # ChangeType carries ``partial_state`` (via the ``SurfaceWriteError`` catch), never a
        # clean, non-partial ``grin_param`` misclassification over a mutated Gradient2 surface.
        raise GrinWriteError(
            f"could not fetch the {par} cell of a GRIN surface ({exc!r}); the cell layout "
            "is unreadable — refusing rather than guessing",
            field="grin_cell", intended=par, actual=None, surface=None,
        ) from exc


def _cell_kind(cell):
    """Classify a LIVE GRIN cell from ``cell.DataType`` — ONLY ``"Double"`` is ``"double"``.

    Every GRIN Par cell is Double live, so ``_expect_grin_layout`` accepts ONLY a ``"double"``
    kind — but the kind is read off the LIVE DataType so a drifted engine is CAUGHT, not
    assumed. **The accept-set is EXACTLY Double**: ``"Double"`` -> ``"double"``,
    ``"Integer"`` -> ``"int"``, and ANY OTHER token (``"String"``, an unknown DataType) ->
    the RAW lower-cased token (NEVER ``"double"``) — so a Header-correct *String* Par cell is
    NOT silently classified Double and mutated as the wrong cell (the pre-fix
    ``!= "Integer" else "double"`` collapsed String -> Double, defeating the exact-Double
    layout gate for the writer, enumerator, and clear refetch). A DataType read THROW ->
    ``GrinWriteError``.
    """
    try:
        token = str(cell.DataType)
    except Exception as exc:  # noqa: BLE001 — a DataType read THROW -> surface_grin
        raise GrinWriteError(
            f"could not read a GRIN cell DataType ({exc!r}); the cell kind is "
            "unclassifiable — refusing rather than guessing",
            field="grin_cell_datatype", intended=None, actual=None, surface=None,
        ) from exc
    if token == "Double":
        return "double"
    if token == "Integer":
        return "int"
    # Any OTHER token (String / unknown) is NOT a Double cell — return the raw kind so the
    # exact-Double layout gate (``_expect_grin_layout``) REFUSES it; never fall through to
    # "double" (the silent-wrong: a String cell emitted/cleared as the Double token).
    return token.lower() if token else "unknown"


def _expect_grin_layout(cell, token, info):
    """Verify the live cell's Header (FIRST) + DataType (Double) match ``info`` (§3.3).

    Header verified FIRST against the expected per-type ``info.header_of(token)`` label -> a
    drift RAISES, never the wrong cell (the ``"Par N(unused)"`` String firewall: the Header
    verify RAISES before any numeric accessor, so an over-index never emits ``0.0``). THEN the
    live DataType must be Double. Returns the verified live kind.

    **The B §0 safety invariant is LOAD-BEARING:** a wrong-``info`` call on a NON-shared
    token (``"Nz1"`` against a Gradient2 row whose Par6 live Header is ``"Nr8"``) RAISES on the
    Header mismatch; a SHARED token (``"Nr2"`` — Par3/Header identical in both types) reads the
    same cell (harmless). The Header-verify is the correctness boundary on every cell op.
    """
    if token not in info._header_of:
        raise GrinWriteError(
            f"unknown GRIN cell token {token!r} for {info.type_token}; valid: "
            f"{list(info._header_of)}",
            field="grin_cell", intended=token, actual=None, surface=None,
        )
    expected_header = info.header_of(token)
    try:
        live_header = str(cell.Header)
    except Exception as exc:  # noqa: BLE001 — a Header read THROW -> surface_grin
        raise GrinWriteError(
            f"could not read a GRIN cell Header ({exc!r}); the cell layout is unreadable "
            "— refusing rather than guessing",
            field="grin_cell_header", intended=None, actual=None, surface=None,
        ) from exc
    if live_header != expected_header:
        raise GrinWriteError(
            f"GRIN cell layout mismatch for {token!r}: expected Header {expected_header!r} "
            f"but the live cell Header is {live_header!r} — refusing rather than "
            "reading/writing the wrong cell",
            field="grin_cell_layout", intended=expected_header, actual=live_header,
            surface=None,
        )
    live_kind = _cell_kind(cell)
    if live_kind != "double":
        raise GrinWriteError(
            f"GRIN cell {live_header!r} ({token}) is a {live_kind} cell live but the GRIN "
            "catalog declared a double cell — the table drifted from the engine; refusing "
            "rather than the wrong accessor",
            field="grin_cell_datatype", intended="double", actual=live_kind, surface=None,
        )
    return live_kind


def _coerce_double(token, value):
    """Coerce ``value`` to a FINITE float for a GRIN Par cell (clone the asphere coercer).

    Reject ``bool`` (an int subclass — a client miswrite) FIRST; reject a non-number;
    reject inf/-inf/nan (non-physical for a coefficient). Writing ``0.0`` is a VALID clear
    (the complete-state reset writes 0.0 for omitted terms), so zero is accepted.
    """
    if isinstance(value, bool):
        raise ToolParamError(
            f"GRIN coefficient {token!r} must not be a bool ({value!r}); a bool is an int "
            "subclass — a client miswrite"
        )
    if not isinstance(value, (int, float)):
        raise ToolParamError(
            f"GRIN coefficient {token!r} is a numeric cell; got {type(value).__name__} "
            f"{value!r} (need a number)"
        )
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"GRIN coefficient {token!r} is a numeric cell; got the non-finite value "
            f"{value!r} (inf/-inf/nan are non-physical and are rejected pre-write)"
        )
    return coerced


def _readback_ok(intended, actual):
    """The ZERO-BOUNDARY read-back oracle — a DELIBERATE deviation from asphere.

    The asphere/CB ``_readback_ok`` uses ``abs_tol=1e-15``, which certifies
    ``isclose(5e-16, 0.0)`` — so (a) a 0.0 write the engine NO-OPS over a stale
    ``Nr12=5e-16`` would pass (defeating the complete-state reset) and (b) a tiny
    intended ``5e-16`` the engine COLLAPSES to 0.0 would also pass. Both are silent-wrong.
    The GRIN rule is:

    1. ``actual`` must be a finite non-bool number (as shipped);
    2. **exactly one of intended/actual == 0.0 -> UNCONDITIONAL FAIL** (no abs-floor
       forgiveness in either direction — kills both silent-wrongs);
    3. both zero -> pass; both nonzero -> ``math.isclose(rel_tol=1e-9, abs_tol=0.0)``.

    The engine stores written doubles verbatim (round-trip residual 0;
    revert clears to exact 0.0), so exact-zero comparison is live-faithful.
    """
    if (actual is None
            or not isinstance(actual, (int, float))
            or isinstance(actual, bool)
            or not math.isfinite(actual)):
        return False
    intended_zero = (intended == 0.0)
    actual_zero = (actual == 0.0)
    if intended_zero != actual_zero:
        return False  #: exactly one zero -> unconditional mismatch
    if intended_zero and actual_zero:
        return True
    return math.isclose(actual, intended, rel_tol=1e-9, abs_tol=0.0)


def read_grin_cell(system, row, token, info):
    """Type-aware READ of one GRIN Par cell (Double, layout-verified) -> float (§3.3).

    Every GRIN coefficient cell is Double, so the value is read via ``DoubleValue`` (after
    the layout verify confirms Double — a drifted Integer/String cell is rejected). ``info``
    is the resolved ``GrinTypeInfo`` (per-type Par/Header map). A read THROW ->
    ``GrinWriteError`` ("refuse rather than guess").
    """
    if token not in info._par_of:
        raise GrinWriteError(
            f"unknown GRIN cell token {token!r} for {info.type_token}; valid: "
            f"{list(info._par_of)}",
            field="grin_cell", intended=token, actual=None, surface=None,
        )
    cell = _grin_cell(system, row, info.par_of(token))
    _expect_grin_layout(cell, token, info)
    try:
        return float(cell.DoubleValue)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_grin, never internal
        raise GrinWriteError(
            f"could not read the value of GRIN cell {token!r} ({exc!r}); the cell read is "
            "unverifiable — refusing rather than guessing",
            field="grin_cell_value", intended=None, actual=None, surface=None,
        ) from exc


def write_grin_cell(system, row, token, value, info):
    """Type-aware WRITE of one GRIN Par cell, read-back-proven (§3.3 firewall).

    Steps (mirroring ``write_computed_double_cell``): fetch + Header/DataType verify
    (``_expect_grin_layout`` — a drift RAISES, never the wrong cell) -> coerce ``value`` to
    a finite float (bool / non-number / non-finite RAISE; ``0.0`` valid) -> write
    ``DoubleValue`` (a write THROW RAISES) -> read it back and verify through the
    ZERO-BOUNDARY ``_readback_ok`` (a silent no-op / a collapse-to-zero / a stale-tiny
    retention on a zero write RAISES). ``info`` is the resolved per-type map.
    Returns the read-back value.
    """
    if token not in info._par_of:
        raise GrinWriteError(
            f"unknown GRIN cell token {token!r} for {info.type_token}; valid: "
            f"{list(info._par_of)}",
            field="grin_cell", intended=token, actual=None, surface=None,
        )
    cell = _grin_cell(system, row, info.par_of(token))
    _expect_grin_layout(cell, token, info)
    coerced = _coerce_double(token, value)
    try:
        cell.DoubleValue = coerced
    except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_grin, never internal
        raise GrinWriteError(
            f"could not write the value {coerced!r} to GRIN cell {token!r} ({exc!r}); the "
            "engine rejected the write — refusing rather than shipping an unverified cell",
            field="grin_cell_write", intended=coerced, actual=None, surface=None,
        ) from exc
    actual = read_grin_cell(system, row, token, info)
    if not _readback_ok(coerced, actual):
        raise GrinWriteError(
            f"GRIN cell {token!r} write did not read back: wrote {coerced!r}, read "
            f"{actual!r} — the engine silently rejected the write (a no-op / a "
            "collapse-to-zero / a stale-tiny retention on a zero write), refusing rather "
            "than shipping an unverified cell",
            field="grin_cell_value", intended=coerced, actual=actual, surface=None,
        )
    return actual


# =========================================================================== #
# §3.4 The r/z label source — the ONE axis-labeller.
# =========================================================================== #
def _term_label(token, power):
    """``r^n`` for a radial coeff, ``z^n`` for an axial coeff, ``"r^0"`` for n0; ``None`` for Delta T.

    The axis derives from the frozen Nz/Nr Header-token PREFIX — NEVER from
    ``power`` (Nz2.power == Nr2.power == 2: power cannot discriminate the axis).
    """
    if power is None:
        return None
    return f"z^{power}" if token.startswith("Nz") else f"r^{power}"


# =========================================================================== #
# §3.2 (c) The import-time consistency guard (a malformed table is an import crash,
# not a runtime silent-wrong).
# =========================================================================== #
def _validate_grin_type_info():
    """Cross-type consistency assert over ``GRIN_TYPE_INFO`` — runs at module load.

    (a) every ``Nz*``/``Nr*`` token's ``power`` matches its numeral suffix (the label-
        derivation soundness pin); (b) tokens unique within a type; (c) a token
        SHARED across types maps to the SAME ``par`` AND ``header`` (the future-member
        collision a module-global union would hit silently becomes an import crash).
    """
    shared = {}   # token -> (par, header) from the first type it appears in
    for info in GRIN_TYPE_INFO.values():
        tokens = [r[0] for r in info.params]
        if len(tokens) != len(set(tokens)):
            raise ValueError(
                f"GRIN type {info.type_token!r}: duplicate token(s) in {tokens}"
            )
        for (tok, par, header, _kind, _role, power) in info.params:
            if tok.startswith("Nz") or tok.startswith("Nr"):
                suffix = tok[2:]
                if not (suffix.isdigit() and int(suffix) == power):
                    raise ValueError(
                        f"GRIN type {info.type_token!r}: token {tok!r} power {power!r} "
                        f"does not match its numeral suffix {suffix!r}"
                    )
            prev = shared.get(tok)
            if prev is None:
                shared[tok] = (par, header)
            elif prev != (par, header):
                raise ValueError(
                    f"GRIN token {tok!r} maps to {(par, header)!r} in "
                    f"{info.type_token!r} but {prev!r} in an earlier type — a shared "
                    "token must map to the SAME cell across types"
                )


_validate_grin_type_info()


__all__ = [
    "GRIN_PARAMS",
    "GRIN_PARAMS_GRADIENT2",
    "GRIN_PARAMS_GRADIENT3",
    "_UNUSED_FROM",
    "GrinTypeInfo",
    "GRIN_TYPE_INFO",
    "GRIN_FAMILY_TYPE_TOKENS",
    "grin_type_of_name",
    "grin_type_of",
    "grin_family_type_of_name",
    "row_is_grin_primitive",
    "row_is_grin_family",
    "_surface_type_grin",
    "_grin_cell",
    "_expect_grin_layout",
    "read_grin_cell",
    "write_grin_cell",
    "_coerce_double",
    "_readback_ok",
    "_term_label",
    "_validate_grin_type_info",
]
