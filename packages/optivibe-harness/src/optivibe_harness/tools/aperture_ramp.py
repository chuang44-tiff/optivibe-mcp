"""tools/aperture_ramp.py — ``ramp_aperture``.

ONE dispatchable tool that ramps the system aperture (EPD or NA) toward a target,
re-optimizing at each step and AUTO-HALVING the step when ``optimize`` reports the
merit is uncomputable (a corner ray cannot trace at the widened pupil), until it
reaches the target OR floors below ``min_step`` at the last-traceable ceiling.

Pure orchestration over the IN-PROCESS handlers ``set_aperture`` + ``optimize``
(+ opt-in ``save_candidate``) — NO new ZOS mechanism, NO new exception class.

THE load-bearing gotcha (probe-verified, optimize_run.py:83-97): a DIRECT in-process
call to the ``optimize``/``set_aperture`` handlers returns a **FLAT** envelope —
``{ok:False, error_family:"optimize_merit_uncomputable", merit:...}`` at the TOP
level, NOT nested under ``result`` (the ``result``-nesting the probe first saw was the
MCP dispatch-boundary view). So this loop reads ``r["ok"]`` / ``r["error_family"]``
FLAT. A reader that looked at ``r["result"]["ok"]`` would never detect the ceiling.

Ceiling recovery leaves the design at the LAST traceable, fully-optimized aperture
(``last_good``) via a TWO-TIER revert: a ZERO-mutation optimize failure
(``optimize_merit_uncomputable`` ceiling / a preflight refusal — the DLS never ran)
reverts only the aperture; but a CYCLES-RUN failure (error_family ``optimize_run_failed``
OR verdict ``diverged`` — the DLS iterated + MUTATED the LDE geometry before aborting,
optimize_run.py:763-773) additionally RESTORES the geometry from a per-trial
SaveAs->LoadFile checkpoint (read-back proven), so ``final_value``/``final_merit`` never
silently misreport a live geometry that is the failed iterate. A restore that itself
fails is disclosed LOUDLY (``geometry_uncertain``), never a clean last_good. The
intermediate re-optimizations (each relaxing criticality for the next step) ARE the
point; a full rollback of the accepted work would discard it.

Precondition (like ``optimize``): variables + a merit are already set. The handler
NEVER raises past its boundary (a module-local body ``try/except -> error_envelope``,
carrying the partial ``ramp_trace`` + ``last_good`` so nothing is lost).

Live ZOS-API integration: exercised by a MODEST EPD ramp to a real ceiling; unit-tested
against the shared-state FLAT-envelope fakes (the mock-must-match-live guard).
"""
import glob
import math
import os
import tempfile

from ..server import ToolSpec
from . import _optimize_common as _oc

# Module-level handler aliases (bound at import) so a test can monkeypatch the
# in-process call surface on THIS module. The handler body references these as
# module globals (resolved at call time), so a patch on the module attribute takes.
from .lens_system import set_aperture as _set_aperture  # noqa: E402
from .optimize_run import optimize as _optimize  # noqa: E402
from .workspace import save_candidate as _save_candidate  # noqa: E402

# The non-mutating preflight gate (variables + a merit present). Aliased for the same
# monkeypatch reason; the precondition check is best-effort (a throw -> proceed).
_preflight = _oc._preflight

# The inline error family (NO new exception class — the variable_lifecycle precedent).
_FAMILY = "ramp_aperture"

# The target-reached tolerance (aperture units; EPD mm / NA are both O(1..100)).
_EPS = 1e-9
# The default + hard cap for the bounded loop.
_DEFAULT_MAX_STEPS = 40
_MAX_STEPS_CAP = 100
# The halving-floor divisor when min_step is not given.
_MIN_STEP_DIVISOR = 64.0


def _finite_positive(x):
    """True iff ``x`` is a finite, strictly-positive real (bool is NOT a number)."""
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
        and x > 0
    )


def _finite_number(x):
    """True iff ``x`` is a finite real (any sign; bool is NOT a number)."""
    return (
        isinstance(x, (int, float))
        and not isinstance(x, bool)
        and math.isfinite(x)
    )


# --------------------------------------------------------------------------- #
# Atomic geometry checkpoint. A CYCLES-RUN optimize failure (DLS iterated +
# mutated the LDE before aborting) needs a GEOMETRY restore, not just an aperture revert.
# Mirrors the apply_lens_spec / tolerance SaveAs->LoadFile checkpoint pattern: a
# FORWARD-SLASH SaveAs path (#73) + a glob-reap of the unique temp stem incl. the engine's
# native ``.ZDA`` companion (#59). All helpers NEVER raise.
# --------------------------------------------------------------------------- #
_CKPT_PREFIX = "optivibe_ramp_ckpt_"


def _forward_slash(path):
    """Normalize to a forward-slash path (#73: SaveAs/LoadFile want forward slashes)."""
    return str(path).replace(os.sep, "/").replace("\\", "/")


def _unlink_quiet(path):
    """Best-effort unlink; never raises."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:  # noqa: BLE001 — a reap failure must not mask the ramp outcome
        pass


def _reap_checkpoint(path):
    """Remove the temp checkpoint placeholder AND the engine's ``.ZDA`` companion (#59).

    The live engine's ``SaveAs`` writes a native ``.ZDA`` binary (same stem), so an unlink
    of the ``.zmx`` placeholder alone leaks it. Globs the unique mkstemp token stem in the
    temp dir (scoped to this token, so it can never delete a foreign file). Never raises.
    """
    if not path:
        return
    _unlink_quiet(path)
    try:
        directory = os.path.dirname(path)
        base = os.path.basename(path)
        stem, _ext = os.path.splitext(base)
        for hit in glob.glob(os.path.join(directory, stem + "*")):
            _unlink_quiet(hit)
    except Exception:  # noqa: BLE001 — a reap glob failure never masks the outcome
        pass


# --------------------------------------------------------------------------- #
# Variable fingerprint (the discriminating restore proof — TERMINAL fix).
#
# A ``_snapshot_last_good`` ``SaveAs`` REPOINTS ``system.SystemFile`` to the checkpoint
# path, so ``SystemFile == checkpoint["path"]`` is VACUOUSLY true BEFORE the restore
# ``LoadFile`` runs; and a diverge that moved only some cells leaves the surface count
# unchanged. So NEITHER a SystemFile-match NOR a count-match can discriminate a REAL
# restore from a gotcha-#73 SILENT NO-OP ``LoadFile`` (which returns cleanly having loaded
# nothing).
#
# The fingerprint samples the ACTUAL OPTIMIZER DOFs — the ONLY quantities a DLS run can
# move — via ``_optimize_common._variable_inventory``. This is complete BY CONSTRUCTION:
# the DLS can move nothing but the variables, so the variable-value tuple is necessary AND
# sufficient to prove a restore. It covers LDE radius/thickness/conic + asphere Par-cell
# COEFFICIENTS + per-config MCE cells (incl. non-current configs) — the whole harness DOF
# surface — REUSING the shipped/audited enumerator (L30, one walk, no parallel per-cell
# code). A predecessor sampled only the three LDE ``Radius``/``Thickness``/``Conic``
# properties, which is VACUOUS for a design whose DOFs are asphere-coefficient-only or
# MCE-cell-only (an aspheric fluorite apochromat, this design class): a
# coefficient/MCE-only failed iterate perturbs no LDE property, so a #73 no-op restore
# passed the property proof while the live coefficient sat at the failed iterate.
#
# A cycles-run optimize failure iterated the DLS and PERTURBED a variable, so a no-op
# restore leaves the LIVE variable value at the FAILED ITERATE != the snapshot value ->
# ``geometry_uncertain`` fires LOUD. (Contrast ``load_design``: its SystemFile-match proof
# is sound only because it loads a DIFFERENT path than the current SystemFile — the ramp
# loads the path it just saved, so that match is vacuous.) Count is kept only as a cheap
# corroborator; the variable-value fingerprint is THE discriminator.
# --------------------------------------------------------------------------- #
def _variable_key(item):
    """A stable comparable key for one ``_variable_inventory`` DOF item.

    The variable SET is identical across a ``LoadFile`` of the same checkpoint file, so the
    keys line up snapshot-vs-restore. Each source carries its own uniqueness discriminator:
    LDE by ``(surface, cell-token)``, asphere by ``(surface, Par-column)``, GRIN by
    ``(surface, Par-column)``, MCE by ``(row, config)``. An unknown source falls back to a
    maximally-specific tuple (fail toward DISTINCTNESS — a colliding key fails the
    fingerprint closed).
    """
    source = item.get("source")
    if source == "lde":
        return ("lde", item.get("surface"), item.get("cell"))
    if source == "asphere":
        return ("asphere", item.get("surface"), item.get("par"))
    if source == "mce":
        return ("mce", item.get("row"), item.get("config"))
    if source == "grin":
        return ("grin", item.get("surface"), item.get("par"))
    return (
        "?", source, item.get("surface"), item.get("row"), item.get("config"),
        item.get("cell"), item.get("par"), item.get("term"),
    )


def _variable_fingerprint(system):
    """A ``dict`` of ``{dof_key: value}`` over EVERY optimizer DOF, or ``None`` (fail-closed).

    Fingerprints the ACTUAL optimizer variables (the ONLY values a DLS run can move) via the
    shipped ``_optimize_common._variable_inventory`` — LDE radius/thickness/conic + asphere
    Par-cell coefficients + per-config MCE cells (incl. non-current configs), complete by
    construction. NEVER raises.

    Returns ``None`` (FAIL-CLOSED — the caller reads None as unprovable -> geometry_uncertain)
    when the fingerprint cannot be TRUSTED:

    - the enumerator (or its enum resolution) THREW -> the DOF set is unreadable;
    - any per-source DISCOVERY ``faults`` were recorded -> a source's coverage was silently
      dropped, so a genuinely-Variable cell behind the fault is invisible to the proof;
    - an EMPTY inventory -> the ramp precondition guarantees >=1 variable, so an empty
      snapshot is anomalous (a proof over nothing is vacuous);
    - any DOF value is None / non-finite / non-numeric -> unreadable, cannot be compared.
    """
    # COVERAGE NOTE: the restore proof is exactly as COMPLETE as ``_variable_inventory``,
    # which does NOT yet enumerate CB coordinate-break Par-cell variables (``set_cb_variable``
    # decenter/tilt) — a pre-existing enumerator limitation. The ramp inherits the CB fix for free the
    # moment that lands (no change here). Until then this is not a live hazard: a CB-Par-ONLY
    # design is already refused ``no_variables`` at the ramp precondition/preflight, and a
    # MIXED design's covered DOFs (LDE/asphere/MCE) co-move with the CB DOFs under a single
    # joint DLS step, so a #73 no-op restore still perturbs a covered DOF and is caught.
    try:
        faults = []
        member = _oc._solve_type_variable_enum(system)
        inventory = _oc._variable_inventory(system, member, faults=faults)
    except Exception:  # noqa: BLE001 — an un-enumerable system -> no fingerprint (fail-closed)
        return None
    if faults:
        return None                            # a source's coverage was silently dropped
    if not inventory:
        return None                            # anomalous: precondition guarantees >=1 var
    fp = {}
    for item in inventory:
        value = item.get("value")
        if not _finite_number(value):          # None / non-finite / non-numeric -> unprovable
            return None
        key = _variable_key(item)
        if key in fp:                          # a non-unique DOF key -> cannot line up a restore
            return None
        fp[key] = float(value)
    return fp


def _fingerprints_match(a, b, rtol=1e-9, atol=1e-12):
    """True iff two variable fingerprints match key-set AND value-by-value (guarded).

    A ``LoadFile`` restore of the SAME checkpoint reloads the byte-faithful DOF values, so
    the key SET is identical and every value matches to well within ``rtol``; the FAILED
    iterate differs by the DLS's real variable move. ``None`` / a differing key set / a
    missing entry / a value beyond tolerance -> NOT a match (fail-closed).
    """
    # WHY TOLERANCED (rtol=1e-9), not exact: the snapshot fingerprint is read from LIVE
    # MEMORY before the ``SaveAs``, but the restore fingerprint is re-read AFTER the
    # ``LoadFile`` parses the ``.zmx`` TEXT back, so a genuine restore differs from the
    # in-memory snapshot by text round-trip rounding (~1e-13..1e-15). An EXACT compare would
    # false-fire ``geometry_uncertain`` on EVERY real restore. The slip band (an iterate
    # within 1e-9 of last-good on every covered DOF) is harmless — the geometry IS last-good
    # to 9 significant figures, while the FAILED iterate's real DLS move is orders larger.
    if a is None or b is None:
        return False
    if set(a) != set(b):                       # a differing DOF set -> unprovable (fail-closed)
        return False
    for key, x in a.items():
        y = b[key]
        if not (math.isfinite(x) and math.isfinite(y)):
            return False                       # a non-finite value -> unprovable
        if abs(x - y) > atol + rtol * max(abs(x), abs(y)):
            return False
    return True


def ramp_aperture(session, params):
    """Ramp the aperture toward ``target``, halving the step on an uncomputable merit.

    Never raises past its boundary: a body fault returns an ``error_envelope`` carrying
    the partial ``ramp_trace`` + ``last_good`` (nothing lost).
    """
    ctx = {"trace": [], "last_good": None}
    try:
        return _ramp_impl(session, params, ctx)
    except Exception as exc:  # noqa: BLE001 — never-raise backstop (partial state kept)
        return _oc.error_envelope(
            "ramp_aperture",
            _FAMILY,
            f"ramp_aperture body fault ({exc!r})",
            ramp_trace=ctx["trace"],
            steps=ctx["trace"],
            last_good=ctx["last_good"],
            final_value=ctx["last_good"],
            final_aperture=ctx["last_good"],
        )


def _ramp_impl(session, params, ctx):
    """The ramp body (validate -> precondition -> resolve -> adaptive loop)."""
    if not isinstance(params, dict):
        params = {}
    system = getattr(session, "system", None)

    # (1) validate the request UP FRONT (ZERO mutation on a bad value).
    target = params.get("target")
    if not _finite_positive(target):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY,
            "target must be a finite positive aperture value (EPD mm or NA)",
        )
    target = float(target)

    step = params.get("step")
    if step is not None and not _finite_positive(step):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY, "step, if given, must be finite and positive",
        )
    min_step = params.get("min_step")
    if min_step is not None and not _finite_positive(min_step):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY, "min_step, if given, must be finite and positive",
        )
    max_steps = params.get("max_steps")
    if max_steps is None:
        max_steps = _DEFAULT_MAX_STEPS
    elif not _finite_positive(max_steps):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY, "max_steps, if given, must be finite and positive",
        )
    max_steps = min(int(max_steps), _MAX_STEPS_CAP)

    from_value = params.get("from_value")
    if from_value is not None and not _finite_positive(from_value):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY, "from_value, if given, must be finite and positive",
        )

    design_name = params.get("design_name")
    require_free_stop = params.get("require_free_stop", True)
    auto_normalize = params.get("auto_normalize", False)

    # (2) precondition (like optimize): variables + a merit must be set. Best-effort —
    # a preflight throw degrades to "proceed" (the loop will surface a real fault).
    try:
        pf = _preflight(system, require_free_stop=False)
    except Exception:  # noqa: BLE001 — an un-preflightable system proceeds
        pf = None
    if pf is not None and not pf[0] and pf[1] in ("no_variables", "no_merit"):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY,
            "ramp_aperture needs optimizer variables AND a merit function set first "
            f"(preflight: {pf[1]}); set variables (set_variable/vary) + build_merit, "
            "then retry.",
        )

    # (3) resolve aperture_type + from_value (default to the CURRENT system aperture).
    aperture_type = params.get("aperture_type")
    if aperture_type is None or from_value is None:
        try:
            aperture = system.SystemData.Aperture
            if aperture_type is None:
                aperture_type = str(aperture.ApertureType)
            if from_value is None:
                from_value = float(aperture.ApertureValue)
        except Exception:  # noqa: BLE001 — fall back to the explicit args / a default
            pass
    if aperture_type is None:
        aperture_type = "EntrancePupilDiameter"
    if not _finite_number(from_value):
        return _oc.error_envelope(
            "ramp_aperture", _FAMILY,
            "could not resolve the starting aperture value; pass from_value explicitly",
        )
    from_value = float(from_value)

    # (4) the adaptive halve-on-uncomputable loop (FLAT in-process envelope).
    initial_step = abs(step) if step is not None else abs(target - from_value) / 10.0
    if initial_step <= 0:
        # degenerate (target == from_value handled by the reach test below); guard /0.
        initial_step = abs(target - from_value) or 1.0
    min_step_val = abs(min_step) if min_step is not None else initial_step / _MIN_STEP_DIVISOR
    if min_step_val <= 0:
        min_step_val = _EPS

    trace = ctx["trace"]
    checkpoints = []
    cur = last_good = from_value
    ctx["last_good"] = last_good
    last_good_merit = None
    cur_step = initial_step
    n_halvings = 0
    n_accepted = 0
    reached_target = False
    ceiling_reached = False
    ceiling_reason = None
    warning = None

    # geometry-checkpoint state. ``checkpoint`` snapshots the CURRENT (last-good)
    # geometry+aperture; a CYCLES-RUN optimize failure restores from it (see _restore_geometry).
    checkpoint = {"path": None, "count": None, "fingerprint": None, "ok": False}
    geometry_restored = None      # True once at least one cycles-run restore succeeded
    geometry_uncertain = False    # True if a needed restore could NOT be proven -> LOUD

    def _revert(value):
        nonlocal warning
        try:
            _set_aperture(session, {"aperture_type": aperture_type, "value": value})
        except Exception as exc:  # noqa: BLE001 — best-effort revert
            warning = (
                f"could not revert to last_good aperture {value:g} (system may be left "
                f"at the failing aperture): {exc!r}"
            )

    def _snapshot_last_good():
        """SaveAs the CURRENT (last-good) geometry+aperture to a fresh temp checkpoint.

        Best-effort + SILENT: a throw leaves ``checkpoint['ok']=False`` (no warning) — the
        loud disclosure is deferred to the point a cycles-run failure actually NEEDS the
        restore and it is unavailable (so a clean ramp is never spuriously warned).
        """
        _reap_checkpoint(checkpoint["path"])
        checkpoint["path"] = None
        checkpoint["count"] = None
        checkpoint["fingerprint"] = None
        checkpoint["ok"] = False
        raw = None
        try:
            fd, raw = tempfile.mkstemp(suffix=".zmx", prefix=_CKPT_PREFIX)
            os.close(fd)
            fwd = _forward_slash(raw)
            # NOTE (accepted): SaveAs REPOINTS system.SystemFile at this temp
            # checkpoint, which the finally then reaps -> SystemFile is left dangling after
            # a clean ramp. This mirrors the apply_lens_spec SaveAs-temp-checkpoint precedent
            # and is caught LOUDLY downstream by the #73 / tolerancing_empty_report net (a
            # disk-load re-blesses), so it is an accepted papercut, not a silent-wrong.
            system.SaveAs(fwd)
            checkpoint["path"] = fwd
            try:
                checkpoint["count"] = int(system.LDE.NumberOfSurfaces)
            except Exception:  # noqa: BLE001 — an unreadable count -> fingerprint-only proof
                checkpoint["count"] = None
            # The load-bearing restore discriminator: fingerprint the OPTIMIZER DOFs a
            # failed optimize would perturb (LDE + asphere coeffs + MCE cells). Best-effort —
            # a None here means a later restore can never be proven (fail-closed ->
            # geometry_uncertain).
            checkpoint["fingerprint"] = _variable_fingerprint(system)
            checkpoint["ok"] = True
        except Exception:  # noqa: BLE001 — snapshot best-effort (disclosed only at need)
            _reap_checkpoint(raw)
            checkpoint["path"] = None
            checkpoint["ok"] = False

    def _restore_readback_ok():
        """READ-BACK-AS-PROOF the restore LoadFile ACTUALLY restored the snapshot DOFs.

        The load-bearing discriminator is the OPTIMIZER-DOF FINGERPRINT captured at snapshot
        time: a #73 SILENT NO-OP LoadFile leaves the live variables at the FAILED iterate,
        whose fingerprint != the snapshot -> False -> geometry_uncertain fires LOUD. A
        SystemFile-match is NOT used (vacuous — the snapshot SaveAs already set it); the
        surface count is kept only as a cheap corroborator.
        """
        if checkpoint["fingerprint"] is None:
            return False                       # no snapshot fingerprint -> unprovable
        try:
            count = int(system.LDE.NumberOfSurfaces)
        except Exception:  # noqa: BLE001 — an unreadable count -> restore unproven
            return False
        if checkpoint["count"] is not None and count != checkpoint["count"]:
            return False                       # cheap corroborator
        return _fingerprints_match(_variable_fingerprint(system), checkpoint["fingerprint"])

    def _restore_geometry(value):
        """Restore last-good geometry+aperture after a CYCLES-RUN optimize failure.

        A ``diverged`` / ``optimize_run_failed`` step means the DLS iterated + MUTATED
        the LDE before aborting, so an aperture-only revert would leave the live geometry
        as the failed iterate while the tool reports ``final_value``/``final_merit`` =
        last_good (a silent misreport). This LoadFile-restores the checkpoint, read-back
        proven. On ANY failure -> a LOUD ``geometry_uncertain`` disclosure (never a silent
        clean last_good).
        """
        nonlocal warning, geometry_uncertain, geometry_restored
        if not checkpoint["ok"] or not checkpoint["path"]:
            geometry_uncertain = True
            warning = (
                "a cycles-run optimize failure MUTATED the geometry but no checkpoint "
                f"was available to restore it; the live design may NOT match "
                f"final_value/final_merit ({value:g}) — reload your saved .zmx to recover."
            )
            _revert(value)          # best-effort aperture-only fallback
            return False
        try:
            system.LoadFile(checkpoint["path"], False)
        except Exception as exc:  # noqa: BLE001 — a restore throw -> UNKNOWN state, loud
            geometry_uncertain = True
            warning = (
                f"the geometry-restore LoadFile threw ({exc!r}); the live design is in "
                "an UNKNOWN state and may NOT match final_value/final_merit — reload your "
                "saved .zmx to recover."
            )
            return False
        if not _restore_readback_ok():
            geometry_uncertain = True
            warning = (
                "the geometry-restore LoadFile returned but the read-back does NOT match "
                "the checkpoint; the live design may be in a PARTIAL state — reload your "
                "saved .zmx to recover."
            )
            return False
        geometry_restored = True
        return True

    try:
        _snapshot_last_good()            # checkpoint the initial (from_value) last-good state
        for _ in range(max_steps):
            if abs(target - cur) <= _EPS:
                reached_target = True
                break
            direction = 1.0 if (target - cur) >= 0 else -1.0
            trial = cur + direction * min(cur_step, abs(target - cur))

            # step the aperture (a set failure -> revert + halve). ZERO-mutation (no
            # optimize ran) so an aperture-only revert suffices.
            try:
                _set_aperture(session, {"aperture_type": aperture_type, "value": trial})
            except Exception as exc:  # noqa: BLE001 — a bad aperture value / engine throw
                trace.append({"value": trial, "set_ok": False, "reason": repr(exc)})
                _revert(last_good)
                cur_step /= 2.0
                n_halvings += 1
                if cur_step < min_step_val:
                    ceiling_reached = True
                    ceiling_reason = "set_aperture_failed"
                    break
                continue

            # re-optimize at the widened aperture; read the FLAT envelope. FINDING 2: a raw
            # mid-run throw (``_optimize`` can re-raise a pythonnet/engine fault AFTER the DLS
            # iterated + MUTATED the LDE) is NETTED into a cycles-run failure envelope so the
            # geometry-restore path below runs (fingerprint-proven) — the geometry is NEVER
            # silently left as the failed iterate on the loud-failure path. Without this local
            # net the throw would hit the outer never-raise backstop, which cannot restore.
            try:
                r = _optimize(
                    session,
                    {"require_free_stop": require_free_stop,
                     "auto_normalize": auto_normalize},
                )
                if not isinstance(r, dict):
                    r = {}
            except Exception as exc:  # noqa: BLE001 — a mid-run fault (geometry may be mutated)
                r = {"ok": False, "error_family": "optimize_run_failed", "_threw": repr(exc)}
            uncomputable = (
                (not r.get("ok"))
                and r.get("error_family") == "optimize_merit_uncomputable"
            )
            diverged = bool(r.get("ok")) and r.get("verdict") == "diverged"

            if r.get("ok") and not diverged:
                # accept: this aperture optimized cleanly -> advance last_good, re-grow step.
                cur = last_good = trial
                ctx["last_good"] = last_good
                last_good_merit = r.get("merit_after")
                n_accepted += 1
                trace.append({
                    "value": trial, "set_ok": True, "opt_ok": True,
                    "verdict": r.get("verdict"), "merit_after": r.get("merit_after"),
                })
                if design_name:
                    cp = _save_candidate(
                        session,
                        {"design_name": design_name, "label": f"ramp_{trial:g}",
                         "render": False},
                    )
                    checkpoints.append(cp.get("seq") if isinstance(cp, dict) else None)
                cur_step = min(initial_step, cur_step * 2.0)   # re-grow toward initial
                # the NEW last-good geometry is the restore point for a later failure.
                _snapshot_last_good()
            else:
                reason = (
                    "merit_uncomputable" if uncomputable
                    else "diverged" if diverged
                    else "run_threw" if r.get("_threw")
                    else "run_failed"
                )
                # A CYCLES-RUN failure (DLS iterated + MUTATED the LDE before aborting):
                # verdict ``diverged`` OR error_family ``optimize_run_failed``
                # (optimize_run.py:763-773). Every OTHER non-ok family
                # (merit_uncomputable ceiling / no_variables / stop refusal) runs ZERO
                # cycles -> the geometry is untouched -> an aperture-only revert suffices
                # (a needless LoadFile churns the seat).
                cycles_run_failure = (
                    diverged or r.get("error_family") == "optimize_run_failed"
                )
                trace.append({
                    "value": trial, "set_ok": True, "opt_ok": False,
                    "reason": reason, "error_family": r.get("error_family"),
                    "cycles_run_failure": cycles_run_failure,
                })
                if cycles_run_failure:
                    _restore_geometry(last_good)   # geometry+aperture, read-back proven
                else:
                    _revert(last_good)             # leave the system traceable (aperture)
                cur_step /= 2.0
                n_halvings += 1
                if cur_step < min_step_val:
                    ceiling_reached = True
                    ceiling_reason = (
                        "merit_uncomputable" if uncomputable else "unstable"
                    )
                    break

        if not reached_target and not ceiling_reached:
            ceiling_reached = True
            ceiling_reason = "max_steps"

        ceiling = (
            {"aperture_type": aperture_type, "value": last_good}
            if ceiling_reached else None
        )
        return {
            "ok": True,
            "tool": "ramp_aperture",
            "aperture_type": aperture_type,
            "from_value": from_value,
            "target": target,
            "reached_target": reached_target,
            "ceiling_reached": ceiling_reached,
            "ceiling_reason": ceiling_reason,
            "ceiling": ceiling,
            "final_value": last_good,
            "final_merit": last_good_merit,
            "steps_used": len(trace),
            "n_accepted": n_accepted,
            "n_halvings": n_halvings,
            "ramp_trace": trace,
            "checkpoints": checkpoints,
            "warning": warning,
            # geometry-restore disclosure (two-tier revert):
            "geometry_restored": geometry_restored,
            "geometry_uncertain": geometry_uncertain,
            # task-named aliases (additive; the orchestrator may read either name).
            "steps": trace,
            "n_steps": len(trace),
            "final_aperture": last_good,
        }
    finally:
        _reap_checkpoint(checkpoint["path"])


RAMP_APERTURE_SPEC = ToolSpec(
    name="ramp_aperture",
    handler=ramp_aperture,
    required_params=("target",),
    param_types={
        "target": "number",
        "aperture_type": "string",
        "from_value": "number",
        "step": "number",
        "min_step": "number",
        "max_steps": "number",
        "design_name": "string",
        "require_free_stop": "boolean",
        "auto_normalize": "boolean",
    },
    description=(
        "Ramp the aperture (EPD or NA) toward a target, re-optimizing each step and "
        "auto-halving the step on optimize_merit_uncomputable, until the target OR the "
        "last-traceable ceiling. Params: target, aperture_type (default current), "
        "from_value, step, min_step, max_steps, design_name (checkpoint each accepted "
        "step), require_free_stop/auto_normalize (forwarded to optimize). Returns the "
        "ramp trace + reached_target/ceiling; leaves the design at the last optimized "
        "aperture. Gotcha: variables + a merit must be set first (like optimize)."
    ),
)

TOOL_SPEC = RAMP_APERTURE_SPEC
