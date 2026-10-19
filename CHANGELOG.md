# Changelog

All notable changes to **optivibe-mcp**.

Versions are tagged in this repository (`v0.1.0` … `v0.1.15`). Each tag points at the merge
commit that published that version, and every tag was verified against the four version
literals in the tree at that commit.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.15] — 2026-10-19

- **Glass tools.** Two new tools help an agent choose glass once a lens form has settled, and the
  two glass searches gain new controls:
  - `explain_glass_swaps` is read-only. It lists which glass elements carry the lens's colour
    and Petzval terms, and which swaps would move them.
  - `trial_glass_swaps` tries up to four swap sets. Each set is re-optimised on its own time
    budget, beside an unchanged control, and judged on measured image quality. A set that falls
    far behind its control and cannot catch up is dropped early.
  - `find_glass_pair` and `find_glasses` now return lens materials by default and take a vendor
    list; `find_glasses` also says how many glasses matched and takes a sort order.

  The harness now has 94 tools; with the five reference tools the server serves 99.
- **Hold the two glass tools that edit a design behind a permission rule.** `trial_glass_swaps`
  edits the open lens while it runs and then restores it; `substitute_glass` leaves its change in place. The README shows a Claude Code `ask` rule that makes
  each call wait for your OK. In a headless (`claude -p`) or `dontAsk` run, a call that matches
  an `ask` rule is denied, not asked.
- **New skill `design-glass-matching`.** It runs the glass step: read the glass table, choose
  swap sets, trial them, and keep a swap only when overall image quality improves at every field.
- **`doc/glass-matching/`** gives the glass-matching math with sources, and a script that
  reproduces its worked examples.
- **Skill updates.** `optimize-loop` leaves glass to the end (it hands a settled lens to
  `design-glass-matching`), reads Strehl with `analyze_strehl(fields="all")`, treats a time-budget
  stop as not converged, cuts an unpromising run early, and reads the `_unphysical` /
  `_unverified` verdict suffixes. `design-aberration-review` adds a second ray fan at a fixed
  scale of three Airy radii and a readable-or-stuck verdict on the form. `design-vision-review`
  follows the new edge rule below, and a reported finding blocks promotion until it is answered.
- **Changed: `check_clearance` reads a glass element's edge at each face's own rim.** For an
  unfolded glass gap, the edge is now the thickness where the wider face reaches its own
  semi-diameter, which is OpticStudio's own edge thickness. Edges read thinner on most
  elements than before. The old reading is reported as `edge_thickness_min_aperture` for this
  release only, for comparison; it never decides a pass. Air gaps and folded systems are read
  as before.
- **Changed: `analyze_axial_color`'s `f_minus_c_shift_mm` now holds F minus C, as its name
  says.** Earlier versions returned it with the opposite sign.
- **Changed: the best-focus reading refuses a plane it did not find**, instead of returning a
  pinned or unmoved plane as a valid reading.
- **Changed: `get_operand` refuses a parameter the operand has no column for.**
- `optimize` takes a time budget for one call's optimizer run, and says what ended its pass loop
  (`stop_reason`).
- `analyze_strehl` takes `fields` and reads several fields in one call; every call also returns
  the polychromatic Strehl (`polychromatic`).
- `render_ray_fan` takes an optional fixed `scale_um`; samples beyond it are marked and listed.
  Every fan now reports the Airy radius.
- `substitute_glass` reports which catalogue the engine actually used, flags a name held by more
  than one loaded catalogue, and takes an optional `catalog`.
- `check_clearance` also reports the back gap's reading at the lens rim (`rim_edge`).
- Fix: calcium fluoride and the lowest-dispersion glasses have their Abbe number back.
- Fix: the doctor no longer warns falsely about Pillow, and a piped report is readable.
- Fix: findings from the 0.1.14 public review (an unflagged empty cross-check, a dropped ranking
  column, and smaller items).

## [0.1.14] — 2026-10-12

- **Three new demos.** An agent reworks a 60× objective into a diffraction-limited 10× / NA 0.45
  apochromat with no human in the loop ([`demos/walk-in-10x/`](demos/walk-in-10x/README.md)); a
  local model meets the same targets through the same server, with a brief tuned for this task
  ([`demos/local-model-10x/`](demos/local-model-10x/README.md)); and an agent refines a 20× zoom,
  misses one target, and has its result held back by the promotion gate
  ([`demos/zoom-20x/`](demos/zoom-20x/README.md)). Each page lists its limits.
- **`render_ray_fan` and `render_field_curvature_distortion`** — two new plot tools, drawn by
  OpticStudio. Each saves the picture and, beside it, a `.data.json` file with the full numbers
  behind the plot. The harness now has 92 tools; with the five reference tools the server
  serves 97.
- **New skill `design-aberration-review`.** Two reviewers read the ray fan and the field
  curvature / distortion plots, with their numbers, and report how the aberrations are balanced.
  They advise; nothing they say blocks a promotion or edits the design.
- **`optimize-loop` walks in a hard target.** A demanding spec is reached by stages: the easier
  targets first, then the hard one moved in by re-weighting, with a review at each stage.
- **`check_clearance` measures folded systems.** Gaps across a fold are measured and
  cross-checked against a traced ray, so a healthy folded design promotes without `force`. A gap
  that cannot be measured is listed as not measured, never counted as clean.
- `analyze_strehl` takes a `field`, and `analyze_wavefront` takes `hx` / `hy`. Both now say which
  field they read (`field_evaluated`).
- `load_design` refuses a file that is not a `.zmx` before loading it, and proves each load
  against the file's own surface count and thicknesses (`load_verified`).
- **Changed: catalog bench column names.** `rms_wfe_worst_waves` is now `rms_wfe_axis_waves`, and
  `strehl_axis` is now `strehl_field1`, because that is what each column holds. The old names are
  still accepted as ranking inputs, but a saved rank template that names an old output column
  should be updated.
- **Fix:** `get_spot` with `per_wave` returned millimetres labelled as micrometres. It now
  returns micrometres. The default RMS spot is computed from a fixed dense pupil sample.
- **Fix:** the operand synonym table shipped in 0.1.12 and 0.1.13 with two entries repeated. No
  lookup answer was wrong; the table is clean again and the release checks now refuse a repeat.
- `get_operand` places each argument in the operand's own column and reports where it put it.
- `render_layout` labels the exit face of each glass element, and its description is shorter.
- Multi-configuration tools refuse a malformed configuration count or value instead of guessing.
- `search_reference` treats "catalogue" as "catalog".
- Skill corrections: `optimize-loop` no longer says a glass can be a variable (`set_variable`
  takes radius, thickness and conic); `bench-catalog` no longer names a constant or a progress
  stream that do not exist; `zos-api-debug` names the error families the server actually returns.
- Corrections to earlier entries below: the 0.1.12 line about the engine-session ledger, and the
  command name of the doctor in 0.1.3.

## [0.1.13] — 2026-10-07

- **The layout review changes the lens.** In a small A/B test (three design runs per arm, same
  brief, same model), every run that used the layout-figure review produced a shorter lens than
  every run without it: 70–100 mm total track against 140–221 mm. Its thinnest elements were
  thicker, axial Strehl stayed at 0.93 or better at every wavelength, and no lens had a
  clearance violation. Three runs per arm is a pattern, not a rate, and the review was tested
  together with the brief's wording, not apart from it. The full comparison is in
  [`demos/vision-ab/`](demos/vision-ab/README.md).
- **`render_layout` draws with OpticStudio's own layout by default**, with the surface labels
  placed on it. `renderer="native_3d"` and `renderer="native_shaded"` add 3-D views, and the
  previous self-drawn figure is still available with `renderer="self"`.
- The layout figure reports which of the rays OptiVibe traces reach the image (`ray_coverage`),
  and `save_candidate` stores that with the candidate. On the default native drawing these are
  OptiVibe's own sampled rays, not a reading of the rays OpticStudio drew. When a render is
  refused, or its picture cannot be published, `save_candidate` says why (`render_error`).
- A stuck OpticStudio analysis window now makes further analysis calls refuse with a clear error
  instead of hanging.
- `optimize-loop` and `design-vision-review` skills: with no human to steer, the agent acts on a
  review finding or declines it with a measurement that answers it, instead of waiting.
- **Fix:** the FFT MTF frequency axis is cycles/mm for every lens unit. `get_mtf` and
  `render_mtf_vs_field` previously labelled it cycles per lens unit, which was wrong for lenses
  not in millimetres. `render_mtf_vs_field` now also accepts cm, inch and metre field heights.
- **Fix:** on Windows, concurrent writers no longer lose or tear lines in the append-only JSONL
  logs.
- `add_operand` and `edit_operand` disclose that a target reads back at 15 significant figures.
- **Upgrade step: rebuild the manual corpus once.** Its table-of-contents pages no longer become
  search hits, so `search_reference` answers from the body of the manual. The corpus format moved
  to version 4, and a corpus built by an earlier release is refused (`corpus_unavailable`) until
  you rerun `packages/optivibe-reference/scripts/build_manual_corpus.py` and restart the MCP
  server.
- Smaller fixes across the workspace and candidate naming, the MCP server's start-up, the doctor
  and the reference layer.

## [0.1.12] — 2026-09-26

- **`render_mtf_vs_field`** — MTF plotted against real image height, tangential and sagittal,
  one curve per frequency: the way lens manufacturers publish it. A reference curve set can be
  overlaid for visual context; no comparison is computed.
- Saved candidates are named for their design (`<design>_NNN_<label>.zmx`), and the layout
  picture is saved beside each one.
- The server instructions now fit the 2,048-character limit Claude Code applies to them. Rules
  that no longer fit moved into the descriptions of the tools they govern, so what the server
  sends and what an agent receives are the same text.
- `add_math_constraint` checks an operand's sign convention against the reference layer before
  authoring it.
- The interaction log records which MCP client made each call.
- `render_layout` no longer tells agents that the native export writes text; it writes real
  PNGs.
- `build_operand_semantics.py` writes 1-based manual page citations, matching the rest of the
  reference layer.
- New dependency floor: `matplotlib>=3.6`.
- **Known limitation:** the workspace root is fixed when the server starts
  (`OPTIVIBE_WORKSPACE_ROOT`, or the launch directory). Changing it requires restarting the MCP
  server.

## [0.1.11] — 2026-09-17

- Element outlines in the layout figure.
- Sharper manual search in the reference layer.
- **MCP tool validation error on GitHub Copilot fixed** — thanks to
  [@ktgw0316](https://github.com/ktgw0316) (Masahiro Kitagawa) for the first external
  contribution to this project ([#12]).
- Review round: the operand inventory is user-built rather than tracked, and two documented
  claims the code did not support were corrected.

## [0.1.10] — 2026-09-16

- A vision review for the layout figure, acting as an advisor to the design loop.
- Review round: an unreadable-manifest remedy that mis-scoped the one row class which is not
  design-scoped, and a finding-recording path that retired the budget about to judge it.

## [0.1.9] — 2026-08-21

- A preflight that refuses a negative merit weight.
- A merit-row normalizer hardened over five rounds.
- Review round: `OverflowError` escapes closed.

## [0.1.8] — 2026-08-19

- The merit-function layer: authoring guards, a range linter, and a self-scan that reports its
  own coverage honestly.

## [0.1.7] — 2026-08-17

- Solve-reference disclosure and surface-solve observability repairs.

## [0.1.6] — 2026-08-16

- The surface-solve constraint layer.
- A policy exclusion that was reported through the fault channel, making one asphere row break
  strict mode.

## [0.1.5] — 2026-08-12

- A promoted keeper is now bound to the audit that measured it.
- A dead engine channel refuses instead of returning a misleading answer.
- The layout figure no longer invents apertures.

## [0.1.4] — 2026-08-05

- The catalog bench.
- Verdicts no longer certify more than they measured.

## [0.1.3] — 2026-07-30

- **The doctor (`python -m optivibe_doctor`)** — a self-diagnostic that names why an install is broken.

## [0.1.2] — 2026-07-28

- Pinned the `mcp` dependency.
- Added an engine-free test suite and Windows CI, so the package can be tested without an
  OpticStudio seat.

## [0.1.1] — 2026-07-28

- Gradient-index (GRIN) media support.

## [0.1.0] — 2026-07-19

- Initial public release of optivibe-mcp.

[0.1.15]: ../../compare/v0.1.14...v0.1.15
[0.1.14]: ../../compare/v0.1.13...v0.1.14
[0.1.13]: ../../compare/v0.1.12...v0.1.13
[0.1.12]: ../../compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/chuang44-tiff/optivibe-mcp/releases/tag/v0.1.0
[#12]: https://github.com/chuang44-tiff/optivibe-mcp/pull/12
