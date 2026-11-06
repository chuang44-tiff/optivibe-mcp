---
name: design-tolerance-desensitize
description: Use as the last finishing stage (stage C), after optimize-loop has built the form (stage A) and design-glass-matching has closed colour (stage B), when the lens meets its nominal spec with margin and the goal is an as-built spec - to find what is sensitive and make the lens less so.
---

# Desensitize: the as-built finishing stage

Stage order is A (form), B (glass for colour), C (this). Start from B's lens at rest. The lens has a **nominal** spec it
already beats and an **as-built** spec it must meet. Stage C spends the margin between them. If the brief gives no
as-built spec, ask for one or state the one you use. Judge on the as-built measure, never on the merit value.

**The as-built measure.** Microscope objective: the share of builds diffraction-limited (RMS wavefront <= 0.0714
waves) at EVERY field, with refocus plus ONE push (lateral decenter of one element or cemented group). Camera lens:
refocus only. A trial whose push sits at its travel limit counts as a failure. Alongside the yield, track the **tail
share**: builds worse than twice the diffraction limit. Do not use the single worst build: in 2000 trials it is one
trial and moves by 30 % between lenses that are the same in every other measure.

1. **Fix the judge before you move anything.**
   - Budget: the wizard preset for the lens type (microscope `{"vendor": "Generic", "grade": "HighPrecision",
     "element_decenter": 0.010, "element_tilt": 0.0167}`; camera `"grade": "Precision", "element_decenter": 0.02,
     "element_tilt": 0.017`). One push travel for the whole session (the brief gives it, for example 0.05 mm).
   - The judge merit is the worst-field merit (per-field RWCE plus a MAXX row), separate from the merit you optimize
     on. The brief supplies it. `pick_push` and `criterion="merit"` read the LOADED merit, so swap the judge merit in
     to measure (`load_merit`) and your working merit back to optimize. The wizard's own merit is refused by the
     picker (`merit_layout_mismatch`); that is expected.
   - Measure the start lens with the step 5 confirm once step 2 has picked the push. Save it as a candidate and
     write its numbers down. Every keep is judged against the START, not against the previous round.
2. **Pick the push first, as at assembly.** Which element to push is your design decision, as it would be on a real
   lens. On the start lens, with the judge merit loaded: `tolerance(mode="pick_push",
   budget=..., cb_group={"decenter_mm": <travel>})`. It shortlists the three top decenter/tilt groups, runs a capped
   MC200 on each and returns the winner, a near-tie flag and a `judge_call` (MC2000). Cost: up to 25 min. A near tie
   means either group will do; take the one the tool ranks first. Use this group for every measurement in the
   session. Re-run the picker only at the end, if the top decenter hotspot has moved to another group.
3. **Find the hotspots.** `tolerance(mode="sensitivity", budget=..., criterion="wavefront")` (paraxial, 1-2 s).
   Read `hotspots[]` only when `hotspots_withheld` is null. The ranking is refocus-only (a sensitivity run cannot
   take the push); how much of each row the push absorbs is unmeasured, so treat the ranking as the list to try, and
   let the confirm decide. On a 10x microscope objective the kept gains came from a surface in the group next to
   the push group.
4. **One lever per round, chosen by cause.** Say the cause and the lever in one line.
   - **Steep surface, high angle of incidence** (the top row is a tilt or decenter of an element with a strong
     curve): an angle-of-incidence ceiling (`RAID` at about 0.8x the current value) on the surface the top row names.
     On a 10x microscope objective this lever produced every kept gain (6 to 9 points of yield over the start, no
     glass change).
   - **A surface that could be concentric** about the stop or the chief-ray centre: bend it toward concentricity, so
     a tilt or decenter turns it about its own centre and changes little.
   - **A fighting pair** (two neighbours with large, opposite contributions that cancel in the nominal): move power
     out of the pair so neither has to balance the other.
   - **An air gap at the top:** a thickness bound.
   - **Glass, only when the brief allows it:** a higher index at a similar Abbe number gives the same power with
     shallower curves. Re-read colour after a keep. On the same 10x a glass search before tolerancing cost 0 to 23 points
     of yield, so a glass change must earn its keep at the confirm like any other lever.
   - **Perturbed copies** (`mode="desensitize"`) when the levers above fail on the top hotspot: it changes the lens
     (reload to revert) and decides nothing.
   Avoid moving a radius directly (on the same 10x a 5 % radius change moved the nominal 23 %).
5. **Screen cheaply, confirm with the push.** After each lever, optimize to rest on the working merit and re-run
   step 3. Keep screening while the RSS falls by more than the noise floor and the nominal still meets the nominal
   spec. The RSS does not decide a keep: on the 10x the engine's own tolerancing optimization lowered its RSS and gained
   nothing as built. When the screened changes add up (or after about three rounds), confirm: load the judge merit
   and run `tolerance(mode="monte_carlo", compensator="dls_cb_group", cb_group={"group": <the step 2 group>,
   "decenter_mm": <travel>}, criterion="merit", budget=..., trials=200, full=true)`. Count the yield (trials at
   or under the threshold and not at the push limit) and the tail share from the per-trial values.
   - **A real gain is obvious at 200 trials.** Two 200-trial runs of the SAME lens differ by up to about 10 points
     of yield (on the 10x one 200-trial run read 70.5 % where 2000 trials gave 63.4 %). So: a gain of about 10 points or more
     over the start, with the tail share not higher, is a **keep**. A smaller gain is **not confirmed**: keep working
     from it if you like, but report it as unconfirmed, and run the confirm again (pooling the two runs) before you
     call it a keep. A loss reloads the last kept candidate.
   - Save every kept lens as a candidate. A 2000-trial judge of the final lens (the `judge_call` from step 2) is the
     verdict; your 200-trial numbers steer the session.
   - A Monte Carlo call stops at its time cap (10 s per trial plus 60 s, or `cap_s`) and is then refused as capped,
     with no statistics. If a call fails or returns fewer trials than asked, rerun it; do not count a partial run.
6. **Find the hotspots again after every keep.** Desensitizing one element often moves the sensitivity to its
   neighbour. Join the new list to the old by `key` only when `run_summary.topology_digest` is unchanged.
7. **Measure time; do not estimate it.** Note the wall time of every tolerancing call, and plan the next round from
   the measured times (on the 10x with a push: about 1.4-2.9 s per trial, so 200 trials take 5-10 min, and `pick_push` up to 25 min). Keep working
   while time remains: the next hotspot, the next lever. Stop when the top hotspot's levers all fail or the time is
   spent.
8. **Report.** The start lens and the final lens side by side: the push group, the yield, the tail share, the P90,
   the nominal at every field, each with the trials it rests on. Then each round: hotspot, cause, lever, readings and
   decision (kept, rejected, screened out, not confirmed). Name the hotspots left untried.

If a glass-clock stop block arrives here, it is the stage-B budget: stage C runs with `OPTIVIBE_GLASS_CLOCK_MODE=off`.
Say so to the user and ask them to set it; do not work around the hook.
