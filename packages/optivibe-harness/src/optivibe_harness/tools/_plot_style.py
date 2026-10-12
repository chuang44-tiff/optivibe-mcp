"""PURE shared style/scale substrate for the analysis plots.

No engine, no clr, no ZOSAPI import. Everything a plot module needs to agree with its own
envelope lives here ONCE: the 1-2-5 ladder, the three scale rules, the canonical rounding of a
value table, the wave colours, the field labels. Each ``*_scale`` returns ONE dict; the figure's
axis limits, its title text and the envelope's ``scale`` block are all read from that dict.
"""
import decimal
import math
import numbers

NUM_PUPIL_SAMPLES = 41
FAN_K = 1.1
CURVATURE_K = 1.0
DISTORTION_K = 1.0
FAN_FLOOR_UM = 0.001
CURVATURE_FLOOR = 1e-6
DISTORTION_FLOOR_PCT = 0.002
#: decimals of the canonical (rounded-once) tables
ROUND_UM, ROUND_LENS, ROUND_PCT = 4, 8, 6
_TOL = 1e-9
#: the rule texts live HERE once: the scale functions and ``empty_scale`` all read these
FAN_RULE = "ladder125(1.1*max|ey,ex|)"
DISTORTION_RULE = "max(0.002, ladder125(1.0*max|dist|))"

#: wave NUMBER -> (name, hex) [measured, 12 captures]
WAVE_COLOURS = ((1, "blue", "#1f4fd8"), (2, "green", "#1a9e2c"), (3, "red", "#d62728"),
                (4, "yellow-olive", "#a8a810"), (5, "magenta", "#c42ad0"))
_SUBUNIT = {"Millimeters": ("um", "mm"), "Inches": ("Mils", "in")}


def _finite(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def ladder125(x):
    """Smallest ``{1,2,5} x 10**n >= x*(1-1e-9)`` as an EXACT decimal float; ``None`` for
    ``x <= 0`` or non-finite. The 1e-9 tolerance stops ``2.0000000000000004`` rounding up to 5."""
    if not _finite(x) or x <= 0:
        return None
    y = x * (1.0 - _TOL)
    n0 = math.floor(math.log10(y))
    for e in (n0 - 1, n0, n0 + 1, n0 + 2):
        for m in (1, 2, 5):
            cand = float("%de%d" % (m, e))  # parsed from text: 0.002, never 2 * 10.0**-3
            if math.isfinite(cand) and cand >= y:  # '2e308' parses to inf: never hand out a non-rung
                return cand
    return None  # no finite rung >= x (x within a decade of float max), or unreachable otherwise


def round_value(v, decimals):
    """Python's decimal round-half-even of one value; non-finite / missing -> ``None``.
    Accepts a builtin ``int`` / ``float``, any ``numbers.Real`` (numpy scalars) and ``decimal.Decimal``;
    ``bool`` and strings stay ``None``. (``_finite`` is deliberately NOT widened: the handlers use it to
    validate wavelength um / weight before float arithmetic.)"""
    if isinstance(v, bool) or not isinstance(v, (int, float, numbers.Real, decimal.Decimal)):
        return None
    try:
        f = float(v)
    except (ValueError, TypeError, OverflowError):
        return None
    return round(f, decimals) if math.isfinite(f) else None


def round_table(values, decimals):
    """The canonical table row: every value rounded ONCE; a missing/non-finite one is ``None``."""
    return [round_value(v, decimals) for v in values]


def max_abs(*tables):
    """Max |v| over the finite entries of any number of (nested) tables; ``None`` when none."""
    best = None
    for t in tables:
        for v in t:
            if isinstance(v, (list, tuple)):
                v = max_abs(v)
            if _finite(v) and (best is None or abs(v) > best):
                best = abs(v)
    return best


def pupil_grid():
    """The exact normalized pupil grid ``(k - 20) / 20.0`` (never accumulated)."""
    return [(k - 20) / 20.0 for k in range(NUM_PUPIL_SAMPLES)]


def _axis_value(k, data_max, floor, what):
    """``(value, floor_applied)`` for a scale block. The floor applies ONLY to ``None`` (no data at all)
    or a finite ``data_max == 0`` (spec: 'floor when the finite data are all exactly 0'). A NUMERIC
    ``data_max`` that is NaN / +-inf / negative is not data: ``ValueError`` naming the class
    (``non_finite`` / ``negative``), never the value. A finite ``data_max > 0`` whose ladder has no finite
    rung (``ladder125`` returns ``None``, or ``k * data_max`` is inf) is refused the same way (class ``overflow``).
    A real number is a builtin int / float (not bool), a ``numbers.Real`` (numpy scalar) or a
    ``decimal.Decimal``, converted with ``float()``; anything else (str, bool, object) raises
    ``ValueError`` with class ``not_a_number`` -- the value is never rendered."""
    if data_max is None:
        return floor, True
    if isinstance(data_max, bool) or not isinstance(data_max, (int, float, numbers.Real, decimal.Decimal)):
        raise ValueError("%s: data_max is not_a_number" % what)
    try:
        v = float(data_max)
    except OverflowError:  # a real number beyond float range (10**400): too large, not "not a number"
        raise ValueError("%s: data_max is overflow" % what) from None
    except (ValueError, TypeError):  # Decimal('sNaN') refuses float(): it is a NaN
        cls = "non_finite" if isinstance(data_max, decimal.Decimal) and data_max.is_nan() else "not_a_number"
        raise ValueError("%s: data_max is %s" % (what, cls)) from None
    if not math.isfinite(v):
        # a FINITE Decimal past float range saturates to inf in float() instead of raising: that is overflow
        big = isinstance(data_max, decimal.Decimal) and data_max.is_finite()
        raise ValueError("%s: data_max is %s" % (what, "overflow" if big else "non_finite"))
    if v < 0:
        raise ValueError("%s: data_max is negative" % what)
    if v == 0:
        return floor, True
    kd = k * v
    raw = ladder125(kd) if math.isfinite(kd) else None
    if raw is None:
        raise ValueError("%s: data_max is overflow" % what)
    return raw, False


def fan_scale(data_max, data_max_at=None):
    """``ladder125(1.1 * max|ey, ex|)`` in um; ``data_max`` is taken from the CANONICAL table.
    ``None`` or ``0`` -> the 0.001 um floor (``floor_applied`` True); NaN / inf / negative raise
    ``ValueError``, and so does a finite ``data_max`` whose ladder overflows (class ``overflow``;
    see ``_axis_value``)."""
    value, floored = _axis_value(FAN_K, data_max, FAN_FLOOR_UM, "fan_scale")
    return {"value": value, "unit": "um",
            "rule": FAN_RULE, "k": FAN_K, "data_max": data_max,
            "data_max_at": data_max_at, "floor_applied": floored}


def curvature_scale(data_max, lens_token, lens_label=None):
    """``ladder125(1.0 * max|S,T|)`` in lens units; displayed in the sub-unit (x1000) when the
    axis is < 0.1 (um for mm, Mils for in). cm / m keep the lens unit + ``display_unit_unmeasured``.
    An axis of exactly 0.05 carries ``unit_switch_threshold_unmeasured``. Floor / raise / overflow
    behave as in ``fan_scale`` (``_axis_value``)."""
    axis, floored = _axis_value(CURVATURE_K, data_max, CURVATURE_FLOOR, "curvature_scale")
    flags, unit, value = [], lens_token, axis
    if lens_token not in _SUBUNIT:
        flags.append("display_unit_unmeasured")
    elif axis < 0.1:
        unit, value = _SUBUNIT[lens_token][0], round(axis * 1000.0, 9)
    if axis == 0.05:
        flags.append("unit_switch_threshold_unmeasured")
    return {"value": value, "unit": unit, "value_lens_units": axis,
            "rule": "ladder125(1.0*max|S,T|)", "k": CURVATURE_K, "data_max": data_max,
            "floor_applied": floored, "flags": flags}


def distortion_scale(data_max):
    """``max(0.002, ladder125(1.0 * max|dist|))`` percent; ``None`` data -> use ``empty_scale``."""
    raw = ladder125(DISTORTION_K * data_max) if _finite(data_max) else None
    value = max(DISTORTION_FLOOR_PCT, raw) if raw is not None else DISTORTION_FLOOR_PCT
    return {"value": value, "unit": "%", "rule": DISTORTION_RULE,
            "k": DISTORTION_K, "data_max": data_max, "floor": DISTORTION_FLOOR_PCT,
            "floor_applied": raw is None or raw < DISTORTION_FLOOR_PCT}


#: per-kind ``(unit, rule, k)`` for ``empty_scale`` -- derived from the SAME constants the scale functions use
_EMPTY_KINDS = {"fan": ("um", FAN_RULE, FAN_K), "distortion": ("%", DISTORTION_RULE, DISTORTION_K)}


def empty_scale(kind, reason):
    """The scale block of a panel that is ALL ``null`` by design (the empty-panel contract): the full
    block (``unit``/``rule``/``k`` from the kind's own table, ``data_max`` null) plus ``reason``.
    An unknown ``kind`` raises ``ValueError`` -- there is no default rule to fall back on."""
    if kind not in _EMPTY_KINDS:
        raise ValueError("empty_scale: unknown kind %r (known: %s)" % (kind, ", ".join(sorted(_EMPTY_KINDS))))
    unit, rule, k = _EMPTY_KINDS[kind]
    return {"value": None, "unit": unit, "rule": rule, "k": k, "data_max": None, "floor_applied": False,
            "reason": reason}


def wave_colour(number):
    """``(name, hex, cycled)`` for a 1-based wave NUMBER; >= 6 cycles and says so."""
    n = int(number)
    cycled = n > len(WAVE_COLOURS)
    _, name, hexc = WAVE_COLOURS[(n - 1) % len(WAVE_COLOURS)]
    return name, hexc, cycled


def format_value(v):
    """A scale/axis number as drawn: shortest ``%g`` form (``500``, ``0.002``)."""
    return "%g" % v


def field_label(field_type, y, max_field, unit_label="mm"):
    """``(label, flags)``. Angle: ``OBJ: {y:.2f} (deg)`` when the max field >= 20,
    ``.4f`` when < 1, ``.2f`` + ``label_decimals_unmeasured`` between. Paraxial/RealImageHeight:
    ``IMA: {y:.4f} <unit>``. ObjectHeight: ``OBJ: {y:.4f} <unit>`` + ``label_format_unmeasured``."""
    if field_type == "Angle":
        if max_field < 1.0:
            return "OBJ: %.4f (deg)" % y, []
        if max_field >= 20.0:
            return "OBJ: %.2f (deg)" % y, []
        return "OBJ: %.2f (deg)" % y, ["label_decimals_unmeasured"]
    if field_type in ("ParaxialImageHeight", "RealImageHeight"):
        return "IMA: %.4f %s" % (y, unit_label), []
    return "OBJ: %.4f %s" % (y, unit_label), ["label_format_unmeasured"]
