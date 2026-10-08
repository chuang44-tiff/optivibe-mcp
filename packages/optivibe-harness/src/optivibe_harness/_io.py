"""_io.py — shared durability substrate for the infrastructure modules.

These modules ARE the audit/training durability substrate: a torn JSONL line
corrupts training data, so writers fsync by default. ``FSYNC_ENABLED`` is the
single override the tests flip for speed.

House style mirrors ``scripts/boot_smoke.py``: utf-8 explicit, a guarded
``safe()``-style swallow idiom, ISO-8601 with a ``Z`` UTC marker.

Live ZOS-API integration: N/A this tier (pure-Python durability helpers; no
backend). The probe findings (A2) that ground ``safe_repr`` are captured in a
test fixture.
"""
import errno
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone

# fsync on by default — these modules are the durability substrate. Tests flip
# this to False (or pass fsync=False to a writer) for speed.
FSYNC_ENABLED: bool = True

# Volatile CPython object address (e.g. " at 0x000001E1F2B4D880") seen in the
# repr of typed .NET object handles and System.Double[] arrays (probe A2). It is
# non-deterministic, so it is scrubbed to keep the corpus diffable.
_ADDR_RE = re.compile(r" at 0x[0-9A-Fa-f]+")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (``...+00:00``)."""
    return datetime.now(timezone.utc).isoformat()


def safe_repr(obj, limit: int = 2000) -> str:
    """Guarded ``repr`` for arbitrary (incl. typed .NET) objects.

    - Scrubs the volatile ``" at 0x<hex>"`` address (probe A2) so output is
      deterministic and diffable.
    - Truncates to ``limit`` chars with a ``...[truncated N chars]`` marker.
    - Never raises: a failing ``repr`` becomes ``<unreprable {type}: {exc}>``.

    NEW-4: the fallback message interpolates the repr-error ``exc``; if that
    exception's OWN ``__str__`` raises, the f-string would re-raise and break the
    never-raise contract. So ``exc`` is rendered via a guarded ``str`` that falls
    back to a bare placeholder — every path returns a string, none raises.
    """
    try:
        text = repr(obj)
    except Exception as exc:  # noqa: BLE001 — never let a bad repr escape
        try:
            type_name = type(obj).__name__
        except Exception:  # noqa: BLE001
            type_name = "?"
        try:
            exc_text = str(exc)
        except Exception:  # noqa: BLE001 — the repr-error's __str__ raised too
            exc_text = "<unprintable error>"
        return f"<unreprable {type_name}: {exc_text}>"

    # Scrub the volatile address BEFORE truncation (it can sit anywhere).
    text = _ADDR_RE.sub("", text)

    if len(text) > limit:
        dropped = len(text) - limit
        text = text[:limit] + f"...[truncated {dropped} chars]"
    return text


def safe_exc(exc, repr_form: bool = False, limit: int = 2000) -> str:
    """Render an ALREADY-CAUGHT exception to text without ever raising.

    An error handler that interpolates its own ``exc`` into an f-string RE-ENTERS
    user code: ``str(exc)`` / ``repr(exc)`` run the exception's OWN ``__str__`` /
    ``__repr__``. A bridged .NET type with a broken ``ToString()`` — or a plain
    Python exception with a raising ``__str__`` — therefore makes the HANDLER raise.
    MEASURED on this package's two ``_never_raise`` decorators
    (``analysis_measure``/``tolerance_run``): both ESCAPED with ``RuntimeError``, the
    tool's typed ``measurement_param`` / ``tolerancing_run`` family was lost, and the
    dispatch envelope degraded to the generic ``internal`` family.

    ``repr_form=True`` renders ``repr(exc)`` (for the ``{exc!r}`` call sites); the
    default renders ``str(exc)``. Either way the OTHER renderer is tried as a
    fallback, then the bare type name, then a constant — every path returns a ``str``.

    THE GUARDS CATCH ``BaseException``, not ``Exception``, for the reason
    ``server._safe_error_text`` already records: a ``__str__`` that raises
    ``KeyboardInterrupt`` would otherwise walk straight out of a never-raise
    envelope. This function does no work anyone would want to interrupt — it renders
    a string for an exception that has ALREADY been caught.

    The volatile ``" at 0x<hex>"`` address is scrubbed and the result truncated,
    exactly as ``safe_repr`` does, so an engine-object repr embedded in an error
    message stays deterministic and bounded.
    """
    text = None
    for render in ((repr, str) if repr_form else (str, repr)):
        try:
            rendered = render(exc)
        except BaseException:  # noqa: BLE001 — the exception's own renderer raised
            continue
        if isinstance(rendered, str):
            text = rendered
            break
    if text is None:
        try:
            text = "<unprintable " + type(exc).__name__ + ">"
        except BaseException:  # noqa: BLE001 — even the type name is hostile
            return "<unprintable exception>"
    try:
        text = _ADDR_RE.sub("", text)
        if len(text) > limit:
            text = text[:limit] + "...[truncated %d chars]" % (len(text) - limit)
        return text
    except BaseException:  # noqa: BLE001 — a hostile ``str`` SUBCLASS reached here
        return "<unprintable exception>"


def safe_call(func, default=None):
    """Call ``func()`` and return its result; on ANY ``Exception`` return ``default``.

    The guarded-read idiom this module's docstring names, as a callable — for the
    sites where a read a docstring calls "non-fatal" sits OUTSIDE every ``try`` and
    is therefore fatal (H-3: the post-Close MFE re-read in ``optimize_run``).

    Pass a ``lambda`` when the ATTRIBUTE LOOKUP can throw too — a bridged .NET handle
    after teardown fails on the attribute, not only on the call. ``safe_call(lambda:
    obj.Member())`` guards both; ``safe_call(obj.Member)`` guards only the call,
    because the attribute is resolved BEFORE this function is entered.

    ``BaseException`` is deliberately NOT caught — a ``KeyboardInterrupt`` /
    ``SystemExit`` still propagates. This differs from ``safe_exc`` above: that one
    is rendering an exception already caught, this one is performing real work.
    """
    try:
        return func()
    except Exception:  # noqa: BLE001 — the guarded read; the caller supplies the default
        return default


def is_finite_number(value) -> bool:
    """True iff ``value`` is a real number that converts to a FINITE ``float``.

    ``math.isfinite`` is NOT a total predicate: on an ``int`` too large for a
    ``float`` it RAISES ``OverflowError`` rather than returning ``False`` — and so
    does ``float(value)``. A JSON payload reaches that state trivially: ``1e400``
    deserializes to ``inf``, and a bare 400-digit integer literal deserializes to an
    ``int`` no ``float`` can hold. A param door that PROMISES a typed refusal must
    therefore ask the finiteness question WITHOUT converting (H-2, measured:
    ``_require_pos_float(10**400)`` raised ``OverflowError`` past a door whose
    docstring promises ``OptimizeError(family="optimize_param")``).

    ``bool`` is not a number here — the param doors reject it separately, with their
    own message, before reaching this predicate.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # noqa: BLE001 — an int too large for a float is not finite
        return False


def safe_float(value):
    """Return ``value`` unchanged if it is a finite number, else a string sentinel.

    Non-finite floats (``inf``/``-inf``/``nan``) are not standard JSON, so they
    are converted to the string sentinels ``"inf"``/``"-inf"``/``"nan"`` exactly
    like ``result_repr`` already stringifies non-JSON values. This keeps the
    JSONL corpus strict-JSON parseable (``json.dumps(..., allow_nan=False)`` would
    otherwise raise). Finite floats (and ``None``) pass through untouched.
    """
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "nan"
        return "inf" if value > 0 else "-inf"
    return value


def atomic_write_bytes(path, data: bytes) -> None:
    """Atomically write ``data`` to ``path``.

    Creates a UNIQUE temp file in the SAME directory via
    ``tempfile.NamedTemporaryFile`` (so ``os.replace`` is atomic on the same
    filesystem AND two concurrent writers — even same-process threads — never
    collide on the temp name), flushes, fsyncs (if enabled), then ``os.replace``s
    into place. After the rename the parent directory is best-effort fsynced on
    POSIX so the rename is durably committed (skipped on Windows where dir
    handles are not fsync-able and ``os.replace`` is already atomic). On any
    error this writer removes ONLY its own temp file and re-raises.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    # Unique temp name in the SAME dir -> no cross-writer collision, and the
    # error-path cleanup can only ever touch THIS writer's temp.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    # Ownership of the raw mkstemp fd only transfers to os.fdopen's file object
    # AFTER os.fdopen succeeds. If os.fdopen itself raises (fd exhaustion), the
    # bare fd would leak — and on Windows a still-open fd blocks os.remove(tmp),
    # leaking the temp file too. So track whether the fd is still ours and close
    # it (guarded) on the failure path BEFORE removing the temp.
    fd_owned = True
    try:
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            # os.fdopen failed; the fd is still raw and ours to close.
            raise
        else:
            # The file object now owns the fd; closing fh closes the fd exactly
            # once. Mark the fd as no longer ours to avoid a double close.
            fd_owned = False
            with fh:
                fh.write(data)
                fh.flush()
                if FSYNC_ENABLED:
                    os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Close the raw fd if we still own it (os.fdopen never took ownership),
        # guarded so an already-closed fd never raises, and so the still-open fd
        # cannot block the temp removal on Windows.
        if fd_owned:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    # Best-effort parent-directory fsync so the rename is durably committed.
    # POSIX only: on Windows directory handles are not fsync-able and os.replace
    # is atomic without it. Respect FSYNC_ENABLED; never let this raise.
    if FSYNC_ENABLED and os.name == "posix":
        try:
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:  # noqa: BLE001 — best-effort durability, never raise
            pass


#: Seconds a writer spins for the per-file append lock before raising OSError (the
#: raise channel every caller already handles). Measured: 8 contending writers x 1000
#: fsynced appends complete in ~7-8 s on this box, so 30 s is a wedge, not contention.
_APPEND_LOCK_TIMEOUT_S = 30.0

#: Spin interval between non-blocking lock attempts.
_APPEND_LOCK_SPIN_S = 0.0002

#: The ONE byte the append lock covers: a SENTINEL far beyond any EOF this file will
#: ever reach (2**62). On Windows a byte-range lock is MANDATORY -- a reader whose
#: read() overlaps a locked byte gets PermissionError -- so the lock must sit where no
#: reader ever reads. The unlocked readers (artifact_sink.load_manifest, journal.load,
#: interaction_log.load) read [0, EOF) and never touch it. Measured: the
#: sentinel lock is granted on an empty file, on a 17 MB file, and 8 writers x 1000
#: appends with a reader looping load() beside them raised ZERO reader exceptions.
_APPEND_LOCK_SENTINEL = 2 ** 62

#: The errnos that mean "another holder has the lock" -- the ONLY ones the spin retries
#:. Windows: ``msvcrt.locking`` raises
#: EACCES (13) on an ``LK_NBLCK`` collision -- measured -- and EDEADLOCK (36) is its
#: documented give-up code. POSIX: ``flock(LOCK_NB)`` raises EWOULDBLOCK / EAGAIN (EACCES
#: kept for older kernels) -- REASONED, not measured. Any OTHER OSError (a bad fd, an
#: unsupported filesystem) raises at once, before any byte is written.
_APPEND_LOCK_CONTENTION_ERRNOS = (
    frozenset({errno.EACCES, errno.EDEADLOCK}) if os.name == "nt"
    else frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}))


def _append_lock_try(fd):
    """ONE non-blocking attempt on the sentinel byte; raises OSError when held."""
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, _APPEND_LOCK_SENTINEL, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _append_lock(fh):
    """Take the per-file append lock on ``fh`` (the SENTINEL byte, a mutex, not data).

    Windows: ``msvcrt.locking(LK_NBLCK, 1)`` at ``_APPEND_LOCK_SENTINEL`` -- the fd is
    ``os.lseek``'d there EXPLICITLY before every attempt (``_locking`` locks from the
    CURRENT file position, so an implicit position would lock whatever byte the last
    write left the pointer at). ``LK_LOCK`` is NOT used: its retry sleeps a full second
    per collision. POSIX: ``fcntl.flock(LOCK_EX | LOCK_NB)`` -- whole-file, advisory, a
    reader is never blocked; REASONED, not measured on this (win32) box.

    Spins every ``_APPEND_LOCK_SPIN_S`` up to ``_APPEND_LOCK_TIMEOUT_S`` -- but ONLY while
    the failure is lock CONTENTION (``_APPEND_LOCK_CONTENTION_ERRNOS``); any other
    ``OSError`` re-raises on the first attempt. Either way the raise comes BEFORE any byte
    is written.
    """
    fd = fh.fileno()
    deadline = time.monotonic() + _APPEND_LOCK_TIMEOUT_S
    while True:
        try:
            _append_lock_try(fd)
            return
        except OSError as exc:
            if (exc.errno not in _APPEND_LOCK_CONTENTION_ERRNOS
                    or time.monotonic() >= deadline):
                raise
            time.sleep(_APPEND_LOCK_SPIN_S)


def _append_unlock(fh):
    """Release the append lock taken by ``_append_lock`` -- does not propagate an OS unlock
    failure when called on the live handle ``append_line_fsync`` supplies (a CLOSED handle's
    ``fileno()`` raises ``ValueError``, which is not caught: no caller passes one).

    Windows: the fd is ``os.lseek``'d to the IDENTICAL sentinel offset, then
    ``LK_UNLCK, 1`` -- the unlock region must be the locked region, byte for byte.
    POSIX: ``flock(LOCK_UN)``. An ``OSError`` here is SWALLOWED, deliberately: by the
    time this runs the record is written, flushed and (if enabled) fsynced, and the OS
    releases a byte-range lock when the handle is closed (measured: a second handle
    takes the sentinel lock immediately after the first is closed without unlocking).
    Raising would report a COMPLETED append (fsynced when enabled) as a failure -- and ``InteractionLog`` counts
    a raise as a dropped record, which is the one lie this helper must not tell.
    """
    try:
        fd = fh.fileno()
        if os.name == "nt":
            import msvcrt
            os.lseek(fd, _APPEND_LOCK_SENTINEL, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def append_line_fsync(path, line: str) -> None:
    """Append ``line`` to ``path`` as exactly one utf-8 record + one ``\\n``.

    Opens in append mode (utf-8, ``newline="\\n"``), takes the per-file append lock
    (``_append_lock`` -- a byte-range lock on the SENTINEL byte ``_APPEND_LOCK_SENTINEL``,
    never on data), seeks to the end, writes the line with exactly one trailing newline,
    flushes, fsyncs (if enabled), releases the lock.

    PER-RECORD ATOMIC ACROSS COOPERATING WRITERS -- processes or threads that append
    THROUGH THIS FUNCTION -- on a LOCAL Windows filesystem, and that is a MEASURED claim
   : without the lock, Windows'
    O_APPEND is seek-then-write and 8 writers x 1000 appends LOST 5104 of 8000 rows
    (1471 torn), and a reader looping load() beside them RAISED on every read it made
    during contention (a torn INTERIOR line); with it, 8000 of 8000, 0 torn, and the
    same reader made 198 reads with 0 exceptions. A writer that does NOT take this lock
    (a foreign process appending raw) is not serialized by it. A NETWORK SHARE (SMB / UNC /
    NAS) is UNMEASURED: byte-range locking and flush there depend on the server.

    WHAT IS AND IS NOT CLAIMED. A lock that cannot be taken within _APPEND_LOCK_TIMEOUT_S
    raises OSError BEFORE any byte is written; so does a NON-contention lock failure, at
    once. The cost of a wedge is paid PER APPEND: while a peer holds the lock (suspended,
    or stuck in fsync on a stalled disk) EVERY append waits the full timeout and then
    raises -- there is no memory of the wedge. A fault DURING write / flush / fsync (disk
    full, a kill between the write and the newline) can still leave a torn FINAL line --
    the lock prevents INTERLEAVING, it does not roll bytes back. The readers' torn-final-line
    tolerance is KEPT, not replaced, and it covers a torn line that fails JSON DECODING
    only: a tail torn INSIDE a multi-byte UTF-8 sequence (a non-ASCII record cut between
    its bytes) makes ``artifact_sink.load_manifest``, ``journal.load`` and
    ``interaction_log.load`` raise ``UnicodeDecodeError`` (pre-existing, the readers are
    outside this fix; measured by constructing the tail, not by a live race). POSIX: ``fcntl.flock`` (whole-file,
    advisory, readers never blocked) -- REASONED, not measured. Append-only; no rewrite
    strategy.

    WARNING: callers MUST NOT pass a string with raw embedded newlines — an
    interior ``\\n`` splits one logical record into multiple physical JSONL lines
    and corrupts the corpus. Route record bodies through ``json.dumps`` (which
    escapes ``\\n``) before calling this.
    """
    path = os.fspath(path)
    if line.endswith("\n"):
        line = line[:-1]
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        _append_lock(fh)
        try:
            fh.seek(0, os.SEEK_END)
            fh.write(line + "\n")
            fh.flush()
            if FSYNC_ENABLED:
                os.fsync(fh.fileno())
        finally:
            _append_unlock(fh)
