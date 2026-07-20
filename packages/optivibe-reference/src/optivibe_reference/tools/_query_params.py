"""tools/_query_params.py — shared RAG candidate-cap param helpers.

Lifted VERBATIM from ``lookup_operand`` (PIN 4) so the two RAG tools
(``lookup_operand`` and ``search_reference``) validate a caller-supplied
candidate cap through ONE source of truth — the ``bool``-before-``int`` trap
(``True == 1`` slipping past an int check) is exactly the kind of bug that
re-appears when a validator is copy-pasted, so it lives here once.

Contract (unchanged from Layer-1):
- ``limit`` and ``top_n`` are ALIASES for the same cap; supplying BOTH raises;
- a cap that is a ``bool`` raises BEFORE int coercion (isinstance(bool) FIRST);
- a non-int or a value ``< 1`` raises;
- the validated override is applied as ``min(override, hard_cap)`` — ``hard_cap``
  is BOTH the default (no override) and the ceiling.
"""
from ..errors import ToolParamError

# The accepted aliases for the optional caller-supplied RAG candidate cap.
CAP_PARAM_ALIASES = ("limit", "top_n")


def validate_cap(name, value):
    """Validate one cap override (``limit``/``top_n``); return the int or raise.

    Rejects a ``bool`` FIRST (``True == 1`` / ``False == 0`` would otherwise slip
    past the int check), then a non-int, then ``< 1`` (so a negative AND a zero
    cap both raise — a cap below 1 is nonsensical). Raises ``ToolParamError``.
    """
    if isinstance(value, bool):
        raise ToolParamError(f"'{name}' must be an int, not a bool")
    if not isinstance(value, int):
        raise ToolParamError(
            f"'{name}' must be an int; got {type(value).__name__}"
        )
    if value < 1:
        raise ToolParamError(f"'{name}' must be >= 1; got {value}")
    return value


def effective_n(params, hard_cap):
    """Resolve the effective RAG candidate cap from the params (never returns None).

    ``limit`` and ``top_n`` are ALIASES for the same cap; supplying BOTH is a
    ``ToolParamError``. The supplied value is validated (see ``validate_cap``)
    then applied as ``min(override, hard_cap)`` — ``hard_cap`` is the ceiling and
    the default when no override is given.
    """
    present = [n for n in CAP_PARAM_ALIASES if n in params]
    if len(present) > 1:
        raise ToolParamError(
            f"give only one of {list(CAP_PARAM_ALIASES)}, not both: {present}"
        )
    if not present:
        return hard_cap
    name = present[0]
    override = validate_cap(name, params[name])
    return min(override, hard_cap)
