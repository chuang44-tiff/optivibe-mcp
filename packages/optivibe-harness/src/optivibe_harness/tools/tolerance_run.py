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

from .._io import safe_float
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


def _never_raise(tool_name):
    """Wrap the handler so it NEVER raises past its boundary (D17).

    A ``ToleranceError`` keeps its intended structured ``family`` (one of the four
    frozen families). ANY other ``Exception`` (pythonnet maps every .NET throw onto an
    ``Exception`` subclass) is netted to a typed ``tolerancing_run`` envelope so a
    disconnected engine mid-run becomes ``{ok:false}`` rather than a crash.
    ``RecursionError`` is in the caught set (the wrapper must never raise).
    ``BaseException`` (KeyboardInterrupt / SystemExit) is deliberately NOT caught — it
    propagates AFTER the slot-reaping ``finally`` (D13) has run.
    """
    def _decorate(handler):
        @functools.wraps(handler)
        def _wrapped(session, params):
            try:
                return handler(session, params)
            except _tc.ToleranceError as exc:
                return _ac.error_envelope(
                    tool_name, getattr(exc, "family", "tolerancing"), str(exc)
                )
            except Exception as exc:  # noqa: BLE001 — D17: net any engine throw
                return _ac.error_envelope(
                    tool_name, _ENGINE_ERROR_FAMILY,
                    f"{tool_name} hit an unexpected engine error: {exc}",
                )
        return _wrapped
    return _decorate


def _resolve_mode(params):
    """Resolve + validate the REQUIRED ``mode`` param (D1)."""
    mode = params.get("mode")
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
    """
    if "trials" not in params:
        return _tc._DEFAULT_TRIALS
    value = params["trials"]
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
    """Pull an optional bool param; reject a non-bool (loud)."""
    if key not in params:
        return default
    value = params[key]
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
    explicit = params.get("output_file")
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
    """Clear the TDE via ``DeleteAllRows`` (author-as-scratch, floor-at-1; D10/G18).

    Guarded: a clear THROW raises ``ToleranceError(family="tolerancing_run")`` so the
    handler nets it (the slot/TDE reaping happens in the caller's ``finally``).
    """
    try:
        tde.DeleteAllRows()
    except Exception as exc:  # noqa: BLE001 — a clear THROW -> tolerancing_run
        raise _tc.ToleranceError(
            f"clearing the Tolerance Data Editor threw ({exc!r}); refusing to author "
            "onto an indeterminate TDE",
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
    Min/Max are written as typed Double properties (``op.Min``/``op.Max`` — probe-proven
    to persist) and read-back-verified. A control/compensator/structural op (no
    ``min``/``max`` in its validated entry) authors its int cells ONLY (no perturbation).

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
                f"could not resolve the int-cell author plan for {token} ({exc}); the "
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
    """Assert an integer property read back as intended (G10 integral-Surf-cell proof)."""
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
    """Assert a double property read back as intended within float tolerance."""
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
        return None, f"strehl_nominal read failed ({exc!r})"
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
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — unreadable -> no mechanical default
        return []
    out = []
    for surf in range(1, n - 1):
        material = _tc._surface_material(system, surf)
        is_glass = bool(material) and str(material).strip() != ""
        if not is_glass:
            continue
        nxt = surf + 1
        if nxt >= n - 1:
            nxt = surf  # degenerate: a single-surface span (surf..surf)
        # TETX / TEDX are surface-RANGE ops (Surf1/Surf2); TIRR is single-surf.
        if nxt > surf:
            out.append({"type": "TETX", "surface": surf, "surface2": nxt,
                        "delta": 0.1})
            out.append({"type": "TEDX", "surface": surf, "surface2": nxt,
                        "delta": 0.05})
        out.append({"type": "TIRR", "surface": surf, "delta": 1.0})
    return out


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
            f"({type(exc).__name__}: {exc})",
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
            if key in params:
                warnings.append(
                    f"{key!r} was passed with mode='sensitivity' (it applies only to "
                    "monte_carlo); echoed-ignored"
                )

    # (1) pre-flight: build + validate the tolerance set (PURE; D14/G17). Open NOTHING.
    supplied = params.get("tolerances")
    if supplied is None:
        to_validate = _tc._default_tolerances(system)
        if include_mechanical:
            # OPT-IN conservative mechanical set (D17): a per-element surface tilt /
            # decenter / irregularity on each glass-bearing interior surface.
            to_validate = list(to_validate) + _default_mechanical(system)
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
                f"the Tolerance Data Editor could not be cleared after the run ({exc!r}); "
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
    if recon.get("escalate"):
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
            warnings=warnings,
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
        out.append(row)
    return out


def _units_by_family(authored):
    """Map each authored operand's FAMILY -> its catalog unit (D16/§3.2)."""
    out = {}
    for e in authored:
        fam = e.get("family")
        if fam is not None and e.get("units") is not None:
            out.setdefault(fam, e["units"])
    return out


def _build_envelope(mode, parsed, authored, trials, echoed_trials, full,
                    strehl_nominal, state_mutated, warnings, output_path,
                    tde_cleared=True, recon=None, known_gaps=None):
    """Build the SENSITIVITY / MONTE_CARLO result envelope (D8/D9/D11/D12/D13/D16)."""
    recon = recon or {"ran": [], "refused": [], "unaccounted": []}
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
        "units_by_family": _units_by_family(authored),
    }

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
        "family (tilt=degrees, decenter/thickness/radius=lens_units, "
        "irregularity=fringes, index=dimensionless) and echoed in units_by_family. omit "
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
        ".zmx loaded fresh from its own path). See load_design, optimize, "
        "analyze_strehl, analyze_wavefront."
    ),
)

TOOL_SPECS = (TOLERANCE_SPEC,)
