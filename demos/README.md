# Demos: what is here, and how to check it

Each demo folder has a page (`README.md`), the brief the agent was given, the stage log the agent
wrote during its run, and the lens file the run produced. This folder also has the two starting
lenses and the script that measures a lens file the way the pages do.

| Demo | Brief | Stage log | Starting lens | Result |
|---|---|---|---|---|
| [walk-in-10x](walk-in-10x/README.md) | [brief.md](walk-in-10x/brief.md) | [stage-log.md](walk-in-10x/stage-log.md) | 60× seed | `walk-in-10x/result_walk-in-10x.zmx` |
| [local-model-10x](local-model-10x/README.md), local model | [brief-local-model.md](local-model-10x/brief-local-model.md) | [stage-log-local-model.md](local-model-10x/stage-log-local-model.md) | 60× seed | `local-model-10x/result_local-model.zmx` |
| [local-model-10x](local-model-10x/README.md), Opus comparison | [walk-in-10x/brief.md](walk-in-10x/brief.md) (the same brief) | [stage-log-opus.md](local-model-10x/stage-log-opus.md) | 60× seed | `local-model-10x/result_opus.zmx` |
| [zoom-20x](zoom-20x/README.md) | [brief.md](zoom-20x/brief.md) | [stage-log.md](zoom-20x/stage-log.md) | zoom seed | `zoom-20x/result_zoom-20x.zmx` |
| [vision-ab](vision-ab/README.md) | [brief-vision.md](vision-ab/brief-vision.md), [brief-numbers-only.md](vision-ab/brief-numbers-only.md) | none was written | 60× seed | `vision-ab/result_lens-a.zmx` to `result_lens-f.zmx` |

The 60× seed is `seeds/seed_BEST_apo60x.zmx`. The zoom seed is `seeds/seed_BEST_zoom20x_pana5g.zmx`.

## The briefs

Each brief is the text the agent received, with one difference: the line that named the lens file
reads `{SEED}` here. In each run it was the full path of that run's own copy of the starting lens.

## The stage logs

Each stage log is the file the agent wrote during its run, unedited. Its figures are the agent's own
readings at the time. Where a log and a page differ, the page's figure is the later re-measure:

- **The Opus log on the local-model page** ends with Strehl 0.970 / 0.947 / 0.941 / 0.922. The page's
  0.972 / 0.954 / 0.945 / 0.927 was read afterwards by the measuring script; the log's figures are the
  run's own merit-function rows.
- **The local-model log** gives a rim working distance of 5.94 mm. That is the last air gap's edge
  taken at the image height, which is what `check_clearance` reports. The page's 5.30 mm is the same
  gap at the lens rim.
- The logs name review replies and candidate files from the run folders. Those are not included; the
  result files here are the ones the pages describe.

## The starting lenses

- **`seeds/seed_BEST_apo60x.zmx`** is a 60× / NA 0.90 dry apochromat with 14 elements. It is derived
  from US 7,982,961 B2, Example 1, and was re-optimized with OptiVibe.
- **`seeds/seed_BEST_zoom20x_pana5g.zmx`** is a 20× zoom with three zoom positions. It is derived from
  US 6,519,094 and was rebuilt and re-optimized with OptiVibe.

Neither file is an OpticStudio sample. The file names are the ones the runs used; the Opus comparison
run named its copy of the 60× seed differently.

## Measuring a lens file

`measure_design.py` loads a lens file fresh and prints the quantities the pages report. It needs the
two packages installed and a ZOS-API licence, and it opens one OpticStudio session.

```bash
python demos/measure_design.py demos/walk-in-10x/result_walk-in-10x.zmx
python demos/measure_design.py demos/seeds/seed_BEST_zoom20x_pana5g.zmx demos/zoom-20x/result_zoom-20x.zmx --json zoom.json
```

It works on a copy, so the file you name is not changed. The top of the script lists every quantity
and the operand or tool it comes from.

Run on the result files here, with the 0.1.14 packages, it gives these figures from the pages:

- **walk-in-10x:** Strehl 0.990 / 0.964 / 0.960 / 0.935, RMS wavefront 0.017 / 0.039 / 0.040 / 0.043
  waves, focal length 20.001 mm, working distance 5.87 mm on axis and 5.05 mm at the rim, field
  curvature 2.2 µm, total track 85.7 mm, no clearance violations.
- **local-model-10x:** Strehl 0.977 / 0.942 / 0.911 / 0.834 for the local model and
  0.972 / 0.954 / 0.945 / 0.927 for Opus; working distance 5.99 and 5.30 mm against 6.79 and 6.00 mm;
  field curvature 1.2 µm for both; total track 66.2 and 87.1 mm.
- **zoom-20x:** polychromatic RMS spot at 0.8 field 19.80 / 7.17 / 25.55 µm for the starting lens and
  10.07 / 7.01 / 10.25 µm for the result; and for the result, distortion 24.01 / 4.67 / 0.67 %,
  lateral colour 5.49 / 1.00 / 26.43 µm and focal lengths 4.500 / 20.120 / 90.000 mm.
- **vision-ab:** total track 79.9 / 70.0 / 100.0 mm for lenses A, B and D and 221.3 / 140.3 / 185.0 mm
  for C, E and F; focal lengths 20.00 to 20.02 mm; no clearance violations on any of the six.

## What is not here

- **The script does not read every figure on every page.** It does not compute the on-axis focus
  spread across wavelengths on the local-model page, the per-wavelength on-axis Strehl and the
  thickness-to-diameter figures on the vision A/B page, or the edge-thickness and air-gap figures on
  the zoom page.
- **The zoom result is a candidate, not a promoted design.** The server refused to promote it; the
  zoom page says why. `result_zoom-20x.zmx` is the candidate the page's figures describe.
- **Tool-call transcripts.** The stage logs are the agent's own summaries.
- **A repeat will not give the same lens.** Running a brief again gives a different result; the
  pages say how many runs each claim rests on.
