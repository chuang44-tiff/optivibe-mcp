"""tools/_config_common.py — the config-aware CROSS-CUT substrate.

NOT dispatchable (no ``TOOL_SPEC``). The shared, probe-grounded primitives the
config-aware acceptance graders reuse so the ``config=None|int|"all"`` routing +
the switch-restore-verify discipline + the coverage reconcile live in EXACTLY one
place (the ``_measurement_common.with_best_focus`` structural twin):

- ``resolve_config_selector(system, config)`` — the UNIFORM
  ``config=None|int|"all"`` contract -> ``(mode, configs, n_configs)``. Raises
  ``ToolParamError`` (-> ``config_param``) on a bool / a non-int-non-"all" / an
  out-of-range int / any string != EXACTLY ``"all"``. An integral float (``2.0``) is
  accepted + coerced (the JSON-round-trip rule).
- ``with_configuration(system, cfg)`` — the save -> switch -> yield -> restore ->
  VERIFY context manager (the ``with_best_focus`` shape, probe Q11). ALWAYS restores
  in ``finally`` + post-restore verifies; a restore mismatch ATTACHES a
  ``mutation_warning`` (NEVER raises past ``finally`` — a finally-raise masks the
  body result).
- ``evaluate_over_configs(session, config, grader_fn)`` — the shared sweep driver
  every grader calls. Owns the current/single/all routing + the coverage reconcile so
  the six graders share ONE contract. NEVER raises.
- ``reconcile_visited(visited, n_configs)`` — INVARIANT-2 (the tolerance-v2 reconcile
  ancestor): ``visited == {1..N}`` or NOT ok.

The switch lever REUSES ``system.MCE.SetCurrentConfiguration(cfg)`` + the
``_mce_cells.current_configuration`` read-back (the EXACT proof
``mce_config.set_current_configuration`` uses) — the cell helpers are imported, NOT the
``mce_config`` dispatchable handler (no dispatch coupling). Every family is a STRING
constant attached via ``error_envelope`` (the ``mce_config`` precedent — NO new
error class).

Live ZOS-API integration: exercised by a live integration test; unit-tested against
the config-bite fakes whose ``SetCurrentConfiguration`` makes the
downstream reads COMPUTE from the active config (a silent no-op switch reddens).
"""
import math
from contextlib import contextmanager

from ..errors import ToolParamError
from . import _mce_cells as _mc


# --------------------------------------------------------------------------- #
# Family / warn STRING constants (attached via error_envelope — the
# mce_config precedent). NO new error class.
# --------------------------------------------------------------------------- #
_CONFIG_PARAM = "config_param"              # bad config value (non-int, out of range, bad "all")
_CONFIG_RESTORE = "config_restore"          # the post-call restore did NOT verify (a loud WARN, never a raise)
_CONFIG_COVERAGE = "config_coverage"        # INVARIANT-2 on an "all" sweep: visited != {1..N}
_CONFIG_NO_VARIATION = "config_no_variation"  # the "all" sweep returned one repeated headline value (WARN)

# The one spelling of the all-config sweep selector (no "All"/"ALL" silent accept).
_ALL = "all"


# --------------------------------------------------------------------------- #
# Guarded config-count reader (the build_merit / preflight "1 on fresh" contract).
# --------------------------------------------------------------------------- #
def safe_number_of_configurations(system):
    """``NumberOfConfigurations`` THROW-GUARDED -> 1 on a fresh / non-MCE / wedged system.

    ``_mce_cells.number_of_configurations`` RAISES on a read fault (the strict author-path
    discriminator). The S3 cross-cut reads the count on EVERY grader / merit build (a
    non-MCE backend, a fresh system, or a transient wedge must NOT crash a read tool), so
    this guarded wrapper degrades any throw to ``1`` (the single-config default — a system
    with no MCE has exactly one configuration). Returns an int >= 1.
    """
    try:
        n = _mc.number_of_configurations(system)
    except Exception:  # noqa: BLE001 — a non-MCE / wedged read degrades to 1 config
        return 1
    try:
        n = int(n)
    except Exception:  # noqa: BLE001 — a non-int count degrades to 1
        return 1
    return n if n >= 1 else 1


def safe_current_configuration(system):
    """``CurrentConfiguration`` THROW-GUARDED -> 1 on a fresh / non-MCE / wedged system.

    The disclosure-only active-config read (persistence + the grader echo). A read fault
    degrades to ``1`` (the single-config default). Returns an int >= 1.
    """
    try:
        c = _mc.current_configuration(system)
    except Exception:  # noqa: BLE001 — a non-MCE / wedged read degrades to 1
        return 1
    try:
        c = int(c)
    except Exception:  # noqa: BLE001 — a non-int read degrades to 1
        return 1
    return c if c >= 1 else 1


# --------------------------------------------------------------------------- #
# resolve_config_selector — the uniform config contract.
# --------------------------------------------------------------------------- #
def _coerce_config_int(config):
    """Coerce a ``config`` selector to an exact int; reject bool / non-integral.

    Accepts an exact ``int`` OR an integral ``float`` (``2.0`` -> 2 — the
    JSON-round-trip rule); a bool (an int subclass — a client miswrite) and a
    non-integral / non-finite float are REJECTED. Returns the int or raises
    ``ToolParamError``.
    """
    if isinstance(config, bool):
        raise ToolParamError(
            f"config must be an integer config index or {_ALL!r}, not a bool ({config!r})"
        )
    if isinstance(config, int):
        return int(config)
    if isinstance(config, float):
        if math.isfinite(config) and config == int(config):
            return int(config)
        raise ToolParamError(
            f"config must be an integer config index, got non-integral float {config!r}"
        )
    raise ToolParamError(
        f"config must be an integer config index or {_ALL!r}, got "
        f"{type(config).__name__} {config!r}"
    )


def resolve_config_selector(system, config):
    """The UNIFORM ``config=None|int|"all"`` contract -> ``(mode, configs, n_configs)``.

    - ``config is None``       -> ``("current", [current_config], n)`` — byte-identical
      default; NO switch.
    - ``config is int (1..N)`` -> ``("single",  [config],         n)``.
    - ``config == "all"``      -> ``("all",     [1..N],           n)``.

    Raises ``ToolParamError`` (-> ``config_param`` at the grader's never-raise wrapper)
    on: a bool, a non-int / non-``"all"`` type, an out-of-range int (``config < 1`` or
    ``> N``), or any string != EXACTLY ``"all"`` (no ``"All"``/``"ALL"`` silent accept —
    ONE spelling). An integral float (``2.0``) is ACCEPTED + coerced; a bool is REJECTED.
    ``n_configs`` is read via ``safe_number_of_configurations`` (THROW-guarded; 1 on a
    fresh system). A 1-config system + ``config="all"`` -> ``("all", [1], 1)`` (valid; no
    spurious refusal) — so a grader called with no config on a single-config system is
    byte-identical to today.
    """
    n = safe_number_of_configurations(system)

    if config is None:
        return ("current", [safe_current_configuration(system)], n)

    # The "all" sweep — EXACTLY this spelling (a non-"all" string is a param error, NOT a
    # silent default). Check str BEFORE the int coerce so "all" never hits the int path.
    if isinstance(config, str):
        if config == _ALL:
            return (_ALL, list(range(1, n + 1)), n)
        raise ToolParamError(
            f"config string must be exactly {_ALL!r} (the all-config sweep), got "
            f"{config!r}; no case-variant is accepted"
        )

    cfg = _coerce_config_int(config)
    if not (1 <= cfg <= n):
        raise ToolParamError(
            f"config {cfg} out of range; valid 1..{n} (NumberOfConfigurations={n})"
        )
    return ("single", [cfg], n)


# --------------------------------------------------------------------------- #
# resolve_single_config_selector — the SINGLE-config (None|int, NO "all") contract.
# --------------------------------------------------------------------------- #
def resolve_single_config_selector(system, config, tool_name):
    """Resolve a SINGLE-config selector (``None`` | int) — ``config="all"`` is REFUSED.

    The shared helper for the heavy-analysis / single-figure tools (``get_mtf`` /
    ``get_spot`` / ``analyze_axial_color`` / ``render_layout`` / ``describe_surfaces``)
    that offer ``config=None|int`` but NOT the ``"all"`` sweep (the render-obscured-slow
    heavy-analysis class on the single serialized seat; a single figure / table shows ONE
    config). Returns the int config to read at (``None`` -> the active config, NO switch),
    or RAISES ``ToolParamError`` (the dispatch envelope / the grader's never-raise wrapper
    nets it to the tool's param family) on ``"all"`` (even a 1-config system's "all" that
    ``resolve_config_selector`` would resolve to mode ``"all"``) OR a bad value (bad string /
    out-of-range int / bool / container — delegated to ``resolve_config_selector``).

    ``tool_name`` is interpolated into the ``"all"``-refusal message so the agent is told
    which tool to loop ``set_current_configuration`` on for a per-all-config read.
    """
    if config is None:
        return None
    if isinstance(config, str) and config == _ALL:
        raise ToolParamError(
            f"{tool_name} does not support config='all' (per-config sweep is the "
            "render-obscured-slow heavy-analysis / single-figure class); pass a single "
            "integer config index k, or loop set_current_configuration(k)"
        )
    # Reuse the shared resolver for the int/range/bool/bad-string validation. A
    # non-"all" string / out-of-range int / bool -> ToolParamError (re-raised).
    mode, configs, _n = resolve_config_selector(system, config)
    if mode == _ALL:  # a 1-config "all" can resolve to mode "all" — also refuse.
        raise ToolParamError(
            f"{tool_name} does not support config='all'; pass a single integer config "
            "index k"
        )
    return configs[0]


# --------------------------------------------------------------------------- #
# with_configuration — save -> switch -> yield -> restore -> VERIFY (Q11).
# --------------------------------------------------------------------------- #
def _switch_configuration(system, cfg):
    """``SetCurrentConfiguration(cfg)`` + read-back-prove ``CurrentConfiguration == cfg``.

    REUSES ``mce_config.set_current_configuration``'s exact proof (the live re-evaluating
    switch + the ``_mce_cells.current_configuration`` read-back). Returns ``True`` iff the
    switch took (a silent no-op reads back the wrong config -> ``False``). A switch / read
    THROW degrades to ``False`` (never raises — the caller discloses).
    """
    try:
        system.MCE.SetCurrentConfiguration(cfg)
    except Exception:  # noqa: BLE001 — a switch throw -> not switched, never raise
        return False
    try:
        current = _mc.current_configuration(system)
    except Exception:  # noqa: BLE001 — an unreadable read-back -> treat as not switched
        return False
    return current == cfg


@contextmanager
def with_configuration(system, cfg):
    """Save -> switch -> yield -> restore -> VERIFY the active config (the with_best_focus shape, Q11).

    1. SAVE ``current = _mce_cells.current_configuration(system)`` (THROW-guarded).
    2. If ``cfg != current``: ``SetCurrentConfiguration(cfg)`` + read-back-prove
       ``== cfg`` (REUSE the S1 ``set_current_configuration`` read-back; a no-op switch is
       the S1 lever's own guard).
    3. yield a MUTABLE dict ``{"config": cfg, "switched": bool, "restore_verified": True,
       "mutation_warning": None}``.
    4. finally: ``SetCurrentConfiguration(current)``; POST-RESTORE re-read; on a mismatch
       set ``restore_verified=False`` + a loud ``mutation_warning``. NEVER raises past
       ``finally`` (a finally-raise masks the body result — the ``with_best_focus``
       precedent). A switch THROW on enter degrades to ``switched=False`` + a
       ``mutation_warning`` (the caller reads at whatever the active index is + DISCLOSES).

    Q11 nesting (required order): ``analyze_strehl``/``analyze_wavefront`` nest
    ``with_best_focus`` INSIDE ``with_configuration`` — ``with_configuration(k)`` OUTER
    (switch config), ``with_best_focus`` INNER (scan that config's focus). Both restore in
    ``finally``; OUTER restores last.
    """
    state = {
        "config": cfg,
        "switched": False,
        "restore_verified": True,
        "mutation_warning": None,
    }

    saved = safe_current_configuration(system)

    if cfg != saved:
        switched = _switch_configuration(system, cfg)
        state["switched"] = switched
        if not switched:
            state["mutation_warning"] = (
                f"could not switch to config {cfg} (SetCurrentConfiguration did not "
                f"read back); reading at the active config {saved} instead"
            )
    # If cfg == saved the active config is already the target — no switch, switched stays
    # False (no switch was needed), the grade reads the active (correct) config.

    try:
        yield state
    finally:
        # RESTORE (always): switch back to the saved active config + post-restore verify.
        # NEVER raises past finally (the with_best_focus precedent — a finally-raise masks
        # the body result the grader already computed).
        try:
            system.MCE.SetCurrentConfiguration(saved)
        except Exception:  # noqa: BLE001 — a restore throw is surfaced as a mutation warning
            pass
        try:
            restored = _mc.current_configuration(system)
        except Exception:  # noqa: BLE001 — an unreadable read-back -> treat as unverified
            restored = None
        if restored != saved:
            state["restore_verified"] = False
            existing = state.get("mutation_warning")
            note = (
                f"config restore did NOT verify (intended active config {saved}, re-read "
                f"{restored!r}); the active configuration may be left at the wrong index "
                "— downstream numbers must not be trusted silently"
            )
            state["mutation_warning"] = (
                f"{existing}; {note}" if existing else note
            )


# --------------------------------------------------------------------------- #
# reconcile_visited — INVARIANT-2 over an "all" sweep.
# --------------------------------------------------------------------------- #
def reconcile_visited(visited, n_configs):
    """INVARIANT-2 (the tolerance-v2 reconcile ancestor): ``visited == {1..N}`` or NOT ok.

    Returns ``{"ok": bool, "visited": sorted(visited), "expected": [1..N],
    "missing": [...]}``. ``ok:true`` => ``missing == []`` (an ``"all"`` claim that
    silently skipped a config is DISCLOSED, never a clean partial). A config the sweep
    COULD NOT switch to OR whose grader returned ``ok:false`` is in ``missing``.
    """
    visited_set = set(visited)
    expected = list(range(1, n_configs + 1))
    missing = [k for k in expected if k not in visited_set]
    return {
        "ok": not missing,
        "visited": sorted(visited_set),
        "expected": expected,
        "missing": missing,
    }


# --------------------------------------------------------------------------- #
# evaluate_over_configs — the shared sweep driver.
# --------------------------------------------------------------------------- #
def _grade_safe(grader_fn, session):
    """Run ``grader_fn(session)``; a throw becomes a structured ``{ok:false}`` (never raises).

    The driver must NEVER let a per-config grader throw abort the sweep (the active config
    is restored by the surrounding ``with_configuration`` ``finally``). A throw is recorded
    as that config's ``ok:false`` entry so ``reconcile_visited`` puts it in ``missing``.
    """
    try:
        result = grader_fn(session)
    except Exception as exc:  # noqa: BLE001 — a grader throw -> ok:false, sweep continues
        return {"ok": False, "error": f"{exc!r}", "error_family": "config_grade"}
    if not isinstance(result, dict):
        return {"ok": False, "error": f"grader returned {type(result).__name__}"}
    return result


def _headline_value(result):
    """Best-effort scalar HEADLINE of a grader result for the ``config_differs`` check.

    The per-config divergence signal: the grader's headline scalar must DIFFER per
    config on a real sweep. The headline is operand-specific (EFL for first-order, wave-1
    STRH for Strehl, on-axis RWCE for wavefront, min edge clearance for clearance), so the
    driver reads a STABLE, JSON-comparable fingerprint of the result rather than guessing a
    single field: the grader attaches ``config_headline`` when it has a natural scalar; else
    the driver falls back to the whole ``headline`` dict (still comparable for "all
    byte-identical"). Returns a comparable value (or ``None`` when nothing is comparable).
    """
    if not isinstance(result, dict):
        return None
    if "config_headline" in result:
        return result["config_headline"]
    if "headline" in result:
        return result["headline"]
    return None


def evaluate_over_configs(session, config, grader_fn):
    """The shared "all-config sweep" driver every grader calls. NEVER raises.

    ``system = session.system``; ``(mode, configs, n) = resolve_config_selector(system,
    config)``.

    - ``mode == "current"``: run ``grader_fn(session)`` DIRECT (no switch) at the active
      config -> the grader's result dict + ``config_evaluated`` (the active config).
    - ``mode == "single"``:  run ``grader_fn(session)`` INSIDE ``with_configuration(system,
      cfg)`` -> the grader's result dict + ``config_evaluated == cfg``.
    - ``mode == "all"``:     loop ``k in 1..N`` INSIDE ``with_configuration(system, k)``,
      collect ``grader_fn(session)`` per config into ``per_config``; ``visited`` = the set
      of k whose grader returned ``ok:true`` AND whose ``with_configuration`` switched (or
      was already active); ``reconcile_visited(visited, n)``. Returns the sweep shape
      (``config_evaluated:"all"``, ``n_configs``, ``per_config``, ``coverage``,
      ``config_differs``, + the ``config_no_variation`` WARN + the ``config_coverage`` flag).

    ``grader_fn(session)`` is the tool's EXISTING pure per-config body, returning its normal
    result dict. A ``ToolParamError`` from ``resolve_config_selector`` (a bad ``config``) is
    re-raised so the grader's own ``@_never_raise`` wrapper nets it to the tool's param
    family. A grader_fn throw mid-sweep is caught -> that config's ``per_config`` entry
    carries ``ok:false`` (the sweep continues; ``reconcile_visited`` puts it in ``missing``).
    The active config is ALWAYS restored.
    """
    system = session.system
    # A bad config selector is a PARAM error — re-raise so the grader's @_never_raise
    # wrapper maps it to the tool's own param family (NO new family for the selector).
    mode, configs, n = resolve_config_selector(system, config)

    if mode == "current":
        result = grader_fn(session)
        if isinstance(result, dict):
            result.setdefault("config_evaluated", configs[0])
        return result

    if mode == "single":
        cfg = configs[0]
        with with_configuration(system, cfg) as ctx:
            result = grader_fn(session)
        if isinstance(result, dict):
            result.setdefault("config_evaluated", cfg)
            if not ctx["restore_verified"]:
                result["mutation_warning"] = ctx["mutation_warning"]
            if not ctx["switched"] and ctx["mutation_warning"]:
                # The switch did not take (a read-back miss / throw) — disclose it.
                result.setdefault("config_switch_warning", ctx["mutation_warning"])
        return result

    # mode == "all": loop 1..N inside with_configuration, collecting per-config results.
    per_config = []
    visited = set()
    headlines = []
    restore_warnings = []
    for k in configs:
        with with_configuration(system, k) as ctx:
            graded = _grade_safe(grader_fn, session)
        graded_ok = bool(graded.get("ok", False))
        # A config counts as VISITED only when the grader succeeded AND the config was the
        # active one for the grade (switched, OR already-active i.e. k == the saved config
        # with no switch needed). ctx["switched"] is False when no switch was needed (k was
        # already active) — distinguish that benign case from a failed switch via the
        # mutation_warning (a failed switch attaches one).
        switch_failed = (not ctx["switched"]) and bool(ctx["mutation_warning"])
        if graded_ok and not switch_failed:
            visited.add(k)
        if not ctx["restore_verified"] and ctx["mutation_warning"]:
            restore_warnings.append(ctx["mutation_warning"])
        entry = {"config": k}
        entry.update(graded)
        per_config.append(entry)
        headlines.append(_headline_value(graded) if graded_ok else None)

    coverage = reconcile_visited(visited, n)
    # config_differs: the per-config HEADLINE values are NOT all byte-identical. Only
    # the comparable (non-None) headlines participate; a single comparable value is "not
    # differing". A multi-config sweep returning one repeated value is the silent-wrong
    # signature of a switch that did not bite.
    comparable = [h for h in headlines if h is not None]
    config_differs = len(comparable) > 1 and any(
        h != comparable[0] for h in comparable[1:]
    )

    result = {
        "ok": True,
        "config_evaluated": _ALL,
        "n_configs": n,
        "per_config": per_config,
        "coverage": coverage,
        "config_differs": config_differs,
    }
    warnings = []
    if n > 1 and not config_differs:
        warnings.append(
            f"the {_ALL!r}-config sweep returned one repeated headline value across "
            f"{n} configs (config_differs=False); a SetCurrentConfiguration that did not "
            "bite is the silent-wrong signature — verify the configs genuinely differ"
        )
        result["config_no_variation"] = True
    if not coverage["ok"]:
        warnings.append(
            f"the {_ALL!r}-config sweep did not cover every config "
            f"(missing {coverage['missing']}); a config could not be switched to or its "
            "grade failed — the per-config vector is incomplete"
        )
        result["config_coverage"] = True
    if restore_warnings:
        warnings.extend(restore_warnings)
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result


__all__ = [
    "resolve_config_selector",
    "resolve_single_config_selector",
    "with_configuration",
    "evaluate_over_configs",
    "reconcile_visited",
    "safe_number_of_configurations",
    "safe_current_configuration",
    "_CONFIG_PARAM",
    "_CONFIG_RESTORE",
    "_CONFIG_COVERAGE",
    "_CONFIG_NO_VARIATION",
]
