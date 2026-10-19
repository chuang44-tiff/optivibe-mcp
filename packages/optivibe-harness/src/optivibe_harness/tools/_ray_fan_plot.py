"""PURE half of ``render_ray_fan``: the figure as DATA, then Agg drawing.

NO engine/clr/ZOSAPI import. The model carries the CANONICAL (rounded-once) tables the envelope
returns; a missing sample is ``None`` there and ``math.nan`` INSIDE the drawn ``ys`` so matplotlib
BREAKS the line (never filtered, never 0.0). The axis limits and the title's "Maximum Scale" text are
read from the ONE scale dict ``_plot_style.fan_scale`` returned.
"""
import math

from . import _plot_style as ps
from ._mtf_field_plot import _close

TITLE = "Transverse Ray Fan Plot"
SURFACE_LINE = "Surface: Image"
_TITLE_PAD = 14.0  # points: lifts the field title clear of the ey/ex label at the top of the centre axis


def _nan(values):
    return [math.nan if v is None else float(v) for v in values]


def _isolated(ys):
    """Indices of finite samples whose neighbours are BOTH missing (nan or off the end) -- a finite run of
    length 1 strokes no line segment, so it is drawn as a marker. Computed ONCE here (never in the drawing)."""
    n = len(ys)
    fin = [not math.isnan(y) for y in ys]
    return [k for k in range(n) if fin[k] and not (k > 0 and fin[k - 1]) and not (k + 1 < n and fin[k + 1])]


def _beyond(ys, limit):
    """Indices of finite samples outside ``+-limit`` (a fixed scale): drawn as an edge marker, ONCE here."""
    return [k for k, y in enumerate(ys) if not math.isnan(y) and abs(y) > limit]


def fan_model(fields, waves, scale, config_line, footer_lines, unit_label="um", airy_um=None):
    """The figure as data.

    ``fields``: ``[{index, label, annotation | None, tan_x, sag_x, ey: {wave: [41|None]},
    ex: {wave: [41|None]}}]`` in index order. ``waves``: ``[{index, um, colour}]``.
    ``scale``: the ``fan_scale`` dict, or a fixed one (``fixed`` True): then every finite sample beyond
    ``+-scale.value`` is listed in its line's ``beyond`` and marked at the edge. ``airy_um``: the Airy radius
    for the title, ``None`` when it could not be read. Returns a model whose ``ys`` arrays are nan-bearing."""
    fixed = bool(scale.get("fixed"))
    blocks = []
    for f in fields:
        panels = {}
        for key, xs, vals, corner in (("tan", f["tan_x"], f["ey"], ("ey", "Py")),
                                      ("sag", f["sag_x"], f["ex"], ("ex", "Px"))):
            lines = []
            for w in waves:
                ys = vals.get(w["index"])
                if ys is None:
                    continue
                drawn = _nan(ys)
                lines.append({"gid": "fan:%d:%d:%s" % (f["index"], w["index"], key[0]),
                              "wave": w["index"], "xs": [float(x) for x in xs], "ys": drawn,
                              "color": w["colour"], "isolated": _isolated(drawn),
                              "beyond": _beyond(drawn, scale["value"]) if fixed else []})
            panels[key] = {"corner": corner, "lines": lines}
        blocks.append({"index": f["index"], "label": f["label"], "annotation": f.get("annotation"),
                       "annotation_token": f.get("annotation_token"),
                       "panels": panels})
    unit = "µm" if unit_label == "um" else unit_label
    scale_line = "Maximum Scale: ± %s %s." % (ps.format_value(scale["value"]), unit)
    if fixed:
        dm = scale.get("data_max")
        scale_line = "Maximum Scale: ± %s %s (fixed; data max %s %s)." % (
            ps.format_value(scale["value"]), unit, "unread" if dm is None else ps.format_value(dm), unit)
    airy_line = ("Airy radius: %s µm (1.22 x λ x working F/#, primary wave)" % ps.format_value(airy_um)
                 if airy_um is not None else "Airy radius: unread")
    title_lines = [TITLE, scale_line, airy_line, SURFACE_LINE, config_line]
    n_isolated = sum(len(ln["isolated"]) for b in blocks for p in b["panels"].values() for ln in p["lines"])
    footer = list(footer_lines)
    if n_isolated:
        footer.append("%d isolated sample(s) drawn as markers" % n_isolated)
    n_beyond = sum(len(ln["beyond"]) for b in blocks for p in b["panels"].values() for ln in p["lines"])
    if n_beyond:
        footer.append("%d sample(s) beyond the fixed scale, marked by a triangle at the edge; the values are in "
                      "the envelope." % n_beyond)
    legend = [("%s µm" % ps.format_value(w["um"]), w["colour"]) for w in waves]
    return {"blocks": blocks, "title_lines": title_lines, "scale_value": scale["value"],
            "legend": legend, "footer_lines": footer}


def _place(n_blocks, b):
    """``(row, first_col)`` on a 4-column grid: two blocks per row, a lone last block centred."""
    row, col = divmod(b, 2)
    if n_blocks % 2 == 1 and b == n_blocks - 1:
        return row, 1
    return row, col * 2


def build_fan_figure(model):
    """``(fig, plt)``; artist ``gid`` = ``fan:<field>:<wave>:<t|s>``, ``annotation:<panel>:<reason>``,
    ``title_block``, ``footer``; one marker artist per isolated sample, gid ``isolated:<field>:<wave>:<t|s>:<k>``,
    and on a fixed scale one edge triangle per sample beyond it, gid ``beyond:<field>:<wave>:<t|s>:<k>``
    (deliberately NOT ``fan:``-prefixed: ``fan:`` gids are the one-per-line registry the consumers key on).

    Every panel draws native-style CROSSING centre axes (owner ruling): a vertical line at pupil 0 and a
    horizontal line at aberration 0 (``axvline``/``axhline``, gids ``center_axis:v`` / ``center_axis:h``), all four
    spines hidden, a light grid. The numeric tick labels sit on the panel's OUTER edges (y numbers left, x numbers
    bottom; owner ruling: "the number covered by the plot"), so no curve can cover them. The vertical-axis
    label sits at its top (``axis_label:y``), the horizontal one at its right end (``axis_label:x``); the field
    title is lifted ``_TITLE_PAD`` points clear of the ``ey``/``ex`` label."""
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    if "text.parse_math" not in matplotlib.rcParams:
        raise RuntimeError("matplotlib >= 3.6 required: text.parse_math is absent")
    blocks = model["blocks"]
    n_rows = max(1, (len(blocks) + 1) // 2)
    scale = model["scale_value"]
    with matplotlib.rc_context({"text.parse_math": False}):
        fig = plt.figure(figsize=(11.0, 2.7 * n_rows + 1.6))
        try:
            gs = fig.add_gridspec(n_rows, 4, hspace=0.55, wspace=0.28, top=1 - 1.3 / (2.7 * n_rows + 1.6),
                                  bottom=0.06)
            for b, blk in enumerate(blocks):
                row, c0 = _place(len(blocks), b)
                for off, key in ((0, "tan"), (1, "sag")):
                    ax = fig.add_subplot(gs[row, c0 + off])
                    panel = blk["panels"][key]
                    ax.set(xlim=(-1.0, 1.0), ylim=(-scale, scale))
                    for side in ("left", "bottom", "top", "right"):
                        ax.spines[side].set_visible(False)
                    ax.set_axisbelow(True)
                    ax.grid(True, color="0.85", linewidth=0.4)
                    ax.axvline(0.0, color="black", linewidth=0.8, gid="center_axis:v")
                    ax.axhline(0.0, color="black", linewidth=0.8, gid="center_axis:h")
                    ax.tick_params(labelsize=6, length=2, labelleft=True, labelbottom=True)
                    # The default ScalarFormatter prints the
                    # floor scale's y numbers as '0.00000' / '-0.00100', wide enough to run into the
                    # neighbour panel's Px/Py label (and from 5e6 um it draws a '1e6' offset). Five
                    # explicit ticks through ps.format_value -- the FCD idiom -- are compact on
                    # every ladder rung and carry no offset multiplier.
                    yt = [-scale, -scale / 2.0, 0.0, scale / 2.0, scale]
                    ax.set_yticks(yt)
                    ax.set_yticklabels([ps.format_value(v) for v in yt])
                    for ln in panel["lines"]:
                        ax.plot(ln["xs"], ln["ys"], linestyle="-", marker="None", color=ln["color"],
                                linewidth=1.0, gid=ln["gid"])
                        for k in ln["isolated"]:
                            ax.plot([ln["xs"][k]], [ln["ys"][k]], marker="o", markersize=2, linestyle="None",
                                    color=ln["color"],
                                    gid="isolated:%s:%s" % (ln["gid"][len("fan:"):], k))
                        for k in ln["beyond"]:
                            up = ln["ys"][k] > 0
                            ax.plot([ln["xs"][k]], [scale if up else -scale], marker="^" if up else "v",
                                    markersize=4, linestyle="None", color=ln["color"], clip_on=False,
                                    gid="beyond:%s:%s" % (ln["gid"][len("fan:"):], k))
                    ax.text(0.0, scale, panel["corner"][0], transform=ax.transData, fontsize=7, ha="left",
                            va="bottom", clip_on=False, gid="axis_label:y")
                    ax.text(1.0, 0.0, panel["corner"][1], transform=ax.transData, fontsize=7, ha="left",
                            va="bottom", clip_on=False, gid="axis_label:x")
                    if off == 0:
                        ax.set_title(blk["label"], loc="left", fontsize=8, pad=_TITLE_PAD)
                    if blk["annotation"]:
                        ax.text(0.5, 0.5, blk["annotation"], transform=ax.transAxes, ha="center",
                                va="center", fontsize=7, wrap=True,
                                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1.5},
                                gid="annotation:%s:%s" % (key, blk["annotation_token"]))
            fig.text(0.01, 0.995, "\n".join(model["title_lines"]), va="top", ha="left", fontsize=9,
                     gid="title_block")
            handles = [Line2D([], [], color=c, label=t) for t, c in model["legend"]]
            fig.legend(handles=handles, fontsize=7, loc="upper left", bbox_to_anchor=(1.0, 1.0))
            fig.text(0.01, 0.005, "\n".join(model["footer_lines"]), va="top", ha="left", fontsize=7,
                     gid="footer")
        except BaseException:
            _close(plt, fig)
            raise
    return fig, plt
