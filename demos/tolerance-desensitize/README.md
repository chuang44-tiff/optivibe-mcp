# Demo: desensitizing a finished 10x objective so more built lenses pass

**On a 10x / NA 0.45 microscope apochromat with every glass locked, an agent following a written tolerancing
procedure raised the share of builds that are diffraction-limited at every field from 57.50 % to 63.65 %, 65.05 % and
63.90 % in three unattended runs (2000-trial Monte Carlo, refocus plus one 0.05 mm push). All three runs found the same
lever, an angle-of-incidence ceiling on one surface, and paid up to 0.0018 waves of nominal wavefront for it.**

| 10x / NA 0.45 apochromat | start lens | agent, 3 runs |
|---|---|---|
| builds diffraction-limited at every field (yield) | 57.50 % | 63.65 %, 65.05 %, 63.90 % |
| builds worse than twice the limit (tail) | 2.25 % | 1.35 %, 1.60 %, 1.05 % |
| P90 of the worst-field wavefront | 0.0951 waves | 0.0917, 0.0919, 0.0912 waves |
| full-field nominal wavefront | 0.0283 waves | 0.0299, 0.0301, 0.0301 waves (worse) |
| what changed | — | one angle-of-incidence ceiling on S13 in every run, plus a working-distance hold; no glass changed |
| run time | — | 77.7, 62.2, 73.8 min |

*Diffraction-limited at every field: polychromatic RMS wavefront at or under 0.0714 waves on axis and at 0.5, 0.7 and
1.0 of the field, in a simulated build perturbed within the tolerance budget, refocused, and with one group pushed
sideways by up to 0.05 mm. A build whose push sits at its travel limit counts as a failure.*

<p align="center">
  <img src="flowchart.svg" width="520" alt="The agent's loop: pick the push group (up to 200 simulated builds per candidate, shared time budget); find hotspots (sensitivity, ranked by element, group or air gap); change one lever, chosen by cause; optimize with a cheap sensitivity screen, and on no gain revert and try the next lever; confirm with a 200-trial Monte Carlo with refocus and push, then keep (about 10 points over the start, tail not higher), mark not confirmed (smaller gain) or revert (a loss); if time and levers are left, go back to the hotspots, otherwise stop and report.">
</p>

## In short: how the agent finds what to fix

This is the last finishing step of a design: the lens already meets its nominal spec, and the job is to make more of
the built lenses meet it too, by spending some of the nominal margin. Four moves, repeated:

- **The rescue part.** The agent asks which part an assembler would push sideways to rescue a bad build. It tries
  each of the three likeliest candidates on up to 200 simulated builds, within a shared time budget, and keeps the
  one whose builds come out best. In all three runs the third candidate ran out of time, so the choice was between
  two.
- **The hotspot.** It wiggles every surface and element by its tolerance, one at a time, and adds up which part
  degrades the image most. That part is the hotspot.
- **The cause.** It asks why that part is touchy (light meeting a surface at a steep angle, for example) and makes
  one change aimed at that cause. The optimizer reshapes the lens around it.
- **The check.** It runs 200 simulated builds with the same rescue push and counts how many now pass. A change that
  is clearly better than the start lens is kept. A smaller gain is only "not confirmed": the agent may work on from
  it, but must check again and report it as such. A loss is undone, and the next idea is tried.

In run 1 the hotspot was a tilt of surface S13, which the marginal ray meets at about 14°; the agent capped that angle
at 11.4°, and its own 200-build check went from 57.0 % to 68.5 % passing.

## How the agent works

The agent is Claude Opus 5.5 driving OpticStudio through the OptiVibe MCP server from Claude Code (Claude Sonnet 5.5
for subagents), following the `design-tolerance-desensitize` skill, a written procedure. Each run started from the
same lens and brief and ran unattended until the agent stopped it.

1. **Fix the judge.** The optimizer works on the design's own merit. A separate **judge merit** holds the RMS
   wavefront at each of the four fields and their maximum, so its value is the worst-field wavefront the pass rule
   reads. The agent loads it for every as-built measurement and never optimizes on it.
2. **Pick the push, as at assembly**, once, on the start lens. Each element or cemented group is scored by its element
   decenter and tilt sensitivities only, the errors a lateral push can act on. The top three each get up to a
   200-trial Monte Carlo with that group as the push, within a shared time budget, and are ranked by yield (or by P90
   when no threshold is given). A near tie is reported but never changes the pick. The chosen group is the push for
   every later measurement.
3. **Find the hotspots.** A paraxial sensitivity run (about 3 s in OpticStudio) perturbs each tolerance on its own and
   records the change in RMS wavefront. The tool assigns every row to an element, a cemented group or an air gap, adds
   each bucket in quadrature and ranks them; the buckets must add back up to OpticStudio's own RSS, or the ranking is
   withheld. The ranking is refocus-only (it cannot include the push): it says where to look, and the agent supplies
   why.
4. **One lever per round, chosen by cause.** The agent names the cause, then picks the lever for it:
   1. **Steep surface, high angle of incidence:** an angle-of-incidence ceiling at about 0.8 times the current
      angle.
   2. **A surface that could be concentric:** bend it toward concentricity.
   3. **A fighting pair:** move power out of it.
   4. **An air gap:** a thickness bound.
   5. **Glass:** only if the brief allows it (not here).

   The procedure says to avoid moving a radius directly, and no run did: the agent adds a constraint row and lets the
   optimizer find the shape.
5. **Screen cheaply.** Optimize to rest on the agent's own merit, re-read the nominal at each field and re-run the
   sensitivity. Keep screening while the RSS falls and the nominal still meets the spec. The RSS decides nothing by
   itself.
6. **Confirm with the push.** A 200-trial Monte Carlo on the judge merit with refocus plus the picked push. The tool
   wraps the group in a temporary coordinate-break layout whose x/y decenter becomes a compensator with ±0.05 mm of
   travel, flags each trial whose push sits at its limit, and proves the lens restored afterwards. The agent counts the
   yield and the tail from the per-trial values.
7. **Keep or reject, against the start lens.** About 10 points or more over the start with the tail no higher is a
   keep; a smaller gain is "not confirmed" (pool a second 200-trial run before calling it a keep); a loss reloads the
   last kept lens. The 10-point bar exists because two 200-trial runs of the same lens can differ by about that much:
   the start lens read 57.0 %, 58.5 % and 50.5 % in the three runs' own start confirms, against 57.50 % at 2000 trials.
8. **Loop, then report.** After a keep, find the hotspots again; stop when the levers on the top hotspot have failed or
   the time is spent, and report every round with its decision.

## What the three runs did

**The same in all three runs.**

- The push picker chose G14-17, the cemented triplet at S14-17. G1-3 hit its time cap unmeasured every time; in runs 1
  and 2 the pick was a declared near tie with G11-13.
- The top hotspot of the start lens was E12-13 (0.0663, 17.0 % of the RSS), led by the tilt of S13. Each run read the
  cause as the high angle of incidence on S13 (about 13.3° to 14.3°). Run 3's report names the result as coma; no run
  measured the aberration split.
- The one lever kept was an angle-of-incidence ceiling on S13 (11.4° in run 1, 11.5° in run 2, 11° and 11.5° in run
  3), plus a working-distance hold. It lowered the sensitivity RSS from 0.1605 to 0.1278-0.1300. No glass changed.
- The final nominal of the three lenses agrees within 0.0003 waves at every field.

**What they tried and turned down.**

- **The front surface, S1** (angle of incidence about 27°), the top hotspot on every delivered lens (about 18 % of the
  RSS). Ceilings of 22° to 25° either cost too much nominal margin (run 1: 0.0650 waves at full field, close to the
  limit) or did not hold up as built (with thicknesses free, 67.5 % in run 1 and 64.5 % in run 2, below the kept
  lenses' first confirms).
- **A tighter S13 ceiling** looked better on the sensitivity screen and was no better as built (64.0 % and 63.0 %
  against 68.5 %, the first confirm of each run's kept lens, runs 1 and 2).
- **A ceiling on S10** was screened out in runs 1 and 2; in run 3, stacked with a tighter S13 ceiling, it gave the
  lowest RSS of any screen in that run (0.1154, a candidate it then rejected) and no yield gain.
- **Thicknesses set free** with an S1 ceiling improved the nominal on the kept lenses (0.0274 and 0.0283 waves at
  full field, against 0.0299 and 0.0301), and their 200-trial runs read 67.5 % and 64.5 %, below the lenses already
  kept.

In five confirmed rounds the RSS fell below the kept lens's and the 200-trial run showed no better yield than that
lens (run 1 rounds 3 and 5, run 2 rounds 3 and 8, run 3 round 4); the agents went with the as-built measure each time.
(Run 2's round 4 also lowered the RSS but was screened out without a confirm.)

**Notes worth keeping.**

- Run 1's round 5 (S1 ceiling with thicknesses free, 67.5 %) was +10.5 points over the start with the tail equal, so
  it met the keep rule as written; the agent compared it with the round 1 lens instead and did not deliver it.
- The agents' counts do not always match the per-trial files. Run 1 wrote 141/200 and 134/200 where the files give
  142/200 and 135/200, and a pooled 69.5 % where the files give 69.75 %; the decisions do not change.
- Run 3 called its own gain "a keep by the skill's 10-point rule, but only just" (+9.7 over its pooled start) and left
  the verdict to the 2000-trial judge.
- Every report names the hotspots left untried (run 1's list includes the tilt of the push group itself, which the
  push does not correct) and gives the wall time of every tolerancing call.

## How it was measured

**The lens.** A 10x / NA 0.45 microscope apochromat, EFL 20 mm, working distance 3.1212 mm, 25 surfaces, 14 glass
elements (four cemented doublets, one cemented triplet, three singlets). Nominal polychromatic RMS wavefront 0.0126,
0.0156, 0.0147 and 0.0283 waves at the axis and at 0.5, 0.7 and 1.0 of the field.

**The budget.** OpticStudio's tolerance wizard preset Generic / High Precision: element decenter 0.010 mm and element
tilt 0.0167° (the wizard's rows carry 0.017°), plus radius (as fringes), thickness, index, Abbe number, surface
irregularity and surface tilt.

**As built.** Each trial perturbs the lens within the budget, refocuses and pushes one element or cemented group
sideways by up to 0.05 mm. The **yield** is the share of builds at or under 0.0714 waves at all four fields (a push at
its travel limit counts as a failure); the **tail** is the share worse than 0.143 waves.

**The brief.** Reach 90 % yield if possible; keep the element count, every glass, EFL, NA, working distance and
clearances; no image-quality rows in the merit; local optimization only; about 2 hours. The push group was the agent's
choice.

**The judge.** After each run, a 2000-trial Monte Carlo of the lens exactly as delivered, no further optimization. The
judge picked the push group for each lens itself, by the same method, and chose G14-17 every time (G1-3 hit its 900 s
cap on all four lenses). A judge run took 50 to 127 min per lens. The standard error of a 2000-trial yield near 60 % is
about 1.1 points.

## Results

All rows are 2000-trial judges with refocus plus a 0.05 mm push of G14-17. P90, P99 and median are of the worst-field
RMS wavefront in waves.

| lens | yield | change | tail (> 0.143) | P90 | mean | median | P99 | trials at push limit | run time |
|---|---|---|---|---|---|---|---|---|---|
| start | 57.50 % | — | 2.25 % | 0.0951 | 0.0722 | 0.0683 | 0.196 | 2 | — |
| agent run 1 | 63.65 % | +6.15 | 1.35 % | 0.0917 | 0.0690 | 0.0650 | 0.155 | 5 | 77.7 min |
| agent run 2 | 65.05 % | +7.55 | 1.60 % | 0.0919 | 0.0687 | 0.0644 | 0.181 | 3 | 62.2 min |
| agent run 3 | 63.90 % | +6.40 | 1.05 % | 0.0912 | 0.0687 | 0.0652 | 0.154 | 5 | 73.8 min |

The gains of 6 to 8 points are well outside the 1.1-point standard error; the three delivered lenses, within 1.4
points of one another, cannot be told apart.

![Share of builds at or under each worst-field wavefront, start lens and three agent runs](figure-10x.png)

| nominal RMS wavefront (waves) | axis | 0.5 field | 0.7 field | 1.0 field | sensitivity RSS |
|---|---|---|---|---|---|
| start | 0.0126 | 0.0156 | 0.0147 | 0.0283 | 0.1605 |
| agent run 1 | 0.0131 | 0.0163 | 0.0159 | 0.0299 | 0.1300 |
| agent run 2 | 0.0128 | 0.0162 | 0.0161 | 0.0301 | 0.1288 |
| agent run 3 | 0.0131 | 0.0162 | 0.0159 | 0.0301 | 0.1278 |

Every agent lens is slightly worse nominally at every field, by up to 0.0018 waves, and all stay far inside the
0.0714 limit: nominal margin spent to lower the sensitivity.

## Limits

- **Small bench.** Three runs on one start lens, one skill version, each delivered lens judged once.
- **Noise.** The agents' single 200-trial confirms of their kept lenses read 1.1 to 9.0 points above the judge (1.1
  to 6.2 points on the pooled basis of runs 1 and 2). On the start lens, 200-trial runs read 50.5 % to 60.0 % against
  57.50 % at 2000 trials.
- **What got worse.** The nominal at every field, and several edge thicknesses (the S18 glass edge from 1.733 mm to
  1.664-1.686 mm); all stay above their floors.
- **One lever only, and 90 % not reached.** Every kept gain is the S13 ceiling; S1 was never fixed; the best judged
  yield is 65.05 %.
- **The runs stopped early**, at 62 to 78 of about 120 minutes.
- **The push was chosen from two of three candidates**, and G11-13 was never judged at 2000 trials on a delivered lens.
- **The hotspot ranking is refocus-only**; how much of each row the push absorbs is unmeasured.
- **Slow confirms.** 193 to 1629 s per 200-trial confirm; the push pick about 24 min.
- **A generic budget** on one apochromat; nothing here says the procedure transfers to another form.

## Files

| | `10x/` |
|---|---|
| brief given to each agent run (two edits: one optimizer sentence reworded with the rule unchanged, and the judge merit's path now points to the file beside it) | [brief.md](10x/brief.md) |
| judge merit | [judge_worstfield.MF](10x/judge_worstfield.MF) |
| start lens | [result_10x-start.zmx](10x/result_10x-start.zmx) |
| agent run 1, as delivered | [result_10x-agent-run1.zmx](10x/result_10x-agent-run1.zmx) |
| agent run 2, as delivered | [result_10x-agent-run2.zmx](10x/result_10x-agent-run2.zmx) |
| agent run 3, as delivered | [result_10x-agent-run3.zmx](10x/result_10x-agent-run3.zmx) |

Run numbers on this page match the file names. The lens files open in OpticStudio; the glass catalogs they name ship
with it.
