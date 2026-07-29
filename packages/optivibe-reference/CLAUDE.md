# CLAUDE.md — optivibe-reference

The grounding (reference) layer for optivibe-mcp: deterministic lookup tables, a
SQLite **FTS5** full-text store over the OpticStudio manual, and an
intent→operand grounding index. It gives the agent something to consult *before*
acting — operand meanings, glass properties, and manual passages — so choices are
grounded rather than guessed.

## How it fits

This package is **not** a standalone MCP server. The harness (`optivibe-harness`)
composes this package's `Dispatcher` **in-process** and routes reference tool
calls to it by tool name (with the SQLite connection passed as arg-0). The
dependency is one-way: the harness imports the reference layer; the reference
layer never imports the harness. There is a single OptiVibe MCP, not one per
package.

## Tools (5)

- `lookup_operand` — intent → merit/tolerance operand (synonyms + units +
  sign-convention; optional `domain="tolerance"`).
- `search_reference` — full-text search over the manual FTS5 corpus.
- `lookup_glass` / `find_glasses` / `find_glass_pair` — glass lookup by name,
  range/window search, and achromat pair-finding.

## Data is built locally, never committed

The description-bearing catalogs, the glass catalog, and the manual corpus are
**built on the user's machine from their own licensed OpticStudio install** and
are gitignored. Only the author's own sources ship: the `*_synonyms.json`
intent vocabularies and `operand_semantics.json` (units + sign-convention). See
the repo-root `README.md` and `PROVENANCE.md` for the build steps and the
data-source ledger.

**Degrade contract:** every data-backed tool tolerates unbuilt data. When a
catalog or corpus is absent (or present but unreadable), its connection is
`None` and the dispatcher returns a typed `*_unavailable` envelope
(`operand_catalog_unavailable` / `glass_catalog_unavailable` / `corpus_unavailable`)
**without crashing** — the other tools keep working. The dispatcher never
raises; callers check the returned envelope, not the absence of an exception.

## Development

```bash
python -m pytest tests -q   # the engine-free suite, from the repository root
```

- **Python:** 3.11
- Reference knowledge is pulled on-demand through the MCP tools, not force-loaded
  per prompt.
