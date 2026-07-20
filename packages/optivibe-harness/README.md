# optivibe-harness

ZOS-API automation, typed tools, and the MCP server for optivibe-mcp. Drives
Zemax OpticStudio through the typed ZOS-API (.NET via pythonnet / `clr`), and
composes the `optivibe-reference` grounding layer in-process so everything is
served through one MCP.

It exposes more than 80 typed tools — loading and editing designs, analyses,
merit-function construction, optimization, and tolerancing — each returning a
uniform result envelope rather than raising, so a caller checks a read-back value
instead of assuming a call succeeded. OpticStudio is opened lazily on the first
design-touching call and reaped automatically; one design at a time.

See the repo-root `README.md` for install and registration and `CLAUDE.md` for
the usage model. Python 3.11.
