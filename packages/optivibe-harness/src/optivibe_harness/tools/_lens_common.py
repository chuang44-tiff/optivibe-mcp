"""tools/_lens_common.py — private shared helpers for the lens tools.

NOT dispatchable (no ``TOOL_SPEC``). These are the building blocks every lens
mutator/reader reuses so the probe-grounded safety rules live in exactly one
place:

- ``_require_int_index`` — pull + validate an index param. **Rejects ``bool``**
  (``bool`` is an ``int`` subclass, and a bool index is a client bug, the
  hard-crash bound trap), rejects non-int / non-exact ``int()``.
- ``_readback_ok`` — the read-back equality oracle. Floats compare via
  ``math.isclose`` (rel/abs ``1e-9``) with ``+inf``/``-inf`` equal to same-sign
  inf (planar surfaces) and ``nan`` never equal; post-reload float drift
  (``123.456`` -> ``123.45599999999999``) is inside tolerance. Strings
  (Material/Comment) compare case-insensitively vs the canonical spelling
  actually written.
- ``_verify_or_raise`` — read-back gate; raises ``SurfaceWriteError`` carrying
  ``(field, intended, actual, surface)`` on a mismatch.
- Bounds-precondition helpers — raise ``ToolParamError`` BEFORE any typed call
  (an out-of-range ``InsertNewSurfaceAt`` HARD-CRASHES the engine, so a
  bounds error can NEVER be an exception handler — it is a client-side gate).

Live ZOS-API integration: N/A this tier (pure-Python helpers; the read-back
oracle is grounded by the captured probe fixture, not a live backend).
"""
import math

from ..errors import SurfaceWriteError, ToolParamError

# Read-back tolerances (spec §A). Tight enough to catch a real rejected write,
# loose enough to absorb the post-reload float drift the probe captured.
READBACK_REL_TOL = 1e-9
READBACK_ABS_TOL = 1e-9

# Finite-conjugate build robustness: the engine truncates a Comment to a
# FIXED 32-char clean PREFIX (``stored == intended[:32]``, deterministic).
# This is the SINGLE place the cap lives — a firmware cap change is a one-constant
# edit caught by the live gate (the cap is pinned to the probe fact, NOT a config knob).
COMMENT_MAX_CHARS = 32


def _image_thickness_ok(i, count, intended, actual):
    """Image-only inf-pin exemption.

    A FINITE thickness intended on the LAST surface (image) that reads back ``+inf``
    is the engine's image-plane pin (engine-owned, like the auto semi-diameter), NOT
    a mismatch. Everything else falls through to the shared ``_readback_ok``:
    - INTERIOR surfaces (``i != count-1``): an interior ``0 -> inf`` collapse is a
      REAL mismatch -> not exempted.
    - ``-inf`` actual (``actual > 0`` fails): NOT exempt -> mismatch.
    - the inf-vs-inf read-vs-read (non-finite ``intended``): the first branch's
      ``isfinite(intended)`` is False -> fall through to ``_readback_ok(inf, inf)``
      -> True.

    Lives in ``_lens_common`` so BOTH the apply verifier (``_spec_roundtrip_mismatches``)
    AND ``set_surface``'s own per-field thickness read-back share ONE oracle: the live
    engine pins the IMAGE thickness to ``+inf`` even on the immediate set_surface
    re-read of a finite write, so set_surface must accept that pin exactly as the
    verifier does (the verifier-only fix would otherwise still rollback because the
    write goes THROUGH set_surface — the real-code locus the spec's
    verifier-only ruling did not account for).
    """
    n_last = count - 1
    if (i == n_last
            and _is_inf(actual) and actual > 0
            and isinstance(intended, (int, float))
            and math.isfinite(intended)):
        return True
    return _readback_ok(intended, actual)


def _comment_readback_ok(intended, actual):
    """Comment read-back oracle: the engine truncates to ``COMMENT_MAX_CHARS``.

    The engine stores a CLEAN PREFIX, so a faithful write makes
    ``actual == intended[:COMMENT_MAX_CHARS]`` exactly. Accept iff
    ``_readback_ok(intended[:CAP], actual)`` (case-insensitive via the unchanged
    shared string path). Prefix-against-INTENDED: a WRONG comment (``'abc…'`` vs
    ``'xyz…'``) still mismatches — only the legitimate truncation of OUR OWN
    intended string is tolerated, a dropped write (engine kept a different/old
    prefix or ``""``) still mismatches (fail-closed).
    """
    if not isinstance(intended, str) or not isinstance(actual, str):
        return False
    return _readback_ok(intended[:COMMENT_MAX_CHARS], actual)


def _clear_material_to_air(lde, i):
    """ACTIVELY clear surface ``i``'s material to canonical air.

    Air is the EMPTY string (``Material=""``); ``"AIR"`` is a TRAP — the engine
    stores the bogus literal ``"AIR"``, which is NOT air. This writes ``""`` and
    read-back-verifies with an EXACT ``== ""`` compare (NOT ``_readback_ok``, by
    design — the air oracle is unambiguous, so ``"AIR"`` / ``"air"`` / ``" "`` /
    stale glass all fail). Idempotent: an already-air surface is a no-op (no
    write). A failed clear raises ``SurfaceWriteError`` into the EXISTING atomic
    rollback. NEVER write ``"AIR"``.

    SHARED locus (L30): both ``apply_lens_spec`` (via ``lens_spec``'s re-export) and
    ``substitute_glass`` (the B3 air arm) consume THIS one copy — a second inlined
    clear-to-air path is a live opportunity for the ``"AIR"`` trap to
    reappear, so it lives in exactly one place.
    """
    row = lde.GetSurfaceAt(i)
    if str(row.Material) == "":
        return  # already air -> idempotent no-op, no write
    row = lde.GetSurfaceAt(i)
    row.Material = ""  # "" is canonical air; NEVER write "AIR"
    row = lde.GetSurfaceAt(i)  # re-fetch (never hold the proxy)
    actual = str(row.Material)
    if actual != "":  # exact ==""; "AIR"/"air"/" "/stale glass all fail
        raise SurfaceWriteError(
            f"surface {i} material clear-to-air did not take effect: Material read "
            f"back {actual!r} (expected '' canonical air; NOTE: 'AIR' is a bogus "
            "literal, not air — only '' is air).",
            field="material", intended="", actual=actual, surface=i,
        )


def _require_int_index(params, key):
    """Pull ``params[key]`` and return it as an ``int``, or raise ``ToolParamError``.

    - A missing key -> ``ToolParamError`` (dispatch enforces presence for required
      params, but composition helpers call this directly, so guard here too).
    - ``bool`` is REJECTED outright: it is an ``int`` subclass, and a bool index is
      a client bug (index bounds are a hard-crash guard, so a bool
      sneaking through as ``0``/``1`` is exactly the kind of silent miswrite the
      precondition exists to stop).
    - A non-int that is not an EXACT integer (``1.5``, ``"3"`` that is not an int,
      an object) -> ``ToolParamError``. A float that is integral (``3.0``) is
      accepted and coerced (a JSON round-trip can turn an int into a float).
    """
    if key not in params:
        raise ToolParamError(f"missing required index param {key!r}")
    value = params[key]
    if isinstance(value, bool):
        raise ToolParamError(
            f"index param {key!r} must be an integer, not a bool ({value!r})"
        )
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value == int(value):
            return int(value)
        raise ToolParamError(
            f"index param {key!r} must be an integer, got non-integral float {value!r}"
        )
    raise ToolParamError(
        f"index param {key!r} must be an integer, got {type(value).__name__} {value!r}"
    )


def _require_bool_param(params, name):
    """Pull + validate an OPTIONAL strict-boolean param. Default ``False``. NO engine read.

    The ``optimize_variable._require_replace_solve`` rule (itself the ``promote_best``
    ``force`` precedent), parameterised by NAME so a second opt-in flag does not become a
    second copy of the rule. ``1``, ``"true"``, ``"yes"`` and ``[]`` must NOT enable an
    opt-in behaviour — a mode reached by truthiness is a mode nobody chose (the measured
    ``force="no"`` defect measured on the save/promote clearance gate).

    A non-bool is not silently read as "no" either: that would let a caller who meant to
    enable strict mode believe they had, and then destroy a solve relationship on the very
    call they were guarding against.

    WHICH LINE DECIDES, stated because a mutation measured it on the original: the
    ``isinstance`` REFUSAL enforces strictness — replacing the ``is True`` below with
    ``bool(value)`` is INERT, because by then the domain is already exactly
    ``{True, False}``. The ``is True`` is a redundant backstop.

    ZERO ENGINE READS on every path, which is why a caller may hoist it above its first
    engine call (``remove_surface`` does exactly that).

    THE ``_require_replace_solve`` RE-POINT IS DEFERRED, not forgotten: that reader's
    message is pinned byte-for-byte by a test owned elsewhere, so re-pointing it risks a
    reddened pin
    for zero behaviour change. Two readers with one ticket beats that.
    """
    value = params.get(name, False)
    if not isinstance(value, bool):
        raise ToolParamError(
            f"{name} must be a boolean (true/false), got {type(value).__name__} "
            f"{value!r}; it is refused rather than read as false, because a caller who "
            "meant to enable it must not silently not have"
        )
    return value is True


def _is_inf(value):
    """True if ``value`` is a float infinity (either sign)."""
    return isinstance(value, float) and math.isinf(value)


def _readback_ok(intended, actual):
    """Return True if a written value reads back as intended.

    Numeric path:
    - ``+inf``/``-inf`` compare EQUAL only to same-sign inf (planar surfaces).
      A finite-vs-inf pair is NOT equal.
    - ``nan`` NEVER equals anything (a nan read-back is always a mismatch).
    - otherwise ``math.isclose(intended, actual, rel_tol, abs_tol)`` (absorbs the
      post-reload float drift).

    String path (Material/Comment): case-insensitive equality (the canonical
    catalog spelling is what gets written, and read-back compares
    case-insensitively against it).

    A type mismatch (one numeric, one string) is a mismatch.
    """
    # String comparison (canonical case-insensitive) when both are strings.
    if isinstance(intended, str) or isinstance(actual, str):
        if isinstance(intended, str) and isinstance(actual, str):
            return intended.casefold() == actual.casefold()
        return False

    # From here both are expected to be numeric.
    try:
        f_intended = float(intended)
        f_actual = float(actual)
    except (TypeError, ValueError):
        return False

    if math.isnan(f_intended) or math.isnan(f_actual):
        return False
    if math.isinf(f_intended) or math.isinf(f_actual):
        # inf equals only same-sign inf; finite-vs-inf is a mismatch.
        return f_intended == f_actual
    return math.isclose(
        f_intended, f_actual, rel_tol=READBACK_REL_TOL, abs_tol=READBACK_ABS_TOL
    )


def _verify_or_raise(field, intended, actual, *, surface):
    """Raise ``SurfaceWriteError`` if ``actual`` did not read back as ``intended``.

    The single read-back gate every mutator funnels through. On a
    mismatch the raised error carries structured ``(field, intended, actual,
    surface)`` so the dispatch envelope message is fully diagnostic.
    """
    if not _readback_ok(intended, actual):
        raise SurfaceWriteError(
            f"{field} write rejected by engine: intended={intended!r} "
            f"actual={actual!r} surface={surface}",
            field=field,
            intended=intended,
            actual=actual,
            surface=surface,
        )


# --------------------------------------------------------------------------- #
# Bounds preconditions — raised BEFORE any typed ZOS-API call.
# OBJECT = surface 0, IMAGE = surface N-1, N = lde.NumberOfSurfaces.
# --------------------------------------------------------------------------- #
def _require_read_index(surface, n):
    """read_surface / set_stop read range: ``0 <= surface <= N-1``."""
    if not (0 <= surface <= n - 1):
        raise ToolParamError(
            f"surface {surface} out of range; valid 0..{n - 1} (N={n})"
        )
    return surface


def _require_geometry_index(surface, n):
    """set_surface geometry / substitute_glass range: ``1 <= surface <= N-1``.

    OBJECT (0) geometry writes are refused (the object surface is not a lens
    surface you write radii on); IMAGE (N-1) is allowed for these.
    """
    if not (1 <= surface <= n - 1):
        raise ToolParamError(
            f"surface {surface} out of range for a geometry/material write; "
            f"valid 1..{n - 1} (OBJECT 0 and beyond IMAGE are refused; N={n})"
        )
    return surface


def _require_stop_index(surface, n):
    """set_stop_surface range: ``1 <= surface <= N-2`` (never OBJECT or IMAGE).

    Setting ``IsStop`` on the image surface silently no-ops, so the stop
    is restricted to an interior surface up front.
    """
    if not (1 <= surface <= n - 2):
        raise ToolParamError(
            f"surface {surface} invalid for the stop; valid 1..{n - 2} "
            f"(stop on OBJECT 0 or IMAGE {n - 1} is invalid; N={n})"
        )
    return surface


def _require_insert_at(at, n):
    """insert_surface range: ``1 <= at <= N-1`` (insert BEFORE surface ``at``).

    An out-of-range ``InsertNewSurfaceAt`` HARD-CRASHES the engine (IPC
    pipe death), so this gate is a hard client-side precondition — the engine
    method is NEVER reached when ``at`` is out of range. You may insert before
    IMAGE (``at = N-1``); ``at = 0`` (before OBJECT) and ``at = N`` (after IMAGE)
    are refused.
    """
    if not (1 <= at <= n - 1):
        raise ToolParamError(
            f"insert position {at} out of range; valid 1..{n - 1} "
            f"(cannot insert before OBJECT 0 or beyond IMAGE; N={n})"
        )
    return at


def _require_remove_at(at, n):
    """remove_surface range: ``1 <= at <= N-2`` (never OBJECT 0 or IMAGE N-1)."""
    if not (1 <= at <= n - 2):
        raise ToolParamError(
            f"remove position {at} out of range; valid 1..{n - 2} "
            f"(cannot remove OBJECT 0 or IMAGE {n - 1}; N={n})"
        )
    return at
