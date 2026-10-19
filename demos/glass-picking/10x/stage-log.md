Internal IDs and file names removed; otherwise as the agent wrote it. Numbers here are the agent's own readings during the run; the measured results are in runs.md and the README.

# Walk-in log: apo10x_na045_fov25 (widen 10x/NA0.45 apo from 2.0 mm to 2.5 mm specimen field)

Seed: {SEED} (14 elements, 25 surfaces, stop S4, reversed trace,
RealImageHeight fields, waves e/F/C/g equal weight, EPD 18 mm). Snapshot: a snapshot of the seed as loaded.

## Stage 0 - widen field, strip colour rows (candidate 1, candidate 2 = review answers)
- Fields set to 0 / 0.625 / 0.875 / 1.25 mm real image height.
- Seed merit colour rows REMOVED (not zero-weighted): AXCL x2 (rows 293-294, w 30000),
  REAY x4 (rows 295-298, w 0) and DIFF x3 (rows 299-301, w 1e6) = lateral colour.
- Seed at 2.5 mm field: poly RWCE 0.0174/0.0407/0.0363/0.1272 waves; poly STRH 0.990/0.961/0.964/0.618;
  rim WD 4.925 mm (FAILS >= 5.0 because S23 auto semi-diameter grew).
- Aberration review 1: edge = coma-like tangential S + wavelength-dependent rim hook + T-S astig.

## Stage 1 - re-optimize at the new field (candidate 3)
- DLS 10 cycles -> 50x3 -> 100x5 (stopped: gain_below_threshold). Merit unchanged otherwise.
- poly RWCE 0.0178/0.0460/0.0452/0.0479; poly STRH 0.991/0.936/0.934/0.926; rim WD 5.050 mm, vertex 5.991 mm.
- Vision review: a reviewer finding, looks_tight on [23,24] (working distance).

## Stage 2 - act on the WD finding with margin (candidate 4 = PROMOTED)
- ETGT(S23) rim-WD row retargeted 5.05 -> 5.30 mm; DLS 50x3 (stopped by itself).
- poly RWCE 0.0174/0.0458/0.0454/0.0486; poly STRH 0.991/0.936/0.933/0.925; rim WD 5.299 mm, vertex 6.302 mm;
  EFL 20.0006 mm; RANG 0.466389 rad (NA 0.4497); TOTR 86.33 mm; field sag T 0.82 / S 0.35 um.
- Vision review: silent (recorded as candidate 5). The reviewer finding answered 'acted' on candidate 4.
- Aberration review 2: limiting term at 1.25 mm is chromatic, primary lateral colour
  (F-C -0.533 um); weight test e+C 0.0501 vs e+F 0.0287 waves. Answers recorded as candidate 6.
- Promoted: result_10x-form-step (candidate 4).
