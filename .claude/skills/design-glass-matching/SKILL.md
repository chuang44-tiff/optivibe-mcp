---
name: design-glass-matching
description: Use as the finishing step on a lens whose form is built and whose performance the optimizer has closed, when secondary colour (axial or lateral) or field curvature (Petzval) is what remains.
---

# Glass matching: the finishing step

Build the form and correct the third-order aberrations with optimize-loop first. Glass comes last, on a lens at rest
by optimize-loop's test (two calls of 50+ cycles each gain under ~0.5 % of the merit; a short call that reads `stable`
is not rest): a swap measured on a lens still moving tells you nothing. Glass is judged on the merit the form was
finished on: add, remove or re-weight no row here, since a changed merit starts a new form step. The goal is
performance, polychromatic RWCE at every field against a control given the same optimization; colour terms only point.

1. **Find the reason, then the lever.** At every stated field read the polychromatic RWCE (`get_operand`, ring 6,
   wave 0) and each single wave at the image plane (`analyze_wavefront` at that field, its `at_image_plane` rows).
   Name what the pattern shows: poly well above every single wave is primary colour, the lever is Abbe number; one
   band end far worse than the other is secondary colour, the lever is partial dispersion; every wave about as poor
   as poly is monochromatic, the lever is index at similar dispersion (Petzval when it is field sag with no colour
   spread). Several can hold at once (the 10x seed shows both colour patterns): say which fields show which. This
   names a property, not an element; the element comes from the screen. `explain_glass_swaps` shares locate carriers
   and never rank them (on 10x the largest share, S15, was never the lever).
2. **Screen every slot once, then go deep.** On the rested lens, one set per slot (each member of every cemented
   group, every single), four sets a call at the default budget, with `drop=false` so no arm is cut behind its control.
   Each call returns `coverage` (slots at rest k of N, open, untried) and `open_arms` (every arm not measured, with the
   action that closes it): report that count, and every open or untried slot as still open. `coverage` resets on any
   non-glass change to the lens and does not see vignetting; it informs, it does not judge. The one-swap screen is what found the 10x lever: S1 −26.9 %, S2 −13.4 % with
   Hammer's glasses, the other measured singles −2.3 % or worse. Then go deep where a slot moved.
3. **Pick candidates by the lever.** `find_glasses` with `sort=pg_f_anomaly` ranks on the anomaly
   pg_f − (0.6438 − 0.001682·vd) (column `pg_f_anomaly`; window `pg_f_anomaly_min`/`_max`); a pg_f sort orders by vd.
   Rank only on a list with no `truncated_note`: narrow the vd and anomaly window rather than raise `limit` (15 rows of
   571 hid S-NBH59, the best S1 single; vd 60–100 matches 752); if you rank from a cut list anyway, say so and name
   the cut. One glass can appear once per catalog: count it once
   by name. For secondary colour push the anomaly the corrective way, down for the flint
   (the lower-vd member) and up for the crown, whatever sign each has today: near the incumbent's vd on the screen,
   wider toward the anomalous families when deep (N-PK51, +14 in vd, was the best 10x S2 single); let nd follow (both
   S2 winners sat lower in nd), and at equal anomaly prefer the higher index (26.9 % against 14.7 % on S1). For primary
   colour step vd at held anomaly. For a monochromatic reason move nd at held vd and anomaly in the direction the
   residual asks (a positive element flattens Petzval with a higher index), and let the trial decide. The candidate
   rows also carry status, relative cost, climate, stain, expansion and (with `transmission_wavelength`) transmission:
   print them beside each kept glass, unknown as unknown, and filter on them only when the brief or user asks.
4. **Trial and judge at rest.** `trial_glass_swaps`, up to four sets a call, (sets + 1) × `time_budget_s` ≤ 1800 s. An
   arm in `open_arms` (dropped, `time_budget`, a first-order value it could not read, or a control that hit
   `time_budget`, when no arm is measured) is unfinished: report it so, never as rejected, and take its `action` if it
   matters. A row's `first_order_warning` names a focal length, f-number or NA more than 1 % off the control: the arm is
   measured and judged as usual, and its report carries the warning beside the decision. An arm that stopped by itself had one or more
   50-cycle calls and can sit 1–6 % above rest, so judge only re-rested arms: `load_design` the arm's `candidate_path` and the
   control's and run each to rest by the test above, the arms that lead or sit within ~6 % of their control first; an
   arm never re-rested is unconfirmed, not rejected. A decision is one of four words: kept; rejected (re-rested and
   behind its control, or a target lost); screened (measured once, never re-rested); not measured (in `open_arms`).
   Reload the screening lens before the next trial call. Keep a swap
   that improves the failing fields (when all pass: the worst field, and the merit) while every passing field still
   passes and no other target is lost: track, thickness, and working distance, which the trial does not read.
   `trial_glass_swaps` (which edits the lens while it runs, then restores it) and `substitute_glass` may be held behind a
   permission rule that asks the user first. If a call is denied, do not work around it: report the sets you would trial,
   or the swap you would keep, and stop.
5. **After every keep**, a keep from a re-try included, the next trial call re-tries the kept slot's cemented partner
   on the new lens, then the kept slot, then pairs from the screen's leading slots. This is a loop, not one step: each
   keep starts it again, so a keep that came from a partner re-try needs its own partner re-try (in one test both 10x
   runs re-tried once, kept that re-try, and stopped with its partner and 10-12 slots untried). A deep round of one or
   two sets is not a search: keep going. The search is done only when the last keep's partner re-try kept nothing and
   the last trial's `coverage` reads `sweep_done` true on the current lens; until then keep going
   while time allows, judged only on measured time: `search_wall_s` plus the optimizer's `wall_time_s` spent before
   the first trial, never on a sense of the clock (in an earlier test both 20x runs stopped at 32 and 37 of 60 minutes, believing
   the hour was gone). A form-limit or exhausted verdict quotes the last `coverage` summary line, which must read sweep
   done and never tried none; otherwise the verdict is "stopped with N open: S…"
   (both 20x runs so far claimed a form limit with the open slots printed in front of them). If, with the screen measured, the best rested swap moved the worst field only a
   small part of what the target needs, hand the form back to optimize-loop for one attempt within what the brief
   allows, run to rest, re-screen the slots that moved, and judge again: a target met, a search exhausted within the
   brief, and a form limit are three different reports; say which, and why. Run the final lens to rest.
6. **Clock and report.** No tool tells the full time: report `search_wall_s` as the measured glass-search time and
   the optimizer's `wall_time_s` before it (each `optimize` envelope) as the measured form-rest subtotal, total elapsed
   time as unavailable, never a guess. Report per slot the reason, lever, candidates, and each trial beside its control at every field where
   measured, else "not measured"; every unfinished arm by name; the decision; and for a keep the on-axis wavefront
   and field sag before and after.
