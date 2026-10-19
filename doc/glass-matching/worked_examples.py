"""Worked examples for doc/glass-matching/glass-matching-math.md.

Every number quoted in the document's worked examples is printed by this script, so a
reader can re-run it and check the arithmetic. It needs no optical engine: it uses only
the standard library and the glass dispersion data of the ``optivibe_reference`` package.

Run from the repository root, in any Python 3.9+ environment (the ``optivibe-reference``
environment works). The output is plain ASCII.

    POSIX shell:  PYTHONPATH=packages/optivibe-reference/src python doc/glass-matching/worked_examples.py
    PowerShell:   $env:PYTHONPATH = "packages/optivibe-reference/src"; python doc/glass-matching/worked_examples.py

Section numbers in the banners match the document's section numbers.
"""
import json

from optivibe_reference.glass_build import open_glass_catalog
from optivibe_reference.glass_dispersion import index_at

# Spectral lines, in micrometres.
G, F, D, C = 0.4358343, 0.4861327, 0.5875618, 0.6562725

CATALOG = "SCHOTT.AGF"
_conn = open_glass_catalog()


def glass(name):
    row = _conn.execute(
        "select formula, cd from glass where catalog=? and name=?", (CATALOG, name)
    ).fetchone()
    if row is None:
        raise KeyError(name)
    return name, row[0], json.loads(row[1])


def n(g, w):
    return index_at(g[1], g[2], w)


def abbe(g, short=F, mid=D, long=C):
    """Generalised Abbe number, eq. (3.1)."""
    return (n(g, mid) - 1.0) / (n(g, short) - n(g, long))


def partial(g, x, y, short=F, long=C):
    """Relative partial dispersion P_{x,y}, eq. (3.2)."""
    return (n(g, x) - n(g, y)) / (n(g, short) - n(g, long))


def disp(g, w, mid=D):
    """Dispersive function D(lambda), eq. (3.3)."""
    return (n(g, w) - n(g, mid)) / (n(g, mid) - 1.0)


def delta_pgf(g):
    """Deviation of P_{g,F} from the normal line, eq. (3.5)."""
    return partial(g, G, F) - (0.6438 - 0.001682 * abbe(g))


def banner(text):
    print("\n" + text + "\n" + "-" * len(text))


# --- Section 3: dispersion quantities -------------------------------------------------
banner("3. Dispersion quantities")
print(f"{'glass':10s} {'n_d':>8s} {'nu_d':>7s} {'P_gF':>7s} {'dP_gF':>8s} {'P_dC':>7s}")
for name in ("K7", "F2", "N-BK7", "N-FK51A", "N-KZFS11", "N-KZFS2", "N-SF6", "N-SSK5", "LF5", "N-SK16"):
    g = glass(name)
    print(
        f"{name:10s} {n(g, D):8.5f} {abbe(g):7.3f} {partial(g, G, F):7.4f} "
        f"{delta_pgf(g):+8.4f} {partial(g, D, C):7.4f}"
    )

# --- Section 5.2: thin achromatic doublet ---------------------------------------------
banner("5.2 Thin achromatic doublet, f = 100 mm, N-BK7 + F2")
ga, gb = glass("N-BK7"), glass("F2")
phi = 1.0 / 100.0
va, vb = abbe(ga), abbe(gb)
phi_a = phi * va / (va - vb)
phi_b = -phi * vb / (va - vb)
print(f"phi_a = {phi_a:+.6f} /mm (f_a = {1 / phi_a:+.2f} mm)")
print(f"phi_b = {phi_b:+.6f} /mm (f_b = {1 / phi_b:+.2f} mm)")
print(f"sum |phi| / phi = {(abs(phi_a) + abs(phi_b)) / phi:.3f}")


def doublet_focus(w):
    """Exact thin-lens focal length at wavelength w (object at infinity)."""
    return 1.0 / (phi_a * (1.0 + disp(ga, w)) + phi_b * (1.0 + disp(gb, w)))


for label, x, y in (("g-F", G, F), ("d-F", D, F), ("F-C", F, C)):
    slope = (partial(ga, x, y) - partial(gb, x, y)) / (va - vb)
    predicted = -slope / phi
    exact = doublet_focus(x) - doublet_focus(y)
    print(f"z_{label}: (Pa-Pb)/(va-vb) = {slope:+.6f}  predicted {predicted:+.4f} mm  exact thin-lens {exact:+.4f} mm")

# --- Section 5.3: three-glass system ---------------------------------------------------
banner("5.3 Three-glass thin apochromat, f = 100 mm")


def triplet(names):
    gs = [glass(x) for x in names]
    v = [abbe(g) for g in gs]
    p = [partial(g, G, F) for g in gs]
    t = v[0] * (p[1] - p[2]) + v[1] * (p[2] - p[0]) + v[2] * (p[0] - p[1])
    e = t / (v[0] - v[2])
    powers = [
        phi * v[0] * (p[1] - p[2]) / t,
        phi * v[1] * (p[2] - p[0]) / t,
        phi * v[2] * (p[0] - p[1]) / t,
    ]
    print(" / ".join(names))
    print(f"  T = {t:+.4f}   E = {e:+.5f}")
    print("  f_i = " + ", ".join(f"{1 / q:+.2f}" for q in powers) + " mm")
    print(f"  sum |phi| / phi = {sum(abs(q) for q in powers) / phi:.2f}")
    print(f"  check: sum phi/nu = {sum(q / w for q, w in zip(powers, v)):+.2e}, "
          f"sum phi P/nu = {sum(q * r / w for q, r, w in zip(powers, p, v)):+.2e}")


triplet(("N-FK51A", "N-KZFS11", "N-SF6"))
triplet(("N-BK7", "N-KZFS11", "F2"))

# --- Section 6: Petzval ------------------------------------------------------------------
banner("6. Petzval radius of thin systems, f = 100 mm")


def petzval_pair(name_a, name_b):
    a, b = glass(name_a), glass(name_b)
    wa, wb = abbe(a), abbe(b)
    qa = phi * wa / (wa - wb)
    qb = -phi * wb / (wa - wb)
    psum = qa / n(a, D) + qb / n(b, D)
    print(f"{name_a} + {name_b}: Petzval sum {psum:.6f} /mm, radius {-1 / psum:+.1f} mm, "
          f"nu_a/nu_b = {wa / wb:.3f}, n_a/n_b = {n(a, D) / n(b, D):.3f}")


g = glass("N-BK7")
print(f"N-BK7 singlet: Petzval sum {phi / n(g, D):.6f} /mm, radius {-n(g, D) / phi:+.1f} mm")
petzval_pair("N-BK7", "F2")
petzval_pair("N-SSK5", "LF5")

# --- Section 4.4: separated thin lenses, formula against a direct trace ---------------
banner("4.4 Three separated thin lenses: predicted colour against a direct trace")
g1, g2, g3 = glass("N-SK16"), glass("F2"), glass("N-SK16")
gap = 12.0
phi1 = phi3 = 1.0 / 42.0
y1, ubar = 10.0, 0.25  # marginal height at lens 1; chief-ray slope in object space


def trace(powers, y, u):
    """Paraxial trace through three thin lenses; returns heights and final slope."""
    heights = []
    for k, q in enumerate(powers):
        heights.append(y)
        u = u - y * q
        if k < 2:
            y = y + gap * u
    return heights, u


def colour_sum(phi2):
    hs, _ = trace((phi1, phi2, phi3), y1, 0.0)
    return sum(h * h * q / abbe(gl) for h, q, gl in zip(hs, (phi1, phi2, phi3), (g1, g2, g3)))


lo, hi = -0.2, -0.01  # bisection for the negative power that zeroes primary axial colour
for _ in range(80):
    mid = 0.5 * (lo + hi)
    if colour_sum(lo) * colour_sum(mid) <= 0:
        hi = mid
    else:
        lo = mid
phi2 = 0.5 * (lo + hi)
powers = (phi1, phi2, phi3)
glasses = (g1, g2, g3)

ys, u_out = trace(powers, y1, 0.0)
ybar1 = -gap * ubar / (1.0 - gap * phi1)  # chief ray passes the centre of lens 2 (the stop)
ybars, ubar_out = trace(powers, ybar1, ubar)
efl = -y1 / u_out
bfd = -ys[2] / u_out
print(f"powers: " + ", ".join(f"{q:+.6f}" for q in powers) + " /mm")
print(f"EFL = {efl:.3f} mm, back focal distance = {bfd:.3f} mm, u'_k = {u_out:+.6f}")
print("  i      y       ybar     y^2 phi/nu    y ybar phi/nu   y^2 phi P_gF/nu   phi/n")
c_l = c_t = c_s = pz = 0.0
for i, (h, hb, q, gl) in enumerate(zip(ys, ybars, powers, glasses), start=1):
    a = h * h * q / abbe(gl)
    b = h * hb * q / abbe(gl)
    s = a * partial(gl, G, F)
    z = q / n(gl, D)
    c_l, c_t, c_s, pz = c_l + a, c_t + b, c_s + s, pz + z
    print(f"  {i} {h:8.4f} {hb:8.4f} {a:+12.6f} {b:+14.6f} {s:+15.6f} {z:+10.6f}")
print(f"  sums             {c_l:+12.6f} {c_t:+14.6f} {c_s:+15.6f} {pz:+10.6f}")


def at(w):
    return tuple(q * (1.0 + disp(gl, w)) for q, gl in zip(powers, glasses))


def crossing(w):
    hs, u = trace(at(w), y1, 0.0)
    return -hs[2] / u


def chief_launch(w):
    """Chief-ray height at lens 1 re-solved at wavelength w so that it crosses the stop (lens 2) on axis."""
    return -gap * ubar / (1.0 - gap * at(w)[0])


def chief_height(w):
    hs, u = trace(at(w), chief_launch(w), ubar)
    return hs[2] + bfd * u


print(f"z_F - z_C: predicted {-c_l / u_out**2:+.5f} mm, direct trace {crossing(F) - crossing(C):+.5f} mm")
print(f"z_g - z_F: predicted {-c_s / u_out**2:+.5f} mm, direct trace {crossing(G) - crossing(F):+.5f} mm")
print(f"h_F - h_C: predicted {c_t / u_out:+.5f} mm, direct trace {chief_height(F) - chief_height(C):+.5f} mm")
print(f"image height h' = {chief_height(D):.4f} mm; Petzval radius {-1 / pz:+.1f} mm, "
      f"Petzval sag at h' = {-0.5 * chief_height(D) ** 2 * pz:+.4f} mm")
print(f"sum |phi| / phi = {sum(abs(q) for q in powers) * efl:.3f}")

# --- Section 5.4: lateral secondary colour of the same lens ----------------------------
banner("5.4 Lateral secondary colour of the three separated thin lenses")
t_i = [h * hb * q / abbe(gl) for h, hb, q, gl in zip(ys, ybars, powers, glasses)]
p_gf = [partial(gl, G, F) for gl in glasses]
p_df = [partial(gl, D, F) for gl in glasses]
print("  i     T_i      P_gF     P_dF    T_i P_gF    T_i P_dF")
for i, (t, pg, pd) in enumerate(zip(t_i, p_gf, p_df), start=1):
    print(f"  {i} {t:+9.6f} {pg:8.6f} {pd:8.6f} {t * pg + 0.0:+10.6f} {t * pd + 0.0:+10.6f}")
c_t_gf = sum(t * p for t, p in zip(t_i, p_gf))
c_t_df = sum(t * p for t, p in zip(t_i, p_df))
print(f"  sums {sum(t_i):+9.6f}                  {c_t_gf:+10.6f} {c_t_df:+10.6f}")
print(f"h_g - h_F: (5.12) {c_t_gf / u_out:+.6f} mm, direct trace (re-aimed) {chief_height(G) - chief_height(F):+.6f} mm")
print(f"h_d - h_F: (5.12) {c_t_df / u_out:+.6f} mm, direct trace (re-aimed) {chief_height(D) - chief_height(F):+.6f} mm")
print("re-aimed ybar_1 per wavelength: " + ", ".join(f"{name} {chief_launch(w):+.6f}"
                                                   for name, w in (("g", G), ("F", F), ("d", D), ("C", C))))
print("re-aimed h' per wavelength:     " + ", ".join(f"{name} {chief_height(w):.6f}"
                                                   for name, w in (("g", G), ("F", F), ("d", D), ("C", C))))
print(f"back focal distance {bfd:.6f} mm, u'_k = {u_out:+.6f}")
for eps in (0.5, 0.25, 0.1):
    def chief_scaled(w, e=eps):
        qs = tuple(q * (1.0 + e * disp(gl, w)) for q, gl in zip(powers, glasses))
        yb = -gap * ubar / (1.0 - gap * qs[0])
        hs, u = trace(qs, yb, ubar)
        return hs[2] + bfd * u

    direct = chief_scaled(G) - chief_scaled(F)
    print(f"  dispersion x {eps:4.2f}: (5.12) {eps * c_t_gf / u_out:+.6f} mm, direct {direct:+.6f} mm, "
          f"error {100.0 * (eps * c_t_gf / u_out / direct - 1.0):+.1f}%")

# --- Section 4.5: thick cemented doublet, surface sums against a direct trace -----------
banner("4.5 Thick cemented doublet N-BK7 + F2: surface sums (4.1) against a direct trace")
# (curvature, thickness to the next surface, glass after the surface or None for air)
SURFACES = (
    (1.0 / 61.47, 6.0, glass("N-BK7")),
    (-1.0 / 43.47, 2.5, glass("F2")),
    (-1.0 / 124.6, 0.0, None),
)


def index_after(gl, w):
    return 1.0 if gl is None else n(gl, w)


def surface_trace(w, y=10.0, u=0.0):
    """Paraxial trace through SURFACES at wavelength w; returns per-surface rows and final slope."""
    rows, n_before = [], 1.0
    for c, t, gl in SURFACES:
        n_after = index_after(gl, w)
        u_after = (n_before * u - y * (n_after - n_before) * c) / n_after
        rows.append((y, u, c, n_before, n_after))
        y, u, n_before = y + t * u_after, u_after, n_after
    return rows, u


def back_focus(w):
    rows, u = surface_trace(w)
    y_last = rows[-1][0]
    return -y_last / u


rows, u_k = surface_trace(D)
thick_cl = thick_cs = thick_pz = 0.0
gl_before = None
print("  j      y        A         A y D(dn/n) F-C    A y D(dn/n) g-F   Petzval term")
for j, ((y, u, c, n0, n1), (_, _, gl)) in enumerate(zip(rows, SURFACES), start=1):
    a = n0 * u + n0 * y * c

    def dn_over_n(g_, short, long):
        return 0.0 if g_ is None else (n(g_, short) - n(g_, long)) / n(g_, D)

    t_fc = a * y * (dn_over_n(gl, F, C) - dn_over_n(gl_before, F, C))
    t_gf = a * y * (dn_over_n(gl, G, F) - dn_over_n(gl_before, G, F))
    t_pz = (n1 - n0) * c / (n0 * n1)
    thick_cl, thick_cs, thick_pz = thick_cl + t_fc, thick_cs + t_gf, thick_pz + t_pz
    print(f"  {j} {y:8.4f} {a:+9.5f} {t_fc:+16.6f} {t_gf:+18.6f} {t_pz:+14.6f}")
    gl_before = gl
print(f"  sums                 {thick_cl:+16.6f} {thick_cs:+18.6f} {thick_pz:+14.6f}")
print(f"focal length {-10.0 / u_k:.3f} mm, u'_k = {u_k:+.6f}")
print(f"z_F - z_C: predicted {-thick_cl / u_k**2:+.5f} mm, direct trace {back_focus(F) - back_focus(C):+.5f} mm")
print(f"z_g - z_F: predicted {-thick_cs / u_k**2:+.5f} mm, direct trace {back_focus(G) - back_focus(F):+.5f} mm")
print(f"Petzval radius {-1.0 / thick_pz:+.1f} mm")

# (8.2) on this doublet: one glass changed, that element's power held.
banner("8.1 (8.2) on the 4.5 doublet: S_P when one glass changes at fixed element power")
for m, new in ((1, "LF5"), (1, "N-SF6"), (0, "F2")):
    (c1, t_m, old), (c2, _, _) = SURFACES[m], SURFACES[m + 1]
    n_o, n_n = n(old, D), n(glass(new), D)
    u1 = -(n_o - 1.0) * c1 / n_o  # unit-height parallel ray through the glass alone in air
    phi = -(n_o * u1 + (1.0 + t_m * u1) * (n_o - 1.0) * c2)
    d_sp = phi * (1.0 / n_n - 1.0 / n_o)
    print(f"  {old[0]} -> {new}: power {phi:+.6f} /mm, n {n_o:.7f} -> {n_n:.7f}, change {d_sp:+.7f} /mm, "
          f"S_P {thick_pz:.6f} -> {thick_pz + d_sp:.6f}, radius {-1.0 / thick_pz:+.1f} -> {-1.0 / (thick_pz + d_sp):+.1f} mm")

# --- Section 4.5 again: by medium, a finite object, an immersed image ------------------
banner("4.5 by medium, finite object, immersed image")


def run(w, y, u, n_img=1.0):
    """Trace SURFACES at wavelength w with image-space index n_img: rows (y, u, c, n, n') and the end (y, u)."""
    out, n_before = [], 1.0
    for k, (c, t, gl) in enumerate(SURFACES):
        n_after = n_img if k == len(SURFACES) - 1 else index_after(gl, w)
        u_after = (n_before * u - y * (n_after - n_before) * c) / n_after
        out.append((y, u, c, n_before, n_after))
        y, u, n_before = y + t * u_after, u_after, n_after
    return out, y, u


def d_over_n(g_, short, long):
    return 0.0 if g_ is None else (n(g_, short) - n(g_, long)) / n(g_, D)


def invariant(r):
    return r[3] * r[1] + r[3] * r[0] * r[2]


def colour_sums(rs):
    """Surface sums of (4.1): F to C and g to F."""
    cl_fc = cl_gf = 0.0
    before = None
    for r, (_, _, gl) in zip(rs, SURFACES):
        a_y = invariant(r) * r[0]
        cl_fc += a_y * (d_over_n(gl, F, C) - d_over_n(before, F, C))
        cl_gf += a_y * (d_over_n(gl, G, F) - d_over_n(before, G, F))
        before = gl
    return cl_fc, cl_gf


rows_d, _, u_d = run(D, 10.0, 0.0)
f_d = -10.0 / u_d
print("  medium     F-C (mm)    g-F (mm)    Petzval (1/mm)   power (1/mm)")
power_abs = 0.0
for m in (0, 1):
    gl, t_m = SURFACES[m][2], SURFACES[m][1]
    r1, r2 = rows_d[m], rows_d[m + 1]
    l_fc = d_over_n(gl, F, C) * (invariant(r1) * r1[0] - invariant(r2) * r2[0])
    l_gf = d_over_n(gl, G, F) * (invariant(r1) * r1[0] - invariant(r2) * r2[0])
    s_m = (r1[2] - r2[2]) * (1.0 - 1.0 / n(gl, D))
    nn = n(gl, D)
    u1 = -(nn - 1.0) * r1[2] / nn  # unit-height ray, parallel, through the glass alone in air
    y2 = 1.0 + t_m * u1
    phi_m = -(nn * u1 + y2 * (nn - 1.0) * r2[2])
    power_abs += abs(phi_m)
    print(f"  {gl[0]:7s} {l_fc:+11.6f} {l_gf:+11.6f} {s_m:+14.6f} {phi_m:+14.6f}")
print(f"  Phi_1 = {f_d * power_abs:.3f}")

# Finite object 200 mm in front at height -10 mm, stop at surface 1 (marginal height 10 mm).
d_obj, h_obj, y_stop = 200.0, -10.0, 10.0
slope = y_stop / d_obj  # marginal slope, from the axial object point to the edge of surface 1
ubar_f = -h_obj / d_obj  # chief slope, from the object point through the centre of surface 1
rows_f, y_end, u_f = run(D, y_stop, slope)
image_dist = -rows_f[-1][0] / u_f
crows_f, _, cu_f = run(D, 0.0, ubar_f)
h_img = crows_f[-1][0] + image_dist * cu_f  # chief height in the plane of the marginal focus
lag = [r[3] * cr[1] * r[0] - r[3] * r[1] * cr[0] for r, cr in zip(rows_f, crows_f)]
cl_fc_f, _ = colour_sums(rows_f)


def focus_f(w, n_img=1.0, y0=y_stop, u0=slope):
    rs, _, u_ = run(w, y0, u0, n_img)
    return -rs[-1][0] / u_


print(f"finite object: image distance {image_dist:.4f} mm, h'_d {h_img:+.5f} mm, "
      f"H = {', '.join(f'{v:.4f}' for v in lag)}")
print(f"  z_F - z_C: predicted {-cl_fc_f / u_f**2:+.5f} mm, direct trace {focus_f(F) - focus_f(C):+.5f} mm")

# Same doublet, object at infinity, image space of index 1.5.
rows_i, _, u_i = run(D, 10.0, 0.0, 1.5)
cl_fc_i, _ = colour_sums(rows_i)
s_p = sum((r[4] - r[3]) * r[2] / (r[3] * r[4]) for r in rows_i)
last_c = SURFACES[-1][0]
print(f"image index 1.5: u'_k {u_i:+.6f}, focal length {-10.0 / u_i:.3f} mm")
print(f"  z_F - z_C: predicted {-cl_fc_i / (1.5 * u_i**2):+.5f} mm, direct trace "
      f"{focus_f(F, 1.5, 10.0, 0.0) - focus_f(C, 1.5, 10.0, 0.0):+.5f} mm")
print(f"  Petzval sum {s_p:.6f} /mm, radius {-1.0 / (1.5 * s_p):+.2f} mm; "
      f"last-surface term c_3 (1 - 1/n') = {last_c * (1.0 - 1.0 / 1.5):+.6f} /mm (belongs to no element)")

# --- Section 9.1: thick-lens Petzval term against phi/n -------------------------------
banner("9.1 Thick singlet: surface Petzval sum against (true power)/n")
n_, c1_, c2_, t_ = 1.5, 1.0 / 60.0, -1.0 / 80.0, 6.0
p1_, p2_ = (n_ - 1.0) * c1_, (1.0 - n_) * c2_
print(f"surface sum {(n_ - 1.0) * (c1_ - c2_) / n_:.6f} /mm, "
      f"true power / n {(p1_ + p2_ - t_ * p1_ * p2_ / n_) / n_:.6f} /mm")

# --- Section 9.2: the secondary-colour error is second order in the dispersion ---------
banner("9.2 Three-lens example: error of the predicted z_g - z_F as the dispersion is scaled")
for eps in (1.0, 0.5, 0.25, 0.1):
    def crossing_scaled(w, e=eps):
        hs, u = trace(tuple(q * (1.0 + e * disp(gl, w)) for q, gl in zip(powers, glasses)), y1, 0.0)
        return -hs[2] / u

    direct = crossing_scaled(G) - crossing_scaled(F)
    predicted = -eps * c_s / u_out**2
    print(f"  dispersion x {eps:4.2f}: predicted {predicted:+.5f} mm, direct {direct:+.5f} mm, "
          f"error {100.0 * (predicted / direct - 1.0):+.1f}%")

# --- Section 8.1: single-glass swap at fixed element power ------------------------------
banner("8.1 Swap the glass of lens 2 at fixed power: predicted change against a direct trace")
base_sec = crossing(G) - crossing(F)
base_pri = crossing(F) - crossing(C)
needed = -c_s * abbe(g2) / (ys[1] ** 2 * phi2)
print(f"current P_gF of lens 2 = {partial(g2, G, F):.4f}; change in P_gF that would zero the "
      f"secondary sum at unchanged nu: {needed:+.4f}")
for cand in ("N-KZFS11", "N-KZFS4", "N-KZFS2", "LF5", "N-SF6"):
    gc = glass(cand)
    h, q = ys[1], phi2
    d_pri = h * h * q * (1.0 / abbe(gc) - 1.0 / abbe(g2))
    d_sec = h * h * q * (partial(gc, G, F) / abbe(gc) - partial(g2, G, F) / abbe(g2))
    d_pz = q * (1.0 / n(gc, D) - 1.0 / n(g2, D))
    glasses = (g1, gc, g3)
    new_sec, new_pri = crossing(G) - crossing(F), crossing(F) - crossing(C)
    glasses = (g1, g2, g3)
    print(f"  {cand:9s} d(z_F-z_C): predicted {-d_pri / u_out**2:+.4f} direct {new_pri - base_pri:+.4f} mm | "
          f"d(z_g-z_F): predicted {-d_sec / u_out**2:+.4f} direct {new_sec - base_sec:+.4f} mm | "
          f"d(Petzval sum) {d_pz:+.6f} /mm")

# --- Section 8.2: the partial dispersion lens 2 would need ------------------------------
banner("8.2 Partial dispersion lens 2 would need, holding its colour contribution")
c2 = ys[1] ** 2 * phi2 / abbe(g2)
print(f"c_2 = y^2 phi/nu of lens 2 = {c2:+.6f}; secondary sum now {c_s:+.6f}; "
      f"P_gF needed in lens 2 to zero it = {partial(g2, G, F) - c_s / c2:.4f}")


# --- Section 8.3: fixed-height prediction against a re-solved lens ----------------------
banner("8.3 Swap the glass of lens 2: fixed-height prediction against re-solving lens 2")


def rebalanced(gl2, start):
    """Re-solve lens 2 for zero primary axial colour with glass gl2 (secant from ``start``).

    The focal length changes, so every returned figure is scaled by the new focal length;
    scaling a thin-lens system (powers times k, gaps divided by k) leaves those products fixed.
    """
    gset = (g1, gl2, g3)

    def csum(q2):
        hs, _ = trace((phi1, q2, phi3), y1, 0.0)
        return sum(h * h * q / abbe(gl) for h, q, gl in zip(hs, (phi1, q2, phi3), gset))

    q_prev, q2 = start * 1.001, start
    for _ in range(60):
        f_prev, f_now = csum(q_prev), csum(q2)
        if abs(f_now) < 1e-15 or f_now == f_prev:
            break
        q_prev, q2 = q2, q2 - f_now * (q2 - q_prev) / (f_now - f_prev)
    qs = (phi1, q2, phi3)

    def cross(w):
        hs, u = trace(tuple(q * (1.0 + disp(gl, w)) for q, gl in zip(qs, gset)), y1, 0.0)
        return -hs[2] / u

    _, u = trace(qs, y1, 0.0)
    f_new = -y1 / u
    pz_new = sum(q / n(gl, D) for q, gl in zip(qs, gset))
    return q2, f_new, (cross(G) - cross(F)) / f_new, pz_new * f_new, sum(abs(q) for q in qs) * f_new


print("  glass       nu    P_gF | phi_2 ratio: predicted, re-solved | (z_g-z_F)/EFL: predicted, re-solved |"
      " EFL    Petzval sum x EFL   sum|phi| x EFL")
for cand in ("N-SF6", "N-SF15", "F2", "LF5", "N-KZFS11"):
    gc = glass(cand)
    ratio = abbe(gc) / abbe(g2)
    pred_sec = c_s + c2 * (partial(gc, G, F) - partial(g2, G, F))
    q2, f_new, sec_direct, pz_direct, strain = rebalanced(gc, phi2 * ratio)
    print(f"  {cand:9s} {abbe(gc):6.2f} {partial(gc, G, F):7.4f} | {ratio:6.3f} {q2 / phi2:6.3f} |"
          f" {-pred_sec / u_out**2 / efl:+.6f} {sec_direct:+.6f} |"
          f" {f_new:8.2f} {pz_direct:+8.4f} {strain:8.2f}")

# --- Section 8.3 again: all three powers re-balanced at fixed focal length and primary colour ----
banner("8.3 Swap the glass of lens 2: all three powers re-balanced, focal length and z_g - z_C held")
base_f = efl
base_pc = crossing(G) - crossing(C)  # z_g - z_C with F2 (the colour to be held)
weights = [(q * base_f) ** 2 for q in (phi1, phi2, phi3)]  # baseline powers as weights
base_ug, base_uc = trace((phi1 * (1 + disp(g1, G)), phi2 * (1 + disp(g2, G)), phi3 * (1 + disp(g3, G))), y1, 0.0)[1], \
    trace((phi1 * (1 + disp(g1, C)), phi2 * (1 + disp(g2, C)), phi3 * (1 + disp(g3, C))), y1, 0.0)[1]


def lens_at(gl2, ks):
    """Image-space (height, slope) at G, D, C for powers scaled by ks; lens 2 keeps its curvature (power scales with n - 1)."""
    q2 = phi2 * (n(gl2, D) - 1.0) / (n(g2, D) - 1.0)
    qs = (phi1 * ks[0], q2 * ks[1], phi3 * ks[2])
    out = {}
    for w in (G, D, C):
        hs, u = trace(tuple(q * (1.0 + disp(gl, w)) for q, gl in zip(qs, (g1, gl2, g3))), y1, 0.0)
        out[w] = (hs[2], u)
    return out, q2


def constraints(gl2, ks):
    """Two conditions without quotients: focal length, and z_g - z_C against its target."""
    st, _ = lens_at(gl2, ks)
    (yg, ug), (yc, uc) = st[G], st[C]
    f_cond = -base_f * st[D][1] / y1 - 1.0  # linear in the image-space slope
    c_cond = ((yc * ug - yg * uc) - base_pc * ug * uc) / (base_f * base_ug * base_uc)  # baseline slopes: constants
    return [f_cond, c_cond]


def colour_quotient(gl2, ks):
    """(z_g - z_C - target) / f in plain quotient form, used only for the feasibility scan."""
    st, _ = lens_at(gl2, ks)
    z = {w: -st[w][0] / st[w][1] for w in (G, C)}
    return (z[G] - z[C] - base_pc) / base_f


def rebalance(gl2, start=(1.0, 1.0, 1.0)):
    """Smallest weighted change of the three power scales meeting both conditions (Newton steps), or None."""
    ks = list(start)
    for _ in range(60):
        g = constraints(gl2, ks)
        jac = [[0.0] * 3 for _ in range(2)]
        for i in range(3):
            kp, km = list(ks), list(ks)
            kp[i] += 1e-6
            km[i] -= 1e-6
            gp, gm = constraints(gl2, kp), constraints(gl2, km)
            for a in range(2):
                jac[a][i] = (gp[a] - gm[a]) / 2e-6
        m = [[sum(jac[a][i] * jac[b][i] / weights[i] for i in range(3)) for b in range(2)] for a in range(2)]
        rhs = [sum(jac[a][i] * (ks[i] - 1.0) for i in range(3)) - g[a] for a in range(2)]
        det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
        if abs(det) < 1e-18:
            return None
        lam = [(rhs[0] * m[1][1] - rhs[1] * m[0][1]) / det, (m[0][0] * rhs[1] - m[1][0] * rhs[0]) / det]
        new = [1.0 + sum(jac[a][i] * lam[a] for a in range(2)) / weights[i] for i in range(3)]
        step = max(abs(a - b) for a, b in zip(new, ks))
        ks = new
        if max(abs(v) for v in constraints(gl2, ks)) < 1e-12 and step < 1e-10:
            return ks
    return None


def scan_residual(gl2, lo_k=0.05, hi_k=3.0, m=61):
    """Smallest |colour residual| / f over a (k1, k3) grid with k2 from the focal-length condition; sign change?"""
    best, pos, neg = None, False, False
    for i in range(m):
        k1 = lo_k + (hi_k - lo_k) * i / (m - 1)
        for j in range(m):
            k3 = lo_k + (hi_k - lo_k) * j / (m - 1)
            a = constraints(gl2, (k1, 0.0, k3))[0]  # the focal-length condition is linear in k2
            b = constraints(gl2, (k1, 1.0, k3))[0] - a
            res = colour_quotient(gl2, (k1, -a / b, k3))
            best = abs(res) if best is None else min(best, abs(res))
            pos |= res > 0
            neg |= res < 0
    return best, pos and neg


def wide_search(gl2, hi_k1=20.0, hi_k3=60.0, m=400):
    """Wide search: for each k1, k2 from the focal-length condition and k3 by bisection on the colour condition
    (sign changes along k3); return the root of smallest weighted change over k1, or None."""

    def colour_at(k1, k3):
        a = constraints(gl2, (k1, 0.0, k3))[0]
        b = constraints(gl2, (k1, 1.0, k3))[0] - a
        k2 = -a / b
        return constraints(gl2, (k1, k2, k3))[1], k2

    def roots(k1):
        out, prev = [], None
        for j in range(m + 1):
            k3 = 0.05 + (hi_k3 - 0.05) * j / m
            c, k2 = colour_at(k1, k3)
            if prev is not None and prev[1] * c < 0 and abs(c) < 1.0 and abs(prev[1]) < 1.0:
                lo, hi = prev[0], k3
                for _ in range(80):
                    mid = 0.5 * (lo + hi)
                    if colour_at(k1, lo)[0] * colour_at(k1, mid)[0] <= 0:
                        hi = mid
                    else:
                        lo = mid
                k3r = 0.5 * (lo + hi)
                k2r = colour_at(k1, k3r)[1]
                if k2r > 0:
                    out.append((k1, k2r, k3r))
            prev = (k3, c)
        return out

    def cost_of(ks):
        return sum(w * (k - 1.0) ** 2 for w, k in zip(weights, ks))

    best = None
    for i in range(101):  # coarse pass over k1, then a fine pass around the best point
        for ks in roots(0.05 + (hi_k1 - 0.05) * i / 100):
            if best is None or cost_of(ks) < cost_of(best):
                best = ks
    if best is not None:
        centre = best[0]
        for i in range(-100, 101):
            for ks in roots(centre + 0.2 * i / 100):
                if cost_of(ks) < cost_of(best):
                    best = ks
    return best


for cand in ("F2", "N-SF15", "N-SF6", "LF5", "N-KZFS11"):
    gc = glass(cand)
    ks = rebalance(gc)
    if ks is None:
        res, changes = scan_residual(gc)
        print(f"  {cand:9s} no solution with k1 and k3 between 0.05 and 3; smallest scan residual {res:.4f} of the "
              f"focal length (sign change on the grid: {changes})")
        wide = wide_search(gc)
        if wide is None:
            print(f"  {cand:9s} no solution found with k1 up to 20 and k3 up to 60")
        else:
            st, q2w = lens_at(gc, wide)
            zd_zg = -st[D][0] / st[D][1] + st[G][0] / st[G][1]
            power_sum = sum(abs(a) for a in (phi1 * wide[0], q2w * wide[1], phi3 * wide[2])) * base_f
            print(f"  {cand:9s} wide search (k1 to 20, k3 to 60): k = {wide[0]:.2f}, {wide[1]:.2f}, {wide[2]:.2f}"
                  f"   z_d - z_g = {zd_zg:+.4f} mm   power sum = {power_sum:.1f}")
        continue
    st, q2c = lens_at(gc, ks)
    zd_zg = -st[D][0] / st[D][1] + st[G][0] / st[G][1]
    big_phi = sum(abs(a) for a in (phi1 * ks[0], q2c * ks[1], phi3 * ks[2])) * base_f
    print(f"  {cand:9s} k = {ks[0]:.3f}, {ks[1]:.3f}, {ks[2]:.3f}   z_d - z_g = {zd_zg:+.6f} mm   Phi_1 = {big_phi:.3f}")

# --- Section 7: thin-lens spherical aberration against index -----------------------------
banner("7. Minimum thin-lens spherical aberration against index (object at infinity, Y = 1)")
print("    n     X_min   bracket at X_min   relative to n = 1.5")
ref = None
for idx in (1.5, 1.6, 1.7, 1.8, 1.9, 2.0):
    a = (idx + 2) / (idx * (idx - 1) ** 2)
    b = 4 * (idx + 1) / (idx * (idx - 1))
    c = (3 * idx + 2) / idx
    d = idx**2 / (idx - 1) ** 2
    x_min = b / (2 * a)
    bracket = a * x_min**2 - b * x_min + c + d
    ref = ref or bracket
    print(f"  {idx:.1f}   {x_min:6.3f}   {bracket:12.3f}   {bracket / ref:12.3f}")
