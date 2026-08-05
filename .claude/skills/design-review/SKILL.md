---
name: design-review
description: Use when the user asks to review, assess, or check a lens design against a spec. Also use after optimization to evaluate results. Triggers on "how does this look", "check the design", "review", "evaluate".
---

# Design Review — Comprehensive Lens Assessment

## Overview

One-command lens assessment. Pulls the relevant analysis metrics through the
ZOS-API, compares them against the user's spec, and outputs a structured
report. Manufacturability ratios are surfaced as **prompted feedback to the
user** — the user brings the acceptance bands; this skill does not bake a verdict.

## When to Use

- After loading a design ("how does this look?")
- After optimization ("did it improve?")
- Before saving ("is this worth keeping?")
- When the user asks to "review", "check", "assess", or "evaluate"
- NOT for: comparing multiple designs (use `/design-compare`)

## Workflow

### Step 1: Gather spec targets

If the user provided a spec (EFL, F-number, max spot size, MTF target, field
angles, wavelength band), use those. If not, ask: "What are your targets? (or I
can just report current state)".

If the user gave a *partial* spec — first-order targets but no image-quality bands,
say — do not silently invent the missing ones. Report the un-targeted metrics
un-graded, and say in the report which columns have no target behind them.

### Step 2: Run the analysis sequence

Pull these through the ZOS-API typed analyses, in order:

1. **System info** — EFL, F-number, total track length, surface count,
   wavelengths, field set.
2. **Prescription** — surface-by-surface radii, thicknesses, glasses.
3. **Spot** — RMS and GEO spot size for ALL fields.
4. **MTF** — MTF curves for ALL fields (report at two representative
   frequencies, e.g. 50 and 100 lp/mm).
5. **Wavefront** — on-axis wavefront error (RMS waves) on a flat image plane.

Run steps 1-2 first (fast prescription reads), then 3-5 (heavier analyses).

`get_mtf` may return **one more data series than you have fields** — the extra one
is the diffraction limit, not a field. Check the series count against the field
count before you map them, and label the limit as the limit.

### Step 3: Output report

```
## Design Review

### System Summary
| Parameter    | Value | Target | Status |
|--------------|-------|--------|--------|
| EFL          | ...   | ...    | PASS/FAIL |
| F-number     | ...   | ...    | PASS/FAIL |
| Total track  | ...   | ...    | — |
| Elements     | ...   | —      | — |

### Image Quality
| Field      | RMS Spot (um) | Target | MTF@50 | MTF@100 | Status |
|------------|---------------|--------|--------|---------|--------|
| On-axis    | ... | ... | ... | ... | PASS/FAIL |
| 0.7 field  | ... | ... | ... | ... | PASS/FAIL |
| Full field | ... | ... | ... | ... | PASS/FAIL |

### Wavefront
- On-axis RMS: ... waves (flat image plane)
- Diffraction limited: YES/NO

### Manufacturability (read-only ratios — bring your own bands)
- Min center / edge thickness and aspect ratios as raw numbers (from `check_clearance`).
- No baked verdict — these are prompts for the user's judgement.

### Verdict: [PASS / NEEDS WORK / FAIL]
[One sentence: what's good, what needs attention.]
```

**The report ends with the verdict heading.** It begins with the literal token
`### Verdict:` followed by exactly one of `PASS`, `NEEDS WORK`, or `FAIL`. That token
is read by tooling downstream — do not substitute prose for it ("Bottom line",
"Summary", "Overall"). Anything else you want to say goes *above* the line.

Keep every section. If a section has nothing in it, say why rather than dropping it
— an absent `Manufacturability` block reads as "clean" when it means "not measured."

### Step 4: Suggest next steps (only if NEEDS WORK or FAIL)

- Spot too large: "Consider freeing more radii or adding an element."
- MTF low at edge: "Field curvature may be the limiter — check the wavefront."
- EFL off target: "Add an EFL operand to the merit function."

Keep suggestions brief. The user is an optical engineer — they need the data
organized, not a lecture.

## Key Rules

- **Evaluate the wavefront on a flat image plane** unless the user explicitly
  asks for a best-focus refocus. If you also report a best-focus figure, label it
  as a refocus and keep the flat-plane number as the headline — a best-focus RMS
  can be 2× rosier, and quoting it unqualified overstates the design.
- **Surface ratios are prompts, not verdicts.** The user owns the acceptance bands.
- **Report the worst field, always.** A summary sentence about field performance
  must be supported by the corner row, not by the fields that flatter the design.
- Ground any operand or glass you are unsure about with the reference tools
  (`search_reference` / `lookup_operand`) — do not guess a code.
