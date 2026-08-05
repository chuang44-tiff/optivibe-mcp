"""optivibe_harness.catalog — the core.

An INTERNAL library (NOT an MCP tool): the skill drives it
in-process by building a ``server.Dispatcher(ZemaxSession())`` and calling
``bench.bench_folder``.

Modules:
- ``metrics``  — the shared per-reading firewall predicates (the 9-token status
  enum, ``reading_ok``, ``coerce_metric``, ``coerce_label``, ``config_sweep_ok``).
  PURE (no engine call, no registry import); built FIRST.
- ``registry`` — the frozen ``MetricAdapter`` contract + the 12 concrete adapters +
  ``profile`` (owns dispatch -> reading_ok -> extract -> coerce). Imports ``metrics``.
- ``bench``    — ``bench_folder`` / ``_bench_one`` / ``row_status`` / the CSV+manifest
  writers / the no-target median basis. Imports both.

No public import surface is guaranteed — the marker exists only to make ``catalog``
a package.
"""
