---
name: design-tolerance-desensitize
description: Use as the last finishing stage (stage C), after optimize-loop has built the form (stage A) and design-glass-matching has closed colour (stage B), when the lens meets its nominal spec with margin and the goal is an as-built spec - to find what is sensitive and make the lens less so.
---

# Desensitize: the as-built finishing stage

Stage order is A (form), B (glass for colour), C (this). Start from B's lens at rest. The lens has a **nominal** spec it
already beats and an **as-built** spec it must meet. Stage C spends the margin between them. If the brief gives no
as-built spec, ask for one or state the one you use. Judge on the as-built measure, never on the merit value.

**This stage is ordinary optimization.** optimize-loop's rules hold: bound a variable before you free it, compare only
at rest, check clearance, look at the layout. Length and thickness are costs, as nominal is: a lens made less sensitive
by growing longer or thicker is not a finished lens. The merit you inherit may hold neither (a wizard ceiling of
1000 mm bounds nothing), so read it (`dump_merit_function`) and, before you free a thickness or an air gap, add what
stage A would have held: a ceiling on each one you free (the brief's limit, else near its start value) and on the
track (about the start lens's, unless the brief gives room). State the ceilings you set.

**The as-built measure** is the brief's. The default: the share of builds that meet the as-built threshold at EVERY
field after what assembly can adjust. That is refocus always, plus ONE push (lateral decenter of one element or
cemented group) where the lens is centred at assembly, as a microscope objective is. The threshold is an RMS
wavefront: 0.0714 waves for a diffraction-limited lens, otherwise the brief's, or one you state. A trial whose push
sits at its travel limit counts as a failure. When the brief gives each field its own threshold, pass Quick Yield
`threshold` as a list, one value per field (the worst field is then the one with the lowest pass rate against its own
threshold), and divide each field's row in the judge merit by its threshold so the Monte Carlo reads 1.0 as the limit.

1. **Fix the judge before you move anything.**
   - Budget: the brief's, else the wizard preset for the lens type (microscope `{"vendor": "Generic", "grade":
     "HighPrecision", "element_decenter": 0.010, "element_tilt": 0.0167}`; camera `{"vendor": "Generic", "grade":
     "Precision", "element_decenter": 0.02, "element_tilt": 0.017}`). One push travel for the whole session, from the brief.
   - The judge merit reads the worst field (one RWCE row per field plus a MAXX row) and is separate from the merit
     you optimize on. Use the brief's, else build and save it. `pick_push` and `criterion="merit"` read the LOADED
     merit, so load the judge merit to measure and your working merit to optimize. The picker refuses the wizard's own
     merit (`merit_layout_mismatch`); that is expected.
   - Save the start lens as a candidate. It is the first parent: every keep in step 5 is judged against its parent,
     the last kept lens. Its as-built BEFORE is the brief's figure, else the picker's 200-trial reading (step 2),
     else a step 6 Monte Carlo on the start lens.
2. **Pick the push first, as at assembly.** Skip this step for a refocus-only lens, or when the brief names the push
   group. Otherwise which element to push is your design decision. On the start lens, with the judge merit loaded:
   `tolerance(mode="pick_push", budget=..., cb_group={"decenter_mm": <travel>})`. It shortlists the three top
   decenter/tilt groups, runs a capped MC200 on each (up to 25 min in all) and returns the winner, a near-tie flag
   and a `judge_call`. A near tie means either group will do; take the one ranked first. Use this group for every
   measurement in the session. Re-run the picker only at the end, if the top decenter hotspot has moved to another
   group.
3. **Find the hotspots.** `tolerance(mode="sensitivity", budget=..., criterion="wavefront")` takes seconds. Read
   `hotspots[]` only when `hotspots_withheld` is null. The ranking is refocus-only; how much of each row a push
   absorbs is unmeasured, so treat the ranking as the list to try and let the confirm decide.
4. **One lever per round, chosen by cause.** Say the cause and the lever in one line.
   - **Steep surface, high angle of incidence** (the top row is a tilt or decenter of an element with a strong
     curve): an angle-of-incidence ceiling (`RAID`, first at about 0.8x the current value) on the surface the top row
     names. It is two rows in one `add_math_constraint` call: `RAID` with `relationship="derived"` at weight 0,
     then an `OPLT` with `refs=[<that row's label>]`, `relationship="le"` at your limit.
   - **Thicknesses and air gaps, inside their ceilings,** to win back the nominal a ceiling cost at the edge of the
     field. Keep the refocus gap and any gap the brief fixes as they are.
   - **Glass, only when the brief allows it:** a higher index at a similar Abbe number gives the same power with
     shallower curves. Re-read colour after a keep. It must earn its keep at the confirm like any other lever.
   Do not set a radius by hand: pull it with a row and let the optimizer rebalance.
5. **The worst field decides.** A build passes only when EVERY field passes, so a change that lowers the sensitivity
   by spending the edge field's nominal is a loss, however good the average looks.
   - **Screen cheaply.** After each lever, optimize to rest on the working merit, then read the nominal RMS wavefront
     at every field, `check_clearance` with your ceilings (`max_glass`, `max_air`), the track, and step 3 again. The
     candidate is out before any yield read if a field's nominal is above the headroom cap (the brief's; else set and
     state your own before the first lever, well short of the nominal spec: the worst field's nominal is the margin
     the tolerances need) or a thickness, a gap or the track is over its ceiling: win it back inside the bounds or revert.
     A lever is worth a yield read when the RSS falls by more than a control optimize (no lever) moves it and the
     worst field's nominal has not risen to pay for it. The RSS does not decide a keep.
   - **Look before you pay for a yield read.** A candidate that goes to the confirm is a stage end: save it and run
     `design-vision-review` with its seq, giving the start lens's track and thickest element in the design intent.
     The review is advice and the readings decide, but answer each finding it records. If you act on one, screen
     again.
   - **Confirm with a paired per-field Quick Yield read** when the screened changes add up (or after about three
     rounds). Parent (the last kept lens, reloaded from its candidate file): `tolerance(mode="quick_yield",
     budget=..., pupil=2, threshold=<the as-built threshold>, trials=1000, repeats=2)`. It runs one analysis per
     field and returns `fields` and a `worst_field`. Note the result's `id`. Then reload the candidate and run the
     same call with `baseline=<the parent's id>`. The paired block gives candidate minus parent per field
     (`fields[].d_yield`) and for the worst field (`worst_field`, also the top-level `d_yield`, `d_p90`), each with
     an SE and `resolved`.
   - **Read the parent once.** The server holds the last 4 Quick Yield results across an engine release, so several
     candidates pair against the same parent read. Measure the parent again only when `baseline` is refused as not
     held.
   - Quick Yield is refocus only and reads its own RMS wavefront, not the loaded merit; it does not take the push
     (`cb_group` is refused). Its yield is NOT the as-built yield: compare it only candidate against parent, never
     with a Monte Carlo number or with the target.
   - **Keep** when the worst field's `d_yield` is positive and resolved, `comparable` is true, NO field's `d_yield` is
     resolved negative, and the worst field's `d_p90` is not resolved higher. A resolved loss at any field reloads
     the parent. When `worst_field.selection_uncertain` is true or the worst field is `censored`, the gain is not
     resolved. Anything else is **not confirmed**: you may keep screening from it, but report it as unconfirmed and
     do not make it the parent until a later paired read resolves it. Save every kept lens as a candidate.
   - **Memory.** Each analysis (one per field per repeat) holds about 3.7 GB per 1000 trials until the engine closes.
     After EVERY Quick Yield call: save the lens, `release_engine`, reload it. If a call is refused for memory, lower
     `repeats`.
6. **Closing Monte Carlo.** Run it unless the brief says an outside judge measures the delivered lens; then report
   AFTER as `not measured` and name that judge. When you stop starting rounds: release the engine, reload the lens
   you deliver, load the judge merit and run `tolerance(mode="monte_carlo", compensator="dls_cb_group",
   cb_group={"group": <the step 2 group>, "decenter_mm": <travel>}, criterion="merit", budget=..., trials=1000,
   full=true)`; for a refocus-only lens leave out `compensator` and `cb_group`. Count the yield (trials at or under
   the threshold and not at the push limit) from the per-trial values. It is the as-built AFTER.
   - A Monte Carlo call stops at its time cap (10 s per trial plus 60 s, or `cap_s`) and is then refused as capped,
     with no statistics, so keep that much time for it. If a call fails or returns fewer trials than asked, rerun
     it; do not count a partial run.
7. **Find the hotspots again after every keep.** Desensitizing one element often moves the sensitivity to its
   neighbour. Join the new list to the old by `key` only when `run_summary.topology_digest` is unchanged.
8. **Measure time; do not estimate it.** Note the wall time of every tolerancing call and plan the next round from
   those times and the clock time used, not from a count of calls. Stop when the as-built target is met, when every
   hotspot's levers have been tried, or when the brief's time is spent (less step 6's). Until then take the next
   hotspot or the next lever.
9. **Report.** The start lens and the final lens side by side: the push group, the yield, the P90,
   the nominal at every field, the track and the thickest element, each with the trials it rests on. Then each
   round: hotspot, cause, lever, the nominal at every field, the track, the paired Quick Yield readings (the worst
   field and its `d_yield`, `d_p90` with their SEs, and any field that lost) and decision (kept, rejected, screened
   out, not confirmed). Name the hotspots left untried.

If a glass-clock stop block arrives here, it is the stage-B budget: stage C runs with `OPTIVIBE_GLASS_CLOCK_MODE=off`.
Say so to the user and ask them to set it; do not work around the hook.
