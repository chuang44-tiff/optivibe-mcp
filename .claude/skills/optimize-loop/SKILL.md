---
name: optimize-loop
description: Use when the user wants to optimize a lens design. Triggers on "optimize", "run optimization", "improve this design", "make it better", "hit the spec". Drives the OptiVibe optimize tool with checkpoints and a preflight, pauses at a midpoint for a look at the layout figure, and records a response to every finding raised there.
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

The loop does not run start-to-finish unattended. Step 4 is a **midpoint pause**: you
save a candidate, optionally ask for a look at its layout figure, and put what comes
back to the user as suggestions before going further. A design can meet every number
you gave it and still be the wrong shape, and a merit function cannot see a defect
nobody wrote a target for.

## When to Use

- User says "optimize this", "improve the design", "hit the spec"
- After loading a starting point and deciding what to free and what to target
- NOT for: a broad global search from scratch (ask for a Hammer run explicitly)

**Do not run Hammer unless the user asked for it.** `algorithm: "Hammer"` is a
global search: it is slow, it is not what "optimize this" means, and it can land
somewhere structurally different from the form the user handed you. If DLS stalls,
*say* it stalled and offer a Hammer run — do not just start one. This is a hard
gate, not a preference.

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
4. **Note what you are about to change about the design class.** Freeing conics,
   substituting glasses, or changing element count alters what the part *is*, not
   just how well it performs. Write down the starting class now; Step 4 must report
   any change to it.

### Step 3: Preflight, then optimize

1. `dry_run` first — it confirms readiness (variables set, merit computable) WITHOUT
   opening the optimizer. If it reports a glass-vertex aperture stop, run
   `normalize_stop` (or pass `auto_normalize`) — `optimize` / `dry_run` default-refuse
   a glass-vertex stop.
2. Call `optimize`. It runs monitored optimization internally and returns a VERDICT —
   read it:
   - `improved` — the merit came down **against the merit function you gave it**. This
     is not the same as "the lens got better." See the reality check below.
   - `diverged` — the merit did not improve / an UNSTABLE configuration. Reload your
     Step-1 snapshot if the result is worse, and make a high-level design change
     (different form / starting point) rather than running more cycles.
   - `optimize_merit_uncomputable` — a corner ray can't trace at full pupil (a
     wide-field / fast merit). Apply corner vignetting with `set_vignetting`
     (`from_rays`), then REBUILD the merit with `build_merit` and re-seed from a
     gentler form before re-running.

3. **Reality-check every `improved` verdict before you build on it.** A merit function
   is a proxy, and a mis-authored operand can make a destroyed lens score well. After
   an `improved` run, confirm the design is still physical:
   - `check_clearance` — no negative air gaps, no interpenetrating surfaces.
   - `get_first_order` — back focal distance still positive and sane.
   - `get_spot` — the real spot moved the same direction the merit did.

   If the merit fell and the geometry got worse, **the merit is wrong, not the lens.**
   `dump_merit_function` and look for a row driving a measurement to an unintended
   target. Reload your Step-1 snapshot or a per-pass `.zmx`, fix the merit, and re-run.

A clean call is not proof of success — and neither is `improved` on its own. Trust
the verdict, the read-back merit, **and** the geometry read-back together.

### Step 4: Assess, checkpoint, then pause

1. Read final EFL / F-number (`get_first_order`) and RMS spot (`get_spot`); compare
   to the pre-optimization state.
2. Confirm the geometry is physical (`check_clearance`) — this is the Step-3 reality
   check, repeated once on the design you are about to keep. Run it BEFORE asking for
   a look at the figure, not after: a remark about an element looking too thick or too
   generous is judged against a measured thickness budget, and with no audit in hand
   there is nothing to judge it against.

   **It reports; it does not certify.** A `check_clearance` that flags nothing is not a
   statement that the design is manufacturable. It substitutes for a degraded read
   rather than refusing, so a surface it could not measure can still produce a
   confident number — and on a folded system the per-gap thickness audit is suppressed
   entirely, so an empty violation list there means *nothing was measured*, not
   *nothing is wrong*. Read what it measured, never the absence of a complaint.
3. `save_candidate` the result. This is the checkpoint the pause is built on, and it is
   what makes a remark about the figure recordable: the candidate's `seq` and its
   `png_sha256` both come from this envelope.
4. **The midpoint pause — ask, then stop.** With the candidate saved, you may invoke the
   `design-vision-review` skill with that candidate's seq to get a second pair of eyes on
   its paired layout figure. Do it here, and whenever the user asks what you think of the
   layout.

   **The reviewer is an ADVISOR, never an autopilot.** What it returns are SUGGESTIONS
   for the user and for you. Nothing it raises edits the design, authors a constraint, or
   re-enters the loop on its own. Present each remark with what was seen, where, the
   levers that would address it, and the evidence — then **WAIT for the user's steer**
   before optimizing again or promoting.

   A review that comes back silent is not a pass. Say the eye found nothing; silence
   never certifies.

Report:

```
## Optimization Result

| Metric   | Before | After | Delta |
|----------|--------|-------|-------|
| Merit    | ...    | ...   | -X%   |
| EFL      | ...    | ...   | ...   |
| Max spot | ...    | ...   | ...   |

Design class: unchanged  (or: name exactly what changed — see below)
Setup changes: none  (or: list what YOU changed — wavelengths, fields, aperture, scale)
Figure review: <n> recorded (<k> unanswered)  (or: silent — the eye found nothing; or: not asked)

Verdict: improved / diverged  (cycles/passes as reported by optimize)
```

**The last line of the report is the verdict line.** It begins with the literal token
`Verdict:` and is read by tooling downstream. Do not replace it with prose — not
"Bottom line", not "Summary", not "Notes". If you have more to say, say it *above*
the line.

**The `Design class:` line is mandatory and must be true.** If the run freed conics,
substituted a glass, or changed element count, say so there in plain terms — e.g.
*"changed: conics freed on S1–S4, S6–S7; this is no longer an all-spherical design."*
A user who handed you an all-spherical triplet and gets back a six-asphere design has
a different manufacturing proposition, and it is not disclosed by the metric table.
Never describe the result using the class of the input if you changed the class.

**The `Setup changes:` line is mandatory and must be honest about authorship.** If
you changed wavelengths, the field set, the aperture, or scaled the lens, list it
there as **your** change. Two rules about attribution:

- **Do not blame the input file for a state you have not read back from it.** "The
  seed had the wrong wavelengths" is a claim about a file; if you did not read the
  file's wavelengths, you cannot make it. Say "I set the wavelengths to F/d/C"
  instead — which is true, checkable, and just as useful.
- A spec the user stated (50 mm, f/4, ±20°) is a *target*, not a licence to silently
  restate the system. Changing the field set changes what every field-dependent
  number in your table means.

### Step 5: Answer every recorded finding, then suggest next steps

If the midpoint pause recorded anything about the figure, each remark owes a RECORDED
RESPONSE before the design can be promoted. **`promote_best` refuses** while any
recorded finding under this design has no response anchored to a saved candidate, and
it names the open ids when it does.

The rule is narrower than it sounds, and the narrowness is the point: **the gate never
reads whether the remark was TRUE, nor whether you AGREED with it.** It reads only that
a response exists for those ids, bound to those bytes, carrying a non-empty reason.

**Answering is not agreeing.** A rejection is a valid response — an obligation to
respond is not an obligation to comply. Declining should be argued, and the argument is
the reason you record. At the pause the user's steer IS the response: record it,
whichever way it went.

Record it rather than merely saying it. A remark you silently dropped and one you
deliberately dismissed look identical afterwards, and prose in the reply scrolls past.
Pass the call to the tool that writes it to disk beside the exact bytes it is about:

```
save_candidate(design_name=..., render=False, judgment={
    "finding_ids": ["<the id the review returned for this remark>"],
    "disposition": "acted" | "declined" | "superseded" | "referred",
    "reason": "<why it is or is not worth acting on>"})
```

`finding_ids` are the ids the review RETURNED — never invented. They are computed from
each remark's own content plus the picture's digest, so an id you composed yourself
names nothing and discharges nothing.

`disposition` is required whenever `finding_ids` is non-empty, and the vocabulary is
four: `acted` (a change was made), `declined` (considered and rejected — the reason is
the argument), `superseded` (it is about bytes the design has moved past), `referred`
(routed to a person or a named test; open on someone else's desk). `reason` has no
default and an empty one is refused. `promote_best` takes the same block.

State it in your reply as well — the user is reading that. But the reply is not the
record, and if you only write prose the judgment did not happen.

Then:

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
  **once per merit configuration**; do not hand-run cycles against an unchanged setup.
  Re-authoring the merit (or freeing different variables) and running again is normal
  iterative design and is expected; re-running the *same* setup to squeeze out cycles
  is not.
- **An `improved` verdict is not proof the lens is sane.** It means the merit fell.
  Confirm the geometry with `check_clearance` / `get_first_order` before you build on
  the result — a mis-authored operand can score a destroyed design well.
- **Trust the verdict, not the absence of an error.** A `diverged` run can leave a
  worse design — reload your snapshot; an uncomputable merit needs vignetting + a
  merit rebuild.
- **Disclose any change of design class** — conics freed, glass substituted, element
  count changed — in the `Design class:` line of the report. Always, even when the
  user did not ask.
- **The figure review is an ADVISOR, not an autopilot.** At the midpoint pause you
  present its suggestions and WAIT for the user's steer; a lever is applied only when
  the user picks it. Nothing it raises edits the design or re-enters the loop by
  itself. When the user asks what you think of the layout, save a candidate and ask for
  a review of that seq — at any point in the session, not only at the pause.
- **You cannot promote over an unanswered recorded finding.** `promote_best` refuses and
  names the open ids. Meeting every stated target is not the same as being finished:
  answer each recorded remark with a `judgment` block. The gate checks that a response
  exists, never that it agreed — so a rejection closes it, provided the reason argues
  the decline.
- Ground operands and glasses with `lookup_operand` / `search_reference` /
  `lookup_glass` — do not guess a code.
