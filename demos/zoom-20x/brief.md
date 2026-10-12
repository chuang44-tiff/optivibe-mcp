# Design brief — Improve a 20x high-ratio zoom

This is a REFINEMENT task. Load this finished multi-configuration design with `load_design`:

    {SEED}

It is a 20x zoom (about 4.5-90 mm) for a 1/1.8" sensor, visible C/d/F, with its zoom positions defined as
configurations. Read it back (`describe_configurations`, `describe_surfaces`, `get_first_order` per configuration)
and measure it yourself before changing anything. If the load fails, say so and stop.

## What to do

Make it better. Improve off-axis image quality at the zoom position where it is worst, without degrading the other
positions and without breaking the zoom. Decide which position and what is limiting it, and improve that.

## Targets

- Off-axis image quality (RMS spot or RMS wavefront at 0.8 field, your choice, stated) improved at the worst zoom
  position, and no worse at any other position. Measure every position before and after.
- Hold, measured before and after, at every zoom position:
  - focal length within +/-3% of the seed's value;
  - the image plane fixed across the range (report the drift);
  - distortion <= 4% wide and <= 2% tele; lateral colour no worse than the seed;
  - element count unchanged; real catalog glass;
  - no glass thinner than 1.0 mm at the edge; no air gap below 0.3 mm.
- If the worst position cannot improve without breaking a held target, say so with the measurements and deliver the
  best balanced design.

## Working rules

- **Never report a number you did not measure in this session.** Every number in your final table must come from a
  tool call you actually made. Name the tool. If you did not measure it, write `not measured`.
- **A clean tool call is not proof of success.** Trust the read-back value, not the absence of an error.
- **If a measured quantity gets worse, say so**, with before and after.
- **State the limiting factor honestly.** If you cannot improve further, say what stopped you and how you know it is a
  real limit rather than an unconverged design.
- Use LOCAL optimization only: `optimize` with `DLS` (default) or `OD`. No Hammer or other global search.
- You may dispatch subagents where a skill calls for one. Dispatch them in the FOREGROUND and wait for the reply.

## No human is available during this run

Nobody will answer a question or give a steer. Where a skill says it pauses for the human, decide yourself, say what
you decided and why, and carry on to the end.

## What to report at the end

1. **BEFORE / AFTER** — every target, measured on the seed and on your result, with the tool that produced each value.
2. **WHAT YOU CHANGED AND WHY** — the moves you made, in order, and what each one bought.
3. **HONEST VERDICT** — what improved, what got worse, and what limits the design now.

## Saving the result (required)

Save every increment you keep as a candidate under one design name, then `save_candidate` + `promote_best` the
design you report on. If a gate refuses the promotion, report the refusal verbatim; do not work around it.
