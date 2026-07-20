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
import math
import os
import re
import tempfile
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


def append_line_fsync(path, line: str) -> None:
    """Append ``line`` to ``path`` as exactly one utf-8 record + one ``\\n``.

    Opens in append mode (utf-8, ``newline="\\n"``), writes the line with exactly
    one trailing newline, flushes, then fsyncs (if enabled).

    WARNING: callers MUST NOT pass a string with raw embedded newlines — an
    interior ``\\n`` splits one logical record into multiple physical JSONL lines
    and corrupts the corpus. Route record bodies through ``json.dumps`` (which
    escapes ``\\n``) before calling this.
    """
    path = os.fspath(path)
    if line.endswith("\n"):
        line = line[:-1]
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(line + "\n")
        fh.flush()
        if FSYNC_ENABLED:
            os.fsync(fh.fileno())
