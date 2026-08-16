"""tools/lens_surface.py — LDE surface read / structural / mutator tools.

Six dispatchable tools over the Lens Data Editor (LDE), all probe-grounded:

- ``read_surface``     — straight read of one surface's fields.
- ``surface_count``    — ``lde.NumberOfSurfaces``.
- ``set_surface``      — write geometry fields (radius/thickness/conic/
  semi_diameter/comment) with read-back-as-proof; **material is REFUSED**
  (written only via ``substitute_glass``).
- ``insert_surface``   — ``InsertNewSurfaceAt`` with a HARD client-side bounds
  precondition (out-of-range insert crashes the engine).
- ``remove_surface``   — ``RemoveSurfaceAt`` with bounds + a stop guard (no force
  flag).
- ``set_stop_surface`` — set ``IsStop`` with read-back that catches the silent
  image-stop no-op.

Stale-proxy discipline: every mutator re-fetches the row by index
immediately before AND after a structural op — an ``ILDERow`` proxy is never held
across an insert/remove/reload.

Live ZOS-API integration: exercised by the live test; unit-tested here against
a FakeLDE/FakeRow double (no backend).
"""
import math

from .._io import safe_float
from ..errors import SolveDrivenError, SurfaceWriteError, ToolParamError
from ..server import ToolSpec
from . import _asphere_cells as _asph
from . import _lens_common as _c
from . import _measurement_common as _mc
from . import _optimize_common as _oc
from . import _solve_cells as _sc
from . import _solve_refs as _refs

# Geometry fields set_surface accepts and the typed property each writes to.
_GEOMETRY_FIELDS = ("radius", "thickness", "conic", "semi_diameter")
# Any of these keys present in set_surface params means the caller is trying to
# write a material/glass through the wrong path — refuse.
_MATERIAL_KEYS = ("material", "glass")

# Map a writable field name -> the ILDERow property name.
_FIELD_TO_PROP = {
    "radius": "Radius",
    "thickness": "Thickness",
    "conic": "Conic",
    "semi_diameter": "SemiDiameter",
    "comment": "Comment",
}


def _safe_surface_type_name(row):
    """``str(row.Type)`` guarded -> the bare member name, or ``None`` on a read throw (S1 GAP-2).

    The positive surface-type read field. A clean read yields the bare member name
    (``"Standard"`` / ``"EvenAspheric"`` / …); a Type read THROW (a wedged/degraded row)
    yields ``None`` so the additive ``type`` field NEVER crashes the base read.
    """
    try:
        return str(row.Type)
    except Exception:  # noqa: BLE001 — a degraded Type read -> None (additive field, never crash)
        return None


def _read_surface_dict(system, lde, surface, n):
    """Build the read_surface result dict from a freshly re-fetched row.

    ``radius``/``thickness``/``semi_diameter`` go through ``safe_float`` (planar
    surfaces read back as ``inf``). OBJECT/IMAGE flags derive from the
    index (object = 0, image = N-1).
    """
    row = lde.GetSurfaceAt(surface)
    out = {
        "surface": surface,
        "radius": safe_float(row.Radius),
        "thickness": safe_float(row.Thickness),
        # conic goes through ``safe_float`` like radius/thickness
        # — a coordinate-break surface's Conic reads back as ``inf`` (probe P1 col 9),
        # and a bare ``float(row.Conic)`` would carry a raw float inf into the LensSpec
        # read that the ``apply`` path's ``_strict_float`` then rejected. ``safe_float``
        # emits the ``"inf"`` string sentinel (the same self-consistent shape
        # radius/thickness already use), which ``LensSpec.read`` + ``from_dict`` route
        # through ``_coerce_inf`` (the L26/L30 sweep: all four numerics handled alike).
        "conic": safe_float(row.Conic),
        "semi_diameter": safe_float(row.SemiDiameter),
        "material": str(row.Material),
        "comment": str(row.Comment),
        "is_stop": bool(row.IsStop),
        "is_object": surface == 0,
        "is_image": surface == n - 1,
        # S1 GAP-2: a POSITIVE surface-type read field (``str(row.Type)`` guarded). Lets a
        # reset be VERIFIED as genuinely Standard (the curved-stop fixed-point) —
        # absent-asphere alone does NOT prove Standard (a degraded asphere read ALSO drops
        # the coefficients). Values: Standard / EvenAspheric / OddAsphere / ExtendedAsphere
        # / ExtendedOddAsphere / ... / None (a degraded Type read). ADDITIVE ONLY: NOT fed
        # into SurfaceSpec / LensSpec.read (the apply round-trip stays byte-identical) and
        # the L132-133 asphere_surface_type carve-out below is untouched.
        "type": _safe_surface_type_name(row),
    }
    # Asphere read path (THROW-GUARDED, graceful-degrade):
    # an EvenAspheric surface adds an additive (read-only) ``aspheric_coefficients``
    # block — the 8 even-asphere Par-cell coefficients (α2..α16) via the SAME substrate
    # reader the write path uses. A non-asphere surface OMITS the field (an existing
    # non-asphere caller is unaffected). The asphere detection AND the coefficient reads
    # are wrapped so this additive feature NEVER crashes the base read of radius/conic/
    # thickness/semi_diameter:
    #   - a row whose ``.Type`` read is ABSENT/THROWS (a non-asphere fake row, a wedged
    #     proxy) degrades gracefully — treated as non-asphere, the block is simply OMITTED
    #     (``is_even_asphere`` is throw-guarded and raises ``AsphereWriteError`` on a Type
    #     throw, which we catch here so the base read survives);
    #   - a DRIFTED / throwing coefficient cell on a genuine asphere returns the Standard
    #     fields + ``aspheric_coefficients: null`` + ``coefficients_unreadable: true`` —
    #     NEVER a whole-surface error that would blind the agent to radius/conic (b.3).
    # NOTE: this read does NOT feed ``LensSpec.read`` in S1 (the carry is S2); it is the
    # agent-facing read-back only.
    try:
        type_key = _asph.asphere_type_of(row)
    except Exception:  # noqa: BLE001 — a Type read absent/throw -> treat as non-asphere
        type_key = None
    if type_key is not None:
        info = _asph.ASPHERE_TYPE_INFO[type_key]
        try:
            if info.gated:
                max_terms = _asph.read_gate_cell(system, row, info)
                norm_radius = _asph.read_computed_double_cell(
                    system, row, info.norm_par, _asph._NORM_HEADER
                )
                out["aspheric_coefficients"] = [
                    _asph.read_computed_double_cell(
                        system, row, info.coeff_par(i), info.header(i)
                    )
                    for i in range(max_terms)
                ]
                # (S7 #13) Parallel ADDITIVE per-coefficient order/term labels.
                out["coefficient_orders"] = _asph.coefficient_order_table(
                    info, out["aspheric_coefficients"]
                )
                out["asphere_surface_type"] = type_key
                out["asphere_norm_radius"] = safe_float(norm_radius)
                out["asphere_max_term"] = max_terms
            else:
                out["aspheric_coefficients"] = [
                    _asph.read_computed_double_cell(
                        system, row, info.coeff_par(i), info.header(i)
                    )
                    for i in range(info.max_terms)
                ]
                # (S7 #13) Parallel ADDITIVE per-coefficient order/term labels.
                out["coefficient_orders"] = _asph.coefficient_order_table(
                    info, out["aspheric_coefficients"]
                )
                # Carry the type (EvenAspheric default omits norm/max_term — Odd/Even are
                # non-gated, absolute-r, fixed-8). EvenAspheric stays back-compat: the
                # carry treats a None asphere_surface_type as EvenAspheric, so we ONLY
                # stamp the type for a NON-EvenAspheric to keep the S2a key set byte-
                # identical for the EvenAspheric round-trip.
                if type_key != "EvenAspheric":
                    out["asphere_surface_type"] = type_key
        except Exception:  # noqa: BLE001 — a drifted/throwing coeff cell -> graceful (b.3)
            out["aspheric_coefficients"] = None
            out["coefficients_unreadable"] = True

    # GRIN (§6.1) additive read block — keyed on the AUTHORABLE resolver
    # (``grin_type_of_name`` over ``GRIN_TYPE_INFO``): a GRIN surface reports its base
    # index ``n0``, its radial coefficient MAP (Nr2..Nr12), the internal Delta-T trace step,
    # and the wavelength-blind caveat. A non-GRIN surface gets NONE of these keys
    # (byte-unchanged control). Map-driven Par1..Par8 ONLY (Par9+ never fetched); a real 0.0
    # coefficient stays visible (zero != unused). A drifted/wedged coefficient cell AFTER
    # positive type ID -> ``grin_coefficients:null`` + ``grin_coefficients_unreadable:true``
    # (never a fabricated 0.0, never a whole-surface error, never silently non-GRIN). Wrapped
    # so this additive read NEVER crashes the base surface read. (``out["type"]`` is the
    # already-read guarded ``str(row.Type)``; a degraded Type read -> None -> non-GRIN.)
    try:
        from . import _grin_cells as _grin
        grin_key = _grin.grin_type_of_name(out.get("type") or "")
    except Exception:  # noqa: BLE001 — a GRIN resolver hiccup -> treat as non-GRIN (omit block)
        grin_key = None
    if grin_key is not None:
        out["grin_surface_type"] = grin_key
        out["grin_wavelength_blind"] = True
        try:
            info = _grin.GRIN_TYPE_INFO[grin_key]
            grin_coeffs = {}
            grin_n0 = None
            grin_step = None
            # Iterate the RESOLVED type's params (was the module-global Gradient2 table — the
            # false ``grin_coefficients_unreadable`` on a good Gradient3); n0 keyed on
            # the TOKEN, never ``power == 0``.
            for tok, _p, _h, _k, role, _pw in info.params:
                val = _grin.read_grin_cell(system, row, tok, info)
                if role == "step":
                    grin_step = val
                elif tok == "n0":
                    grin_n0 = val
                else:
                    grin_coeffs[tok] = val
            out["grin_n0"] = grin_n0
            out["grin_coefficients"] = grin_coeffs
            out["grin_step_size"] = grin_step
        except Exception:  # noqa: BLE001 — a drifted/wedged coeff cell -> graceful degrade
            out["grin_coefficients"] = None
            out["grin_coefficients_unreadable"] = True

    # INSERTION POINT A (frozen): the surface-SOLVE disclosure
    # patch, additive, 0..4 top-level keys, appended LAST so the PRIOR key ORDER of every
    # base/asphere/GRIN field is byte-unchanged. On a freshly-built untouched system the
    # emit returns ``{}`` BY CONSTRUCTION (defaults suppressed, Standard row, one config),
    # so ``out.update({})`` is the identity and that no-op holds STRUCTURALLY, not by measurement.
    #
    # CONTAINMENT: an emit fault DISCLOSES, it never sinks the read. ``Exception``, NOT
    # ``BaseException`` — a KeyboardInterrupt travelling this path MUST propagate,
    # and the read_surface consumer has no per-row guard to absorb it, so the KI row is
    # asserted PER CONSUMER (it propagates here; the describe row-guard absorbs it there).
    #
    # THE UNREADABLE BLOCK HAS TWO PROVENANCES and they are indistinguishable BY DESIGN:
    # a per-cell fetch fault (emitted from INSIDE ``emit_solves_block``) and a
    # whole-block assembly fault (partition/wire-safety, raised AFTER the cells were read
    # and landing here). It freezes 0..4 top-level keys; a fifth key to tell them apart is
    # refused on the record. No consumer may infer a per-cell read failure from it.
    try:
        out.update(_sc.emit_solves_block(system, lde, surface))
    except Exception:  # noqa: BLE001 — an emit fault DISCLOSES; it never sinks the read
        out.update(_sc._unreadable_block())
    return out


def read_surface(session, params):
    """Read one surface's fields. Out-of-range -> ``ToolParamError`` (no call)."""
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _c._require_int_index(params, "surface")
    _c._require_read_index(surface, n)
    return _read_surface_dict(system, lde, surface, n)


def surface_count(session, params):
    """Return ``{count}`` = ``lde.NumberOfSurfaces``."""
    system = session.system
    return {"count": int(system.LDE.NumberOfSurfaces)}


def _set_object_surface(session, params, n):
    """Write the OBJECT (surface 0) thickness — the object distance — only (GAP-1).

    Surface 0 is the OBJECT surface; the engine would SILENTLY ACCEPT a radius /
    conic / semi_diameter / material / comment on it (P4), corrupting the object
    surface, so this CLIENT firewall refuses every field BUT thickness BEFORE any
    write. thickness handling: missing -> refuse; bool/non-number -> refuse; nan ->
    refuse LOUD (a nan object distance is never real intent); ``inf`` (either sign)
    -> ACCEPT (the canonical infinite-conjugate / afocal object); a finite negative
    -> ACCEPT as-is (read-back-arbitrated; the engine enforces any physics). The
    write + read-back DELEGATES to the proven ``LensSpec._apply_object_fields``
    (a function-local import avoids the lens_spec<->lens_surface import cycle).
    """
    # Firewall: refuse every non-thickness field BEFORE any write (the LOCKED
    # named-field messages). Geometry fields first (radius/conic/semi_diameter).
    for field in ("radius", "conic", "semi_diameter"):
        if field in params:
            raise ToolParamError(
                f"surface 0 (OBJECT) accepts thickness only (the object distance); "
                f"'{field}' is not a meaningful object-surface field — the engine "
                "would silently accept it and corrupt the object surface, so it is "
                "refused."
            )
    for key in _MATERIAL_KEYS:
        if key in params:
            raise ToolParamError(
                "surface 0 (OBJECT): material/glass on the object surface is "
                "meaningless and is refused (the engine stores it but it is a "
                "footgun); only thickness (the object distance) is writable on "
                "surface 0."
            )
    if "comment" in params:
        raise ToolParamError(
            "surface 0 (OBJECT) accepts thickness only; comment on the object "
            "surface is refused (use a comment on a real lens surface)."
        )

    # Require thickness (never a silent no-op).
    if "thickness" not in params:
        raise ToolParamError(
            "surface 0 (OBJECT) requires a thickness (the object distance); no "
            "other field is writable on the object surface."
        )
    value = params["thickness"]
    # The SAME numeric screen set_surface uses: reject bool, reject non-number.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolParamError(
            f"thickness must be a number, got {type(value).__name__} {value!r}"
        )
    # nan is never real object-distance intent (and the read-back oracle treats
    # nan as a permanent mismatch) -> refuse up front. inf (either sign) is the
    # canonical infinite-conjugate object and is accepted; finite negative too.
    if math.isnan(value):
        raise ToolParamError(
            "surface 0 (OBJECT) thickness must be finite or inf; got nan."
        )

    # Write + read-back via the proven OBJECT writer (function-local import to
    # avoid the lens_spec <-> lens_surface import cycle).
    from . import lens_spec
    lde = session.system.LDE
    lens_spec.LensSpec._apply_object_fields(
        session.system, lde, {"thickness": float(value)})
    return _read_surface_dict(session.system, lde, 0, n)


def set_surface(session, params):
    """Write geometry/comment fields with read-back proof; material refused.

    Material is REFUSED: any ``material``/``glass`` key present ->
    ``ToolParamError`` pointing to ``substitute_glass`` (the single validated
    write path). At least one writable field must be supplied. Each write is
    followed by an immediate re-read + ``_verify_or_raise``.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _c._require_int_index(params, "surface")
    # GAP-1 (finite-conjugate build robustness): surface 0 (OBJECT) accepts a
    # thickness-only write (the object distance) — the finite-conjugate / afocal
    # build path. Branch BEFORE the shared geometry gate (which refuses 0 for
    # substitute_glass/asphere/every other caller — left byte-identical).
    if surface == 0:
        return _set_object_surface(session, params, n)
    _c._require_geometry_index(surface, n)

    # Refuse material BEFORE any write (single validated choke point).
    for key in _MATERIAL_KEYS:
        if key in params:
            raise ToolParamError(
                "material is written only via substitute_glass "
                "(it validates the glass against the catalog); set_surface "
                "refuses a material/glass key"
            )

    # Collect the writable fields the caller actually provided.
    writes = {}
    for field in _GEOMETRY_FIELDS:
        if field in params:
            value = params[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ToolParamError(
                    f"{field} must be a number, got {type(value).__name__} {value!r}"
                )
            writes[field] = float(value)
    if "comment" in params:
        comment = params["comment"]
        if not isinstance(comment, str):
            raise ToolParamError(
                f"comment must be a string, got {type(comment).__name__}"
            )
        writes["comment"] = comment

    if not writes:
        raise ToolParamError(
            "set_surface requires at least one writable field "
            f"(any of {list(_FIELD_TO_PROP.keys())})"
        )

    # REFUSAL SITE 1: the driven-cell PROBE PRE-PASS.
    #
    # AFTER collection, BEFORE the FIRST setattr. A multi-field call carrying ONE driven
    # cell must mutate NOTHING — a guard inside the write loop would leave the earlier
    # fields written and the design half-mutated by a call that reports failure.
    #
    # SCOPE: ``_GEOMETRY_FIELDS`` — the module's own tuple, which IS the
    # ``CELL_TOKENS`` n writable intersection. ``comment`` is excluded by construction (it
    # is not a solve-bearing cell) and ``material`` never reaches here (refused above,
    # routed to substitute_glass).
    #
    # THE TRI-STATE IS TESTED BY IDENTITY, NEVER TRUTHINESS. ``if probe["driven"]:`` reads
    # UNKNOWN (``None``) as PERMISSION — a two-character fail-open on a WRITE predicate.
    # UNKNOWN fails CLOSED here, the OPPOSITE polarity to the emit's fail-OPEN
    # ``_par_cell_row``; the two are deliberately different and a tidy-up sweep must not
    # "unify" them. A read that cannot see the solve is exactly the case where writing
    # through it is unsafe.
    for field in _GEOMETRY_FIELDS:
        if field not in writes:
            continue
        probe = _sc.refuse_if_driven(system, lde, surface, field)
        if probe.get("driven") is not False:
            raise SolveDrivenError.from_probe(
                probe, cell_token=field, surface=surface, tool="set_surface",
                intended=writes[field])

    # Write each field, then immediately re-fetch the row and read it back.
    warnings = []
    for field, intended in writes.items():
        prop = _FIELD_TO_PROP[field]
        row = lde.GetSurfaceAt(surface)
        # GAP-2: write the FULL comment (no client truncation); the engine
        # truncates to its 32-char clean prefix (P1).
        setattr(row, prop, intended)
        # Re-fetch (never hold the proxy across the read-back boundary).
        row = lde.GetSurfaceAt(surface)
        actual = getattr(row, prop)
        if field in _GEOMETRY_FIELDS:
            # Verify against the RAW value (not the JSON ``safe_float`` sentinel): a
            # planar surface reads back as the float ``inf``, and ``_readback_ok``
            # compares ``inf == inf`` directly (a stringified sentinel would
            # spuriously mismatch the float intended value).
            actual = float(actual)
            # GAP-6: a finite thickness written to the IMAGE (last) surface reads
            # back +inf (the engine's image-plane pin, P3) — NOT a failure. The
            # image-pin oracle accepts it; every other case (interior, -inf,
            # non-image) stays the strict _readback_ok via _verify_or_raise.
            if field == "thickness" and surface == n - 1:
                if not _c._image_thickness_ok(surface, n, intended, actual):
                    raise SurfaceWriteError(
                        f"{field} write rejected by engine: intended={intended!r} "
                        f"actual={actual!r} surface={surface}",
                        field=field, intended=intended, actual=actual,
                        surface=surface,
                    )
            else:
                _c._verify_or_raise(field, intended, actual, surface=surface)
        else:
            # GAP-2: the comment read-back goes through the prefix-against-intended
            # oracle (the engine truncates to a clean 32-char prefix). A WRONG or
            # DROPPED comment still mismatches -> raises (fail-closed).
            actual = str(actual)
            if not _c._comment_readback_ok(intended, actual):
                raise SurfaceWriteError(
                    f"{field} write rejected by engine: intended={intended!r} "
                    f"actual={actual!r} surface={surface}",
                    field=field,
                    intended=intended,
                    actual=actual,
                    surface=surface,
                )
            # WARN (additive, never a rollback) when the engine truncated OUR
            # intended comment past the 32-char cap.
            if len(intended) > _c.COMMENT_MAX_CHARS:
                warnings.append(
                    f"surface {surface}: comment truncated by the engine to "
                    f"{_c.COMMENT_MAX_CHARS} chars; stored={actual!r} "
                    f"(intended {len(intended)} chars)."
                )

    out = _read_surface_dict(system, lde, surface, n)
    if warnings:
        out["warnings"] = warnings
    return out


def insert_surface(session, params):
    """Insert a surface before ``at``. HARD bounds precondition.

    The bounds check raises BEFORE ``InsertNewSurfaceAt`` is ever called — an
    out-of-range insert HARD-CRASHES the engine (IPC pipe death), so it can never
    be an exception handler. After the insert, read-back-verify the count grew by
    one (else ``SurfaceWriteError``).
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    at = _c._require_int_index(params, "at")
    _c._require_insert_at(at, n)  # raises BEFORE any engine call

    lde.InsertNewSurfaceAt(at)
    # Do not hold the returned proxy; re-read the count straight.
    new_count = int(lde.NumberOfSurfaces)
    if new_count != n + 1:
        raise SurfaceWriteError(
            f"insert at {at} did not change surface count: intended={n + 1} "
            f"actual={new_count}",
            field="count",
            intended=n + 1,
            actual=new_count,
            surface=at,
        )
    return {"at": at, "count": new_count}


def remove_surface(session, params):
    """Remove surface ``at``. Bounds + stop guard (no force flag).

    Refuses removing OBJECT 0 / IMAGE N-1 (bounds) and refuses removing the stop
    surface (``ToolParamError`` pointing to ``set_stop_surface`` — reassign the
    stop first). ``RemoveSurfaceAt`` returns ``bool``; ``False`` -> read-back
    count check -> ``SurfaceWriteError``.

    THE SOLVE-REFERENCE DISCLOSURE. Removing a row that a live
    ``SurfacePickup`` names as its SOURCE silently DELETES that solve (measured: a
    ``thickness`` pickup; a coordinate-break ``Par1``/``Par3`` pickup — each measured
    reverting to ``Fixed``, frozen at its last driven value). The
    shipped tool returned ``{"at": 2, "count": 7}`` and disclosed none of it. Every
    success return now carries an additive ``solve_refs`` block (``_solve_refs.SCOPE``
    states what it does and does not audit), and ``refuse_on_solve_refs=true`` turns the
    disclosure into a ZERO-MUTATION refusal.

    DISCLOSE BY DEFAULT, and this is the WEAKER of the two safety postures — recorded
    rather than glossed. An agent that does not read the key still destroys the
    relationship under ``ok: true``. The default is nonetheless disclose, for two
    reasons: ``lens_spec._reconcile_count``'s shrink loop calls this as a BARE STATEMENT
    with the returned dict discarded and no channel to pass an override down, so
    refuse-by-default would break a shipped MCP door with no escape hatch; and under
    refuse-by-default a ``could_not_scan`` must resolve either to a refusal (a
    denial-of-service on a structural tool via any one wedged cell anywhere) or to a
    proceed (a FORBIDDEN ABSENT-vs-UNREADABLE collapse). Under disclose-by-default
    only the OPT-IN strict mode has to decide it — and there refusing is correct,
    because a caller who passed the flag asked for UNKNOWN to be treated as alarm.

    THE ORDER OF THE FIRST FIVE STEPS IS LOAD-BEARING:

    * the flag read is the FIRST executable statement, so a malformed flag costs ZERO
      engine reads — the standard ``_require_remove_at``'s own docstring sets ("raises
      BEFORE any engine call"). It could not be met further down, because the shipped
      body reads ``lde.NumberOfSurfaces`` before validating anything;
    * the STOP GUARD runs BEFORE the scan. Cheap refusals first: a row that is both the
      stop AND a pickup source costs the agent one recoverable extra round-trip (move
      the stop, re-call), whereas scanning first would make EVERY stop-row removal pay
      an O(N) scan. The stop+source engine behaviour is therefore never reached and
      stays unmeasured; this design does not depend on the answer;
    * the strict gate is strictly BEFORE ``RemoveSurfaceAt``, so a refusal is
      zero-mutation, and it reads the PRE-MUTATION scan VERDICT — never the wire
      ``state``. That is the one normative strict rule: a disclosure failure discovered
      AFTER the removal cannot un-remove the row (there is no inverse), so the gate
      reads the verdict, the wire reports the outcome, and a post-mutation degradation
      is REPORTED, never refused;
    * the confirm runs AFTER the count read-back and is skipped entirely (zero engine
      reads) when nothing was at risk.

    THE FAILURE PATH DISCARDS THE SCAN: a removal that mutates and then fails its count
    read-back raises ``SurfaceWriteError`` and the scan result is lost. A known gap.
    """
    refuse = _c._require_bool_param(params, "refuse_on_solve_refs")  # zero engine reads
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    at = _c._require_int_index(params, "at")
    _c._require_remove_at(at, n)  # raises BEFORE any engine call

    # Stop guard: never remove the stop (no force flag).
    row = lde.GetSurfaceAt(at)
    if bool(row.IsStop):
        raise ToolParamError(
            f"cannot remove the stop surface ({at}); set a new stop via "
            "set_stop_surface first"
        )

    scan = _refs.scan_removal(system, lde, at)
    # ``_refs.VERDICTS[0]``, never the literal ``"none_affected"`` (a review finding).
    # The vocabulary is FROZEN and ORDERED in ONE place precisely so a consumer cannot
    # acquire a private copy -- the single-locus rule forbids one module over, and
    # the strict gate is the highest-consequence consumer of it there is.
    if refuse and scan["verdict"] != _refs.VERDICTS[0]:
        raise ToolParamError(_refs.refusal_message(at, scan))

    removed = lde.RemoveSurfaceAt(at)
    new_count = int(lde.NumberOfSurfaces)
    if not bool(removed) or new_count != n - 1:
        raise SurfaceWriteError(
            f"remove at {at} did not take effect: returned={removed!r} "
            f"intended_count={n - 1} actual_count={new_count}",
            field="count",
            intended=n - 1,
            actual=new_count,
            surface=at,
        )
    confirmed = _refs.confirm_after(system, lde, at, scan)
    return {"at": at, "count": new_count, **_refs.disclosure(scan, confirmed)}


def set_stop_surface(session, params):
    """Set the stop to an interior surface with a no-op-catching read-back.

    Bounds ``1 <= surface <= N-2`` (image-stop silently no-ops, object-
    stop is invalid).

    Real-engine stop semantics (probe ``scripts/probe_stop_mechanism.py``):
      * Setting ``IsStop = True`` on a NON-stop surface MOVES the stop to it and
        auto-clears the previous stop — the engine enforces exactly one stop.
      * Setting ``IsStop = False`` on the SOLE stop is a SILENT NO-OP (the engine
        refuses to leave the system stop-less, so the flag stays ``True``).

    Because of the second rule, an ``IsStop=False`` toggle can NOT be used to
    prove liveness (the real engine refuses to clear the sole stop). The
    verification therefore splits on whether the target is already the stop:

    - Target ALREADY the stop: a satisfied no-op. Verify it IS the stop AND the
      ONLY stop (full scan) and return ``ok`` — do NOT try to clear-and-reassert
      (that toggle is a real-engine no-op and would spuriously fail).
    - Target NOT the stop: set ``IsStop = True``, then scan and verify the stop
      MOVED to the target AND no other surface still reports a stop (the engine
      auto-clears the old). If the target does not read back as the stop, that is
      the real silent-no-op case (e.g. an image surface) -> ``SurfaceWriteError``.
    """
    system = session.system
    lde = system.LDE
    n = int(lde.NumberOfSurfaces)
    surface = _c._require_int_index(params, "surface")
    _c._require_stop_index(surface, n)

    # Capture the previously-reported stop surface(s) before the write.
    prev_stops = [
        i for i in range(n) if bool(lde.GetSurfaceAt(i).IsStop)
    ]
    target_was_stop = surface in prev_stops

    if target_was_stop:
        # Satisfied no-op: the target is ALREADY the stop. The real engine refuses
        # to clear the sole stop (an IsStop=False toggle is a no-op), so there is
        # nothing to (re)write — just verify the invariant the engine maintains:
        # the target IS the stop and it is the ONLY stop. A stale stop on another
        # surface would mean the system has more than one stop (engine corruption).
        other_stops = [i for i in prev_stops if i != surface]
        if other_stops:
            raise SurfaceWriteError(
                f"set_stop_surface({surface}): target is the stop but surface(s) "
                f"{other_stops} ALSO report IsStop (the engine should keep exactly "
                "one stop)",
                field="is_stop",
                intended=True,
                actual=False,
                surface=surface,
            )
        return {"surface": surface, "is_stop": True}

    # Target is NOT currently the stop: set IsStop=True, which on the real engine
    # MOVES the stop to the target and auto-clears the old one.
    row = lde.GetSurfaceAt(surface)
    row.IsStop = True

    # Re-fetch the target row and verify the stop actually landed on it. If it did
    # not, this is the real silent-no-op case (e.g. an image surface) — the write
    # was ignored and the stop did not move.
    target = lde.GetSurfaceAt(surface)
    if not bool(target.IsStop):
        raise SurfaceWriteError(
            f"set_stop_surface({surface}) did not take effect: IsStop read back "
            "False (silent no-op — the engine ignored the stop write)",
            field="is_stop",
            intended=True,
            actual=False,
            surface=surface,
        )
    # Positive full-scan read-back: the target IS the stop AND it is the ONLY stop
    # (a stop left on any OTHER surface means the engine did NOT auto-clear the old
    # stop — the move silently did not take).
    other_stops = [
        i for i in range(n) if i != surface and bool(lde.GetSurfaceAt(i).IsStop)
    ]
    if other_stops:
        raise SurfaceWriteError(
            f"set_stop_surface({surface}) did not move the stop off "
            f"surface(s) {other_stops} (still report IsStop)",
            field="is_stop",
            intended=True,
            actual=False,
            surface=surface,
        )

    # --- A1: orphaned former-stop thickness-DOF freeze hygiene (best-effort) --- #
    # The move SUCCEEDED. The former stop K is now an ordinary surface, but its
    # thickness solve SURVIVES the move (probe A1): if K is a powerless air<->air
    # dummy in COLLIMATED space, that free thickness Variable is a zero-merit-
    # sensitivity ghost DOF a global search will wander (the silent-correctness
    # bug). Freeze it ONLY when all FOUR signals hold; DEGRADE to a WARN otherwise.
    # A hygiene fault NEVER fails the move (it already succeeded) -> additive keys.
    frozen = []
    frozen_values = {}
    warnings = []
    orphans = [s for s in prev_stops if s != surface]
    try:
        variable_member = _oc._solve_type_variable_enum(system)
        for K in orphans:
            cell = lde.GetSurfaceAt(K).ThicknessCell
            if not _oc._cell_is_variable(cell, variable_member):
                continue                                    # not a free thickness DOF
            if _oc._surface_is_inert(lde, K) is not True:   # None/False -> not powerless
                continue
            collimated = _mc._gap_is_collimated(system, K)
            if collimated is None:                          # DIV-1: fail-OPEN + WARN
                warnings.append(
                    f"orphaned_stop_dof_check_indeterminate: surface {K} "
                    "(could not verify collimated state); if it is an unbounded dummy "
                    "in collimated space, freeze it with clear_variable"
                )
                continue
            if collimated is not True:                      # DIV-2: a real spacer -> leave
                continue
            # All four hold -> freeze, read-back-proven.
            try:
                t = _mc._read_thickness(system, K)
                _oc._clear_solve_to_fixed_proven(
                    cell, variable_member, K, "thickness",
                    lde=lde, cell_attr="ThicknessCell")
                frozen.append(K)                            # recorded ONLY after the proof
                if t is not None:
                    frozen_values[str(K)] = float(t)
                if t is not None and math.isfinite(t) and abs(t) > 100.0:
                    warnings.append(
                        f"orphaned_stop_dof_frozen at surface {K} holds {t:g} mm "
                        "(pre-existing dead-space); reseat with set_surface(K, "
                        "thickness=<sane>) — freezing only stopped further drift"
                    )
            except SurfaceWriteError as exc:
                warnings.append(f"orphaned_stop_dof_freeze_failed: surface {K} ({exc})")
    except Exception as exc:  # noqa: BLE001 — hygiene NEVER fails the move
        warnings.append(f"orphan_scan_warning: {exc!r}")

    result = {"surface": surface, "is_stop": True,
              "orphaned_stop_dof_frozen": frozen}
    if frozen_values:
        result["orphaned_stop_dof_frozen_values"] = frozen_values
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result


READ_SURFACE_SPEC = ToolSpec(
    name="read_surface",
    handler=read_surface,
    required_params=("surface",),
    param_types={"surface": "number"},
    description=(
        "Read one surface's fields (radius, thickness, conic, semi-diameter, "
        "material, comment, stop/object/image flags). Planar -> inf."
    ),
)

SURFACE_COUNT_SPEC = ToolSpec(
    name="surface_count",
    handler=surface_count,
    required_params=(),
    description="Return the number of surfaces in the lens.",
)

SET_SURFACE_SPEC = ToolSpec(
    name="set_surface",
    handler=set_surface,
    required_params=("surface",),
    param_types={
        "surface": "number",
        "radius": "number",
        "thickness": "number",
        "conic": "number",
        "semi_diameter": "number",
        "comment": "string",
    },
    description=(
        "Write surface geometry (radius/thickness/conic/semi_diameter/comment) "
        "with read-back proof. Material is refused -> use substitute_glass. "
        "Surface 0 (OBJECT) accepts thickness only (the object distance); other "
        "fields on surface 0 are refused."
    ),
)

INSERT_SURFACE_SPEC = ToolSpec(
    name="insert_surface",
    handler=insert_surface,
    required_params=("at",),
    param_types={"at": "number"},
    description=(
        "Insert a new surface before position 'at' (1..N-1). Out-of-range is "
        "refused BEFORE the engine call (an out-of-range insert crashes the engine). "
        "A pickup solve's surface reference was measured to track an insert or remove "
        "of another surface, so no re-authoring is needed after one: measured for "
        "SurfacePickup on the thickness cell and on coordinate-break Par1/Par3, "
        "OpticStudio 2025 R1. Removing the row a pickup names as its SOURCE is the "
        "case that does destroy it -- see remove_surface."
    ),
)

REMOVE_SURFACE_SPEC = ToolSpec(
    name="remove_surface",
    handler=remove_surface,
    required_params=("at",),
    param_types={"at": "number", "refuse_on_solve_refs": "boolean"},
    description=(
        "Remove an interior surface (1..N-2). Refuses OBJECT/IMAGE and refuses "
        "removing the stop (reassign via set_stop_surface first). Returns an additive "
        "'solve_refs' block on every success: state is none_affected, affected or "
        "could_not_scan, and 'affected' lists each solve on a SURVIVING row that "
        "references the removed row, with what it became. Removing the row a "
        "SurfacePickup names as its SOURCE was measured to destroy that solve for the "
        "thickness cell and for coordinate-break Par1-Par5, OpticStudio 2025 R1; on "
        "any other cell or solve family the reference is reported as a FACT and the "
        "consequence is measured per call, never predicted. Set "
        "refuse_on_solve_refs=true to "
        "refuse such a removal instead, before anything is mutated. Limitation: this "
        "reports solve references only, from this call only -- not merit-function, "
        "tolerance or multi-configuration surface references, and the shrink path "
        "inside apply_lens_spec does not report them at all."
    ),
)

SET_STOP_SURFACE_SPEC = ToolSpec(
    name="set_stop_surface",
    handler=set_stop_surface,
    required_params=("surface",),
    param_types={"surface": "number"},
    description=(
        "Set the aperture stop to an interior surface (1..N-2) with a read-back "
        "that catches the silent image-stop no-op."
    ),
)

# Module-level TOOL_SPEC list the server registers (this module ships six).
TOOL_SPECS = (
    READ_SURFACE_SPEC,
    SURFACE_COUNT_SPEC,
    SET_SURFACE_SPEC,
    INSERT_SURFACE_SPEC,
    REMOVE_SURFACE_SPEC,
    SET_STOP_SURFACE_SPEC,
)
