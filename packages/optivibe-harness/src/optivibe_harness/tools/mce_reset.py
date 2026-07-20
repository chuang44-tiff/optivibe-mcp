"""tools/mce_reset.py — the multi-configuration DELETE / reset-to-single-config tools.

TWO dispatchable Multi-Configuration-Editor tools that COLLAPSE configs (the DELETE/reset
concern, distinct from ``mce_config``'s CREATE/author primitive):

- ``remove_configuration(config)`` — ``MCE.DeleteConfiguration(n)`` (1-based, mandatory
  index). The delete is read-back-proven by the ``NumberOfConfigurations`` DECREMENT, NEVER
  the lying-return bool (F4/D14 — the bool returns True even on a no-op). Refuses the
  last/only config (a 1-config system would leave a 0-config corpse), an out-of-range index,
  and a non-integral ``config`` PRE-mutation (ZERO mutation). Discloses the engine-owned
  active-index shift (``current_before``/``current_after``/``active_config_shifted``, read
  back — the handler does NOT re-implement the shift math) + ``mce_rows_preserved`` (a config
  delete removes a per-config COLUMN, the operand ROWS survive — probe §B) + ``deleted_config_dofs``
  (the per-config MCE-source variable DOFs the deleted config carried, SURFACED via the
  ``_variable_inventory`` MCE-filter — never CLEARED).

- ``reset_to_single_config()`` — ``MCE.MakeSingleConfiguration()`` (the micro-probe-blessed
  primitive, 2026-06-26: it BAKES the ACTIVE config into the single survivor, drives the count
  to 1, AND clears the MCE rows to the 1-row floor). The count-1 read-back is the proof (NOT
  the lying bool). Idempotent on an already-single-config system (NO engine call). Discloses
  ``surviving_config`` (the active-config index, read BEFORE the collapse — the agent always
  knows which config's geometry baked), ``mce_rows_cleared`` / ``operands_before``/``after``
  (the ``NumberOfOperands`` read-back, never assumed), ``n_per_config_dofs_removed`` (the
  MCE-filtered inventory count read BEFORE the collapse — DISCLOSE-only, the row-drop removes
  the per-config DOFs structurally), and ``apply_lens_spec_unblocked`` (the count-1 gate now
  passes — the dogfood-#6 "load a blank .zmx" workaround retired).

THE TWO LOAD-BEARING INVARIANTS (the TOP risks):
- **Lying-bool:** EVERY proof is the ``NumberOfConfigurations`` count read-back. A SUT
  trusting the engine's return bool ships a false ``ok:true`` on a no-op; the count guard
  REFUSES it (``mce_config`` "did not decrement"/"did not collapse").
- **Over-clear:** NEITHER tool calls ``_clear_all_variables_core`` (which clears LDE +
  asphere + MCE solves — it would FREEZE the LDE/asphere DOFs the agent set and wants to KEEP
  through the rebuild). The reset only READS ``_variable_inventory`` + FILTERS ``source=="mce"``
  for DISCLOSURE; the DOF removal is the engine's structural consequence of the row-drop.

Every handler returns the uniform never-raise envelope (the L26 firewall) and NEVER raises
past its boundary. NO new error class (D10) — reuses ``mce_config``'s STRING family constants
(``_MCE_PARAM`` / ``_MCE_CONFIG``) imported from the sibling module so the lying-bool/range
discipline stays ONE shape (L30; a one-way ``mce_reset -> mce_config`` underscore-import, no
cycle). ``_mce_cells`` is imported as ``_mc`` (``number_of_configurations`` /
``current_configuration``); ``_variable_inventory`` from ``_optimize_common`` (read-only use).

Live ZOS-API integration; unit-tested against fixture-seeded fakes
(``FakeMCE`` + the ``*NoOp`` lying-bool variants).
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _mce_cells as _mc
from ._analysis_common import error_envelope
# Sibling underscore-import (one-way: mce_reset -> mce_config, never the reverse — no cycle):
# the lying-bool / range / never-raise discipline stays ONE shape (L30).
from .mce_config import _MCE_CONFIG, _MCE_PARAM, _fail, _require_dict, _require_int


# =========================================================================== #
# Shared: the MCE-filtered DOF disclosure (DISCLOSE, do NOT over-clear).
# =========================================================================== #
def _count_mce_dofs(system, config=None):
    """The per-config MCE-source variable DOF count (DISCLOSE-only; READ-only, never clears).

    Reads ``_variable_inventory(system)`` and counts ``source=="mce"`` items (the per-config
    optimizer variables the config collapse / delete will remove as a STRUCTURAL consequence
    of the row-drop). The LDE/asphere items are LEFT untouched (not counted, not Fixed) — they
    survive a reset/delete as live DOFs (no over-clear).

    ``config`` (optional): when given, count ONLY the items whose ``config`` matches (the
    deleted config's exact per-config DOFs); ``deleted_config_dofs_attribution`` is then
    ``"exact"``. When the inventory item lacks a resolvable per-config index (it always
    carries ``config`` for the MCE source — but fail-closed if the attribution is missing),
    the caller falls back to the TOTAL mce count + ``"approximate"``.

    Returns ``(count:int|None, attribution:str)``:
    - ``("exact"/"all")`` count on a clean enumeration;
    - ``(None, "incomplete")`` on a GUARDED enumeration fault (``_variable_inventory`` never
      raises, but a defensive try/except degrades to ``None`` + the caller stamps the
      ``dof_enumeration_incomplete`` flag). The reset/delete is the dominant intent — the
      disclosure is best-effort, NEVER aborts the operation.
    """
    try:
        from ._optimize_common import _variable_inventory
        inv = _variable_inventory(system)
    except Exception:  # noqa: BLE001 — a disclosure read must never break the collapse
        return (None, "incomplete")
    try:
        mce_items = [it for it in inv if it.get("source") == "mce"]
        if config is None:
            return (len(mce_items), "all")
        # Exact per-config attribution: every MCE inventory item carries ``config`` (the
        # _enumerate_mce_variables shape). Count the items on the deleted config only.
        attributable = [it for it in mce_items if it.get("config") is not None]
        if len(attributable) == len(mce_items):
            return (sum(1 for it in mce_items if it.get("config") == config), "exact")
        # A degraded item lacks a per-config index -> do NOT fabricate; disclose the total.
        return (len(mce_items), "approximate")
    except Exception:  # noqa: BLE001 — a malformed inventory degrades to incomplete
        return (None, "incomplete")


# =========================================================================== #
# TOOL 1 — remove_configuration(config)
# =========================================================================== #
def remove_configuration(session, params):
    """Delete ONE MCE configuration, read-back-proven via the count DECREMENT (§Tool 1).

    Params: ``config`` (int, REQUIRED — the 1-based config index to delete).

    Refuses (PRE-mutation, ZERO mutation): a non-integral ``config`` (``mce_param``); the
    last/only config when ``NumberOfConfigurations <= 1`` (``mce_config``); an out-of-range
    index (``mce_config``). The delete is ``MCE.DeleteConfiguration(config)`` whose return
    bool LIES (F4/D14) — the proof is ``NumberOfConfigurations`` decrementing by exactly 1
    (a no-op count -> ``mce_config`` refusal). Discloses the engine-owned active-index shift
    (read back) + ``mce_rows_preserved:true`` (the operand ROWS survive) + ``deleted_config_dofs``
    (the deleted config's per-config MCE DOFs, SURFACED not cleared). NEVER raises.
    """
    params = _require_dict(params)
    try:
        return _remove_configuration_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("remove_configuration", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "remove_configuration", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_config (L26)
        return error_envelope(
            "remove_configuration", _MCE_CONFIG,
            f"unexpected engine fault deleting a configuration ({exc!r}); refusing rather "
            "than claiming an unverified delete",
        )


def _remove_configuration_impl(session, params):
    system = session.system

    config = _require_int(params.get("config"), "config")

    n_before = _mc.number_of_configurations(system)
    # The last/only-config floor (load-bearing): DeleteConfiguration on a 1-config system
    # would leave a 0-config corpse -> refuse PRE-mutation, steer to reset_to_single_config.
    if n_before <= 1:
        raise _fail(
            _MCE_CONFIG,
            f"system has {n_before} configuration(s); cannot delete the last/only config "
            "(a system must keep >=1) — it is already single-config. Use "
            "reset_to_single_config to collapse a multi-config design to one config.",
            field="number_of_configurations", intended=None, actual=n_before,
        )
    # Out-of-range -> mce_config, ZERO mutation.
    if not (1 <= config <= n_before):
        raise _fail(
            _MCE_CONFIG,
            f"config {config} out of range; valid 1..{n_before} "
            f"(NumberOfConfigurations={n_before})",
            field="config", intended=config, actual=n_before,
        )

    current_before = _mc.current_configuration(system)
    # The DOF disclosure read — BEFORE the delete (else the row's per-config column is gone
    # and the inventory undercounts). READ-ONLY: never clears.
    deleted_dofs, attribution = _count_mce_dofs(system, config=config)

    # DeleteConfiguration returns a LYING bool (F4/D14) — the count decrement is the proof.
    system.MCE.DeleteConfiguration(config)
    n_after = _mc.number_of_configurations(system)
    if n_after != n_before - 1:
        raise _fail(
            _MCE_CONFIG,
            f"DeleteConfiguration({config}) did not decrement the count "
            f"({n_before} -> {n_after}); silent no-op — refusing rather than claiming an "
            "unverified delete (the engine's return bool is not a reliable proof, F4/D14)",
            field="number_of_configurations", intended=n_before - 1, actual=n_after,
        )

    current_after = _mc.current_configuration(system)

    result = {
        "ok": True,
        "tool": "remove_configuration",
        "deleted": config,
        "configs_before": n_before,
        "configs_after": n_after,
        "number_of_configurations": n_after,
        "current_before": current_before,
        "current_after": current_after,
        "active_config_shifted": current_after != current_before,
        "mce_rows_preserved": True,
        "deleted_config_dofs": deleted_dofs,
        "deleted_config_dofs_attribution": attribution,
    }
    if deleted_dofs is None:
        result["dof_enumeration_incomplete"] = True
    return result


# =========================================================================== #
# TOOL 2 — reset_to_single_config()
# =========================================================================== #
def reset_to_single_config(session, params):
    """Collapse to a single configuration, count-1 read-back-proven (§Tool 2).

    No required params. Idempotent on an already-single-config system (NO engine call —
    ``already_single:true``). Otherwise calls the micro-probe-blessed primitive
    ``MCE.MakeSingleConfiguration()`` (it BAKES the ACTIVE config, drives the count to 1, AND
    clears the MCE rows to the 1-row floor — live-proven 2026-06-26). The count-1 read-back is
    the proof (NOT the lying bool). Discloses ``surviving_config`` (the active-config index
    read BEFORE the collapse), ``mce_rows_cleared`` / ``operands_before``/``after`` (the
    ``NumberOfOperands`` read-back), ``n_per_config_dofs_removed`` (the MCE-filtered inventory
    count — DISCLOSE-only, NOT cleared), and ``apply_lens_spec_unblocked``. NEVER raises.
    """
    params = _require_dict(params)
    try:
        return _reset_to_single_config_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("reset_to_single_config", _MCE_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "reset_to_single_config", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> mce_config (L26)
        return error_envelope(
            "reset_to_single_config", _MCE_CONFIG,
            f"unexpected engine fault resetting configurations ({exc!r}); refusing rather "
            "than claiming an unverified reset",
        )


# The micro-probe-blessed reset primitive (2026-06-26): MakeSingleConfiguration BAKES the
# ACTIVE config into the single survivor (surface-2 THIC 22 for active config 2, NOT config
# 1's 11), drives NumberOfConfigurations -> 1, AND clears the MCE rows to the 1-row floor.
# DeleteAllConfigurations is the proven fallback (config-1 survivor) on ANY surprise — but the
# probe blessed MakeSingleConfiguration as strictly more intent-faithful, so it is the bound
# primitive. ``surviving_config`` is the active-config index (read BEFORE the collapse).
_RESET_MECHANISM = "MakeSingleConfiguration"


def _reset_collapse(system):
    """Drive the blessed reset primitive. The LYING bool is IGNORED — the caller
    read-back-proves ``NumberOfConfigurations == 1``."""
    system.MCE.MakeSingleConfiguration()


def _reset_to_single_config_impl(session, params):
    system = session.system

    n_before = _mc.number_of_configurations(system)
    current_before = _mc.current_configuration(system)

    # A sub-1 count is STRUCTURALLY IMPOSSIBLE on a real engine (a system always keeps >=1
    # config), so a 0/negative read is a CORRUPT/unreadable count — NEVER fabricate a clean
    # already_single envelope over it (the roadmap charter: never report a clean result over
    # a corrupt/impossible read). Refuse PRE-collapse, ZERO mutation.
    if n_before < 1:
        raise _fail(
            _MCE_CONFIG,
            f"unexpected configuration count {n_before}; a system must keep >=1 config "
            "(a sub-1 count is a corrupt/unreadable read) — refusing rather than claiming "
            "an unverified reset",
            field="number_of_configurations", intended=None, actual=n_before,
        )
    # Idempotent already-single: NO _reset_collapse engine call (don't perturb a
    # clean single-config system — a defensive pre-rebuild call is a safe no-op).
    if n_before == 1:
        operands = _safe_nops(system)
        return {
            "ok": True,
            "tool": "reset_to_single_config",
            "mechanism": _RESET_MECHANISM,
            "already_single": True,
            "configs_before": 1,
            "configs_after": 1,
            "number_of_configurations": 1,
            "current_before": current_before,
            "current_after": current_before,
            "surviving_config": 1,
            "mce_rows_cleared": False,
            "operands_before": operands,
            "operands_after": operands,
            "n_per_config_dofs_removed": 0,
            "apply_lens_spec_unblocked": True,
        }

    nops_before = _safe_nops(system)
    # surviving_config is the ACTIVE config index, read BEFORE the collapse (the blessed
    # MakeSingleConfiguration bakes the active config — the agent always knows which survived).
    surviving_config = current_before

    # The DISCLOSURE read — BEFORE the collapse (else the rows are gone, the inventory
    # undercounts). READ-ONLY: never clears (the LDE/asphere DOFs survive untouched).
    n_mce, _attribution = _count_mce_dofs(system, config=None)

    # The blessed primitive; the LYING bool is IGNORED — the count-1 read-back is the proof.
    _reset_collapse(system)
    n_after = _mc.number_of_configurations(system)
    if n_after != 1:
        raise _fail(
            _MCE_CONFIG,
            f"reset_to_single_config did not collapse to 1 config "
            f"({n_before} -> {n_after}); silent no-op — refusing rather than claiming an "
            "unverified reset (the engine's return bool is not a reliable proof, F4/D14)",
            field="number_of_configurations", intended=1, actual=n_after,
        )

    nops_after = _safe_nops(system)
    current_after = _mc.current_configuration(system)
    # mce_rows_cleared driven by the NumberOfOperands read-back, NEVER assumed (if the blessed
    # primitive left a benign 1-row floor the disclosure stays honest — probe: nops 3->1).
    mce_rows_cleared = (
        isinstance(nops_before, int) and isinstance(nops_after, int)
        and nops_after < nops_before
    )

    result = {
        "ok": True,
        "tool": "reset_to_single_config",
        "mechanism": _RESET_MECHANISM,
        "already_single": False,
        "configs_before": n_before,
        "configs_after": n_after,
        "number_of_configurations": n_after,
        "current_before": current_before,
        "current_after": current_after,
        "surviving_config": surviving_config,
        "mce_rows_cleared": mce_rows_cleared,
        "operands_before": nops_before,
        "operands_after": nops_after,
        "n_per_config_dofs_removed": n_mce,
        "apply_lens_spec_unblocked": True,
        "note": (
            f"the per-config MCE authoring was collapsed by the reset (operands "
            f"{nops_before} -> {nops_after}); re-author with set_config_operand / "
            "set_config_value to rebuild a multi-config design. The surviving design is the "
            f"config that was active before the reset (config {surviving_config}, baked into "
            "the LDE by MakeSingleConfiguration)."
        ),
    }
    if n_mce is None:
        result["dof_enumeration_incomplete"] = True
    return result


def _safe_nops(system):
    """``system.MCE.NumberOfOperands`` -> int, THROW-guarded -> None (a disclosure read).

    A failed read degrades the row-count disclosure to ``None`` (and ``mce_rows_cleared``
    falls to False since it requires two int reads) rather than aborting the reset — the
    collapse is the dominant intent, the row-count is best-effort.
    """
    try:
        return int(system.MCE.NumberOfOperands)
    except Exception:  # noqa: BLE001 — a disclosure read must never break the reset
        return None


# =========================================================================== #
# ToolSpec registration (§Module + Registration).
# =========================================================================== #
REMOVE_CONFIGURATION_SPEC = ToolSpec(
    name="remove_configuration",
    handler=remove_configuration,
    required_params=("config",),
    param_types={"config": "number"},
    description=(
        "Delete ONE multi-config configuration (1-based) — proves the delete by the "
        "configuration count DECREMENTING, NOT the engine's return flag. The operand ROWS "
        "survive (only the deleted config's COLUMN is removed); the active config index may "
        "SHIFT (delete below current shifts it down, delete at current clamps to the new "
        "last) — inspect current_after / active_config_shifted. Refuses the last/only config "
        "(use reset_to_single_config to go to one config) and an out-of-range index with "
        "ZERO mutation. Gotcha: when removing several, delete the HIGHEST index first to "
        "avoid the shift bookkeeping. See reset_to_single_config, describe_configurations."
    ),
)

RESET_TO_SINGLE_CONFIG_SPEC = ToolSpec(
    name="reset_to_single_config",
    handler=reset_to_single_config,
    required_params=(),
    param_types={},
    description=(
        "Collapse a multi-config (zoom) design to a SINGLE configuration — bakes the "
        "currently-active config's geometry into the design, clears the per-config MCE rows, "
        "and UNBLOCKS read_lens_spec / apply_lens_spec (which refuse a multi-config system). "
        "Proves the collapse by the configuration count reading 1, NOT the engine's return "
        "flag. Idempotent on an already-single-config system (no-op). Discloses "
        "surviving_config (the config that was active — its values survive), mce_rows_cleared, "
        "and apply_lens_spec_unblocked. Gotcha: this CLEARS the per-config authoring — "
        "re-author with set_config_operand / set_config_value to rebuild. Use this instead of "
        "loading an unrelated blank file. See remove_configuration, describe_configurations."
    ),
)

TOOL_SPECS = (REMOVE_CONFIGURATION_SPEC, RESET_TO_SINGLE_CONFIG_SPEC)
