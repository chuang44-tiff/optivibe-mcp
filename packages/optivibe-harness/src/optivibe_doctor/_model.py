"""The record types doctor moves between its workers, its classifiers and its renderers.

Rule 2 of the execution model: **workers emit FACTS; the parent alone assigns a status.**
``Observation`` therefore has no ``status`` field, structurally — a probe cannot hand-write
a verdict even by accident.

Stdlib only.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Tuple

# A JSON-representable leaf or container.  Kept loose on purpose: the renderers coerce
# anything unexpected rather than raising, because the parent must always finish its report.
JSONValue = Any


class Status(str, Enum):
    """The five verdicts a single check can carry.

    The SKIP / UNKNOWN split is load-bearing and is the whole of the state machine's
    honesty:

    * ``SKIP``    — the check was **not attempted**: either its flag was not passed
      (``blocked_by == "not_requested"``) or a definitive prerequisite finding makes it
      unattemptable (``blocked_by == "<that finding's id>"``).  A SKIP is a statement about
      the *target's* configuration.  **It never gates.**
    * ``UNKNOWN`` — **doctor could not measure.**  A worker died, timed out, emitted
      malformed JSON, returned a mismatched check id, or an exception escaped doctor's own
      machinery.  UNKNOWN is always a statement about *doctor*, never about the target, and
      it always gates (exit 3).
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIP = "skip"


class State(str, Enum):
    """The run-level verdict."""

    READY = "READY"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"
    INCOMPLETE = "INCOMPLETE"


#: The literal ``blocked_by`` a SKIP carries when its flag simply was not passed.
NOT_REQUESTED = "not_requested"


@dataclass(frozen=True)
class Observation:
    """What a worker emits.  NO status field, by rule 2."""

    check: str
    facts: Mapping[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """What the parent emits."""

    check: str
    status: Status
    reason: str = ""              # a pinned token, "" when PASS with nothing to say
    summary: str = ""
    facts: Mapping[str, JSONValue] = field(default_factory=dict)
    remedy: str = ""
    blocked_by: str = ""          # REQUIRED when status is SKIP (see the SKIP invariant)


@dataclass(frozen=True)
class Summary:
    """The run-level record, and the only thing that carries an exit code."""

    state: State
    complete: bool
    expected: Tuple[str, ...]
    received: Tuple[str, ...]
    counts: Mapping[str, int]
    exit_code: int
