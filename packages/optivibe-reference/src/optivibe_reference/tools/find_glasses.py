"""tools/find_glasses.py — range/window glass search (session-free, never-raise).

The 4th MCP glass tool: a parameterized window search over the re-sourced .agf
glass catalog. Handler signature ``find_glasses(db_conn, params)`` — arg-0 is the
GLASS catalog connection the ref ``Dispatcher`` threads positionally by
``spec.kind=='glass'`` (the same routing ``lookup_glass`` rides).

USE CASE — apochromat / secondary-color glass selection: the designer narrows the
glass space by an Abbe-number window (Vd), a relative-partial-dispersion window
(computed Pg,F), a refractive-index window (Nd), or the manufacturer's anomalous
partial-dispersion offset (ΔPg,F, raw/unvalidated). It returns the matching valid
rows so the designer can hand-pick crown/flint candidates.

NEVER-RAISE contract (mirrors ``lookup_glass`` / §7):
- parameterized SQL throughout;
- every numeric bound is validated with the bool-before-float trap FIRST
  (``True == 1.0`` would otherwise slip a bool bound through) + NaN/inf rejected
  via ``math.isfinite`` (a non-finite bound is structurally invalid, NOT a
  legitimate window edge);
- a ``min > max`` window is a ``ToolParamError`` (an empty-by-construction window
  is a caller mistake, not a legitimately-empty result);
- AT LEAST ONE window bound is required (a bare ``find_glasses`` with no window
  would dump the whole 4783-row catalog — refuse it as a ``ToolParamError``).

THE VALIDITY GATE (the cardinal defense surfaced at the window boundary): a
computed field (``vd``, ``pg_f``, ``nd``) is NULL in the row when it was NOT
in-band-valid. Every window predicate is built as
``<field> IS NOT NULL AND <field> BETWEEN ? AND ?`` so an out-of-band row whose
``vd`` is null can NEVER match a Vd window — only rows with a genuinely-computed
in-band value are returned.

THE ΔPg,F CAVEAT: ``delta_pg_f`` is stored VERBATIM from the ``.agf`` (ED token 4)
and is UNVALIDATED raw vendor data (the probe saw values down to -81.0). A
``delta_pg_f`` window matches on that raw stored value, and the result carries
``delta_pg_f_unvalidated: True`` so the caller knows the trustworthy
partial-dispersion quantity is the COMPUTED ``pg_f`` (with its ``pg_f_valid``
gate), not this field.

An empty result over a WELL-FORMED window is ``ok: True`` with an empty
``candidates`` list — a valid window can legitimately match nothing.
"""
import math

from ..errors import ToolParamError
from ..server import ToolSpec
from ._glass_common import normalize_catalog

_TOOL = "find_glasses"

# The default candidate count AND the hard ceiling for the window search.
_DEFAULT_LIMIT = 50
_HARD_CAP = 200

# The result columns returned per candidate row, in a stable SELECT order. One
# source of truth for the SELECT list and the dict keys (mirror GLASS_COLUMNS).
_RESULT_COLUMNS = (
    "catalog",
    "name",
    "vd",
    "pg_f",
    "delta_pg_f",
    "nd",
    "formula",
    "formula_name",
    "min_wave",
    "max_wave",
)

# The window params: each maps the public ``<field>_min`` / ``<field>_max`` pair
# onto a real glass column. ``valid_only=True`` adds the ``<col> IS NOT NULL``
# clause (the cardinal-defense gate — a NULL computed field is out-of-band and
# must never match a window). ``delta_pg_f`` is raw/unvalidated so it does NOT
# get the NOT NULL gate keyed to a *_valid flag (its NULLs are absent-in-.agf,
# not out-of-band), but a NULL still cannot satisfy a BETWEEN, so SQLite filters
# it naturally; we mark the result as unvalidated instead.
_WINDOW_FIELDS = (
    # (public_prefix, column, valid_only)
    ("vd", "vd", True),
    ("pg_f", "pg_f", True),
    ("nd", "nd", True),
    ("delta_pg_f", "delta_pg_f", False),
)


def _validate_bound(name, value):
    """Validate one numeric window bound; return float or raise (never None here).

    bool-before-float FIRST (``True == 1.0`` slips past a float check), then a
    non-numeric type, then NaN/inf (a non-finite bound is structurally invalid).
    """
    if isinstance(value, bool):
        raise ToolParamError(f"'{name}' must be a number, not a bool")
    if not isinstance(value, (int, float)):
        raise ToolParamError(
            f"'{name}' must be a number; got {type(value).__name__}"
        )
    if not math.isfinite(value):
        raise ToolParamError(f"'{name}' must be a finite number")
    return float(value)


def _resolve_limit(params):
    """Resolve the effective candidate cap (default 50, hard cap 200).

    bool-before-int trap FIRST, then non-int, then ``< 1`` rejected. Applied as
    ``min(override, hard_cap)``.
    """
    if "limit" not in params:
        return _DEFAULT_LIMIT
    value = params["limit"]
    if isinstance(value, bool):
        raise ToolParamError("'limit' must be an int, not a bool")
    if not isinstance(value, int):
        raise ToolParamError(f"'limit' must be an int; got {type(value).__name__}")
    if value < 1:
        raise ToolParamError(f"'limit' must be >= 1; got {value}")
    return min(value, _HARD_CAP)


def _collect_windows(params):
    """Collect + validate the supplied window bounds.

    Returns ``(clauses, sql_params, queried_fields)`` where ``clauses`` is the list
    of SQL predicate strings and ``sql_params`` the matching bound values. Raises
    ``ToolParamError`` on a bad bound or an inverted (min>max) window. Returns no
    clauses when no window param is present (the caller turns that into the
    "give at least one window" error).
    """
    clauses = []
    sql_params = []
    queried_fields = []
    for prefix, column, valid_only in _WINDOW_FIELDS:
        min_key = f"{prefix}_min"
        max_key = f"{prefix}_max"
        has_min = min_key in params
        has_max = max_key in params
        if not (has_min or has_max):
            continue
        queried_fields.append(prefix)
        lo = _validate_bound(min_key, params[min_key]) if has_min else None
        hi = _validate_bound(max_key, params[max_key]) if has_max else None
        if lo is not None and hi is not None and lo > hi:
            raise ToolParamError(
                f"'{min_key}' ({lo}) must be <= '{max_key}' ({hi})"
            )
        # The validity gate: a computed field is NULL when out-of-band, so require
        # NOT NULL for the validated fields so an out-of-band row can never match.
        if valid_only:
            clauses.append(f"{column} IS NOT NULL")
        if lo is not None and hi is not None:
            clauses.append(f"{column} BETWEEN ? AND ?")
            sql_params.extend((lo, hi))
        elif lo is not None:
            clauses.append(f"{column} >= ?")
            sql_params.append(lo)
        else:
            clauses.append(f"{column} <= ?")
            sql_params.append(hi)
    return clauses, sql_params, queried_fields


def _row_to_dict(row):
    return dict(zip(_RESULT_COLUMNS, row))


def find_glasses(db_conn, params):
    """Window-search the glass catalog by Vd / Pg,F / Nd / ΔPg,F (never raises).

    See the module docstring for the full contract.
    """
    catalog = normalize_catalog(params.get("catalog"))
    limit = _resolve_limit(params)
    clauses, sql_params, queried_fields = _collect_windows(params)

    if not queried_fields:
        raise ToolParamError(
            "give at least one of vd_/pg_f_/nd_/delta_pg_f_ window bounds"
        )

    where_parts = list(clauses)
    if catalog is not None:
        # catalog is normalized to the stored ``<BASENAME>.AGF`` form; a single
        # case-insensitive equality matches the stored value.
        where_parts.append("LOWER(catalog) = LOWER(?)")
        sql_params.append(catalog)

    where_sql = " AND ".join(where_parts)
    # Fetch one extra to detect truncation, then order by vd (a sensible, stable
    # key for crown/flint scanning) with name as a tiebreaker for determinism.
    cols = ", ".join(_RESULT_COLUMNS)
    sql = (
        f"SELECT {cols} FROM glass WHERE {where_sql} "
        "ORDER BY vd IS NULL, vd, catalog, name LIMIT ?"
    )
    rows = db_conn.execute(sql, (*sql_params, limit + 1)).fetchall()

    truncated = len(rows) > limit
    rows = rows[:limit]
    candidates = [_row_to_dict(r) for r in rows]

    result = {
        "ok": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "truncated": truncated,
        "match_kind": "range",
        "queried_fields": queried_fields,
    }
    if "delta_pg_f" in queried_fields:
        # Flag the caveat: delta_pg_f is raw, unvalidated .agf data.
        result["delta_pg_f_unvalidated"] = True
        result["note"] = (
            "delta_pg_f is raw unvalidated .agf data (manufacturer ED token 4); "
            "the trustworthy partial dispersion is the computed pg_f (pg_f_valid)"
        )
    return result


FIND_GLASSES_SPEC = ToolSpec(
    name="find_glasses",
    handler=find_glasses,
    required_params=(),
    description=(
        "Narrow the glass catalog to candidates in a property window - by Abbe "
        "number (vd_min/max), relative partial dispersion (pg_f_min/max), "
        "refractive index (nd_min/max), or raw delta-Pg,F (delta_pg_f_min/max) - the "
        "glass-selection step for chromatic / apochromat (secondary-color) design. "
        "At least one window bound is required. Gotcha: a glass whose computed "
        "field is out-of-band (null) never matches that window, and the delta-Pg,F "
        "bounds are raw manufacturer data (unvalidated). See find_glass_pair for "
        "matched apochromat pairs and lookup_glass for one glass's full data."
    ),
    kind="glass",
    param_types={
        "catalog": "string",
        "limit": "integer",
        "vd_min": "number", "vd_max": "number",
        "pg_f_min": "number", "pg_f_max": "number",
        "nd_min": "number", "nd_max": "number",
        "delta_pg_f_min": "number", "delta_pg_f_max": "number",
    },
)

TOOL_SPECS = (FIND_GLASSES_SPEC,)
