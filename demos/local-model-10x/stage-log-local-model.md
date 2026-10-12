# Walk-in stage log — 10x/0.45 long-WD objective (rework of 60x/0.9 apo seed)

## Target
- EFL 20mm ±1% (10x on 200 tube), NA 0.45 ±0.02 (real marginal ray, object air)
- WD (last optic surface -> specimen) >= 5.0 mm vertex AND rim
- 2 mm FOV (specimen half-heights 0 / 0.5 / 0.7 / 1.0 mm), DL: polychromatic Strehl >= 0.80 (RWCE <= 0.075λ) at ALL 4 fields
- Field sag <= 5 um; infinity-corrected, dry, catalog glass
- Element count fixed (14 elements, 25 surfaces); seed cemented joints stay (surfaces 2, 6, 9, 12, 15, 16)
- min glass edge >= 1.0 (promote gate audits centre >= 1.0), min internal air >= 0.5, no zero-thickness air between powered surfaces
- DLS/OD local optimization only (NEVER Hammer).

## SEED (BEFORE) — measured 2026-10-11, this session, all tools named
- load_design: seed_BEST_apo60x.zmx, 25 surfaces, load_verified=geometry, 35 inherited variables, 1 config.
- Trace direction REVERSED: object at infinity (tube side), image = specimen plane (surf 24 'IMAGE specimen'), WD = gap surf23->24.
- get_first_order: EFL 3.33835 mm (60.0x @200mm), WFNO 0.55614, PIMH 0.18372 mm, track 30.483 mm, BFL/WD 0.13022 mm (surface 23 gap, 'WD to specimen').
- get_system_info: aperture EPD 6.0 mm, 3 fields, 4 wavelengths.
- Field table (render_field_curvature_distortion): Angle fields 0 / 1.575 / 3.15 deg; real spec. height max 0.1836 mm; distortion max -0.30%; field curv sag 0.454 um.
- Wavelengths: 0.54607 (primary), 0.48613, 0.65627, 0.43583 um.
- NA real marginal ray (trace_rays Py=1 to surf 23): sin(theta)=0.89906 -> NA 0.899.
- Element count: 14 glasses (S-BAH28, S-PHM52Q | S-NBH52+CAF2 | CAF2+N-KZFS11 | CAF2+N-KZFS11 | CAF2+N-KZFS11+S-FPL51Y(triplet) | N-PSK58 | N-PSK58 | S-LAH58). Cemented joints at surf 2,6,9,12,15,16. Thin AIR gaps 0.05 mm at surf 7,10,13,17,19,21 (widened to >=0.7 in stage A).
- Strehl at image plane (analyze_strehl, per wave w1/2/3/4):
  - f1 (axis): 0.955 / 0.828 / 0.844 / 0.979 (headline wave1 0.955)
  - f2 (1.575deg): 0.902 / 0.798 / 0.793 / 0.947
  - f3 (3.15deg): 0.777 / 0.716 / 0.659 / 0.816
- Polychromatic STRH (weight-0 wave-0 STRH rows, dump_merit_function), at the seed's 3 fields (axis / mid / edge of the seed's 0.184mm field): 0.911 / 0.830 / 0.627.
- analyze_axial_color (F/C): 1.03 um.
- Merit: 265 rows inherited (wizard bounds + OPDX blocks + tail: EFFL 3.333 w30, REAY w12 x2, RWCE w3/5 x4, AXCL w8, CVLT 0.17 w15), merit 0.0217.
- Seed vs NEW targets: WD 0.13 mm << 5; NA 0.899 vs 0.45; field 0.184 vs 1.0 mm; 16 gaps violate my floors (seed used 0.5-centre thin negatives, 0.05 air gaps).

## Stage plan (per optimize-loop walk-in)
0. Record seed; snapshot pristine.
A. Geometry: scale to EFL 20; EPD 18 (=>NA 0.45); fields 0/0.5/0.7/1.0 mm height; ray aiming real; widen 0.05 gaps to ~0.6-0.7; WD gap variable; CTGT/CTLT/ETGT rows surf 23 w~40; EFFL row w~50.
B. Packaging: TOTR+OPLT track ceiling walk-down with 1.3x stop rule; after every step: 4-field RWCE + surf-23 radius.
C. Performance: measure-first rows (field weights/glass trials/lateral colour); 50xN DLS runs; analyze_wavefront 4 fields after every run; STRH reads via weight-0 rows at stage ends.
D. Margins: check_clearance, rim WD exact-sag, field sag, NA, vision + aberration reviews, promote.

## Stage 0 — seed recorded, snapshot 'seed60x_pristine' (0000), poly STRH 0.911/0.830/0.627.

## Stage A — geometry re-target (snapshot 0001_stageA)
- scale_lens to_efl 20 (factor ~6), set_aperture EPD 18, set_field RealImageHeight [0,0.5,0.7,1.0], set_ray_aiming real (ENPP displaced).
- Widened the six 0.05 mm air gaps (surf 7,10,13,17,19,21) to ~0.7; WD gap (surf23) made variable, pre-set ~5.8; never frozen.
- EFFL row t20 w50; WD block: CTGT t5.2 w40 (Surf23), CTLT t8.0 w40, ETGT t5.3 w40.
- After stage A: EFL on target, WD gap ~5.8, geometry far from DL (expected).

## Stage B — packaging walk (snapshots 0002-0004, candidates seq 1 stageC_start_perf1 + seq 2 aberration answers)
- TOTR(t0,w0 as measured operand) + OPLT t120 w1 {Op#:TOTR-row}; track ceiling walked DOWN 120 -> ... -> current with the 1.3x stop rule (stop when ceiling within 1.3x of achieved track).
- build_merit criterion=wavefront rings 6 arms 6, glass=true air=true, min_air 0.7 / min_glass 1.2, max_air 6 / max_glass 5, overall_weight 5 -> 399 wizard rows (incl. 14 ETGT edge floors w10@1.44, MNCA w1@0.7, MXCA/MXCG ceilings).
- design-aberration-review run on the packaged form (two foreground reviewers, fan + FCD; review_1/ folder). Suggestions answered (candidate seq 2 judgment).
- End of B: track 66.16 mm final, packaging rows parked; perf runs begin.

## Stage C — performance (perf1..7 then glass/weight trials; snapshots 0005..0009)
Merit architecture carried through: 399 wizard + tail rows EFFL(20,50), CTGT(5.2,40,S23), CTLT(8.0,40,S23), ETGT(5.3,40,S23), TOTR(0,0), OPLT(120,1). Poly STRH via weight-0 wave-0 STRH rows at stage ends ONLY (add, dump, grep, remove before any optimize). Merit values NOT comparable across rebuilds/loads; STRH is the grading currency.

- perf1-5 (DLS 50x2-4, run dirs candidates/trail/perfN_*): poly Strehl at 4 fields climbed to 0.9848 / 0.9133 / 0.8516 / 0.7321 (dump 1791715821673). Edge (1.0mm) is the limiter.
- perf6/perf7 REAY-attempt FAILURE, recorded: REAY rows at DEFAULT Surf read garbage in the MFE (-0.068 / -0.0); the "improvement" was fake, poly Strehl COLLAPSED 0.985->0.668 (dump 1791715764442), plus an unphysical S3->S4 air centre -0.404 mm. Rolled back to snapshot 0006; rows removed. get_operand('REAY') also reads 0.0 in this infinity-object system.
- Glass trial 1 (snapshot 0007): S16 S-FPL51Y -> HOYA FCD1 (nd 1.49700, vd 81.61, pgF 0.53885, catalog-matched drop-in). trial1/1b: edge 0.7321 -> 0.7480 -> 0.7573 (dumps ...68182, ...72725). KEPT.
- Field weight: edge field (idx4) weight 1->3->6 via set_field(index=4) (vignetting preserved), merit rebuilt (first rebuild dropped glass=true -> rebuilt correctly 316->399 rows, tail re-added by hand; preserve_custom boundary is LOST across load_design).
- trial2/3/4 (edge W6, trail runs trial2_edgeW6..trial4_edgeW6c): 0.7715 (…948852) -> 0.7838 (…966839) -> 0.7849 (…997624, EFFL 20.0007, CTGT 5.2 exact). Plateau.
- trial5 S15 N-KZFS11 -> S-NBM52: ALL fields worse (0.9161/0.8709/0.8191/0.6732, dump ...025758). DECLINED; rolled back — note the rollback lesson: snapshots predate the trial; the correct restore point was the optimize artifact candidates/trail/trial4_edgeW6c/0002_pass01_after.zmx (verified byte-exact 0.7849 re-read, dump ...046117).
- MEASURE-FIRST diagnosis at the 0.7849 plateau: analyze_wavefront per-wave RWCE at edge all <= 0.053λ (w1 0.027/w2 0.035/w3 0.035/w4 0.053) yet poly only 0.785 -> the deficit is PSF-CENTRE MISREGISTRATION (lateral colour), not wavefront shape. trace_rays chiefs at edge: w2 -0.174um, w3 +0.52um, w4 +0.016um.
- REAY fixed properly: lookup_operand shows REAY cell layout; MFE rows with EXPLICIT Surf=24 read the true chief height (probe value 1.0005199311773918 == trace_rays exactly). Authored 9 spec-pinning REAY rows (Hy in {0.5,0.7,1.0} x Wave in {2,3,4}), params {'Surf':24,'Wave':w,'Hy':y}, weight 300 -> 3000 (mu-scale residuals need w~3000 for parity; contribution 0.438 vs ~0.25/wavefront row).
- trial6/7 latcolor300/3000: 0.7898 -> 0.7921. Residual red chief offset +0.506um barely moved -> lateral colour at the edge is GLASS-LOCKED in current glasses (weights done: 10x raise barely moved it).
- Glass trial 3 (THE WIN): S20 N-PSK58 (nd 1.569, vd 71.2, pgF 0.5440) -> HOYA PCD51 (nd 1.593, vd 67.0, pgF 0.5366; PgF shift -0.0075 on the last positive singlet near the specimen where chief/marginal ratio is high). trial8: 0.9758/0.9378/0.9074/0.8261 (dump ...186846) — ALL 4 FIELDS PASS >= 0.80. KEPT.
- trial8b/trial9 continues: 0.9759/0.9396/0.9098/0.8307 -> FINAL STAGE C 0.9767 / 0.9423 / 0.9114 / 0.8344 (fields 0/0.5/0.7/1.0 mm; dump ...230558). Edge margin +0.034.
- Glass verdicts: S16 FCD1 KEPT (+0.016 edge), S15 S-NBM52 DECLINED (all worse), S20 PCD51 KEPT (+0.041 edge, pass-breaker). One surface at a time, snapshot before each, all 4 fields measured after each.

## Stage D — margins and final measurements (candidate seq 3 'apo10x_lwd_003_stageC_final', artifact 63f897dd..., png 63fd56c2...)
- STRH rows removed (merit 414 rows clean) BEFORE the clearance audit.
- check_clearance (max_air 6, max_glass 5): ZERO violations vs spec floors (min glass EDGE 1.449 @11->12; min air centre 0.696 @19->20; min air edge 0.709 @3->4). Three cement-element CENTRES at 1.199 (>=1.0 spec floor; 0.001 under my own 1.2 optimizer floor — disclosed, spec passes). Back airgap: vertex WD 5.9897 mm, rim (exact geometry, edge_approximate:false) 5.9369 mm — both >= 5.0.
- Center-ceiling audit: four glass centres sit 0.0005-0.0038 mm over my own MXCG 5.0 ceiling — converged on their live boundary operands; not a spec item; no action (declined with that measurement).
- get_first_order: EFL 20.0005 (spec 20 +/-0.2), WFNO 1.11164, PIMH 1.00000, total track 66.155 mm, BFL 5.9897. ray_aiming_recommended flagged -> real aiming already set.
- NA real marginal ray (trace_rays Py=1 to surf 23): w1 |M| 0.44979, w2 0.45049, w3 0.44889, w4 0.45120; cross-check 1/(2*|M|) == WFNO exactly (1.11163 vs 1.11164). Spec 0.45 +/-0.02: PASS.
- render_field_curvature_distortion (evaluation/field_curvature_0003): primary-wave field sag 0.99 um sagittal / 0.628 um tangential over full field (spec <=5 um). Worst wave-pair spread at the edge ~3.5 um, still <=5. Distortion F-TanTheta max -0.168% barrel. Engine cross_check agrees to 4.9e-9 mm; no nulls.
- describe_surfaces final: 25 surfaces, 14 glass elements, stop surf 4 (unmoved), cement joints at 2/6/9/12/15/16 intact incl. the 3-element CaF2 triplet (14/15/16: CAF2|N-KZFS11|FCD1). Glasses final: S-BAH28, S-PHM52Q, S-NBH52, CAF2 x4, N-KZFS11 x3, FCD1(S16, ex S-FPL51Y), N-PSK58(S18), PCD51(S20, ex N-PSK58), S-LAH58. All real catalog names (HOYA catalog auto-loaded for the two substitutions, disclosed).
- analyze_axial_color (F/C): 2.489 um (seed 1.03 um at its 0.184 mm field; band 0.436-0.656 spread 4.19 um per FCD figure) — not a stated spec; recorded.
- REVIEWS ON PROMOTED STATE (candidate seq 3 = promoted bytes 63f897dd):
  - design-vision-review (review_4/reply_raw.json): stamps 1-24 all read, 0 unreadable. TWO findings, both looks_tight, both RECORDED (record_findings ids e1ac8011076e5bd2 [3,4], 6d6bb8a7c05a1012 [23,24]) and DECLINED with measurement (gap 3->4 centre 3.625/edge 0.709 vs neighbours 3.145/0.698 — the eye read the rim approach of R11.66 to the flat stop; WD vertex 5.9897 / rim 5.9369 vs >=5.0 spec, longest gap in the design, cone = spec NA 0.45). Judgment recorded: candidate seq 4, disposition declined, receipt read_state ok. NO HUMAN applied (agent steered).
  - design-aberration-review re-run on final state (review_3/: fan_c1.png f9665f0d + fcd_c1.png 1ed530c9, both config 1, waves [1,2,3,4], primary 1; fan_reply.md, fcd_reply.md). MERGED PICTURE: the residual budget sits in the long-wave marginal zonal term (on-axis edge fan 2.896 um red vs 0.263 um blue-green; at field 1.0 one-sided tangential +8.210 um w3 at L=+1.0 confined to L>=0.90, sagittal only 2.478) plus a 4.19 um inter-wave axial spread; field flatness (<=1.2 um/wave vs 5 um) and distortion (-0.16755% achromatic) clean. LIMITING COMBINATION at the 1.0 mm edge: red marginal oblique zonal term — exactly what the FCD1/PCD51 glass moves and edge field weight 6 were driving; measured result on these bytes: edge poly Strehl 0.8344 >= 0.80. All 7 suggestions answered in the seq-4 judgment block (acted-earlier x1, agreed x1, declined-with-measurement x5).
- PROMOTION: promote_best(apo10x_lwd, seq 3) — ok. BEST_apo10x_lwd.zmx + BEST_apo10x_lwd.png published; identity_proven digest_match; clearance_ok true (config=all, 0 violations, candidate_record audit); finding_gate 2/2 answered, verdict finding_answered; no force, no refusals. First promote of seq 4 published the .zmx but best_png_reason no_paired_png (seq 4 saved with render=False per the answer protocol) — re-promoted seq 3 (same bytes, paired png, png_identity digest_proven).
