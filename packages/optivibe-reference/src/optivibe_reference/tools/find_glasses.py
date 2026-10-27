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
  would dump the whole 4782-row catalog — refuse it as a ``ToolParamError``).

THE VALIDITY GATE (the cardinal defense surfaced at the window boundary): a
computed field (``vd``, ``pg_f``, ``nd``) is NULL in the row when it was NOT
in-band-valid. Every window predicate is built as
``<field> IS NOT NULL AND <field> BETWEEN ? AND ?`` so an out-of-band row whose
``vd`` is null can NEVER match a Vd window — only rows with a genuinely-computed
in-band value are returned.

THE ΔPg,F CAVEAT: ``delta_pg_f`` comes from the ``.agf`` (ED token 4) and is
UNVALIDATED vendor data. ``build_glass_db`` stores a value whose magnitude is beyond
``glass_build.DELTA_PG_F_LIMIT`` (0.1) as NULL (the probe saw values down to -81.0),
so such a row cannot match a ``delta_pg_f`` window and sorts last. A window matches on
the stored value, and the result carries
``delta_pg_f_unvalidated: True`` so the caller knows the trustworthy
partial-dispersion quantity is the COMPUTED ``pg_f`` (with its ``pg_f_valid``
gate), not this field.

CATALOG AND KIND CONTROL: ``catalog`` is one catalog or a comma-separated whitelist
(``"SCHOTT,OHARA"``); an item that matches no catalog in the database is a
``ToolParamError``. ``exclude`` (comma-separated kinds, default every non-lens kind;
see ``_glass_kind``) leaves rows of those kinds out; the result carries ``left_out``
(always), ``left_out_kinds`` (only when > 0) and ``unclassified`` (only when > 0).
Rows are classified in Python BEFORE the limit, so the counts cover every match.

COPIES: one glass redistributed by several catalogs (same name, ``_same_physical_glass``) is
listed once, after the kind and opt-in screens and before the limit. The first copy in the sort
order stays and names the catalogs of the removed copies in ``also_in``. A copy is removed only
when it adds nothing: the optical values (nd, vd, pg_f, and the ``pg_f_anomaly`` computed from
them) match within the identity tolerance, and every other listed value other than ``catalog`` plus
the stored transmission data is equal; otherwise both stay. The kept row's ``pg_f_anomaly`` is its
own. Two nulls are equal, a null against a known value differs. ``matched`` counts
the rows after removal, ``duplicates_removed`` the rows removed; ``left_out``, ``screened_out``
and ``unknown_kept`` count before removal. ``dedupe=False`` lists every copy.

An empty result over a WELL-FORMED window is ``ok: True`` with an empty
``candidates`` list — a valid window can legitimately match nothing.
"""
import bisect
import json
import math

from ..errors import ToolParamError
from ..server import ToolSpec
from ._glass_common import _same_physical_glass, check_catalogs_exist, normalize_catalogs
from ._glass_kind import material_kind, parse_exclude

_TOOL = "find_glasses"

# The default candidate count AND the hard ceiling for the window search.
_DEFAULT_LIMIT = 50
_HARD_CAP = 200

# The normal line (g,F partial dispersion against d-line Abbe number) and the offset from it.
_NORMAL_A, _NORMAL_B = 0.6438, -0.001682
_NORMAL_LINE = {"a": _NORMAL_A, "b": _NORMAL_B, "lines": "g,F; d"}
_ANOMALY_SQL = "pg_f - (%r + %r * vd)" % (_NORMAL_A, _NORMAL_B)     # NULL when pg_f or vd is NULL
_FROM = "(SELECT *, %s AS pg_f_anomaly FROM glass)" % _ANOMALY_SQL
_TRUNCATED = "first %d of %d by %s %s; %d not shown; %s"
_TAIL_LIMIT = "set limit=%d to see them all"
_TAIL_CAP = "limit stops at %d: narrow the window to see them all"

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
    "status",
    "relative_cost",
    "climate",
    "stain",
    "acid",
    "tce",
    "pg_f_anomaly",     # stays the last key
)
# A copy is removed only when every one of these (plus the stored transmission data) equals the kept row's.
_COPY_COLUMNS = ("delta_pg_f", "formula", "formula_name", "min_wave", "max_wave", "status",
                 "relative_cost", "climate", "stain", "acid", "tce")
_STATUS_NAMES = ("standard", "preferred", "obsolete", "special", "melt")
# Opt-in caps: param -> result column.
_MAX_FILTERS = (("max_relative_cost", "relative_cost"), ("max_climate", "climate"),
                ("max_stain", "stain"))

# The window params: each maps the public ``<field>_min`` / ``<field>_max`` pair
# onto a real glass column. ``valid_only=True`` adds the ``<col> IS NOT NULL``
# clause (the cardinal-defense gate — a NULL computed field is out-of-band and
# must never match a window). ``delta_pg_f`` is unvalidated so it does NOT get the
# NOT NULL gate keyed to a *_valid flag (its NULLs are absent in the .agf or beyond
# the +-0.1 limit applied at DB build), but a NULL still cannot satisfy a BETWEEN, so
# SQLite filters it naturally; we mark the result as unvalidated instead.
_WINDOW_FIELDS = (
    # (public_prefix, column, valid_only)
    ("vd", "vd", True),
    ("pg_f", "pg_f", True),
    ("nd", "nd", True),
    ("delta_pg_f", "delta_pg_f", False),
    ("pg_f_anomaly", "pg_f_anomaly", True),
)


# The caller may order the matches by any window field.
# The SQL column comes from this fixed tuple, never from the caller's text.
_SORT_FIELDS = ("vd", "nd", "pg_f", "delta_pg_f", "pg_f_anomaly")
_DEFAULT_SORT = "vd"
_ORDERS = ("ascending", "descending")


def _resolve_sort(params):
    """Resolve ``(sort, order)``; default ``("vd", "ascending")``, the order of a call without ``sort``."""
    sort = params.get("sort", _DEFAULT_SORT)
    if not isinstance(sort, str) or sort not in _SORT_FIELDS:
        raise ToolParamError(
            f"'sort' must be one of {', '.join(_SORT_FIELDS)}; got {sort!r}"
        )
    order = params.get("order", "ascending")
    if not isinstance(order, str) or order not in _ORDERS:
        raise ToolParamError(f"'order' must be ascending or descending; got {order!r}")
    return sort, order


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
    try:
        finite = math.isfinite(value)
    except OverflowError:
        raise ToolParamError(f"'{name}' is too large for a number") from None
    if not finite:
        raise ToolParamError(f"'{name}' must be a finite number")
    return float(value)


def _resolve_dedupe(params):
    """Resolve ``dedupe`` (default True); a bool only, so 0 / 1 / "true" are refused."""
    value = params.get("dedupe", True)
    if not isinstance(value, bool):
        raise ToolParamError(f"'dedupe' must be a bool; got {type(value).__name__}")
    return value


def _remove_copies(rows):
    """Drop later copies of one glass; return ``(kept, also_in, removed)``.

    A row is a copy of a kept row of the same name when ``_same_physical_glass`` says so AND its
    every other served column and the stored transmission data equal the kept row's (two nulls are
    equal, a null against a known value is not). ``also_in[i]`` lists the catalogs removed into ``kept[i]``, in the order met.
    """
    col = _RESULT_COLUMNS.index
    key = lambda r: (r[0], r[1], r[col("vd")], r[col("pg_f")], r[col("nd")])   # noqa: E731
    same_data = [col(c) for c in _COPY_COLUMNS] + [-1]    # -1: the fetched "it" transmission data
    same = lambda a, b: all(a[i] == b[i] for i in same_data)   # noqa: E731
    kept, also_in, by_name, removed = [], [], {}, 0
    for r in rows:
        twin = next((i for i in by_name.get(r[1], ())
                     if _same_physical_glass(key(kept[i]), key(r)) and same(kept[i], r)), None)
        if twin is None:
            by_name.setdefault(r[1], []).append(len(kept))
            kept.append(r)
            also_in.append([])
        else:
            also_in[twin].append(r[0])
            removed += 1
    return kept, also_in, removed


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


def _transmission(it_json, wavelength):
    """10 mm internal transmission at ``wavelength`` (um): rows rescaled by t ** (10 / d), linear
    between neighbours, None outside the rows' range or with no rows."""
    if not it_json:
        return None
    pts = sorted((w, t ** (10.0 / d)) for w, t, d in json.loads(it_json))
    if not pts or not pts[0][0] <= wavelength <= pts[-1][0]:
        return None
    i = bisect.bisect_left([w for w, _ in pts], wavelength)
    w1, t1 = pts[i]
    if w1 == wavelength or i == 0:
        return round(t1, 4)
    w0, t0 = pts[i - 1]
    return round(t0 + (t1 - t0) * (wavelength - w0) / (w1 - w0), 4)


def _resolve_buildability(params):
    """Validate the opt-in params; return ``(rules, wavelength)``, rules = ``(column, test)`` pairs
    (a glass passes when the value is unknown or the test holds)."""
    rules = []
    if "status" in params:
        raw = params["status"]
        names = {x.strip().lower() for x in raw.split(",")} if isinstance(raw, str) else set()
        if not names or not names <= set(_STATUS_NAMES):
            raise ToolParamError(
                f"'status' must be a comma-separated list of {', '.join(_STATUS_NAMES)}; got {raw!r}"
            )
        rules.append(("status", names.__contains__))
    rules += [(col, lambda v, b=_validate_bound(key, params[key]): v <= b)
              for key, col in _MAX_FILTERS if key in params]
    wavelength = None
    if "transmission_wavelength" in params:
        wavelength = _validate_bound("transmission_wavelength", params["transmission_wavelength"])
        if wavelength <= 0:
            raise ToolParamError("'transmission_wavelength' must be > 0 (micrometres)")
    if "min_transmission" in params:
        low = _validate_bound("min_transmission", params["min_transmission"])
        if not 0.0 <= low <= 1.0:
            raise ToolParamError(f"'min_transmission' must be in [0, 1]; got {low}")
        if wavelength is None:
            raise ToolParamError("'min_transmission' needs 'transmission_wavelength'")
        rules.append(("transmission", lambda v: v >= low))
    return rules, wavelength


def find_glasses(db_conn, params):
    """Window-search the glass catalog by Vd / Pg,F / Nd / ΔPg,F (never raises).

    See the module docstring for the full contract.
    """
    catalogs = normalize_catalogs(params.get("catalog"))
    excluded = parse_exclude(params.get("exclude"))
    limit = _resolve_limit(params)
    dedupe = _resolve_dedupe(params)
    sort, order = _resolve_sort(params)
    clauses, sql_params, queried_fields = _collect_windows(params)
    rules, t_wave = _resolve_buildability(params)

    if not queried_fields:
        raise ToolParamError(
            "give at least one of vd_/pg_f_/nd_/delta_pg_f_/pg_f_anomaly_ window bounds"
        )

    check_catalogs_exist(db_conn, catalogs)

    where_parts = list(clauses)
    if catalogs is not None:
        # catalogs are normalized to the stored ``<BASENAME>.AGF`` form; a
        # case-insensitive IN matches the stored value.
        marks = ", ".join("?" for _ in catalogs)
        where_parts.append(f"LOWER(catalog) IN ({marks})")
        sql_params.extend(c.lower() for c in catalogs)

    where_sql = " AND ".join(where_parts)
    # Fetch every match (the kind of a row is decided in Python, so the count of rows
    # left out covers the whole match set), ordered by the sort field (vd by default, a
    # stable key for crown/flint scanning; a null field last in either direction) with
    # catalog and name as tiebreakers for determinism.
    cols = ", ".join(_RESULT_COLUMNS + ("it",))   # "it": fetched, never returned
    direction = " DESC" if order == "descending" else ""
    sql = (
        f"SELECT {cols} FROM {_FROM} WHERE {where_sql} "
        f"ORDER BY {sort} IS NULL, {sort}{direction}, catalog, name"
    )
    all_rows = db_conn.execute(sql, sql_params).fetchall()

    rows = []
    left_out_kinds = {}
    unclassified = 0
    screened_out = unknown_kept = 0
    for r in all_rows:
        kind = material_kind(r[0], r[1])
        if kind in excluded:
            left_out_kinds[kind] = left_out_kinds.get(kind, 0) + 1
            continue
        if kind == "unclassified":
            unclassified += 1
        if rules:
            vals = [(_transmission(r[-1], t_wave) if c == "transmission"
                     else r[_RESULT_COLUMNS.index(c)], ok) for c, ok in rules]
            if any(v is not None and not ok(v) for v, ok in vals):
                screened_out += 1
                continue
            unknown_kept += any(v is None for v, _ in vals)
        rows.append(r)

    # Copy removal runs after the kind and opt-in screens and BEFORE the limit.
    if dedupe:
        rows, also_in, duplicates_removed = _remove_copies(rows)
    else:
        also_in, duplicates_removed = [[] for _ in rows], 0
    matched = len(rows)
    truncated = matched > limit
    rows, also_in = rows[:limit], also_in[:limit]
    candidates = [_row_to_dict(r) for r in rows]
    for cand, r, others in zip(candidates, rows, also_in):
        anomaly = cand.pop("pg_f_anomaly")
        cand["also_in"] = others
        cand["transmission"] = None if t_wave is None else _transmission(r[-1], t_wave)
        cand["pg_f_anomaly"] = anomaly

    result = {
        "ok": True,
        "candidates": candidates,
        "candidate_count": len(candidates),
        "matched": matched,
        "duplicates_removed": duplicates_removed,
        "dedupe": dedupe,
        "truncated": truncated,
        "sort": sort,
        "order": order,
        "match_kind": "range",
        "queried_fields": queried_fields,
        "left_out": sum(left_out_kinds.values()),
        "normal_line": dict(_NORMAL_LINE),
    }
    if truncated:
        tail = _TAIL_LIMIT % matched if matched <= _HARD_CAP else _TAIL_CAP % _HARD_CAP
        result["truncated_note"] = _TRUNCATED % (len(candidates), matched, sort, order,
                                                 matched - len(candidates), tail)
    if left_out_kinds:
        result["left_out_kinds"] = left_out_kinds
    if unclassified:
        result["unclassified"] = unclassified
    if rules:
        result["screened_out"] = screened_out
    if unknown_kept:
        result["unknown_kept"] = unknown_kept
    if "delta_pg_f" in queried_fields:
        # Flag the caveat: delta_pg_f is raw, unvalidated .agf data.
        result["delta_pg_f_unvalidated"] = True
        result["note"] = (
            "delta_pg_f is unvalidated .agf data (manufacturer ED token 4); a stored "
            "value beyond +-0.1 reads as null; the trustworthy partial dispersion is "
            "the computed pg_f (pg_f_valid)"
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
        "At least one window bound is required. "
        "catalog takes one catalog or a comma-separated whitelist such as "
        "SCHOTT,OHARA; a name that matches no catalog is refused. By default the "
        "known non-lens kinds are left out: exclude lists the kinds to leave out "
        "(liquid, adhesive, plastic, gas, birefringent, other), the result counts "
        "them in left_out, and exclude=\"\" searches every row. "
        "Matches are ordered by sort (vd, nd, pg_f or delta_pg_f; default vd, "
        "order=descending reverses; sort=pg_f_anomaly ranks by pg_f minus the normal line "
        "0.6438 - 0.001682*vd, null when either is null, window pg_f_anomaly_min/max; a "
        "truncated list carries truncated_note saying what is hidden and how to see it) and cut at limit; matched counts the glasses in the "
        "window that are not left out, after copies are removed (left_out, screened_out and unknown_kept count before removal), so truncated means only the first limit of matched are listed. Gotcha: a glass whose computed "
        "field is out-of-band (null) never matches that window, and the delta-Pg,F "
        "bounds are raw manufacturer data (unvalidated); a stored delta-Pg,F beyond "
        "+-0.1 reads as null. Every candidate also carries status (standard, preferred, "
        "obsolete, special, melt), relative_cost, climate, stain, acid, tce and transmission "
        "(internal, 10 mm path, at transmission_wavelength in micrometres, else null). "
        "Opt-in filters, none applied by default: status (comma list), max_relative_cost, "
        "max_climate, max_stain, min_transmission (needs transmission_wavelength); a glass "
        "whose value is unknown is kept and counted in unknown_kept, screened_out counts "
        "the rest, and the thresholds are the caller's. "
        "One glass that several catalogs carry (same name, same optics) is listed once and "
        "the kept row names the other catalogs in also_in; a copy is removed only when "
        "its optical values (nd, vd, pg_f and the pg_f_anomaly computed from them) match "
        "within a small tolerance and every other listed value and the transmission data are "
        "equal, otherwise both stay, and dedupe=false lists every copy. See find_glass_pair for "
        "matched apochromat pairs. See lookup_glass for one glass's full data."
    ),
    kind="glass",
    param_types={
        "catalog": "string",
        "exclude": "string",
        "limit": "integer",
        "dedupe": "boolean",
        "sort": "string",
        "order": "string",
        "vd_min": "number", "vd_max": "number",
        "pg_f_min": "number", "pg_f_max": "number",
        "nd_min": "number", "nd_max": "number",
        "delta_pg_f_min": "number", "delta_pg_f_max": "number",
        "pg_f_anomaly_min": "number", "pg_f_anomaly_max": "number",
        "status": "string", "max_relative_cost": "number", "max_climate": "number",
        "max_stain": "number", "min_transmission": "number",
        "transmission_wavelength": "number",
    },
)

TOOL_SPECS = (FIND_GLASSES_SPEC,)
