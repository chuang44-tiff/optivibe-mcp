# Design brief — Desensitize a 10x / NA 0.45 apochromat for an as-built yield target with one push -- element count and glasses FIXED

> **About this copy.** This is the brief given to runs 2, 3 and 4. Run 1 used the previous version, which differs by
> one sentence: it lacks "'Every lever tried' means each hotspot in your latest sensitivity list has had each lever the
> skill names tried on it; before you stop early, list any hotspot and lever you did not try." Edits for publication,
> with every rule unchanged: the optimizer sentence no longer names a global optimizer, the in-loop read is described
> by what it does (the tool call itself is unchanged), the judge merit's path now points to the file beside this
> one, and internal stage labels are replaced by plain words. `{SEED}` was replaced by the start lens's path in each run.

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
**0.05 mm** each way. **The push is given: the cemented group G14-17** (the picker chose it on this lens in every
earlier run). Do not run `pick_push`. Its as-built BEFORE, from the bench's 2000-trial push judge of the loaded lens,
is **57.5 %** diffraction-limited; quote it, do not re-measure it.

- Tolerance budget, every call: `budget={"vendor": "Generic", "grade": "HighPrecision", "element_decenter": 0.010,
  "element_tilt": 0.0167}`. `sampling` 4 (the default).
- In-loop check: `tolerance(mode="sensitivity", budget=..., criterion="wavefront")`.
- In-loop confirm: a paired per-field yield read, `tolerance(mode="quick_yield", budget=..., pupil=2,
  threshold=0.0714, trials=1000, repeats=2)` on the parent, then the same call with `baseline=<the parent's id>` on
  the candidate, as the skill says. It reads each of the four fields; the worst field decides. It is refocus only:
  do not pass `cb_group` to it.
- **Headroom cap: 0.035 waves.** A candidate whose nominal polychromatic RMS wavefront is above 0.035 waves at ANY
  field is out before any yield read (the loaded lens reads about 0.030 at the edge of the field).
- **The judge** is the bench's 2000-trial push Monte Carlo on the worst-field merit (`judge_worstfield.MF`, in this
  folder: the RMS wavefront at each of the four fields and their maximum; a build is diffraction-limited at or under
  **0.0714** waves). It runs after you deliver; you do not load that merit or run a Monte Carlo in this run. Deliver
  the lens with its own merit (plus any rows you added) loaded.
- Do not use `dls_cb_tilt`: this lens has no group-tilt adjustment.

## The as-built target

**90 % of builds diffraction-limited at EVERY field** (on axis and at 0.5, 0.7 and 1.0 of the field), with refocus
and your push. The loaded lens is well short of that, so the full target may be out of reach; the score is how far you
move it. The final judge runs a 2000-trial Monte Carlo on the judge merit with refocus and a push of 0.05 mm.

## What to do

Measure every target below on the loaded design as your BEFORE state, with the tool that produced each value,
quoting the given as-built BEFORE. Then desensitize. Radii, thicknesses and spacings are yours to
change.

**Levers this run.** The skill's levers: the angle-of-incidence ceiling on the hotspot's surface, and moving the
glass thicknesses and air gaps inside their bounds to win the edge field back. Which surface, in what order and how
far is your call.

**Packaging, as when the form was built.** This is ordinary optimization: the optimize-loop rules hold.
- **Every glass centre thickness at most 6.5 mm** (the loaded lens's thickest is 6.0 mm). Add the ceiling before you
  free a thickness. A candidate with a thicker element is out before any yield read.
- **Total track** (first surface to the specimen) is 83.0 mm on the loaded lens. Length is a cost, never the lever:
  keep the lens compact as the form-building stage would, and if a change grows the track, compress it back before you count the
  change.
- **Look at the layout** as the skill says (`design-vision-review` on the saved candidate).

**THE GLASSES ARE FIXED.** Do not change any glass, and do not run a glass search.

**THE MERIT'S EXISTING ROWS ARE FIXED.** Do not remove or re-weight any row the design's own merit carries. You may
ADD rows that bound or limit the geometry -- a ray-angle or angle-of-incidence ceiling, a thickness, gap or edge bound,
a first-order hold -- and you may remove rows you added. **Do NOT add rows that target image quality**
(wavefront, spot, MTF, Strehl or aberration operands): the nominal image quality is already far inside the limit, and the task is
sensitivity, not nominal sharpness. Do not optimize on the judge merit; it is for measuring. The final judge measures
the lens exactly as you deliver it, with no further optimization, so every lever counts, including one that leaves
the lens off the merit's own minimum, as long as the nominal targets below still hold.

**KEEP THE ELEMENT COUNT AS THE DESIGN HAS IT. Do not add or remove elements.**

Confirm with paired per-field yield reads as the skill says, and keep a change only by the skill's keep rule. The
closing push Monte Carlo is OFF for this run: the bench judges the lens you deliver with its own 2000-trial push
Monte Carlo, so report the as-built AFTER as `not measured (judged by the bench)`.

**Time: about 2 h 30 min of work.** A note after each tolerance, optimize, save and load call gives the bench's
measured clock; plan the rounds from it. Stop only when the time is spent: a run that stops with an hour left and
hotspots untried has wasted that hour. "Every lever tried" means each hotspot in your latest sensitivity list has had each lever
the skill names tried on it; before you stop early, list any hotspot and lever you did not try. At about 2 h 20 min stop starting new rounds; then measure the lens you
deliver, save and report.

**Decide and proceed.** If several paths are defensible, choose one, state the choice in one line, and continue. Do
not stop to ask -- nobody is watching this run, and a question ends it with nothing delivered.

## Targets (nominal; each must still hold on the lens you deliver)

- **Element count unchanged from the loaded design; every glass unchanged**
- **Nominal diffraction-limited at every field:** polychromatic RMS wavefront <= 0.0714 waves on axis and at 0.5, 0.7
  and 1.0 of the field (measure each; `analyze_wavefront` with `hx`/`hy`, or the RWCE operand with wave 0).
- Effective focal length 20 mm +/- 1 %
- Numerical aperture 0.45 +/- 0.02 (real marginal ray, object space air)
- Working distance (last optical surface to specimen) no shorter than the loaded lens's
- Every glass centre thickness <= 6.5 mm; total track reported (BEFORE 83.0 mm) with its change
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
2. **MEASURED RESULTS** — each target: BEFORE (as loaded) and AFTER, the tool, PASS / FAIL / `not measured`,
   including the total track and the thickest element.
3. **PUSH** — G14-17, as given.
4. **AS BUILT** — BEFORE 57.5 % (bench judge, given) and AFTER `not measured (judged by the bench)`; then the
   paired per-field yield readings that carried each keep (the worst field and every field's `d_yield`).
5. **ROUNDS** — for every round: the hotspot, the cause, the lever and why, the readings, and the decision (kept,
   rejected, screened out, not confirmed). Hotspots left untried, by name.
6. **TIME** — the wall time of each tolerancing call and of the whole session.

## Saving the result (required)

Save the design you report on and PROMOTE it — `save_candidate` then `promote_best` — so there is exactly one keeper
on disk, named after the design. State the file. If a gate refuses the promotion, report the refusal verbatim. If no
change was kept, promote the loaded lens unchanged and say so.

## No human is available during this run

Where a skill says it pauses for the human, treat what it returns as advice, decide yourself, and carry on.
