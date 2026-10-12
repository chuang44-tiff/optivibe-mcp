# Contributing to optivibe-mcp

Bug reports, questions and pull requests are welcome.

## Reporting a problem

Open an [issue](../../issues/new/choose) and include:

- your OpticStudio version and licence tier (the project has been exercised only on 2025 R1, Premium);
- the output of `python -m optivibe_doctor`, or `python -m optivibe_doctor --engine` if the problem
  involves opening OpticStudio;
- the tool call and the result envelope it returned, if a tool misbehaved.

The doctor's output names local paths. Shorten your user name in them if you prefer.

A report that the server works on a version other than 2025 R1 is as useful as a bug report.

## No vendor data, in issues or in code

The repository ships code only. Glass catalogs, manual text, and operand or tolerance descriptions are
built on your own machine from your own licensed install and are never committed.
[PROVENANCE.md](PROVENANCE.md) lists every data source and its licence.

- Do not paste catalog rows, manual text or operand descriptions into an issue or a pull request.
- Do not commit a built data file. The `no-vendor-data` CI check fails any push or pull request that
  tracks one of the built catalog or description `.json` files, any `.db` file, any `.agf` file, or
  anything under a `scripts/captures/` folder.
- Do not attach a lens file that came with OpticStudio. A prescription you wrote yourself is fine.

## Pull requests

- The test suite needs no OpticStudio seat. Install the harness with its `dev` extra, then run the
  suite from the repository root:

  ```bash
  pip install -e packages/optivibe-reference
  pip install -e "packages/optivibe-harness[dev]"
  python -m pytest tests -q
  ```

  CI runs the same suite on `windows-latest`. It proves the package installs, imports and serves its
  tool manifest; it does not prove any tool computes a correct optical result.
- Tools return a result envelope and never raise. A new or changed tool keeps that contract: it
  refuses what it cannot do safely, with a reason, and it reports a read-back value where it can.
- A change that needs a live engine to verify cannot be checked by CI. Say in the pull request what
  you ran, on which OpticStudio version, and what you read back.
- Keep a pull request to one change.
