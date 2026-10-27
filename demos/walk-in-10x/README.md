# Demo: an agent reworks a 60× objective into a 10× / NA 0.45 apochromat, unattended

![The 10x / NA 0.45 objective the agent designed, as OpticStudio draws it](walk-in-10x-layout.png)

**One unattended run turned a finished 60× / NA 0.90 objective into a 10× / NA 0.45 objective with a polychromatic
Strehl ratio of 0.990 / 0.964 / 0.960 / 0.935 across the field.** Every field point clears the diffraction-limited
bar (Strehl ≥ 0.80), and every other target checked was met. No human took part during the run.

## The task

The agent got a written brief and the seed lens file, and nothing else:

- **Seed:** a 60× / NA 0.90 dry apochromat with 14 elements and a 0.13 mm working distance.
- **Goal:** a 10× objective (20 mm focal length ± 1 %), NA 0.45 ± 0.02, ±1.0 mm field, real catalog glass, all 14
  elements kept.
- **Targets:**
  - working distance ≥ 5 mm;
  - polychromatic Strehl ≥ 0.80 on axis and at 0.5, 0.7 and 1.0 field;
  - field curvature ≤ 5 µm;
  - glass edges ≥ 1.0 mm and air gaps ≥ 0.5 mm.

## What the agent did

Claude Opus 5.5 drove the OptiVibe MCP server through Claude Code with the `optimize-loop` and `design-vision-review`
skills: 67 minutes, 487 tool calls. It walked the spec in, taking the easy targets first:

- **Packaging.** Scaling the seed gave a 0.8 mm working distance and a 183 mm lens. The agent brought the working
  distance past 5 mm while cutting the length to 150 mm, then walked it down to 110 and 90 mm.
- **The field edge.** Adding weight on the outer field hurt the centre, so it undid that. It diagnosed the edge as a
  colour limit and changed one glass, on the element nearest the specimen. After that the edge passed 0.80.
- **Layout reviews.** A reviewer read each layout drawing and flagged gaps that looked tight. The agent raised its
  air-gap floor to 1.5 mm. The edge Strehl cost of that was small: 0.937 → 0.935 on the re-measure.
- **It promoted the result itself,** through the server's clearance and identity checks.

## Results

A separate script measured the promoted file afterwards, on a fresh load:

| | axis | 0.5 field | 0.7 field | 1.0 field |
|---|---|---|---|---|
| polychromatic Strehl | 0.990 | 0.964 | 0.960 | 0.935 |
| RMS wavefront error (waves) | 0.017 | 0.039 | 0.040 | 0.043 |

| | measured | target |
|---|---|---|
| focal length | 20.001 mm | 20 mm ± 1 % |
| NA (the run's own real-ray reading) | 0.4496 | 0.45 ± 0.02 |
| working distance, on axis | 5.87 mm | — |
| working distance, at the lens rim | 5.05 mm | ≥ 5 mm |
| field curvature (shift from the on-axis focus) | 2.2 µm | ≤ 5 µm |
| total track | 85.7 mm | — |
| clearance violations | 0 | 0 |

**Working distance.** The last surface curves toward the specimen, so the distance at the lens rim is shorter than
on axis. The rim value, 5.05 mm, was read on the engine later, as the edge thickness of the last air gap. It meets
the target with little margin. The brief set no length target, so the lens is not packaged to a standard 45 mm
parfocal objective length.

Measured in OpticStudio 2025 R1:

- Strehl and wavefront error with the STRH and RWCE operands;
- focal length from the first-order data, and working distance from the last air gap (its thickness
  on axis, the ETVA operand at the rim);
- field curvature from the sagittal and tangential operands, at 11 field points and 4 wavelengths.

## Limits

- **One run, the third attempt from this seed.** A run from an intermediate lens had already succeeded. Of the two
  earlier attempts from this seed, one crashed the machine. The other stopped with an edge Strehl of 0.08. The
  skill's staged walk-in was written in response to those attempts. This run shows the task can be done, not how
  often.
- **This run used an earlier `optimize-loop` revision** than the one in this release.
- **Not done yet:** tolerancing, MTF and a glass-availability check. Some facing radii are
  nearly equal, so the design is expected to be tolerance-sensitive.

A second Opus run, and a local model on the same task, are on the [local-model page](../local-model-10x/README.md).

## Check it yourself

This folder has the [brief](brief.md), the agent's [stage log](stage-log.md) and the result,
`result_walk-in-10x.zmx`. The starting lens and a script that re-measures any lens file are in
[demos/](../README.md).
