# 20× / NA 0.70 dry, 1.5 mm working distance: both runs, stage by stage

Compiled from the run reports after the runs; not a file the agent wrote. The form-step agent's own log is
[stage-log.md](stage-log.md); the glass-step run wrote none. RMS is the polychromatic RMS wavefront error in waves at
0 / 0.5 / 0.7 / 1.0 field. Lateral colour is the chief ray's image height minus the e line's at the field edge
(0.5 mm), F / C / g. Each run's figures are its own readings.

## Stages

| Step | Run | Stage | What was done | Reading | Outcome |
|---|---|---|---|---|---|
| seed | — | patent rebuild (US 12174359 B2, FIG. 2), nearest catalog glasses, no cover glass | loaded as the start | RMS 11.04 / 11.09 / 11.13 / 11.23; Strehl 0.0126 / 0.0058 / 0.0037 / 0.0027; working distance 1.624 mm at the vertex, 1.330 mm at the rim; lateral colour −0.864 / +1.428 / −1.456 µm | not corrected as loaded |
| form | form run | merit rebuilt | the seed's four REAY and three DIFF colour rows removed | — | no colour rows |
| form | form run | first aberration review | lateral colour (C +0.88 µm) named as the limit at the field edge | — | C stayed between +0.874 and +0.890 µm from here on |
| form | form run | result, promoted | 12 optimize calls, 198.6 s | RMS 0.0598 / 0.1035 / 0.1347 / 0.1908; Strehl 0.902 / 0.697 / 0.549 / 0.433; working distance at the rim 1.613 mm; lateral colour −0.342 / +0.874 / −0.244 µm | Strehl met on axis only; lateral colour over 0.30 µm; 2174.6 s, 264 tool calls |
| between | — | rim-row re-optimization, outside both agent runs | six ray-error rows at the pupil rim (0.5, 0.7, 1.0 field, weight 1e5) added; optimized to rest | RMS 0.0691 / 0.1025 / 0.1301 / 0.1843 | start of the glass step |
| glass | glass run | loaded the rim-row lens | read back | Strehl 0.836 / 0.684 / 0.566 / 0.454; working distance at the rim 1.6186 mm; lateral colour −0.352 / +0.881 / −0.270 µm | start |
| glass | glass run | control: lateral-colour rows added, optimized to rest | — | RMS 0.0705 / 0.1035 / 0.1308 / 0.1844 | control |
| glass | glass run | round 1 and round 2 trials (below) | — | kept S16 S-LAH55VS to S-LAH58: 0.0612 / 0.0999 / 0.1290 / 0.1829 | 1 kept |
| glass | glass run | centre-thickness targets on the negative elements | optimized | RMS 0.0528 / 0.0987 / 0.1299 / 0.1843 | on axis lower as a side effect; 0.7 and 1.0 slightly worse |
| glass | glass run | result, promoted | — | Strehl 0.897 / 0.696 / 0.555 / 0.470; working distance at the rim 1.5739 mm; lateral colour −0.347 / +0.873 / −0.252 µm | Strehl met on axis only; 3196.8 s, 182 tool calls |
| compare | earlier glass run | glass step from the form-step lens itself, without the rim rows | — | kept S1 N-LAK34; RMS 0.0560 / 0.0960 / 0.1256 / 0.1805 | its pupil-rim ray error grew; 3260 s, 148 tool calls |

## Glass step, step by step

| | rim-row lens (start) | control (colour rows added, rested) | after kept swap | final (after centre-thickness step) |
|---|---|---|---|---|
| RMS | 0.0691 / 0.1025 / 0.1301 / 0.1843 | 0.0705 / 0.1035 / 0.1308 / 0.1844 | 0.0612 / 0.0999 / 0.1290 / 0.1829 | 0.0528 / 0.0987 / 0.1299 / 0.1843 |
| Strehl | 0.836 / 0.684 / 0.566 / 0.454 | 0.849 / 0.692 / 0.565 / 0.460 (trial tool, different sampling) | not recorded | 0.897 / 0.696 / 0.555 / 0.470 |
| WD at rim (mm) | 1.6186 | 1.618 | 1.580 | 1.5739 |
| lateral colour F / C / g minus e (µm) | −0.352 / +0.881 / −0.270 | −0.340 / +0.869 / −0.244 | −0.349 / +0.873 / −0.259 | −0.347 / +0.873 / −0.252 |

- **The kept swap,** control to swap: better at every field, 0.0705 to 0.0612 on axis and 0.1844 to 0.1829 at the field
  edge (0.5 mm).
- **The centre-thickness step** gave that edge gain back (0.1829 to 0.1843) and cost a little at 0.7 field. Its on-axis
  drop to 0.0528 was a side effect, not a gain.
- **Final lens, other readings:** focal length 10.0023 mm, NA 0.6998, field sag 0.414 µm, largest centre thickness
  7.9411 mm, minimum glass edge 1.768 mm, minimum air gap 0.5275 mm, total track 66.83 mm.
- **Strehl sampling.** A fresh-load measure_design read of the final lens gives Strehl 0.908 / 0.707 / 0.565 / 0.471;
  the RMS and working distance agree to the digit, so the gap is Strehl sampling, not the lens. Compare on RMS.
- **Rim rows.** In the earlier glass run without them, the tangential e-line rim error at the field edge (0.5 mm) grew
  from 1.24 to 3.46 µm.

## Limiting aberration

The limit is lateral colour, with a second floor at the field edge.

- **At 0.5 and 0.7 field:** the gap is the C-line lateral colour, about +0.87 µm.
- **At the field edge:** with lateral colour set aside, the error of each wavelength is still about 0.078 waves. That
  figure is derived by the agent's arithmetic; a later check got 0.082. The check measured C alone at 0.084 and g alone
  at 0.117 waves.
- **The C-line offset** at the pupil centre, at 0.5 field, went from +0.433 to +0.429 µm across the glass step. No
  reading isolates the part due to glass.

## Glass trials (glass run)

Carriers of lateral colour, from `explain_glass_swaps`: S11 (N-LAF21), S2 (S-NBH58), S16 (S-LAH55VS), S1 (S-LAL59),
S10 (NICF-A), S14 (N-FK58), S5 (N-KZFS5).

Round 1, single swaps. Control as the trial tool read it: 0.0705 / 0.1033 / 0.1307 / 0.1844.

| Swap | RMS | Decision |
|---|---|---|
| S16 to S-LAH58 | 0.0612 / 0.0999 / 0.1290 / 0.1829 | kept: better at every field |
| S16 to S-LAH59 | 0.0770 / 0.1014 / 0.1256 / 0.1791 | rejected: on axis past 0.075, better off axis; C cut to +0.808 µm |
| S2 to N-SF6 | 0.0968 / 0.1205 / 0.1440 / 0.1955 | rejected: g colour cut to 0.11 µm, worse at every field |
| S2 to S-NBH53V | 0.0734 / 0.1163 / 0.1491 / 0.2099 | rejected, worse at every field |
| S11 to S-YGH52 | 0.0847 / 0.1145 / 0.1410 / 0.1947 | rejected, worse at every field |
| S11 to S-LAH97 | 0.0853 / 0.1170 / 0.1435 / 0.1949 | rejected, worse at every field |
| S11 to N-KZFS2, N-KZFS11, N-KZFS4, S-LAL7Q, Q-LASFH12S, N-KZFS8; S2 to S-NBH51, S-NBH59; S16 to J-LASFH9, N-LASF44 | not measured | dropped after one block (15 % to 372 % behind) |

Round 2, sets of two on the kept lens. Control 0.0610 / 0.0998 / 0.1290 / 0.1829.

| Set | RMS | Decision |
|---|---|---|
| S1 S-PHM52 + S2 N-KZFS8 | not measured | dropped (28 % behind) |
| S1 S-LAH66 + S2 N-SF6 | 0.0784 / 0.1151 / 0.1436 / 0.1963 | rejected, worse at every field |
| S2 N-SF6 + S16 S-LAH59 | 0.1014 / 0.1202 / 0.1412 / 0.1922 | rejected: C cut from 0.873 to 0.767 µm, worse at every field |
| S5 N-KZFS4 + S6 S-FPL53 | 0.0787 / 0.1118 / 0.1388 / 0.1907 at the time budget | rejected: unfinished, trailing its control at every field |

Stop: the best swap moved the field edge 0.0015 waves of the roughly 0.11 needed. Trials took 1409.7 s, 44.1 % of the
run. On this run the server refused three trial calls for catalog binding.
