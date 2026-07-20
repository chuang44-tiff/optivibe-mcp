# optivibe-reference

The grounding layer for optivibe-mcp: deterministic lookup tables, a SQLite
**FTS5** full-text store over the OpticStudio manual, and an intent→operand
grounding index. It lets the agent consult operand meanings, glass properties,
and manual passages so its choices are grounded rather than guessed.

It is composed **in-process** by `optivibe-harness` (not a standalone server) and
exposes five tools: `lookup_operand`, `search_reference`, `lookup_glass`,
`find_glasses`, and `find_glass_pair`.

## Data is built locally

No vendor data is committed. The glass catalog (recomputed from your install's
`.agf` files), the manual FTS5 corpus (indexed from your manual PDF), and the
description-bearing operand/tolerance catalogs are all **built on your machine
from your own licensed OpticStudio install** and are gitignored. Only the
author's own sources ship — the `*_synonyms.json` intent vocabularies and
`operand_semantics.json`. See the repo-root `README.md` for the build steps and
`PROVENANCE.md` for the data-source ledger.

Any tool whose data has not been built degrades to a typed "unavailable" result
instead of failing — the rest of the server keeps working.

See `CLAUDE.md` for more detail. Python 3.11.
