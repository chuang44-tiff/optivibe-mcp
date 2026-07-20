"""tools/find_glass_pair.py — apochromat pair-finder (session-free, never-raise).

The 5th MCP glass tool: finds glass PAIRS for secondary-spectrum correction. An
apochromat doublet wants two glasses with MATCHED relative partial dispersion
(small ``|Pg,F_A - Pg,F_B|``, so the secondary spectrum cancels) but SEPARATED
Abbe number (large ``|Vd_A - Vd_B|``, so the achromatic power split is realisable
without extreme curvatures). This tool ranks pairs by exactly that trade-off.

Handler signature ``find_glass_pair(db_conn, params)`` — arg-0 is the GLASS
catalog connection the ref ``Dispatcher`` threads positionally by
``spec.kind=='glass'`` (the same routing ``lookup_glass`` / ``find_glasses`` ride).

PARAMS (all optional):
- ``max_delta_pg_f`` (default 0.005): the Pg,F-match tolerance (sliding-window
  width). Only partners within this Pg,F distance are considered.
- ``min_delta_vd`` (default 20.0): the minimum Abbe separation — a pair closer
  than this gives too little power split to be useful.
- ``anchor`` (+ optional ``anchor_catalog``): partners FOR a specific glass.
  Resolved like ``lookup_glass`` (unknown -> ``glass_unknown``; a colliding bare
  name across catalogs -> ``ambiguous_glass`` candidate list — NEVER a silent
  vendor pick). Absent -> the global top-ranked pairs.
- ``catalog``: restrict BOTH glasses to this catalog (case-insensitive).
- ``limit`` (default 20, hard cap 100).

SCORE: ``|ΔVd| / (1.0 + 200.0 * |ΔPg,F|)`` — rewards a big Abbe split and a tiny
Pg,F mismatch. Top ``limit`` by score.

EFFICIENCY: the pair-eligible rows (``vd IS NOT NULL AND pg_f IS NOT NULL AND
nd_valid AND pg_f_valid``) are SORTED by ``pg_f``; a SLIDING WINDOW pointer
advances so each glass only pairs with partners whose ``pg_f`` is within
``max_delta_pg_f``. This is O(N·W) for the narrow tolerances of normal apochromat
search; O(N²) worst case when ``max_delta_pg_f`` spans the whole catalog. The
probe measured the full N² scan at ~3s; the narrow-window search runs well
under 1s.

PHYSICAL-GLASS IDENTITY (the de-dup / self-pair core): the SAME glass is often
redistributed across catalogs (e.g. ``ACRYLIC`` in ``MISC`` / ``MISC1`` /
``MISC-CRB`` — byte-identical properties). Two rows are the SAME physical glass
when they share a ``name`` AND all three optical properties match within
tolerance (``|Δnd| <= 1e-3``, ``|Δvd| <= 0.5``, ``|Δpg_f| <= 1e-3``). A row that
shares a name but differs in ANY property beyond tolerance is a DIFFERENT glass
(a legitimately-distinct cross-catalog same-name pair, which IS returned).

DEGENERACY GUARDS:
- a glass never pairs with itself: excluded when ``i`` and ``j`` are the same
  ``(catalog, name)`` OR the same physical glass (same identity key);
- the ordered duplicate ``(B, A)`` of an emitted ``(A, B)`` is excluded (forward
  pairing i<j);
- two result-pairs collapse to the best-scored one ONLY when they are the same
  unordered pair of PHYSICAL glasses (both endpoints share identity) — a pair
  differing in either physical endpoint stays distinct.

An anchored-but-no-partner or a global-but-none search is ``ok: True`` with an
empty ``pairs`` list (a legitimately empty result, not an error).
"""
import math

from .._envelope import error_envelope
from ..errors import ToolParamError
from ..server import ToolSpec
from ._glass_common import normalize_catalog

_TOOL = "find_glass_pair"

_DEFAULT_MAX_DELTA_PG_F = 0.005
_DEFAULT_MIN_DELTA_VD = 20.0
_DEFAULT_LIMIT = 20
_HARD_CAP = 100

# Physical-glass identity tolerances: two rows sharing a NAME are the SAME
# physical glass (a cross-catalog redistributed copy) only when ALL THREE optical
# properties also match within these tolerances. Any property beyond tolerance =>
# a DIFFERENT glass that happens to share the name (which must still pair / stay
# distinct). These are the audit-specified bounds.
_IDENT_ND_TOL = 1e-3
_IDENT_VD_TOL = 0.5
_IDENT_PG_F_TOL = 1e-3

# The score's Pg,F-mismatch penalty weight (1 + 200*|dPgF|): a 0.005 Pg,F gap
# halves the score vs. a perfect match, so a tiny-mismatch pair outranks a
# wider-mismatch one at equal Abbe split. One source of truth.
_PGF_PENALTY = 200.0


def _validate_positive_number(name, value, default):
    """Validate an optional finite, ``> 0`` numeric param; return it or the default.

    bool-before-float FIRST (``True == 1.0`` slips a bool through), then non-numeric,
    then NaN/inf, then ``<= 0`` (a non-positive tolerance/separation is nonsense).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ToolParamError(f"'{name}' must be a number, not a bool")
    if not isinstance(value, (int, float)):
        raise ToolParamError(
            f"'{name}' must be a number; got {type(value).__name__}"
        )
    if not math.isfinite(value):
        raise ToolParamError(f"'{name}' must be a finite number")
    if value <= 0:
        raise ToolParamError(f"'{name}' must be > 0; got {value}")
    return float(value)


def _resolve_limit(params):
    """Resolve the effective pair cap (default 20, hard cap 100)."""
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


def _same_physical_glass(row_a, row_b):
    """True when two eligible rows are the SAME physical glass (name + props in tol).

    Rows are ``(catalog, name, vd, pg_f, nd)``. Same physical glass = same NAME AND
    ``|Δnd| <= 1e-3`` AND ``|Δvd| <= 0.5`` AND ``|Δpg_f| <= 1e-3`` — i.e. a
    cross-catalog redistributed copy. A None property (shouldn't occur for
    pair-eligible rows, but be defensive) is treated as a non-match on that axis.
    """
    if row_a[1] != row_b[1]:
        return False
    vd_a, pgf_a, nd_a = row_a[2], row_a[3], row_a[4]
    vd_b, pgf_b, nd_b = row_b[2], row_b[3], row_b[4]
    if None in (vd_a, pgf_a, nd_a, vd_b, pgf_b, nd_b):
        return False
    return (
        abs(nd_a - nd_b) <= _IDENT_ND_TOL
        and abs(vd_a - vd_b) <= _IDENT_VD_TOL
        and abs(pgf_a - pgf_b) <= _IDENT_PG_F_TOL
    )


def _assign_identity_clusters(eligible):
    """Group pair-eligible rows into physical-glass clusters; return an id per row.

    Returns a list ``cluster_id`` parallel to ``eligible``: two rows share an id iff
    they are the SAME physical glass (``_same_physical_glass``). We union only rows
    that share a NAME (so the comparison stays cheap — group-by name, then a direct
    pairwise tolerance compare WITHIN each name bucket via union-find). This is an
    exact tolerance test (no rounding-boundary hazard) and the cluster id is the
    physical-identity key both the self-pair guard and the dedup ride.
    """
    n = len(eligible)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    # Bucket row indices by name; only same-name rows can be the same glass.
    by_name = {}
    for idx, row in enumerate(eligible):
        by_name.setdefault(row[1], []).append(idx)
    for idxs in by_name.values():
        if len(idxs) < 2:
            continue
        # Pairwise within the name bucket (buckets are tiny — a handful of catalogs).
        for ii in range(len(idxs)):
            for jj in range(ii + 1, len(idxs)):
                a, b = idxs[ii], idxs[jj]
                if _same_physical_glass(eligible[a], eligible[b]):
                    union(a, b)
    return [find(i) for i in range(n)]


def _load_pair_eligible(db_conn, catalog):
    """Load the pair-eligible rows (sorted by pg_f), optionally catalog-restricted.

    Pair-eligible = a genuinely-computed in-band Vd AND Pg,F (the cardinal-defense
    gate, surfaced here so an out-of-band row never enters a pairing). Returns a
    list of ``(catalog, name, vd, pg_f, nd)`` tuples sorted ascending by pg_f.
    """
    where = (
        "vd IS NOT NULL AND pg_f IS NOT NULL "
        "AND nd_valid = 1 AND pg_f_valid = 1"
    )
    sql_params = []
    if catalog is not None:
        where += " AND LOWER(catalog) = LOWER(?)"
        sql_params.append(catalog)
    sql = (
        "SELECT catalog, name, vd, pg_f, nd FROM glass "
        f"WHERE {where} ORDER BY pg_f, catalog, name"
    )
    return db_conn.execute(sql, sql_params).fetchall()


def _resolve_anchor(db_conn, anchor, anchor_catalog, catalog):
    """Resolve the anchor glass like ``lookup_glass`` (returns row tuple or envelope).

    Returns ``(row, None)`` on a clean resolve, or ``(None, envelope)`` when the
    anchor is unknown / ambiguous (the caller returns the envelope directly).
    The returned row is ``(catalog, name, vd, pg_f, nd, pg_f_valid, vd_present)``;
    a resolved-but-not-pair-eligible anchor is handled by the caller (it yields an
    empty pairs list, since it can pair with nothing).
    """
    if not isinstance(anchor, str):
        raise ToolParamError(
            f"'anchor' must be a string; got {type(anchor).__name__}"
        )
    if not anchor.strip():
        return None, error_envelope(
            _TOOL, "glass_unknown", "empty anchor name", anchor=anchor
        )

    # An anchor_catalog (or the BOTH-restricting catalog) disambiguates the anchor.
    effective_cat = anchor_catalog if anchor_catalog is not None else catalog
    sql_params = [anchor]
    where = "name = ?"
    if effective_cat is not None:
        where += " AND LOWER(catalog) = LOWER(?)"
        sql_params.append(effective_cat)
    rows = db_conn.execute(
        "SELECT catalog, name, vd, pg_f, nd, pg_f_valid, "
        "(vd IS NOT NULL) FROM glass "
        f"WHERE {where} ORDER BY catalog",
        sql_params,
    ).fetchall()
    # Byte-exact name (defensive — no COLLATE NOCASE on the column, but assert it).
    rows = [r for r in rows if r[1] == anchor]

    if not rows:
        return None, error_envelope(
            _TOOL, "glass_unknown", "no glass matched the anchor name",
            anchor=anchor, anchor_catalog=anchor_catalog,
        )
    if effective_cat is None and len(rows) > 1:
        candidates = [{"catalog": r[0], "name": r[1]} for r in rows]
        return None, error_envelope(
            _TOOL, "ambiguous_glass",
            "anchor name resolves in multiple catalogs; pass anchor_catalog",
            anchor=anchor, candidates=candidates, candidate_count=len(candidates),
        )
    return rows[0], None


def _score(delta_vd, delta_pg_f):
    """The apochromat pair score: big Abbe split / tiny Pg,F mismatch."""
    return delta_vd / (1.0 + _PGF_PENALTY * delta_pg_f)


def _pair_dict(a, b, delta_vd, delta_pg_f, score):
    """Build one result pair dict with the lower-Vd glass as ``a`` for stability."""
    # Canonical orientation: the higher-Vd (crown-ish) glass first is a stable,
    # readable convention. a/b are (catalog, name, vd, pg_f, nd) tuples.
    if a[2] < b[2]:
        a, b = b, a
    return {
        "a": {"catalog": a[0], "name": a[1], "vd": a[2], "pg_f": a[3], "nd": a[4]},
        "b": {"catalog": b[0], "name": b[1], "vd": b[2], "pg_f": b[3], "nd": b[4]},
        "delta_vd": delta_vd,
        "delta_pg_f": delta_pg_f,
        "score": score,
    }


def _emit_pairs(eligible, cluster_ids, max_delta_pg_f, min_delta_vd, anchor_row):
    """Run the sliding-window pairing and return the scored pair dicts (pre-dedup).

    ``eligible`` is sorted ascending by pg_f; ``cluster_ids`` is parallel to it and
    carries the physical-glass identity (same id => same physical glass). When
    ``anchor_row`` is given, only pairs that INCLUDE the anchor are kept (the
    anchor must itself be present in ``eligible``). Each emitted pair carries its
    endpoints' cluster ids so the dedup can group on PHYSICAL identity.

    SELF-PAIR exclusion: a pair (i, j) is dropped only when i and j are the SAME
    physical glass (same cluster id) OR the same (catalog, name). A same-name but
    different-properties cross-catalog pair (different cluster ids) is RETURNED.
    """
    pairs = []
    n = len(eligible)
    # The sliding window: ``hi`` advances so [i+1, hi) are the partners whose pg_f
    # is within max_delta_pg_f of glass i. Each glass pairs only forward (i < j),
    # so an emitted (A, B) never re-appears as (B, A).
    hi = 0
    for i in range(n):
        cat_i, name_i, vd_i, pgf_i, nd_i = eligible[i]
        cid_i = cluster_ids[i]
        if hi < i + 1:
            hi = i + 1
        while hi < n and (eligible[hi][3] - pgf_i) <= max_delta_pg_f:
            hi += 1
        for j in range(i + 1, hi):
            cat_j, name_j, vd_j, pgf_j, nd_j = eligible[j]
            cid_j = cluster_ids[j]
            # Self-pair exclusion: same physical glass (same cluster) OR the exact
            # same (catalog, name) row. A same-name-but-DIFFERENT-properties pair
            # (distinct clusters) is a legitimately-distinct pair and IS kept.
            if cid_i == cid_j:
                continue
            if cat_i == cat_j and name_i == name_j:
                continue
            delta_vd = abs(vd_i - vd_j)
            if delta_vd < min_delta_vd:
                continue
            delta_pg_f = abs(pgf_i - pgf_j)
            # Anchor mode: keep only pairs that include the anchor glass.
            if anchor_row is not None:
                a_cat, a_name = anchor_row[0], anchor_row[1]
                involves = (
                    (cat_i == a_cat and name_i == a_name)
                    or (cat_j == a_cat and name_j == a_name)
                )
                if not involves:
                    continue
            score = _score(delta_vd, delta_pg_f)
            pair = _pair_dict(
                eligible[i], eligible[j], delta_vd, delta_pg_f, score
            )
            # Stash the unordered physical-identity key for the dedup step.
            pair["_cluster_key"] = frozenset((cid_i, cid_j))
            pairs.append(pair)
    return pairs


def _dedupe_by_physical_pair(pairs):
    """Collapse result-pairs that are the SAME unordered pair of PHYSICAL glasses.

    The SAME physical glass is often redistributed across catalogs (e.g.
    ``ACRYLIC`` in MISC / MISC1 with identical properties), so ``(A@MISC, B)`` and
    ``(A@MISC1, B)`` — same physical A, same physical B — are the SAME physical
    pairing and must not both be returned. We key on the UNORDERED pair of PHYSICAL
    identity cluster ids and keep the highest-scoring representative.

    Crucially this collapses ONLY true duplicates: two pairs whose endpoints differ
    in EITHER physical glass (a same-name-but-different-properties endpoint lands in
    a different cluster) carry different keys and stay DISTINCT. The frozenset key
    also subsumes the (B, A) reversal.
    """
    best = {}
    for p in pairs:
        key = p["_cluster_key"]
        prev = best.get(key)
        if prev is None or p["score"] > prev["score"]:
            best[key] = p
    out = []
    for p in best.values():
        p.pop("_cluster_key", None)
        out.append(p)
    return out


def find_glass_pair(db_conn, params):
    """Find apochromat glass pairs (matched Pg,F, separated Vd); never raises.

    See the module docstring for the full contract + the sliding-window algorithm.
    """
    max_delta_pg_f = _validate_positive_number(
        "max_delta_pg_f", params.get("max_delta_pg_f"), _DEFAULT_MAX_DELTA_PG_F
    )
    min_delta_vd = _validate_positive_number(
        "min_delta_vd", params.get("min_delta_vd"), _DEFAULT_MIN_DELTA_VD
    )
    limit = _resolve_limit(params)
    catalog = normalize_catalog(params.get("catalog"))
    anchor_catalog = normalize_catalog(
        params.get("anchor_catalog"), "anchor_catalog"
    )

    anchor_row = None
    anchor = params.get("anchor")
    if anchor is not None:
        resolved, envelope = _resolve_anchor(
            db_conn, anchor, anchor_catalog, catalog
        )
        if envelope is not None:
            return envelope
        # resolved is (catalog, name, vd, pg_f, nd, pg_f_valid, vd_present). A
        # resolved-but-not-pair-eligible anchor (null vd/pg_f or invalid flags)
        # can pair with nothing -> empty pairs (ok:True), NOT an error.
        a_vd, a_pgf, a_pgf_valid, a_vd_present = (
            resolved[2], resolved[3], resolved[5], resolved[6]
        )
        if a_vd is None or a_pgf is None or not a_pgf_valid or not a_vd_present:
            return {
                "ok": True,
                "pairs": [],
                "pair_count": 0,
                "truncated": False,
                "criterion": "matched_pgf_max_dvd",
                "match_kind": "pair",
                "anchor": {"catalog": resolved[0], "name": resolved[1]},
                "note": "anchor has no valid Vd/Pg,F (out-of-band) — no pairs",
            }
        anchor_row = (resolved[0], resolved[1], resolved[2], resolved[3], resolved[4])

    eligible = _load_pair_eligible(db_conn, catalog)
    cluster_ids = _assign_identity_clusters(eligible)

    pairs = _emit_pairs(
        eligible, cluster_ids, max_delta_pg_f, min_delta_vd, anchor_row
    )
    pairs = _dedupe_by_physical_pair(pairs)
    # Rank by score descending; tiebreak on names for determinism.
    pairs.sort(
        key=lambda p: (
            -p["score"], p["a"]["catalog"], p["a"]["name"],
            p["b"]["catalog"], p["b"]["name"],
        )
    )

    truncated = len(pairs) > limit
    pairs = pairs[:limit]

    result = {
        "ok": True,
        "pairs": pairs,
        "pair_count": len(pairs),
        "truncated": truncated,
        "criterion": "matched_pgf_max_dvd",
        "match_kind": "pair",
        "max_delta_pg_f": max_delta_pg_f,
        "min_delta_vd": min_delta_vd,
    }
    if anchor_row is not None:
        result["anchor"] = {"catalog": anchor_row[0], "name": anchor_row[1]}
    return result


FIND_GLASS_PAIR_SPEC = ToolSpec(
    name="find_glass_pair",
    handler=find_glass_pair,
    required_params=(),
    description=(
        "Find apochromat / secondary-color glass PAIRS - two glasses with matched "
        "relative partial dispersion (small |delta-Pg,F|) and well-separated Abbe "
        "number (large |delta-Vd|), the doublet condition for cancelling secondary "
        "spectrum. Optionally anchor on one named glass to get its best partners; "
        "tune with max_delta_pg_f (the partial-dispersion mismatch limit) and "
        "min_delta_vd (the minimum Abbe separation). Results rank "
        "best-first (most Abbe separation at the smallest partial-dispersion "
        "mismatch). Gotcha: an ambiguous bare anchor name returns an "
        "ambiguous_glass candidate list - qualify the catalog. See find_glasses to "
        "window-search single glasses and lookup_glass for one glass's full data."
    ),
    kind="glass",
    param_types={
        "max_delta_pg_f": "number",
        "min_delta_vd": "number",
        "limit": "integer",
        "catalog": "string",
        "anchor": "string",
        "anchor_catalog": "string",
    },
)

TOOL_SPECS = (FIND_GLASS_PAIR_SPEC,)
