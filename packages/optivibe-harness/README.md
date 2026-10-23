# optivibe-mcp

<!-- mcp-name: io.github.chuang44-tiff/optivibe-mcp -->

ZOS-API automation, typed tools, and the MCP server for optivibe-mcp. Drives
Zemax OpticStudio through the typed ZOS-API (.NET via pythonnet / `clr`), and
composes the `optivibe-reference` grounding layer in-process so everything is
served through one MCP.

It exposes more than 80 typed tools — loading and editing designs, analyses,
merit-function construction, optimization, and tolerancing — each returning a
uniform result envelope rather than raising, so a caller checks a read-back value
instead of assuming a call succeeded. OpticStudio is opened lazily on the first
design-touching call and reaped automatically; one design at a time.

Install with `pip install optivibe-mcp` (Windows, Python 3.11+, and a Zemax OpticStudio
licensed for the ZOS-API). The [repository README](https://github.com/chuang44-tiff/optivibe-mcp#readme) covers
building the reference data and registering the server with Claude Code, and `AGENTS.md` in
the repository describes the usage model.
