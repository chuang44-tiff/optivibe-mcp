"""tools/_run_budget.py — the run-time budget of ONE local-optimizer call.

Measured live (OpticStudio 25.1, DLS and OD): ``RunAndWaitWithTimeout(seconds)``
returns ``TimedOut`` with the optimizer still running; ``Cancel()`` then stops it in about
0.5 s, in the middle of a cycle too; the improved lens STAYS in the editor, ``Close()``
succeeds and the optimizer opens again. A lens that held one DLS cycle for more than 160 s
stopped the same way.

LOCAL optimizer only. On the Hammer optimizer the same calls improve an internal copy that
never commits and block the tool slot, so the Hammer fork must never come through here.

Cycle-safe: imports nothing from ``optimize_run``.
"""

import time

from .._io import safe_call, safe_exc

#: The budget's clock. Its own name so a test can drive it without moving time for the
#: rest of the process.
clock = time.monotonic

DEFAULT_TIME_BUDGET_S = 120.0
TIME_BUDGET_S_CAP = 300.0
#: How long a cancelled run is given to stop before the call is reported as failed.
CANCEL_WAIT_S = 30.0

#: What ended the pass loop (the ``stop_reason`` result key). Exactly one of these.
STOP_GAIN_BELOW_THRESHOLD = "gain_below_threshold"
STOP_PASS_LIMIT = "pass_limit"
STOP_TIME_BUDGET = "time_budget"


def run_status_name(value):
    """The member NAME of a ``RunStatus`` value (``Completed`` / ``TimedOut`` / ...).

    The timed run returns an ENUM, not a bool, so it is never truth-tested: a zero-valued
    member can be falsy through the bridge. Never raises.
    """
    try:
        return str(value).rsplit(".", 1)[-1].strip()
    except Exception:  # noqa: BLE001 - a bridged value whose __str__ throws
        return ""


def run_pass(opt, remaining_s):
    """Run ONE local-optimizer pass for at most ``remaining_s`` seconds.

    Returns ``(state, detail)``:

    - ``("completed", "")`` the pass finished by itself;
    - ``("budget_reached", <engine status text at the stop>)`` it ran out of time and
      ``Cancel()`` stopped it. The status text may be empty: the STATE says what happened,
      never the text;
    - ``("no_time_left", "")`` nothing was started, because no time was left;
    - ``("failed", <reason>)`` the run did not start, did not stop on Cancel, or the stop
      itself threw.

    ``Cancel()`` is called ONLY after ``TimedOut``: on a run that finished by itself it
    returns False and the wait reports ``FailedToStart``, which is not a failure.
    A throw from ``RunAndWaitWithTimeout`` itself is NOT caught here (the caller's
    ``finally`` closes the optimizer, as it did for the untimed run).
    """
    if remaining_s <= 0:
        return "no_time_left", ""
    name = run_status_name(opt.RunAndWaitWithTimeout(float(remaining_s)))
    if name == "Completed":
        return "completed", ""
    if name != "TimedOut":
        return "failed", f"the optimizer run reported {name or 'no status'}"
    status = safe_call(lambda: str(opt.Status), "")
    try:
        opt.Cancel()
        opt.WaitWithTimeout(CANCEL_WAIT_S)
        still_running = bool(opt.IsRunning)
    except Exception as exc:  # noqa: BLE001 - a stop that throws is a failed run, said so
        return "failed", (
            "the run passed its time budget and the stop could not be confirmed: "
            f"{safe_exc(exc)}"
        )
    if still_running:
        return "failed", (
            "the run passed its time budget and did not stop within "
            f"{CANCEL_WAIT_S:g} s of Cancel"
        )
    return "budget_reached", status


class Budget:
    """One ``optimize`` call's run-time budget.

    Only time spent INSIDE a pass is charged (``spent``): the merit reads, snapshots and
    pass grading between passes are outside it, which is what the served text says.

    It also keeps what the pass loop learned about its own stop (``note_pass``), so the
    result can say which of three things ended the loop (``stop_reason``).
    """

    def __init__(self, seconds):
        self.seconds = seconds
        self.spent = 0.0
        self.reached = False
        self.cancelled = False
        self.stop_status = ""
        self.stable_pass = False
        self.last_pass_gain_pct = None

    def run_pass(self, opt):
        """Run one pass with what is left; returns ``(state, detail)`` as ``run_pass``."""
        started = clock()
        try:
            state, detail = run_pass(opt, self.seconds - self.spent)
        finally:
            self.spent += clock() - started
        if state in ("budget_reached", "no_time_left"):
            self.reached = True
        if state == "budget_reached":
            self.cancelled = True
            self.stop_status = detail
            # A pass that was cut short has no gain to report.
            self.last_pass_gain_pct = None
        return state, detail

    def note_pass(self, verdict, gain_pct):
        """Record a pass that ran to its end: its pass-to-previous ``verdict`` and its gain
        in percent of the previous merit. Returns True when the pass was ``stable``, which
        is the loop's own stop rule."""
        self.stable_pass = verdict == "stable"
        self.last_pass_gain_pct = gain_pct
        return self.stable_pass

    def stop_reason(self):
        """Which of three things ended the pass loop. The time budget is tested first: a
        pass cut short is never graded, so an earlier pass's grade must not speak for it.
        A stable pass ends the loop even when it was the last pass allowed, so that case
        reads ``gain_below_threshold``, not ``pass_limit``."""
        if self.reached:
            return STOP_TIME_BUDGET
        return STOP_GAIN_BELOW_THRESHOLD if self.stable_pass else STOP_PASS_LIMIT

    def status_text(self, opt):
        """The engine status for the result: the text read AT the stop when a pass was
        cancelled (even when that text is empty), else a guarded read now. Never raises."""
        if self.cancelled:
            return self.stop_status
        return safe_call(lambda: str(getattr(opt, "Status", "")), "")

    def warning(self, passes_run):
        """The stop disclosure, or None when the budget was not reached."""
        return stop_warning(self.reached, self.seconds, passes_run)


def stop_warning(budget_reached, time_budget_s, passes_run):
    """The disclosure a budget-stopped call carries, or None when the budget was not reached.

    A partial lens is not a converged one, so the stop is said in the ``warning`` channel as
    well as in the ``budget_reached`` key.
    """
    if not budget_reached:
        return None
    return (
        f"the optimizer was stopped at this call's time budget ({time_budget_s:g} s) "
        f"after {passes_run} pass(es) had started; the lens holds the partial result "
        "and is NOT converged, "
        "whatever the verdict reads. Call optimize again to continue; if repeated "
        "calls stop here with little gain, change the merit function or the "
        "variables rather than adding time"
    )


__all__ = [
    "Budget",
    "CANCEL_WAIT_S",
    "DEFAULT_TIME_BUDGET_S",
    "STOP_GAIN_BELOW_THRESHOLD",
    "STOP_PASS_LIMIT",
    "STOP_TIME_BUDGET",
    "TIME_BUDGET_S_CAP",
    "clock",
    "run_pass",
    "run_status_name",
    "stop_warning",
]
