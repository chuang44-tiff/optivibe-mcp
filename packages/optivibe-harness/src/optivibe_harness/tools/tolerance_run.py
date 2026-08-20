"""tools/tolerance_run.py — the ``tolerance`` tool (sensitivity / monte_carlo).

ONE dispatchable tool ``tolerance(mode, tolerances?, trials?, full?, output_file?)``,
switched by ``mode={sensitivity, monte_carlo}`` (mirrors ``optimize``/``dry_run``).
v1 SCALAR perturbations only (radius TRAD / thickness-air TTHI / index TIND / Abbe
TABB), RMS-wavefront criterion (field-summed, in waves) with a nominal Strehl read
ALONGSIDE, default paraxial back-focus compensation.

The lifecycle:

1. Pre-flight (pure, validate-all, D14) — resolve ``mode``; build ``tolerances`` (or
   ``_default_tolerances(system)``); validate EVERY entry (collect all errors). Any
   error -> ``tolerancing_param``, NOTHING opened/authored.
2. LDE snapshot (D16) — capture the compact state vector.
3. TDE author-as-scratch (D10) — ``DeleteAllRows`` (floor-at-1) -> author one row per
   tolerance (``AddOperand``/``ChangeType``/``Param1``/``Min``/``Max``).
4. Open the single slot (D13) — ``with _tolerancing_session(system) as tol:``.
5. Configure — ``SetupMode``/``Criterion`` (getattr-resolved members); for MC
   ``NumberOfRuns``; ``SaveTolDataFile=False`` (D15); ``os.makedirs`` (D15/#58);
   ``OutputFile`` BEFORE the run.
6. Run — ``RunAndWaitForCompletion()``; read ``Succeeded``/``ErrorMessage`` WHILE open
   (engine-success canary, D11); echo ``trials`` from ``NumberOfRuns``.
7. ``Close()`` in ``finally`` (D13) — slot reaped on every path; ``.ZTD``/report
   glob-reap (D15).
8. Parse the UTF-16-LE report (D11 canary; never zero-fill).
9. TDE clear (D10) — ``DeleteAllRows`` in ``finally`` (floor-at-1).
10. LDE re-read + tripwire (D16) — assert byte-equality; set ``state_mutated``.
11. Strehl-alongside (D8) — nominal ``analyze_strehl`` read; null + warn on failure.
12. Build the envelope — never-raise (D17).

Error families: ``tolerancing_param``, ``tolerancing_unavailable``, ``tolerancing_run``,
``tolerancing_parse``.

Live ZOS-API integration: exercised by the live tolerancing test;
unit-tested against the fixture-seeded fakes whose
``RunAndWaitForCompletion`` writes the CAPTURED UTF-16 report to ``OutputFile`` so the
parser runs against REAL engine text.
"""
import functools
import math
import os
import tempfile

from .._io import safe_call, safe_exc, safe_float
from ..server import ToolSpec
from . import _analysis_common as _ac
from . import _tolerance_catalog as _cat
from . import _tol_cells as _cells
from . import _tolerance_common as _tc
from . import analysis_measure as _am

# The unexpected-engine-throw family (D17): an engine fault that escapes the body net
# degrades to ``tolerancing_run`` (keeps the envelope inside the frozen family set).
_ENGINE_ERROR_FAMILY = "tolerancing_run"

# The Min/Max Double cell columns (probe capture: every perturbation op carries
# Min@col6 / Max@col7, ``DataType == "Double"``). Written via _tol_cells.write_double_
# verified (read-back-proven) — NOT the op.Min/op.Max typed property (the v2 cell path).
_MIN_COL = 6
_MAX_COL = 7

# The engine FLOORS ``TDE.NumberOfOperands`` at 1 (the base row survives
# ``DeleteAllRows``; probe section 1, and both test doubles model it). So a PROVEN-empty
# editor reads ``<= 1``, never ``0`` — asserting 0 would refuse every correct clear.
_TDE_EMPTY_FLOOR = 1


def _never_raise(tool_name):
    """Wrap the handler so it NEVER raises past its boundary (D17).

    A ``ToleranceError`` keeps its intended structured ``family`` (one of the four
    frozen families). ANY other ``Exception`` (pythonnet maps every .NET throw onto an
    ``Exception`` subclass) is netted to a typed ``tolerancing_run`` envelope so a
    disconnected engine mid-run becomes ``{ok:false}`` rather than a crash.
    ``RecursionError`` is in the caught set (the wrapper must never raise).
    ``BaseException`` (KeyboardInterrupt / SystemExit) is deliberately NOT caught — it
    propagates AFTER the slot-reaping ``finally`` (D13) has run.

    ROUND-13 -- EVERY READ OF ``exc`` INSIDE THE HANDLER IS GUARDED. This decorator
    IS the never-raise boundary and its f-string interpolated ``exc`` from inside the
    ``except``: an exception whose ``__str__`` throws made the HANDLER itself raise.
    MEASURED — it ESCAPED with ``RuntimeError``, the typed ``tolerancing_*`` family was
    LOST, and the dispatch envelope degraded to the generic ``internal`` family. Three
    reads are routed, not just the f-string: the bare ``str(exc)`` one line up is the
    same defect differently spelled, and ``getattr(exc, "family", ...)`` suppresses only
    ``AttributeError`` — a ``__getattr__`` raising anything else walks straight out
    (defense-in-depth: today's raiser is our own ``ToleranceError``, so that third arm
    needs a hostile subclass, unlike the other two).
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params):
            try:
                return handler(session, params)
            except _tc.ToleranceError as exc:
                return _ac.error_envelope(
                    tool_name,
                    safe_call(lambda: getattr(exc, "family", "tolerancing"), "tolerancing"),
                    safe_exc(exc),
                )
            except Exception as exc:  # noqa: BLE001 — D17: net any engine throw
                return _ac.error_envelope(
                    tool_name, _ENGINE_ERROR_FAMILY,
                    f"{tool_name} hit an unexpected engine error: {safe_exc(exc)}",
                )
        return _wrapped
    return _decorate


def _resolve_mode(params):
    """Resolve + validate the REQUIRED ``mode`` param (locked D1).

    ROUND-8 -- unbound ``dict.get``, see ``_bool_param``. ``mode`` selects the
    sensitivity / monte_carlo SETUP FORK (``_tc._MODE_TO_SETUP``) — the exact shape of
    the ``optimize_run._resolve_algorithm`` DLS/Hammer fork round 7 swept. Bare base slot
    (no ``isinstance`` conjunct) matches that precedent and keeps today's non-``dict``
    behaviour a raise. Nil reachability today (JSON params + the
    ``server.Dispatcher.call_tool`` non-``dict`` coercion at :517).
    """
    mode = dict.get(params, "mode")
    if mode not in _tc._MODE_TO_SETUP:
        raise _tc.ToleranceError(
            f"mode must be one of {sorted(_tc._MODE_TO_SETUP)}, got {mode!r}",
            family="tolerancing_param",
        )
    return mode


def _resolve_trials(params):
    """Resolve the optional ``trials`` (monte_carlo) — positive int / integral float.

    Default 20 (SIGNATURE). A bool / non-integral float / non-int / ``< 1`` ->
    ``tolerancing_param``. An integral float (a JSON round-trip can float an int) is
    coerced (``20.0`` -> 20).

    ROUND-8 -- unbound ``dict`` slots, see ``_bool_param``. A lying ``__contains__``
    silently runs 20 Monte-Carlo trials for an explicit ``trials=500`` and reports the
    short run as the caller's. Nil reachability today.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, "trials")):
        return _tc._DEFAULT_TRIALS
    value = dict.__getitem__(params, "trials")
    if isinstance(value, bool):
        raise _tc.ToleranceError(
            f"trials must be an integer, not a bool ({value!r})",
            family="tolerancing_param",
        )
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            value = int(value)
        else:
            raise _tc.ToleranceError(
                f"trials must be an integer, got non-integral {value!r}",
                family="tolerancing_param",
            )
    if not isinstance(value, int):
        raise _tc.ToleranceError(
            f"trials must be an integer, got {type(value).__name__} {value!r}",
            family="tolerancing_param",
        )
    if value < 1:
        raise _tc.ToleranceError(
            f"trials must be >= 1, got {value}", family="tolerancing_param"
        )
    return value


def _bool_param(params, key, default):
    """Pull an optional bool param; reject a non-bool (loud).

    ROUND-8 -- MEMBERSHIP AND LOOKUP GO THROUGH THE UNBOUND ``dict``
    SLOTS. Round 7 applied this rule to ``optimize_run``'s copy of this door and left the
    ``optimize_merit`` / ``analysis_measure`` / ``tolerance_run`` copies on ``key not in
    params``, so four copies that used to AGREE started disagreeing. On a ``dict``
    SUBCLASS with a lying ``__contains__`` the door silently substitutes the default;
    measured in the ``optimize_run`` twin, ``require_free_stop=False`` came back **True**.
    Here the three keys are ``full`` / ``strict`` / ``include_mechanical`` — ``strict`` is
    the flag that decides whether a known-gap operand REJECTS the whole tolerance set or
    is merely disclosed, so a defaulted read turns a hard refusal into a soft warning. The
    rule ``_optimize_common.range_headers_supplied`` documents (:2772) is now at every
    copy of the door.

    REACHABILITY IS NIL TODAY AND THAT IS STATED, NOT ASSUMED: ``params`` arrives from
    JSON deserialization (and ``server.Dispatcher.call_tool`` coerces any non-``dict`` to
    ``{}`` at :517), so a ``dict`` subclass is structurally impossible on the shipped
    path. The ``isinstance`` conjunct matches ``optimize_run._bool_param``.
    """
    if not (isinstance(params, dict) and dict.__contains__(params, key)):
        return default
    value = dict.__getitem__(params, key)
    if not isinstance(value, bool):
        raise _tc.ToleranceError(
            f"{key!r} must be a boolean, got {type(value).__name__} {value!r}",
            family="tolerancing_param",
        )
    return value


def _resolve_output_path(session, params):
    """Resolve the report path (SIGNATURE / D15): explicit or a workspace temp.

    An explicit ``output_file`` (a non-empty str) is used as-is. Otherwise a workspace-
    resolved temp report path is built (a ``tempfile.mkstemp`` under the OS temp dir —
    reaped in ``finally``). The parent dir is ``makedirs``'d by the caller BEFORE the
    run (D15/#58: the engine SILENTLY writes nothing to a missing dir).
    """
    explicit = dict.get(params, "output_file")  # unbound base slot (see _bool_param)
    if explicit is not None:
        if not isinstance(explicit, str) or explicit.strip() == "":
            raise _tc.ToleranceError(
                f"output_file must be a non-empty string when provided, got {explicit!r}",
                family="tolerancing_param",
            )
        return os.path.abspath(explicit), False
    # A workspace-resolved temp report. mkstemp creates the (empty) file; the engine
    # overwrites it. The handler reaps it in finally (it is a tool-written temp).
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="optivibe_tol_")
    os.close(fd)
    return path, True


def _clear_tde(tde):
    """Clear the TDE via ``DeleteAllRows``, READ-BACK-PROVEN (floor-at-1; D10).

    Guarded: a clear THROW raises ``ToleranceError(family="tolerancing_run")`` so the
    handler nets it (the slot/TDE reaping happens in the caller's ``finally``).

    ROUND-13 -- THE CLEAR IS NOW PROVEN, NOT TRUSTED. Every CELL write on this same
    authoring path is read-back-proven (``_tol_cells.write_verified_cell`` /
    ``write_double_verified``, D6), yet the operation establishing the PRECONDITION for
    all of them — an empty editor — trusted the return of a void engine call. That is
    the ``AddCatalog`` / ``ScaleByUnits`` class: a clean call is not a
    proof. A SILENT no-op leaves the PREVIOUS run's scratch rows in place, this run
    authors after them, and the reconcile canary keys on
    ``(type, surface[, param])`` — so a leftover row of a type this run also authors is
    INDISTINGUISHABLE from this run's own, and the envelope still reports
    ``tde_cleared:true``. ``tde_cleared`` was honest on a throw and not on a no-op.

    The engine floors ``NumberOfOperands`` at 1 (the base row, probe section 1), so "empty" is
    ``<= _TDE_EMPTY_FLOOR``. An UNREADABLE count refuses rather than proceeding (
    absent would be "not applicable"; unreadable is UNKNOWN, and an unproven clear is
    exactly the indeterminate TDE this function already refuses to author onto).
    """
    try:
        tde.DeleteAllRows()
    except Exception as exc:  # noqa: BLE001 — a clear THROW -> tolerancing_run
        raise _tc.ToleranceError(
            f"clearing the Tolerance Data Editor threw "
            f"({safe_exc(exc, repr_form=True)}); refusing to author onto an "
            "indeterminate TDE",
            family="tolerancing_run",
        )
    try:
        remaining = int(tde.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — the PROOF is unreadable -> refuse
        raise _tc.ToleranceError(
            "the Tolerance Data Editor row count could not be read after DeleteAllRows "
            f"({safe_exc(exc, repr_form=True)}); the clear is UNPROVEN — refusing to "
            "author onto an indeterminate TDE",
            family="tolerancing_run",
        )
    if remaining > _TDE_EMPTY_FLOOR:
        raise _tc.ToleranceError(
            "DeleteAllRows returned cleanly but the Tolerance Data Editor still holds "
            f"{remaining} rows (expected at most {_TDE_EMPTY_FLOOR}, the engine's "
            "floored base row); the clear SILENTLY no-opped — refusing to author onto "
            "leftover scratch rows",
            family="tolerancing_run",
        )


def _author_tolerance_set(tde, operand_enum, authored):
    """Author one TDE row per tolerance, operand-AGNOSTIC (D5/D8 — table-driven).

    ``AddOperand()`` -> ``ChangeType(_resolve_tol_enum(...))`` -> walk
    ``int_cell_writes(meta, entry)`` (the ordered ``(CellSpec, int)`` list the catalog
    derives from the resolved entry: Surf/Surf1 from ``surface``, Surf2 from
    ``surface2``, RollSurf from ``roll_surf``, Code/Par# from ``code``/``param``) writing
    each through ``_tol_cells.write_verified_cell`` (cell.DataType Int/Double
    discriminator + Header-match + read-back proof, D6). For a ``has_minmax`` operand the
    Min/Max are written via ``_tol_cells.write_double_verified`` into the Min@col6 / Max@col7
    Double CELLS (``_MIN_COL`` / ``_MAX_COL``, DataType Double, read-back-proven) — NOT the
    ``op.Min``/``op.Max`` typed property (the v2 cell path). A control/compensator/structural
    op (no ``min``/``max`` in its validated entry) authors its int cells ONLY (no perturbation).

    The compensator cells are authored for ``.zmx`` fidelity but the run discloses
    ``compensator_participates:false`` (D13/G-COMP-INERT). NO per-token code — the
    catalog metadata drives every write (operand-agnostic).

    A write that does not read back -> a raised error (the silent-no-op trap, D6); the
    handler nets it to ``tolerancing_run``. A non-list/empty ``authored`` cannot reach
    here (the pre-flight floors it).
    """
    for entry in authored:
        token = entry["type"]
        meta = _cat.meta_for(token)
        member = _tc._resolve_tol_enum(operand_enum, token)
        op = tde.AddOperand()
        if not bool(op.ChangeType(member)):
            raise _tc.ToleranceError(
                f"the engine rejected operand type {token} during authoring",
                family="tolerancing_run",
            )
        # (a) the int cells (Surf/Surf1/Surf2/RollSurf/Code/Par#) — table-driven, each
        # read-back-verified via _tol_cells (cell.DataType discriminator + Header-match).
        # int_cell_writes raises CatalogResolveError if a required cell is missing; the
        # validator already guards that, so it surfaces as tolerancing_run if it ever does.
        try:
            writes = _cat.int_cell_writes(meta, entry)
        except _cat.CatalogResolveError as exc:
            raise _tc.ToleranceError(
                f"could not resolve the int-cell author plan for {token} "
                f"({safe_exc(exc)}); the "
                "entry is missing a required cell",
                family="tolerancing_param",
            )
        for cell_spec, value in writes:
            _cells.write_int_cell(op, cell_spec, value)
        # (b) the ±perturbation channel (Min/Max Double) — only for has_minmax operands.
        if entry.get("min") is not None or entry.get("max") is not None:
            _cells.write_double_verified(op, _MIN_COL, "Min", float(entry["min"]))
            _cells.write_double_verified(op, _MAX_COL, "Max", float(entry["max"]))


def _verify_int(field, intended, actual, token):
    """DEAD as of the cell-writer migration. ZERO callers in ``src/``.

    ROUND-10a — measured, not asserted: ``grep -rn _verify_int src/`` returns no CALL
    site. (ROUND-13 CORRECTS the wording, not the conclusion: the original said it
    "returns only this definition", and it returns THREE lines — the ``def``, this very
    sentence, and the cross-reference in ``_verify_float``'s docstring below. A reader
    re-running the command to check gets three hits and cannot tell whether the claim
    rotted or was never true. The claim that matters — zero callers — holds.)
    The integral-Surf-cell read-back proof it was written for is now enforced
    by ``_tol_cells.write_int_cell`` on the author path (``_author_tolerances``), which is
    what the integral-Surf-cell read-back test
    actually exercises. That test's head comment still calls THIS function "LOAD-BEARING"
    and claims deleting it would make the test falsely pass — both false; deleting it
    changes nothing, which is exactly why the claim survived. The behaviour is real and
    guarded; only the attribution is wrong.

    Kept (not deleted) because the removal belongs with that test's prose correction in
    ONE change, and the test file is outside this round's ownership.
    """
    try:
        actual_int = int(actual)
    except (TypeError, ValueError):
        actual_int = None
    if actual_int != int(intended):
        raise _tc.ToleranceError(
            f"{token}.{field} did not persist (intended {intended!r}, read back "
            f"{actual!r}); the tolerance authoring was a silent no-op",
            family="tolerancing_run",
        )


def _verify_float(field, intended, actual, token):
    """DEAD as of the cell-writer migration. ZERO callers in ``src/``.

    The sibling of ``_verify_int`` above and dead for the same reason: the double
    read-back proof is enforced by ``_tol_cells.write_double_verified`` on the author
    path.
    """
    try:
        actual_f = float(actual)
    except (TypeError, ValueError):
        actual_f = None
    if actual_f is None or not math.isclose(
        actual_f, float(intended), rel_tol=1e-9, abs_tol=1e-12
    ):
        raise _tc.ToleranceError(
            f"{token}.{field} did not persist (intended {intended!r}, read back "
            f"{actual!r}); the tolerance authoring was a silent no-op",
            family="tolerancing_run",
        )


def _strehl_nominal(session):
    """Read ONE nominal Strehl number ALONGSIDE (D8). NEVER fails the run.

    Calls the existing ``analyze_strehl`` path with ``best_focus=False`` (read-only,
    current image plane, NO mutation) and pulls the wave-1 STRH value. A read failure /
    a missing value degrades to ``(None, <warning>)`` — Strehl is an adjunct, not the
    deliverable (D8). Returns ``(strehl_value_or_None, warning_or_None)``.
    """
    try:
        result = _am.analyze_strehl(session, {"best_focus": False})
    except Exception as exc:  # noqa: BLE001 — D8: never let the adjunct fail the run
        return None, f"strehl_nominal read failed ({safe_exc(exc, repr_form=True)})"
    if not isinstance(result, dict) or not result.get("ok"):
        return None, "strehl_nominal unavailable (analyze_strehl returned no result)"
    at_image = result.get("at_image_plane") or []
    for entry in at_image:
        if isinstance(entry, dict) and entry.get("wave") == 1:
            value = entry.get("value")
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(value):
                return float(value), None
            return None, "strehl_nominal: the wave-1 STRH reading was non-finite"
    return None, "strehl_nominal: no wave-1 STRH reading was returned"


def _default_mechanical(system):
    """A conservative opt-in mechanical default set (D17/include_mechanical=True).

    A per-element surface tilt (TETX, surf-range over a cemented pair / single element)
    + decenter (TEDX) + irregularity (TIRR) on each glass-bearing interior surface. NOT
    auto-expanded into the standing default (D17) — it is built ONLY when
    ``include_mechanical=True``. Pure-python (reads the LDE); NEVER opens a tool. The
    TETX/TEDX surface RANGE is ``surf .. surf`` (a single-surface element span, the
    minimal conservative choice — the engine accepts surf1 < surf2 only for a multi-
    surface element; a single surface uses surf..surf+1 against the next interface).

    ROUND-13 -- RETURNS ``(entries, skipped)``, AND THE DOCSTRING'S "each" WAS FALSE.
    The claim above is a tilt + decenter + irregularity set on EACH glass-bearing
    interior surface. It is not: on the LAST interior surface the ``nxt`` clamp collapses
    to ``nxt = surf``, the ``nxt > surf`` guard is then False, and only TIRR is authored
    — that element ships with NO tilt and NO decenter tolerance, undisclosed. For n=6
    that is surface 4 (a cover glass or window in contact with the image plane: unusual,
    entirely legal, and exactly the element a tilt tolerance matters for).

    The clamp is NOT the bug and is deliberately kept: TETX/TEDX are surface-RANGE
    operands, ``_tolerance_common`` requires ``surface < surface2 < image_surf``, so for
    ``surf == n - 2`` there is no legal ``surface2`` at all — authoring one would be a
    hard ``tolerancing_param`` refusal that fails the whole run. Widening the span
    backwards (``surf-1 .. surf``) would tolerance a DIFFERENT element's tilt and report
    it as this one's, which is worse than the gap. So the fix is disclosure: ``skipped``
    carries the surfaces that got irregularity only, and the caller warns.
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — unreadable -> no mechanical default
        return [], []
    out = []
    skipped = []
    for surf in range(1, n - 1):
        material = _tc._surface_material(system, surf)
        is_glass = bool(material) and str(material).strip() != ""
        if not is_glass:
            continue
        nxt = surf + 1
        if nxt >= n - 1:
            nxt = surf  # degenerate: no legal surface2 exists (see the docstring)
        # TETX / TEDX are surface-RANGE ops (Surf1/Surf2); TIRR is single-surf.
        if nxt > surf:
            out.append({"type": "TETX", "surface": surf, "surface2": nxt,
                        "delta": 0.1})
            out.append({"type": "TEDX", "surface": surf, "surface2": nxt,
                        "delta": 0.05})
        else:
            skipped.append(surf)
        out.append({"type": "TIRR", "surface": surf, "delta": 1.0})
    return out, skipped


def _configure_run(tol, system, mode, trials, output_path):
    """Configure the tool's mode/criterion/trials + OutputFile, then RUN (steps 5/6).

    Resolves ``SetupMode`` / ``Criterion`` members via getattr (D6), sets MC
    ``NumberOfRuns``, disables data-retention saving (``SaveTolDataFile=False``, D15),
    ``makedirs`` the OutputFile parent (D15/#58), sets ``OutputFile`` BEFORE the run,
    runs ``RunAndWaitForCompletion()``, and reads ``Succeeded`` / ``ErrorMessage`` WHILE
    open (the engine-success canary, D11). Returns the echoed ``trials`` (re-read off
    ``NumberOfRuns``, never just the requested value, D12). Raises
    ``ToleranceError(family="tolerancing_run")`` on an engine-success canary miss.
    """
    setup_enum = _tc._tol_namespace_enum(system, "SetupModes")
    crit_enum = _tc._tol_namespace_enum(system, "Criterions")
    tol.SetupMode = _tc._resolve_tol_enum(setup_enum, _tc._MODE_TO_SETUP[mode])
    tol.Criterion = _tc._resolve_tol_enum(crit_enum, _tc._CRITERION_MEMBER)

    echoed_trials = None
    if mode == "monte_carlo":
        tol.NumberOfRuns = trials
        try:
            echoed_trials = int(tol.NumberOfRuns)  # echo from the live tool (D12)
        except Exception:  # noqa: BLE001 — fall back to the requested value
            echoed_trials = trials

    # D15: do not persist the .ZTD data-retention file (we still glob-reap defensively).
    try:
        tol.SaveTolDataFile = False
    except Exception:  # noqa: BLE001 — not all builds expose it; the reap is the net
        pass

    # D15/#58: the engine SILENTLY writes nothing to a missing dir -> makedirs first.
    # Guarded -> a real unwritable parent becomes a structured tolerancing_run envelope.
    try:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    except (OSError, ValueError) as exc:
        raise _tc.ToleranceError(
            f"could not create the parent directory for the report {output_path!r} "
            f"({safe_exc(exc, repr_form=True)})",
            family="tolerancing_run",
        )
    tol.OutputFile = output_path

    # RUN (synchronous, like the optimizer; probe §7).
    ran = bool(tol.RunAndWaitForCompletion())
    succeeded = bool(getattr(tol, "Succeeded", False))
    error_message = getattr(tol, "ErrorMessage", None)
    err_text = "" if error_message is None else str(error_message)
    # The engine-success canary (D11): read WHILE open, BEFORE parse.
    if not ran or not succeeded or (err_text and err_text.lower() not in ("", "none")):
        raise _tc.ToleranceError(
            f"the tolerancing run did not succeed (ran={ran}, Succeeded={succeeded}, "
            f"ErrorMessage={err_text!r}); nothing was parsed",
            family="tolerancing_run",
        )
    return echoed_trials


@_never_raise("tolerance")
def tolerance(session, params):
    """Run a tolerance analysis (sensitivity OR monte_carlo). See the module docstring.

    ``mode`` (REQUIRED) selects sensitivity (the both-sign asymmetric per-parameter
    table + worst offenders + an RSS estimate) or monte_carlo (mean/std/best/worst +
    cumulative-probability percentiles). ``tolerances`` (optional) is a list of
    ``{type, surface, delta}`` (omit for the default budget). ``trials`` (monte_carlo
    only, default 20) is the engine ``NumberOfRuns``. ``full`` (monte_carlo) includes
    the per-trial array. ``output_file`` (optional) overrides the temp report path.

    Gotcha: the criterion is the field-summed RMS WFE in waves; a nominal Strehl is
    reported ALONGSIDE (D8). monte_carlo is STRUCTURALLY non-deterministic
    (``deterministic:false`` / ``seed_supported:false`` — the engine exposes no seed,
    probe §5). The tool CLEARS the Tolerance Data Editor (author-as-scratch, D10). The
    LDE prescription is left untouched (a side-effect tripwire flags a regression).

    The FULL operand vocabulary (62 codes) is supported via the catalog table; pass a
    RESOLVED code (the agent grounds tolerance intent via ``lookup_operand`` with
    ``domain="tolerance"`` — the structured tolerance catalog — or ``search_reference``
    for chapter context). A
    CB/NSC-required operand (TUTX/TUDX/TNPS …) is a LABELED ``known_gap`` refusal (run
    the valid operands + disclose loud unless ``strict=True``); a crash-class operand
    (TNPA/TNMA) is a HARD refusal (the tool never opens). ``include_mechanical=True``
    adds a conservative per-element TETX/TEDX/TIRR set to the DEFAULT budget; ``strict``
    rejects the whole set on ANY gap. Never raises past the handler.
    """
    system = session.system

    # (1) the REQUIRED mode + optional knobs (bad value -> tolerancing_param).
    mode = _resolve_mode(params)
    trials = _resolve_trials(params)
    full = _bool_param(params, "full", False)
    strict = _bool_param(params, "strict", False)
    include_mechanical = _bool_param(params, "include_mechanical", False)
    warnings = []
    # A trials/full passed with mode=sensitivity is echoed-ignored + warned (D-vector
    # flat-required convention) — never a hard reject.
    if mode == "sensitivity":
        for key in ("trials", "full"):
            if dict.__contains__(params, key):  # unbound base slot
                warnings.append(
                    f"{key!r} was passed with mode='sensitivity' (it applies only to "
                    "monte_carlo); echoed-ignored"
                )

    # (1) pre-flight: build + validate the tolerance set (PURE; D14/G17). Open NOTHING.
    # unbound base slot. A lying ``get`` returning None here swaps the caller's
    # EXPLICIT tolerance budget for the auto-derived default one, silently.
    supplied = dict.get(params, "tolerances")
    if supplied is None:
        to_validate = _tc._default_tolerances(system)
        if include_mechanical:
            # OPT-IN conservative mechanical set (D17): a per-element surface tilt /
            # decenter / irregularity on each glass-bearing interior surface — EXCEPT
            # where the surface-RANGE operands have no legal surface2. That
            # element is under-toleranced, so say so rather than let a clean envelope
            # imply the whole mechanical budget was authored.
            mech, mech_skipped = _default_mechanical(system)
            to_validate = list(to_validate) + mech
            if mech_skipped:
                warnings.append(
                    "mechanical_default_partial: glass surface(s) "
                    + ", ".join(str(s) for s in mech_skipped)
                    + " carry only a TIRR irregularity tolerance — no TETX tilt and no "
                    "TEDX decenter. Those are surface-RANGE operands needing "
                    "surface < surface2 < image, and the next interface here IS the "
                    "image surface, so no legal range exists. That element's tilt and "
                    "decenter sensitivity is UNBOUNDED in this run; author an explicit "
                    "'tolerances' list if it matters."
                )
        if not to_validate:
            return _ac.error_envelope(
                "tolerance", "tolerancing_param",
                "no default tolerance budget could be built (the LDE has no powered or "
                "glass interior surface); supply an explicit 'tolerances' list",
            )
    else:
        to_validate = supplied
    authored, errors, validate_warnings, known_gaps = _tc.validate_tolerances(
        system, to_validate, strict=strict
    )
    warnings.extend(validate_warnings)
    if errors:
        return _ac.error_envelope(
            "tolerance", "tolerancing_param",
            "the tolerance set failed validation; see 'errors'. No tool was opened and "
            "nothing was authored.",
            errors=errors,
            known_gaps=known_gaps,
        )

    # (GRIN §1.3e) the GRIN Par# perturbation disclosures + the variable-gradient
    # coverage warning — computed post-validation PRE-run (both read-only; never raise).
    # Threaded ONLY into the SUCCESS + reconcile-ESCALATION envelopes (v2).
    grin_perturbation = _grin_perturbation_disclosures(system, authored)
    grin_coverage_warning = _grin_coverage_warning(system, authored)

    # preflight WARN-and-run: classify system.SystemFile. A non-blessed
    # backing file (empty / New-default / %TEMP% / a harness checkpoint) WARNS that the
    # run will likely produce no report (load_design a saved design) — never a refuse
    # (cond F: pre-run blessing is unpredictable; the post-run empty-report net is the
    # real guarantee).
    _sys_file = _tc._safe_system_file(system)
    _blessed_hint, _preflight_warn = _tc.classify_system_file(_sys_file)
    if _preflight_warn:
        warnings.append(_preflight_warn)

    # (2) the LDE side-effect snapshot (D16) — BEFORE any author/run.
    lde_before = _tc.snapshot_lde(system)

    operand_enum = _tc._tolerance_operand_enum(system)  # TYPE reach -> tolerancing_unavailable
    tde = system.TDE

    output_path, is_temp = _resolve_output_path(session, params)
    echoed_trials = None
    report_bytes = None
    tde_cleared = True
    try:
        # (3) TDE author-as-scratch (D10): clear (floor-at-1) then author each row.
        _clear_tde(tde)
        _author_tolerance_set(tde, operand_enum, authored)

        # (4) open the single slot (D13: None -> tolerancing_unavailable, nothing run).
        with _tc._tolerancing_session(system) as tol:
            # (5/6) configure + run; (6) engine-success canary read WHILE open.
            echoed_trials = _configure_run(tol, system, mode, trials, output_path)
        # (7) slot reaped by the context manager's finally. Read the report bytes off
        # disk NOW (it persists after Close), BEFORE the reap removes a temp report.
        report_bytes = _tc.read_report_bytes(output_path)
    finally:
        # (9) TDE clear in finally (D10/G18) — leave a clean floored editor. Track the
        # ACTUAL result so ``tde_cleared`` in the envelope is HONEST, never hardcoded: a
        # clear throw here -> tde_cleared:false + a warning (the editor may carry leftover
        # scratch rows; reload your .zmx before the next author).
        try:
            tde.DeleteAllRows()
        except Exception as exc:  # noqa: BLE001 — a TDE teardown throw must not mask body
            tde_cleared = False
            warnings.append(
                f"the Tolerance Data Editor could not be cleared after the run "
                f"({safe_exc(exc, repr_form=True)}); "
                "it may carry leftover scratch rows — reload your .zmx before re-authoring"
            )
        # (7/D15) reap the engine's .ZTD side files on EVERY path; reap the report only
        # when it is the tool's own temp (an explicit output_file is the caller's to keep).
        try:
            _tc.reap_tol_artifacts(system, output_path, reap_report=is_temp)
        except Exception:  # noqa: BLE001 — a reap throw must not mask the body
            pass

    # the EMPTY-REPORT safety-net (the load-bearing DISJOINT split, L30):
    # the run Succeeded (the engine-success canary in _configure_run already gated
    # ran/Succeeded) yet wrote a 0-byte report -> the system was not opened from a
    # blessed on-disk original (probe-frozen). Divert to a PRECISE
    # ``tolerancing_empty_report`` envelope BEFORE decode_tol_report. A NON-empty file
    # (short / wrong-BOM / garbage) still flows to the BOM-gate/canary -> the disjoint
    # ``tolerancing_parse`` diagnosis. The two acceptance sets are disjoint by len==0.
    if len(report_bytes or b"") == 0:
        return _ac.error_envelope(
            "tolerance", _tc._EMPTY_REPORT_FAMILY,
            "the headless tolerancing run succeeded but produced an empty (0-byte) "
            "report; nothing was parsed.",
            system_file=_tc._safe_system_file(system),
            blessed_for_tolerancing=_blessed_hint,
            ran=True,
            succeeded=True,
            report_size=0,
            recovery=_tc._EMPTY_REPORT_RECOVERY,
            warnings=warnings,
        )

    # (8) decode + parse the report (the canary; never zero-fill). Pure on the in-memory
    # bytes read above — a parse-canary RAISE surfaces AFTER the slot + TDE were reaped.
    # The G6 trial-count cross-check uses the ECHOED NumberOfRuns (read off the live tool),
    # NOT the requested trials (D11.4/D14): an engine that clamps N must not trigger a
    # false tolerancing_parse. Sensitivity ignores the count.
    cross_check_trials = (
        echoed_trials if (mode == "monte_carlo" and echoed_trials is not None)
        else trials
    )
    text = _tc.decode_tol_report(report_bytes)
    parsed = _tc.parse_tol_report(text, mode=mode, trials=cross_check_trials)
    # Surface any parser-side warnings (a dropped/incomplete sensitivity table, D-row).
    parser_warnings = parsed.get("warnings")
    if parser_warnings:
        warnings.extend(parser_warnings)

    # (10) LDE re-read + tripwire (D16).
    lde_after = _tc.snapshot_lde(system)
    state_mutated = not _tc.lde_unchanged(lde_before, lde_after)
    if state_mutated:
        warnings.append(
            "the tolerancing run unexpectedly mutated the LDE prescription; reload "
            "your .zmx (a future engine regression surfaced here)"
        )

    # (10b) the authored-vs-parsed RECONCILE canary (D12/G-RECON — the CENTERPIECE).
    # PURE partition: ran / refused / unaccounted. A SUPPORTED-tier unaccounted operand
    # (the engine silently omitted an authored op) escalates to tolerancing_reconcile
    # (ok:false) — the over-optimistic class. The invariant ok:true ⇒ unaccounted == [].
    # Reconcile keys on the per-operand SENSITIVITY table; in monte_carlo mode the report
    # has no per-operand row table, so reconcile is a no-op (the operand_errors are still
    # surfaced via the refused/unattributed channels).
    operand_errors = parsed.get("operand_errors") or []
    if mode == "sensitivity":
        recon = _tc.reconcile_authored_vs_parsed(authored, parsed)
    else:
        # monte_carlo has NO per-operand sensitivity table, so reconcile is structurally
        # a no-op (D12 note). BUG-3 honesty: an operand_error line that IS attributable to
        # an AUTHORED operand must NOT be mislabeled "not attributable" — partition the MC
        # error lines into refused (attributed to an authored op) vs unattributed (type=None
        # or a type we never authored) exactly as sensitivity reconcile would.
        authored_types = {e.get("type") for e in authored}
        mc_refused = []
        mc_unattributed = []
        for e in operand_errors:
            etype = e.get("type")
            if etype and etype in authored_types:
                mc_refused.append({"type": etype, "reason_line": e["line"]})
            else:
                mc_unattributed.append(e["line"])
        recon = {"ran": [], "refused": mc_refused, "unaccounted": [],
                 "escalate": False, "unattributed_errors": mc_unattributed}
    # An attributed engine error (a known authored op the engine refused at run) is
    # surfaced HONESTLY as a refusal, not as "not attributable".
    for r in recon.get("refused", []):
        warnings.append(
            f"the engine refused authored operand {r.get('type')!r} at run "
            f"(surfaced not swallowed): {r.get('reason_line')!r}"
        )
    for line in recon.get("unattributed_errors", []):
        warnings.append(
            f"an engine error line was not attributable to any authored operand "
            f"(surfaced not swallowed): {line!r}"
        )
    # A sensitivity row that could not be keyed to a specific authored
    # operand (an unreadable Surf column, or a dropped-corrupt row) is a PARSE ANOMALY —
    # surface it loud (it does NOT mark any authored op ran; the op it would have satisfied
    # then escalates fail-closed).
    for anom in recon.get("anomalous_rows", []):
        warnings.append(
            f"a sensitivity row could not be reconciled to a specific authored operand "
            f"(surfaced not swallowed): {anom.get('detail')!r}"
        )
    # A keyed parsed row matching no authored (type, surface) is an
    # engine/author desync — surfaced, never silently dropped.
    for extra in recon.get("unexpected_parsed_rows", []):
        warnings.append(
            f"a parsed sensitivity row matched no authored operand "
            f"(engine/author desync, surfaced not swallowed): {extra!r}"
        )
    # (GRIN §1.3e) surface the count-fallback disclosure AND the matched-row
    # error lines ABOVE the escalation return — a MIXED result (one group count-fallback +
    # another escalating) must NOT drop these warnings into the void (the escalation
    # envelope already threads warnings=warnings).
    for w in recon.get("par_unresolved", []):
        warnings.append(w)
    for me in recon.get("matched_operand_errors", []):
        warnings.append(
            f"the engine emitted an error line for authored operand {me.get('type')!r} "
            f"whose row ALSO reconciled (surfaced not swallowed): "
            f"{me.get('reason_line')!r}"
        )
    if recon.get("escalate"):
        # (GRIN §1.3e, BUG-COV-ESC) the coverage warning rides BOTH the SUCCESS
        # AND the reconcile-ESCALATION envelopes — a variable GRIN gradient with no coefficient
        # TPAR (unbounded fabrication sensitivity) must NOT be silently lost when the run ALSO
        # escalates. Conditional top-level key (matching _build_envelope's non-None posture).
        escalation_extra = {}
        if grin_coverage_warning is not None:
            escalation_extra["grin_tolerance_coverage_warning"] = grin_coverage_warning
        return _ac.error_envelope(
            "tolerance", _tc._RECONCILE_FAMILY,
            "one or more SUPPORTED authored operands produced NEITHER a sensitivity row "
            "NOR an engine error line (the engine silently omitted them); refusing the "
            "over-optimistic verdict.",
            unaccounted_operands=recon["unaccounted"],
            ran_operands=recon["ran"],
            refused_operands=recon["refused"],
            operand_errors=operand_errors,
            authored_operands=_authored_summary(authored),
            grin_perturbation=grin_perturbation,
            reconciled_operand_keys=recon.get("reconciled_operand_keys", []),
            reconciliation_mode=recon.get("reconciliation_mode"),
            warnings=warnings,
            **escalation_extra,
        )

    # (MCE) the Config#-vs-current-config WARN (tmco_config_mismatch) — the
    # silent-zero guard: the headless sensitivity run analyzes ONLY the current config
    # (probe Q12). A TMCO whose mce_config != the run's CurrentConfiguration reads a
    # SILENT ZERO sensitivity (it RAN, it appears in the report, it just bit nothing at
    # the analyzed config). NOT a refusal (a multi-config campaign legitimately authors
    # rows across configs); it is a loud WARN naming the recovery.
    _emit_tmco_config_mismatch_warnings(system, authored, warnings)

    # (11) Strehl alongside (D8) — nominal, separate read; null + warn on failure.
    strehl_nominal, strehl_warning = _strehl_nominal(session)
    if strehl_warning:
        warnings.append(strehl_warning)

    # (12) build the envelope.
    return _build_envelope(
        mode, parsed, authored, trials, echoed_trials, full, strehl_nominal,
        state_mutated, warnings, output_path, tde_cleared, recon, known_gaps,
        grin_perturbation=grin_perturbation,
        grin_coverage_warning=grin_coverage_warning,
    )


def _current_configuration_guarded(system):
    """Read ``system.MCE.CurrentConfiguration`` guarded -> ``None`` on a fault.

    The headless tolerancing run analyzes only the current config (Q12); this is the
    config the TMCO Config#-mismatch WARN is measured against. A non-MCE / wedged read
    degrades to ``None`` (the WARN is then skipped — disclosure, never load-bearing).
    """
    try:
        return int(system.MCE.CurrentConfiguration)
    except Exception:  # noqa: BLE001 — a non-MCE / wedged read -> no mismatch WARN
        return None


def _emit_tmco_config_mismatch_warnings(system, authored, warnings):
    """Append a ``tmco_config_mismatch`` WARN per authored TMCO whose Config# != current.

    The silent-zero guard (MCE): a headless sensitivity run analyzes ONLY
    the current config, so a TMCO whose ``mce_config`` != the run's CurrentConfiguration
    reads a SILENT ZERO sensitivity (it ran + appears in the report, it just bit nothing
    at the analyzed config). NOT a refusal — a TMCO whose Config# == current is NORMAL
    (bites, no warn). A non-MCE / wedged config read skips the check (no false WARN).
    """
    tmco_entries = [
        e for e in (authored or [])
        if isinstance(e, dict) and e.get("type") == "TMCO"
    ]
    if not tmco_entries:
        return
    current = _current_configuration_guarded(system)
    if current is None:
        return
    for e in tmco_entries:
        cfg = e.get("mce_config")
        row = e.get("mce_row")
        if cfg is not None and int(cfg) != current:
            warnings.append(
                f"tmco_config_mismatch: TMCO row {row} targets config {cfg} but the "
                f"tolerancing run analyzed config {current} (the headless engine "
                "analyzes only the current config); this TMCO reads a SILENT ZERO "
                f"sensitivity — set_current_configuration({cfg}) and re-run, or author "
                f"the TMCO for config {current}."
            )


def _authored_summary(authored):
    """The compact per-operand authored summary for the envelope (D12/§3.2)."""
    out = []
    for e in authored:
        row = {"type": e.get("type"), "surface": e.get("surface"),
               "tier": e.get("tier"), "units": e.get("units")}
        if e.get("surface2") is not None:
            row["surface2"] = e["surface2"]
        # GRIN display carry: name the Par# so the escalation envelope's
        # authored_operands / unaccounted_operands identify the coefficient.
        if e.get("param") is not None:
            row["param"] = e["param"]
        out.append(row)
    return out


def _grin_quantity_for_par(info, par):
    """Map an integer Par# to its quantity dict via the RESOLVED ``GrinTypeInfo.params``.

    (GRIN §1.3b) Walks ``info.params`` rows ``(token, "ParN", header, dtype, kind,
    power)``:
      token "n0"        -> base_index_n0
      kind == "coeff"   -> index_profile_coefficient (coefficient = the live Header)
      else (Delta T)    -> trace_step_delta_t (a numerical trace step, NOT an
                           index-profile quantity; never counts toward gradient coverage)
    A Par# beyond THIS type's table -> grin_par_unmapped (disclosed, never silent).
    """
    target = f"Par{par}"
    for row in info.params:
        token = row[0]
        par_str = row[1]
        header = row[2]
        kind = row[4]
        if par_str != target:
            continue
        if token == "n0":
            return {"quantity": "base_index_n0", "par": par, "coefficient": "n0"}
        if kind == "coeff":
            return {"quantity": "index_profile_coefficient", "par": par,
                    "coefficient": header}
        return {"quantity": "trace_step_delta_t", "par": par, "coefficient": header,
                "note": "numerical trace step, NOT an index-profile quantity; does not "
                        "count toward gradient coverage"}
    return {"quantity": "grin_par_unmapped", "par": par}


def _grin_perturbation_disclosures(system, authored):
    """One disclosure entry per authored GRIN Par# perturbation (GRIN §1.3c).

    Returns a list, NEVER raises. One entry per authored op passing the SHARED
    ``is_param_perturbation_op`` predicate (NOT ``parser_secondary_key``,
    which also selects TEDV/CPAR/CEDV/CNPA; a control/compensator with a supplied
    ``param`` must NEVER earn a ``grin_perturbation`` entry) whose LDE row resolves GRIN
    via ``_grin_cells.grin_type_of`` (the AUTHORABLE resolver — the disclosure needs the
    per-type cell map). Non-GRIN -> no entry; a per-op read throw -> skip that op (never
    a false GRIN claim, never fails the run).
    """
    out = []
    for e in authored:
        if not isinstance(e, dict):
            continue
        token = e.get("type")
        if not _cat.is_param_perturbation_op(token):
            continue
        param = e.get("param")
        if param is None:
            continue
        surface = e.get("surface")
        try:
            from . import _grin_cells as _grin  # lazy — circular-import avoidance
            row = system.LDE.GetSurfaceAt(surface)
            key = _grin.grin_type_of(row)     # the resolved GRIN_TYPE_INFO KEY (or None)
            info = _grin.GRIN_TYPE_INFO.get(key) if key is not None else None
        except Exception:  # noqa: BLE001 — a per-op read throw -> skip (no false claim)
            continue
        if info is None:
            continue                       # non-GRIN surface -> no entry
        entry = dict(_grin_quantity_for_par(info, param))
        entry["surface"] = surface
        entry["type"] = token
        out.append(entry)
    return out


# (GRIN §1.3d) the INDETERMINATE coverage string — a NON-EMPTY faults list
# of ANY source (or the inventory throwing) can never read as clean: a shared LDE
# GetSurfaceAt(i) throw is recorded source:"asphere" yet the `continue` skips the GRIN walk
# for that surface too, so a "grin"-scoped filter reads a false clean over an unenumerated
# GRIN surface. The any-fault rule costs nothing (the warning is advisory).
_GRIN_COVERAGE_INDETERMINATE = (
    "grin_tolerance_coverage: the variable inventory could not be fully enumerated; "
    "gradient tolerance coverage is INDETERMINATE for this run — re-run after resolving "
    "the enumeration fault, or author TPAR per optimized GRIN coefficient to be safe."
)


def _grin_surface_covered(system, authored, surface):
    """True iff an authored TPAR bounds a gradient coefficient on ``surface`` (§1.3d).

    Covered iff an authored **TPAR** (TPAI does NOT certify — the brief-literal trigger)
    with ``surface==surface`` has a ``param`` the RESOLVED GRIN map classifies
    ``index_profile_coefficient`` (map-driven via ``_grin_quantity_for_par`` — no {3..8}
    literal; n0 / Delta-T / unmapped never cover). A Type-resolve throw -> not covered
    (advisory-safe: over-warn). NEVER raises.
    """
    try:
        from . import _grin_cells as _grin  # lazy — circular-import avoidance
        row = system.LDE.GetSurfaceAt(surface)
        key = _grin.grin_type_of(row)         # the resolved GRIN_TYPE_INFO KEY (or None)
        info = _grin.GRIN_TYPE_INFO.get(key) if key is not None else None
    except Exception:  # noqa: BLE001 — cannot resolve -> not covered (advisory over-warn)
        return False
    if info is None:
        return False
    for e in authored:
        if not isinstance(e, dict):
            continue
        token = e.get("type")
        # coverage is the FOURTH consumer of the authorable-perturbation
        # predicate — gate on ``is_param_perturbation_op`` (NOT a hand-coded token literal)
        # so a catalog drift (a TPAR row that loses has_minmax / drifts to a control tier)
        # no longer falsely certifies coverage. KEEP the TPAR-only rule (TPAI does NOT
        # certify — the brief-literal trigger) AND inherit the catalog-drift safety.
        if token != "TPAR" or not _cat.is_param_perturbation_op(token):
            continue
        if e.get("surface") != surface:
            continue
        param = e.get("param")
        if param is None:
            continue
        if _grin_quantity_for_par(info, param).get("quantity") \
                == "index_profile_coefficient":
            return True
    return False


def _grin_coverage_warning(system, authored):
    """The ``grin_tolerance_coverage_warning`` string, or None (GRIN §1.3d).

    NEVER raises. Universe: ``_optimize_common._variable_inventory(system, faults=faults)``
    items ``source=="grin"`` (reuse) -> the surfaces with a VARIABLE (optimized)
    index profile. Fault posture (ANY-source fail-closed): a NON-EMPTY faults
    list of ANY source, OR the inventory call itself throwing, -> the INDETERMINATE
    string, NEVER a silent None. Clean (no grin items, no fault) -> None. Uncovered
    surfaces -> a string NAMING each uncovered surface.

    ROUND-13 -- THE OUTER NET NOW FAILS CLOSED TOO. It was ``except Exception: return
    None``, and ``None`` on this channel ALSO means "fully covered" — so a fault in the
    body (the lazy import, the set comprehension, ``_grin_surface_covered``, the join)
    produced a silent all-clear on the exact question the function exists to answer, in
    direct contradiction of the fault posture two paragraphs up. Both siblings already
    fail closed: ``optimize_run._grin_index_audit_warnings`` refuses ``{}`` on a
    total-body throw and returns a static ``grin_index_audit_failed``, and
    ``_grin_surface_covered`` errs toward UNCOVERED. This one now matches them.
    """
    try:
        from . import _optimize_common as _oc  # lazy — circular-import avoidance
        faults = []
        try:
            inv = _oc._variable_inventory(system, faults=faults)
        except Exception:  # noqa: BLE001 — an inventory throw -> INDETERMINATE
            return _GRIN_COVERAGE_INDETERMINATE
        if faults:
            return _GRIN_COVERAGE_INDETERMINATE   # ANY-source fault -> INDETERMINATE
        grin_surfaces = sorted({
            it.get("surface") for it in inv
            if isinstance(it, dict) and it.get("source") == "grin"
            and it.get("surface") is not None
        })
        if not grin_surfaces:
            return None                            # no variable GRIN -> nothing to warn
        uncovered = [s for s in grin_surfaces
                     if not _grin_surface_covered(system, authored, s)]
        if not uncovered:
            return None
        names = ", ".join(str(s) for s in uncovered)
        return (
            f"grin_tolerance_coverage: the optimized (variable) index gradient on "
            f"surface(s) {names} has NO gradient-coefficient TPAR (Par#=3..8) tolerance "
            "authored; the fabrication sensitivity of the index gradient is UNBOUNDED in "
            "this run — author TPAR(surface=S, param=<Par#>) per optimized coefficient "
            "(Par2=n0 is the base index; Par3..Par8 are the profile coefficients)."
        )
    except Exception:  # noqa: BLE001 — never raise; but INDETERMINATE, never a clean None
        return _GRIN_COVERAGE_INDETERMINATE


def _units_by_family(authored):
    """Map each authored FAMILY -> its unit, ONLY where that family AGREES (H-1).

    Returns ``(agreed, ambiguous)``: ``agreed`` is ``{family: unit}`` for the families
    whose authored operands all carry the SAME catalog unit; ``ambiguous`` is
    ``{family: [sorted units]}`` for those that do not.

    ROUND-13 H-1 -- A FAMILY DOES NOT DETERMINE A UNIT, AND THE OLD ROLLUP PICKED BY
    AUTHORING ORDER. It was ``out.setdefault(fam, e["units"])``, so whichever operand of
    a family happened to be authored FIRST won. Measured against the frozen catalog, SIX
    families are ambiguous, not one:

      scalar                 lens_units | dimensionless | fringes   (TRAD/TTHI · TABB/
                                                                     TCON/TCUR/TIND · TFRN)
      surface_tilt_decenter  degrees | lens_units                   (TETX/Y/Z · TEDR/X/Y)
      roll                   degrees | lens_units                   (TARR/X/Y · TRLR/X/Y)
      sag                    degrees | lens_units                   (TSTX/Y · TSDI/R/X/Y)
      parameter              dimensionless | None                   (TPAI/TPAR · TEDV)

    and BOTH default paths walk straight into it: ``_default_tolerances`` authors TRAD +
    TTHI per powered surface and TIND + TABB per glass (all family ``scalar``), while
    ``_default_mechanical`` authors TETX (degrees) beside TEDX (lens_units). So every
    glass system got a wrong physical unit for a family, silently, with ``ok:true``.

    A wrong unit is worse than an absent one, so an ambiguous family is now OMITTED from
    ``units_by_family`` and DISCLOSED under ``units_ambiguous_families``. The
    unambiguous answer was already in the envelope and still is: ``authored_operands[i]
    ["units"]`` is per-OPERAND and always correct — that is the field a consumer should
    read, and the rollup is only ever a convenience over it.
    """
    seen = {}
    for e in authored:
        fam = e.get("family")
        if fam is not None and e.get("units") is not None:
            seen.setdefault(fam, set()).add(e["units"])
    agreed = {fam: sorted(units)[0] for fam, units in seen.items() if len(units) == 1}
    ambiguous = {fam: sorted(units) for fam, units in seen.items() if len(units) > 1}
    return agreed, ambiguous


def _build_envelope(mode, parsed, authored, trials, echoed_trials, full,
                    strehl_nominal, state_mutated, warnings, output_path,
                    tde_cleared=True, recon=None, known_gaps=None,
                    grin_perturbation=None, grin_coverage_warning=None):
    """Build the SENSITIVITY / MONTE_CARLO result envelope (D8/D9/D11/D12/D13/D16)."""
    recon = recon or {"ran": [], "refused": [], "unaccounted": []}
    units_agreed, units_ambiguous = _units_by_family(authored)
    base = {
        "ok": True,
        "tool": "tolerance",
        "mode": mode,
        "criterion": "RMSWavefront",
        "criterion_units": "waves",
        "compensation": "paraxial_back_focus",
        # D13/G-COMP-INERT: a user compensator is INERT headless — ALWAYS false.
        "compensator_participates": False,
        # The reported criterion ALREADY reflects per-perturbation paraxial back-focus
        # refocus (the engine default for a sensitivity/MC run). Derived-constant-true:
        # disclosed so a design agent cannot DOUBLE-COUNT back focus by reading
        # compensator_participates=False as "the numbers are un-refocused, so as-built is
        # better". (Probe: the TTHI+0.3 criterion matches the BEST-FOCUS value, the
        # back-focus-change block is non-zero, and the perturbed criterion can sit BELOW
        # nominal — impossible at a fixed image plane.)
        "criterion_includes_back_focus_refocus": True,
        "compensation_note": (
            "The reported RMS-WFE criterion ALREADY includes per-perturbation paraxial "
            "back-focus refocus (engine default). compensator_participates=false means "
            "ONLY user-defined COMP/CPAR operands are inert headless; it does NOT mean "
            "the criterion is un-refocused. Do NOT assume as-built performance is better "
            "than these numbers on the back-focus axis. Additional compensators (air "
            "gaps, element tilt/decenter) are NOT applied today (back-focus only)."
        ),
        # D16/G-UNITS-ECHO: echoed from the report "Units are ..." line (never "mm").
        "lens_units": parsed.get("lens_units"),
        "nominal_criterion": safe_float(parsed["nominal_criterion"]),
        "strehl_nominal": safe_float(strehl_nominal) if strehl_nominal is not None
        else None,
        "state_mutated": state_mutated,
        "tde_cleared": tde_cleared,
        "warnings": warnings,
        "report_path": output_path,
        # D12/§3.2 — the no-blind-spot reconcile fields. The INVARIANT: ok:true =>
        # unaccounted_operands == [] (escalation already returned ok:false above).
        "authored_operands": _authored_summary(authored),
        "ran_operands": recon.get("ran", []),
        "refused_operands": recon.get("refused", []),
        "unaccounted_operands": recon.get("unaccounted", []),
        "operand_errors": parsed.get("operand_errors") or [],
        "known_gaps": known_gaps or [],
        # ROUND-13 H-1: only the families whose authored operands AGREE on a unit.
        # A family with a mixed unit set is OMITTED here and named below — the old
        # rollup answered by AUTHORING ORDER, which made every glass default run
        # report a wrong physical unit for family "scalar". Per-operand units are
        # in ``authored_operands`` and are always right.
        "units_by_family": units_agreed,
        "units_ambiguous_families": units_ambiguous,
        # (GRIN §1.3f) the GRIN Par# perturbation disclosures + the per-op reconcile
        # proof fields. ``grin_perturbation`` + coverage are authored-set-derived (both
        # modes); ``reconciliation_mode`` is None in MC (no per-op table — reconcile is a
        # structural no-op there).
        "grin_perturbation": grin_perturbation or [],
        "reconciled_operand_keys": recon.get("reconciled_operand_keys", []),
        "reconciliation_mode": (
            recon.get("reconciliation_mode") if mode == "sensitivity" else None
        ),
    }
    # (GRIN §1.3f) the coverage warning is a CONDITIONAL top-level key (the
    # glass_floor_warning posture) — present ONLY when non-None, never flips ok.
    if grin_coverage_warning is not None:
        base["grin_tolerance_coverage_warning"] = grin_coverage_warning

    if mode == "sensitivity":
        base["sensitivity"] = parsed["sensitivity"]
        base["worst_offenders"] = parsed.get("worst_offenders", [])
        base["rss_estimated_change"] = safe_float(parsed.get("rss_estimated_change"))
        base["rss_estimated_criterion"] = safe_float(
            parsed.get("rss_estimated_criterion")
        )
        base["back_focus_change"] = parsed.get("back_focus_change")
        return base

    # monte_carlo: STRUCTURAL determinism honesty (D12 — always present, always false).
    base["trials"] = int(echoed_trials) if echoed_trials is not None else int(trials)
    base["deterministic"] = False
    base["seed_supported"] = False
    base["monte_carlo"] = parsed["monte_carlo"]
    if base["trials"] < _tc._MIN_STABLE_TRIALS:
        warnings.append(
            f"trials={base['trials']} (< {_tc._MIN_STABLE_TRIALS}): the Monte-Carlo "
            "statistics may be unstable; increase trials for a reliable estimate"
        )
    if full:
        base["per_trial"] = parsed.get("per_trial", [])
    return base


TOLERANCE_SPEC = ToolSpec(
    name="tolerance",
    handler=tolerance,
    required_params=("mode",),
    param_types={
        "mode": "string",
        "tolerances": "array",
        "trials": "number",
        "full": "boolean",
        "output_file": "string",
        "include_mechanical": "boolean",
        "strict": "boolean",
    },
    description=(
        "Run a tolerance analysis: mode='sensitivity' gives the per-parameter both-sign "
        "RMS-wavefront sensitivity (+ worst offenders + an RSS estimate); "
        "mode='monte_carlo' gives mean/std/best/worst + cumulative-probability "
        "percentiles. tolerances is a list of {type:<TDE code>, surface, surface2?, "
        "code?, param?, roll_surf?, delta>0 | min+max}; the FULL operand vocabulary "
        "(radius/thickness/index/abbe, surface+element tilt/decenter, irregularity, "
        "roll, ISO, ...) is supported — pass a RESOLVED code (ground tolerance intent "
        "via lookup_operand with domain='tolerance', the structured tolerance catalog, "
        "or search_reference for chapter context; calling lookup_operand without "
        "domain='tolerance' mis-routes to merit operands; code-not-phrase). Range ops "
        "need surface2; TEZI/TEXI (Zernike form error) additionally REQUIRE "
        "max_term+min_term (1-based Zernike-term indices, min<=max) — omitting them is "
        "refused (no inert 0/0 range). Deltas are "
        "symmetric (Min=-delta/Max=+delta) or override with min+max. Units are per "
        "OPERAND (tilt=degrees, decenter/thickness/radius=lens_units, "
        "irregularity=fringes, index=dimensionless) and echoed per row in "
        "authored_operands[].units — read THAT. units_by_family is a convenience rollup "
        "and carries only the families whose authored operands agree on one unit; a "
        "family that mixes them (e.g. scalar holds TTHI=lens_units beside "
        "TIND=dimensionless) is omitted and named in units_ambiguous_families. omit "
        "tolerances for a default budget; include_mechanical=true adds a conservative "
        "tilt/decenter/irregularity set. Gotcha: a CB/NSC-required operand "
        "(TUTX/TUDX/TNPS) is a labeled known_gap refusal (run the rest + disclose unless "
        "strict=true); a crash-class operand (TNPA/TNMA) is a HARD refusal (the tool "
        "never opens); monte_carlo is non-deterministic (deterministic:false / "
        "seed_supported:false); the reported criterion ALREADY includes per-perturbation "
        "paraxial back-focus refocus (engine default) — compensator_participates:false "
        "means only user COMP/CPAR operands are inert, NOT that the numbers are "
        "un-refocused (back-focus is the only compensator applied today; do not tell the "
        "user as-built is better on the back-focus axis). This CLEARS the Tolerance Data "
        "Editor; the "
        "lens prescription is left untouched. Gotcha: tolerance a design LOADED from "
        "disk via load_design — a freshly-built/just-applied in-memory system produces "
        "an empty report (tolerancing_empty_report; the engine blesses only a saved "
        ".zmx loaded fresh from its own path). GRIN: TPAR/TPAI take a REQUIRED 'param' "
        "(the 1-based Par#); on a GRIN surface n0 is param=2 and the profile "
        "coefficients are param=3..8 (each perturbation is disclosed in "
        "grin_perturbation with its quantity/coefficient); a variable GRIN gradient with "
        "no TPAR coefficient tolerance raises grin_tolerance_coverage_warning. TIND/TABB "
        "refuse a bare-cell GRIN surface (AIR) — use TPAR(param=2) for the base index. "
        "See load_design, optimize, analyze_strehl, analyze_wavefront."
    ),
)

TOOL_SPECS = (TOLERANCE_SPEC,)
