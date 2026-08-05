---
name:
description: Use when the user wants to benchmark / rank a FOLDER of lens designs (or patents) against
  a target spec or each other. Triggers on "benchmark these lenses", "rank this folder of designs",
  "compare these patents/zmx against a 50mm f/1.4", "which of these is best for <spec>".
---

# Bench-Catalog — Folder → Normalized Matrix + Plots + Ranking

## Overview

Turns a FOLDER of lens designs (`.zmx` files and/or patents) into a normalized metric
matrix (`catalog_metrics.csv`), a provenance manifest, two figures (a bar-per-KPI panel
and a labeled layout montage), and a reproducible ranking against a target spec or the
folder's own median. **You (the model) recommend; the user is the judge.**

Distinct from `design-compare` (2–4 designs the user hand-lists, live MCP, a table only):
Is a **folder → CSV + manifest + plots + ranking**, batch, patent-capable,
and **single-seat-clean via a thin CLI** — you never hold the OpticStudio seat during a
bench cycle.

## The fixed workflow (never flexes)

```
prompt → TEMPLATE  →  INGEST (classify zmx vs patent; deep-research build) → stage (native scale)
        →  run the CLI (build + bench + render + plot + rank, ONE engine, reaped)
        →  read CSV + manifest + ranking_scores.json → NARRATE → present matrix + graphs + ranking
```

## Step 1 — prompt → template

Map the user's ask to a `template.json` written to `<workdir>/out/template.json`:

```json
{
  "target_basis": {"efl": 50.0, "fnum": 1.4, "fov": 19.0,
                   "wavelengths": [[0.4861,1.0],[0.5876,1.0],[0.6563,1.0]], "mtf_frequency": 50.0},
  "metrics": ["first_order","rms_spot","mtf","strehl","clearance"],
  "plots": ["bar_kpi","montage"],
  "ranking": {"priorities": ["mtf_worst","total_track_mm"], "weights": {"mtf_worst": 3}}
}
```

- `target_basis` keys are ALL optional. **Target given** (common): parse EFL, f/#, FOV
  (a scalar half-field angle in deg, or an explicit `[[x,y,w],…]` list), wavelengths,
  application. Only pin `fnum` if the user pins it; set `wavelengths` only if stated
  (else native/photopic F/d/C `[0.4861,0.5876,0.6563]`); `mtf_frequency` from the ask
  else **50 cyc/mm** (locked default — all designs share the basis EFL so one frequency
  is comparable). Ask about frequency ONLY if the user names a sensor/pixel context.
- **No target** → OMIT `target_basis.efl` → the bench runs a read-only median-EFL census
  (EFL = median of finite, non-afocal native EFLs; FOV = 3 angle fields at the median
  max-semi-field; f/# native). Just leave the key out.
- **Application → sensible defaults** (your judgment, STATED to the user before the run):
  "machine vision 25mm f/2.8" → efl 25, fnum 2.8, modest FOV, prioritize distortion +
  corner MTF; "mobile" → compact + rel_illum + distortion. State the derived template and
  let the user correct it before the (multi-minute) CLI run.

### The metric menu — the EXHAUSTIVE ALLOWLIST

Map intent to metric keys ONLY from this menu. An unknown/unsupported requested metric →
**CLARIFY or DECLINE** (name the menu) — **NEVER a silent nearest-guess**.

<!-- MENU_TABLE_START -->

| registry key | tool | CSV columns | dir | intent it serves |
|---|---|---|---|---|
| `first_order` | get_first_order | `fnum`, `total_track_mm`, `bfd_mm` (+ fixed `native_efl_mm`/`norm_efl_mm`) | f#↓ track↓ | focal length, f/#, size/compactness, back focus |
| `rms_spot` | get_spot | `rms_spot_um_worst`, `rms_spot_worst_field`, `rms_spot_valid_fields` | ↓ | geometric blur / spot size, worst field |
| `mtf` | get_mtf | `mtf_worst`, `mtf_worst_field` | ↑ | sharpness / resolution / contrast at the basis frequency |
| `strehl` | analyze_strehl | `strehl_axis` | ↑ | on-axis diffraction quality (Strehl) |
| `wavefront` | analyze_wavefront | `rms_wfe_worst_waves` | ↓ | RMS wavefront error |
| `distortion` | analyze_distortion | `distortion_max_pct` | ↓ | geometric distortion |
| `rel_illum` | analyze_relative_illumination | `rel_illum_min` | ↑ | relative illumination / corner brightness / vignetting |
| `lateral_color` | analyze_lateral_color | `lateral_color_max_um` | ↓ | lateral (transverse) chromatic aberration |
| `axial_color` | analyze_axial_color | `axial_color_fc_mm` | ↓ | axial (longitudinal) chromatic aberration |
| `aspheric_profile` | analyze_aspheric_profile | `asphere_max_departure`, `asphere_slope_diff`, `asphere_bfs_radius` | ↓ | asphere manufacturability (departure/slope) |
| `clearance` | check_clearance | `clearance_status`, `n_clearance_violations`, `min_gap_mm` | gap↑ | edge/center thickness manufacturability |
| `collimation` | verify_collimation | `collimation_verdict`, `worst_residual_mrad` | ↓ | afocal / collimation quality |
| _(always)_ | _bench-derived_ | `n_elements` | ↓ | element count / complexity |

<!-- MENU_TABLE_END -->

The exact `tool_name` is a 1:1 alias for its key (`get_spot` → `rms_spot`) — an explicit
tool name in the ask is honored. `n_elements` is ALWAYS emitted (bench-derived) — the user
never selects it, and it has **no registry key**. The **default template** (unspecified
metrics) = `["first_order","rms_spot","mtf","strehl","clearance"]` (+ `n_elements`).

The machine-readable menu (must equal the 12 registry keys above — the served-boundary
allowlist; the drift test parses this block):

<!-- BENCH_CATALOG_MENU_KEYS
["first_order","rms_spot","mtf","strehl","wavefront","distortion","rel_illum",
 "lateral_color","axial_color","aspheric_profile","clearance","collimation"]
-->

### The intent → key phrase map

<!-- PHRASE_MAP_START -->

| user phrase (examples) | keys |
|---|---|
| sharpness · resolution · contrast · "corner MTF" · MTF | `mtf` |
| spot · blur · spot size · geometric | `rms_spot` |
| Strehl · diffraction-limited · on-axis quality | `strehl` |
| wavefront · WFE · OPD | `wavefront` |
| distortion · straight lines · barrel/pincushion | `distortion` |
| vignetting · relative illumination · corner brightness · falloff | `rel_illum` |
| lateral color · transverse chromatic · color fringing | `lateral_color` |
| axial color · longitudinal chromatic · focus shift with color | `axial_color` |
| aspheric quality · sag departure · asphere manufacturability | `aspheric_profile` |
| manufacturability · edge/center thickness · clearance · thin elements | `clearance` |
| compact · small · short · total track · size | `first_order` |
| simple · fewest elements · complexity | `first_order` |
| collimator · afocal · beam quality | `collimation` |

<!-- PHRASE_MAP_END -->

Mapping rules:
1. Explicit metric/tool names → resolve directly (allowlist).
2. Phrase matches → the mapped keys (union; dedup).
3. A phrase with NO menu match (e.g. "veiling glare", "ghosting", "stray light",
   "thermal") → **CLARIFY, do not silently substitute a neighbor**: "I can profile these
   metrics: <menu>. I don't have a '<phrase>' analysis — proceed with the default set, or
   pick from the menu?" NEVER emit a key that is not on the menu.
4. Nothing selected → the lean default template.

## Step 2 — ingestion

Create `<workdir>/{inputs,staging,out}/`. Classify each input:

- **`.zmx`** (case-insensitive) → a Zemax design → `copy` VERBATIM into `staging/`
  (NATIVE scale; scaling is the bench's job). A stem collision suffixes `-2` (noted in
  the report).
- **patent / literature** (a `.pdf`/`.txt`/`.md`, a patent number `US-XXXXXXX`, a URL, or
  a prose design description) → the deep-research path below.
- **anything else** (unreadable / unknown) → SKIPPED with a REPORTED note (never a silent
  drop).

### Patent → `staging/<stem>.spec.json` via `deep-research`

For each patent/doc, invoke the **`deep-research` skill** (or dispatch it as a subagent for
parallel fan-out) with a prescription-specific ask:

> "Extract the full lens PRESCRIPTION for <patent id / title>, embodiment <n if known>: a
> surface-by-surface table (surface #, radius mm, thickness mm, glass/material,
> semi-diameter, conic), the stop surface, the system aperture (EPD or f/#), the field/FOV,
> and the design wavelengths. Return the numbers, cite the source."

Convert the returned prescription into an **`apply_lens_spec`-shaped `LensSpec` JSON** at
NATIVE scale (surfaces + aperture + fields + wavelengths — match the `apply_lens_spec`
schema exactly). For a patent glass not resolvable directly, use the **seat-free**
reference tools `lookup_glass` / `find_glass_pair` to map it to an available catalog glass
(or the nearest nd/vd match) — record the substitution in the spec's notes. Write
`staging/<stem>.spec.json`. The CLI Phase 1 turns each spec into `<stem>.zmx`.

**Extraction failure is honest:** if deep-research cannot return a full, buildable table,
write NO spec.json — record it in your running notes → surfaced in the final report as
"could not build: <patent> (<reason>)". Nothing faked.

## Step 3 — run the CLI (the single-seat discipline — LOAD-BEARING)

Run the CLI as a **detached background Bash** (`run_in_background: true` — no 600 s Bash
wall-cap; a killed foreground run would orphan the engine holding the single seat):

```bash
conda run -n optivibe-harness python -m optivibe_harness.catalog bench \
  --staging <workdir>/staging --out <workdir>/out --template <workdir>/out/template.json
```

You are notified on exit and read the stdout one-line JSON summary; the CLI streams
per-design progress to stderr for liveness.

**The single-seat discipline (a hard rule):** *during a bench cycle call NO seat-holding
OptiVibe tool* — no `load_design` / `apply_lens_spec` / `get_*` / `render_layout` /
`optimize` / any harness/design tool. **All engine work is the CLI's** (it opens ONE
engine, builds + benches + renders + reaps it in `finally`). Before the CLI runs you may
use ONLY the seat-free reference tools and `deep-research`:

- **`_SEAT_FREE_TOOLS`** (route to the reference dispatcher; never grab the N=1 seat):
  **`lookup_glass`, `lookup_operand`, `search_reference`, `find_glasses`, `find_glass_pair`**.

Keeping the skill seat-free makes the CLI the sole engine. **Whether a second OpticStudio
engine can start at all depends on the edition and licence in use** — where it can, it coexists
and loads fine (the `FRU__delta_init` banner is cosmetic). Either way **OptiVibe keeps to ONE engine** for
adaptability, so the bench CLI **SOFT-DECLINES** when an engine is already running: rather than
open a second engine it returns an `ok:false` `engine_busy` envelope (`rc 3`, a stderr remedy)
and benches nothing. It is a one-engine POLICY, not a license block. If EVERY design still fails
to LOAD after a bench — bad/relative staging paths (a relative `--staging` now auto-resolves
against your CWD, so this is rarer), a FOREIGN OpticStudio holding the seat, OR corrupt `.zmx` —
the CLI flags the run `all_designs_failed_load` (`rc 3`, a whole-run failure, artifacts kept as
diagnostics only; the attribution is NEUTRAL because the post-bench half has no ledger proof of a
seat). If the MCP engine was warmed earlier this session, recover by EITHER: (a) run the bench
from a **COLD session** (a fresh Claude Code session that made no design-touching MCP call), OR
(b) the **profile-via-held-seat fallback** — since you already hold the seat, profile each design
directly through the MCP tools (`load_design` + `set_ray_aiming(real)` for fast/displaced-stop
lenses + `set_aperture` `ImageSpaceFNum <f/#>` + the `analyze_*`/`get_*` tools) at the template
basis and hand-write `catalog_metrics.csv` + `ranking.md` (the sanctioned recovery — same
numbers, your engine). Before trusting ANY catalog artifact, check the CLI rc AND the top-level
JSON `ok`; a failed run's `load_not_found`/`load_failed` rows are an ENGINE/PATH symptom, not a
design verdict.

**Runtime expectation:** a bench is minutes-to-over-an-hour for a 30–50-design folder (each
design = ~2–4 loads + the full metric set; a no-target run adds a census pass; a warm
concurrent engine makes the CLI SOFT-DECLINE at preflight (rc 3, `engine_busy`) — see the
single-seat rule above). This is exactly why the CLI runs detached.

## Step 4 — rank + present

Read `out/ranking_scores.json` (the DETERMINISTIC ranked order — the CLI already computed
it), `out/catalog_metrics.csv` (the numbers), and `out/manifest.json` (status tokens, so
**null ≠ 0**). Write `out/ranking.md`: the deterministic ranked list VERBATIM (order +
scores), a per-design one-liner (wins on X, weak on Y, **"no data" on Z — NEVER "scored 0
on Z"**), the top recommendation citing the numbers + the layout form, and the explicit
"**the user is the judge**." You may DISCUSS but must NOT re-order the deterministic
ranking. Present `ranking.md` alongside the matrix + `out/catalog_bars.png` +
`out/catalog_montage.png`.

**Null-handling rule (the reproducibility contract):** a design missing a metric is flagged
**"no data"** for that axis — never scored 0 or perfect. A real `0.0` is a real measured
value. The `rank` scorer enforces this in code; your narrative must match it.

## Artifacts (the working dir)

```
<workdir>/
  inputs/                 # the user's raw inputs (reference copy)
  staging/                # native-scale .zmx (copied + patent-built) + *.spec.json
  out/
    template.json         # the resolved template (you write it, provenance)
    build_ledger.json     # patent-build outcomes {built:[…], failed:[…]}   (CLI)
    catalog_metrics.csv   # the tidy matrix — one row/design, null = empty cell, 0 = real (CLI)
    manifest.json         # full provenance: basis-once + per-metric {value,status,reason} (CLI)
    layouts/<stem>.png    # per-design geometry-only layout PNGs                (CLI)
    catalog_bars.png      # bar-per-KPI (spec bands shaded when a target is set) (CLI)
    catalog_montage.png   # one labeled layout tile per design                  (CLI)
    plot_warnings.txt     # the plotter's warnings sidecar                      (CLI)
    ranking_scores.json   # the DETERMINISTIC ranked order + per-axis scores    (CLI)
    ranking.md            # the model-agnostic narrative around the order       (you)
```

## Key rules

- **You never hold the seat during a bench cycle** — the CLI is the sole engine; you use
  only the seat-free reference tools + `deep-research` + Bash beforehand.
- **One-engine SOFT DECLINE** — even where engines can coexist, OptiVibe keeps to
  ONE engine on purpose, so if you already touched a design this session the CLI soft-declines at
  preflight (`engine_busy`, rc 3) rather than opening a second; a genuinely-unloadable staging
  (bad/relative paths, foreign seat, corrupt files) is caught after the bench
  (`all_designs_failed_load`, rc 3). Recover via a cold session OR the profile-via-held-seat
  fallback; NEVER trust a failed run's rows as design verdicts.
- **Run the CLI detached** (`run_in_background: true`) — no 600 s wall-cap → no orphaned-seat
  hazard.
- **Menu is an allowlist** — an unknown metric → CLARIFY, never a silent nearest-guess.
- **Null ≠ 0** — a missing metric is "no data", never scored 0 or perfect; a real 0 is real.
- **The user is the judge** — you recommend, cite the numbers + the layout, never auto-decide.
- **Nothing dropped, nothing faked** — a patent that couldn't build → the ledger + the
  report; a design that couldn't measure → a `partial`/`failed` row + an empty (never-0)
  cell; a missing layout → a labeled placeholder tile.
