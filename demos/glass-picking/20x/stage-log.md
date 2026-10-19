Internal IDs and file names removed; otherwise as the agent wrote it. Numbers here are the agent's own readings during the run; the measured results are in runs.md and the README.

# Walk-in stage log — dry20x_na070_wd15 (2026-10-17)

Seed: {SEED} (10 elements; glasses fixed).
Promoted: result_20x-form-step = candidate 7.

## Stage 0: seed read-back
- EFL 10.023 mm; real NA 0.698 (EPD 14 mm); field RealImageHeight 0/0.25/0.35/0.5 mm; waves e/F/C/g (e primary); 10 elements.
- Poly RWCE 11.04/11.09/11.13/11.23 waves; poly STRH 0.0126/0.0058/0.0037/0.0027.
- WD 1.624 mm at the vertex, 1.330 mm at the S17 rim (rim fails 1.5 mm).
- Lateral colour at the edge: F -0.864, C +1.428, g -1.456 um.
- Centre thickness over 8 mm: S14 11.93, S16 8.41, S10 8.28. Air gaps under 0.5 mm: S7/S9 0.154, S13/S15 0.205. S4->S5 edge -2.22 mm.

## Stage 1: NA 0.75 form work (candidates 2, 4)
- Presets: S10/S14/S16 thickness to 7.9 mm; S7/S9/S13/S15 to 0.53 mm; stop gap T3 5.4 / T4 3.3; EPD 15 mm.
- Merit v1: wizard wavefront (OPDX) block, floors (air 0.53, glass 1.05, MXCG 7.95), EFFL, ETGT S17 >= 1.55 (rim WD), TOTR/OPLT 99, per-field poly RWCE.
  The seed's REAY x4 and DIFF x3 rows were REMOVED by this rebuild.
- Test: poly RWCE sees lateral colour (edge 0.687 vs single-wave RMS 0.348 at seed best focus), so poly RWCE is the lever.
- Rest (candidate 2): RWCE 0.067/0.105/0.138/0.206. Raising the edge weight from 3 to 10 left the edge at 0.199 and pushed the axis to 0.088 (flat).
- Aberration review 1: lateral colour (C +0.88 um) limits the edge field.
- Merit v2: OPDX block removed; floors weight 30; EFFL weight 100; soft ETGT 1.8 mm at S6/S8/S10/S12 (a reviewer finding).
- Probes: MXCG relaxed to 12 mm left thicknesses at 7.95 mm and the edge 0.207 -> 0.204 (ceiling not limiting); OD flat.
- At rest (candidate 4): RWCE 0.0646/0.1099/0.1434/0.2076; WD rim 1.537 mm; NA 0.749; lateral colour F -0.358 / C +0.890 / g -0.281 um.

## Stage 2: step-down to NA 0.70 (candidate 7, promoted)
- EPD 14 mm, real NA 0.6999; re-optimized to rest.
- RWCE 0.0598/0.1035/0.1347/0.1908; poly STRH 0.902/0.697/0.549/0.433.
- WD rim 1.613 mm (vertex 2.020); lateral colour F -0.342 / C +0.874 / g -0.244 um.
- Trial: stop moved to mid-gap and re-optimized; lateral colour unchanged (C +0.877), so reverted to candidate 7.

## Limit
Edge-field image quality is limited by lateral colour, mostly primary F-C (-1.22 um real) with a secondary part.
It is invariant to weights, merit structure, the thickness ceiling, the algorithm and the stop position.
Per-element (g-F)/(F-C) ratios span only 0.534-0.601, so the secondary part needs a glass change, which is not allowed.

## Vision findings
- A reviewer finding (too_thin S8-S9, candidate 2): acted.
- A reviewer finding (steep_bend S17, candidate 4): declined.
- A reviewer finding (looks_tight S4-S5, candidate 7): declined.
