"""Disclosures that ride the FAILING tails of ``dry_run`` / ``optimize`` (both tail sets).

``ride(env, system, nw)`` takes an ERROR envelope that is about to be returned and adds, WITHOUT
overwriting any key the refusal already carries:

* the negative-weight scan's fault record (``negative_weight_scan_fault``) or its unestablished
  count (``unestablished_weight_rows``), on the SAME key names the success tails use, plus the
  matching ``warning`` sentence;
* when the scan found negative-weight offenders on a refusal that is not itself the
  negative-weight refusal, the offender rows (``negative_weight_rows``) -- a fresh scan must not
  find something and then drop it;
* the malformed-range lint (``malformed_ranges`` / ``malformed_range_warning``).

Two call shapes, and the difference is deliberate:

* ``ride(env, system)`` -- an EARLIER refusal (nothing opened). ``nw`` is scanned here if the
  caller has not scanned yet, and the lint runs. Cost: up to two extra merit scans on a refusal
  path (cheap on an empty merit; a 300-row merit pays two).
* ``ride(env, None, nw)`` -- a POST-OPEN failure (the optimizer / Hammer handle is open). Post-open
  MFE readability is unproven, so NO fresh scan runs: only the ``nw`` captured before the run is
  carried. A post-open tail therefore carries no lint.

What it does not cover: an ``OptimizeError`` raised after the scans (a bad ``cycles`` value) leaves
through ``optimize()``'s outer catch, where the scan result is out of reach; and the timed-Hammer
param refusal from ``_hammer_timed.preflight`` returns before anything is scanned.

``optimize_run`` is imported inside the function: this module is imported BY ``optimize_run``.
"""
from . import _optimize_common as _oc


def ride(env, system, nw=None):
    """Return ``env`` with the disclosures merged in (mutates and returns the same dict)."""
    from . import optimize_run as _orun

    if nw is None and system is not None:
        nw = _oc._scan_negative_weights(system)
    warning = env.get("warning")
    keys = {}
    if nw is not None and nw["offenders"]:
        keys["negative_weight_rows"] = nw["offenders"]
    elif nw is not None:
        warning, keys = _orun._negative_weight_disclosure(nw, warning)
    if system is not None:
        warning, lint_keys = _orun._malformed_range_finding(system, warning)
        keys.update(lint_keys)
    for key, value in keys.items():
        env.setdefault(key, value)
    if warning is not None:
        env["warning"] = warning
    return env
