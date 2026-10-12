# Walk-in stage log — lwd10x_na045 (long-WD 10x / NA 0.45 from the 60x / NA 0.90 seed)

All numbers are tool readings taken in this session. RWCE = polychromatic RMS wavefront (waves, ring 6, wave 0)
at 0 / 0.5 / 0.7 / 1.0 mm field. Design is modelled reversed (tube side in, specimen = image plane).

## Seed (BEFORE)
- 14 elements (2+2+2+2+3+1+1+1), EFL 3.338 mm, real-ray NA 0.899, WD 0.130 mm, track 30.48 mm, field +/-0.183 mm.
- RWCE 0.067 / 0.084 / 0.098 / 0.125; polychromatic Strehl (STRH rows, Samp 2) 0.888 / 0.811 / 0.742 / 0.611.
- Against this brief's floors: 16 clearance violations (0.05 mm air gaps, glass edges down to 0.50 mm).
- Scaled only (EFL 20, EPD 18): NA 0.450, WD 0.780 mm, RWCE 0.178 on axis / 0.439 at 1.0 mm, track 182.6 mm.

## Stage 1 — feasible form (seq 1)
- Spec aimed at: EFL 20, WD >= 5.3 (vertex) / 5.2 (rim), air >= 0.55, glass >= 1.1, track <= 150.
- Moves: all radii/thicknesses variable (45), wavefront merit (4 rings), EFFL, CTGT/CTLT/ETGT on the WD gap, TOTR ceiling.
- Readings: EFL 20.000, WD 5.38, track 150.0, RWCE 0.125 / 0.130 / 0.165 / 0.281, 0 clearance violations.
- Review: 5 findings (too long, thin negatives 12-13 and 15-16, tight gaps 19-20 and 23-24).

## Stage 2 — packaging (seq 2, answers recorded on seq 3)
- Moves: glass ceiling 12 -> 8, air ceiling 14 -> 10, track 150 -> 115 -> 95, air floor 0.8, CTGT 2.2 on thin negatives.
- Readings: track 95.0, WD 5.71, RWCE about 0.15 on axis / 0.29 at 1.0 mm (slow convergence; OD gave 0.01 % in 352 s).
- Aberration review (review_2): no single limiter — primary axial colour 3.66 um g->C, astigmatism 4.32 um,
  colour-dependent coma, lateral colour 1.39 um.
- Vision review: 6 findings (10 mm air gap, thin elements 2-3 and 5-6, tight 21-22, WD vs front block, form).

## Stage 3 — performance (seq 4)
- Moves: 6 rings, air ceiling 7, front element <= 5 mm, chief-ray REAY terms per wavelength, AXCL, field weights 1/1/2/4.
  RWCE fell to 0.032 / 0.056 / 0.073 / 0.105; edge was limited by SECONDARY lateral colour
  (C +0.61 um, g +0.24 um, F -0.13 um at 1.0 mm; a 10x weight raise moved C only to +0.46 um).
- Glass trials (one surface at a time, snapshot before each):
  - S22 S-LAH58 -> N-LAK34: pattern unchanged (kept, marginal gain).
  - S18 N-PSK58 -> S-FPL51Y: no change (reverted).
  - S5 S-NBH52 -> N-KZFS11: no change (reverted).
  - S1 S-BAH28 -> N-KZFS11: C +0.37 um but axis 0.035 -> 0.058 (reverted).
  - S15 N-KZFS11 -> N-KZFS2: C +0.25 um, RWCE 0.039 / 0.048 / 0.052 / 0.065 (kept).
  - S9 N-KZFS11 -> N-KZFS2: C +0.14 um, RWCE 0.036 / 0.054 / 0.057 / 0.064 (kept).
- After convergence: RWCE 0.026 / 0.045 / 0.048 / 0.055, track 89.6, WD 6.63 vertex / 5.40 rim.
- Review: 4 findings (thin rims on 11-12, 16-17, 20-21; WD not generous).

## Stage 4 — margins (seq 5)
- Moves: rim WD floor 5.4 -> 6.0 (no image cost), ETGT >= 2.0 on every positive element's edge.
- Readings: RWCE 0.028 / 0.042 / 0.044 / 0.047.
- Review: 3 findings (front element 1.10 mm wafer, negative front element, steep cemented face 9 — RAID 38.4/40.8 deg).

## Stage 5 — final (seq 6, promoted)
- Moves: CTGT 22 >= 2.2, CVGT 9 >= -0.05 (|R| >= 20 mm).
- Readings: EFL 20.00004, NA 0.4498, WD 6.793 vertex / 6.000 rim, track 87.14,
  RWCE 0.030 / 0.038 / 0.041 / 0.045, Strehl 0.970 / 0.947 / 0.941 / 0.922,
  field sag at 546 nm 0.05 um S / 1.01 um T, RAID at surface 9 34.0 deg, 0 clearance violations.
- Review: 1 finding (near-parallel 1.2 mm air space 21-22) — declined with the gap measurement.
