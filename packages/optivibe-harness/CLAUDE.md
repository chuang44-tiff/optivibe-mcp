# CLAUDE.md — optivibe-harness

The ZOS-API automation layer for optivibe-mcp: typed tools wrapping Zemax
OpticStudio (via pythonnet / `clr`), the MCP server, and the agent runtime. The
typed ZOS-API surface *is* the syntax-safety layer — the agent reasons about the
design and calls typed tools; there is no fragile command-string translation
step.

## What it exposes

More than 80 typed tools covering: loading/reading designs, editing lens data
(surfaces, fields, wavelengths, apertures, stop, glass), analyses (MTF, spot,
ray trace, wavefront, distortion, illumination, …), merit-function construction,
optimization, and tolerancing. It also composes the `optivibe-reference`
grounding layer in-process, so operand/glass lookup and manual search are served
through the same single MCP.

## Session model

- **Single seat (N=1):** one OpticStudio engine, one design at a time; calls are
  serialized.
- **Lazy engine-open:** the engine seat is taken on the first design-touching
  tool call, not at startup.
- **Auto-reap:** the engine is closed for you on shutdown; you never manage
  process lifecycle.

## Trust model (proof, not absence-of-error)

Every tool returns a uniform result envelope and **never raises** — inspect
`result.ok` and the read-back value, not the absence of an exception. A clean
call is not proof of success; trust the read-back or the optimizer's verdict.
The ZOS-API silently accepts some invalid inputs (bad glass, wrong-surface stop),
so the tools read back what they wrote and report the truth.

## Standing notes

- Never hardcode the OpticStudio install path — it is located via env override,
  registry, or a Program Files scan.
- Handle ZOS-API / .NET errors gracefully; OpticStudio may not be running.
- Python 3.11.

See the repo-root `README.md` and `CLAUDE.md` for install, registration, and the
agent-facing usage guide.
