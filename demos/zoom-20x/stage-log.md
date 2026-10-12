# zoom20x_refine — stage log

Seed: `seed_BEST_zoom20x_pana5g.zmx` (3 configs: C1 wide 4.5 mm f/1.7, C2 mid 20.12 mm f/2.35, C3 tele 90 mm f/3.0;
field = paraxial image height 0 / 1.55 / 3.1 mm; primary wave = F 0.4861 um; C1 field 2 carries vignetting factors).
Metric: polychromatic RMS spot radius, centroid-referenced (RSCE, ring 6, wave 0), um. In-loop readings are merit-row
values from `dump_merit_function`; the seed and final rows come from the dedicated tools (`get_operand`,
`analyze_lateral_color`, `analyze_distortion`, `get_first_order`, `check_clearance`).

## Seed (candidate seq 1) — measured

| config | hy 0 | hy 0.5 | hy 0.8 | hy 1.0 | lat. colour max (um) | distortion max (%) |
|---|---|---|---|---|---|---|
| C1 | 9.07 | 20.08 | 19.80 | 18.36 | 17.46 | 30.34 |
| C2 | 8.73 | 6.97 | 7.17 | 14.21 | 1.196 | 8.44 |
| C3 | 6.78 | 16.44 | 25.55 | 32.30 | 68.24 | 0.75 |

Worst 0.8-field position: C3 (tele). Single-wave RSCE at C3 hy 0.8 = 7.49 / 7.95 / 15.13 um vs poly 25.55 -> about
23 um of inter-wave spread: lateral colour limits it. Seed already fails: distortion C1 30.3 % (target 4 %), S23 glass
edge 0.957 mm (floor 1.0), filter plates S27/S29 0.35/0.5 mm thick (sensor stack, left alone).
Aberration review 1: review_1/fan_reply.md + review_2/fcd_reply.md; answers recorded on seq 2.

## Stages

| stage | move | C1 0.8 | C2 0.8 | C3 0.8 | LC C1/C2/C3 | DIMX C1 | candidate |
|---|---|---|---|---|---|---|---|
| opt1-2 | S23 1.0->1.06 mm; append per-config lateral-colour (REAY F-C) rows, C1/C2 caps, DIMX caps, RSCE 0.8 rows, MNEG floor | 19.50 | 6.95 | 22.86 | 15.3/1.00/60.1 | 30.3 | trail only (on-axis and C2 hy0.5/1.0 got worse than seed -> not kept) |
| opt3 | 4x heavier tele colour weights | 18.12 | 7.20 | 24.14 | 14.0/1.00/51.7 | - | REJECTED: on-axis C3 6.78->13.08, C2 hy0.5 6.97->10.11 |
| opt4 (inc1) | reload opt2; add "no worse than seed" RSCE caps at hy 0/0.5/1.0 in every config | 19.51 | 6.76 | 21.89 | 15.2/1.01/60.4 | 30.19 | seq 3 |
| opt5-8 (inc2) | +22 variables (A8 on 4 aspheres, 7 intra-group air gaps, 11 glass thicknesses), MNCG/MXCG bounds, floors 1.02/1.03 | 19.50 | 7.01 | 20.36 | 14.4/1.01/58.5 | 29.67 | seq 4 |
| glass 1 | S6 N-SK14->S-FPM2, S8 S-LAH63->N-LASF44 (measured: each alone throws tele axial colour ~+-141..146 um, the pair cancels) | 19.51 | 7.01 | 17.43 | 11.8/1.00/50.9 | 29.84 | trail |
| glass 2 | S4 N-SK14->S-FPM2, S10 S-LAH63->N-LASF44 | 19.50 | 7.01 | 15.52 | 10.2/1.00/44.8 | 30.03 | trail |
| glass 3 (inc3) | S2 N-SK5->S-FPM2 | 19.02 | 7.01 | 13.68 | 9.0/1.00/40.8 | 29.42 | seq 5 |
| glass 4 (inc4) | S1 and S13 N-SF6->S-NPH1 (250 cycles) | 18.88 | 7.04 | 12.44 | 5.1/0.50/26.6 | 30.00 | seq 6 |
| opt9-12 (inc5) | C1 becomes worst -> drive C1 RSCE hy 0.5/0.8/1.0; +A10 on 4 aspheres (77 variables) | 15.16 | 7.04 | 12.09 | 5.2/0.45/27.0 | 29.90 | seq 7 |
| dist 27 (inc6) | DIMX cap C1 30.3 -> 27 (about 500 cycles to re-converge; at 100 cycles C2 0.8 was 8.57, at 350 7.23) | 12.48 | 7.03 | 10.94 | 5.4/0.98/26.3 | 27.00 | seq 8 |
| dist 24 (inc7) | DIMX cap C1 27 -> 24 (at 600 cycles C3 0.8 was 13.55 and C2 0.8 7.24; recovered by 1000-1400 cycles); tele-edge weight 3 -> 6 | 10.68 | 7.04 | 10.70 | 5.5/1.00/26.4 | 24.01 | seq 9 |
| final | C2 RWCE(0.8) had risen 0.641 -> 0.689 waves; cap it at 0.63 and re-converge (200 cycles) | 10.07 | 7.01 | 10.25 | 5.5/1.00/26.4 | 24.01 | seq 10 (reported design) |
| evidence trial | tele lateral-colour weights 0.5 -> 2.0 on the final glass set, 200 cycles: LC C3 26.43 -> 26.30 um; C3 0.8 10.25 -> 9.96, C1 0.8 10.07 -> 9.95, C2 hy1.0 9.86 -> 10.06 | 9.95 | 7.01 | 9.96 | 5.5/1.00/26.3 | 24.00 | seq 13 (not reviewed, not reported) |

Glass influence measurements (no optimisation, from inc2 state, hy 1.0 lateral colour F-C and AXCL, um):
- S8 S-LAH63->N-LASF44: dLC = +3.5 / +6.2 / +6.2, dAX = +3.7 / +18 / +146 (C1/C2/C3)
- S6 N-SK14->S-FPM2:   dLC = -0.2 / -4.9 / -13.0, dAX = -0.2 / -5.5 / -141

## Final (candidate seq 10) — measured

| config | hy 0 | hy 0.5 | hy 0.8 | hy 1.0 | RWCE 0.8 (waves) | lat. colour max (um) | distortion max (%) | EFL (mm) |
|---|---|---|---|---|---|---|---|---|
| C1 | 2.39 | 10.40 | 10.07 | 15.43 | 1.209 | 5.49 | 24.01 | 4.50009 |
| C2 | 6.90 | 6.50 | 7.01 | 9.86 | 0.630 | 1.002 | 4.67 | 20.12001 |
| C3 | 6.74 | 7.64 | 10.25 | 13.55 | 1.113 | 26.43 | 0.67 | 89.99999 |

Glasses read back on seq 10: S1 S-NPH1, S2/S4/S6 S-FPM2, S8/S10 N-LASF44, S12 N-SK2, S13 S-NPH1, S16 N-SK2, S18 N-SF6,
S20 N-SF8, S21 N-SK2, S23 N-LAF34, S25 N-SF10. 32 surfaces, element count unchanged.

Clearance (check_clearance, all configs, min_air 0.3 / min_glass 1.0): only S27 (0.35) and S29 (0.50) flagged — the
untouched sensor-stack plates. Thinnest lens glass edge 1.0245 mm (S23, C1); thinnest air 0.3977 mm (S14->S15 edge, C3).
Total track 86.2999997 / 86.3000017 / 86.3000047 mm.

On-axis best focus minus the fixed 3.5 mm back gap, F/d/C (analyze_wavefront), um:
seed C1 +6.5/+12.6/+30.4, C2 -27.0/-36.1/-18.2, C3 -36.9/-65.4/+28.1;
final C1 +8.8/+1.0/+12.9, C2 -1.9/-9.6/+8.8, C3 -39.0/-45.8/+30.8.

Reviews on seq 10: aberration review 2 (review_4/fan_reply.md, review_4/fcd_reply.md; answers recorded on seq 12);
vision review, two rounds, silent (review_6/reply_raw.json, review_7/reply_raw.json; silence recorded on seq 11).

## Promotion

`promote_best(design_name='zoom20x_refine', seq=10, min_air=0.3, min_glass=1.0)` was REFUSED
(`promote_clearance_violation`): "REFUSED: surface 27 glass clearance 0.35 < 1.0 mm in config 1 is manufacturably
thin (+5 more); pass force=True WITH judgment={'reason': ...} to promote anyway, or widen the gap (check_clearance for
the full audit)". Not forced, thresholds not lowered. No BEST_zoom20x_refine.zmx was written; the reported design is
`candidates/zmx/zoom20x_refine_010_final_dist24_rwce_cap.zmx`.

## Not met / what limits it now

- Wide distortion 24.0 % against the 4 % target (seed 30.3 %). The walk 30.3 -> 27 -> 24 was stopped on budget, not at
  a measured limit: each step ended with every sampled spot at or below the seed, but the 24 % step needed about
  1400 cycles to recover the tele.
- Tele lateral colour 26.4 um: a 4x weight raise moved it 0.13 um (seq 13), so weights are exhausted on this glass set;
  it only moved when glass changed.
- Tele 0.8 field is now mixed: about 8 um monochromatic and 6 um inter-wave spread (C line 10.6 um against F/d 6.4-6.5).
- Wide: tangential field turn-back and edge astigmatism (-0.119 mm), rim spike in the 1.55 mm tangential fan.
