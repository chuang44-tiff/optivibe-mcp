# Changelog

All notable changes to **optivibe-mcp**.

Versions are tagged in this repository (`v0.1.0` … `v0.2.0`). Each tag points at the merge
commit that published that version, and every tag was verified against the version literals
in the tree at that commit.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-10-23

OptiVibe installs with one command. The server and the reference layer are published on PyPI,
and the server is listed in the MCP Registry.

### Added

- **`pip install optivibe-mcp`** installs the MCP server and the reference layer it composes
  (`optivibe-reference` comes as a dependency). Three console commands come with it:
  `optivibe-mcp` (the stdio server), `optivibe-doctor` (the install check) and
  `optivibe-build-data` (builds the reference data from your own licensed OpticStudio install,
  with no clone; `--probe` needs the `optivibe-reference[probe]` extra, which the server install
  already carries).
- **`get_operand` adds a `paraxial_caution` to `ISNA` and `ISFN`.** On a fast lens the paraxial
  image-space NA reads below the real one (and `ISFN` above the working F-number); the caution
  points at `WFNO` and at `lookup_operand`. The value is unchanged. `lookup_operand` carries the
  same caution on its `ISNA` and `ISFN` entries.
- **`get_mtf` proves which series is the diffraction limit.** It turns the diffraction-limit
  curve on for its own run (the saved setting is not changed) and names series 0 the limit only
  when the engine's description of it says so. New fields: `diffraction_limit_shown` and
  `diffraction_limit_note`; `diffraction_limit_series` is null when the curve could not be
  proven, and `validity_warning` then carries the note.
- **`trial_glass_swaps` sizes its own budget and reruns an unfinished set once.** Without
  `time_budget_s`, the control runs first and each swap set gets a budget sized from the
  control's time to rest. A set that stops on its time budget is rerun once from its checkpoint
  after all sets have run, and the rerun gives the verdict. The new `budget` field says how each
  budget was sized, cut or rerun. An explicit `time_budget_s` still wins.

### Changed

- **The harness distribution is renamed `optivibe-harness` → `optivibe-mcp`.** The import
  packages (`optivibe_harness`, `optivibe_doctor`) are unchanged. In a clone, install the
  reference package first (`pip install -e packages/optivibe-reference`), then
  `pip install -e packages/optivibe-harness`; uninstall an old `optivibe-harness` first.
- **The data build moves into the reference package.** The glass, manual and probe scripts under
  `packages/optivibe-reference/scripts/` keep their command lines and now call the package
  modules. In an installed package the live-probe captures go
  under the package's `data/captures` folder.
- **The doctor names the `optivibe-mcp` distribution**, and its missing-data remedy tells an
  install without a clone to run `optivibe-build-data`.
- **`optivibe-mcp` pins `optivibe-reference` to the same version**, so the server and its
  reference layer always install as a matched pair.
- **`set_zoom` (array mode) moves the whole span.** The decenter is written on the surface before
  `first`, so surface `first` moves with the rest of the span (the same arguments give different
  geometry than before). `first=1` with a lateral decenter is refused up front. The reach check
  holds the Automatic semi-diameters of the span at their configuration-1 values, so a beam that
  walks off is refused as `array_unreached` and rolled back; the new `reach_apertures` field says
  which surfaces were held. The image surface is not held, so check the image landing yourself.
- **`add_return_cb` on a surface that is already a coordinate break no longer overwrites a solve
  silently.** A Par cell holding a non-Fixed solve, or one that cannot be read, is refused as
  `cb_solve_loss` with nothing written, unless `replace_solve=true`; the result then lists
  `replaced_par_solves`, `preserved_par_solves` and `unreadable_par_cells`. A cell that already
  holds the same pickup is not a loss. `par_solve_not_checked` and `par_solve_reason` are removed;
  `par_solve_loss_audited` is true instead.
- **`remove_surface` reports `repointed_to_successor`** when a cocentric, center-of-curvature,
  compensator or position solve that referenced the removed surface now points at the surface that
  slid into its place. A PickupChiefRay Par cell is no longer reported as unscanned.
- **`save_candidate` deletes the `.ZDA` companion its own save produced.** `zda_present` is false
  after a normal save (null when it cannot be read). A companion that existed before the save is
  kept and flagged `zda_preexisting`; a failed delete adds `zda_removal_error`.
- **`promote_best` names a scope mismatch.** When the picture's record was written at different
  floors than the promote, `best_png_reason` reads `png_record_scope_mismatch`;
  `png_production_record_not_written` now means only that no record was written.
- **`render_layout` proves `out_of_plane` from geometry too**: a surface vertex or a traced ray
  point off the drawing plane makes it true, so tilted mirrors and X decenters now read true. It is
  null, with a disclosure line, when a surface frame could not be read. `out_of_plane_scope` says
  what was read. A far-object view whose end gap is negative reports `virtual_conjugate_no_cut`,
  and on an unfolded design the fill ignores coordinate breaks and undrawn dummy rows.
- **`tolerance` and `load_design` no longer warn about a real design saved under the temp
  folder**; the harness's own checkpoint files still warn. The warning for a last glass element
  that touches the image now says it is a standing limit and how to cover it (a zero-thickness air
  surface after the last glass).

### Fixed

- **The reference tools' "not built" errors name `optivibe-build-data`** (and the clone script
  second), so an install without a clone is told a command it has.
- **`optivibe-build-data` says when your OpticStudio's operand list differs from the curated
  one.** The merit and tolerance catalogs build only for the operand set their curated data
  covers; on another set the step now says so instead of asking you to run `--probe` again. The
  manual step no longer fails when only one of the two operand captures is present. The clone
  script `scripts/build_vendor_data.py` behaves the same.
- **`set_field` (replacing all fields) reports a partial rebuild.** An engine error mid-rebuild
  now says how many new fields were added and how many old ones were deleted before it stopped.
- **A failed configuration restore is no longer lost.** When a tool that switches configuration
  fails and the switch back does not verify, the error carries the restore warning.
- **The stdio log follows the workspace root the server anchored at start**, not a later change
  to `OPTIVIBE_WORKSPACE_ROOT`.
- **`release_engine` with `kill_others=true` no longer calls an engine whose command line it
  cannot read "interactive".** It is listed in `cmdline_unreadable`, never killed, and counted
  as still running.
- **`set_solve` refuses a ChiefRayAngle solve that cannot act**: on a mirror, or on a radius with
  no refractive power for the chief ray (a stop dummy, a plane or air-air surface), it is refused
  before anything is written. It was accepted, changed nothing, and could be reported as a match.
- **`set_solve` verifies negative MarginalRayHeight and ChiefRayHeight targets** like positive
  ones, instead of declining to check them.
- **`tolerance` works on a design saved by `save_candidate` or `save_snapshot`.** The save hands
  the engine a forward-slash path; before, the next `tolerance` could return an empty report.
- **A tilted object surface no longer makes a save or promote clearance verdict indeterminate**;
  only a sag-unfaithful surface that bounds a measured gap does.
- **Folded layouts draw the image plane and the stop ticks in the surface's own tilted frame**,
  not as vertical lines.

## [0.1.18] — 2026-10-22

Clearer reports and stricter refusals across the analysis, edit, solve and glass tools, a reworked
glass step, and a new glass-picking demo.

### Added

- **`render_mtf_vs_field` takes a `config` parameter** (switch to that configuration, render,
  restore) and `include_plot_model=true` returns the drawn line data.
- **`get_mtf` returns the engine's messages** (`messages`, and `engine_messages_unread` when they
  could not be read) and a `diffraction_limit_series` field. When the analysis slot it needs is
  held, it skips the diffraction-limit check and says so.
- **`get_spot` and `get_mtf` flag a collimated output** with `collimated_output: true` and a
  message.
- **Edits report what else they removed.** `set_field` (replacing all fields) and `remove_surface`
  report deleted and renumbered multi-configuration rows (`mce_rows_deleted`); `remove_surface`
  also lists merit and tolerance rows that name surfaces by number (`by_number_refs`).
  `serialize_merit` marks a range that ends at the last-surface ceiling (`range_at_ceiling`).
- **`find_glasses` ranks on the partial-dispersion anomaly**, Pg,F − (0.6438 − 0.001682·vd),
  as a column, a sort (`sort=pg_f_anomaly`) and a window (`pg_f_anomaly_min` / `_max`), and says
  when a list was cut (`truncated_note`).
- **`trial_glass_swaps` has a screen mode.** With `drop=false` no swap is cut behind its
  control; every result lists the swaps not yet measured (`open_arms`, each with the action that
  closes it) and how many glass slots of the lens have been tried (`coverage`).

- **`release_engine` frees the single engine seat.** It closes this server's OpticStudio engine
  and confirms the process exited; the next harness call reopens one. Save first: the lens in
  memory is lost. With `kill_others=true` it also kills every other OpticStudio server engine on
  the machine, busy or not, so the agent asks you before using it; an interactive OpticStudio is
  never touched. A process that will not exit fails with `engine_release_failed`.

### Changed

- **The server instructions say to release the seat when done**, instead of "you never manage
  process lifecycle".
- **The `design-glass-matching` skill is reworked.** It names the reason first (primary colour,
  secondary colour, or a monochromatic term) and the glass property that moves it, screens every
  glass slot once before going deep, picks candidates by the anomaly, and judges a swap only after
  the swap and its control are both re-optimized to rest. `optimize-loop` now calls a lens at rest
  only when a call of 50 or more cycles gains under about 0.5 % of the merit, twice in a row.
- **The glass-picking demo is replaced.** [demos/glass-picking](demos/glass-picking/README.md)
  now compares the current glass step with OpticStudio's Hammer optimizer on the 10× and 20×
  objectives.
- **`trial_glass_swaps` no longer refuses a trial whose focal length, F-number or image-space NA
  ends more than 1 % off the control's.** The trial is measured and compared as usual and its row
  carries `first_order_warning`, saying which value moved. `first_order_off_control` now means
  only that a first-order value could not be read.
- **A derived helper row in a math constraint defaults to weight 0** when another row references
  it, so it no longer pulls on the merit by itself. A weight you state is kept.
- `verify_beam_path` leaves out `n_surfaces` when a figure is attached.
- A relative workspace root is made absolute against the launch folder at startup, and
  `save_candidate` audit rows carry `figure_scoreable`.
- `analyze_strehl` and `analyze_wavefront` now say the best-focus restore holds to a relative
  tolerance of 1e-9, not exactly.

### Fixed

- **Operand slots out of range are refused.** `get_operand` and the operand reader used to
  return the engine's silently clamped value for a surface, wavelength or field number past the
  end; they now refuse (`operand_param`) and name the legal range.
- **`analyze_axial_color` keeps its result when the focal-shift curve is bad.** A non-finite
  value in the curve used to fail the call (`analysis_empty`) or return `"nan"` strings; now the
  scalar result is kept, the curve is dropped, and `axial_color_curve_degraded` says why.
- **A folder that refuses writes no longer hangs a figure or a save.** Every temporary file the
  tools create in a caller's folder is bounded; an unwritable folder now gets a refusal naming it.
- **`surface_solve` / `set_solve` fail closed** when a fault after the write leaves the
  relation unreadable (`relation_semantics_unavailable`), and the restore message says "not
  verified" when equality could not be decided.
- **`record_findings` refuses a finding on a figure that cannot be scored**
  (`finding_figure_unscoreable`), instead of accepting a judgement nothing could later check.
- **`freeze_semidiameters`** reports `active_configuration_restored` as null when the
  configuration could not be read back, instead of claiming it was restored.
- **`trial_glass_swaps` names every catalog clash in one refusal**, with a `rebind` list, instead
  of stopping at the first and needing one resubmit per clash.
- **`lookup_glass` and `find_glass_pair` refuse a catalog made only of dots** (`.`, `..`, `.AGF`)
  as a parameter error, instead of reporting that no glass matched.

## [0.1.17] — 2026-10-20

The agent instructions move to `AGENTS.md`. No tool or behaviour changes.

- **`CLAUDE.md` is now `AGENTS.md`.** The operating guide at the repository root and the guides
  in `packages/optivibe-harness/` and `packages/optivibe-reference/` are renamed to `AGENTS.md`,
  the file name other coding agents look for, so every agent reads the same instructions. The
  text is unchanged apart from the two package titles and one cross-reference. Claude Code reads
  `AGENTS.md` by itself from version 2.1.277 when a folder has no `CLAUDE.md`; on an older
  version, put a one-line `CLAUDE.md` containing `@AGENTS.md` beside each `AGENTS.md`.
- The two package READMEs and one source comment now point to `AGENTS.md`.

## [0.1.16] — 2026-10-19

A housekeeping release: no new tools.

- **`remove_surface` reports an unverified removal honestly.** When the surface count read back
  after a removal does not match, the error now says the removal "could not be verified" (the
  row may really be gone) instead of "did not take effect", and it ends with the
  solve-reference scan taken before the attempt: which solves referenced the surface, that none
  did, or that the scan could not run.
- **Glass searches refuse a catalog item that names no catalog.** An item such as `/` or `\`
  used to read as "no catalog given", so `find_glasses(catalog="/")` searched every catalog and
  `"SCHOTT,/"` silently dropped the `/`. `find_glasses`, `find_glass_pair` and `lookup_glass` now
  refuse it and name the item. A blank value still means no catalog.
- **CDS, KDP and TEO2 in the MISC catalogs read as birefringent**, like their copies in the
  birefringent catalog, so the default glass searches no longer offer them as lens glass. Rebuild
  your local glass catalog to pick this up.
- **`trial_glass_swaps` names the control in its messages.** A restore or abort on the unchanged
  control now says "after the control" or "stopped at the control" instead of "trial 0".
- **`build_vendor_data.py` skips the manual step when PyMuPDF is missing.** Without the
  `[manual]` extra the step used to report FAILED and exit non-zero. It now reports SKIPPED with
  the install command, and the operand and tolerance catalogs build synonyms-only, as the README
  describes. Any other missing module still fails the step.
- **`doc/glass-matching/`** states the reference-wavelength rule in its conventions table.

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

[0.2.0]: ../../compare/v0.1.18...v0.2.0
[0.1.18]: ../../compare/v0.1.17...v0.1.18
[0.1.17]: ../../compare/v0.1.16...v0.1.17
[0.1.16]: ../../compare/v0.1.15...v0.1.16
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
