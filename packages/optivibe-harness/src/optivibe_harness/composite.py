"""composite.py — the single-OptiVibe-MCP composition core (NO mcp import).

``CompositeDispatcher`` fronts N owning dispatchers (``[("harness", h),
("reference", r)]``) as ONE dispatcher-shaped surface: it merges their manifests,
builds an immutable route-by-name table, and forwards each ``dispatch`` to the
owning dispatcher — which supplies its OWN arg-0 (the ZOS session for harness
tools, the DB connection for reference tools). The two arg-0 contracts are
reconciled HERE at the routing layer, never by forcing one shape on both
(probe rule).

Both sub-dispatchers expose the identical never-raise contract — ``list_tools()``
-> ``[{name, description, required_params}]`` and ``dispatch(name, params)`` ->
``{ok, tool, result, error, error_family}``. So the composite is a pure
route-by-name; the only NEW mechanisms it adds are:

- a fail-closed collision guard at COMPOSE time (``CompositionCollisionError`` on
  the FIRST duplicate name — never silent last-wins, never deferred to dispatch);
- a SNAPSHOT manifest captured at ``__init__`` so advertise==routable by
  construction (a later mutation of a sub-dispatcher's manifest cannot desync the
  composite's route from what it advertises);
- a thin ``except BaseException`` outer net around the forward so a
  future sub-dispatcher contract breach escaping into mcp's ``call_tool``
  coroutine still envelopes as ``internal`` instead of wedging the serve loop.

``mcp`` is NOT imported here (the MCP adapter lives in ``server_mcp.py`` and
imports ``mcp`` lazily). ``dispatch`` NEVER raises out to the caller.

``LoggedReferenceDispatcher`` and ``compose_dispatchers`` add REFERENCE-dispatch
visibility to the ledger without weakening the lazy-boot lock. AXIS 11 (a settled
design decision) is the obligation: a reference-only
session must still leave NOTHING on disk, so the wrapper only ever READS an
already-installed logger — it can never create one, never activate logging, and
never open the engine. ``compose_dispatchers`` is THE production composition, so a
guard can compose through the same path ``main()`` does. The resulting
reference-dispatch count is structurally partial; the partial-denominator statement
lives in ONE place — ``interaction_log.COVERAGE_NOTE`` and its module docstring —
and is not restated here.

Live ZOS-API integration: the composite routes a real ``get_system_info`` (harness
+ live session) AND a real ``lookup_glass`` (reference + DB conn) through the SAME
composite in the live integration test.
"""
from .server import _safe_error_text


def _copy_entry(entry):
    """Snapshot one advertise entry, deep-copying its mutable nested members.

    Copies the nested ``required_params`` LIST and the nested ``param_types`` DICT
    (the latter only when present — the 5 reference tools carry none, and that
    absence is preserved) so a caller mutating a returned entry cannot reach into
    the source dispatcher's manifest nor the composite's internal snapshot.
    """
    return {
        **entry,
        "required_params": list(entry["required_params"]),
        **(
            {"param_types": dict(entry["param_types"])}
            if "param_types" in entry
            else {}
        ),
    }


class CompositionCollisionError(Exception):
    """Two composed dispatchers advertise the same tool name (fail-closed compose)."""

    error_family = "tool_collision"


class CompositeDispatcher:
    """Routes a tool request to its owning sub-dispatcher by name; never raises.

    Constructed from an ordered list of ``(label, dispatcher)`` pairs. At
    ``__init__`` it snapshots each ``disp.list_tools()`` into a merged manifest and
    an immutable ``{name: dispatcher}`` route in build order, raising
    ``CompositionCollisionError`` on the FIRST duplicate name — fail-closed at
    compose, never silently last-wins, never deferred to dispatch.
    """

    def __init__(self, dispatchers):
        self._route = {}          # {name: owning dispatcher}  — immutable post-init
        self._label_of = {}       # {name: owning label}       — for the diagnostic
        self._manifest = []       # merged advertise list (snapshot, build order)
        for label, disp in dispatchers:
            for entry in disp.list_tools():
                name = entry["name"]
                if name in self._route:
                    raise CompositionCollisionError(
                        f"tool name {name!r} is advertised by both "
                        f"{self._label_of[name]!r} and {label!r}; "
                        f"refusing to compose (fail-closed)"
                    )
                self._route[name] = disp
                self._label_of[name] = label
                # Copy the advertise dict so a later mutation of the source
                # dispatcher's manifest cannot reach into our snapshot. Deep-copy
                # the nested required_params LIST too — a shallow dict(entry) would
                # share that mutable member, so a caller mutating a list_tools()
                # result could corrupt the internal snapshot (BUG-2). The typed-
                # inputSchema cycle added a nested param_types DICT on harness
                # entries; copy it too when present (absence preserved — the 5
                # reference tools carry no param_types).
                self._manifest.append(_copy_entry(entry))

    def list_tools(self):
        """Return a COPY of the merged advertise snapshot (advertise==routable).

        Deep-copies the nested ``required_params`` list AND the nested
        ``param_types`` dict (when present) per entry so a caller mutating either
        cannot reach back into the internal snapshot nor a later ``list_tools()``
        return (BUG-2).
        """
        return [_copy_entry(entry) for entry in self._manifest]

    def dispatch(self, tool_name, params):
        """Dispatch ``tool_name`` by routing to its owning dispatcher; ALWAYS envelopes.

        Coerces a non-dict ``params`` to ``{}`` (mirror ``server.py:160``). A name in
        NO sub-manifest -> a composite-owned ``unknown_tool`` envelope. The route
        lookup AND the forward to ``disp.dispatch()`` both run INSIDE the
        ``except BaseException`` net (mirror ``server.py:164``, which does ``.get()``
        inside its try) so an UNHASHABLE ``tool_name`` (list/dict/set) — whose
        ``dict.get`` raises ``TypeError`` — still envelopes (as ``internal``) instead
        of raising straight out of ``dispatch()``. A sub-dispatcher contract breach (a
        future tool/``__str__`` escaping its own never-raise net) likewise envelopes
        as ``internal`` rather than wedging the mcp serve loop the composite fronts.
        """
        if not isinstance(params, dict):  # mirror server.py:160 — coerce any non-dict
            params = {}
        try:
            disp = self._route.get(tool_name)
            if disp is None:
                return {
                    "ok": False,
                    "tool": tool_name,
                    "result": None,
                    "error": f"unknown tool: {tool_name!r}",
                    "error_family": "unknown_tool",
                }
            return disp.dispatch(tool_name, params)
        except BaseException as exc:  # noqa: BLE001 — outer net: composite must NEVER raise
            return {
                "ok": False,
                "tool": tool_name,
                "result": None,
                "error": _safe_error_text(exc),
                "error_family": "internal",
            }


class LoggedReferenceDispatcher:
    """Log each dispatch of a wrapped (reference) dispatcher through an ALREADY-ACTIVE logger.

    AXIS 11 (owner-reaffirmed): this class NEVER creates an
    ``InteractionLog``, NEVER calls ``LazyHarnessDispatcher._activate_logging()``, never
    touches fd 1/2, never opens the engine, never imports ``mcp``. ``logger_provider`` is a
    zero-arg callable returning a logger-or-None, called PER DISPATCH (it is never cached);
    when it returns None the wrapper is a pure pass-through and NOTHING is written. The
    wrapper holds no attribute naming the session; it holds ONLY the callable -- which, in
    the production composition, is a closure whose cell DOES retain the session. The claim
    "the wrapper cannot reach the session" is WITHDRAWN and is not asserted anywhere: a
    security boundary against Python introspection is not a goal of this cycle.

    The wrapped object is consumed through the two-method duck-type ``composite.py`` already
    relies on (``list_tools`` / ``dispatch``). The reference package is not imported,
    subclassed or inspected. No ``__getattr__`` delegation. No counter, no once-only
    state, no breadcrumb: the wrapper is STATELESS beyond its three ctor attributes.

    OUTCOME GUARANTEE, stated as the narrower TRUE claim: for every dispatch on which the
    logging layer raises nothing or raises only ``Exception`` subclasses, (i) the inner is
    invoked EXACTLY ONCE, and (ii) the wrapper's outcome is the unwrapped dispatcher's BY
    IDENTITY -- the same returned object (``is``), or the same raised exception instance
    (``is``, any ``BaseException``). ``__traceback__`` and ``__context__`` are NOT part of
    the claim: a re-raise adds a frame, and a replaced-then-recovered exception carries the
    logging fault as its ``__context__`` (informative, not a difference in outcome).

    DELIMITED, not covered: a ``BaseException`` (``KeyboardInterrupt`` / ``SystemExit``)
    that ORIGINATES in the logging layer -- in ``record_call`` / ``log()`` / ``_exc_to_dict``,
    during entry or exit -- propagates through the wrapper, exactly as it propagates through
    ``log()``, ``_activate_logging`` and every other never-raise net in this repo. That
    asymmetry is DELIBERATE and repo-wide (a deliberate abort is never swallowed), and this
    wrapper does not widen its ``except`` to ``BaseException`` to buy identity there,
    because doing so would swallow an abort. In that case the inner has still run AT MOST
    ONCE (never twice); the answer is lost to the abort, as it would be anywhere else.
    """

    def __init__(self, inner, logger_provider, *, arg0_label: str = "conn"):
        self._inner = inner
        self._logger_provider = logger_provider
        self._arg0_label = arg0_label

    @property
    def inner(self):
        """The wrapped dispatcher (read-only accessor; there is no ``__getattr__``)."""
        return self._inner

    def list_tools(self):
        """Return ``inner.list_tools()`` verbatim. NEVER logs (advertising is not a call)."""
        return self._inner.list_tools()

    def dispatch(self, tool_name, params):
        """Dispatch through the inner, logging ONE row iff a logger is already installed.

        Completion is tracked by FLAGS, never by the returned value: "the inner returned"
        is ``invoked and inner_exc is None``, so no comparison against ``envelope`` exists
        and an inner returning ANY object -- a sentinel, ``None``, a non-dict -- has that
        object returned by identity. The inner's exception is captured at the point it is
        raised, so whatever ``record_call`` does on exit (re-raise it, REPLACE it because
        ``_exc_to_dict``'s ``str(e)`` raised, or an injected manager SUPPRESSING it) the
        wrapper re-raises the ORIGINAL instance.

        A non-str / unhashable ``tool_name`` reaches only the ``intent`` f-string and the
        inner; neither raises on it, so the wrapper's answer equals the unwrapped
        dispatcher's. (``CompositeDispatcher.dispatch`` envelopes those as ``internal``
        before any sub-dispatcher sees them; direct callers can still get here.)
        """
        try:
            logger = self._logger_provider()
        except Exception:  # noqa: BLE001 - a broken provider degrades to pass-through
            logger = None
        if logger is None:
            # Nothing written, nothing counted, nothing remembered (axis 11).
            return self._inner.dispatch(tool_name, params)

        invoked, envelope, inner_exc = False, None, None
        try:
            with logger.record_call(
                intent=f"dispatch {tool_name}",
                call=f"{tool_name}({self._arg0_label}, params)",
                args=params,
            ) as holder:
                invoked = True  # set BEFORE the call: no path can reach a 2nd inner call
                try:
                    envelope = self._inner.dispatch(tool_name, params)
                except BaseException as exc:  # noqa: BLE001 - capture the ORIGINAL instance
                    inner_exc = exc
                    raise
                holder["result"] = envelope
        except Exception:  # noqa: BLE001 - a logging-layer fault, or the inner's own raise
            if inner_exc is not None:
                raise inner_exc  # the inner's ORIGINAL exception, by identity
            if not invoked:
                # The logging layer failed BEFORE the inner ran -> exactly one inner call.
                return self._inner.dispatch(tool_name, params)
            # invoked and no inner exception => the inner RETURNED; the logging layer failed
            # on exit. The answer stands: fall through.
        if inner_exc is not None:
            # The context manager SUPPRESSED the inner's raise -- the unwrapped dispatcher
            # would have raised, so the wrapper raises that same instance.
            raise inner_exc
        return envelope


def compose_dispatchers(harness, reference, session):
    """THE production composition. ``main()`` calls this and nothing else composes.

    Builds ``[("harness", harness), ("reference", LoggedReferenceDispatcher(reference,
    logger_provider))]`` omitting a ``None`` entry, where ``logger_provider`` is
    ``lambda: getattr(session, "_logger", None)`` -- the shipped idiom (``server.py:567``,
    ``lazy.py:232``) as a PURE READ. It can never activate anything, and because it reads
    ``session._logger`` on EVERY dispatch it can never cache a stale logger.

    WHICH CONDITION GATES A REFERENCE ROW: "``logger_provider()`` returns a logger" -- i.e.
    ``session._logger`` holds an INSTALLED ``InteractionLog`` -- and NOTHING weaker. Neither
    "a dispatchable harness call was made" nor "``_activate_logging()`` RAN" is sufficient:
    the activation flag is set in a ``finally`` EVEN WHEN activation failed, so an
    activation that raises before logger construction leaves ``_logger`` None, never
    retries, and makes every later dispatch -- harness and reference alike -- a silent
    pass-through for the process lifetime. No retry and no durability feature is added here.

    File EXISTENCE is a CONSEQUENCE, not the gate: a filesystem-existence check is
    explicitly REJECTED (it would make the wrapper depend on disk state and drop rows after
    an operator deletion). A reference row can therefore legitimately be ``seq 0`` of a
    freshly created JSONL -- after a harness call whose ``open()`` failed, or after the file
    was deleted. Neither breaches axis 11: a dispatchable harness call occurred in both.

    Returns a ``CompositeDispatcher``; raises ``CompositionCollisionError`` exactly as
    before. This function exists so a guard can compose through the SAME path production
    uses (OF-1) -- the three existing lazy-boot guards compose the BARE reference dispatcher
    by hand and cannot see a change in the shipped composition.
    """
    logger_provider = lambda: getattr(session, "_logger", None)  # noqa: E731 - the shipped idiom
    pairs = []
    if harness is not None:
        pairs.append(("harness", harness))
    if reference is not None:
        pairs.append(("reference", LoggedReferenceDispatcher(reference, logger_provider)))
    return CompositeDispatcher(pairs)
