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
  the six graders share ONE contract. A bad selector (``ToolParamError``) or an
  unreadable count / entry index (``ConfigUnverifiedError`` / ``SessionClosedError``)
  PROPAGATES to the caller's wrapper; the grader body's own throws are caught per config.
- ``reconcile_visited(visited, n_configs)`` — INVARIANT-2 (the tolerance-v2 reconcile
  ancestor): ``visited == {1..N}`` or NOT ok.

The switch lever REUSES ``system.MCE.SetCurrentConfiguration(cfg)`` + the
``_mce_cells.current_configuration`` read-back (the EXACT proof
``mce_config.set_current_configuration`` uses) — the cell helpers are imported, NOT the
``mce_config`` dispatchable handler (no dispatch coupling). Every family is a STRING
constant attached via ``error_envelope`` (the D10 / ``mce_config`` precedent); the one exception is
``ConfigUnverifiedError`` (``config_unverified``), for a count / entry index that could
not be READ — deliberately not a param error, because the fault is not the caller's.

Live ZOS-API integration: exercised by a live integration test; unit-tested against
the config-bite fakes whose ``SetCurrentConfiguration`` makes the
downstream reads COMPUTE from the active config (a silent no-op switch reddens).
"""
from contextlib import contextmanager

from .. import _io
from ..errors import (
    ConfigUnverifiedError,
    SessionClosedError,
    ToolParamError,
    map_dotnet_exception,
)
from . import _error_note as _en
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

# A config index is a 1-based MCE column: no real system has 10**9 of them, so an int whose
# magnitude exceeds this is refused with only its bit length rendered (a 10**5000 selector
# would otherwise be formatted into the message — the int->str digit limit raises there).
_CONFIG_INT_BOUND = 10 ** 9


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
# Strict config readers (the READ-OR-REFUSE twins of the safe_* guards above).
# --------------------------------------------------------------------------- #
def _render_config_value(value, type_name=True):
    """Render a caller-supplied ``value`` for a refusal message. NEVER runs caller code, <= 80 chars.

    ONE locus for every value rendered into a config refusal (S-5). Round 2 (re-verify, the
    terminating criterion): nothing here may DISPATCH to the value's own code, because a
    hostile ``__class__`` / ``__repr__`` / metaclass ``__name__`` can raise a ``BaseException``
    that no ``except Exception`` stops. So:

    - the type comes from ``type(value)`` (never ``isinstance``, which consults ``__class__``);
    - its name from ``type.__dict__["__name__"]`` (the real slot, never a metaclass property);
    - an exact or subclassed ``str`` / ``int`` / ``float`` is reduced through its BASE slot
      (``str.__str__`` / ``int.__int__`` / ``float.__float__``) and rendered by the built-in repr,
      an int above the bound by its bit length only;
    - ANY other object is NOT repr'd at all: only its type name is shown.
    """
    try:
        kind = type(value)
        name = type.__dict__["__name__"].__get__(kind, type)
        if issubclass(kind, str):
            text = repr(str.__str__(value)[:60])
        elif kind is bool:
            text = "True" if value is True else "False"
        elif issubclass(kind, int):
            exact = int.__int__(value)
            text = (repr(exact) if abs(exact) <= _CONFIG_INT_BOUND
                    else f"<a {exact.bit_length()}-bit integer>")
        elif issubclass(kind, float):
            text = float.__repr__(float.__float__(value))
        else:
            text = "<value not rendered>"
        if type_name:
            text = f"{name} {text}"
        return text[:80]
    except Exception:  # noqa: BLE001 — defence in depth; no path above dispatches to caller code
        return "<unrenderable value>"


def _refuse_unreadable(exc, what):
    """Raise the right error for a FAILED config read. Always raises (call inside ``except``).

    Transport loss is not "unverified": a ``System.Runtime.Remoting.RemotingException``
    maps (``errors.map_dotnet_exception``) to ``SessionClosedError`` and is re-raised as
    such, and the harness's OWN ``SessionClosedError`` is re-raised as-is — the session is
    gone and the agent must be told that, not that a count was merely unreadable. Anything
    else is a ``ConfigUnverifiedError`` naming the attribute (``safe_exc``: the exception's
    own ``__str__`` may raise).
    """
    if isinstance(exc, SessionClosedError):
        raise exc
    mapped = map_dotnet_exception(exc)
    if isinstance(mapped, SessionClosedError):
        raise mapped from exc
    raise ConfigUnverifiedError(
        f"could not read MCE.{what} ({_io.safe_exc(exc, limit=300)}); refusing rather "
        "than substituting 1"
    ) from exc


def _strict_mce_int(system, attr, read):
    """``system.MCE.<attr>`` as an exact ``int >= 1``, or ``None`` when there is NO MCE.

    ABSENT vs UNREADABLE is the whole contract:
    - ABSENT — the ``system.MCE`` ACCESS itself raises ``AttributeError``, or returns
      ``None`` (a backend / fake with no MCE) — means "not applicable": return ``None`` and
      the caller treats the system as single-config. An ``AttributeError`` raised reading
      ``.<attr>`` on a PRESENT MCE is NOT absence: it is an unreadable read and refuses.
    - UNREADABLE — any other raise, a ``bool``, a non-convertible value, a non-integral
      float, or an int < 1 — is "unknown": ``ConfigUnverifiedError`` (transport loss
      becomes ``SessionClosedError``; see ``_refuse_unreadable``). Never a substituted 1.

    ``read`` is the one-line member accessor (``lambda mce: mce.NumberOfConfigurations``), not a
    ``getattr(mce, attr)``: the served-closure dynamic-dispatch census (C5) is a CLOSED list and
    a ``getattr`` on an engine object is exactly what it tripwires. ``attr`` is the display name.

    The value is coerced through the base slots (``int.__int__`` / ``float.__float__``) so
    a hostile numeric subclass cannot run caller code here.
    """
    try:
        mce = system.MCE
    except AttributeError:
        return None
    except Exception as exc:  # noqa: BLE001 — classified by _refuse_unreadable
        _refuse_unreadable(exc, attr)
    if mce is None:
        return None
    try:
        raw = read(mce)
    except Exception as exc:  # noqa: BLE001 — classified by _refuse_unreadable
        _refuse_unreadable(exc, attr)

    def _bad():
        return ConfigUnverifiedError(
            f"MCE.{attr} read back {_render_config_value(raw)}, which is not a configuration "
            "index >= 1; refusing rather than substituting 1"
        )

    if isinstance(raw, bool):
        raise _bad()
    try:
        if isinstance(raw, int):
            value = int.__int__(raw)
        elif isinstance(raw, float):
            as_float = float.__float__(raw)
            if not float.is_integer(as_float):  # False for nan / inf too
                raise _bad()
            value = int(as_float)
        else:
            value = int(raw)
    except ConfigUnverifiedError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unconvertible value is an unreadable one
        raise _bad() from exc
    if value < 1:
        raise _bad()
    return value


def strict_number_of_configurations(system):
    """``MCE.NumberOfConfigurations`` as an exact int >= 1, ``None`` if ABSENT, else RAISES.

    The READ-OR-REFUSE twin of ``safe_number_of_configurations`` (which stays
    byte-identical for its ~20 other callers): see ``_strict_mce_int``.
    """
    return _strict_mce_int(
        system, "NumberOfConfigurations", lambda mce: mce.NumberOfConfigurations
    )


def strict_current_configuration(system):
    """``MCE.CurrentConfiguration`` as an exact int >= 1, ``None`` if ABSENT, else RAISES.

    The upper bound ``<= NumberOfConfigurations`` is checked at the call site
    (``config_in_range``), where the strict count is in hand — not inside the reader.
    """
    return _strict_mce_int(
        system, "CurrentConfiguration", lambda mce: mce.CurrentConfiguration
    )


def _strict_count_or_one(system):
    """The strict count, with an ABSENT MCE read as the single-config system it is."""
    n = strict_number_of_configurations(system)
    return 1 if n is None else n


# --------------------------------------------------------------------------- #
# The ONE statement of "is this a 1-based configuration index of an N-config system".
# --------------------------------------------------------------------------- #
def config_in_range(k, n):
    """True iff ``k`` is a 1-based configuration index of a system with ``n`` configs.

    Both arguments must be EXACT built-in ``int`` (a ``bool``, a float, an ``int``
    subclass whose comparison operators are caller code, or ``None`` all read False) and
    ``n >= 1``. The native exporter's R-4 range check and the self path's configuration
    identity read share this ONE predicate
    (unvalidated), so the two cannot drift apart: an index outside ``1..N`` is corrupt by
    definition and is never drawn, captioned or echoed as the configuration evaluated.
    """
    return type(k) is int and type(n) is int and n >= 1 and 1 <= k <= n


# --------------------------------------------------------------------------- #
# resolve_config_selector — the uniform config contract.
# --------------------------------------------------------------------------- #
def _bounded_config_int(v):
    """``v`` (an exact int) if ``abs(v) <= _CONFIG_INT_BOUND``, else a bit-length-only refusal."""
    if abs(v) > _CONFIG_INT_BOUND:
        raise ToolParamError(
            f"config index is far outside any configuration count ({v.bit_length()}-bit "
            f"integer; |config| must be <= {_CONFIG_INT_BOUND})"
        )
    return v


def _refuse_below_one(cfg):
    """Refuse an exact-int ``cfg < 1`` as the CALLER'S mistake, before any count read.

    Round 2 (adversary): a configuration index below 1 is invalid for EVERY count, so it
    is a SHAPE fault, not a range fault. Checking it after an unreadable count served a caller's
    mistake as the engine's (``config_unverified``) -- the inverse of T1. ``cfg`` is bounded first.
    """
    if cfg < 1:
        raise ToolParamError(
            f"config {cfg} out of range; configuration indices start at 1"
        )


def _is_all(config):
    """True iff ``config`` is the EXACT built-in ``str`` ``"all"``.

    ``type(config) is str`` first: a ``str`` SUBCLASS is never ``"all"`` here (its
    ``__eq__`` is caller code, and ``resolve_config_selector`` REFUSES it outright), so a
    hostile ``__eq__`` is never dispatched.
    """
    return type(config) is str and config == _ALL


def _coerce_config_int(config):
    """Coerce a ``config`` selector to an exact int; reject bool / subclass / non-integral / absurd.

    Accepts an exact ``int`` OR an integral ``float`` (``2.0`` -> 2 — the
    JSON-round-trip rule); a bool (an int subclass — a client miswrite) and a
    non-integral / non-finite float are REJECTED. Returns the int or raises
    ``ToolParamError``.

    HOSTILE NUMERICS (T3). Any other ``int`` / ``float`` SUBCLASS is REFUSED, not coerced:
    its ``__int__`` / ``__index__`` / ``__float__`` / ``__eq__`` / ``__repr__`` is caller code
    that ``int(config)`` / ``config == int(config)`` / ``{config!r}`` used to dispatch to (the
    escape was an ``internal`` envelope), and a value that cannot be told from a hostile
    subclass is not worth a guess — the same stance ``config_in_range`` and
    ``with_configuration`` take (exact built-ins only). Refusing, rather than quietly reading
    the subclass through its base slot, is what the ticket's acceptance asks ("a hostile
    subclass is a refusal, never internal"). Nothing here runs a subclass's code: the type
    test is ``type(config)``, and the message renders through ``_render_config_value``. A
    magnitude above ``_CONFIG_INT_BOUND`` is refused rendering only its bit length (never
    the digits). This function touches no engine object, so no transport loss can pass
    through it.
    """
    kind = type(config)  # never ``isinstance``: a ``__class__`` property is caller code (round 2)
    if kind is bool:
        raise ToolParamError(
            "config must be an integer config index or "
            f"{_ALL!r}, not a bool ({_render_config_value(config, type_name=False)})"
        )
    if kind is int:
        return _bounded_config_int(config)
    if kind is float:
        if float.is_integer(config):  # False for nan / inf too
            return _bounded_config_int(int(config))
        raise ToolParamError(
            f"config must be an integer config index, got non-integral float {config!r}"
        )
    if issubclass(kind, (int, float)):
        raise ToolParamError(
            "config must be an exact int or float, not a numeric subclass "
            f"({_render_config_value(config)}); a subclass can carry caller code in its "
            "arithmetic and is refused rather than read"
        )
    raise ToolParamError(
        f"config must be an integer config index or {_ALL!r}, got "
        f"{_render_config_value(config)}"
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

    ORDER: the selector SHAPE is validated FIRST (``None`` / exact ``"all"`` /
    ``_coerce_config_int``) and only then is the count read, so a malformed selector always
    gets its param refusal whatever the MCE state.

    THE COUNT (T1): ``int`` and ``"all"`` read ``strict_number_of_configurations`` — an
    unreadable count RAISES (``ConfigUnverifiedError``; transport loss ->
    ``SessionClosedError``) instead of degrading to ``1``, which blamed the caller
    (``config 2 out of range; valid 1..1``) or swept ``[1]`` as if complete. An ABSENT MCE
    is the single-config system it is (``n = 1``): a 1-config system + ``config="all"`` ->
    ``("all", [1], 1)`` (valid; no spurious refusal) — so a grader called with no config on
    a single-config system is byte-identical to today. ``config is None`` renders no
    verdict on the count, so it keeps the SAFE readers (known limit: the
    ``config_evaluated`` echo can still be a substituted 1).
    """
    if config is None:
        return (
            "current",
            [safe_current_configuration(system)],
            safe_number_of_configurations(system),
        )

    # The "all" sweep — EXACTLY this spelling (a non-"all" string is a param error, NOT a
    # silent default). Check str BEFORE the int coerce so "all" never hits the int path.
    if issubclass(type(config), str):  # not ``isinstance`` (round 2)
        if _is_all(config):
            n = _strict_count_or_one(system)
            return (_ALL, list(range(1, n + 1)), n)
        raise ToolParamError(
            f"config string must be exactly {_ALL!r} (the all-config sweep), got "
            f"{_render_config_value(config, type_name=False)}; no case-variant is accepted"
            + ("" if type(config) is str else " (nor a str subclass)")
        )

    cfg = _coerce_config_int(config)
    _refuse_below_one(cfg)  # SHAPE: invalid for every N, so refused before the count read
    n = _strict_count_or_one(system)
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
    if _is_all(config):
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



def _carry_restore_warning(error, warning):
    """The error to raise for ``error`` with a failed-restore ``warning`` on it. Never raises.

    The dispatcher serves ``str(error)``, so a restore fault that only reached ``ctx`` was lost
    whenever the block raised (a ``ToolParamError`` for a bad path or range). An error that
    already carries envelope disclosures (a dict ``extra``, e.g. ``_Refuse``) gets the warning
    there, where its caller already puts it; an existing ``mutation_warning`` keeps its text and
    the new one is joined after it (one key, both disclosed). Any other goes through the one shared
    ``_error_note.annotate`` (same type and family where it can, else a family-identical
    replacement chained from the original)."""
    try:
        extra = getattr(error, "extra", None)
        if isinstance(extra, dict):
            old = extra.get("mutation_warning")
            if not (isinstance(old, str) and old):
                extra["mutation_warning"] = warning
            elif warning not in old:
                extra["mutation_warning"] = f"{old}; {warning}"
            return error
        return _en.annotate(error, f"; {warning}")
    except Exception:  # noqa: BLE001 -- a failed annotation must never replace the real error
        return error


def _restore_and_verify(system, saved, state):
    """RESTORE (always): switch back to ``saved`` + post-restore verify. NEVER raises (the
    ``with_best_focus`` precedent: a raise here would mask the body's result or error)."""
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


@contextmanager
def with_configuration(system, cfg):
    """Save -> switch -> yield -> restore -> VERIFY the active config (the with_best_focus shape, Q11).

    0. VALIDATE before any engine write (T2 + the S-1/S-2 audit fold-ins). ``cfg`` must be
       an EXACT ``int`` (``type(cfg) is int``; anything else is a ``ToolParamError`` before
       any engine read — production callers already pass ``_coerce_config_int`` output) and
       in ``1..N`` of the STRICT count; the ENTRY index is read STRICTLY and must satisfy
       ``config_in_range(saved, n)``. An unreadable count/entry RAISES out of ``__enter__``
       (``ConfigUnverifiedError``; transport loss -> ``SessionClosedError``) with NO switch
       made — there is nothing to restore. The old ``safe_current_configuration`` entry
       degraded to ``1`` on a fault, so a design entered at config 3 was "restored" to 1
       and ``restore_verified`` read True (``1 == 1``) over a design left at the wrong
       configuration. ``state["config_entry"]`` is the PROVEN entry index. An ABSENT MCE is
       the single-config system it is (``n = saved = 1``), so ``cfg != 1`` is refused as out
       of range rather than attempted-then-warned.
    1. SAVE ``saved`` (above).
    2. If ``cfg != saved``: ``SetCurrentConfiguration(cfg)`` + read-back-prove
       ``== cfg`` (REUSE the S1 ``set_current_configuration`` read-back; a no-op switch is
       the S1 lever's own guard).
    3. yield a MUTABLE dict ``{"config": cfg, "config_entry": saved, "switched": bool,
       "restore_verified": True, "mutation_warning": None}``.
    4. finally: ``SetCurrentConfiguration(saved)``; POST-RESTORE re-read; on a mismatch
       set ``restore_verified=False`` + a loud ``mutation_warning``. NEVER raises past
       ``finally`` (a finally-raise masks the body result — the ``with_best_focus``
       precedent), GIVEN exact-int operands, which the entry validation guarantees (every
       comparison and render below is between built-in ints). A switch THROW on enter
       degrades to ``switched=False`` + a ``mutation_warning`` (the caller reads at
       whatever the active index is + DISCLOSES).

    Q11 nesting (required order): ``analyze_strehl``/``analyze_wavefront`` nest
    ``with_best_focus`` INSIDE ``with_configuration`` — ``with_configuration(k)`` OUTER
    (switch config), ``with_best_focus`` INNER (scan that config's focus). Both restore in
    ``finally``; OUTER restores last.
    """
    if type(cfg) is not int:
        raise ToolParamError(
            f"with_configuration needs an exact int config index, got "
            f"{_render_config_value(cfg)}"
        )
    _bounded_config_int(cfg)  # round 2: a 5000-digit cfg must never reach a ``{cfg}`` render
    _refuse_below_one(cfg)
    n = _strict_count_or_one(system)
    if not config_in_range(cfg, n):
        raise ToolParamError(
            f"config {cfg} out of range; valid 1..{n} (NumberOfConfigurations={n})"
        )
    saved = strict_current_configuration(system)
    if saved is None:  # ABSENT MCE — the single-config system; nothing to switch
        saved = 1
    if not config_in_range(saved, n):
        raise ConfigUnverifiedError(
            f"MCE.CurrentConfiguration read back {saved}, outside 1..{n} "
            "(NumberOfConfigurations); the entry configuration cannot be verified — "
            "refusing before any switch"
        )

    state = {
        "config": cfg,
        "config_entry": saved,
        "switched": False,
        "restore_verified": True,
        "mutation_warning": None,
    }

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
    except BaseException as exc:
        # The restore + verify runs exactly once, BEFORE the error leaves, so its warning can
        # ride on it. A replacement (when the error cannot take the note in place) is raised
        # ``from`` the original; the original is never masked.
        _restore_and_verify(system, saved, state)
        if isinstance(exc, Exception) and not state["restore_verified"]:
            replacement = _carry_restore_warning(exc, state["mutation_warning"])
            if replacement is not exc:
                raise replacement from exc
        raise
    else:
        _restore_and_verify(system, saved, state)


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
    STRH at the requested field for Strehl, wave-1 RWCE at the requested hx/hy for wavefront
    (both default on-axis / field 1), min edge clearance for clearance), so the
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
    """The shared "all-config sweep" driver every grader calls.

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

    A raise from ``with_configuration.__enter__`` (an unreadable count / entry index —
    ``ConfigUnverifiedError``, or ``SessionClosedError`` on transport loss) PROPAGATES: in
    ``"all"`` mode the configs already visited were restored by their own ``finally`` and
    the sweep ABORTS rather than reporting a partial vector over an unproven entry. Like a
    bad selector's ``ToolParamError`` it is for the caller's wrapper to serve.
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
        if switch_failed:
            # The switch to config k did not take, so this entry was graded on whichever
            # config was active. Say so ON THE ENTRY (the single-config path does), not only
            # in the aggregate coverage.
            entry.setdefault("config_switch_warning", ctx["mutation_warning"])
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


# --------------------------------------------------------------------------- #
# MCE rows the ENGINE deletes as a side effect of another edit (one shape, two tools).
# --------------------------------------------------------------------------- #
# Measured: deleting a field deletes every MCE row that names it and renumbers the later
# rows; removing a surface deletes the MCE rows whose surface parameter is that surface.
# The edit tools snapshot the MCE before and call ``mce_rows_deleted`` after, so the
# caller sees which rows went and where the survivors moved.

def mce_snapshot(system):
    """The MCE rows as plain dicts (row, operand, param1-3, per-config values), or ``None``.

    Fail-open: any fault reading the MCE (or one unreadable row) answers ``None``, never
    a raise and never a partial list, so a caller reports "not read" rather than a guess.
    """
    try:
        from .mce_config import _describe_row   # local: mce_config imports this module
        n_rows = int(system.MCE.NumberOfOperands)
        n_configs = _mc.number_of_configurations(system)
        rows = [_describe_row(system, r, n_configs) for r in range(1, n_rows + 1)]
    except Exception:  # noqa: BLE001 - an unreadable MCE is reported, not raised
        return None
    if any("error" in r for r in rows):
        return None
    return [{"row": r["row"], "operand": r["operand"], "param1": r["surface"],
             "param2": r["param"], "param3": r["param3"], "values": r["values"]}
            for r in rows]


def _mce_alignment(before, after, moved=None):
    """Pair surviving rows of ``before`` with ``after`` in order -> ``{old_index: new_index}``.

    A row survives as the same operand with the same per-config values; its parameters may
    have been renumbered. Maximum-weight in-order matching, so two identical-looking rows
    are told apart by their parameters: a pair whose parameters are the ones the edit is
    known to leave the row with outweighs any other. ``moved(row)`` is that expectation (a
    ``(param1, param2, param3)`` tuple, or ``None`` for a row the edit is known to delete);
    without it a pair with unchanged parameters is preferred.
    """
    nb, na = len(before), len(after)
    score = [[0] * (na + 1) for _ in range(nb + 1)]

    def weight(b, a):
        if b["operand"] != a["operand"] or b["values"] != a["values"]:
            return 0
        want = (b["param1"], b["param2"], b["param3"]) if moved is None else moved(b)
        got = (a["param1"], a["param2"], a["param3"])
        return 3 if want == got else 1

    for i in range(nb - 1, -1, -1):
        for j in range(na - 1, -1, -1):
            w = weight(before[i], after[j])
            best = max(score[i + 1][j], score[i][j + 1])
            score[i][j] = max(best, w + score[i + 1][j + 1]) if w else best
    pairs, i, j = {}, 0, 0
    while i < nb and j < na:
        w = weight(before[i], after[j])
        if w and score[i][j] == w + score[i + 1][j + 1]:
            pairs[i] = j
            i, j = i + 1, j + 1
        elif score[i][j] == score[i + 1][j]:
            i += 1
        else:
            j += 1
    return pairs


def mce_rows_deleted(system, before, moved=None):
    """Compare the MCE now against ``before`` (a ``mce_snapshot``) -> the report block.

    Keys: ``state`` (``unchanged`` / ``rows_deleted`` / ``unverified`` / ``not_read``),
    ``rows_before``, ``rows_after``, ``deleted`` (each: row, operand, param1, param2,
    param3, values, as read before the edit) and ``renumbered`` (``{from, to}`` for each
    surviving row whose row number moved). ``unverified`` means a row now present could
    not be matched to one read before. ``not_read`` carries only ``state``. ``moved`` is the
    optional per-row expectation ``_mce_alignment`` takes.
    """
    after = mce_snapshot(system)
    if before is None or after is None:
        return {"state": "not_read"}
    pairs = _mce_alignment(before, after, moved)
    deleted = [before[i] for i in range(len(before)) if i not in pairs]
    moved = [{"from": before[i]["row"], "to": after[j]["row"]}
             for i, j in sorted(pairs.items()) if before[i]["row"] != after[j]["row"]]
    if len(pairs) != len(after):
        state = "unverified"
    else:
        state = "rows_deleted" if deleted else "unchanged"
    return {"state": state, "rows_before": len(before), "rows_after": len(after),
            "deleted": deleted, "renumbered": moved}


__all__ = [
    "resolve_config_selector",
    "resolve_single_config_selector",
    "with_configuration",
    "evaluate_over_configs",
    "reconcile_visited",
    "mce_snapshot",
    "mce_rows_deleted",
    "safe_number_of_configurations",
    "safe_current_configuration",
    "strict_number_of_configurations",
    "strict_current_configuration",
    "_CONFIG_PARAM",
    "_CONFIG_RESTORE",
    "_CONFIG_COVERAGE",
    "_CONFIG_NO_VARIATION",
]
