---
name: optimize-loop
description: Use when the user wants to optimize a lens design. Triggers on "optimize", "run optimization", "improve this design", "make it better", "hit the spec". Drives the OptiVibe optimize tool with checkpoints and a preflight.
---

# Optimize Loop — Driving OptiVibe Optimization

## Overview

Optimize a lens design through the OptiVibe `optimize` tool. You set up variables
and a merit function, preflight with `dry_run`, then call `optimize` — which runs
monitored optimization bursts for you and returns a verdict
(`improved` / `diverged` / …). You do not hand-run the burst loop; the tool owns it.
A DLS or OD run commits its result in place — there is no automatic restore of the
starting form, which is why Step 1 checkpoints it. A Hammer run is the exception: it
checkpoints first and attempts a best-restore if it comes out worse.

## When to Use

- User says "optimize this", "improve the design", "hit the spec"
- After loading a starting point and deciding what to free and what to target
- NOT for: a broad global search from scratch (ask for a Hammer run explicitly)

## Workflow

### Step 1: Checkpoint the starting point

Before optimizing, `save_snapshot` (or `save_candidate`) the START form. There is no
reliable in-session undo across an optimization run, and `optimize` also auto-captures
a per-pass `.zmx` trail.

### Step 2: Set up variables + a merit function

1. Free the variables with `set_variable` (radii, thicknesses, conics, glasses). A
   reused or loaded design can carry INHERITED variables — check with
   `list_variables`, and `clear_all_variables` to start clean.
2. Establish a merit function: `build_merit` for a wizard default (it authors positive
   manufacturability floors via `min_air` / `min_glass`), or the user's own operands.
   If the user supplied a verbatim merit function, drive on THAT.
3. To ground an operand or a glass choice, use the reference tools (`lookup_operand`,
   `search_reference`, `lookup_glass`) — do not guess a code.

### Step 3: Preflight, then optimize

1. `dry_run` first — it confirms readiness (variables set, merit computable) WITHOUT
   opening the optimizer. If it reports a glass-vertex aperture stop, run
   `normalize_stop` (or pass `auto_normalize`) — `optimize` / `dry_run` default-refuse
   a glass-vertex stop.
2. Call `optimize`. It runs monitored optimization internally and returns a VERDICT —
   read it:
   - `improved` — the merit came down; the result is committed.
   - `diverged` — the merit did not improve / an UNSTABLE configuration. Reload your
     Step-1 snapshot if the result is worse, and make a high-level design change
     (different form / starting point) rather than running more cycles.
   - `optimize_merit_uncomputable` — a corner ray can't trace at full pupil (a
     wide-field / fast merit). Apply corner vignetting with `set_vignetting`
     (`from_rays`), then REBUILD the merit with `build_merit` and re-seed from a
     gentler form before re-running.

A clean call is not proof of success — trust the returned verdict and the read-back
merit, never merely that the call did not error.

### Step 4: Assess + checkpoint

1. Read final EFL / F-number (`get_first_order`) and RMS spot (`get_spot`); compare
   to the pre-optimization state.
2. `save_candidate` an accepted result; once you have a keeper, `promote_best` it.

Report:

```
## Optimization Result

| Metric   | Before | After | Delta |
|----------|--------|-------|-------|
| Merit    | ...    | ...   | -X%   |
| EFL      | ...    | ...   | ...   |
| Max spot | ...    | ...   | ...   |

Verdict: improved / diverged  (cycles/passes as reported by optimize)
```

### Step 5: Suggest next steps

- Converged well: "Looks good. Run `/design-review` for a full assessment, or
  `save_candidate` / `promote_best` to keep it."
- Stalled: "Free more variables, substitute glasses, relax a constraint, or change
  the starting point."
- Diverged: "`diverged` means an UNSTABLE configuration — reload the saved snapshot if
  the result is worse, then change the design form; don't run more cycles."

## Key Rules

- **Checkpoint before optimizing** (`save_snapshot` / `save_candidate`). No reliable
  in-session undo.
- **Preflight with `dry_run`** before every `optimize`.
- **Let `optimize` own the loop.** It runs monitored optimization internally — call it
  once with your parameters; do not hand-run cycles.
- **Trust the verdict, not the absence of an error.** A `diverged` run can leave a
  worse design — reload your snapshot; an uncomputable merit needs vignetting + a
  merit rebuild.
- Ground operands and glasses with `lookup_operand` / `search_reference` /
  `lookup_glass` — do not guess a code.
