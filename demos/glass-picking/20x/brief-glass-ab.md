# Design brief — Glass step on a dry 20x / NA 0.70 objective with a 1.5 mm working distance -- element count FIXED, merit FIXED

This is a FINISHING task. The form is already built and at rest: an earlier run reworked a published 20x cover-glass
objective into a dry 20x / NA 0.70 objective with every glass held as the seed had it. Your job is the glass step that comes last: find whether
changing glass improves the lens's overall performance, and keep only what measurement supports.

## Starting point (load this first)

Load this design with `load_design`:

    {SEED}

Read it back (`describe_surfaces`, `get_first_order`, `dump_merit_function`) and confirm what you actually loaded
BEFORE changing anything. If the load fails, say so and stop -- do not build a replacement from scratch.

## What to do

Measure every target below on the loaded design as your BEFORE state, with the tool that produced each value. Then do
the glass step; the `design-glass-matching` skill covers it. Glass choices, radii, thicknesses and spacings are yours
to change.

**The aperture stop is a free air surface; its gaps are already variables. THE MERIT IS FIXED. Optimize with the merit the design carries, exactly as loaded: do not add, remove or re-weight
any row.** It is a performance merit: polychromatic wavefront error at four fields plus the working-distance, focal
length, track, edge and thickness rows, and six rim-ray rows that hold the ray error at the pupil rim. Another arm of this test runs on the same merit, so a changed merit spoils the
comparison.

**KEEP THE ELEMENT COUNT AS THE DESIGN HAS IT. Do not add or remove elements.**

**THE GLASS LIST IS FIXED: choose glasses only from the catalogs SCHOTT, OHARA, HIKARI, LACROIX and INFRARED.** Pass
`catalog="SCHOTT,OHARA,HIKARI,LACROIX,INFRARED"` to `find_glasses` and `find_glass_pair`. The other arm of this test
draws from exactly these catalogs.

**The goal is overall performance:** the polychromatic RMS wavefront error at every field, against the unchanged lens
given the same optimization (the control). A colour term only points to the carrier element; it is not the goal.
Keep a glass change only if it improves the fields that fail the target while every field that passes still passes,
and it costs no other target.

**Time: about 60 minutes of work.** Plan the trials to fit. At about 60 minutes stop starting new trials, run the
final `optimize`, measure, save and report.

**Decide and proceed.** If several paths are defensible, choose by the target priorities (working distance first,
then diffraction-limited performance at EVERY field point), state the choice in one line, and continue. Do not stop to
ask -- nobody is watching this run, and a question ends it with nothing delivered.

## Targets

- **Element count unchanged from the loaded design**
- **Working distance (last optical surface to specimen) >= 1.5 mm, measured from the point of the last surface nearest the specimen: its clear-aperture rim when that is nearer than the vertex.**
- Effective focal length 10 mm +/-1% (= 20x on a 200 mm tube lens)
- Numerical aperture 0.70 +/-0.02, measured by real marginal ray, object space AIR
- Covers a 1.0 mm diameter specimen field (+/-0.5 mm object height)
- **Diffraction-limited across the WHOLE field: polychromatic Strehl >= 0.80 at EVERY field point** -- on axis, and at 0.5, 0.7 and 1.0 of the +/-0.5 mm object height (equivalently, polychromatic RMS wavefront error <= 0.075 lambda at each). Measure all four yourself.
- Field sag across the 1.0 mm FOV <= 2 um
- Still infinity-corrected, dry, real catalog glass
- Minimum glass EDGE thickness >= 1.0 mm; minimum internal air gap >= 0.5 mm; NO zero-thickness contact between powered surfaces
- **Total track <= 100 mm**, read as `get_operand('TOTR')`
- **Largest glass CENTRE thickness <= 8.0 mm**, any element, read from `describe_surfaces` (up to 8.005 mm counts as met)
- Lateral colour at the 1.0 field: measure and report (chief ray, `trace_rays` with Px = Py = 0, each wavelength's image height minus the primary's).

## Optimizer choice for this run

Use LOCAL optimization only: `optimize` with algorithm `DLS` (the default) or `OD`. Do not call `optimize` with
algorithm `Hammer`, and do not use any other global search, in this run.

## Working rules

- **Never report a number you did not measure in this session.** Name the tool that produced each number; write
  `not measured` where you have none.
- **A clean tool call is not proof of success.** Trust the read-back value and the optimizer verdict.
- **If a measured quantity gets worse, say so.**
- Check geometry with `check_clearance` before you count a gain.

## What to report at the end

1. **FINAL PRESCRIPTION** — the output of a final `describe_surfaces` call.
2. **MEASURED RESULTS** — each target: BEFORE (as loaded) and AFTER, the tool, PASS / FAIL / `not measured`.
3. **GLASS** — for every glass change you kept or rejected: the carrier and how you found it, the candidates, each
   trial's polychromatic wavefront error at every field beside its control's, and the decision. If you changed no
   glass, say so and why.
4. **TIME** — about how long the glass step took.

## Saving the result (required)

Save the design you report on and PROMOTE it — `save_candidate` then `promote_best` — so there is exactly one keeper
on disk, named after the design. State the file. If a gate refuses the promotion, report the refusal verbatim.

## No human is available during this run

Where a skill says it pauses for the human, treat what it returns as advice, decide yourself, and carry on.
