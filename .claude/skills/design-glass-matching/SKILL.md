---
name: design-glass-matching
description: Use as the finishing step on a lens whose form is built and whose performance the optimizer has closed, when secondary colour (axial or lateral) or field curvature (Petzval) is what remains.
---

# Glass matching: the finishing step

Build the form and correct the third-order aberrations with optimize-loop first. Glass matching comes last: it
uses the secondary colour or Petzval term that is left to find a wavefront gain. It does not rescue an unfinished
design, and an early swap pulls attention from the form. Start only when the lens sits at a steady local minimum:
a full `optimize` stops by itself with no real gain. A swap measured on a lens that is still moving tells you nothing.

1. **Read what is left.** Call `explain_glass_swaps` with the colour term and no pair: the refusal lists every
   wavelength's offset from the primary. Then call it with the pair that carries what is left, or with `petzval`, to
   see which elements carry it. Element values are signed first-order shares that often cancel: use them to locate carriers, never to rank glasses. Start with the largest carrier.
2. **Pick a few candidates for one carrier** with a different dispersion, not a similar glass; the index is free, since
   the trial re-optimizes. With `find_glasses`, pass no `nd_min` or `nd_max`: shorten the list with the partial
   dispersion and `limit` instead. If `matched` is more than the rows returned, narrow the window, raise `limit`, or
   sort by `delta_pg_f` (ascending for a flint, descending for a crown) so the unusual-dispersion glasses come first,
   before you pick. A null ΔPg,F means the stored value was implausible (beyond ±0.1) and sorts last; treat it as
   unknown, not as normal dispersion.
   Move the Abbe number a step from the carrier's own, not a leap; for secondary colour look at the partial
   dispersion (`find_glass_pair`, `lookup_glass` with name and catalog). If single swaps keep being re-balanced away,
   trial a pair or triple on different elements as one set: a pair whose members are each worse alone is never found
   one swap at a time (slower, so not the first try).
3. **Trial them** with `trial_glass_swaps`, at most four sets per call (`require_free_stop=false` if the stop sits on
   a glass vertex). A trial that stopped by itself or settled is a measurement; a dropped one trailed its control
   and is out. A time-budget stop: rerun it with more time if it leads its control, or if it is on the largest
   carrier. The goal is performance: judge a swap on the polychromatic wavefront error at every field, against its
   control. The colour term points to the carrier; it is not the goal. Keep a swap that improves the fields failing
   the target while every field that passes still passes, and that costs no other target: check track and
   thickness. The trial does not read working distance: read it yourself.
   `trial_glass_swaps` (which edits the lens while it runs, then restores it) and `substitute_glass` may be held behind a
   permission rule that asks the user first. If a call is denied, do not work around it: report the sets you would trial,
   or the swap you would keep, and stop.
4. **Go round again.** A round tries the carriers in order on one lens and ends when a swap is kept or every carrier
   from step 1 has been tried; after a keep, start the next round on the kept lens. Stop when a round keeps nothing,
   or when the best swap so far moved the wavefront only a small part of what the target needs: then the limit is
   the form, so say so. Then a final `optimize`, and check every target.
5. **Report** the carriers, the candidates, the wavefront of each trial beside its control at every field, and the
   decision. For a kept swap, also give the on-axis wavefront and the field sag before and after.
