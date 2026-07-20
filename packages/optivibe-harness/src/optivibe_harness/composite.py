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
