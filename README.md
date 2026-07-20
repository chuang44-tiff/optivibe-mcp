# optivibe-mcp

optivibe-mcp is an **experimental** MCP server that provides tooling for a coding
agent — such as Claude Code — to interface with Zemax OpticStudio.

It exposes OpticStudio operations as typed MCP tools: loading designs, editing lens
data, running analyses, optimization, and tolerancing, driven through the ZOS-API so
the agent doesn't have to write raw API code. Alongside the tools is a reference
layer the agent can query — operand and glass lookups, and full-text search over the
OpticStudio manual.

This is early-stage and provided as-is. Expect rough edges; see [Status](#status).

## What's in it

- **Tools** (`optivibe-harness`) — more than 80 typed tools that drive OpticStudio over
  the ZOS-API. Each returns a uniform result envelope rather than raising, so the
  agent checks a read-back value instead of assuming a call succeeded.
- **Reference layer** (`optivibe-reference`) — operand and glass lookups plus manual
  search, to help ground the agent's choices. Engine-free and license-free; its data
  is built locally (see [Build the reference data](#build-the-reference-data)).

The project ships **code only**. No vendor data (glass catalog, manual text, operand
descriptions) is included — you generate it from your own licensed OpticStudio
install.

## Prerequisites

- **Windows.** OpticStudio and the ZOS-API .NET surface are Windows-only.
- **Zemax OpticStudio**, licensed for the ZOS-API. Developed against
  **2025 R1 (25.1.0, Premium)**; other versions are untested. Access is gated at
  startup on `IsValidLicenseForAPI`.
- **Python 3.11+.**
- **.NET Framework** — used automatically. The bootstrap sets
  `PYTHONNET_RUNTIME=netfx` and locates your install directory (via env override,
  registry, or a Program Files scan). No manual setup.

## Install

Clone the repository, then from `packages/optivibe-harness/`:

```bash
pip install -e ../optivibe-reference   # reference layer first
pip install -e .                        # then the harness
```

Install the reference package first — the harness loads it in-process, and without
it the reference and lookup tools won't be available.

The `[manual]` extra (PyMuPDF) reads your OpticStudio manual PDF. It is needed for
**both** manual search **and** the operand/tolerance description enrichment (the
build extracts verbatim descriptions from the PDF). Without it, the operand and
tolerance catalogs build **synonyms-only** (grounding still works — every entry is
searchable by its synonyms — but carries no manual descriptions). To get the full
enriched build, install it:

```bash
pip install -e "../optivibe-reference[manual]"
```

A per-package `environment.yml` (Python 3.11) is provided if you prefer conda.

## Register with Claude Code

```bash
claude mcp add optivibe -- /path/to/python -m optivibe_harness
```

`/path/to/python` is the interpreter of the environment you installed into — pointing it at
a different Python is a common cause of silent failures. You can find it with:

```bash
python -c "import sys; print(sys.executable)"
```

The equivalent JSON configuration:

```json
{ "command": "/path/to/python", "args": ["-m", "optivibe_harness"] }
```

## Build the reference data

The reference layer's data is built locally from your own licensed install; nothing
vendor-derived is committed. Run it from the repository root:

```bash
python packages/optivibe-reference/scripts/build_vendor_data.py
```

This reads the glass catalog from your `.agf` files (`ZEMAX_GLASSCAT`) and, if you
point it at the manual PDF (`OPTIVIBE_MANUAL_PDF`), indexes it for search.

The operand and tolerance catalogs are built from a live probe of your install's
ZOS-API enum surface. Run the two probes **once** first (each boots a headless
OpticStudio session, reads the operand/tolerance surface, and reaps it):

```bash
python packages/optivibe-reference/scripts/probe_operands.py
python packages/optivibe-reference/scripts/probe_tolerances.py
```

If an input is missing the corresponding step is skipped: that tool (glass or
manual search, or operand/tolerance lookup) returns a typed "unavailable" result
and the rest of the server keeps working. Details and the data-source ledger are
in [PROVENANCE.md](PROVENANCE.md).

## Running it

OpticStudio is not opened at startup — the engine is launched on the first tool call
that touches a design, and it works with one design at a time. The reference and
lookup tools work with no engine running — so install, registration, the reference
build, and every lookup succeed regardless of license tier; the ZOS-API license is
checked only when the engine opens.

A note on trust: tools return a result envelope and don't raise on failure, so a call
completing is not proof it did what you intended. Check the returned value or the
optimizer's verdict rather than the absence of an error.

## Skills

The repository includes a few Claude Code skills for common tasks:

| Skill | For |
|---|---|
| `design-review` | Assess a design against a spec |
| `design-compare` | Compare designs side by side |
| `optimize-loop` | Run optimization with monitoring |
| `zos-api-debug` | Troubleshoot OpticStudio connection / license errors |

## Status

Experimental. It has been developed and exercised against a single OpticStudio
version (2025 R1) on Windows; behavior on other versions is unverified. Interfaces
and tool coverage may change. Issue reports are welcome.

## Contributing & contact

Bug reports, questions, and pull requests are welcome via
[GitHub Issues](../../issues) and pull requests. Please don't include any
vendor-derived data (glass catalogs, manual text, operand descriptions) in
issues or PRs — see [PROVENANCE.md](PROVENANCE.md).

## Licensing & provenance

optivibe-mcp is licensed under the **Apache License 2.0** — see [LICENSE](LICENSE)
and [NOTICE](NOTICE).

It is code-only and drives your own licensed OpticStudio install at runtime; no
vendor data or SDK files are redistributed. The reference data you build locally
stays local (it is gitignored). optivibe-mcp is an independent project and is not
affiliated with or endorsed by Ansys/Zemax. See [PROVENANCE.md](PROVENANCE.md) for
the data-source and license ledger.
