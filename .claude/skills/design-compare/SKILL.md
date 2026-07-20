---
name: design-compare
description: Use when the user wants to compare two or more lens designs side by side. Triggers on "compare", "which is better", "side by side", "A vs B", "rank these designs".
---

# Design Compare — Side-by-Side Lens Comparison

## Overview

Loads multiple designs, runs an identical analysis on each through the ZOS-API,
and outputs a comparison table highlighting which design wins on each metric.
The user makes the final call — this skill organizes the data and recommends.

## When to Use

- User has 2+ designs and wants to pick the best one
- After running optimization from different starting points
- Comparing a design before/after optimization
- NOT for: single-design assessment (use `/design-review`)

## Workflow

### Step 1: Identify designs to compare

Get the list from the user. Formats:

- File paths to `.zmx` files (e.g. `data/lenses/design_a.zmx`,
  `design_b.zmx`).
- A sample / built-in design from the OpticStudio sample library.
- Current design vs a saved file ("compare current state with the saved version").

### Step 2: Analyze each design

For each design in sequence:

1. Load it (or skip if "current").
2. Read system info — EFL, F-number, total track, element count.
3. Pull RMS spot for all fields.
4. Pull MTF at a representative frequency for all fields.
5. Record the results, move to the next design.

**Important:** save the current design FIRST if it has unsaved changes, so it can
be reloaded after the comparison.

### Step 3: Output comparison table

```
## Design Comparison

| Metric         | Design A | Design B | ... | Winner |
|----------------|----------|----------|-----|--------|
| EFL            | ...      | ...      | ... | — |
| F-number       | ...      | ...      | ... | — |
| Total track    | ...      | ...      | ... | shorter |
| Elements       | ...      | ...      | ... | fewer |
| Max RMS spot   | ...      | ...      | ... | smaller |
| On-axis MTF@50 | ...      | ...      | ... | higher |
| Edge MTF@50    | ...      | ...      | ... | higher |
| Glasses        | ...      | ...      | ... | — |

### Summary
[2-3 sentences: Design A wins on X, Design B on Y. Recommendation: Design [X]
is the stronger candidate because ...]
```

### Step 4: Restore original state

After the comparison, reload whichever design the user was working on before it
started.

## Key Rules

- **Save before comparing.** Loading other designs overwrites the current state.
- **Same analysis on every design.** Don't skip MTF on one and run it on another.
- **Let the user decide.** Present the data, give a recommendation, but don't
  auto-pick — the user is the optical engineer.
- If comparing more than 4 designs, suggest narrowing down first — too many
  columns gets unreadable.
