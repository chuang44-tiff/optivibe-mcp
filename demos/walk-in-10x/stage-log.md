# apo10x_lwd — walk-in stage log (2026-10-08)

Seed: seed_BEST_apo60x.zmx (14 elements, EFL 3.338 mm, NA 0.899, WD 0.130 mm, field ±0.183 mm).
Promoted: BEST_apo10x_lwd.zmx (= candidates/zmx/apo10x_lwd_008_final_minair15.zmx).

| Stage | Spec aimed at | Moves | Readings (poly RWCE waves, fields 0/0.5/0.7/1.0) | Cand. | Review |
|---|---|---|---|---|---|
| 0 | setup | scale_lens to EFL 20 (x5.991); EPD 18; RealImageHeight field 0/0.5/0.7/1.0 mm; freed fixed 0.05 mm air gaps + WD | scaled WD 0.780 mm, track 182.6 | — | — |
| 1 | WD>=5, EFL 20, track<=150 | wavefront wizard + floors (air 0.5/glass 1.0), CTGT/CTLT/ETGT on S23, EFFL, OPLT(TOTR) | 0.082/0.116/0.148/0.215; WD 5.38 | seq 1 | 6 findings (acted 5, declined 1) |
| 2 | packaging | track 110 -> 90, max_glass 8, max_air 15, min_air 0.6, CTGT>=2 on S2/S12/S15 | 0.080/0.095/0.123/0.197; WD 5.65 | seq 2 | 1 finding (S16 steep; acted later) |
| 3 | DL at every field | edge reweight (centre got worse 0.080->0.132: reverted weights); AXCL x2; real-ray lateral-colour DIFFs; glass trials: S20->S-FPL53 (no gain, rejected), S22 S-LAH58->S-NPH2 (edge 0.111->0.064, kept); EPD/NA centring; CVLT S16 | 0.0145/0.0234/0.0292/0.0427 | seq 3 | 2 tight-gap findings (acted) |
| 4 | finding response + real ray aiming | max_glass 6, RANG NA pin, CTGT S10/S17>=1.0, real ray aiming (paraxial aiming had overstated NA: 0.4522 vs 0.4395 real), EPD back to 18 | 0.0143/0.0375/0.0387/0.0417 | seq 4 | 2 tight-gap findings (acted) |
| 5 | finding response | min_air 1.0 global | 0.0157/0.0368/0.0382/0.0417 | seq 5 | 2 repeat tight-gap (acted, 1.5 mm) |
| 6 | finding response | CTGT S10/S17 >= 1.5 | 0.0160/0.0375/0.0388/0.0428 | seq 6 | 1 tight-gap (acted at root) |
| 7 | finding response | min_air 1.5 global | 0.0174/0.0388/0.0401/0.0433; STRH 0.988/0.958/0.957/0.929 | seq 8 | silent (recorded seq 9) -> promoted |
