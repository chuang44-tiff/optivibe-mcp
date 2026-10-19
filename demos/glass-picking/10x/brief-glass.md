# Design brief — Glass finishing step on a dry 10x / NA 0.45 apochromat with a 2.5 mm field -- element count FIXED

This is a FINISHING task. The form is already built: an earlier run widened a 10x / NA 0.45 dry apochromat to a 2.5 mm
field with every glass held as the seed had it, and optimized it to rest with no colour rows in its merit. Your job is
the glass step that comes last: find whether changing glass gains on what is left, and keep only what measurement
supports.

## Starting point (load this first)

Load this design with `load_design`:

    {SEED}
Read it back (`describe_surfaces`, `get_first_order`) and confirm what you actually loaded BEFORE changing anything.
If the load fails, say so and stop -- do not build a replacement from scratch and present it as the seed.

## What to do

Load the design and measure every target below on it as your BEFORE state, with the tool that produced each value.
Then do the glass finishing step; the `design-glass-matching` skill covers it. Glass choices, radii, thicknesses and
spacings are yours to change.

**KEEP THE ELEMENT COUNT AS THE DESIGN HAS IT. Do not add or remove elements.** If you believe the target is
unreachable at this element count, say so with the measurement that shows it and deliver the best lens you reached.

**What the glass step is for: secondary colour.** The earlier run measured the lens as limited at mid-field
(0.625 and 0.875 mm) by the g line: its spherical aberration changes with wavelength, so the g line's own wavefront
error sets the polychromatic value there, and on axis the g line focuses apart from F and C. That is the term a glass
change can move. At the field edge the limit is primary lateral colour; that is not the goal of this step. Do not trade
it away: it must not get worse than the loaded design's.

**The merit is yours.** You may add single-wavelength wavefront rows (the g line at the mid fields, for example) and
lateral-colour rows that hold the edge where it is, weighted as you judge. Judge a glass change on the polychromatic
wavefront error at every field against its control; a colour term only points to the carrier element. A trial set may
change one to three glasses together; use a set of two or three when single changes get re-balanced away.

**Decide and proceed.** If several paths are defensible, choose by the stated target priorities (working distance
first, then diffraction-limited performance at EVERY field point, then secondary colour), state the choice and why in
one line, and continue. Do not stop to ask -- nobody is watching this run, and a question ends it with nothing
delivered.

## Targets

- **Element count unchanged from the loaded design**
- **Working distance (last optical surface to specimen) >= 5.0 mm, measured from the point of the last surface nearest the specimen: its clear-aperture rim when that is nearer than the vertex.** Check the loaded design against this before anything else.
- Effective focal length 20 mm +/-1% (= 10x on a 200 mm tube lens)
- Numerical aperture 0.45 +/-0.02, measured by real marginal ray, object space AIR
- Covers a 2.5 mm diameter specimen field (+/-1.25 mm object height)
- **Diffraction-limited across the WHOLE field: polychromatic Strehl >= 0.80 at EVERY field point** -- on axis, and at 0.5, 0.7 and 1.0 of the +/-1.25 mm object height (equivalently, polychromatic RMS wavefront error <= 0.075 lambda at each). Measure every one of the four field points yourself, and in the final report give the value at each with the tool or operand that produced it.
- Field sag across the 2.5 mm FOV <= 5 um
- Still infinity-corrected, dry, real catalog glass
- Minimum glass EDGE thickness >= 1.0 mm; minimum internal air gap >= 0.5 mm; NO zero-thickness contact between powered surfaces
- **Total track <= 100 mm**, read as `get_operand('TOTR')` (first lens surface to the specimen plane)
- **Lateral colour at the 1.0 field no worse than the loaded design's.** At the 1.0 field trace the chief ray (pupil
  centre, `trace_rays` with Px = Py = 0) at every wavelength and take each one's image height minus the primary's.
  Report every wavelength's difference, not only the largest.

## Secondary colour to measure and report, BEFORE and AFTER (the gain this step is judged on, after the targets)

- Per-wavelength RMS wavefront error at each of the four field points, beside the polychromatic value.
- Axial colour across the whole band: the best-focus shift of each wavelength relative to the primary, on axis, and
  the g line's shift against the mean of F and C.

## Look at the layout before you accept a round

Do not judge the geometry from the numbers alone. After the figure-bearing
`save_candidate` of each post-optimization assessment, invoke the
`design-vision-review` skill with that candidate's seq as its args
(`candidate seq <N>`), so it reviews the paired PNG at the `png_path` of that same
call's envelope, and read what it returns.

YOU ARE EXPLICITLY AUTHORIZED to dispatch a subagent for that review -- the skill's
own contract is that a review is never done inline. Dispatch it in the FOREGROUND
(`run_in_background` false or absent) and do not advance the round until its reply
is back in front of you.

WHAT IT IS NOT: it is a second pair of eyes, not a measurement.

## A look at the figure is a hypothesis, never a measurement

Whatever the figure suggests -- whoever did the looking -- treat it as a HYPOTHESIS
and confirm it with a tool (`check_clearance`, `describe_surfaces`,
`get_first_order`) before you act on it or report it. Looking does NOT replace
`check_clearance`, and a finding you could not confirm with a tool is reported as
unconfirmed, not as a result.

## Working rules

- **Never report a number you did not measure in this session.** Every number in your final
  table must come from a tool call you actually made. If you did not measure something, write
  `not measured`. Do not estimate, do not infer it from a related quantity, and do not answer
  from memory. Name the tool that produced each number.
- **A clean tool call is not proof of success.** Trust the read-back value and the optimizer
  verdict, not the absence of an error.
- **If a measured quantity gets worse, say so.** Do not report only your best intermediate
  value, and do not report a target as met on the strength of an earlier state.
- **State the limiting factor honestly.** If you cannot meet a target, say what stopped you
  and how you know it is a real limit rather than an unconverged design.

## Optimizer choice for this run

Use LOCAL optimization only: `optimize` with algorithm `DLS` (the default) or `OD`.
Do not call `optimize` with algorithm `Hammer`, and do not use any other global search,
in this run.

## Take the reviewer's findings seriously

A reviewer finding is about what the lens LOOKS like, and it is often about PROPORTION:
an element thin for its diameter, a lens long for its focal length, a gap that pinches.

- Decline a finding only with a measurement that answers what the finding actually says. A
  passing floor is NOT such a measurement for a proportion finding.
- A proportion finding that no stated target covers is NOT dismissed for that reason. Act on
  it, or keep it and say plainly in your report why you could not.

## What to report at the end

1. **FINAL PRESCRIPTION** — the output of a final `describe_surfaces` call.
2. **MEASURED RESULTS** — a table: each acceptance criterion, the measured value, the tool
   that produced it, and PASS / FAIL / `not measured`; then the secondary-colour quantities above.
3. **WHAT CONSTRAINED THE SHAPE** — which constraints you imposed, HOW (a `build_merit`
   parameter vs an `add_operand` code), and whether you VERIFIED each one held in the final design.
4. **HONEST VERDICT** — did you meet the spec? Name the aberration that limits the lens at the end: monochromatic
   (shared by every wavelength) or chromatic, and if chromatic, primary colour, secondary colour (the g line against
   F and C), or the change of an aberration with wavelength. Say how you know it is real rather than unconverged.

## Saving the result (required)

Save the design you are reporting on, and PROMOTE it — `save_candidate` then `promote_best`
— so there is exactly one canonical keeper on disk. Name the promoted file after the design.
State the file you promoted in section 1. If a gate REFUSES the promotion, report the refusal
and its reason verbatim; do not work around it.

## No human is available during this run

Nobody will answer a question or give a steer while you work. Where a skill says it
pauses for the human, treat what it returns as advice, decide yourself, say what you
decided and why, and carry on to the end of the task.

## One extra reporting requirement for this task

State explicitly what the loaded design measured BEFORE your changes and what it measures
AFTER, for every target. A refinement that made something worse must be reported as
worse, not omitted.

## Glass section of the report

For every glass change you keep or reject: the carrier element and how you found it, the candidates, each trial's
polychromatic wavefront error at every field beside the unchanged lens's given the same optimization, and your
decision. If you changed no glass, say so and say why. Then give the GAIN: every target and the secondary-colour
quantities BEFORE (as loaded) and AFTER, side by side.
