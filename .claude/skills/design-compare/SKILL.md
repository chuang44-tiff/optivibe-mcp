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

Decide the battery **before** you run the first design, and run that same battery
on every design. Adding an analysis after you have moved on costs a reload and
invites an asymmetric comparison.

**`get_mtf` returns one more data series than you have fields.** The FIRST series
(index 0) is the **diffraction limit**, not a field — it is the same curve for both
designs and comparing it tells you nothing. Count the series against the field
count before you map them: with 3 fields you get 4 series, with 4 fields you get 5.
On-axis is series **1**, and the last series is the corner. Two designs with
different field counts have different series counts — map each one separately.

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

**The report ends with the recommendation.** It begins with the literal token
`Recommendation:` and names one design. That token is read by tooling downstream —
do not substitute prose for it.

The `Edge MTF@50` row is the one that decides most comparisons. Keep it, and keep
it honest: it is the *worst* field, not the mid-field.

### Step 4: Restore original state

After the comparison, reload whichever design the user was working on before it
started. If there was none, say so rather than leaving the end state unstated —
the last design you loaded is still in the engine.

## Key Rules

- **Save before comparing.** Loading other designs overwrites the current state.
- **Same analysis on every design.** Don't skip MTF on one and run it on another.
- **Never compare the diffraction limit and call it on-axis.** It is `get_mtf`
  series 0, it is nearly identical for two designs at the same f/#, and reporting it
  as an on-axis row turns a large real difference into a false tie. On-axis is
  series 1.
- **Never drop a field from the comparison because it is bad.** Every field you
  analyzed appears in the report, worst included. A claim that a design is corrected
  "across the full field" is false unless the corner row supports it — dropping the
  corner and then concluding full-field correction is the failure mode this rule
  exists to prevent, and every number in such a report can be individually true.
- **Let the user decide.** Present the data, give a recommendation, but don't
  auto-pick — the user is the optical engineer.
- If comparing more than 4 designs, suggest narrowing down first — too many
  columns gets unreadable.
