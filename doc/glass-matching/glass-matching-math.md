# Glass matching: the mathematics

This document sets out the first-order mathematics that the glass-matching tools use to answer three
questions about an existing lens:

1. Which elements carry the colour, the secondary colour and the field curvature?
2. What does changing the glass of one element do to those quantities?
3. What does the change cost in element power, which is a proxy for steep curves and thick elements?

Every formula is either cited to a source that is openly readable, or derived here from cited formulas
with the derivation shown. Formulas that are our own are marked **(derived here)**. Section 10 lists the
sources in AMA style and says which page or slide of each was used.

All worked numbers are printed by [`worked_examples.py`](worked_examples.py), which needs no optical
engine. Re-run it to check the arithmetic.

## 1. Conventions and notation

### 1.1 Conventions

These hold throughout. Sources differ among themselves; each place where a cited formula was written in
another convention carries a *Sign note*.

| Item | Convention |
|---|---|
| Units | Lengths in mm, powers and curvatures in 1/mm. Indices, Abbe numbers and partial dispersions are dimensionless. |
| Light direction | Left to right, along $+z$. |
| Heights | $y$ is positive above the axis. |
| Ray slope | $u$ is the paraxial slope $dy/dz$, positive when the ray rises to the right. |
| Curvature | $c = 1/r$, positive when the centre of curvature lies to the right of the surface. |
| Primes | Unprimed quantities are before a surface or element; primed quantities are after it. |
| Change across a surface | $\Delta_j(x) = x'_j - x_j$: the value after surface $j$ minus the value before it. |
| Change with wavelength | $\delta x = x_s - x_l$: the value at the short end of the waveband minus the value at the long end. For a pair of wavelengths $p$ and $q$ it is $x_p - x_q$. |
| Change of glass | $\Delta_\mathrm{sub}\,x$: the value with the new glass minus the value with the old glass. |
| Index in $\delta n / n$ | The $n$ in the denominator is the index at the reference wavelength. |
| Marginal ray | $(y, u)$: from the axial object point through the edge of the aperture stop, taken with $y > 0$ at the stop. |
| Chief ray | $(\bar y, \bar u)$: from the edge of the field through the centre of the aperture stop. The field point is taken on the side that makes the object-space slope $\bar u$ positive. |
| Surface power | $\phi = (n' - n)\,c$. |
| Thin-lens power | $\phi = (n - 1)(c_1 - c_2)$ for a thin lens of index $n$ in air. Without an argument, $\phi$ is the power at the reference wavelength. |
| Final image space | Index $n'_k$ (1 for an image in air) and marginal slope $u'_k$, where $k$ is the last surface. |
| Image plane | The paraxial focus of the marginal ray at the reference wavelength. |
| Reference wavelength | The system's primary wavelength, except when the system has three or more wavelengths and the primary is the shortest or the longest of them: then the reference is the interior wavelength nearest the centre of the band, $(\lambda_\mathrm{shortest} + \lambda_\mathrm{longest})/2$, and the shorter wins a tie. The library returns `reference_replaced: true` in that case, and `explain_glass_swaps` raises the flag of the same name. |
| Wavelength labels | $s$ (short), $0$ (reference) and $l$ (long) bound the design waveband; $p$ and $q$ are any two wavelengths. The classical visible choice is $s = \mathrm{F}$ (486.13 nm), $0 = \mathrm{d}$ (587.56 nm), $l = \mathrm{C}$ (656.27 nm), with $\mathrm{g}$ = 435.83 nm for the partial dispersion. |
| Focus position | $z_\lambda$ is the paraxial image position at wavelength $\lambda$, so $z_p - z_q > 0$ means wavelength $p$ focuses farther from the lens than $q$. |

### 1.2 Notation

| Symbol | Meaning | Unit |
|---|---|---|
| $y$, $\bar y$ | marginal-ray and chief-ray height at a surface or element | mm |
| $u$, $\bar u$ | marginal-ray and chief-ray slope | 1 |
| $n$, $\nu$ | refractive index; Abbe number ($\nu_\mathrm{d}$ when taken on the F, d and C lines) | 1 |
| $c$, $r$, $t$ | curvature; radius; axial distance to the next surface | 1/mm; mm; mm |
| $\phi$, $\phi_i$ | power of a surface, a thin lens or the whole system; power of element $i$ | 1/mm |
| $f$, $f_\mathrm{a}$ | system focal length; focal length of element a | mm |
| $P_{p,q}$ | relative partial dispersion for wavelengths $p$ and $q$; written $P_i$ for element $i$ once the pair is fixed | 1 |
| $\Delta P_{\mathrm{g,F}}$ | deviation of $P_{\mathrm{g,F}}$ from the normal line (Schott's name) | 1 |
| $D(\lambda)$ | dispersion function, equation (3.3) | 1 |
| $A$, $\bar A$ | refraction invariants of the marginal and chief rays | 1 |
| $H$ | Lagrange invariant | mm |
| $C_L$, $C_T$ | axial and lateral colour sums across the waveband | mm |
| $C_L^{p,q}$, $C_T^{p,q}$ | axial and lateral colour sums for the wavelength pair $p$, $q$ | mm |
| $L_i$, $T_i$ | axial and lateral colour contributions of element $i$ | mm |
| $h'$ | chief-ray height in the image plane | mm |
| $\Theta$, $E$ | three-glass determinant, equation (5.10); "sag" of the glass triangle | 1 |
| $\Phi_1$ | sum of absolute element powers divided by the system power | 1 |
| $S_\mathrm{P}$, $R_\mathrm{P}$ | Petzval sum; radius of the Petzval surface | 1/mm; mm |
| $S_\mathrm{I}$, $W_{040}$ | Seidel sum for spherical aberration; its wavefront coefficient | mm |
| $X$, $Y$, $m$ | shape factor; conjugate factor; magnification | 1 |
| $Q_1 \dots Q_4$ | thin-lens spherical aberration coefficients, equation (7.2) | 1 |
| $i$, $j$, $k$ | element index; surface index; last surface | |
| a, b, c | glass labels for lenses in contact (sections 5 and 6); upright, not the curvature $c$ | |
| 1, 2, 3 | lens labels for the separated lens of sections 4.4 and 8 | |

## 2. Paraxial ray trace

Refraction at a surface and transfer to the next surface are<sup>1</sup>

$$
n'u' = n\,u - y\,\phi, \qquad y_{j+1} = y_j + u'_j\,t'_j \tag{2.1}
$$

where $t'_j$ is the axial distance from surface $j$ to surface $j+1$. A thin lens in air is traced with
the same two equations, with $n = n' = 1$ and $\phi$ the lens power. The image lies where the marginal
ray meets the axis, at a distance $-y_k/u'_k$ after the last surface.<sup>1</sup>

Two quantities are built from the trace.<sup>2</sup> The refraction invariants of the marginal and chief
rays at a surface are

$$
A = n\,u + n\,y\,c, \qquad \bar A = n\,\bar u + n\,\bar y\,c \tag{2.2}
$$

that is, the index times the paraxial angle of incidence of each ray. The Lagrange invariant is

$$
H = n\,\bar u\,y - n\,u\,\bar y = \bar A\,y - A\,\bar y \tag{2.3}
$$

Sasián prints the Lagrange invariant as the Cyrillic letter Ж; this document writes $H$. It has the same
value at every surface, and the same construction applied to any two paraxial rays is likewise constant
through the system. Section 4.3 uses that property.

For a system in air with the object at infinity the focal length is $f = -y_1/u'_k$, where $y_1$ is the
marginal ray height at the first surface.

## 3. Dispersion quantities

### 3.1 Abbe number and partial dispersion for any waveband

For the classical visible lines the Abbe number is
$\nu_\mathrm{d} = (n_\mathrm{d} - 1)/(n_\mathrm{F} - n_\mathrm{C})$ and the relative partial dispersion
for two wavelengths $p$ and $q$ is $P_{p,q} = (n_p - n_q)/(n_\mathrm{F} - n_\mathrm{C})$.<sup>3</sup>

For a design waveband bounded by wavelengths $s$ and $l$ with reference wavelength $0$, we use the same
definitions with the band's own wavelengths **(our generalisation)**:

$$
\nu = \frac{n_0 - 1}{n_s - n_l} \tag{3.1}
$$

$$
P_{p,q} = \frac{n_p - n_q}{n_s - n_l} \tag{3.2}
$$

Sources use different partial dispersions. Schott's technical note and Sasián's notes use
$P_{\mathrm{g,F}}$; Greivenkamp's notes use $P_{\mathrm{d,C}}$.<sup>3-5</sup> A value of $P$ means
nothing without its two wavelengths, so the tools always state them.

### 3.2 The dispersion function

A compact way to carry the whole dispersion curve of a glass is the quantity

$$
D(\lambda) = \frac{n(\lambda) - n_0}{n_0 - 1} \tag{3.3}
$$

introduced by Albuquerque et al, who write the index as $N$ and call $D$ the dispersive
power.<sup>6</sup> This document calls it the dispersion function, because "dispersive power" is also a
common name for $1/\nu$. The power of a thin lens at wavelength $\lambda$ is then
$\phi(\lambda) = \phi\,[1 + D(\lambda)]$, exactly, because thin-lens power is proportional to
$n - 1$.<sup>6</sup> The two descriptions are linked by **(derived here)**

$$
D(\lambda_p) - D(\lambda_q) = \frac{P_{p,q}}{\nu}, \qquad D(\lambda_s) - D(\lambda_l) = \frac{1}{\nu} \tag{3.4}
$$

### 3.3 The normal line and the deviation from it

Most glasses lie close to a straight line on a plot of $P_{p,q}$ against $\nu_\mathrm{d}$. Schott defines
this normal line through its glasses K7 and F2, and defines the deviation $\Delta P_{p,q}$ of a glass as
its vertical distance from the line. For the g, F partial<sup>3</sup>

$$
\Delta P_{\mathrm{g,F}} = P_{\mathrm{g,F}} - \left(0.6438 - 0.001682\,\nu_\mathrm{d}\right) \tag{3.5}
$$

The same note gives the lines for four other pairs of spectral lines: C and t, C and s, F and e, and i
and g.<sup>3</sup>

Catalogue values of $\Delta P$ cannot be mixed between manufacturers, because each maker draws its normal
line through different reference glasses: Schott through K7 and F2,<sup>3</sup> HOYA through C7 and
F2,<sup>7</sup> and OHARA through NSL7 and PBM2.<sup>8</sup> The tools therefore compute $P$ and $\Delta P$
from each glass's dispersion formula against one stated line. A catalogue's own $\Delta P$ is served
beside them (`delta_pg_f`, read verbatim from the `.agf`) for reference, and no computation uses it.

### 3.4 Worked example

Computed from the catalogue dispersion formulas with equations (3.1), (3.2) and (3.5):

| Glass (Schott) | $n_\mathrm{d}$ | $\nu_\mathrm{d}$ | $P_{\mathrm{g,F}}$ | $\Delta P_{\mathrm{g,F}}$ | $P_{\mathrm{d,C}}$ |
|---|---|---|---|---|---|
| K7 | 1.51112 | 60.41 | 0.5422 | +0.0000 | 0.3049 |
| F2 | 1.62004 | 36.37 | 0.5828 | +0.0002 | 0.2938 |
| N-BK7 | 1.51680 | 64.17 | 0.5349 | −0.0009 | 0.3076 |
| N-SK16 | 1.62041 | 60.32 | 0.5412 | −0.0011 | 0.3051 |
| N-SSK5 | 1.65844 | 50.88 | 0.5575 | −0.0007 | 0.3003 |
| LF5 | 1.58144 | 40.85 | 0.5748 | −0.0003 | 0.2959 |
| N-FK51A | 1.48656 | 84.47 | 0.5359 | +0.0342 | 0.3062 |
| N-KZFS11 | 1.63775 | 42.41 | 0.5605 | −0.0120 | 0.3000 |
| N-KZFS2 | 1.55836 | 54.01 | 0.5419 | −0.0111 | 0.3061 |
| N-SF6 | 1.80518 | 25.36 | 0.6158 | +0.0146 | 0.2867 |

K7 comes out at 0.0000 and F2 at +0.0002. The line is defined through these two glasses, so this checks
the constants of (3.5) against the catalogue data.

## 4. Colour contributions

### 4.1 Per surface

Let $\delta n = n_s - n_l$ be the index difference of a medium across the waveband ($\delta n = 0$ for
air). The sums for axial and lateral colour over the surfaces $j$ are<sup>9</sup>

$$
C_L = \sum_j A_j\,y_j\,\Delta_j\!\left(\frac{\delta n}{n}\right), \qquad
C_T = \sum_j \bar A_j\,y_j\,\Delta_j\!\left(\frac{\delta n}{n}\right) \tag{4.1}
$$

Sasián also gives them as wavefront coefficients, $\delta_\lambda W_{020} = \tfrac12 C_L$ and
$\delta_\lambda W_{111} = C_T$.<sup>9</sup> This document works with $C_L$ and $C_T$, converts them to
lengths in section 4.3, and does not use the wavefront coefficients or their sign convention further.

A stop shift leaves the axial sum unchanged and changes the lateral one in proportion to it:
$\Delta_\mathrm{stop}(\delta_\lambda W_{111}) = 2\,(\Delta_\mathrm{stop}\bar y / y)\,\delta_\lambda W_{020}$,
where $\Delta_\mathrm{stop}$ is the change caused by moving the stop.<sup>2</sup> An aspheric cap on a
surface changes neither sum.<sup>9</sup>

### 4.2 Per thin element

For thin lenses in air, each with power $\phi_i$, Abbe number $\nu_i$ and ray heights $y_i$ and
$\bar y_i$, the sums reduce to<sup>9</sup>

$$
C_L = \sum_i \frac{y_i^2\,\phi_i}{\nu_i}, \qquad C_T = \sum_i \frac{y_i\,\bar y_i\,\phi_i}{\nu_i} \tag{4.2}
$$

We write $L_i = y_i^2\phi_i/\nu_i$ for the axial colour contribution of element $i$ and
$T_i = y_i\bar y_i\phi_i/\nu_i$ for its lateral contribution. These two columns are what the tools print
to show which elements carry the colour:

- axial colour is carried by strong elements where the marginal ray is high ($y_i^2$);
- lateral colour is carried by strong elements where both rays are high ($y_i\bar y_i$), and elements on
  opposite sides of the stop contribute with opposite signs, because $\bar y$ changes sign there.

A thick element or a cemented group is handled by summing the surface terms of (4.1) over its surfaces.

Within a cemented group the interface term of (4.1) belongs to two glasses. Regrouping (4.1) by medium
**(derived here)**, the glass $m$ between surfaces $j$ and $j+1$ carries

$$
L_m = \frac{\delta n_m}{n_m}\left(A_j\,y_j - A_{j+1}\,y_{j+1}\right), \qquad
T_m = \frac{\delta n_m}{n_m}\left(\bar A_j\,y_j - \bar A_{j+1}\,y_{j+1}\right) \tag{4.2a}
$$

which sums to (4.1) over the media and reduces to (4.2) for a thin lens in air. Air carries no term.

### 4.3 Conversion to focus and image shifts (derived here)

The lecture notes give the sums as wavefront coefficients and give lengths only for a single thin lens.
The general conversion follows from the invariant of section 2.

*Step 1.* Differentiate the refraction equation (2.1) with respect to wavelength, holding the incoming
ray fixed. With $\phi = (n'-n)\,c$ this gives the change of slope produced by one surface:

$$
n'\,\delta u' = -A\,\Delta_j\!\left(\frac{\delta n}{n}\right)
$$

For a thin lens in air the same step gives $\delta u' = -y\,\delta\phi$, with
$\delta\phi = \phi/\nu$ across the waveband by (3.4).

*Step 2.* The perturbed ray differs from the marginal ray by $(\delta y, \delta u)$. The quantity
$n\,(\delta u\,y - u\,\delta y)$ formed from these two rays is constant through the rest of the system,
as noted under (2.3). Just after the surface, $\delta y = 0$, so it equals
$-A\,y\,\Delta_j(\delta n/n)$. At the image plane the marginal ray has $y = 0$, so it equals
$-n'_k\,u'_k\,\delta y'$. Equating, and summing over surfaces, the transverse displacement of the marginal
ray at the image plane is $\delta y' = C_L/(n'_k u'_k)$.

*Step 3.* The displaced ray meets the axis at $\delta z = -\delta y'/u'_k$. For lateral colour the
perturbed ray is the chief ray, and the same quantity is formed between the perturbed chief ray and the
marginal ray. Hence

$$
z_s - z_l = -\frac{C_L}{n'_k\left(u'_k\right)^2} \tag{4.3}
$$

$$
h'_s - h'_l = \frac{C_T}{n'_k\,u'_k} \tag{4.4}
$$

where $h'$ is the chief-ray height in the image plane.

*Check against the sources.* For one thin lens with the object at infinity, $u'_k = -y/f$ and
$C_L = y^2/(f\nu)$, so (4.3) gives $z_s - z_l = -f/\nu$: blue focuses short by $f/\nu$.

*Sign note.* Greivenkamp<sup>5</sup> writes $\delta f/f = 1/\nu$ as a positive magnitude; here
$z_s - z_l = -f/\nu$ is signed. The two agree in magnitude.

### 4.4 Worked example: three separated thin lenses

The object is at infinity. Lenses 1 and 3 are N-SK16 with $f = +42$ mm; lens 2 is F2, placed 12 mm from
each, with its power solved so that $C_L = 0$. Glass data are in the table of section 3.4, and $P$ means
$P_{\mathrm{g,F}}$. The inputs for the two rays are the marginal height at lens 1, $y_1 = 10$ mm, and the
object-space chief-ray slope, $\bar u = 0.25$. The stop is at lens 2, so the chief-ray height at lens 1
is solved from $\bar y_2 = 0$, which gives $\bar y_1 = -4.2$ mm. The lens has focal length 104.85 mm.

| Lens | $y_i$ (mm) | $\bar y_i$ (mm) | $L_i$ (mm) | $T_i$ (mm) | $L_i P_i$ (mm) | $\phi_i/n_i$ (1/mm) |
|---|---|---|---|---|---|---|
| 1 | 10.000 | −4.200 | +0.039470 | −0.016577 | +0.021361 | +0.014694 |
| 2 | 7.143 | 0.000 | −0.067304 | 0.000000 | −0.039224 | −0.029612 |
| 3 | 8.398 | +4.200 | +0.027835 | +0.013921 | +0.015064 | +0.014694 |
| Sum | | | 0.000000 | −0.002656 | −0.002800 | −0.000225 |

The predictions are compared with a direct trace of the same three thin lenses at each wavelength, using
$\phi(\lambda) = \phi\,[1 + D(\lambda)]$, with the chief-ray height at lens 1 re-solved from
$\bar y_2 = 0$ at that wavelength. The third row uses equation (5.2) of section 5.1.

| Quantity | Formula | Predicted | Direct trace |
|---|---|---|---|
| $z_\mathrm{F} - z_\mathrm{C}$ | (4.3) | 0.0000 mm | −0.0187 mm |
| $h'_\mathrm{F} - h'_\mathrm{C}$ | (4.4) | +0.0279 mm | +0.0279 mm |
| $z_\mathrm{g} - z_\mathrm{F}$ | (5.2) | +0.308 mm | +0.256 mm |

The formulas are first-order in the index change. The lateral colour agrees to 0.3%. The axial colour
that the sum sets to zero is 0.019 mm in the direct trace, against individual contributions of about
3 to 7 mm, so the residual is a quarter of a percent of the terms being balanced. The secondary colour
is predicted 20% high. Section 9.2 discusses why.

### 4.5 Worked example: a thick cemented doublet

This checks the surface form (4.1) on real surfaces. The doublet is N-BK7 and F2 with radii +61.47,
−43.47 and −124.6 mm and thicknesses 6.0 and 2.5 mm, traced with $y_1 = 10$ mm and the object at
infinity. Its focal length is 91.95 mm.

| Surface | $y_j$ (mm) | $A_j$ | Term of $C_L$, F to C (mm) | Term of $C_L^{\mathrm{g,F}}$ (mm) | Petzval term (1/mm) |
|---|---|---|---|---|---|
| 1 | 10.000 | +0.16268 | +0.008638 | +0.004621 | +0.005543 |
| 2 | 9.667 | −0.42140 | −0.021243 | −0.013416 | −0.000967 |
| 3 | 9.573 | −0.18558 | +0.018698 | +0.010897 | +0.003072 |
| Sum | | | +0.006093 | +0.002102 | +0.007648 |

| Quantity | Formula | Predicted | Direct trace |
|---|---|---|---|
| $z_\mathrm{F} - z_\mathrm{C}$ | (4.3) | −0.5152 mm | −0.5145 mm |
| $z_\mathrm{g} - z_\mathrm{F}$ | (5.2) | −0.1777 mm | −0.1767 mm |

With these radii the doublet is not fully achromatic, which makes it a fair test: the surface sums
predict a half-millimetre primary colour to 0.1% and the g to F shift to 0.6%. The Petzval radius from
(6.2) is −130.8 mm.

By medium (4.2a, 6.1a) the N-BK7 carries +0.030270 mm of the F to C sum and the F2 −0.024177 mm
(g to F: +0.016192 and −0.014090 mm); their Petzval terms are +0.013381 and −0.005733 per mm; traced in
isolation in air their powers are +0.019901 and −0.009178 per mm, so $\Phi_1 = 2.67$. With the object
200 mm in front at height −10 mm and the stop at surface 1 (marginal height 10 mm), the image lies
165.099 mm behind the last surface at height +8.382 mm, and $z_\mathrm{F} - z_\mathrm{C}$ is −1.7447 mm
predicted, −1.7404 mm traced. With the object back at infinity and an image space of index 1.5 the same
sums give −1.8473 mm predicted, −1.8435 mm traced, and $R_\mathrm{P} = -134.1$ mm; the last surface's term
$c_3(1 - 1/n'_k) = -0.002675$ per mm then belongs to no element.

## 5. Secondary colour

Secondary colour (also called secondary spectrum) is what is left after primary colour is corrected. An
achromat brings two wavelengths, usually F and C, to a common focus, and a wavelength between them,
usually d or e, still focuses somewhere else. Greivenkamp calls this residual "the secondary chromatic
aberration or secondary color of the doublet", and notes that the d focus is not necessarily the
largest shift from the common F and C focus.<sup>5</sup> So when the two end wavelengths of a lens
disagree, the lens has primary colour; secondary colour is the middle wavelength measured against the
common focus of the two ends.

### 5.1 Per element

Replace the full-band index difference by the difference between two wavelengths $p$ and $q$. For thin
lenses Sasián gives the result as a wavefront coefficient, which in this document's symbols reads
$\delta_\lambda W_{020} = \tfrac12 \sum_i y_i^2\,\phi_i\,P_i/\nu_i$ with $P_i$ the partial dispersion
from $\lambda$ to the F line; his slide writes $y_m$ for the marginal height and $1/f$ for the
power.<sup>4</sup> With $L_i$ the axial colour contribution of element $i$ and $P_i = P_{p,q}$ of its
glass,

$$
C_L^{p,q} = \sum_i L_i\,P_i \tag{5.1}
$$

and, by the derivation of section 4.3 **(derived here)**,

$$
z_p - z_q = -\frac{C_L^{p,q}}{n'_k\left(u'_k\right)^2} \tag{5.2}
$$

Using the dispersion function (3.3), the whole chromatic focal-shift curve is, to first order
**(derived here)**,

$$
z_\lambda - z_0 = -\frac{1}{n'_k\left(u'_k\right)^2}\sum_i y_i^2\,\phi_i\,D_i(\lambda) \tag{5.3}
$$

*A consequence for an achromatic lens.* If the primary colour is corrected, $\sum_i L_i = 0$. Then (5.1)
is unchanged when the same constant is added to every $P_i$, so the secondary spectrum depends only on
the *differences* in partial dispersion between the elements, weighted by their primary colour
contributions. The secondary colour is zero when every element that carries primary colour has the same
$P$. The two-glass and three-glass results below make this concrete.

### 5.2 Two glasses in contact

Two thin lenses in contact with total power $\phi$, corrected for primary colour, satisfy
$\phi_\mathrm{a} + \phi_\mathrm{b} = \phi$ and
$\phi_\mathrm{a}/\nu_\mathrm{a} + \phi_\mathrm{b}/\nu_\mathrm{b} = 0$. The powers are<sup>5</sup>

$$
\phi_\mathrm{a} = \phi\,\frac{\nu_\mathrm{a}}{\nu_\mathrm{a} - \nu_\mathrm{b}}, \qquad
\phi_\mathrm{b} = -\phi\,\frac{\nu_\mathrm{b}}{\nu_\mathrm{a} - \nu_\mathrm{b}} \tag{5.4}
$$

*Sign note.* Sasián's slide<sup>4</sup> prints the equivalent relation as
$f_\mathrm{a}\nu_\mathrm{a} = f_\mathrm{b}\nu_\mathrm{b} = F(\nu_\mathrm{a} - \nu_\mathrm{b})$, where his
$F$ is the combined focal length. With signed focal lengths, (5.4) gives
$f_\mathrm{a}\nu_\mathrm{a} = -f_\mathrm{b}\nu_\mathrm{b}$; the two differ by a factor of −1 in the
middle term.

Putting (5.4) into (5.1) and (5.2), for an object at infinity **(derived here)**,

$$
z_p - z_q = -f\,\frac{P_\mathrm{a} - P_\mathrm{b}}{\nu_\mathrm{a} - \nu_\mathrm{b}} \tag{5.5}
$$

Both sets of notes state this residual: Sasián as a wavefront coefficient proportional to
$(P_\mathrm{a} - P_\mathrm{b})/(\nu_\mathrm{a} - \nu_\mathrm{b})$,<sup>4</sup> and Greivenkamp as a
fractional focal shift equal to the same ratio, which he writes $\Delta P/\Delta\nu$.<sup>5</sup>

*Sign note.* Greivenkamp<sup>5</sup> writes the shift as a positive quantity for the d and C lines;
(5.5) is signed. The two agree in magnitude.

The price of a pair is its element power. Define

$$
\Phi_1 = \frac{\sum_i \lvert\phi_i\rvert}{\lvert\phi\rvert} \tag{5.6}
$$

Then from (5.4) **(derived here)**

$$
\Phi_1 = \frac{\nu_\mathrm{a} + \nu_\mathrm{b}}{\lvert\nu_\mathrm{a} - \nu_\mathrm{b}\rvert} \tag{5.7}
$$

A pair with matched $P$ but nearly equal $\nu$ therefore needs very strong elements. Section 7.1 takes
this up.

*Worked example.* N-BK7 with F2, $f = 100$ mm: $f_\mathrm{a} = +43.33$ mm, $f_\mathrm{b} = -76.45$ mm,
and $\Phi_1 = 3.62$.

| Wavelengths $p$, $q$ | $(P_\mathrm{a} - P_\mathrm{b})/(\nu_\mathrm{a} - \nu_\mathrm{b})$ | $z_p - z_q$ from (5.5) | From exact thin-lens focal lengths |
|---|---|---|---|
| g, F | −0.001722 | +0.1722 mm | +0.1726 mm |
| d, F | +0.000499 | −0.0499 mm | −0.0500 mm |

The g focus lies 0.17 mm beyond the common F and C focus, and the d focus 0.05 mm short of it: the
familiar secondary spectrum of about $f/2000$ for ordinary glasses. Greivenkamp quotes a ratio of about
$1/2200$ for the d, C partial.<sup>5</sup>

### 5.3 Three glasses in contact

Three thin lenses in contact that correct primary and secondary colour satisfy<sup>4</sup>

$$
\sum_i \phi_i = \phi, \qquad \sum_i \frac{\phi_i}{\nu_i} = 0, \qquad \sum_i \frac{\phi_i\,P_i}{\nu_i} = 0 \tag{5.8}
$$

Sasián's slide writes these for the element curvatures $c_i$ (the difference of the two surface
curvatures of element $i$) and a quantity $\Delta n$ for each glass, which it does not define and which
we read as the principal dispersion $n_\mathrm{F} - n_\mathrm{C}$. On that reading the element power is $\phi_i = \nu_i\,c_i\,(n_\mathrm{F} - n_\mathrm{C})_i$, which turns his conditions into
(5.8). His solution, written in powers by the same substitution, is<sup>4</sup>

$$
\phi_\mathrm{a} = \phi\,\frac{\nu_\mathrm{a}\,(P_\mathrm{b} - P_\mathrm{c})}{\Theta}, \qquad
\phi_\mathrm{b} = \phi\,\frac{\nu_\mathrm{b}\,(P_\mathrm{c} - P_\mathrm{a})}{\Theta}, \qquad
\phi_\mathrm{c} = \phi\,\frac{\nu_\mathrm{c}\,(P_\mathrm{a} - P_\mathrm{b})}{\Theta} \tag{5.9}
$$

$$
\Theta = \nu_\mathrm{a}\,(P_\mathrm{b} - P_\mathrm{c}) + \nu_\mathrm{b}\,(P_\mathrm{c} - P_\mathrm{a}) + \nu_\mathrm{c}\,(P_\mathrm{a} - P_\mathrm{b}) \tag{5.10}
$$

He writes the result with $E = \Theta/(\nu_\mathrm{a} - \nu_\mathrm{c})$ and identifies $E$ as the "sag"
of the triangle formed by the three glasses on the $P$–$\nu$ diagram: the vertical distance of glass b
from the line joining a and c.<sup>4</sup> $\lvert\Theta\rvert$ is twice the area of that triangle
**(derived here)**.

Every power in (5.9) is proportional to $1/\Theta$. A flat triangle, three glasses nearly on one line,
gives very strong elements. A useful three-glass set needs one glass well off the line joining the other
two.<sup>4,10</sup>

*Worked example*, $f = 100$ mm, with $P = P_{\mathrm{g,F}}$:

| Glasses a / b / c | $\Theta$ | $E$ | Focal lengths (mm) | $\Phi_1$ |
|---|---|---|---|---|
| N-FK51A / N-KZFS11 / N-SF6 | −1.904 | −0.0322 | +40.8, −56.2, +305.0 | 4.6 |
| N-BK7 / N-KZFS11 / F2 | −0.330 | −0.0119 | +23.1, −16.3, +35.5 | 13.3 |

Both sets satisfy all three conditions. The second, with a triangle less than a fifth of the area, needs
about three times the element power.

### 5.4 Lateral secondary colour (derived here)

Replace the band difference by a pair $p$, $q$, as in 5.1, and follow 4.3 with the chief ray as the
perturbed ray.

*Step 1.* At surface $j$, holding the incoming ray fixed,
$n'\,\delta\bar u' = -\bar A_j\,\Delta_j\!\left[(n_p - n_q)/n\right]$.

*Step 2.* The quantity $n\,(\delta\bar u\,y - u\,\delta\bar y)$ formed with the marginal ray is constant
through the rest of the system (2.3). Just after the surface $\delta\bar y = 0$, so it equals
$-\bar A_j\,y_j\,\Delta_j\!\left[(n_p - n_q)/n\right]$; in the image plane $y = 0$, so it equals
$-n'_k\,u'_k\,\delta\bar y'$. Summing over surfaces, and reducing to thin lenses in air by
$\delta_{p,q}\phi_i = \phi_i P_i/\nu_i$ (3.4):

$$
C_T^{p,q} = \sum_j \bar A_j\,y_j\,\Delta_j\!\left(\frac{n_p - n_q}{n}\right) = \sum_i T_i\,P_i \tag{5.11}
$$

$$
h'_p - h'_q = \frac{C_T^{p,q}}{n'_k\,u'_k} \tag{5.12}
$$

If $\sum_i T_i = 0$ only the differences in $P$ matter, as for (5.1).

*Worked check* on the lens of 4.4, with $P = P_{\mathrm{g,F}}$ in the first pair and $P_{\mathrm{d,F}}$ in
the second:

| Lens | $T_i$ (mm) | $T_i P_{\mathrm{g,F}}$ (mm) | $T_i P_{\mathrm{d,F}}$ (mm) |
|---|---|---|---|
| 1 | −0.016577 | −0.008972 | +0.011519 |
| 2 | 0.000000 | 0.000000 | 0.000000 |
| 3 | +0.013921 | +0.007534 | −0.009673 |
| Sum | −0.002656 | −0.001437 | +0.001846 |

| Quantity | (5.12) | Direct trace |
|---|---|---|
| $h'_\mathrm{g} - h'_\mathrm{F}$ | +0.01507 mm | +0.01527 mm |
| $h'_\mathrm{d} - h'_\mathrm{F}$ | −0.01935 mm | −0.01944 mm |

The prediction is 1.3% low for g to F and 0.5% low for d to F. As in 9.2 the error is second order: with
the dispersion scaled by 0.5, 0.25 and 0.1 the g to F error is 0.6%, 0.3% and 0.1%.

## 6. Field curvature

### 6.1 Petzval sum

The Petzval sum over surfaces, and its thin-lens form, are<sup>9,10</sup>

$$
S_\mathrm{P} = \sum_j \frac{n'_j - n_j}{n_j\,n'_j\,r_j}, \qquad
S_\mathrm{P} = \sum_i \frac{\phi_i}{n_i}\ \ \text{(thin lenses in air)} \tag{6.1}
$$

Regrouped by medium as in (4.2a) **(derived here)**, the glass between surfaces $j$ and $j+1$ carries

$$
S_{\mathrm{P},m} = (c_j - c_{j+1})\left(1 - \frac{1}{n_m}\right) \tag{6.1a}
$$

The sum over the media equals the surface form when the image space is air. Otherwise the surface form
carries in addition the term $c_k(1 - 1/n'_k)$ of the last surface, which belongs to no glass.

The Petzval sum does not depend on the bending of the elements.<sup>10</sup> It follows from the surface
form that it does not depend on the element separations or on the stop position either. For a flat object, the radius
of the Petzval image surface is given by the first relation below.<sup>10</sup> The second is the sag of
a sphere of that radius at image height $h'$, to second order in $h'$:

$$
\frac{1}{R_\mathrm{P}} = -n'_k\,S_\mathrm{P}, \qquad \text{sag} = \frac{h'^2}{2R_\mathrm{P}} \tag{6.2}
$$

The sag is measured along $+z$ from the image plane. A positive lens gives a negative $R_\mathrm{P}$: the
centre of curvature lies on the lens side, so the image surface curves back towards the lens.<sup>10</sup>

*Sign note.* Sasián's formula slide<sup>9</sup> writes the thin-lens relation as
$1/\rho'_k = \sum\phi/n$, without the minus sign; here $1/R_\mathrm{P} = -n'_k S_\mathrm{P}$, as in
Gross's slides.<sup>10</sup> The two differ by a factor of −1.

### 6.2 Flattening the field by glass choice

For an object at infinity the total power is $\sum_i (y_i/y_1)\,\phi_i$ while the Petzval sum is
$\sum_i \phi_i/n_i$, so the two can be traded<sup>10</sup>:

- positive elements should have high index and sit where the marginal ray is high, so that they give
  much power for little Petzval sum;
- negative elements should have low index and sit where the marginal ray is low, so that they give
  little negative power for much negative Petzval sum.

For two thin lenses in contact that are also achromatic, the field is flat when
$\phi_\mathrm{a}/n_\mathrm{a} + \phi_\mathrm{b}/n_\mathrm{b} = 0$ as well, which requires<sup>11</sup>

$$
\frac{\nu_\mathrm{a}}{\nu_\mathrm{b}} = \frac{n_\mathrm{a}}{n_\mathrm{b}} \tag{6.3}
$$

More generally, putting (5.4) into (6.1) gives the Petzval sum of any achromatic contact pair
**(derived here)**:

$$
S_\mathrm{P} = \phi\,\frac{\nu_\mathrm{a}/n_\mathrm{a} - \nu_\mathrm{b}/n_\mathrm{b}}{\nu_\mathrm{a} - \nu_\mathrm{b}} \tag{6.4}
$$

Ordinary crown and flint pairs have the higher index in the low-$\nu$ glass, which makes (6.4) worse than
a singlet. A "new achromat" reverses this: a crown of raised index with a flint of lowered
index.<sup>4</sup>

*Worked example*, $f = 100$ mm:

| System | $S_\mathrm{P}$ (1/mm) | $R_\mathrm{P}$ (mm) | Sasián's slide<sup>4</sup> |
|---|---|---|---|
| N-BK7 singlet | 0.006593 | −151.7 | −152 (BK7) |
| N-BK7 + F2 | 0.007142 | −140.0 | −139 (BK7 and F2) |
| N-SSK5 + LF5 | 0.004834 | −206.9 | −219 (SSKN5 and LF5) |

The slide labels its values "P" without defining the symbol; they are read here as Petzval radii, which
the first two rows support. The third row uses today's N-SSK5 in place of the slide's older SSKN5, which
accounts for the difference.

## 7. Element power and spherical aberration

### 7.1 Power sum as a proportion measure

Albuquerque et al rank glass combinations on several scores at once. One of them is the sum of the
absolute element powers for a system normalised to unit total power, which is $\Phi_1$ of (5.6);
combinations above a chosen limit are discarded.<sup>6</sup> Strong opposing powers mean steep curves,
thick elements and tight tolerances, so $\Phi_1$ is a computable stand-in for the proportions of the lens
before any ray is traced. Equation (5.7) and the last column of the tables in section 5 are this score.

For separated elements we use the same definition, $\Phi_1 = f\sum_i\lvert\phi_i\rvert$ for a lens of
positive focal length $f$ **(our extension)**; reference 6 treats lenses in contact.

### 7.2 Thin-lens spherical aberration against index

For a thin lens in air with the stop at the lens, the Seidel sum for spherical aberration
is<sup>9,12</sup>

$$
S_\mathrm{I} = \tfrac14\,y^4\,\phi^3\left[Q_1 X^2 - Q_2\,X\,Y + Q_3 Y^2 + Q_4\right] \tag{7.1}
$$

with the shape factor $X = (c_1 + c_2)/(c_1 - c_2)$, the conjugate factor
$Y = (1+m)/(1-m) = (u'+u)/(u'-u)$ for magnification $m$, and the coefficients

$$
Q_1 = \frac{n+2}{n(n-1)^2}, \qquad Q_2 = \frac{4(n+1)}{n(n-1)}, \qquad Q_3 = \frac{3n+2}{n}, \qquad Q_4 = \frac{n^2}{(n-1)^2} \tag{7.2}
$$

which the source names $A$, $B$, $C$ and $D$.<sup>9</sup> $S_\mathrm{I}$ is positive for a positive
lens, which is undercorrected spherical aberration. The wavefront coefficient is
$W_{040} = S_\mathrm{I}/8$.<sup>9</sup>

Minimising (7.1) over the bending $X$ **(derived here)**:

$$
X_{\min} = \frac{Q_2\,Y}{2Q_1} = \frac{2(n^2-1)}{n+2}\,Y \tag{7.3}
$$

$$
S_{\mathrm{I},\min} = \tfrac14\,y^4\,\phi^3\left[\frac{n^2}{(n-1)^2} - \frac{n}{n+2}\,Y^2\right] \tag{7.4}
$$

For an object at infinity ($Y = 1$) the bracket in (7.4) is $n(4n-1)/[(n-1)^2(n+2)]$ **(derived here)**:

| $n$ | $X_{\min}$ | Bracket | Relative to $n = 1.5$ |
|---|---|---|---|
| 1.5 | 0.714 | 8.571 | 1.000 |
| 1.6 | 0.867 | 6.667 | 0.778 |
| 1.7 | 1.022 | 5.438 | 0.634 |
| 1.8 | 1.179 | 4.589 | 0.535 |
| 2.0 | 1.500 | 3.500 | 0.408 |

$X_{\min}$ is positive: the more strongly curved side faces the distant object, as expected. At the same
power and aperture, raising the index of a positive element from 1.5 to 1.8 roughly halves the least
spherical aberration it can have. Index is therefore a lever on spherical aberration as well as on field
curvature, and it acts through the cube of the element power, so it matters most in the strongest
elements.

## 8. Predicting a glass change

Sections 4 to 7 describe a lens as it stands. This section is our own construction from those formulas.
It is the part of the method that the literature we could read does not supply, and section 8.3 states
what holds when the focal length is kept fixed.

### 8.1 One glass changed, element power held (derived here)

If the glass of element $i$ changes from old to new while its power and the ray heights stay fixed, the
sums change by

$$
\Delta_\mathrm{sub}\,C_L = y_i^2\,\phi_i\left(\frac{1}{\nu^\mathrm{new}} - \frac{1}{\nu^\mathrm{old}}\right), \qquad
\Delta_\mathrm{sub}\,C_L^{p,q} = y_i^2\,\phi_i\left(\frac{P^\mathrm{new}}{\nu^\mathrm{new}} - \frac{P^\mathrm{old}}{\nu^\mathrm{old}}\right) \tag{8.1}
$$

$$
\Delta_\mathrm{sub}\,C_T = y_i\,\bar y_i\,\phi_i\left(\frac{1}{\nu^\mathrm{new}} - \frac{1}{\nu^\mathrm{old}}\right), \qquad
\Delta_\mathrm{sub}\,S_\mathrm{P} = \phi_i\left(\frac{1}{n^\mathrm{new}} - \frac{1}{n^\mathrm{old}}\right) \tag{8.2}
$$

These are local sensitivities. Tested on the lens of section 4.4 by changing the glass of lens 2 and
tracing directly:

| New glass for lens 2 | $\Delta_\mathrm{sub}(z_\mathrm{F} - z_\mathrm{C})$ predicted | direct | $\Delta_\mathrm{sub}(z_\mathrm{g} - z_\mathrm{F})$ predicted | direct |
|---|---|---|---|---|
| LF5 | −0.812 mm | −0.807 mm | −0.526 mm | −0.513 mm |
| N-KZFS11 | −1.054 mm | −1.046 mm | −0.756 mm | −0.733 mm |
| N-KZFS2 | −2.417 mm | −2.384 mm | −1.613 mm | −1.523 mm |
| N-SF6 | +3.211 mm | +3.246 mm | +2.221 mm | +2.352 mm |

The predictions hold to within 6%. They also show why a single glass change at fixed power is not a
design move on its own: any change of $\nu$ throws the primary colour out by 0.8 mm or more, and the lens
has to be re-balanced before the change can be judged.

Tested on the doublet of section 4.5 with the database indices at d: changing the F2 to LF5 at fixed element power gives $\Delta_\mathrm{sub} S_\mathrm{P} = -0.009178\,(1/1.58144 - 1/1.62004) = -0.000138$ per mm, so $S_\mathrm{P}$ falls from 0.007648 to 0.007510 per mm; to N-SF6, $+0.000581$ (0.008229); and the N-BK7 to F2, $-0.000836$ (0.006812). In a cemented element "power held" is not a construction that can be carried out on its own: the shared surface belongs to the neighbour too, so (8.2) for such an element is a local sensitivity, read beside a measurement, never a forecast. Changing the glasses of several elements at once, each at its own fixed power, adds their terms, $\Delta_\mathrm{sub} S_\mathrm{P} = \sum_j \phi_j\,(1/n_j^\mathrm{new} - 1/n_j^\mathrm{old})$, because the Petzval sum is a sum over elements; the same reading applies to every cemented element in such a set.

### 8.2 One glass changed, colour contribution held (derived here)

For a lens corrected for primary colour, (5.1) gives the secondary sum as $\sum_i L_i P_i$ with
$\sum_i L_i = 0$. If the glass of element $i$ changes and its power is rescaled to keep its colour
contribution $L_i$,

$$
\phi_i^\mathrm{new} = \phi_i\,\frac{\nu^\mathrm{new}}{\nu^\mathrm{old}}, \qquad
\Delta_\mathrm{sub}\,C_L^{p,q} = L_i\left(P^\mathrm{new} - P^\mathrm{old}\right) \tag{8.3}
$$

and the partial dispersion that element $i$ would need in order to cancel the whole secondary sum is

$$
P_i^\mathrm{need} = P_i - \frac{C_L^{p,q}}{L_i} \tag{8.4}
$$

Equation (8.4) gives a target that is easy to read. In the lens of section 4.4, lens 2 has
$P_{\mathrm{g,F}} = 0.5828$ and would need 0.5412. That is the $P_{\mathrm{g,F}}$ of the N-SK16 in lenses
1 and 3, as the consequence noted in section 5.1 requires: the secondary colour vanishes when all three
elements share one $P$. Among ordinary glasses that $P$ belongs to crowns near $\nu = 60$, the region
lenses 1 and 3 already occupy. This is the trade the designer actually faces: the closer the match in
$P$, the smaller the separation in $\nu$ and the larger the powers, by (8.3) and (5.7).

### 8.3 A limit of the fixed-height prediction

Equation (8.3) assumes the ray heights and the focal length do not move when the element power is
rescaled. In a tightly balanced lens they move a great deal. Re-solving the power of lens 2 alone for
zero primary colour in the lens of section 4.4, and tracing directly:

| Glass for lens 2 | $\phi_2^\mathrm{new}/\phi_2^\mathrm{old}$ from (8.3) | re-solved | $(z_\mathrm{g} - z_\mathrm{F})/f$ predicted | re-solved | New $f$ (mm) | $\Phi_1$ |
|---|---|---|---|---|---|---|
| N-SF6 | 0.697 | 0.594 | +0.00526 | +0.00199 | 51.4 | 3.9 |
| N-SF15 | 0.830 | 0.752 | +0.00442 | +0.00217 | 64.1 | 5.4 |
| F2 (current) | 1.000 | 1.000 | +0.00294 | +0.00245 | 104.9 | 10.0 |
| LF5 | 1.123 | 1.238 | +0.00237 | +0.00529 | 269.1 | 28.8 |
| N-KZFS11 | 1.166 | 1.340 | +0.00136 | +0.00788 | 828.0 | 92.7 |

The two columns of secondary colour order the candidates oppositely, but the focal length in the table
runs from 51 to 828 mm, so its rows are lenses of different focal length. The table shows that
re-solving one power is not a fair re-balance. It does not show how the candidates compare at a fixed
focal length.

A like-for-like comparison holds the focal length (104.850 mm) and the primary colour
$z_\mathrm{g} - z_\mathrm{C}$ at the value a direct trace gives with F2 (+0.2377 mm), and re-balances
all three powers. Lens 2 keeps its curvatures, so its power first changes with the index of the new glass,
by the ratio $(n^\mathrm{new}-1)/(n^\mathrm{old}-1)$; each lens power is then multiplied by a scale $k_i$
(all equal to 1 for the current lens), and of the scale sets that meet both conditions the one with the smallest
$\sum_i (\phi_i f)^2 (k_i - 1)^2$ is taken, so the baseline powers act as weights. The three-lens
example of section 4.4 gives, with lens 2's glass changed and traced directly at g, F, d, C:

| Glass for lens 2 | $z_\mathrm{d} - z_\mathrm{g}$ (mm) | $\Phi_1$ | $k_1, k_2, k_3$ |
|---|---|---|---|
| F2 (current) | −0.318051 | 10.023 | 1, 1, 1 |
| N-SF15 | −0.358128 | 10.923 | 1.441, 1.031, 0.593 |
| N-SF6 | −0.398995 | 12.265 | 1.725, 1.099, 0.311 |
| LF5 | none with scales between 0.05 and 3 | | |
| N-KZFS11 | none with scales between 0.05 and 3 | | |

Neither of the two smallest-change solutions reduces the secondary colour $z_\mathrm{d} - z_\mathrm{g}$ below
its value with the current glass. They fall in the same order as the fixed-height prediction, which also
places N-SF15 ahead of N-SF6. The two glasses whose partial dispersion matches best, LF5 and N-KZFS11,
have no solution with $k_1$ and $k_3$ between 0.05 and 3 (with $k_2$ taken from the focal-length
condition, the colour condition never changes sign there, and its smallest residual is about 0.009 and
0.013 of the focal length). Solutions do exist far outside that range. The one of smallest weighted change
for LF5 has $k = 3.19, 17.13, 36.31$, and for N-KZFS11 $k = 3.20, 16.39, 34.74$: scales of about 17 and 35
on lenses 2 and 3, a sum of absolute powers times the focal length near 180 against 10 for the current
lens, and a secondary colour $z_\mathrm{d} - z_\mathrm{g}$ of $-1.020$ and $-1.028$ mm, about three
times worse than the $-0.318$ mm of the current lens. These glasses are reachable on paper but not as a
usable lens. Both tables point the same way: these glasses demand far more power than the lens can give.

Two conclusions follow:

1. Formulas (8.1) to (8.4) locate the problem and explain a candidate. They do not tell whether a
   candidate can be reached at a sensible power in a given lens; that is a separate question.
2. No first-order calculation here is established as a way to choose between candidates (section 9.4).
   The choice belongs to a trial in the real lens, re-optimised and measured against the same lens
   re-optimised with its glass unchanged.

This is in the spirit of the published two-step method of Li et al: choose the glasses with the geometry
fixed, using a chromatic merit function; then optimise the geometry with the glasses fixed; and, if the
practical merit function is not met, re-optimise the glasses with the geometry fixed.<sup>13</sup>
How far each trial is re-optimised before the candidates are compared, and how the measured performance
and the layout are weighed against each other, are engineering choices of the tools. We found no open
source that prescribes them.

## 9. Limits of the model

### 9.1 Thin-lens approximation

Sections 4.2 to 8 treat elements as thin. For thick elements and cemented groups the surface sums (4.1)
and (6.1) are exact to first order and should be used; the thin-element forms are for explanation. In
particular $\phi_i/n_i$ with the true power of a thick lens is not its Petzval term: for a lens of index
1.5 with radii +60 and −80 mm and thickness 6 mm, the surface sum is 0.009722 per mm and the true power
divided by the index is 0.009583 per mm, 1.4% less.

### 9.2 First order in the index change

The sums are derivatives evaluated at the reference wavelength. In the direct trace of section 4.4 the
primary colour that the sum sets to zero is −0.019 mm, and the secondary colour is predicted 20% high.
The second error is larger because the g to F interval lies wholly to one side of the reference
wavelength, and because the ray heights at the later lenses themselves change with wavelength, which the
first-order sum ignores. The error is second order: when every glass's dispersion is scaled by 0.5, 0.25
and 0.1, the 20% error falls to 9.1%, 4.3% and 1.7%. The sum of contributions is an estimate of a
residual left after large terms cancel, so a small relative error in the terms is a large relative error
in the residual.

### 9.3 Surfaces the model does not cover

An aspheric term changes neither colour sum,<sup>9</sup> and the Petzval sum depends only on vertex
curvatures. Gradient-index media, diffractive surfaces, tilted or decentred elements and mirrors are
outside the formulas as written here.

### 9.4 Not yet established

- **Comparison with the optical engine.** The sums of sections 4 to 6 have been checked against direct
  paraxial traces of thin lenses and of a thick cemented doublet, and not yet against a real lens in the
  engine. That comparison (computed $C_L$ against the engine's axial colour, computed $S_\mathrm{P}$
  against the engine's Petzval value, and the sign and unit of each) is the next step.
- **High numerical aperture.** In fast systems such as microscope objectives, higher-order and induced
  colour can outweigh the first-order sums. We have not yet read an open source that quantifies this,
  so this document makes no claim about where the first-order picture stops being a useful guide.
- **A single glass substitution in a highly corrected lens.** The effect of one glass change in such a
  lens has not been shown to be predictable by these first-order sums. In one test on a 14-element
  microscope objective, for two substitutions whose measured effect on lateral colour was known, the
  fixed-height formula gave the opposite sign. A constrained re-balance gave the right sign for those
  two and the wrong sign for two other substitutions. In that lens one element carries several hundred
  times the total secondary colour, so the total is a small difference of large terms. One lens and four
  substitutions do not establish a rule either way. Until more cases are measured, a predicted change
  belongs beside a measured one and is not a basis for choosing.

## 10. References

Each source was read from its page images on 12 October 2026. No source behind a paywall was used. Lecture
notes and slides are cited by author, course, lecture and year; a URL is given where the file has a stable one.

1. Greivenkamp JE. Paraxial raytracing. In: *OPTI-502 Optical Design and Instrumentation I* [lecture
   notes, section 8]. University of Arizona, College of Optical Sciences; 2019. Accessed October 12,
   2026. https://wp.optics.arizona.edu/jgreivenkamp/wp-content/uploads/sites/11/2019/08/502-08-Paraxial-Raytracing.pdf
2. Sasián J. Chromatic aberrations. In: *OPTI 517 Lens Design* [lecture notes, lecture 9]. University of
   Arizona, College of Optical Sciences; undated, posted 2016. Accessed October 12, 2026.
3. SCHOTT AG. *TIE-29: Refractive Index and Dispersion*. Technical information. SCHOTT AG; April 2005.
   Accessed October 12, 2026.
   https://wp.optics.arizona.edu/optomech/wp-content/uploads/sites/53/2016/10/tie-29_refractive_index_v2_us.pdf
4. Sasián J. Secondary spectrum. In: *OPTI 517 Lens Design* [lecture notes, lecture 10]. University of
   Arizona, College of Optical Sciences; undated, posted 2016. Accessed October 12, 2026.
5. Greivenkamp JE. Chromatic effects. In: *OPTI-502 Optical Design and Instrumentation I* [lecture notes,
   section 20]. University of Arizona, College of Optical Sciences; 2019. Accessed October 12, 2026.
   https://wp.optics.arizona.edu/jgreivenkamp/wp-content/uploads/sites/11/2019/08/502-20-Chromatic-Effects.pdf
6. de Albuquerque BFC, Sasian J, de Sousa FL, Montes AS. Method of glass selection for color correction in
   optical system design. *Opt Express*. 2012;20(13):13592-13611. doi:10.1364/OE.20.013592
7. HOYA Corporation. *Optical Glass* [technical data, section 2.4]. HOYA Corporation USA, Optics Division;
   undated. Accessed October 12, 2026.
   https://wp.optics.arizona.edu/optomech/wp-content/uploads/sites/53/2016/10/Hoya-OpticalGlass.pdf
8. OHARA GmbH. *Optical Glass* [pocket catalogue]. OHARA GmbH; January 2020:82-83. Accessed October 12,
   2026. https://www.ohara-gmbh.com/fileadmin/user_upload/OHARA-Taschenkatalog_Jan-2020_01.pdf
9. Sasián J. Summary of formulas. In: *OPTI 517 Lens Design* [lecture notes, lecture 13]. University of
   Arizona, College of Optical Sciences; undated, posted 2016. Accessed October 12, 2026.
10. Gross H. Correction of aberrations 1. In: *Design and Correction of Optical Systems* [lecture slides,
    part 12]. Friedrich-Schiller-Universität Jena, Institute of Applied Physics; 2012. Archived January 30,
    2017. Accessed October 12, 2026.
    https://web.archive.org/web/20170130142017id_/http://www.iap.uni-jena.de/iapmedia/de/Lecture/Design+and+correction+of+optical+systems1351638000/DaCOS12_Lecture_Part_12_Correction_of_aberrations_1_112.pdf
11. Gross H. Astigmatism and field curvature. In: *Imaging and Aberration Theory* [lecture slides, lecture
    8]. Friedrich-Schiller-Universität Jena, Institute of Applied Physics; 2013. Archived March 21, 2016.
    Accessed October 12, 2026.
12. Sasián J. Spherical aberration. In: *OPTI 517 Lens Design* [lecture notes, lecture 8]. University of
    Arizona, College of Optical Sciences; undated, posted 2016. Accessed October 12, 2026.
13. Li L, Wang QH, Xu XQ, Li DH. Two-step method for lens system design. *Opt Express*.
    2010;18(12):13285-13300. doi:10.1364/OE.18.013285

### Where in each source

| Reference | Slides or pages used |
|---|---|
| 1 | Slides 8-3 to 8-5 and 8-7 |
| 2 | Slides 6 to 8 |
| 3 | Pages 2 to 4 (sections 2.1 and 2.2, equations 2.2-1 to 2.2-5) |
| 4 | Slides 3, 4, 7, 8, 9 and 22 |
| 5 | Slides 20-3, 20-4, 20-12 to 20-15 and 20-17 |
| 6 | Journal pages 13595, 13596, 13598 and 13601 (equations 3, 8, 22 and 29) |
| 7 | Page 6 (section 2.4) |
| 8 | Printed pages 82 and 83 |
| 9 | Slides 5, 6, 7, 10 and 11 |
| 10 | Slides 19, 20 and 31 |
| 11 | Slide 35 |
| 12 | Slides 19 and 21 |
| 13 | Journal page 13288 |

The lecture numbers of references 2, 4, 9 and 12 are taken from the file names; the slides themselves
carry no lecture number and no date.

### Sources located but not used

- The classical papers on glass selection for apochromats (Herzberger and McClure 1963, Stephens 1959 and
  1960, Robb 1985, Sigler 1986, Rayces and Rosete-Aguilar 2001) are not openly available. They were not
  read and nothing here rests on them.
- The standard textbooks were not consulted for the same reason. Where a result is a textbook standard,
  it is cited here to open lecture notes that state it.
