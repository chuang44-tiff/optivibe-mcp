---
name: design-vision-review
description: Use when the user asks what you think of the layout, and at the optimization loop's midpoint pause after a saved checkpoint, where looking at the LAYOUT FIGURE is REQUIRED feedback rather than a debug step reached once something already looks wrong — dispatches a vision reviewer over the saved candidate's paired PNG, named by its seq, and returns findings a tool then checks. The reviewer is an ADVISOR to the human and the agent — its findings are SUGGESTIONS, and nothing it raises edits the design on its own. Triggers on "what do you think of the layout", "render the layout before the next optimization round", "look at the layout figure you just built", "look at the layout", "does the figure look right", "eyeball the drawing", "what does the render show". NOT a substitute for check_clearance.
---

# Design Vision Review — a second pair of eyes on the layout figure

## Overview

A vision reviewer looks at a SAVED layout PNG and says what looks wrong. It is a hypothesis generator — the tool is the check. Blind-adjudicated violation
recall was 4 of 25 = 0.16 and it fell as the figure got busier — but that was measured on the v0 prompt: the shipped reviewer-prompt.md is a NEW
INSTRUMENT, and no rate attaches to it until the corpus bench re-measures. An empty findings list means the eye found nothing, never that the design is clean.
The reviewer is an ADVISOR ROLE, not an autopilot: what it finds is advice to the human and the agent, and a lever moves only when a human chooses it.

## When to Use

- ON DEMAND — the user asks "what do you think of the layout", or tells you to look at it. This is a
  PRIMARY entry, not a debug aside: save the candidate first, then invoke this skill with its seq.
- After a `save_candidate`, over that candidate's paired PNG named by its seq, when the drawing
  is worth a look before the next optimization round: "look at the layout", "does the figure look right", "eyeball the drawing". This is the loop's MIDPOINT PAUSE — the caller presents what comes back and waits for the human's steer.
- After a large edit, when the FORM may have moved and no operand says so.
- NOT for measurement. The three hard negatives, verbatim: no edge gap off the
  figure, no config_headline, nothing inferred from a folded system's gaps. They
  reinforce the structural gates; they never replace them.
- NOT a substitute for check_clearance, and NOT a scorer. A number written into a
  note is quarantined as NOT evidence.

## Workflow

### Step 1 — Bind from the named candidate

THE FIGURE UNDER REVIEW IS THE CANDIDATE'S OWN PAIRED PNG, never an ad-hoc render. Bind from the envelope of the candidate whose seq is named in your invocation's ARGUMENTS line
(`candidate seq <N>`), and state that seq before dispatching. If your invocation names no seq, refuse the dispatch and give the caller the remedy rather than saving one yourself: save_candidate
first and name its seq. That envelope's png_path, seq and png_sha256 are what Step 4 binds a finding to, and a finding raised over any other picture cannot be recorded against these bytes.
That envelope names the .zmx digest artifact_sha256, NOT zmx_sha256 — one value, two names across surfaces; read the key that is there. A null png_sha256, or png_ok false, means the
candidate has NO bound picture: refuse the dispatch and say so.

Do not call `render_layout` for the bindings; a second render is a different picture. Every binding comes from that ONE envelope: png_path and png_sha256 from those keys; surface_labels,
stop_label and figure_disclosures from the keys of those names; extra_flags from flags or "none"; config from config_evaluated; n_surfaces from n_surfaces; image_surface = n_surfaces - 1.
No binding may be empty, and an ABSENT envelope key counts as empty: refuse the dispatch, never fill from memory. The disclosures travel with the image: a reviewer not told what the figure
hides inherits the defect.

### Step 2 — Dispatch the reviewer

ONE general-purpose subagent per review, dispatched in the FOREGROUND — `run_in_background` unset or false — and the loop does not advance until the reply is in hand: a BACKGROUNDED
reviewer cannot gate the round it was dispatched for, and its permission prompts are refused unasked. Per-reviewer scratchpad directory review_<call_index>/. The DISPATCH instructs
the reviewer to WRITE its raw reply verbatim to review_<call_index>/reply_raw.json — INSIDE its own scratchpad directory, NOT beside the PNG — BEFORE returning; on receipt, verify the returned message is BYTE-IDENTICAL to that file. A reply has
already reached the driver's context by the moment the driver could write it, so the SAVE is the reviewer's act and byte-identity is the record's proof. Read reviewer-prompt.md beside
this file. If that file is missing, STOP and say so; never improvise a reviewer prompt. Fill direction_classes from the nine classes in the VISION_FINDING_CLASSES block below,
design_intent from your own one-line statement of what the lens is supposed to be, reply_path with that same review_<call_index>/reply_raw.json resolved against this run's working directory, and the other seven from Step 1 — TOKEN SUBSTITUTION over the ten declared names, never str.format. THE RENDERED PROMPT IS THE ONLY DOCUMENT THE REVIEWER READS: a constraint stated here and not in it did not reach the reviewer, which is why the write-scope rule lives in the sibling and not in this step.

### Step 3 — The loop

```
THRESHOLD = 0.20     # unreadable-stamp fraction; provenance below
MAX_ROUNDS = 2       # review rounds per design (the owner's number)
env = the envelope Step 1 bound       # ONE figure per state; the loop consumes it
if env.png_ok is not True or env.png_sha256 is null: STOP_VOID
n_surfaces = env.n_surfaces           # the SAME envelope, never a second call
round = 1
while round <= MAX_ROUNDS:
    if env.surface_labels is empty, any label does not read as an integer (one
       optional trailing "?" allowed), or n_surfaces is not a whole number >= 2:
        STOP_ESCALATE                 # no stamped universe can be derived
    universe = those labels READ AS INTEGERS plus the image plane, n_surfaces - 1
    review = dispatch the reviewer over env.png_path   # Step 2; it wrote reply_raw.json
    if review is not the JSON object of the output format, or a finding's direction is not in the block:
        STOP_ESCALATE                 # broken REPLY -- name that reason; not a broken figure
    if review.stamps_read and review.unreadable_stamps are both empty:
        STOP_ESCALATE                 # the eye never addressed the figure
    if any member of review.stamps_read or review.unreadable_stamps, READ AS AN
       INTEGER, falls outside universe:
        STOP_ESCALATE                 # the figure cannot carry that stamp
    if len(review.unreadable_stamps) / len(universe) > THRESHOLD:
        STOP_ESCALATE                 # broken ADDRESSING: fix the FIGURE,
                                      # never iterate the design on it
    if review.findings != []:
        route EVERY finding by the Step-4 table, then:
        STOP_FINDINGS                 # the routed handoff -- a first-round finding
                                      # is never overwritten by a later round
    if round == MAX_ROUNDS:
        STOP_CLEAN  # the clean stop discriminates the stop REASON for the bench scorer's status enum; it saves no round at these values.
    round = round + 1
STOP_BUDGET                           # backstop only -- unreachable when the steps
                                      # above are followed exactly; the round cap
                                      # must survive a driver that miscounts
```

    # 0.20 sits inside the measured gap [0.172, 0.250]: 5/29 unreadable (0.172) still
    # addressed correctly (stamp recall 0.828, precision 1.000; stamp-readback probe,
    #); 3/12 (0.250) and 8/28 (0.286) each returned ZERO violation recall
    # (dogfood). Interpolation, n=3, not a rate; the next corpus bench
    # pass re-derives it.

The unreadable fraction is LOOP CONTROL. It never enters a finding, and there is no
schema field it could go in.

### Step 4 — Record the findings, then route them, then return

**RECORD FIRST, THEN ROUTE.** Before any row of the table below, call `record_findings` with design_name, the seq and png_sha256 bound in Step 1, and the reply's findings ARRAY
element-for-element unmodified — NOT the whole reply object: stamps_read and unreadable_stamps are loop control (see above) and are recorded nowhere. An empty array is legal and records
nothing; silence is not a row. A refusal is a **STOP, never a skip** — routing a finding that was never recorded produces a response nothing can later be found to answer. Recording is
IDEMPOTENT, so call it whether or not the reviewer already did. The ids come back in finding_ids, COMPUTED from each finding's own content plus the picture digest — never caller-supplied,
never invented — and they are the ids every later judgment block must name.

| the finding | where it goes |
|---|---|
| figure-scoped `looks_tight`, `looks_generous` or `too_thin` | call `check_clearance` FIRST. A CONFIRMED FLOOR violation is a finding a tool AGREED with; it still reaches the human as a SUGGESTION and the edit waits for the steer. A floor that PASSES answers nothing: a `too_thin` whose floor passes goes to the thin-element row below, never booked contradicted or declined. A `looks_generous` is adjudicated against the budget declared for this design — it follows automatically from build_merit; pass explicit values to override; a design or configuration change retires it. With none in force, book it NO ORACLE (last row); ABOVE it the audit REPORTS rather than confirms |
| figure-scoped `too_thick`, `asymmetric`, `steep_bend` or `wrong_sign_suspected` | a `too_thick` needs a declared maximum-glass budget: with none in force it is NO ORACLE whether or not a violation is reported, because the floor measures a MINIMUM and says nothing about thickness. Above one it REPORTS, exactly as `looks_generous` does. The other three: a SUGGESTION to the human and the agent, note quarantined; no edit on this channel alone |
| `too_thin`, or a thin edge, that a named tool does not CONFIRM | a SUGGESTION to the human and the agent — a fixed floor cannot measure thickness against DIAMETER, so a floor that passes never closes it. Carry three things: (a) WHAT WAS SEEN AND WHERE, by the stamps the reviewer read; (b) THE LEVERS, as merit operands a steer may pick — a minimum centre thickness (CTGT on that surface), a thickness-to-diameter ratio (CTVA and DMVA combined through `add_math_constraint`), a curvature limit (CVLT or CVGT on the steep surface), or an edge floor (ETGT, which evaluates each surface's OWN semi-diameter and can differ from `check_clearance`'s edge by up to ~46% — a force, not a verdict); (c) THE EVIDENCE — the `check_clearance` numbers, and a `tolerance` run in sensitivity mode over that element's thickness, radius and irregularity, taken after `load_design` reloads the saved file, because tolerancing an in-memory build returns an empty report. The threshold is the task spec's: cite the limit it states, and where it states none, ASK the human. No fixed shop number belongs here |
| REPORTED — the audit measured it and handed you both numbers | **make the ballpark call a designer makes in two seconds.** Ask whether the SEPARATION is worth acting on, never whether one number is bigger: a gap 0.004% over a declared 16 mm is not generous, it is CONVERGED — sitting exactly where its own boundary operand parked it. Only a FLOOR violation reaches CONFIRMED now; above a ceiling there is no comparable fact, only a matter of degree, which is what an exact predicate cannot judge. A REPORTED row authors nothing and gates nothing; act on it by making the judgment and **RECORDING** it -- `save_candidate(..., render=False, judgment={"finding_ids": [...], "disposition": ..., "reason": ...})`, which writes it to disk bound to that candidate's digest. Say it in your reply too, but the reply is not the record: prose scrolls past, and a dismissal nobody can find later is indistinguishable from a finding you dropped |
| system-scoped | a human, or a NAMED first-order test; never a scored bucket |
| keyed on a stamp the reviewer could not read | DROP it, and record the drop |
| the floor says fine and the reviewer still means something | with NO budget declared, book it as having NO ORACLE, never as contradicted. With one declared, the ceiling decides and a gap inside its limit IS contradicted. And if the thickness could not be read at all, the tool measured NOTHING: no edit, no closure, reported as unmeasured rather than as either verdict. A `too_thin` is never booked here: a floor that passes is not an answer to it |
| CONFIRMED by a NAMED tool — the advice arm | it goes to the human and the agent as a SUGGESTION carrying the tool's numbers and the levers above. NO finding — CONFIRMED, UNCONFIRMED, NO ORACLE or REPORTED — authors a constraint or re-enters the loop on its own; a lever is applied only when the human chooses it, and a constraint authored on that steer is ONE soft, low-weight constraint (`add_math_constraint`); every stated target outranks it |
| CONFIRMED by a NAMED tool — the completion arm | it is already RECORDED (above), and a RECORDED finding blocks **PROMOTION** until a recorded response names it: promote_best refuses (refuse_finding_unanswered) and names the open ids. Answer it — `save_candidate(..., render=False, judgment={"finding_ids": [...], "disposition": ..., "reason": ...})` — whatever the answer says. At the pause the human's steer IS that answer: record it. The gate reads ONLY that a response EXISTS for those ids, bound to those bytes, carrying a non-empty reason; it never reads whether the finding was TRUE or whether you AGREED. NO ORACLE and REPORTED are recorded and block promotion on the same terms. NO ORACLE is never auto-closed — it goes to a human, dispositioned referred; neither is REPORTED |
| the review came back SILENT (no findings at all) | NOT a pass. Record VISION_SILENT, say the eye found nothing, and proceed |

Silence never certifies. A RECORDED finding blocks PROMOTION — not this loop's report — until a recorded response names it, whatever that response says. Every routed finding that was not dropped goes to the human as a SUGGESTION — what was seen
and where, the levers, the evidence — and the caller WAITS for the steer before re-optimizing or promoting. Return the stop token, the
per-round saved reply paths, the finding_ids `record_findings` returned, and every finding with its route; the skill performs no edit.

## Key Rules

- **An empty findings list is not a clean bill of health.** At this recall it is weak evidence of nothing. Say so when you report it.
- **Never review inline.** The reviewer is a subagent that reads the PNG; a driver describing the figure to itself measures the driver, not the drawing.
- **The REVIEWER writes the raw record; the driver proves it.** Byte-identity between the returned message and reply_raw.json is the proof; a summary is not a record.
- **Escalate broken addressing; never iterate on it.** Above the threshold the FIGURE is what gets fixed.
- **Advice, not autopilot.** A finding no tool confirms still reaches the human with its levers and its
  evidence; a floor that passed is not a reason to drop it silently.
- **A number in a note is not evidence.** Every real number comes from a tool.

<!-- VISION_FINDING_CLASSES
["looks_tight", "looks_generous", "too_thin", "too_thick", "asymmetric",
 "steep_bend", "wrong_sign_suspected", "not_the_expected_form",
 "wrong_kind_element"]
-->
