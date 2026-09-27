"""_grounding.py — the harness's OWN read of the reference operand catalog (S-REF-3).

Reviewed over three audit rounds. Before changing anything here: several of the choices
below look over-cautious and are each closing a specific audited failure.

WHAT THIS IS FOR. ``add_math_constraint`` authors merit constraints from an operand code
plus a ``sign_convention`` that the AGENT read from ``lookup_operand`` and handed back.
Until S-REF-3 the handler simply believed it. This module lets the handler get its own
answer, so a relayed convention that contradicts the reference can be refused.

WHAT IT DELIBERATELY DOES **NOT** DO. It has ONE refusal-capable answer:
the catalog states a convention and the relayed one differs. Everything else -- no
catalog, an untrusted catalog, a code the catalog does not carry, a code whose recorded
convention is null -- collapses into ``unavailable``, and the caller then behaves exactly
as it did before this cycle. That is the owner's cold-clone ruling (tracked floor +
enrich): the harness's tracked tables are the always-present floor and this module only
ever ADDS a check. It must never be the reason a valid call fails.

THE LIVE ENUM IS THE AUTHORITY ON EXISTENCE, NOT THIS CATALOG. ``merit_math`` already
refuses any code the live ``MeritOperandType`` enum does not know (``merit_math.py``,
``_resolve_enum``). A code absent HERE therefore means the user-built catalog is older or
incomplete -- never that the agent invented something -- so absence must not refuse.
(The draft spec got this backwards; an audit falsified it.)

NEVER raises past its own boundary. NEVER writes. NEVER creates a file.
"""
import os
import sys
import threading

#: Nothing to say about this code -- the caller must behave exactly as it did pre-S-REF-3.
GROUNDING_UNAVAILABLE = "unavailable"
#: The catalog states a usable convention for this code, and it is trustworthy.
GROUNDING_OK = "ok"

# Module-level cache + the ONE lock. The lock covers the stat, the open, the build, the
# cache swap, the warn-once flag AND the query itself. The query is inside it because
# ``open_catalog`` hands back a connection used from async MCP worker threads
# (check_same_thread=False): with the query outside, one worker could be mid-SELECT while
# another detected a replaced catalog and closed that same connection -- the belt would
# turn the ProgrammingError into ``unavailable`` and SILENTLY skip grounding while a
# perfectly good catalog was present, which is the worst available outcome because it is
# indistinguishable from a cold clone. A single-row primary-key lookup on an in-memory DB
# is cheap enough to serialize. (Audit r1#6, r2#2.)
_LOCK = threading.Lock()
_CACHE = None          # {"conn": sqlite3.Connection, "stamp": (mtime_ns, size), "path": str}
_WARNED = False        # one stderr WARN per process, never a raise


def _warn_once(what):
    """Emit at most ONE stderr WARN per process. Caller holds _LOCK."""
    global _WARNED
    if _WARNED:
        return
    _WARNED = True
    try:
        sys.stderr.write(
            "WARN optivibe_harness._grounding: reference operand catalog unusable "
            "(%s); add_math_constraint will not cross-check sign_convention. This is a "
            "MISSING CHECK, not an error -- the tool behaves as it did before S-REF-3.\n"
            % (what,)
        )
    except Exception:  # noqa: BLE001 -- a broken stderr must never break a dispatch
        pass


def _stamp(path):
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)


def _conn_locked(catalog_build):
    """Return a usable catalog connection, or None. Caller holds _LOCK.

    The catalog JSON is gitignored and user-built, so it can appear, vanish or be rebuilt
    under a running server. It is rebuilt with ``os.replace``, so content changes
    atomically -- but an in-memory DB built from it does NOT, which is why the stamp is
    re-checked on every call rather than cached for the process lifetime (audit r1#5).

    The stamp is taken BEFORE and AFTER the open and the result is cached only if the two
    agree. Stat-before-only files new content under the old stamp; stat-after-only is
    WORSE -- it files OLD content under the NEW stamp, so the stale build is never
    rebuilt again and can refuse correct calls indefinitely (audit r3#1).

    KNOWN LIMIT, accepted in the spec: a replacement preserving both st_size and
    st_mtime_ns is undetectable here. Closing it means hashing the file on every
    grounding call, which is not worth it.
    """
    global _CACHE
    path = catalog_build.CATALOG_JSON_PATH
    if not os.path.isfile(path):
        _CACHE = None                     # cold clone / public release: the normal case
        return None

    current = _stamp(path)
    if _CACHE is not None and _CACHE["path"] == path and _CACHE["stamp"] == current:
        return _CACHE["conn"]

    for _attempt in (0, 1):               # one retry, then give up rather than guess
        before = _stamp(path)
        conn = catalog_build.open_catalog()
        after = _stamp(path)
        if before == after:
            previous = _CACHE
            _CACHE = {"conn": conn, "stamp": after, "path": path}
            if previous is not None:
                try:
                    previous["conn"].close()
                except Exception:  # noqa: BLE001 -- closing a dead handle is not our problem
                    pass
            return conn
        try:
            conn.close()                  # raced with a rebuild; discard, do not publish
        except Exception:  # noqa: BLE001
            pass
    return None


def sign_convention_for(code, engine_version):
    """What the reference says this operand's ``sign_convention`` is.

    Returns exactly one of:
      ``(GROUNDING_OK, <non-empty str>)`` -- the catalog states a convention for ``code``
          and the catalog is trustworthy (schema and engine version both match).
      ``(GROUNDING_UNAVAILABLE, None)`` -- this module has NOTHING to say. Deliberately
          does not distinguish: no reference package; no catalog file; an untrusted
          catalog; ``code`` absent; ``code`` present with a null convention.

    ``engine_version`` is the OpticStudio version string of the LIVE session. It is
    required because schema compatibility says nothing about SEMANTIC freshness: an older
    same-schema catalog can carry a superseded convention and would then make the handler
    refuse a CORRECT call (audit r3#2). A catalog built against a different engine than
    the one running is therefore not allowed to refuse anything. Pass ``None`` offline --
    every offline test then exercises the degrade path by construction, and the refusal
    can only fire where a live engine says which version is running.

    RESIDUAL LIMIT, accepted and recorded in the spec: this detects a catalog built
    against a DIFFERENT engine version, not one built against the SAME version before a
    semantics correction. The alternative is never refusing at all.
    """
    # The input guards are INSIDE a try, because `not code` dispatches to the argument's
    # own __bool__: a str SUBCLASS passes isinstance and can raise from there. Without this
    # the "NEVER raises" claim above was false in exactly that corner -- found by the
    # doc-alignment read, the repo's hostile-subclass class (cf. the earlier __float__ case).
    # Unreachable through the MCP (JSON only yields plain str), reachable by a direct call.
    try:
        if not isinstance(code, str) or not code:
            return (GROUNDING_UNAVAILABLE, None)
        if not isinstance(engine_version, str) or not engine_version:
            return (GROUNDING_UNAVAILABLE, None)
    except Exception:  # noqa: BLE001 -- a hostile argument means "nothing to say"
        return (GROUNDING_UNAVAILABLE, None)

    with _LOCK:
        try:
            # Imported INSIDE the call, not at module scope: a harness install without the
            # reference package must degrade to ``unavailable``, never fail to import. The
            # same shape is already used by __main__.py and optivibe_doctor for the same
            # reason -- this follows an existing pattern rather than inventing one.
            from optivibe_reference import catalog_build

            conn = _conn_locked(catalog_build)
            if conn is None:
                return (GROUNDING_UNAVAILABLE, None)

            row = conn.execute(
                "SELECT sign_convention, schema_version, optic_studio_version "
                "FROM operand WHERE code = ?",
                (code,),
            ).fetchone()
            if row is None:
                return (GROUNDING_UNAVAILABLE, None)   # older/incomplete catalog, NOT an error

            convention, schema_version, catalog_engine = row[0], row[1], row[2]

            # Compare against the REFERENCE's own constant, read at call time -- not a
            # literal frozen here. A harness-local ``3`` keeps passing after the reference
            # bumps to 4, so the guard that exists to notice a bump could not (audit r1#8).
            if schema_version != catalog_build.SCHEMA_VERSION:
                return (GROUNDING_UNAVAILABLE, None)
            if catalog_engine != engine_version:
                return (GROUNDING_UNAVAILABLE, None)
            if not isinstance(convention, str) or not convention:
                return (GROUNDING_UNAVAILABLE, None)   # null convention: no usable source
            return (GROUNDING_OK, convention)
        except Exception as exc:  # noqa: BLE001 -- the belt. A grounding failure is a
            # MISSING CHECK, never a broken dispatch. Import errors, a corrupt catalog, a
            # schema that moved, a closed handle: all degrade to today's behaviour.
            _warn_once(type(exc).__name__)
            return (GROUNDING_UNAVAILABLE, None)


def reset_cache_for_tests():
    """Drop the cached connection, stamp and warn flag. TEST-ONLY; never called at runtime."""
    global _CACHE, _WARNED
    with _LOCK:
        if _CACHE is not None:
            try:
                _CACHE["conn"].close()
            except Exception:  # noqa: BLE001
                pass
        _CACHE = None
        _WARNED = False
