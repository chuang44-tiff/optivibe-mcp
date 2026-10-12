# Design brief — Rework a dry apochromat into a LONG-WD 10x / NA 0.45 -- element count FIXED

This is a REFINEMENT task. You are not starting from a blank sheet: a finished,
working design already exists and your job is to modify it and re-optimize.

## Starting point (load this first)

Load this design with `load_design`:

    {SEED}

It is a finished infinity-corrected DRY apochromatic microscope objective at roughly 60x / NA 0.899 (EFL near 3.34 mm on a 200 mm tube lens), with a very SHORT working distance. Read its actual values back -- including its working distance and its element count -- rather than trusting this description.

Read it back (`describe_surfaces`, `get_first_order`) and confirm what you actually
loaded BEFORE changing anything. If the load fails, say so and stop — do not build a
replacement from scratch and present it as the seed.

## What to do

Load the seed and read back what it actually is (EFL, NA, magnification, field, waveband, element count, on-axis Strehl, and WORKING DISTANCE) -- report those seed values explicitly as your BEFORE state. Then rework it into a 10x objective: 20 mm effective focal length (10x on a 200 mm tube lens), numerical aperture 0.45, covering a 2 mm diameter specimen field (+/-1.0 mm object height), DRY and infinity-corrected.

**The working distance is a first-class target: at least 5 mm from the last optical surface to the specimen.** Scaling alone will NOT deliver it -- the seed's working distance is very short and scaling to 20 mm EFL yields well under 1 mm. Reaching 5 mm requires reworking the front group. Measure it by real geometry and treat it as a constraint the optimizer must hold.

**KEEP THE ELEMENT COUNT AS THE SEED HAS IT. Do not add or remove elements.** Radii, thicknesses, spacings and glass choices are all yours to change; the element count is not. If you believe the target is unreachable at this element count, say so with the measurement that shows it and deliver the best form you reached -- do NOT change the element count to get there.

**Decide and proceed.** If you reach a point where several paths are defensible, choose by the stated target priorities (working distance first, then on-axis quality, then field), state the choice and why in one line, and continue. Do not stop to ask which path to take -- nobody is watching this run, and a question ends it with nothing delivered.

## Targets

- **Element count unchanged from the seed**
- **Working distance (last optical surface to specimen) >= 5.0 mm**
- Effective focal length 20 mm +/-1% (= 10x on a 200 mm tube lens)
- Numerical aperture 0.45 +/-0.02, measured by real marginal ray, object space AIR
- Covers a 2 mm diameter specimen field (+/-1.0 mm object height)
- On-axis Strehl >= 0.90
- 0.7-field Strehl >= 0.75
- Full-field RMS wavefront error <= 0.15 lambda
- Field sag across the 2 mm FOV <= 5 um
- Still infinity-corrected, dry, real catalog glass
- Minimum glass EDGE thickness >= 1.0 mm; minimum internal air gap >= 0.5 mm; NO zero-thickness contact between powered surfaces

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

WHEN TO USE IT: after each `save_candidate` at a post-optimization assessment, before
you accept an optimization round and move on; and again on the design you are about
to promote. It is most useful
where a figure shows something a scalar does not -- a collapsed or crossed group, an
element the optimizer flipped, a gap that closed, a beam that misses an aperture, a
form that is not the form you believe you are designing.

WHAT IT IS NOT: it is a second pair of eyes, not a measurement.

## A look at the figure is a hypothesis, never a measurement

Whatever the figure suggests -- whoever did the looking -- treat it as a HYPOTHESIS
and confirm it with a tool (`check_clearance`, `describe_surfaces`,
`get_first_order`) before you act on it or report it. Looking does NOT replace
`check_clearance`, and a finding you could not confirm with a tool is reported as
unconfirmed, not as a result. Blind-adjudicated violation recall for the figure
channel was 4 of 25 = 0.16 (2026-08-21): silence never certifies, and a
tool-CONFIRMED finding always blocks.

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
  passing floor is NOT such a measurement for a proportion finding: for "too thin" or
  "skinny", report centre thickness against diameter; for "too long", total track against
  focal length; for "looks tight", the gap against its neighbours.
- A proportion finding that no stated target covers is NOT dismissed for that reason. Act on
  it (add a constraint and re-optimize, then show the stated targets still hold), or keep it
  and say plainly in your report why you could not.


## What to report at the end

1. **FINAL PRESCRIPTION** — the output of a final `describe_surfaces` call.
2. **MEASURED RESULTS** — a table: each acceptance criterion, the measured value, the tool
   that produced it, and PASS / FAIL / `not measured`.
3. **WHAT CONSTRAINED THE SHAPE** — which constraints you imposed (thickness floors, edge
   thickness, air gaps, colour/achromatism operands), HOW (a `build_merit` parameter vs an
   `add_operand` code), and whether you VERIFIED each one held in the final design.
4. **HONEST VERDICT** — did you meet the spec? If not, what is the actual limiting factor,
   and how do you know it is real rather than an unconverged design?

## Saving the result (required)

Save the design you are reporting on, and PROMOTE it — `save_candidate` then `promote_best`
— so there is exactly one canonical keeper on disk rather than a pile of numbered candidates
someone else has to guess between. Name the promoted file after the design. State the file
you promoted in section 1. If a gate REFUSES the promotion, report the refusal and its reason
verbatim; do not work around it, and do not report a design as final that you could not
promote.

## No human is available during this run

Nobody will answer a question or give a steer while you work. Where a skill says it
pauses for the human, treat what it returns as advice, decide yourself, say what you
decided and why, and carry on to the end of the task.


## One extra reporting requirement for this task

State explicitly what the seed measured BEFORE your changes and what it measures
AFTER, for every target. A refinement that made something worse must be reported as
worse, not omitted.
