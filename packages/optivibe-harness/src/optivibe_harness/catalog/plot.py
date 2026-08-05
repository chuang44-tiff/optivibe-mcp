"""catalog/plot.py — the pure-Python plotter.

Renders the figures from a ``catalog_metrics.csv`` produced by the
bench core:

- ``catalog_bars.png`` — one bar-per-KPI panel per numeric metric column.
- ``catalog_montage.png`` — one layout-thumbnail tile per design.
- ``plot_warnings.txt`` — a plain-text warnings sidecar for the skill.

Scope boundary (load-bearing): pure Python + matplotlib/Agg. Touches NO engine,
NO ZOS-API tool, NO ``Dispatcher``, NO ``render_layout``. The ONLY inputs are the CSV
on disk, an optional caller-supplied set of ALREADY-rendered layout PNG paths, and an
optional spec-band ``target_basis`` dict. Column selection is derived from the CSV
header + data alone — NO ``registry``/``bench``/``metrics`` import.

Reuse: the PNG magic-byte oracle is imported (``_is_png``); the matplotlib
Agg idiom + the temp -> gate -> ``os.replace`` atomic save mirror ``tools/layout_render``.

The SINGLE cell-disposition + parse locus is ``classify_cell`` — the token ``float(``
appears NOWHERE else in this module (the disposition matrix is built ONCE and
consumed by BOTH column-selection and bar-rendering; a mutation test pins it).

NEVER raises: ``_plot_all`` wraps its body; a single design/tile/panel fault degrades
to a placeholder/skip + a warning. ``plot_metrics`` returns the written PNG paths only.
"""
import csv
import glob
import math
import os
import tempfile

from ..tools._image_gate import _is_png

# --------------------------------------------------------------------------- #
# Column-selection provenance guard — identity / normalization-proof
# columns that are labels or provenance, NEVER decision KPIs. ``norm_efl_mm`` is
# DELIBERATELY ABSENT (it is the achieved normalized EFL, a real KPI panel).
# --------------------------------------------------------------------------- #
_PROVENANCE_COLUMNS = {
    "design_name", "status", "reason", "source_file", "source_kind",
    "folded", "notes", "scaled_ok",            # identity / bool / status
    "vignetting_basis",                        # normalization operating-point token (string)
    "target_efl_mm", "native_efl_mm", "scale_factor",   # normalization provenance
}

# The narrow index-helper suffix: matches the two shipped
# ``*_worst_field`` index columns verbatim; NOT a broad ``_field`` that would swallow
# a future numeric KPI like ``depth_of_field``.
_INDEX_HELPER_SUFFIX = "_worst_field"

_BARS_NAME = "catalog_bars.png"
_MONTAGE_NAME = "catalog_montage.png"
_SIDECAR_NAME = "plot_warnings.txt"

_PANEL_BASELINE = 0.0                  # the fixed y for "n/a"/"?" annotations.
_BAR_COLOR = "#4C78A8"
_BAND_COLOR = "#4CAF50"                 # accept-green
_NA_COLOR = "0.5"
_SKIP_COLOR = "#D62728"                 # warn


# =========================================================================== #
# 4. The plot-disposition CLASSIFIER — the SINGLE parse + disposition locus.
#    ALLOWLIST; the DEFAULT branch is skip-with-marker (NOT a denylist). The ONLY
#    place ``float(`` appears in this module (the AST mutate-fails pins it).
# =========================================================================== #
def classify_cell(raw):
    """Map a RAW CSV cell string to a rendering disposition. NEVER raises.

    Returns one of exactly three dispositions:
      ("finite", <float>)  — a finite number (INCLUDING 0.0) -> draw a bar at that value.
      ("null",   None)     — the EMPTY cell -> NO bar + an "n/a" annotation.
      ("skip",   None)     — the DEFAULT for EVERYTHING ELSE -> NO bar + a "?" marker.
    """
    s = "" if raw is None else str(raw).strip()
    # ALLOWLIST branch 1 — the empty cell (null, ANY upstream status). NEVER imputed to 0.
    if s == "":
        return ("null", None)
    # ALLOWLIST branch 2 — a parseable FINITE number (0 / 0.0 = a real measured zero).
    try:
        v = float(s)
    except (TypeError, ValueError):
        return ("skip", None)          # DEFAULT: unparseable token -> skip-with-marker.
    if not math.isfinite(v):
        return ("skip", None)          # DEFAULT: inf/nan/-inf parse OK -> skip (not a bar, not 0).
    return ("finite", v)


# --------------------------------------------------------------------------- #
# matplotlib Agg idiom (mirror tools/layout_render._import_mpl): Agg BEFORE pyplot,
# lazy + guarded. A broken install -> a plot_unavailable warning + return [].
# --------------------------------------------------------------------------- #
def _import_mpl():
    """Lazy, guarded matplotlib import. Agg BEFORE pyplot. Returns ``(plt, imread)``."""
    import matplotlib
    matplotlib.use("Agg", force=True)  # headless, BEFORE pyplot
    import matplotlib.pyplot as plt
    from matplotlib.image import imread
    return plt, imread


# --------------------------------------------------------------------------- #
# Build-throw figure-leak guard. A build fn creates its figure INTERNALLY,
# so a throw AFTER plt.figure()/add_subplot (before the fig reaches the saver) would
# leak it across the many-designs loop. This decorator snapshots the open-figure set,
# and on ANY exception exit closes ONLY the figures created inside the call, then
# RE-RAISES so the caller's build-failed warning still fires. On a NORMAL return the
# created figure is LEFT OPEN — the saver owns and closes it (NO double-close).
# --------------------------------------------------------------------------- #
def _figure_guarded(build_fn):
    def _wrapped(*args, **kwargs):
        plt, _imread = _import_mpl()          # cached; also guarantees Agg. May raise
        pre = set(plt.get_fignums())          # (no figure yet) -> propagates, no leak.
        try:
            return build_fn(*args, **kwargs)
        except BaseException:
            for num in set(plt.get_fignums()) - pre:
                try:
                    plt.close(num)
                except Exception:  # noqa: BLE001 — teardown never masks the real error.
                    pass
            raise
    _wrapped.__name__ = getattr(build_fn, "__name__", "_wrapped")
    _wrapped.__doc__ = build_fn.__doc__
    return _wrapped


# --------------------------------------------------------------------------- #
# 3. CSV parsing + KPI-column selection.
# --------------------------------------------------------------------------- #
def _parse_csv(csv_path, warnings):
    """Parse the CSV -> ``(header, rows)``. ``rows`` = list of dict keyed by header
    (a missing trailing cell reads as ``""``; extra cells ignored). NEVER raises."""
    try:
        # utf-8-sig strips a leading UTF-8 BOM if present (harmless otherwise) — else the
        # first header cell becomes "﻿design_name" and every design is mislabeled.
        with open(csv_path, "r", newline="", encoding="utf-8-sig") as fh:
            data = list(csv.reader(fh))
    except (OSError, ValueError) as exc:
        warnings.append(f"csv unreadable: {exc!r}")
        return [], []
    if not data:
        warnings.append("csv empty: no header")
        return [], []
    header = [str(c) for c in data[0]]
    rows = []
    for cells in data[1:]:
        row = {col: (cells[i] if i < len(cells) else "") for i, col in enumerate(header)}
        rows.append(row)
    return header, rows


def _design_name(row, i):
    """The design's DISPLAY identity — the label under a tile and the key a layout PNG is
    matched on. ``design_name`` cell -> the ``source_file`` STEM -> an index fallback.

    The middle step is LOW. The bench QUARANTINES a row whose field basis is
    unproven by emptying every cell that would reach the ranker as a number — and a bare
    patent-number stem (``007.zmx``, this bench's own staging convention) makes
    ``design_name`` exactly such a cell, so it is emptied too. That is correct: it removes
    the row's VOTE. But this plotter then read the empty cell, labelled the design
    ``design_0``, and — because ``_map_from_list`` matches PNGs by stem — reported the
    design's perfectly valid ``007.png`` as "matches no design" and drew a placeholder
    over it. The record survived in the CSV and the manifest; the PICTURE of it did not.

    ``source_file`` is on the quarantine keep-list precisely because it is a path (a
    non-numeric string), so it survives and still identifies the design. Reading it here
    restores the LABEL and the PNG match WITHOUT restoring a rank-addressable cell: this
    function feeds tile captions and ``layout_map`` keys only, never a ranking axis (the
    ranker reads the raw CSV in ``__main__``). Never raises."""
    if not isinstance(row, dict):
        return f"design_{i}"
    name = row.get("design_name")
    if name:
        return name
    src = row.get("source_file")
    if isinstance(src, str) and src.strip():
        stem = os.path.splitext(os.path.basename(src.strip()))[0]
        if stem:
            return stem
    return f"design_{i}"


def _select_kpi_columns(header, rows, warnings):
    """Build the disposition matrix ONCE for every candidate column, then
    select the bar-per-KPI panels. Returns ``(kpi_columns, disposition)``.

    A candidate column = NOT provenance AND NOT ``*_worst_field`` (each excluded index
    helper is warned). A candidate becomes a panel iff it has >=1 ``finite`` cell;
    an all-null candidate is dropped + warned (the ``norm_efl_mm`` warning is enriched).
    """
    candidate_columns = []
    for col in header:
        if col in _PROVENANCE_COLUMNS:
            continue
        if col.endswith(_INDEX_HELPER_SUFFIX):
            warnings.append(f"column '{col}' excluded as index-helper")
            continue
        candidate_columns.append(col)

    # Classify ONCE: one classify_cell call per candidate cell, one matrix.
    disposition = {
        col: [classify_cell(row.get(col, "")) for row in rows]
        for col in candidate_columns
    }

    kpi_columns = []
    for col in candidate_columns:
        disp_list = disposition[col]
        if any(disp == "finite" for disp, _ in disp_list):
            kpi_columns.append(col)          # >=1 finite cell -> a panel (header order).
            continue
        # No finite cell -> the column is DROPPED; name the CAUSE accurately (FIX 3):
        # every cell empty ("all-null") vs non-null-but-unparseable ("no plottable values").
        if all(disp == "null" for disp, _ in disp_list):
            if col == "norm_efl_mm":
                warnings.append(
                    "column 'norm_efl_mm' all-null: no EFL panel "
                    "(native-scale run — see manifest native_efl_mm)")
            else:
                warnings.append(f"column '{col}' all-null: no panel")
        else:
            warnings.append(
                f"column '{col}' no plottable values "
                "(all cells unparseable/skipped): no panel")
    return kpi_columns, disposition


# =========================================================================== #
# 5. Plot 1 — bar-per-KPI (catalog_bars.png).
# =========================================================================== #
def _informational_figure(plt, message, warnings, warning):
    """A single-panel placeholder figure (0 designs / no KPIs / 0-design montage)."""
    warnings.append(warning)
    fig = plt.figure(figsize=(6.0, 3.0), dpi=120)
    ax = fig.add_subplot(1, 1, 1)
    ax.text(0.5, 0.5, message, ha="center", va="center",
            transform=ax.transAxes, fontsize=12, color="0.3")
    ax.axis("off")
    return fig, {}


@_figure_guarded
def _build_bar_figure(rows, header, kpi_columns, disposition, target_basis, warnings):
    """Build the bar-per-KPI figure. Returns ``(fig, per_panel_meta)``.

    ``disposition`` is the disposition matrix (built ONCE upstream) — this function NEVER
    re-calls ``classify_cell`` (an anti-drift lock). Skip-cell + absent-panel-band
    warnings are appended to ``warnings`` (mutate-in-place; the return stays ``(fig, dict)``).
    """
    plt, _imread = _import_mpl()
    n_designs = len(rows)
    design_names = [_design_name(r, i) for i, r in enumerate(rows)]

    # Short-circuit BEFORE any grid geometry (no sqrt/ceil/min on empty counts).
    if n_designs == 0:
        return _informational_figure(plt, "no designs to plot", warnings,
                                     "no designs to plot")
    if not kpi_columns:
        return _informational_figure(plt, "no numeric KPIs selected", warnings,
                                     "no numeric KPIs selected")

    n_kpi = len(kpi_columns)
    ncols = min(3, n_kpi)
    nrows = math.ceil(n_kpi / ncols)
    width = max(6.0, ncols * max(3.0, 0.45 * n_designs))
    height = max(3.0, nrows * 3.0)
    fig = plt.figure(figsize=(width, height), dpi=120)
    axes = [fig.add_subplot(nrows, ncols, i + 1) for i in range(nrows * ncols)]

    per_panel_meta = {}
    for panel_i, col in enumerate(kpi_columns):
        ax = axes[panel_i]
        disp_list = disposition[col]
        finite_vals = []
        n_na = 0
        n_skip = 0
        for slot, (disp, val) in enumerate(disp_list):
            if disp == "finite":
                # A real 0.0 renders as a Rectangle of height 0 with a drawn edge
                # (a visible baseline TICK) — assertable + unmistakable from a null's no-bar.
                ax.bar(slot, val, width=0.8, color=_BAR_COLOR,
                       edgecolor="black", linewidth=0.8, zorder=2)
                finite_vals.append(val)
            elif disp == "null":
                ax.text(slot, _PANEL_BASELINE, "n/a", ha="center", va="bottom",
                        fontsize=6, color=_NA_COLOR, zorder=3)
                n_na += 1
            else:  # skip — the ONLY place skip warnings emit (inside a SELECTED panel)
                ax.text(slot, _PANEL_BASELINE, "?", ha="center", va="bottom",
                        fontsize=8, color=_SKIP_COLOR, zorder=3)
                raw = rows[slot].get(col, "")
                warnings.append(
                    f"cell '{col}'/'{design_names[slot]}' unplottable: {raw}")
                n_skip += 1

        ax.set_title(col, fontsize=8)
        ax.set_xticks(range(n_designs))
        ax.set_xticklabels(design_names, rotation=90, fontsize=7)

        # y-limits FREEZE: set BEFORE any band is drawn.
        if finite_vals:
            data_min = min(finite_vals)
            ax.set_ylim(bottom=min(0, data_min))
        else:  # defense — a 0-finite column is dropped during selection, so this should not happen.
            ax.text(0.5, 0.5, "(no data)", ha="center", va="center",
                    transform=ax.transAxes, fontsize=8, color="0.5")
            ax.set_ylim(0, 1)

        has_band = _draw_band(ax, col, target_basis)

        per_panel_meta[col] = {
            "ax": ax, "n_bars": len(finite_vals), "n_na": n_na, "n_skip": n_skip,
            "design_slots": list(design_names), "has_band": has_band,
        }

    # Unused trailing grid cells -> off.
    for ax in axes[n_kpi:]:
        ax.axis("off")

    # A target_basis band naming a column that is NOT a rendered panel -> warn.
    if isinstance(target_basis, dict):
        for key in target_basis:
            if key not in kpi_columns:
                warnings.append(
                    f"target_basis band for '{key}' skipped: not a rendered panel")

    return fig, per_panel_meta


def _draw_band(ax, col, target_basis):
    """Draw the frozen-limit acceptance band for ``col`` (if any). Returns True iff
    a band was drawn. NEVER raises — a malformed band entry is skipped. Band min/max are
    consumed AS-IS (never float()-parsed; the frozen ``get_ylim()`` is the ONLY open-end
    value)."""
    if not isinstance(target_basis, dict):
        return False
    band = target_basis.get(col)
    if not isinstance(band, dict):
        return False
    lo, hi = ax.get_ylim()
    bmin = band.get("min")
    bmax = band.get("max")
    try:
        if bmin is not None and bmax is not None:
            ax.axhspan(bmin, bmax, alpha=0.15, color=_BAND_COLOR, zorder=0)
        elif bmin is not None:
            ax.axhspan(bmin, hi, alpha=0.15, color=_BAND_COLOR, zorder=0)
        elif bmax is not None:
            ax.axhspan(lo, bmax, alpha=0.15, color=_BAND_COLOR, zorder=0)
        else:
            return False
    except Exception:  # noqa: BLE001 — a malformed band value never crashes the panel.
        return False
    # Re-freeze the limits: a band that exceeds the frozen frame must not re-trigger
    # autoscale (the FORBIDDEN axhspan(min, 1e9) failure mode). Idempotent otherwise.
    ax.set_ylim(lo, hi)
    return True


# =========================================================================== #
# 6. Plot 2 — layout montage (catalog_montage.png).
# =========================================================================== #
def _build_layout_map(design_names, layout_pngs, warnings):
    """Resolve ``layout_pngs`` (None/dict/list/dir-str/other) into
    ``dict[design_name -> path | None]`` (one entry per design, CSV row order)."""
    if layout_pngs is None:
        return {name: None for name in design_names}
    if isinstance(layout_pngs, dict):
        return {name: layout_pngs.get(name) for name in design_names}
    if isinstance(layout_pngs, str):
        paths = sorted(glob.glob(os.path.join(layout_pngs, "*.png")))
        return _map_from_list(design_names, paths, warnings)
    if isinstance(layout_pngs, list):
        return _map_from_list(design_names, layout_pngs, warnings)
    warnings.append(f"layout_pngs unusable type {type(layout_pngs).__name__}: "
                    "all tiles are placeholders")
    return {name: None for name in design_names}


def _map_from_list(design_names, paths, warnings):
    """Match a list of PNG paths to designs by EXACT case-sensitive stem —
    ``Cooke.png`` does NOT match ``cooke``. An extra path matching no design is warned."""
    names = set(design_names)
    stem_map = {}
    for p in paths:
        if not isinstance(p, str):
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        if stem in names:
            stem_map.setdefault(stem, p)
        else:
            warnings.append(f"layout png '{p}' matches no design: ignored")
    return {name: stem_map.get(name) for name in design_names}


@_figure_guarded
def _build_montage_figure(rows, layout_map, warnings):
    """Build the layout-montage figure (one tile PER design, nothing dropped). Returns
    ``(fig, per_tile_meta)``. A missing/invalid PNG -> a labeled placeholder + a warning."""
    plt, imread = _import_mpl()
    design_names = [_design_name(r, i) for i, r in enumerate(rows)]
    n_designs = len(design_names)

    if n_designs == 0:  # short-circuit before geometry.
        warnings.append("no designs to montage")
        fig = plt.figure(figsize=(2.4, 2.4), dpi=120)
        ax = fig.add_subplot(1, 1, 1)
        ax.text(0.5, 0.5, "no designs", ha="center", va="center",
                transform=ax.transAxes, fontsize=10, color="0.3")
        ax.axis("off")
        return fig, {}

    ncols = min(5, max(1, n_designs))
    nrows = math.ceil(n_designs / ncols)
    fig = plt.figure(figsize=(ncols * 2.2, nrows * 2.4), dpi=120)
    axes = [fig.add_subplot(nrows, ncols, i + 1) for i in range(nrows * ncols)]

    per_tile_meta = {}
    for i, name in enumerate(design_names):
        ax = axes[i]
        path = layout_map.get(name)
        kind = "placeholder"
        reason = None
        # Gate: draw an image ONLY if the path passes the PNG magic-byte oracle.
        if path is not None and _is_png(path):
            try:
                img = imread(path)
                ax.imshow(img)
                ax.axis("off")
                kind = "image"
            except Exception as exc:  # noqa: BLE001 — corrupt-after-magic PNG -> placeholder
                reason = f"imread_failed: {type(exc).__name__}"
        if kind == "placeholder":
            ax.set_facecolor("0.85")
            ax.set_xticks([])
            ax.set_yticks([])
            ax.text(0.5, 0.5, "no layout", ha="center", va="center",
                    transform=ax.transAxes, fontsize=9, color="0.4")
            if reason is None:
                reason = "no_valid_png"
            warnings.append(f"design '{name}': no valid layout PNG")
        # Label EVERY tile (image or placeholder) — a labeled placeholder, never a drop.
        ax.set_title(name, fontsize=7)
        per_tile_meta[name] = {"kind": kind, "reason": reason, "label": name}

    for ax in axes[n_designs:]:
        ax.axis("off")

    return fig, per_tile_meta


# =========================================================================== #
# 8. Writers + the atomic durability gate (mirror tools/layout_render).
# =========================================================================== #
def _save_figure(fig, plt, out_dir, name, warnings, paths):
    """Save ``fig`` to ``out_dir/name`` through the temp -> ``_is_png`` gate -> ``os.replace``
    atomic path. Reaps the mkstemp temp in a finally on EVERY exit. NEVER raises."""
    tmp = None
    try:
        os.makedirs(out_dir, exist_ok=True)   # a missing dir is otherwise a silent no-op.
        fd, tmp = tempfile.mkstemp(suffix=".png", dir=out_dir)
        os.close(fd)
        fig.savefig(tmp, dpi=120, format="png", bbox_inches="tight")  # tight bounding box.
        _close_figure(plt, fig)               # close BEFORE the gate (leak-safe).
        if _is_png(tmp):
            final = os.path.join(out_dir, name)
            os.replace(tmp, final)
            tmp = None
            paths.append(final)
        else:
            warnings.append(f"save failed magic-byte gate: {name}")
    except Exception as exc:  # noqa: BLE001 — an unwritable dir / savefig fault -> warning.
        warnings.append(f"save failed: {name}: {exc!r}")
    finally:
        _close_figure(plt, fig)               # backstop (idempotent).
        if tmp and os.path.exists(tmp):        # reap the temp on EVERY exit.
            try:
                os.remove(tmp)
            except OSError:
                pass


def _close_figure(plt, fig):
    try:
        plt.close(fig)
    except Exception:  # noqa: BLE001 — teardown must never mask the caller's result.
        pass


def _write_sidecar(out_dir, warnings):
    """Write the ``plot_warnings.txt`` sidecar (one warning per line) — guarded,
    never-raise, written even when empty. NOT added to the returned PNG-paths list."""
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, _SIDECAR_NAME), "w", encoding="utf-8") as fh:
            for w in warnings:
                fh.write(str(w) + "\n")
    except (OSError, ValueError):
        pass


# =========================================================================== #
# Core + public entry point.
# =========================================================================== #
def _plot_all(csv_path, out_dir, layout_pngs, target_basis):
    """The tested core: write the PNGs + the warnings sidecar. Returns
    ``(png_paths, warnings)`` — the second element is NOT in the public signature but is
    surfaced via the sidecar. NEVER raises."""
    warnings = []
    paths = []
    try:
        try:
            os.makedirs(out_dir, exist_ok=True)   # before any write.
        except (OSError, ValueError) as exc:
            warnings.append(f"out_dir unwritable: {exc!r}")

        try:
            plt, _imread = _import_mpl()
        except BaseException as exc:  # noqa: BLE001 — broken mpl -> plot_unavailable + [].
            warnings.append(
                f"plot_unavailable: matplotlib import failed: {type(exc).__name__}: {exc}")
            _write_sidecar(out_dir, warnings)
            return [], warnings

        header, rows = _parse_csv(csv_path, warnings)
        design_names = [_design_name(r, i) for i, r in enumerate(rows)]
        kpi_columns, disposition = _select_kpi_columns(header, rows, warnings)

        # --- bars (its own guarded build+save; a bars fault never blocks the montage) --
        try:
            fig, _meta = _build_bar_figure(
                rows, header, kpi_columns, disposition, target_basis, warnings)
            _save_figure(fig, plt, out_dir, _BARS_NAME, warnings, paths)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"bars build failed: {exc!r}")

        # --- montage ------------------------------------------------------------ #
        try:
            layout_map = _build_layout_map(design_names, layout_pngs, warnings)
            fig, _tmeta = _build_montage_figure(rows, layout_map, warnings)
            _save_figure(fig, plt, out_dir, _MONTAGE_NAME, warnings, paths)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"montage build failed: {exc!r}")

    except Exception as exc:  # noqa: BLE001 — top-level never-raise net.
        warnings.append(f"plot_all failed: {exc!r}")
    _write_sidecar(out_dir, warnings)
    return paths, warnings


def plot_metrics(csv_path, out_dir, *, layout_pngs=None, target_basis=None):
    """Render the plots from a ``catalog_metrics.csv``.

    Args:
      csv_path:     path to the bench core's catalog_metrics.csv.
      out_dir:      dir to write the PNGs (``os.makedirs(exist_ok=True)`` first).
      layout_pngs:  per-design layout PNG source (None/dict/list/dir-str);
                    None -> every montage tile is a placeholder.
      target_basis: optional ``dict[kpi_column -> {"min": a?, "max": b?}]`` spec-band
                    overlay; None -> no bands.

    Returns: the list of PNG paths actually written (magic-byte-proven). NEVER raises.
    """
    try:
        return _plot_all(csv_path, out_dir, layout_pngs, target_basis)[0]
    except Exception:  # noqa: BLE001 — the public wrapper is unconditionally safe.
        return []


__all__ = [
    "plot_metrics", "classify_cell", "_plot_all",
    "_build_bar_figure", "_build_montage_figure",
    "_PROVENANCE_COLUMNS",
]
