"""PURE half of ``render_field_curvature_distortion``: the figure as DATA, then Agg.

NO engine/clr/ZOSAPI import. Envelope arrays arrive in LENS UNITS (shifts) and % (distortion); the ONE
place lens units become display units is ``fcd_model`` (factor 1000 exactly when the curvature scale's
``unit`` differs from ``lens_token``; never ``value / value_lens_units``, which is rounded). A missing
sample is ``None`` upstream and ``math.nan`` INSIDE the drawn arrays so matplotlib BREAKS the line
(never filtered, never 0.0). The drawn title block / footer are exactly ``model["title_lines"]`` /
``model["footer_lines"]`` (the envelope serves the same lists).
"""
import math

from . import _plot_style as ps
from ._mtf_field_plot import _close

TITLE = "Field Curvature / F-Tan(θ) Distortion"
_UNIT_NAMES = {"um": "µm"}
_REASON_TEXT = {"zero_field_range": "zero field range"}


def _nan(values, factor=1.0):
    return [math.nan if v is None else float(v) * factor for v in values]


def _fmt(v):
    return "n/a" if v is None else ps.format_value(v)


def _disp_unit(curv, lens_token, lens_label):
    return lens_label if curv["unit"] == lens_token else _UNIT_NAMES.get(curv["unit"], curv["unit"])


def _tick_fmt(v, scientific):
    if v == 0:
        return "0"
    return "%.1e" % v if scientific else ps.format_value(v)


def _wave_key(w):
    try:
        return (0, int(w))
    except (TypeError, ValueError):
        return (1, str(w))


def _title_lines(axis, title_values, scales, config_line, lens_label, disp):
    curv, dist = scales["curvature"], scales["distortion_pct"]
    tv = title_values or {}
    lines = [TITLE, "Maximum Field is %s %s" % (ps.format_value(axis["max"]), axis["unit"])]
    on_axis = tv.get("on_axis_shifts")
    if axis.get("zero_range") and on_axis:
        lines.append("Field Curvature (zero field range): on-axis shifts per wave, %s" % lens_label)
        for w in sorted(on_axis, key=_wave_key):
            lines.append("  wave %s: Sagittal = %s, Tangential = %s" % (
                w, _fmt(on_axis[w].get("sag")), _fmt(on_axis[w].get("tan"))))
    else:
        lines.append("Field Curvature Sagittal = %s, Tangential = %s %s (primary wave)" % (
            _fmt(tv.get("field_curvature_sag")), _fmt(tv.get("field_curvature_tan")), lens_label))
    if dist["value"] is None:
        why = dist.get("reason")
        lines.append("Maximum distortion: not computed: %s" % _REASON_TEXT.get(why, why))
    else:
        mx = tv.get("max_distortion_pct")
        lines.append("Maximum distortion = %s%% (primary wave)" % ("n/a" if mx is None else ps.format_value(mx)))
    lines.append("Scale: ± %s %s (curvature), %s (distortion)" % (
        ps.format_value(curv["value"]), disp,
        "± %s %%" % ps.format_value(dist["value"]) if dist["value"] is not None else "not computed"))
    lines.append(config_line)
    return lines


def fcd_model(axis, waves, scales, title_values, config_line, footer_lines, lens_token, lens_label):
    """The figure as data.

    ``axis``: ``{"quantity": "angle"|"image_height", "unit": "deg"|<lens label>, "samples": [N floats],
    "max": float, "zero_range": bool}``.
    ``waves``: ``[{"index", "um", "tan_shift": [N|None], "sag_shift": [N|None], "distortion": [N|None]}]`` in
    wave-INDEX order (colour by wave NUMBER via ``ps.wave_colour``).
    ``scales``: ``{"curvature": ps.curvature_scale(...), "distortion_pct": ps.distortion_scale(...) or
    ps.empty_scale("distortion", <reason TOKEN>)}``.
    ``title_values``: the spec ``title_values`` dict, plus ``"on_axis_shifts": {wave: {"sag", "tan"}}``
    when the field range is zero (then the field-curvature title values are ``None``).
    ``config_line``: ``"Configuration k of n"`` | ``"Configuration: unread"``.
    ``footer_lines``: the handler's conditional footer lines; the model APPENDS the unit-switch line.
    Returns ``{"panels", "title_lines", "footer_lines", "legend", "scale_values", "display_factor", ...}``."""
    curv, dist = scales["curvature"], scales["distortion_pct"]
    factor = 1000.0 if curv["unit"] != lens_token else 1.0
    ys = [float(s) for s in axis["samples"]]
    s_lines, d_lines = [], []
    for w in waves:
        hexc = ps.wave_colour(w["index"])[1]
        for tag, key, style in (("S", "sag_shift", ":"), ("T", "tan_shift", "-")):
            s_lines.append({"gid": "fcd:%s:%s" % (w["index"], tag), "ys": list(ys),
                            "xs": _nan(w[key], factor), "color": hexc, "linestyle": style})
        d_lines.append({"gid": "fcd:%s:dist" % w["index"], "ys": list(ys), "xs": _nan(w["distortion"]),
                        "color": hexc, "linestyle": "-"})
    disp = _disp_unit(curv, lens_token, lens_label)
    reason = dist.get("reason") if dist["value"] is None else None
    annotation = None if reason is None else {
        "token": reason, "text": "Distortion not computed: %s" % _REASON_TEXT.get(reason, reason)}
    footer = list(footer_lines)
    # On a LIVE panel (scale value not None) a drawn wave whose
    # block is entirely null is an invisible curve with a legend entry -- say so in the footer (no gid, no key).
    for w in waves:
        if (curv["value"] is not None and all(v is None for v in w["sag_shift"])
                and all(v is None for v in w["tan_shift"])):
            footer.append("wave %d: S/T not available (every sample is null)" % w["index"])
        if dist["value"] is not None and all(v is None for v in w["distortion"]):
            footer.append("wave %d: distortion not available (every sample is null)" % w["index"])
    if factor != 1.0:
        footer.append("curvature axis shown in %s (lens unit %s x 1000)"
                      % (disp, lens_label))
    return {"panels": {"curv": {"lines": s_lines, "scale": curv["value"], "annotation": None},
                       "dist": {"lines": d_lines, "scale": dist["value"], "annotation": annotation}},
            "title_lines": _title_lines(axis, title_values, scales, config_line, lens_label, disp),
            "footer_lines": footer,
            "legend": [("%s µm" % ps.format_value(w["um"]), ps.wave_colour(w["index"])[1]) for w in waves],
            "scale_values": {"curvature": curv["value"], "distortion_pct": dist["value"]},
            "display_factor": factor, "axis_max": axis["max"], "zero_range": bool(axis.get("zero_range")),
            "field_label": ("Field (%s)" % axis["unit"]) if axis.get("quantity") == "angle"
            else ("Height (%s)" % axis["unit"]),
            "x_labels": {"curv": disp, "dist": "Percent"}}


# The legend's style handles: drawn AND counted (header band) from this one tuple (owner-pending keep/remove).
_STYLE_HANDLES = (("Sagittal", ":"), ("Tangential", "-"))


def build_fcd_figure(model):
    """``(fig, plt)``; gids ``fcd:<w>:<S|T|dist>``, ``annotation:<panel>:<reason TOKEN>``, ``title_block``,
    ``footer``, ``axis_label:x`` / ``axis_label:y``.

    Both panels draw CROSSING centre axis LINES (axvline at x = 0, axhline at the field 0 at the bottom; all
    spines hidden) with the numeric tick labels on the panel's OUTER edges (y left, x bottom) so no curve can
    cover them (owner ruling, supersedes ticks/labels along the centre axes). The vertical-axis label
    sits ABOVE the axis top and the panel title above that (no collision)."""
    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    if "text.parse_math" not in matplotlib.rcParams:
        raise RuntimeError("matplotlib >= 3.6 required: text.parse_math is absent")
    n_t, n_f = len(model["title_lines"]), len(model["footer_lines"])
    n_head = max(n_t, len(model["legend"]) + len(_STYLE_HANDLES))      # the legend can be taller than the title block
    head_in, foot_in, panel_in = 0.3 + 0.19 * n_head + 0.3, 0.15 + 0.15 * n_f + 0.45, 3.9
    h_in = head_in + foot_in + panel_in
    ymax = float(model["axis_max"]) if model["axis_max"] else 1.0
    zero = model["zero_range"]
    with matplotlib.rc_context({"text.parse_math": False}):
        fig = plt.figure(figsize=(9.5, h_in))
        try:
            fig.subplots_adjust(left=0.06, right=0.97, bottom=foot_in / h_in, top=1 - head_in / h_in, wspace=0.16)
            for i, (key, title) in enumerate((("curv", "Field curvature (S dotted, T solid)"),
                                              ("dist", "Distortion"))):
                ax = fig.add_subplot(1, 2, i + 1)
                panel = model["panels"][key]
                lim = panel["scale"] if panel["scale"] is not None else ps.DISTORTION_FLOOR_PCT
                sci = key == "dist" and lim < 0.5
                ax.set(xlim=(-lim, lim), ylim=(0.0, ymax))
                for sp in ax.spines.values():
                    sp.set_visible(False)
                ax.axvline(0.0, color="black", linewidth=0.8, gid="axis_line:y")
                ax.axhline(0.0, color="black", linewidth=0.8, gid="axis_line:x")
                ax.set_axisbelow(True)
                ax.grid(True, color="0.85", linewidth=0.4)
                ax.tick_params(labelsize=6)
                xt = [-lim, -lim / 2.0, 0.0, lim / 2.0, lim]
                ax.set_xticks(xt)
                ax.set_xticklabels([_tick_fmt(v, sci) for v in xt])
                yt = [0.0, ymax / 2.0, ymax] if not zero else [0.0]
                ax.set_yticks(yt)
                ax.set_yticklabels([ps.format_value(v) for v in yt])
                for ln in panel["lines"]:
                    ax.plot(ln["xs"], ln["ys"], linestyle="None" if zero else ln["linestyle"],
                            marker="o" if zero else "None", markersize=3, clip_on=not zero,
                            color=ln["color"], linewidth=1.0, gid=ln["gid"])
                ax.set_title(title, loc="left", fontsize=8, pad=18)
                ax.annotate(model["field_label"], xy=(0.0, ymax), xytext=(4, 3), textcoords="offset points",
                            fontsize=7, ha="left", va="bottom", annotation_clip=False, gid="axis_label:y")
                ax.annotate(model["x_labels"][key], xy=(lim, 0.0), xytext=(-2, 3), textcoords="offset points",
                            fontsize=7, ha="right", va="bottom", annotation_clip=False, gid="axis_label:x")
                if panel["annotation"]:
                    ax.text(0.5, 0.5, panel["annotation"]["text"], transform=ax.transAxes, ha="center",
                            va="center", fontsize=7, wrap=True,
                            bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85, "pad": 1.5},
                            gid="annotation:%s:%s" % (key, panel["annotation"]["token"]))
            fig.text(0.01, 1 - 0.1 / h_in, "\n".join(model["title_lines"]), va="top", ha="left", fontsize=9,
                     gid="title_block")
            handles = [Line2D([], [], color=c, label=t) for t, c in model["legend"]]
            handles += [Line2D([], [], color="0.3", linestyle=ls, label=lab) for lab, ls in _STYLE_HANDLES]
            fig.legend(handles=handles, fontsize=7, loc="upper right", bbox_to_anchor=(0.995, 0.995),
                       frameon=False)
            fig.text(0.01, 0.1 / h_in, "\n".join(model["footer_lines"]), va="bottom", ha="left", fontsize=7,
                     gid="footer")
        except BaseException:
            _close(plt, fig)
            raise
    return fig, plt
