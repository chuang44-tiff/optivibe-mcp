"""tools/_measurement_common.py — private substrate for the measurement tools.

NOT dispatchable (no ``TOOL_SPEC``). The shared, probe-grounded correctness
primitives the four ``analysis_measure`` tools reuse so the load-bearing rules
live in exactly one place (mirrors ``_analysis_common`` / ``_optimize_common``):

- ``read_operand_slots(system, code, slots_by_name)`` — the NAMED-slot 9-arg
  ``GetOperandValue`` reader (the named-slot firewall). A NAMED slot dict
  + an operand-specific slot->position map means a value can NEVER land in the
  wrong positional arg (the live 0.0 trap). Reuses ``analysis_operand``'s exact
  ``suspicious_sentinel`` predicate. Returns ``(raw, suspicious)``.
- ``valid_density(n)`` — the ONE shared Samp/Ring predicate: an int
  (or integral float) ``>= 1``. The validator and the executor key on this SAME
  predicate (no accept-set divergence, L30). NEVER clamps — a bad value is the
  caller's bug, surfaced by the caller as ``measurement_param``.
- ``with_best_focus(system, wave)`` — the save->mutate->scan->restore context
  manager (the correctness crux). Captures the back-airgap thickness
  AND its solve, runs a bounded per-wave scan (min RWCE / max STRH) or QuickFocus
  for poly, ALWAYS restores in ``finally``, and read-back-verifies the restore
  (``restore_verified``). Fail-closed degrade paths never leave the system mutated.
- ``resolve_back_airgap(system)`` — the read-guarded back-airgap surface resolver
  (the surface BEFORE the image surface). A read throw -> fail-closed (None).

Live ZOS-API integration: exercised by the measurement-analysis live test;
unit-tested against the fixture-seeded fakes
reproducing the probe slot semantics + per-wave best-focus thickness scan.
"""
import math
from contextlib import contextmanager

from .._io import safe_float
from .analysis_operand import _merit_operand_enum, _SUSPICIOUS_MAGNITUDE
from ..enums import _resolve_enum

# A 9-arg GetOperandValue takes (type, a2, a3, a4, a5, a6, a7, a8, a9). The
# trailing args are MANDATORY (analysis_operand). Unused slots are
# 0 (int) or 0.0 (float) — the engine ignores them per operand.
_GETOPERAND_ARITY = 9


def suspicious_sentinel(raw):
    """The overflow/sentinel predicate (reused EXACTLY from analysis_operand).

    A reading is suspicious when it is non-finite (inf/nan) OR its magnitude
    reaches the sentinel threshold (``abs >= 1e10`` — the rot-sym EFLX/EFLY 1e10
    sentinel). Decided on the RAW reading BEFORE ``safe_float``
    stringifies a non-finite value. A non-number degrades to suspicious (it cannot
    be a valid scalar). NEVER raises.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return True
    return (not math.isfinite(raw)) or abs(raw) >= _SUSPICIOUS_MAGNITUDE


def read_operand_slots(system, code, slots_by_name):
    """Read one operand scalar via the 9-arg ``GetOperandValue`` with NAMED slots.

    ``code`` is the operand name (resolved against the live ``MeritOperandType``
    enum — the source of truth). ``slots_by_name`` is ``{position: ("Header",
    value)}`` keyed on the operand's GUI parameter-cell positions (2..9), built by
    the caller from the operand-specific slot map so a value can NEVER
    land in the wrong positional arg (the silent 0.0 trap).

    A position not present in ``slots_by_name`` defaults to 0 (an unused slot the
    engine ignores). Position values are coerced to the slot's natural numeric
    type by the engine; we pass them through verbatim. Returns ``(raw, suspicious)``
    — ``raw`` is the RAW reading (the caller passes it through ``safe_float`` for
    the wire); ``suspicious`` is the sentinel predicate on the raw value.
    """
    enum_type = _merit_operand_enum(system)
    member = _resolve_enum(enum_type, code)
    # Build the 8 positional args (a2..a9); each defaults to 0 unless named.
    args = []
    for pos in range(2, 2 + (_GETOPERAND_ARITY - 1)):  # positions 2..9 (8 slots)
        if pos in slots_by_name:
            args.append(slots_by_name[pos][1])
        else:
            args.append(0)
    raw = system.MFE.GetOperandValue(member, *args)
    return raw, suspicious_sentinel(raw)


def _gap_is_collimated(system, surface, tol_deg=1e-3):
    """True iff the on-axis marginal ray is (near-)collimated AFTER ``surface``.

    Reads the on-axis marginal ray angle (RANG, deg) in the space AFTER ``surface``
    via ``read_operand_slots``. RANG carries ``Surf`` at slot 2; the ray-operand slot
    map is ``Surf=2, Wave=3, Hx=4, Hy=5, Px=6, Py=7``. The on-axis marginal ray is
    ``Hx=Hy=0, Px=0, Py=1``.

    The ghost dummy gap = 0.0 deg; the SMALLEST real
    converging gap = 0.0287 (28x above ``tol_deg``). Tri-state return:
      - ``True``  -> ``|marginal angle| < tol_deg`` (collimated: moving this
                     thickness repositions nothing downstream),
      - ``False`` -> provably converging/diverging,
      - ``None``  -> UNPROVABLE (a suspicious sentinel / non-finite reading, or a
                     read throw).
    The caller NEVER acts on ``None`` (A1 -> don't freeze + WARN; A3 -> don't flag).
    NEVER raises.
    """
    try:
        raw, suspicious = read_operand_slots(
            system, "RANG",
            {2: ("Surf", int(surface)), 3: ("Wave", 1),
             4: ("Hx", 0), 5: ("Hy", 0), 6: ("Px", 0), 7: ("Py", 1)})
        if suspicious or not isinstance(raw, (int, float)) or isinstance(raw, bool):
            return None
        raw = float(raw)
        if not math.isfinite(raw):
            return None
        return abs(raw) < tol_deg
    except Exception:  # noqa: BLE001 — unprovable read -> None (fail-open/closed per caller)
        return None


def valid_density(n):
    """The ONE shared Samp/Ring density predicate: int(-egral) ``>= 1``.

    Accepts a Python ``int`` OR an integral ``float`` (a JSON round-trip can float
    an int: ``6.0`` -> 6). Rejects a bool, a non-number, a non-integral float, and
    any value ``< 1`` (incl. 0 — the silent 0.0 trap). NEVER clamps
    (the caller rejects with ``measurement_param``; clamping would hide a caller
    bug). Returns ``True`` iff ``n`` is a valid density.
    """
    if isinstance(n, bool):
        return False
    if isinstance(n, int):
        return n >= 1
    if isinstance(n, float):
        if not math.isfinite(n) or n != int(n):
            return False
        return int(n) >= 1
    return False


def density_as_int(n):
    """Coerce a ``valid_density``-approved value to a plain ``int`` (6 -> 6, 6.0 -> 6).

    PRECONDITION: ``valid_density(n)`` is True. Used by callers AFTER the guard so
    the engine always receives an integer ring/sample count.
    """
    return int(n)


def resolve_back_airgap(system):
    """Resolve the back-airgap surface number (the surface BEFORE the image surface).

    The image surface is ``NumberOfSurfaces - 1`` (0-based); the back airgap is the
    surface immediately before it. READ-GUARDED: a throw reading the
    surface count -> ``None`` (fail-closed — the caller degrades to current-plane
    numbers with ``best_focus_unavailable``, NEVER mutates on an unresolvable
    surface). Returns the integer surface number, or ``None``.
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — fail-closed: an unreadable count -> no resolve
        return None
    # Need at least OBJECT + one airgap + IMAGE (>=3 surfaces) for a back airgap to
    # exist before the image surface.
    if n < 3:
        return None
    return n - 2  # image is n-1; the back airgap is the surface before it.


def read_field_hy(system):
    """Read ``SystemData.Fields`` as ``[(index, y, hy)]`` (meridional Hy). NEVER raises.

    ``hy = Y / max|Y|`` (Hx = 0); a 0/absent max field height -> ``hy 0.0``. A read
    throw degrades to one on-axis field ``[(0, 0.0, 0.0)]`` (the caller still grades
    on-axis). Returns a NON-EMPTY list. (Lifted verbatim from ``_collimation._read_fields``
    — the canonical home is the measurement layer, L30; ``_collimation._read_fields``
    delegates here. NOT ``_layout_rays._read_fields`` — that returns the layout
    ``(hy, raw_y, flags)`` shape + carries the render/batch-trace caller blast radius.)
    """
    try:
        fields = system.SystemData.Fields
        n = int(fields.NumberOfFields)
    except Exception:  # noqa: BLE001 — an unreadable field set -> on-axis only
        return [(0, 0.0, 0.0)]
    if n <= 0:
        return [(0, 0.0, 0.0)]
    ys = []
    for i in range(1, n + 1):
        try:
            y = float(fields.GetField(i).Y)
        except Exception:  # noqa: BLE001 — an unreadable field Y -> 0.0
            y = 0.0
        ys.append(y)
    max_abs = max((abs(y) for y in ys), default=0.0)
    out = []
    for idx, y in enumerate(ys):
        hy = (y / max_abs) if max_abs > 0.0 else 0.0
        out.append((idx, y, hy))
    return out or [(0, 0.0, 0.0)]


def image_surface_index(system):
    """The image surface index (``NumberOfSurfaces - 1``), or ``None`` if unresolvable.

    Guarded (a ``NumberOfSurfaces`` throw -> ``None``); ``None`` when < 2 surfaces.
    Callers treat ``None`` as fail-closed (G1: loud surf-0; G2: analysis_empty). Shared
    by BOTH ``analysis_operand.get_operand`` (the surf default) AND
    ``analysis_measure.analyze_lateral_color`` (the internal image-surface resolve) — one
    home, no duplication (L30).
    """
    try:
        n = int(system.LDE.NumberOfSurfaces)
    except Exception:  # noqa: BLE001 — fail-closed: an unreadable count -> None
        return None
    return (n - 1) if n >= 2 else None


def _read_thickness(system, surface):
    """Read ``surface``'s thickness as a float; a throw -> ``None`` (fail-closed)."""
    try:
        return float(system.LDE.GetSurfaceAt(surface).Thickness)
    except Exception:  # noqa: BLE001 — an unreadable thickness -> degrade, never raise
        return None


def _write_thickness(system, surface, value):
    """Write ``surface``'s thickness; returns True on success, False on a throw."""
    try:
        system.LDE.GetSurfaceAt(surface).Thickness = value
        return True
    except Exception:  # noqa: BLE001 — a write throw is surfaced as a restore failure
        return False


def _solve_type_name(system, surface):
    """Read the thickness-cell solve TYPE name; a throw -> ``None`` (fail-closed).

    Capturing the SOLVE (not just the float) is mandatory: the back
    airgap is the optimizer's DOF and WILL carry a Variable/pickup solve; a
    value-only restore silently un-varies it (the L28 trap).
    """
    try:
        cell = system.LDE.GetSurfaceAt(surface).ThicknessCell
        return str(cell.GetSolveData().Type)
    except Exception:  # noqa: BLE001 — an unreadable solve -> None (treated as plain)
        return None


def _restore_solve(system, surface, original_solve_name):
    """Re-instate the captured thickness-cell solve (never un-vary a DOF).

    The minimum faithful restore for the back-airgap DOF: if the captured solve was
    ``Variable`` re-make it Variable; if it was ``Fixed`` (or unknown) leave the
    cell Fixed (the value write already restored the number). Returns True on
    success, False on a throw (the caller flags ``restore_verified=False``).

    NOTE (probe-grounded scope): the captured-then-restored solve set is
    Variable/Fixed — the two solves the back airgap carries in this harness's
    optimize loop (set_variable / clear_variable). A richer solve (pickup/marginal)
    is captured by NAME for the verification read-back; re-instating its full
    parameters is out of scope for the best-focus scan (which only needs to not
    silently un-vary the DOF). The post-restore verification still asserts the solve
    NAME matches, so an un-restorable richer solve surfaces as ``restore_verified=
    False`` rather than a silent desync.
    """
    try:
        cell = system.LDE.GetSurfaceAt(surface).ThicknessCell
        if original_solve_name == "Variable":
            cell.MakeSolveVariable()
        else:
            cell.MakeSolveFixed()
        return True
    except Exception:  # noqa: BLE001 — a restore throw -> caller flags it, never raises
        return False


def _eval_focus_metric(system, surface, thickness, code, slots_by_name):
    """Set the back-airgap thickness, then read the focus metric (RWCE/STRH).

    Writes ``thickness`` to ``surface`` and reads ``code`` (RWCE to minimize, STRH
    to maximize) via ``read_operand_slots``. Returns ``(value, suspicious)`` —
    ``value`` is the RAW float (or ``None`` if the write failed / the read was
    non-numeric, so the caller rejects the candidate).
    """
    if not _write_thickness(system, surface, thickness):
        return None, True
    raw, suspicious = read_operand_slots(system, code, slots_by_name)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None, True
    return float(raw), suspicious


def _golden_section_min(system, surface, lo, hi, code, slots_by_name, *,
                        iterations=24):
    """Golden-section MINIMIZE of ``code`` over the back-airgap thickness in [lo, hi].

    Used for the per-wave best focus (RWCE minimized — equivalently STRH maximized).
    A non-finite/None candidate mid-scan is REJECTED (the candidate is
    skipped, best-so-far kept). Returns ``(best_thickness, best_value)`` or
    ``(None, None)`` if no finite candidate was found in the window (the caller
    degrades to the current plane with ``best_focus_unavailable``).
    """
    invphi = (math.sqrt(5.0) - 1.0) / 2.0  # 1/phi ~ 0.618
    invphi2 = (3.0 - math.sqrt(5.0)) / 2.0  # 1/phi^2

    best_t = None
    best_v = None

    def _consider(t):
        nonlocal best_t, best_v
        v, suspicious = _eval_focus_metric(system, surface, t, code, slots_by_name)
        if v is None or not math.isfinite(v) or suspicious:
            return None
        if best_v is None or v < best_v:
            best_t, best_v = t, v
        return v

    a, b = lo, hi
    c = a + invphi2 * (b - a)
    d = a + invphi * (b - a)
    fc = _consider(c)
    fd = _consider(d)
    for _ in range(iterations):
        # If a candidate was unraytraceable, shrink toward the readable side rather
        # than trusting a None comparison (defensive — keeps best-so-far).
        if fc is None and fd is None:
            break
        if fd is None or (fc is not None and fc < fd):
            b, d, fd = d, c, fc
            c = a + invphi2 * (b - a)
            fc = _consider(c)
        else:
            a, c, fc = c, d, fd
            d = a + invphi * (b - a)
            fd = _consider(d)
    return best_t, best_v


def _quickfocus_poly(system):
    """Run QuickFocus (poly best plane): RMSWavefront + UseCentroid.

    ``Tools.OpenQuickFocus()`` -> ``Criterion = QuickFocusCriterion.RMSWavefront``
    (probe: there is NO ``Wavefront`` member) -> ``UseCentroid = True`` ->
    ``RunAndWaitForCompletion()``. Reaps the tool via ``Close()`` in a ``finally``
    (the L22 analog). Returns True if it ran, False on any throw (the caller
    degrades to ``best_focus_unavailable``). QuickFocus mutates the back-airgap
    thickness itself; the surrounding context manager restores it afterward.
    """
    try:
        qf = system.Tools.OpenQuickFocus()
    except Exception:  # noqa: BLE001 — QuickFocus unavailable -> degrade
        return False
    if qf is None:
        return False
    try:
        criterion_member = _quickfocus_criterion(system, "RMSWavefront")
        try:
            qf.Criterion = criterion_member
            qf.UseCentroid = True
        except Exception:  # noqa: BLE001 — a settings throw -> still try the run
            pass
        try:
            qf.RunAndWaitForCompletion()
        except Exception:  # noqa: BLE001 — a run throw -> degrade
            return False
        return True
    finally:
        try:
            qf.Close()
        except Exception:  # noqa: BLE001 — QuickFocus teardown must never raise
            pass


def _quickfocus_criterion(system, name):
    """Resolve the live ``QuickFocusCriterion`` member (probe: RMSWavefront, NOT Wavefront).

    A fake system injects ``_quickfocus_enums["QuickFocusCriterion"]`` so unit tests
    resolve without the backend; otherwise the live ``ZOSAPI.Tools.General`` (or
    ``ZOSAPI.Tools``) namespace. A resolution failure -> ``None`` (the caller runs
    QuickFocus with the engine default criterion rather than crashing).
    """
    injected = getattr(system, "_quickfocus_enums", None)
    if injected is not None and "QuickFocusCriterion" in injected:
        try:
            return _resolve_enum(injected["QuickFocusCriterion"], name)
        except Exception:  # noqa: BLE001 — degrade to engine default
            return None
    try:  # pragma: no cover - live backend path
        import ZOSAPI.Tools.General as _tg  # type: ignore

        return getattr(_tg.QuickFocusCriterion, name)
    except Exception:  # noqa: BLE001 — degrade to engine default
        return None


@contextmanager
def with_best_focus(system, *, wave=None, code="RWCE", slots_by_name=None,
                    scan_halfwidth=0.5, poly=False):
    """Save->mutate->scan->restore best-focus context manager (the correctness crux).

    Yields a ``focus`` dict the caller reads INSIDE the ``with`` block to take its
    best-focus operand readings (the back-airgap thickness has been shifted to the
    best plane); the thickness AND solve are ALWAYS restored in ``finally``, then
    read-back-verified.

    Parameters:
    - ``wave`` — the wavelength index for the per-wave scan metric (``code``); None
      / ``poly=True`` selects QuickFocus (one polychromatic plane).
    - ``code`` — the per-wave scan metric operand (``"RWCE"`` minimized; the
      probe-confirmed best-focus criterion). ``slots_by_name`` is its NAMED slot map
      for the scan reads (the caller supplies the density+wave slots).
    - ``scan_halfwidth`` — the +/- mm window around the loaded thickness the
      bounded golden-section scan searches (the probe's per-wave shifts were
      <= 0.06 mm; 0.5 mm is a safe bracket).
    - ``poly`` — run QuickFocus instead of the per-wave scan.

    The yielded ``focus`` dict:
    ``{"available": bool, "plane_thickness": float|None, "reason": str|None,
       "restore_verified": bool, "mutation_warning": str|None}``.

    Degrade paths (never raise, never leave mutated): an unresolvable back-airgap
    surface OR a non-finite loaded thickness -> ``available:False`` +
    ``best_focus_unavailable`` (NO mutation attempted); no improving plane in the
    window -> ``available:False`` + the current plane restored. A restore throw /
    a post-restore mismatch -> ``restore_verified:False`` + a loud
    ``mutation_warning`` (the system MAY be left mutated; the grader must not trust
    downstream numbers silently).
    """
    focus = {
        "available": False,
        "plane_thickness": None,
        "reason": None,
        "restore_verified": True,
        "mutation_warning": None,
    }

    surface = resolve_back_airgap(system)
    if surface is None:
        focus["reason"] = "best_focus_unavailable"
        focus["mutation_warning"] = (
            "could not resolve the back-airgap surface; reporting the current plane "
            "only (no mutation attempted)"
        )
        yield focus
        return

    t0 = _read_thickness(system, surface)
    if t0 is None or not math.isfinite(t0):
        focus["reason"] = "best_focus_unavailable"
        focus["mutation_warning"] = (
            f"the back-airgap thickness on surface {surface} is unreadable/non-finite; "
            "reporting the current plane only (no mutation attempted)"
        )
        yield focus
        return

    original_solve = _solve_type_name(system, surface)

    try:
        if poly or wave is None:
            ran = _quickfocus_poly(system)
            if ran:
                focus["available"] = True
                focus["plane_thickness"] = safe_float(_read_thickness(system, surface))
            else:
                focus["reason"] = "best_focus_unavailable"
                focus["mutation_warning"] = (
                    "QuickFocus did not run; reporting the current plane only"
                )
        else:
            lo = t0 - scan_halfwidth
            hi = t0 + scan_halfwidth
            best_t, best_v = _golden_section_min(
                system, surface, lo, hi, code, slots_by_name or {},
            )
            if best_t is None:
                focus["reason"] = "best_focus_unavailable"
                focus["mutation_warning"] = (
                    "no improving best-focus plane was found in the scan window; "
                    "reporting the current plane"
                )
            else:
                # Park the back airgap at the best plane so the caller's reads INSIDE
                # the with-block see the best-focus numbers.
                _write_thickness(system, surface, best_t)
                focus["available"] = True
                focus["plane_thickness"] = safe_float(best_t)
        yield focus
    finally:
        # RESTORE (always): write the thickness back AND re-instate the solve.
        restored_t = _write_thickness(system, surface, t0)
        restored_solve = _restore_solve(system, surface, original_solve)
        # POST-RESTORE verification read-back: re-read thickness + solve; both
        # must equal the captured originals or the system MAY be mutated.
        ok_t = False
        rb_t = _read_thickness(system, surface)
        if rb_t is not None and math.isfinite(rb_t):
            ok_t = math.isclose(rb_t, t0, rel_tol=1e-9, abs_tol=1e-9)
        rb_solve = _solve_type_name(system, surface)
        ok_solve = (rb_solve == original_solve) if original_solve is not None else True
        if not (restored_t and restored_solve and ok_t and ok_solve):
            focus["restore_verified"] = False
            focus["mutation_warning"] = _join(
                focus.get("mutation_warning"),
                f"best-focus restore on surface {surface} did NOT verify "
                f"(thickness intended={t0!r} actual={rb_t!r}; solve intended="
                f"{original_solve!r} actual={rb_solve!r}); the system may be mutated — "
                "downstream numbers must not be trusted silently",
            )


def _join(existing, new):
    """Append ``new`` to ``existing`` (either may be None)."""
    if existing:
        return f"{existing}; {new}"
    return new
