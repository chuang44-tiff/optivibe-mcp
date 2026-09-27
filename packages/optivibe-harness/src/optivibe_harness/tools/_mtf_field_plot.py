"""PURE half of ``render_mtf_vs_field``: reference validation + digest,
plot MODEL, Agg render + atomic save. NO engine/clr/ZOSAPI import. Generated text names vignetting
SURFACES, never a cause [SA-8]; provenance + engine messages pass VERBATIM. Markers: computed FILLED,
vignetted chief OPEN, reference none [CR-Q1-6][P-6] — segments between markers are NOT computed."""
import contextlib
import hashlib
import json
import math
import os
import secrets
import unicodedata

from .._io import safe_exc, safe_repr
from ._image_gate import _is_png

REFERENCE_KINDS = ("published", "synthetic", "other")
_INVISIBLE = frozenset({"Cf", "Cc", "Zs", "Zl", "Zp"})  # a text needs >= 1 char outside [P2R3-5]
MEASUREMENT_BASES = ("lens_only", "system", "unknown")
ORIENTATIONS = ("tangential", "sagittal")
_REQUIRED_KEYS = ("source", "kind", "aperture", "extraction", "spectral_weighting", "measurement_basis",
                  "x_quantity", "x_units", "frequency_units", "modulation_scale", "curves")
_UNRESOLVED_UNITS = (None, "", "unknown")  # ``_lens_units_string``'s unread value [SA3-4]
_MAX_CURVES, _MIN_POINTS, _MAX_POINTS = 12, 2, 500
_PALETTE = ("#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#8c564b")  # per frequency
_LINESTYLE = {"tangential": "-", "sagittal": "--"}
_COMPUTED_LW, _REFERENCE_LW, _REFERENCE_ALPHA = 1.8, 0.9, 0.45
_OUT_OF_RANGE_PAD = 0.02
SYNTHETIC_BANNER = "SYNTHETIC REFERENCE — not a datasheet"
REFERENCE_FOOTER = "Reference drawn for visual context only — no comparison was computed."
ALL_VIGNETTED = "all sampled chief rays vignetted at surface(s) {%s}"
ONE_VIGNETTED = "chief ray vignetted: field %s at surface %s (open marker)"
SAMPLING_WARNING = "engine warned: Sampling too low — pass a larger sample_size (512 is the largest)"
UNIT_UNRESOLVED = "lens unit could not be read — X axis unit is unknown"
_SAMPLING_TOKEN = "Sampling too low"


class ReferenceInvalid(ValueError):
    """A reference-contract breach; the handler maps it to the ``reference_invalid`` envelope."""


def _number(value, where):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferenceInvalid(f"reference.{where}: must be a number, got {safe_repr(value)}")
    try:  # a 400-digit JSON int / a hostile __float__ -> a refusal, never an escape [P2R1-6]
        number = float(value)
    except Exception:  # noqa: BLE001
        number = math.nan
    if not math.isfinite(number):
        raise ReferenceInvalid(f"reference.{where}: must be finite, got {safe_repr(value)}")
    return number


def _one_of(value, key, allowed, why=""):
    """The CANONICAL token of ``allowed`` that ``value`` equals by the BASE-SLOT compare (an unbound
    ``str.__eq__`` cannot dispatch to a caller subclass), else ``ReferenceInvalid`` [P2R2-6]."""
    tok = next((t for t in allowed if isinstance(value, str) and str.__eq__(t, value) is True), None)
    if tok is None:
        raise ReferenceInvalid(f"reference.{key}: {safe_repr(value)} not in {list(allowed)}{why}")
    return tok


def _points(points, where):
    if not isinstance(points, list) or not _MIN_POINTS <= len(points) <= _MAX_POINTS:
        raise ReferenceInvalid(f"reference.{where}.points: need {_MIN_POINTS}–{_MAX_POINTS} pairs")
    out = []
    for j, pair in enumerate(points):
        at = f"{where}.points[{j}]"
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ReferenceInvalid(f"reference.{at}: must be an [h, m] pair")
        h, m = _number(pair[0], at + ".h"), _number(pair[1], at + ".m")
        if h < 0:
            raise ReferenceInvalid(f"reference.{at}.h: image height must be >= 0, got {h!r}")
        if out and h <= out[-1][0]:
            raise ReferenceInvalid(f"reference.{at}.h: heights must be STRICTLY increasing")
        if m > 1:
            raise ReferenceInvalid(f"reference.{at}.m={m!r}: percent data? modulation must be a fraction")
        if m < 0:
            raise ReferenceInvalid(f"reference.{at}.m={m!r}: modulation must be in [0, 1]")
        out.append([h, m])
    return out


def validate_reference(ref, *, frequencies, lens_unit, frequency_units):
    """A dict CONSTRUCTED from the reference contract's keys (+ ``note``), each curve exactly ``{frequency, orientation,
    points}``, or ``ReferenceInvalid(msg)`` naming the field. Unknown keys are DROPPED — the digest covers
    what was validated and drawn, and no copy/serialiser sees caller-shaped depth [P2R1-9]. NOTHING from the
    caller's object is stored: membership fields hold the CANONICAL token, text fields ``str.__str__`` of the
    value (an exact ``str``); every text is UTF-8-encodable and capped [P2R2-5][P2R2-6]."""
    if lens_unit in _UNRESOLVED_UNITS:
        raise ReferenceInvalid(f"the design's lens unit could not be resolved ({safe_repr(lens_unit)}); "
                               "an overlay cannot be unit-checked")
    if not isinstance(ref, dict):
        raise ReferenceInvalid("reference: must be an object")
    for key in _REQUIRED_KEYS:
        if key not in ref:
            raise ReferenceInvalid(f"reference.{key}: required key missing")
    texts = (("source", 300), ("aperture", 300), ("extraction", 300), ("spectral_weighting", 300))
    out = {}
    for key, cap in texts + ((("note", 500),) if "note" in ref else ()):
        value = ref[key]  # ONE read of the caller's mapping [round 5]
        if not isinstance(value, str):  # isinstance cannot dispatch to the caller's code
            raise ReferenceInvalid(f"reference.{key}: must be a non-empty string")
        text = str.__str__(value)  # canonical FIRST: every check below runs on `text` [P2R3-5]
        if len(text) > cap:
            raise ReferenceInvalid(f"reference.{key}: longer than {cap} characters")
        try:
            str.encode(text, "utf-8")
        except UnicodeEncodeError:
            raise ReferenceInvalid(f"reference.{key}: contains a lone surrogate — not drawable text") from None
        if not any(unicodedata.category(ch) not in _INVISIBLE for ch in text):
            raise ReferenceInvalid(f"reference.{key}: contains no visible character")
        out[key] = text
    for key, allowed, why in (
            ("kind", REFERENCE_KINDS, ""), ("measurement_basis", MEASUREMENT_BASES, ""),
            ("x_quantity", ("real_image_height",), " (relative field / angle cannot be drawn)"),
            ("x_units", (lens_unit,), " (the design's lens unit; no conversion is done)"),
            ("frequency_units", (frequency_units,), " (the tool's frequency unit)"),
            ("modulation_scale", ("fraction",), " (modulation must be a 0..1 fraction)")):
        out[key] = _one_of(ref[key], key, allowed, why)
    curves = ref["curves"]
    if not isinstance(curves, list) or not 1 <= len(curves) <= _MAX_CURVES:
        raise ReferenceInvalid(f"reference.curves: need 1–{_MAX_CURVES} curves")
    requested = [float(f) for f in frequencies]
    out["curves"], seen = [], set()
    for i, curve in enumerate(curves):
        where = f"curves[{i}]"
        if not isinstance(curve, dict):
            raise ReferenceInvalid(f"reference.{where}: must be an object")
        freq = _number(curve.get("frequency"), where + ".frequency")
        if freq not in requested:
            raise ReferenceInvalid(f"reference.{where}.frequency {freq:g} not requested {requested}")
        raw = curve.get("orientation")
        orient = next((o for o in ORIENTATIONS if isinstance(raw, str) and str.__eq__(o, raw) is True), None)
        if orient is None:
            raise ReferenceInvalid(f"reference.{where}.orientation {safe_repr(raw)}: need one of {ORIENTATIONS}")
        if (freq, orient) in seen:
            raise ReferenceInvalid(f"reference.{where}: duplicate (frequency, orientation)")
        seen.add((freq, orient))
        out["curves"].append({"frequency": freq, "orientation": orient,
                              "points": _points(curve.get("points"), where)})
    return out


def reference_digest(ref):
    """sha256 over the canonical JSON of the VALIDATED ``ref`` (key order does not matter): the digest
    covers the validated known keys only, never a caller's extras [P2R1-9]."""
    return hashlib.sha256(json.dumps(ref, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _footer(fields, header, x_units, reference):
    lines = [header["curve_note"]] if header.get("curve_note") else []
    for m in header.get("missing") or ():
        lines.append(f"not drawn (missing, never 0): field {m['field_index']} @ "
                     f"{m['frequency']:g} {m['orientation']} — {m['reason']}")
    if unplaced := [f["index"] for f in fields if not _finite(f.get("image_height"))]:
        lines.append(f"unplaced (no finite chief-ray height): fields {unplaced}")
    vignetted = [(f["index"], f["vignette_surface"]) for f in fields if f.get("vignette_surface")]
    if vignetted and len(vignetted) == len(fields):
        lines.append(ALL_VIGNETTED % ", ".join("%d" % s for s in sorted({s for _, s in vignetted})))
    else:
        lines.extend(ONE_VIGNETTED % pair for pair in vignetted)
    sampling = header.get("sampling")
    if sampling:
        lines.append(f"sampling: requested {sampling.get('requested')}"
                     f"{' (default)' if sampling.get('default_applied') else ''}, "
                     f"read back {sampling.get('read_back')}")
    messages = list(header.get("messages") or ())
    if any(_SAMPLING_TOKEN in m for m in messages):
        lines.append(SAMPLING_WARNING)
    lines.extend(f"engine message: {m}" for m in messages)
    if x_units in _UNRESOLVED_UNITS:
        lines.append(UNIT_UNRESOLVED)
    if reference:
        lines.append(REFERENCE_FOOTER)
    return lines


def plot_model(points, fields, frequencies, reference, x_units, header):
    """The figure as data from the ``points``/``fields``, a validated ``reference`` (or None) and
    ``header`` (optional keys: config_evaluated, n_configs, design, frequency_units, curve_note,
    missing, messages, sampling, modulation_out_of_range)."""
    header = header or {}
    vignette = {f["index"]: f.get("vignette_surface") or 0 for f in fields}
    placed = sorted((p for p in points if _finite(p.get("image_height"))),
                    key=lambda p: p["image_height"])
    color_of = {float(f): _PALETTE[n % len(_PALETTE)] for n, f in enumerate(frequencies)}
    lines = []
    for freq in frequencies:
        for orient in ORIENTATIONS:
            xs, ys, idx, marks = [], [], [], []
            for p in placed:
                value = next((a for a in p["at"] if a["frequency"] == freq), {}).get(orient)
                xs.append(float(p["image_height"]))
                ys.append(float(value) if _finite(value) else math.nan)
                idx.append(p["field_index"])
                marks.append("open" if vignette.get(p["field_index"]) else "filled")
            lines.append({"role": "computed", "frequency": freq, "orientation": orient, "xs": xs,
                          "ys": ys, "field_indices": idx, "markers": marks, "alpha": 1.0,
                          "color": color_of[float(freq)], "linestyle": _LINESTYLE[orient],
                          "linewidth": _COMPUTED_LW})
    beyond = []
    if reference:
        for c in reference["curves"]:
            lines.append({"role": "reference", "frequency": c["frequency"], "orientation": c["orientation"],
                          "xs": [h for h, _ in c["points"]], "ys": [m for _, m in c["points"]],
                          "markers": None, "color": color_of[float(c["frequency"])],
                          "alpha": _REFERENCE_ALPHA, "linestyle": _LINESTYLE[c["orientation"]],
                          "linewidth": _REFERENCE_LW})
        h_max = max(c["points"][-1][0] for c in reference["curves"])
        beyond = [p["field_index"] for p in placed if p["image_height"] > h_max]
    y_limits = (0.0, 1.0)
    out_of_range = header.get("modulation_out_of_range") or []
    if out_of_range:  # extended ONLY when the flag is set [CR-Q1-9]; values never clipped
        vals = [v for ln in lines for v in ln["ys"] if _finite(v)]
        vals += [r["value"] for r in out_of_range if _finite(r.get("value"))]
        lo, hi = min(vals + [0.0]), max(vals + [1.0])
        y_limits = (lo - _OUT_OF_RANGE_PAD if lo < 0 else 0.0,
                    hi + _OUT_OF_RANGE_PAD if hi > 1 else 1.0)
    k, n = header.get("config_evaluated"), header.get("n_configs")
    tag = "config: unread" if k is None or n is None else (f"[config {k} of {n}]" if n > 1 else "")
    title = " ".join(t for t in ("MTF vs real image height", header.get("design"), tag) if t)
    legend = [f"computed (this design, config {k if k is not None else 'unread'})"]
    if reference:
        legend.append(f"reference: {reference['source']}, {reference['aperture']}")
    synthetic = reference is not None and reference.get("kind") == "synthetic"
    return {"lines": lines, "title": title, "x_label": f"real image height ({x_units})",
            "y_label": "modulation (0..1)", "y_limits": y_limits,
            "frequency_units": header.get("frequency_units") or f"cycles/{x_units}",
            "footer_lines": _footer(fields, header, x_units, reference),
            "legend_groups": legend, "beyond_reference_extent": beyond,
            "synthetic_banner": SYNTHETIC_BANNER if synthetic else None}


def exclusive_temp(d, mkdir=False):
    """[round 5, HIGH] A private temp FILE (or, ``mkdir=True``, DIRECTORY) in ``d``, created by ONE
    exclusive call per attempt, at most 3 attempts; returns its path. ``tempfile.mkstemp``/``mkdtemp``
    RETRY ``PermissionError`` while ``os.access(d, W_OK)`` is True — on Windows W_OK reads only the
    read-only ATTRIBUTE, not the ACL — up to ``os.TMP_MAX`` (2**31-1) times: a listable-but-ACL-denied
    folder (``C:\Windows``) hung the call with the engine seat held. Here ``FileExistsError`` (a
    foreign name, never opened for write, never unlinked) moves to the next attempt; ANY other
    ``OSError`` stops at once, re-raised naming ``d``; exhaustion raises ``FileExistsError``. The name
    ``.mtf_vs_field_tmp_<12 hex>`` has NO ``.png`` suffix: no ``.png`` name appears in a user folder
    before its bytes are gated (``savefig(format="png")`` is explicit). The fd is closed at once;
    the caller's ``finally`` owns the created path on every exit."""
    for _ in range(3):
        name = os.path.join(d, ".mtf_vs_field_tmp_" + secrets.token_hex(6))
        try:  # the name is created in ONE atomic call (O_EXCL / mkdir) — never re-derived
            if mkdir:
                os.mkdir(name)
            else:
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0))
                os.close(fd)  # closed at once; the caller's `finally` owns the created path
            return name
        except FileExistsError:
            continue
        except OSError as exc:  # PermissionError (the ACL) included: stop NOW, no retry loop
            raise OSError(f"the directory {d!r} refused a private temp: {safe_exc(exc)}") from None
    raise FileExistsError(f"3 random temp names in {d!r} already existed; nothing was created")


def _close(plt, fig):
    with contextlib.suppress(Exception):  # teardown never masks the caller's result
        if plt is not None and fig is not None:
            plt.close(fig)


def _build_figure(model):
    """(fig, plt); artist ``gid`` = ``<role>:<f>:<orientation>`` (+ ``:filled``/``:open`` markers)."""
    import matplotlib
    matplotlib.use("Agg", force=True)  # headless, BEFORE pyplot
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    if "text.parse_math" not in matplotlib.rcParams:
        raise RuntimeError("matplotlib >= 3.6 required: text.parse_math is absent, pass-through "
                           "text could not be drawn verbatim")
    with matplotlib.rc_context({"text.parse_math": False}):  # verbatim text [P2R1-10]
        fig, ax = plt.subplots(figsize=(8.0, 5.5))
        try:
            for ln in model["lines"]:
                gid = f"{ln['role']}:{ln['frequency']:g}:{ln['orientation']}"
                ax.plot(ln["xs"], ln["ys"], linestyle=ln["linestyle"], color=ln["color"],
                        linewidth=ln["linewidth"], alpha=ln["alpha"], marker="None", gid=gid)
                for kind in ("filled", "open") if ln["role"] == "computed" else ():
                    pts = [(x, y) for x, y, m in zip(ln["xs"], ln["ys"], ln["markers"])
                           if m == kind and math.isfinite(y)]
                    if pts:
                        ax.plot([p[0] for p in pts], [p[1] for p in pts], linestyle="None",
                                marker="o", color=ln["color"], gid=f"{gid}:{kind}",
                                markerfacecolor=ln["color"] if kind == "filled" else "none")
            ax.set(xlabel=model["x_label"], ylabel=model["y_label"], ylim=model["y_limits"])
            ax.set_title(model["title"], fontsize=10)
            ax.grid(True, alpha=0.3)
            colors = {ln["frequency"]: ln["color"] for ln in model["lines"] if ln["role"] == "computed"}
            handles = [Line2D([], [], color="0.4", label=g) for g in model["legend_groups"]]
            handles += [Line2D([], [], color=c, marker="o", label=f"{f:g} {model['frequency_units']}")
                        for f, c in colors.items()] + [Line2D([], [], color="k", linestyle=_LINESTYLE[o],
                                                              label=o) for o in ORIENTATIONS]
            # [P2R6-OWN-2] the legend sits OUTSIDE the axes (right of the data area, top-aligned);
            # `bbox_inches="tight"` widens the canvas to keep it — nothing is drawn over the data
            legend = ax.legend(handles=handles, fontsize=7, loc="upper left",
                               bbox_to_anchor=(1.02, 1.0), borderaxespad=0.0)
            ax.text(0.0, -0.12, "\n".join(model["footer_lines"]), transform=ax.transAxes,
                    va="top", ha="left", fontsize=7, gid="footer")
            if model["synthetic_banner"]:
                # [P2R6-OWN-2] a small corner TAG above the legend column, never across the data
                ax.text(1.02, 1.02, model["synthetic_banner"], transform=ax.transAxes, ha="left",
                        va="bottom", fontsize=8, fontweight="bold", color="#D62728",
                        gid="synthetic_banner")
            # [P2R6-OWN-7] reserve the right margin: widen the FIGURE by the legend / tag column so
            # both lie inside `fig.bbox` (nothing clipped) while the axes keep their physical size
            fig.canvas.draw()
            column = max([legend.get_window_extent().width] + [
                t.get_window_extent().width for t in ax.texts if t.get_gid() == "synthetic_banner"])
            w0 = fig.get_figwidth()
            w1 = w0 + column / fig.dpi + 0.4
            fig.set_size_inches(w1, fig.get_figheight())
            fig.subplots_adjust(left=fig.subplotpars.left * w0 / w1, right=fig.subplotpars.right * w0 / w1)
        except BaseException:
            _close(plt, fig)
            raise
    return fig, plt


def render_png(model, out_path):
    """``(path, sha256, None, None)`` | ``(None, None, fault, published)``; NEVER raises. A bounded
    ``exclusive_temp`` beside
    ``out_path`` → savefig → close → ``_is_png(tmp)`` → ``os.replace`` → ``_is_png(final)`` → FINAL-byte
    digest. [R-D] the destination is the caller's pathname: a post-publication fault discloses
    ``published`` (= ``out_path``) and NEVER unlinks it; ``published`` is ``None`` before the replace."""
    fig = plt = tmp = published = stage = None
    try:
        fig, plt = _build_figure(model)
        tmp = exclusive_temp(os.path.dirname(os.path.abspath(out_path)))  # bounded [round 5]
        fig.savefig(tmp, dpi=120, format="png", bbox_inches="tight")
        _close(plt, fig)
        if not _is_png(tmp):
            return None, None, "render_failed: the rendered temp failed the PNG magic-byte gate", None
        os.replace(tmp, out_path)
        tmp, published, stage = None, out_path, "re-gate"
        if not _is_png(out_path):
            return None, None, ("render_failed: the published file failed the PNG magic-byte re-gate; it "
                                "was LEFT at the caller's path (not verified)"), published
        stage = "digest read (re-gate PASSED)"  # the message names the STAGE [P2R3-6]
        with open(out_path, "rb") as fh:
            return out_path, hashlib.sha256(fh.read()).hexdigest(), None, None
    except Exception as exc:  # noqa: BLE001 — every fault is a render_failed tuple.
        return None, None, f"render_failed: {safe_exc(exc, repr_form=True)}" + (
            f" after publication at stage {stage}; the file was LEFT at the caller's path"
            if published else ""), published
    finally:
        _close(plt, fig)
        if tmp and os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.remove(tmp)
