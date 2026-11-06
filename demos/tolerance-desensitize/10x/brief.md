# Design brief — Desensitize a 10x / NA 0.45 apochromat for an as-built yield target with one push (stage C) -- element count and glasses FIXED

This is a FINISHING task. The lens is built, its glass is chosen and its nominal performance is very good: every
field is well inside the diffraction limit. Built with real manufacturing errors it falls short: too many builds miss
the diffraction limit somewhere in the field, even after the assembler refocuses and centres one element. Your job is
the last stage: find what makes the lens sensitive and make it less so, spending some of the nominal margin, so more
of the lenses AS BUILT are diffraction-limited. The `design-tolerance-desensitize` skill covers it.

## Starting point (load this first)

Load this design with `load_design`:

    {SEED}

Read it back (`describe_surfaces`, `get_first_order`, `dump_merit_function`) and confirm what you actually loaded
BEFORE changing anything. If the load fails, say so and stop -- do not build a replacement from scratch.

## The as-built rule for this run

The lens will be assembled with **refocus plus ONE push**: a lateral decenter of one element or cemented group, up to
**0.05 mm** each way. **Which element to push is your decision**, as it would be on a real lens: choose it as the
skill says, and say which one you chose and why.

- Tolerance budget, every call: `budget={"vendor": "Generic", "grade": "HighPrecision", "element_decenter": 0.010,
  "element_tilt": 0.0167}`. `sampling` 4 (the default).
- In-loop check: `tolerance(mode="sensitivity", budget=..., criterion="wavefront")`.
- Push travel: `decenter_mm` 0.05 in every `cb_group`.
- **The judge merit** is in this file: `judge_worstfield.MF` (in this folder) (6 rows: the
  RMS wavefront at each of the four fields and their maximum). Its value is the WORST field's RMS wavefront in waves,
  and a build is diffraction-limited when it is at or under **0.0714**. `pick_push` and the Monte Carlo confirm with
  `criterion="merit"` read the merit that is LOADED. So: before the first swap, save the design's own merit with
  `save_merit` to a file in your workspace; load the judge merit with `load_merit` to measure; load your saved merit
  back with `load_merit` before you optimize. Deliver the lens with its own merit (plus any rows you added) loaded.
- Do not use `dls_cb_tilt`: this lens has no group-tilt adjustment.

## The as-built target

**90 % of builds diffraction-limited at EVERY field** (on axis and at 0.5, 0.7 and 1.0 of the field), with refocus
and your push. The loaded lens is well short of that, so the full target may be out of reach; the score is how far you
move it. The final judge runs a 2000-trial Monte Carlo on the judge merit with refocus and a push of 0.05 mm.

## What to do

Measure every target below on the loaded design as your BEFORE state, with the tool that produced each value,
including the as-built baseline with your chosen push. Then desensitize. Radii, thicknesses and spacings are yours to
change.

**THE GLASSES ARE FIXED.** Do not change any glass, and do not run a glass search.

**THE MERIT'S EXISTING ROWS ARE FIXED.** Do not remove or re-weight any row the design's own merit carries. You may
ADD rows that bound or limit the geometry -- a ray-angle or angle-of-incidence ceiling, a thickness, gap or edge bound,
a first-order hold -- and you may remove rows you added. **Do NOT add rows that target image quality** (wavefront,
spot, MTF, Strehl or aberration operands): the nominal image quality is already far inside the limit, and the task is
sensitivity, not nominal sharpness. Do not optimize on the judge merit; it is for measuring. The final judge measures
the lens exactly as you deliver it, with no further optimization, so every lever counts, including one that leaves
the lens off the merit's own minimum, as long as the nominal targets below still hold.

**KEEP THE ELEMENT COUNT AS THE DESIGN HAS IT. Do not add or remove elements.**

Confirm with 200-trial Monte Carlo runs as the skill says, and keep a change only by the skill's keep rule.

**Time: about 2 hours of work.** Measure the time each tolerancing call takes and plan the rounds from it. Keep
working while time remains. At about 2 hours stop starting new rounds, measure the lens you deliver, save and report.

**Decide and proceed.** If several paths are defensible, choose one, state the choice in one line, and continue. Do
not stop to ask -- nobody is watching this run, and a question ends it with nothing delivered.

## Targets (nominal; each must still hold on the lens you deliver)

- **Element count unchanged from the loaded design; every glass unchanged**
- **Nominal diffraction-limited at every field:** polychromatic RMS wavefront <= 0.0714 waves on axis and at 0.5, 0.7
  and 1.0 of the field (measure each; `analyze_wavefront` with `hx`/`hy`, or the RWCE operand with wave 0).
- Effective focal length 20 mm +/- 1 %
- Numerical aperture 0.45 +/- 0.02 (real marginal ray, object space air)
- Working distance (last optical surface to specimen) no shorter than the loaded lens's
- No new clearance violation (`check_clearance`, default floors), and no existing one made worse. Record the loaded
  lens's readings in BEFORE.

## Optimizer choice for this run

Use LOCAL optimization only: `optimize` with algorithm `DLS` (the default) or `OD`. Do not use any global search in
this run.

## Working rules

- **Never report a number you did not measure in this session.** Name the tool that produced each number; write
  `not measured` where you have none.
- **A clean tool call is not proof of success.** Trust the read-back value and the optimizer verdict.
- **If a measured quantity gets worse, say so.**
- Check geometry with `check_clearance` before you count a gain.

## What to report at the end

1. **FINAL PRESCRIPTION** — the output of a final `describe_surfaces` call.
2. **MEASURED RESULTS** — each target: BEFORE (as loaded) and AFTER, the tool, PASS / FAIL / `not measured`.
3. **PUSH** — the group you chose, why, and the picker's readings for each candidate.
4. **AS BUILT** — BEFORE and AFTER with your push: the yield (share of trials at or under 0.0714, push-limit trials
   counted as failures), the tail share (trials above 0.143), the P90, and the number of trials each rests on.
5. **ROUNDS** — for every round: the hotspot, the cause, the lever and why, the readings, and the decision (kept,
   rejected, screened out, not confirmed). Hotspots left untried, by name.
6. **TIME** — the wall time of each tolerancing call and of the whole session.

## Saving the result (required)

Save the design you report on and PROMOTE it — `save_candidate` then `promote_best` — so there is exactly one keeper
on disk, named after the design. State the file. If a gate refuses the promotion, report the refusal verbatim. If no
change was kept, promote the loaded lens unchanged and say so.

## No human is available during this run

Where a skill says it pauses for the human, treat what it returns as advice, decide yourself, and carry on.
