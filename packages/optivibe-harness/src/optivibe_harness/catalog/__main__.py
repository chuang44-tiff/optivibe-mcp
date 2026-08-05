"""catalog/__main__.py — the thin batch CLI for benching a folder of lens designs.

The ONE ``src/`` addition the skill ships (no MCP tool, no
``server.py`` edit, no tool-count change). Two subcommands:

- ``bench`` — the SOLE seat-holder for the whole engine phase. Inside ONE
  ``try/finally`` reaping the ZOS session: (1) BUILD every
  ``staging/*.spec.json`` into ``staging/<stem>.zmx`` (``apply_lens_spec`` ->
  ``save_snapshot`` -> ``shutil.copy`` the returned path, BEFORE any ``.zmx`` is
  loaded — the BUILD-BEFORE-LOAD invariant); (2) BENCH via the shipped
  ``bench.bench_folder`` verbatim; (3) render one geometry-only layout PNG per
  design; (4) PLOT via ``plot.plot_metrics``; (5) run the pure ``rank`` scorer to
  write ``ranking_scores.json``. Prints a one-line JSON summary to stdout.

- ``rank`` — the DETERMINISTIC scorer. Pure Python, NO engine: reads the CSV
  numbers (+ the sibling ``manifest.json`` status tokens + an optional
  ``build_ledger.json``) and writes ``ranking_scores.json`` (the reproducible
  ranked order; null-never-0 is a REAL code contract here).

The CLI is a PLAIN process (NOT the MCP) — it does NOT run ``_isolate_stdio()``;
it prints freely (a single machine-readable JSON line on stdout, human notes on
stderr).
"""
import argparse
import glob
import json
import math
import os
import shutil
import sys

from . import bench as B
from . import registry
from .metrics import reading_ok
from .plot import classify_cell, plot_metrics


# --------------------------------------------------------------------------- #
# Defaults (mirror bench._DEFAULT_METRICS without importing a private name into
# the axis-default path — the ranker's own contract).
# --------------------------------------------------------------------------- #
_DEFAULT_METRICS = ("first_order", "rms_spot", "mtf", "strehl", "clearance")
_SPEC_SUFFIX = ".spec.json"
_UNVERIFIED_WEIGHT = 0.5   # an unverified axis is discounted, not dropped.
# FIX-A (the ABSOLUTE never-fake-0 invariant): a positive weight is clamped into a SANE
# FINITE range so no weight magnitude can push ``num``/``den`` to ``inf`` (overflow, a huge
# weight) or underflow ``num`` to a fabricated ``0.0`` (a subnormal weight). ``0`` stays ``0``
# (a deliberately zero-weighted, non-contributing axis); the range floors a POSITIVE weight.
_MAX_WEIGHT = 1e6
_MIN_WEIGHT = 1e-6

RC_NOT_BENCHED = 3   # the "not benched" rc (engine_busy soft-decline preflight
#                     AND the all-designs-failed-to-load discriminator). A caller keys on the
#                     error_family for WHICH (engine_busy vs all_designs_failed_load).

# The LOAD-stage failure family SET, found during hardening review: a contended second
# engine SILENTLY no-ops ``LoadFile`` -> ``load_design``'s read-back proof fails -> the tool
# emits ``load_failed`` (NOT ``load_not_found``, which is a PRE-engine missing/bad/relative-path
# precheck with NO LoadFile call). The post-bench discriminator fires on EITHER family, so a
# CLEAN-path foreign contention (all ``load_failed``) is caught, not only a relative/bad-path
# artifact (all ``load_not_found``). ``load_param`` (non-.zmx / empty path — a caller error) does
# NOT occur per-design in bench (every design has a path) and is deliberately excluded.
_LOAD_FAILURE_FAMILIES = frozenset({"load_not_found", "load_failed"})


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _finite_pos(v):
    return _num(v) and math.isfinite(v) and v > 0.0


# =========================================================================== #
# Shared: the CSV + JSON readers (never raise on a malformed input).
# =========================================================================== #
def _read_csv_rows(csv_path):
    """Parse the catalog CSV into ``(header, rows)`` (rows = list of dict keyed by
    header, a missing trailing cell -> ``""``). Never raises."""
    import csv as _csv
    try:
        with open(csv_path, "r", newline="", encoding="utf-8-sig") as fh:
            data = list(_csv.reader(fh))
    except (OSError, ValueError, _csv.Error):
        # FIX-B: the C ``csv`` reader raises ``csv.Error`` (NOT a ValueError subclass) on a
        # malformed/oversized-field/binary CSV — the ``rank`` subcommand consumes an
        # ARBITRARY external CSV, so this is reachable. Catch it too so the "Never raises"
        # contract holds and ``_cmd_rank`` reports the ``ok:false`` envelope (FIX-6), never
        # a traceback.
        return [], []
    if not data:
        return [], []
    header = [str(c) for c in data[0]]
    rows = []
    for cells in data[1:]:
        rows.append({col: (cells[i] if i < len(cells) else "")
                     for i, col in enumerate(header)})
    return header, rows


def _read_json(path):
    """Read a JSON file -> object, or None on any fault. Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# =========================================================================== #
# 8.1 — the DETERMINISTIC scorer (pure; the null-never-0 code contract).
# =========================================================================== #
def _column_direction_map():
    """``{csv_column_name -> higher_is_better}`` over every registry ColumnSpec.

    ``higher_is_better`` is ``True`` | ``False`` | ``None`` (not a KPI direction).
    A column absent from this map defaults to ``None`` (no flip — spec's literal
    ``flip = (higher_is_better is False)`` rule)."""
    m = {}
    for adapter in registry.REGISTRY.values():
        for spec in adapter.columns:
            m[spec.name] = spec.higher_is_better
    return m


def _default_axes(template):
    """The default axis set = the KPI columns (``higher_is_better is not None``) of
    the selected metrics, in registry order. Used when ``ranking.priorities`` is
    absent."""
    metrics = template.get("metrics") if isinstance(template, dict) else None
    if not isinstance(metrics, list) or not metrics:
        metrics = list(_DEFAULT_METRICS)
    axes = []
    seen = set()
    for key in metrics:
        canon = registry.resolve_key(key)
        if canon is None:
            continue
        for spec in registry.REGISTRY[canon].columns:
            if spec.higher_is_better is not None and spec.name not in seen:
                axes.append(spec.name)
                seen.add(spec.name)
    return axes


def _unknown_metrics(template):
    """The requested ``template['metrics']`` keys that resolve to NO registry adapter
    (an unknown/typo'd/unsupported metric). The CLI DISCLOSES these (a stderr warning +
    an ``unknown_metrics`` summary field) rather than silently dropping them (FIX-4 —
    the served-boundary honesty gap the SKILL clarify-rule enforces primarily). Total;
    never raises."""
    metrics = template.get("metrics") if isinstance(template, dict) else None
    if not isinstance(metrics, list):
        return []
    out = []
    for key in metrics:
        if registry.resolve_key(key) is None and str(key) not in out:
            out.append(str(key))
    return out


def _resolve_axes(template):
    """Axes = ``ranking.priorities`` (a list of CSV column names) if present, else
    the default KPI columns of the selected metrics."""
    ranking = template.get("ranking") if isinstance(template, dict) else None
    ranking = ranking if isinstance(ranking, dict) else {}
    pri = ranking.get("priorities")
    if isinstance(pri, list) and pri:
        return [str(c) for c in pri]
    return _default_axes(template)


def _axis_weight(weights, axis):
    """The base weight for an axis (``ranking.weights[axis]``, default 1.0). A
    non-numeric / non-finite (nan/inf) / negative weight defaults to 1.0.

    FIX-A (the ABSOLUTE never-fake-0 invariant, pt 1): a valid POSITIVE weight is CLAMPED
    into ``[_MIN_WEIGHT, _MAX_WEIGHT]`` so no weight magnitude can push ``den``/``num`` to
    ``inf`` (a huge weight overflowing the sum) or underflow ``num`` to a fabricated ``0.0``
    (a subnormal weight — e.g. ``5e-324`` — where ``w * score`` rounds to 0 while ``den`` is
    positive). ``0`` is preserved verbatim (a deliberately zero-weighted axis contributes
    nothing and, if it is the sole axis, keeps the design at ``den == 0`` -> None/unrankable
    per pt 3, NOT a fabricated 0.0)."""
    if isinstance(weights, dict):
        w = weights.get(axis)
        if _num(w):
            # FIX-2 (R5, the never-raise ABSOLUTE close): a 300+-digit INTEGER weight from the
            # template JSON is a real ``int`` (passes ``_num``) but ``math.isfinite(w)`` /
            # ``float(w)`` on it raises ``OverflowError`` (it exceeds max float) -> an uncaught
            # traceback in the ``rank`` subcommand, breaching "never raises over every input".
            # Coerce through a GUARDED ``float`` FIRST so ANY weight magnitude (huge int, huge
            # float, subnormal, nan/inf) is safe: an un-coercible weight falls to the 1.0
            # default; a coercible one keeps the EXACT prior isfinite/non-negative/clamp
            # behavior (byte-identical for every normal weight).
            try:
                w = float(w)
            except (OverflowError, ValueError, TypeError):
                return 1.0
            if math.isfinite(w) and w >= 0.0:
                if w == 0.0:
                    return 0.0
                return min(max(w, _MIN_WEIGHT), _MAX_WEIGHT)
    return 1.0


def _row_name(row, i):
    name = row.get("design_name") if isinstance(row, dict) else None
    return name if name else f"design_{i}"


def _manifest_status_map(manifest):
    """``{design_name -> [ {column -> status_token}, ... ]}`` from a bench manifest (for
    the unverified discount). FIX-C: the manifest is name-keyed BUT a bench folder can hold
    two designs sharing a design_name, so the value is a LIST of per-block status dicts in
    manifest order (not a single dict that last-wins-collapses same-name blocks). A caller
    pairs the k-th same-named CSV row with the k-th same-named block, so a duplicate name no
    longer inherits a sibling's status. Total; never raises."""
    out = {}
    if not isinstance(manifest, dict):
        return out
    for block in manifest.get("designs") or []:
        if not isinstance(block, dict):
            continue
        name = block.get("design_name")
        if not name:
            continue
        # FIX-B: a same-named block whose ``metrics`` is a NON-DICT (malformed) is
        # PLACEHOLDER-PADDED with an empty ``{}`` status (kept in occurrence order), NOT
        # skipped — otherwise the per-occurrence pairing SHIFTS and a later same-named CSV
        # row inherits a SIBLING's ``unverified`` discount. Fail-closed: a malformed block
        # contributes NO discount for its own occurrence, never a neighbour's.
        metrics = block.get("metrics")
        per = {}
        if isinstance(metrics, dict):
            for col, cell in metrics.items():
                if isinstance(cell, dict):
                    per[col] = cell.get("status")
        out.setdefault(name, []).append(per)
    return out


def _resolve_design_status(manifest_statuses, name, occurrence):
    """The per-ROW status dict for the ``occurrence``-th row named ``name`` (FIX-C — the
    unverified discount must land on the RIGHT row, not be inherited across same-name rows).

    Accepts BOTH shapes: a LIST (the ``_manifest_status_map`` per-occurrence form -> index by
    occurrence, out-of-range -> ``{}``) and a plain DICT (a directly-supplied
    ``{name -> {col -> status}}``, applied to every same-name row — correct when names are
    unique). Missing / any other -> ``{}``. Never raises."""
    entry = manifest_statuses.get(name)
    if isinstance(entry, list):
        return entry[occurrence] if 0 <= occurrence < len(entry) else {}
    if isinstance(entry, dict):
        return entry
    return {}


def _ledger_source_kinds(ledger):
    """``{stem -> source_kind}`` from a ``build_ledger.json`` (patent provenance).
    Total; never raises."""
    out = {}
    if not isinstance(ledger, dict):
        return out
    for entry in ledger.get("built") or []:
        if isinstance(entry, dict) and entry.get("stem"):
            out[entry["stem"]] = entry.get("source_kind") or "zmx"
    return out


def compute_ranking(rows, template, *, manifest_statuses=None, source_kinds=None):
    """THE deterministic scorer. Pure — no engine, no file I/O.

    ``rows``: parsed CSV rows (list of dict, cells are strings). ``template``: the
    resolved template dict (reads ``ranking.priorities``/``weights`` + ``metrics``).
    ``manifest_statuses``: ``{design_name -> {col -> status}}`` (an ``unverified``
    axis is discounted ×0.5). ``source_kinds``: ``{stem -> source_kind}`` (ledger).

    TYPE CONTRACT (tracked, not guarded): ``rows`` MUST be a list-of-dict, and
    ``manifest_statuses``/``source_kinds`` MUST be dicts (of dicts / of scalars). Both CLI
    subcommands supply exactly these types — ``_read_csv_rows`` yields list-of-dict and
    ``_manifest_status_map``/``_ledger_source_kinds`` yield dicts — so a NON-dict ``rows``
    element (or a non-dict container) is OUTSIDE the documented contract and unreachable from
    either subcommand; it would ``AttributeError`` here by design (no per-container guard — the
    ``_cmd_bench``/``_cmd_rank`` outer nets are the boundary backstop for any such pathology).

    NULL POLICY (the load-bearing contract): a ``null``/``skip`` cell = no-data on
    that axis -> the axis is DROPPED from that design's weighted mean (its weight
    leaves the denominator), NEVER imputed to 0/worst/best. A real ``0``/``0.0`` is a
    finite value that participates at its axis min end. Returns the ranked list."""
    manifest_statuses = manifest_statuses or {}
    source_kinds = source_kinds or {}
    axes = _resolve_axes(template)
    ranking = template.get("ranking") if isinstance(template, dict) else None
    ranking = ranking if isinstance(ranking, dict) else {}
    weights = ranking.get("weights") if isinstance(ranking.get("weights"), dict) else {}
    dirmap = _column_direction_map()
    first_priority = axes[0] if axes else None

    names = [_row_name(row, i) for i, row in enumerate(rows)]

    # FIX-C: pair each row to its manifest status block by (name, occurrence order), so a
    # DUPLICATE design_name does NOT inherit a sibling's status (the manifest is name-keyed;
    # same-name rows disambiguate by row order — aligned with FIX-2's per-row identity).
    _name_occurrence = {}
    row_statuses = []
    for _i, _name in enumerate(names):
        _k = _name_occurrence.get(_name, 0)
        _name_occurrence[_name] = _k + 1
        row_statuses.append(_resolve_design_status(manifest_statuses, _name, _k))

    # --- Per-axis min-max normalize over the designs with FINITE data on the axis. -
    # FIX-2: the per-axis structures are keyed by ROW IDENTITY (the row index ``i``),
    # NOT ``design_name``. Two rows sharing a design_name (the ``rank`` subcommand
    # consumes an ARBITRARY external CSV, so duplicate names are reachable) must EACH
    # participate in the axis min-max + get their own score — a name-keyed dict would
    # OVERWRITE the first row's value (dropping the true min/max) and let a null row
    # inherit the survivor's finite value (the never-fake-0 breach, path 2). The
    # design_name is preserved for display via ``names[i]``.
    per_axis_value = {}   # axis -> {row_idx -> finite value}
    per_axis_score = {}   # axis -> {row_idx -> normalized score in [0,1]}
    per_axis_degenerate = {}
    for axis in axes:
        vals = {}
        for i, row in enumerate(rows):
            disp, v = classify_cell(row.get(axis, ""))
            if disp == "finite":
                vals[i] = v
        per_axis_value[axis] = vals
        finite_values = list(vals.values())
        flip = dirmap.get(axis) is False   # literal spec rule: flip iff exactly False.
        lo = min(finite_values) if finite_values else None
        hi = max(finite_values) if finite_values else None
        # FIX-A (the ABSOLUTE never-fake-0 invariant, pt 2): guard the min-max span against
        # a NON-FINITE range. A ``±1e308`` axis (both extremes present -> ``hi - lo``
        # OVERFLOWS to ``inf``) makes EVERY ``(v - lo)/(hi - lo)`` non-finite (inf/nan), so
        # no value on the axis can be honestly normalized -> the ENTIRE axis is treated as
        # ABSENT (``scores = {}`` -> every design reads ``no_data`` on it, dropping it from
        # num/den), NEVER a NaN score that would poison the weighted mean OR fabricate a 0.0
        # (``finite/inf -> 0.0``). ``min == max`` stays the degenerate flat-0.5 branch below
        # (its span is 0, not non-finite).
        span = (hi - lo) if (lo is not None and hi is not None) else None
        span_nonfinite = span is not None and not math.isfinite(span)
        # FIX-5: a degenerate axis (<=1 finite value, or all-identical min==max)
        # normalizes to a flat 0.5 — recorded so a reader can distinguish that 0.5
        # from a mid-range real score (it did not discriminate).
        degenerate = len(finite_values) <= 1 or (lo is not None and hi == lo)
        per_axis_degenerate[axis] = degenerate or span_nonfinite
        scores = {}
        if not span_nonfinite:
            for i, v in vals.items():
                if degenerate:
                    s = 0.5
                else:
                    s = (v - lo) / (hi - lo)
                    if flip:
                        s = 1.0 - s
                # Belt-and-suspenders (pt 2): a non-finite normalized value (should be
                # unreachable once the span is finite) is DROPPED, never scored.
                if not math.isfinite(s):
                    continue
                scores[i] = s
        per_axis_score[axis] = scores

    # --- Combine = weighted mean over ONLY the axes each design has finite data on. -
    results = []
    for i, row in enumerate(rows):
        name = names[i]
        num = 0.0
        den = 0.0
        axes_scored = []
        axes_missing = []
        unverified_axes = []
        per_axis_out = {}
        design_status = row_statuses[i]
        for axis in axes:
            scores = per_axis_score[axis]
            if i in scores:
                score = scores[i]
                base_w = _axis_weight(weights, axis)
                eff_w = base_w
                if design_status.get(axis) == "unverified":
                    eff_w = base_w * _UNVERIFIED_WEIGHT
                    unverified_axes.append(axis)
                num += eff_w * score
                den += eff_w
                per_axis_out[axis] = {"value": per_axis_value[axis][i],
                                      "score": score,
                                      "degenerate": bool(per_axis_degenerate[axis])}
                # FIX-C: a ZERO-effective-weight axis is scored (present in ``per_axis``
                # with its value + score) but EXCLUDED from ``axes_scored`` — it does not
                # participate in num/den, so it must not count as "scored" and must not
                # perturb the ``-len(axes_scored)`` tie-break (ranking-inert, not just
                # deterministic). A positive-weight axis is unaffected.
                if eff_w > 0.0:
                    axes_scored.append(axis)
            else:
                axes_missing.append(axis)
                per_axis_out[axis] = "no_data"
        # FIX-A (the never-fake-0 CLASS, a DIRECT INVARIANT): the combined score is
        # ``None`` + ``unrankable`` whenever ``den == 0`` — REGARDLESS of cause (no scored
        # axis, all-zero effective weights, a single zero-weight axis, any other path). A
        # fabricated ``0.0`` is UNREACHABLE on any ``den == 0`` path; the ONLY way to score
        # ``0.0`` is a genuinely-measured axis-minimum (finite data, positive weight,
        # normalized to 0 -> ``den > 0``). This collapses the two prior limbs
        # (``axes_scored`` empty AND the ``den == 0`` all-zero-weight case) that were patched
        # limb-by-limb into ONE rule, so no sibling ``den == 0`` trigger can fabricate a 0.0.
        combined = (num / den) if den > 0 else None
        # FIX-A (pt 3, the FINITE-or-None backstop): the combined score is EXACTLY a finite
        # float OR ``None`` on EVERY path — a non-finite result (nan/inf, should be
        # unreachable now that weights are clamped + non-finite axis scores are dropped) is
        # coerced to ``None`` + unrankable, so ``combined ∈ {finite float, None}`` always and
        # the sort key (below) is TOTAL + reproducible across input permutations.
        if combined is not None and not math.isfinite(combined):
            combined = None
        unrankable = combined is None
        fp_score = per_axis_score.get(first_priority, {}).get(i)
        results.append({
            "design_name": name,
            "combined_score": combined,
            "unrankable": unrankable,
            "per_axis": per_axis_out,
            "axes_scored": axes_scored,
            "axes_missing": axes_missing,
            # FIX-2b (R6, the never-raise CLASS, direct-API path): ``source_kind`` is a
            # documented SCALAR token, but a malformed ledger (or a direct caller) can hand a
            # NON-scalar (a set/dict) which then makes the ``_sort_key`` ``json.dumps(entry,
            # sort_keys=True)`` tiebreak raise ``TypeError`` (a dict with unsortable keys) or
            # otherwise mis-serialize — a raise OFF the CLI path (breaching the "never raises"
            # docstring). Coerce to ``str`` at the SOURCE (the honest normalization of a scalar
            # field): a real ``"zmx"``/``"patent_built"`` is byte-identical (``str(s) is s``),
            # only a non-scalar is normalized to its repr so the total, permutation-invariant
            # sort key can never raise on this field.
            "source_kind": str(source_kinds.get(name, "zmx")),
            "unverified_axes": unverified_axes,
            "_fp": fp_score if _num(fp_score) else -1.0,
        })

    # --- Order + the fully-deterministic tie-break. An unrankable (no-data) design
    # sorts LAST (labeled, never a fabricated middle/worst score); rankable designs
    # order by descending combined score. ------------------------------------------
    def _sort_key(e):
        unrankable = e["combined_score"] is None
        score = e["combined_score"] if not unrankable else 0.0
        # FIX-1 (R5, the reproducible-order ABSOLUTE close): ``design_name`` is NOT unique
        # across two DISTINCT same-named designs, so two dups tied on every earlier key had
        # IDENTICAL sort keys and Python's stable sort preserved INPUT order -> the written
        # ranking (and the stamped ranks) was permutation-DEPENDENT on a duplicate-name tie
        # (a reproducibility requirement). A CONTENT-BASED final tiebreaker (a canonical
        # ``json.dumps`` of the ENTIRE entry, ``sort_keys`` -> key-order-invariant) makes the
        # order a total, permutation-invariant function of each design's scored content: two
        # same-named designs with DIFFERENT content sort deterministically; two identical in
        # BOTH name AND content are genuinely interchangeable (any order is byte-identical
        # output). The key is stable (each entry field is computed from the design's own row +
        # catalog-wide min/max anchors, both permutation-invariant) and total (string compare).
        # ``rank`` is not yet stamped at sort time, so it never enters the key.
        content = json.dumps(e, sort_keys=True, default=str)
        return (unrankable, -score, -len(e["axes_scored"]), -e["_fp"],
                e["design_name"], content)

    results.sort(key=_sort_key)
    for rank, e in enumerate(results, start=1):
        e["rank"] = rank
        e.pop("_fp", None)
    return results


def _cmd_rank(args):
    """The ``rank`` subcommand — the OUTER NEVER-RAISE NET (R6, a DIRECT INVARIANT).

    The exhaustive class close: instead of patching each malformed-input type at its raise
    site, the whole ``rank`` body is wrapped so ANY exception — an unhashable metric key, a
    malformed template, a bad weight, a non-scalar source_kind, a FUTURE unknown pathology —
    becomes a structured ``{"ok": false, "reason": "internal", ...}`` envelope + rc 1 printed
    as the normal JSON summary, NEVER a traceback out of ``main``. Mirrors the ``bench`` path's
    top-level net shape. The early ``ok:false`` validation returns inside ``_rank_impl`` do NOT
    raise, so they pass through unchanged; the net is a pure backstop (with FIX-2 the known
    malformed inputs degrade GRACEFULLY inside ``_rank_impl`` rather than reaching this net)."""
    try:
        return _rank_impl(args)
    except Exception as exc:  # noqa: BLE001 — the boundary net: no input can traceback the CLI.
        print(json.dumps({"ok": False, "reason": "internal", "error": repr(exc)}))
        print(f": FATAL: {exc!r}", file=sys.stderr)
        return 1


def _rank_impl(args):
    """The ``rank`` subcommand body (wrapped by ``_cmd_rank``'s never-raise net): read the CSV
    (+ sibling manifest + optional ledger), score deterministically, write
    ``ranking_scores.json``. Engine-free.

    FIX-6: the REQUIRED inputs (CSV + template) are VALIDATED up front — a missing /
    unreadable required input reports ``ok:false`` + a reason (never a silent ``ok:true``
    over an empty ranking). A read fault is distinguished from a genuinely-empty catalog
    (a header-only CSV with 0 data rows is valid -> ok:true, n_ranked 0)."""
    # --- FIX-6: validate the required inputs before scoring. -----------------------
    errors = []
    if not os.path.isfile(args.csv):
        errors.append(f"csv not found: {args.csv}")
    if not os.path.isfile(args.template):
        errors.append(f"template not found: {args.template}")
    if errors:
        print(json.dumps({"ok": False, "reason": "input_unreadable", "errors": errors}))
        for e in errors:
            print(f": {e}", file=sys.stderr)
        return 1
    template = _read_json(args.template)
    if template is None:
        msg = f"template not readable/parseable: {args.template}"
        print(json.dumps({"ok": False, "reason": "input_unreadable", "errors": [msg]}))
        print(f": {msg}", file=sys.stderr)
        return 1
    header, rows = _read_csv_rows(args.csv)
    if not header and not rows:
        # The file exists but yielded NO header AND NO rows -> empty/malformed/unparseable (a
        # real read fault, incl. a csv.Error-raising oversized/binary CSV caught in
        # ``_read_csv_rows``), distinct from a valid header-only catalog (header, 0 rows).
        # FIX-D: ONE consistent not-ok reason vocabulary -> ``input_unreadable`` (matching the
        # missing-CSV / missing-template path above); the ``csv`` field carries the specifics.
        msg = f"csv empty or unreadable: {args.csv}"
        print(json.dumps({"ok": False, "reason": "input_unreadable", "csv": args.csv}))
        print(f": {msg}", file=sys.stderr)
        return 1

    manifest_path = args.manifest or os.path.join(
        os.path.dirname(os.path.abspath(args.csv)), "manifest.json")
    manifest = _read_json(manifest_path)
    ledger = _read_json(args.ledger) if args.ledger else None
    ranking = compute_ranking(
        rows, template,
        manifest_statuses=_manifest_status_map(manifest),
        source_kinds=_ledger_source_kinds(ledger))
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(ranking, fh, indent=1, default=str)

    summary = {"ok": True, "out": args.out, "n_ranked": len(ranking)}
    # FIX-4: disclose any unknown requested metric (never a silent drop).
    unknown = _unknown_metrics(template)
    if unknown:
        summary["unknown_metrics"] = unknown
        print(f": WARNING unknown metric(s) requested, dropped: {unknown}",
              file=sys.stderr)
    print(json.dumps(summary))
    return 0


# =========================================================================== #
# 3 — the ``bench`` subcommand (the SOLE seat-holder; reaps in finally).
# =========================================================================== #
def _spec_stem(path):
    """The stem of a ``*.spec.json`` file (``foo.spec.json`` -> ``foo``)."""
    base = os.path.basename(path)
    if base.endswith(_SPEC_SUFFIX):
        return base[:-len(_SPEC_SUFFIX)]
    return os.path.splitext(base)[0]


def _unique_build_stem(staging_dir, stem):
    """Resolve a build stem whose ``<stem>.zmx`` destination does NOT already exist in
    ``staging_dir`` (FIX-3). If ``<stem>.zmx`` is free, returns ``stem``; else loops
    ``<stem>-built``, ``<stem>-built-2``, ... until the destination is free, so a build
    NEVER overwrites an existing staged file (a user's ``.zmx``/``-built.zmx``, or another
    build's output this run)."""
    if not os.path.exists(os.path.join(staging_dir, stem + ".zmx")):
        return stem
    n = 1
    while True:
        cand = stem + "-built" + ("" if n == 1 else f"-{n}")
        if not os.path.exists(os.path.join(staging_dir, cand + ".zmx")):
            return cand
        n += 1


def _build_specs(dispatcher, staging_dir, out_dir):
    """Phase 1 — build every ``staging/*.spec.json`` into ``staging/<stem>.zmx``
    BEFORE any ``.zmx`` is loaded (the BUILD-BEFORE-LOAD invariant). Returns the
    ``build_ledger`` dict (a fixed shape). A refusal is a REPORTED absence (ledger
    ``failed``), never a crash and never a silent drop."""
    built = []
    failed = []
    for spec_path in sorted(glob.glob(os.path.join(staging_dir, "*" + _SPEC_SUFFIX))):
        stem = _spec_stem(spec_path)
        spec = _read_json(spec_path)
        if not isinstance(spec, dict):
            failed.append({"stem": stem, "stage": "apply",
                           "error_family": "spec_unreadable",
                           "message": f"spec.json is not a JSON object: {spec_path}"})
            continue
        # Stem-collision guard (FIX-3): never overwrite an EXISTING staged .zmx — not a
        # user's ``<stem>.zmx``, not a user's ``<stem>-built.zmx``, and not another
        # build's output this run (each build writes its .zmx before the next iteration,
        # so the on-disk check catches within-run convergence too). Loop a suffix until
        # the destination path is free (``-built``, ``-built-2``, ...).
        final_stem = _unique_build_stem(staging_dir, stem)

        env = dispatcher.dispatch("apply_lens_spec", {"lens_spec": spec})
        ok, _hint, reason = reading_ok(env)
        if not ok:
            failed.append({"stem": stem, "stage": "apply",
                           "error_family": reason or "apply_refused",
                           "message": f"apply_lens_spec: {reason}"})
            continue

        snap = dispatcher.dispatch("save_snapshot", {"label": final_stem})
        ok, _hint, reason = reading_ok(snap)
        if not ok:
            failed.append({"stem": stem, "stage": "save",
                           "error_family": reason or "save_refused",
                           "message": f"save_snapshot: {reason}"})
            continue
        src_path = snap["result"].get("path")
        dst = os.path.join(staging_dir, final_stem + ".zmx")
        try:
            shutil.copy(src_path, dst)
        except (OSError, shutil.Error, TypeError) as exc:
            failed.append({"stem": stem, "stage": "save",
                           "error_family": "copy_failed",
                           "message": f"copy {src_path!r} -> {dst!r}: {exc!r}"})
            continue
        built.append({"stem": final_stem, "source_kind": "patent_built",
                      "zmx": os.path.join("staging", final_stem + ".zmx")})
    return {"built": built, "failed": failed}


def _render_layouts(dispatcher, staging_dir, layouts_dir):
    """Phase 3 — one geometry-only (``draw_rays=False``) layout PNG per staged
    ``.zmx`` (a 3rd load per design). A render fault is NON-FATAL (the montage
    degrades to a placeholder tile — plot.py handles a missing PNG). Never raises."""
    os.makedirs(layouts_dir, exist_ok=True)
    rendered = 0
    for path in sorted(glob.glob(os.path.join(staging_dir, "*.zmx"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        env = dispatcher.dispatch("load_design", {"path": path})
        ok, _hint, _reason = reading_ok(env)
        if not ok:
            continue
        png = os.path.join(layouts_dir, stem + ".png")
        renv = dispatcher.dispatch(
            "render_layout", {"path": png, "draw_rays": False})
        ok, _hint, _reason = reading_ok(renv)
        if ok:
            rendered += 1
    return rendered


def _bands_from_basis(target_basis):
    """Translate the manifest's resolved ``run.target_basis`` into ``plot_metrics``'s
    per-COLUMN acceptance-band dict. The 0.99/1.01 + 0.97/1.03 factors are
    ARBITRARY COSMETIC shading constants (never the ranker's pass/fail). Returns the
    merged dict, or ``None`` if neither band applies."""
    if not isinstance(target_basis, dict):
        return None
    bands = {}
    efl = target_basis.get("efl_mm")
    if _finite_pos(efl):
        bands["norm_efl_mm"] = {"min": efl * 0.99, "max": efl * 1.01}
    fnum = target_basis.get("fnum")
    if target_basis.get("fnum_basis") == "pinned" and _finite_pos(fnum):
        bands["fnum"] = {"min": fnum * 0.97, "max": fnum * 1.03}
    return bands or None


def _build_session():
    """Construct an UN-opened ``ZemaxSession`` reading the same operator-surface env
    as ``__main__`` (``OPTIVIBE_CONNECT_TIMEOUT_S`` / ``OPTIVIBE_CALL_WARN_S``). The
    engine opens on the first dispatch; this touches no engine."""
    from ..session import ZemaxSession
    connect_timeout_s = _env_float("OPTIVIBE_CONNECT_TIMEOUT_S", 30.0)
    call_warn_s = _env_float("OPTIVIBE_CALL_WARN_S", 60.0)
    return ZemaxSession(connect_timeout_s=connect_timeout_s,
                        slow_call_threshold_s=call_warn_s)


def _env_float(name, default):
    """Read an env var as a positive-finite float, else ``default`` (mirrors
    ``__main__._env_float`` — a typo/pathological value never crashes the run)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


# =========================================================================== #
# One-engine policy (engine_busy soft-decline + post-bench discriminator).
#
# A premise correction, established live: a second OpticStudio engine may or may not be
# able to start depending on the OpticStudio edition and licence in use — on at least one
# edition a 2nd engine COEXISTS and loads fine (a real forced collision returned n_ok:2,
# rc 0 — the FRU startup banner it prints is COSMETIC, not a crippling failure); an
# earlier reported "all load_not_found" failure was actually a RELATIVE-STAGING-PATH bug
# (fixed by _bench_impl's os.path.abspath), NOT an engine-seat collision. OptiVibe
# DELIBERATELY keeps to ONE engine either way, for adaptability — not because a second one
# is assumed to be blocked — so the preflight is an honest SOFT DECLINE (engine_busy) when
# an engine is already running — it opens no second engine. The all-designs-failed-to-load
# discriminator stays as the never-rc0 backstop for a genuinely-unloadable staging.
# =========================================================================== #
def _find_live_engine_seat():
    """Return the ``engine_ledger.EngineRecord`` of a LIVE concurrent seat holder
    (engine alive AND its recording parent still alive), else ``None``.

    Reuses the engine-session-launch predicates VERBATIM: a record is a live seat iff
    ``engine_identity_ok(engine_pid, engine_create_time) AND NOT
    is_parent_dead(parent_pid, parent_create_time)`` (confirmed live). Records are
    scanned in ascending ``engine_pid`` order so the named PID is deterministic when
    (defensively) more than one live record exists.

    NEVER raises; fails SAFE — ``read_ledger()`` -> ``{}`` on any fault, both predicates
    never raise (AccessDenied -> parent ALIVE -> refuse; gone/recycled engine ->
    identity_ok False -> pass; dead parent -> pass). A fault -> None -> no false
    refuse; the post-bench discriminator is the backstop.

    INTENTIONAL, do NOT change: the ``from .. import engine_ledger`` below is the
    FIRST line INSIDE the guarded ``try``, so an ImportError -> ``except`` -> None ->
    fail-OPEN (no false refuse; the post-bench backstop covers it). Do NOT hoist this
    import to module level — a module-level import error would crash ``__main__`` load
    instead of degrading safely.
    """
    try:
        from .. import engine_ledger
        records = sorted(engine_ledger.read_ledger().values(),
                         key=lambda r: r.engine_pid)
        for rec in records:
            if (engine_ledger.engine_identity_ok(rec.engine_pid, rec.engine_create_time)
                    and not engine_ledger.is_parent_dead(rec.parent_pid,
                                                          rec.parent_create_time)):
                return rec
    except Exception:  # noqa: BLE001 — read_ledger + predicates already never raise; belt only.
        return None
    return None


def _all_designs_failed_to_load(summary):
    """True iff EVERY design failed at the LOAD stage (family in ``_LOAD_FAILURE_FAMILIES``:
    ``load_not_found`` OR ``load_failed``) — the 'engine loaded NO design' HEURISTIC signature
    (a WHOLE-RUN failure, NOT N genuinely-bad per-design verdicts). Read from the on-disk
    manifest's TOP-LEVEL ``ledger`` list (bench.py ``_build_manifest`` pops each block's
    ``_ledger`` there; the ``designs`` rows do NOT carry ``error_family`` — see the verified
    schema this discriminator was built against). ``n_designs``/``n_ok`` from the summary
    dict; the manifest via the never-raising ``_read_json``.

    Found during a hardening review: the family term is the SET, not the single
    ``load_not_found``. A contended second engine SILENTLY no-ops ``LoadFile`` (the
    read-back proof fails -> ``load_failed``), so a clean-path foreign contention produces
    an all-``load_failed`` ledger that a ``load_not_found``-only check would MISS -> a
    false ``ok:true`` / rc 0 (exactly the silent-wrong this discriminator exists to close).
    An earlier reported failure only showed ``load_not_found`` because
    its staging paths were RELATIVE (a pre-engine path artifact).

    The exact conjunction:
        n_designs is a non-bool int AND n_designs > 0
        AND n_ok == 0
        AND manifest_doc is a dict (readable JSON object)
        AND manifest_doc['ledger'] is a list of EXACTLY n_designs items
        AND EVERY entry is a dict with stage=='load' AND
            error_family in _LOAD_FAILURE_FAMILIES
    The ``len == n_designs`` coverage guard keeps this non-vacuous: a design that DID load then
    failed later carries a stage!='load' (or no) ledger entry -> coverage/stage term rejects it
    -> False (a healthy/mixed/partial run is never flagged). NEVER raises. (A malformed/
    unreadable manifest is NOT proof of contention -> False is the correct, fail-SAFE answer;
    the exception guard is a data fault, not a code-fault swallow.)
    """
    try:
        n_designs = summary.get("n_designs")
        n_ok = summary.get("n_ok")
        if (not isinstance(n_designs, int) or isinstance(n_designs, bool)
                or n_designs <= 0 or n_ok != 0):
            return False
        manifest_doc = _read_json(summary.get("manifest"))
        if not isinstance(manifest_doc, dict):
            return False
        ledger = manifest_doc.get("ledger")
        if not isinstance(ledger, list) or len(ledger) != n_designs:
            return False
        return all(isinstance(e, dict)
                   and e.get("stage") == "load"
                   and e.get("error_family") in _LOAD_FAILURE_FAMILIES
                   for e in ledger)
    except Exception:  # noqa: BLE001 — a data fault (bad manifest) is not contention proof.
        return False


def _finalize_bench_summary(summary):
    """The single post-bench boundary chokepoint: the ONE place the
    all-designs-failed-to-load HEURISTIC is applied. Returns ``(summary, rc)``.

    On the signature (``_all_designs_failed_to_load`` True): flip ok->False, add the
    NEUTRAL/HONEST attribution keys, KEEP the artifact paths + counts as evidence,
    rc=RC_NOT_BENCHED. Otherwise byte-identical passthrough ``(summary, 0)``. Do NOT add
    sibling guards in bench_folder / layouts / plot / rank.

    Found during a hardening review: the run-level family is the NEUTRAL
    ``all_designs_failed_load``, NOT a CERTAIN ``engine_start_contended``.
    Every-design-failed-to-load can be a contended seat OR bad/relative staging paths OR
    corrupt .zmx — the post-bench half has NO ledger proof of a seat (a ledger-visible
    seat is refused earlier at PREFLIGHT), so it must NOT assert engine contention. The
    ``remedy`` names all three plausible causes honestly.

    FAIL-CLOSED (Fix-3): a NON-DICT ``summary`` is OUTSIDE the documented contract
    (``_bench_impl`` always builds a dict); it RAISES here (defense-in-depth) so the
    engine-phase ``try/except Exception: return 1`` net reports rc 1 + reaps, rather than the
    discriminator's data-fault guard swallowing it to ``(summary, 0)``. This function otherwise
    has NO rc-0-swallowing catch — ``_all_designs_failed_to_load`` already never raises (its
    data-fault guard returns False on a bad manifest — the correct, fail-safe answer), and the
    dict work below cannot raise on a dict summary. A genuine CODE fault here therefore
    PROPAGATES to that engine-phase net — fail-closed rc 1 — rather than silently emitting rc 0
    on a possibly-failed run. NEVER return ``(summary, 0)`` from an exception handler.
    """
    if not isinstance(summary, dict):
        # Fix-3: a non-dict summary is a CODE fault, not a data fault — fail CLOSED
        # (raise -> the engine-phase net returns rc 1 + reaps), never a silent (summary, 0).
        raise TypeError(
            "_finalize_bench_summary requires a dict summary, got "
            f"{type(summary).__name__}")
    if not _all_designs_failed_to_load(summary):
        return summary, 0
    n = summary.get("n_designs")
    flipped = dict(summary)
    flipped.update({
        "ok": False,
        "error_family": "all_designs_failed_load",
        "stage": "post_bench",
        "reason": ("every design failed at the LOAD stage (no design loaded) — a "
                   "whole-run failure, NOT N per-design design verdicts."),
        "remedy": ("possible causes: (a) a contended OpticStudio seat — most likely a "
                   "FOREIGN/interactive OpticStudio not in OptiVibe's ledger (the "
                   "ledger-visible case is caught earlier at preflight); (b) bad or "
                   "relative staging paths (a relative path resolves under the "
                   "workspace/output root, not your CWD — pass absolute staging paths); "
                   "(c) missing / empty / corrupt .zmx. Check the CLI rc and top-level "
                   "`ok`; the per-design `load_not_found`/`load_failed` rows and "
                   "CSV/manifest are kept as diagnostics."),
        "engine_pid": None,   # a foreign engine (if any) is not in OptiVibe's ledger.
        "parent_pid": None,
        "failure_signature": {"n_load_failed": n, "n_designs": n},
    })
    return flipped, RC_NOT_BENCHED


def _cmd_bench(args):
    """The ``bench`` subcommand — the OUTER NEVER-RAISE NET (R7, the symmetric close with the
    ``rank`` path). The whole bench body — its SETUP (import Dispatcher / makedirs /
    template read / session construct) AND the engine phase — is wrapped so ANY exception
    becomes a structured ``{"ok": false, "reason": ...}`` envelope + rc 1, NEVER a traceback out
    of ``main``. A SETUP fault (e.g. ``--out`` names an existing FILE -> makedirs
    ``FileExistsError``) happens BEFORE ``session.open()``, so NO seat is taken and there is
    nothing to reap; a fault AFTER open is still reaped by ``_bench_impl``'s ``finally``.
    Mirrors ``_cmd_rank``'s outer net."""
    try:
        return _bench_impl(args)
    except Exception as exc:  # noqa: BLE001 — the boundary net: no input can traceback the CLI.
        print(json.dumps({"ok": False, "reason": "internal", "error": repr(exc)}))
        print(f": FATAL: {exc!r}", file=sys.stderr)
        return 1


def _bench_impl(args):
    """The ``bench`` subcommand body (wrapped by ``_cmd_bench``'s never-raise net): build ->
    bench -> layouts -> plot -> rank, over ONE engine reaped EXACTLY ONCE in ``finally``.
    Prints a one-line JSON summary to stdout. The engine-phase behavior is unchanged."""
    from ..server import Dispatcher

    # A later hardening round (a fix-INTRODUCED sibling of the abspath fix below): reject an
    # EMPTY / whitespace-only ``--staging`` BEFORE the abspath. ``os.path.abspath("") ==
    # os.getcwd()``, so an empty ``--staging`` (a plausible ``--staging "$STAGING"`` with an unset
    # var) would SILENTLY bench whatever ``*.zmx`` sits in the operator's CWD and report
    # ``ok:true`` / rc 0 — the exact "authoritatively benchmarks the WRONG dataset, reports
    # success" class this ticket exists to close (before CHANGE 1's abspath an empty staging
    # globbed relative paths that all failed to load -> the discriminator -> rc 3; the abspath
    # newly opened this silent-success hole). Pure ARG validation: report regardless of engine
    # state (BEFORE the abspath AND before the engine-busy preflight), open no session, makedirs
    # nothing, return early — mirroring the ``rank`` path's up-front input validation. Uses the
    # CLI's existing ``input_unreadable`` not-ok vocabulary (matching the ``--out`` path below).
    if not isinstance(args.staging, str) or not args.staging.strip():
        print(json.dumps({"ok": False, "reason": "input_unreadable",
                          "staging": args.staging}))
        print(": --staging is empty or whitespace; refusing to bench the "
              "current working directory.", file=sys.stderr)
        return 1

    # The ROOT FIX: resolve a RELATIVE ``--staging`` to an ABSOLUTE
    # path against the CWD (what the user means). ``_bench_impl`` sets ``session.workspace_root
    # = out_dir`` below, and ``load_design`` resolves a RELATIVE path FLAT under the
    # workspace_root (= out_dir), so a relative ``--staging`` globbed relative paths that
    # ``load_design`` then looked for at ``<out_dir>/<rel-staging>/*.zmx`` (nonexistent) ->
    # every design ``load_not_found``. THIS was an earlier reported failure's real root cause
    # (a relative staging path), NOT a seat collision. Absolutizing here makes
    # ``bench_folder``, ``_build_specs`` AND ``_render_layouts`` all glob absolute paths ->
    # ``load_design`` loads.
    staging = os.path.abspath(args.staging)
    out_dir = args.out

    # --- preflight: SOFT-DECLINE if an engine is already running. ---
    # This is NOT about a licence collision — a second engine may or may not be able to
    # start at all depending on the OpticStudio edition and licence in use (confirmed live
    # that on at least one edition a second engine coexists fine). OptiVibe
    # DELIBERATELY keeps to ONE engine either way (for adaptability), so if a live engine
    # is already recorded in the ledger the bench declines rather than opening a second.
    # Returns before _build_session()/makedirs -> no out dir, no session, no 2nd engine,
    # nothing to reap.
    rec = _find_live_engine_seat()
    if rec is not None:
        print(json.dumps({
            "ok": False,
            "error_family": "engine_busy",
            "stage": "preflight",
            "reason": ("an OpticStudio engine is already running; this bench keeps to a "
                       "SINGLE engine (it will not open a second)"),
            "engine_pid": rec.engine_pid,
            "parent_pid": rec.parent_pid,
            "remedy": ("run this bench from a COLD session (no prior design-touching MCP "
                       "call), or profile the designs through the held MCP seat"),
            "csv": None, "manifest": None, "bars": None, "montage": None,
            "ranking_scores": None, "n_designs": None, "n_ok": None,
            "n_partial": None, "n_failed": None, "build_failed": None,
        }))
        print(f": ENGINE_BUSY: an OpticStudio engine (pid={rec.engine_pid}, "
              f"parent pid={rec.parent_pid}) is already running — this bench keeps to one "
              f"engine. Run from a cold session, or profile via the held MCP seat.",
              file=sys.stderr)
        return RC_NOT_BENCHED

    try:
        os.makedirs(out_dir, exist_ok=True)   # a missing dir is otherwise a silent no-op.
    except (OSError, ValueError) as exc:
        # An operator-arg edge that happens BEFORE any session is constructed (e.g. ``--out``
        # names an existing FILE -> ``FileExistsError``, or an embedded-NUL path -> ``ValueError``
        # -- the catch is deliberately broadened beyond ``OSError`` for that case). NO seat is
        # taken yet, so there is nothing to reap. Degrade GRACEFULLY here with the
        # ``input_unreadable`` reason (matching the ``rank`` path's up-front input
        # validation), rather than reaching the outer net.
        print(json.dumps({"ok": False, "reason": "input_unreadable", "out": out_dir}))
        print(f": out dir not creatable: {out_dir}: {exc!r}", file=sys.stderr)
        return 1
    template = _read_json(args.template)
    if template is None:
        print(f": template not readable: {args.template}", file=sys.stderr)
        template = {}

    session = _build_session()
    try:
        session.workspace_root = out_dir   # saved artifacts land under out/.
    except Exception:  # noqa: BLE001 — an attr set must never sink the run.
        pass

    ledger = {"built": [], "failed": []}
    try:
        # OPEN the engine eagerly: the CLI is the SOLE seat-holder for the whole engine
        # phase. The plain Dispatcher does NOT lazy-open (that is the MCP's
        # LazyHarnessDispatcher) — its handlers dispatch against a LIVE session, so the
        # session must be opened before the first tool call (mirrors the live test's
        # ZemaxSession().__enter__()). An open failure raises -> the except below exits 1
        # and the finally reaps (close on a half-open/never-opened session is safe).
        session.open()
        dispatcher = Dispatcher(session)

        # Phase 1 — build patent specs BEFORE any .zmx load (build-before-load).
        ledger = _build_specs(dispatcher, staging, out_dir)
        with open(os.path.join(out_dir, "build_ledger.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(ledger, fh, indent=1, default=str)

        # Phase 2 — bench (writes catalog_metrics.csv + manifest.json).
        result = B.bench_folder(dispatcher, staging, template, out_dir)

        # Phase 3 — per-design geometry-only layouts (unless --no-layouts).
        layouts_dir = os.path.join(out_dir, "layouts")
        if not args.no_layouts:
            _render_layouts(dispatcher, staging, layouts_dir)

        # Phase 4 — plot (bars + montage + warnings sidecar).
        bands = _bands_from_basis(
            (((result.get("manifest") or {}).get("run") or {}).get("target_basis")))
        plot_metrics(
            os.path.join(out_dir, "catalog_metrics.csv"), out_dir,
            layout_pngs=(None if args.no_layouts else layouts_dir),
            target_basis=bands)

        # Phase 5 — the pure deterministic rank scorer (engine-free; writes
        # ranking_scores.json from the just-written CSV + manifest + ledger).
        _header, rows = _read_csv_rows(os.path.join(out_dir, "catalog_metrics.csv"))
        ranking = compute_ranking(
            rows, template,
            manifest_statuses=_manifest_status_map(result.get("manifest")),
            source_kinds=_ledger_source_kinds(ledger))
        with open(os.path.join(out_dir, "ranking_scores.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(ranking, fh, indent=1, default=str)

        summary = {
            "ok": True,
            "csv": os.path.join(out_dir, "catalog_metrics.csv"),
            "manifest": os.path.join(out_dir, "manifest.json"),
            "bars": os.path.join(out_dir, "catalog_bars.png"),
            "montage": os.path.join(out_dir, "catalog_montage.png"),
            "ranking_scores": os.path.join(out_dir, "ranking_scores.json"),
            "n_designs": result.get("n_designs"),
            "n_ok": result.get("n_ok"),
            "n_partial": result.get("n_partial"),
            "n_failed": result.get("n_failed"),
            "build_failed": [f.get("stem") for f in ledger.get("failed", [])],
        }
        # FIX-4: disclose any unknown requested metric (never a silent drop).
        unknown = _unknown_metrics(template)
        if unknown:
            summary["unknown_metrics"] = unknown
            print(f": WARNING unknown metric(s) requested, dropped: {unknown}",
                  file=sys.stderr)
        # A DIRECT-INVARIANT boundary: an all-designs-failed-to-load run is never
        # ok/rc0 (the run-level attribution is NEUTRAL — could be a foreign seat, bad/relative
        # staging paths, or corrupt .zmx; the ledger-visible seat is refused at preflight).
        summary, rc = _finalize_bench_summary(summary)
        if rc == RC_NOT_BENCHED:
            print(": ALL_DESIGNS_FAILED_LOAD: every design failed at the LOAD "
                  "stage (no design loaded) — possibly a foreign/contended OpticStudio seat, "
                  "bad/relative staging paths, or corrupt .zmx. Check rc + top-level ok; "
                  "artifacts kept for diagnosis.", file=sys.stderr)
        print(json.dumps(summary))
        return rc
    except Exception as exc:  # noqa: BLE001 — top-level: report + exit 1; finally reaps.
        print(f": FATAL: {exc!r}", file=sys.stderr)
        return 1
    finally:
        # SINGLE-OWNER reap: idempotent, never-raises, safe if never opened.
        session.close()


# =========================================================================== #
# Arg parse + entry.
# =========================================================================== #
def _build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m optivibe_harness.catalog",
        description="Batch benchmark + deterministic rank of a folder of lens designs.")
    sub = parser.add_subparsers(dest="command", required=True)

    bench_p = sub.add_parser("bench", help="build+bench+layout+plot a staging folder")
    bench_p.add_argument("--staging", required=True, help="the staging dir of *.zmx / *.spec.json")
    bench_p.add_argument("--out", required=True, help="the output dir (CSV/manifest/PNGs/ranking)")
    bench_p.add_argument("--template", required=True, help="the resolved template.json")
    bench_p.add_argument("--no-layouts", action="store_true",
                         help="skip per-design layout rendering (placeholder tiles)")
    bench_p.set_defaults(func=_cmd_bench)

    rank_p = sub.add_parser("rank", help="deterministic score of a catalog_metrics.csv")
    rank_p.add_argument("--csv", required=True, help="the catalog_metrics.csv")
    rank_p.add_argument("--template", required=True, help="the resolved template.json")
    rank_p.add_argument("--ledger", default=None, help="an optional build_ledger.json")
    rank_p.add_argument("--manifest", default=None,
                        help="the manifest.json (default: the CSV's sibling manifest.json)")
    rank_p.add_argument("--out", required=True, help="the ranking_scores.json to write")
    rank_p.set_defaults(func=_cmd_rank)
    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
