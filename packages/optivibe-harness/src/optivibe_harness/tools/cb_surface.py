"""tools/cb_surface.py — the coordinate-break primitive floor.

THREE dispatchable surface-level authoring tools (NOT a LensSpec extension — the flat
SurfaceSpec schema cannot carry surface type / Par cells):

- ``add_coordinate_break`` — ChangeType a surface to a CoordinateBreak + write the six
  Par cells (Decenter X/Y, Tilt About X/Y/Z, Order) type-aware, with TWO-TIER proof:
  (a) cell read-back of every written value (via ``_cb_cells.write_cb_cell``); (b) the
  DECISIVE global-frame gate — ``global_rotation_angles`` after the CB must reflect the
  authored tilt within 1e-9 deg (the only proof the change took — the typed-setter trap
  would read the cell back while the frame did not move; we never use the typed setter,
  but the gate is the belt-and-suspenders).
- ``set_cb_variable`` — make a Double CB Par cell an optimizer Variable, proved via the
  optimizer's own ``opt.Variables`` increment. REFUSES ``param == "order"`` AND any cell
  whose live ``cell.DataType == "Integer"`` (BOTH checks — the DataType check is the
  load-bearing one, L30; the engine silently accepts a variable on the Integer Order
  cell and the read-back cleanly reports Variable, Q5, so the read-back is NOT a safety
  net).
- ``add_return_cb`` — author the ATOMIC INVERSE of an entry CB: a ``SurfacePickup``
  (scale -1) on each of Par1-Par5 (decenter + tilt) that TRACKS the entry live, plus
  the return Order = ``1 - entry_order`` written as a LITERAL (NOT a pickup — Order is
  the Integer cell). Discloses ``index_shift`` + a LOUD warning that a later upstream
  ``InsertNewSurfaceAt`` desyncs the by-number pickup. Does NOT claim on-axis
  restoration after a propagation gap (Q4 — the clean inverse is exact only for
  co-located CBs).

Every handler returns the uniform never-raise envelope and NEVER raises past its
boundary (the L26 firewall): an EXPECTED failure (bad param / out-of-range / a
read-back mismatch) is a structured ``{ok:false}`` dict; an unexpected engine throw is
caught broad and resolved to the ``surface_write`` family.

Live ZOS-API integration: exercised by the live CB test; unit-tested
against the fixture-seeded fake LDE/cell doubles.
"""
from ..errors import SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _cb_cells as _cb
from . import _lens_common as _lc
from ._analysis_common import error_envelope


_CB_AUTHOR = "cb_author"          # ChangeType / Par-write firewall family
_CB_PROOF = "cb_proof"            # the global-frame gate family
_CB_PARAM = "cb_param"            # a bad param value family
_CB_VARIABLE_INT = "cb_variable_integer_cell"  # §3 refusal family

# The recovery text for a PART-WAY ``add_coordinate_break``.
#
# It must NOT assert that the surface IS now a CoordinateBreak: on the
# R0 path — a ``ChangeType`` that threw — that outcome is UNKNOWN, and this cycle
# exists to stop a surface certifying more than it measured. The certainty is carried
# by the FIELD (``committed`` = read-back-confirmed, ``attempted`` = outcome unknown),
# never by the prose, and the prose sends the caller to ``read_surface`` to find out.
#
# It must also NAME THE COST of each recovery route: the re-drive fixes
# the coordinate break but does NOT restore the surface's pre-call optical role, the
# reload discards every edit made since the last save, and a design that was never
# saved has NEITHER route. The draft offered the reload as if it were free.
#
# The LEAD-IN is PER-PATH (/ C2-3). "authored PART-WAY" is a CLAIM
# that something landed; on the path where ``committed`` is EMPTY nothing has read back
# and the only thing observed is that a call threw. Serving the PART-WAY lead-in there
# is the prose over-claiming exactly what the two-ledger split was built to stop. The
# TAIL (read-back-first, the two routes, their costs, the never-saved case) is IDENTICAL
# on both paths — this is a per-path lead-in, NOT a deletion of the phrase.
_CB_RECOVERY_PARTWAY = (
    "the coordinate break was authored PART-WAY: `committed` lists the sub-steps that "
    "READ BACK as done; `attempted` (present only when it applies) lists a sub-step "
    "whose outcome is UNKNOWN because the call threw during it. "
)
_CB_RECOVERY_UNCONFIRMED = (
    "NO sub-step of this coordinate break read back as done (`committed` is empty) and "
    "one sub-step's outcome is UNKNOWN because the call threw during it (`attempted`), "
    "so whether the surface was mutated AT ALL is not established. "
)
_CB_RECOVERY_TAIL = (
    "Read the surface back "
    "with read_surface(surface) before deciding — if it reads CoordinateBreak the "
    "system will also read FOLDED (check_clearance / get_first_order). To recover, "
    "EITHER re-drive add_coordinate_break on the same surface with corrected values "
    "(this fixes the coordinate break; it does NOT restore the surface's pre-call "
    "optical role), OR load_design your saved baseline, which DISCARDS every edit made "
    "since that save. If this design was never saved, neither route restores the "
    "pre-call state — save_snapshot before authoring coordinate breaks."
)
_CB_RECOVERY = _CB_RECOVERY_PARTWAY + _CB_RECOVERY_TAIL


def _cb_recovery(committed, attempted):
    """The CB recovery text whose LEAD-IN matches what the ledgers establish (C2-3).

    ``committed`` empty AND ``attempted`` non-empty is the one path where NOTHING read
    back — claiming the coordinate break "was authored PART-WAY" there asserts a
    mutation the code never observed. Everywhere else at least one sub-step DID read
    back, so "authored PART-WAY" is TRUE and keeps being served (``test_x4c`` pins that
    complement: this is a per-path lead-in, not a blanket strip).
    """
    if attempted and not committed:
        return _CB_RECOVERY_UNCONFIRMED + _CB_RECOVERY_TAIL
    return _CB_RECOVERY


# --------------------------------------------------------------------------- #
# Shared validation helpers.
# --------------------------------------------------------------------------- #
def _require_dict(params):
    """Never-raise (L26): a non-dict ``params`` becomes ``{}`` (defense in depth)."""
    return params if isinstance(params, dict) else {}


def _surface_in_geometry_range(lde, surface):
    """Bounds-check ``1 <= surface <= N-1`` (the geometry firewall, client-side).

    OBJECT (0) is refused (you do not retype the object surface); IMAGE (N-1) is
    allowed. Raises ``ToolParamError`` BEFORE any engine touch — a CB is a geometry
    mutation, so it inherits the geometry index range. Returns the validated index.
    """
    n = int(lde.NumberOfSurfaces)
    _lc._require_geometry_index(surface, n)
    return surface


def _finite_number(value, label):
    """Require a FINITE number (reject bool / non-number / inf / nan) -> float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"{label} must be a finite number, got {type(value).__name__} {value!r}"
        )
    import math
    coerced = float(value)
    if not math.isfinite(coerced):
        raise ToolParamError(
            f"{label} must be a finite number (inf/-inf/nan are non-physical), got "
            f"{value!r}"
        )
    return coerced


def _require_order(value, label="order"):
    """Require an Order flag in {0, 1}, coercing an INTEGRAL float (the number contract).

    ``order`` is advertised as ``param_types["order"] == "number"``, so the MCP/JSON
    round-trip delivers an integral float (``1`` -> ``1.0``). Per the
    "number = handler accepts integral float" contract (the SAME coercion ``surface``
    uses via ``_require_int_index``), an integral float ``0.0``/``1.0`` is accepted and
    coerced to an int; ``1.5`` (non-integral), ``"1"`` (a string), ``True`` (a bool),
    NaN/inf, and an order outside {0, 1} are still rejected LOUD.
    """
    import math
    if isinstance(value, bool):
        raise ToolParamError(
            f"{label} must be an integer 0 or 1, not a bool ({value!r})"
        )
    if isinstance(value, int):
        coerced = value
    elif isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            coerced = int(value)
        else:
            raise ToolParamError(
                f"{label} must be an integer 0 or 1, got non-integral float {value!r}"
            )
    else:
        raise ToolParamError(
            f"{label} must be an integer 0 or 1, got {type(value).__name__} {value!r}"
        )
    if coerced not in (0, 1):
        raise ToolParamError(
            f"{label} must be 0 (decenter-then-tilt) or 1 (tilt-then-decenter), got "
            f"{coerced}"
        )
    return coerced


# =========================================================================== #
# §2. add_coordinate_break
# =========================================================================== #
def add_coordinate_break(session, params):
    """ChangeType a surface to a CoordinateBreak + write its six Par cells (§2).

    Params: ``surface`` (int, REQUIRED), ``decenter_x``/``decenter_y``/``tilt_x``/
    ``tilt_y``/``tilt_z`` (float, default 0.0), ``order`` (0 or 1, default 0).

    Validate-then-open: bounds-check ``surface`` (client-side firewall before any
    engine touch), reject ``order`` not in {0,1} LOUD, reject non-finite numerics
    LOUD. Then ChangeType + write the six cells (each read-back-proven via
    ``write_cb_cell``). TWO-TIER proof: the cell read-backs PLUS the decisive
    global-frame gate (the measured ``GetGlobalMatrix`` rotation block matches the
    authored tilt+order matrix PRODUCT within 1e-9, element-wise). NEVER raises past
    the boundary.
    """
    params = _require_dict(params)
    # TWO ledgers, threaded exactly as ``add_return_cb`` threads one. There
    # is NO rollback (DQ-4: a SaveAs checkpoint un-blesses the loaded design and a
    # targeted value-restore silently drops a Fixed solve) — what ships instead is an
    # HONEST disclosure of what was left behind, with the CERTAINTY split across the
    # two ledgers: ``committed`` holds only read-back-CONFIRMED sub-steps, ``attempted``
    # holds a sub-step whose outcome is unknown because the call threw during it.
    committed, attempted = [], []
    try:
        return _add_coordinate_break_impl(session, params, committed, attempted)
    except ToolParamError as exc:
        return error_envelope(
            "add_coordinate_break", _CB_PARAM, str(exc),
            **_partial_state_fields(
                committed, _cb_recovery(committed, attempted), attempted),
        )
    except SurfaceWriteError as exc:
        return error_envelope(
            "add_coordinate_break", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
            **_partial_state_fields(
                committed, _cb_recovery(committed, attempted), attempted),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> surface_write (L26)
        return error_envelope(
            "add_coordinate_break", "surface_write",
            f"unexpected engine fault authoring the coordinate break ({exc!r}); "
            "refusing rather than shipping an unverified surface",
            **_partial_state_fields(
                committed, _cb_recovery(committed, attempted), attempted),
        )


def _add_coordinate_break_impl(session, params, committed, attempted):
    system = session.system
    lde = system.LDE

    surface = _lc._require_int_index(params, "surface")
    _surface_in_geometry_range(lde, surface)

    n = int(lde.NumberOfSurfaces)
    # The geometry firewall (_surface_in_geometry_range -> _lens_common._require_geometry_index)
    # allows N-1, which is correct for set_surface/substitute_glass and WRONG here: a CB at
    # the IMAGE surface has no downstream frame to verify, so _verify_global_frame refuses
    # AFTER the retype and all six cell writes have already landed. Both sibling tools
    # already carry this refusal (set_mirror, set_diffraction_grating) and fold_beam refuses
    # > n-2 outright; add_coordinate_break was the only member of its family missing it.
    # Refuse HERE — zero mutation.
    #
    # The "do NOT use N-2" clause is NOT padding: the probe measured
    # add_coordinate_break(surface=7) SUCCEEDING while destroying the design (EFFL
    # 50.04 -> 119.88, field-1 RMS 5.47 -> 2763.66 um). This tool RETYPES in place, it
    # does not insert, so the obvious remedial advice trades a loud failure for a silent
    # one. The message names insert_surface instead; OF-11 measured the capability cost
    # of that route at ZERO (a byte-identical optical system, proof_ok true).
    if surface == n - 1:
        raise ToolParamError(
            f"surface {surface} is the IMAGE surface; a coordinate break there has no "
            "downstream surface, so its global frame cannot be verified — refusing "
            "BEFORE any mutation. add_coordinate_break RETYPES a surface in place, it "
            f"does NOT insert, so authoring at surface {n - 2} instead MAY silently "
            "destroy a live optic — this tool never READ that surface, so its optical "
            "role is unmeasured; what IS measured (the probe 4.2) is that the retype "
            "succeeds while destroying a design: add_coordinate_break(surface=7) on a "
            "real Cooke triplet returned ok:true with EFFL 50.04 -> 119.88 and field-1 "
            f"RMS 5.47 -> 2763.66 um. Use insert_surface(at={n - 1}) to make room, then "
            f"add_coordinate_break on the NEW surface {n - 1}."
        )

    written = {
        "decenter_x": _finite_number(params.get("decenter_x", 0.0), "decenter_x"),
        "decenter_y": _finite_number(params.get("decenter_y", 0.0), "decenter_y"),
        "tilt_x": _finite_number(params.get("tilt_x", 0.0), "tilt_x"),
        "tilt_y": _finite_number(params.get("tilt_y", 0.0), "tilt_y"),
        "tilt_z": _finite_number(params.get("tilt_z", 0.0), "tilt_z"),
        "order": _require_order(params.get("order", 0)),
    }

    # ChangeType -> CoordinateBreak (THROW-guarded -> surface_write) + read-back proof.
    cb_member = _cb._surface_type_coordinate_break(system)
    row = lde.GetSurfaceAt(surface)
    # The settings READ and the retype MUTATION are different
    # operations and get different ledger treatment. ``GetSurfaceTypeSettings`` is a
    # read: if it throws, ``ChangeType`` was NEVER attempted, so filing the retype as
    # "outcome UNKNOWN" there tells the user its outcome is unknown when it provably
    # never ran. The read is guarded WITHOUT a ledger entry -> ``partial_state: False``,
    # which is true and matches the refusal message served below.
    try:
        settings = row.GetSurfaceTypeSettings(cb_member)
    except Exception as exc:  # noqa: BLE001 — a settings READ throw, zero mutation
        raise SurfaceWriteError(
            f"could not read the CoordinateBreak surface-type settings for surface "
            f"{surface} ({exc!r}); the retype was NEVER attempted — refusing rather "
            "than authoring on an un-retyped surface",
            field="surface_type_settings", intended="CoordinateBreak", actual=None,
            surface=surface,
        ) from exc
    # LOAD-BEARING: file the retype as ATTEMPTED **before** the
    # call that can mutate. R0 is the one path where the retype's outcome is genuinely
    # unknown — a ChangeType that threw may or may not have mutated the row — and a bare
    # ``partial_state: False`` there would be a fresh false-clean. It is filed under
    # ``attempted``, NEVER ``committed``: nothing has read back yet.
    attempted.append(
        f"changetype(surface={surface}) — outcome UNKNOWN if this call threw"
    )
    try:
        row.ChangeType(settings)
    except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> surface_write
        raise SurfaceWriteError(
            f"could not ChangeType surface {surface} to a coordinate break ({exc!r}); "
            "refusing rather than authoring on an un-retyped surface",
            field="changetype", intended="CoordinateBreak", actual=None,
            surface=surface,
        ) from exc
    # Read-back-as-proof: the surface is genuinely a CoordinateBreak (a ChangeType
    # that silently no-opped would leave a Standard surface whose Par cells do not
    # exist — caught here before any cell write).
    row = lde.GetSurfaceAt(surface)
    if not _cb.is_coordinate_break(row):
        raise SurfaceWriteError(
            f"surface {surface} is not a coordinate break after ChangeType — the "
            "retype silently no-opped; refusing rather than writing Par cells to a "
            "non-CB surface",
            field="surface_type", intended="CoordinateBreak", actual=None,
            surface=surface,
        )
    # The retype READ BACK as a CoordinateBreak, so its outcome is no longer unknown:
    # promote it to ``committed`` and CLEAR ``attempted``. Without the clear, every
    # later failure envelope (a cell write, the frame gate) would carry a permanent
    # false "outcome unknown" for a retype that provably took (T37b).
    committed.append(f"changetype(surface={surface}) CONFIRMED")
    attempted.clear()

    # (a) write the six Par cells type-aware, each read-back-proven (write_cb_cell).
    #
    # The SAME two-ledger discipline the
    # retype uses, applied uniformly. ``write_cb_cell`` WRITES FIRST and reads back
    # AFTER (``_cb_cells.write_cb_cell``), so a setter that mutates and then throws, a
    # read-back that throws after a landed write, and an engine that clamps to a wrong
    # value ALL leave the cell mutated while the call raises. Appending only on return
    # filed those in NEITHER ledger — the envelope told the caller the cell was
    # untouched when it may have been written, and the printed recovery contract
    # (``attempted`` "lists a sub-step") was false on exactly those paths. The entry is
    # filed as ATTEMPTED before the call and PROMOTED to ``committed`` on return; the
    # in-flight entry is removed rather than the list cleared, so a stale entry from an
    # earlier sub-step could never be silently swallowed here.
    for param, value in written.items():
        entry = f"cell {param}={value!r}"
        attempted.append(entry)
        _cb.write_cb_cell(system, row, param, value)
        committed.append(entry)
        attempted.remove(entry)

    # (b) THE DECISIVE global-frame gate (§2): the POST-CB surface's measured rotation
    # block must equal the authored tilt+order matrix PRODUCT within 1e-9 (element-wise).
    # The CB redirects the DOWNSTREAM frame, so the proof reads GetGlobalMatrix of the
    # surface AFTER the CB (surface+1). A cell that reads back while the frame did not
    # move is the typed-setter trap — we never use the typed setter, but the gate is
    # belt-and-suspenders.
    global_frame = _verify_global_frame(system, lde, surface, written)

    return {
        "ok": True,
        "surface": surface,
        "written": written,
        "global_frame": global_frame,
        "proof_ok": True,
    }


def _verify_global_frame(system, lde, surface, written):
    """Verify the post-CB global frame reflects the authored tilt (§2 gate).

    Reads the RAW ``GetGlobalMatrix`` rotation block of the surface AFTER the CB (the
    frame the CB redirects) AND the UPSTREAM frame (the CB surface itself), then runs
    the RELATIVE COMPOSE-AND-COMPARE oracle: the expected 3x3 rotation is built as a
    matrix PRODUCT from the authored ``(tilt_x, tilt_y, tilt_z, order)`` (Order-branched
    — ``Rx.Ry.Rz`` for order 0, ``Rz.Ry.Rx`` for order 1) and compared ELEMENT-WISE to
    THIS CB's CONTRIBUTION ``R_upstream^T . R_measured`` within ``GLOBAL_FRAME_TOL_DEG``
    (BUG-1 fix: the prior ABSOLUTE compare assumed an identity upstream and false-refused
    a SECOND/compound CB whose upstream frame already carries the prior fold's rotation;
    a still-earlier atan2-decompose gate false-refused combined multi-axis tilts AND
    single-axis tilts > 180 deg). For a FIRST CB the upstream frame is identity so the
    relative compare reduces EXACTLY to the absolute one. A mismatch -> ``SurfaceWriteError``
    (cb_proof). Decenter does NOT affect the rotation block (proven), so it is verified
    by the cell read-back, never here. Returns the global-frame dict for the envelope
    (the single-axis angle READOUT is informational display only, NOT the oracle).

    The post-CB surface is ``surface + 1``. The CALLER-FACING firewall in
    ``_add_coordinate_break_impl`` now refuses ``surface == N-1`` (the IMAGE surface)
    BEFORE any mutation, so a well-formed call cannot reach this branch. It is RETAINED
    as defence-in-depth for a surface count that changed under the tool (an engine-side
    truncation between the pre-condition read and this read), and because
    ``_verify_global_frame`` is a shared proof helper that must not assume its caller
    pre-checked. (The prior docstring here asserted "a CB is never the IMAGE surface —
    the geometry firewall allowed surface <= N-1", which states the conclusion and then
    cites the premise that defeats it: N-1 IS the IMAGE surface. Fixed.
    ``_surface_in_geometry_range``'s own docstring is ACCURATE and must not be touched —
    N-1 is legitimate for the ``set_surface``/``substitute_glass`` range it states.)
    """
    n = int(lde.NumberOfSurfaces)
    post = surface + 1
    if post > n - 1:
        # A CB with no downstream surface cannot have its frame verified — refuse
        # rather than claim proof on an un-checkable change.
        raise SurfaceWriteError(
            f"coordinate break on surface {surface} has no downstream surface "
            f"(post-CB surface {post} > IMAGE {n - 1}); cannot verify the global "
            "frame — refusing rather than claiming an unverified proof",
            field="global_frame", intended=post, actual=n - 1, surface=surface,
        )

    # Read the RAW row-major rotation block + translation slots.
    measured_R, x, y, z = _cb.read_global_matrix(system, lde, post)

    # The compound-fold false-refusal + REGRESSION fix: the post-CB
    # measured block is the LIVE upstream frame PRE-MULTIPLIED by this CB's contribution
    # — ``R_measured = R_upstream . R_thisCB``. The absolute ``rotation_residual`` assumes
    # R_upstream is IDENTITY, which holds ONLY for a FIRST CB; for a SECOND/compound CB
    # the prior fold's rotation is carried in R_upstream, so the absolute compare
    # FALSE-REFUSES. We must read the UPSTREAM frame that EXCLUDES this CB's own rotation.
    #
    # The decisive live fact (from the global-matrix probe capture): ``GetGlobalMatrix(s)``
    # at the CB surface ALREADY INCLUDES that CB's own rotation (the prisms capture shows
    # the CoordinateBreak at i=3 carrying the non-identity R(45) at its OWN matrix, while
    # i=2 — the surface BEFORE the CB — is identity). So reading upstream at ``surface``
    # would DOUBLE-REMOVE this CB's rotation (``R_thisCB^T . (R_upstream . R_thisCB)`` !=
    # R_thisCB unless R_thisCB commutes), false-refusing even a SINGLE fold (residual
    # sin45 = 0.707). The correct upstream frame is ``GetGlobalMatrix(surface - 1)`` — the
    # surface BEFORE the CB: IDENTITY for a first CB, R(prior) for a compound CB. Live-
    # verified to 1.1e-16 for both single and compound. We then verify THIS CB's
    # CONTRIBUTION ``R_upstream^T . R_measured`` (transpose == inverse for an orthonormal
    # rotation) against the authored tilt product. For an identity upstream this reduces
    # EXACTLY to the original absolute check (backward-compat). The upstream read is
    # THROW-guarded; a non-orthonormal upstream block fails CLOSED (relative residual inf).
    #
    # Edge (self-adversary, L26): ``surface - 1`` is the OBJECT (0) when surface == 1 —
    # the OBJECT's global matrix is identity live-consistent (the capture's i=0/i=1 carry
    # the identity R block), so the relative compare reduces to the absolute one, which is
    # correct for a first CB. ``surface - 1`` can never be negative here (the geometry
    # firewall already rejected surface < 1 via ``_surface_in_geometry_range`` -> OBJECT
    # refused), so the index is always a valid >= 0 read. A degraded / throwing / non-
    # success upstream read fails CLOSED (``read_global_matrix`` raises ``CBCellError`` ->
    # the cb_proof refusal), never a silent pass.
    upstream = surface - 1
    upstream_R, _ux, _uy, _uz = _cb.read_global_matrix(system, lde, upstream)

    # THE DECISIVE GATE: this CB's CONTRIBUTION (the measured block with the upstream
    # frame removed) must equal the matrix PRODUCT of the authored tilts composed per
    # Order, element-wise within the 1e-9 gate. This is a strict superset of single-axis
    # (a pure tilt_x composes as Rx.Ry(0).Rz(0)) and is immune to atan2 wraparound (no
    # decomposition). The decenter never enters the rotation block, so it is proved by
    # the cell read-back, not here.
    residual = _cb.relative_rotation_residual(
        upstream_R, measured_R, written["tilt_x"], written["tilt_y"],
        written["tilt_z"], written["order"],
    )
    if residual > _cb.GLOBAL_FRAME_TOL_DEG:
        raise SurfaceWriteError(
            f"coordinate-break global-frame proof failed: this CB's contribution "
            f"(the post-CB rotation block with the live upstream frame removed) does "
            f"not match the authored tilt product (tilt_x={written['tilt_x']}, "
            f"tilt_y={written['tilt_y']}, tilt_z={written['tilt_z']}, "
            f"order={written['order']}); max element-wise residual {residual} > "
            f"{_cb.GLOBAL_FRAME_TOL_DEG} — the coordinate change did not take (the "
            "typed-setter no-op trap?); refusing rather than shipping an unverified "
            "surface",
            field="global_frame", intended=0.0, actual=residual, surface=surface,
        )

    # Informational single-axis READOUT for the envelope (DISPLAY ONLY — NOT the oracle,
    # which is the matrix compare above). R7 (spec): the decisive gate at the
    # residual compare has ALREADY passed by this point, so a fault in this read must
    # DEGRADE THE READOUT, not discard a CB that was just proven correct — the probe 
    # measured the discarded CB byte-identical to a success control, with the frame
    # re-reading identically to the last digit.
    #
    # This does NOT weaken _verify_global_frame: the residual compare above is untouched
    # and still raises (T41 is the mutate-fails guard for exactly that).
    readout_unavailable = False
    try:
        tilt_x, tilt_y, tilt_z, _x, _y, _z = _cb.global_rotation_angles(
            system, lde, post
        )
    except Exception:  # noqa: BLE001 — a display-only read fault degrades the readout
        tilt_x = tilt_y = tilt_z = None
        readout_unavailable = True
    frame = {
        "tilt_x": _cb_safe(tilt_x), "tilt_y": _cb_safe(tilt_y),
        "tilt_z": _cb_safe(tilt_z), "x": _cb_safe(x), "y": _cb_safe(y),
        "z": _cb_safe(z), "rotation_residual": _cb_safe(residual),
    }
    # Emitted ONLY when it fires (L-6): the healthy block stays BYTE-IDENTICAL, which
    # matters because place_element and fold_beam read ``global_frame``.
    if readout_unavailable:
        frame["readout_unavailable"] = True
    return frame


def _cb_safe(value):
    """JSON-safe float (NaN/inf -> string sentinel via the tier-wide safe_float)."""
    from .._io import safe_float
    return safe_float(value)


# =========================================================================== #
# §3. set_cb_variable
# =========================================================================== #
def set_cb_variable(session, params):
    """Make a Double CB Par cell an optimizer Variable, opt.Variables-proven (§3).

    Params: ``surface`` (int, REQUIRED), ``param`` (str, REQUIRED — one of
    decenter_x/decenter_y/tilt_x/tilt_y/tilt_z; NOT order).

    REFUSES ``param == "order"`` AND any cell whose live ``cell.DataType ==
    "Integer"`` (BOTH checks — the DataType check is the load-bearing one per L30; the
    name check is the friendly one). Structured refusal ``cb_variable_integer_cell``,
    zero mutation. On a Double cell: ``cell.MakeSolveVariable()``, then read back the
    solve Type == Variable AND confirm the optimizer's ``opt.Variables`` incremented
    (the Q5 falsification — a flag that only reads back Variable is NOT a real DOF).
    NEVER raises past the boundary.
    """
    params = _require_dict(params)
    try:
        return _set_cb_variable_impl(session, params)
    except ToolParamError as exc:
        return error_envelope("set_cb_variable", _CB_PARAM, str(exc))
    except SurfaceWriteError as exc:
        return error_envelope(
            "set_cb_variable", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> surface_write (L26)
        return error_envelope(
            "set_cb_variable", "surface_write",
            f"unexpected engine fault setting the coordinate-break variable ({exc!r}); "
            "refusing rather than shipping an unverified DOF",
        )


def _set_cb_variable_impl(session, params):
    system = session.system
    lde = system.LDE

    surface = _lc._require_int_index(params, "surface")
    _surface_in_geometry_range(lde, surface)

    param = params.get("param")
    if param not in _cb._PARAM_NAMES:
        raise ToolParamError(
            f"param must be one of {list(_cb._PARAM_NAMES)}, got {param!r}"
        )

    row = lde.GetSurfaceAt(surface)
    if not _cb.is_coordinate_break(row):
        raise SurfaceWriteError(
            f"surface {surface} is not a coordinate break; cannot set a CB variable "
            "on it (author it with add_coordinate_break first)",
            field="surface_type", intended="CoordinateBreak", actual=None,
            surface=surface,
        )

    # §3 REFUSAL — BOTH checks. (1) the friendly name check: order is never a variable.
    if param == "order":
        return error_envelope(
            "set_cb_variable", _CB_VARIABLE_INT,
            "the Order parameter is the discrete decenter/tilt-order flag (0/1), not a "
            "continuous optimizer DOF; refusing a variable solve on it (zero mutation). "
            "The engine silently accepts it as a real DOF — there is no engine guard.",
            surface=surface, param=param,
        )
    # (2) THE LOAD-BEARING check (L30): refuse ANY cell whose LIVE DataType is Integer.
    # The Q5 trap — MakeSolveVariable on the Integer Order cell does NOT raise, the
    # solve reads back Variable, AND the optimizer counts it — so the read-back is NOT
    # a safety net. The ONLY guard is the live cell.DataType, read BEFORE any mutation.
    if _cb.is_integer_cb_cell(system, row, param):
        return error_envelope(
            "set_cb_variable", _CB_VARIABLE_INT,
            f"coordinate-break parameter {param!r} is an Integer cell live; refusing a "
            "variable solve on an integer cell (the engine accepts it silently as a "
            "real DOF — a nonsensical continuous variable on a discrete flag; the "
            "read-back is not a safety net, L30). Zero mutation.",
            surface=surface, param=param,
        )

    # The cell is a Double DOF. Re-fetch it BEFORE baselining the opt-count so the
    # idempotency read-back below uses the live cell.
    cell = _cb._cb_cell(system, row, param)

    # DOUBLE-VARY IDEMPOTENCY (#11 / R8, the set_asphere_variable template): an already-
    # Variable Double CB Par cell is benign — the 2nd MakeSolveVariable does NOT increment
    # opt.Variables (the var is already counted), so baselining the count first and then
    # demanding a +1 increment would mis-flag a legitimate idempotent re-var as a "phantom
    # DOF". The Integer-cell refusal (the Order flag / live DataType==Integer) already ran
    # ABOVE, so an Integer phantom never reaches here — a Double already-Variable cell IS a
    # genuine DOF. Return the honest idempotent envelope (matches set_asphere_variable).
    variable_member = _variable_member(system)
    if _solve_type_name(cell) == str(variable_member):
        return {
            "ok": True,
            "surface": surface,
            "param": param,
            "is_variable": True,
            "dof_proven": True,
            "was_variable": True,
            "variables_before": None,
            "variables_after": None,
        }

    # Baseline the optimizer var count BEFORE the solve so the increment is the proof
    # (Q5 — opt.Variables is the authority, NOT the cell read-back; ``NumberOfVariables``
    # does NOT exist on this build).
    before = _open_count_close_variables(system)
    try:
        cell.MakeSolveVariable()
    except Exception as exc:  # noqa: BLE001 — a solve THROW -> surface_write
        raise SurfaceWriteError(
            f"could not make coordinate-break parameter {param!r} a variable on "
            f"surface {surface} ({exc!r}); the engine rejected the solve",
            field="cb_variable", intended="Variable", actual=None, surface=surface,
        ) from exc

    # Read back the solve Type == Variable (the cell-level proof) ...
    variable_member = _variable_member(system)
    solve_name = _solve_type_name(cell)
    if solve_name != str(variable_member):
        raise SurfaceWriteError(
            f"coordinate-break variable solve on {param!r} (surface {surface}) did not "
            f"take effect: solve Type reads {solve_name!r} (silent no-op); refusing "
            "rather than claiming a DOF that does not exist",
            field="cb_variable", intended="Variable", actual=solve_name,
            surface=surface,
        )
    # ... AND confirm the optimizer's own DOF count incremented (the Q5 falsification:
    # a flag that only reads back Variable is NOT a real DOF). A None/ambiguous before/
    # after count degrades to a non-fatal warning (the optimizer may be unavailable);
    # the load-bearing positive proof is the increment when both counts are readable.
    after = _open_count_close_variables(system)
    # dof_proven is POSITIVELY established ONLY when BOTH counts are readable ints AND
    # the increment is exactly +1 (it must be proven, not merely "after looks
    # like an int"). Anything else is an UNPROVEN DOF.
    dof_proven = (
        isinstance(before, int) and isinstance(after, int) and after == before + 1
    )
    warning = None
    if isinstance(before, int) and isinstance(after, int) and not dof_proven:
        # The cell reads Variable but the optimizer count did NOT increment by one —
        # the Q5 silent trap (or a desync). Refuse: a DOF the optimizer does not count
        # is not a real DOF.
        raise SurfaceWriteError(
            f"coordinate-break variable on {param!r} (surface {surface}) reads back "
            f"Variable but the optimizer DOF count did not increment ({before} -> "
            f"{after}); the solve is not a real optimizer variable — refusing rather "
            "than claiming a phantom DOF",
            field="cb_variable_dof", intended=before + 1, actual=after,
            surface=surface,
        )
    # Warn whenever the increment proof was NOT positively established and no
    # raise fired — i.e. EITHER count is non-int (an asymmetric optimizer flake:
    # before=None & after=int, before=int & after=None, or both None). The prior gate
    # only warned on a non-int AFTER, so ``before=None, after=int`` slipped through as
    # a phantom CLEAN ``ok:true`` implying a proven DOF. The reject-domain is now the
    # FULL complement of dof_proven (which is already handled by the raise above only
    # in the both-int mismatch case), so every unproven-but-non-raising path warns.
    if not dof_proven:
        warning = (
            "the optimizer DOF count could not be confirmed to have incremented by "
            "one (the optimizer may have been unavailable on the baseline or post "
            f"read: before={before!r}, after={after!r}); the cell reads back Variable "
            "but the opt.Variables increment proof was NOT established — treat this "
            "DOF as UNCONFIRMED"
        )

    result = {
        "ok": True,
        "surface": surface,
        "param": param,
        "is_variable": True,
        # Surface the proof status POSITIVELY so a caller never reads a bare
        # clean ok:true as a proven DOF.
        "dof_proven": dof_proven,
        "variables_before": before if isinstance(before, int) else None,
        "variables_after": after if isinstance(after, int) else None,
    }
    if warning is not None:
        result["warning"] = warning
    return result


def _variable_member(system):
    """The live ``SolveType.Variable`` member (reused from the optimize tier)."""
    from . import _optimize_common as _oc
    return _oc._solve_type_variable_enum(system)


def _solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the read-back truth source).

    THROW-guarded -> ``SurfaceWriteError`` (a read THROW on the proof leaves the DOF
    unverifiable — refuse rather than guess it took).
    """
    try:
        return str(cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read back the solve type of a coordinate-break cell ({exc!r}); "
            "the variable is unverifiable — refusing rather than guessing it took",
            field="cb_variable", intended="Variable", actual=None, surface=None,
        ) from exc


def _open_count_close_variables(system):
    """Open the optimizer, read ``opt.Variables`` (the DOF count), close it (Q5/L22).

    A cell that merely reads back Variable is NOT proof it is a real optimizer DOF —
    ``ILocalOptimization.Variables`` (the only var-count member; ``NumberOfVariables``
    does NOT exist on this build) is the authority. Opens ONCE, reads, ``Close()`` in
    finally (the L22 single-seat reap). Returns the int count, or ``None`` if the
    optimizer is unavailable / the count is unreadable (a non-fatal degradation — the
    caller treats a None as "could not confirm the increment", never a crash).
    """
    opt = None
    try:
        opt = system.Tools.OpenLocalOptimization()
        if opt is None:
            return None
        try:
            return int(opt.Variables)
        except Exception:  # noqa: BLE001 — an unreadable count degrades to None
            return None
    except Exception:  # noqa: BLE001 — the optimizer being unavailable is non-fatal
        return None
    finally:
        if opt is not None:
            try:
                opt.Close()
            except Exception:  # noqa: BLE001 — teardown must never raise (L22)
                pass


# =========================================================================== #
# §4. add_return_cb
# =========================================================================== #
def add_return_cb(session, params):
    """Author the atomic inverse of an entry CB (pickup -1 + Order flip) (§4).

    Params: ``entry_surface`` (int, REQUIRED), ``return_surface`` (int, REQUIRED —
    ChangeType'd to a CB if it is not, disclosed in the envelope).

    Authors the return as the atomic inverse: a ``SurfacePickup`` solve (scale -1,
    ``.Surface = entry``, ``.Column = ParN``) on each of Par1-Par5 (decenter + tilt)
    that TRACKS the entry live, AND the return Order = ``1 - entry_order`` written as
    a LITERAL (NOT a pickup — Order is the Integer cell, §4 nit 3). Pickup-is-live
    proof: each return cell tracks ``-entry`` (the CELL pickup value == -entry CELL
    value, §4 nit 4 — NOT the global frame). Discloses ``index_shift`` + a LOUD
    warning that a later upstream ``InsertNewSurfaceAt`` desyncs the by-number pickup.
    Does NOT claim on-axis restoration after a propagation gap (Q4). NEVER raises.
    """
    params = _require_dict(params)
    # LOW (atomicity): a ledger the impl appends each COMMITTED sub-step to (the
    # ChangeType, each authored pickup, the Order literal). On a mid-authoring failure
    # the impl raises and we DISCLOSE the half-written state honestly on the failure
    # envelope (``partial_state`` + ``committed`` + a recovery hint) — a CB primitive is
    # a low-level building block the agent re-drives, so an honest partial disclosure is
    # the proportionate fix (vs the full SaveAs/LoadFile checkpoint apply_lens_spec uses).
    committed = []
    try:
        return _add_return_cb_impl(session, params, committed)
    except ToolParamError as exc:
        # A param/validation failure fires BEFORE any mutation (committed is empty), so
        # no partial-state disclosure is needed; if somehow non-empty, disclose it.
        return error_envelope(
            "add_return_cb", _CB_PARAM, str(exc),
            **_partial_state_fields(committed),
        )
    except SurfaceWriteError as exc:
        return error_envelope(
            "add_return_cb", getattr(exc, "error_family", "surface_write"),
            str(exc), field=getattr(exc, "field", None),
            surface=getattr(exc, "surface", None),
            **_partial_state_fields(committed),
        )
    except Exception as exc:  # noqa: BLE001 — a raw engine throw -> surface_write (L26)
        return error_envelope(
            "add_return_cb", "surface_write",
            f"unexpected engine fault authoring the return coordinate break ({exc!r}); "
            "refusing rather than shipping an unverified return CB",
            **_partial_state_fields(committed),
        )


def _partial_state_fields(committed, recovery=None, attempted=None):
    """Build the honest partial-state disclosure for a CB failure envelope.

    If NOTHING landed and nothing was attempted (the failure fired before any mutation —
    a param/validation refusal, or the pre-mutation guards), returns
    ``{"partial_state": False}`` so the envelope is unambiguous. If sub-steps DID land (a
    mid-authoring throw after the ChangeType / some pickups / the Order write), returns
    ``partial_state:True`` + the ordered ``committed`` ledger + a recovery hint — the
    editor retains those mutations and the caller must re-drive or undo them. This
    mirrors the apply_lens_spec ``partial_state`` honesty without the heavier
    SaveAs/LoadFile checkpoint (DQ-4: the checkpoint un-blesses the loaded design and a
    targeted value-restore silently drops a ``Fixed`` solve).

    **The certainty split.** ``committed`` carries ONLY sub-steps that READ
    BACK as done. A sub-step whose outcome is UNKNOWN — because the call throwing is the
    only thing we observed — goes in ``attempted``, which is emitted only when it is
    non-empty. Filing an attempt under ``committed`` would be the tool certifying more
    than it measured, which is the whole defect class this fix exists to close.

    ``add_return_cb`` passes NEITHER new argument, so its envelope keeps the same keys in
    the same order with the same text — byte-identical (pinned by the shipped adversarial
    test assertions).
    """
    if not committed and not attempted:
        return {"partial_state": False}
    out = {"partial_state": True, "committed": list(committed)}
    if attempted:                      # emitted ONLY when the uncertainty is real
        out["attempted"] = list(attempted)
    out["recovery"] = recovery or (
        "the return CB authoring failed PART-WAY — the editor retains the committed "
        "sub-steps above (a ChangeType and/or some Par-cell pickups/the Order "
        "literal). Re-author the return CB after correcting the fault, or reload "
        "your design .zmx to discard the partial state."
    )
    return out


def _add_return_cb_impl(session, params, committed):
    system = session.system
    lde = system.LDE

    entry_surface = _lc._require_int_index(params, "entry_surface")
    return_surface = _lc._require_int_index(params, "return_surface")
    _surface_in_geometry_range(lde, entry_surface)
    _surface_in_geometry_range(lde, return_surface)

    if entry_surface == return_surface:
        raise ToolParamError(
            f"entry_surface and return_surface must differ (both {entry_surface}); a "
            "CB cannot be its own inverse"
        )
    if return_surface <= entry_surface:
        raise ToolParamError(
            f"return_surface ({return_surface}) must come AFTER entry_surface "
            f"({entry_surface}); the return CB undoes the entry downstream"
        )

    entry_row = lde.GetSurfaceAt(entry_surface)
    if not _cb.is_coordinate_break(entry_row):
        raise SurfaceWriteError(
            f"the entry surface {entry_surface} is not a coordinate break; author it "
            "with add_coordinate_break before adding a return CB",
            field="surface_type", intended="CoordinateBreak", actual=None,
            surface=entry_surface,
        )

    # Read the entry Order to compute the return flip = 1 - entry_order (Q4).
    entry_order = _cb.read_cb_cell(system, entry_row, "order")
    if entry_order not in (0, 1):
        raise SurfaceWriteError(
            f"the entry CB Order on surface {entry_surface} is {entry_order!r} "
            "(expected 0 or 1); refusing to author a return whose Order flip is "
            "ambiguous",
            field="order", intended="0 or 1", actual=entry_order,
            surface=entry_surface,
        )
    return_order = 1 - entry_order  # §4 nit 3: a LITERAL, never a pickup.

    # ChangeType the return surface to a CB if it is not already one (disclosed). The
    # on-the-fly ChangeType is THROW-guarded (nit 2: the same never-raise net as the
    # entry path) + read-back-proven (is_coordinate_break after).
    return_row = lde.GetSurfaceAt(return_surface)
    changed_type = False
    if not _cb.is_coordinate_break(return_row):
        cb_member = _cb._surface_type_coordinate_break(system)
        try:
            settings = return_row.GetSurfaceTypeSettings(cb_member)
            return_row.ChangeType(settings)
        except Exception as exc:  # noqa: BLE001 — a ChangeType THROW -> surface_write (nit 2)
            raise SurfaceWriteError(
                f"could not ChangeType the return surface {return_surface} to a "
                f"coordinate break ({exc!r}); refusing rather than authoring on an "
                "un-retyped surface",
                field="changetype", intended="CoordinateBreak", actual=None,
                surface=return_surface,
            ) from exc
        return_row = lde.GetSurfaceAt(return_surface)
        if not _cb.is_coordinate_break(return_row):
            raise SurfaceWriteError(
                f"return surface {return_surface} is not a coordinate break after "
                "ChangeType — the retype silently no-opped; refusing rather than "
                "wiring pickups to a non-CB surface",
                field="surface_type", intended="CoordinateBreak", actual=None,
                surface=return_surface,
            )
        changed_type = True
        # LOW: the ChangeType COMMITTED (the editor now holds a retyped CB row). Record
        # it so a later mid-authoring throw discloses this committed sub-step honestly.
        committed.append(f"changed_type(surface={return_surface})")

    # Author the pickup (-1) on each of Par1-Par5 (decenter + tilt), referencing the
    # entry surface NUMBER. The pickup tracks the entry live (P3 / Q4). Each authored
    # pickup is recorded the instant it lands (LOW: a throw on the Nth pickup discloses
    # the prior N-1 as committed).
    pickups = []
    for param in _cb._DOUBLE_PARAMS:
        _author_pickup(system, return_row, param, entry_surface)
        committed.append(f"pickup({param})")

    # Set the return Order = 1 - entry_order DIRECTLY (a literal Integer write,
    # read-back-proven via write_cb_cell — NOT a pickup; §4 nit 3).
    _cb.write_cb_cell(system, return_row, "order", return_order)
    committed.append(f"order={return_order}")

    # Pickup-is-live proof (§4 nit 4): each return CELL value reads back == -entry CELL
    # value (the CELL pickup value, NOT the global frame — do not conflate with the Q4
    # gap caveat). Read the entry cell, the return cell, assert return == -entry, and
    # assert the return solve Type is SurfacePickup.
    for param in _cb._DOUBLE_PARAMS:
        entry_val = _cb.read_cb_cell(system, entry_row, param)
        return_cell = _cb._cb_cell(system, return_row, param)
        # The solve is a SurfacePickup (proves it is a live tracking solve, not a
        # hand-written value).
        solve_name = _pickup_solve_type_name(return_cell)
        if "SurfacePickup" not in solve_name:
            raise SurfaceWriteError(
                f"return CB {param!r} (surface {return_surface}) is not a "
                f"SurfacePickup solve after authoring (reads {solve_name!r}); the "
                "pickup did not take — refusing rather than shipping a stale return CB",
                field="cb_pickup", intended="SurfacePickup", actual=solve_name,
                surface=return_surface,
            )
        return_val = _cb.read_cb_cell(system, return_row, param)
        if not _cb._readback_ok(-entry_val, return_val):
            raise SurfaceWriteError(
                f"return CB {param!r} (surface {return_surface}) reads {return_val!r} "
                f"but the entry value is {entry_val!r} (expected the negated "
                f"{-entry_val!r}); the scale -1 pickup did not track — refusing rather "
                "than shipping a wrong inverse",
                field="cb_pickup", intended=-entry_val, actual=return_val,
                surface=return_surface,
            )
        pickups.append({
            "param": param, "from_surface": entry_surface, "scale": -1.0,
            "entry_value": _cb_safe(entry_val), "return_value": _cb_safe(return_val),
        })

    return {
        "ok": True,
        "entry_surface": entry_surface,
        "return_surface": return_surface,
        "order_entry": entry_order,
        "order_return": return_order,
        "pickups": pickups,
        "changed_type": changed_type,
        # §4: NEVER claim on-axis restoration — the clean inverse is exact only for
        # CO-LOCATED CBs (gap=0). A real propagation gap leaves a correct physical
        # off-axis residual (Q4); the tool authors the inverse TRANSFORM, the user owns
        # whether a gap sits between.
        "restores_on_axis_only_if_colocated": True,
        # The by-number pickup desync hazard (P5 / the insert-renumbering gotcha).
        "warning": (
            f"the return CB's Par1-Par5 pickups reference the entry surface by NUMBER "
            f"({entry_surface}); a later upstream InsertNewSurfaceAt renumbers indices "
            "and SILENTLY desyncs the pickup target — re-author the return CB after any "
            "upstream insert/remove. The inverse restores the frame to the input axis "
            "ONLY for co-located (gap=0) CBs; a propagation gap leaves a correct "
            "off-axis residual (this tool authors the transform, not an on-axis "
            "guarantee)."
        ),
        # index_shift disclosure (the normalize_stop / apply_lens_spec precedent). This
        # tool does NOT insert a surface (it retypes an existing return_surface in
        # place), so no index shifted — disclosed as the no-shift convention so a caller
        # holding indices knows nothing moved.
        "index_shift": {"from": return_surface, "delta": 0},
    }


def _author_pickup(system, return_row, param, entry_surface):
    """Author a SurfacePickup (scale -1, .Surface=entry, .Column=ParN) on ``param``.

    Composes the live CB-cell + the SurfacePickup solve enum. THROW-guarded -> a
    structured ``SurfaceWriteError`` (the read-back-proof of the pickup happens in the
    caller's nit-4 loop). The pickup-data object surface varies live (some builds wrap
    it in ``solve._S_SurfacePickup``); both shapes are handled (the probe idiom).
    """
    pickup_member = _cb._surface_pickup_solve_enum(system)
    col_member = _resolve_par_column(system, param)
    cell = _cb._cb_cell(system, return_row, param)
    try:
        solve = cell.CreateSolveType(pickup_member)
        sp = getattr(solve, "_S_SurfacePickup", solve)
        sp.Surface = entry_surface
        sp.ScaleFactor = -1.0
        sp.Column = col_member
        cell.SetSolveData(solve)
    except Exception as exc:  # noqa: BLE001 — a pickup-author THROW -> surface_write
        raise SurfaceWriteError(
            f"could not author the scale -1 SurfacePickup on return CB {param!r} "
            f"(from surface {entry_surface}) ({exc!r}); refusing rather than shipping "
            "an unverified return CB",
            field="cb_pickup", intended="SurfacePickup", actual=None, surface=None,
        ) from exc


def _resolve_par_column(system, param):
    """Resolve the ``ParN`` SurfaceColumn member for ``param`` (the pickup .Column)."""
    from ..enums import _resolve_enum
    col_name = _cb._PARAM_TO_COL[param]
    return _resolve_enum(_cb._surface_column_enum(system), col_name)


def _pickup_solve_type_name(cell):
    """Read ``cell.GetSolveData().Type`` as a string (the pickup read-back proof)."""
    try:
        return str(cell.GetSolveData().Type)
    except Exception as exc:  # noqa: BLE001 — a solve read THROW -> surface_write
        raise SurfaceWriteError(
            f"could not read back the solve type of a return CB cell ({exc!r}); the "
            "pickup is unverifiable — refusing rather than guessing it took",
            field="cb_pickup", intended="SurfacePickup", actual=None, surface=None,
        ) from exc


# =========================================================================== #
# ToolSpec registration.
# =========================================================================== #
ADD_COORDINATE_BREAK_SPEC = ToolSpec(
    name="add_coordinate_break",
    handler=add_coordinate_break,
    required_params=("surface",),
    param_types={
        "surface": "number",
        "decenter_x": "number",
        "decenter_y": "number",
        "tilt_x": "number",
        "tilt_y": "number",
        "tilt_z": "number",
        "order": "number",
    },
    description=(
        "Make a surface a coordinate break: tilt/decenter the local frame "
        "(decenter_x/y, tilt_x/y/z in degrees, order 0=decenter-then-tilt / "
        "1=tilt-then-decenter). Writes the six tilt/decenter/order cells and PROVES the "
        "change took via the post-CB global frame geometry, not just the cell "
        "read-back. Gotcha: the typed tilt/decenter property setter is a SILENT no-op — "
        "this tool writes the editor cells and verifies the global frame, never that "
        "property. See add_return_cb, set_cb_variable, describe_surfaces."
    ),
)

SET_CB_VARIABLE_SPEC = ToolSpec(
    name="set_cb_variable",
    handler=set_cb_variable,
    required_params=("surface", "param"),
    param_types={"surface": "number", "param": "string"},
    description=(
        "Make a coordinate-break tilt/decenter cell an optimizer Variable "
        "(param one of decenter_x/decenter_y/tilt_x/tilt_y/tilt_z), proved by the "
        "optimizer's own DOF count incrementing. Gotcha: REFUSES the Order parameter "
        "and any Integer cell — the engine silently accepts a variable on the discrete "
        "0/1 Order flag and counts it as a real DOF (nonsensical), and the read-back is "
        "not a safety net. See add_coordinate_break, set_variable."
    ),
)

ADD_RETURN_CB_SPEC = ToolSpec(
    name="add_return_cb",
    handler=add_return_cb,
    required_params=("entry_surface", "return_surface"),
    param_types={"entry_surface": "number", "return_surface": "number"},
    description=(
        "Author a return coordinate break that undoes an entry CB: a scale -1 "
        "SurfacePickup on each decenter/tilt cell (tracks the entry live) plus the "
        "return Order flipped to 1-entry_order. Retypes return_surface to a CB if "
        "needed (disclosed). Gotcha: the inverse restores the input axis ONLY for "
        "co-located (gap=0) CBs — a propagation gap leaves a correct off-axis residual; "
        "and an upstream insert_surface renumbers and desyncs the by-number pickup "
        "(re-author after any upstream insert). See add_coordinate_break."
    ),
)

TOOL_SPECS = (ADD_COORDINATE_BREAK_SPEC, SET_CB_VARIABLE_SPEC, ADD_RETURN_CB_SPEC)
