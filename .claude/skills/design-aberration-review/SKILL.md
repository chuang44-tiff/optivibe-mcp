---
name: design-aberration-review
description: Use before you name a design's limiting aberration, before you launch an operand-targeted optimize, and when you start optimizing for performance — also on "which aberration limits this", "ray fan", "field curvature", "astigmatism", "distortion", "what is the lens limited by". Dispatches TWO reviewers (ray fan, and field curvature / distortion); each draws its plot and reads the PNG and the FULL raw numbers, both saved, and merges their replies into one picture of how the aberrations are balanced. The reviewers are ADVISORS: the main agent must listen and answer each suggestion, but nothing here blocks a promotion or stops a loop.
---

# Design Aberration Review — read how the aberrations are balanced, not one operand

## Overview

An optimizer balances aberrations against each other, and an agent that fixes its eyes on one operand tends to tunnel:
it chases that one term into a hang, a local minimum or a bad form. This skill is the cure. It shows you the whole
balance — third order against higher order, astigmatism against field curvature, colour in both plots — before you
commit to a diagnosis or a targeted move.

Two things are read together. The IMAGE gives the SHAPE of an aberration, and balanced or higher-order aberration
shows plainly there and poorly in a table. The NUMBERS give the SIZE; the drawn scale can make a figure look better
or worse than it is, so no magnitude is ever taken from the picture. Each reviewer gets both.

It mirrors `design-vision-review`: the reviewers advise, the main agent listens and answers, it blocks nothing.

## When to Use

- You are about to say which aberration limits the design, or to launch an `optimize` aimed at one operand.
- You are starting to optimize for performance on a design that already meets its geometry.
- The user asks what the lens is limited by, or asks about the ray fan, field curvature, astigmatism or distortion.
- NOT a measurement. A number comes from a tool, never from a reviewer, and NOT a per-stage gate.

## Workflow

1. **Pin the state.** `save_candidate`, then review with no design-changing call until both replies are back. Quote
   the candidate's seq and `artifact_sha256`; those bytes are what the review is about (the render tools read the live
   design).
2. **Each reviewer draws and reads its own figure, for EVERY configuration.** That is the streamlined way: the plots and
   their numbers go straight to the reader, with no copy through you. Choose one `waves` list for both figures (at most
   five, edge to edge, the primary among them). A multi-config design — a zoom or any other — is reviewed whole: the
   limiting aberration often sits in a different configuration for each aberration, and `"all"` is refused, so each
   configuration is its own render with an explicit `config`.
3. **Save the images and the results.** Before you dispatch, create `review_<n>/` under the workspace root (a relative
   `path` resolves there, not in your working directory), plus the next `review_<n+1>/` for a re-render: the render
   tool writes into an existing folder only. Each render's `path` is `review_<n>/fan_c<k>.png` or `fcd_c<k>.png` (k = the configuration). The tool writes the data beside it as `<png stem>.data.json` and names it in `data_path`.
   If `data_path` is null, its `data_sidecar_failed:<reason>` flag says why: the reviewer renders again into a fresh
   `review_<n>` and, if that fails too, says so in its reply. Never retype the data by hand.
   When the replies are back, check `review_<n>/` (or the folder a reply says it re-rendered into) holds a PNG and a
   data file for every figure and configuration, and that each pair agrees on `config_evaluated`,
   `waves_selected.drawn` and `primary_wave`. Anything missing or mismatched: dispatch again.
4. **Dispatch two reviewers**, in the foreground and in parallel, from `reviewer-prompt.md` beside this file (stop if
   it is missing): one for the fan, one for field curvature / distortion. Fill its placeholders: `{FIGURE}` (ray fan,
   or field curvature / distortion), `{DESIGN_INTENT}` (one line on what the lens is meant to be), `{CONFIG_FILES}`
   (one line per configuration: `config <k>: render it with waves <list>, image <png path>, data <png stem>.data.json`
   — ALL of them for that figure) and `{REPLY_PATH}` (`review_<n>/fan_reply.md` or `fcd_reply.md`). Do not rewrite
   the prompt's instructions.
5. **Merge into ONE cross-plot picture**, not two verdicts side by side: which aberrations are present, how they are
   balanced against each other and across configurations, which one limits and at what configuration, field and wave.
   Each plot's scale is fitted to its own configuration, so two that look alike can differ several-fold: compare
   configurations by the numbers, never by eye. Every size claim quotes a value from a reply or a data file, and the
   merge names both reply files. If either reply is missing or unusable, there is no merged picture and no claim of a limiting
   aberration — dispatch again.
6. **Use it.** The merged picture picks the move, not one operand. Check the claim behind it with a numeric tool
   first. The move takes up a suggestion, or your answer says why another is better (a human's steer counts as that
   answer). On a multi-config design, check a fix at every configuration. Any design or config change retires the
   review; a new diagnosis needs a fresh one.
7. **Answer before you move on.** Record the answers to THIS review's suggestions (below) before the next
   design-changing call — after every review, not only the final one.
8. **Review what you promote.** The last review is on the state you promote. If the design changed after it, review
   again before `promote_best`, or say in your report why you did not.

## Answering a suggestion

Follow `design-vision-review`'s "Answering a finding — the decline standard, and NO HUMAN" section: every suggestion is
answered, either acted on or declined with a measurement that answers what it says. With a human present the
suggestions go to the human and the steer decides; with none, you are the steer. Record the answers with
`save_candidate(..., render=False, judgment={"reason": ...})`. That call creates a NEW candidate row from the live
design and does not annotate the reviewed seq, so the reason names the reviewed seq, its `artifact_sha256`, each
figure's `png_sha256` (read from its `.data.json`), each suggestion and how it was answered. Pass no `finding_ids` (`record_findings` refuses these PNGs).

## Key Rules

- **Image for shape, numbers for size.** A reviewer that cannot quote a value that exists only in the JSON did not read it.
- **Listen, do not obey.** A suggestion is a hypothesis; the tool is the check. A contradicted suggestion is dropped
  or revised with the measured value.
- **Blocks nothing.** No promotion gate, no loop stop.
- **Never review a mismatched or stale pair.** State and config must match, and a change retires the review.
- **Reading a render yourself is a look, not a review.** It is worth doing — a field curvature curve that swings between
  the fields your merit samples is an optimizer exploiting the sampling, which no number at those fields shows. But only
  the two reviewers and the merge count as the review this skill requires.
- **Do not name the engine vendor** in any prose you write or relay; say "the engine".
