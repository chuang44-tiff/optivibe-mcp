# Changelog

All notable changes to **optivibe-mcp**.

Versions are tagged in this repository (`v0.1.0` … `v0.1.12`). Each tag points at the merge
commit that published that version, and every tag was verified against the four version
literals in the tree at that commit.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.12] — 2026-09-26

- **`render_mtf_vs_field`** — MTF plotted against real image height, tangential and sagittal,
  one curve per frequency: the way lens manufacturers publish it. A reference curve set can be
  overlaid for visual context; no comparison is computed.
- Saved candidates are named for their design (`<design>_NNN_<label>.zmx`), and the layout
  picture is saved beside each one.
- The server instructions now fit the 2,048-character limit Claude Code applies to them. Rules
  that no longer fit moved into the descriptions of the tools they govern, so what the server
  sends and what an agent receives are the same text.
- `add_math_constraint` checks an operand's sign convention against the reference layer before
  authoring it.
- The engine-session ledger records which MCP client started each engine.
- `render_layout` no longer tells agents that the native export writes text; it writes real
  PNGs.
- `build_operand_semantics.py` writes 1-based manual page citations, matching the rest of the
  reference layer.
- New dependency floor: `matplotlib>=3.6`.
- **Known limitation:** the workspace root is fixed when the server starts
  (`OPTIVIBE_WORKSPACE_ROOT`, or the launch directory). Changing it requires restarting the MCP
  server.

## [0.1.11] — 2026-09-17

- Element outlines in the layout figure.
- Sharper manual search in the reference layer.
- **MCP tool validation error on GitHub Copilot fixed** — thanks to
  [@ktgw0316](https://github.com/ktgw0316) (Masahiro Kitagawa) for the first external
  contribution to this project ([#12]).
- Review round: the operand inventory is user-built rather than tracked, and two documented
  claims the code did not support were corrected.

## [0.1.10] — 2026-09-16

- A vision review for the layout figure, acting as an advisor to the design loop.
- Review round: an unreadable-manifest remedy that mis-scoped the one row class which is not
  design-scoped, and a finding-recording path that retired the budget about to judge it.

## [0.1.9] — 2026-08-21

- A preflight that refuses a negative merit weight.
- A merit-row normalizer hardened over five rounds.
- Review round: `OverflowError` escapes closed.

## [0.1.8] — 2026-08-19

- The merit-function layer: authoring guards, a range linter, and a self-scan that reports its
  own coverage honestly.

## [0.1.7] — 2026-08-17

- Solve-reference disclosure and surface-solve observability repairs.

## [0.1.6] — 2026-08-16

- The surface-solve constraint layer.
- A policy exclusion that was reported through the fault channel, making one asphere row break
  strict mode.

## [0.1.5] — 2026-08-12

- A promoted keeper is now bound to the audit that measured it.
- A dead engine channel refuses instead of returning a misleading answer.
- The layout figure no longer invents apertures.

## [0.1.4] — 2026-08-05

- The catalog bench.
- Verdicts no longer certify more than they measured.

## [0.1.3] — 2026-07-30

- **`optivibe doctor`** — a self-diagnostic that names why an install is broken.

## [0.1.2] — 2026-07-28

- Pinned the `mcp` dependency.
- Added an engine-free test suite and Windows CI, so the package can be tested without an
  OpticStudio seat.

## [0.1.1] — 2026-07-28

- Gradient-index (GRIN) media support.

## [0.1.0] — 2026-07-19

- Initial public release of optivibe-mcp.

[0.1.12]: ../../compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.9...v0.1.10
[0.1.9]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/chuang44-tiff/optivibe-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/chuang44-tiff/optivibe-mcp/releases/tag/v0.1.0
[#12]: https://github.com/chuang44-tiff/optivibe-mcp/pull/12
