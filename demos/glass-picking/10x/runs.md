# 10× / NA 0.45, 2.5 mm field: both runs, stage by stage

Compiled from the two runs' final reports after the runs; not a file the agent wrote. The form-step agent's own log
is [stage-log.md](stage-log.md); the glass-step run wrote none. RMS is the polychromatic RMS wavefront error in waves
at 0 / 0.5 / 0.7 / 1.0 field. Each run's figures are its own readings.

## Stages

| Step | Run | Stage | What was done | Reading | Outcome |
|---|---|---|---|---|---|
| seed | — | walk-in-10x result at its own 2 mm field | loaded as the start | RMS 0.0174 / 0.0388 / 0.0401 / 0.0433; Strehl 0.990 / 0.964 / 0.960 / 0.935; working distance at the rim 5.049 mm | meets the 2 mm brief |
| form | form run | field widened to 2.5 mm, no other change | read back before any change | RMS 0.0174 / 0.0407 / 0.0363 / 0.1272; Strehl at the edge 0.618; working distance at the rim 4.925 mm | edge field and working distance fail |
| form | form run | first aberration review (ray fan and field curvature) | acted on an on-axis guard and a refocus toward the medial surface; declined a glass swap (glass fixed by the brief) | — | answers recorded with the candidate |
| form | form run | layout review | reviewer flagged the working distance as tight; rim working-distance row retargeted from 5.05 to 5.30 mm | — | acted |
| form | form run | second layout review and second aberration review | layout review silent; colour-row suggestions declined (no colour rows by the brief) | — | answers recorded |
| form | form run | result, promoted | — | RMS 0.0174 / 0.0458 / 0.0454 / 0.0486; Strehl 0.991 / 0.936 / 0.933 / 0.925; working distance at the rim 5.299 mm; total track 86.328 mm | every target met; 1382 s, 230 tool calls |
| glass | glass run | loaded the form-step result | read back before any change | RMS 0.0174 / 0.0458 / 0.0454 / 0.0486; Strehl 0.9886 / 0.9328 / 0.9284 / 0.9230 | start |
| glass | glass run | trials (below) | 22 swaps by the agent's count | — | none kept |
| glass | glass run | layout reviews | stop air gap flagged as tight: one soft edge-thickness target added. A pinched-beam finding and a thick-front-pair finding declined with measurements (Petzval sum 0.00035 /mm; centre thickness 5.25 mm under the 6.0 mm ceiling) | — | one acted, two declined |
| glass | glass run | result, promoted | no glass changed | RMS 0.0189 / 0.0452 / 0.0453 / 0.0488; Strehl 0.9867 / 0.9349 / 0.9293 / 0.9222; working distance at the rim 5.2995 mm; total track 86.162 mm | every target met; 4665 s, 238 tool calls |

## Other readings

- **Strehl sampling.** The two runs sampled Strehl differently. The form-step result reads 0.991 / 0.936 / 0.933 / 0.925
  in the form run and 0.9886 / 0.9328 / 0.9284 / 0.9230 in the glass run. Their RMS readings agree.
- **Delivered glass-step lens:** focal length 20.0008 mm, NA 0.44966, total track 86.162 mm, minimum glass edge
  1.4392 mm, minimum air gap 1.4976 mm, field sag S / T 0.375 / 0.900 µm (1.18 µm at worst over wavelengths); targets
  ≤ 5 µm field sag, ≤ 100 mm track, ≥ 1.0 mm edge, ≥ 0.5 mm gap.
- **Secondary colour, loaded to delivered:** g-line RMS at 0.5 field 0.0492 to 0.0479 waves; on axis, g focus minus
  the mean of F and C −0.345 to −0.362 µm.

## Limiting aberration

The limit is chromatic. The e line alone stays at about 0.032 waves or less (0.0323 at most).

- **At the field edge:** primary lateral colour.
- **At 0.5 and 0.7 field:** the change of aberration with wavelength. At 0.5 field, the g line's own error is 0.048 waves; the
  others' errors are 0.019 to 0.036.
- **On axis:** the g line, which focuses about 0.36 µm from F and C.

The agent's evidence that the lens was at rest:

- control re-optimizations repeated to about 1e-5 waves;
- an optimizer pass on the loaded lens gained 0.0067 % and stopped.

The S18 + S20 N-PK51 set shows the trade. It lowered the g line at 0.5 field from 0.0492 to 0.0459 waves, but e, F
and C rose, and the polychromatic value stayed level.

## Glass trials

Carriers of axial colour, from `explain_glass_swaps`: S15 (N-KZFS11), S1 (S-BAH28), S5 (S-NBH52), S9 (N-KZFS11),
S8 (CaF2), S2 (S-PHM52Q).

| Set | RMS | Decision |
|---|---|---|
| control, under an earlier merit | 0.0164 / 0.0422 / 0.0419 / 0.0539 | — |
| S15 to N-KZFS4 | 0.0176 / 0.0435 / 0.0444 / 0.0584 | rejected, worse at every field |
| S15 to NBF1 | 0.0184 / 0.0441 / 0.0463 / 0.0586 | rejected, worse at every field |
| S15 to S-LAH66 | 0.0204 / 0.0489 / 0.0500 / 0.0631 | rejected, worse at every field |
| S15 to N-KZFS5 | 0.0231 / 0.0518 / 0.0520 / 0.0640 | rejected, worse at every field |
| S1 to N-KZFS5, LASFN15, H-ZLAF68N, S-NBH52; S5 to N-KZFS11, N-KZFS5, N-KZFS4, KZFSN5; S9 to N-KZFS4; S8 to FCD100 (+146 %); S2 to N-PSK57; S16 to S-FPL53; S9+S12+S15 to NBF1; S9+S12+S15 to N-KZFS4; S5 N-KZFS11 + S15 NBF1 | not measured | dropped after one block |
| control, loaded lens | 0.0174 / 0.0458 / 0.0454 / 0.0486 | — |
| S5 to N-KZFS5, by hand to rest | 0.0179 / 0.0462 / 0.0466 / 0.0504 | rejected, worse at every field |
| S16 to S-FPL53, by hand to rest | 0.0175 / 0.0461 / 0.0450 / 0.0489 | rejected, within 0.0004 of the control |
| S18 + S20 to N-PK51, by hand to rest | 0.0175 / 0.0460 / 0.0454 / 0.0488 | rejected, not better; g at 0.5 field 0.0492 to 0.0459 |

One trial round was invalid because a trial lens was left loaded; it was discarded and rerun.
