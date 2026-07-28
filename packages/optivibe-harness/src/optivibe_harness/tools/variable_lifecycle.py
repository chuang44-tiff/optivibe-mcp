"""tools/variable_lifecycle.py — the S1 variable-lifecycle tools (iterative-design-gaps).

TWO dispatchable tools over the BUILT optimize-variable substrate (the
``_optimize_common`` enumerator the three preflight counters share):

- ``list_variables`` — READ-ONLY: enumerate EVERY optimizer variable (Variable-solved
  cell) across LDE (radius/thickness/conic) + asphere coefficients + per-config (MCE)
  cells, as a ``source``-discriminated inventory ({source, surface|row, cell, value,
  solve}) with a per-source tally + the ``opt.Variables`` cross-check
  (``inventory_matches_optimizer``). Use after ``load_design`` to SEE the inherited
  variables a loaded ``.zmx`` silently carries in (variable solves SURVIVE a
  load) before you optimize. NO mutation.

- ``clear_all_variables`` — bulk-clear EVERY variable system-wide back to Fixed,
  read-back-proven (a re-enumerate reads 0). The honest framing: each cleared cell FREEZES
  AT ITS CURRENT value — it does NOT restore a prior value. A clear that did
  NOT take (a silent ``MakeSolveFixed`` no-op / a per-cell throw left a residual) REFUSES
  (``ok:false``, ``cleared_all:false``, ``unclear_residual``); the cells that
  DID clear stay cleared (no rollback — a bulk clear has no checkpoint).

The enumerator + the bulk-clear core live in ``_optimize_common`` so ``load_design`` /
``apply_lens_spec`` (the inherited-variable disclosure + opt-in ``reset_variables``) import
them WITHOUT importing this dispatchable module (no import cycle).

ONE new STRING error family ``variable_lifecycle`` (shared by both tools), attached via the
existing ``error_envelope`` helper (the ``_config_common`` / ``mce_config`` / ``freeze_semi``
precedent — NO new error class). Both handlers NEVER raise past their boundary (L26).

Live ZOS-API integration: exercised by the live test; unit-tested
against 5-var-survives-load fakes (the
mock-divergence guard — the four REDDEN axes).
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _optimize_common as _oc
from . import optimize_variable as _var
from ._analysis_common import error_envelope

_FAMILY = _oc._VARIABLE_LIFECYCLE_FAMILY

# The cell tokens `vary` bulk-sets — the radius/thickness/conic scope `set_variable`
# owns. Asphere coefficients (set_asphere_variable) and per-config MCE cells
# (set_config_variable) are DELIBERATELY out of scope (they carry extra required
# params). A cells entry outside this set is a MALFORMED request (whole-call refuse).
_VARY_CELLS = ("radius", "thickness", "conic")


def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _open_count_close_variables(system):
    """Open the optimizer, read ``opt.Variables`` (the DOF count), close it (L22).

    The ``opt.Variables`` cross-check for ``list_variables`` (the enumerator is the itemized
    truth; the optimizer count is a NON-FATAL corroborator). Opens ONCE, reads the
    ``Variables`` PROPERTY (probe D: NOT ``NumberOfVariables``, which does not exist on
    ``ILocalOptimization``), ``Close()`` in finally (the L22 single-seat reap). Returns the
    int count, or ``None`` if the optimizer is unavailable / the count is unreadable (a
    non-fatal degradation — ``inventory_matches_optimizer`` is then ``None``).
    """
    opt = None
    try:
        opt = system.Tools.OpenLocalOptimization()
        if opt is None:
            return None
        try:
            return int(opt.Variables)
        except Exception:  # noqa: BLE001 — an unreadable count degrades to None
            return None
    except Exception:  # noqa: BLE001 — the optimizer being unavailable is non-fatal
        return None
    finally:
        if opt is not None:
            try:
                opt.Close()
            except Exception:  # noqa: BLE001 — teardown must never raise (L22)
                pass


def list_variables(session, params):
    """List EVERY optimizer variable across LDE + asphere + per-config MCE (read-only).

    No params. Enumerates the inventory (the ``_optimize_common`` shared walk), tallies it
    per source, and cross-checks ``len(inventory)`` against ``opt.Variables`` (a NON-FATAL
    corroborator — a mismatch is disclosed via ``inventory_matches_optimizer:false``, never
    flips ``ok``; ``None`` when the optimizer is unavailable). NO mutation. NEVER raises.
    """
    params = _require_dict(params)
    system = session.system
    try:
        member = _oc._solve_type_variable_enum(system)
        # Thread the per-source discovery-fault signal so the inventory is never
        # SILENTLY short — a source whose enumeration deterministically faults drops that
        # whole source's coverage, and a consumer must SEE the inventory was incomplete.
        faults = []
        inventory = _oc._variable_inventory(system, member, faults)
        opt_variables = _open_count_close_variables(system)
        matches = (
            None if opt_variables is None else len(inventory) == opt_variables
        )
        result = {
            "ok": True,
            "tool": "list_variables",
            "n_variables": len(inventory),
            "variables": inventory,
            "by_source": _oc._by_source_tally(inventory),
            "opt_variables": opt_variables,
            "inventory_matches_optimizer": matches,
        }
        # Keep the happy path BYTE-IDENTICAL: disclose the partial-enumeration signal ONLY
        # when a discovery fault actually occurred (no fault -> no new keys).
        if faults:
            result["enumeration_complete"] = False
            result["enumeration_faults"] = faults
        return result
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary (L26)
        return error_envelope(
            "list_variables", _FAMILY,
            f"unexpected fault enumerating optimizer variables ({exc!r})",
        )


def clear_all_variables(session, params):
    """Clear EVERY optimizer variable system-wide back to Fixed, read-back-proven.

    No params. Delegates to the shared ``_clear_all_variables_core`` (the SAME locus the
    load/apply ``reset_variables`` hooks use, L30): re-fetch each Variable cell from its
    identifiers, ``MakeSolveFixed()`` it (guarded per-cell), then RE-ENUMERATE -> 0 as the
    proof. A clear that did NOT take refuses (``ok:false``, ``cleared_all:false``,
    ``unclear_residual``). Each cleared cell FREEZES AT ITS CURRENT value (``frozen_at_current``
    + the honest ``note``) — it does NOT reset to a prior value. NEVER raises.
    """
    _require_dict(params)
    return _oc._clear_all_variables_core(session.system)


def vary(session, params):
    """Bulk-make many surfaces' cells optimizer Variables in ONE call, read-back-proven.

    The bulk ``set_variable`` sibling of ``list_variables``/``clear_all_variables`` — the
    bulk-authoring win (author ``[2,..,20] x [radius,thickness]`` in ONE call instead of
    dozens of round-trips). Required: ``surfaces`` (a typed JSON array of int surface
    numbers) + ``cells`` (a typed JSON array of tokens from radius|thickness|conic). Cell
    scope is radius/thickness/conic ONLY — asphere coefficients (``set_asphere_variable``)
    and per-config MCE cells (``set_config_variable``) are separate tools with extra
    required params.

    TWO-PHASE:

    - VALIDATE THE REQUEST UP FRONT (zero-mutation refuse): a malformed ``surfaces`` (not a
      non-empty list of ints) or a ``cells`` token outside {radius,thickness,conic} is a
      malformed CALL -> refuse the WHOLE call (``ok:false``, ``_FAMILY`` family), mutate
      NOTHING (the ``build_merit`` precedent).
    - THEN CONTINUE-AND-REPORT: for each (surface, cell) pair call the proven
      ``set_variable`` core (read-back-proven — a lying ``MakeSolveVariable`` bool RAISES
      ``SurfaceWriteError``). An OUT-OF-RANGE surface (OBJECT 0 / IMAGE / beyond) SKIPS
      that one pair; a per-pair ``set_variable`` refusal (plano/degenerate cell) buckets
      into ``refused`` — the batch CONTINUES (the bulk-friction goal; one bad pair never
      fails the batch).

    Returns ``{ok, tool, n_applied, n_skipped, n_refused, applied, skipped, refused,
    n_variables_now, inventory_matches_optimizer}`` echoing the shared ``_variable_inventory``
    cross-check. ``ok:false`` ONLY when NOTHING was applied (never let the caller believe a
    fully-failed bulk op succeeded). NEVER raises past the boundary (L26).
    """
    params = _require_dict(params)
    surfaces = params.get("surfaces")
    cells = params.get("cells")

    # Phase 1 — validate the REQUEST up front (a malformed call -> whole-call refuse,
    # ZERO mutation). A bad cell TOKEN or a malformed `surfaces` is a malformed CALL.
    if (
        not isinstance(surfaces, list)
        or not surfaces
        or not all(isinstance(s, int) and not isinstance(s, bool) for s in surfaces)
    ):
        return error_envelope(
            "vary", _FAMILY, "surfaces must be a non-empty array of ints"
        )
    if (
        not isinstance(cells, list)
        or not cells
        or not all(c in _VARY_CELLS for c in cells)
    ):
        return error_envelope(
            "vary", _FAMILY,
            f"cells must be a non-empty array of {'|'.join(_VARY_CELLS)}",
        )

    try:
        # Dedupe (preserve order) so a repeated surface/cell is one attempt.
        surfaces_deduped = list(dict.fromkeys(surfaces))
        cells_deduped = list(dict.fromkeys(cells))

        system = session.system
        n = int(system.LDE.NumberOfSurfaces)

        applied = []
        skipped = []
        refused = []
        for s in surfaces_deduped:
            # Interior optical surfaces only — OBJECT (0) and IMAGE (N-1) are skipped
            # (an out-of-range surface is NOT a malformed call; it skips ONE pair, DIV-6).
            if not (1 <= s <= n - 2):
                skipped.append(
                    {"surface": s,
                     "reason": "out of geometry range (OBJECT/IMAGE excluded)"}
                )
                continue
            for c in cells_deduped:
                try:
                    _var.set_variable(session, {"surface": s, "cell": c})
                    applied.append(
                        {"surface": s, "cell": c, "solve_type": "Variable"}
                    )
                except SurfaceWriteError as exc:  # silent MakeSolveVariable no-op
                    refused.append(
                        {"surface": s, "cell": c, "reason": str(exc),
                         "family": "surface_write"}
                    )
                except ToolParamError as exc:  # a bad surface/cell reached set_variable
                    refused.append(
                        {"surface": s, "cell": c, "reason": str(exc),
                         "family": "tool_param"}
                    )
                except Exception as exc:  # noqa: BLE001 — a per-pair fault never aborts
                    refused.append(
                        {"surface": s, "cell": c, "reason": repr(exc)}
                    )

        # Cross-check via the SHARED enumerator (the list_variables truth source).
        member = _oc._solve_type_variable_enum(system)
        n_variables_now = len(_oc._variable_inventory(system, member))
        opt_variables = _open_count_close_variables(system)
        inventory_matches_optimizer = (
            None if opt_variables is None else n_variables_now == opt_variables
        )

        # ok:false ONLY when NOTHING applied (every pair skipped/refused) — never let the
        # caller believe a fully-failed bulk op succeeded.
        if not applied:
            return error_envelope(
                "vary", _FAMILY,
                "no cell was varied (every candidate pair was skipped or refused)",
                n_applied=0, n_skipped=len(skipped), n_refused=len(refused),
                applied=applied, skipped=skipped, refused=refused,
                n_variables_now=n_variables_now,
                inventory_matches_optimizer=inventory_matches_optimizer,
            )

        return {
            "ok": True,
            "tool": "vary",
            "n_applied": len(applied),
            "n_skipped": len(skipped),
            "n_refused": len(refused),
            "applied": applied,
            "skipped": skipped,
            "refused": refused,
            "n_variables_now": n_variables_now,
            "inventory_matches_optimizer": inventory_matches_optimizer,
        }
    except Exception as exc:  # noqa: BLE001 — never raise past the boundary (L26)
        return error_envelope(
            "vary", _FAMILY, f"unexpected fault bulk-varying ({exc!r})"
        )


LIST_VARIABLES_SPEC = ToolSpec(
    name="list_variables",
    handler=list_variables,
    required_params=(),
    param_types={},
    description=(
        "List EVERY optimizer variable (Variable-solved cell) across the system — LDE "
        "radius/thickness/conic, asphere coefficients, and per-config (MCE) cells — as an "
        "inventory ({source, surface|row, cell, value, solve}) with a per-source tally + "
        "the opt.Variables cross-check. Read-only. Use after load_design to SEE inherited "
        "variables before you optimize. Returns active optimization DOFs under variables; "
        "inspect that list before optimizing."
    ),
)

CLEAR_ALL_VARIABLES_SPEC = ToolSpec(
    name="clear_all_variables",
    handler=clear_all_variables,
    required_params=(),
    param_types={},
    description=(
        "Clear EVERY optimizer variable system-wide (LDE + asphere + per-config MCE) back "
        "to Fixed, read-back-proven (a re-list reads 0). Gotcha: each cleared cell FREEZES "
        "AT ITS CURRENT value — it does NOT restore an original; if a value is bad, "
        "set_surface it or load a snapshot. Use before deliberately re-varying so an "
        "inherited variable can't silently drive optimization."
    ),
)

VARY_SPEC = ToolSpec(
    name="vary",
    handler=vary,
    required_params=("surfaces", "cells"),
    param_types={"surfaces": "array", "cells": "array"},
    description=(
        "Bulk-make many surfaces' cells optimizer variables in ONE call, each "
        "read-back-proven. Params: surfaces (array of int), cells (array of "
        "radius|thickness|conic). Returns applied/skipped/refused buckets + the "
        "variable-inventory cross-check. Gotcha: asphere/MCE coefficients are out of "
        "scope (use set_asphere_variable / set_config_variable); an out-of-range "
        "surface is skipped, a bad cell token refuses the whole call."
    ),
)

TOOL_SPECS = (LIST_VARIABLES_SPEC, CLEAR_ALL_VARIABLES_SPEC, VARY_SPEC)
