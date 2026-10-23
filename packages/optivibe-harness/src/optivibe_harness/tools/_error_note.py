"""tools/_error_note.py -- carry a plain-English note on an exception that is already propagating.

NOT dispatchable (no ``TOOL_SPEC``). The dispatcher serves ``"{Type}: {str(exc)}"`` and
classifies by the exception's type, so a note that must reach the caller has to travel in the
message of an exception of the SAME family. ``annotate`` does that and nothing else; it NEVER
raises, and when the note cannot be attached it returns the exception unchanged (the original
error still travels, as it did before).

- a .NET exception (transport loss, engine fault) is re-expressed as the SAME mapped harness
  error class (``map_dotnet_exception``) with the note appended, chained ``from`` the original,
  so ``session_closed`` stays ``session_closed``;
- every other ``Exception`` keeps its type: a single string argument gets the note appended in
  place (``HarnessError`` subclasses with structured attributes keep them);
- a class with its own ``__str__`` may not show the note; that is accepted (the original error
  still travels unchanged in type and family).
"""
import copy

from ..errors import HarnessError, map_dotnet_exception


class AnnotatedError(Exception):
    """A family-identical stand-in for an error that cannot take a note in place.

    Not a ``HarnessError`` and not a .NET type, so the dispatcher classifies it exactly as it
    classifies the plain Python exceptions it replaces (``internal``). Its message is the
    original served text plus the note; the original is its ``__cause__``."""


def annotate(exc, note):
    """The exception to raise for ``exc`` with ``note`` in its served message. Never raises.

    Order: (1) a .NET exception becomes its mapped harness class (family kept); (2) a single
    ``str`` argument (or none) takes the note in place; (3) any other shape (``OSError(13, ..)``,
    ``ValueError("a", "b")``) is NOT mutated -- a copy of it takes the note when ``str()`` of
    the copy shows it (same type, attributes and family); (4) else a non-``HarnessError`` is
    replaced by ``AnnotatedError`` (family ``internal``, as the original had); (5) else, only a
    ``HarnessError`` whose copy cannot show the note, the original is returned unchanged."""
    try:
        mapped = map_dotnet_exception(exc)
        if mapped is not None:
            mapped.args = (f"{mapped}{note}",)
            mapped.__cause__ = exc
            return mapped
        args = exc.args
        if len(args) == 1 and type(args[0]) is str:
            exc.args = (args[0] + note,)
            return exc
        if not args:
            exc.args = (note.lstrip("; "),)
            return exc
        text = f"{exc}{note}"
        # A class that keeps the default ``__str__`` serves ``args[0]`` alone once args is one
        # str, so its copy shows the note; one with its own ``__str__`` (``OSError``) may not.
        # Never a NEW StopIteration: raised out of a generator (``with_configuration``) PEP 479
        # turns it into a bare RuntimeError that drops both the message and the note.
        if type(exc).__str__ is BaseException.__str__ and not isinstance(exc, StopIteration):
            try:
                clone = copy.copy(exc)
                clone.args = (text,)
                clone.__cause__ = exc
                return clone
            except Exception:  # noqa: BLE001 -- not copyable: use the stand-in below
                pass
        if not isinstance(exc, HarnessError):
            return AnnotatedError(f"{type(exc).__name__}: {text}")
        return exc
    except Exception:  # noqa: BLE001 -- a failed annotation must never replace the real error
        return exc


def raise_with_note(exc, note):
    """Raise ``exc`` (a caught ``Exception``) carrying ``note``; same family, original chained."""
    new = annotate(exc, note)
    if new is exc:
        raise exc
    raise new from exc
