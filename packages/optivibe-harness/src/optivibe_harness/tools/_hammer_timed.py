"""tools/_hammer_timed.py -- the timed Hammer run that substitutes glass.

The automatic Hammer run (``AutomaticOptimization = True``) never touches a glass that carries a
Substitute solve. A manual run does: ``RunAndWaitWithTimeout`` for the whole budget, ``Cancel()``,
then ``WaitForCompletion()`` -- the result reaches the lens at that wait, not before (measured live).
Everything specific to that run lives here; ``optimize_run`` only routes to it.

What a caller of this module can rely on:

- ``preflight`` runs at the TOP of ``optimize``, before any mutation, and returns a refusal
  envelope (family ``optimize_substitute_catalog``) or the ``Timed`` plan.
- ``route`` wraps the Hammer fork: it reads the substitute cells again (a normalisation may have
  inserted a surface), runs the fork, and adds the timed result keys and any restore warning.
- ``Timed.writes`` is the ONE cleanup owner for the catalog field: whatever it wrote is written
  back when the context exits, and it exits after the Hammer handle has closed.

No member of the Hammer handle is read after ``Close()`` (that read killed the engine, measured).
Cycle-safe: nothing here imports ``optimize_run`` at module level.
"""
import contextlib

from .._io import safe_call, safe_exc
from ..enums import _resolve_enum
from ..errors import OptimizeError, SessionClosedError, ToolParamError, map_dotnet_exception
from . import _optimize_common as _oc
from . import _run_budget as _rb
from . import _solve_cells as _sc
from . import lens_glass as _lg
from . import surface_solve as _ss

FAMILY = "optimize_substitute_catalog"
TIMED_RUN_CAP = 60.0                      # minutes; the automatic run keeps its own cap
_MODES = ("auto", "timed")
_SUBSTITUTE = "MaterialSubstitute"


class _Unreadable(Exception):
    """A material cell whose solve type could not be read: the substitute census is unknown."""


def _kind_api():
    """The reference layer's glass-kind helpers (imported late, as the other glass tools do)."""
    from optivibe_reference.tools import _glass_kind as gk
    return gk.catalog_key, gk.material_kind, gk._MIXED_CATALOGS


def _open_glass_db():
    """The reference glass table, opened read-only. A module-level name so a test can stub it."""
    from optivibe_reference.glass_build import open_glass_catalog
    return open_glass_catalog()


def _refuse(message, **extra):
    return _oc.error_envelope("optimize", FAMILY, message, **extra)


def _merge(*parts):
    kept = [p for p in parts if p]
    return "; ".join(kept) if kept else None


def _in_use(system):
    """The raw ``GetCatalogsInUse()`` list, blanks included (the engine's own view)."""
    return [str(c) for c in system.SystemData.MaterialCatalogs.GetCatalogsInUse()]


def _text(value):
    return _oc._base_token(value or "").strip()


def _read_surface(lde, surface):
    """One surface's glass, bound catalog and kind, whatever its material solve is now."""
    material_kind = _kind_api()[1]
    row = lde.GetSurfaceAt(surface)
    glass = _text(row.Material)
    bound = _lg._bound_catalog(row)
    return {"surface": surface, "glass": glass, "catalog": bound,
            "kind": material_kind(bound or "", glass)}


def _read_cells(system):
    """Every surface whose material cell carries a Substitute solve, in surface order.

    Reads through the accessors ``set_solve`` and ``read_surface`` use: the COLUMN-route material
    cell, its solve type, the ``_S_MaterialSubstitute`` view's ``Catalog`` field, and the bound
    catalog on the row. A cell whose solve type cannot be read raises ``_Unreadable``: a census
    that skips an unreadable cell could let a mixed catalog through.
    """
    lde = system.LDE
    out = []
    for surface in range(int(lde.NumberOfSurfaces)):
        cell = _sc.solve_cell(system, lde, surface, "material")
        kind = _sc.read_solve_type(cell)
        if kind is None:
            raise _Unreadable(surface)
        if kind != _SUBSTITUTE:
            continue
        view = _sc.unwrap_or_none(cell.GetSolveData())
        out.append(dict(_read_surface(lde, surface), field=_text(getattr(view, "Catalog", ""))))
    return out


def _write_catalog(system, surface, value):
    """Write ``value`` into the Substitute solve's ``Catalog`` field of ``surface``'s material."""
    lde = system.LDE
    cell = _sc.solve_cell(system, lde, surface, "material")
    member = _resolve_enum(_sc.solve_type_enum(system), _SUBSTITUTE)
    _ss.author_solve(
        system, cell, member, {"Catalog": value}, surface=surface, cell_label="material",
        none_create_error=lambda: ToolParamError(
            "the engine would not create a Substitute solve on surface %s" % surface),
    )


class Timed:
    """One timed Hammer call's state: the plan, the baseline, the run facts, the cleanup."""

    def __init__(self, catalog):
        self.catalog = catalog            # the normalised catalog name, or None
        self.cells = []                   # the baseline: re-read just before the checkpoint
        self.in_use_before = None
        self.written = {}                 # surface -> the Catalog field it held before our write
        self.warnings = []
        self.run_status = None
        self.systems = None

    def begin(self, system):
        """Step 3b: the reporting baseline, read AFTER any normalisation and before the checkpoint."""
        try:
            self.cells = _read_cells(system)
            self.in_use_before = _in_use(system)
        except Exception as exc:  # noqa: BLE001 -- an unreadable baseline cannot be reported on
            raise OptimizeError(
                "the Substitute solves could not be read before the run (%s); nothing was run"
                % safe_exc(exc), family="optimize_unavailable")

    @contextlib.contextmanager
    def writes(self, system):
        """Write ``catalog`` into every substitute cell, and write the prior values back on exit.

        The try opens BEFORE the first write, so a write loop that fails half way, a Hammer open
        that throws, an early return and a normal exit all restore what was written. A restore
        that fails does not raise; it leaves a warning naming the surfaces.
        """
        try:
            if self.catalog:
                for cell in self.cells:
                    self.written[cell["surface"]] = cell["field"]
                    try:
                        _write_catalog(system, cell["surface"], self.catalog)
                    except Exception as exc:  # noqa: BLE001 -- say which surface, restore the rest
                        if (isinstance(exc, SessionClosedError)
                                or isinstance(map_dotnet_exception(exc), SessionClosedError)):
                            raise          # a lost session keeps its class (the test `route` uses)
                        raise OptimizeError(
                            "could not write catalog %s into the Substitute solve of surface %s "
                            "(%s); the surfaces already written are being restored"
                            % (self.catalog, cell["surface"], safe_exc(exc)),
                            family="optimize_unavailable")
            yield
        finally:
            self._restore(system)

    def _restore(self, system):
        failed = []
        for surface, prior in reversed(list(self.written.items())):
            try:
                _write_catalog(system, surface, prior)
            except Exception:  # noqa: BLE001 -- a restore failure is a warning, never a raise
                failed.append(surface)
        self.written = {}
        if failed:
            self.warnings.append(
                "the Substitute catalog field could not be put back on surface(s) %s; set it "
                "again by hand or reload your last saved file" % sorted(failed))

    def run(self, ham, run_time_m):
        """Steps 7-9. True when the run started; False on FailedToStart (nothing was changed).

        ``Cancel()`` and ``WaitForCompletion()`` return values are not success flags (measured:
        the wait returned False on a run Cancel had already ended), so neither is read. The
        result reaches the lens at the wait; the re-read of the merit after ``Close()`` is the
        live check on it.
        """
        self.run_status = _rb.run_status_name(ham.RunAndWaitWithTimeout(float(run_time_m) * 60.0))
        if self.run_status == "FailedToStart":
            return False
        ham.Cancel()
        ham.WaitForCompletion()
        self.systems = safe_call(lambda: int(ham.Systems), None)
        return True

    def finish(self, system, result):
        """Add the timed keys (an ``ok`` result) and the restore warnings (any result)."""
        if not isinstance(result, dict):
            return result
        warning = result.get("warning")
        if result.get("ok") is True:
            warning = _merge(warning, self._report(system, result))
        result["warning"] = _merge(warning, *self.warnings)
        if result["warning"] is None and result.get("ok") is not True:
            result.pop("warning")
        return result

    def _report(self, system, result):
        """Fill the timed result keys from the lens as it stands NOW; return a warning or None."""
        notes = []
        result["hammer_mode"] = "timed"
        result["run_status"] = self.run_status
        result["systems_tried"] = self.systems
        result["catalog"] = self.catalog
        result["substitute_cells"] = len(self.cells)
        if not self.cells:
            notes.append("no glass in this lens carries a Substitute solve, so a timed Hammer "
                         "run could not change any glass")
        try:
            now = [_read_surface(system.LDE, c["surface"]) for c in self.cells]
            in_use_after = _in_use(system)
        except Exception as exc:  # noqa: BLE001 -- say so; do not report an empty change list
            result["glass_changes"] = None
            result["catalogs_in_use_changed"] = None
            notes.append("the glasses could not be read back after the run (%s); read them with "
                         "read_surface" % safe_exc(exc))
            return _merge(*notes)
        moved = [(c, n) for c, n in zip(self.cells, now)
                 if (c["glass"], c["catalog"]) != (n["glass"], n["catalog"])]
        try:
            status = _statuses([n for _c, n in moved])
        except Exception as exc:  # noqa: BLE001 -- the glass table is a nicety; keep the outcome
            status = {}
            notes.append("the glass table could not be read (%s), so the status of every changed "
                         "glass is null and whether it is discontinued is unknown" % safe_exc(exc))
        changes = []
        for prior, cell in moved:
            state = status.get(cell["surface"])
            entry = {
                "surface": cell["surface"],
                "before": {k: prior[k] for k in ("glass", "catalog", "kind")},
                "after": {"glass": cell["glass"], "catalog": cell["catalog"],
                          "kind": cell["kind"], "status": state},
                "discontinued": state == "obsolete",
                "non_lens": cell["kind"] != "lens",
            }
            changes.append(entry)
            if entry["discontinued"]:
                notes.append("surface %s now holds %s, which the glass table lists as "
                             "discontinued" % (cell["surface"], cell["glass"]))
            if entry["non_lens"]:
                notes.append("surface %s now holds %s, which is a %s, not a lens glass"
                             % (cell["surface"], cell["glass"], cell["kind"]))
        result["glass_changes"] = changes
        changed = None
        if in_use_after != self.in_use_before:
            changed = {"before": list(self.in_use_before), "after": in_use_after}
            notes.append("the catalogs in use changed during the run (Hammer can add catalogs)")
        result["catalogs_in_use_changed"] = changed
        return _merge(*notes)


def _statuses(cells):
    """``{surface: status}`` for every cell, or an exception: all of it, or none of it."""
    if not cells:
        return {}
    conn = _open_glass_db()
    try:
        return {c["surface"]: _glass_status(conn, c["catalog"], c["glass"]) for c in cells}
    finally:
        safe_call(conn.close, None)


def _glass_status(conn, catalog, glass):
    """The glass table's ``status`` for ``glass`` in ``catalog``, or None when it is not found."""
    catalog_key = _kind_api()[0]
    key = catalog_key(catalog or "")
    for stored, status in conn.execute(
            "SELECT catalog, status FROM glass WHERE name = ? COLLATE NOCASE", (glass,)):
        if catalog_key(stored) == key:
            return status
    return None


def preflight(system, params):
    """The params check, then (timed only) the refusals; ``(timed, refusal)``.

    Called at the top of ``optimize``, before the normalisation, the thin-gap nudge, the trail
    and the checkpoint: a refusal returns with the lens and the trail untouched. A call with no
    ``hammer_mode`` and no ``catalog`` returns ``(None, None)`` at once. ``(Timed, None)`` means
    go ahead; ``(None, envelope)`` is a refusal to return as is.
    """
    has_mode = isinstance(params, dict) and dict.__contains__(params, "hammer_mode")
    has_catalog = isinstance(params, dict) and dict.__contains__(params, "catalog")
    if not (has_mode or has_catalog):
        return None, None
    mode = dict.get(params, "hammer_mode", "auto")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ToolParamError("hammer_mode must be 'auto' or 'timed', got %r" % (mode,))
    if has_mode and dict.get(params, "algorithm", "DLS") != "Hammer":
        raise ToolParamError("hammer_mode applies only to algorithm='Hammer'")
    if has_catalog and mode != "timed":
        raise ToolParamError("catalog applies only to hammer_mode='timed'")
    if mode != "timed":
        return None, None
    return _timed_preflight(system, params, has_catalog)


def _timed_preflight(system, params, has_catalog):
    from . import optimize_run as _orun          # late: optimize_run imports this module

    _orun._require_pos_float(
        params, "run_time_m", _orun._DEFAULT_RUN_TIME_M, cap=TIMED_RUN_CAP)
    catalog_key, _kind, mixed = _kind_api()
    catalog = None
    if has_catalog:
        raw = dict.get(params, "catalog")
        if not isinstance(raw, str):
            raise ToolParamError("catalog must be a string, got %r" % (raw,))
        catalog = catalog_key(raw)
        if not catalog:
            raise ToolParamError("catalog is empty; name a catalog that is in use, e.g. SCHOTT")
    try:
        in_use = _in_use(system)
        cells = _read_cells(system)
    except Exception as exc:  # noqa: BLE001 -- an unreadable census cannot clear the run
        return None, _refuse(
            "the Substitute solves or the catalogs in use could not be read (%s); nothing was run"
            % safe_exc(exc))
    keys = [catalog_key(c) for c in in_use if c]
    if catalog is not None:
        if catalog not in keys:
            return None, _refuse(
                "catalog %s is not in use in this lens; the catalogs in use are %s. Name one of "
                "them, so the lens's catalog list does not change behind you"
                % (catalog, ", ".join(keys) or "none"), catalogs_in_use=keys)
        if catalog in mixed:
            return None, _refuse(_mixed_message([catalog], keys), catalogs_in_use=keys)
    else:
        mixed_in_use = [k for k in keys if k in mixed]
        blank = [c["surface"] for c in cells if not c["field"]]
        naming = [c["surface"] for c in cells if catalog_key(c["field"]) in mixed]
        if (blank and mixed_in_use) or naming:
            return None, _refuse(_mixed_message(mixed_in_use or [
                catalog_key(c["field"]) for c in cells if catalog_key(c["field"]) in mixed], keys),
                catalogs_in_use=keys, surfaces=sorted(set(blank + naming)))
    return Timed(catalog), None


def _mixed_message(names, keys):
    return (
        "a timed Hammer run could draw glasses from the mixed catalog(s) %s, which hold liquids, "
        "adhesives, gases and plastics beside lens glass. Pass catalog=<one lens catalog that is "
        "in use> (in use: %s) to limit the picks to it" % (", ".join(sorted(set(names))), ", ".join(keys)))


def writes(timed, system):
    """The cleanup context for the ``with`` that opens Hammer: a no-op for the automatic run."""
    return contextlib.nullcontext() if timed is None else timed.writes(system)


def session_kwargs(timed):
    """Extra ``_hammer_session`` arguments: none for the automatic run, so its call is unchanged."""
    return {} if timed is None else {"automatic": False}


def route(timed, system, call):
    """Run the Hammer fork ``call()``; for a timed call, read the baseline first and report after.

    An exception that leaves the fork while a restore warning is pending still carries it: an
    ``OptimizeError`` of an envelope family, and any other fault (as ``optimize_unavailable``),
    return as the refusal envelope with the warning in its ``warning`` key (the text stays in
    ``error`` too); an ``OptimizeError`` of another family, or a lost session (its class is kept
    for the server), is re-raised with the text merged in.
    """
    if timed is None:
        return call()
    timed.begin(system)
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 -- a pending warning must not be lost with the fault
        if not timed.warnings:
            raise
        text = _merge(safe_exc(exc), *timed.warnings)
        if isinstance(exc, SessionClosedError) or isinstance(map_dotnet_exception(exc), SessionClosedError):
            raise SessionClosedError(text) from exc      # the class the server maps stays; so does the note
        family = getattr(exc, "family", "optimize") if isinstance(exc, OptimizeError) else "optimize_unavailable"
        from . import optimize_run as _orun          # late: optimize_run imports this module
        if family not in _orun._ENVELOPE_FAMILIES:
            raise OptimizeError(text, family=family)
        return timed.finish(system, _oc.error_envelope("optimize", family, text))
    return timed.finish(system, result)
