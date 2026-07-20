# OptiVibe — driving Zemax OpticStudio with Claude

OptiVibe is an MCP server that lets you (the agent) drive Zemax OpticStudio for
optical lens design. You reason about the design; the typed tools take the verified
action against the ZOS-API. This file is your operating guide — read the session and
trust models first, they change how you must interpret every tool result.

For install and setup, see [README.md](README.md). For data provenance and licensing,
see [PROVENANCE.md](PROVENANCE.md).

## The session model — read this first

- **Single seat (N = 1).** One OpticStudio engine, one design at a time. Calls are
  serialized; do not assume concurrency.
- **Lazy engine-open.** The engine seat is taken on the *first design-touching tool
  call*, not at startup. Reference and lookup tools run "cold" with no engine open.
- **Reaping is automatic.** The engine is closed for you on shutdown — you never manage
  the process lifecycle, never call a connect/close yourself.

## The trust model — proof, not absence-of-error

- **A clean call is not proof of success.** Trust the *read-back value* or the
  *optimizer verdict* the tool returns — never merely that the call did not raise.
- **Tools return a uniform envelope and never raise.** Inspect `result.ok` and the
  read-back, not an exception. A tool that couldn't do what you asked tells you so in
  its envelope (often with a typed `error_family`); handle that, don't assume success.

## The design workflow

A typical session: load or build a starting point → set what's variable → build a
merit function → preflight → optimize → assess → checkpoint. The discipline that keeps
this honest:

### Optimize discipline

- **Preflight before you run.** Set variables (`set_variable`) and a merit function
  (`build_merit` or your own operands), then `dry_run` to confirm readiness *without*
  opening the optimizer.
- **The stop must be free.** `optimize` / `dry_run` default-refuse a glass-vertex
  aperture stop — run `normalize_stop` first (or pass `auto_normalize`). A front-vertex
  glass stop is also handled by `normalize_stop` (it inserts a zero-thickness dummy air
  stop ahead of the front glass); do not hand-build a dummy stop.
- **Call `optimize` once; it owns the loop.** It runs monitored optimization internally
  and returns a verdict (`improved` / `diverged` / …). Read the verdict — do not
  hand-run cycles.
- **Uncomputable merit.** If a wide-field / fast merit can't be evaluated (a corner ray
  won't trace at full pupil), apply corner vignetting (`set_vignetting`), then *rebuild*
  the merit (`build_merit`) and re-seed from a gentler form before re-running.

### Checkpoint discipline

- **Snapshot the start.** Before optimizing, `save_snapshot` (or `save_candidate`) the
  starting form — there is no reliable in-session undo. `optimize` also auto-captures a
  per-pass `.zmx` trail.
- **Save keepers.** After each accepted `optimize`, `save_candidate` the result; once
  you have a keeper, `promote_best` it. Reuse one design name so a design's candidates
  stay together.

## Grounding — the reference layer

Do not guess an operand code, a glass name, or a typed member. Resolve them from intent
through the reference tools:

- `lookup_operand` — merit-function operands by intent.
- `search_reference` — full-text search over the OpticStudio manual.
- `lookup_glass` / `find_glasses` / `find_glass_pair` — glass by name, by property, or
  an achromatic pair.

The reference layer is session-free (it answers without taking the engine seat) and its
data is built locally from your own licensed install. If a piece isn't built, the
affected tool returns a typed "unavailable" envelope and the rest keeps working — treat
that as "not built here", not a failure.

## Manufacturability & judgment

The tools carry design judgment, not just API access — lean on it:

- `build_merit` authors positive manufacturability floors by default
  (`min_air` / `min_glass`); it won't hand you a knife-edge design.
- `check_clearance` audits center/edge thickness and clearances after optimization.
- `save_candidate` / `promote_best` gate a keeper on clearance — a manufacturably-thin
  design can't be promoted silently.

Surface manufacturability as *feedback* to the user; the user owns the acceptance bands,
not the tool.

## What you can do

The harness spans a full sequential-design workflow:

- **Lens data** — surfaces, apertures, fields, wavelengths, stop, glasses and catalogs,
  per-surface apertures / obstructions.
- **Geometry** — coordinate breaks, fold mirrors, reflective surfaces, diffraction
  gratings, even/odd/extended aspheres.
- **Multi-configuration** — zoom / focus / conjugate / array systems, config-spanning
  merit and optimization.
- **Merit & optimization** — the wizard and hand-authored operands, derived/relational
  operand math, DLS and Hammer optimization.
- **Analysis** — first-order, spot, MTF, wavefront, Strehl, distortion, relative
  illumination, axial/lateral color, collimation.
- **Tolerancing** — sensitivity and Monte-Carlo manufacturability verdicts.
- **Figures** — headless meridional layout rendering with per-field rays.

## Skills

Reach for these Claude Code skills for common workflows — describe the task and the
skill is selected:

- **`design-review`** — assess a design against a spec.
- **`design-compare`** — rank designs side by side.
- **`optimize-loop`** — set up, preflight, and run optimization with checkpoints.
- **`zos-api-debug`** — troubleshoot an OpticStudio connection / license error.
