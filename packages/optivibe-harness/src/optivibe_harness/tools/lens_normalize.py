"""tools/lens_normalize.py — normalize_stop (the dummy-stop convention).

ONE dispatchable tool that enforces the dummy-stop convention: a real aperture
stop is a free-standing dummy AIR surface in an airspace (both adjacent gaps
freed + positively bounded), NOT coincident with a lens vertex. It COMPOSES the
primitives (insert_surface / set_surface / set_stop_surface / set_variable)
inheriting their read-back firewalls — it adds no new mutator.

Three outcomes (locked §1.2):

- ``noop_already_free`` — stop free-standing AND both gaps already bounded.
- ``noop_bounds_added`` — stop free-standing but a gap was UNBOUNDED / inert; the
  missing positive-Min bound(s) are added; the stop is NOT moved (Fork 3).
- ``noop_gaps_freed`` — stop free-standing; a gap was freed (made Variable) but
  add_bounds=False so ZERO bounds were added (the honest label for a free-only
  mutation — never mislabeled ``noop_bounds_added`` when no bound was authored).
- ``refactored_split`` — stop was on a glass vertex; a dummy is inserted into an
  existing adjacent air gap, the gap is split ``t -> t/2, t/2``, the stop is moved
  onto the dummy, both gaps freed + bounded.

Fail-closed (locked §1.3): ``normalize_no_stop`` / ``normalize_no_airspace`` /
``normalize_param`` / ``surface_write`` — all structured ``{ok:false}`` envelopes.

Read-back-as-proof (locked §3.3): the refactor is declared ``ok`` ONLY
after every post-condition passes (the LOAD-BEARING gate re-runs
``_classify_stop``, which must read ``free_airspace``); a failure bubbles the
``SurfaceWriteError`` up as the ``surface_write`` envelope (NEVER a half-refactored
``ok``). A ``normalize00_before`` snapshot is taken BEFORE the first mutation.

ORDERING GOTCHA (LIVE finding) — add structural bounds AFTER build_merit:
``normalize_stop`` authors its MNEA/MNCA bound operands directly on the live MFE.
``build_merit`` runs the OpticStudio SEQ optimization wizard, whose ``Apply()``/
``OK()`` REPLACES the entire operand list — wiping ANY bounds added beforehand. So a
manual caller doing ``normalize_stop`` THEN ``build_merit`` LOSES the bounds (the
freed gap is then unprotected and can collapse). Correct order: ``build_merit``
FIRST, then ``normalize_stop`` (or re-run ``normalize_stop`` after each
``build_merit``). The ``optimize(auto_normalize=True)`` path is already safe — a
merit must exist before optimize, so ``normalize_stop`` runs AFTER the wizard. This
is a documented ordering constraint, not a guard (the tool cannot reliably detect a
wizard-built merit it will be re-wiped from).

Live ZOS-API integration: exercised by the live tests; unit-tested against
the FakeLDE/FakeMFE doubles (no backend).
"""
from dataclasses import asdict

from .._io import safe_float
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _lens_common as _lc
from . import _structural_common as _sc
from . import lens_surface as _ls
from . import optimize_variable as _ov
from ._analysis_common import error_envelope

# bound_kind token -> the air boundary operand it adds (locked §5).
#   edge   -> MNEA (Min Edge thickness, AIR)   — the DEFAULT (the edge collapses
#             first on a converging gap: probe ETVA 9.89 < CTVA 10.0).
#   center -> MNCA (Min Center thickness, AIR) — the rarer diverging-gap case.
_BOUND_KIND_TO_OPERAND = {"edge": "MNEA", "center": "MNCA"}
_DEFAULT_BOUND_KIND = "edge"
_DEFAULT_MIN_AIR = 0.5


# --------------------------------------------------------------------------- #
# Param validation (locked §1.1 / §1.3 normalize_param).
# --------------------------------------------------------------------------- #
def _parse_params(params):
    """Validate + normalize the four optional params (locked §1.1).

    Returns ``(bound_kind, operand_token, min_air, add_bounds, free_gaps)`` or
    raises ``ToolParamError`` (caught by the handler -> ``normalize_param``).
    """
    bound_kind = params.get("bound_kind", _DEFAULT_BOUND_KIND)
    if bound_kind not in _BOUND_KIND_TO_OPERAND:
        raise ToolParamError(
            f"bound_kind must be one of {sorted(_BOUND_KIND_TO_OPERAND)}, "
            f"got {bound_kind!r}"
        )
    operand_token = _BOUND_KIND_TO_OPERAND[bound_kind]

    min_air = params.get("min_air", _DEFAULT_MIN_AIR)
    if isinstance(min_air, bool) or not isinstance(min_air, (int, float)):
        raise ToolParamError(
            f"min_air must be a positive number, got {type(min_air).__name__} "
            f"{min_air!r}"
        )
    min_air = float(min_air)
    if not (min_air > 0):
        # probe Q6/d: a 0/negative Min is INERT (contributes 0; silently fails to
        # protect). Refusing it up front is the anti-trap.
        raise ToolParamError(
            f"min_air must be > 0 (a 0/negative Min is an inert bound that silently "
            f"fails to protect the gap), got {min_air}"
        )

    add_bounds = params.get("add_bounds", True)
    if not isinstance(add_bounds, bool):
        raise ToolParamError(
            f"add_bounds must be a bool, got {type(add_bounds).__name__} {add_bounds!r}"
        )
    free_gaps = params.get("free_gaps", True)
    if not isinstance(free_gaps, bool):
        raise ToolParamError(
            f"free_gaps must be a bool, got {type(free_gaps).__name__} {free_gaps!r}"
        )
    return bound_kind, operand_token, min_air, add_bounds, free_gaps


# --------------------------------------------------------------------------- #
# Snapshot discipline (locked §1.4).
# --------------------------------------------------------------------------- #
def _snapshot_before(session, meta):
    """Take the ``normalize00_before`` snapshot; NEVER raises (decoupled, §1.4).

    Mirrors ``optimize_run._snapshot``: a missing sink yields no row (+ a warning
    the caller surfaces once); a non-conforming sink that raises is degraded to an
    ``ok=False`` row rather than aborting the refactor. Returns ``(row_or_None,
    warning_or_None)``.
    """
    sink = getattr(session, "artifact_sink", None)
    if sink is None:
        return None, "no artifact sink configured; normalize00_before not captured"
    try:
        result = sink.snapshot("normalize00_before", meta)
        fields = asdict(result)
        row = {
            "label": fields["label"],
            "ok": fields["ok"],
            "path": fields["path"],
            "bytes": fields["bytes"],
            "seq": fields["seq"],
            "error": fields["error"],
        }
    except Exception as exc:  # noqa: BLE001 — §1.4: a snapshot raise never aborts
        row = {
            "label": "normalize00_before",
            "ok": False,
            "path": None,
            "bytes": 0,
            "seq": None,
            "error": f"snapshot raised: {exc!r}",
        }
    warning = None
    if not row["ok"]:
        warning = f"normalize00_before snapshot failed: {row.get('error')}"
    return row, warning


# --------------------------------------------------------------------------- #
# MFE de-dupe / inert-upgrade (locked §3.1 step 3 / §5 de-dupe E9).
# --------------------------------------------------------------------------- #
def _find_existing_bound(mfe, operand_token, surface, *, strict=False):
    """Find an existing operand of ``operand_token`` whose Surf cell == ``surface``.

    Scans ``1..NumberOfOperands`` (``GetOperandAt(i)``), reading the operand TYPE
    (via ``TypeName``) and the Surf cell (``GetCellAt(2).IntegerValue``, probe Q6 —
    NOT the typed Surf1 prop). Returns the matching operand row or ``None``.

    Two read-failure regimes (a shared helper split):

    - ``strict=False`` (DE-DUPE/WRITE path, ``_ensure_bound``): a read failure is
      SWALLOWED — an unreadable count -> ``None`` (nothing to de-dupe), a malformed
      row -> skipped. ``None`` drives a fresh ``_add_bound_operand``, which is the
      idempotent, read-back-proven safe fallback. Tolerance is CORRECT on the write
      path: refusing here would block a recoverable fresh add.

    - ``strict=True`` (STATE-VERIFY path, ``_gap_is_bounded``): a read EXCEPTION is
      INDETERMINATE and must NOT be coerced to "no match" -> ``False`` (that would
      mislabel a possibly-bounded gap as unbounded). It re-raises as a STRUCTURED
      ``SurfaceWriteError`` ("refuse rather than guess"). NOTE: only an actual read
      EXCEPTION raises — a read that SUCCEEDS and yields ``NumberOfOperands == 0``
      (or no row whose type+Surf cell matches) is a legitimate ABSENT bound and
      still returns ``None`` (never a raise).
    """
    try:
        count = int(mfe.NumberOfOperands)
    except Exception as exc:  # noqa: BLE001 — see strict/tolerant split below
        if strict:
            raise SurfaceWriteError(
                f"could not read NumberOfOperands while verifying the {operand_token} "
                f"bound on surface {surface} ({exc!r}); the bound state is "
                "indeterminate — refusing rather than guessing it unbounded (H-A)",
                field="bound_operand_count",
                intended=None,
                actual=None,
                surface=surface,
            ) from exc
        return None  # tolerant (de-dupe): an unreadable count -> nothing to de-dupe
    for i in range(1, count + 1):
        try:
            op = mfe.GetOperandAt(i)
            type_name = str(op.TypeName)
            if type_name != operand_token:
                continue
            surf_cell = int(op.GetCellAt(2).IntegerValue)
        except Exception as exc:  # noqa: BLE001 — see strict/tolerant split below
            if strict:
                raise SurfaceWriteError(
                    f"could not read operand row {i} while verifying the "
                    f"{operand_token} bound on surface {surface} ({exc!r}); the bound "
                    "state is indeterminate — refusing rather than guessing it "
                    "unbounded (H-A)",
                    field="bound_operand_row",
                    intended=None,
                    actual=None,
                    surface=surface,
                ) from exc
            continue  # tolerant (de-dupe): skip a row we cannot read
        if surf_cell == surface:
            return op
    return None


def _editor_or_raise(system, attr, field):
    """Fetch an engine EDITOR handle (``system.LDE`` / ``system.MFE``); a getter THROW
    -> ``SurfaceWriteError``.

    ``_normalize_impl`` reads ``system.LDE`` and
    ``system.MFE`` as RAW property getters BEFORE any local firewall. A .NET throw on
    either getter is neither ``ToolParamError`` nor ``SurfaceWriteError``, so it escapes
    ``normalize_stop`` as the dispatch ``internal`` family — on EVERY call. This helper
    resolves a getter throw to the structured ``surface_write`` envelope every other
    engine interaction on the path already uses ("refuse rather than guess"). ``field``
    is ``"lde_handle"`` / ``"mfe_handle"`` for the diagnostic envelope.
    """
    try:
        return getattr(system, attr)
    except Exception as exc:  # noqa: BLE001 — a getter THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not acquire the {attr} editor handle ({exc!r}); the normalize is "
            "unverifiable — refusing rather than guessing the engine is responsive",
            field=field,
            intended=None,
            actual=None,
            surface=None,
        ) from exc


def _surface_count_or_raise(lde, surface):
    """Read ``lde.NumberOfSurfaces`` as an int; a read THROW -> ``SurfaceWriteError``.

    The surface-count reads in the committed vertex refactor are raw
    LDE reads. A THROW (no local catch) would escape ``normalize_stop`` as dispatch
    ``internal``; this resolves it to the structured ``surface_write`` envelope the
    committed-refactor regime uses (consistent with ``_material_is_air`` /
    ``_gap_thickness``).
    """
    try:
        return int(lde.NumberOfSurfaces)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read NumberOfSurfaces during the vertex refactor ({exc!r}); "
            "the refactor is unverifiable — refusing rather than guessing the count",
            field="count",
            intended=None,
            actual=None,
            surface=surface,
        ) from exc


def _get_surface_or_raise(lde, idx, field, surface=None):
    """Fetch ``lde.GetSurfaceAt(idx)`` as a row; a THROW -> ``SurfaceWriteError``.

    EVERY ``lde.GetSurfaceAt(...)`` ROW FETCH on the
    committed-refactor / write path is a raw .NET read. A THROW on the fetch CALL
    itself (evaluated BEFORE any field-guard like ``_material_is_air`` can wrap the
    subsequent ``.Material`` read) would have no local catch and would escape
    ``normalize_stop`` as dispatch ``internal``. This single shared helper resolves a
    fetch THROW to the structured ``surface_write`` envelope the committed-refactor
    regime uses — the same firewall ``_classify_stop`` / ``_select_host_gap`` already
    apply to their row fetches. ALL raw row fetches on the refactor path route through
    here. ``surface`` (defaulting to ``idx``) is the surface number carried on the
    structured error for the diagnostic envelope.
    """
    try:
        return lde.GetSurfaceAt(idx)
    except Exception as exc:  # noqa: BLE001 — a fetch THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not fetch surface {idx} during the vertex refactor ({exc!r}); the "
            "refactor is unverifiable — refusing rather than guessing the surface row",
            field=field,
            intended=None,
            actual=None,
            surface=idx if surface is None else surface,
        ) from exc


def _row_material_or_raise(row, idx, field="material"):
    """Read ``row.Material`` as a str; a THROW -> ``SurfaceWriteError``.

    The diagnostic re-reads of ``.Material`` (to
    build the failure message + the structured ``actual``) are raw .NET reads
    evaluated WHILE constructing the ``SurfaceWriteError`` — a THROW there fires
    before the error object exists and would escape as dispatch ``internal`` on the
    committed-refactor path. Read the value INTO a local through this guard first,
    then interpolate the local — a Material read THROW resolves to ``surface_write``.
    """
    try:
        return str(row.Material)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read the Material of surface {idx} during the vertex refactor "
            f"({exc!r}); the refactor is unverifiable — refusing rather than guessing",
            field=field,
            intended=None,
            actual=None,
            surface=idx,
        ) from exc


def _operand_number_or_raise(operand_row, operand_token, surface):
    """Read ``operand_row.OperandNumber`` as an int; a read THROW -> ``SurfaceWriteError``.

    ``OperandNumber`` is a raw .NET read on the de-dupe SKIP /
    UPGRADE return paths. A THROW here (no local catch) would escape ``normalize_stop``
    as dispatch ``internal``; this resolves it to the structured ``surface_write``
    envelope like every other read on the write/refactor surface.
    """
    try:
        return int(operand_row.OperandNumber)
    except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
        raise SurfaceWriteError(
            f"could not read the OperandNumber of the existing {operand_token} bound "
            f"on surface {surface} ({exc!r})",
            field="bound_operand_number",
            intended=None,
            actual=None,
            surface=surface,
        ) from exc


def _bound_is_armed(existing, surface, *, strict):
    """True iff ``existing`` is an ARMED protective bound for ``surface``.

    A bound only PROTECTS a gap when its Surf1->Surf2 range is
    ARMED, i.e. ``Surf1 == surface AND Surf2 == surface AND Target > 0`` (LIVE
    finding: ``Surf2 == 0`` is an INERT empty range that does NOT constrain the
    gap). ``_find_existing_bound`` matched on Surf1 only; this completes the test by
    also reading the Surf2 cell (``GetCellAt(3)``) and the Target. The caller has
    already confirmed ``Surf1 == surface`` (that is how ``existing`` was found), so
    here we verify Surf2 and Target.

    Two read-failure regimes (mirroring ``_find_existing_bound``):

    - ``strict=True`` (STATE-VERIFY, ``_gap_is_bounded``): a Surf2/Target read
      EXCEPTION is INDETERMINATE and re-raises as a STRUCTURED ``SurfaceWriteError``
      ("refuse rather than guess") — never silently coerced to "not armed" (which
      would mislabel a possibly-protected gap as unbounded).
    - ``strict=False`` (DE-DUPE/WRITE, ``_ensure_bound``): a read EXCEPTION is
      SWALLOWED and treated as NOT armed -> the operand is upgraded+armed (the
      idempotent, read-back-proven safe path), consistent with the tolerant de-dupe.
    """
    try:
        surf2_cell = int(existing.GetCellAt(3).IntegerValue)
        target = float(existing.Target)
    except Exception as exc:  # noqa: BLE001 — strict: indeterminate; tolerant: treat as not-armed
        if strict:
            raise SurfaceWriteError(
                f"could not read the Surf2/Target of the matched bound on surface "
                f"{surface} ({exc!r}); the armed-range state is indeterminate — "
                "refusing rather than guessing it protective",
                field="bound_armed_range",
                intended=None,
                actual=None,
                surface=surface,
            ) from exc
        return False
    return surf2_cell == surface and target > 0


def _ensure_bound(mfe, system, operand_token, surface, min_air):
    """Ensure ONE positive-Min bound of ``operand_token`` on ``surface`` (§3.1/§5).

    De-dupe (E9): if an operand of this type already has ``Surf1 == surface``:

    - ARMED (``Surf2 == surface`` AND Target ``> 0``) -> SKIP (a real protective
      bound already exists, no duplicate); returns ``{"action": "skip", ...}``.
    - NOT armed (inert Target ``<= 0`` AND/OR an UNARMED ``Surf2 != surface`` —
      including the INERT empty range ``Surf2 == 0``) -> UPGRADE: set its
      Target to ``min_air`` AND ARM its Surf2 cell to ``surface``, then read-back +
      verify ALL THREE cells (turning the inert/empty-range bound into a live armed
      single-gap range); returns ``{"action": "upgrade", ...}``.

    Absent -> add it via ``_add_bound_operand`` (cell-aware, read-back-proven); returns
    ``{"action": "add", ...}``. The returned dict carries ``operand``/``surface``/
    ``target``/``operand_number`` for the result ``bounds`` list, plus ``changed``
    (True iff a write happened — drives the noop-vs-bounds-added action verdict).

    A Surf1-only match is NOT enough to call a gap protected — the
    Surf1->Surf2 range must be ARMED. A legacy/manual operand with ``Surf1 ==
    surface``, ``Target > 0`` but ``Surf2 == 0`` (the documented INERT empty range)
    is NOT skipped: it falls through to the UPGRADE path, which ARMS Surf2 (and
    read-back-proves it) so the gap is actually constrained.
    """
    existing = _find_existing_bound(mfe, operand_token, surface)
    if existing is not None:
        # SKIP only if the existing range is ARMED (Surf2 == surface AND
        # Target > 0) — NOT merely Surf1 + Target>0. The de-dupe (write-path) regime
        # is tolerant: an unreadable Surf2/Target -> NOT armed -> fall through to the
        # idempotent upgrade+arm (the read-back-proven safe path).
        if _bound_is_armed(existing, surface, strict=False):
            try:
                current_target = float(existing.Target)
            except Exception:  # noqa: BLE001 — armed already proved Target>0; defensive
                current_target = min_air
            return {
                "action": "skip",
                "changed": False,
                "operand": operand_token,
                "surface": surface,
                "target": safe_float(current_target),
                "operand_number": _operand_number_or_raise(existing, operand_token, surface),
            }
        # Inert/unarmed existing bound -> upgrade its Target to min_air AND arm its
        # Surf2 cell to ``surface`` (an unarmed Surf2 makes an EMPTY range
        # that does NOT protect the gap; arming it is the single-gap range).
        #
        # These are WRITE-time mutators. ``existing.Target
        # = min_air`` and ``existing.GetCellAt(3).IntegerValue = surface`` are RAW .NET
        # writes (the setter / cell-write CALL itself); a .NET THROW on the WRITE has no
        # local catch and escapes ``normalize_stop`` as dispatch ``internal``. Wrap the
        # write SEQUENCE in a guarded block that re-raises a STRUCTURED ``SurfaceWriteError``
        # on any throw (a value MISMATCH is still caught DISTINCTLY by the read-back
        # ``_verify_or_raise`` checks below — write-THROW and value-MISMATCH are BOTH
        # surface_write but via different mechanisms, both now covered).
        try:
            existing.Target = min_air
            existing.GetCellAt(3).IntegerValue = surface   # ARM Surf2 (single-gap range)
        except Exception as exc:  # noqa: BLE001 — a write THROW -> surface_write, never internal
            raise SurfaceWriteError(
                f"could not write the upgraded Target/Surf2 of the {operand_token} bound "
                f"on surface {surface} ({exc!r}); the upgrade write was rejected by the "
                "engine — refusing rather than shipping a possibly-inert bound",
                field="bound_upgrade_write",
                intended=min_air,
                actual=None,
                surface=surface,
            ) from exc
        # The read-back READS (``float(existing.Target)``,
        # ``int(existing.GetCellAt(2)/(3).IntegerValue)``) are raw .NET reads on the
        # WRITE path. ``_verify_or_raise`` converts only a value MISMATCH to
        # ``SurfaceWriteError``; a read THROW here would escape as dispatch
        # ``internal``. Read each value INTO a local inside a try/except that re-raises
        # a STRUCTURED ``SurfaceWriteError`` on a throw, THEN feed the local to
        # ``_verify_or_raise`` (a genuine mismatch still raises, unchanged).
        try:
            upgraded_target = float(existing.Target)
        except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
            raise SurfaceWriteError(
                f"could not read back the upgraded Target of the {operand_token} bound "
                f"on surface {surface} ({exc!r}); the upgrade is unverifiable — "
                "refusing rather than shipping a possibly-inert bound",
                field="bound_target",
                intended=min_air,
                actual=None,
                surface=surface,
            ) from exc
        _lc._verify_or_raise("bound_target", min_air, upgraded_target, surface=surface)
        # Read-back-as-proof consistency: re-verify the matched
        # Surf1 cell too, don't trust it. A stale/silent-no-op Surf cell (probe
        # Q6) would mean we just "upgraded" an operand that does NOT target this gap.
        try:
            surf_cell_actual = int(existing.GetCellAt(2).IntegerValue)
        except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
            raise SurfaceWriteError(
                f"could not read back the Surf cell of the upgraded {operand_token} "
                f"bound on surface {surface} ({exc!r}); the upgrade is unverifiable — "
                "refusing rather than guessing it targets this gap",
                field="bound_surf_cell",
                intended=surface,
                actual=None,
                surface=surface,
            ) from exc
        _lc._verify_or_raise(
            "bound_surf_cell", surface, surf_cell_actual, surface=surface,
        )
        # Read-back-PROVE the newly-armed Surf2 cell. A
        # silent-no-op on Surf2 (probe Q6) would leave the range EMPTY -> the inert
        # bug re-opens; a read THROW -> surface_write, a mismatch -> surface_write.
        try:
            surf2_cell_actual = int(existing.GetCellAt(3).IntegerValue)
        except Exception as exc:  # noqa: BLE001 — a read THROW -> surface_write, never internal
            raise SurfaceWriteError(
                f"could not read back the Surf2 cell of the upgraded {operand_token} "
                f"bound on surface {surface} ({exc!r}); the arm is unverifiable — "
                "refusing rather than shipping a possibly-empty-range bound",
                field="bound_surf2_cell",
                intended=surface,
                actual=None,
                surface=surface,
            ) from exc
        _lc._verify_or_raise(
            "bound_surf2_cell", surface, surf2_cell_actual, surface=surface,
        )
        return {
            "action": "upgrade",
            "changed": True,
            "operand": operand_token,
            "surface": surface,
            "target": safe_float(min_air),
            "operand_number": _operand_number_or_raise(existing, operand_token, surface),
        }

    operand_number = _sc._add_bound_operand(
        mfe, system, operand_token, surface, min_air
    )
    return {
        "action": "add",
        "changed": True,
        "operand": operand_token,
        "surface": surface,
        "target": safe_float(min_air),
        "operand_number": operand_number,
    }


# --------------------------------------------------------------------------- #
# Compose helpers (free a gap / set a planar AIR thickness) — read-back-proven.
# --------------------------------------------------------------------------- #
def _free_gap(session, surface):
    """Make the thickness cell of ``surface`` Variable (composes set_variable).

    Idempotent: an already-Variable cell read-backs Variable and is fine. Inherits
    set_variable's read-back firewall (a silent no-op -> ``SurfaceWriteError``).
    """
    _ov.set_variable(session, {"surface": surface, "cell": "thickness"})


def _gap_is_variable(system, surface):
    """True iff ``surface``'s thickness cell reads back a Variable solve.

    Firewall: the state-verify path (``_free_post_state_
    complete``) calls this on a fail-closed/refusal path. A transient ThicknessCell /
    GetSolveData read failure must NOT escape the tool's ``surface_write`` firewall as
    an opaque ``internal`` crash — it resolves to a STRUCTURED ``SurfaceWriteError``,
    exactly as ``_material_is_air`` does for a Material read. A genuine non-Variable
    cell (read succeeds, solve is not Variable) still returns ``False`` — only a
    READ FAILURE raises.
    """
    variable_member = _ov._oc._solve_type_variable_enum(system)
    try:
        cell = system.LDE.GetSurfaceAt(surface).ThicknessCell
        solve_name = _ov._solve_type_name(cell)
    except Exception as exc:  # noqa: BLE001 — propagate as structured, never internal
        raise SurfaceWriteError(
            f"could not read the thickness solve on surface {surface} ({exc!r}); the "
            "free-stop state is indeterminate — refusing rather than guessing",
            field="thickness_solve",
            intended=None,
            actual=None,
            surface=surface,
        )
    return solve_name == str(variable_member)


def _gap_thickness(system, surface):
    """Read ``surface``'s thickness as a float (sign + value proof).

    Firewall: the ``_normalize_free`` non-positive entry-gap
    check (and the vertex re-reads) call this on fail-closed paths. A transient
    ``.Thickness`` read failure resolves to a STRUCTURED ``SurfaceWriteError`` (so the
    handler tags ``surface_write``), never an opaque ``internal`` crash — mirroring
    ``_select_host_gap``, which already fail-closes the host-thickness read.
    """
    try:
        return float(system.LDE.GetSurfaceAt(surface).Thickness)
    except Exception as exc:  # noqa: BLE001 — propagate as structured, never internal
        raise SurfaceWriteError(
            f"could not read the thickness on surface {surface} ({exc!r}); the air-gap "
            "state is indeterminate — refusing rather than guessing",
            field="thickness",
            intended=None,
            actual=None,
            surface=surface,
        )


def _gap_is_bounded(mfe, operand_token, surface):
    """True iff an ARMED positive-Min bound of ``operand_token`` protects ``surface``.

    State-verification: reuses the de-dupe scan (``_find_existing_bound``) and
    treats only a present operand whose Surf1->Surf2 range is ARMED — ``Surf1 ==
    surface AND Surf2 == surface AND Target > 0`` — as a real bound. An inert
    ``<= 0`` Target OR an UNARMED ``Surf2 != surface`` (the INERT empty range,
    incl. ``Surf2 == 0``) does NOT protect the gap, so it does not count (a
    Surf1-only / Target>0-only match is NOT sufficient).

    Firewall: EVERY MFE read on this state-verify path is INDETERMINATE on failure and must NOT
    be silently coerced to ``False`` (which would mislabel a possibly-fully-enforced
    gap as unbounded, flipping the verdict to ``noop_enforcement_skipped`` on a
    transient MFE-read hiccup). The FIND step (``_find_existing_bound(...,
    strict=True)`` — the ``NumberOfOperands`` count read and each per-row Surf1-cell
    read) AND the armed-range read (the Surf2 cell + Target,
    via ``_bound_is_armed(..., strict=True)``) resolve to a
    STRUCTURED ``SurfaceWriteError`` (the same "refuse rather than guess" thesis the
    other reads — ``_gap_is_variable`` / ``_gap_thickness`` / ``_material_is_air`` —
    uphold), surfaced by the handler as the ``surface_write`` envelope. A genuine
    absent OR unarmed bound (every read SUCCEEDS, but no matching ARMED operand —
    including a cleanly EMPTY MFE where ``NumberOfOperands == 0``) still returns
    ``False`` — only a read EXCEPTION raises.
    """
    existing = _find_existing_bound(mfe, operand_token, surface, strict=True)
    if existing is None:
        return False
    return _bound_is_armed(existing, surface, strict=True)


def _free_post_state_complete(system, mfe, operand_token, gap_a, gap_b):
    """True iff the free stop is GENUINELY convention-compliant (verified state).

    ``noop_already_free`` is defined (module docstring / §1.2) as "free-standing AND
    both gaps already bounded". This reads the LIVE state UNCONDITIONALLY (not gated
    on the flags): the convention holds only when BOTH adjacent gaps are positively
    bounded AND Variable. So ``noop_already_free`` can be claimed only when the
    system truly is already free — never merely because ``add_bounds=False`` /
    ``free_gaps=False`` suppressed the writes (the false-no-op). An incomplete
    state with no write falls through to the honest ``noop_enforcement_skipped``.
    """
    for s in (gap_a, gap_b):
        if not _gap_is_variable(system, s):
            return False
        if not _gap_is_bounded(mfe, operand_token, s):
            return False
    return True


# --------------------------------------------------------------------------- #
# The handler.
# --------------------------------------------------------------------------- #
def normalize_stop(session, params):
    """Enforce the dummy-stop convention on the current system's sole stop.

    See the module docstring for the three outcomes + the fail-closed families.
    EXPECTED failures (no stop / no airspace / bad param / a mid-refactor read-back
    mismatch) are returned as the locked ``{ok:false}`` envelope — never raised
    past this boundary.

    ORDERING GOTCHA: the bounds this tool adds live on the MFE. Call
    ``build_merit`` BEFORE ``normalize_stop`` (or re-run ``normalize_stop`` after each
    ``build_merit``) — the SEQ-wizard ``Apply()``/``OK()`` inside ``build_merit``
    REPLACES the operand list and wipes any bounds added earlier. See the module
    docstring.
    """
    # Track whether a rollback snapshot was ACTUALLY captured. Threaded into
    # _normalize_impl, populated only when _snapshot_before returns an ok row, and
    # read by the surface_write handler so the envelope NEVER advertises a rollback
    # artifact that does not exist.
    snap_state = {"label": None, "warning": None}
    try:
        return _normalize_impl(session, params, snap_state)
    except ToolParamError as exc:
        return error_envelope("normalize_stop", "normalize_param", str(exc))
    except SurfaceWriteError as exc:
        # A mid-refactor read-back mismatch. Carry the snapshot label for
        # caller rollback ONLY when a snapshot row was truly captured; else set
        # it null + warn that no rollback artifact exists. Do NOT auto-revert (a
        # silent revert is itself a surprise).
        return error_envelope(
            "normalize_stop",
            "surface_write",
            str(exc),
            field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
            snapshot=snap_state["label"],
            warning=snap_state["warning"],
        )


def _normalize_impl(session, params, snap_state=None):
    """The normalize_stop body (see ``normalize_stop`` for the contract)."""
    system = session.system
    # ``system.LDE`` / ``system.MFE`` are RAW property
    # getters evaluated BEFORE any firewall — a .NET throw on either would escape as
    # dispatch ``internal`` on every call. Route them through ``_editor_or_raise`` so a
    # getter throw resolves to the structured ``surface_write`` envelope.
    lde = _editor_or_raise(system, "LDE", "lde_handle")
    mfe = _editor_or_raise(system, "MFE", "mfe_handle")
    if snap_state is None:
        snap_state = {"label": None, "warning": None}

    bound_kind, operand_token, min_air, add_bounds, free_gaps = _parse_params(params)

    # (§2) detect the stop + its classification BEFORE any mutation.
    stop_idx, classification = _sc._classify_stop(lde)
    if classification == "no_stop":
        return error_envelope(
            "normalize_stop",
            "normalize_no_stop",
            "no surface reports IsStop (an afocal/stopless system has no aperture "
            "stop to normalize)",
        )
    if classification == _sc._INDETERMINATE:
        # A Material read failed -> we cannot classify the stop. Fail-closed as
        # a structured surface_write envelope (the read-back firewall shape); NEVER
        # mutate (no snapshot, no insert) on an indeterminate read. A snapshot was
        # not taken, so no rollback artifact exists -> snapshot=None + a warning.
        return error_envelope(
            "normalize_stop",
            "surface_write",
            "the aperture stop classification is indeterminate (a surface Material "
            "read failed); refusing to mutate rather than guessing a vertex stop. "
            "Retry when the engine is responsive.",
            field="material",
            surface=stop_idx,
            snapshot=None,
            warning="no normalize00_before snapshot was taken (refused before any "
            "mutation); no rollback artifact exists",
        )

    # (§1.4) snapshot BEFORE the first mutation.
    snap_meta = {
        "classification_before": classification,
        "stop_before": stop_idx,
        "bound_kind": bound_kind,
        "min_air": min_air,
    }
    snap_row, snap_warning = _snapshot_before(session, snap_meta)
    # Advertise the snapshot label in a later surface_write envelope ONLY if a
    # snapshot row was TRULY captured (sink present AND ok). A None sink or an
    # ok=False row leaves the label null + threads a warning that no rollback
    # artifact exists — the caller is never told a rollback exists when it does not.
    if snap_row is not None and snap_row.get("ok"):
        # Thread the ACTUAL captured label from the row (not a re-stated
        # constant) so a label-rewriting custom sink cannot desync the advertised
        # rollback label from the real artifact. The in-tree ArtifactSink echoes
        # "normalize00_before" verbatim, so this is identical for the shipped sink.
        snap_state["label"] = snap_row["label"]
        snap_state["warning"] = None
    else:
        snap_state["label"] = None
        snap_state["warning"] = (
            snap_warning
            or "no normalize00_before snapshot was captured; no rollback artifact "
            "exists"
        )

    if classification == "free_airspace":
        return _normalize_free(
            session, system, lde, mfe, stop_idx, operand_token, min_air,
            add_bounds, free_gaps, snap_warning,
        )
    # classification == "on_glass_vertex"
    return _normalize_vertex(
        session, system, lde, mfe, stop_idx, operand_token, min_air,
        add_bounds, free_gaps, snap_warning,
    )


def _normalize_free(session, system, lde, mfe, stop_idx, operand_token, min_air,
                    add_bounds, free_gaps, snap_warning):
    """Free-airspace entry path (locked §3.1) — the stop is NOT moved (Fork 3)."""
    # The two adjacent air gaps: object-side (predecessor's thickness leading in)
    # and image-side (the stop's own thickness). Both are air (free-standing).
    gap_before_surface = stop_idx - 1
    gap_after_surface = stop_idx
    warning = snap_warning
    # Track gap-freeing SEPARATELY from bound-adding so the
    # action label reflects what actually happened. ``noop_bounds_added`` must be
    # claimed ONLY when a bound was truly added/upgraded — freeing a gap with
    # add_bounds=False adds ZERO bounds and must not borrow that label.
    bounds_changed = False
    gaps_freed = False

    # Flag a non-positive entry gap (locked §4 "negative existing gap at entry"):
    # never silently "fix" geometry — bound it positive + WARN.
    nonpos = [
        s for s in (gap_before_surface, gap_after_surface)
        if _gap_thickness(system, s) <= 0
    ]
    if nonpos:
        warning = _join_warning(
            warning,
            f"non-positive entry air gap(s) on surface(s) {nonpos}; bounded "
            "positive but the geometry was not silently altered",
        )

    # (§3.1 step 2) ensure each adjacent gap is Variable (idempotent).
    if free_gaps:
        for s in (gap_before_surface, gap_after_surface):
            if not _gap_is_variable(system, s):
                _free_gap(session, s)
                gaps_freed = True
            # read-back-proven Variable (set_variable already raises on no-op).

    # (§3.1 step 3) ensure a positive-Min bound on each gap (de-dupe / inert-upgrade).
    bounds = []
    if add_bounds:
        for s in (gap_before_surface, gap_after_surface):
            res = _ensure_bound(mfe, system, operand_token, s, min_air)
            if res["changed"]:
                bounds_changed = True
            bounds.append(
                {
                    "operand": res["operand"],
                    "surface": res["surface"],
                    "target": res["target"],
                    "operand_number": res["operand_number"],
                }
            )

    # The action must reflect TRUE post-state, not just "did we write". The
    # write outcome splits into HONEST labels:
    #   - a bound was added/upgraded -> noop_bounds_added (a positive-Min bound was
    #     authored; this is the headline change, whether or not a gap was also freed).
    #   - NO bound added but a gap was freed (free_gaps=True, add_bounds=False) ->
    #     noop_gaps_freed (ZERO bounds were added, so it must NOT be
    #     mislabeled noop_bounds_added — the label names the work that happened).
    #   - NO write at all splits into two honest cases:
    #     * the stop is genuinely "already free" ONLY if BOTH gaps are actually
    #       bounded (positive Min) AND (when free_gaps is on) Variable -> the verified
    #       noop_already_free (free-standing AND both gaps already bounded).
    #     * if enforcement was SUPPRESSED by add_bounds=False / free_gaps=False and the
    #       post-state is NOT fully bounded+variable, the system is NOT "already free";
    #       relabel honestly to noop_enforcement_skipped + WARN what was left undone.
    if bounds_changed:
        action = "noop_bounds_added"
    elif gaps_freed:
        action = "noop_gaps_freed"
    elif _free_post_state_complete(
        system, mfe, operand_token, gap_before_surface, gap_after_surface,
    ):
        action = "noop_already_free"
    else:
        action = "noop_enforcement_skipped"
        warning = _join_warning(
            warning,
            "the free stop is NOT fully enforced (a gap is unbounded and/or not "
            f"Variable) but the work was suppressed by add_bounds={add_bounds} / "
            f"free_gaps={free_gaps}; re-run with the flags enabled to enforce the "
            "dummy-stop convention",
        )

    return {
        "ok": True,
        "action": action,
        "classification_before": "free_airspace",
        "classification_after": "free_airspace",
        "stop_before": stop_idx,
        "stop_after": stop_idx,           # the stop is NOT moved (Fork 3)
        "dummy_surface": None,
        "gap_before_surface": gap_before_surface,
        "gap_after_surface": gap_after_surface,
        "freed": _freed_list(free_gaps, gap_before_surface, gap_after_surface),
        "bounds": bounds,
        "warning": warning,
    }


def _normalize_vertex(session, system, lde, mfe, stop_idx, operand_token, min_air,
                      add_bounds, free_gaps, snap_warning):
    """On-glass-vertex refactor: insert + split + move stop + free + bound (§3.2)."""
    warning = snap_warning

    # Choose the host air gap (image-side preferred). Fail-closed on a
    # cemented interior vertex / object/inf gap / out-of-bound insert.
    host = _sc._select_host_gap(lde, stop_idx)
    if not host["ok"]:
        # D4 (Bug 4): a FRONT-VERTEX stop (the stop's only adjacent air is the
        # OBJECT/inf gap) is reseated onto a fresh dummy AIR surface AHEAD of the
        # front glass — there is no adjacent finite air gap to split. The selector
        # flags this case with ``front_vertex:True``. Mis-detect guard: only route
        # here when the stop is genuinely on a glass vertex whose predecessor is the
        # OBJECT/air at index 0 (``stop_idx == 1``) — a cemented INTERIOR vertex
        # (glass on both sides, host_gap_surface != 0) still fail-closes
        # ``normalize_no_airspace``.
        if host.get("front_vertex") and _is_front_vertex(lde, stop_idx):
            return _normalize_front_vertex(
                session, system, lde, mfe, stop_idx, snap_warning,
            )
        return error_envelope(
            "normalize_stop", "normalize_no_airspace", host["reason"],
            stop_surface=stop_idx,
        )

    host_gap_surface = host["host_gap_surface"]
    insert_at = host["insert_at"]
    t = host["thickness"]
    half = t / 2.0

    # BUG-1 (locked §4 "Negative existing gap at entry"): detect a non-positive
    # HOST/entry air gap UP FRONT — symmetric with _normalize_free, which already
    # WARNs on a <=0 entry gap. Splitting a non-positive host into two non-positive
    # halves is degenerate (a later >0 check would trip with a generic
    # "split air gap is non-positive" that looks like an engine write-rejection).
    # Fail-closed BEFORE any mutation with a SPECIFIC reason NAMING the pre-existing
    # non-positive entry gap, so the caller can tell a degenerate-input refusal from
    # a read-back firewall trip — never silently "fix" the geometry (probe (d)).
    if not (t > 0):
        raise SurfaceWriteError(
            f"the pre-existing host air gap on surface {host_gap_surface} (the entry "
            f"gap to split) is non-positive ({t}); refusing to split a non-positive "
            "entry gap into two non-positive halves — fix the negative gap first "
            "(the geometry was not silently altered)",
            field="entry_gap_thickness",
            intended="> 0",
            actual=t,
            surface=host_gap_surface,
        )

    # The count reads are raw LDE reads on the committed-refactor
    # path. A read THROW must resolve to surface_write (the committed-refactor regime),
    # never escape as dispatch internal.
    n_before = _surface_count_or_raise(lde, insert_at)

    # Insert the dummy — composes insert_surface (the crash-bound firewall
    # already guarantees 1 <= insert_at <= N-1 or it raises first). The count
    # grew by 1 (insert_surface's own read-back proves it).
    _ls.insert_surface(session, {"at": insert_at})
    n_after = _surface_count_or_raise(lde, insert_at)
    if n_after != n_before + 1:                                   # defense in depth
        raise SurfaceWriteError(
            f"dummy insert at {insert_at} did not grow the surface count "
            f"({n_before} -> {n_after})",
            field="count", intended=n_before + 1, actual=n_after, surface=insert_at,
        )

    # Index arithmetic after the insert (downstream surfaces shifted +1):
    #   image-side: host_gap_surface == stop_idx (unchanged; dummy is at stop_idx+1).
    #     gap-before-dummy = stop_idx (the old host, now leading INTO the dummy),
    #     dummy = stop_idx + 1, gap-after-dummy = the dummy's own thickness.
    #   object-side: insert_at == stop_idx, so the dummy lands at stop_idx and the
    #     old stop shifts to stop_idx + 1. gap-before-dummy = stop_idx - 1 (the old
    #     host, unchanged index), dummy = stop_idx.
    dummy_idx = insert_at
    gap_before_dummy = insert_at - 1     # the pre-dummy air gap (the old host gap)
    gap_after_dummy = insert_at          # the dummy's OWN thickness cell

    # Split the air thickness t -> t/2, t/2, both planar AIR. Composes
    # set_surface (radius=inf; a fresh Standard surface defaults to AIR Material="").
    # Each write is _verify_or_raise-proven by set_surface; re-reads here too.
    _ls.set_surface(session, {"surface": gap_before_dummy, "thickness": half})
    _ls.set_surface(session, {"surface": dummy_idx, "thickness": half, "radius": float("inf")})

    # The dummy is AIR (Material == "").
    # Fetch the dummy ROW ONCE through the guarded helper (a
    # GetSurfaceAt THROW -> surface_write, never internal), and read its Material into
    # a guarded local BEFORE building any diagnostic — the previous code evaluated
    # ``lde.GetSurfaceAt(dummy_idx)`` and ``.Material`` raw, THREE times, while
    # constructing the error, so a throw on any of those escaped as dispatch internal.
    dummy_row = _get_surface_or_raise(lde, dummy_idx, "material")
    if not _sc._material_is_air(dummy_row):
        dummy_material = _row_material_or_raise(dummy_row, dummy_idx)
        raise SurfaceWriteError(
            f"dummy surface {dummy_idx} is not AIR after insert "
            f"(Material={dummy_material!r})",
            field="material", intended="", actual=dummy_material,
            surface=dummy_idx,
        )
    # Both split thicknesses re-read == t/2 (and > 0).
    for s in (gap_before_dummy, dummy_idx):
        actual = _gap_thickness(system, s)
        _lc._verify_or_raise("split_thickness", half, actual, surface=s)
        if not (actual > 0):
            raise SurfaceWriteError(
                f"split air gap on surface {s} is non-positive ({actual})",
                field="thickness", intended=half, actual=actual, surface=s,
            )

    # Move the stop onto the dummy — composes set_stop_surface (moves IsStop
    # + auto-clears the old vertex stop; its full-scan read-back proves exactly
    # one stop, on the dummy).
    _ls.set_stop_surface(session, {"surface": dummy_idx})

    # Free both adjacent gaps (set_variable read-back-proven).
    if free_gaps:
        for s in (gap_before_dummy, gap_after_dummy):
            _free_gap(session, s)
            # The freed gap thickness is > 0 (probe (d): the engine won't
            # protect the sign).
            actual = _gap_thickness(system, s)
            if not (actual > 0):
                raise SurfaceWriteError(
                    f"freed air gap on surface {s} is non-positive ({actual})",
                    field="thickness", intended=half, actual=actual, surface=s,
                )

    # Add the positive-Min bound on BOTH gaps (de-dupe / cell-aware).
    bounds = []
    if add_bounds:
        for s in (gap_before_dummy, gap_after_dummy):
            res = _ensure_bound(mfe, system, operand_token, s, min_air)
            bounds.append(
                {
                    "operand": res["operand"],
                    "surface": res["surface"],
                    "target": res["target"],
                    "operand_number": res["operand_number"],
                }
            )

    # LOAD-BEARING: re-run _classify_stop -> must read "free_airspace". Trust
    # the classification read, not the index arithmetic.
    new_stop_idx, classification_after = _sc._classify_stop(lde)
    if classification_after != "free_airspace" or new_stop_idx != dummy_idx:
        raise SurfaceWriteError(
            f"post-refactor classification is {classification_after!r} with stop on "
            f"surface {new_stop_idx} (expected free_airspace on the dummy "
            f"{dummy_idx})",
            field="classification", intended="free_airspace", actual=classification_after,
            surface=dummy_idx,
        )

    return {
        "ok": True,
        "action": "refactored_split",
        "classification_before": "on_glass_vertex",
        "classification_after": "free_airspace",
        "stop_before": stop_idx,
        "stop_after": dummy_idx,
        "dummy_surface": dummy_idx,
        "gap_before_surface": gap_before_dummy,
        "gap_after_surface": gap_after_dummy,
        "freed": _freed_list(free_gaps, gap_before_dummy, gap_after_dummy),
        "bounds": bounds,
        "warning": warning,
    }


def _is_front_vertex(lde, stop_idx):
    """True iff the stop is a FRONT-vertex stop: on a glass vertex whose predecessor
    is the OBJECT/air at index 0 (D4 mis-detect guard).

    The dedicated front-dummy reseat is valid ONLY for a stop on the FRONT lens
    vertex — i.e. ``stop_idx == 1`` (predecessor is OBJECT index 0), the stop's OWN
    material is glass (it is on a glass vertex), and the predecessor (OBJECT) is air.
    A cemented INTERIOR vertex (glass on both sides, or a stop deeper than surface 1)
    is NOT a front vertex and must keep fail-closing ``normalize_no_airspace``.

    All reads route through the committed-refactor firewall (``_get_surface_or_raise``
    + ``_material_is_air``) so a .NET throw resolves to ``surface_write``, never escapes
    as dispatch ``internal``.
    """
    if stop_idx != 1:
        return False  # predecessor must be the OBJECT at index 0
    stop_row = _get_surface_or_raise(lde, stop_idx, "material")
    prev_row = _get_surface_or_raise(lde, stop_idx - 1, "material")
    # The stop is on a glass vertex (its OWN material is glass) and its predecessor
    # (the OBJECT) is air. ``_material_is_air`` raises (-> surface_write) on a read
    # failure, never silently fabricates "not air".
    stop_is_glass = not _sc._material_is_air(stop_row)
    prev_is_air = _sc._material_is_air(prev_row)
    return stop_is_glass and prev_is_air


def _normalize_front_vertex(session, system, lde, mfe, stop_idx, snap_warning):
    """Reseat a FRONT-vertex stop onto a fresh dummy AIR surface AHEAD of the glass (D4).

    There is NO freeable adjacent air gap (the only adjacent air is the OBJECT/inf
    gap), so this is a pure stop-only reseat — NO ``_free_gap`` / ``_ensure_bound`` /
    MNEA-MNCA bounds. Sequence (probe F6, live-proven):

    1. ``insert_surface(at=stop_idx)`` — the dummy lands at ``stop_idx``; the glass
       stop shifts to ``stop_idx+1``.
    2. ``set_surface(stop_idx, radius=inf, thickness=0.0)`` — a fresh Standard surface
       defaults to AIR (Material="").
    3. ``set_stop_surface(stop_idx)`` — moves the stop onto the dummy, auto-clears the
       old vertex stop.

    Then the read-back gates: count == before+1; dummy is AIR; dummy
    thickness reads back == 0.0 (NOT collapsed/merged by the engine); exactly ONE
    stop (set_stop_surface's full-scan); LOAD-BEARING ``_classify_stop`` ->
    (``free_airspace``, stop_idx). Any gate mismatch -> ``SurfaceWriteError`` (the
    handler's ``surface_write`` envelope; the D2 ``apply_lens_spec`` rollback if reached
    through apply).
    """
    n_before = _surface_count_or_raise(lde, stop_idx)

    # (1) insert the dummy at the stop index (glass shifts to stop_idx+1). The
    # crash-bound firewall in insert_surface guarantees 1 <= stop_idx <= N-1 or raises.
    _ls.insert_surface(session, {"at": stop_idx})
    n_after = _surface_count_or_raise(lde, stop_idx)
    if n_after != n_before + 1:                                   # count grew by 1
        raise SurfaceWriteError(
            f"front-vertex dummy insert at {stop_idx} did not grow the surface count "
            f"({n_before} -> {n_after})",
            field="count", intended=n_before + 1, actual=n_after, surface=stop_idx,
        )

    dummy_idx = stop_idx

    # (2) configure the dummy: radius inf, thickness 0 (pure reseat — no prescription
    # nudge; the read-back gates cover the engine-collapse risk). A fresh Standard surface is AIR.
    _ls.set_surface(
        session, {"surface": dummy_idx, "thickness": 0.0, "radius": float("inf")}
    )

    # The dummy is AIR (Material == "").
    dummy_row = _get_surface_or_raise(lde, dummy_idx, "material")
    if not _sc._material_is_air(dummy_row):
        dummy_material = _row_material_or_raise(dummy_row, dummy_idx)
        raise SurfaceWriteError(
            f"front-vertex dummy surface {dummy_idx} is not AIR after insert "
            f"(Material={dummy_material!r})",
            field="material", intended="", actual=dummy_material, surface=dummy_idx,
        )

    # The dummy thickness reads back == 0.0 (the engine did NOT collapse/merge it).
    dummy_thickness = _gap_thickness(system, dummy_idx)
    _lc._verify_or_raise("dummy_thickness", 0.0, dummy_thickness, surface=dummy_idx)

    # (3) move the stop onto the dummy — composes set_stop_surface (moves IsStop +
    # auto-clears the old vertex stop; its full-scan read-back proves exactly one
    # stop, on the dummy).
    _ls.set_stop_surface(session, {"surface": dummy_idx})

    # LOAD-BEARING: re-run _classify_stop -> must read "free_airspace" on the
    # dummy. Trust the classification read, not the index arithmetic.
    new_stop_idx, classification_after = _sc._classify_stop(lde)
    if classification_after != "free_airspace" or new_stop_idx != dummy_idx:
        raise SurfaceWriteError(
            f"post-reseat classification is {classification_after!r} with stop on "
            f"surface {new_stop_idx} (expected free_airspace on the dummy {dummy_idx})",
            field="classification", intended="free_airspace",
            actual=classification_after, surface=dummy_idx,
        )

    warning = _join_warning(
        snap_warning,
        "stop reseated onto a new dummy AIR vertex; no airspace bounds added (the "
        "front element has no freeable air gap)",
    )
    return {
        "ok": True,
        "action": "refactored_front_dummy",
        "classification_before": "on_glass_vertex",
        "classification_after": "free_airspace",
        "stop_before": stop_idx,
        "stop_after": dummy_idx,
        "dummy_surface": dummy_idx,
        "gap_before_surface": None,
        "gap_after_surface": None,
        "freed": [],
        "bounds": [],
        # Fix 4: the dummy was INSERTED at ``stop_idx``, so every surface at index
        # >= stop_idx shifted +1 (the old front glass is now at stop_idx+1). Disclose
        # the convention so a caller holding pre-reseat indices can re-map them.
        "index_shift": {"from": stop_idx, "delta": 1},
        "warning": warning,
    }


def _freed_list(free_gaps, gap_a, gap_b):
    """Build the ``freed`` result list (empty when free_gaps is off)."""
    if not free_gaps:
        return []
    return [
        {"surface": gap_a, "cell": "thickness"},
        {"surface": gap_b, "cell": "thickness"},
    ]


def _join_warning(existing, new):
    """Append ``new`` to ``existing`` (either may be None)."""
    if existing:
        return f"{existing}; {new}"
    return new


NORMALIZE_STOP_SPEC = ToolSpec(
    name="normalize_stop",
    handler=normalize_stop,
    required_params=(),
    param_types={
        "bound_kind": "string",
        "min_air": "number",
        "add_bounds": "boolean",
        "free_gaps": "boolean",
    },
    description=(
        "Free a glass-vertex aperture stop so it can be optimized: make it a "
        "free-standing dummy AIR surface (splitting an adjacent air gap if needed), "
        "free both adjacent gaps, and add a positive-Min air boundary on each so they "
        "cannot collapse. Run this before optimize/dry_run, which refuse a glass-vertex "
        "stop. Gotcha: call build_merit BEFORE normalize_stop — the merit wizard "
        "replaces the operand list and would wipe these bounds. See dry_run, optimize, "
        "build_merit."
    ),
)

TOOL_SPECS = (NORMALIZE_STOP_SPEC,)
