# optivibe-mcp

**Lens design with an AI agent.** optivibe-mcp gives a coding agent such as Claude Code typed tools for Zemax
OpticStudio, and skills that take a lens from its first form through glass choice to an as-built tolerance check.
It is experimental.

![A 10× / NA 0.45 objective an agent designed in one unattended run, as OpticStudio draws it](demos/walk-in-10x/walk-in-10x-layout.png)

*One unattended run: a 60× objective reworked into a 10× / NA 0.45 apochromat, polychromatic Strehl ≥ 0.935 at
every field point. [How it was done and measured](demos/walk-in-10x/README.md).*

```bash
pip install optivibe-mcp "optivibe-reference[manual]"
optivibe-build-data --probe
claude mcp add optivibe -- C:/path/to/venv/Scripts/optivibe-mcp.exe
```

On [PyPI](https://pypi.org/project/optivibe-mcp/) and in the official [MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.chuang44-tiff/optivibe-mcp)
as `io.github.chuang44-tiff/optivibe-mcp`. Details: [Install](#install).

## What it does

- **More than 80 typed tools** (`optivibe-mcp`, in `packages/optivibe-harness/`) drive OpticStudio over the ZOS-API:
  load and edit lenses, analyse, optimize, trial glass swaps and tolerance. Beyond spherical surfaces they author
  aspheres, coordinate breaks, fold mirrors, diffraction gratings and gradient-index media. Each returns a result
  envelope with read-backs instead of raising, so the agent checks a value rather than assuming a call worked.
- **Skills** carry a design through its stages: optimize the form, choose glass, then desensitize the lens so more
  built copies pass (see [Skills](#skills)).
- **A reference layer** (`optivibe-reference`) grounds the agent's choices: operand and glass lookups and manual
  search. Engine-free and licence-free; its data is built locally (see
  [Build the reference data](#build-the-reference-data)).

The project ships **code only**. No vendor data (glass catalog, manual text, operand descriptions) is included; you
generate it from your own licensed OpticStudio install.

## Demos

| Demo | Result |
|---|---|
| [A 60× objective reworked into a 10× apochromat, unattended](demos/walk-in-10x/README.md) | Strehl ≥ 0.935 at all four field points in one run |
| [The same job on a local model](demos/local-model-10x/README.md) | Strehl ≥ 0.834 with a tuned brief; the page says where it trails Opus |
| [Refining a 20× zoom](demos/zoom-20x/README.md) | Blur at the worst zoom position more than halved; the distortion target missed |
| [Does the agent design better when it sees the drawing?](demos/vision-ab/README.md) | Every vision run made a shorter lens: 70–100 mm against 140–221 mm |
| [The glass step against OpticStudio's Hammer](demos/glass-picking/README.md) | On the 10×, all three runs beat Hammer's merit by 12–19 % |
| [Desensitizing a finished 10× so more built lenses pass](demos/tolerance-desensitize/README.md) | Builds diffraction-limited at every field: 57.50 % to 69.25–74.65 % in four runs |

Each demo ships its brief and lens files; [demos/](demos/README.md) lists them and has a script that re-measures a
lens file.

## Prerequisites

- **Windows.** OpticStudio and the ZOS-API .NET surface are Windows-only.
- **Zemax OpticStudio**, licensed for the ZOS-API. Developed against
  **2025 R1 (25.1.0, Premium)**; other versions are untested. Access is gated at
  first engine open on `IsValidLicenseForAPI`.
- **Python 3.11+.**
- **.NET Framework** — used automatically. The bootstrap sets
  `PYTHONNET_RUNTIME=netfx` and locates your install directory (via env override,
  registry, or a Program Files scan). No manual setup.

## Install

### From PyPI

In a Python 3.11+ virtual environment:

```bash
pip install optivibe-mcp "optivibe-reference[manual]"
optivibe-build-data --probe
optivibe-doctor
```

Both packages are on PyPI: [`optivibe-mcp`](https://pypi.org/project/optivibe-mcp/) and
[`optivibe-reference`](https://pypi.org/project/optivibe-reference/), released together with
matching versions. `optivibe-mcp` is the server; it brings the reference layer (`optivibe-reference`) with it. The
`[manual]` extra (PyMuPDF) is optional and reads your OpticStudio manual PDF (see
[Manual install](#manual-install)). `optivibe-build-data` builds the reference data from your own
OpticStudio install, and `--probe` also runs the two operand probes, which open OpticStudio and
need the ZOS-API licence. `optivibe-doctor` checks the install. Then
[register the server with Claude Code](#register-with-claude-code).

`optivibe-build-data` writes into the installed package's `data/` and `data/captures/`
directories, so install `optivibe-mcp` into a virtual environment (or with `pip install --user`)
rather than into a system Python whose site-packages is not writable.

### From a clone

Clone the repository, then run one command from its root:

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

It creates a virtual environment in `.venv`, installs both packages, builds the reference
data from your OpticStudio install, runs the doctor, and prints the command that registers
the server with Claude Code. It opens no OpticStudio session. Options:

- `-Python <path>`: the Python 3.11+ to build the environment from, when the `python` on
  PATH is older;
- `-Probe`: also run the two operand probes, which open OpticStudio and need the ZOS-API
  licence (see [Build the reference data](#build-the-reference-data));
- `-Register`: run the `claude mcp add` command for you.

The script only runs the steps below, which you can also do by hand.

### Manual install

From `packages/optivibe-harness/`:

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

Then check the install:

```bash
python -m optivibe_doctor
```

It checks the interpreter, the two packages, their dependencies, the reference data and your
OpticStudio install directory, and prints one line per check with a remedy for anything that is
wrong. It opens no OpticStudio session unless you ask: `--engine` also opens one and proves the
ZOS-API licence, and `--boot` starts the real server once. On a fresh install the reference-data
checks read `WARN` until you [build the reference data](#build-the-reference-data), and the
exit code is 1 while any check warns.

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

After a PyPI install you can register the `optivibe-mcp` command instead. Give its full path
(`where optivibe-mcp` prints it while the environment is active):

```bash
claude mcp add optivibe -- C:/path/to/venv/Scripts/optivibe-mcp.exe
```

## Glass tools

Once the optimizer has settled a lens, three tools help with the glass step. `explain_glass_swaps`
is read-only: it lists which glass elements carry the colour and Petzval terms. `trial_glass_swaps`
tries up to four swap sets, each re-optimized beside an unchanged control, and judges them on
measured image quality; it edits the open lens while it runs and restores it at the end.
`substitute_glass` makes one swap and leaves it in place. The `design-glass-matching` skill runs
this step.

To have Claude Code ask you before either tool that edits the lens runs, add an `ask` rule to your
settings (for example `.claude/settings.json`):

```json
{
  "permissions": {
    "ask": ["mcp__optivibe__trial_glass_swaps", "mcp__optivibe__substitute_glass"]
  }
}
```

An `ask` rule takes precedence over an `allow` rule and still asks under `bypassPermissions`. In a
headless run (`claude -p`) or in `dontAsk` mode there is no one to ask, so a matching call is
denied.

## Build the reference data

The reference layer's data is built locally from your own licensed install; nothing
vendor-derived is committed. After a PyPI install, run `optivibe-build-data` (add `--probe`
to run the two probes below first); it needs no clone. From a clone, run it from the
repository root:

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
| `design-vision-review` | Observe a rendered layout and report what lens data does not show (advisory) |
| `design-aberration-review` | Read the ray fan and field curvature / distortion plots and report how the aberrations are balanced (advisory) |
| `optimize-loop` | Run optimization with monitoring |
| `design-glass-matching` | Choose glass for a settled lens: find the elements that carry the colour or field-curvature terms, trial swaps against a control, and keep a swap only when overall image quality improves |
| `design-tolerance-desensitize` | The last finishing stage: pick the push compensator, find the tolerance hotspots, and keep a change only when the as-built yield improves |
| `bench-catalog` | Benchmark and rank a folder of lens designs against a target spec or each other |
| `zos-api-debug` | Troubleshoot OpticStudio connection / license errors |

## Status

Experimental, early-stage and provided as-is; expect rough edges. It has been developed
and exercised against a single OpticStudio version (2025 R1) on Windows; behavior on
other versions is unverified. Interfaces and tool coverage may change.

**Running another OpticStudio version or licence tier?** Please
[open an issue](../../issues/new/choose) with the output of `python -m optivibe_doctor --engine`,
whether it works or not. A report that it works on your version is as useful as a bug.

## Contributing & contact

Bug reports, questions, and pull requests are welcome via
[GitHub Issues](../../issues) and pull requests; see [CONTRIBUTING.md](CONTRIBUTING.md).
Please don't include any vendor-derived data (glass catalogs, manual text, operand
descriptions) in issues or PRs — see [PROVENANCE.md](PROVENANCE.md).

## Licensing & provenance

optivibe-mcp is licensed under the **Apache License 2.0** — see [LICENSE](LICENSE)
and [NOTICE](NOTICE).

It is code-only and drives your own licensed OpticStudio install at runtime; no
vendor data or SDK files are redistributed. The reference data you build locally
stays local (it is gitignored). optivibe-mcp is an independent project and is not
affiliated with or endorsed by Ansys/Zemax. See [PROVENANCE.md](PROVENANCE.md) for
the data-source and license ledger.
