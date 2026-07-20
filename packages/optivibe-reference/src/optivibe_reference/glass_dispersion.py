"""glass_dispersion.py — the .agf dispersion engine (PURE math; lifted verbatim).

Lifted VERBATIM from the live probe (``scripts/probe_glass.py``): the 13 ``_n_*``
dispersion forms + the ``DISPERSION`` dispatch dict + ``index_at`` / ``abbe_vd`` /
``partial_pgf`` + ``FORMULA_NAMES`` / ``FORMULA_CD_COUNT`` + the 4 cardinal-line
µm constants. The recompute IS the provenance — the probe's recomputed Nd matched
the ``.agf``-stored Nd to <=2e-6 AND matched the live engine INDX to full double
precision (N-BK7 Pg,F engine-vs-ours delta = 0.0). See §4.

Zero I/O, zero engine dependency. ``index_at`` returns ``None`` on an
unimplemented formula or any raised math error; ``abbe_vd`` / ``partial_pgf``
return ``None`` on a ``None`` input or a zero ``nF-nC``.

Equation FORMS are written in our own notation (local authorship: NOT pasted manual
prose; the manual equation glyphs render as images and do not extract as text).
Formulas #4 (Sellmeier2) and #10 (Extended) are absent from the v1 battery but
implemented for forward-compat (§4).
"""
import math

# --- cardinal spectral lines (micrometers) --------------------------------
LINE_d = 0.5875618   # d (He yellow) -- the Nd reference line
LINE_g = 0.4358343   # g (Hg blue)
LINE_F = 0.4861327   # F (H blue)
LINE_C = 0.6562725   # C (H red)

# --- authoritative .agf NM-field dispersion-formula numbering -------------
FORMULA_NAMES = {
    1: "Schott",
    2: "Sellmeier1",
    3: "Herzberger",
    4: "Sellmeier2",
    5: "Conrady",
    6: "Sellmeier3",
    7: "Handbook1",
    8: "Handbook2",
    9: "Sellmeier4",
    10: "Extended",
    11: "Sellmeier5",
    12: "Extended2",
    13: "Extended3",
}

# How many CD coefficients each formula's n(lambda) equation consumes. The .agf CD
# line is right-padded with zeros (commonly to 10 fields) so the field COUNT on the
# line is NOT the equation's coefficient count -- this is the equation count.
FORMULA_CD_COUNT = {
    1: 6,    # Schott:     a0,a1,a2,a3,a4,a5
    2: 6,    # Sellmeier1: K1,L1,K2,L2,K3,L3
    3: 6,    # Herzberger: A,B,C,D,E,F
    4: 5,    # Sellmeier2: A,B1,L1,B2,L2
    5: 3,    # Conrady:    n0,A,B
    6: 8,    # Sellmeier3: K1,L1,K2,L2,K3,L3,K4,L4
    7: 4,    # Handbook1:  A,B,C,D
    8: 4,    # Handbook2:  A,B,C,D
    9: 5,    # Sellmeier4: A,B,C,D,E
    10: 8,   # Extended:   a0..a7
    11: 10,  # Sellmeier5: K1,L1,...,K5,L5
    12: 8,   # Extended2:  a0..a7 (last two terms use lambda^+4,+6)
    13: 9,   # Extended3:  a0..a8
}


# ==========================================================================
# DISPERSION FORMULAS  -- n(lambda), lambda in micrometers.
# ==========================================================================
def _n_schott(c, w):
    # n^2 = a0 + a1 w^2 + a2 w^-2 + a3 w^-4 + a4 w^-6 + a5 w^-8
    w2 = w * w
    n2 = (c[0] + c[1] * w2 + c[2] / w2 + c[3] / w2**2
          + c[4] / w2**3 + c[5] / w2**4)
    return math.sqrt(n2)


def _n_sellmeier1(c, w):
    # n^2 - 1 = sum_i K_i w^2 / (w^2 - L_i)   (3 terms)
    w2 = w * w
    s = sum(c[2 * i] * w2 / (w2 - c[2 * i + 1]) for i in range(3))
    return math.sqrt(1.0 + s)


def _n_herzberger(c, w):
    # n = A + B*L + C*L^2 + D w^2 + E w^4 + F w^6,  L = 1/(w^2 - 0.028)
    w2 = w * w
    L = 1.0 / (w2 - 0.028)
    return (c[0] + c[1] * L + c[2] * L * L
            + c[3] * w2 + c[4] * w2**2 + c[5] * w2**3)


def _n_sellmeier2(c, w):
    # n^2 - 1 = A + B1 w^2/(w^2 - L1^2) + B2 w^2/(w^2 - L2^2)
    # agf coeff order: A, B1, L1, B2, L2  (L stored as wavelength, squared here)
    w2 = w * w
    A, B1, L1, B2, L2 = c[0], c[1], c[2], c[3], c[4]
    s = A + B1 * w2 / (w2 - L1 * L1) + B2 * w2 / (w2 - L2 * L2)
    return math.sqrt(1.0 + s)


def _n_conrady(c, w):
    # n = n0 + A/w + B/w^3.5
    return c[0] + c[1] / w + c[2] / w**3.5


def _n_sellmeier3(c, w):
    # n^2 - 1 = sum_i K_i w^2/(w^2 - L_i)  (4 terms)
    w2 = w * w
    s = sum(c[2 * i] * w2 / (w2 - c[2 * i + 1]) for i in range(4))
    return math.sqrt(1.0 + s)


def _n_handbook1(c, w):
    # n^2 = A + B/(w^2 - C) - D w^2
    w2 = w * w
    return math.sqrt(c[0] + c[1] / (w2 - c[2]) - c[3] * w2)


def _n_handbook2(c, w):
    # n^2 = A + B w^2/(w^2 - C) - D w^2
    w2 = w * w
    return math.sqrt(c[0] + c[1] * w2 / (w2 - c[2]) - c[3] * w2)


def _n_sellmeier4(c, w):
    # n^2 = A + B w^2/(w^2 - C) + D w^2/(w^2 - E)
    w2 = w * w
    A, B, C, D, E = c[0], c[1], c[2], c[3], c[4]
    return math.sqrt(A + B * w2 / (w2 - C) + D * w2 / (w2 - E))


def _n_extended(c, w):
    # n^2 = a0 + a1 w^2 + a2 w^-2 + a3 w^-4 + a4 w^-6 + a5 w^-8
    #        + a6 w^-10 + a7 w^-12
    w2 = w * w
    return math.sqrt(c[0] + c[1] * w2 + c[2] / w2 + c[3] / w2**2
                     + c[4] / w2**3 + c[5] / w2**4 + c[6] / w2**5
                     + c[7] / w2**6)


def _n_sellmeier5(c, w):
    # n^2 - 1 = sum_i K_i w^2/(w^2 - L_i)  (5 terms)
    w2 = w * w
    s = sum(c[2 * i] * w2 / (w2 - c[2 * i + 1]) for i in range(5))
    return math.sqrt(1.0 + s)


def _n_extended2(c, w):
    # n^2 = a0 + a1 w^2 + a2 w^-2 + a3 w^-4 + a4 w^-6 + a5 w^-8
    #        + a6 w^4 + a7 w^6   (last two terms: POSITIVE powers of w)
    w2 = w * w
    return math.sqrt(c[0] + c[1] * w2 + c[2] / w2 + c[3] / w2**2
                     + c[4] / w2**3 + c[5] / w2**4
                     + c[6] * w2**2 + c[7] * w2**3)


def _n_extended3(c, w):
    # n^2 = a0 + a1 w^2 + a2 w^4 + a3 w^-2 + a4 w^-4 + a5 w^-6
    #        + a6 w^-8 + a7 w^-10 + a8 w^-12
    w2 = w * w
    return math.sqrt(c[0] + c[1] * w2 + c[2] * w2**2 + c[3] / w2
                     + c[4] / w2**2 + c[5] / w2**3 + c[6] / w2**4
                     + c[7] / w2**5 + c[8] / w2**6)


DISPERSION = {
    1: _n_schott,
    2: _n_sellmeier1,
    3: _n_herzberger,
    4: _n_sellmeier2,
    5: _n_conrady,
    6: _n_sellmeier3,
    7: _n_handbook1,
    8: _n_handbook2,
    9: _n_sellmeier4,
    10: _n_extended,
    11: _n_sellmeier5,
    12: _n_extended2,
    13: _n_extended3,
}


def index_at(formula, coeffs, w):
    """n(w) for a formula; returns None if unimplemented or it raises."""
    fn = DISPERSION.get(formula)
    if fn is None:
        return None
    try:
        return fn(coeffs, w)
    except Exception:  # noqa: BLE001
        return None


def abbe_vd(nd, nf, nc):
    if nf is None or nc is None or nd is None:
        return None
    denom = nf - nc
    if denom == 0:
        return None
    return (nd - 1.0) / denom


def partial_pgf(ng, nf, nc):
    if None in (ng, nf, nc):
        return None
    denom = nf - nc
    if denom == 0:
        return None
    return (ng - nf) / denom
